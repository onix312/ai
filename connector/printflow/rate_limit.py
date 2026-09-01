"""Ограничение частоты запросов (идея 34).

Публичные маршруты (`/api/public/order`, трекинг, витрина) открыты всем:
форма заказа на сайте доступна без доступа к цеху, и ничто не мешало
набить базу тысячами заявок простым циклом. Идемпотентность защищает от
повтора одного и того же запроса, но не от тысячи разных.

Механика намеренно простая и без зависимостей: скользящее окно на
`collections.deque` в памяти процесса. Состояние не переживает рестарт —
для защиты от случайного флуда и ручного перебора этого достаточно, а
настоящий анти-бот живёт на фронтире (прокси/Cloudflare), не в цеховом
коннекторе.

Правила:
  * ключ — (bucket, идентификатор клиента): IP или токен трекинга;
  * превышение — честные `429` и заголовок `Retry-After`;
  * приватные маршруты не ограничиваются: панель дёргает API часто,
    и тормозить собственного оператора незачем.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

# Сколько запросов в окне разрешено каждой корзине.
DEFAULT_RULES: dict[str, tuple[int, int]] = {
    # (лимит, окно в секундах)
    "public_order": (10, 600),       # форма заказа: 10 заявок в 10 минут
    "public_track": (60, 60),        # трекинг: страница обновляется часто
    "public_catalog": (240, 60),     # витрина: каталог листают активно
    "login": (10, 300),              # вход в облако: защита от перебора
    "upload": (30, 600),             # загрузка моделей
    "default": (600, 60),            # прочее: запас на телеметрию панели
}

DEFAULT_BUCKET = "default"


class RateLimiter:
    """Скользящее окно на процесс. Потокобезопасно."""

    def __init__(self, rules: dict[str, tuple[int, int]] | None = None) -> None:
        self.rules = dict(DEFAULT_RULES)
        if rules:
            self.rules.update(rules)
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self._lock = threading.Lock()
        self.rejected = 0
        self.checked = 0

    def rule(self, bucket: str) -> tuple[int, int]:
        return self.rules.get(bucket) or self.rules[DEFAULT_BUCKET]

    def check(self, bucket: str, key: str) -> tuple[bool, dict]:
        """Разрешён ли запрос. Возвращает ``(ok, подробности)``."""
        limit, window = self.rule(bucket)
        now = time.time()
        with self._lock:
            self.checked += 1
            hits = self._hits[(bucket, str(key or "anon"))]
            cutoff = now - window
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= limit:
                self.rejected += 1
                retry_after = max(1, int(window - (now - hits[0])) + 1)
                return False, {
                    "bucket": bucket, "limit": limit, "window": window,
                    "retry_after": retry_after,
                    "error": f"Слишком много запросов. Повторите через {retry_after} с.",
                }
            hits.append(now)
            return True, {"bucket": bucket, "limit": limit, "window": window,
                          "remaining": limit - len(hits)}

    def reset(self, bucket: str = "", key: str = "") -> None:
        """Сбросить счётчики (тесты, ручной сброс после инцидента)."""
        with self._lock:
            if not bucket:
                self._hits.clear()
                return
            self._hits.pop((bucket, str(key or "anon")), None)

    def stats(self) -> dict:
        with self._lock:
            return {"checked": self.checked, "rejected": self.rejected,
                    "tracked": len(self._hits), "rules": dict(self.rules)}


limiter = RateLimiter()


def client_key(headers) -> str:
    """Идентификатор клиента: реальный IP за прокси, иначе адрес соединения."""
    if headers is not None:
        forwarded = headers.get("X-Forwarded-For") or ""
        if forwarded:
            return str(forwarded.split(",")[0]).strip()
        real_ip = headers.get("X-Real-IP") or ""
        if real_ip:
            return str(real_ip).strip()
    return "unknown"
