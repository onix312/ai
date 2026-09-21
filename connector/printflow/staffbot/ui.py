"""UI бота — клавиатуры и тексты, без бизнеса и без сети.

Бот тонкий: только уведомления + кнопка «Открыть цех» web_app.
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
    "Откройте цех кнопкой ниже — там полка, касса, очередь, принтеры, заказы, inbox, деньги.\n"
    "Я пришлю важное сам: печать, низкий остаток, кассу, заказы.\n\n"
    "Команды: /start, /help, /code — ваш chat_id для владельца."
)

# Совместимость со старыми тестами: нижняя панель → канонические команды
REPLY_ALIASES: dict[str, str] = {
    "🛒 продать": "меню",
    "📦 полка": "меню",
    "💰 касса": "меню",
    "📷 кадр": "меню",
    "📊 итоги": "меню",
    "⚙️ ещё": "меню",
    "⚙️ еще": "меню",
    "ещё": "меню",
    "еще": "меню",
    "more": "меню",
    "продажа": "меню",
    "стеллаж": "меню",
    "полка": "меню",
    "касса": "меню",
    "принтеры": "меню",
    "очередь": "меню",
    "заказы": "меню",
    "деньги": "меню",
}


def web_app_keyboard(url: str, extra_rows: list[list[dict]] | None = None) -> dict:
    """Клавиатура с кнопкой web_app «Открыть цех» + опциональные строки."""
    url = (url or "").strip() or "https://example.com/staff"
    rows: list[list[dict]] = [[{"text": "🏭 Открыть цех", "web_app": {"url": url}}]]
    if extra_rows:
        rows.extend(extra_rows)
    return {"inline_keyboard": rows}


def main_menu_keyboard(url: str) -> dict:
    """Главное меню — только кнопка цеха и помощь."""
    return web_app_keyboard(
        url,
        extra_rows=[
            [
                {"text": "❔ Помощь", "callback_data": "cmd:help"},
                {"text": "🆔 Мой код", "callback_data": "cmd:code"},
            ]
        ],
    )


def help_keyboard(url: str) -> dict:
    return web_app_keyboard(url)


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
