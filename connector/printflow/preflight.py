"""Preflight — проверка перед стартом печати.

Возвращает {ok, blocks:[], warns:[], infos:[]} где block = нельзя стартовать,
warn = можно но с подтверждением, info = просто заметка.

Настройки: preflight_* в config.DEFAULT_SETTINGS
"""
from __future__ import annotations


from .estimate import parse_3mf_complete, _parse_gcode_head, _read_head
from pathlib import Path


def check_preflight(db, manager, printer_id: str, filename: str, plate: int = 1, ams_mapping: list[int] | None = None) -> dict:
    """Проверить можно ли запускать печать."""
    if not db.setting("preflight_enabled", True):
        return {"ok": True, "blocks": [], "warns": [], "infos": []}

    blocks: list[dict] = []
    warns: list[dict] = []
    infos: list[dict] = []

    # принтер
    printer = manager.get(printer_id) if manager else None
    if not printer:
        blocks.append({"code": "no_printer", "title": "Принтер не выбран", "detail": "Добавьте принтер в Настройках"})
        return {"ok": False, "blocks": blocks, "warns": warns, "infos": infos}
    snap = printer.snapshot()
    state = snap["printer"]["state"]
    if db.setting("preflight_block_idle", True) and state not in ("IDLE", "FINISH"):
        blocks.append({"code": "busy", "title": "Принтер занят", "detail": f"Состояние: {snap['printer']['state_label']} ({state}) — дождитесь завершения"})

    # HMS
    if db.setting("preflight_block_hms", True):
        problems = snap["printer"].get("problems") or []
        severe = [p for p in problems if p.get("severity") in ("error", "fatal")]
        if severe:
            worst = severe[0]
            blocks.append({"code": "hms", "title": "Ошибка принтера", "detail": f"{worst.get('title') or worst.get('code')} — устраните перед печатью"})
        elif problems and db.setting("preflight_block_hms", True) is False:
            # если не блок — warning
            pass

    # файл и оценка
    est = {}
    upload_path = None
    plates = []
    detail = {}
    try:
        from .config import UPLOAD_DIR
        from .estimate import estimate_file
        cand = UPLOAD_DIR / Path(filename).name
        if cand.exists():
            upload_path = cand
        else:
            cand2 = Path(str(db.setting("watch_folder_path", ""))).expanduser() / Path(filename).name if db.setting("watch_folder_path", "") else None
            if cand2 and cand2.exists():
                upload_path = cand2
        if upload_path:
            is_3mf = upload_path.name.lower().endswith(".3mf")
            if is_3mf:
                detail = parse_3mf_complete(upload_path)
                plates = detail.get("plates", [])
                if plates and 0 < plate <= len(plates):
                    est = plates[plate - 1]
                elif plates:
                    est = plates[0]
                # если grams 0 но есть estimate_file с slice_info — взять оттуда
                if not est.get("grams"):
                    ef = estimate_file(upload_path)
                    if ef.get("grams") or ef.get("total_grams"):
                        # сохранить total для проверки остатка
                        est["grams"] = ef.get("total_grams") or ef.get("grams")
                        est["minutes"] = ef.get("total_minutes") or ef.get("minutes")
                        if not plates and ef.get("plates"):
                            plates = ef["plates"]
                slice_info = detail.get("slice_info", {})
                bed_need = est.get("bed_type") or (slice_info.get("bed_type") if isinstance(slice_info, dict) else None)
                if bed_need and db.setting("preflight_warn_nozzle", True):
                    infos.append({"code": "bed", "title": "Тип стола в файле", "detail": str(bed_need)})
                nd = est.get("nozzle_diameter")
                printer_nozzle = None
                try:
                    printer_nozzle = float(snap.get("printer", {}).get("nozzle_size") or printer.record.get("nozzle_size") or 0.4)
                except Exception:
                    printer_nozzle = 0.4
                if nd and db.setting("preflight_warn_nozzle", True):
                    if abs(nd - printer_nozzle) > 0.05:
                        warns.append({"code": "nozzle", "title": "Диаметр сопла не совпадает", "detail": f"В файле {nd}мм, в принтере {printer_nozzle}мм — детализация пострадает"})
            else:
                # .gcode
                text = _read_head(upload_path)
                est = _parse_gcode_head(text) if text else {}
        else:
            # локальной копии нет — пробуем оценить с SD карты принтера (важно для имён с запятой/пробелом)
            try:
                if manager:
                    sd_est = manager._slicer_estimate(printer, filename)
                    if sd_est.get("grams") or sd_est.get("minutes"):
                        est = {"grams": sd_est.get("grams"), "minutes": sd_est.get("minutes"),
                               "material": sd_est.get("material", ""), "color": sd_est.get("color", "")}
            except Exception:
                pass
    except Exception as exc:
        infos.append({"code": "estimate", "title": "Не удалось прочитать оценку", "detail": str(exc)})

    # материал и филамент
    if db.setting("preflight_block_material", True) and est.get("material"):
        need = str(est.get("material") or "").upper()
        trays = snap["ams"].get("trays", []) or []
        active = next((t for t in trays if t.get("active")), None) if trays else None
        # также проверить маппинг
        if ams_mapping is not None and trays:
            # ams_mapping — список слотов
            for slot in ams_mapping:
                tr = next((t for t in trays if int(t.get("slot", -1)) == int(slot)), None)
                if tr and tr.get("type") and tr.get("type").upper() != need and need:
                    blocks.append({"code": "material_map", "title": "Не тот материал в слоте", "detail": f"Слот {slot}: {tr.get('type')} vs нужно {need}"})
        elif active and active.get("type"):
            loaded = str(active.get("type") or "").upper()
            if loaded and loaded != need:
                blocks.append({"code": "material", "title": "Не тот материал в AMS", "detail": f"В активном слоте {loaded}, а файл требует {need} — замените катушку"})
        # многоцвет
        if est.get("filaments") and len(est["filaments"]) > 1:
            # проверить что все материалы есть в AMS
            need_types = {str(f.get("type") or "").upper() for f in est["filaments"]}
            have_types = {str(t.get("type") or "").upper() for t in trays}
            missing = need_types - have_types
            if missing and db.setting("preflight_block_material", True):
                warns.append({"code": "multicolor", "title": "Многоцвет: не все материалы в AMS", "detail": f"Нужно {', '.join(need_types)}, в AMS {', '.join(have_types) or 'пусто'}"})

    # остаток пластика
    if db.setting("preflight_block_filament", True) and est.get("grams"):
        need_g = float(est.get("grams") or 0)
        # если qty в заказе — умножить, но тут нет qty, берем 1
        trays = snap["ams"].get("trays", []) or []
        active = next((t for t in trays if t.get("active")), None) if trays else None
        if active:
            # найти spool
            try:
                from .accounting import Accounting
                acc = Accounting(db)
                spool = acc.pick_spool(printer.id, str(active.get("slot")), active.get("type"), active.get("uuid"))
                if spool:
                    if not int(float(spool.get("verified", 1) or 0)):
                        blocks.append({"code": "spool_unverified", "title": "Катушка AMS не проверена", "detail": "Уточните массу и цену катушки в складе перед стартом"})
                    remain_g = float(spool.get("remaining_grams") or 0)
                    if remain_g and remain_g < need_g * 1.15:
                        blocks.append({"code": "filament", "title": "Мало пластика", "detail": f"Нужно ~{need_g:.0f}г (+15% запас), в катушке {remain_g:.0f}г — замените"})
                    elif remain_g and remain_g < need_g * 2:
                        warns.append({"code": "filament_low", "title": "Пластика впритык", "detail": f"Нужно {need_g:.0f}г, осталось {remain_g:.0f}г"})
            except Exception:
                pass
        # также по AMS remain%
        if active and active.get("remain") is not None:
            try:
                if float(active.get("remain")) < 15:
                    warns.append({"code": "ams_low", "title": "Мало пластика в AMS", "detail": f"Слот {active.get('label')}: {active.get('remain')}% — скоро кончится"})
            except Exception:
                pass

    # SD карта
    if db.setting("preflight_warn_sd", True):
        if not snap["printer"].get("sdcard"):
            warns.append({"code": "sd", "title": "SD-карта не обнаружена", "detail": "Принтер не видит SD — печать может не стартовать"})
        else:
            # если есть информация о занятости — пока info
            pass

    # влажность AMS
    if db.setting("preflight_warn_humidity", True):
        hum = snap["ams"].get("humidity")
        if hum is not None:
            try:
                if float(hum) > float(db.setting("dry_humidity_threshold", 55)):
                    # только для гигроскопичных — но пока для всех
                    warns.append({"code": "humidity", "title": "Высокая влажность в AMS", "detail": f"{hum}% при пороге {db.setting('dry_humidity_threshold',55)}% — просушите пластик"})
            except Exception:
                pass

    # калибровка
    if db.setting("preflight_warn_calibration", True):
        try:
            # проверить maintenance due
            # просто смотрим snapshot maintenance
            maint = snap.get("maintenance") or {}
            if maint.get("due", 0) > 0:
                infos.append({"code": "maintenance", "title": "Просрочено обслуживание", "detail": f"{maint.get('due')} задач просрочено — проверьте ТО"})
        except Exception:
            pass

    ok = len(blocks) == 0
    return {"ok": ok, "blocks": blocks, "warns": warns, "infos": infos, "estimate": est, "plates": plates}
