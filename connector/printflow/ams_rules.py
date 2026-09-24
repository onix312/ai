"""Хранилище правил AMS; сами расчёты и проверка значений в ams_defaults."""
from __future__ import annotations

from . import ams_defaults


def list_rules(db) -> list[dict]:
    return ams_defaults.rules(db)


def save(db, data: dict) -> dict:
    return ams_defaults.save_rule(db, data)


def delete(db, rid: str) -> None:
    rid = str(rid or "").strip()
    if not rid:
        raise ValueError("Не указан id правила")
    db.execute("DELETE FROM ams_rules WHERE id=?", (rid,))
