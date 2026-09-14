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
