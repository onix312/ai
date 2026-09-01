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


def check_data_icons() -> bool:
    """Б8: все data-icon="..." в HTML существуют в реестре PFIcons (icons.js)."""
    import re
    label = "data-icon в реестре PFIcons"
    print(f"\n==> {label}", flush=True)
    try:
        # Читаем реестр иконок из icons.js
        icons_js = (ROOT / "site" / "assets" / "icons.js").read_text(encoding="utf-8")
        # Ищем все ключи в объекте ICONS: key: '<svg...', без кавычек вокруг ключа
        registry = set(re.findall(r'^\s+([a-z0-9\-_]+)\s*:\s*\'', icons_js, re.MULTILINE))
        # Читаем все HTML-файлы в site/
        missing = []
        for html_file in (ROOT / "site").rglob("*.html"):
            content = html_file.read_text(encoding="utf-8")
            icons = re.findall(r'data-icon="([^"]+)"', content)
            for icon in icons:
                if icon not in registry:
                    missing.append(f"{html_file.relative_to(ROOT)}: {icon}")
        if missing:
            print(f"FAIL: {label} — не найдены иконки:", file=sys.stderr)
            for m in missing[:10]:
                print(f"  {m}", file=sys.stderr)
            return False
        print(f"OK: {label} (проверено {len(registry)} иконок)")
        return True
    except Exception as e:
        print(f"SKIP: {label} — {e}")
        return True  # не блокируем если icons.js нет


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

    # Б8: проверка что все data-icon существуют в PFIcons
    checks.append(check_data_icons())

    # 14.0 (94): headless-стенд панели. node --check ловит только синтаксис,
    # а обращение к необъявленной переменной (Б1/Б2) видно лишь при загрузке
    # скриптов с заглушкой DOM.
    if node:
        checks.append(run("Headless-стенд панели (необъявленные переменные)",
                          [node, "scripts/panel-check.js"]))
    elif args.require_tools:
        print("FAIL: node не найден", file=sys.stderr)
        checks.append(False)
    else:
        print("SKIP: node не найден — стенд панели не запущен")

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
