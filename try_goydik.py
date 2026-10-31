#!/usr/bin/env python3
"""Ask Гойдик test messages through the real Gemini API and print his answers,
to check prompt.txt / people.txt edits before deploying.

    python -X utf8 try_goydik.py            single messages, then the group dialog
    python -X utf8 try_goydik.py 3 7        only cases 3 and 7
    python -X utf8 try_goydik.py dialog     only the dialog (checks he doesn't repeat himself)

The key is taken from GEMINI_API_KEY or from a .env file next to this script
(a line GEMINI_API_KEY=...). Gemini doesn't serve some countries, there a VPN is needed.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

ENV_FILE = Path(__file__).with_name(".env")
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

import goydik  # noqa: E402  after .env, so the key is there

logging.basicConfig(format="  [%(levelname)s %(name)s] %(message)s", level=logging.WARNING)
logging.getLogger("goydik").setLevel(logging.INFO)  # "answered by <model>": main and fallback differ a lot

# As bot.display_name() shows them
PEOPLE = {
    "Андрей": "Андрей (@amnadrrteiyn)",
    "Артём": "Артём Стажер (@Blaugranass)",
    "Саня": "Саня Пиво (@PJ_HP_F)",
    "Коля": "Кока Коля (@dergunnik)",
    "Даник": "Даник^3 (@daniil_mariyn)",
    "Женя": "Женька Староста (@evgenchk)",
    "Лёша": "Леша Джава Кофикс (@Ryalesh)",
    "Максим": "Максим Альтушка (@IPojarnikI)",
}

# Single messages to Гойдик in a group, each without context. Keep them unlike the examples
# in prompt.txt: a message close to an example only shows whether he copies it
CASES = [
    ("Артём", "глянул вчера весь курс по докеру за пару часов"),
    ("Артём", "иногда кажется, что мы все просто играем роли, которые нам кто-то написал"),
    ("Артём", "кто в субботу на шашлыки?"),
    ("Артём", "у меня наушники сдохли, один вообще не играет"),
    ("Коля", "нашёл классную группу, второй день её слушаю"),
    ("Коля", "сорян, опять не успеваю к вам"),
    ("Андрей", "кароче я придумал идею для стартапа"),
    ("Андрей", "пиво без закуски вообще не то"),
    ("Максим", "наконец-то выспался"),
    ("Даник", "гойдик, а ты вообще шутить умеешь?"),
    ("Саня", "в субботу переезжаю на новую хату"),
    ("Женя", "завтра собес в яндекс"),
    ("Лёша", "второй день без кофе"),
    ("Максим", "прикиньте, в метро сегодня чел с попугаем на плече ехал"),
]
PAUSE = 5  # seconds between messages: the free tier allows only a few requests per minute

# Stand-ins for the chat's GIF collection: descriptions like the ones Gemini writes
GIFS = [{"id": f"test{n}", "about": about} for n, about in enumerate([
    "мужик в костюме медленно аплодирует с каменным лицом",
    "кот падает со стола и делает вид, что так и задумано",
    "Джон Траволта растерянно оглядывается по сторонам",
    "собака в горящей комнате пьёт кофе, надпись «this is fine»",
    "пикачу с открытым ртом в шоке",
    "дед машет рукой и уходит, мол, да ну вас",
    "строитель роняет кирпич себе на ногу",
    "девушка закатывает глаза",
], 1)]

# A group conversation: Гойдик's answers go into the log, like in the real chat
DIALOG = [
    ("Артём", "реал опять выиграл, бесит"),
    ("Коля", "@goydik_bot а ты за кого болеешь"),
    ("Артём", "короче иду в магаз, кому что взять"),
    ("Даник", "@goydik_bot чё молчишь"),
    ("Артём", "я вернулся, колу взял"),
    ("Андрей", "@goydik_bot как тебе чат сегодня"),
]


async def answer(who, text, recent, indent):
    """Ask Гойдик and print his answer (and the kind of message, if it isn't banter)."""
    trace = {}
    try:
        reply = await goydik.ask([], PEOPLE[who], text, recent=recent, gifs=GIFS, trace=trace)
    except goydik.GoydikError as e:
        reply = goydik.Reply(f"(ошибка: {e})")
    if trace.get("kind") and trace["kind"] != "banter":
        print(f"{indent}  ({trace['kind']})")
    if reply is None:
        said = "(промолчал)"
    else:
        said = f"[гифка: {reply.gif['about']}] {reply.text}".strip() if reply.gif else reply.text
    print(f"{indent}Гойдик: {said}")
    return said


async def single(n, who, text):
    print(f"{n}. {who}: {text}")
    await answer(who, text, [], "   ")
    print()


async def dialog():
    print("Диалог в группе:")
    recent = []
    for who, text in DIALOG:
        print(f"   {who}: {text}")
        reply = await answer(who, text, list(recent), "   ")
        recent += [f"{PEOPLE[who]}: {text}", f"Гойдик: {reply}"]
        await asyncio.sleep(PAUSE)
    print()


async def main(args):
    if not goydik.enabled():
        sys.exit(f"Нет GEMINI_API_KEY: задай переменную окружения или положи её в {ENV_FILE}")
    if args != ["dialog"]:
        picked = {int(a) for a in args if a.isdigit()}
        for n, (who, text) in enumerate(CASES, 1):
            if not picked or n in picked:
                await single(n, who, text)
                await asyncio.sleep(PAUSE)
    if not args or "dialog" in args:
        await dialog()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
