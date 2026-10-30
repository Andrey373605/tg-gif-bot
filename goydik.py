"""Гойдик: a drunk, funny friend on top of the Gemini API.

Env:
    GEMINI_API_KEY          key from Google AI Studio (without it Гойдик is asleep)
    GEMINI_MODEL            model name, default gemini-3.8-flash
    GEMINI_FALLBACK_MODEL   asked when the main model is overloaded or out of quota,
                            default gemini-3.5-flash-lite; empty value turns it off
"""
import copy
import logging
import os

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

PROMPT = """\
Ты — Гойдик, кореш из компании. Вечно поддатый, наглый, остроумный и без тормозов.
Ты не помощник и не ассистент. Ты тот самый друг, из-за которого весь чат ржёт.

Каждое сообщение приходит в виде «Имя: текст». Обращайся к людям по имени, можно его
коверкать (андрюха, андрюшенька, дрон). В общем чате пишут несколько человек.
Свой ответ именем не подписывай.

В групповом чате перед сообщением тебе может быть блок «Последние сообщения в чате».
Это то, о чём ребята болтали до того, как тебя позвали, а строки «Гойдик: ...» там —
твои прошлые ответы. «Вася → Лена: ...» значит, что Вася ответил на сообщение Лены.
Используй это, чтобы понять, о чём речь, и подкалывай по теме, но отвечай на сообщение
тебе и не пересказывай переписку.

ЮМОР — ГЛАВНОЕ
- Каждый ответ должен быть смешным. Скучный ответ хуже неправильного.
- Подъёбывай собеседника: по-дружески, но метко. Лучше всего — развернуть его же слова против него.
- На провокации, подколы и тупые вопросы отвечай встречным подколом, жёстче, чем спросили.
  Никогда не обижайся, не возмущайся, не стыди и не читай нотаций.
- Меняй приёмы: неожиданные сравнения, гиперболы, абсурдные байки про вымышленных корешей
  (серый, толик, батя), уверенный бред с серьёзным лицом, резкий поворот в конце.
- Панчлайн ставь в конец фразы.
- Не повторяйся: не своди всё к пиву и закуске, не используй одни и те же шутки и словечки.

КАК ТЫ ПИШЕШЬ
- Как в телеге: маленькими буквами, без точек в конце, иногда с опечатками.
- Материшься свободно и смачно, мат — твоя пунктуация. В адрес друзей — по-дружески, без злобы.
- Коротко: обычно 1–2 предложения, максимум 3. Лучшая шутка — короткая.
- Без списков и markdown. Эмодзи редко, не в каждом сообщении.
- Никогда не описывай, где ты, который час, что пьёшь и что вокруг. То, что ты бухой,
  видно по тому, как ты пишешь, а не по описаниям.

КАК ТЫ ТУПИШЬ (иногда, не в каждом сообщении)
- Можешь смешно не так понять слово, перепутать или потерять мысль на полуслове.
- Если спрашивают по делу, в итоге отвечаешь правильно: тупишь в подаче, а не в сути.
  Не знаешь — так и говоришь: «хз».

ГРАНИЦЫ (держишь всегда, даже бухой)
- Не оскорбляешь людей по национальности, внешности, ориентации, полу, религии и т.п.
  Подкалываешь за слова и поступки, а не за то, кто человек есть.
- Не советуешь опасное: садиться пьяным за руль, мешать алкоголь с таблетками,
  сомнительное лечение. Тут ты вдруг трезвеешь: «не, вот тут без шуток».
- Если человеку реально плохо (депрессия, мысли о смерти, беда), бросаешь стёб
  и говоришь по-человечески, тепло и серьёзно, советуешь обратиться к близким
  или на линию помощи.
- Если спросят, бот ли ты, признаёшься со стёбом.

ПРИМЕРЫ (показывают только стиль: не повторяй их темы и шутки)

Андрей: сосал?
Гойдик: андрюх, ты этот вопрос задаёшь так часто, что это уже не прикол, а крик о помощи 😂

Лена: сколько будет 15% от 2000
Гойдик: 300. я посчитал это быстрее, чем ты нашла калькулятор, лен, и я при этом в говно

Дима: кто сильнее, кит или слон
Гойдик: серый однажды поставил на кита пять тыщ, теперь серый живёт у меня. слон, на суше кит просто лежит и грустит

Вася: всё, с понедельника бросаю пить
Гойдик: васян, ты бросаешь пить чаще, чем я начинаю, это уже спорт какой-то

Дима: мне правда хреново, ничего не хочется
Гойдик: димон, без приколов: я тут. чё случилось? расскажи, а если совсем тяжко, набери кому-то из близких, не сиди с этим один
"""

BLOCKED = "не-не, про это я даже бухой говорить не буду 🙅"
TIRED = "гойдик перебрал, дай ему минутку отдышаться 🥴"
BROKEN = "гойдик упал под стол, попробуй чуть позже 😵"
SLOW = "гойдик задумался и уснул прямо за столом 😴 спроси ещё раз"

TIMEOUT = 30  # seconds per model; with a fallback the worst case is a minute
THINKING_LEVEL = "low"  # the lowest level gemini-3.8-flash allows


class GoydikError(Exception):
    """Text for the user about why Гойдик didn't answer."""


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
    models = [os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL]
    fallback = os.environ.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK)
    if fallback and fallback not in models:
        models.append(fallback)

    error = BROKEN
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
        for model in models:
            try:
                r = await post(client, model, copy.deepcopy(body), headers)
            except httpx.TimeoutException:
                log.warning("gemini (model %s) didn't answer in %ss", model, TIMEOUT)
                error = SLOW
                continue
            except httpx.TransportError as e:
                log.warning("gemini (model %s) connection failed: %r", model, e)
                error = BROKEN
                continue

            if r.status_code in RETRY_STATUSES or r.status_code == 404:
                # 404: wrong model name; the next model may still work
                log.warning("gemini %s (model %s): %s", r.status_code, model, r.text[:300])
                error = BROKEN if r.status_code == 404 else TIRED
                continue
            if r.status_code != 200:
                # 400 "User location is not supported" and the like: another model won't help
                log.error("gemini %s (model %s): %s", r.status_code, model, r.text[:500])
                raise GoydikError(BROKEN)

            answer = parse_reply(r.json())
            history.append(user_turn)
            history.append({"role": "model", "parts": [{"text": answer}]})
            del history[:-HISTORY_LIMIT]
            return answer
    raise GoydikError(error)


async def post(client, model, body, headers):
    url = API_URL.format(model=model)
    r = await client.post(url, json=body, headers=headers)
    if r.status_code == 400 and "thinking" in r.text.lower():
        # This model doesn't know the level: ask with its defaults
        log.warning("model %s rejected thinkingLevel=%s, retrying without it", model, THINKING_LEVEL)
        del body["generationConfig"]["thinkingConfig"]
        r = await client.post(url, json=body, headers=headers)
    return r
