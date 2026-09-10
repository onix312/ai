"""Маршруты НПД-контура: лимит года и сверка чеков по дням.

Логика и смысл — в ``npd.py``; здесь только транспорт. Отметка «чеки выбиты»
ничего не двигает в деньгах, но входит в аудит: по ней видно, кто и когда
подтвердил, что расчёты дня закрыты чеками.
"""
from __future__ import annotations

from typing import Any

from .router import Ctx, router


def _npd(api: Any):
    service = getattr(api, "npd", None)
    if service is None:
        from .npd import Npd
        service = Npd(api.db)
        api.npd = service
    return service


def _actor(ctx: Ctx) -> str:
    return str(ctx.arg("actor", "panel") or "panel")[:120]


@router.get("/api/npd/status", doc="Лимит налогового режима: остаток, темп, прогноз")
def npd_status(api: Any, ctx: Ctx):
    return _npd(api).status()


@router.get("/api/npd/days", doc="Дни с деньгами и отметки о чеках (?back=14)")
def npd_days(api: Any, ctx: Ctx):
    service = _npd(api)
    back = int(ctx.num("back", 14) or 14)
    return {"days": service.days(back), "pending": service.pending()}


@router.post("/api/npd/day/mark", audit="НПД: чеки за день подтверждены",
             doc="Подтвердить, что чеки по дню выбиты (day, checks, amount, note)")
def npd_day_mark(api: Any, ctx: Ctx):
    return _npd(api).mark_day(ctx.arg("day", ""), checks=ctx.num("checks", 0),
                              amount=ctx.num("amount", 0),
                              note=str(ctx.arg("note", "") or ""), actor=_actor(ctx))


@router.post("/api/npd/day/unmark", audit="НПД: отметка о чеках снята",
             doc="Снять отметку о чеках за день (ошиблись кнопкой)")
def npd_day_unmark(api: Any, ctx: Ctx):
    return _npd(api).unmark_day(ctx.arg("day", ""), actor=_actor(ctx))
