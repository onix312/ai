"""Маршруты СБП (Касса 16.0): платежи, экран входящих, подтверждение, возврат.

Транспортный слой: логика и гарантии «деньги не ломаются» живут в ``sbp.py``.
Здесь — только привязка путей, публичность и аудит.
"""
from __future__ import annotations

from typing import Any

from .router import Ctx, router
from .sbp import Sbp


def _sbp(api: Any) -> Sbp:
    service = getattr(api, "sbp", None)
    if service is None:
        from .sbp import Sbp as _Sbp
        service = _Sbp(api.db, api.acc)
        api.sbp = service
    return service


@router.get("/api/sbp/settings", doc="Настройки СБП: счёт, статический QR, банк")
def sbp_settings(api: Any, ctx: Ctx):
    return _sbp(api).settings()


@router.get("/api/payment/qr",
            doc="Платёжный QR: строка и SVG (ГОСТ Р 56042-2014, ссылка или QR магазина)")
def payment_qr(api: Any, ctx: Ctx):
    """Код оплаты на любую сумму — для кассы, счёта, ценника и бота.

    Генерируется локально: ни банковского API, ни внешних сервисов.
    """
    from .accounting import num
    from .payment_qr import build
    return build(api.db,
                 amount=num(ctx.one("amount", "0")),
                 purpose=str(ctx.one("purpose", "") or ""),
                 number=str(ctx.one("number", "") or ""))


@router.get("/api/payment/qr/check",
            doc="Проверка реквизитов платёжного QR: чего не хватает")
def payment_qr_check(api: Any, ctx: Ctx):
    from .payment_qr import check, mode_of, requisites
    req = requisites(api.db)
    problems = check(req)
    return {"mode": mode_of(api.db), "requisites": req, "problems": problems,
            "ready": not problems}


@router.get("/api/sbp/state", doc="Экран «Входящие платежи»: сводка и список pending")
def sbp_state(api: Any, ctx: Ctx):
    return _sbp(api).state()


@router.get("/api/sbp/payments", doc="Журнал СБП-платежей (?status, ?order_id, ?limit)")
def sbp_payments(api: Any, ctx: Ctx):
    return {"payments": _sbp(api).list(
        status=ctx.one("status", ""), order_id=ctx.one("order_id", ""),
        limit=int(ctx.num("limit", 100) or 100))}


@router.post("/api/sbp/create", audit="СБП: платёж создан", idempotent=True,
             doc="Создать СБП-платёж (new). Деньги не меняются до подтверждения")
def sbp_create(api: Any, ctx: Ctx):
    from .accounting import num
    return _sbp(api).create(
        amount=num(ctx.arg("amount", 0)),
        order_id=str(ctx.arg("order_id", "") or "").strip(),
        sale_id=str(ctx.arg("sale_id", "") or "").strip(),
        chat_id=str(ctx.arg("chat_id", "") or "").strip(),
        purpose=str(ctx.arg("purpose", "") or "").strip(),
        note=str(ctx.arg("note", "") or "").strip(),
        request_id=str(ctx.arg("request_id", "") or "").strip(),
        actor=str(ctx.arg("actor", "panel") or "panel")[:120],
        qr_kind=str(ctx.arg("qr_kind", "dynamic") or "dynamic").strip(),
        qr_payload=str(ctx.arg("qr_payload", "") or "").strip())


@router.post("/api/sbp/confirm", audit="СБП: оплата подтверждена",
             doc="Подтвердить платёж: проводка в журнал и закрытие долга заказа")
def sbp_confirm(api: Any, ctx: Ctx):
    return _sbp(api).confirm(
        str(ctx.arg("id", "") or ctx.arg("payment_id", "") or "").strip(),
        actor=str(ctx.arg("actor", "panel") or "panel")[:120],
        account_id=str(ctx.arg("account_id", "") or "").strip(),
        note=str(ctx.arg("note", "") or "").strip())


@router.post("/api/sbp/reject", audit="СБП: платёж отклонён",
             doc="Отклонить неподтверждённый платёж (выручка не создаётся)")
def sbp_reject(api: Any, ctx: Ctx):
    return _sbp(api).reject(
        str(ctx.arg("id", "") or ctx.arg("payment_id", "") or "").strip(),
        reason=str(ctx.arg("reason", "") or "").strip(),
        actor=str(ctx.arg("actor", "panel") or "panel")[:120])


@router.post("/api/sbp/refund", audit="СБП: возврат выполнен",
             doc="Возврат подтверждённого платежа (руководитель/владелец, bank_done=1)")
def sbp_refund(api: Any, ctx: Ctx):
    return _sbp(api).refund(
        str(ctx.arg("id", "") or ctx.arg("payment_id", "") or "").strip(),
        actor=str(ctx.arg("actor", "panel") or "panel")[:120],
        note=str(ctx.arg("note", "") or "").strip(),
        bank_done=str(ctx.arg("bank_done", "") or "").strip().lower()
        in ("1", "true", "yes", "да"))
