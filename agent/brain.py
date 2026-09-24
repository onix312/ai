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
"""
from __future__ import annotations

import ast
import datetime
import re
import time
from typing import Any, Callable

from . import config, model, pc, skills

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


def math_answer(text: str) -> str:
    """Арифметика без модели: «2+2», «посчитай 15*3.5». Только числа и знаки."""
    match = _MATH_RE.match(normalize_phrase(text).casefold())
    if not match:
        return ""
    raw = match.group(1).replace("×", "*").replace("÷", "/").replace("x", "*").replace(",", ".")
    if not re.search(r"\d", raw) or not re.search(r"[+\-*/]", raw) or len(raw) > 60:
        return ""
    try:
        value = _evaluate(ast.parse(raw.strip(), mode="eval"))
    except ZeroDivisionError:
        return "На ноль делить нельзя."
    except (SyntaxError, ValueError, OverflowError, TypeError):
        return ""
    if isinstance(value, float):
        value = round(value, 6)
        if value.is_integer():
            value = int(value)
    return f"{match.group(1).strip()} = {str(value).replace('.', ',')}"


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

def _level(text: str) -> int | None:
    match = re.search(r"(\d{1,3})\s*(?:%|процент)?", text)
    if not match:
        words = {"ноль": 0, "десять": 10, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
                 "половин": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
                 "сто": 100, "максимум": 100, "на всю": 100}
        for word, value in words.items():
            if word in text:
                return value
        return None
    return max(0, min(100, int(match.group(1))))


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
    if not workshop and re.search(r"(громкост|звук)\w*\s*(на|в|до)?\s*\d{1,3}|(громкост|звук)\w*\s+(на\s+)?(максимум|половин|ноль)", low):
        level = _level(low)
        if level is not None:
            return _plan("system.volume", {"level": level}, "звук: уровень")
    if re.search(r"(какая|сколько)\s+(сейчас\s+)?громкост|громкость\s*\?*$", low):
        return _plan("system.volume", {}, "звук: узнать")
    if re.search(r"(погромче|громче|^прибав\w*$|прибав\w*\s+(звук|громкост)|увелич\w*\s+(звук|громкост)|добав\w*\s+звук)", low):
        return _plan("system.volume", {"delta": 20 if "намного" in low or "сильно" in low else 10}, "звук: громче")
    if re.search(r"(потише|тише|^убав\w*$|убав\w*\s+(звук|громкост)|уменьш\w*\s+(звук|громкост))", low) and "говори" not in low:
        return _plan("system.volume", {"delta": -20 if "намного" in low or "сильно" in low else -10}, "звук: тише")

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
        return _plan("window.arrange", {"action": "minimize", "title": "" if target in ("его", "ее", "это") else target},
                     "окно: свернуть", target={"pronoun": target in ("его", "ее", "это")})
    if re.match(r"^(?:сверни все|сверни всё|покажи рабочий стол|рабочий стол)", low):
        return _plan("system.hotkey", {"keys": "win+d"}, "окна: рабочий стол")
    match = re.match(r"^(?:разверни|раскрой)(?:\s+(?:окно))?(?:\s+(?P<t>.+?))?(?:\s+на\s+весь\s+экран)?$", low)
    if match and not workshop:
        target = (match.group("t") or "").strip()
        return _plan("window.arrange", {"action": "maximize", "title": "" if target in ("его", "ее", "это") else target},
                     "окно: развернуть", target={"pronoun": target in ("его", "ее", "это")})
    match = re.match(r"^(?:закрой|закрыть)\s+(?:окно\s+|программу\s+)?(?P<t>.+)$", low)
    if match and not workshop:
        target = match.group("t").strip()
        if target in ("его", "ее", "это", "это окно", "окно"):
            return _plan("window.close", {"title": ""}, "окно: закрыть", target={"pronoun": True})
        return _plan("window.close", {"title": target}, "окно: закрыть")
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
    match = re.search(r"(?:таймер|засеки|фокус|помодоро|напомни)\w*\s+(?:на\s+|через\s+)?(\d{1,3})\s*(мин\w*|час\w*)", low)
    if match:
        note = re.sub(r".*?(?:\d{1,3})\s*(?:мин\w*|час\w*)\s*", "", phrase, count=1).strip(" ,.")
        return _plan("scheduler.focus_timer", {"minutes": max(1, min(240, _minutes(*match.groups()))),
                                               "note": note[:120]}, "таймер")
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
        match = re.fullmatch(r"(?:а\s+)?(?:давай\s+)?(?:на\s+)?(\d{1,3})\s*%?", low)
        if match:
            return _plan("system.volume", {"level": max(0, min(100, int(match.group(1))))}, "звук: уточнение")
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
    return []


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
    "6. reply — одно-два предложения, без markdown."
)


class Brain:
    """Разговор с помощником: правила → контекст → память → модель → навык → ответ."""

    def __init__(self, agent: Any, clock: Callable[[], datetime.datetime] | None = None) -> None:
        self.agent = agent
        self.clock = clock or datetime.datetime.now

    # --- доступ к агенту ------------------------------------------------
    @property
    def runner(self) -> Any:
        return self.agent.runner

    @property
    def store(self) -> Any:
        return self.runner.store

    def _run(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        run = getattr(self.agent, "run_skill", None)
        if callable(run):
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

        if mode != "pc":
            answer = clock_answer(clean, self.clock()) or math_answer(clean)
            if answer:
                steps.append({"kind": "rule", "title": "Часы и арифметика", "detail": "без модели"})
                return self._reply(session, clean, answer, kind="answer", source="clock",
                                   steps=steps, started=started)
            if re.search(r"что ты умеешь|что умеешь|^помощь$|^help$|твои навыки|список навыков|чем (ты )?можешь помочь",
                         clean.casefold()):
                steps.append({"kind": "rule", "title": "Реестр навыков", "detail": "готовые навыки"})
                return self._reply(session, clean, capabilities_text(self.runner.catalog()), kind="answer",
                                   source="registry", steps=steps, started=started,
                                   suggestions=["Как там компьютер?", "Какие окна открыты?", "Громкость 30"])
            memory_reply = self._memory(clean)
            if memory_reply:
                steps.append({"kind": "memory", "title": "Память", "detail": memory_reply["op"]})
                return self._reply(session, clean, memory_reply["text"], kind="memory", source="memory",
                                   steps=steps, started=started, extra={"memory": memory_reply.get("rows", [])})

        found = follow_up(clean, history)
        if found:
            steps.append({"kind": "context", "title": "Понял по прошлой реплике", "detail": found["why"]})
        else:
            found = understand(clean)
            if found:
                steps.append({"kind": "rule", "title": "Понял без модели", "detail": found["why"]})
        if found:
            found = resolve_pronoun(found, history)
            if found.get("clarify"):
                return self._reply(session, clean, found["clarify"], kind="clarify", steps=steps, started=started)
            return self._execute(session, clean, found, history, steps, started)

        if mode == "pc":
            return self._reply(session, clean, "", kind="skip", handled=False, steps=steps,
                               started=started, save=False)
        return self._think(session, clean, history, steps, started)

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
            return {"op": op, "text": ("Про это в памяти ничего нет." if payload
                                       else "Память пока пуста. Скажите «запомни, что …».")}
        lines = [f"• {row['text']}" for row in rows[:8]]
        head = f"Про «{payload}» помню:" if payload else "Вот что я помню:"
        return {"op": op, "text": head + "\n" + "\n".join(lines), "rows": rows}

    # --- исполнение плана -------------------------------------------------
    def _execute(self, session: str, text: str, plan: dict[str, Any], history: list[dict[str, Any]],
                 steps: list[dict[str, Any]], started: float, source: str = "") -> dict[str, Any]:
        name = plan["skill"]
        skill = skills.get(name, self.runner.learned())
        if skill is None:
            return self._reply(session, text, f"Навыка «{name}» нет в реестре.", kind="error",
                               steps=steps, started=started)
        params = dict(plan.get("params") or {})
        delta = 0
        if name == "system.volume" and "delta" in params:
            delta = int(params.get("delta") or 0)
            params = self._volume_delta(params)
        if name == "scheduler.focus_stop" and not params.get("timer_id"):
            running = [row for row in self.store.list_focus_timers(10) if row.get("status") == "running"]
            if not running:
                return self._reply(session, text, "Запущенных таймеров нет.", kind="answer", steps=steps, started=started)
            params["timer_id"] = int(running[0]["id"])
        available, why = skills.availability(skill, self.runner.caps)
        if not available:
            steps.append({"kind": "skill", "title": skill["title"], "detail": "недоступен"})
            return self._reply(session, text, f"{skill['title']}: сейчас недоступно — {why}", kind="error",
                               skill=name, params=params, steps=steps, started=started)
        steps.append({"kind": "skill", "title": skill["title"], "detail": executor_describe(skill, params)})
        result = self._run(name, params)
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
        reply = plan.get("reply") or summarize(name, result)
        if plan.get("reply") and not result.get("ok"):
            reply = summarize(name, result)
        kind = "pending" if pending else ("action" if result.get("ok") else "error")
        return self._reply(session, text, reply, kind=kind, skill=name, params=params,
                           result=_trim(result), pending=pending, target=target,
                           source=source or plan.get("source") or "rules", steps=steps, started=started,
                           suggestions=suggestions_for(name, result))

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
               steps: list[dict[str, Any]], started: float) -> dict[str, Any]:
        state = model.status()
        memories = self.store.recall(text, 5, touch=False)
        if not state.get("ok"):
            steps.append({"kind": "model", "title": "Модель", "detail": "недоступна"})
            note = ("Этого я без модели не понимаю. Попробуйте командой: «громкость 30», «открой блокнот», "
                    "«переключись на телеграм», «что грузит компьютер», «запомни, что…». "
                    f"Модель: {state.get('reason')}")
            if memories:
                note = "Из памяти: " + "; ".join(row["text"] for row in memories[:3]) + ".\n" + note
            return self._reply(session, text, note, kind="clarify", source="rules", steps=steps, started=started,
                               suggestions=["Что ты умеешь?", "Как там компьютер?", "Какие окна открыты?"])
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
        return self._execute(session, text, {**_plan(name, params, "модель"), "source": "model"},
                             history, steps, started, source="model")

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
        if save and text:
            self.store.add_turn(session, "user", text, {})
            self.store.add_turn(session, "assistant", reply, {
                "skill": skill, "params": params or {}, "target": target or {}, "kind": kind,
                "ok": bool(result.get("ok")) if isinstance(result, dict) else kind not in ("error",)})
        payload = {"ok": kind != "error", "handled": handled, "session": session, "reply": reply,
                   "kind": kind, "skill": skill or None, "params": params or {}, "result": result or {},
                   "pending": pending, "target": target or {}, "source": source, "steps": steps or [],
                   "suggestions": suggestions or [], "ms": int((time.time() - started) * 1000) if started else 0}
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
