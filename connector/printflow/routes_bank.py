"""Маршруты поступлений из банка (раунд «авто-СБП»).

Транспорт: импорт выписки, список, ручная привязка и подтверждение.
Логика и гарантии «деньги не ломаются» — в ``bank_receipts.py`` и ``sbp.py``.
"""
from __future__ import annotations

import json
from typing import Any

from .router import Ctx, router
from .bank_receipts import BankReceipts


def _bank(api: Any) -> BankReceipts:
    service = getattr(api, "bank", None)
    if service is None:
        from .bank_receipts import BankReceipts as _BankReceipts
        service = _BankReceipts(api.db, api.acc, api.sbp)
        api.bank = service
    return service


@router.get("/api/bank/state", doc="Поступления из банка: сводка и очередь на сверку")
def bank_state(api: Any, ctx: Ctx):
    return _bank(api).state()


@router.get("/api/bank/receipts", doc="Журнал поступлений (?status, ?limit)")
def bank_receipts(api: Any, ctx: Ctx):
    return {"receipts": _bank(api).list(
        status=ctx.one("status", ""), limit=int(ctx.num("limit", 100) or 100))}


@router.post("/api/bank/import", audit="Банк: выписка разнесена", idempotent=False,
             doc="Разнести выписку Т-Банка (CSV текстом или списком rows)")
def bank_import(api: Any, ctx: Ctx):
    body = ctx.body if isinstance(ctx.body, dict) else {}
    rows = body.get("rows")
    if rows is None:
        csv_text = str(body.get("csv", "") or "")
        from .bank_receipts import parse_tbank_csv
        rows = parse_tbank_csv(csv_text)
    elif isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except Exception:
            rows = []
    return _bank(api).ingest(
        rows if isinstance(rows, list) else [],
        source=str(body.get("source", "tbank_csv") or "tbank_csv"),
        actor=str(body.get("actor", "panel") or "panel")[:120])


@router.post("/api/bank/link", audit="Банк: поступление связано с платежом",
             doc="Привязать поступление к СБП-платежу (id/номер) вручную")
def bank_link(api: Any, ctx: Ctx):
    return _bank(api).link(
        str(ctx.arg("receipt_id", "") or ctx.arg("id", "") or "").strip(),
        str(ctx.arg("payment_id", "") or ctx.arg("payment_number", "") or "").strip(),
        actor=str(ctx.arg("actor", "panel") or "panel")[:120],
        confirm=str(ctx.arg("confirm", "") or "").strip().lower()
        in ("1", "true", "yes", "да"),
        force=str(ctx.arg("force", "") or "").strip().lower()
        in ("1", "true", "yes", "да"),
        pin=str(ctx.arg("pin", "") or ""))


@router.post("/api/bank/confirm", audit="Банк: поступление подтверждено",
             idempotent=True,
             doc="Подтвердить СБП-платёж, привязанный к поступлению")
def bank_confirm(api: Any, ctx: Ctx):
    # idempotent=True: задвоенный клик по «Подтвердить» не гоняет проводку
    # второй раз и не пишет второй записи в журнал (гарантия и в ядре СБП).
    return _bank(api).confirm(
        str(ctx.arg("receipt_id", "") or ctx.arg("id", "") or "").strip(),
        actor=str(ctx.arg("actor", "panel") or "panel")[:120],
        pin=str(ctx.arg("pin", "") or ""))
