#!/usr/bin/env python3
"""Telegram bot: send it a picture / GIF / video / sticker, get back a GIF
that can be saved to "My GIFs".

Env:
    BOT_TOKEN       token from @BotFather (required)
    WEBHOOK_URL     public https URL of the service -> webhook mode
                    (on Render it's taken from RENDER_EXTERNAL_URL automatically);
                    if empty, the bot runs in polling mode (for local runs)
    PORT            port for webhook mode (Render sets it)
    ADMIN_CHAT_ID   chat id that receives /inc reports (send /id to the bot to learn yours)
"""
import asyncio
import hashlib
import logging
import os
import secrets
import tempfile
import time
from pathlib import Path

from telegram import BotCommand, MessageEntity, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from links import LinkError, download
from tg_gif import convert

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

MAX_DOWNLOAD = 20 * 1024 * 1024  # Bot API can't download larger files
MAX_GIF = 50 * 1024 * 1024  # Bot API upload limit
INC_COOLDOWN = 60  # seconds between reports from one user, so nobody can flood the admin

HELP = (
    "Кинь мне картинку, GIF, видео, стикер или ссылку на них, а я верну гифку, "
    "которую можно сохранить в «Мои GIF».\n\n"
    "Картинку можно отправить и сжатой, и файлом. Ссылки подходят с Pinterest, "
    "TikTok, X, Reddit и других сайтов, а также прямые ссылки на файлы. "
    "Из видео беру первые 20 секунд.\n\n"
    "Что-то сломалось? Напиши /inc и опиши проблему, а можно ответить командой /inc "
    "на сообщение, с которым что-то не так."
)
INC_USAGE = (
    "Опиши проблему после команды, например:\n"
    "/inc кинул ссылку на пин, бот ответил ошибкой\n\n"
    "Можно ответить командой /inc на сообщение, с которым что-то не так: я перешлю и его."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat id этого чата: {update.effective_chat.id}")


def describe_user(user):
    if user is None:
        return "неизвестно"
    parts = [user.full_name]
    if user.username:
        parts.append(f"@{user.username}")
    parts.append(f"id {user.id}")
    return ", ".join(parts)


async def report_incident(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = " ".join(context.args).strip()
    replied = msg.reply_to_message
    if not text and not replied:
        await msg.reply_text(INC_USAGE)
        return

    admin_chat = os.environ.get("ADMIN_CHAT_ID")
    if not admin_chat:
        log.warning("/inc used but ADMIN_CHAT_ID is not set")
        await msg.reply_text("Сейчас сообщить о проблеме нельзя: бот ещё не настроен 😿")
        return

    user = msg.from_user
    last = context.bot_data.setdefault("inc_last", {})
    now = time.monotonic()
    if user and now - last.get(user.id, -INC_COOLDOWN) < INC_COOLDOWN:
        await msg.reply_text("Ты только что отправил инцидент, подожди минуту.")
        return

    inc_id = secrets.token_hex(3)
    report = (
        f"🚨 Инцидент #{inc_id}\n"
        f"От: {describe_user(user)}\n"
        f"Чат: {msg.chat_id}\n"
        f"Время: {msg.date:%Y-%m-%d %H:%M} UTC\n\n"
        f"{text or '(без описания, см. пересланное сообщение)'}"
    )
    try:
        # Plain text, no parse_mode: user text can't break formatting
        await context.bot.send_message(admin_chat, report)
        if replied:
            await context.bot.forward_message(admin_chat, msg.chat_id, replied.message_id)
    except Exception:
        log.exception("failed to deliver incident %s", inc_id)
        await msg.reply_text("Не получилось отправить инцидент, попробуй позже 😿")
        return

    if user:
        last[user.id] = now
    log.info("incident #%s from %s", inc_id, describe_user(user))
    await msg.reply_text(f"Спасибо! Инцидент #{inc_id} отправлен, разберусь.")


def pick_file(msg):
    """Return (file object with file_id/file_size, suffix) for the incoming media."""
    if msg.photo:
        return msg.photo[-1], ".jpg"
    if msg.animation:
        return msg.animation, ".mp4"
    if msg.video:
        return msg.video, ".mp4"
    if msg.video_note:
        return msg.video_note, ".mp4"
    if msg.sticker:
        if msg.sticker.is_animated:
            return None, None  # .tgs (Lottie) isn't supported
        return msg.sticker, ".webm" if msg.sticker.is_video else ".webp"
    if msg.document:
        name = msg.document.file_name or "file"
        return msg.document, Path(name).suffix or ".bin"
    return None, None


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    media, suffix = pick_file(msg)
    if media is None:
        await msg.reply_text("Этот формат не поддерживается (анимированные стикеры .tgs пока не умею).")
        return
    if media.file_size and media.file_size > MAX_DOWNLOAD:
        await msg.reply_text("Файл больше 20 МБ: боты не могут скачивать такие файлы.")
        return

    await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_DOCUMENT)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Name the GIF by content so Telegram shows a sensible file name
        name = hashlib.md5(media.file_unique_id.encode()).hexdigest()[:8]
        src = tmp / f"gif_{name}{suffix}"
        tg_file = await context.bot.get_file(media.file_id)
        await tg_file.download_to_drive(src)
        await convert_and_reply(msg, src, tmp)


async def convert_and_reply(msg, src, tmp):
    try:
        dst = await asyncio.to_thread(convert, src, tmp, 2, 480, 10)
    except Exception:
        log.exception("conversion failed")
        await msg.reply_text("Не получилось сконвертировать этот файл 😿")
        return

    if dst.stat().st_size > MAX_GIF:
        await msg.reply_text("Гифка вышла больше 50 МБ, попробуй файл покороче или поменьше.")
        return
    with dst.open("rb") as f:
        await msg.reply_animation(f, filename=dst.name)


def find_url(msg):
    for ent in msg.entities or []:
        if ent.type == MessageEntity.TEXT_LINK:
            return ent.url
        if ent.type == MessageEntity.URL:
            return msg.parse_entity(ent)
    return None


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    url = find_url(msg)
    if not url:
        await start(update, context)
        return
    if "://" not in url:
        url = "https://" + url

    await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_DOCUMENT)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        try:
            src = await asyncio.to_thread(download, url, tmp)
        except LinkError as e:
            await msg.reply_text(str(e))
            return
        except Exception:
            log.exception("link download failed: %s", url)
            await msg.reply_text("Не получилось скачать по этой ссылке 😿")
            return
        name = hashlib.md5(url.encode()).hexdigest()[:8]
        src = src.rename(tmp / f"gif_{name}{src.suffix}")
        await convert_and_reply(msg, src, tmp)


async def set_commands(app):
    # The menu next to the input field in Telegram
    await app.bot.set_my_commands([
        BotCommand("help", "Что умеет бот"),
        BotCommand("inc", "Сообщить о проблеме"),
    ])


def main():
    token = os.environ["BOT_TOKEN"]
    # Python 3.14 no longer creates a loop implicitly, PTB expects one
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(token).post_init(set_commands).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("inc", report_incident))
    app.add_handler(CommandHandler("id", show_id))
    media = (
        filters.PHOTO | filters.ANIMATION | filters.VIDEO | filters.VIDEO_NOTE
        | filters.Sticker.ALL | filters.Document.ALL
    )
    app.add_handler(MessageHandler(media, handle_media))
    app.add_handler(MessageHandler(filters.Entity("url") | filters.Entity("text_link"), handle_link))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, start))

    webhook_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if webhook_url:
        # The secret path keeps strangers from posting fake updates
        path = hashlib.sha256(token.encode()).hexdigest()[:32]
        port = int(os.environ.get("PORT", 8080))
        log.info("webhook mode on port %s", port)
        app.run_webhook(
            listen="0.0.0.0", port=port, url_path=path,
            webhook_url=f"{webhook_url.rstrip('/')}/{path}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("polling mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
