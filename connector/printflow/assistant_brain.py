"""Мозг помощника панели (18.21): понять, вспомнить, найти в базе, предложить, ответить.

Что было до 18.21. Страница сама решала, куда отправить фразу: регулярка
`looksLikeCommand` выбирала между «диспетчером каталога» (модель выбирает одно
действие из двадцати) и «разговором» (модель отвечает прозой). У обоих путей не
было ни памяти, ни контекста: «поставь его на паузу» после вопроса про P1S
модель понимала как угодно, «а у второго?» — никак, имя станка и номер заказа
угадывались моделью вместо поиска в базе, а история жила в браузере шестью
репликами.

Как теперь. Одна точка входа `chat()` и порядок слоёв, в котором каждый
следующий зовётся только если предыдущий не справился:

  1. **Уточнение.** Если прошлая реплика помощника спросила «какой станок?», короткий
     ответ («второй», «P2S») завершает начатое действие.
  2. **Память.** «Запомни…», «забудь…», «что ты помнишь про Марию», «меня зовут…».
  3. **Мгновенные ответы.** Арифметика, часы и дата, «что ты умеешь», приветствие
     со сводкой цеха — без модели.
  4. **Цех без модели.** Команды станкам, заказы, клиенты, долги, деньги, план,
     склад, переходы по разделам — правила плюс поиск сущностей в базе:
     станок по имени, модели, порядковому номеру или местоимению из контекста,
     заказ по номеру или имени клиента. Два подходящих станка — вопрос «какой?».
     Деньги — за период из фразы (`periods.parse`: «с 21.09 по 25.09», «за
     неделю», «за сентябрь», «на прошлой неделе»); без периода — 30 дней, и
     ответ всё равно называет даты. «Топ товаров за неделю», «что берут»,
     «топ клиентов» — реестр продаж с этими границами, а не остатки стеллажа.
     Короткие продолжения — «а за прошлую неделю?», «а по штукам?», «а
     клиенты?», «а доход?», «а через Авито?», «а по дням?» — наследуют тему и
     окно из мета прошлой реплики (`_followup`), как человек не повторяет
     вопрос целиком; поправки «нет, за 2 недели», «не …, а …» читаются как
     замена окна/темы; период без темы и контекста — живое уточнение с тремя
     срезами окна. Срезы: деньги (итого и по дням, динамика «растут/падают» —
     сравнением с окном той же длины), каналы продаж («через Авито/ВБ/стеллаж»
     — из реестра), крупные заказы за окно, печать, расходы по категориям,
     счёт заказов.
  5. **Компьютер.** Команды ПК уходят агенту (`POST /chat`, `mode=pc`) — он
     исполняет их по своим правилам подтверждения и отвечает словами.
  6. **Модель.** Планировщик в JSON-режиме видит факты цеха, память и
     разговор; выбранное действие проверяется каталогом (`assistant.ACTIONS`).
  7. **Разговор.** Всё остальное отвечает модель по фактам цеха, свежее — с
     веб-поиском Ollama (`assistant.converse`), без модели — факты базы.

Инварианты прежние и проверяются тестами: помощник ничего не исполняет с
деньгами и печатью — он возвращает действие каталога, адрес и признак
подтверждения берутся из каталога на сервере, а выполняет человек кнопкой
«Подтвердить». Числа не выдумываются: ответы шагов 3–4 собраны из тех же
сервисов, что рисуют панель.
"""
from __future__ import annotations

import datetime
import re
import time
from typing import Any

from . import assistant, assistant_knowledge as knowledge, assistant_memory as memory
from . import periods
from .logging_setup import log

MAX_TEXT = 1000
HISTORY_TURNS = 12

_ORDINALS = {"первый": 0, "первого": 0, "первом": 0, "первому": 0, "1": 0, "один": 0,
             "второй": 1, "второго": 1, "втором": 1, "второму": 1, "2": 1, "два": 1,
             "третий": 2, "третьего": 2, "третьем": 2, "3": 2, "три": 2,
             "четвертый": 3, "четвертого": 3, "4": 3, "пятый": 4, "5": 4,
             "последний": -1, "последнего": -1, "последнем": -1}
_PRONOUNS = ("его", "него", "ему", "этот", "эту", "это", "он", "она", "её", "ее", "нем", "нём", "тот", "туда")

ORDER_STATUS_WORDS: dict[str, str] = {
    "нов": "new", "смет": "estimate", "расчет": "estimate", "предоплат": "prepay", "оплат": "prepay",
    "очеред": "queue", "печат": "printing", "производ": "printing", "пост": "post", "обработ": "post",
    "готов": "ready",
}
ORDER_STATUS_TITLES = {"new": "новый", "estimate": "смета", "prepay": "предоплата", "queue": "в очереди",
                       "printing": "печать", "post": "постобработка", "ready": "готов", "done": "выдан"}

NAV: dict[str, tuple[str, tuple[str, ...]]] = {
    "dashboard": ("Обзор", ("обзор", "главн", "дашборд")),
    "printers": ("Принтеры", ("принтер", "станк", "парк", "камер")),
    "queue": ("Очередь печати", ("очеред", "задани")),
    "orders": ("Заказы", ("заказ", "канбан", "доск")),
    "inventory": ("Склад · катушки", ("склад", "катуш", "пластик", "инвентар")),
    "shelf": ("Стеллаж", ("стеллаж", "полк", "витрин")),
    "customers": ("Клиенты", ("клиент", "crm")),
    "finance": ("Финансы", ("финанс", "деньг", "налог", "учет", "учёт", "долг")),
    "calc": ("Калькулятор", ("калькулятор", "себестоим")),
    "settings": ("Настройки", ("настройк",)),
    "library": ("Библиотека", ("библиотек", "инструкц", "гайд")),
    "print": ("Печать форм", ("форм", "ценник", "этикет", "бланк")),
    "conveyor": ("Конвейер", ("конвейер", "farmloop")),
}

# Слова, по которым фраза — команда компьютеру, даже если агент сейчас выключен.
_PC_RE = re.compile(
    r"\b(окн[оаеу]|окон|громк|звук|музык|трек|пауз[ау] (музык|видео)|блокнот|калькулятор|проводник|браузер|"
    r"скриншот|снимок экрана|на экране|буфер|скопируй|вставь|клавиш|нажми|сверни|разверни|"
    r"переключись|рабочий стол|компьютер|пк\b|процесс|диспетчер задач|загрузк|папк|файл|документ|"
    r"телеграм|орк[ау]|bambu|бамбу|таймер|засеки|скажи вслух|озвучь|заблокируй)", re.IGNORECASE)


class Context:
    """Всё, что нужно одному разговору: база, сервисы, история и кэш снимка парка."""

    def __init__(self, api: Any, session: str, source: str) -> None:
        self.api = api
        self.db = getattr(api, "db", None)
        self.session = memory.session_key(session)
        self.source = source
        self.started = time.time()
        self.steps: list[dict[str, Any]] = []
        self._snapshot: dict[str, Any] | None = None
        self.history = memory.dialog(self.db, self.session, HISTORY_TURNS) if self.db is not None else []

    def step(self, kind: str, title: str, detail: str = "") -> None:
        self.steps.append({"kind": kind, "title": title, "detail": str(detail)[:240]})

    def snapshot(self) -> dict[str, Any]:
        if self._snapshot is None:
            manager = getattr(self.api, "manager", None)
            try:
                self._snapshot = manager.snapshot() if manager is not None else {}
            except Exception as exc:
                log().warning("Мозг помощника: снимок парка не прочитался (%s)", exc)
                self._snapshot = {}
        return self._snapshot or {}

    def entities(self) -> dict[str, Any]:
        """Сущности из недавних реплик: последний станок, заказ, клиент."""
        found: dict[str, Any] = {}
        for turn in reversed(self.history):
            meta = turn.get("meta") or {}
            for key, value in (meta.get("entities") or {}).items():
                if key not in found and value:
                    found[key] = value
        return found

    def last_meta(self) -> dict[str, Any]:
        for turn in reversed(self.history):
            if turn.get("role") == "assistant":
                return turn.get("meta") or {}
        return {}


def _norm(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def _money(value: Any) -> str:
    try:
        number = float(str(value or 0).replace(",", ".").replace(" ", ""))
    except ValueError:
        return str(value)
    shown = f"{number:,.0f}".replace(",", " ") if number == int(number) else f"{number:,.2f}".replace(",", " ")
    return f"{shown} ₽"


def plural(count: Any, one: str, few: str, many: str) -> str:
    """Форма слова для числа: 1 клиент, 2 клиента, 5 клиентов, 21 клиент, 11 клиентов."""
    try:
        value = abs(int(float(count)))
    except (TypeError, ValueError):
        return many
    if value % 10 == 1 and value % 100 != 11:
        return one
    if 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
        return few
    return many


def eta_label(minutes: float, now: datetime.datetime | None = None) -> str:
    """Во сколько закончит: «около 22:55», «завтра около 07:10»."""
    now = now or datetime.datetime.now()
    finish = now + datetime.timedelta(minutes=max(0.0, float(minutes or 0)))
    days = (finish.date() - now.date()).days
    prefix = "" if days <= 0 else ("завтра " if days == 1 else f"{finish:%d.%m} ")
    return f"{prefix}около {finish:%H:%M}"


# Ключи extra, которые помощник переносит в мета своей реплики: из них строится
# продолжение разговора («а за прошлую неделю?», «а по штукам?», «а клиенты?»).
_REMEMBER_META = ("topic", "period", "subject", "rank_by", "category", "view", "channel")

# Что именно человек имеет в виду в короткой реплике-продолжении. Порядок
# важен: «топ клиентов» — про клиентов, а не про топ; «кто больше покупал» —
# до слова «топ».
_TOPIC_CUSTOMER_RE = re.compile(r"(клиент|кто\s+(больше|много|чаще|покуп))")
_TOPIC_TOP_RE = re.compile(r"(топ|товар|продукт|что\s+(берут|популярн|забирают|продав))")
_TOPIC_MONEY_RE = re.compile(r"(доход|выручк|прибыл|расход|динамик|заработ|финанс|маржа|продал|продаж)")
_TOPIC_PRINT_RE = re.compile(r"(напечатал|задани|печати|печат)")
_TOPIC_SPEND_RE = re.compile(r"(потратил|затрат|ушло|ушли|налог|аренд|электричеств|филамент|пластик|катушк)")
_RANK_QTY_RE = re.compile(r"по\s+(штукам|штуке|количеств)")
_RANK_AMOUNT_RE = re.compile(r"по\s+(сумме|деньгам|выручк|рубл)")
# «больше, чем прошлая?» — и «как динамика?», «продажи растут?»: второй вариант
# окна человек не называет — берётся окно той же длины сразу перед этим.
_COMPARE_RE = re.compile(r"(больше|меньше|чем|динамик|раст\w*|пада\w*|вырос\w*|упал\w*|сниж\w*)")
# Период без темы: «а за прошлую неделю?» — после него человек ждёт уточнения.
_PERIOD_FILLERS = re.compile(
    r"\b(а|как|что|сколько|давай|дай|посмотри|покажи|смотри|какой|какая|какие|интересует|хочу|было|бы|есть|там|за)\b")


def _topic_signal(low: str) -> str:
    """Тема в короткой фразе: «customers», «top_orders», «top_products»,
    «money», «print», «spend», «orders» или пусто, если тема только из контекста.
    Порядок важен: «топ заказов» — про заказы, а не про топ товаров."""
    if _TOPIC_CUSTOMER_RE.search(low):
        return "customers"
    # «самый большой» без слова «заказ» — про заказы только в продолжении
    # («а самый большой?» после счёта заказов); в прямом вопросе слово надо.
    if re.search(r"(топ|крупн\w*|лучш\w*)\s+заказ|заказ\w*\s+(самый\s+больш\w*|крупн\w*)|самый\s+больш\w*", low):
        return "top_orders"
    if _TOPIC_TOP_RE.search(low):
        return "top_products"
    if _TOPIC_MONEY_RE.search(low):
        return "money"
    if _TOPIC_PRINT_RE.search(low):
        return "print"
    if _TOPIC_SPEND_RE.search(low):
        return "spend"
    if re.search(r"заказ\w*", low):
        return "orders"
    return ""


def _is_pure_period(text: str) -> bool:
    """«а за прошлую неделю?», «сколько за вчера» — период и только период."""
    period = periods.parse(text)
    if not period.explicit or not period.matched:
        return False
    low = _norm(text)
    if len(low) > 48:
        return False
    rest = low.replace(_norm(period.matched), " ", 1)
    rest = _PERIOD_FILLERS.sub(" ", rest)
    return not re.search(r"[а-яa-z0-9]", rest)


def _short_phrase(period: periods.Period) -> str:
    """Фраза окна без дат в скобках — для подсказок: «за неделю», а не
    «за неделю (20.09–26.09)». Даты нужны в ответе, в чипе-вопросе — лишние."""
    return re.sub(r"\s*[\(（][^)\]）]*[\)）]\s*$", "", period.phrase)


def _prev_period_question(period: periods.Period) -> str:
    """Следующий за окном вопрос тем же шагом: «А на прошлой неделе?».

    Каждая фраза понятна тому же парсеру периодов: клик по подсказке —
    не новый вопрос для человека и не пустота для мозга.
    """
    by_kind = {"today": "А вчера?", "yesterday": "А позавчера?", "day_before": "А вчера?",
               "week": "А на прошлой неделе?", "last_week": "А за 7 дней до этого?",
               "weekend": "А на прошлых выходных?", "last_weekend": "А за 2 дня до этого?",
               "month": "А в прошлом месяце?", "year": "А в прошлом году?"}
    if period.kind in by_kind:
        return by_kind[period.kind]
    if period.days == 7:  # «за неделю» парсер отдаёт спаном, а не календарной неделей
        return "А на прошлой неделе?"
    return f"А за {period.days} {plural(period.days, 'день', 'дня', 'дней')} до этого?"


def _money_suggestions(period: periods.Period) -> list[str]:
    phrase = _short_phrase(period)
    return [_prev_period_question(period), f"Топ товаров {phrase}?", f"Кто больше всего покупал {phrase}?"]


def _top_suggestions(subject: str, period: periods.Period, rank_by: str) -> list[str]:
    phrase = _short_phrase(period)
    if subject == "customers":
        return [f"Топ товаров {phrase}?", f"Какой доход {phrase}?"]
    alt = "по штукам" if rank_by == "amount" else "по сумме"
    return [f"А {alt}?", f"А кто больше всего покупал {phrase}?", f"Какой доход {phrase}?"]


def _print_suggestions(period: periods.Period) -> list[str]:
    return [_prev_period_question(period), f"Какой доход {_short_phrase(period)}?"]


def _spend_suggestions(period: periods.Period) -> list[str]:
    return [_prev_period_question(period), f"Какой доход {_short_phrase(period)}?"]


# Словарь владельца → категория расходов (та же, что в учётных проводках).
_SPEND_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("пластик|филамент|катушк|пластмасс", "filament", "пластик"),
    ("электричеств|энерг|свет", "energy", "электричество"),
    ("аренд", "rent", "аренду"),
    ("налог", "tax", "налоги"),
    ("комисс", "fee", "комиссии"),
)
_SPEND_CATEGORY_LABELS = {category: label for _pattern, category, label in _SPEND_CATEGORIES}
_SPEND_VERB_RE = re.compile(r"(потратил|затрат|расход|ушло|ушли|ушла|на\s+что)")
# «Сколько напечатали?», «сколько заданий/часов печати» — журнал заданий,
# но не «сколько часов до конца» (прогресс станка) и не «сколько пластика
# осталось» (катушки).
_PRINT_QUESTION_RE = re.compile(
    r"(напечатал\w*|сколько\s+печатали|сколько\s+(всего\s+)?(задани|часов\s+(печати|напечата)"
    r"|грамм\s+(напечата|выпечата)))")
_ORDERS_QUESTION_RE = re.compile(r"(сколько\s+(всего\s+)?заказов|заказов\s+(было|стало|нов))")


def _spend_category(low: str) -> str:
    """Категория расходов из фразы: «на пластик» → filament, «на аренду» → rent."""
    for pattern, category, _label in _SPEND_CATEGORIES:
        if re.search(pattern, low):
            return category
    return ""


# Каналы продаж: названия из справочника цеха + слова, которыми их называет
# владелец. «ВБ» ≠ «Wildberries» по буквам, а «озон» в базе латиницей.
_CHANNEL_ALIASES: tuple[tuple[str, str, str], ...] = (
    ("авито", "авито", ""),
    ("вб|вайлдберриз", "wildberr", ""),
    ("озон|маркетплейс", "ozon", ""),
    ("тг|телеграм", "telegram", ""),
    ("витрин", "витрин", ""),
    ("сарафан|напрямую", "напрямую", ""),
    ("b2b|счёт|счет", "b2b", ""),
    ("стеллаж", "стеллаж", "Стеллаж"),
    ("онлайн", "онлайн", "Онлайн"),
)


def _channel_names(ctx: Context) -> list[str]:
    """Каналы из справочника цеха — те, что видны в реестре продаж."""
    if ctx.db is None:
        return []
    try:
        return [str(row["name"]) for row in ctx.db.query(
            "SELECT name FROM channels ORDER BY position") if row.get("name")]
    except Exception:
        return []


def _channel_match(low: str, names: list[str]) -> str:
    """Канал из фразы: «через Авито», «с ВБ», «стеллаж». Пусто — канала нет."""
    for name in names:
        norm = _norm(name)
        if len(norm) >= 3 and norm in low:
            return name
    for word, stem, literal in _CHANNEL_ALIASES:
        if re.search(rf"(?<![а-яa-z0-9]){word}(?![а-яa-z0-9])", low):
            for name in names:
                if stem in _norm(name):
                    return name
            if literal:  # «Стеллаж»/«Онлайн» живут в реестре, не в справочнике
                return literal
    return ""


def _correction_tail(low: str) -> str:
    """«нет, за 2 недели», «не за неделю, а за месяц», «нет, топ клиентов».

    Человек поправляет: важна часть после «нет/не», а при «…, а …» — то,
    что после «а». Берём хвост, только если в нём читается явный период или
    тема; иначе оставляем фразу как есть («нет, спасибо» — не поправка окна).
    """
    head = re.match(r"^(?:нет|не|ладно|короче|ну)\s*[,.!?\s]+(.+)$", low)
    if not head:
        return low
    candidate = head.group(1).strip(" .,!?")
    if not candidate or candidate == low:
        return low
    last = re.split(r"\s+а\s+", candidate)[-1].strip(" .,!?")
    if last and last != candidate:
        if periods.parse(last).explicit or _topic_signal(last):
            return last
        return low
    if periods.parse(candidate).explicit or _topic_signal(candidate):
        return candidate
    return low


# ---------------------------------------------------------------------------
# Сущности цеха
# ---------------------------------------------------------------------------

def printers(ctx: Context) -> list[dict[str, Any]]:
    """Станки из снимка панели: имя, модель, состояние, прогресс, задание."""
    rows = []
    for snap in ctx.snapshot().get("printers") or []:
        if not isinstance(snap, dict):
            continue
        info = snap.get("printer") or {}
        job = snap.get("job") or {}
        order = job.get("order") or {}
        rows.append({"id": str(snap.get("id") or info.get("id") or ""),
                     "name": str(snap.get("name") or info.get("name") or snap.get("id") or "станок"),
                     "model": str(snap.get("model") or info.get("model") or ""),
                     "state": str(info.get("state") or "").upper(),
                     "progress": int(float(info.get("progress") or 0)),
                     "remaining_min": float(info.get("remaining_min") or 0),
                     "task": str(order.get("product") or info.get("task") or job.get("name") or "").strip(),
                     "online": info.get("online") is not False})  # None — «не знаю», не «отключён»
    return rows


def name_in(name: str, phrase: str) -> bool:
    """Имя станка во фразе с учётом падежа: «Альфа» находится в «что с Альфой»."""
    target = _norm(name)
    low = _norm(phrase)
    if not target:
        return False
    if re.search(rf"(?<![0-9a-zа-я]){re.escape(target)}(?![0-9a-zа-я])", low):
        return True
    wanted = [stem for stem in memory.stems(target) if len(stem) >= 3]
    if not wanted:
        return False
    have = set(memory.stems(low))
    return all(stem in have for stem in wanted)


def resolve_printer(ctx: Context, phrase: str, states: tuple[str, ...] = ()
                    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    """Станок из фразы: имя, модель, «второй», местоимение, единственный подходящий.

    Возвращает (станок, кандидаты, как понял). Нет станка и кандидатов больше
    одного — вызывающий спрашивает «какой?», а не выбирает сам.
    """
    rows = printers(ctx)
    if not rows:
        return None, [], "станков в панели нет"
    low = _norm(phrase)
    words = re.findall(r"[0-9a-zа-я]+", low)
    for row in rows:
        model = _norm(row["model"])
        if name_in(row["name"], low):
            return row, [], f"по имени «{row['name']}»"
        if model and re.search(rf"(?<![0-9a-z]){re.escape(model)}(?![0-9a-z])", low):
            same_model = [item for item in rows if _norm(item["model"]) == model]
            if len(same_model) == 1:
                return row, [], f"по модели {row['model']}"
            return None, same_model, f"модель {row['model']} у нескольких станков"
    for word in words:
        if word in _ORDINALS and re.search(r"(станок|станк|принтер|у\s|на\s|второ|перв|трет|последн)", low):
            index = _ORDINALS[word]
            if -len(rows) <= index < len(rows):
                return rows[index], [], f"по порядку ({word})"
    last = ctx.entities().get("printer") or {}
    if last and (any(pronoun in words for pronoun in _PRONOUNS) or not re.search(r"(станок|станк|принтер|все)", low)):
        match = next((row for row in rows if row["id"] == last.get("id")), None)
        if match:
            return match, [], "из прошлой реплики"
    pool = [row for row in rows if not states or row["state"] in states]
    if len(pool) == 1:
        return pool[0], [], "единственный подходящий"
    if len(rows) == 1:
        return rows[0], [], "единственный станок"
    return None, pool or rows, "подходит несколько станков"


def _order_rows(ctx: Context, search: str, limit: int = 8) -> list[dict[str, Any]]:
    repo = getattr(ctx.api, "repo", None)
    if repo is None:
        return []
    try:
        return list(repo.orders(search=search, limit=limit) or [])
    except Exception as exc:
        log().warning("Мозг помощника: заказы не прочитались (%s)", exc)
        return []


def resolve_order(ctx: Context, phrase: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    """Заказ из фразы: номер, имя клиента, местоимение из контекста."""
    low = _norm(phrase)
    number = re.search(r"(?:заказ\w*|№|номер)\s*№?\s*(\d{2,7})|(?<!\d)(\d{4,7})(?!\d)", low)
    if number:
        value = number.group(1) or number.group(2)
        rows = [row for row in _order_rows(ctx, value, 20) if str(row.get("number") or "") == value]
        if rows:
            return rows[0], [], f"по номеру №{value}"
        return None, [], f"заказа №{value} нет"
    last = ctx.entities().get("order") or {}
    words = re.findall(r"[0-9a-zа-я]+", low)
    if last and any(pronoun in words for pronoun in _PRONOUNS):
        rows = [row for row in _order_rows(ctx, str(last.get("number") or ""), 10)
                if str(row.get("number") or "") == str(last.get("number") or "")]
        if rows:
            return rows[0], [], "из прошлой реплики"
    name = customer_hint(phrase)
    if name:
        rows = [row for row in _order_rows(ctx, name[:5], 30)
                if row.get("status") != "done" and name[:4] in _norm(row.get("customer_name"))]
        if len(rows) == 1:
            return rows[0], [], f"по клиенту «{rows[0].get('customer_name')}»"
        if rows:
            return None, rows[:5], f"у клиента «{name}» несколько заказов"
    return None, [], ""


def customer_hint(phrase: str) -> str:
    """Основа имени клиента из фразы («заказ Марии» → «мари»), если оно там есть."""
    match = re.search(r"(?:клиент\w*|заказ\w*|для|у|с)\s+([А-ЯЁ][а-яё]{2,})", str(phrase or ""))
    if not match:
        return ""
    stems = memory.stems(match.group(1))
    return stems[0] if stems else ""


def order_line(order: dict[str, Any]) -> str:
    status = ORDER_STATUS_TITLES.get(str(order.get("status") or ""), str(order.get("status") or "—"))
    parts = [f"Заказ №{order.get('number') or '—'}", str(order.get("product") or "без названия"),
             f"статус «{status}»"]
    if order.get("due"):
        parts.append(f"срок {str(order['due'])[:10]}")
    if order.get("price") not in (None, ""):
        parts.append(_money(order.get("price")))
    if order.get("customer_name"):
        parts.append(str(order["customer_name"]))
    debt = order.get("debt") if order.get("debt") is not None else None
    if debt not in (None, "", 0, 0.0, "0"):
        parts.append(f"долг {_money(debt)}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Ответы
# ---------------------------------------------------------------------------

def _answer(ctx: Context, text: str, reply: str, *, kind: str = "answer", source: str = "rules",
            action: dict[str, Any] | None = None, params: dict[str, Any] | None = None,
            explain: str = "", warnings: list[str] | None = None, facts: list[dict[str, Any]] | None = None,
            link: dict[str, str] | None = None, suggestions: list[str] | None = None,
            entities: dict[str, Any] | None = None, awaiting: dict[str, Any] | None = None,
            extra: dict[str, Any] | None = None, save: bool = True) -> dict[str, Any]:
    """Единая форма ответа мозга + запись реплик в разговор."""
    payload: dict[str, Any] = {
        "ok": kind != "error", "session": ctx.session, "reply": reply, "answer": reply, "kind": kind,
        "source": source, "steps": ctx.steps, "action": action, "params": params or {},
        "explain": explain, "warnings": warnings or [], "facts": facts or [], "link": link,
        "suggestions": suggestions or [], "entities": entities or {}, "awaiting": bool(awaiting),
        "ms": int((time.time() - ctx.started) * 1000)}
    if extra:
        payload.update(extra)
    if save and ctx.db is not None and text:
        try:
            memory.add_turn(ctx.db, ctx.session, "user", text, {"source": ctx.source})
            # Помним, о чём шла речь: «а за прошлую неделю?» после «Какой
            # доход?» не начинается с нуля — слой `_followup` читает эти ключи
            # из мета прошлой реплики.
            remembered = {key: (extra or {}).get(key) for key in _REMEMBER_META
                          if (extra or {}).get(key) not in (None, "", {}, [])}
            memory.add_turn(ctx.db, ctx.session, "assistant", reply, {
                "kind": kind, "source": source, "entities": entities or {},
                "action": (action or {}).get("id"), "params": params or {},
                "awaiting": awaiting or {}, **remembered})
        except Exception as exc:
            log().warning("Мозг помощника: разговор не записан (%s)", exc)
    return payload


def _action(action_id: str) -> dict[str, Any]:
    spec = assistant.ACTIONS[action_id]
    return {"id": action_id, "title": spec["title"], "method": spec["method"], "path": spec["path"],
            "confirm": bool(spec["confirm"]), "doc": spec["doc"]}


# ---------------------------------------------------------------------------
# 1. Уточнение
# ---------------------------------------------------------------------------

def _clarification(ctx: Context, text: str) -> dict[str, Any] | None:
    awaiting = ctx.last_meta().get("awaiting") or {}
    if not awaiting or awaiting.get("slot") != "printer_id":
        return None
    options = awaiting.get("options") or []
    low = _norm(text)
    if len(low) > 60:
        return None
    chosen = None
    for option in options:
        if _norm(option.get("name")) and (_norm(option.get("name")) in low or low in _norm(option.get("name"))):
            chosen = option
            break
        if option.get("model") and _norm(option["model"]) == low:
            chosen = option
            break
    if chosen is None:
        for word in re.findall(r"[0-9a-zа-я]+", low):
            if word in _ORDINALS and -len(options) <= _ORDINALS[word] < len(options):
                chosen = options[_ORDINALS[word]]
                break
    if chosen is None:
        return None
    ctx.step("context", "Ответ на уточнение", f"выбран {chosen.get('name')}")
    params = dict(awaiting.get("params") or {})
    params["printer_id"] = chosen["id"]
    return _printer_proposal(ctx, text, params.get("command", ""), chosen)


# ---------------------------------------------------------------------------
# 2. Память
# ---------------------------------------------------------------------------

def _memory(ctx: Context, text: str) -> dict[str, Any] | None:
    op, payload = memory.memory_command(text)
    if not op or ctx.db is None:
        return None
    ctx.step("memory", "Память", op)
    db = ctx.db
    if op == "remember":
        saved = memory.remember(db, payload, source=ctx.source)
        if saved.get("duplicate"):
            return _answer(ctx, text, f"Это я уже помню: «{saved['memory']['text']}».", kind="memory", source="memory")
        return _answer(ctx, text, f"Запомнил: «{saved['memory']['text']}». Буду учитывать в ответах.",
                       kind="memory", source="memory", suggestions=["Что ты помнишь?"])
    if op == "forget":
        rows = memory.forget(db, payload)
        if not rows:
            return _answer(ctx, text, f"В памяти нет записи про «{payload}».", kind="memory", source="memory",
                           suggestions=["Что ты помнишь?"])
        return _answer(ctx, text, "Забыл: " + "; ".join(f"«{row['text']}»" for row in rows) + ".",
                       kind="memory", source="memory")
    if op == "name":
        memory.remember(db, f"Владельца зовут {payload}", kind="profile", subject="имя", source=ctx.source)
        return _answer(ctx, text, f"Приятно познакомиться, {payload}! Запомнил.", kind="memory", source="memory")
    if op == "whoami":
        name = memory.owner_name(db)
        return _answer(ctx, text, f"Вас зовут {name}." if name else "Пока не знаю. Скажите: «меня зовут …».",
                       kind="memory", source="memory")
    if payload and len(payload.split()) <= 3:
        card = customer_card(ctx, text, payload.split()[-1], record_required=True)
        if card:  # «что ты помнишь про Ивана» — клиент из базы: карточка вместе с памятью
            return card
    rows = memory.recall(db, payload, 8) if payload else memory.memories(db, 12)
    if not rows:
        return _answer(ctx, text, "Про это в памяти ничего нет." if payload
                       else "Память пока пуста. Скажите «запомни, что …» — и я буду это учитывать.",
                       kind="memory", source="memory")
    head = f"Про «{payload}» помню:" if payload else "Вот что я помню:"
    return _answer(ctx, text, head + "\n" + "\n".join(f"• {row['text']}" for row in rows[:8]),
                   kind="memory", source="memory", extra={"memory": rows[:8]})


# ---------------------------------------------------------------------------
# 3. Мгновенные ответы
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {"ноль": "0", "один": "1", "одну": "1", "два": "2", "две": "2", "три": "3", "четыре": "4",
                 "пять": "5", "шесть": "6", "семь": "7", "восемь": "8", "девять": "9", "десять": "10",
                 "двадцать": "20", "сто": "100", "тысячу": "1000", "тысяча": "1000", "половину": "0.5"}
_NUM = r"(\d+(?:[.,]\d+)?|" + "|".join(_NUMBER_WORDS) + r")"
_MATH_FOLLOW: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"(?:подел\w*|раздел\w*|дели\w*)\s+(?:это\s+|его\s+|результат\s+)?(?:на\s+)?" + _NUM + r"\b"), "/", "÷"),
    (re.compile(r"(?:умнож\w*|помнож\w*)\s+(?:это\s+|его\s+|результат\s+)?(?:на\s+)?" + _NUM + r"\b"), "*", "×"),
    (re.compile(r"(?:прибав\w*|добав\w*|плюс|приплюсу\w*)\s+(?:к\s+этому\s+|ещ[её]\s+)?" + _NUM + r"\b"), "+", "+"),
    (re.compile(r"(?:отним\w*|отнять|вычт\w*|вычесть|минус|убав\w*)\s+(?:от\s+этого\s+|ещ[её]\s+)?" + _NUM + r"\b"), "-", "−"),
)
_PERCENT_RE = re.compile(_NUM + r"\s*(?:%|процент\w*)")


def _math_follow_up(ctx: Context, text: str) -> dict[str, Any] | None:
    """«А если поделить на 5?» после «17*23+4»: считаем от прошлого результата."""
    previous = str((ctx.last_meta().get("entities") or {}).get("number") or "")
    low = _norm(text).rstrip("?!. ")
    if not previous or len(low) > 60:
        return None
    pretty = previous.replace(".", ",")
    expression = shown = ""
    divides_by_zero = False
    if re.search(r"в\s+квадрат", low):
        expression, shown = f"{previous}*{previous}", f"{pretty}²"
    percent = _PERCENT_RE.search(low)
    if not expression and percent and re.search(r"(от\s+(этого|него|результата|числа)|^а\s|^\d|^сколько)", low):
        number = _NUMBER_WORDS.get(percent.group(1), percent.group(1)).replace(",", ".")
        expression, shown = f"{previous}*{number}/100", f"{number.replace('.', ',')}% от {pretty}"
    for pattern, operator, sign in _MATH_FOLLOW:
        if expression:
            break
        found = pattern.search(low)
        if found:
            number = _NUMBER_WORDS.get(found.group(1), found.group(1)).replace(",", ".")
            divides_by_zero = operator == "/" and float(number) == 0
            expression = f"({previous}){operator}{number}"
            shown = f"{pretty} {sign} {number.replace('.', ',')}"
    if not expression:
        return None
    if divides_by_zero:
        return _answer(ctx, text, "На ноль делить нельзя.", kind="clarify", source="math",
                       entities={"number": previous})
    result = assistant.simple_math(expression)
    if not result:
        return None
    ctx.step("context", "Продолжение расчёта", f"от прошлого результата {previous}")
    return _answer(ctx, text, f"{shown} = {result}", source="math",
                   entities={"number": result.replace(",", ".")})


def _instant(ctx: Context, text: str) -> dict[str, Any] | None:
    low = _norm(text)
    math = assistant.simple_math(text)
    if math:
        ctx.step("rule", "Арифметика", "без модели")
        return _answer(ctx, text, math, source="math", entities={"number": math.replace(",", ".")})
    follow = _math_follow_up(ctx, text)
    if follow:
        return follow
    if re.fullmatch(r"(а\s+)?(ты\s+)?кто\s+ты|ты\s+кто|расскажи\s+(о|про)\s+себ[ея]|как\s+тебя\s+зовут|представься", low.rstrip("?!. ")):
        ctx.step("rule", "Кто я", "без модели")
        return _answer(ctx, text, "Я NOZZA — помощник цеха PrintFlow. Вижу станки, заказы, клиентов и деньги в "
                       "вашей базе, помню то, что вы просите запомнить, предлагаю действия — а выполняете вы "
                       "кнопкой «Подтвердить».\n" + capabilities_text(ctx).split("\n", 1)[-1], source="registry",
                       suggestions=suggestions_for(ctx))
    if re.fullmatch(r"(а\s+)?(ну\s+)?как\s+(у\s+тебя\s+)?(дела|ты|жизнь|сам|поживаешь|оно)", low.rstrip("?!. ")):
        ctx.step("rule", "Как дела", "сводка цеха")
        return _answer(ctx, text, "Работаю, всё под контролем. " + farm_phrase(ctx), source="farm",
                       entities=_single_busy(ctx), suggestions=suggestions_for(ctx))
    if re.search(r"(который|сколько)\s+(сейчас\s+)?(час|времени)|^время\??$", low):
        ctx.step("rule", "Часы", "без модели")
        return _answer(ctx, text, f"Сейчас {datetime.datetime.now():%H:%M}.", source="clock")
    if knowledge._is_date_question(text):
        ctx.step("rule", "Часы", "без модели")
        return _answer(ctx, text, assistant.date_line(), source="clock")
    if re.search(r"что ты умеешь|что умеешь|чем (ты )?можешь помочь|^помощь$|твои возможности", low):
        ctx.step("rule", "Возможности", "каталог панели и агент")
        return _answer(ctx, text, capabilities_text(ctx), source="registry",
                       suggestions=["Что сейчас печатается?", "Кто должен денег?", "Как там компьютер?"])
    if re.fullmatch(r"(привет|здравствуй\w*|добрый (день|вечер)|доброе утро|хай|здорово|салют)[!.]*", low):
        ctx.step("rule", "Приветствие", "сводка цеха")
        name = memory.owner_name(ctx.db) if ctx.db is not None else ""
        return _answer(ctx, text, f"Привет{', ' + name if name else ''}! " + farm_phrase(ctx),
                       source="farm", entities=_single_busy(ctx),
                       suggestions=["Что сейчас печатается?", "Брифинг на сегодня", "Что ты умеешь?"])
    if re.fullmatch(r"(спасибо|благодарю|спс|отлично|супер|класс)[!.]*", low):
        return _answer(ctx, text, "Обращайтесь! Если что-то нужно запомнить — скажите «запомни, что …».", source="rules")
    return None


def capabilities_text(ctx: Context) -> str:
    agent = assistant.agent_config(ctx.db) if ctx.db is not None else {"enabled": False}
    lines = ["Я помощник цеха. Умею:",
             "• отвечать по базе: что печатается, заказы, клиенты, долги, деньги, план, склад;",
             "• предлагать действия — пауза или стоп станка, статус и выдача заказа, запуск задания; "
             "выполняете вы кнопкой «Подтвердить»;",
             "• помнить: «запомни, что…», «что ты помнишь про Марию»;",
             "• понимать продолжение: «а у второго?», «поставь его на паузу», «сколько ему осталось?», "
             "«а если поделить на 5?»;",
             "• отвечать на общие вопросы моделью Ollama, свежее — через её веб-поиск."]
    if agent.get("enabled"):
        lines.append("• управлять компьютером через агента: окна, звук, программы, файлы, экран.")
    else:
        lines.append("• компьютер (окна, звук, программы) — после включения агента в настройках.")
    return "\n".join(lines)


def farm_phrase(ctx: Context) -> str:
    rows = printers(ctx)
    if not rows:
        return "Станки пока не подключены — начните с раздела «Принтеры»."
    busy = [row for row in rows if row["state"] in ("RUNNING", "PREPARE")]
    paused = [row for row in rows if row["state"] == "PAUSE"]
    queue = len(ctx.snapshot().get("queue") or [])
    if len(rows) == 1:
        only = rows[0]
        state = "печатает" if busy else ("на паузе" if paused else "свободен")
        parts = [f"{only['name']} {state}" + (f" «{only['task']}», {only['progress']}%" if (busy or paused) and only["task"] else "")]
    else:
        parts = [f"Печата{'ет' if len(busy) == 1 else 'ют'} {len(busy)} из {len(rows)}" if busy
                 else f"Все {len(rows)} {plural(len(rows), 'станок', 'станка', 'станков')} свободны"]
        if paused:
            parts.append("на паузе: " + ", ".join(row["name"] for row in paused[:3]))
    if queue:
        parts.append(f"в очереди {queue}")
    return ", ".join(parts) + "."


def _single_busy(ctx: Context) -> dict[str, Any]:
    """Единственный занятый станок становится «им» для следующей реплики."""
    busy = [row for row in printers(ctx) if row["state"] in ("RUNNING", "PAUSE", "PREPARE")]
    if len(busy) == 1:
        return {"printer": {"id": busy[0]["id"], "name": busy[0]["name"]}}
    return {}


def suggestions_for(ctx: Context) -> list[str]:
    """Подсказки по ситуации в цехе, а не один и тот же набор кнопок."""
    rows = printers(ctx)
    tips: list[str] = []
    if any(row["state"] == "PAUSE" for row in rows):
        tips.append("Продолжи печать")
    if any(row["state"] in ("RUNNING", "PREPARE") for row in rows):
        tips.append("Сколько осталось печатать?")
    try:
        if knowledge._num((ctx.api.acc.debts() or {}).get("total")):
            tips.append("Кто должен денег?")
    except Exception:
        pass
    tips += ["Брифинг на сегодня", "Что ты умеешь?"]
    return tips[:4]


def _printer_line(row: dict[str, Any], *, eta: bool = True) -> str:
    state = {"RUNNING": "печатает", "PAUSE": "на паузе", "PREPARE": "готовится", "FINISH": "закончил",
             "FAILED": "ошибка", "IDLE": "свободен"}.get(row["state"], row["state"].lower() or "нет данных")
    line = f"{row['name']} — {state}"
    if row["state"] in ("RUNNING", "PAUSE", "PREPARE"):
        if row["task"]:
            line += f" «{row['task']}»"
        line += f", {row['progress']}%"
        if row["remaining_min"] >= 1:
            line += f", осталось ~{knowledge._minutes_label(row['remaining_min'])}"
            if eta and row["state"] in ("RUNNING", "PREPARE"):
                line += f" (закончит {eta_label(row['remaining_min'])})"
    elif not row.get("online", True):
        line += ", не на связи"
    return line


_FARM_RE = re.compile(
    r"(как\s+(там\s+)?(у\s+нас\s+)?(дела|обстановка|ситуация|успехи|оно)\s+(на\s+ферм|в\s+цех|с\s+принтер|со\s+станк|"
    r"с\s+печать|на\s+производств|с\s+парк)"
    r"|что\s+(там\s+)?(у\s+нас\s+)?(на\s+ферм|в\s+цех|с\s+принтерам|со\s+станкам|с\s+ферм|с\s+парк|на\s+производств)"
    r"|статус\w*\s+(ферм|парк|станк|принтер|цех)|состояни\w*\s+(ферм|парк|станк|принтер|цех)"
    r"|как\s+(там\s+)?(ферма|станки|принтеры|парк|цех|производство)\b|что\s+(там\s+)?происходит|обстановк)")


def _farm_overview(ctx: Context, text: str) -> dict[str, Any] | None:
    """«Как дела на ферме?» — каждый станок одной строкой, с временем окончания."""
    low = _norm(text)
    if not _FARM_RE.search(low):
        return None
    rows = printers(ctx)
    ctx.step("rule", "Обзор парка", f"станков {len(rows)}")
    if not rows:
        return _answer(ctx, text, farm_phrase(ctx), source="farm", link={"title": "Принтеры", "href": "/#printers"})
    lines = [farm_phrase(ctx)] if len(rows) > 1 else []
    lines += [("• " if len(rows) > 1 else "") + _printer_line(row) + "." for row in rows[:8]]
    queue = ctx.snapshot().get("queue") or []
    if queue:
        names = [str(item.get("name") or item.get("title") or "") for item in queue[:3] if isinstance(item, dict)]
        names = [name for name in names if name]
        if names:
            lines.append("Следующим в очереди: " + ", ".join(f"«{name}»" for name in names) + ".")
    return _answer(ctx, text, "\n".join(lines), source="farm", entities=_single_busy(ctx),
                   link={"title": "Принтеры", "href": "/#printers"}, suggestions=suggestions_for(ctx))


# ---------------------------------------------------------------------------
# 4. Цех без модели
# ---------------------------------------------------------------------------

_STATE_WORDS = {"RUNNING": "печатает", "PAUSE": "на паузе", "PREPARE": "готовится", "FINISH": "закончил печать",
                "FAILED": "в ошибке", "IDLE": "свободен", "OFFLINE": "не на связи"}
_PAUSE_RE = re.compile(r"(поставь\s+(?:\S+\s+)?на\s+паузу|приостанови|пауз[ау]\b|на паузу)", re.IGNORECASE)
_RESUME_RE = re.compile(r"(продолж\w*|возобнов\w*|сними\s+(?:\S+\s+)?с\s+паузы|сними с паузы)", re.IGNORECASE)
_STOP_RE = re.compile(r"(останови\w*|прерви|отмени\s+печать|стоп\b|заверши\s+печать)", re.IGNORECASE)
_PRINT_CTX_RE = re.compile(r"(станок|станк|принтер|печат|p1s|p2s|x1|a1|bambu|задани)", re.IGNORECASE)


def _printer_command(ctx: Context, text: str) -> dict[str, Any] | None:
    low = _norm(text)
    command = ""
    if _RESUME_RE.search(low):
        command = "resume"
    elif _PAUSE_RE.search(low):
        command = "pause"
    elif _STOP_RE.search(low) and _PRINT_CTX_RE.search(low):
        command = "stop"
    if not command:
        return None
    if re.search(r"(музык|видео|трек|песн|плеер|ютуб|фильм|сериал)", low):
        return None  # это медиа компьютера — решит агент
    last = ctx.entities().get("printer")
    words = re.findall(r"[0-9a-zа-я]+", low)
    states = {"pause": ("RUNNING", "PREPARE"), "resume": ("PAUSE",), "stop": ("RUNNING", "PAUSE", "PREPARE")}[command]
    explicit = (_PRINT_CTX_RE.search(low) or (last and any(p in words for p in _PRONOUNS))
                or any(name_in(row["name"], low) for row in printers(ctx)))
    nothing = {"pause": "ставить на паузу нечего", "resume": "продолжать нечего", "stop": "останавливать нечего"}[command]
    if not explicit and not any(row["state"] in states for row in printers(ctx)):
        # «Пауза» без станка, и ни один станок сейчас не подходит — вероятно,
        # это про музыку на компьютере: пусть решает агент. Агент выключен —
        # говорим прямо, а не «не понял».
        agent_on = bool(assistant.agent_config(ctx.db).get("enabled")) if ctx.db is not None else False
        if agent_on or not printers(ctx):
            return None
        ctx.step("entity", "Станки", "ни один не подходит, агент выключен")
        return _answer(ctx, text, f"Станки сейчас не печатают — {nothing}. Если это про музыку или видео на "
                       "компьютере — включите агента компьютера в настройках помощника.", kind="clarify",
                       source="entity", link={"title": "Настройки", "href": "/#settings"},
                       suggestions=suggestions_for(ctx))
    printer, candidates, how = resolve_printer(ctx, text, states)
    ctx.step("entity", "Станок", how)
    if printer is not None and printer["state"] and printer["state"] not in states:
        state = _STATE_WORDS.get(printer["state"], printer["state"].lower())
        return _answer(ctx, text, f"{printer['name']} сейчас {state} — {nothing}.", source="entity",
                       entities={"printer": {"id": printer["id"], "name": printer["name"]}},
                       suggestions=suggestions_for(ctx))
    if printer is None:
        if not candidates:
            return _answer(ctx, text, "Станков для этой команды не нашёл: " + how + ".", kind="error", source="entity")
        options = [{"id": row["id"], "name": row["name"], "model": row["model"]} for row in candidates[:6]]
        verb = {"pause": "поставить на паузу", "resume": "продолжить", "stop": "остановить"}[command]
        listing = ", ".join(f"{index + 1}) {row['name']}" + (f" ({row['model']})" if row["model"] else "")
                            for index, row in enumerate(candidates[:6]))
        return _answer(ctx, text, f"Какой станок {verb}? {listing}.", kind="clarify", source="entity",
                       awaiting={"slot": "printer_id", "options": options, "params": {"command": command}},
                       suggestions=[row["name"] for row in candidates[:4]])
    return _printer_proposal(ctx, text, command, printer)


def _printer_proposal(ctx: Context, text: str, command: str, printer: dict[str, Any]) -> dict[str, Any]:
    verb = {"pause": "Поставить на паузу", "resume": "Продолжить печать", "stop": "Остановить печать"}.get(command, command)
    warnings = []
    state = str(printer.get("state") or "")
    if command == "pause" and state and state not in ("RUNNING", "PREPARE"):
        warnings.append(f"Станок сейчас не печатает (состояние {state or '—'})")
    if command == "resume" and state and state != "PAUSE":
        warnings.append(f"Станок не на паузе (состояние {state or '—'})")
    if command == "stop":
        warnings.append("Остановка прерывает печать — продолжить это задание будет нельзя")
    task = f" — «{printer.get('task')}»" if printer.get("task") else ""
    reply = f"{verb}: {printer.get('name')}{task}. Подтвердите в карточке."
    return _answer(ctx, text, reply, kind="action", source="entity", action=_action("printer_command"),
                   params={"printer_id": printer["id"], "command": command},
                   explain=f"{verb} на станке {printer.get('name')}", warnings=warnings,
                   entities={"printer": {"id": printer["id"], "name": printer.get("name")}})


_REMAIN_RE = re.compile(
    r"(сколько\s+(\S+\s+){0,2}осталось|когда\s+(\S+\s+){0,2}(законч|допечата|освобод|будет\s+готов|кончит|доделает)"
    r"|какой\s+прогресс|прогресс\s+печат|на\s+скольк\w+\s+процент|сколько\s+процент|долго\s+(ещ[её]\s+)?(печатать|осталось))")
_REMAIN_NOT_RE = re.compile(r"(пластик|катушк|филамент|грамм|денег|деньг|заказ|дн[еяй]\b|срок|склад|мест[оа]|рабоч)")
_OBJECT_PRONOUNS = ("ним", "ней", "нем", "него", "нее", "неё", "он", "она", "ему", "его")


def _printer_remaining(ctx: Context, text: str) -> dict[str, Any] | None:
    """«Сколько ему осталось?», «когда закончит?» — прогресс и время окончания по часам."""
    rows = printers(ctx)
    if not rows:
        return None
    working = ("RUNNING", "PAUSE", "PREPARE")
    printer, candidates, how = resolve_printer(ctx, text, working)
    if printer is None:
        busy = [row for row in candidates if row["state"] in working]
        if not busy:
            return _answer(ctx, text, "Сейчас ничего не печатается. " + farm_phrase(ctx), source="farm",
                           suggestions=suggestions_for(ctx))
        ctx.step("entity", "Станки", f"занято {len(busy)} — отвечаю про все")
        return _answer(ctx, text, "\n".join("• " + _printer_line(row) + "." for row in busy[:8]), source="farm",
                       suggestions=suggestions_for(ctx))
    ctx.step("entity", "Станок", how)
    entities = {"printer": {"id": printer["id"], "name": printer["name"]}}
    if printer["state"] not in working:
        state = _STATE_WORDS.get(printer["state"], printer["state"].lower() or "без данных")
        return _answer(ctx, text, f"{printer['name']} сейчас ничего не печатает ({state}).", source="farm",
                       entities=entities, suggestions=suggestions_for(ctx))
    task = f" «{printer['task']}»" if printer["task"] else ""
    left = knowledge._minutes_label(printer["remaining_min"]) if printer["remaining_min"] >= 1 else ""
    if printer["state"] == "PAUSE":
        reply = f"{printer['name']} на паузе{task} на {printer['progress']}%" + (
            f" — после продолжения останется ~{left}." if left else ".")
        tips = ["Продолжи печать"]
    else:
        reply = f"{printer['name']}{task}: {printer['progress']}%" + (
            f", осталось ~{left}, закончит {eta_label(printer['remaining_min'])}." if left else ", почти готово.")
        tips = ["Поставь его на паузу", "Как дела на ферме?"]
    return _answer(ctx, text, reply, source="farm", entities=entities, suggestions=tips)


def _printer_status(ctx: Context, text: str) -> dict[str, Any] | None:
    low = _norm(text)
    if _REMAIN_RE.search(low) and not _REMAIN_NOT_RE.search(low):
        return _printer_remaining(ctx, text)
    follow = re.fullmatch(r"(а\s+)?(у|на|что\s+(у|на))\s+(\S+)(\s+станк\w*)?\??", low)
    asks = re.search(r"(что|как)\s+(там\s+)?(с|у|на)\s+", low) or follow
    words = re.findall(r"[0-9a-zа-я]+", low)
    about_him = (bool(ctx.entities().get("printer")) and any(word in _OBJECT_PRONOUNS for word in words)
                 and bool(re.search(r"(что|как)\s+(там\s+)?(с\s+ним|с\s+ней|у\s+него|у\s+нее|он|она)\b|что\s+(он|она)\s+печата", low)))
    if not asks and not about_him:
        return None
    named = any(name_in(row["name"], low) or (row["model"] and _norm(row["model"]) in low.split())
                for row in printers(ctx))
    ordinal = any(word in _ORDINALS for word in words)
    if not named and not about_him and not (ordinal and (follow or re.search(r"станк|принтер", low))):
        return None
    printer, _candidates, how = resolve_printer(ctx, text)
    if printer is None:
        return None
    ctx.step("entity", "Станок", how)
    state = {"RUNNING": "печатает", "PAUSE": "на паузе", "PREPARE": "готовится", "FINISH": "закончил",
             "FAILED": "ошибка", "IDLE": "свободен"}.get(printer["state"], printer["state"].lower() or "нет данных")
    line = f"{printer['name']}: {state}"
    if printer["task"] and printer["state"] in ("RUNNING", "PAUSE", "PREPARE"):
        line += f" «{printer['task']}», {printer['progress']}%"
        if printer["remaining_min"] >= 5 and printer["state"] == "RUNNING":
            line += (f", осталось ~{knowledge._minutes_label(printer['remaining_min'])}"
                     f" (закончит {eta_label(printer['remaining_min'])})")
    suggestions = {"RUNNING": ["Поставь его на паузу"], "PAUSE": ["Продолжи печать на нём"]}.get(printer["state"], [])
    return _answer(ctx, text, line + ".", source="farm", entities={"printer": {"id": printer["id"], "name": printer["name"]}},
                   suggestions=suggestions + ["А у второго?"] if len(printers(ctx)) > 1 else suggestions)


def _order_intents(ctx: Context, text: str) -> dict[str, Any] | None:
    low = _norm(text)
    mentions = re.search(r"заказ|№\s*\d|\bего\b|\bеё\b|\bее\b", low)
    if not mentions:
        return None
    fulfill = re.search(r"\b(выдай|выдать|отдай|отдать|закрой|закрыть)\b", low)
    status_change = re.search(r"(переведи|поставь|смени\s+статус|отметь|перенеси)\b.*\b(в|на|как)\s+(\w+)", low)
    lookup = re.search(r"(что|как|где)\s+(там\s+)?(с|по)?\s*заказ|статус\w*\s+заказ|заказ\w*\s*№?\s*\d|покажи\s+заказ", low)
    if not (fulfill or status_change or lookup):
        return None
    order, candidates, how = resolve_order(ctx, text)
    if order is None:
        if candidates:
            listing = "; ".join(order_line(row) for row in candidates[:4])
            return _answer(ctx, text, f"Нашёл несколько: {listing}. Назовите номер.", kind="clarify", source="entity")
        if how:
            return _answer(ctx, text, how[:1].upper() + how[1:] + ".", kind="error", source="entity",
                           link={"title": "Заказы", "href": "/#orders"})
        return None
    ctx.step("entity", "Заказ", how)
    entities = {"order": {"id": order.get("id"), "number": order.get("number")}}
    if order.get("customer_name"):
        entities["customer"] = {"name": order.get("customer_name")}
    if fulfill:
        warnings = []
        if order.get("status") != "ready":
            warnings.append(f"Заказ не в статусе «готов» (сейчас «{ORDER_STATUS_TITLES.get(order.get('status'), order.get('status'))}»)")
        return _answer(ctx, text, f"Выдать заказ №{order.get('number')} — {order.get('product') or 'без названия'}. "
                       "Оплату и способ выдачи уточните в карточке.", kind="action", source="entity",
                       action=_action("order_fulfill"), params={"id": order.get("id")},
                       explain=f"Выдача заказа №{order.get('number')}", warnings=warnings, entities=entities)
    if status_change:
        word = status_change.group(3)
        status = next((code for stem, code in ORDER_STATUS_WORDS.items() if word.startswith(stem)), "")
        if not status:
            return _answer(ctx, text, "Такого статуса нет. Есть: " + ", ".join(ORDER_STATUS_TITLES.values()) + ".",
                           kind="clarify", source="entity", entities=entities)
        return _answer(ctx, text, f"Перевести заказ №{order.get('number')} в «{ORDER_STATUS_TITLES[status]}».",
                       kind="action", source="entity", action=_action("order_status"),
                       params={"id": order.get("id"), "status": status},
                       explain=f"Статус заказа №{order.get('number')} → {ORDER_STATUS_TITLES[status]}", entities=entities)
    suggestions = ["Выдай его"] if order.get("status") == "ready" else ["Переведи его в готово"]
    return _answer(ctx, text, order_line(order) + ".", source="entity", entities=entities,
                   facts=[knowledge._fact("заказ", f"Заказ №{order.get('number')}", order_line(order), f"order:{order.get('id')}")],
                   link={"title": "Открыть заказы", "href": "/#orders"}, suggestions=suggestions)


_CUSTOMER_RE = re.compile(
    r"(?:что|как)\s+(?:там\s+)?(?:с|у|по)\s+(?:клиент\w*\s+)?(?P<a>[А-ЯЁ][а-яё]{2,})"
    r"|клиент\w*\s+(?P<b>[А-ЯЁа-яё][а-яё]{2,})"
    r"|(?:расскажи|напомни|покажи)\s+(?:мне\s+)?(?:про|о|об|по)?\s*(?:клиент\w*\s+)?(?P<c>[А-ЯЁа-яё][а-яё]{2,})"
    r"|(?:что\s+(?:ты\s+)?(?:знаешь|известно)|кто\s+так(?:ой|ая))\s+(?:про|о|об)?\s*(?:клиент\w*\s+)?(?P<d>[А-ЯЁа-яё][а-яё]{2,})")
_NOT_NAMES = {"него", "нее", "неё", "них", "это", "этом", "этот", "все", "всё", "всех", "себя", "себе", "меня", "нас",
              "вас", "тебя", "погоду", "погода", "новости", "сегодня", "завтра", "план", "день", "деньги", "долги",
              "склад", "печать", "станки", "принтеры", "очередь", "финансы", "клиентов", "клиентах"}


def _name_match(stem: str, full_name: Any) -> bool:
    """Основа имени совпадает с началом слова в карточке: «иван» → «Иван (демо)», но «ол» ≠ «Николай»."""
    stem = stem[:4]
    if len(stem) < 3:
        return False
    return any(word.startswith(stem) for word in re.findall(r"[0-9a-zа-я]+", _norm(full_name)))


def _customer(ctx: Context, text: str) -> dict[str, Any] | None:
    low = _norm(text)
    match = _CUSTOMER_RE.search(str(text or ""))
    if not match or "заказ" in low:
        return None
    raw = next(group for group in match.groups() if group)
    return customer_card(ctx, text, raw, record_required=not raw[:1].isupper())


def customer_card(ctx: Context, text: str, raw: str, record_required: bool = False) -> dict[str, Any] | None:
    """Карточка клиента: контакты, заказы и то, что о нём помнит помощник.

    `record_required` — ответить, только если в базе есть клиент или заказ
    (строчное слово или вопрос к памяти: без записи это не имя клиента).
    """
    raw = raw.strip(" .,!?«»\"")
    if not raw or raw.casefold().replace("ё", "е") in _NOT_NAMES or any(name_in(row["name"], raw) for row in printers(ctx)):
        return None
    capital = raw[:1].isupper() and not record_required
    stem = (memory.stems(raw) or [_norm(raw)])[0]
    repo = getattr(ctx.api, "repo", None)
    try:
        people = [row for row in (repo.customers() if repo is not None else []) if _name_match(stem, row.get("name"))]
    except Exception:
        people = []
    orders = [row for row in _order_rows(ctx, stem[:5], 30) if _name_match(stem, row.get("customer_name"))]
    notes = memory.recall(ctx.db, raw, 3) if ctx.db is not None else []
    if not people and not orders and not (capital and notes):
        return None  # строчное слово без карточки и заказов — не имя клиента
    ctx.step("entity", "Клиент", f"«{raw}»: карточек {len(people)}, заказов {len(orders)}, в памяти {len(notes)}")
    lines = []
    if people:
        person = people[0]
        line = f"{person.get('name')}"
        if person.get("phone"):
            line += f", {person['phone']}"
        count = person.get("orders")
        if count not in (None, ""):
            line += f", {count} {plural(count, 'заказ', 'заказа', 'заказов')}"
            if person.get("revenue") not in (None, "") and knowledge._num(person.get("revenue")):
                line += f" на {_money(person['revenue'])}"
        if person.get("last_order"):
            line += f", последний {str(person['last_order'])[:10]}"
        lines.append(line + ".")
        if len(people) > 1:
            lines.append("Похожие: " + ", ".join(str(row.get("name")) for row in people[1:4]) + ".")
    active = [row for row in orders if row.get("status") != "done"]
    for row in active[:3]:
        lines.append(order_line(row) + ".")
    if orders and not active:
        lines.append(f"Активных заказов нет, всего было {len(orders)}.")
    for row in notes:
        lines.append(f"Помню: {row['text']}.")
    entities = {"customer": {"name": (people[0].get("name") if people else raw)}}
    if len(active) == 1:
        entities["order"] = {"id": active[0].get("id"), "number": active[0].get("number")}
    return _answer(ctx, text, "\n".join(lines), source="entity", entities=entities,
                   link={"title": "Клиенты", "href": "/#customers"})


def _navigate(ctx: Context, text: str) -> dict[str, Any] | None:
    match = re.match(r"^(?:открой|перейди\s+(?:в|на|к)|зайди\s+в|покажи\s+раздел|открой\s+раздел)\s+(?:раздел\s+)?(.+)$",
                     _norm(text))
    if not match:
        return None
    target = match.group(1)
    for view, (title, hints) in NAV.items():
        if any(target.startswith(hint) or hint in target.split() for hint in hints) or any(hint in target for hint in hints):
            ctx.step("rule", "Раздел панели", title)
            return _answer(ctx, text, f"Открываю раздел «{title}».", kind="navigate", source="rules",
                           link={"title": title, "href": f"/#{view}"})
    return None


def _rank_products(rep: dict[str, Any], rank_by: str) -> list[dict[str, Any]]:
    """Топ позиций реестра продаж: по штукам («берут») или по сумме."""
    items = [item for item in (rep.get("products") or []) if (item.get("qty") or 0) > 0]
    items.sort(key=lambda item: -(item.get("qty" if rank_by == "qty" else "amount") or 0))
    return items[:5]


def _rank_customers(rep: dict[str, Any]) -> list[dict[str, Any]]:
    """Кто покупал в окне: по строкам реестра, только с именем."""
    by: dict[str, dict[str, Any]] = {}
    for row in rep.get("rows") or []:
        name = str(row.get("customer") or "").strip()
        if not name:
            continue
        bucket = by.setdefault(name, {"name": name, "qty": 0.0, "amount": 0.0})
        bucket["qty"] += float(row.get("qty") or 0)
        bucket["amount"] += float(row.get("amount") or 0)
    items = sorted(by.values(), key=lambda item: -item["amount"])
    return items[:5]


def _period_clarify(ctx: Context, text: str, period: periods.Period) -> dict[str, Any]:
    """Период назван, темы нет, контекста нет — уточняем, как человек.

    «А за прошлую неделю?» в первый же день работы — не ошибка: человек
    назвал окно и ждёт, что его догонит тема. Вместо падения в модель
    отвечаем предложением трёх главных срезов этого окна.
    """
    ctx.step("clarify", "Период без темы", period.phrase)
    phrase = _short_phrase(period)
    reply = (f"Понял: {period.phrase}. Что посчитать — доход, топ товаров или кто больше "
             f"всего покупал? Скажите — посчитаю.")
    return _answer(ctx, text, reply, kind="clarify", source="rules",
                   link={"title": "Финансы", "href": "/#finance"},
                   suggestions=[f"Какой доход {phrase}?", f"Топ товаров {phrase}?",
                                f"Кто больше всего покупал {phrase}?"],
                   extra={"period": period.as_dict()})


def _followup(ctx: Context, text: str) -> dict[str, Any] | None:
    """Продолжение разговора (18.25): «а за прошлую неделю?», «а по штукам?»,
    «а клиенты?» — тема и окно наследуются из мета прошлой реплики помощника.

    Человек не повторяет вопрос целиком: после «Какой доход за неделю?» он
    спросит «а за прошлую?» и ждёт тот же ответ для другого окна. До 18.25
    такая реплика падала в модель или в нерелевантный слой. Теперь короткие
    фразы разбираются детерминированно: новый период — из фразы, тема — из
    фразы или из контекста, а ответ строит тот же код, что и прямой вопрос.
    """
    low = _norm(text)
    if len(low) > 60:
        return None
    # Конкретный заказ по номеру или имени — не продолжение, а свой вопрос.
    if re.search(r"заказ\w*\s+(№\s*\d|[а-яa-z]{3,})", low):
        return None
    low = _correction_tail(low)  # «нет, за 2 недели», «не …, а …»
    period = periods.parse(low)
    meta = ctx.last_meta()
    prev = periods.Period.from_dict(meta.get("period") or None)
    topic = meta.get("topic")
    subject = meta.get("subject") or "products"
    rank_by = meta.get("rank_by") or "amount"
    category = meta.get("category") or ""
    if topic not in ("money", "top", "print", "spend", "orders", "top_orders", "channel") or prev is None:
        if _is_pure_period(low):
            return _period_clarify(ctx, low, period)
        return None
    prev_key = {"money": "money", "print": "print", "spend": "spend", "orders": "orders",
                "top_orders": "top_orders", "channel": "channel",
                "top": ("customers" if subject == "customers" else "top_products")}[topic]
    signal = _topic_signal(low)
    rank_new = "qty" if _RANK_QTY_RE.search(low) else ("amount" if _RANK_AMOUNT_RE.search(low) else "")
    cat_new = _spend_category(low)
    channel_new = _channel_match(low, _channel_names(ctx))
    view_new = "by_day" if re.search(r"по дн\w*|каждый дн\w*", low) else ""
    # «продали через стеллаж» — канал конкретнее «продали»: срез канала
    # побеждает общий сигнал темы.
    if channel_new:
        aspect = "channel"
    else:
        aspect = signal or prev_key
    rank_changed = bool(rank_new) and rank_new != rank_by
    cat_changed = bool(cat_new) and cat_new != category
    channel_changed = bool(channel_new) and channel_new != (meta.get("channel") or "")
    view_changed = bool(view_new) and view_new != (meta.get("view") or "") and aspect == "money"
    # «а как динамика?» — сравнение с прошлым окном тоже смена ответа.
    compare_changed = bool(_COMPARE_RE.search(low)) and aspect == "money"
    if aspect == prev_key and not (period.explicit or cat_changed or channel_changed
                                   or view_changed or compare_changed
                                   or (rank_changed and aspect in ("customers", "top_products"))):
        return None  # Ничего не поменялось — не повторяем один и тот же ответ
    use_period = period if period.explicit else prev
    if aspect == "money":
        if view_new == "by_day":
            return _money_by_day_reply(ctx, text, use_period)
        return _money_reply(ctx, text, use_period, compare=bool(_COMPARE_RE.search(low)))
    if aspect in ("customers", "top_products"):
        sub = "customers" if aspect == "customers" else "products"
        rb = (rank_new or rank_by) if sub == "products" else "amount"
        return _top_reply(ctx, text, sub, use_period, rb)
    if aspect == "top_orders":
        return _top_orders_reply(ctx, text, use_period)
    if aspect == "channel":
        return _channel_reply(ctx, text, channel_new, use_period)
    if aspect == "print":
        return _print_reply(ctx, text, use_period)
    if aspect == "spend":
        cat = _spend_category(low) or category
        if not cat:
            return None
        return _spend_reply(ctx, text, cat, use_period)
    return _orders_count_reply(ctx, text, use_period)


def _top_reply(ctx: Context, text: str, subject: str, period: periods.Period,
               rank_by: str = "amount") -> dict[str, Any]:
    """Топ продаж/клиентов за окно — общий ответ и для «Топ товаров за неделю?»,
    и для продолжения «а по штукам?», «а клиенты?» (слой `_followup`)."""
    api = ctx.api
    acc = getattr(api, "acc", None)
    if acc is None or not hasattr(acc, "sales_details"):
        return None
    ctx.step("rule", "Топ продаж", f"{period.phrase} — {subject}")
    try:
        rep = acc.sales_details(limit=5000, start=period.start, end=period.until)
    except Exception as exc:
        return _answer(ctx, text, f"Реестр продаж не прочитался: {exc.__class__.__name__}.",
                       kind="error", source="facts")
    if subject == "customers":
        items = _rank_customers(rep)
        if not items:
            return _answer(ctx, text,
                           f"{period.phrase.capitalize()} в реестре продаж нет строк с именем клиента.",
                           kind="clarify", source="facts",
                           link={"title": "Финансы", "href": "/#finance"})
        lines = [f"Кто покупал {period.phrase}:"]
        for index, item in enumerate(items, 1):
            qty = item["qty"]
            qty_text = f"{int(round(qty))} шт" if qty == int(round(qty)) else f"{qty} шт"
            lines.append(f"{index}. {item['name']} — {_money(item['amount'])} ({qty_text})")
        reply = "\n".join(lines)
    else:
        items = _rank_products(rep, rank_by)
        if not items:
            return _answer(ctx, text,
                           f"{period.phrase.capitalize()} в реестре продаж нет ни одной строки: "
                           "продаж не было или они проведены в другом окне. Раздел «Финансы» "
                           "покажет точные даты проводок.",
                           kind="clarify", source="facts",
                           link={"title": "Финансы", "href": "/#finance"},
                           suggestions=[f"Какой доход {_short_phrase(period)}?", "Что сейчас печатается?"])
        total = float(rep.get("total_amount") or 0)
        total_qty = float(rep.get("total_qty") or 0)
        by_word = "по штукам" if rank_by == "qty" else "по сумме"
        lines = [f"Топ товаров {period.phrase} ({by_word}):"]
        for index, item in enumerate(items, 1):
            qty = float(item.get("qty") or 0)
            qty_text = f"{int(round(qty))} шт" if qty == int(round(qty)) else f"{qty} шт"
            amount = float(item.get("amount") or 0)
            share = f", {amount / total * 100:.0f}% от суммы" if total else ""
            lines.append(f"{index}. {item.get('name')} — {qty_text}, {_money(amount)}{share}")
        qty_sum = int(round(total_qty)) if total_qty == int(round(total_qty)) else round(total_qty, 1)
        lines.append(f"Итого: {qty_sum} шт на {_money(total)}.")
        reply = "\n".join(lines)
    return _answer(ctx, text, reply, source="facts",
                   entities={}, link={"title": "Финансы", "href": "/#finance"},
                   suggestions=_top_suggestions(subject, period, rank_by),
                   extra={"topic": "top", "subject": subject, "rank_by": rank_by,
                          "period": period.as_dict()})


def _top_sales(ctx: Context, text: str) -> dict[str, Any] | None:
    """«Топ товаров за неделю», «что берут», «топ клиентов» — по реестру продаж.

    До 18.24 слово «товар» относило фразу к теме «склад», и на «Топ товаров за
    неделю?» помощник вываливал остатки стеллажа с ценниками — что лежит на
    полке, а не что у неё купили. Ответ — реестр продаж `sales_details` с
    границами из фразы: та же правда, что рисует раздел «Финансы» (документы,
    заказы, полка), с периодом, который владелец попросил. Короткие
    продолжения («а по штукам?», «а клиенты?») доходят сюда через `_followup`.
    """
    subject = periods.top_subject(text)
    if subject is None:
        return None
    period = periods.parse(text)
    rank_by = periods.rank_by(text) if subject == "products" else "amount"
    return _top_reply(ctx, text, subject, period, rank_by)


def _money_reply(ctx: Context, text: str, period: periods.Period, compare: bool = False) -> dict[str, Any]:
    """Деньги за окно — общий ответ для «Какой доход за неделю?» и для
    продолжения «а за прошлую неделю?» (слой `_followup`)."""
    api = ctx.api
    ctx.step("rule", "Деньги", f"учёт панели — {period.phrase}")
    try:
        if period.explicit:
            summary = api.acc.summary(1, start=period.start, end=period.until)
        else:
            summary = api.acc.summary(30)
    except Exception as exc:
        return _answer(ctx, text, f"Финансы не прочитались: {exc.__class__.__name__}.", kind="error", source="facts")
    reply = (f"{period.phrase.capitalize()}: доход {_money(summary.get('income'))}, "
             f"расход {_money(summary.get('expense'))}, прибыль {_money(summary.get('profit'))}, "
             f"маржа {summary.get('margin')}%.")
    if compare:
        # «больше, чем прошлая неделя?» — сравниваем с окном той же длины.
        previous = period.previous()
        try:
            prev_summary = api.acc.summary(1, start=previous.start, end=previous.until)
            prev_profit = float(prev_summary.get("profit") or 0)
            profit = float(summary.get("profit") or 0)
            diff = round(profit - prev_profit, 2)
            # Сдвиг окна «сегодня» даёт вчерашнюю дату: называем её датой,
            # а не «сегодня».
            prev_phrase = previous.phrase if previous.kind not in ("today", "yesterday", "day_before") \
                else f"{previous.first:%d.%m}"
            ctx.step("rule", "Сравнение", prev_phrase)
            if diff == 0:
                tail = "столько же"
            else:
                tail = f"{'больше' if diff > 0 else 'меньше'} на {_money(abs(diff))}"
            reply += f" {prev_phrase.capitalize()} прибыль была {_money(prev_profit)} — {tail}."
        except Exception as exc:
            log().warning("Мозг помощника: сравнение с прошлым периодом не считалось (%s)", exc)
    return _answer(ctx, text, reply, source="facts", link={"title": "Финансы", "href": "/#finance"},
                   suggestions=_money_suggestions(period),
                   extra={"topic": "money", "period": period.as_dict()})


def _print_reply(ctx: Context, text: str, period: periods.Period) -> dict[str, Any]:
    """«Сколько напечатали за месяц?» — журнал завершённых заданий за окно."""
    api = ctx.api
    ctx.step("rule", "Печать", f"журнал заданий — {period.phrase}")
    try:
        if period.explicit:
            summary = api.acc.summary(1, start=period.start, end=period.until)
        else:
            summary = api.acc.summary(30)
    except Exception as exc:
        return _answer(ctx, text, f"Журнал заданий не прочитался: {exc.__class__.__name__}.",
                       kind="error", source="facts")
    jobs = int(summary.get("jobs_done") or 0)
    hours = float(summary.get("print_hours") or 0)
    grams = float(summary.get("grams") or 0)
    failed = int(summary.get("jobs_failed") or 0)
    grams_text = f"{grams / 1000:.1f} кг" if grams >= 1000 else f"{int(round(grams))} г"
    reply = (f"{period.phrase.capitalize()}: {jobs} {plural(jobs, 'задание', 'задания', 'заданий')} "
             f"на {hours:.1f} ч печати, {grams_text} пластика")
    if failed:
        reply += f", {failed} {plural(failed, 'неудача', 'неудачи', 'неудач')}"
    reply += "."
    return _answer(ctx, text, reply, source="facts", link={"title": "Обзор", "href": "/#dashboard"},
                   suggestions=_print_suggestions(period),
                   extra={"topic": "print", "period": period.as_dict()})


def _spend_reply(ctx: Context, text: str, category: str, period: periods.Period) -> dict[str, Any]:
    """«Сколько потратили на пластик?» — расходы категории за окно."""
    api = ctx.api
    ctx.step("rule", "Расходы", f"категория «{category}» — {period.phrase}")
    try:
        rep = api.acc.category_spending(start=period.start, end=period.until, category=category)
    except Exception as exc:
        return _answer(ctx, text, f"Расходы не прочитались: {exc.__class__.__name__}.", kind="error", source="facts")
    label = _SPEND_CATEGORY_LABELS.get(category, category)
    if float(rep.get("total") or 0) <= 0:
        reply = f"{period.phrase.capitalize()}: на {label} ничего не ушло."
    else:
        count = int(rep.get("count") or 0)
        reply = (f"{period.phrase.capitalize()}: на {label} ушло {_money(rep.get('total'))} — "
                 f"{count} {plural(count, 'покупка', 'покупки', 'покупок')}.")
    return _answer(ctx, text, reply, source="facts", link={"title": "Финансы", "href": "/#finance"},
                   suggestions=_spend_suggestions(period),
                   extra={"topic": "spend", "category": category, "period": period.as_dict()})


def _money_by_day_reply(ctx: Context, text: str, period: periods.Period) -> dict[str, Any]:
    """«Доход по дням за неделю?» — каждый день окна, итог той же сводкой.

    Дни без движения не перечислять: на месяце это двадцать пустых строк.
    Итог считается `summary` — те же цифры, что и в обычном ответе про деньги.
    """
    api = ctx.api
    ctx.step("rule", "Деньги по дням", period.phrase)
    try:
        if period.explicit:
            summary = api.acc.summary(1, start=period.start, end=period.until)
        else:
            summary = api.acc.summary(30)
        days = api.acc.daily_breakdown(period.start, period.until)
    except Exception as exc:
        return _answer(ctx, text, f"Финансы не прочитались: {exc.__class__.__name__}.", kind="error", source="facts")
    active = [d for d in days if d["income"] or d["expense"]]
    if not active:
        reply = f"{period.phrase.capitalize()}: движения по деньгам не было."
    else:
        now_year = datetime.date.today().year
        lines = [f"{period.phrase.capitalize()} по дням:"]
        for d in active[:12]:
            date_part = f"{d['date'][8:10]}.{d['date'][5:7]}"
            if d["date"][:4] != str(now_year):
                date_part += f".{d['date'][2:4]}"
            inc, exp = d["income"], d["expense"]
            if inc and exp:
                lines.append(f"{date_part}: +{_money(inc)} −{_money(exp)}")
            elif inc:
                lines.append(f"{date_part}: +{_money(inc)}")
            else:
                lines.append(f"{date_part}: −{_money(exp)}")
        if len(active) > 12:
            lines.append(f"ещё {len(active) - 12} {plural(len(active) - 12, 'день', 'дня', 'дней')} с движением — все даты в разделе «Финансы»")
        lines.append(f"Итого: доход {_money(summary.get('income'))}, расход {_money(summary.get('expense'))}, "
                     f"прибыль {_money(summary.get('profit'))}, маржа {summary.get('margin')}%.")
        reply = "\n".join(lines)
    return _answer(ctx, text, reply, source="facts", link={"title": "Финансы", "href": "/#finance"},
                   suggestions=[f"А в целом {_short_phrase(period)}?" if period.explicit else "А в целом?",
                                f"Топ товаров {_short_phrase(period)}?"],
                   extra={"topic": "money", "view": "by_day", "period": period.as_dict()})


def _channel_reply(ctx: Context, text: str, channel: str, period: periods.Period) -> dict[str, Any] | None:
    """«Сколько продали через Авито за месяц?» — канал из реестра продаж.

    Реестр `sales_details` уже несёт канал каждой строки (документ — канал
    канала, заказ — канал заказа, полка — «Стеллаж»/«Онлайн»): считаем сумму
    по названию, а не по второй базе.
    """
    api = ctx.api
    acc = getattr(api, "acc", None)
    if acc is None or not hasattr(acc, "sales_details"):
        return None
    ctx.step("rule", "Канал продаж", f"{channel} — {period.phrase}")
    try:
        rep = acc.sales_details(limit=5000, start=period.start, end=period.until)
    except Exception as exc:
        return _answer(ctx, text, f"Реестр продаж не прочитался: {exc.__class__.__name__}.",
                       kind="error", source="facts")
    by_channel: dict[str, float] = {}
    count: dict[str, int] = {}
    for row in rep.get("rows") or []:
        name = str(row.get("channel") or "Магазин")
        by_channel[name] = by_channel.get(name, 0.0) + float(row.get("amount") or 0)
        count[name] = count.get(name, 0) + 1
    if channel not in by_channel:
        close = next((name for name in by_channel if _norm(channel) in _norm(name)
                      or _norm(name) in _norm(channel)), "")
        channel = close or channel
    total_all = float(rep.get("total_amount") or 0)
    amount = by_channel.get(channel, 0.0)
    if not amount:
        reply = f"{period.phrase.capitalize()}: через «{channel}» продаж не было."
    else:
        share = f", {amount / total_all * 100:.0f}% от всех продаж" if total_all else ""
        reply = (f"{period.phrase.capitalize()} через «{channel}»: {_money(amount)} — "
                 f"{count.get(channel, 0)} {plural(count.get(channel, 0), 'позиция', 'позиции', 'позиций')}{share}.")
    return _answer(ctx, text, reply, source="facts", link={"title": "Финансы", "href": "/#finance"},
                   suggestions=[_prev_period_question(period), f"Топ товаров {_short_phrase(period)}?",
                                f"Какой доход {_short_phrase(period)}?"],
                   extra={"topic": "channel", "channel": channel, "period": period.as_dict()})


def _top_orders_reply(ctx: Context, text: str, period: periods.Period) -> dict[str, Any] | None:
    """«Самый большой заказ за месяц?» — три крупных заказа доски за окно."""
    api = ctx.api
    repo = getattr(api, "repo", None)
    if repo is None or not hasattr(repo, "orders"):
        return None
    ctx.step("rule", "Крупные заказы", period.phrase)
    try:
        rows = repo.orders()
    except Exception as exc:
        return _answer(ctx, text, f"Заказы не прочитались: {exc.__class__.__name__}.", kind="error", source="facts")
    in_window = [r for r in rows if period.start <= str(r.get("created_at") or "") < period.until]
    priced = [r for r in in_window if float(knowledge._num(r.get("price")) or 0) > 0]
    if not priced:
        reply = f"{period.phrase.capitalize()}: заказов с ценой не было."
    else:
        top = sorted(priced, key=lambda r: -float(knowledge._num(r.get("price")) or 0))[:3]
        lines = [f"Самые крупные заказы {period.phrase}:"]
        for index, row in enumerate(top, 1):
            line = (f"{index}. №{row.get('number') or '—'} · "
                    f"{row.get('product') or 'без названия'} — {_money(row.get('price'))}")
            if row.get("customer_name"):
                line += f", {row['customer_name']}"
            lines.append(line)
        reply = "\n".join(lines)
    return _answer(ctx, text, reply, source="facts", link={"title": "Заказы", "href": "/#orders"},
                   suggestions=[_prev_period_question(period), f"Сколько заказов {_short_phrase(period)}?",
                                f"Какой доход {_short_phrase(period)}?"],
                   extra={"topic": "top_orders", "period": period.as_dict()})


def _orders_count_reply(ctx: Context, text: str, period: periods.Period) -> dict[str, Any] | None:
    """«Сколько заказов за месяц?» — доска заказов за окно, не весь архив."""
    api = ctx.api
    repo = getattr(api, "repo", None)
    if repo is None or not hasattr(repo, "orders"):
        return None
    ctx.step("rule", "Заказы", f"доска заказов — {period.phrase}")
    try:
        rows = [row for row in repo.orders()
                if period.start <= str(row.get("created_at") or "") < period.until]
        if ctx.db is not None:
            finals = {r["id"] for r in ctx.db.query("SELECT id FROM statuses WHERE is_final=1")}
        else:
            finals = {"done", "canceled", "cancelled"}
    except Exception as exc:
        return _answer(ctx, text, f"Заказы не прочитались: {exc.__class__.__name__}.", kind="error", source="facts")
    total = len(rows)
    if not total:
        reply = f"{period.phrase.capitalize()} новых заказов не было."
    else:
        done = sum(1 for row in rows if str(row.get("status")) in finals)
        working = total - done
        reply = (f"{period.phrase.capitalize()}: {total} {plural(total, 'заказ', 'заказа', 'заказов')} — "
                 f"{done} {plural(done, 'выдан', 'выданы', 'выдано')}, {working} {plural(working, 'в работе', 'в работе', 'в работе')}.")
    return _answer(ctx, text, reply, source="facts", link={"title": "Заказы", "href": "/#orders"},
                   suggestions=[f"Какой доход {_short_phrase(period)}?",
                                f"Топ товаров {_short_phrase(period)}?"],
                   extra={"topic": "orders", "period": period.as_dict()})


def _reads(ctx: Context, text: str) -> dict[str, Any] | None:
    low = _norm(text)
    api = ctx.api
    if re.search(r"(кто|сколько)\s+(мне\s+|нам\s+)?долж(ен|ны|на|ник)|долг(и|ов)?\b|дебитор", low) and "заказ" not in low:
        ctx.step("rule", "Долги", "учёт панели")
        try:
            debts = api.acc.debts()
        except Exception as exc:
            return _answer(ctx, text, f"Долги не прочитались: {exc.__class__.__name__}.", kind="error", source="facts")
        rows = debts.get("rows") or []
        if not rows:
            return _answer(ctx, text, "Долгов нет — все заказы оплачены.", source="facts",
                           link={"title": "Финансы", "href": "/#finance"})
        count = debts.get("count") or len(rows)
        lines = [f"Должны {_money(debts.get('total'))} — {count} {plural(count, 'клиент', 'клиента', 'клиентов')}"
                 + (f", просрочено {_money(debts.get('overdue'))}" if knowledge._num(debts.get("overdue")) else "") + "."]
        for row in rows[:5]:
            days = row.get("days")
            age = ""
            if days is not None:
                age = ", с сегодняшнего дня" if int(knowledge._num(days)) == 0 else \
                    f", {int(knowledge._num(days))} {plural(days, 'день', 'дня', 'дней')}"
            lines.append(f"• {row.get('customer') or 'клиент'} — {_money(row.get('debt'))}, заказ №{row.get('number') or '—'}"
                         + age + (" — просрочен" if row.get("overdue") else ""))
        return _answer(ctx, text, "\n".join(lines), source="facts", link={"title": "Финансы", "href": "/#finance"})
    # «Сколько напечатали за месяц?» — журнал заданий за окно, а не деньги.
    if _PRINT_QUESTION_RE.search(low) and "план" not in low and "осталось" not in low:
        return _print_reply(ctx, text, periods.parse(text))
    # «Сколько заказов за месяц?» — доска заказов за окно, не весь архив.
    if _ORDERS_QUESTION_RE.search(low) and "очеред" not in low:
        found = _orders_count_reply(ctx, text, periods.parse(text))
        if found is not None:
            return found
    # «Самый большой заказ за месяц?» — крупные заказы доски, не счёт заказов.
    if re.search(r"(топ|крупн\w*|самый\s+больш\w*|лучш\w*)\s+заказ|заказ\w*\s+(самый\s+больш\w*|крупн\w*)", low):
        found = _top_orders_reply(ctx, text, periods.parse(text))
        if found is not None:
            return found
    # «Сколько продали через Авито?» — канал из реестра продаж, не вся сводка.
    channel = _channel_match(low, _channel_names(ctx))
    if channel and (re.search(r"(продаж|продал|выручк|доход|сколько|заказ)", low)
                    or periods.parse(text).explicit):
        return _channel_reply(ctx, text, channel, periods.parse(text))
    # «Сколько потратили на пластик?» — расходы категории, не вся сводка.
    if _SPEND_VERB_RE.search(low) and _spend_category(low):
        return _spend_reply(ctx, text, _spend_category(low), periods.parse(text))
    if (re.search(r"(выручк|прибыл|доход|расход|ушло|ушли|динамик|сколько (мы )?заработал|финанс|маржа|продаж|продал|потратил|затрат)", low)
            and not any(word in low for word in ("пластик", "филамент", "катуш", "электричеств", "энерг",
                                                 "аренд", "налог", "комисс"))):
        # Период — из фразы: «с 21.09 по 25.09», «за неделю», «за сентябрь».
        # Без него — 30 дней, как раньше; ответ в любом случае называет даты,
        # чтобы месяц нельзя было принять за неделю.
        period = periods.parse(text)
        # «по дням» — каждый день окна, итог той же сводкой.
        if re.search(r"по дн\w*|каждый дн\w*", low):
            return _money_by_day_reply(ctx, text, period)
        # «как динамика?», «продажи растут?» — без второго окна берём окно
        # той же длины сразу перед этим, как и на «больше, чем прошлая?».
        compare = bool(re.search(
            r"(больше|меньше|чем|по сравнен|как прошл|чем прошл|чем в прошл|динамик|раст\w*|пада\w*|вырос\w*|упал\w*|сниж\w*)", low))
        return _money_reply(ctx, text, period, compare=compare)
    if re.search(r"(план\s+на\s+(сегодня|день)|что\s+печатать\s+дальше|что\s+дальше\s+печатать|следующее\s+задание)", low):
        ctx.step("rule", "План", "мастер-план производства")
        try:
            plan = api.planner.day_plan()
        except Exception as exc:
            return _answer(ctx, text, f"План не прочитался: {exc.__class__.__name__}.", kind="error", source="facts")
        lines = [str(plan.get("verdict_text") or "План на сегодня.")]
        suggested = plan.get("suggested_next") or {}
        if suggested:
            lines.append(f"Следующее: {suggested.get('title') or suggested.get('name')} — {suggested.get('hours', '—')} ч.")
        lines.append(f"Заказов к печати {plan.get('orders_to_print', 0)}, часов в плане {plan.get('total_hours', 0)}.")
        return _answer(ctx, text, "\n".join(lines), source="facts", link={"title": "Очередь", "href": "/#queue"})
    if re.search(r"(брифинг|сводк\w* (на )?(сегодня|утро)|итог\w* дня|как прош\w* день)", low):
        kind = "summary" if re.search(r"итог|прош", low) else "briefing"
        ctx.step("rule", "День владельца", kind)
        day = knowledge.day(api, kind)
        return _answer(ctx, text, "\n".join(str(line) for line in (day.get("lines") or [])[:10]) or "Сводка пуста.",
                       source="facts")
    return None


def _knowledge_fast(ctx: Context, text: str) -> dict[str, Any] | None:
    """Живые вопросы, на которые у панели уже есть детерминированный ответ."""
    if knowledge._plastic_question(text) or (knowledge._printing_question(text) and "сколько" not in _norm(text)):
        history = [{"role": turn["role"], "content": turn["text"]} for turn in ctx.history[-6:]]
        found = knowledge.answer(ctx.api, text, fast=True, history=history)
        if found.get("answered"):
            ctx.step("rule", "Факты цеха", found.get("source") or "facts")
            entities = {}
            busy = [row for row in printers(ctx) if row["state"] in ("RUNNING", "PAUSE", "PREPARE")]
            if len(busy) == 1:
                entities["printer"] = {"id": busy[0]["id"], "name": busy[0]["name"]}
            return _answer(ctx, text, found["answer"], source="farm", facts=found.get("facts") or [],
                           warnings=found.get("warnings") or [], entities=entities)
    return None


# ---------------------------------------------------------------------------
# 5. Компьютер
# ---------------------------------------------------------------------------

def _computer(ctx: Context, text: str, delegate: bool) -> dict[str, Any] | None:
    if ctx.db is None or not delegate:
        return None
    cfg = assistant.agent_config(ctx.db)
    looks_pc = bool(_PC_RE.search(text))
    if not cfg.get("enabled"):
        if looks_pc:
            ctx.step("agent", "Компьютер", "агент выключен")
            return _answer(ctx, text, "Похоже на команду компьютеру, но агент выключен в настройках помощника. "
                           "Включите «Агент компьютера» и запустите его — тогда окна, звук, программы и файлы "
                           "будут доступны отсюда.", kind="clarify", source="agent",
                           link={"title": "Настройки", "href": "/#settings"})
        return None
    reply = assistant.agent_chat(ctx.db, text, session=ctx.session)
    if not reply.get("ok") and not reply.get("handled"):
        if looks_pc:
            ctx.step("agent", "Компьютер", "агент не отвечает")
            return _answer(ctx, text, f"Это команда компьютеру, а агент не отвечает: {reply.get('reason') or 'нет связи'}.",
                           kind="error", source="agent", extra={"agent_down": True})
        return None
    if not reply.get("handled"):
        return None
    for step in reply.get("steps") or []:
        ctx.steps.append({"kind": "agent", "title": f"Агент: {step.get('title')}", "detail": step.get("detail", "")})
    kind = "pc" if reply.get("kind") not in ("error", "clarify") else reply.get("kind")
    return _answer(ctx, text, str(reply.get("reply") or "Готово."), kind=kind, source="agent",
                   suggestions=list(reply.get("suggestions") or []),
                   extra={"skill": reply.get("skill"), "pending": reply.get("pending")})


# ---------------------------------------------------------------------------
# 6–7. Модель
# ---------------------------------------------------------------------------

def _memory_block(ctx: Context, text: str) -> str:
    if ctx.db is None:
        return ""
    rows = memory.recall(ctx.db, text, 5, touch=False)
    pinned = [row for row in memory.memories(ctx.db, 20) if row.get("pinned")]
    name = memory.owner_name(ctx.db)
    lines = [f"Владельца зовут {name}."] if name else []
    seen = set()
    for row in pinned + rows:
        if row["id"] not in seen:
            seen.add(row["id"])
            lines.append(f"- {row['text']}")
    return ("Память о владельце и цехе:\n" + "\n".join(lines)) if lines else ""


def _planner(ctx: Context, text: str) -> dict[str, Any] | None:
    """Модель выбирает действие каталога по фразе, фактам, памяти и разговору."""
    if ctx.db is None:
        return None
    about_shop = (assistant.catalog_phrase(text) or re.search(r"стан[оа]к|станк", _norm(text))
                  or any(name_in(row["name"], text) for row in printers(ctx)))
    if not about_shop:
        return None
    state = assistant.status(ctx.db)
    if not state.get("available"):
        return None
    cfg = assistant.config(ctx.db)
    catalog = "\n".join(
        f"- {name}: {spec['title']}" + (f" (параметры: {', '.join(spec['params'])})" if spec["params"] else "")
        + (" — с подтверждением" if spec["confirm"] else "") for name, spec in assistant.ACTIONS.items())
    park = "; ".join(f"{row['name']} [{row['id']}] {row['state'] or '—'}" for row in printers(ctx)[:8])
    system = (
        "Ты — диспетчер цеха 3D-печати в системе PrintFlow. Выбери действие из каталога или ответь сам.\n"
        f"{knowledge.shop_context(ctx.api)}\nСтанки (имя [id] состояние): {park or 'нет'}\n"
        f"{_memory_block(ctx, text)}\n\nКаталог:\n{catalog}\n\n"
        "Верни ОДИН JSON: {\"action\": \"id или пустая строка\", \"params\": {…}, \"reply\": \"ответ по-русски\", "
        "\"ask\": \"уточняющий вопрос или пустая строка\"}.\nПравила: только действия каталога; printer_id — id из "
        "списка станков; не выдумывай суммы, номера и граммы; вопрос о состоянии — чтение или ответ в reply; "
        "не хватает данных — ask.")
    messages = [{"role": "system", "content": system}]
    for turn in ctx.history[-6:]:
        messages.append({"role": turn["role"], "content": turn["text"][:400]})
    messages.append({"role": "user", "content": text[:MAX_TEXT]})
    ctx.step("model", "Планировщик", cfg["model"])
    ok, payload, reason = assistant._post_json(
        f"{cfg['url']}/api/chat", {"model": cfg["model"], "stream": False, "format": "json",
                                   "options": {"temperature": 0.1}, "messages": messages},
        timeout=min(60.0, float(cfg["timeout_sec"])))
    if not ok or not isinstance(payload, dict):
        ctx.step("model", "Планировщик", f"не ответил: {reason}")
        return None
    answer = assistant._extract_json(str((payload.get("message") or {}).get("content") or ""))
    action_id = str(answer.get("action") or "").strip().casefold()
    ask = " ".join(str(answer.get("ask") or "").split())[:300]
    said = " ".join(str(answer.get("reply") or "").split())[:600]
    if ask and not action_id:
        return _answer(ctx, text, ask, kind="clarify", source="model")
    spec = assistant.ACTIONS.get(action_id)
    if spec is None:
        if action_id:
            ctx.step("check", "Проверка каталогом", f"действия «{action_id}» нет — отклонено")
        return None
    params = assistant._clean_params(spec, answer.get("params"))
    ctx.step("check", "Проверка каталогом", f"{action_id}: параметры {', '.join(params) or 'нет'}")
    if not spec["confirm"]:
        return None  # чтения отвечают факты базы, а не пересказ модели
    if "printer_id" in spec["params"] and params.get("printer_id") not in {row["id"] for row in printers(ctx)}:
        printer, _cands, how = resolve_printer(ctx, text)
        if printer is None:
            return None
        params["printer_id"] = printer["id"]
        ctx.step("entity", "Станок", how)
    missing = [key for key in spec["params"] if key not in params and key not in ("value", "account_id", "payment_method")]
    warnings = ["Не хватает параметров: " + ", ".join(missing) + " — добавьте их в карточке"] if missing else []
    return _answer(ctx, text, said or f"{spec['title']}. Подтвердите в карточке.", kind="action", source="model",
                   action=_action(action_id), params=params, explain=said, warnings=warnings)


def _converse(ctx: Context, text: str) -> dict[str, Any]:
    history = [{"role": turn["role"], "content": turn["text"]} for turn in ctx.history[-8:]]
    if knowledge.topics_of(text):
        found = knowledge.answer(ctx.api, text, fast=True, history=history)
        if found.get("answered"):
            ctx.step("facts", "Факты базы", ", ".join(found.get("topics") or []) or "поиск")
            return _answer(ctx, text, found["answer"], source="facts", facts=found.get("facts") or [],
                           warnings=found.get("warnings") or [])
    context = knowledge.shop_context(ctx.api)
    block = _memory_block(ctx, text)
    if block:
        context += "\n" + block
    ctx.step("model", "Разговор", "модель по фактам цеха")
    reply = assistant.converse(ctx.db, text, history, context=context[:1400])
    if reply.get("ok"):
        return _answer(ctx, text, reply["answer"], source="web" if reply.get("web") else "model",
                       warnings=list(reply.get("warnings") or []),
                       extra={"sources": reply.get("sources") or [], "model": reply.get("model") or "",
                              "web": bool(reply.get("web"))})
    found = knowledge.answer(ctx.api, text, fast=True, history=history)
    if found.get("answered"):
        return _answer(ctx, text, found["answer"], source="facts", facts=found.get("facts") or [],
                       warnings=[f"Модель недоступна: {reply.get('reason')}"])
    reason = str(reply.get("reason") or "модель недоступна")
    off = "выключен" in reason.casefold()
    ctx.step("model", "Разговор", "модель выключена в настройках" if off else f"модель не ответила: {reason}")
    lines = ["Такое без модели я не разберу." if off else f"Модель сейчас не ответила ({reason}), а без неё такое я не разберу.",
             "Зато знаю цех: станки, заказы, клиентов, долги, деньги, план и склад — и умею считать, "
             "запоминать и открывать разделы панели."]
    if off:
        lines.append("Свободные вопросы заработают, когда в настройках помощника включите модель Ollama.")
    return _answer(ctx, text, " ".join(lines), kind="clarify", source="rules", suggestions=suggestions_for(ctx),
                   link={"title": "Настройки помощника", "href": "/#settings"} if off else None,
                   warnings=[] if off else [f"Модель недоступна: {reason}"],
                   # Явный признак для агента компьютера: панель фразу не поняла —
                   # пусть решает он сам, а не показывает человеку этот запасной ответ.
                   extra={"understood": False})


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------

def chat(api: Any, text: str, session: str = "main", source: str = "panel", delegate: bool = True) -> dict[str, Any]:
    """Реплика владельца → ответ помощника (см. порядок слоёв в шапке модуля)."""
    clean = " ".join(str(text or "").split())[:MAX_TEXT]
    ctx = Context(api, session, source)
    if not clean:
        return _answer(ctx, "", "Напишите или скажите, что нужно.", kind="clarify", save=False)
    for layer in (_clarification, _memory, _instant):
        found = layer(ctx, clean)
        if found:
            return found
    for layer in (_followup, _printer_command, _printer_status, _farm_overview, _order_intents, _customer,
                  _navigate, _top_sales, _reads, _knowledge_fast):
        try:
            found = layer(ctx, clean)
        except Exception as exc:  # один сломанный сервис не роняет разговор
            log().warning("Мозг помощника: слой %s упал (%s)", layer.__name__, exc)
            ctx.step("error", layer.__name__, exc.__class__.__name__)
            found = None
        if found:
            return found
    found = _computer(ctx, clean, delegate)
    if found:
        return found
    try:
        found = _planner(ctx, clean)
    except Exception as exc:
        log().warning("Мозг помощника: планировщик упал (%s)", exc)
        found = None
    if found:
        return found
    return _converse(ctx, clean)


def context_summary(api: Any) -> dict[str, Any]:
    """Что помощник видит прямо сейчас — для боковой панели страницы (И315)."""
    ctx = Context(api, "main", "panel")
    rows = printers(ctx)
    db = getattr(api, "db", None)
    summary: dict[str, Any] = {
        "ok": True, "date": assistant.date_line(), "farm": farm_phrase(ctx),
        "printers": [{"id": row["id"], "name": row["name"], "model": row["model"], "state": row["state"],
                      "progress": row["progress"], "task": row["task"]} for row in rows[:12]],
        "queue": len(ctx.snapshot().get("queue") or []),
        "owner": memory.owner_name(db) if db is not None else "",
        "memory": memory.stats(db) if db is not None else {"memories": 0, "turns": 0},
    }
    try:
        debts = api.acc.debts()
        summary["debts"] = {"total": debts.get("total"), "count": debts.get("count"), "overdue": debts.get("overdue")}
    except Exception:
        summary["debts"] = None
    return summary
