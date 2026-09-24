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
                     "online": bool(info.get("online", True))})
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
            memory.add_turn(ctx.db, ctx.session, "assistant", reply, {
                "kind": kind, "source": source, "entities": entities or {},
                "action": (action or {}).get("id"), "params": params or {}, "awaiting": awaiting or {}})
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

def _instant(ctx: Context, text: str) -> dict[str, Any] | None:
    low = _norm(text)
    math = assistant.simple_math(text)
    if math:
        ctx.step("rule", "Арифметика", "без модели")
        return _answer(ctx, text, math, source="math")
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
                       source="farm", suggestions=["Что сейчас печатается?", "Брифинг на сегодня", "Что ты умеешь?"])
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
             "• понимать продолжение: «а у второго?», «поставь его на паузу»;",
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
    parts = [f"Печатают {len(busy)} из {len(rows)}" if busy else f"Все {len(rows)} станков свободны"]
    if paused:
        parts.append("на паузе: " + ", ".join(row["name"] for row in paused[:3]))
    if queue:
        parts.append(f"в очереди {queue}")
    return ", ".join(parts) + "."


# ---------------------------------------------------------------------------
# 4. Цех без модели
# ---------------------------------------------------------------------------

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
    if not explicit and not any(row["state"] in states for row in printers(ctx)):
        # «Пауза» без станка, и ни один станок сейчас не подходит — вероятно,
        # это про музыку на компьютере: пусть решает агент.
        return None
    printer, candidates, how = resolve_printer(ctx, text, states)
    ctx.step("entity", "Станок", how)
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


def _printer_status(ctx: Context, text: str) -> dict[str, Any] | None:
    low = _norm(text)
    follow = re.fullmatch(r"(а\s+)?(у|на|что\s+(у|на))\s+(\S+)(\s+станк\w*)?\??", low)
    asks = re.search(r"(что|как)\s+(там\s+)?(с|у|на)\s+", low) or follow
    if not asks:
        return None
    named = any(name_in(row["name"], low) or (row["model"] and _norm(row["model"]) in low.split())
                for row in printers(ctx))
    ordinal = any(word in _ORDINALS for word in re.findall(r"[0-9a-zа-я]+", low))
    if not named and not (ordinal and (follow or re.search(r"станк|принтер", low))):
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
            line += f", осталось ~{knowledge._minutes_label(printer['remaining_min'])}"
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


def _customer(ctx: Context, text: str) -> dict[str, Any] | None:
    low = _norm(text)
    match = re.search(r"(?:что|как)\s+(?:там\s+)?(?:с|у|по)\s+(?:клиент\w*\s+)?([А-ЯЁ][а-яё]{2,})|клиент\w*\s+([А-ЯЁ][а-яё]{2,})",
                      str(text or ""))
    if not match or "заказ" in low:
        return None
    raw = match.group(1) or match.group(2)
    if any(name_in(row["name"], raw) for row in printers(ctx)):
        return None
    stem = (memory.stems(raw) or [_norm(raw)])[0]
    repo = getattr(ctx.api, "repo", None)
    try:
        people = [row for row in (repo.customers() if repo is not None else []) if stem[:4] in _norm(row.get("name"))]
    except Exception:
        people = []
    orders = [row for row in _order_rows(ctx, stem[:5], 30) if stem[:4] in _norm(row.get("customer_name"))]
    notes = memory.recall(ctx.db, raw, 3) if ctx.db is not None else []
    if not people and not orders and not notes:
        return None
    ctx.step("entity", "Клиент", f"«{raw}»: карточек {len(people)}, заказов {len(orders)}, в памяти {len(notes)}")
    lines = []
    if people:
        person = people[0]
        line = f"{person.get('name')}"
        if person.get("phone"):
            line += f", {person['phone']}"
        if person.get("orders") not in (None, ""):
            line += f", заказов {person['orders']}"
        if person.get("revenue") not in (None, ""):
            line += f", выручка {_money(person['revenue'])}"
        lines.append(line + ".")
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
        lines = [f"Должны {_money(debts.get('total'))} — {debts.get('count')} клиентов"
                 + (f", просрочено {_money(debts.get('overdue'))}" if knowledge._num(debts.get("overdue")) else "") + "."]
        for row in rows[:5]:
            lines.append(f"• {row.get('customer') or 'клиент'} — {_money(row.get('debt'))}, заказ №{row.get('number') or '—'}"
                         + (f", {row.get('days')} дн." if row.get("days") is not None else "")
                         + (" — просрочен" if row.get("overdue") else ""))
        return _answer(ctx, text, "\n".join(lines), source="facts", link={"title": "Финансы", "href": "/#finance"})
    if re.search(r"(выручк|прибыл|доход|сколько (мы )?заработал|финанс|маржа)", low):
        ctx.step("rule", "Деньги", "учёт панели за 30 дней")
        try:
            summary = api.acc.summary(30)
        except Exception as exc:
            return _answer(ctx, text, f"Финансы не прочитались: {exc.__class__.__name__}.", kind="error", source="facts")
        reply = (f"За 30 дней: доход {_money(summary.get('income'))}, расход {_money(summary.get('expense'))}, "
                 f"прибыль {_money(summary.get('profit'))}, маржа {summary.get('margin')}%.")
        return _answer(ctx, text, reply, source="facts", link={"title": "Финансы", "href": "/#finance"})
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
    return _answer(ctx, text, "Не могу ответить: " + str(reply.get("reason") or "модель недоступна")
                   + ". Спросите про заказ, клиента, долги, печать или склад — это я знаю без модели.",
                   kind="error", source="model",
                   suggestions=["Что сейчас печатается?", "Кто должен денег?", "Что ты умеешь?"])


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
    for layer in (_printer_command, _printer_status, _order_intents, _customer, _navigate, _reads, _knowledge_fast):
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
