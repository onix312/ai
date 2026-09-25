"""Время по-русски (18.22): «через 20 минут», «завтра в 9», «по будням в 8:30».

Зачем свой разбор. Напоминание — самая частая личная просьба, и она обязана
работать без модели: на компьютере мастерской модели может не быть, а
«напомни через 20 минут выключить чайник» должно сработать всегда. Поэтому
здесь регулярные выражения по частям фразы: повтор, «через», день, дата,
время, часть дня. Каждая понятая часть вырезается из фразы — остаток и есть
«о чём напомнить».

Непонятое не угадывается: `parse` вернёт None, и помощник спросит «когда?».
Уже прошедшее время отмечается `past=True` — «сегодня в 8», сказанное в 21:00,
не превращается молча в завтрашнее.

Повторы хранятся строкой: `daily`, `weekdays`, `weekends`, `weekly:4`
(пятница), `monthly:25`, `every:30` (минуты). `advance` считает следующий раз.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

# --- числа словами --------------------------------------------------------------
_ONES = {"ноль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
         "шесть": 6, "семь": 7, "восемь": 8, "девять": 9}
_TEENS = {"десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
          "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
         "восемьдесят": 80, "девяносто": 90, "сто": 100}
_FUZZY = {"пару": 2, "пара": 2, "пары": 2, "несколько": 3}
_WORDS = sorted({**_ONES, **_TEENS, **_TENS, **_FUZZY}, key=len, reverse=True)
NUM = (r"(?:\d+(?:[.,]\d+)?|(?:" + "|".join(_TENS) + r")(?:\s+(?:"
       + "|".join(word for word in _ONES if word != "ноль") + r"))?|" + "|".join(_WORDS) + r")")

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
WEEKDAYS_ON = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье")
WEEKDAYS_EVERY = ("по понедельникам", "по вторникам", "по средам", "по четвергам", "по пятницам", "по субботам",
                  "по воскресеньям")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")
_MONTH_STEMS = ("январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")
_MONTH_RE = (r"(?P<mon>январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|"
             r"октябр\w*|ноябр\w*|декабр\w*)")
_WD_ACC = {"понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3, "пятницу": 4, "пятница": 4,
           "субботу": 5, "суббота": 5, "воскресенье": 6}
_WD_PLURAL = {"понедельникам": 0, "вторникам": 1, "средам": 2, "четвергам": 3, "пятницам": 4, "субботам": 5,
              "воскресеньям": 6}
_WD_WORDS = "понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье"
_HOUR_WORDS = {"час": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
               "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12}
_ORDINAL_GEN = {"первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5, "шестого": 6,
                "седьмого": 7, "восьмого": 8, "девятого": 9, "десятого": 10, "одиннадцатого": 11,
                "двенадцатого": 12}
_PARTS = {"с утра": 9, "утром": 9, "днем": 13, "в обед": 13, "после обеда": 15, "вечером": 19,
          "перед сном": 22, "ночью": 23}

# --- выражения ----------------------------------------------------------------------
_NOT_TIME = r"(?!\s*(?:мин|сек|раз|шт|руб|р\b|₽|%|процент|км|кг|кило|литр|л\b|м\b|см|мм|книг|страниц|заказ|человек|дн|дня|недел|месяц|лет|год))"
_REL_RE = re.compile(r"\bчерез\s+(?:(?P<n>" + NUM + r")\s+)?(?P<u>полчаса|пол\s*часа|полтора\s+часа|мин\w*|час\w*|сут\w*"
                     r"|дн\w*|день|недел\w*|месяц\w*)(?![а-я])")
_DAY_RE = re.compile(r"\b(?P<d>сегодня|завтра|послезавтра)\b")
_WD_RE = re.compile(r"\b(?:в|во)\s+(?:(?P<next>следующ\w+|эт[уо]т?|ближайш\w+)\s+)?(?P<wd>" + _WD_WORDS + r")\b")
_WEEKEND_RE = re.compile(r"\bв\s+(?:эти\s+|ближайшие\s+)?выходные\b")
_DATE_RE = re.compile(r"(?<![\d.])(?P<day>\d{1,2})(?:-?го|-?е)?\s+" + _MONTH_RE
                      + r"(?:\s+(?P<year>\d{4})(?:\s*(?:года|г\.?))?)?")
_NUMDATE_RE = re.compile(r"(?<![\d:.])(?<!в\s)(?<!к\s)(?P<day>\d{1,2})\.(?P<mon>\d{1,2})(?:\.(?P<year>\d{2,4}))?(?![\d:])")
_HALF_RE = re.compile(r"\bв\s+половин\w*\s+(?P<ord>" + "|".join(_ORDINAL_GEN) + r")(?:\s+(?P<part>утра|дня|вечера|ночи))?\b")
_TIME_RE = re.compile(r"\b(?:в|к)\s+(?P<h>\d{1,2})(?:(?:[:.]|\s*ч(?:ас\w*)?\s+)(?P<m>\d{2}))?(?![\d.,])"
                      r"(?:\s*ч(?:ас\w*)?\b)?(?:\s+(?:мин\w*\s+)?(?P<part>утра|дня|вечера|ночи))?" + _NOT_TIME)
_BARE_TIME_RE = re.compile(r"(?<![\d.:])(?P<h>\d{1,2}):(?P<m>\d{2})(?![\d])(?:\s+(?P<part>утра|дня|вечера|ночи))?")
_WORD_TIME_RE = re.compile(r"\b(?:в|к)\s+(?P<hw>" + "|".join(_HOUR_WORDS) + r")(?:\s+час\w*)?"
                           r"(?:\s+(?P<part>утра|дня|вечера|ночи))?\b(?!\s*(?:раз|минут|час))")
_NOON_RE = re.compile(r"\b(?:в|к)\s+(?P<noon>полдень|полночь)\b")
_PART_RE = re.compile(r"(?:\b(?:с утра|утром|днем|после обеда|вечером|перед сном|ночью)\b|\bв обед\b)")
_REPEATS = (
    (re.compile(r"\bкаждый\s+будний\s+день\b|\bпо\s+будням\b|\bв\s+будни\b|\bпо\s+рабочим\s+дням\b"), "weekdays", None),
    (re.compile(r"\bпо\s+выходным\b|\bкаждые\s+выходные\b"), "weekends", None),
    (re.compile(r"\bкаждое\s+утро\b|\bпо\s+утрам\b"), "daily", 9),
    (re.compile(r"\bкаждый\s+вечер\b|\bпо\s+вечерам\b"), "daily", 19),
    (re.compile(r"\bкаждую\s+ночь\b"), "daily", 23),
    (re.compile(r"\bкаждый\s+день\b|\bежедневно\b|\bкаждодневно\b"), "daily", None),
    (re.compile(r"\bкаждую\s+неделю\b|\bеженедельно\b|\bраз\s+в\s+неделю\b"), "weekly", None),
    (re.compile(r"\bкаждый\s+месяц\b|\bежемесячно\b|\bраз\s+в\s+месяц\b"), "monthly", None),
)
_REPEAT_WD_RE = re.compile(r"\bкажд\w+\s+(?P<wd>" + _WD_WORDS + r")\b|\bпо\s+(?P<wdp>" + "|".join(_WD_PLURAL) + r")\b")
_REPEAT_EVERY_RE = re.compile(r"\b(?:кажд\w+|раз\s+в)\s+(?:(?P<n>" + NUM + r")\s+)?(?P<u>мин\w*|час\w*)(?![а-я])")
_REPEAT_MONTHDAY_RE = re.compile(r"\bкажд\w+\s+(?P<md>\d{1,2})(?:-?е|-?го)?\s+числ\w*"
                                 r"|\b(?P<md2>\d{1,2})(?:-?го)?\s+числа\s+каждого\s+месяца")
_TRIGGER_RE = re.compile(r"\b(?:поставь|создай|сделай|добавь)?\s*(?:напоминани\w*|напомни(?:ть|шь)?|напоминай)\b"
                         r"(?:\s+(?:мне|нам))?", re.IGNORECASE)


def number(raw: Any) -> float | None:
    """«25», «2,5», «двадцать пять», «пару» → число; иначе None."""
    text = " ".join(str(raw or "").lower().replace("ё", "е").split()).replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    total = 0
    for word in text.split():
        for table in (_TENS, _TEENS, _ONES, _FUZZY):
            if word in table:
                total += table[word]
                break
        else:
            return None
    return float(total)


def plural(count: int, one: str, few: str, many: str) -> str:
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def _month(word: str) -> int:
    for index, stem in enumerate(_MONTH_STEMS):
        if word.startswith(stem):
            return index + 1
    return 0


def _apply_part(hour: int, part: str) -> int:
    """«7 вечера» → 19, «12 ночи» → 0, «3 дня» → 15."""
    if part == "утра":
        return 0 if hour == 12 else hour
    if part in ("дня", "вечера"):
        return hour + 12 if hour < 12 else hour
    if part == "ночи":
        if hour == 12:
            return 0
        return hour + 12 if 5 <= hour < 12 else hour
    return hour


def _part_word_shift(hour: int, word: str) -> int:
    """«вечером в 8» → 20: слово части дня уточняет час без «утра/вечера»."""
    if word in ("вечером", "после обеда", "перед сном") and hour < 12:
        return hour + 12
    if word in ("днем", "в обед") and hour <= 6:
        return hour + 12
    if word == "ночью" and 5 <= hour < 12:
        return hour + 12
    return hour


def _next_weekday(today: dt.date, weekday: int, strictly_next_week: bool = False) -> dt.date:
    days = (weekday - today.weekday()) % 7
    if strictly_next_week:
        days = days + 7 if days <= (6 - today.weekday()) else days
    return today + dt.timedelta(days=days)


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def _add_month(moment: dt.datetime, day: int) -> dt.datetime:
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    for candidate in (day, 30, 29, 28):
        date = _safe_date(year, month, min(day, candidate))
        if date:
            return moment.replace(year=date.year, month=date.month, day=date.day)
    return moment + dt.timedelta(days=30)


def _matches(moment: dt.datetime, repeat: str) -> bool:
    kind, _, arg = repeat.partition(":")
    if kind == "weekdays":
        return moment.weekday() < 5
    if kind == "weekends":
        return moment.weekday() >= 5
    if kind == "weekly" and arg:
        return moment.weekday() == int(arg)
    if kind == "monthly" and arg:
        return moment.day == int(arg)
    return True


def advance(due: dt.datetime, repeat: str, now: dt.datetime) -> dt.datetime | None:
    """Следующий раз повторяющегося напоминания — строго позже `now`. Без повтора — None."""
    kind, _, arg = str(repeat or "").partition(":")
    if not kind:
        return None
    if kind == "every":
        step = dt.timedelta(minutes=max(5, int(arg or 60)))
        if due > now:
            return due
        missed = int((now - due) / step) + 1
        return due + step * missed
    if kind == "monthly":
        day = int(arg or due.day)
        moment = due
        for _ in range(40):
            moment = _add_month(moment, day)
            if moment > now:
                return moment
        return None
    moment = due
    if moment <= now:
        # Долго выключенный агент не «догоняет» пропущенные дни по одному.
        moment = moment + dt.timedelta(days=max(0, (now - moment).days))
    for _ in range(400):
        moment = moment + dt.timedelta(days=7 if kind == "weekly" else 1)
        if moment > now and _matches(moment, repeat):
            return moment
    return None


def first_at(repeat: str, hour: int, minute: int, now: dt.datetime, start: dt.date | None = None) -> dt.datetime:
    """Первый раз повтора: ближайший подходящий день с этим временем, позже `now`."""
    day = start or now.date()
    for _ in range(400):
        moment = dt.datetime.combine(day, dt.time(hour, minute))
        if moment > now and _matches(moment, repeat):
            return moment
        day += dt.timedelta(days=1)
    return now + dt.timedelta(days=1)


def repeat_label(repeat: str, moment: dt.datetime | None = None) -> str:
    kind, _, arg = str(repeat or "").partition(":")
    hm = f" в {moment:%H:%M}" if moment is not None else ""
    if kind == "daily":
        return "каждый день" + hm
    if kind == "weekdays":
        return "по будням" + hm
    if kind == "weekends":
        return "по выходным" + hm
    if kind == "weekly":
        weekday = int(arg) if arg else (moment.weekday() if moment else 0)
        return WEEKDAYS_EVERY[weekday] + hm
    if kind == "monthly":
        return f"каждый месяц {int(arg or (moment.day if moment else 1))}-го" + hm
    if kind == "every":
        minutes = int(arg or 60)
        if minutes == 60:
            return "каждый час"
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"каждые {hours} {plural(hours, 'час', 'часа', 'часов')}"
        return f"каждые {minutes} {plural(minutes, 'минуту', 'минуты', 'минут')}"
    return ""


def label(moment: dt.datetime, repeat: str = "", now: dt.datetime | None = None) -> str:
    """Когда — словами: «через 20 минут, в 12:50», «завтра в 10:00», «в пятницу в 19:00»."""
    now = now or dt.datetime.now()
    if repeat:
        return repeat_label(repeat, moment)
    hm = f"{moment:%H:%M}"
    days = (moment.date() - now.date()).days
    if days == 0:
        minutes = int((moment - now).total_seconds() // 60)
        if 0 < minutes < 60:
            return f"через {minutes} {plural(minutes, 'минуту', 'минуты', 'минут')}, в {hm}"
        return f"сегодня в {hm}"
    if days == 1:
        return f"завтра в {hm}"
    if days == 2:
        return f"послезавтра в {hm}"
    if 2 < days < 7:
        return f"{WEEKDAYS_ON[moment.weekday()]}, {moment.day} {MONTHS[moment.month - 1]}, в {hm}"
    year = f" {moment.year}" if moment.year != now.year else ""
    return f"{moment.day} {MONTHS[moment.month - 1]}{year} в {hm}"


def day_label(day: dt.date, today: dt.date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta == -1:
        return "вчера"
    year = f" {day.year}" if day.year != today.year else ""
    return f"{day.day} {MONTHS[day.month - 1]}{year}"


def _free(spans: list[tuple[int, int]], match: re.Match[str]) -> bool:
    start, end = match.span()
    return all(end <= left or start >= right for left, right in spans)


def _search(pattern: re.Pattern[str], text: str, spans: list[tuple[int, int]]) -> re.Match[str] | None:
    for match in pattern.finditer(text):
        if _free(spans, match):
            return match
    return None


def parse(text: str, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """Фраза → {at, repeat, text, label, past, iso}. None — во фразе нет времени.

    `text` — то, что осталось от фразы без времени и без «напомни»: «позвонить маме».
    """
    now = (now or dt.datetime.now()).replace(microsecond=0)
    original = " ".join(str(text or "").split())
    low = original.lower().replace("ё", "е")
    spans: list[tuple[int, int]] = []
    repeat, default_hour = "", None
    repeat_day: int | None = None

    # 1. Повтор.
    match = _search(_REPEAT_EVERY_RE, low, spans)
    if match:
        count = number(match.group("n")) if match.group("n") else 1.0
        minutes = int(round((count or 1) * (60 if match.group("u").startswith("час") else 1)))
        repeat = f"every:{max(5, minutes)}"
        spans.append(match.span())
    if not repeat:
        match = _search(_REPEAT_MONTHDAY_RE, low, spans)
        if match:
            day = int(match.group("md") or match.group("md2"))
            if 1 <= day <= 31:
                repeat, repeat_day = f"monthly:{day}", day
                spans.append(match.span())
    if not repeat:
        match = _search(_REPEAT_WD_RE, low, spans)
        if match:
            weekday = _WD_ACC.get(match.group("wd") or "", _WD_PLURAL.get(match.group("wdp") or "", 0))
            repeat = f"weekly:{weekday}"
            spans.append(match.span())
    if not repeat:
        for pattern, kind, hour in _REPEATS:
            match = _search(pattern, low, spans)
            if match:
                repeat, default_hour = kind, hour
                spans.append(match.span())
                break

    # 2. «Через …».
    delta: dt.timedelta | None = None
    match = _search(_REL_RE, low, spans)
    if match:
        unit = match.group("u")
        count = number(match.group("n")) if match.group("n") else 1.0
        count = count if count is not None else 1.0
        if re.match(r"пол\s*часа|полчаса", unit):
            delta = dt.timedelta(minutes=30)
        elif unit.startswith("полтора"):
            delta = dt.timedelta(minutes=90)
        elif unit.startswith("мин"):
            delta = dt.timedelta(minutes=max(1.0, count))
        elif unit.startswith("час"):
            delta = dt.timedelta(minutes=round(count * 60))
        elif unit.startswith("недел"):
            delta = dt.timedelta(weeks=count)
        elif unit.startswith("месяц"):
            delta = dt.timedelta(days=round(30 * count))
        else:
            delta = dt.timedelta(days=count)
        spans.append(match.span())

    # 3. День: сегодня/завтра, день недели, дата, выходные.
    day: dt.date | None = None
    same_weekday = False
    match = _search(_DAY_RE, low, spans)
    if match:
        day = now.date() + dt.timedelta(days=("сегодня", "завтра", "послезавтра").index(match.group("d")))
        spans.append(match.span())
    if day is None:
        match = _search(_WD_RE, low, spans)
        if match:
            weekday = _WD_ACC[match.group("wd")]
            later = bool(match.group("next") and match.group("next").startswith("следующ"))
            day = _next_weekday(now.date(), weekday, later)
            same_weekday = day == now.date()
            spans.append(match.span())
    if day is None:
        match = _search(_DATE_RE, low, spans)
        if match:
            month = _month(match.group("mon"))
            year = int(match.group("year") or now.year)
            day = _safe_date(year, month, int(match.group("day"))) if month else None
            if day and not match.group("year") and day < now.date():
                day = _safe_date(year + 1, month, int(match.group("day")))
            if day:
                spans.append(match.span())
    if day is None:
        match = _search(_NUMDATE_RE, low, spans)
        if match:
            raw_year = match.group("year")
            year = int(raw_year) + (2000 if raw_year and len(raw_year) == 2 else 0) if raw_year else now.year
            day = _safe_date(year, int(match.group("mon")), int(match.group("day")))
            if day and not raw_year and day < now.date():
                day = _safe_date(year + 1, int(match.group("mon")), int(match.group("day")))
            if day:
                spans.append(match.span())
    if day is None:
        match = _search(_WEEKEND_RE, low, spans)
        if match:
            day = now.date() if now.weekday() >= 5 else _next_weekday(now.date(), 5)
            spans.append(match.span())

    # 4. Время.
    hour = minute = None
    qualified = False
    match = _search(_HALF_RE, low, spans)
    if match:
        hour, minute = _ORDINAL_GEN[match.group("ord")] - 1, 30
        if match.group("part"):
            hour, qualified = _apply_part(hour, match.group("part")), True
        spans.append(match.span())
    for pattern in (_TIME_RE, _BARE_TIME_RE):
        if hour is not None:
            break
        match = _search(pattern, low, spans)
        if match:
            value_h, value_m = int(match.group("h")), int(match.group("m") or 0)
            if value_h <= 23 and value_m <= 59:
                hour, minute = value_h, value_m
                if match.group("part"):
                    hour, qualified = _apply_part(hour, match.group("part")), True
                qualified = qualified or value_h >= 13 or value_h == 0
                spans.append(match.span())
    if hour is None:
        match = _search(_WORD_TIME_RE, low, spans)
        if match:
            hour, minute = _HOUR_WORDS[match.group("hw")], 0
            if match.group("part"):
                hour, qualified = _apply_part(hour, match.group("part")), True
            elif match.group("hw") == "час":
                hour = 13  # «в час» без уточнения — днём
            spans.append(match.span())
    if hour is None:
        match = _search(_NOON_RE, low, spans)
        if match:
            hour, minute, qualified = (12, 0, True) if match.group("noon") == "полдень" else (0, 0, True)
            if match.group("noon") == "полночь" and day is None:
                day = now.date() + dt.timedelta(days=1)
            spans.append(match.span())
    match = _search(_PART_RE, low, spans)
    if match:
        word = match.group(0)
        if hour is None:
            hour, minute, qualified = _PARTS[word], 0, True
        elif not qualified:
            hour, qualified = _part_word_shift(hour, word), True
        spans.append(match.span())

    if not spans:
        return None

    # 5. Сборка.
    if repeat:
        kind, _, arg = repeat.partition(":")
        if kind == "every":
            at = (dt.datetime.combine(day or now.date(), dt.time(hour, minute or 0))
                  if hour is not None else now + dt.timedelta(minutes=int(arg)))
            if at <= now:
                at = advance(at, repeat, now) or at
        else:
            use_hour = hour if hour is not None else (default_hour if default_hour is not None else 9)
            if kind == "weekly" and not arg:
                repeat = f"weekly:{(day or now.date()).weekday()}"
            if kind == "monthly" and not arg:
                repeat = f"monthly:{(day or now.date()).day}"
            at = first_at(repeat, use_hour, minute or 0, now, start=day)
            if repeat_day and at.day != repeat_day:
                at = first_at(repeat, use_hour, minute or 0, now)
    elif delta is not None:
        at = now + delta
        if hour is not None and delta >= dt.timedelta(days=1):
            at = at.replace(hour=hour, minute=minute or 0, second=0)
        at = at.replace(second=0) if delta < dt.timedelta(minutes=1) else at
    elif day is not None:
        if hour is None:
            if day == now.date():
                evening = now.replace(hour=19, minute=0, second=0)
                at = evening if evening > now + dt.timedelta(minutes=30) else \
                    (now + dt.timedelta(hours=1)).replace(second=0)
            else:
                at = dt.datetime.combine(day, dt.time(9, 0))
        else:
            at = dt.datetime.combine(day, dt.time(hour, minute or 0))
            if at <= now and same_weekday:
                at += dt.timedelta(days=7)  # «в пятницу в 10», сказанное в пятницу в полдень, — следующая
            if at <= now and not qualified and hour <= 11 and at + dt.timedelta(hours=12) > now:
                at += dt.timedelta(hours=12)
    elif hour is not None:
        at = now.replace(hour=hour, minute=minute or 0, second=0)
        if at <= now:
            if not qualified and hour <= 11 and at + dt.timedelta(hours=12) > now:
                at += dt.timedelta(hours=12)
            else:
                at += dt.timedelta(days=1)
    else:
        return None

    rest = original
    for start, end in sorted(spans, reverse=True):
        rest = rest[:start] + " " + rest[end:]
    rest = _TRIGGER_RE.sub(" ", rest, count=1)
    rest = re.sub(r"\b(?:пожалуйста|плиз)\b", " ", rest, flags=re.IGNORECASE)
    rest = " ".join(rest.split()).strip(" ,.:;!—–-")
    rest = re.sub(r"^(?:о\s+том,?\s+что|о\s+том,?\s+чтобы|что|чтобы|насчет|насчёт|мне|нам)\s+", "", rest,
                  flags=re.IGNORECASE).strip(" ,.:;!—–-")
    return {"at": at, "iso": at.strftime("%Y-%m-%d %H:%M:%S"), "repeat": repeat, "text": rest,
            "label": label(at, repeat, now), "past": at <= now}


# ---------------------------------------------------------------------------
# Даты для целей и счёта дней: «до конца года», «к 1 декабря», «до лета»
# ---------------------------------------------------------------------------

_SEASONS = {"лет": 6, "осен": 9, "зим": 12, "весн": 3}
_DEADLINE_RE = re.compile(
    r"\b(?:до|к|ко)\s+(?:(?P<end>конц\w*|окончани\w*)\s+(?P<what>года|месяца|недели)"
    r"|(?P<ny>нов\w+\s+год\w*)|(?P<season>лет\w*|осен\w*|зим\w*|весн\w*)"
    r"|(?P<day>\d{1,2})(?:-?го)?\s+" + _MONTH_RE + r"(?:\s+(?P<year>\d{4}))?"
    r"|(?P<nd>\d{1,2})\.(?P<nm>\d{1,2})(?:\.(?P<ny2>\d{2,4}))?"
    r"|(?P<wd>" + _WD_WORDS.replace("среду", "среды").replace("пятницу", "пятницы").replace("субботу", "субботы")
    + r"|понедельника|вторника|четверга|воскресенья))\b"
    r"|\bза\s+(?P<n>" + NUM + r")\s+(?P<u>дн\w*|день|недел\w*|месяц\w*|год\w*|лет)\b")
_WD_GEN = {"понедельника": 0, "вторника": 1, "среды": 2, "четверга": 3, "пятницы": 4, "субботы": 5, "воскресенья": 6}


def target_date(text: str, today: dt.date, new_year_is_first: bool = True) -> tuple[dt.date, tuple[int, int]] | None:
    """Дата из «до/к …»: конец года, Новый год, лето, 1 декабря, 31.12, пятница, «за 3 месяца».

    `new_year_is_first` — «до Нового года» для счёта дней это 1 января; для
    срока цели («к Новому году») — 31 декабря.
    """
    low = str(text or "").lower().replace("ё", "е")
    match = _DEADLINE_RE.search(low)
    if not match:
        return None
    if match.group("end"):
        what = match.group("what")
        if what == "года":
            return dt.date(today.year, 12, 31), match.span()
        if what == "месяца":
            first_next = (dt.date(today.year + 1, 1, 1) if today.month == 12
                          else dt.date(today.year, today.month + 1, 1))
            return first_next - dt.timedelta(days=1), match.span()
        return today + dt.timedelta(days=6 - today.weekday()), match.span()
    if match.group("ny"):
        return (dt.date(today.year + 1, 1, 1) if new_year_is_first else dt.date(today.year, 12, 31)), match.span()
    if match.group("season"):
        month = next(value for stem, value in _SEASONS.items() if match.group("season").startswith(stem))
        date = dt.date(today.year, month, 1)
        return (date if date > today else dt.date(today.year + 1, month, 1)), match.span()
    if match.group("day"):
        month = _month(match.group("mon"))
        year = int(match.group("year") or today.year)
        date = _safe_date(year, month, int(match.group("day"))) if month else None
        if date and not match.group("year") and date < today:
            date = _safe_date(year + 1, month, int(match.group("day")))
        return (date, match.span()) if date else None
    if match.group("nd"):
        raw_year = match.group("ny2")
        year = (int(raw_year) + (2000 if len(raw_year) == 2 else 0)) if raw_year else today.year
        date = _safe_date(year, int(match.group("nm")), int(match.group("nd")))
        if date and not raw_year and date < today:
            date = _safe_date(year + 1, int(match.group("nm")), int(match.group("nd")))
        return (date, match.span()) if date else None
    if match.group("wd"):
        weekday = _WD_GEN.get(match.group("wd"), _WD_ACC.get(match.group("wd"), 0))
        days = (weekday - today.weekday()) % 7 or 7
        return today + dt.timedelta(days=days), match.span()
    count = number(match.group("n")) or 1
    unit = match.group("u")
    if unit.startswith("недел"):
        days = 7 * count
    elif unit.startswith("месяц"):
        days = 30 * count
    elif unit.startswith(("год", "лет")):
        days = 365 * count
    else:
        days = count
    return today + dt.timedelta(days=round(days)), match.span()


def any_date(text: str, today: dt.date) -> dt.date | None:
    """Дата из фразы без «до»: «1 декабря», «25.12», «Новый год», «в пятницу»."""
    low = str(text or "").lower().replace("ё", "е")
    found = target_date(low, today) or target_date("до " + low, today)
    if found:
        return found[0]
    parsed = parse(low, dt.datetime.combine(today, dt.time(0, 0)))
    return parsed["at"].date() if parsed else None
