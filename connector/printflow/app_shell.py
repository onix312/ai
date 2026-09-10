"""Оболочка-приложение для Android: чем телефон кассира отличается от вкладки.

Сам APK не «довинчивает» кассу — он решает три бытовые вещи, из-за которых
касса в браузере проигрывает:

  * вкладку выгружает Android, когда не хватает памяти (утром кассир снова
    вводит код, и «невидимая смена» прерывается на полчаса);
  * у приложения есть иконка и свой процесс, экран не гаснет посреди очереди;
  * приложение может позвонить, даже когда экран погашен, — браузеру это
    почти всегда недоступно.

Здесь только серверная половина: чем раздать APK и как оболочка узнаёт, что
вышла новая версия. Сборка — `scripts/android-build.sh`, инструкция —
`docs/ANDROID.md`. APK кладётся в `site/app/` (вне Git: файл весит мегабайты,
а источник правды — код в `android/`).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import SITE

# Каталог сборки внутри статики: оттуда файл отдаётся тем же файловым слоем,
# что и остальная панель, — отдельного «скачивательного» сервера не нужно, и
# имя файла в URL остаётся человекочитаемым («NOZZA-kassa-17.0.7.apk»).
APP_DIR_NAME = "app"
APK_SUFFIX = ".apk"
VERSION_FILE = "version.json"


def app_dir() -> Path:
    return Path(SITE) / APP_DIR_NAME


def _version_info(root: Path) -> dict[str, Any]:
    """Манифест сборки, который пишет `android-build.sh` рядом с APK."""
    raw = root / VERSION_FILE
    if not raw.is_file():
        return {}
    try:
        data = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def latest_apk(root: Path | None = None) -> tuple[Path | None, dict[str, Any]]:
    """Свежий APK в каталоге сборки (если их несколько — берём новый по mtime)."""
    base = root or app_dir()
    if not base.is_dir():
        return None, {}
    files = sorted((p for p in base.iterdir()
                    if p.is_file() and p.suffix.lower() == APK_SUFFIX),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None, {}
    return files[0], _version_info(base)


def status(root: Path | None = None) -> dict[str, Any]:
    """Что отдать оболочке и панели: есть ли сборка, какая версия, где скачать.

    `root` — каталог сборки; по умолчанию `site/app`. Параметр нужен тестам и
    «пересборке в обход репозитория», а не для того, чтобы уводить раздачу.
    """
    apk, meta = latest_apk(root)
    if apk is None:
        return {"available": False, "url": "", "file": "", "size_mb": 0.0,
                "version": "", "version_code": 0, "built_at": "",
                "package": str(meta.get("package") or "ai.printflow.kassa"),
                "hint": ("Сборки нет. На ПК владельца: ./scripts/android-build.sh — "
                         "APK появится здесь и его можно будет скачать телефоном "
                         "через локальную сеть.")}
    stat = apk.stat()
    return {
        "available": True,
        "url": f"/{APP_DIR_NAME}/{apk.name}",
        "file": apk.name,
        "size_mb": round(stat.st_size / 1024 / 1024, 2),
        "version": str(meta.get("version") or ""),
        "version_code": int(meta.get("version_code") or 0),
        "built_at": str(meta.get("built_at") or ""),
        "package": str(meta.get("package") or "ai.printflow.kassa"),
        "hint": "Ставится поверх себя: данные кассы в WebView, не в приложении.",
    }


def check_version(seen: Any, root: Path | None = None) -> dict[str, Any]:
    """Ответ оболочке «нужно ли обновиться».

    `seen` — versionCode установленного APK (целое или строка). Сравниваем
    только вверх: понижение версии — всегда отказ, иначе авто-обновление
    устроило бы маятник между двумя сборками.
    """
    out = status(root)
    try:
        installed = int(seen or 0)
    except (TypeError, ValueError):
        installed = 0
    update = bool(out["available"]) and out["version_code"] > installed
    return {"available": out["available"], "update_available": update,
            "installed": installed, "url": out["url"] if update else "",
            "version": out["version"], "version_code": out["version_code"],
            "file": out["file"]}
