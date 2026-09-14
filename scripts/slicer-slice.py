#!/usr/bin/env python3
"""Нарезать STL своим движком PrintFlow Stage 1 (локально, без принтера).

Примеры:

    python3 scripts/slicer-slice.py model.stl --output model.gcode
    python3 scripts/slicer-slice.py model.stl --plan --report plan.json
    python3 scripts/slicer-slice.py model.stl --output model.gcode \\
        --layer-height 0.16 --walls 3 --infill 25 --pattern grid \\
        --material PETG --supports --brim --brim-width 6

Команда только пишет отдельный G-code и отчёт. Она не отправляет файл на
принтер и не ставит его в очередь: это делает оператор в панели, посмотрев
отчёт. Исходная модель никогда не перезаписывается.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from connector.printflow.slicer import SlicerError  # noqa: E402
from connector.printflow.slicer_engine import (  # noqa: E402
    ENGINE_ID, ENGINE_VERSION, slice_model)
from connector.printflow.slicer_profile import (  # noqa: E402
    P1S, audit_gcode, audit_model, settings_from, settings_payload,
    validate_settings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="STL для нарезки")
    parser.add_argument("--output", type=Path, help="новый G-code, исходник не меняется")
    parser.add_argument("--report", type=Path, help="JSON-отчёт нарезки")
    parser.add_argument("--plan", action="store_true",
                        help="только аудит и план, без записи G-code")
    parser.add_argument("--layer-height", type=float, default=None)
    parser.add_argument("--first-layer-height", type=float, default=None)
    parser.add_argument("--walls", type=int, default=None)
    parser.add_argument("--infill", type=float, default=None, help="заполнение, %%")
    parser.add_argument("--pattern", default=None,
                        choices=("lines", "grid", "triangles", "concentric", "gyroid"))
    parser.add_argument("--supports", action="store_true", default=None)
    parser.add_argument("--brim", action="store_true", default=None)
    parser.add_argument("--brim-width", type=float, default=None)
    parser.add_argument("--material", default=None)
    parser.add_argument("--nozzle-temp", type=int, default=None)
    parser.add_argument("--bed-temp", type=int, default=None)
    parser.add_argument("--speed", type=float, default=None, help="скорость, мм/с")
    parser.add_argument("--nozzle", type=float, default=None, help="диаметр сопла, мм")
    parser.add_argument("--seam", default=None, choices=("nearest", "aligned"))
    parser.add_argument("--no-center", action="store_true",
                        help="не центрировать модель на столе")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides = {
        "layer_height": args.layer_height,
        "first_layer_height": args.first_layer_height,
        "walls": args.walls,
        "infill_percent": args.infill,
        "infill_pattern": args.pattern,
        "supports": args.supports,
        "brim": args.brim,
        "brim_width_mm": args.brim_width,
        "material": args.material,
        "nozzle_temp": args.nozzle_temp,
        "bed_temp": args.bed_temp,
        "speed_mm_s": args.speed,
        "nozzle_mm": args.nozzle,
        "seam": args.seam,
        "center_model": False if args.no_center else None,
    }
    try:
        settings = settings_from({}, P1S, overrides)
        warnings = validate_settings(settings, P1S)
        audit = audit_model(args.source, settings, P1S)
    except (SlicerError, OSError, ValueError) as exc:
        print(f"Ошибка слайсера: {exc}", file=sys.stderr)
        return 2

    report: dict = {
        "engine": ENGINE_ID,
        "engine_version": ENGINE_VERSION,
        "profile": P1S.id,
        "model_audit": audit,
        "settings": settings_payload(settings, P1S),
        "warnings": warnings + list(audit.get("warnings") or []),
    }
    if args.plan:
        report["planned"] = True
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            args.report.write_text(text + "\n", encoding="utf-8")
        print(text)
        print(f"План готов: {audit['layers']} слоёв, файл не записан.")
        return 0

    if not args.output:
        print("Укажите --output для записи G-code (или --plan для проверки).",
              file=sys.stderr)
        return 2
    try:
        gcode, slicing = slice_model(args.source, settings, P1S)
        gcode_audit = audit_gcode(gcode, P1S)
    except SlicerError as exc:
        print(f"Ошибка слайсера: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(gcode, encoding="utf-8", newline="\n")
    report.update({"slicing": slicing, "gcode_audit": gcode_audit,
                   "output": str(args.output),
                   "output_bytes": len(gcode.encode("utf-8"))})
    report["warnings"] = (report["warnings"]
                          + list(gcode_audit.get("warnings") or []))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"Создан отдельный файл: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
