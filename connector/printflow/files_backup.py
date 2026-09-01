"""Резервное копирование файлов цеха (идея 21).

Раньше бэкап был только у базы: `make_backup()` снимал копию
`printflow.sqlite3`, а загруженные модели, фотографии заказов, библиотека и
сертификат шлюза Studio оставались вне копии. То есть после потери диска
восстанавливалась учётная система, у которой нет ни одного файла: заказы
без фото, номенклатура без моделей, очередь без 3MF.

Здесь — архив файловых хранилищ рядом с копией базы:

  backups/
    printflow-manual-20260831-120000.sqlite3      ← база (как раньше)
    printflow-manual-20260831-120000.files.tar.gz ← uploads/photos/library/…

Что кладём и чего не кладём:
  * `uploads`, `photos`, `library`, `exports`, `studio-gateway` — да;
  * ключ шифрования `.printflow.key` — НЕТ: вместе с архивом он обесценивает
    шифрование секретов (а бэкапы как раз и уезжают на чужие диски);
  * `backups` — нет, иначе копия содержит саму себя;
  * лог — нет, это диагностика, а не данные.

Архив проверяется после записи (чтение оглавления), а его состав и размеры
возвращаются в отчёте — «что именно я сохранил» должно быть видно.
"""
from __future__ import annotations

import json
import tarfile
import time
from pathlib import Path

from .config import BACKUP_DIR, DATA_DIR, PHOTO_DIR, UPLOAD_DIR, ensure_dirs

# Каталоги, которые входят в файловую копию.
INCLUDED = ("uploads", "photos", "library", "exports", "studio-gateway", "slicer-out")
# Что никогда не попадает в архив.
EXCLUDED_NAMES = {".printflow.key", "backups", "restore.request", "connector.log"}
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024   # 2 ГБ: защита от «архив на весь диск»


def included_dirs() -> list[Path]:
    """Существующие каталоги данных, которые стоит копировать."""
    found = []
    for name in INCLUDED:
        path = DATA_DIR / name
        if path.is_dir():
            found.append(path)
    # Явные константы могли быть переопределены (тесты, перенос данных).
    for path in (UPLOAD_DIR, PHOTO_DIR):
        if path.is_dir() and path not in found:
            found.append(path)
    return found


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if set(path.relative_to(DATA_DIR).parts) & EXCLUDED_NAMES:
            continue
        yield path


def collect_manifest() -> dict:
    """Состав файловых хранилищ: сколько файлов и байт по каталогам."""
    manifest = {"dirs": [], "files": 0, "bytes": 0, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    for directory in included_dirs():
        count = 0
        size = 0
        for path in _iter_files(directory):
            try:
                size += path.stat().st_size
                count += 1
            except OSError:
                continue
        manifest["dirs"].append({"name": directory.name, "files": count, "bytes": size})
        manifest["files"] += count
        manifest["bytes"] += size
    return manifest


def make_files_backup(prefix: str = "printflow-manual", keep: object | None = None) -> dict:
    """Сжать файловые хранилища в tar.gz рядом с копией базы."""
    ensure_dirs()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    manifest = collect_manifest()
    if not manifest["files"]:
        return {"ok": True, "skipped": True, "reason": "файлов данных нет",
                "manifest": manifest}
    if manifest["bytes"] > MAX_TOTAL_BYTES:
        return {"ok": False, "manifest": manifest,
                "error": f"Файлов больше {MAX_TOTAL_BYTES // (1024 * 1024)} МБ — "
                         f"сначала настройте политику хранения"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{prefix}-{stamp}.files.tar.gz"
    try:
        with tarfile.open(target, "w:gz") as tar:
            for directory in included_dirs():
                for path in _iter_files(directory):
                    try:
                        tar.add(path, arcname=path.relative_to(DATA_DIR).as_posix())
                    except OSError:
                        continue
            info = tarfile.TarInfo("backup-manifest.json")
            payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            info.size = len(payload)
            tar.addfile(info, __import__("io").BytesIO(payload))
    except (OSError, tarfile.TarError) as exc:
        target.unlink(missing_ok=True)
        return {"ok": False, "error": str(exc), "manifest": manifest}

    # Проверка: архив должен читаться, иначе это не бэкап, а мусор на диске.
    try:
        with tarfile.open(target, "r:gz") as tar:
            names = tar.getnames()
    except (OSError, tarfile.TarError) as exc:
        target.unlink(missing_ok=True)
        return {"ok": False, "error": f"архив не читается: {exc}", "manifest": manifest}

    from .config import rotate_backups
    rotate_backups(BACKUP_DIR, keep)
    return {"ok": True, "file": target.name, "entries": len(names),
            "size": target.stat().st_size, "manifest": manifest}


def restore_files_backup(filename: str, target_dir: Path | None = None) -> dict:
    """Разложить файловую копию обратно. Только пути внутри каталога данных."""
    source = Path(filename)
    if not source.is_absolute():
        source = BACKUP_DIR / filename
    if not source.is_file():
        return {"ok": False, "error": f"Архив не найден: {source.name}"}
    destination = Path(target_dir or DATA_DIR)
    destination.mkdir(parents=True, exist_ok=True)
    restored = 0
    try:
        with tarfile.open(source, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    continue
                if member.name == "backup-manifest.json":
                    continue
                tar.extract(member, destination)
                restored += 1
    except (OSError, tarfile.TarError) as exc:
        return {"ok": False, "error": str(exc), "restored": restored}
    return {"ok": True, "restored": restored, "into": str(destination)}


def list_files_backups() -> list[dict]:
    """Файловые копии в каталоге бэкапов (новейшие первыми)."""
    if not BACKUP_DIR.is_dir():
        return []
    rows = []
    for path in sorted(BACKUP_DIR.glob("*.files.tar.gz"), reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append({"name": path.name, "size": stat.st_size,
                     "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))})
    return rows
