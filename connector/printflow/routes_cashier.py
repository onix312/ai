"""Маршруты мобильной кассы (Касса 16.0, итерация 2).

Транспортный слой: вход по коду магазина, продажа с полки, подтверждение СБП.
Гарантии «деньги не ломаются» живут в ``cashier.py`` и ``sbp.py``.
"""
from __future__ import annotations

from typing import Any

from .router import Ctx, router
from .cashier import Cashier


def _cashier(api: Any) -> Cashier:
    service = getattr(api, "cashier", None)
    if service is None:
        from .cashier import Cashier as _Cashier
        service = _Cashier(api.db, api.acc)
        api.cashier = service
    return service


def _token(ctx: Ctx) -> str:
    return str(ctx.arg("token", ctx.one("token", "")) or "").strip()


@router.post("/api/cashier/login", doc="Вход кассира по коду магазина")
def cashier_login(api: Any, ctx: Ctx):
    return _cashier(api).login(str(ctx.arg("code", "") or ""))


@router.post("/api/cashier/logout", doc="Выход кассира")
def cashier_logout(api: Any, ctx: Ctx):
    return _cashier(api).logout(_token(ctx))


@router.get("/api/cashier/catalog", doc="Полка для кассы: товары, цены, остатки")
def cashier_catalog(api: Any, ctx: Ctx):
    _cashier(api).require(_token(ctx))
    return _cashier(api).catalog()


@router.get("/api/cashier/incoming", doc="СБП-продажи кассы, ожидающие сверки")
def cashier_incoming(api: Any, ctx: Ctx):
    _cashier(api).require(_token(ctx))
    return _cashier(api).incoming()


@router.post("/api/cashier/sell", audit="Касса: продажа", idempotent=True,
             doc="Продажа с полки: наличные (сразу) или СБП (платёж до сверки)")
def cashier_sell(api: Any, ctx: Ctx):
    body = ctx.body if isinstance(ctx.body, dict) else {}
    items = body.get("items") or ctx.arg("items", []) or []
    if isinstance(items, str):
        import json as _json
        try:
            items = _json.loads(items)
        except Exception:
            items = []
    return _cashier(api).sell(
        items,
        str(ctx.arg("method", "cash") or "cash"),
        _token(ctx),
        request_id=str(ctx.arg("request_id", "") or "").strip(),
        cashier_name=str(ctx.arg("cashier_name", "") or "").strip())


@router.post("/api/cashier/confirm-sbp", audit="Касса: СБП подтверждена",
             doc="Подтвердить СБП-продажу: списать склад и записать выручку")
def cashier_confirm_sbp(api: Any, ctx: Ctx):
    return _cashier(api).confirm_sbp(
        str(ctx.arg("payment_id", "") or ctx.arg("id", "") or "").strip(),
        _token(ctx),
        note=str(ctx.arg("note", "") or "").strip())


@router.post("/api/cashier/reject-sbp", audit="Касса: СБП отклонена",
             doc="Отклонить СБП-продажу (склад не списывался)")
def cashier_reject_sbp(api: Any, ctx: Ctx):
    return _cashier(api).reject_sbp(
        str(ctx.arg("payment_id", "") or ctx.arg("id", "") or "").strip(),
        _token(ctx),
        reason=str(ctx.arg("reason", "") or "").strip())


@router.get("/api/cashier/shift/current", doc="Открытая смена с живым расчётом")
def cashier_shift_current(api: Any, ctx: Ctx):
    return _cashier(api).current_shift(_token(ctx))


@router.post("/api/cashier/shift/open", audit="Касса: смена открыта",
             doc="Открыть смену: зафиксировать стартовый пересчёт ящика")
def cashier_shift_open(api: Any, ctx: Ctx):
    return _cashier(api).open_shift(
        _token(ctx), ctx.num("open_cash", 0))


@router.post("/api/cashier/shift/close", audit="Касса: смена закрыта",
             doc="Закрыть смену: пересчёт ящика, расчёт сервера, расхождение")
def cashier_shift_close(api: Any, ctx: Ctx):
    return _cashier(api).close_shift(
        _token(ctx), ctx.num("close_cash", 0),
        note=str(ctx.arg("note", "") or ""))


@router.post("/api/cashier/collect", audit="Касса: выемка",
             doc="Выемка из ящика (только старший, в пределах остатка)")
def cashier_collect(api: Any, ctx: Ctx):
    return _cashier(api).collect(
        _token(ctx), ctx.num("amount", 0),
        note=str(ctx.arg("note", "") or ""))
