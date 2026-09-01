"""Системные маршруты PrintFlow 14.0: диагностика, схема настроек, поиск.

Первые маршруты, объявленные через реестр `router` (идеи 1, 2): логика
живёт в сервисах (`diagnostics.py`, `settings_schema.py`, `search.py`),
а здесь только транспорт — привязка пути, публичность, аудит.
"""
from __future__ import annotations

from typing import Any

from .router import Ctx, router
from .search import LIMIT_DEFAULT, Search


@router.get("/api/diagnostics", doc="Самодиагностика: потоки, база, бэкапы, парк")
def diagnostics(api: Any, ctx: Ctx):
    """Снимок состояния системы (идея 12). Секретов в ответе нет."""
    from . import diagnostics as service
    hours = int(ctx.num("hours", 24) or 24)
    return service.collect(api, hours=hours)


@router.get("/api/diagnostics/report", doc="Самодиагностика текстом (для бота)")
def diagnostics_report(api: Any, ctx: Ctx):
    from . import diagnostics as service
    return {"text": service.human_report(service.collect(api))}


@router.get("/api/settings/schema", doc="Схема настроек: типы, группы, границы")
def settings_schema(api: Any, ctx: Ctx):
    """Форма настроек и валидация строятся из одной схемы (идея 10)."""
    from . import settings_schema as service
    payload = service.describe()
    if ctx.one("diff") in ("1", "true", "yes"):
        payload["changed"] = service.diff_defaults(api.db.settings())
    return payload


@router.get("/api/search", public=False, doc="Единый поиск по цеху")
def search(api: Any, ctx: Ctx):
    """Один поиск вместо четырёх (идея 70)."""
    term = ctx.one("q") or ctx.one("term") or ctx.one("query")
    limit = int(ctx.num("limit", LIMIT_DEFAULT) or LIMIT_DEFAULT)
    groups = [g for g in (ctx.one("groups") or "").split(",") if g]
    service = getattr(api, "search", None)
    if service is None:
        service = Search(api.db)
        api.search = service
    from .search import GROUPS
    return service.run(term, limit, tuple(groups) if groups else GROUPS)


@router.get("/api/openapi.json", doc="Спецификация API из реестра маршрутов")
def openapi_spec(api: Any, ctx: Ctx):
    """Документ API, собранный из того же реестра, по которому ходят запросы (Н3).

    Разойтись с кодом не может: маршрут появляется в документе в момент
    объявления. `?pretty=1` — с отступами для чтения глазами.
    """
    from . import openapi as service
    spec = service.build()
    return spec


@router.get("/api/openapi.json/stats", doc="Сколько операций в спецификации")
def openapi_stats(api: Any, ctx: Ctx):
    from . import openapi as service
    return {"spec": service.counts(service.build()),
            "registry": api.router.count() if hasattr(api, "router") else None}
