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


# ------------------------------------------------------------ 18.11: слои
def _gcode_job_file(api: Any, ctx: Ctx) -> tuple[Any, Any, str]:
    """Файл для шкалы слоёв: печатающееся задание принтера или файл по имени.

    Возвращает ``(путь | None, задание | None, причина)``. Причина — уже
    человеческая фраза для пустого состояния виджета.
    """
    from .config import UPLOAD_DIR
    from .gcode_layers import resolve_job_file
    from .library import LIBRARY_DIR

    roots = (UPLOAD_DIR, LIBRARY_DIR)
    name = str(ctx.one("file") or "").strip()
    if name:
        path = resolve_job_file(name, roots)
        if path is None:
            return None, None, "Файл не найден в загрузках и библиотеке"
        return path, None, ""
    printer_id = str(ctx.one("printer_id") or "").strip()
    if not printer_id:
        return None, None, "Не указан принтер"
    from .bed_projection import running_job
    job = running_job(api.db, printer_id)
    if job is None:
        return None, None, "Сейчас на этом принтере ничего не печатается"
    job_file = str(job.get("file") or "")
    if not job_file:
        return None, job, "У задания не записан файл"
    path = resolve_job_file(job_file, roots)
    if path is None:
        return None, job, ("Файл задания не найден локально — задание запущено "
                           "с карты принтера или из Studio без копии")
    if path.suffix.lower() not in (".3mf", ".gcode"):
        return None, job, "Слои считаются только по G-code или 3MF с нарезкой"
    return path, job, ""


def _job_brief(job: Any) -> dict | None:
    if not job:
        return None
    return {
        "id": str(job.get("id") or ""),
        "name": str(job.get("name") or job.get("file") or ""),
        "file": str(job.get("file") or ""),
        "plate": job.get("plate"),
        "layers": job.get("layers"),
        "progress": job.get("progress"),
    }


@router.get("/api/gcode/layers",
            doc="Шкала слоёв: послойный индекс G-code печатающегося задания (18.11)")
def gcode_layers(api: Any, ctx: Ctx):
    """Индекс слоёв: список слоёв с высотой, отрезками, пластиком и M73.

    Пока файл разбирается в фоне, отвечает ``building: true`` — виджет
    опрашивает повторно. Без локального файла — ``has: false`` и причина.
    """
    from .gcode_layers import index_for

    path, job, reason = _gcode_job_file(api, ctx)
    if path is None:
        return {"has": False, "reason": reason, "job": _job_brief(job)}
    plate = None
    try:
        plate = int(job.get("plate") or 0) if job else int(ctx.one("plate") or 0)
    except (TypeError, ValueError):
        plate = None
    idx = index_for(path, plate or None, wait=1.5)
    if idx is None:
        return {"has": False, "job": _job_brief(job),
                "reason": "В 3MF нет нарезанного G-code (Metadata/plate_N.gcode) — "
                          "файл нужно нарезать в Studio"}
    out = idx.overview()
    out.update({"has": True, "job": _job_brief(job), "file": path.name, "reason": ""})
    return out


@router.get("/api/gcode/layer",
            doc="Шкала слоёв: отрезки одного слоя по корзинам (18.11)")
def gcode_layer(api: Any, ctx: Ctx):
    from .gcode_layers import index_for

    path, job, reason = _gcode_job_file(api, ctx)
    if path is None:
        return 404, {"error": reason}
    try:
        number = int(ctx.one("layer") or 0)
    except (TypeError, ValueError):
        number = 0
    plate = None
    try:
        plate = int(job.get("plate") or 0) if job else int(ctx.one("plate") or 0)
    except (TypeError, ValueError):
        plate = None
    idx = index_for(path, plate or None, wait=1.5)
    if idx is None:
        return 404, {"error": "В файле нет нарезанного G-code"}
    if idx.building:
        return 202, {"building": True, "total": len(idx.layers)}
    layer = idx.layer(number)
    if layer is None:
        return 404, {"error": f"Слоя {number} нет: в файле {len(idx.layers)} слоёв"}
    out = layer.detail()
    out["total"] = len(idx.layers)
    return out


# ------------------------------------------------------- 18.11: «Объявить сейчас»
@router.post("/api/studio/announce", audit="Bambu Studio: объявление шлюза вручную",
             doc="Разослать SSDP-объявления шлюза и станков немедленно (18.11)")
def studio_announce(api: Any, ctx: Ctx):
    """Кнопка «Объявить сейчас» в карточке шлюза.

    Не ждёт периода рассылки: Studio, открытая только что, увидит шлюз и
    ретранслируемые станки сразу. В ответе — сколько объявлений ушло и куда.
    """
    studio = getattr(api.manager, "studio", None) if api.manager else None
    if not studio:
        return 400, {"error": "Шлюз Studio не инициализирован"}
    result = studio.announce_now()
    if not result.get("ok"):
        return 400, {"error": result.get("error") or "Шлюз выключен", **result}
    return result
