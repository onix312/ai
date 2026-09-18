"""Маршруты собственного слайсера PrintFlow (Stage 1, P1S).

Контракт повторяет FarmLoop: маршрут честно сообщает готовность, физические
пределы и причину блокировки, а не «всё готово». Нарезка ничего не отправляет
на принтер: файл появляется в библиотеке, а в очередь его ставит оператор —
или отдельный явный запрос с подтверждением, если гейты это разрешают.
"""
from __future__ import annotations

from pathlib import Path

from .config import DATA_DIR, UPLOAD_DIR
from .router import Ctx, router
from .slicer import SlicerError
from .slicer_engine import (
    ENGINE_ID,
    ENGINE_VERSION,
    MAX_MODEL_MB,
    MAX_TRIANGLES,
    slice_model,
)
from .slicer_profile import (
    P1S,
    PROFILE_ID,
    audit_gcode,
    audit_model,
    profile_payload,
    settings_from,
    settings_payload,
    validate_settings,
)
from .slicer_run import SliceMachine, config_from_settings

# Поля, которые запрос может переопределить на один вызов.
_OVERRIDE_KEYS = (
    "layer_height", "first_layer_height", "walls", "infill_percent",
    "infill_pattern", "supports", "support_spacing_mm", "brim", "brim_width_mm",
    "nozzle_mm", "nozzle_temp", "bed_temp", "speed_mm_s", "travel_speed_mm_s",
    "material", "ams_slot", "extrusion_width_mm", "top_solid_layers",
    "bottom_solid_layers", "seam", "retract_mm", "retract_speed_mm_s",
    "retract_min_travel_mm", "zhop_mm", "fan_percent", "flow", "center_model",
)


def _overrides(body: dict) -> dict:
    return {key: body[key] for key in _OVERRIDE_KEYS if key in body}


def _raw_settings(api) -> dict:
    try:
        return api.db.settings() or {}
    except Exception:                # нет базы — работаем на заводских
        return {}


def _resolve_input(api, body: dict) -> Path:
    """Путь к модели: по id в библиотеке или по имени в uploads."""
    from .library import FileLibrary
    fid = str(body.get("id") or body.get("fid") or "").strip()
    if fid:
        return Path(FileLibrary(api.db).resolve(fid))
    name = str(body.get("file") or body.get("name") or "").strip()
    if not name:
        raise SlicerError("Укажите модель: id из библиотеки или имя файла")
    return UPLOAD_DIR / Path(name).name


def _prepare(api, body: dict):
    """Общая часть маршрутов: настройки, профиль, гейты, валидация."""
    raw = _raw_settings(api)
    settings = settings_from(raw, P1S, _overrides(body))
    warnings = validate_settings(settings, P1S)
    machine = SliceMachine(config_from_settings(raw))
    return settings, warnings, machine


@router.get("/api/slicer/engine", doc="Движок нарезки PrintFlow: статус, версия и пределы Stage 1")
def slicer_engine(api, ctx: Ctx):
    raw = _raw_settings(api)
    machine = SliceMachine(config_from_settings(raw))
    return {
        "id": ENGINE_ID,
        "version": ENGINE_VERSION,
        "available": True,
        "provider": str(raw.get("slicer_provider") or "external"),
        "provider_active": str(raw.get("slicer_provider") or "external") == ENGINE_ID,
        "profile": PROFILE_ID,
        "input_formats": list(P1S.input_formats),
        "limits": {
            "model_mb": MAX_MODEL_MB,
            "triangles": MAX_TRIANGLES,
            "bed_mm": [P1S.bed_width_mm, P1S.bed_height_mm],
            "max_print_height_mm": P1S.max_print_height_mm,
            "layer_height_mm": [P1S.min_layer_height, P1S.max_layer_height],
        },
        "unsupported": [
            "переменная высота слоя",
            "тонкие стенки и заполнение зазоров",
            "поддержки-деревья и интерфейсный слой",
            "мультиматериал и назначение AMS по цвету",
            "глажка, спиральный режим, дуги",
        ],
        "stage": machine.stage.value,
        "blocked_reason": machine.blocked_reason(),
    }


@router.get("/api/slicer/profile", doc="Профиль слайсера: пределы принтера и готовность движка")
def slicer_profile_route(api, ctx: Ctx):
    raw = _raw_settings(api)
    settings = settings_from(raw, P1S)
    machine = SliceMachine(config_from_settings(raw))
    payload = profile_payload(P1S, settings)
    payload.update({
        "available": True,
        "engine_active": str(raw.get("slicer_provider") or "external") == ENGINE_ID,
        "can_slice": bool(machine.config.engine_gate),
        "gates": machine.gates(),
        "blocked_reason": machine.blocked_reason(),
    })
    return payload


@router.get("/api/slicer/settings", doc="Настройки нарезки и safety-gate слайсера")
def slicer_settings_route(api, ctx: Ctx):
    raw = _raw_settings(api)
    settings = settings_from(raw, P1S)
    machine = SliceMachine(config_from_settings(raw))
    warnings = validate_settings(settings, P1S)
    return {
        "profile": PROFILE_ID,
        "manual_only": machine.config.manual_only,
        "settings": settings_payload(settings, P1S),
        "warnings": warnings,
        "gates": machine.gates(),
        "stage": machine.stage.value,
        "blocked_reason": machine.blocked_reason(),
    }


@router.post("/api/slicer/plan", doc="План нарезки: аудит модели без записи файла")
def slicer_plan(api, ctx: Ctx):
    """Проверить модель и показать, что именно будет нарезано.

    Ничего не пишет на диск и не отправляет на принтер: только аудит,
    расчёт числа слоёв и предупреждения.
    """
    body = ctx.body or {}
    try:
        settings, warnings, machine = _prepare(api, body)
        source = _resolve_input(api, body)
        audit = audit_model(source, settings, P1S)
    except (SlicerError, OSError, KeyError, ValueError) as exc:
        return 400, {"error": str(exc)}
    machine.plan()
    return {
        "ok": True,
        "profile": PROFILE_ID,
        "engine": ENGINE_ID,
        "model": audit,
        "settings": settings_payload(settings, P1S),
        "warnings": warnings + list(audit.get("warnings") or []),
        "machine": machine.snapshot(),
    }


@router.post("/api/slicer/slice", audit="Слайсер: нарезка модели своим движком",
             doc="Нарезать STL своим движком и положить G-code в библиотеку")
def slicer_slice(api, ctx: Ctx):
    """Нарезать модель и пройти аудит результата.

    Файл попадает в библиотеку, но НЕ в очередь: в ручном режиме оператор
    сначала смотрит отчёт. Постановка в очередь возможна только явным
    `enqueue` вместе с `confirm`, когда гейты это разрешают.
    """
    body = ctx.body or {}
    try:
        settings, warnings, machine = _prepare(api, body)
        source = _resolve_input(api, body)
        audit = audit_model(source, settings, P1S)
        machine.audit_model(str(source.name), ok=True)
        machine.start()
        text, report = slice_model(source, settings, P1S)
    except (SlicerError, OSError, KeyError, ValueError) as exc:
        return 400, {"error": str(exc)}

    from .library import FileLibrary
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{Path(source).stem}.gcode"
    destination = UPLOAD_DIR / name
    if destination.exists():
        destination = UPLOAD_DIR / f"{Path(source).stem}-{abs(hash(text)) % 10000:04d}.gcode"
    destination.write_text(text, encoding="utf-8", newline="\n")
    machine.sliced(ok=True, output=destination.name)

    # FarmLoop поверх своей нарезки: тот же конвейер, что у внешнего CLI.
    # Исходный G-code остаётся рядом, в отдельный файл пишется копия с блоком.
    farmloop_report = None
    farm_profile = str(body.get("farmloop_profile") or "").strip()
    if not farm_profile and _raw_settings(api).get("slicer_auto_postprocess_farmloop"):
        farm_profile = str(body.get("farmloop_profile") or "").strip()
    if farm_profile:
        from .farmloop import prepare_file
        template_path = DATA_DIR / "farmloop-templates" / f"{farm_profile}.gcode"
        if not template_path.is_file():
            return 400, {
                "error": f"Для профиля {farm_profile} не установлен проверенный "
                         "FarmLoop-шаблон",
                "output": destination.name,
                "machine": machine.snapshot(),
            }
        farm_output = destination.with_name(destination.stem + ".farmloop.gcode")
        farmloop_report = prepare_file(
            destination, farm_output,
            template_path.read_text(encoding="utf-8", errors="replace"),
            metadata={
                "job_id": body.get("job_id", ""), "cycle": body.get("cycle", 1),
                "cycles": body.get("cycles", 1), "ams": settings.ams_slot,
                "material": settings.material, "color": body.get("color", ""),
            })
        destination = farm_output
        text = destination.read_text(encoding="utf-8", errors="replace")

    try:
        gcode_audit = audit_gcode(text, P1S)
        machine.audited(ok=True)
    except SlicerError as exc:
        machine.audited(ok=False, reason=str(exc))
        return 400, {"error": str(exc), "machine": machine.snapshot()}

    record = FileLibrary(api.db).put(destination.name, text.encode("utf-8"),
                                     source="printflow-slicer")
    if body.get("confirm"):
        machine.confirm(True)
    job = None
    if body.get("enqueue"):
        if not machine.enqueue_allowed():
            return 400, {
                "error": machine.blocked_reason() or
                         "Постановка в очередь запрещена гейтами слайсера",
                "machine": machine.snapshot(),
            }
        if api.manager:
            job = api.manager.enqueue({
                "file": destination.name,
                "name": Path(source).stem,
                "source": "printflow-slicer",
                "no_auto": 1,
                "allow_auto_start": False,
            })
    return {
        "ok": True,
        "output": destination.name,
        "path": str(destination),
        "size": destination.stat().st_size,
        "report": report,
        "audit": gcode_audit,
        "farmloop": farmloop_report,
        "model_audit": audit,
        "warnings": warnings + list(audit.get("warnings") or [])
                    + list(gcode_audit.get("warnings") or []),
        "library": record,
        "job": job,
        "machine": machine.snapshot(),
    }
