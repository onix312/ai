"""Оформление сообщений бота сотрудников: форматы, тексты, клавиатуры.

Здесь нет ни бизнеса, ни Telegram-API — только «как выглядит ответ»:
часы «2 ч 15 мин», деньги «12 500 ₽», строки состояний принтера и сборки
inline-клавиатур. Отдельные кнопки конкретных экранов живут рядом со своими
сценариями (sales/catalog/…), здесь — общие для всех.
"""
from __future__ import annotations

from .. import APP_VERSION
from ..accounting import num

# Состояния принтера на языке цеха. Английские коды Bambu остаются
# служебными, человек читает русскую строку.
STATE_RU = {
    "RUNNING": "печатает", "IDLE": "свободен", "PAUSE": "на паузе",
    "PAUSED": "на паузе", "FINISH": "печать завершена", "PREPARE": "готовится",
    "FAILED": "ошибка", "OFFLINE": "не в сети", "UNKNOWN": "нет данных",
}

HELP = f"""PrintFlow {APP_VERSION} — цех в кармане.

Все действия — кнопками ниже:
🛒 Продать · 📦 Полка · 💰 Касса · 📷 Кадр · 📊 Итоги · ⚙️ Ещё

Если бот попросит цифру, сумму или имя — просто напишите.
Важное я пришлю сам: печать, низкий остаток, кассу, заказы."""

# Кнопки нижней панели главного меню → команда диспетчера. Кнопка — основной
# интерфейс; текстовые команды остаются скрытым способом для опытных.
REPLY_ALIASES: dict[str, str] = {
    "🛒 продать": "продажа", "продать": "продажа",
    "📦 полка": "стеллаж", "полка": "стеллаж",
    "💰 касса": "касса", "касса": "касса",
    "📷 кадр": "кадр", "кадр": "кадр",
    "📊 итоги": "день", "итоги": "день",
    "⚙️ ещё": "more", "⚙️ еще": "more",
    "ещё": "more", "еще": "more", "more": "more",
}


def hm(minutes: float) -> str:
    """Минуты → «2 ч 15 мин»: так читается быстрее, чем «135 мин»."""
    total = int(max(0, num(minutes)))
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


def money(value: float) -> str:
    return f"{round(num(value)):,}".replace(",", " ") + " ₽"


def keyboard(*rows: list[tuple[str, str]]) -> dict:
    """Inline-клавиатура: [[(текст, callback_data)], ...]."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


def inline_rows(buttons: list[list[dict]]) -> dict:
    """Inline-клавиатура из уже готовых кнопок-словарей."""
    return {"inline_keyboard": buttons}


def home_row(label: str = "🏠 В меню") -> list[dict]:
    """Кнопка возврата в главное меню — должна быть на каждом экране."""
    return [{"text": label, "callback_data": "cmd:menu"}]


def nav_row(prefix: str, page: int, total_pages: int,
            label: str = "") -> list[dict]:
    """Строка листания «◀ 2/5 ▶» для inline-списков."""
    head = f"{label} " if label else ""
    return [
        {"text": "◀", "callback_data": f"cmd:{prefix}:prev"},
        {"text": f"{head}{page + 1}/{total_pages}", "callback_data": f"cmd:{prefix}"},
        {"text": "▶", "callback_data": f"cmd:{prefix}:next"},
    ]


def paginate(rows: list, page: int, per_page: int = 8) -> tuple:
    """Нарезать список на страницы; вернуть (срез, page, total_pages)."""
    per_page = max(1, int(per_page))
    total = len(rows)
    total_pages = max(1, -(-total // per_page))  # округление вверх
    page = max(0, min(int(page), total_pages - 1))
    start = page * per_page
    return rows[start:start + per_page], page, total_pages


def page_from_command(command: str, prefix: str, state: dict,
                      chat: str) -> int:
    """Страница пагинации из callback «prefix:next/prev/число»."""
    part = command[len(prefix):].lstrip(":")
    if part in ("next", "вперёд"):
        return state.get(chat, 0) + 1
    if part in ("prev", "назад"):
        return state.get(chat, 0) - 1
    try:
        return max(0, int(num(part)))
    except (TypeError, ValueError):
        return 0


def money_or(value: float, empty: str = "без цены") -> str:
    """Деньги, если сумма задана, иначе честная заглушка."""
    return money(value) if num(value) else empty
