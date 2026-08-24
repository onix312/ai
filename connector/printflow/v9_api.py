"""HTTP-маршруты цеха 9.0: склад, AMS, приход, пресеты, QR, SD.

Возвращает ``None``, если путь не из 9.0 — вызывающий код идёт дальше.
"""
from __future__ import annotations

from typing import Any

from .accounting import num


def workshop(api):
    w = getattr(api, "workshop", None)
    if w is not None:
        return w
    from .workshop_v9 import WorkshopV9
    w = WorkshopV9(
        api.db,
        getattr(api, "repo", None),
        getattr(api, "shopping", None),
        getattr(api, "manager", None),
        getattr(api, "acc", None),
    )
    try:
        w.ensure_schema()
    except Exception:
        pass
    api.workshop = w
    return w


def _one(query: dict, key: str, default: str = "") -> str:
    return (query.get(key) or [default])[0]


def _files_payload(printer, folder: str) -> dict:
    from . import sd_browser
    folder = sd_browser.sanitize_remote_path(folder)
    files = printer.files.list_files(folder)
    return {
        "path": folder,
        "files": files,
        "crumbs": sd_browser.breadcrumbs(folder),
        "parent": sd_browser.parent_path(folder),
    }


def dispatch_get(api, path: str, query: dict) -> tuple[int, Any] | None:
    if path == "/api/workshop/about":
        return 200, workshop(api).about()
    if path == "/api/workshop/inventory":
        return 200, workshop(api).inventory_summary()
    if path == "/api/workshop/enough":
        return 200, workshop(api).enough_for_next(_one(query, "printer_id"))
    if path == "/api/workshop/docs":
        return 200, {"docs": workshop(api).workshop_docs(
            _one(query, "kind"), int(num(_one(query, "limit", "80"), 80)))}
    if path == "/api/workshop/doc":
        row = workshop(api).workshop_doc(_one(query, "id"))
        return (200, row) if row else (404, {"error": "Документ не найден"})
    if path == "/api/workshop/scrap":
        return 200, {"items": workshop(api).scrap_list(
            _one(query, "spool_id"), int(num(_one(query, "limit", "80"), 80)))}
    if path == "/api/workshop/suppliers":
        return 200, {"suppliers": workshop(api).suppliers()}
    if path == "/api/workshop/presets":
        return 200, {"presets": workshop(api).plate_presets()}
    if path == "/api/workshop/shift":
        return 200, workshop(api).shift_state(_one(query, "day"))
    if path == "/api/workshop/slots":
        return 200, {"history": workshop(api).slot_history(
            _one(query, "printer_id"), int(num(_one(query, "limit", "80"), 80)))}
    if path == "/api/workshop/qr":
        info = workshop(api).qr_wizard(_one(query, "spool_id"), _one(query, "kind", "spool"))
        try:
            from .qrgen import svg as qr_svg
            info["svg"] = qr_svg(info.get("payload") or "", scale=4)
        except Exception:
            info["svg"] = ""
        try:
            extra = api.qr_target(info.get("path") or "/spool.html", info.get("query") or "")
            info.update(extra)
        except Exception:
            pass
        return 200, info
    if path in ("/api/workshop/label", "/api/workshop/spool-label"):
        html = workshop(api).spool_label_html(_one(query, "spool_id") or _one(query, "id"))
        return 200, {"html": html, "ok": True}
    if path == "/api/printer/preview":
        from . import sd_browser
        printer = api.printer_or_fail(_one(query, "printer_id"))
        remote = sd_browser.sanitize_remote_path(_one(query, "path"))
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
        return 200, sd_browser.preview_from_bytes(
            remote.rsplit("/", 1)[-1], data, path=remote)
    return None


def dispatch_post(api, path: str, body: dict, query: dict) -> tuple[int, Any] | None:
    body = body or {}
    if not path.startswith("/api/workshop/"):
        return None
    w = workshop(api)
    if path == "/api/workshop/location":
        return 200, w.set_spool_location(
            str(body.get("id") or body.get("spool_id") or ""),
            str(body.get("location") or "shop"),
            str(body.get("note") or ""),
        )
    if path == "/api/workshop/slot":
        return 200, w.bind_unique_slot(
            str(body.get("id") or body.get("spool_id") or ""),
            body.get("slot"),
            str(body.get("printer_id") or ""),
            str(body.get("tray_uuid") or ""),
            body.get("force") is True,
            str(body.get("note") or ""),
        )
    if path == "/api/workshop/scrap":
        return 200, w.record_scrap(
            str(body.get("spool_id") or body.get("id") or ""),
            body.get("grams"),
            str(body.get("reason") or ""),
            str(body.get("note") or ""),
            body.get("confirmed") is True,
            str(body.get("request_id") or ""),
        )
    if path == "/api/workshop/supplier/save":
        return 200, {"ok": True, "supplier": w.save_supplier(body)}
    if path == "/api/workshop/supplier/delete":
        w.delete_supplier(str(body.get("id") or ""))
        return 200, {"ok": True}
    if path == "/api/workshop/supplier/apply-price":
        return 200, w.apply_supplier_price(
            str(body.get("id") or body.get("supplier_id") or ""),
            str(body.get("material") or ""),
        )
    if path == "/api/workshop/receipt":
        return 200, w.filament_receipt(
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
    if path == "/api/workshop/preset/save":
        return 200, {"ok": True, "preset": w.save_plate_preset(body)}
    if path == "/api/workshop/preset/delete":
        w.delete_plate_preset(str(body.get("id") or ""))
        return 200, {"ok": True}
    if path == "/api/workshop/preset/apply":
        return 200, w.apply_plate_preset(
            str(body.get("job_id") or ""), str(body.get("preset_id") or body.get("id") or ""))
    if path == "/api/workshop/mixed-label":
        return 200, w.attach_mixed_label(
            str(body.get("job_id") or body.get("id") or ""),
            body.get("items") if isinstance(body.get("items"), list) else None,
            int(num(body.get("plates"), 1) or 1),
            str(body.get("label") or ""),
        )
    if path == "/api/workshop/no-auto":
        return 200, w.set_no_auto(
            str(body.get("id") or body.get("job_id") or ""),
            body.get("no_auto", True) not in (False, 0, "0", "false"),
        )
    if path == "/api/workshop/clone":
        return 200, w.clone_job(str(body.get("id") or body.get("job_id") or ""))
    if path == "/api/workshop/shift":
        return 200, w.check_shift(
            str(body.get("item_id") or body.get("id") or ""),
            body.get("done", True) not in (False, 0, "0", "false"),
            str(body.get("note") or ""),
            str(body.get("day") or ""),
        )
    return None
