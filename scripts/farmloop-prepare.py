#!/usr/bin/env python3
"""Подготовить G-code Bambu Studio для FarmLoop Stage 1 P1S.

Пример:
  python3 scripts/farmloop-prepare.py input.gcode \
      --template farmloop-p1s-stage1.gcode \
      --output input.farmloop.gcode --job-id demo --cycle 1 --cycles 10 \
      --ams AMS1/3 --material PETG --color black

Команда только пишет отдельный файл. Она не загружает и не запускает принтер.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from connector.printflow.farmloop import FarmLoopError, prepare_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="G-code из Bambu Studio")
    parser.add_argument("--template", type=Path, required=True,
                        help="проверенный FarmLoop Stage 1 G-code блок")
    parser.add_argument("--output", type=Path, required=True,
                        help="новый G-code, исходник не меняется")
    parser.add_argument("--report", type=Path, help="JSON-отчёт проверки")
    for name in ("job-id", "cycle", "cycles", "ams", "material", "color"):
        parser.add_argument("--" + name, default="")
    args = parser.parse_args()
    try:
        template = args.template.read_text(encoding="utf-8", errors="replace")
        metadata = {key.replace("-", "_"): value for key, value in vars(args).items()
                    if key in {"job_id", "cycle", "cycles", "ams", "material", "color"}
                    and value}
        report = prepare_file(args.source, args.output, template, metadata=metadata)
        report_text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            args.report.write_text(report_text + "\n", encoding="utf-8")
        print(report_text)
        print(f"Создан отдельный файл: {args.output}")
        return 0
    except (FarmLoopError, OSError) as exc:
        print(f"Ошибка FarmLoop: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
