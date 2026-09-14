"""Маршруты вариаций товара (18.0).

Один товар легко живёт в сотнях сочетаний цвета, размера и пластика, поэтому
создание, привязка катушки и пересчёт цен — отдельные действия, а не поля
большой формы «сохранить карточку».

Маршруты объявлены декоратором, а не if-цепочкой в `api.py`: диспетчер
спрашивает реестр первым, а цепочка обязана только сокращаться — это правило
проекта (`docs/МАРШРУТЫ.md`, `test_get_routes_port`).

Ни один маршрут здесь не печатает и не списывает plastic: вариация — строка
справочника, расход появляется только в партии по решению оператора.
"""
from __future__ import annotations

from typing import Any

from .router import Ctx, router


@router.post("/api/nomenclature/variants/generate",
             audit="Номенклатура: созданы вариации товара",
             doc="Собрать вариации товара по осям (preview — только подсчёт)")
def nomenclature_variants_generate(api: Any, ctx: Ctx):
    """Создать вариации декартовым произведением осей.

    Повторный вызов не плодит дубли: существующие сочетания пропускаются.
    Слишком много сочетаний — отказ словами с числами, а не 500.
    """
    body = ctx.body or {}
    try:
        result = api.nom.generate_variants(
            body.get("nom_id", ""), body.get("axes"),
            sku_prefix=str(body.get("sku_prefix") or ""),
            preview=bool(body.get("preview")))
    except ValueError as exc:
        return 400, {"error": str(exc)}
    if result.get("created"):
        api.catalog_changed("variants_generate")
    return 200, {"ok": True, **result}


@router.post("/api/nomenclature/variants/recalc",
             doc="Пересчитать себестоимость и цены всех вариаций товара")
def nomenclature_variants_recalc(api: Any, ctx: Ctx):
    """Пересчёт по цене катушки каждой вариации.

    Своя цена владельца не затирается: машина считает, человек решает.
    """
    body = ctx.body or {}
    result = api.nom.recalc_variant_prices(
        body.get("nom_id", ""),
        only_auto=body.get("only_auto", True) is not False)
    return 200, {"ok": True, **result}


@router.post("/api/nomenclature/variant/spool",
             doc="Привязать катушку к вариации: цена грамма берётся из неё")
def nomenclature_variant_spool(api: Any, ctx: Ctx):
    """Выбор пластика для вариации — это выбор конкретной катушки.

    Цена грамма дальше берётся из этой катушки, а не из справочника: у
    магазина PLA за 1200 и PETG за 3600 за килограмм.
    """
    body = ctx.body or {}
    api.nom.save_variant({
        "id": body.get("id", ""), "nom_id": body.get("nom_id", ""),
        "spool_id": body.get("spool_id", "")})
    return 200, {"ok": True,
                 "item": api.nom.variant_economics(body.get("id", ""))}
