"""Гойдик: the chat's friend who roasts everyone, on top of the Gemini API.
The persona lives in prompt.txt.

Env:
    GEMINI_API_KEY          key from Google AI Studio (without it Гойдик is asleep)
    GEMINI_MODEL            model name, default gemini-3.8-flash
    GEMINI_FALLBACK_MODEL   asked when the main model is overloaded or out of quota,
                            default gemini-3.5-flash-lite; empty value turns it off
"""
import asyncio
import copy
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("goydik")

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK = "gemini-3.5-flash-lite"
# Overloaded (503), internal error (500) or this model's free quota is over (429):
# another model may still answer
RETRY_STATUSES = {429, 500, 503}
HISTORY_LIMIT = 20  # messages (user + model) remembered per chat
MAX_INPUT = 2000
MAX_REPLY = 4000  # Telegram limit is 4096

PROMPT_FILE = Path(__file__).with_name("prompt.txt")

# How the bot packs messages for Gemini. Lives here, not in prompt.txt,
# so editing the persona can't break it
FORMAT_NOTE = """
ФОРМАТ СООБЩЕНИЙ
- Сообщение тебе приходит в виде «Имя (@username): текст». Свой ответ именем не подписывай.
- В групповом чате перед ним может быть блок «Последние сообщения в чате»: о чём болтали,
  пока тебя не позвали. Используй его, чтобы понять, о чём речь, но отвечай только
  на сообщение тебе и не пересказывай переписку.
- Строки «Гойдик: ...» в этом блоке — твои прошлые ответы, не повторяй их шутки.
- «Вася (@vasya) → Лена: ...» значит, что Вася ответил на сообщение Лены.
- «[фото]», «[видео]», «[стикер 😂]» — что человек скинул. Картинки и видео ты не видишь.
"""

PROMPT = PROMPT_FILE.read_text(encoding="utf-8").strip() + "\n" + FORMAT_NOTE

BLOCKED = "не-не, про это я даже бухой говорить не буду 🙅"
TIRED = "гойдик перебрал, дай ему минутку отдышаться 🥴"
BROKEN = "гойдик упал под стол, попробуй чуть позже 😵"
SLOW = "гойдик задумался и уснул прямо за столом 😴 спроси ещё раз"

TIMEOUT = 30  # seconds per request
HEDGE_AFTER = 8  # no answer by then: ask the next model too and take whichever answers first
BUSY_FOR = 300  # an overloaded model goes to the back of the queue for this many seconds
DAILY_QUOTA_PAUSE = 3600  # out of free requests for today: re-check the model hourly
THINKING_LEVEL = "low"  # the lowest level gemini-3.8-flash allows

busy_until = {}  # model -> time.monotonic() when it's worth asking it first again


class GoydikError(Exception):
    """Text for the user about why Гойдик didn't answer."""


class TryNext(Exception):
    """This model can't answer right now, another one may."""

    def __init__(self, user_text):
        super().__init__(user_text)
        self.user_text = user_text


def enabled():
    return bool(os.environ.get("GEMINI_API_KEY"))


def parse_reply(data):
    if (data.get("promptFeedback") or {}).get("blockReason"):
        raise GoydikError(BLOCKED)
    candidates = data.get("candidates") or []
    if not candidates:
        raise GoydikError(BLOCKED)
    cand = candidates[0]
    parts = (cand.get("content") or {}).get("parts") or []
    # Thinking models may return their reasoning as parts marked "thought"
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        if cand.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
            raise GoydikError(BLOCKED)
        log.warning("empty answer, finishReason=%s", cand.get("finishReason"))
        raise GoydikError(BROKEN)
    if text.lower().startswith("гойдик:"):
        text = text.split(":", 1)[1].strip()
    return text[:MAX_REPLY]


async def ask(history, author, text, recent=None, transport=None):
    """Send `author: text` with the chat history; on success both sides are added to history.
    `recent`: the last lines of a group chat ("Имя: текст"), passed along as context."""
    message = f"{author}: {text[:MAX_INPUT]}"
    if recent:
        message = (
            "Последние сообщения в чате, для контекста:\n" + "\n".join(recent)
            + "\n\nСообщение тебе:\n" + message
        )
    user_turn = {"role": "user", "parts": [{"text": message}]}
    body = {
        "system_instruction": {"parts": [{"text": PROMPT}]},
        "contents": history + [user_turn],
        "generationConfig": {
            "temperature": 1.1,
            "maxOutputTokens": 2048,
            # 3.x Flash models think at "medium" by default, which can take a minute on a long
            # group context; a drunk friend doesn't need deep reasoning
            "thinkingConfig": {"thinkingLevel": THINKING_LEVEL},
        },
        # Friendly swearing sometimes trips the default harassment filter
        "safetySettings": [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"}],
    }
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}

    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        answer = await race(client, model_queue(), body, headers)
    history.append(user_turn)
    history.append({"role": "model", "parts": [{"text": answer}]})
    del history[:-HISTORY_LIMIT]
    return answer


def model_queue():
    """Main model, then fallback; models that were overloaded recently go last."""
    models = [os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL]
    fallback = os.environ.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK)
    if fallback and fallback not in models:
        models.append(fallback)
    now = time.monotonic()
    return sorted(models, key=lambda m: busy_until.get(m, 0) > now)  # stable: order kept otherwise


async def race(client, models, body, headers):
    """Ask models[0]; if it fails, or is silent for HEDGE_AFTER seconds, ask the next one too.
    The first proper answer wins, the rest are cancelled."""
    queue = list(models)
    running = set()
    error = BROKEN

    def start_next():
        model = queue.pop(0)
        running.add(asyncio.create_task(attempt(client, model, copy.deepcopy(body), headers)))

    start_next()
    try:
        while running:
            wait = HEDGE_AFTER if queue else None
            done, _ = await asyncio.wait(running, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                start_next()  # current model is taking too long, don't just wait for it
                continue
            for task in done:
                running.discard(task)
                try:
                    return task.result()
                except TryNext as e:
                    error = e.user_text
                    if queue and not running:
                        start_next()
        raise GoydikError(error)
    finally:
        for task in running:
            task.cancel()


def error_info(r):
    """(one-line reason, seconds Google asks to wait or None) from a Gemini error response."""
    try:
        err = r.json().get("error") or {}
    except ValueError:
        return r.text[:200].replace("\n", " "), None
    reason = (err.get("message") or "").split("\n")[0][:200]
    wait, daily = None, False
    for detail in err.get("details") or []:
        kind = detail.get("@type", "")
        if kind.endswith("RetryInfo"):
            try:
                wait = float(detail.get("retryDelay", "").rstrip("s"))
            except ValueError:
                pass
        if kind.endswith("QuotaFailure"):
            quotas = [v.get("quotaId", "") for v in detail.get("violations") or []]
            reason += f" [{', '.join(quotas)}]"
            daily = daily or any("PerDay" in q for q in quotas)
    if daily:
        # Daily free quota is over: no point asking this model again soon,
        # whatever retryDelay says
        wait = max(wait or 0, DAILY_QUOTA_PAUSE)
    return reason, wait


async def attempt(client, model, body, headers):
    """One model's answer; TryNext if another model may still help, GoydikError if not."""
    url = API_URL.format(model=model)
    started = time.monotonic()
    try:
        r = await client.post(url, json=body, headers=headers)
        if r.status_code == 400 and "thinking" in r.text.lower():
            # This model doesn't know the level: ask with its defaults
            log.warning("model %s rejected thinkingLevel=%s, retrying without it", model, THINKING_LEVEL)
            del body["generationConfig"]["thinkingConfig"]
            r = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        log.warning("gemini (model %s) didn't answer in %ss", model, TIMEOUT)
        busy_until[model] = time.monotonic() + BUSY_FOR
        raise TryNext(SLOW)
    except httpx.TransportError as e:
        log.warning("gemini (model %s) connection failed: %r", model, e)
        raise TryNext(BROKEN)

    if r.status_code in RETRY_STATUSES:
        reason, wait = error_info(r)
        pause = max(BUSY_FOR, wait or 0)
        busy_until[model] = time.monotonic() + pause
        log.warning("gemini %s (model %s), asking it last for %ds: %s", r.status_code, model, pause, reason)
        raise TryNext(TIRED)
    if r.status_code == 404:
        log.error("gemini 404, no model %s: %s", model, error_info(r)[0])
        raise TryNext(BROKEN)
    if r.status_code != 200:
        # 400 "User location is not supported" and the like: another model won't help
        log.error("gemini %s (model %s): %s", r.status_code, model, error_info(r)[0])
        raise GoydikError(BROKEN)
    answer = parse_reply(r.json())
    log.info("answered by %s in %.1fs", model, time.monotonic() - started)
    return answer
