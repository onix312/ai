"""Единое состояние каналов связи с принтером (17.0.19).

Раньше «принтер на связи» собирался из последнего MQTT-кадра: FTPS мог лежать,
а панель показывала «online», и оператор жал «Скачать файл» в пустоту. Здесь
каждый канал считается отдельно — MQTT, FTPS, HTTP/облако, камера — и у каждого
свои четыре факта:

- когда в последний раз получилось;
- когда в последний раз не получилось и почему;
- сколько раз подряд не получалось;
- когда будет следующая попытка.

Общего «online» намеренно нет: сводка называет, какой именно канал молчит и что
оператору безопасно сделать. Печать при мёртвом FTPS возможна, файлы — нет, и
это разные сообщения.

Таблица `printer_links` (схема — `schema_v3.py`) не касается денег и заданий:
это только наблюдение, удалить строку канала — значит просто начать считать
заново.

Пороги «новостей давно не было» подобраны для принтера Bambu в локальной сети и
меняются настройками `link_stale_<канал>` без правки кода.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import now_iso

# канал -> (человеческое имя, порог «давно не было вестей» в секундах,
#           что ломается для оператора, когда канал молчит)
CHANNELS: dict[str, tuple[str, int, str]] = {
    "mqtt": ("MQTT", 60, "статус печати и команды не доходят"),
    "ftps": ("FTPS", 900, "файлы и SD-карта недоступны"),
    "http": ("HTTP и облако", 300, "облачные команды и камера могут не работать"),
    "camera": ("Камера", 900, "превью печати не обновляется"),
}

# Что безопасно сделать, когда канал молчит. Опасных советов нет: перезапуск
# принтера посреди печати — не «безопасное действие».
ACTIONS: dict[str, str] = {
    "mqtt": "Проверьте, что принтер в той же сети, и переподключите его в разделе «Печать»",
    "ftps": "Проверьте Access Code и доступ по FTPS в настройках принтера",
    "http": "Проверьте адрес и порт принтера; при работе через облако — доступ в интернет",
    "camera": "Проверьте, включена ли камера в настройках принтера",
}

STATE_LABELS = {
    "never": "ещё не проверяли",
    "ok": "на связи",
    "stale": "давно не было вестей",
    "down": "не отвечает",
}


def link_id(printer_id: str, channel: str) -> str:
    """Первичный ключ строки канала: `upsert` в базе работает по одному ключу."""
    return f"{printer_id}::{channel}"


class ConnectionState:
    """Хранит и разбирает состояние каналов одного или всех принтеров."""

    def __init__(self, db: Any, now: Callable[[], str] = now_iso) -> None:
        self.db = db
        self._now = now

    # ------------------------------------------------------------ запись
    def mark_ok(self, printer_id: str, channel: str) -> None:
        """Канал ответил. Счётчик неудач обнуляется — успех всё перечёркивает."""
        if channel not in CHANNELS or not printer_id:
            return
        stamp = self._now()
        self.db.upsert("printer_links", {
            "link_id": link_id(printer_id, channel),
            "printer_id": printer_id, "channel": channel, "state": "ok",
            "last_ok_at": stamp, "last_error": "", "attempts": 0,
            "next_retry_at": "", "updated_at": stamp,
        }, key="link_id")

    def mark_fail(self, printer_id: str, channel: str, reason: str,
                  retry_in: int = 30) -> None:
        """Канал не ответил. Причина обязательна: без неё оператор гадает."""
        if channel not in CHANNELS or not printer_id:
            return
        stamp = self._now()
        row = self.row(printer_id, channel)
        attempts = int((row or {}).get("attempts") or 0) + 1
        self.db.upsert("printer_links", {
            "link_id": link_id(printer_id, channel),
            "printer_id": printer_id, "channel": channel, "state": "down",
            "last_fail_at": stamp,
            "last_error": str(reason or "причина не сообщена")[:300],
            "attempts": attempts,
            "next_retry_at": _shift(stamp, max(1, int(retry_in))),
            "updated_at": stamp,
        }, key="link_id")

    # ------------------------------------------------------------ чтение
    def row(self, printer_id: str, channel: str) -> dict | None:
        row = self.db.one("SELECT * FROM printer_links WHERE link_id=?",
                          (link_id(printer_id, channel),))
        return row or None

    def stale_seconds(self, channel: str) -> int:
        """Порог «давно не было вестей»: настройка важнее значения по умолчанию."""
        default = CHANNELS[channel][1]
        try:
            value = int(self.db.setting(f"link_stale_{channel}", default) or default)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    def describe(self, printer_id: str, channel: str) -> dict:
        """Состояние одного канала словами и фактами."""
        title, _threshold, breaks = CHANNELS[channel]
        row = self.row(printer_id, channel)
        stamp = self._now()
        base = {
            "channel": channel, "title": title, "breaks": breaks,
            "state": "never", "label": STATE_LABELS["never"],
            "last_ok_at": "", "last_fail_at": "", "last_error": "",
            "attempts": 0, "next_retry_at": "", "silent_for": None,
            "action": "",
        }
        if not row:
            return base
        base.update({
            "last_ok_at": row.get("last_ok_at") or "",
            "last_fail_at": row.get("last_fail_at") or "",
            "last_error": row.get("last_error") or "",
            "attempts": int(row.get("attempts") or 0),
            "next_retry_at": row.get("next_retry_at") or "",
        })
        if (row.get("state") or "never") == "down":
            base["state"] = "down"
            base["action"] = ACTIONS[channel]
        elif base["last_ok_at"]:
            silent = _age_seconds(base["last_ok_at"], stamp)
            base["silent_for"] = silent
            if silent is not None and silent > self.stale_seconds(channel):
                base["state"] = "stale"
                base["last_error"] = base["last_error"] or "канал молчит"
                base["action"] = ACTIONS[channel]
            else:
                base["state"] = "ok"
        base["label"] = STATE_LABELS[base["state"]]
        return base

    def snapshot(self, printer_id: str) -> dict:
        """Все каналы принтера + сводка, которая ничего не склеивает в «online»."""
        channels = [self.describe(printer_id, channel) for channel in CHANNELS]
        known = [c for c in channels if c["state"] != "never"]
        alive = [c for c in known if c["state"] == "ok"]
        bad = [c for c in known if c["state"] in ("down", "stale")]
        if not known:
            verdict = "не проверяли"
            summary = "Связь с принтером ещё не проверялась."
        elif not bad:
            verdict = "все каналы живы"
            summary = "Все проверенные каналы отвечают."
        elif not alive:
            verdict = "нет связи"
            summary = ("Молчит всё, что проверяли: "
                       + ", ".join(f"{c['title']} ({c['label']})" for c in bad) + ".")
        else:
            verdict = "частично"
            summary = ("Работает " + ", ".join(c["title"] for c in alive)
                       + "; молчит "
                       + ", ".join(f"{c['title']} ({c['label']})" for c in bad) + ".")
        return {
            "printer_id": printer_id,
            "verdict": verdict,
            "summary": summary,
            "action": bad[0]["action"] if bad else "",
            "channels": channels,
        }

    def require_printer(self, printer_id: str) -> str:
        """Проверить, что принтер существует.

        Запрос живёт в сервисе, а не в модуле маршрутов: контракт
        `test_router.RoutesHaveNoSqlTests` не пускает SQL в транспортный слой.
        """
        row = self.db.one("SELECT id FROM printers WHERE id=?", (printer_id,))
        if not row:
            raise ValueError("Принтер не найден")
        return printer_id

    def all_printers(self) -> list[dict]:
        return [self.snapshot(row["id"]) for row in self.db.query(
            "SELECT id FROM printers WHERE COALESCE(enabled,1)=1"
            " ORDER BY position, id")]


def _parse(stamp: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def _shift(stamp: str, seconds: int) -> str:
    moment = _parse(stamp)
    if moment is None:
        return ""
    return (moment + timedelta(seconds=seconds)).isoformat()


def _age_seconds(stamp: str, now_stamp: str) -> int | None:
    moment, now = _parse(stamp), _parse(now_stamp)
    if moment is None or now is None:
        return None
    return max(0, int((now - moment).total_seconds()))
