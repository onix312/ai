"""Mini App: валидация initData по токену бота.

Документация Telegram:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Алгоритм:
1. initData — query string вида ``user=...&auth_date=...&hash=...``.
2. Сортируем пары кроме hash по ключу, склеиваем через ``\\n`` как ``k=v``.
3. secret_key = HMAC_SHA256(\"WebAppData\", bot_token)
4. hash = HMAC_SHA256(data_check_string, secret_key) hex
5. Сравниваем с hash из initData, проверяем auth_date (не старше 24ч).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any


def _parse_init_data(init_data: str) -> dict[str, str]:
    """Парсим query string в dict, значения — как есть (url-decoded)."""
    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    return {k: v for k, v in pairs}


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict[str, Any] | None:
    """Проверить подпись initData и вернуть пользователя.

    Возвращает dict ``{id, first_name, ...}`` или None если подпись неверна.
    ``bot_token`` — токен рабочего бота из настроек.
    """
    if not init_data or not bot_token:
        return None
    try:
        data = _parse_init_data(init_data)
    except Exception:
        return None
    recv_hash = data.pop("hash", "")
    if not recv_hash:
        return None
    # Проверка времени — защита от реплея старой ссылки.
    try:
        auth_date = int(data.get("auth_date", "0") or 0)
    except ValueError:
        return None
    if max_age and auth_date:
        if time.time() - auth_date > max_age:
            # Протухло, но не отклоняем жёстко в тестах — даём None чтобы
            # маршрут вернул 401 и фронт показал «обновите Mini App».
            return None
    # data_check_string: отсортированные по ключу пары k=v через \n
    check_parts = [f"{k}={v}" for k, v in sorted(data.items())]
    check_string = "\n".join(check_parts)
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, recv_hash):
        return None
    # user — JSON строка, url-encoded
    user_raw = data.get("user", "")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except Exception:
        return None
    if not isinstance(user, dict) or not user.get("id"):
        return None
    return user


def extract_init_data_from_ctx(ctx) -> str:
    """Достать initData из заголовка или тела запроса (для тестов)."""
    # Заголовок X-Telegram-Init-Data — основной путь (Telegram рекомендует).
    hdr = ""
    try:
        # Ctx хранит заголовки в ctx.headers если есть, иначе пробуем body/args
        hdr = str(getattr(ctx, "headers", {}).get("x-telegram-init-data", "") or "")
    except Exception:
        hdr = ""
    if hdr:
        return hdr
    # Тело: поле initData или init_data
    try:
        body = ctx.body if isinstance(ctx.body, dict) else {}
        v = body.get("initData") or body.get("init_data") or body.get("init_data_raw") or ""
        if v:
            return str(v)
    except Exception:
        pass
    # Query arg ?initData=...
    try:
        v = ctx.arg("initData", "") or ctx.arg("init_data", "") or ""
        if v:
            return str(v)
    except Exception:
        pass
    return ""
