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
    lines = [
        BEGIN,
        "; FarmLoop Stage 1: автоматическое охлаждение и безопасный сброс детали P1S",
        "; 1. Охлаждение стола",
        "M140 S0",
        "M104 S0",
    ]
    if fan_assist:
        lines.append("M106 P2 S255")
    temp = max(20, min(100, int(cooldown_temp or 35)))
    lines.append(f"M190 R{temp}")
    if fan_assist:
        lines.append("M106 P2 S0")
    lines.extend([
        "; 2. Отвод сопла",
        "G91",
        f"G1 Z{float(z_lift):.1f} F1200",
        "G90",
        "; 3. Проход толкателя",
        "G1 X128 Y10 F3000",
        f"G1 Y{float(pusher_y):.1f} F{int(pusher_speed)}",
        "G1 Y10 F3000",
        "; 4. Парковка",
        "G28 X Y",
    ])
    if custom_gcode and custom_gcode.strip():
        lines.append("; 5. Дополнения")
        for line in custom_gcode.strip().splitlines():
            line_str = line.strip()
            if line_str and not line_str.startswith("; PRINTFLOW FARMLOOP"):
                lines.append(line_str)
    lines.append(END)
    return "\n".join(lines) + "\n"


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
