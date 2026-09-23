"""Журнал действий автопилота AMS и откат (18.13).

Зачем. Полный автомат без журнала — это «само куда-то переехало». Автопилот
меняет привязки, остатки и настройки слота в принтере; каждое такое действие
попадает сюда вместе со снимком «до» и «после», поэтому панель может показать
ленту, а оператор — откатить любую строку одной кнопкой.

Что важно. Здесь нет тяжёлой логики: строки пишет и читает `ams_sync` и
менеджер, а восстановление значений живёт в одном месте — `undo_action`, чтобы
откат нельзя было сделать «почти правильно» в двух разных файлах.
"""
from __future__ import annotations

import json
from typing import Any

from .accounting import num, uid
from .config import now_iso

#: Человеческие подписи видов действий — для ленты и карточки в «Смене».
KIND_LABELS: dict[str, str] = {
    "create": "Завёл катушку",
    "bind": "Привязал катушку к слоту",
    "move": "Перенёс катушку в другой слот",
    "unbind": "Отвязал катушку от слота",
    "empty": "Катушка кончилась в слоте",
    "remain": "Поправил остаток по датчику",
    "fill": "Остаток вырос — долив",
    "learn": "Запомнил правило",
    "push": "Записал настройки слота в принтер",
    "accept": "Принял данные AMS",
    "undo": "Откат действия",
    "skip": "Оставил как есть",
}

#: Действия, которые меняют деньги или ломают печать: о них сообщаем в Telegram.
IMPORTANT_KINDS: frozenset[str] = frozenset({"unbind", "empty", "conflict", "runout"})

#: Поля катушки, которые умеет восстанавливать откат.
SPOOL_FIELDS = ("material", "brand", "color_name", "color_hex", "total_grams",
                "remaining_grams", "price", "printer_id", "ams_slot", "tray_uuid",
                "location", "ams_sync", "ams_state", "verified", "archived")

#: Поля памяти слота, которые умеет восстанавливать откат.
SLOT_FIELDS = ("spool_id", "tray_uuid", "material", "color_name", "color_hex",
               "label", "remain_pct", "grams_left", "state", "emptied_at")


def _json(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def spool_snapshot(db, spool_id: str) -> dict:
    """Снимок катушки для отката — только поля, которые автопилот меняет."""
    spool_id = str(spool_id or "").strip()
    if not spool_id:
        return {}
    row = db.one("SELECT * FROM spools WHERE id=?", (spool_id,))
    if not row:
        return {}
    keys = set(row.keys())
    snapshot = {field: row.get(field) for field in SPOOL_FIELDS if field in keys}
    # Идентификатор нужен откату, но в UPDATE не попадает: он и есть ключ.
    snapshot["id"] = spool_id
    return snapshot


def slot_snapshot(db, printer_id: str, slot: Any) -> dict:
    """Снимок памяти слота для отката."""
    if not printer_id or slot in (None, ""):
        return {}
    row = db.one("SELECT * FROM ams_slots WHERE printer_id=? AND slot=?",
                 (printer_id, str(slot)))
    if not row:
        return {}
    return {field: row.get(field) for field in SLOT_FIELDS if field in set(row.keys())}


def log_action(db, kind: str, title: str, *, printer_id: str = "", slot: Any = "",
               spool_id: str = "", detail: str = "", before: dict | None = None,
               after: dict | None = None, undoable: bool = True,
               ident: str = "") -> dict:
    """Записать действие автопилота. Возвращает строку журнала."""
    kind = str(kind or "").strip() or "other"
    values = {
        "id": ident or uid("amsact"),
        "at": now_iso(),
        "printer_id": str(printer_id or ""),
        "slot": "" if slot is None else str(slot),
        "spool_id": str(spool_id or ""),
        "kind": kind,
        "title": str(title or KIND_LABELS.get(kind, kind))[:200],
        "detail": str(detail or "")[:1000],
        "before": _json(before),
        "after": _json(after),
        "undoable": 1 if undoable else 0,
        "undone_at": "",
        "undone_by": "",
    }
    db.upsert("ams_actions", values)
    return values


def actions(db, printer_id: str = "", limit: int = 40, kind: str = "",
            only_undoable: bool = False) -> list[dict]:
    """Лента действий автопилота: свежие сверху."""
    sql = "SELECT * FROM ams_actions WHERE 1=1"
    params: list[Any] = []
    if printer_id:
        sql += " AND printer_id=?"
        params.append(str(printer_id))
    if kind:
        sql += " AND kind=?"
        params.append(str(kind))
    if only_undoable:
        sql += " AND undoable=1 AND undone_at=''"
    sql += " ORDER BY at DESC, id DESC LIMIT ?"
    params.append(max(1, min(500, int(limit or 40))))
    out = []
    for row in db.query(sql, tuple(params)):
        item = dict(row)
        for key in ("before", "after"):
            try:
                item[key] = json.loads(item.get(key) or "{}")
            except json.JSONDecodeError:
                item[key] = {}
        item["undoable"] = bool(num(item.get("undoable")))
        item["undone"] = bool(str(item.get("undone_at") or ""))
        item["kind_label"] = KIND_LABELS.get(str(item.get("kind") or ""),
                                             str(item.get("kind") or ""))
        item["important"] = str(item.get("kind") or "") in IMPORTANT_KINDS
        out.append(item)
    return out


def action_count(db, printer_id: str = "", hours: int = 24) -> int:
    """Сколько действий автопилот сделал за период — для карточки в «Смене»."""
    from datetime import datetime, timedelta

    since = (datetime.now() - timedelta(hours=max(1, int(hours or 24)))).replace(
        microsecond=0).isoformat()
    sql = "SELECT COUNT(*) AS n FROM ams_actions WHERE at>=?"
    params: list[Any] = [since]
    if printer_id:
        sql += " AND printer_id=?"
        params.append(str(printer_id))
    row = db.one(sql, tuple(params))
    return int(num((row or {}).get("n")))


def last_fix(db, printer_id: str) -> dict | None:
    """Последнее исправимое действие принтера — для кнопки «вернуть как было»."""
    rows = actions(db, printer_id, limit=1, only_undoable=True)
    return rows[0] if rows else None


def mark_undone(db, ident: str, note: str = "") -> None:
    db.execute("UPDATE ams_actions SET undone_at=?, undone_by=? WHERE id=?",
               (now_iso(), str(note or "")[:200], str(ident or "")))


def _restore_spool(db, snapshot: dict) -> bool:
    """Вернуть катушке снятые значения."""
    spool_id = str(snapshot.get("id") or "").strip()
    if not spool_id:
        return False
    fields = {field: snapshot[field] for field in SPOOL_FIELDS if field in snapshot}
    if not fields:
        return False
    sets = ", ".join(f"{field}=?" for field in fields)
    db.execute(f"UPDATE spools SET {sets}, updated_at=? WHERE id=?",
               (*fields.values(), now_iso(), spool_id))
    return True


def _restore_slot(db, printer_id: str, slot: Any, snapshot: dict) -> bool:
    """Вернуть память слота: значения из снимка, иначе пустая строка памяти."""
    if not printer_id or slot in (None, ""):
        return False
    slot_s = str(slot)
    if not snapshot:
        db.execute("DELETE FROM ams_slots WHERE printer_id=? AND slot=?",
                   (printer_id, slot_s))
        return True
    values = {field: snapshot[field] for field in SLOT_FIELDS if field in snapshot}
    row = db.one("SELECT id FROM ams_slots WHERE printer_id=? AND slot=?",
                 (printer_id, slot_s))
    values.update({"printer_id": printer_id, "slot": slot_s, "updated_at": now_iso()})
    if row:
        sets = ", ".join(f"{field}=?" for field in values)
        db.execute(f"UPDATE ams_slots SET {sets} WHERE id=?", (*values.values(), row["id"]))
    else:
        values["id"] = uid("ams")
        values.setdefault("seen_at", now_iso())
        db.upsert("ams_slots", values)
    return True


def undo_action(db, ident: str, manager: Any = None) -> dict:
    """Откатить действие автопилота.

    Что восстанавливается:
      * ``create`` — заведённая катушка уходит в архив (удалять нельзя: на неё
        ссылаются списания и события), память слота возвращается к снимку;
      * ``bind``/``move``/``unbind``/``empty``/``remain``/``fill``/``accept`` —
        поля катушки и строка памяти слота из снимка «до»;
      * ``push`` — прежние настройки слота отправляются в принтер (нужен
        живой менеджер: без него откат честно отказывается).
    """
    ident = str(ident or "").strip()
    if not ident:
        raise ValueError("Не указано действие")
    row = db.one("SELECT * FROM ams_actions WHERE id=?", (ident,))
    if not row:
        raise ValueError("Действие не найдено")
    if str(row.get("undone_at") or ""):
        raise ValueError("Это действие уже откатили")
    if not int(num(row.get("undoable"), 1)):
        raise ValueError("Действие не откатывается — смотрите журнал")
    kind = str(row.get("kind") or "")
    try:
        before = json.loads(row.get("before") or "{}")
    except json.JSONDecodeError:
        before = {}
    try:
        after = json.loads(row.get("after") or "{}")
    except json.JSONDecodeError:
        after = {}
    printer_id = str(row.get("printer_id") or "")
    slot = row.get("slot") or ""

    if kind == "create":
        spool_id = str(after.get("spool_id") or row.get("spool_id") or "")
        if spool_id:
            db.execute(
                "UPDATE spools SET archived=1, ams_slot='', tray_uuid='',"
                " location='shop', label_note=?, updated_at=? WHERE id=?",
                (f"Откат автопилота {ident}: катушка заведена ошибочно",
                 now_iso(), spool_id))
        _restore_slot(db, printer_id, slot, before.get("slot") or {})
        detail = f"Катушка {spool_id} убрана со склада (архив), память слота возвращена"
    elif kind == "push":
        previous = before.get("slot_settings") or {}
        if manager is None or not printer_id:
            raise ValueError("Чтобы вернуть настройки слота, нужен принтер на связи")
        from .ams_push import push_slot_settings

        printer = manager.printers.get(printer_id)
        if printer is None:
            raise ValueError("Принтер не найден в парке")
        result = push_slot_settings(db, printer, slot, previous, reason="undo")
        detail = ("Прежние настройки слота отправлены в принтер"
                  if result.get("pushed") else
                  f"Принтер не принял настройки: {result.get('error') or 'нет связи'}")
    else:
        _restore_spool(db, before.get("spool") or {})
        _restore_slot(db, printer_id, slot, before.get("slot") or {})
        detail = "Значения катушки и память слота возвращены к состоянию до действия"

    mark_undone(db, ident, "откат из панели")
    undo_row = log_action(
        db, "undo", f"Откат: {KIND_LABELS.get(kind, kind)}", printer_id=printer_id,
        slot=slot, spool_id=str(row.get("spool_id") or ""), detail=detail,
        before=after, after=before, undoable=False)
    return {"ok": True, "undo": undo_row, "action_id": ident, "kind": kind,
            "detail": detail}
