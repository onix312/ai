"""Маршруты принтеров: состояние каналов связи (17.0.19).

Первый доменный модуль, вынесенный из общего диспетчера `api.py` после системы,
кассы и цеха. Логика живёт в `connection_state.py`, здесь только транспорт.
"""
from __future__ import annotations

from typing import Any

from .connection_state import CHANNELS, ConnectionState
from .router import Ctx, router


@router.get("/api/printer/links", doc="Состояние каналов связи: MQTT, FTPS, HTTP, камера")
def printer_links(api: Any, ctx: Ctx):
    """Каналы связи по каждому принтеру — без общего «online».

    Сводка намеренно не склеивает каналы: печать при мёртвом FTPS возможна,
    файлы — нет, и оператору нужно знать, что именно молчит и что безопасно
    сделать. `?printer_id=` — один принтер, без параметра — все включённые.
    """
    links = ConnectionState(api.db)
    printer_id = ctx.one("printer_id").strip()
    if printer_id:
        items = [links.snapshot(links.require_printer(printer_id))]
    else:
        items = links.all_printers()
    return {
        "channels": [{"channel": name, "title": title, "breaks": breaks}
                     for name, (title, _stale, breaks) in CHANNELS.items()],
        "printers": items,
    }


# ----------------------------------------------------- чтение состояния парка
# Перенесено из if-цепочки `Api.get()` (17.0.20). Поведение то же: те же
# параметры, те же словари в ответе, те же исключения. Отличие одно — маршрут
# виден в реестре и в спецификации API.


@router.get("/api/printers", doc="Список принтеров без секретов")
def printers(api: Any, ctx: Ctx):
    return {"printers": api.repo.printers()}


@router.get("/api/printer/telemetry", doc="Телеметрия принтера за период")
def printer_telemetry(api: Any, ctx: Ctx):
    minutes = int(ctx.num("minutes", 180) or 180)
    return {"points": api.manager.guard.telemetry(ctx.one("printer_id"), minutes)}


@router.get("/api/printer/maintenance", doc="Регламентные работы и наработка часов")
def printer_maintenance(api: Any, ctx: Ctx):
    guard = api.manager.guard
    printer_id = ctx.one("printer_id")
    return {"tasks": guard.maintenance(printer_id),
            "hours": guard.runtime_hours(printer_id)}


@router.get("/api/printer/alerts", doc="Активные предупреждения принтера")
def printer_alerts(api: Any, ctx: Ctx):
    return {"alerts": api.manager.guard.alerts(ctx.one("printer_id"))}


@router.get("/api/printer/shots", doc="Список снимков камеры принтера")
def printer_shots(api: Any, ctx: Ctx):
    printer = api.printer_or_fail(ctx.one("printer_id"))
    return {"shots": printer.camera.snapshot_list()}


@router.get("/api/printer/health", doc="Самопроверка принтера")
def printer_health(api: Any, ctx: Ctx):
    printer = api.printer_or_fail(ctx.one("printer_id"))
    return printer.health() if hasattr(printer, "health") else {"ok": False}
