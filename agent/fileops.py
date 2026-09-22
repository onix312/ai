"""Раскладка файлов по делам (18.14, идея И144).

Навыки `files.tidy_plan` и `files.tidy_apply` двигают файлы на диске, поэтому
устроены в два шага и с двумя предохранителями:

  1. **сначала план.** `files.tidy_plan` — риск `read`: он возвращает список
     перемещений с причиной каждого, и человек читает план, а не догадывается,
     что случилось;
  2. **потом подтверждение.** `files.tidy_apply` — риск `write`, поэтому агент
     спрашивает человека на этом же компьютере (тот же механизм, что для клика в
     чужом окне, `server.Agent.queue_action`);
  3. **только разрешённые папки.** Путь обязан лежать внутри папок, которые
     владелец указал в настройках агента: ассистент не ходит по всему диску и не
     трогает системное;
  4. **никакого удаления.** Файл переезжает, а не исчезает; при совпадении имён
     предлагается новое имя, а не перезапись.

Папки назначения — подпапки той же папки («Модели», «Счета», «Фото», …), поэтому
навык работает без знания устройства панели: ему не нужно спрашивать, где у
PrintFlow Watch Folder, и он не может положить файл не туда, где владелец ищет.
"""
from __future__ import annotations

import pathlib
import re
import shutil
from typing import Any

# Куда что кладём. Порядок важен: сначала совпадение в имени (счёт, договор,
# акт — их расширение может быть любым), потом расширение.
NAME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Счета", ("счёт", "счет", "invoice", "акт", "упд", "счет-фактура")),
    ("Договоры", ("договор", "contract", "соглашение", "ндс")),
    ("Заказы", ("заказ", "order", "заявка", "бриф", "тз")),
    ("Модели", ("модель", "model", "деталь", "корпус", "брелок", "адресник")),
    ("Чертежи", ("чертёж", "чертеж", "чертежи", "схема", "drawing")),
)
SUFFIX_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Модели", (".stl", ".3mf", ".obj", ".step", ".stp", ".iges", ".igs", ".f3d")),
    ("Нарезка", (".gcode", ".g", ".nc")),
    ("Фото", (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic")),
    ("Архивы", (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2")),
    ("Установщики", (".exe", ".msi", ".bat", ".apk")),
    ("Документы", (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".md", ".csv",
                   ".rtf", ".odt", ".ods")),
)
MAX_PLAN_ROWS = 200
# Файлы крупнее этого не двигаем: база станка, образ диска и всё, что владелец
# скорее всего держит на месте сознательно.
MAX_MOVE_BYTES = 512 * 1024 * 1024


def allowed_roots(extra: list[str] | None = None) -> list[pathlib.Path]:
    """Папки, внутри которых ассистенту разрешено смотреть и двигать файлы."""
    from . import config

    roots: list[pathlib.Path] = []
    for raw in list(config.file_folders()) + list(extra or []):
        try:
            path = pathlib.Path(str(raw or "")).expanduser()
        except (OSError, ValueError):
            continue
        text = str(path).strip()
        if not text:
            continue
        resolved = _resolve(path)
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _resolve(path: pathlib.Path) -> pathlib.Path:
    """Путь без `..` и символических ссылок: иначе «внутри папки» проверяется на глаз."""
    try:
        return path.resolve(strict=False)
    except (OSError, ValueError):
        return path.absolute()


def inside_allowed(path: str | pathlib.Path, extra: list[str] | None = None
                   ) -> tuple[bool, str, pathlib.Path]:
    """Лежит ли путь внутри разрешённых папок. Возвращает (можно, причина, путь)."""
    target = _resolve(pathlib.Path(str(path or "")).expanduser())
    roots = allowed_roots(extra)
    if not roots:
        return False, ("Папки ассистента не заданы: укажите PRINTFLOW_ASSISTANT_FOLDERS "
                       "или PRINTFLOW_DOWNLOADS_FOLDER"), target
    for root in roots:
        try:
            target.relative_to(root)
            return True, "", target
        except ValueError:
            continue
    shown = ", ".join(str(root) for root in roots[:4])
    return False, f"Путь «{target}» вне разрешённых папок ({shown})", target


def destination_for(path: pathlib.Path) -> tuple[str, str]:
    """Куда просится файл и почему. Пустая папка назначения — значит «не трогай»."""
    name = path.name.casefold()
    for folder, hints in NAME_RULES:
        for hint in hints:
            if hint.casefold() in name:
                return folder, f"в имени есть «{hint}»"
    suffix = path.suffix.lower()
    for folder, suffixes in SUFFIX_RULES:
        if suffix in suffixes:
            return folder, f"расширение {suffix or '—'}"
    return "", "ни имя, ни расширение не подходят ни под одну папку"


def unique_target(folder: pathlib.Path, name: str) -> pathlib.Path:
    """Имя без перезаписи: «счёт.pdf» → «счёт (2).pdf», если первый уже лежит."""
    candidate = folder / name
    if not candidate.exists():
        return candidate
    stem, suffix = pathlib.Path(name).stem, pathlib.Path(name).suffix
    for number in range(2, 1000):
        candidate = folder / f"{stem} ({number}){suffix}"
        if not candidate.exists():
            return candidate
    return folder / f"{stem}-{re.sub(r'[^0-9]', '', str(id(name))[:6]) or 'x'}{suffix}"


def plan(folder: str | pathlib.Path) -> dict[str, Any]:
    """План раскладки: что куда переедет и почему. Ничего не двигает."""
    allowed, reason, root = inside_allowed(folder)
    if not allowed:
        return {"ok": False, "folder": str(root), "moves": [], "skipped": [],
                "reason": reason}
    if not root.is_dir():
        return {"ok": False, "folder": str(root), "moves": [], "skipped": [],
                "reason": "Папка не найдена"}
    known = {name for name, _ in NAME_RULES} | {name for name, _ in SUFFIX_RULES}
    moves: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    # Сканируем все файлы под папкой: прямые — кандидаты на перемещение,
    # лежащие в подпапках назначения — уже разложены и показываются как
    # пропущенные. Так тест `already_sorted` видит файл внутри `Модели/`.
    for path in sorted(root.rglob("*")):
        if len(moves) + len(skipped) >= MAX_PLAN_ROWS:
            break
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if len(rel.parts) != 1:
                # Файл в подпапке: если это папка назначения — уже лежит.
                if rel.parts[0] in known:
                    skipped.append({"path": str(path),
                                    "reason": "уже лежит в папке назначения"})
                else:
                    skipped.append({"path": str(path),
                                    "reason": "в подпапке — не трогаем"})
                continue
            if path.name.startswith("."):
                skipped.append({"path": str(path), "reason": "служебный файл (имя с точки)"})
                continue
            size = path.stat().st_size
            if size > MAX_MOVE_BYTES:
                skipped.append({"path": str(path),
                                "reason": f"файл больше {MAX_MOVE_BYTES // (1024 * 1024)} МБ"})
                continue
        except OSError as exc:
            skipped.append({"path": str(path), "reason": f"не читается: {exc.__class__.__name__}"})
            continue
        destination, why = destination_for(path)
        if not destination:
            skipped.append({"path": str(path), "reason": why})
            continue
        target_folder = root / destination
        moves.append({"path": str(path), "name": path.name, "destination": destination,
                      "target": str(unique_target(target_folder, path.name)),
                      "reason": why, "bytes": size})
    return {"ok": True, "folder": str(root), "moves": moves, "skipped": skipped,
            "reason": "", "count": len(moves),
            "total_bytes": sum(int(row["bytes"]) for row in moves),
            "hint": ("Ничего не перемещено: это план. Подтвердите, чтобы разложить файлы"
                     if moves else "Раскладывать нечего")}


def execute(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Исполнить план. Только перемещения внутри той же папки, удаления нет."""
    done: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for row in rows or []:
        source = pathlib.Path(str(row.get("path") or ""))
        target = pathlib.Path(str(row.get("target") or ""))
        allowed, reason, resolved = inside_allowed(source)
        if not allowed:
            failed.append({"path": str(resolved), "reason": reason})
            continue
        try:
            if not source.is_file():
                failed.append({"path": str(source), "reason": "файл исчез до перемещения"})
                continue
            if source.resolve(strict=False) == target.resolve(strict=False):
                failed.append({"path": str(source), "reason": "источник и назначение совпали"})
                continue
            source_root = source.parent.resolve(strict=False)
            target_parent = target.parent.resolve(strict=False)
            if target_parent == source_root or target_parent.parent != source_root:
                # Целевая папка обязана быть подпапкой той же папки: план, который
                # кто-то подменил, не уедет в другое место диска.
                failed.append({"path": str(source),
                               "reason": f"назначение «{target}» не в подпапке «{source_root}»"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            done.append({"path": str(source), "target": str(target),
                         "destination": str(row.get("destination") or "")})
        except (OSError, shutil.Error) as exc:
            failed.append({"path": str(source), "reason": f"{exc.__class__.__name__}: {exc}"})
    return {"ok": not failed, "moved": len(done), "done": done, "failed": failed,
            "reason": "" if not failed else f"{len(failed)} файл(ов) не перемещены"}
