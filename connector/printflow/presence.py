"""Кассы на связи: кто молчит и когда об этом сказать владельцу (17.0.13).

До этого раунда панель и владелец узнавали об обрыве у кассы только от кассира
(«не видно кассу»). Presence живёт в ``cashier_tokens.last_seen``: коннектор
отмечает каждый запрос кассы, панель показывает список, а этот сторож следит,
чтобы молчание телефона не осталось незамеченным.

Правила, чтобы не превратиться в спам:

* порог в минутах — настройка ``cashier_offline_alert_min``; 0 = выключено
  (телефон на ночь уносят домой, и «касса офлайн» в 23:00 никому не нужна);
* об одной и той же кассе сообщаем **один раз** за эпизод молчания: вернулась
  на связь — эпизод закрыт, следующее сообщение только после нового обрыва;
* истёкшая сессия (12 часов, смена закончилась) — не «обрыв», а конец смены,
  про неё молчим;
* сообщение уходит в фоне, поток ожидания его не ждёт.
"""
from __future__ import annotations

import threading
import time
from typing import Any

# Как часто заглядывать в базу. Отметки пишутся раз в 20 с, панель считает
# кассу «на связи» по минуте — чаще смотреть смысла нет.
INTERVAL = 30.0


class CashierWatcher:
    """Фоновый сторож: «касса молчит дольше порога» → одно сообщение."""

    def __init__(self, cashier: Any, manager: Any, db: Any) -> None:
        self.cashier = cashier
        self.manager = manager
        self.db = db
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._alerted: set[str] = set()

    # ------------------------------------------------------------------ жизнь
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="pf-cashier-watch",
                                        daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    def threshold_seconds(self) -> float:
        """Порог молчания в секундах. 0 — сторож выключен настройкой."""
        try:
            minutes = float(self.db.setting("cashier_offline_alert_min", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, minutes) * 60.0

    def check(self) -> list[str]:
        """Один проход: кого оповестить. Возвращает имена касс (для тестов)."""
        threshold = self.threshold_seconds()
        if threshold <= 0:
            self._alerted.clear()
            return []
        report = self.cashier.sessions()
        alive: set[str] = set()
        said: list[str] = []
        for item in report.get("sessions", []):
            key = str(item.get("id") or "")
            if not key:
                continue
            if item.get("expires_at") and self._expired(item):
                continue           # смена кончилась — это не обрыв
            alive.add(key)
            silent = item.get("silent_seconds")
            if silent is None:
                continue           # касса ни разу не откликалась — сказать нечего
            if item.get("online"):
                self._alerted.discard(key)
                continue
            if float(silent) < threshold or key in self._alerted:
                continue
            self._alerted.add(key)
            said.append(str(item.get("name") or "касса"))
            self._notify(str(item.get("name") or "касса"), float(silent))
        self._alerted &= alive    # пропавшие из базы сессии — забыть
        return said

    def _notify(self, name: str, silent: float) -> None:
        minutes = max(1, int(round(silent / 60.0)))
        text = (f"📵 Касса «{name}» молчит {minutes} мин — телефон без связи с ПК. "
                f"Продажи идут, но в очередь: проверьте Wi-Fi и коннектор.")
        try:
            self.manager.notify_async(text, event="cashier_offline")
        except Exception:
            pass

    @staticmethod
    def _expired(item: dict) -> bool:
        from datetime import datetime, timezone
        try:
            deadline = datetime.fromisoformat(str(item.get("expires_at") or ""))
        except ValueError:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline.timestamp() < time.time()

    # ------------------------------------------------------------------- цикл
    def _loop(self) -> None:
        while not self._stop.wait(INTERVAL):
            try:
                self.check()
            except Exception:
                try:
                    from .logging_setup import log

                    log().exception("Сторож касс: проход не удался")
                except Exception:
                    pass
