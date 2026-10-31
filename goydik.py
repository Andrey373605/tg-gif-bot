"""Гойдик: the chat's friend who roasts everyone, on top of the Gemini API.

An answer takes two requests. First Гойдик drafts a few different answers (a roast,
a laugh, a made-up story, a one-word reaction...), then a judge picks the funniest one.
A single request gives the most obvious roast every time.

The persona lives in prompt.txt, the dossier on chat members in people.txt. Гойдик only
sees the dossier of the people a message is about, so he can't drag the others in.

Env:
    GEMINI_API_KEY          key from Google AI Studio (without it Гойдик is asleep)
    GEMINI_MODEL            model name, default gemini-3.8-flash
    GEMINI_FALLBACK_MODEL   comma-separated models asked in turn when the main one is
                            overloaded or out of quota; they also go first for judging.
                            Default gemini-3.6-flash,gemini-3.5-flash-lite, empty turns them off
"""
import asyncio
import copy
import json
import logging
import os
import re
import time
from pathlib import Path

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
OPTIONS = 4  # answers drafted for the judge to choose from
MAX_INPUT = 2000
MAX_REPLY = 4000  # Telegram limit is 4096

PROMPT_FILE = Path(__file__).with_name("prompt.txt")
PEOPLE_FILE = Path(__file__).with_name("people.txt")

# How the bot packs messages and what it wants back. Lives here, not in prompt.txt,
# so editing the persona can't break it
DRAFT_TASK = f"""
КАК УСТРОЕН ЗАПРОС
- Сообщение тебе приходит в виде «Имя (@username): текст». Перед ним может быть блок
  последних сообщений: о чём болтали до тебя. Отвечай только на сообщение тебе.
- «Гойдик: ...» — твои прошлые ответы. «Вася (@vasya) → Лена: ...» — Вася ответил Лене.
  «[фото]», «[стикер 😂]» — что скинули; картинки и видео ты не видишь.
- В конце может быть список твоих последних ответов: эти шутки, темы и словечки уже
  отработаны, не повторяй их.

ЗАДАЧА
Сразу в чат ты не пишешь: сначала придумываешь варианты, потом из них выберут лучший.
1. Определи, что это за сообщение (поле kind):
   banter — потрындеть, подколоть, наехать, риторический вопрос;
   question — человеку реально нужна инфа или помощь;
   serious — человеку плохо или тема серьёзная;
   bait — ловушка, где любой ответ делает тебя проигравшим («ответишь — лох», «кто ответит,
   тот гей», «не отвечай на это»). Только такие ловушки, обычные наезды — это banter.
   Решай по сообщению тебе, а не по блоку последних сообщений.
2. Придумай {OPTIONS} совсем разных варианта ответа (поле options). Минимум три из них —
   настоящие шутки с поворотом, каждая своим приёмом: стёб, абсурд, шутка без жертвы,
   байка про себя, обида с подколом, встречный вопрос с подвохом, затупить. Четвёртым
   может быть короткая реакция, если она подходит по смыслу. Двух вариантов одного приёма не надо.
   Для question каждый вариант содержит настоящий ответ, для serious — без стёба,
   для bait варианты не нужны.
   Факт из досье — максимум в одном варианте, только один факт и только если он сам
   напрашивается. Два факта в одной шутке — нельзя.
3. В поле facts каждого варианта честно перечисли факты из досье, на которых он держится
   (по 2–3 слова: «стройка», «кола по акции»), или оставь пустым. Варианты с двумя и больше
   фактами бот выбросит.
"""

JUDGE_PROMPT = """Ты — самый придирчивый участник телеграм-чата «адские дрочиллы иитп 2007»: одногруппники-айтишники из Минска, всем около двадцати, юмор сухой и матерный. Бот чата Гойдик придумал несколько вариантов ответа на сообщение. Выбери тот, после которого чат реально напишет «ахахах» или «база».

Сначала проверь каждый вариант (поле verdicts): запиши его проблему или пустую строку, если проблем нет.
- притянут факт: вариант шутит про работу, хобби или привычку человека, а в сообщении об этом ни слова и прямо из него это не следует. В тему, если сообщение про то же или факт прямо вытекает: «колу взял» → кола по акции, «реал выиграл» → барса, «нет времени» → работа, «наушники сдохли» → новые не купит. Притянуто: «сходил на концерт» → работа, «переезжаю» → тиктоки, «чел с попугаем в метро» → альтушки
- винегрет: в одной шутке склеены два и больше факта из досье («на стройке колу по акции дают»)
- повтор: та же тема, шутка или словечко, что в последних ответах Гойдика
- длинно: больше двух предложений, объясняет шутку или нанизывает несколько подколов. Одно-два предложения с одной шуткой — нормально
- мимо: на реальный вопрос нет ответа, на серьёзное — стёб, не про сообщение или не подходит по смыслу (на «ты еблан?» ответить «база» — согласиться, что еблан), на переспрос «причём тут это?» — новая бессмыслица вместо ответа
- шаблон: звучит как тамада, ассистент или натужная заготовка
- нельзя: мамы, педофилия, народы, всерьёз политика

Потом выбери (choice) самый смешной вариант без проблем: тот, что точнее и неожиданнее всего отвечает на само сообщение. Факт из досье в тему — нормально, притянутый — нет. Выбирай шутку: голую реакцию в пару слов («база», «факты», «грац») бери, только если у всех шуток есть проблемы или сообщению правда больше ничего не надо (простое «ок», стикер). Если проблемы у всех — бери наименее плохой. Выбранный вариант можно чуть подправить (поле text) — сократить, убрать лишнее, — но не превращать в другую шутку. С маленькой буквы, без точки в конце, без смайликов.

Досье — только чтобы понимать шутки для своих, а не чтобы искать варианты с фактами:
"""

DRAFT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "kind": {"type": "STRING", "enum": ["banter", "question", "serious", "bait"]},
        "options": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "reaction": {"type": "STRING"},
                    "text": {"type": "STRING"},
                    # Listed so the bot can drop options that glue several facts together:
                    # the light judge lets them through however it's told
                    "facts": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["reaction", "text", "facts"],
                "propertyOrdering": ["reaction", "text", "facts"],
            },
        },
    },
    "required": ["kind", "options"],
    "propertyOrdering": ["kind", "options"],
}
# Checking every option for concrete problems before choosing: asked just "which is funniest",
# the judge goes for whatever hits the dossier, however far-fetched
JUDGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdicts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"option": {"type": "INTEGER"}, "problem": {"type": "STRING"}},
                "required": ["option", "problem"],
                "propertyOrdering": ["option", "problem"],
            },
        },
        "choice": {"type": "INTEGER"},
        "text": {"type": "STRING"},
    },
    "required": ["verdicts", "choice", "text"],
    "propertyOrdering": ["verdicts", "choice", "text"],
}

BLOCKED = "не-не, про это я даже бухой говорить не буду 🙅"
TIRED = "гойдик перебрал, дай ему минутку отдышаться 🥴"
BROKEN = "гойдик упал под стол, попробуй чуть позже 😵"
SLOW = "гойдик задумался и уснул прямо за столом 😴 спроси ещё раз"

TIMEOUT = 45  # seconds per request
# No answer by then: ask the next model too and take whichever answers first.
# Drafting several answers takes the main model longer than picking one
DRAFT_HEDGE = 20
JUDGE_HEDGE = 8
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


async def ask(history, author, text, recent=None, transport=None, trace=None):
    """Гойдик's answer to `author: text`, or None when he stays silent (a bait message).
    `history`: lines of a private chat; the exchange is added to it on success.
    `recent`: the last lines of a group chat ("Имя: текст"), passed along as context.
    `trace`: a dict to fill with the drafted options and the judge's pick (for try_goydik.py)."""
    message = f"{author}: {text[:MAX_INPUT]}"
    lines = list(history) + list(recent or ())
    used = own_answers(lines)
    people = dossier(f"{author}\n{text}")
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}

    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        draft = request_body(
            f"{PROMPT}\n\nДОСЬЕ (только те, о ком речь)\n{people}\n{DRAFT_TASK}",
            chat_text(message, lines, used), DRAFT_SCHEMA, temperature=1.2,
        )
        kind, options = await race(client, model_queue(), draft, headers, parse_draft, DRAFT_HEDGE)
        if trace is not None:
            trace.update(kind=kind, options=options)
        if kind == "bait":
            log.info("staying silent on a bait message")
            return None  # not added to history: there was no answer

        answer = options[0]["text"]
        if len(options) > 1:
            judge = request_body(
                JUDGE_PROMPT + people, judge_text(message, lines, used, options), JUDGE_SCHEMA, temperature=0.4,
            )
            try:
                pick = await race(client, model_queue(light_first=True), judge, headers, picker(options), JUDGE_HEDGE)
                answer = (pick.get("text") or "").strip() or options[pick["choice"] - 1]["text"]
                if trace is not None:
                    trace.update(pick)
            except GoydikError:
                log.warning("judge failed, taking the first option")  # still a fine answer

    answer = tidy(answer)
    history.extend([message, f"Гойдик: {answer}"])
    del history[:-HISTORY_LIMIT]
    return answer


def own_answers(lines):
    """Гойдик's last answers among the chat lines."""
    return [line.split(": ", 1)[1] for line in lines if line.startswith("Гойдик: ")][-RECENT_OWN:]


def chat_text(message, lines, used):
    parts = []
    if lines:
        parts.append("Последние сообщения в чате, для контекста:\n" + "\n".join(lines))
    parts.append("Сообщение тебе:\n" + message)
    if used:
        # Gemini keeps picking up its own recent jokes from the context; a reminder
        # right before the answer works better than one in the system prompt
        parts.append("Твои последние ответы (уже отработаны, не повторяй):\n" + "\n".join(f"- {a}" for a in used))
    return "\n\n".join(parts)


def judge_text(message, lines, used, options):
    parts = []
    if lines:
        parts.append("Последние сообщения в чате:\n" + "\n".join(lines))
    parts.append("Сообщение Гойдику:\n" + message)
    if used:
        parts.append("Последние ответы Гойдика:\n" + "\n".join(f"- {a}" for a in used))
    rows = []
    for n, o in enumerate(options, 1):
        fact = f"; факт из досье: {o['facts'][0]}" if o["facts"] else ""
        rows.append(f"{n}. [{o['reaction']}{fact}] {o['text']}")
    parts.append("Варианты:\n" + "\n".join(rows))
    return "\n\n".join(parts)


def request_body(system, text, schema, temperature):
    return {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 4096,
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


def parse_draft(data):
    """(kind, options) from the drafting request."""
    draft = json_reply(data)
    kind = draft.get("kind")
    options = [
        {
            "reaction": str(o.get("reaction", "")),
            "text": o["text"].strip(),
            "facts": [str(f) for f in o.get("facts") or [] if str(f).strip()],
        }
        for o in draft.get("options") or []
        if isinstance(o, dict) and isinstance(o.get("text"), str) and o["text"].strip()
    ]
    # Two dossier facts in one joke make a mess ("на стройке колу по акции дают")
    single = [o for o in options if len(o["facts"]) < 2]
    if single:
        options = single
    if kind != "bait" and not options:
        log.warning("draft without options: %.200s", draft)
        raise TryNext(BROKEN)
    return kind, options


def picker(options):
    """Parser for the judge's answer: {"verdicts", "choice", "text"} with a valid choice."""
    def parse(data):
        pick = json_reply(data)
        choice = pick.get("choice")
        if not isinstance(choice, int) or not 1 <= choice <= len(options):
            log.warning("judge picked no option: %.200s", pick)
            raise TryNext(BROKEN)
        return pick
    return parse


def tidy(text):
    """Chat style the models sometimes forget: quotes around, a signature, a final period."""
    text = text.strip()
    if text.endswith(".") and not text.endswith(".."):
        text = text[:-1]
    if len(text) > 1 and text[0] in "«\"" and text[-1] in "»\"":
        text = text[1:-1].strip()
    if text.lower().startswith("гойдик:"):
        text = text.split(":", 1)[1].strip()
    return text[:MAX_REPLY]


def model_queue(light_first=False):
    """Main model, then fallbacks in order; for light work like judging the fallbacks go
    first, so the main model's small free quota is kept for drafting. Models that were
    overloaded recently go last."""
    main = os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    fallbacks = os.environ.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK).split(",")
    fallbacks = [m.strip() for m in fallbacks if m.strip() and m.strip() != main]
    models = fallbacks + [main] if light_first else [main] + fallbacks
    now = time.monotonic()
    return sorted(models, key=lambda m: busy_until.get(m, 0) > now)  # stable: order kept otherwise


async def race(client, models, body, headers, parse, hedge_after):
    """Ask models[0]; if it fails, or is silent for `hedge_after` seconds, ask the next one too.
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
            wait = hedge_after if queue else None
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
