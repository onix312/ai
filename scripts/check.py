#!/usr/bin/env python3
"""Единая локальная проверка PrintFlow, совпадающая с CI.

Запуск из корня репозитория::

    python scripts/check.py          # полный набор, включая unit-тесты
    python scripts/check.py --quick  # синтаксис, lint и целостность файлов
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(label: str, command: list[str]) -> bool:
    print(f"\n==> {label}\n    {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        print(f"FAIL: {label} (код {result.returncode})", file=sys.stderr)
        return False
    print(f"OK: {label}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверить репозиторий PrintFlow")
    parser.add_argument("--quick", action="store_true",
                        help="не запускать полный набор unit-тестов")
    parser.add_argument("--require-tools", action="store_true",
                        help="считать отсутствие Node.js или Ruff ошибкой")
    args = parser.parse_args(argv)
    checks: list[bool] = []

    ruff = shutil.which("ruff")
    if ruff:
        checks.append(run("Ruff (ошибки Python)", [ruff, "check", ".", "--select", "F,E9"]))
    elif args.require_tools:
        print("FAIL: ruff не найден", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: ruff не найден (установите: pip install ruff)")

    checks.append(run(
        "Компиляция Python",
        [sys.executable, "-m", "compileall", "-q", "connector", "pf.py",
         "launcher_window.py", "scripts"],
    ))

    node = shutil.which("node")
    js_files = sorted((ROOT / "site" / "assets").glob("*.js")) + [ROOT / "site" / "sw.js"]
    if node:
        for path in js_files:
            checks.append(run(f"JavaScript: {path.relative_to(ROOT)}",
                              [node, "--check", str(path)]))
    elif args.require_tools:
        print("FAIL: node не найден", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: node не найден; синтаксис JS также проверяется unit-тестом при наличии Node.js")

    if not args.quick:
        checks.append(run(
            "Unit-тесты",
            [sys.executable, "-m", "unittest", "discover", "-s", "connector/tests", "-v"],
        ))

    if shutil.which("git") and (ROOT / ".git").exists():
        checks.append(run("Пробелы и конфликт-маркеры", ["git", "diff", "--check"]))

    failed = checks.count(False)
    print(f"\n{'FAILED' if failed else 'PASSED'}: {len(checks) - failed}/{len(checks)} проверок")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
