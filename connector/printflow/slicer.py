"""CLI-обёртка Orca / Bambu Studio / PrusaSlicer — не движок с нуля.

Документы запрещают писать свой слайсер. Если бинарь не найден —
``available=False`` и честный отказ. Фейковый G-code из STL не генерируем.
Команда: ``{bin} --slice --output {outdir} {input}``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import DATA_DIR, UPLOAD_DIR

CANDIDATE_BINS = (
    "orca-slicer",
    "OrcaSlicer",
    "orcaslicer",
    "bambu-studio",
    "BambuStudio",
    "bambustudio",
    "prusa-slicer",
    "prusa-slicer-console",
    "PrusaSlicer",
)

_WIN_DIRS = (
    r"C:\Program Files\OrcaSlicer",
    r"C:\Program Files\Bambu Studio",
    r"C:\Program Files\BambuStudio",
    r"C:\Program Files\PrusaSlicer",
    r"C:\Program Files\PrusaSlicer\prusa-slicer-console.exe",
)

_LINUX_DIRS = (
    Path.home() / ".local" / "bin",
    Path("/usr/bin"),
    Path("/usr/local/bin"),
    Path("/opt/OrcaSlicer"),
    Path("/opt/BambuStudio"),
    Path("/opt/PrusaSlicer"),
)

SLICE_EXTS = {".stl", ".obj", ".3mf", ".step", ".stp"}
OUTPUT_EXTS = (".gcode.3mf", ".gcode", ".3mf")


class SlicerError(ValueError):
    """Слайсер недоступен или нарезка не удалась."""


def _looks_like_bin(path: Path) -> bool:
    name = path.name.lower()
    return any(token in name for token in (
        "orca", "bambu", "prusa", "slicer",
    )) and path.is_file()


def find_slicer_bin(explicit: str = "") -> str:
    """Путь к CLI слайсера: настройка, PATH, типичные каталоги."""
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path)
        found = shutil.which(explicit)
        if found:
            return found
    for name in CANDIDATE_BINS:
        found = shutil.which(name)
        if found:
            return found
    extra: list[Path] = []
    if os.name == "nt":
        extra.extend(Path(p) for p in _WIN_DIRS)
        pf = os.environ.get("ProgramFiles")
        if pf:
            extra.append(Path(pf) / "OrcaSlicer")
            extra.append(Path(pf) / "Bambu Studio")
            extra.append(Path(pf) / "PrusaSlicer")
    else:
        extra.extend(_LINUX_DIRS)
    for folder in extra:
        if folder.is_file() and _looks_like_bin(folder):
            return str(folder)
        if not folder.is_dir():
            continue
        for name in CANDIDATE_BINS:
            cand = folder / name
            if cand.is_file():
                return str(cand)
            exe = folder / f"{name}.exe"
            if exe.is_file():
                return str(exe)
        for child in folder.iterdir():
            if _looks_like_bin(child):
                return str(child)
    return ""


def slicer_name(bin_path: str) -> str:
    lower = Path(bin_path).name.lower()
    if "orca" in lower:
        return "OrcaSlicer"
    if "bambu" in lower:
        return "Bambu Studio"
    if "prusa" in lower:
        return "PrusaSlicer"
    return Path(bin_path).stem or "slicer"


def status(explicit: str = "") -> dict:
    """Состояние CLI: available, bin, name, error."""
    bin_path = find_slicer_bin(explicit)
    if not bin_path:
        return {
            "available": False,
            "bin": "",
            "name": "",
            "error": (
                "Слайсер не найден. Установите OrcaSlicer, Bambu Studio "
                "или PrusaSlicer и укажите путь в настройках, либо добавьте "
                "бинарь в PATH. PrintFlow не нарезает модели сам."
            ),
        }
    return {
        "available": True,
        "bin": bin_path,
        "name": slicer_name(bin_path),
        "error": "",
    }


def _collect_output(folder: Path) -> Path | None:
    files: list[Path] = []
    if not folder.is_dir():
        return None
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.endswith(OUTPUT_EXTS) and not name.startswith("."):
            files.append(path)
    if not files:
        return None
    files.sort(key=lambda p: (
        0 if p.name.lower().endswith(".gcode.3mf") else
        1 if p.name.lower().endswith(".gcode") else 2,
        -p.stat().st_mtime,
    ))
    return files[0]


def slice_file(input_path: str | Path, output_dir: str | Path | None = None,
               explicit_bin: str = "", timeout: int = 600) -> dict:
    """Нарезать STL/3MF через CLI. Без бинаря — SlicerError, без фейкового G-code."""
    src = Path(input_path).expanduser()
    if not src.is_file():
        raise SlicerError(f"Файл не найден: {src.name}")
    ext = src.suffix.lower()
    if src.name.lower().endswith(".gcode.3mf") or ext == ".gcode":
        raise SlicerError("Файл уже нарезан — отправьте его в очередь как есть")
    if ext not in SLICE_EXTS and not src.name.lower().endswith(".3mf"):
        raise SlicerError(f"Слайсер не принимает {src.suffix or 'этот тип'}")
    info = status(explicit_bin)
    if not info["available"]:
        raise SlicerError(info["error"])
    out_root = Path(output_dir) if output_dir else (DATA_DIR / "slicer-out")
    out_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="pf-slice-", dir=str(out_root)))
    cmd = [info["bin"], "--slice", "--output", str(work), str(src)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(work),
        )
    except subprocess.TimeoutExpired as exc:
        raise SlicerError(f"Слайсер не ответил за {timeout} с") from exc
    except OSError as exc:
        raise SlicerError(f"Не удалось запустить слайсер: {exc}") from exc
    produced = _collect_output(work)
    if proc.returncode != 0 or produced is None:
        err = (proc.stderr or proc.stdout or "").strip()[:800]
        raise SlicerError(
            "Слайсер не нарезал файл. "
            + (err or f"код {proc.returncode}, выходных G-code/3MF нет")
        )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / produced.name
    if dest.exists():
        dest = UPLOAD_DIR / f"{produced.stem}-{src.stem[:12]}{produced.suffix}"
    shutil.copy2(produced, dest)
    return {
        "ok": True,
        "bin": info["bin"],
        "name": info["name"],
        "input": src.name,
        "output": dest.name,
        "path": str(dest),
        "size": dest.stat().st_size,
    }
