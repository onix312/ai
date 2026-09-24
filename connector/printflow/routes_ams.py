"""Правила, журнал отката, доктор и только советующий планировщик AMS."""
from __future__ import annotations

from typing import Any

from . import ams_rules, ams_actions
from .router import Ctx, router


@router.get("/api/ams/rules", doc="Правила значений новых катушек AMS")
def rules(api: Any, ctx: Ctx):
    return {"rules": ams_rules.list_rules(api.db),
            "push_enabled": bool(api.db.setting("ams_push_settings", True))}


@router.post("/api/ams/rules/save", audit="Правило AMS изменено",
             doc="Создать или обновить правило значений катушек AMS")
def rule_save(api: Any, ctx: Ctx):
    return {"ok": True, "rule": ams_rules.save(api.db, ctx.body)}


@router.post("/api/ams/rules/delete", audit="Правило AMS удалено",
             doc="Удалить пользовательское правило AMS")
def rule_delete(api: Any, ctx: Ctx):
    ams_rules.delete(api.db, str(ctx.arg("id") or ""))
    return {"ok": True}


@router.get("/api/ams/actions", doc="Журнал решений автопилота AMS")
def actions(api: Any, ctx: Ctx):
    return {"actions": ams_actions.list_actions(api.db, ctx.one("printer_id").strip())}


@router.get("/api/ams/actions/detail", doc="Снимки до и после решения AMS")
def action_detail(api: Any, ctx: Ctx):
    row = ams_actions.detail(api.db, ctx.one("id"))
    return row if row else (404, {"error": "Решение не найдено"})


@router.post("/api/ams/actions/undo", audit="Решение автопилота AMS отменено",
             doc="Откатить одно решение AMS, если карточка и слот не менялись после него")
def action_undo(api: Any, ctx: Ctx):
    from .ams_actions import undo
    return undo(api.db, str(ctx.arg("id") or ""))


@router.get("/api/ams/doctor", doc="Проверить слоты, дубли, влажность и связь с AMS")
def ams_doctor(api: Any, ctx: Ctx):
    from .ams_doctor import doctor
    pid = ctx.one("printer_id").strip()
    printer = api.manager.get(pid) if pid and api.manager else None
    snapshot = None
    if printer and getattr(printer, "connected", False):
        try:
            snapshot = printer.snapshot()
        except Exception:
            pass
    return doctor(api.db, pid, snapshot)


@router.get("/api/ams/plan", doc="Советы по катушкам для очереди печати (без изменений)")
def ams_plan(api: Any, ctx: Ctx):
    from .ams_doctor import plan
    pid = ctx.one("printer_id").strip()
    if not pid:
        raise ValueError("Не указан принтер")
    return plan(api.db, pid)


@router.post("/api/ams/plan", doc="Советы по раскладке для указанных материалов (без изменений)")
def ams_plan_custom(api: Any, ctx: Ctx):
    from .ams_doctor import plan
    pid = str(ctx.arg("printer_id") or "").strip()
    if not pid:
        raise ValueError("Не указан принтер")
    return plan(api.db, pid, ctx.body.get("materials"))


@router.post("/api/ams/tidy", audit="AMS: привести в порядок",
             doc="Обновить учёт и безопасно отправить настройки в свободный AMS")
def ams_tidy(api: Any, ctx: Ctx):
    from .ams_autopilot import tidy
    from .ams_doctor import doctor
    pid = str(ctx.arg("printer_id") or "").strip()
    printer = api.printer_or_fail(pid)
    snap = printer.snapshot()
    result = tidy(api.db, printer, api.manager, snap)
    return {**result, "doctor": doctor(api.db, pid, snap)}
