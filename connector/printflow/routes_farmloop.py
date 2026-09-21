"""Контракт FarmLoop-адаптеров PrintFlow.

Маршрут только сообщает готовность профиля. Физические команды и подготовка
файла идут через существующую загрузку задания и safety-gate менеджера.
"""
from __future__ import annotations

from typing import Any

from .config import DATA_DIR
from .farmloop import P1S_STAGE1, profile_payload
from .router import Ctx, router

# Статусы станка, при которых тестовая очистка стола безопасна: стол пуст,
# печать не идёт. Всё остальное (RUNNING, PAUSE, OFFLINE, UNKNOWN) — занято.
FREE_STATES = ("IDLE", "FINISH")


def _printer_name(printer: Any) -> str:
    """Человеческое имя станка для текста ошибки."""
    record = getattr(printer, "record", None)
    if isinstance(record, dict) and record.get("name"):
        return str(record["name"])
    return str(getattr(printer, "id", "") or "P1S")


def _printer_state(printer: Any) -> str:
    """Статус станка из его снимка (пусто, если снимка нет)."""
    try:
        snap = printer.snapshot() if hasattr(printer, "snapshot") else {}
    except Exception:
        return ""
    if not isinstance(snap, dict):
        return ""
    printer_part = snap.get("printer")
    if not isinstance(printer_part, dict):
        return ""
    return str(printer_part.get("state") or "").upper()


def _test_clean_blockers(printer: Any) -> list[str]:
    """Почему на этом станке нельзя запускать тестовую очистку стола.

    Пустой список — станок свободен: подключен и не печатает.
    """
    reasons: list[str] = []
    if not getattr(printer, "connected", False):
        reasons.append("не подключен")
    state = _printer_state(printer)
    if state not in FREE_STATES:
        reasons.append(f"занят (статус: {state or 'неизвестен'})")
    return reasons


def _pick_free_printer(manager: Any) -> tuple[Any | None, list[str]]:
    """Перебор пула: первый свободный станок и причины по остальным.

    18.12.1: панель слала ``{confirmed: true}`` без ``printer_id``, а
    ``manager.get('')`` возвращал ПЕРВЫЙ принтер словаря — обычно занятый
    или отключённый. «Тестовая очистка стола» отвечала «Принтер занят» и
    выглядела сломанной, хотя свободный станок в парке был.
    """
    printers = getattr(manager, "printers", None)
    if not isinstance(printers, dict) or not printers:
        return None, []
    free = None
    notes: list[str] = []
    for printer in printers.values():
        reasons = _test_clean_blockers(printer)
        if not reasons and free is None:
            free = printer
            continue
        if reasons:
            notes.append(f"«{_printer_name(printer)}» — {'; '.join(reasons)}")
    return free, notes


@router.get("/api/farmloop/profile", doc="Профиль FarmLoop для принтера и готовность шаблона")
def farmloop_profile(api: Any, ctx: Ctx):
    requested = str(ctx.one("profile", P1S_STAGE1.id) or P1S_STAGE1.id)
    if requested != P1S_STAGE1.id:
        return 400, {"error": f"Неизвестный профиль FarmLoop: {requested}",
                      "available": [P1S_STAGE1.id]}
    template = DATA_DIR / "farmloop-templates" / f"{requested}.gcode"
    payload = profile_payload(P1S_STAGE1)
    payload.update({
        "available": True,
        "template_installed": template.is_file(),
        "template_name": template.name if template.is_file() else "",
        "can_prepare": template.is_file(),
        "input_format": ".gcode",
        "blocked_reason": "" if template.is_file() else (
            "Проверенный шаблон не установлен. Сначала соберите и проверьте "
            "механику FarmLoop Stage 1 на P1S."),
    })
    return payload


@router.get("/api/farmloop/settings", doc="Настройки и safety-gate FarmLoop")
def farmloop_settings(api: Any, ctx: Ctx):
    """Возвращает только не секретные настройки FarmLoop и вычисленные гейты."""
    keys = (
        "farmloop_profile", "farmloop_mechanics_verified",
        "farmloop_template_verified", "farmloop_sensor_mode",
        "farmloop_sensor_timeout_s", "farmloop_camera_threshold_pct",
        "farmloop_cooldown_s", "farmloop_pusher_enabled",
        "farmloop_bender_enabled", "farmloop_auto_next",
        "farmloop_unattended_series", "farmloop_max_cycles",
        "farmloop_max_detach_attempts", "slicer_provider",
        "slicer_auto_postprocess_farmloop",
    )
    settings = {key: api.db.setting(key, "") for key in keys}
    profile = str(settings.get("farmloop_profile") or P1S_STAGE1.id)
    template = DATA_DIR / "farmloop-templates" / f"{profile}.gcode"
    settings["template_installed"] = template.is_file()
    settings["can_prepare"] = bool(template.is_file())
    physical = all(bool(settings.get(key)) for key in (
        "farmloop_mechanics_verified", "farmloop_template_verified",
        "farmloop_pusher_enabled", "farmloop_bender_enabled"))
    sensing = settings.get("farmloop_sensor_mode") in {"sensor", "camera", "both"}
    settings["can_auto_next"] = bool(physical and sensing and template.is_file())
    settings["can_unattended_series"] = bool(
        settings["can_auto_next"] and int(settings.get("farmloop_max_cycles") or 0) > 1)
    settings["blocked_reason"] = ""
    if not settings["can_prepare"]:
        settings["blocked_reason"] = "Нет установленного шаблона FarmLoop"
    elif not settings["can_auto_next"]:
        settings["blocked_reason"] = "Не подтверждены механика и пустая платформа"
    return {"profile": profile, "settings": settings}


@router.get("/api/farmloop/template", doc="G-code активного шаблона FarmLoop и разобранные параметры")
def farmloop_template_get(api: Any, ctx: Ctx):
    from .farmloop import DEFAULT_STAGE1_TEMPLATE, parse_template_blocks
    requested = str(ctx.one("profile", P1S_STAGE1.id) or P1S_STAGE1.id)
    if requested != P1S_STAGE1.id:
        return 400, {"error": f"Неизвестный профиль FarmLoop: {requested}",
                      "available": [P1S_STAGE1.id]}
    template = DATA_DIR / "farmloop-templates" / f"{requested}.gcode"
    installed = template.is_file()
    if installed:
        content = template.read_text(encoding="utf-8", errors="replace")
    else:
        content = DEFAULT_STAGE1_TEMPLATE
    blocks = parse_template_blocks(content)
    return {
        "ok": True,
        "profile": requested,
        "template_installed": installed,
        "template_name": template.name if installed else f"{requested}.gcode",
        "gcode": content,
        "blocks": blocks,
    }


@router.post("/api/farmloop/template", audit="Конвейер: сохранение G-code шаблона",
             doc="Сохранить и проверить G-code шаблон FarmLoop")
def farmloop_template_post(api: Any, ctx: Ctx):
    from .farmloop import (
        DEFAULT_STAGE1_TEMPLATE,
        FarmLoopError,
        build_template_from_blocks,
        parse_template_blocks,
        validate_template,
    )
    body = ctx.body or {}
    requested = str(body.get("profile") or P1S_STAGE1.id)
    if requested != P1S_STAGE1.id:
        return 400, {"error": f"Неизвестный профиль FarmLoop: {requested}",
                      "available": [P1S_STAGE1.id]}

    if body.get("reset_default"):
        gcode = DEFAULT_STAGE1_TEMPLATE
    elif isinstance(body.get("blocks"), dict):
        b = body["blocks"]
        gcode = build_template_from_blocks(
            cooldown_temp=b.get("cooldown_temp", 35),
            fan_assist=bool(b.get("fan_assist", True)),
            z_lift=b.get("z_lift", 15.0),
            pusher_y=b.get("pusher_y", 245.0),
            pusher_speed=b.get("pusher_speed", 2400),
            custom_gcode=str(b.get("custom_gcode") or ""),
        )
    else:
        gcode = str(body.get("gcode") or "").strip()
        if not gcode:
            return 400, {"error": "Передайте gcode или параметры blocks"}

    try:
        report = validate_template(gcode)
    except FarmLoopError as exc:
        return 400, {"error": str(exc)}

    template_dir = DATA_DIR / "farmloop-templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    template_path = template_dir / f"{requested}.gcode"
    template_path.write_text(gcode, encoding="utf-8", newline="\n")

    if getattr(api, "db", None):
        api.db.add_event(
            "farmloop",
            "Обновлен шаблон FarmLoop",
            f"Шаблон {requested} ({report['commands']} команд)",
            "",
            {"profile": requested, "commands": report["commands"]},
        )

    return {
        "ok": True,
        "profile": requested,
        "template_installed": True,
        "template_name": template_path.name,
        "commands": report["commands"],
        "blocks": parse_template_blocks(gcode),
        "gcode": gcode,
    }


@router.post("/api/farmloop/test-clean", audit="Конвейер: тестовая очистка стола",
             doc="Тестовый запуск G-code толкателя FarmLoop на свободном принтере")
def farmloop_test_clean(api: Any, ctx: Ctx):
    from .farmloop import BEGIN, END, FarmLoopError, _lines, validate_template

    body = ctx.body or {}
    if body.get("confirmed") is not True:
        return 400, {"error": "Подтвердите тестовую очистку стола"}

    requested = str(body.get("profile") or P1S_STAGE1.id)
    template_path = DATA_DIR / "farmloop-templates" / f"{requested}.gcode"
    if not template_path.is_file():
        return 400, {
            "error": f"Для профиля {requested} не установлен шаблон FarmLoop. "
                     "Сначала настройте и сохраните шаблон в конструкторе."
        }

    template_text = template_path.read_text(encoding="utf-8", errors="replace")
    try:
        validate_template(template_text)
    except FarmLoopError as exc:
        return 400, {"error": f"Невалидный шаблон FarmLoop: {exc}"}

    lines = _lines(template_text)
    start = next(i for i, line in enumerate(lines) if line.strip() == BEGIN)
    finish = next(i for i, line in enumerate(lines) if line.strip() == END)
    commands = [
        line.strip() for line in lines[start + 1:finish]
        if line.strip() and not line.strip().startswith(";")
    ]
    if not commands:
        return 400, {"error": "В шаблоне FarmLoop нет команд движения"}

    manager = getattr(api, "manager", None)
    if not manager:
        return 400, {"error": "Менеджер принтеров недоступен"}

    # 18.12.1: станок выбирается явно. Без printer_id маршрут перебирает пул
    # и берёт первый свободный, а не «первый в словаре» (тот почти всегда
    # занят или отключён). Причины отказа перечисляются по каждому станку —
    # владелец видит, что именно освободить.
    pid = str(body.get("printer_id") or "").strip()
    pool = getattr(manager, "printers", None)
    pool = pool if isinstance(pool, dict) else {}
    auto_notes: list[str] = []
    if pid:
        printer = manager.get(pid)
        if not printer:
            return 400, {"error": "Принтер не найден. Добавьте его в разделе «Принтеры»."}
        reasons = _test_clean_blockers(printer)
        if reasons:
            name = _printer_name(printer)
            return 400, {
                "error": f"«{name}» — {'; '.join(reasons)}. "
                         "Тестовая очистка стола разрешена только на подключенном "
                         "свободном станке (статус IDLE или FINISH).",
                "printer_id": pid,
                "printer": name,
                "blocked": reasons,
            }
    else:
        printer, auto_notes = _pick_free_printer(manager)
        if printer is None and not pool:
            # Пула нет (менеджер без словаря printers) — прежняя ветка «первого».
            printer = manager.get("")
            if printer is not None:
                reasons = _test_clean_blockers(printer)
                if reasons:
                    auto_notes = [f"«{_printer_name(printer)}» — {'; '.join(reasons)}"]
                    printer = None
        if printer is None:
            if not auto_notes:
                return 400, {
                    "error": "Принтеров нет: добавьте станок в разделе «Принтеры»."
                }
            listing = "; ".join(auto_notes)
            return 400, {
                "error": f"Нет свободного станка: {listing}. "
                         "Освободите станок или выберите другой в списке.",
                "blocked": auto_notes,
            }

    printer_name = _printer_name(printer)
    for cmd in commands:
        printer.gcode(cmd)

    if getattr(api, "db", None):
        api.db.add_event(
            "farmloop",
            "Тестовая очистка стола",
            f"Выполнено {len(commands)} команд на «{printer_name}»",
            "",
            {"printer_id": printer.id, "profile": requested, "commands": len(commands)},
        )

    return {
        "ok": True,
        "executed_commands": len(commands),
        "printer": printer_name,
        "printer_id": getattr(printer, "id", pid),
        "profile": requested,
        "auto_selected": not pid,
    }


# 18.12.1+ новые маршруты

@router.get("/api/farmloop/metrics", doc="Метрики FarmLoop")
def farmloop_metrics(api: Any, ctx: Ctx):
    manager = getattr(api, "manager", None)
    if not manager or not hasattr(manager, "farmloop_metrics"):
        return {"success": 0, "fail": 0, "success_rate": 0}
    return manager.farmloop_metrics()


@router.get("/api/farmloop/digital-twin", doc="Цифровой двойник конвейера")
def farmloop_digital_twin(api: Any, ctx: Ctx):
    manager = getattr(api, "manager", None)
    pid = str(ctx.one("printer_id", "") or "")
    if manager and hasattr(manager, "farmloop_digital_twin"):
        return manager.farmloop_digital_twin(pid)
    return {"ok": False, "error": "менеджер недоступен"}


@router.get("/api/farmloop/roi", doc="ROI стол для проверки детали")
def farmloop_roi_get(api: Any, ctx: Ctx):
    raw = api.db.setting("bed_roi", "")
    try:
        import json as _json
        roi = _json.loads(raw) if isinstance(raw, str) and raw else raw
    except Exception:
        roi = None
    return {"roi": roi, "raw": raw}


@router.post("/api/farmloop/roi", audit="Конвейер: ROI", doc="Сохранить ROI")
def farmloop_roi_post(api: Any, ctx: Ctx):
    body = ctx.body or {}
    roi = body.get("roi")
    if not isinstance(roi, list) or len(roi) != 4:
        return 400, {"error": "roi должен быть [x0,y0,x1,y1] 0..1"}
    try:
        vals = [float(x) for x in roi]
        if not all(0.0 <= v <= 1.0 for v in vals):
            return 400, {"error": "координаты 0..1"}
        if vals[2] <= vals[0] or vals[3] <= vals[1]:
            return 400, {"error": "x1>x0 и y1>y0"}
        import json as _json
        api.db.set_setting("bed_roi", _json.dumps(vals))
        return {"ok": True, "roi": vals}
    except Exception as exc:
        return 400, {"error": str(exc)}


@router.post("/api/farmloop/timeline/preview", doc="Live G-code preview из timeline")
def farmloop_timeline_preview(api: Any, ctx: Ctx):
    from .farmloop import FarmLoopError, build_from_timeline
    body = ctx.body or {}
    blocks = body.get("blocks")
    if not isinstance(blocks, list):
        return 400, {"error": "blocks — список"}
    try:
        res = build_from_timeline(blocks)
        return res
    except FarmLoopError as exc:
        return 400, {"error": str(exc), "ok": False}


@router.post("/api/farmloop/timeline/validate", doc="Dry-run валидация timeline")
def farmloop_timeline_validate(api: Any, ctx: Ctx):
    from .farmloop import FarmLoopError, build_from_timeline, dry_run_validate
    body = ctx.body or {}
    if body.get("blocks"):
        try:
            res = build_from_timeline(body["blocks"])
            return {"ok": True, "warnings": res.get("warnings", []), "preview_path": res.get("preview_path", [])}
        except FarmLoopError as exc:
            return 400, {"ok": False, "error": str(exc)}
    gcode = str(body.get("gcode") or "")
    if not gcode:
        return 400, {"error": "gcode или blocks"}
    res = dry_run_validate(gcode)
    if not res.get("ok"):
        return 400, res
    return res


@router.post("/api/farmloop/timeline/save", audit="Конвейер: timeline", doc="Сохранить timeline как шаблон")
def farmloop_timeline_save(api: Any, ctx: Ctx):
    from .farmloop import FarmLoopError, build_from_timeline, validate_template
    body = ctx.body or {}
    blocks = body.get("blocks")
    profile = str(body.get("profile") or P1S_STAGE1.id)
    if not isinstance(blocks, list):
        return 400, {"error": "blocks — список"}
    try:
        built = build_from_timeline(blocks)
        gcode = built["gcode"]
        validate_template(gcode)
    except FarmLoopError as exc:
        return 400, {"error": str(exc)}
    template_dir = DATA_DIR / "farmloop-templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    template_path = template_dir / f"{profile}.gcode"
    template_path.write_text(gcode, encoding="utf-8", newline="\n")
    try:
        import json as _json
        (template_dir / f"{profile}.timeline.json").write_text(_json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    api.db.add_event("farmloop", "Timeline сохранён", f"{profile} {len(blocks)} блоков", "", {"profile": profile})
    return {"ok": True, "profile": profile, "gcode": gcode, "preview_path": built.get("preview_path", []), "warnings": built.get("warnings", [])}


@router.get("/api/farmloop/timeline", doc="Загрузить timeline")
def farmloop_timeline_get(api: Any, ctx: Ctx):
    profile = str(ctx.one("profile", P1S_STAGE1.id) or P1S_STAGE1.id)
    p = DATA_DIR / "farmloop-templates" / f"{profile}.timeline.json"
    if not p.is_file():
        return {"ok": True, "blocks": [], "profile": profile, "exists": False}
    try:
        import json as _json
        blocks = _json.loads(p.read_text(encoding="utf-8"))
        return {"ok": True, "blocks": blocks, "profile": profile, "exists": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/api/farmloop/queue/balance", doc="Балансировщик очереди")
def farmloop_queue_balance(api: Any, ctx: Ctx):
    manager = getattr(api, "manager", None)
    if not manager:
        return {"ok": False, "error": "менеджер недоступен"}
    if hasattr(manager, "balance_queue"):
        try:
            res = manager.balance_queue()
            if isinstance(res, dict) and res.get("ok"):
                jobs = api.db.query("SELECT COUNT(*) n FROM print_jobs WHERE state='queued'")
                return {"ok": True, "plan": res.get("plan", []), "jobs": (jobs[0].get("n") if jobs else 0), "printers": len(getattr(manager, "printers", {}))}
        except Exception:
            pass
    try:
        jobs = api.db.query("SELECT * FROM print_jobs WHERE state='queued' ORDER BY priority DESC, datetime(created_at)")
        printers = list(getattr(manager, "printers", {}).values())
        snaps = {}
        for pr in printers:
            try:
                snaps[pr.id] = pr.snapshot()
            except Exception:
                snaps[pr.id] = {}
        plan = []
        used = set()
        for job in jobs:
            need_mat = str(job.get("material") or "").upper()
            best_pid = ""
            best_score = -1
            for pr in printers:
                if pr.id in used and len(jobs) > len(printers):
                    continue
                snap = snaps.get(pr.id, {})
                state = (snap.get("printer") or {}).get("state") or "OFFLINE"
                if state not in ("IDLE", "FINISH"):
                    continue
                loaded = {str(t.get("type") or "").upper() for t in (snap.get("ams") or {}).get("trays", [])}
                score = 0
                if need_mat and need_mat in loaded:
                    score += 10
                if not need_mat:
                    score += 5
                qlen = len([j for j in jobs if j.get("printer_id") == pr.id])
                score -= qlen
                if score > best_score:
                    best_score = score
                    best_pid = pr.id
            if best_pid:
                plan.append({"job_id": job["id"], "printer_id": best_pid, "score": best_score, "material": need_mat})
                used.add(best_pid)
        return {"ok": True, "plan": plan, "jobs": len(jobs), "printers": len(printers)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/api/farmloop/ams/cleanup", audit="AMS: очистка фантомов", doc="Очистить фантомные AMS слоты")
def farmloop_ams_cleanup(api: Any, ctx: Ctx):
    from .repo import cleanup_ams_phantoms
    res = cleanup_ams_phantoms(api.db)
    return res


@router.get("/api/farmloop/bed/projection", doc="Проекция слоя на камеру")
def farmloop_bed_projection(api: Any, ctx: Ctx):
    raw = api.db.setting("bed_projection", "")
    try:
        import json as _json
        proj = _json.loads(raw) if isinstance(raw, str) and raw else raw
    except Exception:
        proj = None
    return {"projection": proj, "raw": raw}


@router.post("/api/farmloop/bed/projection", audit="Конвейер: проекция", doc="Сохранить проекцию")
def farmloop_bed_projection_post(api: Any, ctx: Ctx):
    body = ctx.body or {}
    proj = body.get("projection")
    if not isinstance(proj, dict):
        return 400, {"error": "projection — объект homography"}
    try:
        import json as _json
        api.db.set_setting("bed_projection", _json.dumps(proj))
        return {"ok": True, "projection": proj}
    except Exception as exc:
        return 400, {"error": str(exc)}


@router.get("/api/farmloop/spools/delta", doc="ΔE поиск катушек")
def farmloop_spools_delta(api: Any, ctx: Ctx):
    target = str(ctx.one("hex", ctx.one("color", "")) or "")
    material = str(ctx.one("material", "") or "")
    if not target:
        return 400, {"error": "hex required"}
    try:
        from .accounting import Accounting
        acc = Accounting(api.db)
        res = acc.spools_by_delta(target, material)
        return {"ok": True, "spools": res, "target": target}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
