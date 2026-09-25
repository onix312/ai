"""Мозг помощника компьютера (18.21): понять фразу, вспомнить, выбрать навык, ответить.

До 18.21 у агента было девяносто шесть навыков и ни одного способа попросить
их словами: окно `/ui` требовало выбрать навык из списка и заполнить JSON, а
`voice.command` возвращал фразу обратно. Здесь то, что превращает реестр в
помощника:

  1. **Понимание без модели.** Частые команды («громкость 30», «открой
     блокнот», «переключись на телеграм», «сверни его», «что грузит
     компьютер») разбираются правилами — мгновенно, офлайн и одинаково каждый
     раз. Модель не нужна, чтобы убавить звук.
  2. **Контекст разговора.** Каждая реплика пишется в `dialog` с меткой: какой
     навык, какое окно, какой файл. «Закрой его», «ещё громче», «открой
     первый», «а на 50» понимаются по прошлой реплике, а не угадываются.
  3. **Память.** «Запомни, что…», «забудь…», «что ты помнишь про Марию» —
     своя таблица `memories`; найденное подмешивается в рассуждение модели.
  4. **Планировщик на модели.** То, что правила не поняли, уходит модели в
     JSON-режиме со списком только доступных навыков. Её ответ проверяется
     реестром (имя, параметры, доступность) — выдуманный навык не исполнится.
  5. **Ответ человеческими словами.** Результат навыка пересказывается фразой
     («Громкость 35%», «Открыто 6 окон: …»), а не JSON; следующий шаг
     подсказывается кнопками.

Границы прежние: навык с риском `write` и выше исполняется только после
подтверждения человека на этом компьютере (`Agent.run_skill` ставит его в
очередь), деньги и печать остаются за панелью.

18.22 — помощник учится и становится личным:

  6. **Обучение.** «Научись: когда я говорю «рабочий режим» — открой Telegram
     и громкость 30», «нет, я имел в виду …», «это значит …» после непонятой
     фразы, 👍/👎 под ответом, синонимы («телега» — это телеграм). Фраза,
     которую поняла модель и навык выполнился, запоминается сама — в следующий
     раз без модели (`learning.py`). Урок и поправка важнее встроенных правил,
     самовыученное — только там, где правила молчат.
  7. **Личное.** Напоминания со временем по-русски, списки, цели с темпом,
     привычки с сериями, расходы, дневник, «мой день», счёт дат и единиц
     (`intents.py`, `personal.py`). Уточнения («Когда напомнить?») помнят, о
     чём шла речь: ответ «через час» понимается следующей репликой.
"""
from __future__ import annotations

import ast
import datetime
import inspect
import re
import threading
import time
from typing import Any, Callable

from . import config, intents, model, pc, skills, when
from .personal_skills import describe_steps, is_live

SESSION_RE = re.compile(r"[^0-9A-Za-z_.:-]+")
MAX_TEXT = 1000
PLAN_TIMEOUT_SEC = 45.0

_RU_DAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
_RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря")

# Обращения и вежливость, которые не несут команды: «Ноза, пожалуйста, сделай тише».
_LEAD_RE = re.compile(
    r"^(?:(?:эй|слушай|окей|ok|ну|а|так|и)\s*,?\s+)*"
    r"(?:(?:ноза|нозза|nozza|noza|помощник|ассистент)\s*,?\s+)?"
    r"(?:(?:пожалуйста|будь добр|будь другом|плиз|please)\s*,?\s+)?", re.IGNORECASE)
_TAIL_RE = re.compile(r"[\s,]*(?:пожалуйста|плиз|please|спасибо)?[\s.!?]*$", re.IGNORECASE)
_WORKSHOP_WORDS = ("заказ", "задани", "печат", "принтер", "станок", "станк", "катуш", "пластик",
                   "клиент", "долг", "касс", "смен", "очеред", "полк", "стеллаж", "p1s", "p2s", "x1c", "a1")


def normalize_phrase(text: str) -> str:
    """Фраза без обращения, вежливости и хвостовой пунктуации — то, что разбирают правила."""
    clean = " ".join(str(text or "").replace("ё", "е").replace("Ё", "Е").split())[:MAX_TEXT]
    clean = _LEAD_RE.sub("", clean, count=1)
    clean = _TAIL_RE.sub("", clean)
    return clean.strip()


# Сессия окна агента (`/ui`): подтверждение там — карточкой в ленте.
WINDOW_SESSION = "window"
# Самообучение на таких навыках бессмысленно: «запомни …» и уроки повторять незачем.
_NO_SELF_LEARN = ("agent.", "learn.", "memory.", "assistant.macro")


def session_key(raw: str) -> str:
    return (SESSION_RE.sub("", str(raw or "main"))[:40] or "main")


def _plan(skill: str, params: dict[str, Any] | None = None, why: str = "",
          reply: str = "", target: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"skill": skill, "params": dict(params or {}), "why": why, "reply": reply,
            "target": dict(target or {}), "source": "rules"}


def _minutes(value: str, unit: str) -> int:
    number = int(value)
    return number * 60 if unit.startswith("час") else number


# ---------------------------------------------------------------------------
# 1. Время, дата, арифметика — ответы без навыков
# ---------------------------------------------------------------------------

def date_line(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now()
    return f"Сегодня {_RU_DAYS[now.weekday()]}, {now.day} {_RU_MONTHS[now.month - 1]} {now.year} года."


def clock_answer(text: str, now: datetime.datetime | None = None) -> str:
    """«Который час», «какое сегодня число» — часы компьютера, а не модель."""
    low = normalize_phrase(text).casefold()
    now = now or datetime.datetime.now()
    if re.search(r"(который|сколько)\s+(сейчас\s+)?(час|времени)|^время$|^сколько время", low):
        return f"Сейчас {now:%H:%M}."
    if re.search(r"(какое|какой)\s+(сегодня\s+)?(число|день|дата)|какое сегодня|день недели|сегодняшняя дата", low):
        return date_line(now)
    return ""


_MATH_RE = re.compile(r"^(?:сколько будет|посчитай|вычисли|реши|сколько)?\s*([0-9+\-*/().,\s×÷x]+?)\s*=?\s*\??$",
                      re.IGNORECASE)
_BINARY = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
           ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b}


def _evaluate(node: ast.AST) -> float:
    """Разбор дерева выражения вручную: только числа и четыре действия, без eval."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _evaluate(node.left), _evaluate(node.right)
        if abs(left) > 1e15 or abs(right) > 1e15:
            raise OverflowError("слишком большие числа")
        return _BINARY[type(node.op)](left, right)
    raise ValueError("не арифметика")


def _fmt_number(value: float) -> str:
    value = round(float(value), 6)
    return str(int(value)) if value.is_integer() else str(value).replace(".", ",")


def math_value(text: str) -> tuple[str, float | None]:
    """Арифметика без модели и её значение — чтобы понять «а если поделить на 5?»."""
    match = _MATH_RE.match(normalize_phrase(text).casefold())
    if not match:
        return "", None
    raw = match.group(1).replace("×", "*").replace("÷", "/").replace("x", "*").replace(",", ".")
    if not re.search(r"\d", raw) or not re.search(r"[+\-*/]", raw) or len(raw) > 60:
        return "", None
    try:
        value = _evaluate(ast.parse(raw.strip(), mode="eval"))
    except ZeroDivisionError:
        return "На ноль делить нельзя.", None
    except (SyntaxError, ValueError, OverflowError, TypeError):
        return "", None
    return f"{match.group(1).strip()} = {_fmt_number(value)}", float(value)


def math_answer(text: str) -> str:
    """Арифметика без модели: «2+2», «посчитай 15*3.5». Только числа и знаки."""
    return math_value(text)[0]


_NUMBER_WORDS = {"ноль": 0, "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
                 "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "двадцать": 20,
                 "тридцать": 30, "сорок": 40, "пятьдесят": 50, "сто": 100, "двести": 200, "тысячу": 1000,
                 "тысяча": 1000}
_FOLLOW_NUM = r"(?P<n>-?\d+(?:[.,]\d+)?|[а-я]+)"
_FOLLOW_FILL = r"(?:(?:это|его|результат|число|к\s+этому|от\s+этого|еще)\s+)*"
_FOLLOW_OPS = (("/", r"(?:по|раз)?дел\w*"), ("*", r"(?:у|по)?множ\w*"), ("+", r"прибав\w*|добав\w*|плюс"),
               ("-", r"отним\w*|отня\w*|вычт\w*|вычес\w*|минус"))
_SIGNS = {"/": "÷", "*": "×", "+": "+", "-": "−"}


def math_follow_up(text: str, number: float | None) -> tuple[str, float | None]:
    """«А если поделить на 5?», «умножь на 3», «15% от этого» — действие над прошлым результатом.

    Срабатывает только сразу после ответа-числа: агент хранит его в метке
    реплики (`target.number`), а не угадывает, о каком числе речь.
    """
    if number is None:
        return "", None
    low = normalize_phrase(text).casefold().rstrip("?!. ")
    low = re.sub(r"^(?:(?:а|и|теперь|тогда|если|ну)\s+)+", "", low)
    prev = _fmt_number(number)
    if re.fullmatch(r"(?:это\s+|его\s+)?(?:в\s+квадрат\w*|возвед\w*\s+(?:это\s+|его\s+)?в\s+квадрат)", low):
        value = number * number
        return (f"{prev}² = {_fmt_number(value)}", value) if abs(value) < 1e15 else ("", None)
    if re.fullmatch(r"(?:(?:раз|по)?дели\w*\s+)?(?:это\s+|его\s+)?пополам", low):
        return f"{prev} ÷ 2 = {_fmt_number(number / 2)}", number / 2
    match = re.fullmatch(r"(?P<p>\d+(?:[.,]\d+)?)\s*(?:%|процент\w*)(?:\s+от\s+(?:этого|него|результата|числа|суммы))?",
                         low)
    if match:
        percent = float(match.group("p").replace(",", "."))
        value = number * percent / 100
        return f"{_fmt_number(percent)}% от {prev} = {_fmt_number(value)}", value
    for op, verbs in _FOLLOW_OPS:
        match = re.fullmatch(rf"(?:{verbs})\s+{_FOLLOW_FILL}(?:на\s+)?{_FOLLOW_NUM}", low)
        if not match:
            continue
        raw = match.group("n").replace(",", ".")
        other = float(raw) if re.fullmatch(r"-?\d+(?:\.\d+)?", raw) else _NUMBER_WORDS.get(raw)
        if other is None:
            return "", None
        if op == "/" and float(other) == 0:
            return "На ноль делить нельзя.", None
        value = {"/": lambda: number / other, "*": lambda: number * other,
                 "+": lambda: number + other, "-": lambda: number - other}[op]()
        if abs(value) > 1e15:
            return "", None
        return f"{prev} {_SIGNS[op]} {_fmt_number(other)} = {_fmt_number(value)}", value
    return "", None


# ---------------------------------------------------------------------------
# 1б. Разговор: привет, как дела, кто ты, спасибо, пока
# ---------------------------------------------------------------------------

_TALK = (
    ("greet", re.compile(r"^(?:привет\w*|здравствуй\w*|здрасьте|здорово|добрый\s+(?:день|вечер)|доброй\s+ночи|хай|"
                         r"хелло|hello|hi|hey|салют|приветствую)(?:[\s,!]+(?:как\s+(?:у\s+тебя\s+)?дела|как\s+ты))?$")),
    ("how", re.compile(r"^(?:как\s+(?:у\s+тебя\s+|твои\s+)?(?:дела|делишки|жизнь|поживаешь|ты|сам|настроение)"
                       r"(?:\s+там)?|как\s+ты\s+там|что\s+нового|как\s+обстановка|все\s+в\s+порядке)$")),
    ("who", re.compile(r"^(?:кто\s+ты(?:\s+такой|\s+такая)?|ты\s+кто(?:\s+такой)?|что\s+ты\s+такое|как\s+тебя\s+зовут|"
                       r"представься|расскажи\s+о\s+себе)$")),
    ("thanks", re.compile(r"^(?:спасибо|благодарю|спс|пасиб\w*|thanks|thank\s+you|thx|сенкс|мерси)"
                          r"(?:\s+(?:большое|огромное|тебе|вам))*$")),
    ("bye", re.compile(r"^(?:пока|до\s+свидания|до\s+завтра|до\s+встречи|спокойной\s+ночи|бывай|увидимся)$")),
    # 18.22: короткое согласие и «нет, спасибо» — не повод говорить «не понимаю».
    ("ok", re.compile(r"^(?:нет,?\s+спасибо|не\s+надо,?\s+спасибо|ничего\s+не\s+надо|ок|окей|ok|хорошо|ладно|понятно|"
                     r"ясно|отлично|супер|класс|круто|договорились|принято)$")),
)


def small_talk(text: str) -> str:
    """Вид реплики без команды: greet / how / who / thanks / bye — или пусто.

    Проверяется и сырая фраза: `normalize_phrase` срезает хвостовое «спасибо»
    как вежливость, а здесь «спасибо» — вся реплика.
    """
    raw = " ".join(str(text or "").casefold().replace("ё", "е").split()).strip(" .!?,")
    for candidate in (raw, normalize_phrase(text).casefold().strip(" .!?,")):
        for kind, pattern in _TALK:
            if candidate and pattern.match(candidate):
                return kind
    return ""


# Вопросы, на которые отвечает панель цеха, а не компьютер: станки, заказы,
# деньги, склад. Шире `_WORKSHOP_WORDS`, который лишь не даёт правилам
# компьютера перехватить фразу про печать.
_PANEL_WORDS = _WORKSHOP_WORDS + ("ферм", "деньг", "выручк", "прибыл", "расход", "склад", "остат", "брифинг",
                                  "итог", "план ", "должн", "задолж", "оплат", "продаж", "материал", "филамент",
                                  "petg", "pla", "abs", "осталось", "закончит", "прогресс")
_PANEL_SOURCES = {"farm": "парк", "entity": "база цеха", "facts": "факты базы", "knowledge": "база", "math": "арифметика",
                  "clock": "часы", "memory": "память панели", "model": "модель панели", "rules": "правила панели"}


def panel_session(session: str) -> str:
    """Сессия агента в панели: контекст «его / второй» у окна агента свой, не общий с панелью."""
    return session_key(f"agent-{session}")


# ---------------------------------------------------------------------------
# 2. Память: команды «запомни / забудь / что помнишь»
# ---------------------------------------------------------------------------

_REMEMBER_RE = re.compile(
    r"^(?:запомни|запиши в память|занеси в память|имей в виду|учти|помни|сохрани в память)"
    r"(?:\s*,\s*|\s*:\s*|\s+)(?:что\s+|то,?\s+что\s+)?(?P<text>.{2,})$", re.IGNORECASE)
_FORGET_RE = re.compile(
    r"^(?:забудь|удали из памяти|сотри из памяти|выкинь из памяти)"
    r"(?:\s*,\s*|\s*:\s*|\s+)(?:что\s+|про\s+|о\s+|об\s+)?(?P<text>.{2,})$", re.IGNORECASE)
_RECALL_RE = re.compile(
    r"^(?:что ты (?:помнишь|знаешь|запомнил)|что (?:у тебя )?в памяти|вспомни|покажи память|"
    r"что я (?:тебе )?(?:говорил|рассказывал))(?:\s+(?:обо|о|об|про)\s+(?P<text>.+))?$", re.IGNORECASE)
_NAME_RE = re.compile(r"^(?:меня зовут|мое имя|моё имя|зови меня|называй меня)\s+(?P<name>[A-Za-zА-Яа-яЁё-]{2,30})",
                      re.IGNORECASE)


def memory_command(text: str) -> tuple[str, str]:
    """(«remember» | «forget» | «recall» | «name» | «whoami» | «», содержимое)."""
    clean = normalize_phrase(text)
    low = clean.casefold()
    for kind, pattern in (("remember", _REMEMBER_RE), ("forget", _FORGET_RE)):
        match = pattern.match(clean)
        if match:
            return kind, match.group("text").strip(" .,!")
    match = _RECALL_RE.match(clean)
    if match:
        return "recall", (match.group("text") or "").strip(" .,!?")
    match = _NAME_RE.match(clean)
    if match:
        return "name", match.group("name").strip().capitalize()
    if re.search(r"как меня зовут|ты знаешь,? как меня зовут|кто я\??$", low):
        return "whoami", ""
    return "", ""


# ---------------------------------------------------------------------------
# 3. Правила: частые команды компьютера
# ---------------------------------------------------------------------------

def _word_number(text: str) -> int | None:
    """Число из цифр или слов («тридцать пять», «пять») — И333, через словарь `when`."""
    match = re.search(when.NUM + r"(?![а-яa-z])", text)
    if not match:
        return None
    value = when.number(match.group(0).replace("ё", "е"))
    return int(value) if value is not None else None


def _level(text: str) -> int | None:
    match = re.search(r"(\d{1,3})\s*(?:%|процент)?", text)
    if not match:
        words = {"максимум": 100, "на всю": 100, "половин": 50, "минимум": 0, "ноль": 0}
        for word, value in words.items():
            if word in text:
                return value
        number = _word_number(text)
        return max(0, min(100, number)) if number is not None else None
    return max(0, min(100, int(match.group(1))))


def _volume_plan(low: str) -> dict[str, Any] | None:
    """Громкость в любом порядке слов (И335) и числа словами (И333).

    «на 30 громкость», «сделай громкость 30», «громкость тридцать пять»,
    «сделай тише», «на пять громче», «убавь на пять». Дельта важнее уровня:
    «громкость на 10 тише» — это −10, а не «громкость 10». «Убавь цену» — не
    про звук: направление без «звук/громкост» берётся только с явным числом.
    """
    if re.search(r"(какая|сколько)\s+(сейчас\s+)?громкост", low) or re.match(r"^(?:а\s+|ну\s+)?громкость\s*\??$", low):
        return _plan("system.volume", {}, "звук: узнать")
    if "скорост" in low or "говори" in low:
        return None  # «не говори тише» и регулятор скорости — не про звук
    step = 20 if "намного" in low or "сильно" in low else 10
    explicit = re.search(r"(?:на|в)\s+(?P<n>" + when.NUM + r")\s*(?:%|процент)?(?![а-яa-z])", low)
    named = _word_number(explicit.group("n")) if explicit else None
    words = bool(re.search(r"звук|громкост", low))
    up = bool(re.search(r"погромче|громче", low))
    down = bool(re.search(r"потише|тише", low))
    up_verb = bool(re.search(r"прибав\w*|добав\w*\s+звук|увелич\w*", low))
    down_verb = bool(re.search(r"убав\w*|уменьш\w*", low))
    bare = re.match(r"^(?:давай\s+)?(?:прибав\w*|убав\w*|погромче|потише|громче|тише)"
                    r"(?:\s+(?:на|в)\s+" + when.NUM + r")?$", low)
    if up != down or up_verb != down_verb:
        toward_up = up or (up_verb and not down and not down_verb)
        if named is not None and (up or down or words or bare):
            return _plan("system.volume", {"delta": named if toward_up else -named},
                         "звук: громче" if toward_up else "звук: тише")
        if up or down or words or bare:
            return _plan("system.volume", {"delta": step if toward_up else -step},
                         "звук: громче" if toward_up else "звук: тише")
    if words:
        level = _level(low)
        if level is not None:
            return _plan("system.volume", {"level": level}, "звук: уровень")
    return None


_ORDINAL_PICK = {"первый": 0, "первое": 0, "один": 0, "одна": 0, "1": 0, "1-й": 0, "раз": 0,
                 "второй": 1, "второе": 1, "два": 1, "две": 1, "2": 1, "2-й": 1,
                 "третий": 2, "третье": 2, "три": 2, "3": 2, "3-й": 2,
                 "четвертый": 3, "четвертое": 3, "четыре": 3, "4": 3, "4-й": 3,
                 "пятый": 4, "пятое": 4, "пять": 4, "5": 4, "5-й": 4,
                 "последний": -1, "последнее": -1}


# Разговорные имена приложений: «телега» → Telegram (кириллица против латиницы
# контainment сам не найдёт). Правая сторона — канонический кусок имени.
_NICKNAMES: dict[str, str] = {
    "телега": "telegram", "телеграм": "telegram", "телеграмм": "telegram", "тг": "telegram",
    "хром": "chrome", "гугл": "chrome",
    "блокнот": "notepad",
    "калькулятор": "calculator", "кальк": "calculator",
    "проводник": "explorer", "эксплорер": "explorer",
    "вк": "vk", "вконтакте": "vk",
}


def pick_option(phrase: str, options: list[str]) -> str | None:
    """Короткий ответ на «какое окно?»: порядковое слово или имя из списка (И336)."""
    low = normalize_phrase(phrase).casefold().replace("ё", "е")
    if not low:
        return None
    for word in re.findall(r"[0-9a-zа-я-]+", low):
        if word in _ORDINAL_PICK:
            index = _ORDINAL_PICK[word]
            if -len(options) <= index < len(options):
                return options[index]
    aliases = [_NICKNAMES[word] for word in re.findall(r"[0-9a-zа-я-]+", low) if word in _NICKNAMES]
    for option in options:
        name = option.casefold().replace("ё", "е")
        if name and (name in low or low in name):
            return option
        for word in re.findall(r"[0-9a-zа-я-]{4,}", low):
            if word in name:
                return option
        for alias in aliases:
            if alias in name:
                return option
    return None


def understand(text: str) -> dict[str, Any] | None:
    """Фраза → план навыка по правилам. `None` — правила не поняли, дальше думает модель."""
    phrase = normalize_phrase(text)
    low = phrase.casefold()
    if not low:
        return None
    workshop = any(word in low for word in _WORKSHOP_WORDS)

    # --- звук
    if re.search(r"\b(выключи|отключи|убери|вырубь|выруби)\s+(весь\s+)?звук\b|без звука|замьют|^mute$|тишина$", low):
        return _plan("system.volume", {"mute": "on"}, "звук: выключить")
    if re.search(r"\b(включи|верни)\s+звук\b|^unmute$", low):
        return _plan("system.volume", {"mute": "off"}, "звук: включить")
    if not workshop:
        volume = _volume_plan(low)
        if volume is not None:
            return volume

    # --- музыка и видео
    if not workshop:
        if re.search(r"^(пауза|поставь на паузу|останови (музыку|видео|трек)|продолж\w* (музыку|воспроизведение|видео)"
                     r"|play|плей|воспроизв\w*)$|(музык|видео|трек)\w* на паузу", low):
            return _plan("system.media", {"action": "play_pause"}, "медиа: пауза")
        if re.search(r"(следующ\w* (трек|песн\w*|видео)|переключи (трек|песню)|^дальше$|^next$)", low):
            return _plan("system.media", {"action": "next"}, "медиа: следующий")
        if re.search(r"(предыдущ\w* (трек|песн\w*|видео)|прошл\w* (трек|песн\w*)|^назад трек|^prev$)", low):
            return _plan("system.media", {"action": "prev"}, "медиа: предыдущий")

    # --- окна
    if re.search(r"(какое|что за)\s+(сейчас\s+)?окно|активн\w* окн|где я сейчас|в каком я окне", low):
        return _plan("window.active", {}, "окно: активное")
    if re.search(r"(какие|список|покажи)\s+(все\s+)?(окна|окон)|что (сейчас )?открыто|какие программы открыты", low):
        return _plan("window.list", {"limit": 15}, "окна: список")
    match = re.match(r"^(?:переключись|переключи|перейди|вернись)\s+(?:на|в|к)\s+(?P<t>.+)$", low)
    if match and not workshop and not match.group("t").startswith(("сайт", "страниц")) \
            and not pc.resolve_site(match.group("t")):
        return _plan("window.focus", {"title": match.group("t")}, "окно: фокус")
    match = re.match(r"^(?:покажи|подними|выведи)\s+окно\s+(?P<t>.+)$", low)
    if match:
        return _plan("window.focus", {"title": match.group("t")}, "окно: фокус")
    match = re.match(r"^(?:разложи|поставь|расставь)\s+(?P<a>.+?)\s+и\s+(?P<b>.+?)(?:\s+рядом|\s+пополам|\s+50\s*/\s*50)?$", low)
    if match and ("рядом" in low or "разложи" in low or "пополам" in low):
        return _plan("window.snap", {"left_title": match.group("a"), "right_title": match.group("b")}, "окна: рядом")
    match = re.match(r"^(?:сверни|спрячь)(?:\s+(?:окно|окна))?(?:\s+(?P<t>.+))?$", low)
    if match and not (match.group("t") or "").startswith(("все", "всё")):
        target = (match.group("t") or "").strip()
        if target in ("его", "ее", "это"):
            return _plan("window.arrange", {"action": "minimize", "title": ""},
                         "окно: свернуть", target={"pronoun": True})
        return _plan("window.arrange", {"action": "minimize", "title": target},
                     "окно: свернуть", target={"needs": "window"} if not target else {})
    if re.match(r"^(?:сверни все|сверни всё|покажи рабочий стол|рабочий стол)", low):
        return _plan("system.hotkey", {"keys": "win+d"}, "окна: рабочий стол")
    match = re.match(r"^(?:разверни|раскрой)(?:\s+(?:окно))?(?:\s+(?P<t>.+?))?(?:\s+на\s+весь\s+экран)?$", low)
    if match and not workshop:
        target = (match.group("t") or "").strip()
        if target in ("его", "ее", "это"):
            return _plan("window.arrange", {"action": "maximize", "title": ""},
                         "окно: развернуть", target={"pronoun": True})
        return _plan("window.arrange", {"action": "maximize", "title": target},
                     "окно: развернуть", target={"needs": "window"} if not target else {})
    match = re.match(r"^(?:закрой|закрыть)(?:\s+(?:окно|программ\w*))?(?:\s+(?P<t>.*))?$", low)
    if match and not workshop:
        target = (match.group("t") or "").strip()
        if target in ("его", "ее", "это", "это окно", "окно"):
            return _plan("window.close", {"title": ""}, "окно: закрыть", target={"pronoun": True})
        return _plan("window.close", {"title": target}, "окно: закрыть",
                     target={"needs": "window"} if not target else {})
    match = re.match(r"^(?:прочитай|что написано в|покажи текст)\s+(?:окн\w*\s*)?(?P<t>.*)$", low)
    if match and "окн" in low:
        return _plan("window.text", {"title": match.group("t").strip()}, "окно: текст")

    # --- клавиши и ввод
    match = re.match(r"^(?:нажми|жми|нажать|зажми)\s+(?:на\s+)?(?:клавиш\w*\s+|сочетание\s+)?(?P<k>.+)$", low)
    if match:
        keys, reason = pc.parse_combo(match.group("k"))
        if not reason:
            return _plan("system.hotkey", {"keys": "+".join(keys)}, "клавиши")
    match = re.match(r"^(?:введи|набери|впиши|напечатай текст)[:\s]+(?P<t>.+)$", phrase, re.IGNORECASE)
    if match:
        return _plan("window.type", {"text": match.group("t")}, "ввод текста")

    # --- программы, сайты, папки
    match = re.match(r"^(?:открой|запусти|включи|открыть|запустить|зайди на|перейди на сайт|открой сайт)\s+(?P<t>.+)$", low)
    if match and not workshop:
        target = re.sub(r"^(?:программу|приложение|сайт|страницу)\s+", "", match.group("t")).strip()
        # «открой телеграм и квазимодо бла»: хвост после «и» из двух и более слов —
        # не имя приложения, а несвязанный мусор или чужая команда; не гадаем
        # и ничего не открываем (И332: непонятая часть не исполняется).
        if re.search(r"(?:\sи\s|\sили\s|,\s*)\S+\s+\S+", target):
            return None
        if re.match(r"^(?:папку\s+)?(?:загрузки|загрузок|скачанное|downloads)$", target):
            return _plan("app.open", {"target": config.DOWNLOADS_FOLDER}, "папка загрузок")
        if target in ("его", "ее", "это", "файл", "первый", "первый файл", "последний"):
            return _plan("app.open", {"target": ""}, "открыть найденное", target={"pronoun": True})
        plan, reason = pc.launch_plan(target, config.PRINTFLOW_URL, config.file_folders())
        if not reason:
            return _plan("app.open", {"target": target}, "открыть", target={"title": plan.get("title")})

    # --- экран
    if re.search(r"(сделай|сними)\s+(скрин|скриншот|снимок экрана)|^скриншот$", low):
        return _plan("screen.shot", {}, "экран: снимок")
    if re.search(r"что (у меня |сейчас )?на экране|опиши экран|что ты видишь", low):
        return _plan("screen.describe", {}, "экран: описать")
    match = re.match(r"^найди на экране\s+(?P<t>.+)$", low)
    if match:
        return _plan("screen.find", {"text": match.group("t")}, "экран: найти")

    # --- буфер обмена
    if re.search(r"что (у меня )?в буфере|прочитай буфер|покажи буфер", low):
        return _plan("clipboard.read", {}, "буфер: прочитать")
    if re.search(r"истори\w* буфера", low):
        return _plan("clipboard.history", {"limit": 10}, "буфер: история")
    match = re.match(r"^(?:скопируй|положи в буфер|запиши в буфер)[:\s]+(?P<t>.+)$", phrase, re.IGNORECASE)
    if match:
        return _plan("clipboard.write", {"text": match.group("t")}, "буфер: записать")

    # --- файлы
    match = re.match(r"^(?:найди|поищи|где)\s+(?:мой\s+|мои\s+)?(?:файл\w*|документ\w*)\s+(?P<t>.+)$", low)
    if match:
        return _plan("files.quick_open", {"name": match.group("t"), "limit": 8}, "файлы: по имени")
    match = re.match(r"^(?:найди|поищи|где)\s+(?:мой\s+|мои\s+|наш\s+)?(?P<t>(?:договор|прайс|счет|сч[её]т|акт|накладн)\w*.*)$", low)
    if match:
        return _plan("files.search", {"query": match.group("t"), "limit": 6}, "файлы: по тексту")
    match = re.match(r"^(?:найди|поищи)\s+в\s+(?:документах|файлах|бумагах)\s+(?P<t>.+)$", low)
    if match:
        return _plan("files.search", {"query": match.group("t"), "limit": 6}, "файлы: по тексту")
    if re.search(r"(недавн\w*|последн\w*|свеж\w*)\s+(файл|документ|загрузк)", low):
        return _plan("files.recent", {"limit": 8}, "файлы: недавние")
    if re.search(r"(разбери|наведи порядок|прибери\w*)\s+(в\s+)?загрузк", low):
        return _plan("files.tidy_plan", {}, "загрузки: план")

    # --- компьютер
    if re.search(r"(как\s+(там\s+)?(компьютер|комп|пк)|здоровье (компьютера|пк)|нагрузк\w* (на )?(компьютер|процессор|пк)|"
                 r"сколько (свободной )?памяти|места на диске|свободн\w* мест|загрузка процессора|температур)", low):
        return _plan("system.health", {}, "пк: здоровье")
    if re.search(r"(какие|список)\s+процесс|что (грузит|тормозит|жрет|ест)\s+(компьютер|комп|пк|процессор|память)", low):
        return _plan("system.process_list", {"limit": 8}, "пк: процессы")
    for pattern, action in ((r"заблокируй\s+(компьютер|экран|пк|комп)|блокировка экрана", "lock"),
                            (r"(усыпи|спящий режим|в сон)\b.*|отправь\s+(компьютер|пк)\s+в\s+сон", "sleep"),
                            (r"перезагрузи\s+(компьютер|пк|комп)|перезагрузка компьютера", "restart"),
                            (r"выключи\s+(компьютер|пк|комп)\b", "shutdown"),
                            (r"отмени\s+(выключение|перезагрузку)", "cancel"),
                            (r"(погаси|выключи)\s+(экран|монитор)", "screen_off")):
        if re.search(pattern, low):
            return _plan("system.power", {"action": action}, f"питание: {action}")

    # --- таймеры и заметки
    match = re.search(r"(?:таймер|засеки|фокус|помодоро|напомни)\w*\s+(?:на\s+|через\s+)?"
                      r"(?P<n>" + when.NUM + r"|полтора\s+часа|полчаса|пол\s*часа)\s*(?P<u>мин\w*|час\w*)?", low)
    if match:
        if match.group("n").startswith("пол"):
            minutes = 90 if "полтора" in match.group("n") else 30
        else:
            number = _word_number(match.group("n")) or 1
            minutes = number * (60 if (match.group("u") or "").startswith("час") else 1)
        tail = phrase[match.end():]
        note = re.sub(r"^\s*(?:на\s+|для\s+|про\s+)?", "", tail).strip(" ,.")[:120]
        return _plan("scheduler.focus_timer", {"minutes": max(1, min(240, int(minutes))),
                                               "note": note}, "таймер")
    if re.search(r"(останови|выключи|сбрось|стоп)\s+таймер", low):
        return _plan("scheduler.focus_stop", {}, "таймер: стоп")
    if re.search(r"(мои|какие|список)\s+таймер", low):
        return _plan("scheduler.focus_list", {"limit": 5}, "таймеры")
    match = re.match(r"^(?:заметка|запиши заметку|напомни мне|запиши)[:\s]+(?P<t>.+)$", phrase, re.IGNORECASE)
    if match and not workshop:
        return _plan("voice.note", {"text": match.group("t")}, "заметка")

    # --- голос
    match = re.match(r"^(?:скажи вслух|произнеси|озвучь|прочитай вслух)[:\s]+(?P<t>.+)$", phrase, re.IGNORECASE)
    if match:
        return _plan("voice.say", {"text": match.group("t")}, "озвучка")

    # --- мета
    if re.search(r"почему (не получилось|не вышло|ошибка|отказ)|что пошло не так", low):
        return _plan("agent.why", {}, "почему")
    if re.search(r"(что ты делал|твой журнал|журнал действий|последние действия)", low):
        return _plan("agent.journal", {"limit": 8}, "журнал")

    # --- день владельца
    if re.search(r"^(доброе утро|брифинг|что на сегодня|план на день|утренн\w* сводк)", low):
        return _plan("day.briefing", {}, "день: брифинг")
    if re.search(r"(итог\w* дня|как прош\w* день|вечерн\w* сводк)", low):
        return _plan("day.summary", {"days": 1}, "день: итог")
    return None


def is_pc_phrase(text: str) -> bool:
    """Команда компьютеру — даже если навык сейчас недоступен (для честного отказа)."""
    return understand(text) is not None


# ---------------------------------------------------------------------------
# 4. Контекст: «его», «ещё», «а на 50», «открой первый»
# ---------------------------------------------------------------------------

def follow_up(text: str, history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """План по прошлой реплике помощника: местоимения и «ещё» без повторения всей фразы."""
    low = normalize_phrase(text).casefold()
    last = next((turn for turn in reversed(history or [])
                 if turn.get("role") == "assistant" and (turn.get("meta") or {}).get("skill")), None)
    if not last:
        return None
    meta = last.get("meta") or {}
    skill = str(meta.get("skill") or "")
    params = dict(meta.get("params") or {})
    target = dict(meta.get("target") or {})
    if re.fullmatch(r"(а\s+)?(теперь\s+)?(еще|ещё)(\s+раз)?|повтори|снова|еще|ещё", low) and skill:
        if skill == "system.volume" and target.get("delta"):
            return _plan(skill, {"delta": target["delta"]}, "повтор: звук")
        if skill == "system.media":
            return _plan(skill, params, "повтор: медиа")
        return _plan(skill, params, "повтор")
    if skill == "system.volume" or skill == "system.media":
        if re.fullmatch(r"(а\s+)?(еще|ещё)\s+(громче|погромче)|(а\s+)?громче", low):
            return _plan("system.volume", {"delta": 10}, "звук: ещё громче")
        if re.fullmatch(r"(а\s+)?(еще|ещё)\s+(тише|потише)|(а\s+)?тише", low):
            return _plan("system.volume", {"delta": -10}, "звук: ещё тише")
        match = re.fullmatch(r"(?:а\s+)?(?:давай\s+)?(?:на\s+)?(" + when.NUM + r")\s*%?", low)
        if match:
            level = _word_number(match.group(1))
            if level is not None:
                return _plan("system.volume", {"level": max(0, min(100, level))}, "звук: уточнение")
    window_title = str(target.get("window") or "")
    if window_title:
        for pattern, plan_skill, extra in ((r"(закрой|закрыть)\s+(его|ее|её|это|окно)", "window.close", {}),
                                           (r"(сверни|спрячь)\s+(его|ее|её|это|окно)", "window.arrange", {"action": "minimize"}),
                                           (r"(разверни)\s+(его|ее|её|это|окно)", "window.arrange", {"action": "maximize"}),
                                           (r"(вернись|переключись)\s+(туда|обратно|к нему|на него)", "window.focus", {})):
            if re.fullmatch(pattern, low):
                return _plan(plan_skill, {**extra, "title": window_title}, "окно из прошлой реплики",
                             target={"window": window_title})
    files = list(target.get("files") or [])
    if files:
        match = re.fullmatch(r"(открой|покажи)\s+(его|ее|её|это|файл|первый|второй|третий|последний)(\s+файл)?", low)
        if match:
            index = {"второй": 1, "третий": 2, "последний": len(files) - 1}.get(match.group(2), 0)
            index = max(0, min(len(files) - 1, index))
            return _plan("app.open", {"target": files[index]}, "файл из прошлой реплики",
                         target={"files": [files[index]]})
    return None


def resolve_pronoun(plan: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    """«Закрой его» без прошлого окна — просим назвать, а не закрываем что попало."""
    if not plan.get("target", {}).get("pronoun"):
        return plan
    for turn in reversed(history or []):
        target = (turn.get("meta") or {}).get("target") or {}
        if target.get("window") and plan["skill"].startswith("window."):
            plan["params"]["title"] = target["window"]
            plan["target"] = {"window": target["window"]}
            return plan
        if target.get("files") and plan["skill"] == "app.open":
            plan["params"]["target"] = target["files"][0]
            plan["target"] = {"files": target["files"][:1]}
            return plan
    plan["clarify"] = ("Какое окно? Назовите его: «сверни телеграм»." if plan["skill"].startswith("window.")
                       else "Что открыть? Сначала найдите файл: «найди файл договор».")
    return plan


# ---------------------------------------------------------------------------
# 5. Ответ словами
# ---------------------------------------------------------------------------

def _plural(number: int, one: str, few: str, many: str) -> str:
    tail = number % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    return one if tail == 1 else few if 2 <= tail <= 4 else many


def summarize(skill: str, result: dict[str, Any]) -> str:
    """Результат навыка — одной-двумя фразами. Отказ — причиной, без «ошибка 500»."""
    if result.get("needs_confirmation") or result.get("queued"):
        text = str(result.get("text") or skill)
        return f"Нужно ваше подтверждение: {text}. Окно «Подтвердить» открыто на этом компьютере."
    if not result.get("ok"):
        reason = str(result.get("reason") or "навык не выполнился")
        return f"Не получилось: {reason}"
    if result.get("say"):
        return str(result["say"])  # личные навыки и обучение (18.22) отвечают готовой фразой
    if skill == "system.volume":
        if result.get("muted"):
            return "Звук выключен."
        level = result.get("level")
        approx = " (примерно — через медиаклавиши)" if result.get("approximate") else ""
        return f"Громкость {level}%{approx}." if level is not None else "Готово."
    if skill == "system.media":
        return f"{result.get('done') or 'Готово'}."
    if skill == "app.open":
        return f"Открываю: {result.get('title') or result.get('target') or 'готово'}."
    if skill == "window.active":
        return f"Сейчас активно окно «{result.get('title')}»."
    if skill == "window.list":
        rows = [str(row.get("title") if isinstance(row, dict) else row) for row in result.get("windows") or []]
        rows = [row for row in rows if not pc.is_own_window(row)]
        if not rows:
            return "Открытых окон не видно."
        shown = ", ".join(f"«{row[:40]}»" for row in rows[:6])
        more = f" и ещё {len(rows) - 6}" if len(rows) > 6 else ""
        return f"Открыто {len(rows)} {_plural(len(rows), 'окно', 'окна', 'окон')}: {shown}{more}."
    if skill in ("window.focus", "window.arrange", "window.close"):
        title = result.get("title") or "окно"
        done = {"window.focus": "на переднем плане", "window.close": "попросил закрыться — программа сама спросит про несохранённое"}.get(
            skill, result.get("done") or "готово")
        return f"«{title}» — {done}."
    if skill == "window.snap":
        left = (result.get("left") or {}).get("title", "")
        right = (result.get("right") or {}).get("title", "")
        return f"Разложил рядом: «{left}» слева, «{right}» справа."
    if skill == "window.text":
        text = str(result.get("text") or "").strip()
        return (f"В окне «{result.get('title')}»: {text[:600]}" if text
                else f"Окно «{result.get('title')}» не отдаёт текст элементов (современные программы рисуют его сами).")
    if skill == "system.health":
        return health_phrase(result)
    if skill == "system.process_list":
        rows = result.get("processes") or []
        if not rows:
            return "Список процессов пуст."
        shown = ", ".join(f"{row.get('name')} ({row.get('mem') or row.get('cpu') or '—'})" for row in rows[:6])
        return f"Процессы сверху списка: {shown}."
    if skill == "system.power":
        return f"{result.get('done') or 'Готово'}."
    if skill == "system.hotkey":
        return f"Нажал {'+'.join(result.get('keys') or [])}."
    if skill == "clipboard.read":
        text = str(result.get("text") or "")
        return f"В буфере: «{text[:400]}{'…' if len(text) > 400 else ''}»" if text else "Буфер пуст."
    if skill == "clipboard.write":
        return "Положил текст в буфер обмена."
    if skill == "clipboard.history":
        rows = result.get("history") or []
        return ("История буфера: " + "; ".join(f"«{str(row.get('text'))[:50]}»" for row in rows[:5])) if rows else "История буфера пуста."
    if skill == "files.quick_open":
        rows = result.get("files") or []
        if not rows:
            return f"Файлов с «{result.get('query')}» в имени не нашёл в разрешённых папках."
        shown = "; ".join(str(row.get("name")) for row in rows[:5])
        return f"Нашёл {len(rows)} {_plural(len(rows), 'файл', 'файла', 'файлов')}: {shown}. Скажите «открой первый»."
    if skill == "files.search":
        rows = result.get("passages") or result.get("results") or []
        if not rows:
            return "В проиндексированных документах этого нет. Если файлы новые — скажите «обнови индекс»."
        first = rows[0]
        return (f"Нашёл {len(rows)} {_plural(len(rows), 'отрывок', 'отрывка', 'отрывков')}. Самый близкий — "
                f"{first.get('title') or first.get('path')}: «{str(first.get('text') or '')[:220]}»")
    if skill == "files.recent":
        rows = result.get("files") or result.get("documents") or []
        return ("Недавние файлы: " + "; ".join(str(row.get("title") or row.get("name") or row.get("path")) for row in rows[:6])
                if rows else "Недавних файлов не видно.")
    if skill == "scheduler.focus_timer":
        timer = result.get("timer") or {}
        end = str(timer.get("end_at") or "")[11:16]
        return f"Таймер на {result.get('minutes') or timer.get('duration_min')} мин запущен" + (f", закончится в {end}." if end else ".")
    if skill == "scheduler.focus_stop":
        return "Таймер остановлен."
    if skill == "voice.say":
        return "Говорю вслух." if result.get("spoken") else str(result.get("hint") or "Готово.")
    if skill == "voice.note":
        return "Заметка сохранена."
    if skill == "screen.shot":
        return f"Снимок экрана сделан ({max(1, int(result.get('size') or 0) // 1024)} КБ), на диск не сохранён."
    if skill == "screen.describe":
        return str(result.get("description") or "Описание пустое.")
    if skill == "screen.find":
        found = result.get("found") or []
        return (f"На экране нашёл: {', '.join(str(item.get('text') if isinstance(item, dict) else item)[:40] for item in found[:5])}."
                if found else "На экране этого не видно.")
    if skill in ("day.briefing", "day.summary"):
        lines = result.get("lines") or []
        return "\n".join(str(line) for line in lines[:8]) or str(result.get("text") or "Сводка пуста.")
    if skill == "panel.ask":
        return str(result.get("answer") or result.get("reason") or "Панель не ответила.")
    if skill == "agent.why":
        return str(result.get("text") or result.get("reason") or result.get("hint") or "Отказов в журнале нет.")
    if skill == "agent.journal":
        rows = result.get("entries") or []
        return ("Последнее: " + "; ".join(f"{row.get('skill')} — {row.get('outcome')}" for row in rows[:6])) if rows else "Журнал пуст."
    hint = str(result.get("hint") or "").strip()
    return hint or "Готово."


def _ru(number: Any) -> str:
    """Число по-русски: десятичная запятая («3,8 ГБ», а не «3.8 ГБ»)."""
    return str(number).replace(".", ",")


def health_phrase(state: dict[str, Any]) -> str:
    """Снимок здоровья компьютера словами, с тем, на что стоит обратить внимание."""
    parts = []
    cpu = state.get("cpu_percent")
    if isinstance(cpu, (int, float)):
        parts.append(f"процессор {cpu:.0f}%")
    memory = state.get("memory") or {}
    if memory.get("total_gb"):
        parts.append(f"память {memory.get('load')}% ({_ru(memory.get('used_gb'))} из {_ru(memory.get('total_gb'))} ГБ)")
    for disk in (state.get("disks") or [])[:3]:
        parts.append(f"диск {disk.get('mount')} свободно {_ru(disk.get('free_gb'))} ГБ")
    if state.get("uptime_hours") is not None:
        parts.append(f"работает {_ru(state['uptime_hours'])} ч")
    battery = state.get("battery") or {}
    if battery:
        parts.append(f"батарея {battery.get('percent')}%" + (" на зарядке" if battery.get("plugged") else ""))
    text = ("Компьютер: " + ", ".join(parts) + ".") if parts else "Состояние компьютера не прочиталось."
    warnings = pc.health_warnings(state)
    if warnings:
        text += " Внимание: " + "; ".join(warnings) + "."
    return text


def suggestions_for(skill: str, result: dict[str, Any]) -> list[str]:
    """Что разумно сказать дальше — кнопками под ответом."""
    if skill in ("system.volume", "system.media"):
        return ["Громче", "Тише", "Выключи звук"]
    if skill == "window.list":
        rows = [str(row.get("title") if isinstance(row, dict) else row) for row in result.get("windows") or []]
        rows = [row for row in rows if not pc.is_own_window(row)]
        return [f"Переключись на {row.split(' - ')[-1][:24]}" for row in rows[:3]]
    if skill in ("window.focus", "window.arrange"):
        return ["Сверни его", "Разверни его", "Какие окна открыты?"]
    if skill == "system.health":
        return ["Что грузит компьютер?", "Недавние файлы"]
    if skill == "files.quick_open" and result.get("files"):
        return ["Открой первый", "Открой второй"]
    if skill == "scheduler.focus_timer":
        return ["Мои таймеры", "Останови таймер"]
    personal = {"reminder.add": ["Мои напоминания"], "reminder.list": ["Мой день"],
                "goal.add": ["Мои цели"], "goal.progress": ["Мои цели"], "habit.add": ["Мои привычки"],
                "habit.check": ["Мои привычки"], "expense.add": ["Сколько я потратил за неделю?"],
                "learn.list": ["Что ты не понял?"], "learn.unknown": ["Чему ты научился?"],
                "me.today": ["Мои напоминания", "Мои цели"]}
    if skill in ("list.add", "list.remove"):
        return ["Что купить?"] if result.get("list") == "покупки" or "покупки" in str(result.get("params")) else ["Мои списки"]
    if skill == "me.today":
        return [str(item) for item in (result.get("suggestions") or [])][:2] + personal[skill]
    return personal.get(skill, [])


def _rule_step(text: str, personal: Any, now: datetime.datetime) -> dict[str, Any] | None:
    """Одна часть урока → шаг по правилам. «Живые» навыки (время «сейчас») хранятся фразой."""
    phrase = normalize_phrase(text)
    found = understand(phrase)
    if found and not found.get("clarify") and not (found.get("target") or {}).get("pronoun"):
        return {"say": text, "skill": found["skill"], "params": found["params"]}
    intent = intents.personal_intent(phrase, personal, now) if personal is not None else None
    if intent and intent.get("skill"):
        if is_live(intent["skill"]):
            return {"say": text, "live": intent["skill"]}  # «напомни через 5 минут» — время считается при вызове
        return {"say": text, "skill": intent["skill"], "params": intent["params"]}
    return None


def resolve_meaning(meaning: str, personal: Any = None, now: datetime.datetime | None = None) -> list[dict[str, Any]]:
    """Смысл урока → шаги. Понятое правилами — план навыка; остальное — фраза, её поймёт мозг при вызове.

    «Открой телеграм и поставь громкость 30» — два шага; «что печатается» —
    фраза (её ответит панель); «напомни через 5 минут» — фраза, потому что
    застывшее «через 5 минут» от момента урока было бы неправдой.
    """
    now = now or datetime.datetime.now()
    parts = intents.split_meaning(meaning)
    if len(parts) > 1:
        # «Громкость 30 и открой блокнот»: правило громкости съело бы всю фразу —
        # поэтому сначала части, и только если каждая понятна сама по себе.
        split = [_rule_step(part, personal, now) for part in parts]
        if all(split):
            return [step for step in split if step]
    whole = _rule_step(meaning, personal, now)
    if whole:
        return [whole]
    if len(parts) <= 1:
        return [{"say": meaning}]
    return [_rule_step(part, personal, now) or {"say": part} for part in parts]


def _short(text: str, limit: int = 60) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[:limit - 1].rstrip() + "…"


_ALIAS_VERBS = re.compile(r"^(?:что|кто|как|где|когда|сколько|какой|какая|какие|почему|зачем|покажи|открой|включи|"
                          r"выключи|найди|запусти|сделай|поставь|закрой|напомни|добавь)\b", re.IGNORECASE)


def looks_like_alias(phrase: str, meaning: str, hint: bool = False) -> bool:
    """««Телега» — это телеграм» — синоним слова, а не команда: коротко и без глагола."""
    if hint:
        return len(phrase.split()) <= 3 and len(meaning.split()) <= 4
    return len(phrase.split()) <= 2 and len(meaning.split()) <= 2 and not _ALIAS_VERBS.match(meaning.strip())


# ---------------------------------------------------------------------------
# 6. Мозг целиком
# ---------------------------------------------------------------------------

_PLANNER_RULES = (
    "Ты — помощник NOZZA на компьютере владельца мастерской 3D-печати. Ты управляешь "
    "компьютером только через навыки из списка ниже и отвечаешь по-русски.\n"
    "Верни ОДИН JSON-объект: {\"skill\": \"имя навыка или пустая строка\", "
    "\"params\": {…}, \"reply\": \"короткий ответ человеку\", \"ask\": \"уточняющий вопрос или пустая строка\"}.\n"
    "Правила:\n"
    "1. Бери навык только из списка. Нет подходящего — skill пустой, ответь сам в reply.\n"
    "2. Параметры — только объявленные у навыка и только из слов человека или контекста.\n"
    "3. Если не хватает важного (какое окно, какой файл) — заполни ask и не выбирай навык.\n"
    "4. Вопросы про заказы, клиентов, деньги, печать и склад — навык panel.ask с question.\n"
    "5. Не выдумывай факты: если не знаешь — так и скажи в reply.\n"
    "6. reply — одно-два предложения, без markdown.\n"
    "7. Напоминание — reminder.add: время словами в when («через 20 минут», «завтра в 10»), о чём — в text. "
    "Списки, цели, привычки, расходы, дневник — навыки list.*, goal.*, habit.*, expense.*, diary.*."
)


class Brain:
    """Разговор с помощником: правила → контекст → память → модель → навык → ответ."""

    def __init__(self, agent: Any, clock: Callable[[], datetime.datetime] | None = None) -> None:
        self.agent = agent
        self.clock = clock or datetime.datetime.now
        self._panel_down = False  # последний вопрос панели остался без ответа (для честного «панель молчит»)
        # Откат «верни как было» (И334): прежний уровень громкости по сессиям.
        self._undo: dict[str, dict[str, Any]] = {}
        # Вложенный разговор (шаг выученной команды) не пишет реплики и не ищет
        # выученное повторно — так урок не может вызвать сам себя.
        self._local = threading.local()
        if clock is not None:
            try:
                agent.runner.clock = clock  # навыки считают «сейчас» по тем же часам, что и мозг
            except Exception:  # noqa: BLE001
                pass

    # --- доступ к агенту ------------------------------------------------
    @property
    def runner(self) -> Any:
        return self.agent.runner

    @property
    def store(self) -> Any:
        return self.runner.store

    @property
    def learning(self) -> Any:
        return self.runner.learning

    @property
    def personal(self) -> Any:
        return self.runner.personal

    def _run(self, name: str, params: dict[str, Any], popup: bool = True) -> dict[str, Any]:
        """Навык через агента: запись и системное — через подтверждение человека.

        `popup=False` — разговор идёт в окне агента, подтверждение там же
        карточкой; всплывающее окно подтверждения не нужно.
        """
        run = getattr(self.agent, "run_skill", None)
        if callable(run):
            if not popup and "ask" in inspect.signature(run).parameters:
                return run(name, params, ask=False)
            return run(name, params)
        return self.runner.run(name, params)

    # --- главный вход -----------------------------------------------------
    def chat(self, text: str, session: str = "main", mode: str = "full",
             plan: dict[str, Any] | None = None) -> dict[str, Any]:
        """Реплика человека → ответ. `mode="pc"` — только команды компьютеру, без модели.

        `plan` — готовый план от панели (навык и параметры, выбранные её
        планировщиком): здесь он проходит те же проверки реестра и подтверждения.
        """
        started = time.time()
        session = session_key(session)
        clean = " ".join(str(text or "").split())[:MAX_TEXT]
        steps: list[dict[str, Any]] = []
        if not clean and not plan:
            return self._reply(session, "", "Скажите или напишите, что сделать.", kind="clarify",
                               handled=False, steps=steps, started=started, save=False)
        history = self._history(session)

        if plan and plan.get("skill"):
            steps.append({"kind": "plan", "title": "План от панели", "detail": str(plan.get("skill"))})
            return self._execute(session, clean or str(plan.get("skill")), _plan(
                str(plan["skill"]), plan.get("params") if isinstance(plan.get("params"), dict) else {},
                "план панели"), history, steps, started, source="panel")

        nested = bool(getattr(self._local, "nested", False))
        now = self.clock()
        if not nested:
            taught = self._learning_turn(session, clean, history, steps, started, mode)
            if taught:
                return taught
        # Синонимы владельца («телега» → «телеграм») — для правил и выученного.
        work = clean if nested else self._with_aliases(clean, steps)
        if not nested:
            learned = self._learned(work, ("correction", "taught"))
            if learned:
                return self._run_learned(session, clean, learned, history, steps, started)

        if mode != "pc":
            counted = intents.util_answer(normalize_phrase(work), now)
            if counted:
                steps.append({"kind": "rule", "title": "Посчитал сам", "detail": counted[1]})
                return self._reply(session, clean, counted[0], kind="answer", source="util", steps=steps,
                                   started=started)
            answer = clock_answer(clean, self.clock())
            if answer:
                steps.append({"kind": "rule", "title": "Часы и календарь", "detail": "без модели"})
                return self._reply(session, clean, answer, kind="answer", source="clock",
                                   steps=steps, started=started)
            answer, value = math_value(clean)
            if answer:
                steps.append({"kind": "rule", "title": "Арифметика", "detail": "посчитал сам, без модели"})
            else:
                answer, value = math_follow_up(clean, self._last_number(history))
                if answer:
                    steps.append({"kind": "context", "title": "Понял по прошлой реплике",
                                  "detail": "действие над прошлым результатом"})
            if answer:
                return self._reply(session, clean, answer, kind="answer", source="math", steps=steps, started=started,
                                   target={"number": value} if value is not None else None)
            talk = small_talk(clean)
            if talk:
                return self._small_talk(session, clean, talk, steps, started)
            if re.search(r"что ты умеешь|что умеешь|^помощь$|^help$|твои навыки|список навыков|чем (ты )?можешь помочь",
                         clean.casefold()):
                steps.append({"kind": "rule", "title": "Реестр навыков", "detail": "готовые навыки"})
                return self._reply(session, clean, capabilities_text(self.runner.catalog()), kind="answer",
                                   source="registry", steps=steps, started=started,
                                   suggestions=["Как там компьютер?", "Какие окна открыты?", "Громкость 30"])
            memory_reply = self._memory(clean)
            if memory_reply:
                steps.append({"kind": "memory", "title": "Память", "detail": memory_reply["op"]})
                if memory_reply["op"] == "recall" and memory_reply.get("payload") and not memory_reply.get("rows"):
                    # «Что ты знаешь про Иванова»: в памяти пусто — может, это клиент или станок цеха.
                    known = self._ask_panel(session, clean, steps, started)
                    if known:
                        return known
                return self._reply(session, clean, memory_reply["text"], kind="memory", source="memory",
                                   steps=steps, started=started, extra={"memory": memory_reply.get("rows", [])})

        if not nested:
            cancelled = self._cancel_turn(session, clean, work, steps, started)
            if cancelled:
                return cancelled
            chained = self._chain_turn(session, clean, work, history, steps, started, now)
            if chained:
                return chained

        mine = self._personal_turn(session, clean, work, history, steps, started, now)
        if mine:
            return mine

        found = follow_up(work, history)
        if found:
            steps.append({"kind": "context", "title": "Понял по прошлой реплике", "detail": found["why"]})
        else:
            found = understand(work)
            if found:
                steps.append({"kind": "rule", "title": "Понял без модели", "detail": found["why"]})
        if found:
            found = resolve_pronoun(found, history)
            if found.get("clarify"):
                return self._reply(session, clean, found["clarify"], kind="clarify", steps=steps, started=started)
            if (found.get("target") or {}).get("needs") == "window" and not found["params"].get("title"):
                asked = self._window_clarify(session, clean, found, steps, started)
                if asked:
                    return asked
            return self._execute(session, clean, found, history, steps, started)

        if not nested:
            learned = self._learned(work, ("self",))
            if learned:
                return self._run_learned(session, clean, learned, history, steps, started)

        if mode == "pc":
            return self._reply(session, clean, "", kind="skip", handled=False, steps=steps,
                               started=started, save=False)
        # Вопрос не про компьютер. Про цех (или продолжение разговора с панелью)
        # и всегда, когда своей модели нет, — сначала мозг панели: у него база
        # станков, заказов и денег. Иначе своя модель выберет навык сама
        # (среди них и `panel.ask`), не тратя время на второй мозг.
        state = model.status()
        last = history[-1] if history and history[-1].get("role") == "assistant" else {}
        workshop = any(word in f"{clean.casefold()} " for word in _PANEL_WORDS)
        if workshop or not state.get("ok") or (last.get("meta") or {}).get("source") == "panel":
            answer = self._ask_panel(session, clean, steps, started)
            if answer:
                return answer
        return self._think(session, clean, history, steps, started, state=state, workshop=workshop)

    # --- разговор -----------------------------------------------------------
    def _owner_name(self) -> str:
        try:
            rows = self.store.memories(5, kind="profile")
        except Exception:
            return ""
        row = next((row for row in rows if row.get("subject") == "имя"), None)
        return row["text"].replace("Владельца зовут", "").strip() if row else ""

    @staticmethod
    def _last_number(history: list[dict[str, Any]]) -> float | None:
        """Число из прошлого ответа агента (арифметика) — для «а если поделить на 5?»."""
        if not history or history[-1].get("role") != "assistant":
            return None
        value = ((history[-1].get("meta") or {}).get("target") or {}).get("number")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _small_talk(self, session: str, text: str, talk: str, steps: list[dict[str, Any]],
                    started: float) -> dict[str, Any]:
        """Привет / как дела / кто ты — по-человечески и с живой сводкой цеха, без модели."""
        name = self._owner_name()
        farm, panel_note = "", ""
        if talk in ("greet", "how"):
            client = getattr(self.runner, "panel", None)
            try:
                context = client.context() if client is not None and hasattr(client, "context") else {}
            except Exception:
                context = {}
            if context.get("ok"):
                farm = " ".join(str(context.get("farm") or "").split())
                name = name or " ".join(str(context.get("owner") or "").split())
                steps.append({"kind": "panel", "title": "Сводка цеха", "detail": "от панели"})
            else:
                panel_note = " Панель цеха сейчас не отвечает — про станки скажу, когда она вернётся."
                steps.append({"kind": "panel", "title": "Панель цеха", "detail": str(context.get("reason") or "не отвечает")})
        hello = f"Привет, {name}!" if name else "Привет!"
        mine = self._personal_hint(self.clock()) if talk == "greet" else ""
        texts = {
            "greet": (f"{hello} {farm}{mine}" if farm else
                      f"{hello} Я на связи: компьютер, окна, звук, файлы, память и личные дела.{mine}{panel_note}"),
            "how": "Работаю, всё под контролем." + (f" {farm}" if farm else panel_note),
            "who": ("Я NOZZA — помощник цеха на этом компьютере. Сам управляю окнами, звуком, программами и файлами, "
                    "помню ваши просьбы, а про станки, заказы и деньги спрашиваю панель цеха. Всё, что меняет "
                    "систему или цех, — только после вашего «Подтвердить»."),
            "thanks": f"Пожалуйста{', ' + name if name else ''}! Обращайтесь.",
            "bye": "До связи! Если что — позовите.",
            "ok": "Хорошо. Если что — я рядом.",
        }
        steps.append({"kind": "rule", "title": "Разговор", "detail": {"greet": "приветствие", "how": "как дела",
                      "who": "кто я", "thanks": "благодарность", "bye": "прощание", "ok": "согласие"}[talk]})
        chips = ["Что сейчас печатается?", "Как там компьютер?", "Что ты умеешь?"] if talk in ("greet", "how", "who") else []
        if talk == "greet":
            try:  # привычки владельца: «в это время вы обычно…» — первыми кнопками
                chips = self.learning.suggestions(self.clock()) + ["Мой день"] + chips[:2]
            except Exception:  # noqa: BLE001
                pass
        return self._reply(session, text, texts[talk].strip(), kind="answer", source="talk", steps=steps,
                           started=started, suggestions=chips)

    # --- панель цеха ----------------------------------------------------------
    def _ask_panel(self, session: str, text: str, steps: list[dict[str, Any]],
                   started: float) -> dict[str, Any] | None:
        """Спросить мозг панели. None — панель молчит или сама не поняла (тогда решает агент).

        Панель отвечает со своей базой и своим контекстом («его», «второй»);
        `delegate: False` в клиенте не даёт ей вернуть вопрос агенту — петли нет.
        Предложенное панелью действие не исполняется здесь: оно становится
        планом `panel.do` и ждёт «Подтвердить» в окне агента, как любая запись.
        """
        client = getattr(self.runner, "panel", None)
        if client is None or not callable(getattr(client, "chat", None)):
            return None
        try:
            data = client.chat(text, session=panel_session(session), source="agent")
        except Exception as exc:  # noqa: BLE001 — панель не должна ронять разговор
            data = {"ok": False, "reason": exc.__class__.__name__}
        data = data if isinstance(data, dict) else {}
        reply = str(data.get("reply") or "").strip()
        if not reply or data.get("kind") == "error":
            self._panel_down = True
            steps.append({"kind": "panel", "title": "Панель цеха", "detail": str(data.get("reason") or "не отвечает")})
            return None
        self._panel_down = False
        if data.get("understood") is False or (data.get("kind") == "clarify" and data.get("source") == "rules"):
            steps.append({"kind": "panel", "title": "Панель цеха", "detail": "это не про цех"})
            return None
        source = str(data.get("source") or "")
        steps.append({"kind": "panel", "title": "Ответила панель цеха", "detail": _PANEL_SOURCES.get(source, source)})
        for step in list(data.get("steps") or [])[:4]:
            if isinstance(step, dict) and step.get("title"):
                steps.append({"kind": "panel", "title": f"Панель: {step['title']}",
                              "detail": str(step.get("detail") or "")[:160]})
        chips = [str(item) for item in (data.get("suggestions") or []) if str(item or "").strip()][:4]
        action = data.get("action") if isinstance(data.get("action"), dict) else None
        if data.get("kind") == "action" and action and action.get("id"):
            explain = re.sub(r"\s*Подтвердите в карточке\.?\s*$", "", reply).strip().rstrip(".") \
                or str(action.get("title") or action["id"])
            plan = _plan("panel.do", {"action": str(action["id"]),
                                      "params": dict(data.get("params") or {}) if isinstance(data.get("params"), dict) else {},
                                      "explain": explain[:240]},
                         "предложила панель", reply=f"{explain}. Подтвердите в карточке.")
            return self._execute(session, text, plan, self._history(session), steps, started, source="panel")
        extra: dict[str, Any] = {"panel_asked": True}
        link = data.get("link") if isinstance(data.get("link"), dict) else None
        href = str((link or {}).get("href") or "")
        if link and href.startswith("/") and not href.startswith("//"):
            url = f"{client.url}{href}"
            title = " ".join(str(link.get("title") or "Панель").split())[:60]
            extra["link"] = {"title": title, "href": url}
            if data.get("kind") == "navigate":
                opened, why = pc.open_url(url)
                steps.append({"kind": "skill", "title": "Браузер", "detail": "открыл раздел панели" if opened else why})
                reply = (f"Открыл в браузере раздел «{title}» панели цеха." if opened
                         else f"Раздел «{title}» — в панели цеха: ссылка ниже.")
        kind = str(data.get("kind") or "answer")
        return self._reply(session, text, reply, kind=kind if kind in ("answer", "clarify", "memory") else "answer",
                           source="panel", steps=steps, started=started, suggestions=chips, extra=extra)

    # --- память -----------------------------------------------------------
    def _memory(self, text: str) -> dict[str, Any] | None:
        op, payload = memory_command(text)
        if not op:
            return None
        if op == "remember":
            saved = self.store.remember(payload, kind="fact", source="chat")
            if not saved.get("ok"):
                return {"op": op, "text": saved.get("reason") or "Не запомнил."}
            if saved.get("duplicate"):
                return {"op": op, "text": f"Это я уже помню: «{saved['memory']['text']}»."}
            return {"op": op, "text": f"Запомнил: «{saved['memory']['text']}».", "rows": [saved["memory"]]}
        if op == "forget":
            rows = self.store.forget(payload)
            if not rows:
                return {"op": op, "text": f"В памяти нет записи про «{payload}». Скажите «что ты помнишь», чтобы увидеть всё."}
            return {"op": op, "text": "Забыл: " + "; ".join(f"«{row['text']}»" for row in rows) + ".", "rows": rows}
        if op == "name":
            saved = self.store.remember(f"Владельца зовут {payload}", kind="profile", subject="имя", source="chat")
            return {"op": op, "text": f"Приятно познакомиться, {payload}! Запомнил.", "rows": [saved.get("memory", {})]}
        if op == "whoami":
            rows = self.store.memories(5, kind="profile")
            name_row = next((row for row in rows if row.get("subject") == "имя"), None)
            if not name_row:
                return {"op": op, "text": "Я пока не знаю, как вас зовут. Скажите: «меня зовут …»."}
            return {"op": op, "text": name_row["text"].replace("Владельца зовут", "Вас зовут") + "."}
        rows = self.store.recall(payload, 8) if payload else self.store.memories(12)
        if not rows:
            return {"op": op, "payload": payload, "text": ("Про это в памяти ничего нет." if payload
                                                           else "Память пока пуста. Скажите «запомни, что …».")}
        lines = [f"• {row['text']}" for row in rows[:8]]
        head = f"Про «{payload}» помню:" if payload else "Вот что я помню:"
        return {"op": op, "text": head + "\n" + "\n".join(lines), "rows": rows}

    # --- обучение (18.22) ---------------------------------------------------
    def _learning_turn(self, session: str, text: str, history: list[dict[str, Any]], steps: list[dict[str, Any]],
                       started: float, mode: str) -> dict[str, Any] | None:
        """Ответ на уточнение, урок, поправка. None — реплика не про это."""
        phrase = normalize_phrase(text)
        last = history[-1] if history and history[-1].get("role") == "assistant" else {}
        awaiting = (last.get("meta") or {}).get("awaiting")
        if isinstance(awaiting, dict) and awaiting.get("kind"):
            done = self._awaiting(session, text, phrase, awaiting, history, steps, started)
            if done:
                return done
        if mode == "pc":
            return None
        lesson = intents.teach_command(phrase)
        if lesson:
            done = self._lesson(session, text, lesson, history, steps, started)
            if done:
                return done
        fix = intents.correction(phrase)
        if fix is not None:
            return self._correction(session, text, fix, history, steps, started)
        return None

    def _awaiting(self, session: str, text: str, phrase: str, awaiting: dict[str, Any], history: list[dict[str, Any]],
                  steps: list[dict[str, Any]], started: float) -> dict[str, Any] | None:
        """Прошлая реплика помощника была вопросом — эта, возможно, ответ на него."""
        kind = str(awaiting.get("kind") or "")
        if intents.CANCEL_RE.match(phrase):
            steps.append({"kind": "context", "title": "Уточнение", "detail": "владелец передумал"})
            return self._reply(session, text, "Хорошо, не буду.", kind="answer", source="teach", steps=steps,
                               started=started)
        now = self.clock()
        if kind in ("teach", "correction"):
            meaning = intents.this_means(phrase) if kind == "teach" else (intents.this_means(phrase) or phrase)
            target = str(awaiting.get("phrase") or "")
            if not meaning or not target or normalize_phrase(meaning).casefold() == normalize_phrase(target).casefold():
                return None
            steps.append({"kind": "context", "title": "Ответ на мой вопрос", "detail": "что значила прошлая фраза"})
            plan_steps = resolve_meaning(meaning, self.personal, now)
            return self._learn_now(session, text, target, meaning, plan_steps,
                                   "taught" if kind == "teach" else "correction", history, steps, started)
        if kind == "remind_when":
            parsed = when.parse(phrase, now)
            if not parsed:
                return None
            steps.append({"kind": "context", "title": "Ответ на «когда напомнить»", "detail": parsed["label"]})
            if parsed["past"]:
                return self._reply(session, text, f"Это время уже прошло ({parsed['label']}). Когда напомнить?",
                                   kind="clarify", source="personal", steps=steps, started=started,
                                   extra={"awaiting": awaiting})
            about = str(awaiting.get("text") or "") or parsed["text"] or "Напоминание"
            return self._execute(session, text, {**_plan("reminder.add", {"text": about, "due": parsed["iso"],
                                                                           "repeat": parsed["repeat"]}, "напоминание"),
                                                 "source": "personal"}, history, steps, started, source="personal")
        if kind == "remind_text":
            if len(phrase) < 2 or text.rstrip().endswith("?") or understand(phrase):
                return None
            steps.append({"kind": "context", "title": "Ответ на «о чём напомнить»", "detail": _short(phrase)})
            return self._execute(session, text, {**_plan("reminder.add", {
                "text": phrase, "due": str(awaiting.get("iso") or ""), "repeat": str(awaiting.get("repeat") or "")},
                "напоминание"), "source": "personal"}, history, steps, started, source="personal")
        if kind == "window":
            options = [str(row) for row in (awaiting.get("options") or []) if str(row).strip()]
            pick = pick_option(phrase, options)
            if not pick:
                return None
            steps.append({"kind": "context", "title": "Ответ на «какое окно»", "detail": _short(pick)})
            params = dict(awaiting.get("params") or {})
            params["title"] = pick
            plan = _plan(str(awaiting.get("skill") or ""), params, "окно: выбрано из списка")
            plan["target"] = {"window": pick}
            return self._execute(session, text, plan, history, steps, started)
        return None

    def _lesson(self, session: str, text: str, lesson: dict[str, Any], history: list[dict[str, Any]],
                steps: list[dict[str, Any]], started: float) -> dict[str, Any] | None:
        """«Научись…», «забудь команду…», «чему ты научился», «что ты не понял»."""
        op = lesson["op"]
        titles = {"list": "что выучено", "unknown": "что не понято", "habits": "привычки", "forget": "забыть",
                  "answer": "урок: ответ", "teach": "урок: команда"}
        if op == "teach" and not lesson.get("explicit", True):
            # ««Рыжик» — это клиент из Питера» — факт, а не урок: урок, только если
            # смысл — понятная команда или короткий синоним.
            meaning = str(lesson.get("meaning") or "")
            implied = resolve_meaning(meaning, self.personal, self.clock())
            if not any(step.get("skill") or step.get("live") for step in implied) \
                    and not looks_like_alias(str(lesson.get("phrase") or ""), meaning):
                return None
        steps.append({"kind": "learned", "title": "Обучение", "detail": titles.get(op, op)})
        if op in ("list", "unknown", "habits", "forget"):
            skill = {"list": "learn.list", "unknown": "learn.unknown", "habits": "learn.habits",
                     "forget": "learn.forget"}[op]
            params = {"phrase": lesson["phrase"]} if op == "forget" else {}
            return self._execute(session, text, {**_plan(skill, params, "обучение"), "source": "teach"}, history,
                                 steps, started, source="teach")
        if op == "answer":
            saved = self.learning.teach(lesson["phrase"], answer=lesson["answer"],
                                        meaning=f"ответ «{lesson['answer']}»", source="taught")
            reply = (f"Запомнил: на «{_short(lesson['phrase'])}» отвечу «{_short(lesson['answer'], 120)}»."
                     if saved.get("ok") else f"Не запомнил: {saved.get('reason')}")
            return self._reply(session, text, reply, kind="answer" if saved.get("ok") else "error", source="teach",
                               steps=steps, started=started)
        phrase, meaning = str(lesson.get("phrase") or ""), str(lesson.get("meaning") or "")
        if lesson.get("split"):
            phrase, meaning = self._split_lesson(str(lesson["split"]))
        if not phrase or not meaning:
            return self._reply(session, text, "Не понял, где ваша фраза, а где действие. Скажите так: когда я говорю "
                                              "«рабочий режим» — открой телеграм и громкость 30.",
                               kind="clarify", source="teach", steps=steps, started=started)
        plan_steps = resolve_meaning(meaning, self.personal, self.clock())
        commands = [step for step in plan_steps if step.get("skill") or step.get("live")]
        if not commands and looks_like_alias(phrase, meaning, bool(lesson.get("alias_hint"))):
            saved = self.learning.set_alias(phrase, meaning)
            if saved.get("ok"):
                steps.append({"kind": "learned", "title": "Синоним", "detail": f"{saved['word']} → {saved['meaning']}"})
                return self._reply(session, text, f"Запомнил: «{phrase}» — это «{meaning}». Теперь, например, "
                                                  f"«открой {phrase}» пойму как «открой {meaning}».",
                                   kind="answer", source="teach", steps=steps, started=started,
                                   suggestions=["Чему ты научился?"])
        saved = self.learning.teach(phrase, plan_steps, meaning=meaning, source="taught")
        if not saved.get("ok"):
            return self._reply(session, text, f"Не запомнил: {saved.get('reason')}", kind="error", source="teach",
                               steps=steps, started=started)
        tail = "" if commands else " Правилами это пока не разбирается — при вызове спрошу панель или модель."
        again = " Переучился: раньше эта фраза значила другое." if saved.get("replaced") else ""
        steps.append({"kind": "learned", "title": "Запомнил урок", "detail": describe_steps(plan_steps)})
        return self._reply(session, text, f"Запомнил: «{phrase}» → {describe_steps(plan_steps)}.{again}{tail} "
                                          f"Скажите «{phrase}» — сделаю.",
                           kind="answer", source="teach", steps=steps, started=started,
                           suggestions=[phrase[:40][:1].upper() + phrase[:40][1:], "Чему ты научился?"])

    @staticmethod
    def _split_lesson(rest: str) -> tuple[str, str]:
        """«когда я говорю рабочий режим открой телеграм» без запятой: действие — первая понятная правилам часть."""
        words = rest.split()
        for index in range(1, len(words)):
            tail = " ".join(words[index:])
            if understand(tail):
                return " ".join(words[:index]).strip(" ,.«»\""), tail
        return "", ""

    def _correction(self, session: str, text: str, fix: dict[str, Any], history: list[dict[str, Any]],
                    steps: list[dict[str, Any]], started: float) -> dict[str, Any] | None:
        """«Нет, я имел в виду …» — выполнить правильное и запомнить поправку для прошлой фразы."""
        if len(history) < 2 or history[-1].get("role") != "assistant" or history[-2].get("role") != "user":
            return None
        meta = history[-1].get("meta") or {}
        if meta.get("source") in ("teach", "talk"):
            return None
        previous = str(history[-2].get("text") or "")
        meaning = str(fix.get("meaning") or "")
        if not meaning:
            if not (meta.get("skill") or meta.get("source") in ("learned", "model", "personal", "util", "panel")):
                return None
            if meta.get("learned_id"):
                self.learning.bad(int(meta["learned_id"]))
            steps.append({"kind": "learned", "title": "Поправка", "detail": "жду, что было нужно"})
            return self._reply(session, text, f"Понял, с «{_short(previous)}» я ошибся. Что нужно было сделать? "
                                              "Скажите — я запомню.",
                               kind="clarify", source="teach", steps=steps, started=started,
                               extra={"awaiting": {"kind": "correction", "phrase": previous}})
        plan_steps = resolve_meaning(meaning, self.personal, self.clock())
        if not any(step.get("skill") or step.get("live") for step in plan_steps) and not fix.get("marked"):
            return None  # «нет звука» — жалоба, а не поправка
        if meta.get("learned_id"):
            self.learning.bad(int(meta["learned_id"]))
        steps.append({"kind": "learned", "title": "Поправка", "detail": f"«{_short(previous)}» → {_short(meaning)}"})
        return self._learn_now(session, text, previous, meaning, plan_steps, "correction", history, steps, started)

    def _learn_now(self, session: str, text: str, phrase: str, meaning: str, plan_steps: list[dict[str, Any]],
                   source: str, history: list[dict[str, Any]], steps: list[dict[str, Any]],
                   started: float) -> dict[str, Any]:
        """Запомнить «фраза → смысл» и сразу сделать смысл: человек ведь этого и хотел."""
        saved = self.learning.teach(phrase, plan_steps, meaning=meaning, source=source)
        if not saved.get("ok"):
            return self._reply(session, text, f"Не запомнил: {saved.get('reason')}", kind="error", source="teach",
                               steps=steps, started=started)
        head = f"Понял: «{_short(phrase)}» — это «{_short(meaning)}». Запомнил."
        return self._run_steps_plan(session, text, plan_steps, history, steps, started,
                                    extra={"learned_id": saved["entry"]["id"]}, head=head)

    def _with_aliases(self, text: str, steps: list[dict[str, Any]]) -> str:
        try:
            out, applied = self.learning.apply_aliases(text)
        except Exception:  # noqa: BLE001 — синонимы не должны ронять разговор
            return text
        if applied:
            steps.append({"kind": "learned", "title": "Ваши слова",
                          "detail": ", ".join(f"{word} → {meaning}" for word, meaning in applied)})
        return out

    def _learned(self, text: str, sources: tuple[str, ...]) -> dict[str, Any] | None:
        try:
            return self.learning.match(text, sources)
        except Exception:  # noqa: BLE001
            return None

    def _run_learned(self, session: str, text: str, match: dict[str, Any], history: list[dict[str, Any]],
                     steps: list[dict[str, Any]], started: float) -> dict[str, Any]:
        entry = match["entry"]
        self.learning.used(entry["id"])
        steps.append({"kind": "learned", "title": "Выученное",
                      "detail": f"«{_short(entry['phrase'])}» · {entry['source_title']} · {match['how']}"})
        extra = {"learned_id": entry["id"]}
        if entry.get("kind") == "answer":
            return self._reply(session, text, entry["answer"], kind="answer", source="learned", steps=steps,
                               started=started, extra=extra)
        return self._run_steps_plan(session, text, match["steps"], history, steps, started, extra=extra)

    def _run_steps_plan(self, session: str, text: str, plan_steps: list[dict[str, Any]], history: list[dict[str, Any]],
                        steps: list[dict[str, Any]], started: float, extra: dict[str, Any] | None = None,
                        head: str = "", origin: str = "learned") -> dict[str, Any]:
        """Шаги выученного подряд: навык — через реестр и подтверждение, фраза — через мозг."""
        plan_steps = [step for step in plan_steps if isinstance(step, dict)][:8]
        if not plan_steps:
            return self._reply(session, text, "В выученном нет шагов — научите заново.", kind="error",
                               source="learned", steps=steps, started=started)
        if len(plan_steps) == 1 and not head:
            return self._one_step(session, text, plan_steps[0], history, steps, started, extra=extra, save=True,
                                  origin=origin)
        results = [self._one_step(session, text, step, history, steps, started, save=False, origin=origin)
                   for step in plan_steps]
        lines = [str(result.get("reply") or "") for result in results if result.get("reply")]
        pending = next((result["pending"] for result in results if result.get("pending")), None)
        ok = all(result.get("kind") != "error" for result in results)
        body = "\n".join(f"• {line}" for line in lines) if len(lines) > 1 else (lines[0] if lines else "")
        reply = f"{head}\n{body}".strip() if head else body
        skill_name = next((str(result.get("skill")) for result in reversed(results) if result.get("skill")), "")
        link = next((result["link"] for result in results if isinstance(result.get("link"), dict)), None)
        merged = {**(extra or {}), **({"link": link} if link else {})}
        return self._reply(session, text, reply, kind="pending" if pending else ("action" if ok else "error"),
                           skill=skill_name, pending=pending, source=origin, steps=steps, started=started,
                           result={"ok": ok}, extra=merged or None,
                           suggestions=next((result.get("suggestions") for result in results if result.get("suggestions")), []))

    def _one_step(self, session: str, text: str, step: dict[str, Any], history: list[dict[str, Any]],
                  steps: list[dict[str, Any]], started: float, extra: dict[str, Any] | None = None,
                  save: bool = True, origin: str = "learned") -> dict[str, Any]:
        if step.get("skill"):
            plan = {**_plan(str(step["skill"]), dict(step.get("params") or {}),
                            "выучено" if origin == "learned" else "цепочка"), "source": origin}
            return self._execute(session, text, plan, history, steps, started, source=origin, extra=extra, save=save)
        said = str(step.get("say") or "").strip()
        previous = (getattr(self._local, "nested", False), getattr(self._local, "nosave", False))
        self._local.nested, self._local.nosave = True, True
        try:
            inner = self.chat(said, session=session)
        finally:
            self._local.nested, self._local.nosave = previous
        for item in (inner.get("steps") or [])[:6]:
            steps.append(item)
        if not save:
            return inner
        merged = {**(extra or {}), **({"link": inner["link"]} if isinstance(inner.get("link"), dict) else {})}
        return self._reply(session, text, str(inner.get("reply") or ""), kind=str(inner.get("kind") or "answer"),
                           skill=str(inner.get("skill") or ""), params=inner.get("params") or {},
                           result=inner.get("result") or {}, pending=inner.get("pending"), target=inner.get("target"),
                           source="learned", steps=steps, started=started, suggestions=inner.get("suggestions") or [],
                           extra=merged or None)

    def feedback(self, turn_id: int, rating: int) -> dict[str, Any]:
        """👍/👎 под ответом. 👍 закрепляет выученное; 👎 снижает вес и спрашивает, что было нужно."""
        turn = self.store.turn(int(turn_id or 0))
        if not turn or turn.get("role") != "assistant":
            return {"ok": False, "reason": "Ответ не найден — возможно, разговор очищен"}
        meta = turn.get("meta") or {}
        session = str(turn.get("session") or WINDOW_SESSION)
        before = self.store.user_turn_before(session, int(turn["id"]))
        phrase = str((before or {}).get("text") or "")
        self.learning.feedback(rating, int(turn["id"]), phrase, str(turn.get("text") or ""),
                               str(meta.get("skill") or ""), str(meta.get("source") or ""))
        learned_id = int(meta.get("learned_id") or 0)
        if int(rating) > 0:
            note = "Спасибо! Запомню, что так — правильно."
            if learned_id:
                self.learning.good(learned_id)
            elif meta.get("source") == "model" and meta.get("skill") and phrase and meta.get("ok"):
                saved = self.learning.teach(phrase, [{"skill": meta["skill"], "params": meta.get("params") or {}}],
                                            meaning=str(meta.get("skill")), source="self")
                if saved.get("ok"):
                    note = "Спасибо! Запомнил эту фразу — в следующий раз сделаю так же и без модели."
            return {"ok": True, "text": note}
        if learned_id:
            self.learning.bad(learned_id)
        if phrase:
            self.learning.note_unknown(phrase)
        ask = (f"Понял, с «{_short(phrase)}» я ошибся. Что нужно было сделать? Напишите — я запомню."
               if phrase else "Понял, ошибся. Что нужно было сделать?")
        meta_out: dict[str, Any] = {"kind": "clarify", "source": "teach", "skill": "", "params": {}, "target": {},
                                    "ok": True}
        if phrase:
            meta_out["awaiting"] = {"kind": "correction", "phrase": phrase}
        saved_turn = self.store.add_turn(session, "assistant", ask, meta_out)
        return {"ok": True, "text": "Учту.", "message": {"reply": ask, "kind": "clarify", "source": "teach",
                                                        "turn_id": saved_turn.get("id"), "session": session}}

    # --- личное (18.22) -----------------------------------------------------
    def _personal_turn(self, session: str, text: str, work: str, history: list[dict[str, Any]],
                       steps: list[dict[str, Any]], started: float, now: datetime.datetime) -> dict[str, Any] | None:
        try:
            intent = intents.personal_intent(normalize_phrase(work), self.personal, now)
        except Exception as exc:  # noqa: BLE001 — личный слой не должен ронять команды компьютеру
            steps.append({"kind": "rule", "title": "Личные дела", "detail": f"не разобрал: {exc.__class__.__name__}"})
            return None
        if not intent:
            return None
        steps.append({"kind": "rule", "title": "Личные дела", "detail": intent.get("why") or ""})
        if intent.get("clarify"):
            return self._reply(session, text, intent["clarify"], kind="clarify", source="personal", steps=steps,
                               started=started, suggestions=list(intent.get("suggestions") or []),
                               extra={"awaiting": intent["awaiting"]} if intent.get("awaiting") else None)
        plan = {**_plan(intent["skill"], intent.get("params") or {}, intent.get("why", "")), "source": "personal"}
        if intent["skill"] == "me.today" and normalize_phrase(work).casefold().startswith("доброе утро"):
            name = self._owner_name()
            plan["greeting"] = f"Доброе утро, {name}!" if name else "Доброе утро!"
        return self._execute(session, text, plan, history, steps, started, source="personal")

    def _personal_hint(self, now: datetime.datetime) -> str:
        """Для приветствия: что сегодня по личному — коротко."""
        try:
            rows = self.personal.reminders(("active",), 30)
            fired = self.personal.reminders(("fired",), 10)
        except Exception:  # noqa: BLE001
            return ""
        end = now.strftime("%Y-%m-%d") + " 23:59:59"
        today = [row for row in rows if str(row.get("due_at") or "") <= end]
        parts = []
        if today:
            shown = "; ".join(f"{str(row['due_at'])[11:16]} — {_short(row['text'], 40)}" for row in today[:2])
            more = f" и ещё {len(today) - 2}" if len(today) > 2 else ""
            parts.append(f"на сегодня {shown}{more}")
        if fired:
            parts.append(f"ждут отметки: {len(fired)}")
        return (" По личному: " + ", ".join(parts) + ".") if parts else ""

    # --- отмена и цепочки (18.23, И332–И336) --------------------------------
    def _cancel_turn(self, session: str, text: str, work: str,
                     steps: list[dict[str, Any]], started: float) -> dict[str, Any] | None:
        """«Отмена» и «верни как было» (И334): гасит ожидающее, откатывает громкость."""
        low = normalize_phrase(work).casefold()
        undo_words = bool(re.match(r"^(?:верни|возврати|откати)(?:\s+(?:громкост\w*|звук))?\s+как\s+было$|^откат$", low))
        plain = intents.CANCEL_RE.match(low)
        if not undo_words and not plain:
            return None
        pending: list[dict[str, Any]] = []
        get_pending = getattr(self.agent, "pending", None)
        if not undo_words and callable(get_pending):
            try:
                pending = [row for row in (get_pending() or []) if isinstance(row, dict)]
            except Exception:  # noqa: BLE001
                pending = []
        if pending:
            confirm = getattr(self.agent, "confirm_action", None)
            dropped = []
            for action in pending[:8]:
                if callable(confirm):
                    try:
                        confirm(str(action.get("id") or ""), False)
                    except Exception:  # noqa: BLE001
                        pass
                dropped.append(_short(str(action.get("text") or action.get("kind") or "действие"), 40))
            steps.append({"kind": "context", "title": "Отмена", "detail": f"гашу {len(dropped)} ожидающих"})
            return self._reply(session, text, "Отменил ожидающие действия: " + "; ".join(dropped) + ".",
                               kind="answer", source="rules", steps=steps, started=started)
        undo = self._undo.get(session)
        if undo:
            self._undo.pop(session, None)
            steps.append({"kind": "context", "title": "Откат", "detail": "вернуть звук как было"})
            return self._execute(session, text, _plan("system.volume", dict(undo), "откат звука"),
                                 [], steps, started)
        if undo_words:
            return self._reply(session, text, "Возвращать нечего: громкость я пока не менял.",
                               kind="answer", source="rules", steps=steps, started=started)
        steps.append({"kind": "context", "title": "Отмена", "detail": "ожидающих действий нет"})
        return self._reply(session, text, "Принято. Ожидающих действий нет — отменять нечего.",
                           kind="answer", source="rules", steps=steps, started=started)

    def _chain_turn(self, session: str, text: str, work: str, history: list[dict[str, Any]],
                    steps: list[dict[str, Any]], started: float,
                    now: datetime.datetime) -> dict[str, Any] | None:
        """«Открой телеграм и громкость 30» — две команды одной фразой (И332).

        Цепочка собирается только когда каждая часть понятна правилам сама по
        себе; иначе фраза целиком уходит прежним слоям (модель, панель).
        """
        parts = intents.split_meaning(work)
        if len(parts) < 2:
            return None
        plan_steps: list[dict[str, Any]] = []
        for part in parts:
            step = _rule_step(part, self.personal, now)
            if not step:
                return None
            plan_steps.append(step)
        steps.append({"kind": "context", "title": "Две команды в одной фразе",
                      "detail": f"{len(plan_steps)} шага: " + " · ".join(_short(part, 20) for part in parts)})
        return self._run_steps_plan(session, text, plan_steps, history, steps, started,
                                    head=f"Понял {len(plan_steps)} команды одной фразой:", origin="rules")

    def _window_clarify(self, session: str, text: str, plan: dict[str, Any],
                        steps: list[dict[str, Any]], started: float) -> dict[str, Any] | None:
        """«Закрой» без объекта (И336): список окон вопросом, а не догадка.

        Возвращает None, когда выбирать не из чего (одно окно — берём его) или
        план уже наполнен — тогда вызывающий исполняет его как обычно.
        """
        titles: list[str] = []
        reason = ""
        try:
            from . import winapi
            titles, reason = winapi.list_windows(12)
        except Exception as exc:  # noqa: BLE001
            reason = exc.__class__.__name__
        options = [str(row).strip() for row in titles if str(row).strip()][:5]
        if not options:
            return self._reply(session, text, "Не вижу открытых окон, чтобы выбрать. "
                                             f"Назовите окно целиком: «закрой блокнот». ({reason or 'окон нет'})",
                               kind="clarify", skill=plan.get("skill", ""), source="rules",
                               steps=steps, started=started)
        if len(options) == 1:
            plan["params"]["title"] = options[0]
            plan["target"] = {"window": options[0]}
            steps.append({"kind": "context", "title": "Одно окно", "detail": options[0]})
            return None
        action = {"window.close": "закрыть", "window.focus": "показать"}.get(
            str(plan.get("skill") or ""), {"minimize": "свернуть", "maximize": "развернуть"}.get(
                str((plan.get("params") or {}).get("action") or ""), "выбрать"))
        lines = "\n".join(f"{index}) {name}" for index, name in enumerate(options, 1))
        steps.append({"kind": "context", "title": "Уточнение", "detail": "какое окно — из списка"})
        return self._reply(
            session, text, f"Какое окно {action}?\n{lines}\nСкажите номер или имя.",
            kind="clarify", skill=str(plan.get("skill") or ""), source="rules", steps=steps, started=started,
            extra={"awaiting": {"kind": "window", "skill": str(plan.get("skill") or ""),
                                "params": dict(plan.get("params") or {}), "options": options}})

    # --- исполнение плана -------------------------------------------------
    def _execute(self, session: str, text: str, plan: dict[str, Any], history: list[dict[str, Any]],
                 steps: list[dict[str, Any]], started: float, source: str = "",
                 extra: dict[str, Any] | None = None, save: bool = True) -> dict[str, Any]:
        name = plan["skill"]
        skill = skills.get(name, self.runner.learned())
        origin = source or plan.get("source") or "rules"
        if skill is None:
            return self._reply(session, text, f"Навыка «{name}» нет в реестре.", kind="error", source=origin,
                               steps=steps, started=started, extra=extra, save=save)
        params = dict(plan.get("params") or {})
        delta = 0
        volume_before = None
        if name == "system.volume":
            try:
                volume_before, _reason = pc.volume_get()
            except Exception:  # noqa: BLE001
                volume_before = None
        if name == "system.volume" and "delta" in params:
            delta = int(params.get("delta") or 0)
            params = self._volume_delta(params)
        if name == "scheduler.focus_stop" and not params.get("timer_id"):
            running = [row for row in self.store.list_focus_timers(10) if row.get("status") == "running"]
            if not running:
                return self._reply(session, text, "Запущенных таймеров нет.", kind="answer", source=origin,
                                   steps=steps, started=started, extra=extra, save=save)
            params["timer_id"] = int(running[0]["id"])
        available, why = skills.availability(skill, self.runner.caps)
        if not available:
            steps.append({"kind": "skill", "title": skill["title"], "detail": "недоступен"})
            return self._reply(session, text, f"{skill['title']}: сейчас недоступно — {why}", kind="error",
                               skill=name, params=params, source=origin, steps=steps, started=started,
                               extra=extra, save=save)
        steps.append({"kind": "skill", "title": skill["title"], "detail": executor_describe(skill, params)})
        result = self._run(name, params, popup=session != WINDOW_SESSION)
        if result.get("ok") and not result.get("queued"):
            try:  # привычки владельца: что и в какой час он просит (подсказки, не автозапуск)
                self.learning.record_usage(name, params, text, self.clock())
            except Exception:  # noqa: BLE001
                pass
            if name == "system.volume" and isinstance(volume_before, dict):
                # И334: «верни как было» — вернуть прежний уровень громкости.
                prev_level = volume_before.get("level")
                undo = None
                if params.get("mute") == "on":
                    undo = {"level": int(prev_level)} if prev_level is not None else {"mute": "off"}
                elif params.get("mute") == "off":
                    undo = {"level": int(prev_level)} if prev_level is not None else {"mute": "on"}
                elif prev_level is not None:
                    undo = {"level": int(prev_level)}
                if undo and undo != {key: params.get(key) for key in undo}:
                    self._undo[session] = undo
                elif undo:
                    self._undo.pop(session, None)
        target = dict(plan.get("target") or {})
        target.pop("pronoun", None)
        if delta:
            target["delta"] = delta
        title = str(result.get("title") or "") if name.startswith("window.") else ""
        if name.startswith("window.") and name not in ("window.list", "window.text") and result.get("ok"):
            target["window"] = title or params.get("title") or target.get("window", "")
        if name == "files.quick_open":
            target["files"] = [str(row.get("path")) for row in (result.get("files") or [])[:5]]
        pending = None
        if result.get("queued"):
            pending = {"id": result.get("id"), "text": result.get("text")}
            if result.get("ttl"):
                pending["ttl"] = int(result["ttl"])  # окно показывает, сколько действие ещё ждёт
        reply = plan.get("reply") or summarize(name, result)
        if plan.get("reply") and not result.get("ok"):
            reply = summarize(name, result)
        if plan.get("greeting") and result.get("ok"):
            reply = f"{plan['greeting']} {reply}"
        if pending and session == WINDOW_SESSION and not plan.get("reply"):
            # В окне агента всплывающего окна нет — подтверждают карточкой в ленте.
            reply = f"Нужно ваше подтверждение: {result.get('text') or name}. Подтвердите в карточке ниже."
        kind = "pending" if pending else ("action" if result.get("ok") else "error")
        return self._reply(session, text, reply, kind=kind, skill=name, params=params,
                           result=_trim(result), pending=pending, target=target,
                           source=source or plan.get("source") or "rules", steps=steps, started=started,
                           suggestions=suggestions_for(name, result), extra=extra, save=save)

    def _volume_delta(self, params: dict[str, Any]) -> dict[str, Any]:
        """«Громче» — это «текущая + 10»: навык получает уровень, а не догадку."""
        delta = int(params.pop("delta", 0) or 0)
        current, _reason = pc.volume_get()
        level = current.get("level")
        if level is None:
            params["delta"] = delta  # исполнитель нажмёт медиаклавиши
            return params
        params["level"] = max(0, min(100, int(level) + delta))
        return params

    # --- модель -----------------------------------------------------------
    def _think(self, session: str, text: str, history: list[dict[str, Any]],
               steps: list[dict[str, Any]], started: float, state: dict[str, Any] | None = None,
               workshop: bool = False) -> dict[str, Any]:
        state = state if state is not None else model.status()
        memories = self.store.recall(text, 5, touch=False)
        if not state.get("ok"):
            # Техническая причина (порт, ошибка Ollama) — в ходе мысли, а не в лицо человеку.
            steps.append({"kind": "model", "title": "Модель", "detail": str(state.get("reason") or "недоступна")[:200]})
            awaiting: dict[str, Any] | None = None
            if workshop and getattr(self, "_panel_down", False):
                note = ("Это вопрос к панели цеха, а она сейчас не отвечает. Проверьте, что PrintFlow запущен, "
                        "и спросите ещё раз.")
            else:
                # Непонятое не пропадает: фраза копится во вкладке «Обучение», а
                # «это значит …» следующей репликой учит помощника сразу.
                if not getattr(self._local, "nested", False):
                    try:
                        self.learning.note_unknown(text)
                        awaiting = {"kind": "teach", "phrase": text}
                    except Exception:  # noqa: BLE001
                        awaiting = None
                note = (f"Пока не понимаю «{_short(text)}»: без модели такое не разобрать. Научите меня — скажите "
                        "«это значит …» (например, «это значит открой телеграм»), и я запомню. Уже умею: команды "
                        "компьютеру, вопросы про цех, память, напоминания, списки, цели, привычки, расходы и счёт. "
                        "Свободные вопросы заработают, когда на этом компьютере запустите модель (Ollama).")
            if memories:
                note = "Из памяти: " + "; ".join(row["text"] for row in memories[:3]) + ".\n" + note
            return self._reply(session, text, note, kind="clarify", source="rules", steps=steps, started=started,
                               suggestions=["Что ты умеешь?", "Чему ты научился?", "Что сейчас печатается?"],
                               extra={"panel_asked": True, **({"awaiting": awaiting} if awaiting else {})})
        catalog = skills.prompt(self.runner.caps, self.runner.learned())
        context = [date_line(self.clock()) + f" Время {self.clock():%H:%M}."]
        try:
            window, _reason = pc.find_window("") if pc.IS_WINDOWS else (None, "")
            if window:
                context.append(f"Активное окно человека: «{window['title']}».")
        except Exception:
            pass
        if memories:
            context.append("Память о владельце: " + "; ".join(row["text"] for row in memories))
        system = (f"{_PLANNER_RULES}\n\nКонтекст:\n" + "\n".join(context)
                  + f"\n\nНавыки (только эти):\n{catalog[:6000]}")
        messages = [{"role": turn["role"], "content": turn["text"][:500]} for turn in history[-8:]]
        messages.append({"role": "user", "content": text})
        steps.append({"kind": "model", "title": "Думаю моделью", "detail": state.get("model") or ""})
        reply = model.chat(messages, system=system, fmt="json", temperature=0.1,
                           timeout=min(PLAN_TIMEOUT_SEC, config.MODEL_TIMEOUT_SEC))
        if not reply["ok"]:
            return self._reply(session, text, f"Модель не ответила: {reply['reason']}", kind="error",
                               source="model", steps=steps, started=started)
        answer = model.parse_json(reply["text"])
        if not answer:
            # Модель ответила прозой вопреки режиму — это тоже ответ человеку.
            return self._reply(session, text, reply["text"][:1200], kind="answer", source="model",
                               steps=steps, started=started)
        ask = " ".join(str(answer.get("ask") or "").split())
        name = str(answer.get("skill") or "").strip().casefold()
        said = " ".join(str(answer.get("reply") or "").split())
        if ask and not name:
            return self._reply(session, text, ask, kind="clarify", source="model", steps=steps, started=started)
        if not name:
            return self._reply(session, text, said or "Не знаю, что ответить.", kind="answer", source="model",
                               steps=steps, started=started)
        skill = skills.get(name, self.runner.learned())
        if skill is None:
            steps.append({"kind": "check", "title": "Проверка реестром", "detail": f"навыка {name} нет"})
            return self._reply(session, text, (said + " " if said else "")
                               + f"(Модель предложила несуществующий навык «{name}» — не выполняю.)",
                               kind="answer", source="model", steps=steps, started=started)
        params, errors = skills.check_params(skill, answer.get("params"))
        hard = [error for error in errors if "не объявлен" not in error]
        if hard:
            steps.append({"kind": "check", "title": "Проверка параметров", "detail": "; ".join(hard)})
            return self._reply(session, text, f"Чтобы выполнить «{skill['title']}», уточните: " + "; ".join(hard),
                               kind="clarify", source="model", steps=steps, started=started)
        steps.append({"kind": "check", "title": "Проверка реестром", "detail": "навык и параметры в порядке"})
        answer = self._execute(session, text, {**_plan(name, params, "модель"), "source": "model"},
                               history, steps, started, source="model")
        if answer.get("kind") == "action" and not name.startswith(_NO_SELF_LEARN) and "due" not in params \
                and not getattr(self._local, "nested", False):
            # Самообучение (И318): понятое моделью и выполненное — в следующий раз без модели.
            try:
                saved = self.learning.teach(text, [{"skill": name, "params": params}],
                                            meaning=executor_describe(skill, params), source="self")
            except Exception:  # noqa: BLE001
                saved = {}
            if saved.get("ok"):
                answer["steps"].append({"kind": "learned", "title": "Запомнил фразу",
                                        "detail": "в следующий раз пойму без модели"})
                answer["learned"] = True
        return answer

    # --- служебное --------------------------------------------------------
    def _history(self, session: str) -> list[dict[str, Any]]:
        try:
            return self.store.dialog(session, 12)
        except Exception:
            return []

    def _reply(self, session: str, text: str, reply: str, *, kind: str, skill: str = "",
               params: dict[str, Any] | None = None, result: dict[str, Any] | None = None,
               pending: dict[str, Any] | None = None, target: dict[str, Any] | None = None,
               source: str = "rules", steps: list[dict[str, Any]] | None = None, started: float = 0.0,
               suggestions: list[str] | None = None, handled: bool = True, save: bool = True,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
        turn_id = 0
        if save and text and not getattr(self._local, "nosave", False):
            self.store.add_turn(session, "user", text, {})
            meta = {"skill": skill, "params": params or {}, "target": target or {}, "kind": kind, "source": source,
                    "ok": bool(result.get("ok")) if isinstance(result, dict) and result else kind not in ("error",)}
            if extra and isinstance(extra.get("link"), dict):
                meta["link"] = extra["link"]  # «ссылка ниже» должна остаться и после перезагрузки окна
            if extra and extra.get("learned_id"):
                meta["learned_id"] = int(extra["learned_id"])  # 👎 и «нет, не то» снижают вес именно этого урока
            if extra and isinstance(extra.get("awaiting"), dict):
                meta["awaiting"] = extra["awaiting"]  # «Когда напомнить?» — ответ поймётся следующей репликой
            turn_id = int(self.store.add_turn(session, "assistant", reply, meta).get("id") or 0)
        payload = {"ok": kind != "error", "handled": handled, "session": session, "reply": reply,
                   "kind": kind, "skill": skill or None, "params": params or {}, "result": result or {},
                   "pending": pending, "target": target or {}, "source": source, "steps": steps or [],
                   "suggestions": suggestions or [], "ms": int((time.time() - started) * 1000) if started else 0,
                   "turn_id": turn_id or None}
        if extra:
            payload.update(extra)
        return payload


def executor_describe(skill: dict[str, Any], params: dict[str, Any]) -> str:
    from .executor import describe
    return describe(skill, params)


def _trim(result: dict[str, Any], limit: int = 12) -> dict[str, Any]:
    """Результат навыка для окна: без мегабайтов текста и сотен строк."""
    out: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, list):
            out[key] = value[:limit]
        elif isinstance(value, str):
            out[key] = value[:2000]
        else:
            out[key] = value
    return out


def capabilities_text(catalog: list[dict[str, Any]]) -> str:
    """«Что ты умеешь» по реестру: группы готовых навыков и что недоступно."""
    groups: dict[str, list[str]] = {}
    for row in catalog:
        if row.get("available"):
            groups.setdefault(row["name"].split(".", 1)[0], []).append(row["title"])
    titles = {"system": "компьютер", "window": "окна", "screen": "экран", "voice": "голос", "clipboard": "буфер",
              "files": "файлы", "app": "программы", "memory": "память", "panel": "панель цеха", "day": "день",
              "avito": "Авито", "tg": "Телеграм", "scheduler": "таймеры", "knowledge": "знания",
              "agent": "сам помощник", "assistant": "макросы", "safety": "белый список"}
    parts = [f"{titles.get(group, group)} ({len(names)})" for group, names in groups.items()]
    ready = sum(len(names) for names in groups.values())
    return (f"Готово {ready} навыков: " + ", ".join(parts) + ". Примеры: «громкость 30», «открой блокнот», "
            "«переключись на телеграм», «что грузит компьютер», «найди файл договор», «таймер на 25 минут», "
            "«запомни, что…»." if parts else "Сейчас готовых навыков нет — посмотрите причины в реестре.")
