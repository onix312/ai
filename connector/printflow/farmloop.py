"""FarmLoop Stage 1: безопасная подготовка G-code для Bambu P1S.

PrintFlow не выдумывает движения механики. Слайсер остаётся Bambu Studio или
OrcaSlicer, а сюда передаётся проверенный оператором блок G-code конкретного
FarmLoop-комплекта. Модуль проверяет совместимость, маркеры, повторную обработку
и выпускает отдельный файл. Это намеренно не команда к принтеру: сначала нужен
реальный P1S Stage 1, его точная механика и тестовый G-code.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR

BEGIN = "; PRINTFLOW FARMLOOP BEGIN"
END = "; PRINTFLOW FARMLOOP END"
PROFILE_ID = "bambu-p1s-farmloop-stage1"


class FarmLoopError(ValueError):
    """Файл нельзя безопасно подготовить под выбранный FarmLoop-профиль."""


@dataclass(frozen=True)
class FarmLoopProfile:
    id: str = PROFILE_ID
    printer: str = "Bambu Lab P1S"
    stage: str = "Stage 1"
    plate_width_mm: float = 256.0
    plate_height_mm: float = 256.0
    max_print_height_mm: float = 200.0
    requires_verified_template: bool = True


P1S_STAGE1 = FarmLoopProfile()


def farmloop_profile_id(raw: dict | None = None) -> str:
    """Активный FarmLoop-профиль: из настроек или дефолт Stage 1."""
    return str((raw or {}).get("farmloop_profile") or "").strip() or P1S_STAGE1.id


def farmloop_gate(raw: dict | None = None) -> dict:
    """18.8: гейты конвейера из словаря настроек (без доступа к базе).

    Зовут карточка слайсера (раздел «Печать») и вкладка «Конвейер»:
      * can_prepare — файл серии можно собрать (шаблон на месте);
      * can_series  — серия продолжит сама (механика + датчик + шаблон);
      * max_cycles  — предел циклов из настроек, 1..1000.
    Логика та же, что в routes_farmloop: не вычисляем гейт дважды.
    """
    raw = raw or {}
    profile = farmloop_profile_id(raw)
    template = DATA_DIR / "farmloop-templates" / f"{profile}.gcode"
    template_ok = bool(template.is_file())
    physical = all(bool(raw.get(key)) for key in (
        "farmloop_mechanics_verified", "farmloop_template_verified",
        "farmloop_pusher_enabled", "farmloop_bender_enabled"))
    sensing = str(raw.get("farmloop_sensor_mode") or "") in {"sensor", "camera", "both"}
    can_series = bool(physical and sensing and template_ok)
    if not template_ok:
        reason = "Нет установленного шаблона FarmLoop"
    elif not physical:
        reason = "Не подтверждена механика конвейера (допуски)"
    elif not sensing:
        reason = "Не выбран датчик подтверждения пустой платформы"
    else:
        reason = ""
    try:
        max_cycles = max(1, int(raw.get("farmloop_max_cycles") or 1))
    except (TypeError, ValueError):
        max_cycles = 1
    return {
        "profile_id": profile,
        "template_installed": template_ok,
        "auto_postprocess": bool(raw.get("slicer_auto_postprocess_farmloop")),
        "can_prepare": template_ok,
        "can_series": can_series,
        "max_cycles": max_cycles,
        "blocked_reason": reason,
    }


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").splitlines()


def has_farmloop_block(text: str) -> bool:
    """Есть ли уже полный PrintFlow-блок, а не случайное упоминание."""
    return BEGIN in text and END in text


def validate_template(template: str, profile: FarmLoopProfile = P1S_STAGE1) -> dict:
    """Проверить переданный оператором блок FarmLoop.

    В шаблоне обязательны маркеры начала/конца и хотя бы одна команда движения.
    Пустой или только комментарийный блок запрещён: он создаёт ложное ощущение
    автоматизации. Опасные команды не запрещаем строковым списком вслепую — их
    проверяет отдельный профиль принтера, а неизвестный шаблон не активируем.
    """
    if not profile.requires_verified_template:
        raise FarmLoopError("Профиль без требования проверки шаблона запрещён")
    lines = _lines(template)
    if not any(line.strip() == BEGIN for line in lines):
        raise FarmLoopError(f"В шаблоне нет точного маркера: {BEGIN}")
    if not any(line.strip() == END for line in lines):
        raise FarmLoopError(f"В шаблоне нет точного маркера: {END}")
    if sum(line.strip() == BEGIN for line in lines) != 1 or sum(line.strip() == END for line in lines) != 1:
        raise FarmLoopError("В шаблоне должен быть ровно один блок FarmLoop")
    start = next(i for i, line in enumerate(lines) if line.strip() == BEGIN)
    finish = next(i for i, line in enumerate(lines) if line.strip() == END)
    if finish <= start:
        raise FarmLoopError("Конец FarmLoop-блока находится раньше начала")
    body = [line.strip() for line in lines[start + 1:finish]
            if line.strip() and not line.strip().startswith(";")]
    if not body:
        raise FarmLoopError("FarmLoop-шаблон пустой: нужны реальные G-code-команды")
    commands = [line.split(None, 1)[0].upper() for line in body]
    if not any(command in {"G0", "G1", "G2", "G3", "G28", "G29"} for command in commands):
        raise FarmLoopError("FarmLoop-шаблон не содержит движения принтера")
    return {
        "profile": profile.id,
        "commands": len(body),
        "start_line": start + 1,
        "end_line": finish + 1,
    }


def build_template_from_blocks(
    cooldown_temp: int = 35,
    fan_assist: bool = True,
    z_lift: float = 15.0,
    pusher_y: float = 245.0,
    pusher_speed: int = 2400,
    custom_gcode: str = "",
) -> str:
    """Собрать проверенный G-code шаблон FarmLoop из параметров конструктора."""
    # legacy wrapper -> timeline builder
    blocks = [
        {"type": "cool", "bed": 0, "nozzle": 0},
        {"type": "fan", "on": bool(fan_assist), "p": 2, "speed": 255},
        {"type": "cool", "bed_wait": int(cooldown_temp or 35)},
        {"type": "fan", "on": False, "p": 2},
        {"type": "park", "z_lift": float(z_lift)},
        {"type": "push", "x": 128, "y_start": 10, "y_end": float(pusher_y), "speed": int(pusher_speed)},
        {"type": "park", "home_xy": True},
    ]
    if custom_gcode and custom_gcode.strip():
        blocks.append({"type": "custom", "gcode": custom_gcode})
    return build_from_timeline(blocks)["gcode"]


# 18.12.1+: full timeline constructor
TIMELINE_TYPES = {"cool", "fan", "park", "push", "dwell", "custom"}

def build_from_timeline(blocks: list[dict]) -> dict:
    """Собрать G-code из timeline блоков: Cool/Fan/Park/Push/Dwell/Custom.

    Возвращает {gcode, preview_path, warnings, blocks_count}.
    """
    if not isinstance(blocks, list) or not blocks:
        raise FarmLoopError("Timeline пуст")
    if len(blocks) > 50:
        raise FarmLoopError("Слишком много блоков (max 50)")
    lines = [BEGIN, "; FarmLoop timeline constructor"]
    preview_path: list[dict] = []
    cur_x, cur_y, cur_z = 128.0, 10.0, 15.0
    warnings: list[str] = []
    custom_started = False

    def clamp(v, lo, hi):
        try:
            return max(lo, min(hi, float(v)))
        except Exception:
            return lo

    for idx, b in enumerate(blocks):
        if not isinstance(b, dict):
            raise FarmLoopError(f"Блок {idx} не объект")
        t = str(b.get("type") or "").lower()
        if t not in TIMELINE_TYPES:
            raise FarmLoopError(f"Блок {idx}: неизвестный тип {t}")
        if t == "cool":
            bed = b.get("bed")
            nozzle = b.get("nozzle")
            bed_wait = b.get("bed_wait")
            if bed is not None:
                lines.append(f"M140 S{int(clamp(bed,0,120))}")
            if nozzle is not None:
                lines.append(f"M104 S{int(clamp(nozzle,0,300))}")
            if bed_wait is not None:
                bw = int(clamp(bed_wait,20,100))
                lines.append(f"M190 R{bw} ; wait bed {bw}C")
        elif t == "fan":
            p = int(b.get("p", 2))
            on = bool(b.get("on", True))
            speed = int(clamp(b.get("speed", 255), 0, 255))
            if on:
                lines.append(f"M106 P{p} S{speed} ; fan on")
            else:
                lines.append(f"M106 P{p} S0 ; fan off")
        elif t == "park":
            x = b.get("x")
            y = b.get("y")
            z_lift = b.get("z_lift")
            home_xy = bool(b.get("home_xy"))
            if z_lift is not None:
                zl = clamp(z_lift, 0, 50)
                lines.append("G91")
                lines.append(f"G1 Z{zl:.1f} F1200 ; lift")
                lines.append("G90")
                cur_z += zl
            if home_xy:
                lines.append("G28 X Y ; home xy")
                cur_x, cur_y = 128.0, 10.0
            else:
                if x is not None or y is not None:
                    nx = clamp(x, 0, 256) if x is not None else cur_x
                    ny = clamp(y, 0, 256) if y is not None else cur_y
                    lines.append(f"G1 X{nx:.1f} Y{ny:.1f} F3000 ; park")
                    preview_path.append({"x": cur_x, "y": cur_y, "x2": nx, "y2": ny, "type": "move"})
                    cur_x, cur_y = nx, ny
        elif t == "push":
            x = clamp(b.get("x", cur_x), 0, 256)
            y_start = clamp(b.get("y_start", 10), 0, 256)
            y_end = clamp(b.get("y_end", 245), 0, 256)
            speed = int(clamp(b.get("speed", 2400), 300, 10000))
            repeat = max(1, min(5, int(b.get("repeat", 1) or 1)))
            # move to start
            lines.append(f"G1 X{x:.1f} Y{y_start:.1f} F3000 ; push start")
            preview_path.append({"x": cur_x, "y": cur_y, "x2": x, "y2": y_start, "type": "move"})
            cur_x, cur_y = x, y_start
            for _ in range(repeat):
                lines.append(f"G1 Y{y_end:.1f} F{speed} ; push forward")
                preview_path.append({"x": cur_x, "y": cur_y, "x2": x, "y2": y_end, "type": "push"})
                cur_x, cur_y = x, y_end
                lines.append(f"G1 Y{y_start:.1f} F3000 ; push back")
                preview_path.append({"x": cur_x, "y": cur_y, "x2": x, "y2": y_start, "type": "return"})
                cur_x, cur_y = x, y_start
            # force feedback hint
            if repeat > 1:
                warnings.append(f"push repeat {repeat} — проверь нагрузку на толкатель")
        elif t == "dwell":
            ms = int(clamp(b.get("ms", b.get("seconds", 2) * 1000 if b.get("seconds") else 1000), 100, 30000))
            lines.append(f"G4 P{ms} ; dwell {ms}ms")
        elif t == "custom":
            g = str(b.get("gcode") or "").strip()
            if not g:
                continue
            if not custom_started:
                # Раздел дополнений нужен и человеку, и разбору шаблона:
                # `parse_template_blocks` ищет именно этот маркер, и без него
                # строки оператора терялись при повторном открытии конструктора.
                lines.append("; 5. Дополнения")
                custom_started = True
            # safety: forbid dangerous
            upper = g.upper()
            if "M112" in upper or "M999" in upper:
                raise FarmLoopError(f"Блок {idx}: запрещённая команда M112/M999")
            # check each line
            for line in g.splitlines():
                ls = line.strip()
                if not ls:
                    continue
                if ls.startswith("; PRINTFLOW FARMLOOP"):
                    continue
                # validate no out-of-bounds moves
                import re
                mx = re.search(r"X(-?\d+(?:\.\d+)?)", ls.upper())
                my = re.search(r"Y(-?\d+(?:\.\d+)?)", ls.upper())
                if mx:
                    try:
                        xv = float(mx.group(1))
                        if xv < -10 or xv > 266:
                            warnings.append(f"custom X {xv} вне стола")
                    except Exception:
                        pass
                if my:
                    try:
                        yv = float(my.group(1))
                        if yv < -10 or yv > 266:
                            warnings.append(f"custom Y {yv} вне стола")
                    except Exception:
                        pass
                lines.append(ls)
    lines.append(END)
    gcode = "\n".join(lines) + "\n"
    # dry-run validation
    val = dry_run_validate(gcode)
    warnings.extend(val.get("warnings", []))
    if not val.get("ok"):
        raise FarmLoopError("; ".join(val.get("errors", ["validation failed"])))
    return {"gcode": gcode, "preview_path": preview_path, "warnings": warnings,
            "blocks_count": len(blocks), "ok": True}


def dry_run_validate(gcode: str) -> dict:
    """Dry-run валидация G-code: границы, опасные команды."""
    errors = []
    warnings = []
    try:
        lines = gcode.splitlines()
        for i, line in enumerate(lines, 1):
            up = line.strip().upper()
            if not up or up.startswith(";"):
                continue
            # опасные
            if up.startswith("M112") or "M997" in up or "M999" in up:
                errors.append(f"строка {i}: запрещённая команда {up[:10]}")
            # температура
            import re
            m = re.search(r"M10[49]\s+S(\d+)", up)
            if m:
                try:
                    t = int(m.group(1))
                    if t > 300:
                        errors.append(f"строка {i}: температура сопла {t} >300")
                except Exception:
                    pass
            m = re.search(r"M14[09]\s+S(\d+)", up)
            if m:
                try:
                    t = int(m.group(1))
                    if t > 120:
                        warnings.append(f"строка {i}: температура стола {t} высокая")
                except Exception:
                    pass
        return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings}
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": warnings}


DEFAULT_STAGE1_TEMPLATE = build_template_from_blocks()


def parse_template_blocks(template: str) -> dict:
    """Разобрать G-code шаблона на параметры для блочного конструктора."""
    import re
    blocks = {
        "cooldown_temp": 35,
        "fan_assist": False,
        "z_lift": 15.0,
        "pusher_y": 245.0,
        "pusher_speed": 2400,
        "custom_gcode": "",
    }
    lines = _lines(template)
    custom_lines = []
    in_custom = False
    for line in lines:
        raw = line.strip()
        if not raw or raw in (BEGIN, END):
            continue
        if "; 5. Дополнения" in raw or "; Дополнения" in raw:
            in_custom = True
            continue
        if in_custom:
            if not raw.startswith("; PRINTFLOW"):
                custom_lines.append(raw)
            continue
        m_cool = re.search(r"M190\s+R(\d+)", raw, re.IGNORECASE)
        if m_cool:
            blocks["cooldown_temp"] = int(m_cool.group(1))
        if re.search(r"M106\s+P2\s+S255", raw, re.IGNORECASE):
            blocks["fan_assist"] = True
        m_z = re.search(r"G1\s+Z(\d+(?:\.\d+)?)", raw, re.IGNORECASE)
        if m_z:
            blocks["z_lift"] = float(m_z.group(1))
        m_y = re.search(r"G1\s+Y(\d+(?:\.\d+)?)\s+F(\d+)", raw, re.IGNORECASE)
        if m_y:
            y_val = float(m_y.group(1))
            speed_val = int(m_y.group(2))
            if y_val > 50:
                blocks["pusher_y"] = y_val
                blocks["pusher_speed"] = speed_val
    blocks["custom_gcode"] = "\n".join(custom_lines)
    return blocks


def audit_source(gcode: str, profile: FarmLoopProfile = P1S_STAGE1) -> dict:
    """Минимальный аудит исходника до внедрения FarmLoop."""
    lines = _lines(gcode)
    if not lines:
        raise FarmLoopError("G-code пустой")
    if has_farmloop_block(gcode):
        raise FarmLoopError("Файл уже содержит FarmLoop-блок; повторная обработка запрещена")
    upper = gcode.upper()
    if "M104" not in upper and "M109" not in upper:
        raise FarmLoopError("Не найдены команды температуры сопла: это не похожий FDM G-code")
    if "M140" not in upper and "M190" not in upper:
        raise FarmLoopError("Не найдены команды температуры стола: проверьте файл Bambu Studio")
    z_values: list[float] = []
    import re
    for line in lines:
        match = re.search(r"\bZ(-?\d+(?:\.\d+)?)", line.upper())
        if match and line.lstrip().upper().startswith(("G0", "G1")):
            try:
                z_values.append(float(match.group(1)))
            except ValueError:
                pass
    height = max(z_values, default=0.0)
    warnings: list[str] = []
    if height > profile.max_print_height_mm:
        warnings.append(
            f"Высота {height:.1f} мм выше ограничения Stage 1 профиля "
            f"{profile.max_print_height_mm:.0f} мм")
    return {
        "lines": len(lines),
        "height_mm": round(height, 1),
        "warnings": warnings,
        "profile": profile.id,
    }


def prepare_gcode(gcode: str, template: str,
                  profile: FarmLoopProfile = P1S_STAGE1,
                  metadata: dict | None = None) -> tuple[str, dict]:
    """Добавить проверенный FarmLoop-блок перед концом исходного G-code.

    Исходный текст сохраняется неизменённым до первой строки блока. В metadata
    попадают профиль и цикл; значения превращаются только в комментарии и не
    могут подменить G-code-команды шаблона.
    """
    source = audit_source(gcode, profile)
    template_info = validate_template(template, profile)
    meta = metadata or {}
    header = [
        BEGIN,
        f"; profile: {profile.id}",
        f"; printer: {profile.printer}",
        f"; stage: {profile.stage}",
    ]
    # 18.8: spool — id катушки со склада, с которой серия должна печататься.
    for key in ("job_id", "cycle", "cycles", "ams", "material", "color", "spool"):
        if key in meta and str(meta[key]).strip():
            value = str(meta[key]).replace("\n", " ").replace("\r", " ")[:160]
            header.append(f"; {key}: {value}")
    block = "\n".join(header + _lines(template)[1:-1] + [END])
    output = gcode.rstrip() + "\n\n" + block + "\n"
    return output, {
        "profile": profile.id,
        "source_lines": source["lines"],
        "source_height_mm": source["height_mm"],
        "warnings": source["warnings"],
        "template_commands": template_info["commands"],
        "injected": True,
    }


def prepare_file(source: str | Path, destination: str | Path, template: str,
                 profile: FarmLoopProfile = P1S_STAGE1,
                 metadata: dict | None = None) -> dict:
    """Файловая обёртка: никогда не затирает source."""
    src = Path(source)
    dst = Path(destination)
    if src.resolve() == dst.resolve():
        raise FarmLoopError("Исходный G-code и результат должны быть разными файлами")
    text = src.read_text(encoding="utf-8", errors="replace")
    output, report = prepare_gcode(text, template, profile, metadata)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(output, encoding="utf-8", newline="\n")
    report.update({"source": str(src), "output": str(dst), "output_bytes": len(output.encode("utf-8"))})
    return report


def profile_payload(profile: FarmLoopProfile = P1S_STAGE1) -> dict:
    return {
        "id": profile.id,
        "printer": profile.printer,
        "stage": profile.stage,
        "plate_mm": [profile.plate_width_mm, profile.plate_height_mm],
        "max_print_height_mm": profile.max_print_height_mm,
        "requires_verified_template": profile.requires_verified_template,
    }
