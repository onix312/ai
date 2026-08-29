"""Внутреннее хранилище файлов цеха — content-addressed копия + запись в SQLite.

Путь на диске: ``DATA_DIR/library/{sha256[:2]}/{sha256}{ext}``.
Копия с исходным именем кладётся в ``UPLOAD_DIR``, чтобы ``enqueue``
и FTPS к принтеру не менялись. Таблица ``library_files`` создаётся
через ``ensure_schema`` как ModelRegistry — SCHEMA_VERSION не бампаем.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .accounting import uid
from .config import DATA_DIR, UPLOAD_DIR, now_iso

LIBRARY_DIR = DATA_DIR / "library"

_KIND_BY_EXT = {
    ".stl": "stl",
    ".obj": "stl",
    ".3mf": "3mf",
    ".gcode": "gcode",
    ".gcode.3mf": "3mf",
}


def file_kind(name: str) -> str:
    lower = (name or "").lower()
    if lower.endswith(".gcode.3mf"):
        return "3mf"
    ext = Path(lower).suffix
    return _KIND_BY_EXT.get(ext, "other")


def _safe_ext(name: str) -> str:
    lower = (name or "").lower()
    if lower.endswith(".gcode.3mf"):
        return ".gcode.3mf"
    ext = Path(name or "").suffix
    if ext.lower() in {".stl", ".obj", ".3mf", ".gcode", ".png", ".jpg", ".jpeg"}:
        return ext
    return ext[:12] if ext else ""


class FileLibrary:
    """Каталог STL/3MF/G-code: SHA-хранилище + метаданные в SQLite."""

    def __init__(self, db):
        self.db = db
        self.ensure_schema()

    def ensure_schema(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS library_files (
                id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                ext TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'other',
                size INTEGER NOT NULL DEFAULT 0,
                path TEXT NOT NULL DEFAULT '',
                upload_name TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                nom_id TEXT NOT NULL DEFAULT '',
                order_id TEXT NOT NULL DEFAULT '',
                model_id TEXT NOT NULL DEFAULT '',
                grams REAL NOT NULL DEFAULT 0,
                minutes REAL NOT NULL DEFAULT 0,
                material TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0
            )
        """)
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_library_sha ON library_files(sha256)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_library_kind ON library_files(kind)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_library_created ON library_files(created_at)"
        )

    def put(self, name: str, data: bytes, source: str = "",
            nom_id: str = "", order_id: str = "", model_id: str = "",
            note: str = "", estimate: dict | None = None) -> dict:
        """Сохранить байты: SHA-путь + копия в uploads + строка в БД."""
        if not data:
            raise ValueError("Пустой файл")
        name = Path(name or "file.bin").name
        sha = hashlib.sha256(data).hexdigest()
        ext = _safe_ext(name)
        kind = file_kind(name)
        LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        dest_dir = LIBRARY_DIR / sha[:2]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{sha}{ext}"
        if not dest.exists():
            dest.write_bytes(data)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        upload_name = name
        upload_path = UPLOAD_DIR / upload_name
        if upload_path.exists() and upload_path.stat().st_size != len(data):
            stem = Path(name).stem
            upload_name = f"{stem}-{sha[:8]}{ext}"
            upload_path = UPLOAD_DIR / upload_name
        if not upload_path.exists():
            upload_path.write_bytes(data)
        est = estimate or {}
        grams = float(est.get("total_grams") or est.get("grams") or 0) or 0
        minutes = float(est.get("total_minutes") or est.get("minutes") or 0) or 0
        material = str(est.get("material") or "")
        existing = self.db.one(
            "SELECT * FROM library_files WHERE sha256=? AND archived=0", (sha,)
        )
        now = now_iso()
        if existing:
            self.db.execute(
                """UPDATE library_files SET name=?, upload_name=?, source=?,
                   nom_id=COALESCE(NULLIF(?, ''), nom_id),
                   order_id=COALESCE(NULLIF(?, ''), order_id),
                   model_id=COALESCE(NULLIF(?, ''), model_id),
                   grams=CASE WHEN ? > 0 THEN ? ELSE grams END,
                   minutes=CASE WHEN ? > 0 THEN ? ELSE minutes END,
                   material=COALESCE(NULLIF(?, ''), material),
                   note=COALESCE(NULLIF(?, ''), note),
                   updated_at=? WHERE id=?""",
                (name, upload_name, source or existing["source"],
                 nom_id, order_id, model_id,
                 grams, grams, minutes, minutes, material, note, now, existing["id"]),
            )
            return self.get(existing["id"])
        fid = uid("lib")
        self.db.execute(
            """INSERT INTO library_files
               (id, sha256, name, ext, kind, size, path, upload_name, source,
                nom_id, order_id, model_id, grams, minutes, material, note,
                created_at, updated_at, archived)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (fid, sha, name, ext, kind, len(data), str(dest), upload_name, source,
             nom_id, order_id, model_id, grams, minutes, material, note, now, now),
        )
        return self.get(fid)

    def put_path(self, path: str | Path, source: str = "", **kwargs) -> dict:
        path = Path(path)
        return self.put(path.name, path.read_bytes(), source=source, **kwargs)

    def get(self, fid: str) -> dict:
        row = self.db.one("SELECT * FROM library_files WHERE id=?", (fid,))
        if not row:
            raise KeyError("Файл не найден")
        return dict(row)

    def find_sha(self, sha: str) -> dict | None:
        row = self.db.one(
            "SELECT * FROM library_files WHERE sha256=? AND archived=0", (sha,)
        )
        return dict(row) if row else None

    def list(self, kind: str = "", q: str = "", limit: int = 80) -> list[dict]:
        where = ["archived=0"]
        args: list = []
        if kind:
            where.append("kind=?")
            args.append(kind)
        if q:
            where.append("(name LIKE ? OR note LIKE ? OR material LIKE ?)")
            like = f"%{q}%"
            args.extend([like, like, like])
        sql = "SELECT * FROM library_files WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit or 80))
        return [dict(r) for r in self.db.query(sql, tuple(args))]

    def delete(self, fid: str, purge: bool = False) -> None:
        row = self.get(fid)
        if purge:
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except Exception:
                pass
            self.db.execute("DELETE FROM library_files WHERE id=?", (fid,))
            return
        self.db.execute(
            "UPDATE library_files SET archived=1, updated_at=? WHERE id=?",
            (now_iso(), fid),
        )

    def resolve(self, fid: str) -> Path:
        """Путь к байтам: SHA-файл, иначе копия в uploads."""
        row = self.get(fid)
        path = Path(row.get("path") or "")
        if path.is_file():
            return path
        upload = UPLOAD_DIR / (row.get("upload_name") or row.get("name") or "")
        if upload.is_file():
            return upload
        raise FileNotFoundError("Файл пропал с диска")

    def upload_name(self, fid: str) -> str:
        row = self.get(fid)
        name = row.get("upload_name") or row.get("name")
        if not name:
            raise FileNotFoundError("У файла нет имени в uploads")
        return name
