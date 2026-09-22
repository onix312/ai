"""UI бота — клавиатуры и тексты, без бизнеса и без сети.

Бот тонкий: уведомления + кнопка «Открыть цех» web_app + текстовые отчёты
(18.12.3: «статус», «принтеры», «заказы», «полка», «очередь», «кадр»,
«деньги» отвечают словами и фотографиями, пока Mini App недоступен).
"""
from __future__ import annotations

import urllib.parse

from .. import APP_VERSION
from .core.config import normalize_miniapp_url

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
    "Если кнопки цеха нет (адрес Mini App ещё не настроен) — спросите текстом, "
    "я отвечу словами и фото:\n"
    "статус · принтеры · заказы [номер] · полка · очередь · кадр · деньги · код.\n\n"
    "Команды: /start, /help, /code — ваш chat_id для владельца."
)

# Совместимость со старыми тестами: нижняя панель → канонические команды
# Нижняя панель Telegram исторически вела в меню: до 18.12.3 бот умел только
# кнопку web_app. Теперь у слов есть свои ответы («полка» → остатки, «кадр» →
# фото), поэтому в алиасах остались лишь те, у которых ответа нет: продажа,
# закрытие месяца и прочая витрина живут в Mini App.
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


def miniapp_button(url: str, text: str = "🏭 Открыть цех") -> dict | None:
    """Кнопка web_app для адреса цеха; None — Telegram такую не примет.

    Требование Telegram: web_app открывается только по https. Кнопка с
    ``http://`` или пустым адресом роняет ВСЁ сообщение (``BUTTON_URL_INVALID``,
    400), поэтому её отсутствие — не косметика, а условие доставки.
    """
    clean = normalize_miniapp_url(url)
    if not clean or urllib.parse.urlparse(clean).scheme != "https":
        return None
    return {"text": text, "web_app": {"url": clean}}


def web_app_keyboard(url: str, extra_rows: list[list[dict]] | None = None) -> dict:
    """Клавиатура с кнопкой web_app «Открыть цех» + опциональные строки.

    Ненастроенный или не-HTTPS адрес кнопку не добавляет: раньше сюда
    подставлялся ``https://example.com/staff``, и человек попадал на страницу
    «Example Domain» вместо цеха (18.12.2).
    """
    rows: list[list[dict]] = []
    button = miniapp_button(url)
    if button:
        rows.append([button])
    if extra_rows:
        rows.extend(extra_rows)
    return {"inline_keyboard": rows}


def markup_or_none(keyboard: dict | None) -> dict | None:
    """Клавиатура для Telegram или None, если в ней нет ни одного ряда.

    Пустой ``inline_keyboard`` Telegram отклоняет, и сообщение не уходит —
    а без адреса Mini App клавиатура может остаться без рядов.
    """
    rows = (keyboard or {}).get("inline_keyboard") or []
    return keyboard if rows else None


# Текстовые отчёты бота (18.12.3): та же информация, что в Mini App, но прямо
# в чате — словами и фото. Кнопки нужны, когда адрес Mini App ещё не настроен.
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


def report_keyboard(url: str, role: str = "") -> dict:
    """Клавиатура меню и отчётов: цех, текстовые ответы, деньги по роли.

    Кнопка web_app идёт первой — так её видно сразу, если адрес настроен.
    Если адреса нет, `miniapp_button` вернёт None, и меню останется рабочим:
    статус, заказы, полка и кадр отвечают в чате и без внешнего адреса.
    """
    rows: list[list[dict]] = [list(row) for row in REPORT_ROWS]
    if role in ("owner", "manager"):
        rows.append([{"text": "💰 Деньги", "callback_data": "cmd:money"}])
    rows.append([
        {"text": "❔ Помощь", "callback_data": "cmd:help"},
        {"text": "🆔 Мой код", "callback_data": "cmd:code"},
    ])
    return web_app_keyboard(url, extra_rows=rows)


def main_menu_keyboard(url: str, role: str = "") -> dict:
    """Главное меню — кнопка цеха (если адрес готов) и текстовые отчёты."""
    return report_keyboard(url, role)


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
