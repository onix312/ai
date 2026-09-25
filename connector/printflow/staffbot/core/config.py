"""Конфиг Telegram-бота сотрудников — чистый слой.

Читает настройки из db.settings(include_secrets=True), не знает про транспорт.

19.0: Mini App убран. Бот отвечает сам — кнопками и текстом с фото, а вопросы
по цеху разбирает мозг помощника (`assistant_brain`). Внешний HTTPS-адрес
больше не нужен ни для чего: адресат `public_url` остался только для ссылок
на панель и страницу заказа.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TelegramConfig:
    enabled: bool
    token: str
    chat_id: str
    public_url: str
    digest_time: str = "09:00"
    evening_time: str = "20:00"
    weekly_day: int = 1
    weekly_time: str = "20:00"


def _s(db) -> dict[str, Any]:
    try:
        return db.settings(include_secrets=True) or {}
    except Exception:
        return {}


def get_token(db) -> str:
    return str(_s(db).get("telegram_token") or "").strip()


def get_chat_id(db) -> str:
    return str(_s(db).get("telegram_chat_id") or "").strip()


def get_public_url(db) -> str:
    """Публичный адрес панели для ссылок в тексте (может быть пустым)."""
    return str(_s(db).get("public_url") or _s(db).get("base_url") or "").strip()


def load_config(db) -> TelegramConfig:
    s = _s(db)
    enabled = bool(s.get("telegram_enabled") and s.get("telegram_bot") and s.get("telegram_token") and s.get("telegram_chat_id"))
    token = str(s.get("telegram_token") or "").strip()
    chat_id = str(s.get("telegram_chat_id") or "").strip()
    public_url = str(s.get("public_url") or s.get("base_url") or "").strip()
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
        digest_time=digest_time,
        evening_time=evening_time,
        weekly_day=weekly_day,
        weekly_time=weekly_time,
    )
