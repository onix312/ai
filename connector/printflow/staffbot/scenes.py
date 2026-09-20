"""Сцены бота — SQLite с нуля, TTL, переживают рестарт.

Таблица bot_scenes: chat_id PK, scene, data JSON, expires_at ISO, created_at ISO.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from ..config import now_iso
from .core.db import ensure_scenes_table

# Константы сцен — для совместимости и тестов
SELL = "sell"
CASH_RECONCILE = "cash_reconcile"
CLIENT_REPLY = "client_reply"


class BotScenes:
    def __init__(self, db):
        self.db = db
        ensure_scenes_table(db)

    def _parse_row(self, row: dict | None) -> dict | None:
        if not row:
            return None
        exp = str(row.get("expires_at") or "")
        if exp:
            try:
                # ISO, считаем протухшим если < now
                dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    # naive — считаем локальным, сравниваем строкой как раньше
                    if exp < now_iso():
                        # протухло — удаляем
                        self.db.execute("DELETE FROM bot_scenes WHERE chat_id=?", (row.get("chat_id"),))
                        return None
                else:
                    if dt < datetime.now(dt.tzinfo):
                        self.db.execute("DELETE FROM bot_scenes WHERE chat_id=?", (row.get("chat_id"),))
                        return None
            except Exception:
                # если не парсится — сравниваем строкой
                if exp and exp < now_iso():
                    self.db.execute("DELETE FROM bot_scenes WHERE chat_id=?", (row.get("chat_id"),))
                    return None
        try:
            data = json.loads(row.get("data") or "{}")
        except Exception:
            data = {}
        return {"scene": row.get("scene"), "data": data, "chat_id": row.get("chat_id"), "expires_at": exp}

    def active(self, chat_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM bot_scenes WHERE chat_id=?", (str(chat_id),))
        return self._parse_row(row)

    def set(self, chat_id: str, scene: str, data: dict | None = None, ttl: int = 600) -> None:
        data = data or {}
        try:
            expires = (datetime.now() + timedelta(seconds=int(ttl))).isoformat()
        except Exception:
            expires = (datetime.now() + timedelta(seconds=600)).isoformat()
        # таблица bot_scenes использует chat_id как PK, а не id — поэтому
        # делаем прямой INSERT OR REPLACE, а не через db.upsert (который ждёт id)
        try:
            self.db.execute(
                "INSERT INTO bot_scenes(chat_id,scene,data,expires_at,created_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(chat_id) DO UPDATE SET scene=excluded.scene, data=excluded.data,"
                " expires_at=excluded.expires_at, created_at=excluded.created_at",
                (
                    str(chat_id),
                    str(scene),
                    json.dumps(data, ensure_ascii=False),
                    expires,
                    now_iso(),
                ),
            )
        except Exception:
            # fallback для старых схем где есть updated_at
            try:
                self.db.execute(
                    "INSERT OR REPLACE INTO bot_scenes(chat_id,scene,data,expires_at,created_at) VALUES(?,?,?,?,?)",
                    (
                        str(chat_id),
                        str(scene),
                        json.dumps(data, ensure_ascii=False),
                        expires,
                        now_iso(),
                    ),
                )
            except Exception:
                # последний fallback — через upsert с key=chat_id если поддерживается
                try:
                    self.db.upsert(
                        "bot_scenes",
                        {
                            "chat_id": str(chat_id),
                            "scene": str(scene),
                            "data": json.dumps(data, ensure_ascii=False),
                            "expires_at": expires,
                            "created_at": now_iso(),
                            "id": str(chat_id),
                        },
                    )
                except Exception:
                    self.db.execute(
                        "INSERT OR REPLACE INTO bot_scenes(chat_id,scene,data,expires_at) VALUES(?,?,?,?)",
                        (str(chat_id), str(scene), json.dumps(data, ensure_ascii=False), expires),
                    )

    def pop(self, chat_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM bot_scenes WHERE chat_id=?", (str(chat_id),))
        parsed = self._parse_row(row)
        self.db.execute("DELETE FROM bot_scenes WHERE chat_id=?", (str(chat_id),))
        return parsed

    def sweep(self) -> int:
        """Удалить протухшие сцены, вернуть количество."""
        now = now_iso()
        # Удаляем где expires_at < now и не пусто
        try:
            cur = self.db.execute(
                "DELETE FROM bot_scenes WHERE expires_at<>'' AND expires_at<?", (now,)
            )
            return int(cur.rowcount or 0)
        except Exception:
            # fallback — перебор
            rows = self.db.query("SELECT chat_id, expires_at FROM bot_scenes WHERE expires_at<>''")
            removed = 0
            for r in rows:
                exp = str(r.get("expires_at") or "")
                if exp and exp < now:
                    self.db.execute("DELETE FROM bot_scenes WHERE chat_id=?", (r.get("chat_id"),))
                    removed += 1
            return removed
