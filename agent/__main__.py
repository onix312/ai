"""Запуск агента: `python -m agent` из корня репозитория.

Отдельная точка входа нужна, чтобы агент можно было поставить в автозапуск
Windows как свою программу, не трогая `pf.py` и окружение коннектора.
"""
from __future__ import annotations

import sys

from .server import run

if __name__ == "__main__":
    sys.exit(run())
