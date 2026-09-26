"""Понимание вопроса владельца: за какой период и о чём (18.24, идеи И337–И341).

Что было до 18.24. Вопросы про деньги упирались в одну цифру: слой «Деньги»
всегда спрашивал `acc.summary(30)` и отвечал «За 30 дней: доход …». «Какой доход
с 21.09 по 25.09?» и «Какой доход за неделю?» получали один и тот же ответ за
месяц — период во фразе просто не читался ни одним слоем. Вопросы про товары
читались ещё хуже: слово «товар» относило фразу к теме «склад», и на «Топ
товаров за неделю?» помощник вываливал остатки стеллажа с ценниками — то, что
лежит на полке, вместо того, что у неё купили. Модель(Ollama) в этот момент
была недоступна, поэтому владелец видел не «модель ошиблась», а «помощник не
понимает по-русски».

Как теперь. Понимание вопроса выделено в отдельный детерминированный модуль,
который не зовёт модель и не зависит от неё:

  * `parse()` читает период из фразы: точный диапазон («с 21.09 по 25.09»,
    «21–25 сентября», «с 21 по 25 число»), относительное окно («за неделю»,
    «за 30 дней», «на прошлой неделе», «с начала месяца»), календарный месяц
    («в сентябре», «за сентябрь»), день («сегодня», «вчера», «позавчера»);
  * `money_question()` и `top_question()` отвечают, о чём фраза — о деньгах или
    о том, что покупают. `rank_by()` отличает «что берут» (по штукам) от
    «что приносит деньги» (по сумме);
  * периода нет во фразе — `Period.explicit=False` и берётся разумное окно
    (30 дней). Ответ всё равно называет даты, поэтому «за 30 дней» нельзя
    принять за «за неделю»;
  * `previous()` даёт окно той же длины рядом — для сравнения «больше/меньше
    прошлой недели» без второго вопроса.

Инварианты: границы периода полуоткрытые (`start` включается, `until` нет) —
ровно так их ждут SQL-запросы учёта; числа не выдумываются — если периода во
фразе нет, модуль честно возвращает окно по умолчанию и признак `explicit`.
Модуль не знает ни про базу, ни про модель: только русская фраза и часы.
"""
from __future__ import annotations

import calendar
import datetime
import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_DAYS = 30

_MONTHS: dict[str, int] = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "май": 5, "мая": 5, "мае": 5,
    "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}
# Месяц в любой форме: «сентябрь», «сентября», «сентябре». Границы слов
# обязательны: иначе «маяк» и «мартышка» становятся названиями месяцев.
_MONTH_RE = re.compile(
    r"(?<![а-я])(январ(?:ь|я|е)|феврал(?:ь|я|е)|март[а]?|апрел(?:ь|я|е)|ма[йяе]|"
    r"июн(?:ь|я|е)|июл(?:ь|я|е)|август[ае]?|сентябр(?:ь|я|е)|октябр(?:ь|я|е)|"
    r"ноябр(?:ь|я|е)|декабр(?:ь|я|е))(?![а-я])")
_MONTH_NAMES = {1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
                7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября",
                12: "декабря"}
_MONTH_TITLES = {1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
                 7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь",
                 12: "декабрь"}

_SEP = r"[.\-/]"
_D = r"(?P<d1>\d{1,2})"
_D2 = r"(?P<d2>\d{1,2})"
_Y1 = r"(?:[.\-/](?P<y1>\d{2,4}))?"
_Y2 = r"(?:[.\-/](?P<y2>\d{2,4}))?"

# «с 21.09 по 25.09», «21.09-25.09», «с 21.09.2026 по 25.09.2026»
_RE_DIGITS = re.compile(rf"(?:с\s+)?{_D}{_SEP}(?P<m1>\d{{1,2}}){_Y1}\s*(?:по|до|-|–|—)\s*"
                        rf"{_D2}{_SEP}(?P<m2>\d{{1,2}}){_Y2}")
# «с 21 по 25 сентября», «21-25 сентября», «с 21 сентября по 25 октября»
_RE_WORDS = re.compile(
    rf"(?:с\s+)?{_D}(?:\s*(?P<m1w>{_MONTH_RE.pattern}))?\s*(?:по|до|-|–|—)\s*"
    rf"{_D2}(?:\s*(?P<m2w>{_MONTH_RE.pattern}))?")
# «за 21.09», «21 сентября», «25.09.2026»
_RE_ONE = re.compile(rf"(?<![\d.]){_D}(?:{_SEP}(?P<m>\d{{1,2}})(?:{_SEP}(?P<y>\d{{2,4}}))?|"
                     rf"\s*(?P<mw>{_MONTH_RE.pattern}))(?![\d])")
# «с 21 по 25» — числа месяца без названия месяца
_RE_PLAIN = re.compile(rf"с\s+{_D}\s*(?:по|до|-|–|—)\s*{_D2}(?!\s*{_SEP}\d)")

_RE_N_DAYS = re.compile(r"(?:за|последние|за\s+последние)\s+(\d{1,3})\s*"
                        r"(дн[ейя]*|суток|сутки|день|дня)")
_RE_SPAN = re.compile(r"за\s+(?:один\s+|одну\s+|две\s+|два\s+|три\s+|четыре\s+)?"
                      r"(?P<n>\d{1,2})?\s*(?P<u>недел\w*|месяц\w*|квартал\w*|год[а]?|"
                      r"полгод[а]?|сутки|день)")
_SPAN_WORD_MULT = {"один": 1, "одну": 1, "две": 2, "два": 2, "три": 3, "четыре": 4}
_RE_THIS_WEEK = re.compile(r"(?:на|за|в)\s+(эт[уо]й|эту|текущ\w+|нынешн\w+)\s+недел\w*")
_RE_LAST_WEEK = re.compile(r"(?:на|за|в)\s+(прошл\w+|предыдущ\w+|минувш\w+)\s+недел\w*")
_RE_THIS_MONTH = re.compile(r"(?:в|за|на)\s+(этом|текущем|нынешнем)\s+месяц\w*"
                            r"|с\s+начала\s+месяца")
_RE_LAST_MONTH = re.compile(r"(?:в|за|на)\s+(прошлом|предыдущем|минувшем)\s+месяц\w*")
_RE_MONTH_ONLY = re.compile(rf"(?:в|за|месяц\s+)?\s*(?P<m>{_MONTH_RE.pattern})")
_RE_YEAR = re.compile(r"(?:в|за)\s+(20\d{2})\s*(?:год\w*)?")
_RE_WEEKEND = re.compile(r"(?:за|на|в)\s+(выходн\w+|эти\s+выходные)")
_RE_YTD = re.compile(r"с\s+начала\s+год[а]?|за\s+(этот\s+)?год")

# Неделя начинается с понедельника, как в календаре цеха.


@dataclass(frozen=True)
class Period:
    """Окно времени вопроса: границы, подпись и как оно было понято.

    `start` включается, `until` нет — ровно то, что ждут запросы учёта
    (`at>=? AND at<?`). `explicit=False` означает, что периода во фразе не было
    и взято окно по умолчанию: отвечающий обязан показать даты, чтобы владелец
    не принял месяц за неделю.
    """

    start: str
    until: str
    label: str
    phrase: str
    days: int
    kind: str
    explicit: bool
    matched: str
    first: datetime.date
    last: datetime.date
    today: "datetime.date | None" = field(default=None, compare=False)

    @property
    def start_date(self) -> str:
        return self.first.isoformat()

    @property
    def end_date(self) -> str:
        """Последний день окна (включительно)."""
        return self.last.isoformat()

    def previous(self) -> "Period":
        """Окно той же длины сразу перед этим: «больше прошлой недели?»."""
        return shift(self, -1)

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "until": self.until, "first": self.first.isoformat(),
                "last": self.last.isoformat(), "days": self.days, "label": self.label,
                "phrase": self.phrase, "kind": self.kind, "explicit": self.explicit}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Period | None":
        """Окно из мета реплики: продолжение «а за прошлую неделю?».

        Мета хранит результат `as_dict`. Не хватает границ или дней — честно
        `None`: дальше мозг не продолжает тему, а отвечает как на новый вопрос.
        """
        if not data:
            return None
        try:
            return cls(start=str(data["start"]), until=str(data["until"]),
                       label=str(data.get("label") or ""), phrase=str(data.get("phrase") or ""),
                       days=int(data.get("days") or 0), kind=str(data.get("kind") or "range"),
                       explicit=bool(data.get("explicit")), matched="",
                       first=datetime.date.fromisoformat(str(data["first"])),
                       last=datetime.date.fromisoformat(str(data["last"])))
        except (KeyError, TypeError, ValueError):
            return None


def _norm(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def _month_of(word: str) -> int:
    for prefix, number in _MONTHS.items():
        if str(word or "").startswith(prefix):
            return number
    return 0


def _year(value: str | None, now: datetime.date) -> int:
    if not value:
        return now.year
    number = int(value)
    if number < 100:
        return 2000 + number
    return number


def _day(number: str | None, month: int, year: int, now: datetime.date) -> datetime.date:
    """День календаря: год подставляется, а будущее считается прошлым годом."""
    day = max(1, min(31, int(number or 1)))
    try:
        date = datetime.date(year, month, day)
    except ValueError:  # 31 февраля и тому подобное
        date = datetime.date(year, month, 1)
    if date > now + datetime.timedelta(days=1):
        try:
            date = date.replace(year=year - 1)
        except ValueError:
            date = date.replace(year=year - 1, day=28)
    return date


def _label(first: datetime.date, last: datetime.date, now: datetime.date) -> str:
    """Подпись окна: «21–25 сентября», «28 сентября – 3 октября 2025»."""
    year_suffix = "" if last.year == now.year else f" {last.year}"
    if first == last:
        return f"{first.day} {_MONTH_NAMES[first.month]}{year_suffix}"
    if first.month == last.month and first.year == last.year:
        return f"{first.day}–{last.day} {_MONTH_NAMES[first.month]}{year_suffix}"
    return (f"{first.day} {_MONTH_NAMES[first.month]} – {last.day} "
            f"{_MONTH_NAMES[last.month]}{year_suffix}")


def _phrase(first: datetime.date, last: datetime.date, label: str, kind: str, days: int,
            now: datetime.date) -> str:
    """Готовая вставка в ответ: «с 21 по 25 сентября», «за неделю (19–25.09)»."""
    if kind == "range":
        year_suffix = "" if last.year == now.year else f" {last.year}"
        if first.month != last.month or first.year != last.year:
            return (f"с {first.day} {_MONTH_NAMES[first.month]} по {last.day} "
                    f"{_MONTH_NAMES[last.month]}{year_suffix}")
        return f"с {first.day} по {last.day} {_MONTH_NAMES[last.month]}{year_suffix}"
    if kind == "today":
        return f"сегодня ({first:%d.%m})"
    if kind == "yesterday":
        return f"вчера ({first:%d.%m})"
    if kind == "day":
        return f"{first.day} {_MONTH_NAMES[first.month]}"
    if kind == "day_before":
        return f"позавчера ({first:%d.%m})"
    if kind == "week":
        return f"на этой неделе ({first:%d.%m}–{last:%d.%m})"
    if kind == "last_week":
        return f"на прошлой неделе ({first:%d.%m}–{last:%d.%m})"
    if kind == "weekend":
        return f"за выходные ({first:%d.%m}–{last:%d.%m})"
    if kind == "month":
        return f"за {_MONTH_TITLES[first.month]} {first.year}"
    if kind == "quarter":
        return f"за {days} дней ({label})"
    if kind == "year":
        return f"за {first.year}"
    short = f"{first:%d.%m}–{last:%d.%m}"
    if days == 7:
        return f"за неделю ({short})"
    if days == 30:
        return f"за месяц ({short})"
    if days == 90:
        return f"за квартал ({short})"
    if days == 365:
        return f"за год ({short})"
    return f"за {days} дн. ({short})"


def _make(first: datetime.date, last: datetime.date, now: datetime.date, *,
          kind: str, explicit: bool, matched: str) -> Period:
    if last < first:
        first, last = last, first
    start = datetime.datetime.combine(first, datetime.time.min).isoformat(timespec="seconds")
    until = datetime.datetime.combine(last + datetime.timedelta(days=1),
                                      datetime.time.min).isoformat(timespec="seconds")
    days = (last - first).days + 1
    label = _label(first, last, now)
    return Period(start=start, until=until, label=label,
                  phrase=_phrase(first, last, label, kind, days, now), days=days,
                  kind=kind, explicit=explicit, matched=matched.strip(),
                  first=first, last=last, today=now)


def span(now: datetime.date, days: int, *, kind: str = "span", explicit: bool = False,
         matched: str = "") -> Period:
    """Окно на `days` календарных дней, заканчивающееся сегодня."""
    days = max(1, int(days or 1))
    return _make(now - datetime.timedelta(days=days - 1), now, now, kind=kind,
                 explicit=explicit, matched=matched)


def shift(period: Period, direction: int) -> Period:
    """Сдвиг окна на его длину: `-1` — предыдущий такой же период."""
    step = period.days * int(direction or 1)
    return _make(period.first + datetime.timedelta(days=step),
                 period.last + datetime.timedelta(days=step),
                 period.today or period.last, kind=period.kind, explicit=period.explicit,
                 matched=period.matched)


def _today(now: datetime.datetime) -> datetime.date:
    return now.date() if isinstance(now, datetime.datetime) else now


# ---------------------------------------------------------------------------
# Чтение периода
# ---------------------------------------------------------------------------

def _absolute(low: str, now: datetime.date) -> Period | None:
    """Точные даты: диапазон цифрами, диапазон со словами, одна дата."""
    found = _RE_DIGITS.search(low)
    if found:
        year = _year(found.group("y1") or found.group("y2"), now)
        first = _day(found.group("d1"), int(found.group("m1")), year, now)
        last = _day(found.group("d2"), int(found.group("m2")),
                    _year(found.group("y2") or found.group("y1"), now), now)
        if first > last:  # «с 25.12 по 05.01» — конец в следующем году
            last = last.replace(year=last.year + 1)
        return _make(first, last, now, kind="range", explicit=True, matched=found.group(0))

    found = _RE_PLAIN.search(low)
    if found:
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        first = datetime.date(now.year, now.month, min(int(found.group("d1")), days_in_month))
        last = datetime.date(now.year, now.month, min(int(found.group("d2")), days_in_month))
        if first > last:  # «с 28 по 3» — начало в прошлом месяце
            prev_last = now.replace(day=1) - datetime.timedelta(days=1)
            prev_days = calendar.monthrange(prev_last.year, prev_last.month)[1]
            first = datetime.date(prev_last.year, prev_last.month,
                                  min(int(found.group("d1")), prev_days))
        return _make(first, last, now, kind="range", explicit=True, matched=found.group(0))

    found = _RE_WORDS.search(low)
    if found is None:
        month1 = month2 = 0
    else:
        month1 = _month_of(found.group("m1w") or "")
        month2 = _month_of(found.group("m2w") or "")
    if found is not None and (month1 or month2):
        month = month1 or month2
        first = _day(found.group("d1"), month, now.year, now)
        last = _day(found.group("d2"), month2 or month, now.year, now)
        if first > last:
            last = last.replace(year=last.year + 1)
        return _make(first, last, now, kind="range", explicit=True, matched=found.group(0))

    found = _RE_ONE.search(low)
    if found:
        month = int(found.group("m") or 0) or _month_of(found.group("mw") or "")
        if month:
            day = _day(found.group("d1"), month, _year(found.group("y"), now), now)
            kind = "today" if day == now else "day"
            return _make(day, day, now, kind=kind, explicit=True, matched=found.group(0))
    return None


def _relative(low: str, now: datetime.date) -> Period | None:
    """Относительные окна: неделя, месяц, «на прошлой неделе», месяц словами."""
    if "сегодня" in low:
        return _make(now, now, now, kind="today", explicit=True, matched="сегодня")
    if "позавчера" in low:
        return _make(now - datetime.timedelta(days=2), now - datetime.timedelta(days=2),
                     now, kind="day_before", explicit=True, matched="позавчера")
    if "вчера" in low:
        return _make(now - datetime.timedelta(days=1), now - datetime.timedelta(days=1),
                     now, kind="yesterday", explicit=True, matched="вчера")

    found = _RE_YEAR.search(low)
    if found:
        year = int(found.group(1))
        last = datetime.date(year, 12, 31)
        if last > now:  # текущий или будущий год — считаем до сегодня
            last = now
        return _make(datetime.date(year, 1, 1), last, now, kind="year", explicit=True,
                     matched=found.group(0))

    found = _RE_WEEKEND.search(low)
    if found:
        # последний завершившийся выходной: ближайшее воскресенье ≤ сегодня
        sunday = now - datetime.timedelta(days=(now.weekday() - 6) % 7)
        saturday = sunday - datetime.timedelta(days=1)
        return _make(saturday, sunday, now, kind="weekend", explicit=True,
                     matched=found.group(0))

    found = _RE_LAST_WEEK.search(low)
    if found:
        monday = now - datetime.timedelta(days=now.weekday() + 7)
        return _make(monday, monday + datetime.timedelta(days=6), now, kind="last_week",
                     explicit=True, matched=found.group(0))
    found = _RE_THIS_WEEK.search(low)
    if found:
        monday = now - datetime.timedelta(days=now.weekday())
        return _make(monday, now, now, kind="week", explicit=True, matched=found.group(0))

    found = _RE_LAST_MONTH.search(low)
    if found:
        first_this = now.replace(day=1)
        last_prev = first_this - datetime.timedelta(days=1)
        return _make(last_prev.replace(day=1), last_prev, now, kind="month",
                     explicit=True, matched=found.group(0))
    found = _RE_THIS_MONTH.search(low)
    if found:
        return _make(now.replace(day=1), now, now, kind="month", explicit=True,
                     matched=found.group(0))

    found = _RE_YTD.search(low)
    if found:
        return _make(datetime.date(now.year, 1, 1), now, now, kind="year", explicit=True,
                     matched=found.group(0))

    found = _RE_N_DAYS.search(low)
    if found:
        days = max(1, int(found.group(1)))
        kind = "today" if days == 1 else "span"
        return span(now, days, kind=kind, explicit=True, matched=found.group(0))

    found = _RE_SPAN.search(low)
    if found:
        head = found.group(0)
        number = 1
        if found.group("n"):  # «за 2 недели», «за 3 месяца»
            number = int(found.group("n"))
        elif head[2:].split()[:1][0].strip(" ") in _SPAN_WORD_MULT:  # «за две недели»
            number = _SPAN_WORD_MULT[head[2:].split()[0]]
        unit = found.group("u")
        if unit.startswith("недел"):
            days = 7 * number
        elif unit.startswith("месяц"):
            days = 30 * number
        elif unit.startswith("квартал"):
            days = 90 * number
        elif unit.startswith("полгод"):
            days = 180 * number
        elif unit.startswith("год"):
            days = 365 * number
        else:  # сутки, день
            days = number
        days = max(1, min(days, 730))
        kind = "today" if days == 1 else "span"
        return span(now, days, kind=kind, explicit=True, matched=found.group(0))

    found = _RE_MONTH_ONLY.search(low)
    if found:
        month = _month_of(found.group("m"))
        if month:
            # «за сентябрь 2025» — год назван явно и побеждает текущему;
            # без года текущий месяц считается только до сегодня.
            tail = low[found.end("m"):]
            year_match = re.search(r"(20\d{2})", tail)
            if year_match:
                year = int(year_match.group(1))
            else:
                year = now.year
                if month > now.month + 1:  # сентябрь в январе — прошлый год
                    year -= 1
            first = datetime.date(year, month, 1)
            next_month = datetime.date(year + (month // 12), month % 12 + 1, 1)
            last = next_month - datetime.timedelta(days=1)
            if not year_match and last > now:
                last = now
            return _make(first, last, now, kind="month", explicit=True,
                         matched=found.group("m"))
    return None


def parse(text: str, now: datetime.datetime | None = None) -> Period:
    """Период из русской фразы. Всегда возвращает окно: без периода — 30 дней."""
    moment = now or datetime.datetime.now()
    day = _today(moment)
    low = _norm(text)
    found = _absolute(low, day) or _relative(low, day)
    if found is not None:
        return found
    return span(day, DEFAULT_DAYS, kind="default", explicit=False, matched="")


# ---------------------------------------------------------------------------
# О чём вопрос
# ---------------------------------------------------------------------------

_MONEY_STRONG = ("доход", "выручк", "прибыл", "маржа", "заработал", "заработ", "оборот",
                 "финанс", "денег", "деньг", "касс", "убыт", "рентаб", "продаж",
                 "продал", "продано", "выруч")
_MONEY_WEAK = ("расход", "затрат", "потратил", "трат")
_PLASTIC = ("пластик", "филамент", "катуш", "бобин", "материал")
# Сильные слова сами открывают вопрос о товарах; слабые — только с ним же.
_TOP_STRONG = ("топ", "популярн", "ходов", "лидер", "продав", "востребован", "берут",
               "берем", "берём", "покупают", "покупаем", "чаще всего", "больше всего")
_TOP_WEAK = ("лучш", "самые", "самый")
_GOODS = ("товар", "позици", "издели", "модел", "штук", "ваз", "брелок", "заклад")
_QTY_WORDS = ("берут", "берем", "берём", "покупают", "покупаем", "штук", "шт ", "шт.",
              "популярн", "чаще", "част", "больше всего", "по количеств", "сколько раз")
_CLIENTS = ("клиент", "заказчик", "покупател")


def money_question(text: str) -> bool:
    """«Сколько денег» — про доход, прибыль, маржу, расходы или кассу.

    «Расход» считается деньгами только без слов про пластик: «расход пластика»
    — вопрос склада, а не финансов.
    """
    low = _norm(text)
    if any(word in low for word in _MONEY_STRONG):
        return True
    if any(word in low for word in _MONEY_WEAK):
        return not any(word in low for word in _PLASTIC)
    return False


def top_subject(text: str) -> str | None:
    """Чем хвалятся: «products» — что берут, «customers» — кто покупает, иначе None.

    «Лучший» и «самый» открывают топ только рядом со словом про предмет:
    «самый большой заказ» — не про товары. «Сколько продали» — про сумму, не про
    список; «сколько штук» — наоборот, про список. «Топ клиентов и товаров»
    двоен и честнее не догадываться.
    """
    low = _norm(text)
    has_goods = any(word in low for word in _GOODS)
    has_clients = any(word in low for word in _CLIENTS)
    if not (any(word in low for word in _TOP_STRONG)
            or (any(word in low for word in _TOP_WEAK) and (has_goods or has_clients))):
        return None
    if "сколько" in low and not any(word in low for word in ("штук", "шт")):
        return None
    if has_goods and has_clients:
        return None
    if has_clients:
        return "customers"
    return "products"


def top_question(text: str) -> bool:
    """«Что берут / топ товаров» — про то, что покупают, а не про остатки."""
    return top_subject(text) == "products"


def rank_by(text: str) -> str:
    """По чему сортировать топ: `qty` — «что берут», `amount` — «что приносит»."""
    low = _norm(text)
    if any(word in low for word in _QTY_WORDS):
        return "qty"
    return "amount"


def period_question(text: str) -> bool:
    """Во фразе явно назван период (нужно ли вообще искать окно)."""
    return parse(text).explicit
