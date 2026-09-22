"""Тексты и картинки бота — цех словами, без Mini App (18.12.3).

Зачем. Mini App открывается только по внешнему HTTPS-адресу
(`docs/MINIAPP-ЦЕХА.md`), а он есть не у всех и не сразу. Пока адреса нет —
и вообще, когда удобнее прочитать чат, чем открывать окно, — бот должен
отвечать сам: текстом и, когда есть что показать, фотографией (кадр камеры
принтера или снимок изделия из фотоальбома заказа).

Здесь только чистые функции: словари → строки, выбор кадра, обрезка под
лимиты Telegram. Ни сети, ни базы: данные приносит `handlers/report.py`,
он же отправляет. Поэтому файл проверяется обычными тестами без бота.
"""
from __future__ import annotations

# Лимиты Telegram: подпись к фото — 1024 знака, текст — 4096; держим запас,
# потому что старые сборки резали по 4096 байта, а не по символам.
CAPTION_LIMIT = 1024
TEXT_LIMIT = 3800

STATE_RU = {
    "RUNNING": "печатает",
    "IDLE": "свободен",
    "PAUSE": "на паузе",
    "PAUSED": "на паузе",
    "FINISH": "печать завершена",
    "PREPARE": "готовится",
    "FAILED": "ошибка",
    "OFFLINE": "не в сети",
    "UNKNOWN": "нет данных",
}

# Статусы заказов: в базе они латиницей, в переписке — по-русски. Ключи,
# которых нет в таблице, показываем как есть: лучше английское слово, чем
# выдуманное «в работе» у несуществующего статуса.
ORDER_RU = {
    "new": "новая заявка",
    "estimate": "расчёт",
    "prepay": "ждём предоплату",
    "queue": "в очереди",
    "printing": "печатается",
    "post": "постобработка",
    "ready": "готов к выдаче",
    "done": "выдан",
    "stocked": "на складе",
    "in_progress": "в работе",
    "cancelled": "отменён",
    "canceled": "отменён",
    "hold": "на паузе",
    "archive": "в архиве",
}


def limit(text: str, size: int = TEXT_LIMIT) -> str:
    """Обрезать текст под лимит Telegram, не разрывая посреди слова грубо."""
    text = str(text or "")
    if len(text) <= size:
        return text
    cut = text[: max(0, size - 1)]
    space = cut.rfind(" ")
    if space > size - 200:
        cut = cut[:space]
    return cut.rstrip() + "…"


def signed(value) -> str:
    """«+950 ₽» / «−300 ₽»: знак виден и не спорит со словом «расход»."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    if abs(number) < 0.005:
        return money(0)
    sign = "+" if number > 0 else "−"
    return sign + money(abs(number))


def money(value) -> str:
    """«1 250 ₽» — без копеек, если они нулевые."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    whole = round(number, 2)
    if abs(whole - round(whole)) < 0.005:
        text = f"{int(round(whole)):,}".replace(",", " ")
    else:
        text = f"{whole:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} ₽"


def plural(count, one: str, few: str, many: str) -> str:
    """Русское число с существительным: 1 задание, 2 задания, 5 заданий."""
    try:
        n = abs(int(count))
    except (TypeError, ValueError):
        n = 0
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def state_label(state: str) -> str:
    return STATE_RU.get(str(state or "").upper(), str(state or "").strip() or "нет данных")


def order_label(status: str) -> str:
    key = str(status or "").strip().lower()
    return ORDER_RU.get(key, key or "без статуса")


# ------------------------------------------------------------------ принтеры
def printers_from_snapshot(snapshot: dict) -> list[dict]:
    """Привести `manager.snapshot()` к плоскому списку принтеров.

    У снимка две формы: список словарей (менеджер) и что-то иное (заглушки в
    тестах, пустой цех) — во втором случае честно отдаём пустой список.
    """
    rows = (snapshot or {}).get("printers") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        info = row.get("printer") if isinstance(row.get("printer"), dict) else {}
        connection = row.get("connection") if isinstance(row.get("connection"), dict) else {}
        job = row.get("job") if isinstance(row.get("job"), dict) else {}
        order = job.get("order") if isinstance(job.get("order"), dict) else {}
        temperature = row.get("temperature") if isinstance(row.get("temperature"), dict) else {}
        out.append({
            "id": str(row.get("id") or ""),
            "name": str(row.get("name") or "Принтер"),
            "connected": bool(connection.get("connected")),
            "state": str(info.get("state") or row.get("state") or "").upper(),
            "state_label": str(info.get("state_label") or ""),
            "progress": info.get("progress"),
            "remaining_min": info.get("remaining_min"),
            "task": str(info.get("task") or info.get("file") or ""),
            "order": str(order.get("number") or ""),
            "nozzle": temperature.get("nozzle"),
            "bed": temperature.get("bed"),
        })
    return out


def printer_line(printer: dict) -> str:
    """Одна строка про принтер: состояние, прогресс, задание."""
    name = printer.get("name") or "Принтер"
    if not printer.get("connected"):
        return f"🔌 {name} — не в сети"
    state = printer.get("state") or ""
    label = state_label(state)
    tail: list[str] = []
    if state in ("RUNNING", "PREPARE", "IDLE", "FINISH", "PAUSE", "PAUSED"):
        tmp = _temps(printer)
        if tmp:
            tail.append(tmp)
    if state in ("RUNNING", "PREPARE"):
        try:
            percent = int(round(float(printer.get("progress") or 0)))
        except (TypeError, ValueError):
            percent = 0
        if percent:
            tail.append(f"{percent}%")
        task = str(printer.get("task") or "").strip()
        if task:
            tail.append(task[:48])
        if printer.get("order"):
            tail.append(f"заказ №{printer['order']}")
        try:
            left = int(round(float(printer.get("remaining_min") or 0)))
        except (TypeError, ValueError):
            left = 0
        if left > 0:
            tail.append(f"~{left} мин")
    return f"🖨 {name} — {label}" + (f" · {' · '.join(tail)}" if tail else "")


def _temps(printer: dict) -> str:
    """«🌡 220/60 °C» — сопло и стол, если телеметрия есть."""
    def one(value) -> str:
        try:
            return str(int(round(float(value))))
        except (TypeError, ValueError):
            return ""

    nozzle, bed = one(printer.get("nozzle")), one(printer.get("bed"))
    if not nozzle and not bed:
        return ""
    return f"🌡 {nozzle or '—'}/{bed or '—'} °C"


def printers_text(printers: list[dict]) -> str:
    """Список принтеров словами: кто печатает, кто свободен, кто отвалился."""
    if not printers:
        return "🖨 Принтеров нет — добавьте их в панели (Настройки → Принтеры)."
    lines = [f"🖨 Принтеры: {plural(len(printers), 'станок', 'станка', 'станков')}"]
    for printer in printers:
        lines.append(printer_line(printer))
    return "\n".join(lines)


# -------------------------------------------------------------------- сводка
def status_text(summary: dict, printers: list[dict]) -> str:
    """Полная сводка цеха: цифры дня, затем построчно принтеры."""
    summary = summary or {}
    total = int(summary.get("total") or 0)
    online = int(summary.get("online") or 0)
    printing = int(summary.get("printing") or 0)
    lines = ["📊 Цех сейчас"]
    if total:
        line = f"🖨 В сети {online} из {total}"
        if printing:
            line += f", печатает {plural(printing, 'станок', 'станка', 'станков')}"
        lines.append(line)
    else:
        lines.append("🖨 Принтеров нет — добавьте в панели")
    lines.append(f"🧾 Очередь печати: {plural(summary.get('queue') or 0, 'задание', 'задания', 'заданий')}")
    lines.append(f"📦 Готово к выдаче: {plural(summary.get('orders_ready') or 0, 'заказ', 'заказа', 'заказов')}")
    if int(summary.get("inbox") or 0):
        lines.append(f"✉️ Непрочитанных в inbox: {int(summary['inbox'])}")
    if int(summary.get("low_stock") or 0):
        lines.append(f"⚠ Мало на полке: {plural(summary['low_stock'], 'позиция', 'позиции', 'позиций')}")
    lines.append(f"💰 Деньги за сегодня: {money(summary.get('today_money'))}")
    if printers:
        lines.append("")
        for printer in printers:
            lines.append(printer_line(printer))
    return "\n".join(lines)


def summary_line(summary: dict) -> str:
    """Компактная строка для меню: цифры дня в одну строку."""
    summary = summary or {}
    parts = [f"🖨 {int(summary.get('online') or 0)}/{int(summary.get('total') or 0)}"]
    printing = int(summary.get("printing") or 0)
    if printing:
        parts.append(f"печатает {printing}")
    parts.append(f"🧾 очередь {int(summary.get('queue') or 0)}")
    parts.append(f"📦 готово {int(summary.get('orders_ready') or 0)}")
    inbox = int(summary.get("inbox") or 0)
    if inbox:
        parts.append(f"✉️ inbox {inbox}")
    low = int(summary.get("low_stock") or 0)
    if low:
        parts.append(f"⚠ мало {low}")
    parts.append(f"💰 {money(summary.get('today_money'))}")
    return " · ".join(parts)


# -------------------------------------------------------------------- заказы
def order_line(order: dict) -> str:
    number = str(order.get("number") or order.get("id") or "").strip()
    product = str(order.get("product") or "").strip() or "без названия"
    tail = [order_label(order.get("status"))]
    customer = str(order.get("customer_name") or "").strip()
    if customer:
        tail.append(customer)
    price = order.get("price")
    if price:
        tail.append(money(price))
    return f"№{number} · {product} · " + " · ".join(tail)


def orders_text(orders: list[dict], title: str = "📦 Заказы") -> str:
    if not orders:
        return f"{title}: пока ничего нет."
    lines = [f"{title}: {plural(len(orders), 'заказ', 'заказа', 'заказов')}"]
    for order in orders:
        lines.append(order_line(order))
    return "\n".join(lines)


def order_card(order: dict) -> str:
    """Карточка одного заказа — когда в команде назвали номер."""
    order = order or {}
    number = str(order.get("number") or order.get("id") or "").strip()
    lines = [f"📦 Заказ №{number} · {str(order.get('product') or 'без названия')}"]
    lines.append(f"Статус: {order_label(order.get('status'))}")
    if order.get("customer_name"):
        lines.append(f"Клиент: {order['customer_name']}")
    if order.get("material") or order.get("color"):
        lines.append("Материал: " + " ".join(str(x) for x in (order.get("material"), order.get("color")) if x))
    if order.get("qty"):
        lines.append(f"Количество: {order['qty']}")
    if order.get("price"):
        lines.append(f"Цена: {money(order['price'])}")
    if order.get("due"):
        lines.append(f"Срок: {str(order['due'])[:10]}")
    if order.get("file"):
        lines.append(f"Файл: {str(order['file'])[:60]}")
    return "\n".join(lines)


def order_caption(order: dict) -> str:
    number = str((order or {}).get("number") or (order or {}).get("id") or "").strip()
    product = str((order or {}).get("product") or "").strip()
    return limit(f"📦 №{number} · {product} · {order_label((order or {}).get('status'))}", CAPTION_LIMIT)


# -------------------------------------------------------------------- полка
def shelf_line(item: dict) -> str:
    name = str(item.get("name") or item.get("nom_id") or "позиция").strip()
    try:
        qty = item.get("qty")
        qty_text = f"{int(qty)} шт" if float(qty or 0) == int(float(qty or 0)) else f"{qty} шт"
    except (TypeError, ValueError):
        qty_text = f"{item.get('qty')} шт"
    low = _is_low(item)
    tail = qty_text + (" (мало)" if low else "")
    if item.get("category_name"):
        tail += f" · {item['category_name']}"
    if item.get("price"):
        tail += f" · {money(item['price'])}"
    return f"{'⚠' if low else '•'} {name} — {tail}"


def _is_low(item: dict) -> bool:
    try:
        qty = float(item.get("qty") or 0)
        minimum = float(item.get("min_qty") or 0)
    except (TypeError, ValueError):
        return False
    return minimum > 0 and qty <= minimum


def shelf_text(items: list[dict]) -> str:
    if not items:
        return "🛒 Полка пуста: ни одной позиции."
    low = [item for item in items if _is_low(item)]
    # Сначала то, что заканчивается: смысл отчёта — успеть пополнить.
    ordered = low + [item for item in items if not _is_low(item)]
    lines = [f"🛒 Полка: {plural(len(items), 'позиция', 'позиции', 'позиций')}"
             + (f", из них мало — {len(low)}" if low else "")]
    for item in ordered:
        lines.append(shelf_line(item))
    return "\n".join(lines)


# ------------------------------------------------------------------ очередь
def queue_line(job: dict, index: int) -> str:
    name = str(job.get("name") or job.get("file") or "задание").strip()
    state = str(job.get("state") or "").strip()
    tail: list[str] = []
    if state == "running":
        tail.append("печатается")
    elif state == "queued":
        tail.append("в очереди")
    elif state:
        tail.append(state)
    if job.get("order") and isinstance(job["order"], dict) and job["order"].get("number"):
        tail.append(f"заказ №{job['order']['number']}")
    try:
        minutes = int(round(float(job.get("est_minutes") or 0)))
    except (TypeError, ValueError):
        minutes = 0
    if minutes:
        tail.append(f"~{minutes} мин")
    return f"{index}. {name[:60]}" + (f" — {' · '.join(tail)}" if tail else "")


def queue_text(queue: list[dict]) -> str:
    if not queue:
        return "🧾 Очередь печати пуста — принтеры свободны."
    lines = [f"🧾 Очередь печати: {plural(len(queue), 'задание', 'задания', 'заданий')}"]
    for index, job in enumerate(queue, 1):
        lines.append(queue_line(job, index))
    return "\n".join(lines)


# ------------------------------------------------------------------- деньги
def money_text(today: dict, week: dict) -> str:
    today = today or {}
    week = week or {}
    income = float(today.get("income") or 0)
    expense = float(today.get("expense") or 0)
    lines = ["💰 Деньги"]
    lines.append(f"Сегодня: приход {money(income)} · расход {money(abs(expense))}"
                 f" · итог {signed(income + expense)}")
    lines.append(f"За 7 дней: приход {money(week.get('income'))}")
    return "\n".join(lines)


# ------------------------------------------------------------------ картинки
def printer_name(printer, printers: list[dict] | None = None) -> str:
    """Имя принтера из живого объекта: `record['name']`, свойство или снимок."""
    record = getattr(printer, "record", None)
    name = ""
    if isinstance(record, dict):
        name = str(record.get("name") or "").strip()
    if not name:
        name = str(getattr(printer, "name", "") or "").strip()
    if not name:
        pid = str(getattr(printer, "id", "") or "")
        name = next((str(p.get("name")) for p in (printers or []) if str(p.get("id")) == pid), "")
    return name or "Принтер"


def camera_caption(name: str, printers: list[dict] | None = None) -> str:
    """Подпись к кадру камеры: чей кадр и что он делает."""
    printer = next((p for p in (printers or []) if str(p.get("name")) == str(name)), None)
    if not printer:
        return limit(f"🖨 Кадр с камеры: {name or 'принтер'}", CAPTION_LIMIT)
    if not printer.get("connected"):
        return limit(f"🖨 {name} — не в сети, кадр последний удачный", CAPTION_LIMIT)
    label = state_label(printer.get("state"))
    tail = []
    try:
        percent = int(round(float(printer.get("progress") or 0)))
    except (TypeError, ValueError):
        percent = 0
    if percent and printer.get("state") in ("RUNNING", "PREPARE"):
        tail.append(f"{percent}%")
    try:
        left = int(round(float(printer.get("remaining_min") or 0)))
    except (TypeError, ValueError):
        left = 0
    if left > 0:
        tail.append(f"~{left} мин")
    return limit(f"🖨 {name} — {label}" + (f" · {' · '.join(tail)}" if tail else ""), CAPTION_LIMIT)


def camera_absent_text(printers: list[dict]) -> str:
    """Кадр попросили, а камеры нет — объясняем, почему, а не молчим."""
    if not printers:
        return ("📷 Кадр недоступен: принтеров нет. Добавьте станок в панели — "
                "и камера появится здесь же.")
    names = ", ".join(str(p.get("name")) for p in printers if p.get("name"))
    return ("📷 Кадр недоступен: камера не отдала ни одного кадра"
            + (f" ({names})" if names else "")
            + ".\nПроверьте, что телефон в сети цеха или включён Tailscale; "
              "живое видео — в Mini App и панели.")
