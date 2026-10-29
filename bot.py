#!/usr/bin/env python3
"""Telegram bot: send it a picture / GIF / video / sticker, get back a GIF
that can be saved to "My GIFs".

Env:
    BOT_TOKEN       token from @BotFather (required)
    WEBHOOK_URL     public https URL of the service -> webhook mode
                    (on Render it's taken from RENDER_EXTERNAL_URL automatically);
                    if empty, the bot runs in polling mode (for local runs)
    PORT            port for webhook mode (Render sets it)
"""
import asyncio
import hashlib
import logging
import os
import tempfile
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from tg_gif import convert

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

MAX_DOWNLOAD = 20 * 1024 * 1024  # Bot API can't download larger files
MAX_GIF = 50 * 1024 * 1024  # Bot API upload limit

HELP = (
    "Кинь мне картинку, GIF, видео или стикер, а я верну гифку, "
    "которую можно сохранить в «Мои GIF».\n\n"
    "Картинку можно отправить и сжатой, и файлом."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


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


def main():
    token = os.environ["BOT_TOKEN"]
    # Python 3.14 no longer creates a loop implicitly, PTB expects one
    asyncio.set_event_loop(asyncio.new_event_loop())
    app =Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    media = (
        filters.PHOTO | filters.ANIMATION | filters.VIDEO | filters.VIDEO_NOTE
        | filters.Sticker.ALL | filters.Document.ALL
    )
    app.add_handler(MessageHandler(media, handle_media))
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
