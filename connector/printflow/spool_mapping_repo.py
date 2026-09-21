"""AMS mapping persistence (12): file_hash + printer -> mapping.

Таблица ams_mappings хранит автоподбор AMS для файлов, чтобы watch_folder
и конвейер не гадали заново.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .config import now_iso


def ensure_schema(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS ams_mappings (
            id TEXT PRIMARY KEY,
            file_hash TEXT DEFAULT '',
            filename TEXT DEFAULT '',
            printer_id TEXT DEFAULT '',
            mapping_json TEXT DEFAULT '[]',
            created_at TEXT,
            updated_at TEXT
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_ams_map_hash ON ams_mappings(file_hash)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ams_map_printer ON ams_mappings(printer_id, file_hash)")


def _uid(prefix="amap") -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def save_mapping(db, file_hash: str, filename: str, printer_id: str, mapping: list[int]) -> dict:
    ensure_schema(db)
    file_hash = str(file_hash or "").strip().lower()
    if not file_hash:
        return {"ok": False, "error": "no hash"}
    # upsert by hash+printer
    existing = db.one(
        "SELECT * FROM ams_mappings WHERE file_hash=? AND printer_id=?",
        (file_hash, printer_id or ""),
    )
    payload = {
        "id": existing["id"] if existing else _uid(),
        "file_hash": file_hash,
        "filename": filename or "",
        "printer_id": printer_id or "",
        "mapping_json": json.dumps(mapping or [], ensure_ascii=False),
        "updated_at": now_iso(),
        "created_at": existing.get("created_at") if existing else now_iso(),
    }
    db.upsert("ams_mappings", payload, key="id")
    return {"ok": True, "mapping": mapping}


def load_mapping(db, file_hash: str, printer_id: str = "") -> list[int] | None:
    ensure_schema(db)
    file_hash = str(file_hash or "").strip().lower()
    if not file_hash:
        return None
    if printer_id:
        row = db.one(
            "SELECT * FROM ams_mappings WHERE file_hash=? AND printer_id=? ORDER BY datetime(updated_at) DESC LIMIT 1",
            (file_hash, printer_id),
        )
        if row:
            try:
                return json.loads(row.get("mapping_json") or "[]")
            except Exception:
                return None
    # fallback any printer
    row = db.one(
        "SELECT * FROM ams_mappings WHERE file_hash=? ORDER BY datetime(updated_at) DESC LIMIT 1",
        (file_hash,),
    )
    if row:
        try:
            return json.loads(row.get("mapping_json") or "[]")
        except Exception:
            return None
    return None


def list_mappings(db, limit: int = 100) -> list[dict]:
    ensure_schema(db)
    rows = db.query("SELECT * FROM ams_mappings ORDER BY datetime(updated_at) DESC LIMIT ?", (int(limit),))
    for r in rows:
        try:
            r["mapping"] = json.loads(r.get("mapping_json") or "[]")
        except Exception:
            r["mapping"] = []
    return rows
