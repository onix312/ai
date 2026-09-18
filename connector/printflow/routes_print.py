"""Маршруты печати: каталог форм и сами листы (обновление 18.0).

Контент-студия держала печать внутри себя: формы жили в `content.py` под
путями `/api/content/*` рядом с генераторами постов. Когда студия ушла,
печать осталась — она нужна цеху каждый день, поэтому получила собственный
раздел и собственный модуль.

Правило раздела: **лист печатается сервером**, браузер только открывает
готовый HTML. Так форма одинакова на любом устройстве в сети, а правка
размеров и цвета не зависит от того, из какого окна нажали «Печать».

* `GET /api/print/forms` — каталог: что можно напечатать и на какой бумаге;
* `GET /api/print/stickers` — стикеры в упаковку (шаблон, размер, тираж);
* `GET /api/print/signs` — таблички цеха 92 × 65 мм, 6 на лист;
* `GET /api/print/business-card` — визитки 85 × 55 мм, 4 на лист;
* `GET /api/print/report` — цеховой отчёт за период;
* `GET /api/print/warranty` — гарантийный талон заказа.

Неизвестный шаблон, размер или лист — это `400` с объяснением, а не пустая
страница: раньше `/api/content/stickers?kind=nope` отдавал `200` и чистый
лист, и понять, что шаблон просто не найден, было нельзя. Числовые параметры
проверяются так же строго: `?copies=abc` — ошибка, а не тихий ноль, иначе
оператор получает «напечатано», а на бумаге пусто.
"""
from __future__ import annotations

from typing import Any

from . import printforms as pf
from .router import Ctx, router


def _int_param(ctx: Ctx, name: str, default: int, low: int, high: int) -> int:
    """Целое из строки запроса с понятной ошибкой вместо тихой подстановки."""
    raw = ctx.one(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"Параметр {name} должен быть числом, получено: {raw}") from None
    if not low <= value <= high:
        raise ValueError(f"Параметр {name} должен быть от {low} до {high}, получено: {value}")
    return value


@router.get("/api/print/forms", doc="Каталог печатных форм: бумага, размер, источник")
def get_print_forms(api: Any, ctx: Ctx):
    from .printing import groups

    group = ctx.one("group")
    if group and group not in groups():
        return 400, {"error": f"Неизвестная группа: {group}. Доступно: "
                              + ", ".join(groups())}
    from .printing import forms_catalog
    forms = [item for item in forms_catalog()
             if not group or item.get("group") == group]
    return 200, {"groups": groups(), "forms": forms}


@router.get("/api/print/stickers", doc="Стикеры в упаковку (печатный лист HTML)")
def get_print_stickers(api: Any, ctx: Ctx):
    from .printing import stickers
    try:
        copies = _int_param(ctx, "copies", 0, 0, 200)
        html = stickers(ctx.one("kind", "all"), ctx.one("size", pf.DEFAULT_LABEL_SIZE),
                        ctx.one("sheet", "A4"), copies)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, {"html": html}


@router.get("/api/print/signs", doc="Таблички цеха 92 × 65 мм, 6 на лист (HTML)")
def get_print_signs(api: Any, ctx: Ctx):
    from .printing import signs_html
    try:
        copies = _int_param(ctx, "copies", 0, 0, 24)
        html = signs_html(ctx.one("kind", "delivery"), ctx.one("note", ""),
                          copies, ctx.one("sheet", "A4"))
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, {"html": html}


@router.get("/api/print/business-card", doc="Визитки 85 × 55 мм, 4 на лист (HTML)")
def get_print_business_card(api: Any, ctx: Ctx):
    from .printing import business_card_html
    customer_id = ctx.one("customer_id")
    try:
        html = business_card_html(api.db, customer_id)
    except ValueError as exc:
        return 404, {"error": str(exc)}
    return 200, {"html": html, "personalized": bool(customer_id)}


@router.get("/api/print/report", doc="Цеховой отчёт за период (печатный лист HTML)")
def get_print_report(api: Any, ctx: Ctx):
    from .printing import workshop_report_html
    try:
        days = _int_param(ctx, "days", 30, 7, 366)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, {"html": workshop_report_html(api.db, days), "days": days}


@router.get("/api/print/warranty", doc="Гарантийный талон заказа (печатный лист HTML)")
def get_print_warranty(api: Any, ctx: Ctx):
    from .printing import warranty_html
    order_id = ctx.one("order_id") or ctx.one("id")
    if not order_id:
        return 400, {"error": "Не указан заказ: нужен параметр order_id"}
    try:
        html = warranty_html(api.db, order_id, ctx.one("customer_id"))
    except ValueError as exc:
        return 404, {"error": str(exc)}
    return 200, {"html": html}
