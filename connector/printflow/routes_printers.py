"""Маршруты принтеров: каналы связи и память слотов AMS (17.0.19, 17.0.25).

Первый доменный модуль, вынесенный из общего диспетчера `api.py` после системы,
кассы и цеха. Логика живёт в `connection_state.py` и `ams_sync.py`, здесь
только транспорт.
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


@router.get("/api/pult/summary", doc="Сводка для пульта цеха: парк, очередь, AMS, катушки")
def pult_summary(api: Any, ctx: Ctx):
    """Один ответ вместо четырёх — телефону у станка так дешевле.

    Пульт цеха (18.0.4) каждый раз спрашивал парк, память слотов AMS и склад
    отдельными запросами. Здесь то же самое одним ответом, а форма снимка
    парка совпадает с `/api/state`, чтобы страница отрисовывала оба ответа
    одним кодом. Только чтение: изменения — отдельными маршрутами.
    """
    from .pult import summary

    printer_id = str(ctx.one("printer_id") or "").strip()
    return summary(api.db, api.manager, printer_id)


@router.get("/api/ams/memory", doc="Память слотов AMS: что в них стоит по базе")
def ams_memory(api: Any, ctx: Ctx):
    """Раскладка AMS из базы — работает и когда принтер молчит.

    Живая телеметрия живёт в снимке парка (`/api/state`) и пропадает вместе
    с принтером: выключили, перезапустили панель — и слоты «неизвестны».
    Здесь то, что PrintFlow успел запомнить сам: какая катушка стоит в слоте,
    сколько в ней осталось и когда это видели. ``stale`` — данным больше
    получаса, показывать их как живые нельзя.
    """
    from .ams_sync import MEMORY_STALE_MIN, backfill_slots, slot_memory

    printer_id = str(ctx.one("printer_id") or "").strip()
    # Привязки, которые уже лежат в базе, дописываем в память сразу: принтер
    # может быть выключен, а раскладка всё равно известна.
    backfill_slots(api.db, printer_id)
    slots = slot_memory(api.db, printer_id)
    printers = sorted({str(s.get("printer_id") or "") for s in slots if s.get("printer_id")})
    return {
        "printer_id": printer_id,
        "printers": printers,
        "stale_min": MEMORY_STALE_MIN,
        "slots": slots,
    }


@router.post("/api/ams/memory/clear", audit="AMS: память слотов очищена",
             doc="Забыть память слотов AMS (вручную)")
def ams_memory_clear(api: Any, ctx: Ctx):
    """Ручная чистка памяти слотов.

    Зачем. Катушку переставили руками, принтер выключен — панель продолжает
    показывать старую раскладку. Оператору нужен способ её сбросить, не
    дожидаясь синка.

    Что чистится. Только память (`ams_slots`). Привязки катушек на складе
    (`spools.ams_slot`) и история смен не трогаются: иначе одно нажатие
    ломало бы учёт расхода пластика. Без `slot` забывается весь принтер.
    """
    from .ams_sync import forget_slots, slot_memory

    printer_id = str(ctx.arg("printer_id") or "").strip()
    slot = ctx.arg("slot", None)
    slot_s = "" if slot is None else str(slot).strip()
    if not printer_id:
        return 400, {"error": "Укажите принтер: без него непонятно, чью память чистить"}
    removed = forget_slots(api.db, printer_id, slot_s or None)
    api.db.add_event(
        "ams", "Память слотов AMS очищена",
        f"Забыто слотов: {removed}" + (f", слот {slot_s}" if slot_s else "")
        + ". Привязки катушек на складе не тронуты.",
        printer_id, {"slot": slot_s, "removed": removed})
    return {"ok": True, "removed": removed, "printer_id": printer_id,
            "slot": slot_s, "slots": slot_memory(api.db, printer_id)}


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


@router.post("/api/studio/confirm", audit="Bambu Studio: подтверждение входящего проекта",
             doc="Подтверждение / запуск / постановка в очередь задания из Studio")
def studio_confirm(api: Any, ctx: Ctx):
    """Подтвердить входящий проект из Bambu Studio.

    Оператор может:
    - action = "start": запустить печать прямо сейчас на выбранном принтере
    - action = "queue": поставить в очередь печати
    - action = "reject": отклонить / закрыть уведомление
    """
    studio = getattr(api.manager, "studio", None) if api.manager else None
    if not studio:
        return 400, {"error": "Studio Gateway не инициализирован"}

    pending_id = str(ctx.arg("pending_id") or "").strip()
    action = str(ctx.arg("action") or "queue").strip().lower()

    if not pending_id:
        return 400, {"error": "Не указан pending_id"}

    pending = studio.pending_get(pending_id)
    if not pending:
        return 404, {"error": "Проект не найден или уже обработан"}

    if action == "reject":
        studio.pending_dismiss(pending_id)
        api.db.add_event(
            "studio", "Проект отклонен оператором",
            pending.get("filename") or "", pending.get("printer_id") or "",
            {"pending_id": pending_id}
        )
        return {"ok": True, "action": "rejected", "pending_id": pending_id}

    printer_id = str(ctx.arg("printer_id") or pending.get("printer_id") or "").strip()
    mapping = ctx.arg("ams_mapping", None)
    if not isinstance(mapping, list):
        mapping = pending.get("ams_mapping") or []
    plate = int(ctx.num("plate", pending.get("plate") or 1) or 1)

    payload = dict(pending.get("payload") or {})
    payload.update({
        "printer_id": printer_id,
        "plate": plate,
        "ams_mapping": mapping,
    })
    if "bed_level" in ctx.body:
        payload["bed_level"] = bool(ctx.body["bed_level"])
    if "flow_cali" in ctx.body:
        payload["flow_cali"] = bool(ctx.body["flow_cali"])
    if "timelapse" in ctx.body:
        payload["timelapse"] = bool(ctx.body["timelapse"])

    job = None
    started = False

    # Постановка в очередь
    try:
        job = api.manager.enqueue(payload)
    except Exception as exc:
        return 400, {"error": f"Ошибка добавления в очередь: {exc}"}

    job_id = job.get("id") or ""

    if action == "start":
        if not printer_id:
            return 400, {"error": "Для немедленного запуска необходимо выбрать принтер"}
        try:
            check = {}
            if hasattr(api.manager, "preflight"):
                check = api.manager.preflight(
                    printer_id, payload.get("file") or "", plate, mapping
                ) or {}
            if check.get("blocks"):
                first_block = (check.get("blocks") or [{}])[0].get("reason", "Запуск заблокирован preflight")
                return 400, {"error": f"Проверка перед запуском: {first_block}"}

            api.manager.start_job(job_id, printer_id)
            started = True
        except Exception as exc:
            return 400, {"error": f"Не удалось запустить печать: {exc}"}

    studio.pending_dismiss(pending_id)
    api.db.add_event(
        "studio", "Проект из Studio подтвержден",
        f"{pending.get('filename')} → {'запущен' if started else 'в очереди'}",
        printer_id,
        {"pending_id": pending_id, "job_id": job_id, "action": action, "started": started}
    )

    return {
        "ok": True,
        "action": action,
        "job": job,
        "started": started,
        "pending_id": pending_id,
    }


@router.get("/api/printer/health", doc="Самопроверка принтера")
def printer_health(api: Any, ctx: Ctx):
    printer = api.printer_or_fail(ctx.one("printer_id"))
    return printer.health() if hasattr(printer, "health") else {"ok": False}
