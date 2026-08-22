"""Поиск заказов по фото PrintFlow 8.5 (идея 19).

Честный алгоритм без ML: average-hash (aHash) 8×8 — пиксели уменьшенного
серого кадра сравниваются со средним значением, получаются 64 бита.
Похожие фото дают малое расстояние Хэмминга. Pillow опционален: без него
модуль честно отключается (как в spaghetti.py).
"""
from __future__ import annotations

from typing import Any

from .config import PHOTO_DIR
from .db import Database

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:  # pragma: no cover — Pillow опционален
    HAS_PIL = False


def a_hash_of_bytes(data: bytes) -> int | None:
    """64-битный average-hash JPEG/PNG-изображения; None — если не удалось."""
    if not HAS_PIL:
        return None
    try:
        import io
        img = Image.open(io.BytesIO(data)).convert("L").resize((8, 8))
        values = list(img.getdata())
        if not values:
            return None
        mean = sum(values) / len(values)
        bits = 0
        for v in values:
            bits = (bits << 1) | (1 if v > mean else 0)
        return bits
    except Exception:
        return None


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _photo_file(path_name: str) -> Any:
    if not path_name:
        return None
    from pathlib import Path
    p = Path(path_name)
    if not p.is_absolute():
        p = PHOTO_DIR / p
    return p if p.is_file() else None


def similar(db: Database, photo_id: str, limit: int = 10) -> dict[str, Any]:
    """Фото, похожие на заданное: по всем фото заказов."""
    if not HAS_PIL:
        return {"ok": False, "error": "Pillow не установлен — поиск по фото выключен"}
    photo = db.one("SELECT * FROM order_photos WHERE id=?", (photo_id,))
    if not photo:
        raise ValueError("Фото не найдено")
    f = _photo_file(str(photo.get("file") or ""))
    if not f:
        raise ValueError("Файл фото не найден")
    target = a_hash_of_bytes(f.read_bytes())
    if target is None:
        raise ValueError("Не удалось разобрать фото")
    out = []
    for row in db.query("SELECT * FROM order_photos WHERE id<>? ORDER BY at DESC", (photo_id,)):
        f2 = _photo_file(str(row.get("file") or ""))
        if not f2:
            continue
        h2 = a_hash_of_bytes(f2.read_bytes())
        if h2 is None:
            continue
        out.append({"photo_id": row["id"], "order_id": row.get("order_id") or "",
                    "at": row.get("at") or "", "note": row.get("note") or "",
                    "distance": hamming(target, h2)})
    out.sort(key=lambda x: x["distance"])
    # «похоже» — расстояние меньше четверти бит; дальше — просто список
    out = [{**x, "close": x["distance"] <= 16} for x in out[:max(1, limit)]]
    return {"ok": True, "photos": out}
