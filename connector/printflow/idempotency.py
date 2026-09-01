"""Идемпотентность изменяющих запросов (идея 5).

Проблема. Кнопка «Провести приход» на плохом Wi-Fi нажимается дважды,
Telegram-бот ретраит отправку, браузер повторяет POST после обрыва —
и в цехе появляются два прихода, две продажи или два задания в очереди.
Таблица `idempotency_keys` в базе уже была, но пользовался ею только
публичный заказ (`/api/public/order`), а остальные ~200 изменяющих
маршрутов полагались на удачу.

Как работает. Клиент присылает заголовок `Idempotency-Key` (или поле
`request_id` в теле — его уже умеют приход и списание). Сервер:

  1. первый раз выполняет действие и запоминает ответ под этим ключом;
  2. при повторе с тем же ключом возвращает сохранённый ответ, НЕ выполняя
     действие второй раз, и добавляет `"replayed": true`;
  3. ключ живёт `TTL` (по умолчанию сутки), затем чистится.

Ключ обязателен только для маршрутов, помеченных в реестре
`idempotent=True`; для остальных он просто игнорируется, поэтому старые
клиенты продолжают работать без изменений.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

TTL_SECONDS = 24 * 3600
MAX_CACHED = 2000


class IdempotencyStore:
    """Хранилище ключ → ответ. SQLite, чтобы переживать рестарт коннектора."""

    def __init__(self, db, ttl: int = TTL_SECONDS) -> None:
        self.db = db
        self.ttl = ttl
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _cleanup(self) -> None:
        cutoff = time.time() - self.ttl
        try:
            self.db.execute("DELETE FROM idempotency_keys WHERE kind='http' AND created_at < ?",
                            (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(cutoff)),))
            count = self.db.one(
                "SELECT COUNT(*) AS n FROM idempotency_keys WHERE kind='http'")
            if count and int(count["n"]) > MAX_CACHED:
                self.db.execute(
                    "DELETE FROM idempotency_keys WHERE kind='http' AND request_id NOT IN ("
                    " SELECT request_id FROM idempotency_keys WHERE kind='http'"
                    " ORDER BY created_at DESC LIMIT ?)", (MAX_CACHED,))
        except Exception:
            pass

    def get(self, key: str, scope: str) -> tuple[bool, Any]:
        """Есть ли сохранённый ответ для ключа. Возвращает ``(найдено, ответ)``."""
        if not key:
            return False, None
        from .config import now_iso
        with self._lock:
            self._cleanup()
            row = self.db.one(
                "SELECT response FROM idempotency_keys WHERE request_id=? AND kind=?",
                (self._compose(key, scope), "http"))
            self.hits += 1 if row else 0
            self.misses += 0 if row else 1
        if not row:
            return False, None
        try:
            return True, json.loads(row["response"] or "null")
        except json.JSONDecodeError:
            return False, None
        finally:
            _ = now_iso

    def put(self, key: str, scope: str, response: Any) -> None:
        if not key:
            return
        from .config import now_iso
        payload = json.dumps(response, ensure_ascii=False, default=str)
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO idempotency_keys"
                " (request_id, kind, entity_id, response, created_at)"
                " VALUES (?,?,?,?,?)",
                (self._compose(key, scope), "http", scope, payload, now_iso()))

    @staticmethod
    def _compose(key: str, scope: str) -> str:
        return f"http:{scope}:{key}"

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses, "ttl": self.ttl}


def extract_key(body: dict | None, headers) -> str:
    """Ключ идемпотентности из заголовка или тела запроса."""
    if headers is not None:
        for name in ("Idempotency-Key", "X-Idempotency-Key", "X-Request-Id"):
            value = headers.get(name)
            if value:
                return str(value).strip()[:128]
    if isinstance(body, dict):
        for name in ("request_id", "idempotency_key"):
            value = body.get(name)
            if value:
                return str(value).strip()[:128]
    return ""


def guarded(store: IdempotencyStore, scope: str, key: str,
            action: Callable[[], Any]) -> tuple[Any, bool]:
    """Выполнить действие ровно один раз для данного ключа.

    Возвращает ``(ответ, повтор_ли)``.
    """
    if not key:
        return action(), False
    found, cached = store.get(key, scope)
    if found:
        if isinstance(cached, dict):
            cached = {**cached, "replayed": True}
        return cached, True
    result = action()
    store.put(key, scope, result)
    return result, False
