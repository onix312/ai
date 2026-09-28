"""Proactivity / Event Engine 1.0.

The engine is intentionally notification-only. It never executes skills.
Explicit commitments (reminders/timers) keep their existing guaranteed delivery.
Inferred/proactive events require global Autopilot and pass attention budget,
quiet hours, urgency threshold and dedupe before becoming notifications.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .store import now_iso

MODES = ("silent", "important", "balanced", "active")
URGENCIES = ("low", "normal", "important", "urgent")
DEFAULTS = {
    "mode": "balanced",
    "max_nonurgent_per_hour": 2,
    "cooldown_minutes": 30,
    "quiet_start": "22:00",
    "quiet_end": "08:00",
}
PREFIX = "proactivity."


def _iso(moment: dt.datetime) -> str:
    return moment.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _urgency_rank(value: str) -> int:
    try:
        return URGENCIES.index(str(value or "").strip().casefold())
    except ValueError:
        return URGENCIES.index("normal")


def _valid_hhmm(value: Any) -> bool:
    text = str(value or "")
    try:
        dt.datetime.strptime(text, "%H:%M")
        return len(text) == 5
    except ValueError:
        return False


def _quiet(now: dt.datetime, start: str, end: str) -> bool:
    if not (_valid_hhmm(start) and _valid_hhmm(end)) or start == end:
        return False
    current = now.strftime("%H:%M")
    if start < end:
        return start <= current < end
    return current >= start or current < end


@dataclass(slots=True)
class EventEngine:
    agent: Any

    def __post_init__(self) -> None:
        self.store = self.agent.runner.store
        self.personal = self.agent.runner.personal
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        for statement in (
            """CREATE TABLE IF NOT EXISTS assistant_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                source TEXT NOT NULL,
                event_key TEXT NOT NULL,
                provider TEXT DEFAULT 'core',
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                text TEXT DEFAULT '',
                urgency TEXT DEFAULT 'normal',
                explicit INTEGER DEFAULT 0,
                state TEXT DEFAULT 'new',
                reason TEXT DEFAULT '',
                notification_id INTEGER DEFAULT 0,
                occurrences INTEGER DEFAULT 1,
                payload_json TEXT NOT NULL DEFAULT '{}')""",
            "CREATE INDEX IF NOT EXISTS assistant_events_created ON assistant_events(created_at)",
            "CREATE INDEX IF NOT EXISTS assistant_events_key ON assistant_events(source,event_key)",
            "CREATE INDEX IF NOT EXISTS assistant_events_state ON assistant_events(state,created_at)",
        ):
            self.store._run(statement)

    # ---------------------------------------------------------------- settings
    def settings(self) -> dict[str, Any]:
        out = dict(DEFAULTS)
        for key in DEFAULTS:
            try:
                row = self.store.get_preference(PREFIX + key)
            except Exception:
                row = None
            if not row:
                continue
            value = row.get("value")
            if key in ("max_nonurgent_per_hour", "cooldown_minutes"):
                try:
                    out[key] = int(value)
                except (TypeError, ValueError):
                    pass
            else:
                out[key] = str(value or "")
        return out

    def payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "settings": self.settings(),
            "modes": list(MODES),
            "urgencies": list(URGENCIES),
            "autopilot": getattr(self.agent, "autonomy", None).level() == "autopilot"
                if getattr(self.agent, "autonomy", None) is not None else False,
            "recent": self.list(30),
            "invariant": (
                "Event Engine 1.0 создаёт только уведомления. Он не выполняет skills "
                "и не обходит confirmations."
            ),
        }

    def update(self, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict):
            return {"ok": False, "reason": "Настройки proactivity должны быть объектом"}
        unknown = sorted(set(str(key) for key in patch) - set(DEFAULTS))
        if unknown:
            return {"ok": False, "reason": "Неизвестные настройки: " + ", ".join(unknown)}

        clean: dict[str, Any] = {}
        if "mode" in patch:
            value = str(patch.get("mode") or "").strip().casefold()
            if value not in MODES:
                return {"ok": False, "reason": f"Неизвестный proactivity mode «{value}»"}
            clean["mode"] = value
        for key, low, high in (
            ("max_nonurgent_per_hour", 0, 20),
            ("cooldown_minutes", 1, 24 * 60),
        ):
            if key in patch:
                try:
                    value = int(patch[key])
                except (TypeError, ValueError):
                    return {"ok": False, "reason": f"{key} должен быть целым числом"}
                if not low <= value <= high:
                    return {"ok": False, "reason": f"{key}: допустимо {low}..{high}"}
                clean[key] = value
        for key in ("quiet_start", "quiet_end"):
            if key in patch:
                value = str(patch[key] or "").strip()
                if not _valid_hhmm(value):
                    return {"ok": False, "reason": f"{key}: нужно время HH:MM"}
                clean[key] = value

        for key, value in clean.items():
            self.store.set_preference(PREFIX + key, str(value))
        return self.payload()

    def reset(self) -> dict[str, Any]:
        for key in DEFAULTS:
            try:
                self.store.delete_preference(PREFIX + key)
            except Exception:
                pass
        return self.payload()

    # ---------------------------------------------------------------- events
    def _existing(self, source: str, event_key: str) -> dict[str, Any] | None:
        rows = self.store._rows(
            "SELECT * FROM assistant_events WHERE source=? AND event_key=? ORDER BY id DESC LIMIT 1",
            (source[:40], event_key[:180]),
        )
        return self._decode(rows[0]) if rows else None

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        try:
            out["payload"] = json.loads(out.pop("payload_json") or "{}")
        except (TypeError, ValueError):
            out["payload"] = {}
        out["explicit"] = bool(out.get("explicit"))
        return out

    def list(self, limit: int = 50, state: str = "") -> list[dict[str, Any]]:
        value = max(1, min(200, int(limit or 50)))
        if state:
            rows = self.store._rows(
                "SELECT * FROM assistant_events WHERE state=? ORDER BY id DESC LIMIT ?",
                (str(state), value),
            )
        else:
            rows = self.store._rows(
                "SELECT * FROM assistant_events ORDER BY id DESC LIMIT ?", (value,))
        return [self._decode(row) for row in rows]

    def record_explicit(self, notification: dict[str, Any],
                        now: dt.datetime | None = None) -> dict[str, Any]:
        """Mirror an already-created Personal notification into the event journal."""
        moment = (now or dt.datetime.now()).replace(microsecond=0)
        nid = int(notification.get("id") or 0)
        key = f"notification:{nid}"
        existing = self._existing("personal", key)
        if existing:
            return existing
        cursor = self.store._run(
            "INSERT INTO assistant_events("
            "created_at,last_seen,source,event_key,provider,kind,title,text,urgency,"
            "explicit,state,reason,notification_id,occurrences,payload_json"
            ") VALUES(?,?,?,?,?,?,?,?,?,1,'delivered','explicit commitment',?,1,?)",
            (
                _iso(moment), _iso(moment), "personal", key, "personal",
                str(notification.get("kind") or "notification")[:40],
                str(notification.get("title") or "Уведомление")[:160],
                str(notification.get("text") or "")[:1000],
                "important" if notification.get("kind") in ("reminder", "timer") else "normal",
                nid,
                json.dumps({"ref_id": notification.get("ref_id")}, ensure_ascii=False),
            ),
        )
        return self._existing("personal", key) or {"id": int(cursor.lastrowid or 0)}

    def emit(self, *, source: str, event_key: str, title: str, text: str = "",
             kind: str = "proactive", urgency: str = "normal",
             provider: str = "core", payload: dict[str, Any] | None = None,
             now: dt.datetime | None = None) -> dict[str, Any]:
        """Record one proactive event and maybe surface it as a notification."""
        moment = (now or dt.datetime.now()).replace(microsecond=0)
        clean_source = str(source or "event").strip().casefold()[:40] or "event"
        clean_key = str(event_key or "").strip()[:180]
        if not clean_key:
            clean_key = hashlib.sha256(
                f"{clean_source}|{title}|{text}".encode("utf-8")).hexdigest()[:24]

        existing = self._existing(clean_source, clean_key)
        if existing:
            self.store._run(
                "UPDATE assistant_events SET last_seen=?,occurrences=occurrences+1 WHERE id=?",
                (_iso(moment), int(existing["id"])),
            )
            fresh = self._existing(clean_source, clean_key) or existing
            return {"ok": True, "deduped": True, "event": fresh,
                    "delivered": fresh.get("state") == "delivered"}

        urgency_value = str(urgency or "normal").strip().casefold()
        if urgency_value not in URGENCIES:
            urgency_value = "normal"
        cursor = self.store._run(
            "INSERT INTO assistant_events("
            "created_at,last_seen,source,event_key,provider,kind,title,text,urgency,"
            "explicit,state,reason,notification_id,occurrences,payload_json"
            ") VALUES(?,?,?,?,?,?,?,?,?,0,'new','',0,1,?)",
            (
                _iso(moment), _iso(moment), clean_source, clean_key,
                str(provider or "core").strip().casefold()[:40] or "core",
                str(kind or "proactive")[:40],
                str(title or "NOZZA")[:160],
                str(text or "")[:1000],
                urgency_value,
                json.dumps(dict(payload or {}), ensure_ascii=False, default=str)[:10000],
            ),
        )
        event_id = int(cursor.lastrowid or 0)
        event = self._decode(self.store._rows(
            "SELECT * FROM assistant_events WHERE id=?", (event_id,))[0])
        return self._decide(event, moment)

    def _decide(self, event: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
        settings = self.settings()
        allowed, reason = self._attention_allows(event, now, settings)
        if not allowed:
            self.store._run(
                "UPDATE assistant_events SET state='suppressed',reason=? WHERE id=?",
                (reason[:500], int(event["id"])),
            )
            event = self._decode(self.store._rows(
                "SELECT * FROM assistant_events WHERE id=?", (int(event["id"]),))[0])
            return {"ok": True, "delivered": False, "event": event, "reason": reason}

        note = self.personal.notify(
            "event",
            str(event.get("title") or "NOZZA"),
            str(event.get("text") or ""),
            int(event["id"]),
        )
        self.store._run(
            "UPDATE assistant_events SET state='delivered',reason='',notification_id=? WHERE id=?",
            (int(note.get("id") or 0), int(event["id"])),
        )
        event = self._decode(self.store._rows(
            "SELECT * FROM assistant_events WHERE id=?", (int(event["id"]),))[0])
        return {"ok": True, "delivered": True, "event": event, "notification": note}

    def _attention_allows(self, event: dict[str, Any], now: dt.datetime,
                          settings: dict[str, Any]) -> tuple[bool, str]:
        policy = getattr(self.agent, "autonomy", None)
        if policy is None or policy.level() != "autopilot":
            return False, "proactive delivery requires Autopilot"

        mode = str(settings.get("mode") or "balanced")
        if mode == "silent":
            return False, "proactivity mode is silent"
        threshold = {
            "important": "important",
            "balanced": "normal",
            "active": "low",
        }.get(mode, "normal")
        urgency = str(event.get("urgency") or "normal")
        if _urgency_rank(urgency) < _urgency_rank(threshold):
            return False, f"urgency {urgency} below {mode} threshold"

        if urgency != "urgent" and _quiet(
            now,
            str(settings.get("quiet_start") or ""),
            str(settings.get("quiet_end") or ""),
        ):
            return False, "quiet hours"

        if urgency not in ("important", "urgent"):
            max_hour = int(settings.get("max_nonurgent_per_hour") or 0)
            if max_hour <= 0:
                return False, "non-urgent attention budget is zero"
            since = _iso(now - dt.timedelta(hours=1))
            rows = self.store._rows(
                "SELECT COUNT(*) AS n FROM assistant_events "
                "WHERE explicit=0 AND state='delivered' AND created_at>=? "
                "AND urgency NOT IN ('important','urgent')",
                (since,),
            )
            count = int(rows[0]["n"] or 0) if rows else 0
            if count >= max_hour:
                return False, "hourly attention budget exhausted"
        return True, ""

    # ---------------------------------------------------------------- scanners
    def tick(self, now: dt.datetime | None = None) -> list[dict[str, Any]]:
        """Scan safe local state and emit notification-only proactive events."""
        moment = (now or dt.datetime.now()).replace(microsecond=0)
        delivered: list[dict[str, Any]] = []
        for result in self._goal_events(moment) + self._task_events(moment):
            if result.get("delivered") and isinstance(result.get("notification"), dict):
                delivered.append(result["notification"])
        # Keep the event journal bounded without deleting recent diagnostics.
        cutoff = _iso(moment - dt.timedelta(days=90))
        self.store._run(
            "DELETE FROM assistant_events WHERE created_at<? AND state IN ('suppressed','delivered')",
            (cutoff,),
        )
        return delivered

    def _goal_events(self, now: dt.datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        today = now.date()
        try:
            rows = self.personal.goals("active")
        except Exception:
            return out
        for row in rows:
            raw = str(row.get("deadline") or "")
            if not raw:
                continue
            try:
                deadline = dt.date.fromisoformat(raw)
            except ValueError:
                continue
            left = (deadline - today).days
            if left > 1:
                continue
            if left < 0:
                urgency = "important"
                text = f"Срок цели «{row.get('title')}» уже прошёл. Прогресс: {row.get('progress') or 0}/{row.get('target') or 0}."
                kind = "goal_overdue"
            elif left == 0:
                urgency = "important"
                text = f"Сегодня срок цели «{row.get('title')}». Прогресс: {row.get('progress') or 0}/{row.get('target') or 0}."
                kind = "goal_due"
            else:
                urgency = "normal"
                text = f"Завтра срок цели «{row.get('title')}». Прогресс: {row.get('progress') or 0}/{row.get('target') or 0}."
                kind = "goal_due_soon"
            out.append(self.emit(
                source="goal",
                event_key=f"goal:{row.get('id')}:{kind}:{today.isoformat()}",
                provider="personal",
                kind=kind,
                title="Цель",
                text=text,
                urgency=urgency,
                payload={"goal_id": row.get("id"), "deadline": raw},
                now=now,
            ))
        return out

    def _task_events(self, now: dt.datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            tasks = self.agent.tasks.list(100)
        except Exception:
            return out
        for task in tasks:
            status = str(task.get("status") or "")
            if status != "failed":
                continue
            marker = str(task.get("updated_at") or task.get("error") or task.get("current_step") or "")
            digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()[:12]
            out.append(self.emit(
                source="task",
                event_key=f"task:{task.get('id')}:failed:{digest}",
                provider="core",
                kind="task_failed",
                title="Задача остановилась",
                text=f"«{task.get('title') or 'Задача'}»: {task.get('error') or 'шаг не выполнен'}",
                urgency="important",
                payload={"task_id": task.get("id"), "current_step": task.get("current_step")},
                now=now,
            ))
        return out
