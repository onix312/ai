"""Журнал решений автопилота AMS и безопасный откат учёта одним действием.

Снимки до/после включают затронутые катушки и память слотов. Откат никогда не
трогает принтер физически: после проверки снимка он возвращает только БД.
Если человек или следующий автосинк уже изменил запись, откат отказывается
вместо того, чтобы затирать новые данные.
"""
from __future__ import annotations

import json
from typing import Iterable

from .accounting import uid
from .config import now_iso


def snapshot(db, spool_ids: Iterable[str] = (),
             positions: Iterable[tuple[str, str]] = ()) -> dict:
    """Явно запоминаем и отсутствующие строки (будущая новая катушка)."""
    ids = dict.fromkeys(str(s) for s in spool_ids if s)
    spots = dict.fromkeys((str(p), str(s)) for p, s in positions if p and s != "")
    return {
        "spools": {ident: db.one("SELECT * FROM spools WHERE id=?", (ident,))
                   for ident in ids},
        "slots": {json.dumps([p, s], ensure_ascii=False): db.one(
            "SELECT * FROM ams_slots WHERE printer_id=? AND slot=?", (p, s))
                  for p, s in spots},
    }


def record(db, printer_id: str, slot: str, spool_id: str, action: str,
           detail: str, before: dict, after: dict, undo_of: str = "") -> dict:
    """Пишем событие с оригинальными данными; одно решение = один откат."""
    row = db.upsert("ams_actions", {
        "id": uid("aa"), "at": now_iso(), "printer_id": printer_id,
        "slot": str(slot), "spool_id": spool_id or "", "action": action,
        "detail": str(detail)[:400], "undo_of": undo_of,
        "before_json": json.dumps(before, ensure_ascii=False, sort_keys=True),
        "after_json": json.dumps(after, ensure_ascii=False, sort_keys=True),
    })
    db.add_event("ams", "Автопилот AMS: " + action, str(detail)[:400],
                 printer_id, {"action_id": row["id"], "spool_id": spool_id,
                              "slot": str(slot), "action": action})
    return row


def feed(db, printer_id: str = "", limit: int = 30) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    if printer_id:
        return db.query("SELECT * FROM ams_actions WHERE printer_id=? ORDER BY rowid DESC LIMIT ?",
                        (printer_id, limit))
    return db.query("SELECT * FROM ams_actions ORDER BY rowid DESC LIMIT ?", (limit,))


def _same(current: dict | None, expected: dict | None, *, memory: bool) -> bool:
    if current is None or expected is None:
        return current is None and expected is None
    # Повторный опрос обновляет seen_at памяти без изменения раскладки.
    volatile = {"seen_at", "updated_at"} if memory else set()
    return {k: v for k, v in current.items() if k not in volatile} == {
        k: v for k, v in expected.items() if k not in volatile}


def undo(db, action_id: str) -> dict:
    """Откатить только если ни одна запись из снимка «после» не менялась."""
    with db.transaction():
        action = db.one("SELECT rowid, * FROM ams_actions WHERE id=?", (action_id,))
        if not action:
            raise ValueError("Решение автопилота не найдено")
        if action.get("undone_at"):
            raise ValueError("Решение уже откатили")
        if action["action"] not in {"auto_create", "auto_bind", "auto_move",
                                     "auto_unbind", "auto_empty", "auto_update", "refill", "loss", "runout"}:
            raise ValueError("Команду принтеру и конфликт нельзя отменить через учёт")
        before = json.loads(action["before_json"])
        after = json.loads(action["after_json"])
        if before == after:
            raise ValueError("Здесь нет изменений учёта для отката")
        newer = db.one(
            "SELECT id FROM ams_actions WHERE rowid>? AND printer_id=? AND slot=?"
            " AND before_json<>after_json AND undone_at='' LIMIT 1",
            (action["rowid"], action["printer_id"], action["slot"]))
        if newer:
            raise ValueError("Слот уже менялся после этого решения; откатите более позднее первым")
        for ident, expected in after.get("spools", {}).items():
            current = db.one("SELECT * FROM spools WHERE id=?", (ident,))
            if not _same(current, expected, memory=False):
                raise ValueError("Катушка уже изменена после автопилота — откат опасен")
        for key, expected in after.get("slots", {}).items():
            printer_id, slot = json.loads(key)
            current = db.one("SELECT * FROM ams_slots WHERE printer_id=? AND slot=?",
                             (printer_id, slot))
            if not _same(current, expected, memory=True):
                raise ValueError("Память слота уже изменена — откат опасен")
        for ident, previous in before.get("spools", {}).items():
            if previous is None:
                # Катушка могла попасть в задания без изменения самой карточки:
                # физический расход откатом привязки удалять нельзя.
                for table in ("filament_usage", "print_jobs", "filament_scrap",
                              "drying_sessions", "batches", "nom_variants",
                              "nom_variant_structures"):
                    if db.one(f"SELECT 1 FROM {table} WHERE spool_id=? LIMIT 1", (ident,)):
                        raise ValueError("Катушка уже используется в учёте — удаление опасно")
                # orders.spools — JSON, не физическая ссылка БД: не оставляем
                # заказ со ссылкой на исчезнувшую карточку.
                for order in db.query("SELECT spools FROM orders WHERE spools LIKE ?",
                                      (f"%{ident}%",)):
                    try:
                        refs = json.loads(order.get("spools") or "[]")
                    except (TypeError, ValueError):
                        refs = []
                    if any(isinstance(ref, dict) and str(ref.get("spool_id")) == ident
                           for ref in refs):
                        raise ValueError("Катушка уже закреплена за заказом — удаление опасно")
                db.execute("DELETE FROM spools WHERE id=?", (ident,))
            else:
                db.upsert("spools", previous)
        for key, previous in before.get("slots", {}).items():
            printer_id, slot = json.loads(key)
            if previous is None:
                db.execute("DELETE FROM ams_slots WHERE printer_id=? AND slot=?",
                           (printer_id, slot))
            else:
                db.upsert("ams_slots", previous)
        db.execute("UPDATE ams_actions SET undone_at=? WHERE id=?", (now_iso(), action_id))
        undo_row = record(db, action["printer_id"], action["slot"], action["spool_id"],
                          "undo", f"Отменено: {action['detail']}", after, before,
                          undo_of=action_id)
        return {"ok": True, "action_id": action_id, "undo_id": undo_row["id"],
                "printer_id": action["printer_id"], "slot": action["slot"]}


def list_actions(db, printer_id: str = "") -> list[dict]:
    sql = ("SELECT id,at,printer_id,slot,spool_id,action,detail,undone_at,undo_of"
           " FROM ams_actions")
    if printer_id:
        sql += " WHERE printer_id=?"
    sql += " ORDER BY rowid DESC LIMIT 100"
    return db.query(sql, (printer_id,) if printer_id else ())


def detail(db, action_id: str) -> dict | None:
    row = db.one("SELECT * FROM ams_actions WHERE id=?", (action_id,))
    if not row:
        return None
    return {**row, "before": json.loads(row.get("before_json") or "{}"),
            "after": json.loads(row.get("after_json") or "{}")}
