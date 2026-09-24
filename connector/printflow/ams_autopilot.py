"""Оркестрация синка и записи настроек AMS для фонового и ручного запуска."""
from __future__ import annotations

from .ams_push import push
from .ams_sync import sync_ams_spools, sync_printer_info


NOTICE_EVENTS = frozenset({"ams_unbind", "ams_empty", "ams_loss", "ams_conflict", "ams_runout"})


def send_notices(db, manager, printer_id: str, notices: list[dict]) -> None:
    """Только пять значимых переходов, через существующие подписки Telegram."""
    if not db.setting("telegram_enabled", False) or not db.setting("telegram_token", ""):
        return
    notify = getattr(manager, "notify_async", None)
    if not callable(notify):
        return
    name = db.one("SELECT name FROM printers WHERE id=?", (printer_id,)) or {}
    for entry in notices:
        key = str(entry.get("event") or "")
        if key in NOTICE_EVENTS:
            notify(f"PrintFlow · {name.get('name') or printer_id}\n"
                   f"{entry.get('detail') or 'Изменение AMS'}", event=key,
                   critical=key == "ams_runout")


def tidy(db, printer, manager=None, snap: dict | None = None) -> dict:
    """Обновить склад, память и настройки; принтер занят → запись отложена."""
    snap = snap if snap is not None else printer.snapshot()
    info_ok = sync_printer_info(db, printer.id, snap)
    counts = sync_ams_spools(db, printer.id, snap)
    sent = push(db, printer, snap)
    if manager is not None:
        send_notices(db, manager, printer.id, counts.get("alerts") or [])
    return {"ok": True, "printer_info": info_ok, **counts, "push": sent}
