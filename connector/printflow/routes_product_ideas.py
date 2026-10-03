"""Endpoints for importing and reviewing 3D model ideas."""
from __future__ import annotations

from typing import Any

from .router import Ctx, router


@router.get("/api/ideas/models", doc="Сохранённые кандидаты 3D-моделей")
def ideas_models_list(api: Any, ctx: Ctx):
    from .product_ideas import ProductIdeas
    return 200, {"ok": True, "items": ProductIdeas(api.db).list()}


@router.post("/api/ideas/models/import", doc="Импорт карточки модели с Thingiverse, Printables или MakerWorld")
def ideas_models_import(api: Any, ctx: Ctx):
    from .product_ideas import ProductIdeas
    try:
        item = ProductIdeas(api.db).import_url(ctx.body.get("url", ""),
                             bool(ctx.body.get("refresh")), ctx.body.get("days", 90))
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "item": item}


@router.post("/api/ideas/models/decision", doc="Сохранить решение по кандидату модели")
def ideas_models_decision(api: Any, ctx: Ctx):
    from .product_ideas import ProductIdeas
    try:
        item = ProductIdeas(api.db).decide(ctx.body.get("id", ""),
                                          str(ctx.body.get("status") or ""),
                                          ctx.body.get("quantity", 0),
                                          ctx.body.get("note", ""),
                                          ctx.body.get("price", 0), ctx.body.get("grams", 0),
                                          ctx.body.get("hours", 0), ctx.body.get("other_cost", 0),
                                          ctx.body.get("filament_cost", 0), ctx.body.get("machine_cost", 0))
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "item": item}


@router.post("/api/ideas/models/outcome", doc="Сохранить фактический результат пробной партии")
def ideas_models_outcome(api: Any, ctx: Ctx):
    from .product_ideas import ProductIdeas
    try:
        item = ProductIdeas(api.db).record_trial(
            ctx.body.get("id", ""), ctx.body.get("sold", 0), ctx.body.get("returns", 0),
            ctx.body.get("defects", 0), ctx.body.get("revenue", 0))
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "item": item}
