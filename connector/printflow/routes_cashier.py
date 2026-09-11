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


@router.get("/api/cashier/offline/qr", doc="QR магазина для оплаты без связи (кэш кассы)")
def cashier_offline_qr(api: Any, ctx: Ctx):
    """Код, который касса держит в телефоне: статический QR магазина.

    Без него офлайн-СБП невозможен, поэтому отдаваем его заранее (при входе и
    время от времени), а не в момент, когда сеть уже легла.
    """
    _cashier(api).require(_token(ctx))
    return _cashier(api).offline_qr()


@router.get("/api/cashier/incoming", doc="СБП-продажи кассы, ожидающие сверки")
def cashier_incoming(api: Any, ctx: Ctx):
    _cashier(api).require(_token(ctx))
    return _cashier(api).incoming()


@router.get("/api/cashier/sessions", doc="Кассы на связи: кто вошёл и когда отвечал")
def cashier_sessions(api: Any, ctx: Ctx):
    """Список для панели владельца: одна строка на телефон.

    Токен не спрашиваем намеренно: маршрут не отдаёт ни секретов, ни денег —
    только имена, время и «молчит N минут». Кассовые страницы защищены кодом,
    панель работает в LAN и так же открыта (см. docs/КАССА-16.0.md).
    """
    return _cashier(api).sessions()


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
        cashier_name=str(ctx.arg("cashier_name", "") or "").strip(),
        discount_pct=ctx.num("discount_pct", 0),
        manager_pin=str(ctx.arg("manager_pin", "") or ""),
        box_id=str(ctx.arg("box_id", "") or ""),
        # Штамп локальной продажи из очереди (17.0.8). Пусто — обычная продажа.
        offline_at=str(ctx.arg("offline_at", "") or "").strip())


@router.post("/api/cashier/offline/abandon", audit="Касса: снята офлайн-очередь",
             doc="Снять неотправленные записи офлайн-очереди с обязательной причиной")
def cashier_offline_abandon(api: Any, ctx: Ctx):
    body = ctx.body if isinstance(ctx.body, dict) else {}
    ids = body.get("request_ids") or ctx.arg("request_ids", []) or []
    if isinstance(ids, str):
        import json as _json
        try:
            ids = _json.loads(ids)
        except Exception:
            ids = []
    return _cashier(api).abandon_offline(
        list(ids or []), _token(ctx),
        str(ctx.arg("reason", "") or "").strip())


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
        reason=str(ctx.arg("reason", "") or "").strip(),
        # Для офлайн-заявки это обязательный вопрос: «ушёл ли товар с
        # покупателем». Ответа нет — отклонения нет (полка не должна врать).
        goods_taken=_tri(ctx.arg("goods_taken", None)))


def _tri(value: Any) -> bool | None:
    """Три состояния: не спросили / да / нет."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "да", "ушёл", "ушла", "taken"):
        return True
    if text in ("0", "false", "no", "нет", "вернулся", "returned"):
        return False
    return None


@router.post("/api/cashier/return", audit="Касса: принят возврат", idempotent=True,
             doc="Принять возврат товара и денег (старший): полка + возвратная проводка")
def cashier_return(api: Any, ctx: Ctx):
    return _cashier(api).return_sale(
        str(ctx.arg("sale_id", "") or "").strip(), _token(ctx), note=str(ctx.arg("note", "") or ""),
        lines=ctx.arg("lines", None) or None,
        request_id=str(ctx.arg("request_id", "") or ""))


@router.post("/api/cashier/sale/cancel", audit="Касса: продажа отменена",
             doc="Отменить наличную продажу текущей смены (любой кассир)")
def cashier_sale_cancel(api: Any, ctx: Ctx):
    return _cashier(api).cancel_sale(
        str(ctx.arg("sale_id", "") or ctx.arg("id", "") or "").strip(),
        _token(ctx))


@router.get("/api/cashier/shift/sales", doc="Продажи открытой смены")
def cashier_shift_sales(api: Any, ctx: Ctx):
    return _cashier(api).shift_sales(
        _token(ctx), str(ctx.arg("box_id", "") or ""))


@router.get("/api/cashier/shift/current", doc="Открытая смена с живым расчётом")
def cashier_shift_current(api: Any, ctx: Ctx):
    return _cashier(api).current_shift(
        _token(ctx), str(ctx.arg("box_id", "") or ""))


@router.post("/api/cashier/shift/open", audit="Касса: смена открыта",
             doc="Открыть смену: зафиксировать стартовый пересчёт ящика")
def cashier_shift_open(api: Any, ctx: Ctx):
    return _cashier(api).open_shift(
        _token(ctx), ctx.num("open_cash", 0),
        str(ctx.arg("box_id", "") or ""))


@router.post("/api/cashier/shift/close", audit="Касса: смена закрыта",
             doc="Закрыть смену: пересчёт ящика, расчёт сервера, расхождение")
def cashier_shift_close(api: Any, ctx: Ctx):
    return _cashier(api).close_shift(
        _token(ctx), ctx.num("close_cash", 0),
        note=str(ctx.arg("note", "") or ""),
        box_id=str(ctx.arg("box_id", "") or ""))


@router.post("/api/cashier/reconcile", audit="Касса: пересчёт ящика", idempotent=True,
             doc="Пересчёт наличных: закрыть текущий отсчёт расхождением и открыть новый")
def cashier_reconcile(api: Any, ctx: Ctx):
    # idempotent=True: повторный тап «Записать пересчёт» не закрывает смену
    # дважды и не открывает вторую — пересчёт сверяется с фактом один раз.
    return _cashier(api).reconcile(
        _token(ctx), ctx.num("counted_cash", 0),
        note=str(ctx.arg("note", "") or ""),
        box_id=str(ctx.arg("box_id", "") or ""))


@router.post("/api/cashier/collect", audit="Касса: выемка",
             doc="Выемка из ящика (только старший, в пределах остатка)")
def cashier_collect(api: Any, ctx: Ctx):
    return _cashier(api).collect(
        _token(ctx), ctx.num("amount", 0),
        note=str(ctx.arg("note", "") or ""),
        box_id=str(ctx.arg("box_id", "") or ""))
