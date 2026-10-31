"""Гойдик's GIFs: the ones people send in a group, remembered so he can send them back.

A bot can resend any file it has seen by its file_id, so a GIF is downloaded only once,
for Gemini to describe what's on it. The collection lives as gifs.json pinned in a storage
chat (GIF_STORAGE_CHAT_ID, by default ADMIN_CHAT_ID): Render's free instance forgets
everything when it falls asleep. Without a storage chat the GIFs are kept until a restart.
"""
import asyncio
import io
import json
import logging
import os
import time

from telegram import InputMediaDocument

log = logging.getLogger("gifs")

FILE_NAME = "gifs.json"
PER_CHAT = 150  # GIFs kept per chat, the oldest go first
IN_PROMPT = 60  # the newest ones Гойдик chooses from: each costs prompt tokens
MAX_SIZE = 8 * 1024 * 1024  # bigger files aren't worth downloading for a description
SAVE_DELAY = 30  # seconds: a burst of GIFs is saved once


class GifStore:
    def __init__(self):
        self.chats = {}  # str(chat_id) -> [{"id": file_id, "uid": file_unique_id, "about", "added"}]
        self.storage = os.environ.get("GIF_STORAGE_CHAT_ID") or os.environ.get("ADMIN_CHAT_ID")
        self.message_id = None  # the pinned gifs.json in the storage chat
        self.save_task = None
        # One description at a time: a batch of GIFs would hit the per-minute quota
        self.describing = asyncio.Lock()

    def find(self, chat_id, uid):
        return next((g for g in self.chats.get(str(chat_id), []) if g["uid"] == uid), None)

    def add(self, chat_id, file_id, uid, about):
        gifs = self.chats.setdefault(str(chat_id), [])
        gifs.append({"id": file_id, "uid": uid, "about": about, "added": int(time.time())})
        del gifs[:-PER_CHAT]

    def choices(self, chat_id):
        return self.chats.get(str(chat_id), [])[-IN_PROMPT:]

    async def load(self, bot):
        """Read gifs.json pinned in the storage chat, if it's there."""
        if not self.storage:
            log.warning("no GIF_STORAGE_CHAT_ID or ADMIN_CHAT_ID: GIFs are forgotten on restart")
            return
        try:
            pinned = (await bot.get_chat(self.storage)).pinned_message
            if not (pinned and pinned.document and pinned.document.file_name == FILE_NAME):
                log.info("no pinned %s in the storage chat yet", FILE_NAME)
                return
            data = await (await bot.get_file(pinned.document.file_id)).download_as_bytearray()
            self.chats = json.loads(bytes(data).decode("utf-8"))["chats"]
            self.message_id = pinned.message_id
            log.info("loaded %d GIFs", sum(len(g) for g in self.chats.values()))
        except Exception:
            log.exception("couldn't load the GIFs from the storage chat")

    def schedule_save(self, bot):
        if self.storage and not (self.save_task and not self.save_task.done()):
            self.save_task = asyncio.create_task(self.save_later(bot))

    async def save_later(self, bot):
        await asyncio.sleep(SAVE_DELAY)
        await self.save(bot)

    async def flush(self, bot):
        """Save now if a save is waiting."""
        if self.save_task and not self.save_task.done():
            self.save_task.cancel()
            await self.save(bot)

    async def save(self, bot):
        """Replace the pinned gifs.json with the current collection (or post and pin it)."""
        data = json.dumps({"chats": self.chats}, ensure_ascii=False).encode("utf-8")
        try:
            if self.message_id:
                try:
                    await bot.edit_message_media(
                        InputMediaDocument(io.BytesIO(data), filename=FILE_NAME),
                        chat_id=self.storage, message_id=self.message_id,
                    )
                    return
                except Exception:
                    log.warning("couldn't update the pinned %s, posting a new one", FILE_NAME, exc_info=True)
            sent = await bot.send_document(
                self.storage, io.BytesIO(data), filename=FILE_NAME, disable_notification=True,
                caption="Гифки Гойдика. Не удаляй и не открепляй, иначе он их забудет",
            )
            self.message_id = sent.message_id
            await bot.pin_chat_message(self.storage, sent.message_id, disable_notification=True)
        except Exception:
            log.exception("couldn't save the GIFs to the storage chat (the bot needs the right to pin there)")


STORE = GifStore()
