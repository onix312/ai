"""Маршруты цеха: склад, AMS, приход, пресеты, QR, SD (идеи 1, 9, З3).

Перенос из `v9_api.py` — диспетчера, который появился в релизе 9.0, вызывался
первым и возвращал `None` для «чужих» путей. Здесь те же обработчики, но
объявлены в реестре `router`, поэтому у каждого маршрута видны публичность,
роли, аудит и идемпотентность, а справка API строится автоматически.
"""
from __future__ import annotations

from typing import Any

from .accounting import num
from .router import Ctx, router


def workshop(api: Any):
    """Сервис цеха: берём готовый или создаём и запоминаем на api."""
    existing = getattr(api, "workshop", None)
    if existing is not None:
        return existing
    from .workshop_v9 import WorkshopV9
    service = WorkshopV9(
        api.db,
        getattr(api, "repo", None),
        getattr(api, "shopping", None),
        getattr(api, "manager", None),
        getattr(api, "acc", None),
    )
    try:
        service.ensure_schema()
    except Exception:
        pass
    api.workshop = service
    return service


def _files_payload(printer: Any, folder: str) -> dict:
    """Содержимое папки SD-карты (используется и legacy-веткой api.py)."""
    from . import sd_browser
    folder = sd_browser.sanitize_remote_path(folder)
    return {
        "path": folder,
        "files": printer.files.list_files(folder),
        "crumbs": sd_browser.breadcrumbs(folder),
        "parent": sd_browser.parent_path(folder),
    }


def _flag(value: Any, default: bool = True) -> bool:
    """Флаг из тела запроса: `false`, 0, "0" и "false" — это «нет»."""
    if value is None:
        return default
    return value not in (False, 0, "0", "false", "False")


# ------------------------------------------------------------------ чтение
@router.get("/api/workshop/about", doc="Паспорт цеха: версии, режимы, счётчики")
def workshop_about(api: Any, ctx: Ctx):
    return workshop(api).about()


@router.get("/api/workshop/inventory", doc="Сводка по пластику и AMS")
def workshop_inventory(api: Any, ctx: Ctx):
    return workshop(api).inventory_summary()


@router.get("/api/workshop/enough", doc="Хватит ли пластика на следующее задание")
def workshop_enough(api: Any, ctx: Ctx):
    return workshop(api).enough_for_next(ctx.one("printer_id"))


@router.get("/api/workshop/docs", doc="Документы цеха: приход, списание, брак")
def workshop_docs(api: Any, ctx: Ctx):
    return {"docs": workshop(api).workshop_docs(
        ctx.one("kind"), int(num(ctx.one("limit", "80"), 80)))}


@router.get("/api/workshop/doc", doc="Один документ цеха")
def workshop_doc(api: Any, ctx: Ctx):
    row = workshop(api).workshop_doc(ctx.one("id"))
    return (200, row) if row else (404, {"error": "Документ не найден"})


@router.get("/api/workshop/scrap", doc="Журнал списаний пластика")
def workshop_scrap_list(api: Any, ctx: Ctx):
    return {"items": workshop(api).scrap_list(
        ctx.one("spool_id"), int(num(ctx.one("limit", "80"), 80)))}


@router.get("/api/workshop/suppliers", doc="Поставщики пластика")
def workshop_suppliers(api: Any, ctx: Ctx):
    return {"suppliers": workshop(api).suppliers()}


@router.get("/api/workshop/presets", doc="Пресеты плит")
def workshop_presets(api: Any, ctx: Ctx):
    return {"presets": workshop(api).plate_presets()}


@router.get("/api/workshop/shift", doc="Чек-лист смены")
def workshop_shift_state(api: Any, ctx: Ctx):
    return workshop(api).shift_state(ctx.one("day"))


@router.get("/api/workshop/slots", doc="История слотов AMS")
def workshop_slots(api: Any, ctx: Ctx):
    return {"history": workshop(api).slot_history(
        ctx.one("printer_id"), int(num(ctx.one("limit", "80"), 80)))}


@router.get("/api/workshop/qr", doc="QR-мастер:payload, SVG и ссылка")
def workshop_qr(api: Any, ctx: Ctx):
    info = workshop(api).qr_wizard(ctx.one("spool_id"), ctx.one("kind", "spool"))
    try:
        from .qrgen import svg as qr_svg
        info["svg"] = qr_svg(info.get("payload") or "", scale=4)
    except Exception:
        info["svg"] = ""
    try:
        info.update(api.qr_target(info.get("path") or "/spool.html", info.get("query") or ""))
    except Exception:
        pass
    return info


@router.get("/api/workshop/label", doc="Этикетка катушки (HTML)")
@router.get("/api/workshop/spool-label", doc="Этикетка катушки (HTML, старый адрес)")
def workshop_label(api: Any, ctx: Ctx):
    return {"ok": True,
            "html": workshop(api).spool_label_html(ctx.one("spool_id") or ctx.one("id"))}


@router.get("/api/printer/preview", doc="Превью модели с SD-карты принтера")
def printer_preview(api: Any, ctx: Ctx):
    from . import sd_browser
    printer = api.printer_or_fail(ctx.one("printer_id"))
    remote = sd_browser.sanitize_remote_path(ctx.one("path"))
    if not sd_browser.can_print(remote):
        raise ValueError("Превью только для 3MF и G-code, не для логов и таймлапса")
    data = b""
    files = getattr(printer, "files", None)
    if files is not None:
        try:
            data = files.read_head(remote, 2 * 1024 * 1024) or b""
        except Exception:
            data = b""
        if len(data) < 64:
            try:
                data = files.download(remote, 2 * 1024 * 1024) or data
            except Exception:
                pass
    return sd_browser.preview_from_bytes(remote.rsplit("/", 1)[-1], data, path=remote)


# ------------------------------------------------------------------ запись
@router.post("/api/workshop/location", audit="Катушка: место хранения")
def workshop_location(api: Any, ctx: Ctx):
    body = ctx.body
    return workshop(api).set_spool_location(
        str(body.get("id") or body.get("spool_id") or ""),
        str(body.get("location") or "shop"),
        str(body.get("note") or ""),
    )


@router.post("/api/workshop/slot", audit="Катушка: привязка к слоту AMS")
def workshop_slot(api: Any, ctx: Ctx):
    body = ctx.body
    return workshop(api).bind_unique_slot(
        str(body.get("id") or body.get("spool_id") or ""),
        body.get("slot"),
        str(body.get("printer_id") or ""),
        str(body.get("tray_uuid") or ""),
        body.get("force") is True,
        str(body.get("note") or ""),
    )


@router.post("/api/workshop/scrap", audit="Списание пластика", idempotent=True)
def workshop_scrap(api: Any, ctx: Ctx):
    body = ctx.body
    return workshop(api).record_scrap(
        str(body.get("spool_id") or body.get("id") or ""),
        body.get("grams"),
        str(body.get("reason") or ""),
        str(body.get("note") or ""),
        body.get("confirmed") is True,
        str(body.get("request_id") or ""),
    )


@router.post("/api/workshop/supplier/save", audit="Поставщик: сохранение")
def workshop_supplier_save(api: Any, ctx: Ctx):
    return {"ok": True, "supplier": workshop(api).save_supplier(ctx.body)}


@router.post("/api/workshop/supplier/delete", audit="Поставщик: удаление")
def workshop_supplier_delete(api: Any, ctx: Ctx):
    workshop(api).delete_supplier(str(ctx.body.get("id") or ""))
    return {"ok": True}


@router.post("/api/workshop/supplier/apply-price", audit="Поставщик: цена в справочник")
def workshop_supplier_price(api: Any, ctx: Ctx):
    body = ctx.body
    return workshop(api).apply_supplier_price(
        str(body.get("id") or body.get("supplier_id") or ""),
        str(body.get("material") or ""),
    )


@router.post("/api/workshop/receipt", audit="Приход пластика", idempotent=True)
def workshop_receipt(api: Any, ctx: Ctx):
    body = ctx.body
    return workshop(api).filament_receipt(
        items=body.get("items") if isinstance(body.get("items"), list) else None,
        material=str(body.get("material") or ""),
        color_name=str(body.get("color_name") or ""),
        color_hex=str(body.get("color_hex") or ""),
        brand=str(body.get("brand") or ""),
        spool_count=body.get("spool_count", 1),
        spool_grams=body.get("spool_grams", 1000),
        total_amount=body.get("total_amount", 0),
        price_per_kg=body.get("price_per_kg", 0),
        supplier=str(body.get("supplier") or ""),
        supplier_id=str(body.get("supplier_id") or ""),
        shopping_id=str(body.get("shopping_id") or ""),
        account_id=str(body.get("account_id") or ""),
        note=str(body.get("note") or ""),
        confirmed=body.get("confirmed") is True,
        request_id=str(body.get("request_id") or ""),
    )


@router.post("/api/workshop/preset/save", audit="Пресет плиты: сохранение")
def workshop_preset_save(api: Any, ctx: Ctx):
    return {"ok": True, "preset": workshop(api).save_plate_preset(ctx.body)}


@router.post("/api/workshop/preset/delete", audit="Пресет плиты: удаление")
def workshop_preset_delete(api: Any, ctx: Ctx):
    workshop(api).delete_plate_preset(str(ctx.body.get("id") or ""))
    return {"ok": True}


@router.post("/api/workshop/preset/apply", audit="Пресет плиты: применение к заданию")
def workshop_preset_apply(api: Any, ctx: Ctx):
    body = ctx.body
    return workshop(api).apply_plate_preset(
        str(body.get("job_id") or ""),
        str(body.get("preset_id") or body.get("id") or ""),
    )


@router.post("/api/workshop/mixed-label", audit="Смешанная плита: маркировка")
def workshop_mixed_label(api: Any, ctx: Ctx):
    body = ctx.body
    return workshop(api).attach_mixed_label(
        str(body.get("job_id") or body.get("id") or ""),
        body.get("items") if isinstance(body.get("items"), list) else None,
        int(num(body.get("plates"), 1) or 1),
        str(body.get("label") or ""),
    )


@router.post("/api/workshop/no-auto", audit="Задание: флаг «без автозапуска»")
def workshop_no_auto(api: Any, ctx: Ctx):
    body = ctx.body
    return workshop(api).set_no_auto(
        str(body.get("id") or body.get("job_id") or ""),
        _flag(body.get("no_auto"), True),
    )


@router.post("/api/workshop/clone", audit="Задание: клонирование")
def workshop_clone(api: Any, ctx: Ctx):
    body = ctx.body
    return workshop(api).clone_job(str(body.get("id") or body.get("job_id") or ""))


@router.post("/api/workshop/shift", audit="Смена: отметка чек-листа")
def workshop_shift_check(api: Any, ctx: Ctx):
    body = ctx.body
    return workshop(api).check_shift(
        str(body.get("item_id") or body.get("id") or ""),
        _flag(body.get("done"), True),
        str(body.get("note") or ""),
        str(body.get("day") or ""),
    )
