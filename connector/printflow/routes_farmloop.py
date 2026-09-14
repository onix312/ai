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
