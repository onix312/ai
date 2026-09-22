"""Конфиг Telegram-бота сотрудников — чистый слой.

Читает настройки из db.settings(include_secrets=True), не знает про транспорт.

18.12.2: адрес Mini App цеха. Раньше при незаполненном адресе функция молча
отдавала ``https://example.com/staff``, и кнопка «🏭 Открыть цех» вела на
страницу «Example Domain» — владелец видел именно её и не понимал, что
настраивать. Теперь незаполненный адрес возвращает пустую строку, а бот
объясняет, что сделать (см. ``MINIAPP_HINT`` и ``miniapp_state``).
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any


@dataclass
class TelegramConfig:
    enabled: bool
    token: str
    chat_id: str
    public_url: str
    miniapp_url: str
    digest_time: str = "09:00"
    evening_time: str = "20:00"
    weekly_day: int = 1
    weekly_time: str = "20:00"


# Инструкция вместо мёртвой кнопки: короткая, по шагам, с путём в панели.
MINIAPP_HINT = (
    "🏭 Mini App цеха пока не открывается — адрес не настроен.\n\n"
    "Mini App — это страница цеха внутри Telegram (полка, касса, очередь, "
    "принтеры, заказы, деньги). Telegram открывает её только по адресу "
    "https://…, поэтому панели нужен внешний HTTPS-адрес.\n\n"
    "1. Дайте панели HTTPS-адрес: docs/MINIAPP-ЦЕХА.md (Cloudflare Tunnel, "
    "Tailscale или свой домен).\n"
    "2. Панель → Настройки → Telegram → «Адрес Mini App цеха» — вставьте "
    "адрес именно цеха (например https://ceh.example.ru/staff) и сохраните. "
    "Можно заполнить и «Публичный адрес панели» — тогда /staff добавится сам.\n"
    "3. Вернитесь в этот чат и нажмите «Меню» — кнопка «🏭 Открыть цех» "
    "появится."
)

# Причины, по которым кнопку web_app отдавать нельзя.
REASON_NOT_SET = "not_set"
REASON_NOT_HTTPS = "not_https"


def _s(db) -> dict[str, Any]:
    try:
        return db.settings(include_secrets=True) or {}
    except Exception:
        return {}


def get_token(db) -> str:
    return str(_s(db).get("telegram_token") or "").strip()


def get_chat_id(db) -> str:
    return str(_s(db).get("telegram_chat_id") or "").strip()


def normalize_miniapp_url(value: Any) -> str:
    """Привести адрес цеха к виду ``https://host[:port][/path]`` без хвостового слэша.

    Адрес без схемы («ceh.example.ru») считается HTTPS: Telegram других для
    Mini App не принимает, и догадка здесь всегда в одну сторону.
    Пустая строка или мусор → ``""``.
    """
    raw = str(value or "").strip().strip('"').strip("'").rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def get_miniapp_url(db) -> str:
    """URL кнопки web_app — или пустая строка, если адрес не настроен.

    Приоритет: ``staff_miniapp_url`` (прямой адрес цеха) → ``miniapp_url``
    (старое имя ключа) → ``public_url``/``base_url``/``app_url`` + ``/staff``.
    Хвостовой ``/staff`` у прямого адреса не удваивается.
    """
    s = _s(db)
    for key in ("staff_miniapp_url", "miniapp_url"):
        direct = normalize_miniapp_url(s.get(key))
        if direct:
            return direct
    for key in ("public_url", "base_url", "app_url"):
        base = normalize_miniapp_url(s.get(key))
        if base:
            return base if base.endswith("/staff") else base + "/staff"
    return ""


def miniapp_reason(url: str) -> str:
    """Почему кнопку отдавать нельзя: ``""`` — всё хорошо."""
    if not url:
        return REASON_NOT_SET
    if urllib.parse.urlparse(url).scheme != "https":
        return REASON_NOT_HTTPS
    return ""


def miniapp_state(db) -> dict:
    """Одна точка правды для бота, уведомлений и панели.

    ``ready`` — Telegram примет такую кнопку web_app; ``problem`` — короткое
    объяснение для человека (пусто, когда всё в порядке).
    """
    url = get_miniapp_url(db)
    reason = miniapp_reason(url)
    problem = ""
    if reason == REASON_NOT_SET:
        problem = ("Адрес Mini App не задан: Настройки → Telegram → "
                   "«Адрес Mini App цеха» (нужен адрес https://…)")
    elif reason == REASON_NOT_HTTPS:
        problem = (f"Адрес Mini App — {url}, а Telegram открывает Mini App "
                   "только по https://")
    return {"url": url, "ready": not reason, "reason": reason, "problem": problem}


def miniapp_hint(db) -> str:
    """Текст-инструкция для бота: что сделать, чтобы кнопка заработала."""
    state = miniapp_state(db)
    head = f"⚠ {state['problem']}.\n\n" if state["problem"] else ""
    return head + MINIAPP_HINT


def load_config(db) -> TelegramConfig:
    s = _s(db)
    enabled = bool(s.get("telegram_enabled") and s.get("telegram_bot") and s.get("telegram_token") and s.get("telegram_chat_id"))
    token = str(s.get("telegram_token") or "").strip()
    chat_id = str(s.get("telegram_chat_id") or "").strip()
    public_url = str(s.get("public_url") or s.get("base_url") or "").strip()
    miniapp_url = get_miniapp_url(db)
    digest_time = str(s.get("digest_time") or "09:00").strip() or "09:00"
    evening_time = str(s.get("evening_chart_time") or "20:00").strip() or "20:00"
    try:
        weekly_day = int(s.get("weekly_report_day") or 1)
    except Exception:
        weekly_day = 1
    weekly_time = str(s.get("weekly_report_time") or "20:00").strip() or "20:00"
    return TelegramConfig(
        enabled=enabled,
        token=token,
        chat_id=chat_id,
        public_url=public_url,
        miniapp_url=miniapp_url,
        digest_time=digest_time,
        evening_time=evening_time,
        weekly_day=weekly_day,
        weekly_time=weekly_time,
    )
