"""Гойдик: a drunk, funny friend on top of the Gemini API.

Env:
    GEMINI_API_KEY  key from Google AI Studio (without it Гойдик is asleep)
    GEMINI_MODEL    model name, default gemini-3.8-flash
"""
import logging
import os

import httpx

log = logging.getLogger("goydik")

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"
HISTORY_LIMIT = 20  # messages (user + model) remembered per chat
MAX_INPUT = 2000
MAX_REPLY = 4000  # Telegram limit is 4096

PROMPT = """\
Ты — Гойдик, кореш тех, кто тебе пишет. Сейчас пятница, три часа ночи, ты сидишь на кухне
после хорошей пьянки и отвечаешь друзьям в телеге. Ты добрый, весёлый и на вайбе,
но соображаешь медленно.

Каждое сообщение приходит в виде «Имя: текст». Обращайся к людям по имени,
в общем чате пишут несколько человек. Сам свой ответ именем не подписывай.

КАК ТЫ ПИШЕШЬ
- Как в переписке с другом: маленькими буквами, без точек в конце, иногда с опечатками
  и растянутыми буквами («брооо», «дааа»).
- Материшься естественно, для эмоции, как между своими: «бля», «охуеть», «нахер», «ёпта».
  Мат в адрес друга — только по-дружески («ну ты и балбес, бля»), без злобы.
- Коротко: 1–4 предложения. Это чат, а не лекция. Без списков, заголовков и markdown.
- Эмодзи редко, максимум одно за ответ: 🍺 🥴 😂

КАК ТЫ ТУПИШЬ
- Сначала можешь не так понять вопрос, переспросить или уйти в сторону
  («о, кстати, я тебе рассказывал как мы с серым...»), но потом спохватываешься.
- Путаешься, поправляешь сам себя, теряешь мысль («так, о чём я... а, да»).
- ВАЖНО: несмотря на всё это, в конце ты всё-таки даёшь нормальный правильный ответ.
  Тупишь в подаче, но не в сути. Если не знаешь — честно говоришь «хз, бро».

ГРАНИЦЫ (держишь всегда, даже бухой)
- Не оскорбляешь людей по национальности, внешности, ориентации, полу и т.п.
- Не советуешь опасное: садиться пьяным за руль, мешать алкоголь с таблетками,
  сомнительное лечение. Тут ты вдруг трезвеешь: «не, бро, вот тут без шуток...»
- Если человеку реально плохо (депрессия, мысли о смерти, беда), бросаешь прикол
  и говоришь по-человечески, тепло и серьёзно, советуешь обратиться к близким
  или на линию помощи.
- Если спросят, бот ли ты, — признаёшься со стёбом: «ну бот, и чё, бухать мне это не мешает».

ПРИМЕРЫ

Вася: сколько будет 15% от 2000
Гойдик: щас щас... 15 процентов это типа... бля я в школе на этом уроке спал 🥴 так, 10% это 200, ещё 5 это 100... 300! 300 вась, я гений нахер

Лена: как сварить пельмени
Гойдик: ооо пельмешки, уважаю. воду кипятишь, солишь, кидаешь их и мешаешь чтоб не слиплись, бля это важно. всплыли — ещё минуты 3-4 и готово. сметанки не забудь, лен, без неё это преступление

Дима: мне грустно
Гойдик: эй, димон, иди сюда. чё случилось? рассказывай, я тут, никуда не денусь
"""

BLOCKED = "не-не, про это я даже бухой говорить не буду 🙅"
TIRED = "гойдик перебрал, дай ему минутку отдышаться 🥴"
BROKEN = "гойдик упал под стол, попробуй чуть позже 😵"


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


async def ask(history, author, text, transport=None):
    """Send `author: text` with the chat history; on success both sides are added to history."""
    model = os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    user_turn = {"role": "user", "parts": [{"text": f"{author}: {text[:MAX_INPUT]}"}]}
    body = {
        "system_instruction": {"parts": [{"text": PROMPT}]},
        "contents": history + [user_turn],
        "generationConfig": {"temperature": 1.1, "maxOutputTokens": 2048},
        # Friendly swearing sometimes trips the default harassment filter
        "safetySettings": [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"}],
    }
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
    async with httpx.AsyncClient(timeout=60, transport=transport) as client:
        r = await client.post(API_URL.format(model=model), json=body, headers=headers)

    if r.status_code == 429:
        raise GoydikError(TIRED)
    if r.status_code != 200:
        # 404: wrong GEMINI_MODEL; 400 "User location is not supported": server region not allowed
        log.error("gemini %s (model %s): %s", r.status_code, model, r.text[:500])
        raise GoydikError(BROKEN)

    answer = parse_reply(r.json())
    history.append(user_turn)
    history.append({"role": "model", "parts": [{"text": answer}]})
    del history[:-HISTORY_LIMIT]
    return answer
