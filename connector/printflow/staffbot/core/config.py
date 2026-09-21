"""Конфиг Telegram-бота сотрудников — чистый слой.

Читает настройки из db.settings(include_secrets=True), не знает про транспорт.
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
    miniapp_url: str
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


def get_miniapp_url(db) -> str:
    """URL для кнопки web_app.

    Приоритет: miniapp_url → public_url + /staff → base_url + /staff → fallback.
    """
    s = _s(db)
    for key in ("miniapp_url", "staff_miniapp_url", "public_url", "base_url", "app_url"):
        v = str(s.get(key) or "").strip()
        if not v:
            continue
        if key == "miniapp_url" or key == "staff_miniapp_url":
            return v.rstrip("/")
        # public_url/base_url → добавляем /staff
        return v.rstrip("/") + "/staff"
    # fallback для тестов и локальной разработки
    return "https://example.com/staff"


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
