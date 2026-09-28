"""Local Secret Vault for NOZZA.

On Windows values are encrypted with the current-user DPAPI scope. The public
API deliberately has no reveal operation: plaintext can only be resolved by
trusted provider code inside the agent process.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from .store import now_iso


class VaultCipher(Protocol):
    @property
    def available(self) -> bool: ...
    @property
    def reason(self) -> str: ...
    def protect(self, data: bytes) -> bytes: ...
    def unprotect(self, data: bytes) -> bytes: ...


class _Blob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


class DpapiCipher:
    @property
    def available(self) -> bool:
        return sys.platform == "win32"

    @property
    def reason(self) -> str:
        return "" if self.available else "DPAPI доступен только на Windows"

    @staticmethod
    def _blob(data: bytes) -> tuple[_Blob, Any]:
        buf = ctypes.create_string_buffer(data)
        blob = _Blob(
            len(data),
            ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)),
        )
        return blob, buf

    def protect(self, data: bytes) -> bytes:
        if not self.available:
            raise RuntimeError(self.reason)
        source, _source_buf = self._blob(data)
        out = _Blob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        ok = crypt32.CryptProtectData(
            ctypes.byref(source),
            "NOZZA Secret Vault",
            None, None, None,
            0x1,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(out),
        )
        if not ok:
            raise OSError("CryptProtectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)

    def unprotect(self, data: bytes) -> bytes:
        if not self.available:
            raise RuntimeError(self.reason)
        source, _source_buf = self._blob(data)
        out = _Blob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            None, None, None,
            0x1,
            ctypes.byref(out),
        )
        if not ok:
            raise OSError("CryptUnprotectData failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)


@dataclass(slots=True)
class SecretVault:
    store: Any
    cipher: VaultCipher | None = None

    def __post_init__(self) -> None:
        if self.cipher is None:
            self.cipher = DpapiCipher()
        self.store._run(
            """CREATE TABLE IF NOT EXISTS assistant_secrets(
                name TEXT PRIMARY KEY,
                ciphertext TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '{}')"""
        )

    def status(self) -> dict[str, Any]:
        cipher = self.cipher
        return {
            "available": bool(cipher and cipher.available),
            "reason": "" if cipher and cipher.available else str(
                cipher.reason if cipher else "Нет cipher backend"),
            "count": len(self.list()),
            "backend": "windows-dpapi" if isinstance(cipher, DpapiCipher) else "injected",
        }

    @staticmethod
    def _name(value: Any) -> str:
        name = " ".join(str(value or "").split())[:120]
        if not name:
            raise ValueError("У секрета нет имени")
        return name

    def put(self, name: str, secret: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        cipher = self.cipher
        if cipher is None or not cipher.available:
            return {"ok": False, "reason": str(cipher.reason if cipher else "Vault недоступен")}
        try:
            clean_name = self._name(name)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        raw = str(secret or "")
        if not raw:
            return {"ok": False, "reason": "Пустой секрет не сохраняется"}
        if len(raw) > 16_384:
            return {"ok": False, "reason": "Секрет слишком большой"}
        try:
            encrypted = cipher.protect(raw.encode("utf-8"))
        except Exception as exc:
            return {"ok": False, "reason": f"Шифрование не сработало: {exc.__class__.__name__}"}
        encoded = base64.b64encode(encrypted).decode("ascii")
        now = now_iso()
        existing = self.store._rows(
            "SELECT created_at FROM assistant_secrets WHERE name=?", (clean_name,))
        created = str(existing[0]["created_at"]) if existing else now
        self.store._run(
            "INSERT INTO assistant_secrets(name,ciphertext,created_at,updated_at,meta_json) "
            "VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "ciphertext=excluded.ciphertext,updated_at=excluded.updated_at,meta_json=excluded.meta_json",
            (
                clean_name, encoded, created, now,
                json.dumps(dict(meta or {}), ensure_ascii=False, default=str)[:4000],
            ),
        )
        return {"ok": True, "secret": self.get_meta(clean_name), "replaced": bool(existing)}

    def get_meta(self, name: str) -> dict[str, Any] | None:
        rows = self.store._rows(
            "SELECT name,created_at,updated_at,meta_json FROM assistant_secrets WHERE name=?",
            (str(name),),
        )
        if not rows:
            return None
        row = dict(rows[0])
        try:
            row["meta"] = json.loads(row.pop("meta_json") or "{}")
        except (TypeError, ValueError):
            row["meta"] = {}
        return row

    def list(self) -> list[dict[str, Any]]:
        rows = self.store._rows(
            "SELECT name,created_at,updated_at,meta_json FROM assistant_secrets ORDER BY name")
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["meta"] = json.loads(item.pop("meta_json") or "{}")
            except (TypeError, ValueError):
                item["meta"] = {}
            out.append(item)
        return out

    def delete(self, name: str) -> dict[str, Any]:
        count = int(self.store._run(
            "DELETE FROM assistant_secrets WHERE name=?", (str(name),)).rowcount or 0)
        return {"ok": bool(count), "deleted": bool(count),
                "reason": "" if count else "Секрет не найден"}

    def resolve(self, name: str) -> tuple[str, str]:
        """Trusted internal use only. Never expose this through HTTP or Brain."""
        cipher = self.cipher
        if cipher is None or not cipher.available:
            return "", str(cipher.reason if cipher else "Vault недоступен")
        rows = self.store._rows(
            "SELECT ciphertext FROM assistant_secrets WHERE name=?", (str(name),))
        if not rows:
            return "", "Секрет не найден"
        try:
            protected = base64.b64decode(str(rows[0]["ciphertext"]).encode("ascii"), validate=True)
            plain = cipher.unprotect(protected).decode("utf-8")
            return plain, ""
        except Exception as exc:
            return "", f"Секрет не расшифрован: {exc.__class__.__name__}"
