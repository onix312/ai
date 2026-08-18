"""Шина событий: сервер сам сообщает браузеру, что изменилось.

Раньше интерфейс опрашивал сервер каждые 2.5 секунды и каждый раз забирал
состояние целиком, даже когда ничего не происходило. Шина переворачивает
схему: кто-то из модулей публикует событие — все открытые вкладки получают
его сразу через SSE (``/api/stream``).

Правила простые и защищают от подвисших вкладок:

* у каждого подписчика своя очередь ограниченного размера;
* если вкладка не успевает читать (уснувший ноутбук, спящий телефон), её
  очередь не растёт бесконечно — она очищается и получает одно событие
  ``resync``: «перезагрузи данные целиком»;
* отписка гарантирована блоком ``with bus.subscription()``.
"""
from __future__ import annotations

import queue
import threading
from contextlib import contextmanager
from typing import Iterator

# Сколько событий держим для медленного подписчика, прежде чем сказать
# «перечитай всё». 64 — это примерно минута активной печати.
QUEUE_LIMIT = 64


class Subscriber:
    """Очередь одной вкладки. Отдаёт события по мере поступления."""

    __slots__ = ("queue", "overflow")

    def __init__(self, limit: int = QUEUE_LIMIT) -> None:
        self.queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=limit)
        self.overflow = False

    def put(self, kind: str, payload: object) -> None:
        try:
            self.queue.put_nowait((kind, payload))
        except queue.Full:
            # Вкладка отстала: чистим очередь и просим перечитать данные,
            # иначе она будет догонять устаревшие кадры телеметрии.
            self.drain()
            self.overflow = True
            try:
                self.queue.put_nowait(("resync", {}))
            except queue.Full:
                pass

    def drain(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                return

    def get(self, timeout: float) -> tuple[str, object] | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None


class EventBus:
    """Точка обмена: модули публикуют, вкладки подписываются."""

    def __init__(self, limit: int = QUEUE_LIMIT) -> None:
        self._subscribers: set[Subscriber] = set()
        self._lock = threading.Lock()
        self._limit = limit
        self.published = 0

    def subscribe(self) -> Subscriber:
        subscriber = Subscriber(self._limit)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    @contextmanager
    def subscription(self) -> Iterator[Subscriber]:
        subscriber = self.subscribe()
        try:
            yield subscriber
        finally:
            self.unsubscribe(subscriber)

    def publish(self, kind: str, payload: object = None) -> int:
        """Разослать событие. Возвращает число получателей."""
        with self._lock:
            targets = list(self._subscribers)
        for subscriber in targets:
            subscriber.put(kind, payload if payload is not None else {})
        self.published += 1
        return len(targets)

    @property
    def listeners(self) -> int:
        with self._lock:
            return len(self._subscribers)


class LiveBroadcaster:
    """Один поток на всех: следит за принтерами и рассылает телеметрию.

    Экономика простая. Раньше каждая открытая вкладка дёргала ``/api/state``
    каждые 2.5 секунды, и сервер собирал полный снимок парка на каждый запрос.
    Теперь снимок собирается один раз и только когда принтер действительно
    прислал новые данные (по MQTT обновляется ``last_message``), а вкладки
    получают готовое состояние.

    Когда никто не смотрит — поток спит и не трогает ни базу, ни принтеры.
    """

    IDLE_INTERVAL = 2.0      # ничего не подключено — торопиться некуда
    LIVE_INTERVAL = 0.8      # принтер на связи: обновляем почти мгновенно
    HEARTBEAT = 15.0         # даже без изменений напоминаем о себе

    def __init__(self, bus: EventBus, manager) -> None:
        self.bus = bus
        self.manager = manager
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fingerprint: tuple = ()
        self._sent_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="pf-live", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    def fingerprint(self) -> tuple:
        """Дешёвый признак «что-то поменялось» — без сборки полного снимка."""
        try:
            with self.manager.lock:
                printers = list(self.manager.printers.values())
        except Exception:
            return ()
        marks = []
        for printer in printers:
            marks.append((
                getattr(printer, "id", ""),
                round(float(getattr(printer, "last_message", 0.0) or 0.0), 1),
                bool(getattr(printer, "connected", False)),
                str(getattr(printer, "previous_state", "")),
            ))
        return tuple(marks)

    def interval(self) -> float:
        try:
            with self.manager.lock:
                live = any(getattr(p, "connected", False) for p in self.manager.printers.values())
        except Exception:
            live = False
        return self.LIVE_INTERVAL if live else self.IDLE_INTERVAL

    def _loop(self) -> None:
        import time

        while not self._stop.wait(self.interval()):
            if self.bus.listeners == 0:
                continue
            try:
                mark = self.fingerprint()
                stale = time.time() - self._sent_at > self.HEARTBEAT
                if mark == self._fingerprint and not stale:
                    continue
                snapshot = self.manager.snapshot()
                self._fingerprint = mark
                self._sent_at = time.time()
                self.bus.publish("telemetry", snapshot)
            except Exception:
                try:
                    from .logging_setup import log

                    log().exception("Не удалось разослать телеметрию")
                except Exception:
                    pass
