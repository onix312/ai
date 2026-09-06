#!/usr/bin/env python3
"""PrintFlow 17.0 — автоматический сборщик фронтенда.

Заказчик одобрил стек Lit + Vite со сборкой (трекер решений №11), но с
условием: запуск PrintFlow не должен требовать Node. Поэтому:

  * исходники компонентов живут в  frontend/src/  (Lit, ES-модули);
  * собираются Vite в  site/assets/dist/  (бандл коммитится в репозиторий);
  * панель и LAN-страницы подключают собранный бандл обычным <script> —
    без сборки и без Node на боевой машине.

Этот скрипт делает всё сам:
  1. проверяет наличие node/npm (подсказывает, как поставить);
  2. ставит зависимости (npm install) — только если их нет;
  3. запускает сборку (npm run build);
  4. печатает путь к бандлу.

Использование:
    python3 scripts/build_frontend.py          # поставить deps и собрать
    python3 scripts/build_frontend.py --force  # переустановить deps
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DIST = ROOT / "site" / "assets" / "dist"


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(cmd: list[str], cwd: Path) -> None:
    print(f"==> {' '.join(cmd)}  (в {cwd})")
    res = subprocess.run(cmd, cwd=str(cwd))
    if res.returncode != 0:
        sys.exit(f"Команда завершилась с кодом {res.returncode}: {' '.join(cmd)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Сборка фронтенда PrintFlow 17.0")
    ap.add_argument("--force", action="store_true", help="переустановить зависимости")
    args = ap.parse_args()

    if not have("node") or not have("npm"):
        print(
            "Node.js и npm не найдены.\n"
            "Сборка компонентов 17.0 требует Node 18+ (проверено на Node 22).\n"
            "  • Windows: winget install OpenJS.NodeJS.LTS\n"
            "  • macOS:   brew install node\n"
            "  • Linux:   sudo apt install nodejs npm\n"
            "Без Node можно просто запускать PrintFlow — собранный бандл уже\n"
            "лежит в site/assets/dist и коммитится в репозиторий.",
            file=sys.stderr,
        )
        return 2

    if args.force or not (FRONTEND / "node_modules").exists():
        run(["npm", "install", "--no-audit", "--no-fund"], FRONTEND)

    run(["npm", "run", "build"], FRONTEND)

    print("\nГотово. Бандл:")
    for f in sorted(DIST.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(ROOT)}  ({f.stat().st_size // 1024} КБ)")
    print("\nНе забудьте поднить ?v= у ассетов и версию кэша в sw.js.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
