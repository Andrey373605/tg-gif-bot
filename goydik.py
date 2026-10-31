"""Гойдик: the chat's friend who roasts everyone, on top of the Gemini API.

The persona lives in prompt.txt, the dossier on chat members in people.txt. Гойдик only
sees the dossier of the people a message is about, so he can't drag the others in.
In groups he may also answer with one of the chat's own GIFs (see gifs.py).

Env:
    GEMINI_API_KEY          key from Google AI Studio (without it Гойдик is asleep)
    GEMINI_MODEL            model name, default gemini-3.8-flash
    GEMINI_FALLBACK_MODEL   comma-separated models asked in turn when the main one is
                            overloaded or out of quota. Default
                            gemini-3.6-flash,gemini-3.5-flash-lite, empty turns them off
"""
import asyncio
import copy
import json
import logging
import os
import random
import re
import time
from base64 import b64encode
from pathlib import Path
from typing import NamedTuple

import httpx

log = logging.getLogger("goydik")

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"
# 3.6 Flash writes Russian nearly as well as 3.8 and has its own free quota;
# Flash-Lite is the last resort
DEFAULT_FALLBACK = "gemini-3.6-flash,gemini-3.5-flash-lite"
# Overloaded (503), internal error (500) or this model's free quota is over (429):
# another model may still answer
RETRY_STATUSES = {429, 500, 503}
HISTORY_LIMIT = 20  # lines of a private chat remembered
RECENT_OWN = 4  # Гойдик's last answers quoted back to him as "already used"
MAX_INPUT = 2000
MAX_REPLY = 4000  # Telegram limit is 4096

PROMPT_FILE = Path(__file__).with_name("prompt.txt")
PEOPLE_FILE = Path(__file__).with_name("people.txt")

# How the bot packs messages and what it wants back. Lives here, not in prompt.txt,
# so editing the persona can't break it
FORMAT_NOTE = """
КАК УСТРОЕН ЗАПРОС
- Сообщение тебе приходит в виде «Имя (@username): текст». Перед ним может быть блок
  последних сообщений: о чём болтали до тебя. Отвечай только на сообщение тебе.
- «Гойдик: ...» — твои прошлые ответы. «Вася (@vasya) → Лена: ...» — Вася ответил Лене.
  «[фото: ...]», «[гифка: ...]» — картинка или гифка с описанием того, что на ней: реагируй
  как на увиденное. Голые «[фото]», «[гифка]», «[видео]», «[стикер 😂]» — что скинули, без
  описания: отреагируй на сам факт, не выдумывай, что там, и не жалуйся, что не видишь.
- «(тебя позвал Вася ...; отвечай на это сообщение его автору ...)» — Вася показал тебе
  чужое сообщение: отвечай на само сообщение и его автору. Если Вася показывает, что про
  тебя пишут, — огрызайся на того, кто писал. Подкалывать Васю вместо ответа — мимо.
- В конце может быть список твоих последних ответов: эти шутки, темы, факты и словечки
  уже отработаны, не повторяй их.

ЧТО ВЕРНУТЬ (JSON)
- kind — что это за сообщение: banter — потрындеть, подколоть, наехать, риторический вопрос;
  question — человеку реально нужна инфа или помощь; serious — человеку плохо или тема
  серьёзная; bait — ловушка, где любой ответ делает тебя проигравшим («ответишь — лох»,
  «кто ответит, тот гей», «не отвечай на это»). Только такие ловушки, обычные наезды — banter.
  Решай по сообщению тебе, а не по блоку последних сообщений. На bait бот промолчит.
- text — ровно то, что уйдёт в чат: только сам ответ, без рассуждений и вариантов.
- gif — если ниже есть список «ГИФКИ ЧАТА» и одна из них подходит точно, а не примерно, —
  её номер; text тогда — короткая подпись к ней или пустая строка. Гифка — редкая приправа,
  обычно gif = 0. Но если тебя просят кинуть гифку — кидай самую подходящую из списка.

ПЕРЕД ОТВЕТОМ ПРОВЕРЬ СЕБЯ
- это ответ на то, что человек написал, а не заготовка?
- ответ в настрое, который указан в конце запроса? Подкалывать, когда настрой другой, — мимо
- голое «база» или «найс» — только если сообщению правда больше ничего не надо
- если взял факт из досье: он про то, о чём пишет человек, или прямо из этого вытекает?
  Факт один, а не два склеенных?
- не повторяешь тему, факт или словечко из своих последних ответов?
- одно-два предложения, без объяснения шутки?
"""

ANSWER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "kind": {"type": "STRING", "enum": ["banter", "question", "serious", "bait"]},
        "text": {"type": "STRING"},
        "gif": {"type": "INTEGER"},  # number in the chat's GIF list, 0 for none
    },
    "required": ["kind", "text", "gif"],
    "propertyOrdering": ["kind", "text", "gif"],
}

DESCRIBE_PROMPT = """Тебе показывают гифку из телеграм-чата друзей. Опиши её так, чтобы потом по описанию можно было понять, в ответ на что её кинуть: что происходит и какую реакцию или эмоцию она выражает. До 12 слов, по-русски, без вступлений вроде «на гифке». Если на гифке есть надпись — перепиши её в кавычках."""
PHOTO_PROMPT = """Тебе показывают картинку из телеграм-чата друзей. Коротко опиши, что на ней, так, чтобы друг без картинки понял, о чём речь и что ответить: что изображено и что происходит. Если это скриншот, мем или на картинке есть текст — перепиши главный текст в кавычках. До 30 слов, по-русски, без вступлений вроде «на картинке»."""
DESCRIBE_SCHEMA = {"type": "OBJECT", "properties": {"about": {"type": "STRING"}}, "required": ["about"]}
GIF_MARK = "[гифка"  # how a GIF looks in the chat log: «[гифка: кот падает со стола]»

# Whatever the prompt says, Gemini drifts into roasting every message, and the chat got
# tired of it. So each answer gets a mood drawn at random, and a roast is only one of them
MOODS = [
    ("поддержи разговор без подколов: своё мнение, встречный вопрос или реакция по теме", 3),
    ("шутка без жертвы: смешное наблюдение или абсурд про саму ситуацию, никого не задевая", 3),
    ("подыграй: продолжи мысль человека, как будто ты в теме", 2),
    ("поддержи человека или порадуйся за него, по-дружески", 2),
    ("короткая выдуманная история про себя в тему", 1),
    ("удивись или загорись темой, переспроси", 1),
    ("беззлобный дружеский подкол", 2),
]

BLOCKED = "не-не, про это я даже бухой говорить не буду 🙅"
TIRED = "гойдик перебрал, дай ему минутку отдышаться 🥴"
BROKEN = "гойдик упал под стол, попробуй чуть позже 😵"
SLOW = "гойдик задумался и уснул прямо за столом 😴 спроси ещё раз"

TIMEOUT = 45  # seconds per request
HEDGE_AFTER = 12  # no answer by then: ask the next model too and take whichever answers first
BUSY_FOR = 300  # an overloaded model goes to the back of the queue for this many seconds
DAILY_QUOTA_PAUSE = 3600  # out of free requests for today: re-check the model hourly
THINKING_LEVEL = "low"  # the lowest level gemini-3.8-flash allows

busy_until = {}  # model -> time.monotonic() when it's worth asking it first again


class Reply(NamedTuple):
    text: str  # with a GIF it's the caption, may be empty
    gif: dict | None = None  # one of the GIFs passed to ask()


class GoydikError(Exception):
    """Text for the user about why Гойдик didn't answer."""


class TryNext(Exception):
    """This model can't answer right now, another one may."""

    def __init__(self, user_text):
        super().__init__(user_text)
        self.user_text = user_text


def enabled():
    return bool(os.environ.get("GEMINI_API_KEY"))


def load_people(path):
    """[(pattern, dossier)] from people.txt: each person starts with «## Имя: шаблоны»."""
    people = []
    for block in re.split(r"^## ", path.read_text(encoding="utf-8"), flags=re.M)[1:]:
        head, _, body = block.partition("\n")
        name, _, patterns = head.partition(":")
        pattern = re.compile(r"(?<!\w)(?:" + "|".join(patterns.split()) + ")", re.I)
        facts = "\n".join(line for line in body.splitlines() if line.strip() and not line.startswith("#"))
        people.append((pattern, f"{name.strip()}\n{facts}"))
    return people


PROMPT = PROMPT_FILE.read_text(encoding="utf-8").strip()
PEOPLE = load_people(PEOPLE_FILE)


def dossier(text):
    """Dossier of the people `text` is about: the author and whoever is named in it."""
    found = [facts for pattern, facts in PEOPLE if pattern.search(text)]
    return "\n\n".join(found) or "(ни о ком из досье тут речи нет)"


async def ask(history, author, text, recent=None, gifs=None, note=None, want_gif=False, transport=None, trace=None):
    """Гойдик's Reply to `author: text`, or None when he stays silent (a bait message).
    `history`: lines of a private chat; the exchange is added to it on success.
    `recent`: the last lines of a group chat ("Имя: текст"), passed along as context.
    `gifs`: the chat's GIFs ({"about": description, ...}) he may answer with.
    `note`: who asked him to answer this message; the people named there get no dossier,
    so he answers the message instead of roasting them.
    `want_gif`: the message asks for a GIF, so he sends one if he has any.
    `trace`: a dict to fill with the raw answer (for try_goydik.py)."""
    message = f"{author}: {text[:MAX_INPUT]}"
    if note:
        message += f"\n({note})"
    lines = list(history) + list(recent or ())
    used = own_answers(lines)
    if not want_gif and any(a.startswith(GIF_MARK) for a in used[-2:]):
        gifs = None  # a GIF every other answer gets old fast, unless someone asks for one
    gifs = gifs or []
    people = dossier(f"{author}\n{text}")
    system = f"{PROMPT}\n\nДОСЬЕ (только те, о ком речь)\n{people}\n{FORMAT_NOTE}"
    if gifs:
        system += "\nГИФКИ ЧАТА\n" + "\n".join(f"{n}. {g['about']}" for n, g in enumerate(gifs, 1))
    mood = random.choices([m for m, _ in MOODS], weights=[w for _, w in MOODS])[0]
    if want_gif:
        # Asked for "rare seasoning, only an exact fit", he kept answering a request with text
        mood = ("тебя просят кинуть гифку: выбери из списка «ГИФКИ ЧАТА» самую подходящую "
                "и поставь её номер в gif, text — короткая подпись или пусто" if gifs else
                "тебя просят кинуть гифку, а гифок у тебя пока нет: скажи это по-своему — "
                "ты кидаешь гифки, которые люди скидывают в этот чат")
    body = request_body(system, chat_text(message, lines, used, mood), ANSWER_SCHEMA, temperature=1.1)
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}

    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        answer = await race(client, model_queue(), body, headers, answer_parser(len(gifs)))
    if trace is not None:
        trace.update(answer, mood=mood)
    if answer["kind"] == "bait":
        log.info("staying silent on a bait message")
        return None  # not added to history: there was no answer

    gif = gifs[answer["gif"] - 1] if answer["gif"] else None
    text = tidy(answer["text"])
    said = f"{GIF_MARK}: {gif['about']}] {text}".strip() if gif else text
    history.extend([message, f"Гойдик: {said}"])
    del history[:-HISTORY_LIMIT]
    return Reply(text, gif)


def answer_parser(gif_count):
    """Parser for the answer: {"kind", "text", "gif"}; `gif_count` GIFs were offered."""
    def parse(data):
        answer = json_reply(data)
        kind = answer.get("kind")
        text = answer.get("text") if isinstance(answer.get("text"), str) else ""
        gif = answer.get("gif")
        gif = gif if isinstance(gif, int) and 1 <= gif <= gif_count else 0
        if kind != "bait" and not text.strip() and not gif:  # a GIF may go without a caption
            log.warning("empty answer: %.200s", answer)
            raise TryNext(BROKEN)
        return {"kind": kind, "text": text, "gif": gif}
    return parse


async def describe_gif(data, mime_type, hint="", transport=None):
    """A short description of a GIF (its video bytes) for picking it as a reaction later.
    `hint`: what's known besides the picture, like its caption or file name."""
    return await describe(DESCRIBE_PROMPT, data, mime_type, hint, transport)


async def describe_photo(data, mime_type="image/jpeg", hint="", transport=None):
    """What's on a picture sent to Гойдик, text on it included (screenshots, memes)."""
    return await describe(PHOTO_PROMPT, data, mime_type, hint, transport)


async def describe(prompt, data, mime_type, hint, transport):
    content = [
        {"inline_data": {"mime_type": mime_type, "data": b64encode(data).decode()}},
        {"text": hint or "(без подписи)"},
    ]
    body = request_body(prompt, content, DESCRIBE_SCHEMA, temperature=0.2)
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        # Only the cheapest model: a description doesn't need wit, it has the most free
        # requests a day, and the better models' small quota is kept for answers
        return await race(client, model_queue("cheap"), body, headers, parse_about)


def parse_about(data):
    about = str(json_reply(data).get("about") or "").strip().rstrip(".")
    if not about:
        raise TryNext(BROKEN)
    return about[:300]


def own_answers(lines):
    """Гойдик's last answers among the chat lines."""
    return [line.split(": ", 1)[1] for line in lines if line.startswith("Гойдик: ")][-RECENT_OWN:]


def chat_text(message, lines, used, mood):
    parts = []
    if lines:
        parts.append("Последние сообщения в чате, для контекста:\n" + "\n".join(lines))
    parts.append("Сообщение тебе:\n" + message)
    if used:
        # Gemini keeps picking up its own recent jokes from the context; a reminder
        # right before the answer works better than one in the system prompt
        parts.append("Твои последние ответы (уже отработаны, не повторяй):\n" + "\n".join(f"- {a}" for a in used))
    parts.append(
        f"Настрой на этот ответ: {mood}. Если человеку реально нужна помощь или ему плохо — "
        "забудь про настрой и ответь как надо."
    )
    return "\n\n".join(parts)


def request_body(system, content, schema, temperature):
    """`content`: the user's text, or a list of parts (text, a video...)."""
    parts = [{"text": content}] if isinstance(content, str) else content
    return {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 4096,
            # JSON keeps the model's reasoning out of the chat: only the "text" field is sent
            "responseMimeType": "application/json",
            "responseSchema": schema,
            # 3.x Flash models think at "medium" by default, which can take a minute on a long
            # group context; a drunk friend doesn't need deep reasoning
            "thinkingConfig": {"thinkingLevel": THINKING_LEVEL},
        },
        # Friendly swearing sometimes trips the default harassment filter
        "safetySettings": [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"}],
    }


def reply_text(data):
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
    return text


def json_reply(data):
    text = reply_text(data)
    try:
        reply = json.loads(text)
    except json.JSONDecodeError:
        reply = None
    if not isinstance(reply, dict):
        log.warning("answer is not a JSON object: %.200s", text)
        raise TryNext(BROKEN)
    return reply


def tidy(text):
    """Chat style the models sometimes forget: quotes around, a signature, a final period,
    a GIF's description copied into its caption."""
    text = re.sub(r"^\[гифка[^\]]*\]", "", text.strip()).strip()
    if text.endswith(".") and not text.endswith(".."):
        text = text[:-1]
    if len(text) > 1 and text[0] in "«\"" and text[-1] in "»\"":
        text = text[1:-1].strip()
    if text.lower().startswith("гойдик:"):
        text = text.split(":", 1)[1].strip()
    return text[:MAX_REPLY]


def model_queue(order="main"):
    """Models to ask, in turn: the main model, then fallbacks; for "cheap" work (describing
    GIFs) only the last fallback. Models overloaded recently go last."""
    main = os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    fallbacks = os.environ.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK).split(",")
    fallbacks = [m.strip() for m in fallbacks if m.strip() and m.strip() != main]
    models = (fallbacks or [main])[-1:] if order == "cheap" else [main] + fallbacks
    now = time.monotonic()
    return sorted(models, key=lambda m: busy_until.get(m, 0) > now)  # stable: order kept otherwise


async def race(client, models, body, headers, parse):
    """Ask models[0]; if it fails, or is silent for HEDGE_AFTER seconds, ask the next one too.
    The first proper answer wins, the rest are cancelled."""
    queue = list(models)
    running = set()
    error = BROKEN

    def start_next():
        model = queue.pop(0)
        running.add(asyncio.create_task(attempt(client, model, copy.deepcopy(body), headers, parse)))

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


async def attempt(client, model, body, headers, parse):
    """One model's parsed answer; TryNext if another model may still help, GoydikError if not."""
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
    answer = parse(r.json())
    log.info("answered by %s in %.1fs", model, time.monotonic() - started)
    return answer
