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
