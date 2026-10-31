#!/usr/bin/env python3
"""Telegram bot: send it a picture / GIF / video / sticker / link, get back a GIF
that can be saved to "My GIFs". Plain text goes to Гойдик (Gemini).

In private chats everything works without commands. In groups the bot only
reacts to commands: /gif, /goydik, /inc, /help, /id.

Env:
    BOT_TOKEN       token from @BotFather (required)
    WEBHOOK_URL     public https URL of the service -> webhook mode
                    (on Render it's taken from RENDER_EXTERNAL_URL automatically);
                    if empty, the bot runs in polling mode (for local runs)
    PORT            port for webhook mode (Render sets it)
    ADMIN_CHAT_ID   chat or channel id that receives /inc reports (send /id there to learn it)
    GEMINI_API_KEY  enables Гойдик, see goydik.py
    GIF_STORAGE_CHAT_ID  chat where Гойдик keeps the group GIFs he learned, by default
                    ADMIN_CHAT_ID, see gifs.py
"""
import asyncio
import hashlib
import logging
import os
import re
import secrets
import tempfile
import time
from collections import deque
from pathlib import Path

from telegram import BotCommand, Chat, MessageEntity, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import goydik
from gifs import MAX_SIZE as MAX_GIF_TO_LEARN, STORE as GIFS
from links import LinkError, download
from tg_gif import convert

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

MAX_DOWNLOAD = 20 * 1024 * 1024  # Bot API can't download larger files
MAX_GIF = 50 * 1024 * 1024  # Bot API upload limit
INC_COOLDOWN = 60  # seconds between reports from one user, so nobody can flood the admin

HELP = (
    "Я умею две вещи.\n\n"
    "🎞 Гифки. Кинь картинку, GIF, видео, стикер или ссылку на них (Pinterest, TikTok, X, "
    "Reddit и другие сайты, прямые ссылки на файлы), а я верну гифку, которую можно "
    "сохранить в «Мои GIF». Из видео беру первые 20 секунд.\n\n"
    "🍺 Гойдик. Просто напиши мне что-нибудь, и ответит Гойдик, свой пацан, который всех подъёбывает.\n\n"
    # {username} is filled from Telegram, so the text follows the bot whatever it's called
    "В групповых чатах отметь меня (@{username} вопрос), и ответит Гойдик. "
    "Если отметить меня в ответ на чьё-то сообщение, Гойдик его прокомментирует. "
    "А чтобы продолжить разговор, просто ответь на сообщение Гойдика.\n"
    "Остальное в группах — командами:\n"
    "/gif — ответь этой командой на картинку, видео или ссылку, или напиши /gif ссылка\n"
    "/goydik вопрос — тоже спросить Гойдика\n"
    "/inc описание — сообщить о проблеме с ботом"
)
GIF_USAGE = (
    "Ответь командой /gif на сообщение с картинкой, видео, стикером или ссылкой, "
    "или напиши /gif ссылка"
)
GOYDIK_USAGE = "спроси чё-нибудь, бро: /goydik как дела\nили ответь /goydik на сообщение 🍺"
GOYDIK_ASLEEP = "Гойдик сейчас спит 😴 (не настроен ключ Gemini)"
INC_USAGE = (
    "Опиши проблему после команды, например:\n"
    "/inc кинул ссылку на пин, бот ответил ошибкой\n\n"
    "Можно ответить командой /inc на сообщение, с которым что-то не так: я перешлю и его."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP.format(username=context.bot.username))


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # effective_message: in a channel the command arrives as channel_post, not message
    await update.effective_message.reply_text(f"chat id этого чата: {update.effective_chat.id}")


# --- GIFs -----------------------------------------------------------------------------


def pick_file(msg):
    """Return (file object with file_id/file_size, suffix) for the media in msg."""
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


def find_url(msg):
    for ent, text in list(msg.parse_entities().items()) + list(msg.parse_caption_entities().items()):
        if ent.type == MessageEntity.TEXT_LINK:
            return ent.url
        if ent.type == MessageEntity.URL:
            return text
    return None


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


async def gif_from_media(msg, media, suffix, context):
    """Convert `media` and reply to `msg` (they may be different messages, e.g. /gif as a reply)."""
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


async def gif_from_url(msg, url, context):
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


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    media, suffix = pick_file(msg)
    if media is None:
        await msg.reply_text("Этот формат не поддерживается (анимированные стикеры .tgs пока не умею).")
        return
    await gif_from_media(msg, media, suffix, context)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    url = find_url(msg)
    if not url:
        await start(update, context)
        return
    await gif_from_url(msg, url, context)


async def gif_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    url = find_url(msg)  # /gif <link>
    if url:
        await gif_from_url(msg, url, context)
        return

    src = msg.reply_to_message
    if src:
        media, suffix = pick_file(src)
        if media:
            await gif_from_media(msg, media, suffix, context)
            return
        url = find_url(src)
        if url:
            await gif_from_url(msg, url, context)
            return
        if src.sticker:
            await msg.reply_text("Анимированные стикеры .tgs пока не умею 😿")
            return
    await msg.reply_text(GIF_USAGE)


# --- Гойдик ---------------------------------------------------------------------------


def command_text(msg):
    """Text after the command: '/goydik@bot как дела' -> 'как дела' (keeps line breaks)."""
    parts = (msg.text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


RECENT_LIMIT = 30  # group messages kept as context for Гойдик
RECENT_MAX_CHARS = 500

MEDIA_MARKS = [
    ("photo", "[фото]"), ("video", "[видео]"), ("animation", "[гифка]"), ("video_note", "[кружок]"),
    ("voice", "[голосовое]"), ("audio", "[музыка]"), ("document", "[файл]"), ("poll", "[опрос]"),
]


def display_name(user):
    """'Артём Стажер (@Blaugranass)': people.txt finds people in its dossier by name or username."""
    if user is None:
        return "кто-то"
    return f"{user.full_name} (@{user.username})" if user.username else user.full_name


def remember(chat_data, message_id, who, text):
    recent = chat_data.setdefault("recent", deque(maxlen=RECENT_LIMIT))
    recent.append({"id": message_id, "who": who, "text": text[:RECENT_MAX_CHARS]})


def forget(chat_data, message_id):
    recent = chat_data.get("recent")
    if recent:
        for entry in [e for e in recent if e["id"] == message_id]:
            recent.remove(entry)


def describe_message(msg, username):
    """One line of text for the context log: text, caption and a mark for media."""
    parts = [mark for attr, mark in MEDIA_MARKS if getattr(msg, attr)]
    if msg.sticker:
        parts.append(f"[стикер {msg.sticker.emoji or ''}]".replace(" ]", "]"))
    text = strip_mention(msg.text or msg.caption, username)
    if text:
        parts.append(text)
    return " ".join(parts)


async def record_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keep the last messages of a group in memory, so Гойдик knows what's going on.
    Needs privacy mode off in @BotFather, otherwise Telegram sends only commands and tags."""
    msg = update.message
    if (msg.text or "").startswith("/"):
        return  # commands are noise for the conversation
    line = describe_message(msg, context.bot.username)
    if not line:
        return  # service messages: someone joined, the title changed...
    who = display_name(msg.from_user)
    replied = msg.reply_to_message
    if replied and replied.from_user:
        # "Вася → Лена": names can't be declined automatically, an arrow reads fine
        target = "Гойдик" if replied.from_user.username == context.bot.username else replied.from_user.full_name
        who = f"{who} → {target}"
    remember(context.chat_data, msg.message_id, who, line)
    if msg.animation and goydik.enabled():
        context.application.create_task(learn_gif(context.bot, context.chat_data, msg))


async def learn_gif(bot, chat_data, msg):
    """Remember a GIF sent to the group, with Gemini's description of what's on it: Гойдик
    can send it back later, and the chat log shows «[гифка: ...]» instead of a bare «[гифка]»."""
    about = await gif_about(bot, msg.chat_id, msg.animation, msg.caption)
    if not about:
        return
    for entry in chat_data.get("recent", ()):
        if entry["id"] == msg.message_id:
            entry["text"] = entry["text"].replace("[гифка]", f"[гифка: {about}]", 1)


GIF_TIMEOUT = 30  # seconds for Telegram to hand over a GIF: the default 5 is often too little


async def gif_about(bot, chat_id, anim, caption=None):
    """What's on a GIF: from the collection, or asked from Gemini now and remembered.
    None if it couldn't be described."""
    gif = GIFS.find(chat_id, anim.file_unique_id)
    if gif:
        return gif["about"]
    if (anim.file_size or 0) > MAX_GIF_TO_LEARN or not goydik.enabled():
        return None
    async with GIFS.describing:
        # The same GIF may have been described while this one waited: one GIF is both
        # learned in the background and answered to when it's sent to Гойдик
        gif = GIFS.find(chat_id, anim.file_unique_id)
        if gif:
            return gif["about"]
        hint = " ".join(filter(None, [caption, anim.file_name]))
        try:
            tg_file = await bot.get_file(anim.file_id, read_timeout=GIF_TIMEOUT)
            data = await tg_file.download_as_bytearray(read_timeout=GIF_TIMEOUT)
            about = await goydik.describe_gif(bytes(data), anim.mime_type or "video/mp4", hint)
        except Exception:
            log.warning("couldn't describe a GIF", exc_info=True)
            return None
        GIFS.add(chat_id, anim.file_id, anim.file_unique_id, about)
    GIFS.schedule_save(bot)
    return about


async def replied_gif(bot, msg):
    """Description of the GIF `msg` replies to, if it does."""
    replied = msg.reply_to_message
    if replied and replied.animation:
        return await gif_about(bot, msg.chat_id, replied.animation, replied.caption)
    return None


def recent_lines(chat_data, exclude_id):
    return [f"{m['who']}: {m['text']}" for m in chat_data.get("recent", ()) if m["id"] != exclude_id]


async def keep_typing(bot, chat_id):
    """Telegram shows "typing..." for 5 seconds, and Gemini often thinks longer."""
    while True:
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            log.warning("couldn't send typing action", exc_info=True)
        await asyncio.sleep(4)


async def talk(msg, text, context):
    if not goydik.enabled():
        await msg.reply_text(GOYDIK_ASLEEP)
        return
    author = display_name(msg.from_user)
    in_group = msg.chat.type != Chat.PRIVATE
    if in_group:
        # The group log already has earlier questions and Гойдик's answers
        history, recent = [], recent_lines(context.chat_data, exclude_id=msg.message_id)
    else:
        history, recent = context.chat_data.setdefault("goydik_history", []), None

    typing = asyncio.create_task(keep_typing(context.bot, msg.chat_id))
    ok = False
    try:
        # GIFs are collected in groups only, each group has its own
        gifs = GIFS.choices(msg.chat_id) if in_group else None
        reply = await goydik.ask(history, author, text, recent=recent, gifs=gifs)
        ok = True
    except goydik.GoydikError as e:
        reply = goydik.Reply(str(e))
    except Exception:
        log.exception("goydik failed")
        reply = goydik.Reply(goydik.BROKEN)
    finally:
        typing.cancel()
    if reply is None:
        # A "reply and you're a loser" trap: Гойдик doesn't take the bait. Drop it from the
        # group log too, or Gemini keeps seeing it and stays silent on every next message
        forget(context.chat_data, msg.message_id)
        return
    sent = await send_gif(msg, reply) if reply.gif else None
    if sent:
        said = f"[гифка: {reply.gif['about']}] {reply.text}".strip()
    else:
        said = reply.text or "🗿"  # a GIF without a caption that couldn't be sent
        sent = await msg.reply_text(said)
    if ok and in_group and sent:
        # The bot doesn't receive its own messages, so add the answer to the log by hand
        remember(context.chat_data, sent.message_id, "Гойдик", said)


async def send_gif(msg, reply):
    """Send the GIF Гойдик picked; None if Telegram no longer has it (then he answers with text)."""
    try:
        return await msg.reply_animation(reply.gif["id"], caption=reply.text or None)
    except Exception:
        log.warning("couldn't send a GIF from the collection", exc_info=True)
        return None


CALLED_EMPTY = "(тебя просто позвали, ничего не написав)"


def with_reply(text, replied, username, gif=None):
    """Add the message Гойдик was called on, so he knows what people are talking about.
    `gif`: the description of the GIF in that message, if there is one."""
    if not replied or not replied.from_user:
        return text
    if replied.from_user.username == username:
        return text  # reply to Гойдик himself: it's already in the history
    who = display_name(replied.from_user)
    if gif:
        caption = f" с подписью «{replied.caption}»" if replied.caption else ""
        seen = f"{who} кинул гифку{caption}: {gif}"
        return f"{text}\n(это в ответ на то, что {seen})" if text else f"чё скажешь: {seen}"
    quoted = replied.text or replied.caption
    if quoted:
        if not text:
            return f"чё скажешь про это сообщение от {who}: «{quoted}»"
        return f"{text}\n(это в ответ на сообщение от {who}: «{quoted}»)"
    if replied.photo or replied.video or replied.animation or replied.sticker or replied.video_note:
        # Гойдик only reads text
        seen = f"{who} показывает тебе картинку или видео, но ты его не видишь, глаза не фокусируются"
        return f"{text}\n({seen})" if text else f"({seen})"
    return text


def mentions_bot(msg, username):
    me = f"@{username}".lower()
    for ent, text in list(msg.parse_entities().items()) + list(msg.parse_caption_entities().items()):
        if ent.type == MessageEntity.MENTION and text.lower() == me:
            return True
    return False


def strip_mention(text, username):
    """'слышь @goydik_bot пиво будешь' -> 'слышь пиво будешь'."""
    text = re.sub(rf"@{re.escape(username)}\b", "", text or "", flags=re.I)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


async def goydik_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    gif = await replied_gif(context.bot, msg)
    text = with_reply(command_text(msg), msg.reply_to_message, context.bot.username, gif)
    if not text:
        await msg.reply_text(GOYDIK_USAGE)
        return
    await talk(msg, text, context)


def replies_to_goydik(msg, username):
    """A reply to one of Гойдик's text answers (replies to the bot's GIFs don't count)."""
    replied = msg.reply_to_message
    return bool(
        replied and replied.from_user and replied.from_user.username == username
        and replied.text and not replied.animation
    )


async def group_goydik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """In groups Гойдик answers when the bot is tagged ('@goydik_bot как дела')
    or when someone replies to his message."""
    msg = update.message
    username = context.bot.username
    if not (mentions_bot(msg, username) or replies_to_goydik(msg, username)):
        return  # someone else was tagged or answered
    text = strip_mention(msg.text or msg.caption, username)
    if msg.animation:
        # A GIF sent to Гойдик: tell him what's on it, or he says he can't see it
        about = await gif_about(context.bot, msg.chat_id, msg.animation, msg.caption)
        mark = f"[гифка: {about}]" if about else "[гифка]"
        text = f"{mark} {text}".strip()
    text = with_reply(text, msg.reply_to_message, username, await replied_gif(context.bot, msg))
    # A reply with just a sticker or photo: tell Гойдик what it was
    text = text or describe_message(msg, username)
    # Just "@bot" with nothing else: Гойдик reacts to being called
    await talk(msg, text or CALLED_EMPTY, context)


async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not goydik.enabled():
        await start(update, context)
        return
    await talk(msg, strip_mention(msg.text, context.bot.username) or CALLED_EMPTY, context)


# --- Incidents ------------------------------------------------------------------------


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


# --- Setup ----------------------------------------------------------------------------


async def startup(app):
    # The menu next to the input field in Telegram
    await app.bot.set_my_commands([
        BotCommand("gif", "Сделать гифку из картинки, видео или ссылки"),
        BotCommand("goydik", "Спросить Гойдика"),
        BotCommand("inc", "Сообщить о проблеме"),
        BotCommand("help", "Что умеет бот"),
    ])
    await GIFS.load(app.bot)


async def shutdown(app):
    # Render stops the instance when it falls asleep: save GIFs learned in the last seconds
    await GIFS.flush(app.bot)


def build_app(token):
    # A few updates at once, so one long video doesn't block everyone else.
    # Not more: parallel ffmpeg runs would eat the free instance's memory
    app = (
        Application.builder().token(token).post_init(startup).post_shutdown(shutdown)
        .concurrent_updates(4).build()
    )
    # Only new messages. Without this the bot would also answer posts in the incidents
    # channel and crash on edited messages (update.message is None there)
    new_msg = filters.UpdateType.MESSAGE
    # Groups get commands and @mentions; everything else works only in private chats
    private = new_msg & filters.ChatType.PRIVATE
    groups = new_msg & filters.ChatType.GROUPS
    mention = filters.Entity("mention") | filters.CaptionEntity("mention")

    # Group -1 runs before the handlers below and doesn't stop them
    app.add_handler(MessageHandler(groups, record_group_message), group=-1)

    app.add_handler(CommandHandler(["start", "help"], start, filters=new_msg))
    app.add_handler(CommandHandler("gif", gif_command, filters=new_msg))
    app.add_handler(CommandHandler(["goydik", "g"], goydik_command, filters=new_msg))
    app.add_handler(CommandHandler("inc", report_incident, filters=new_msg))
    app.add_handler(CommandHandler("id", show_id, filters=new_msg | filters.UpdateType.CHANNEL_POST))
    media = (
        filters.PHOTO | filters.ANIMATION | filters.VIDEO | filters.VIDEO_NOTE
        | filters.Sticker.ALL | filters.Document.ALL
    )
    # One handler for tags and replies: both may come in one message, and only the first
    # matching handler of a group runs
    app.add_handler(MessageHandler(groups & (mention | filters.REPLY) & ~filters.COMMAND, group_goydik))
    app.add_handler(MessageHandler(private & media, handle_media))
    app.add_handler(MessageHandler(private & (filters.Entity("url") | filters.Entity("text_link")), handle_link))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, private_text))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, start))
    return app


def main():
    token = os.environ["BOT_TOKEN"]
    # Python 3.14 no longer creates a loop implicitly, PTB expects one
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = build_app(token)

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
