"""Запуск: python -m agent.native_ui"""
from __future__ import annotations

import sys


def main() -> int:
    try:
        from .app import run
    except ImportError as exc:
        if exc.name == "PySide6" or str(exc).startswith("No module named 'PySide6"):
            print("Нативный интерфейс Люмы требует PySide6.")
            print("Установите зависимости агента: pip install -r agent/requirements.txt")
            return 2
        raise
    return run()


if __name__ == "__main__":
    sys.exit(main())
