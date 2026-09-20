"""Контракт FarmLoop-адаптеров PrintFlow.

Маршрут только сообщает готовность профиля. Физические команды и подготовка
файла идут через существующую загрузку задания и safety-gate менеджера.
"""
from __future__ import annotations

from typing import Any

from .config import DATA_DIR
from .farmloop import P1S_STAGE1, profile_payload
from .router import Ctx, router


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

    pid = str(body.get("printer_id") or "").strip()
    printer = manager.get(pid)
    if not printer:
        return 400, {"error": "Принтер не найден. Добавьте его в разделе «Принтеры»."}

    if not getattr(printer, "connected", False):
        printer_name = printer.record.get("name", "P1S") if hasattr(printer, "record") else "P1S"
        return 400, {"error": f"Принтер «{printer_name}» не подключен по локальной сети"}

    snap = printer.snapshot() if hasattr(printer, "snapshot") else {}
    state = str(snap.get("printer", {}).get("state", "UNKNOWN")).upper()
    if state not in ("IDLE", "FINISH"):
        return 400, {
            "error": f"Принтер занят (статус: {state}). Тестовая очистка стола разрешена только при статусе IDLE."
        }

    for cmd in commands:
        printer.gcode(cmd)

    printer_name = printer.record.get("name", "P1S") if hasattr(printer, "record") else "P1S"
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
        "printer_id": printer.id,
        "profile": requested,
    }
