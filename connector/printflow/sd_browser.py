"""SD-карта принтера: LIST/CWD Unix/DOS/MLSD, папки и безопасные пути.

Печатать можно только 3MF/G-code. Журналы, таймлапс и ipcam — смотреть,
но не запускать. Папки всегда видны.
"""
from __future__ import annotations

import re
from typing import Iterable

PRINT_EXT = (".3mf", ".gcode", ".gcode.3mf")
MEDIA_EXT = (".jpg", ".jpeg", ".png", ".mp4", ".avi", ".log", ".txt")
BLOCK_PRINT_DIRS = ("timelapse", "ipcam", "log", "logs")
KNOWN_DIRS = ("cache", "timelapse", "ipcam", "log", "model", "firmware")

_UNIX = re.compile(
    r"^(?P<perm>[-dlbcps][-rwxstST]{9})\s+"
    r"(?P<nlink>\d+)\s+\S+\s+\S+\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<month>\S+)\s+(?P<day>\d+)\s+(?P<time>\S+)\s+"
    r"(?P<name>.+)$"
)
_DOS = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{2})\s+"
    r"(?P<clock>\d{1,2}:\d{2}(?:AM|PM)?)\s+"
    r"(?P<dir><DIR>|\d+)\s+"
    r"(?P<name>.+)$",
    re.IGNORECASE,
)
_MLSD_FACT = re.compile(r"([^=;]+)=([^;]*);")


def sanitize_remote_path(path: str | None) -> str:
    """Нормализовать путь на SD: без `..`, null и обратных слешей."""
    raw = str(path or "/").replace("\\", "/").replace("\x00", "")
    parts: list[str] = []
    for chunk in raw.split("/"):
        name = chunk.strip()
        if not name or name in (".",):
            continue
        if name == ".." or name.startswith(".."):
            raise ValueError("Путь не должен содержать «..»")
        if len(name) > 180:
            raise ValueError("Слишком длинное имя на карте")
        parts.append(name)
    out = "/" + "/".join(parts)
    if len(out) > 240:
        raise ValueError("Слишком длинный путь на карте")
    return out


def parent_path(path: str) -> str:
    clean = sanitize_remote_path(path)
    if clean == "/":
        return "/"
    return sanitize_remote_path("/".join(clean.rstrip("/").split("/")[:-1]) or "/")


def join_path(folder: str, name: str) -> str:
    base = sanitize_remote_path(folder)
    leaf = str(name or "").replace("\\", "/").split("/")[-1].strip()
    if not leaf or leaf in (".", ".."):
        raise ValueError("Некорректное имя файла")
    return sanitize_remote_path((base.rstrip("/") + "/" + leaf) if base != "/" else "/" + leaf)


def breadcrumbs(path: str) -> list[dict]:
    clean = sanitize_remote_path(path)
    crumbs = [{"name": "SD", "path": "/"}]
    acc = ""
    for chunk in [p for p in clean.split("/") if p]:
        acc += "/" + chunk
        crumbs.append({"name": chunk, "path": acc})
    return crumbs


def _is_print_name(name: str) -> bool:
    low = name.lower()
    return low.endswith(".gcode.3mf") or low.endswith(".3mf") or low.endswith(".gcode")


def _is_media_name(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(ext) for ext in MEDIA_EXT)


def path_blocks_print(path: str) -> bool:
    parts = {p.lower() for p in sanitize_remote_path(path).split("/") if p}
    return bool(parts & set(BLOCK_PRINT_DIRS))


def classify_entry(name: str, *, is_dir: bool, folder: str = "/", size: int = 0) -> dict:
    """Классифицировать элемент листинга: dir / print / media / other."""
    folder = sanitize_remote_path(folder)
    full = join_path(folder, name) if name not in (".", "..") else folder
    kind = "other"
    printable = False
    if is_dir:
        kind = "dir"
    elif _is_print_name(name):
        kind = "print"
        printable = not path_blocks_print(full)
    elif _is_media_name(name) or path_blocks_print(full):
        kind = "media"
    return {
        "name": name,
        "path": full if not is_dir else full.rstrip("/") or "/",
        "dir": bool(is_dir),
        "kind": kind,
        "size": int(size or 0),
        "printable": printable,
        "folder": folder,
    }


def parse_unix_line(line: str) -> dict | None:
    text = line.rstrip("\r\n")
    if not text or text.startswith("total "):
        return None
    m = _UNIX.match(text)
    if not m:
        return None
    name = m.group("name").split(" -> ", 1)[0].strip()
    if name in (".", ".."):
        return None
    return {
        "name": name,
        "dir": m.group("perm").startswith("d"),
        "size": int(m.group("size") or 0),
    }


def parse_dos_line(line: str) -> dict | None:
    text = line.rstrip("\r\n")
    if not text:
        return None
    m = _DOS.match(text)
    if not m:
        return None
    name = m.group("name").strip()
    if name in (".", ".."):
        return None
    raw_dir = m.group("dir")
    is_dir = str(raw_dir).upper() == "<DIR>"
    size = 0 if is_dir else int(raw_dir)
    return {"name": name, "dir": is_dir, "size": size}


def parse_mlsd_line(line: str) -> dict | None:
    text = line.rstrip("\r\n")
    if not text or " " not in text or "=" not in text or ";" not in text:
        return None
    facts, name = text.split(" ", 1)
    name = name.strip()
    if not name or name in (".", ".."):
        return None
    parsed = {k.lower(): v for k, v in _MLSD_FACT.findall(facts + ("" if facts.endswith(";") else ";"))}
    if not parsed or "type" not in parsed:
        return None
    typ = (parsed.get("type") or "").lower()
    if typ in ("cdir", "pdir"):
        return None
    is_dir = typ == "dir" or typ.endswith("dir")
    try:
        size = int(parsed.get("size") or 0)
    except ValueError:
        size = 0
    return {"name": name, "dir": is_dir, "size": size}


def parse_listing(raw: str | bytes | Iterable[str], folder: str = "/") -> list[dict]:
    """Разобрать Unix LIST, DOS LIST или MLSD в классифицированные записи."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "ignore")
    elif isinstance(raw, str):
        text = raw
    else:
        text = "\n".join(str(x) for x in raw)
    folder = sanitize_remote_path(folder)
    seen: dict[str, dict] = {}
    for line in text.splitlines():
        parsed = parse_mlsd_line(line) or parse_unix_line(line) or parse_dos_line(line)
        if not parsed:
            continue
        item = classify_entry(parsed["name"], is_dir=parsed["dir"],
                              folder=folder, size=parsed.get("size") or 0)
        seen[item["name"].lower()] = item
    items = list(seen.values())
    items.sort(key=lambda x: (0 if x["dir"] else 1, x["name"].lower()))
    return items


def can_print(path: str, name: str = "") -> bool:
    leaf = name or path.rsplit("/", 1)[-1]
    if not _is_print_name(leaf):
        return False
    return not path_blocks_print(path)


def list_via_ftp(ftp, path: str = "/") -> list[dict]:
    """Обойти прошивки: MLSD → LIST path → CWD+LIST → NLST."""
    path = sanitize_remote_path(path)
    lines: list[str] = []

    def collect(cmd: str, arg: str = "") -> bool:
        buf: list[str] = []
        try:
            if arg:
                ftp.retrlines(f"{cmd} {arg}", buf.append)
            else:
                ftp.retrlines(cmd, buf.append)
        except Exception:
            return False
        if buf:
            lines.extend(buf)
            return True
        return False

    if not collect("MLSD", path if path != "/" else ""):
        if not collect("LIST", path):
            cwd_ok = False
            try:
                ftp.cwd(path)
                cwd_ok = True
            except Exception:
                cwd_ok = False
            if cwd_ok:
                if not collect("MLSD"):
                    collect("LIST")
            if not lines:
                try:
                    names = ftp.nlst(path if path != "/" else ".")
                except Exception:
                    names = []
                for raw_name in names:
                    leaf = str(raw_name).replace("\\", "/").rstrip("/").split("/")[-1]
                    if not leaf or leaf in (".", ".."):
                        continue
                    guess_dir = "." not in leaf or leaf.lower() in KNOWN_DIRS
                    item = classify_entry(leaf, is_dir=guess_dir, folder=path, size=0)
                    lines.append(item["name"])
                    # NLST не даёт тип — вернём как есть через синтетический unix
    if lines and all(" " not in ln and ";" not in ln for ln in lines[:8]):
        # чистый NLST
        items = []
        for raw_name in lines:
            leaf = str(raw_name).replace("\\", "/").rstrip("/").split("/")[-1]
            if not leaf or leaf in (".", ".."):
                continue
            guess_dir = leaf.lower() in KNOWN_DIRS or "." not in leaf
            items.append(classify_entry(leaf, is_dir=guess_dir, folder=path, size=0))
        items.sort(key=lambda x: (0 if x["dir"] else 1, x["name"].lower()))
        return items
    return parse_listing(lines, folder=path)


def preview_from_bytes(name: str, data: bytes, *, path: str = "") -> dict:
    """Короткое превью 3MF/G-code без печати media/логов."""
    if path and path_blocks_print(path):
        raise ValueError("Превью печати из timelapse/log недоступно")
    leaf = name or (path.rsplit("/", 1)[-1] if path else "")
    if not _is_print_name(leaf):
        raise ValueError("Превью только для 3MF и G-code")
    from pathlib import Path
    import tempfile
    suffix = ".gcode.3mf" if leaf.lower().endswith(".gcode.3mf") else Path(leaf).suffix or ".gcode"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data[:8 * 1024 * 1024])
        tmp_path = Path(tmp.name)
    try:
        from .estimate import estimate_file
        est = estimate_file(tmp_path) or {}
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    comments = []
    if not leaf.lower().endswith(".3mf"):
        text = data[:8000].decode("utf-8", "ignore")
        comments = [ln.strip() for ln in text.splitlines() if ln.startswith(";")][:40]
    return {
        "name": leaf,
        "path": path or leaf,
        "grams": est.get("total_grams") or est.get("grams") or 0,
        "minutes": est.get("total_minutes") or est.get("minutes") or 0,
        "material": est.get("material") or "",
        "color": est.get("color") or "",
        "plate_count": est.get("plate_count") or len(est.get("plates") or []) or 0,
        "filaments": est.get("filaments") or [],
        "comments": comments,
        "estimate": est,
    }
