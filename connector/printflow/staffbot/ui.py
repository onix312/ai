"""UI бота — клавиатуры и тексты, без бизнеса и без сети.

19.0: Mini App убран — у бота нет кнопки web_app, всё меню на callback-кнопках.
Каждая кнопка отвечает в чате: сводка, принтеры, заказы, полка, очередь, кадр,
деньги (по роли) и ассистент — мозг помощника отвечает на вопросы словами.
"""
from __future__ import annotations

from .. import APP_VERSION

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

HELP = (
    f"PrintFlow {APP_VERSION} — цех в кармане.\n\n"
    "Всё меню — кнопки под сообщением: статус, принтеры, заказы, полка, "
    "очередь, кадр. Я пришлю важное сам: печать, низкий остаток, кассу, заказы.\n\n"
    "🤖 Ассистент отвечает на вопросы про цех словами — спросите "
    "«что печатает P1S», «кто нам должен», «сколько заказов в работе».\n"
    "Можно и просто написать вопрос сообщением — пойму и отвечу.\n\n"
    "Команды: /start, /help, /code — ваш chat_id для владельца."
)

# Совместимость со старыми тестами: нижняя панель → канонические команды
REPLY_ALIASES: dict[str, str] = {
    "🛒 продать": "меню",
    "📦 полка": "полка",
    "💰 касса": "деньги",
    "📷 кадр": "кадр",
    "📊 итоги": "деньги",
    "⚙️ ещё": "меню",
    "⚙️ еще": "меню",
    "ещё": "меню",
    "еще": "меню",
    "more": "меню",
}


def markup_or_none(keyboard: dict | None) -> dict | None:
    """Клавиатура для Telegram или None, если в ней нет ни одного ряда.

    Пустой ``inline_keyboard`` Telegram отклоняет, и сообщение не уходит.
    """
    rows = (keyboard or {}).get("inline_keyboard") or []
    return keyboard if rows else None


# Меню на кнопках (19.0): каждая кнопка имеет своего обработчика в чате —
# ни одна не требует внешнего адреса и ни одна не ведёт «в никуда».
REPORT_ROWS: list[list[dict]] = [
    [
        {"text": "📊 Статус", "callback_data": "cmd:status"},
        {"text": "🖨 Принтеры", "callback_data": "cmd:printers"},
    ],
    [
        {"text": "📦 Заказы", "callback_data": "cmd:orders"},
        {"text": "🛒 Полка", "callback_data": "cmd:shelf"},
    ],
    [
        {"text": "🧾 Очередь", "callback_data": "cmd:queue"},
        {"text": "📷 Кадр", "callback_data": "cmd:frame"},
    ],
]

ASSISTANT_ROW: list[dict] = [{"text": "🤖 Ассистент", "callback_data": "cmd:ask"}]


def main_menu_keyboard(role: str = "", url: str = "") -> dict:
    """Главное меню — ассистент, текстовые отчёты, деньги по роли.

    `url` остался в подписи для совместимости со старыми вызовами и
    игнорируется: кнопки web_app в боте больше нет.
    """
    rows: list[list[dict]] = []
    if role in ("owner", "manager"):
        rows.append(list(ASSISTANT_ROW))
    rows.extend(list(row) for row in REPORT_ROWS)
    if role in ("owner", "manager"):
        rows.append([{"text": "💰 Деньги", "callback_data": "cmd:money"}])
    rows.append([
        {"text": "❔ Помощь", "callback_data": "cmd:help"},
        {"text": "🆔 Мой код", "callback_data": "cmd:code"},
    ])
    return {"inline_keyboard": rows}


def help_keyboard(role: str = "", url: str = "") -> dict:
    """Клавиатура помощи: те же кнопки меню, помощь и код."""
    return main_menu_keyboard(role)


def ask_keyboard(url: str = "") -> dict:
    """Клавиатура в режиме ассистента: выход в меню, помощь."""
    return {
        "inline_keyboard": [
            [{"text": "🏠 Меню", "callback_data": "cmd:menu"}],
        ]
    }


def keyboard(*rows: list[tuple[str, str]]) -> dict:
    """Совместимый helper: [(text, callback_data)] → inline_keyboard."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


def paginate(rows: list, page: int, per_page: int = 8) -> tuple[list, int, int]:
    per_page = max(1, int(per_page))
    total = len(rows)
    total_pages = max(1, -(-total // per_page))
    page = max(0, min(int(page), total_pages - 1))
    start = page * per_page
    return rows[start : start + per_page], page, total_pages
