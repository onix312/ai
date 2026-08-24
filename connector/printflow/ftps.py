"""Работа с файлами принтера по FTPS (implicit TLS, порт 990).

Позволяет читать список моделей на SD-карте, загружать 3MF/G-code с
компьютера и удалять файлы. Используется только в локальной сети.
"""
from __future__ import annotations

import ftplib
import ssl
from pathlib import Path
from typing import Callable

PRINT_EXT = (".3mf", ".gcode", ".gcode.3mf")


class _HeadLimit(Exception):
    """Внутреннее исключение: достигнут лимит чтения шапки файла с SD."""


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP_TLS с implicit TLS: соединение шифруется с первого байта."""

    def __init__(self, *args, **kwargs):
        self._sock = None
        super().__init__(*args, **kwargs)

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        conn = self.context.wrap_socket(conn, server_hostname=self.host,
                                        session=self.sock.session)
        return conn, size


class PrinterFiles:
    """Тонкая обёртка: каждое действие открывает и закрывает соединение."""

    def __init__(self, host: str, access_code: str, port: int = 990,
                 timeout: int = 8, retries: int = 3, block_size: int = 262144):
        self.host = host
        self.access_code = access_code
        self.port = port
        self.timeout = max(1, min(120, int(timeout)))
        self.retries = max(1, min(10, int(retries)))
        self.block_size = max(16 * 1024, min(4 * 1024 * 1024, int(block_size)))
        self.last_ok: bool | None = None
        self.last_error: str = ""

    def _connect(self) -> ImplicitFTPS:
        if not self.host or not self.access_code:
            raise ConnectionError("Не задан IP-адрес или Access Code принтера")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            context.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            pass
        ftp = ImplicitFTPS(context=context)
        ftp.encoding = "utf-8"
        ftp.connect(self.host, self.port, timeout=self.timeout)
        ftp.login("bblp", self.access_code)
        ftp.prot_p()
        ftp.set_pasv(True)
        return ftp

    def test(self) -> dict:
        try:
            ftp = self._connect()
            try:
                ftp.voidcmd("NOOP")
            finally:
                self._quit(ftp)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _quit(ftp: ImplicitFTPS) -> None:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    def list_files(self, path: str = "/") -> list[dict]:
        from . import sd_browser
        try:
            path = sd_browser.sanitize_remote_path(path)
        except ValueError as exc:
            self.last_ok = False
            self.last_error = str(exc)[:160]
            raise
        ftp = None
        try:
            ftp = self._connect()
            entries = sd_browser.list_via_ftp(ftp, path)
            self.last_ok = True
            self.last_error = ""
            return entries
        except Exception as exc:
            self.last_ok = False
            self.last_error = str(exc)[:160]
            raise
        finally:
            if ftp is not None:
                self._quit(ftp)

    @staticmethod
    def _join(base: str, name: str) -> str:
        return (base.rstrip("/") + "/" + name) if base not in ("", "/") else "/" + name

    @classmethod
    def _parse_line(cls, line: str, base: str) -> dict | None:
        parts = line.split(maxsplit=8)
        if len(parts) < 9:
            return None
        perms, size, name = parts[0], parts[4], parts[8]
        if name in (".", ".."):
            return None
        is_dir = perms.startswith("d")
        if not is_dir and not name.lower().endswith(PRINT_EXT):
            return None
        try:
            size_int = int(size)
        except ValueError:
            size_int = 0
        return {"name": name, "path": cls._join(base, name), "size": size_int,
                "dir": is_dir, "modified": " ".join(parts[5:8])}

    def upload(self, local_path: str | Path, remote_name: str,
               progress: Callable[[int], None] | None = None) -> dict:
        local = Path(local_path)
        if not local.exists():
            raise FileNotFoundError("Файл не найден")
        total = local.stat().st_size if local.exists() else 0
        sent = 0
        last_exc = None
        for attempt in range(self.retries):
            # После частичного обрыва новый прогресс начинается с нуля: байты
            # не дозаливаются, а весь файл отправляется повторно.
            sent = 0
            try:
                ftp = self._connect()
                try:
                    with local.open("rb") as fp:
                        def callback(block: bytes):
                            nonlocal sent
                            sent += len(block)
                            if progress:
                                try:
                                    progress(sent, total)
                                except TypeError:
                                    progress(sent)
                        ftp.storbinary(f"STOR {remote_name}", fp,
                                       blocksize=self.block_size, callback=callback)
                finally:
                    self._quit(ftp)
                return {"ok": True, "name": remote_name, "size": sent, "total": total, "sha": self.sha_short(local_path)}
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < self.retries:
                    import time
                    time.sleep(0.4 * (attempt + 1))
        raise last_exc if last_exc else ConnectionError("FTPS upload failed")

    def upload_bytes(self, data: bytes, remote_name: str) -> dict:
        import io
        ftp = self._connect()
        try:
            ftp.storbinary(f"STOR {remote_name}", io.BytesIO(data), blocksize=self.block_size)
        finally:
            self._quit(ftp)
        return {"ok": True, "name": remote_name, "size": len(data)}


    def sha_short(self, local_path: str | Path) -> str:
        """Короткий sha для дедупликации (первые 1МБ)."""
        import hashlib
        h=hashlib.sha256()
        try:
            with open(local_path,"rb") as f:
                h.update(f.read(1024*1024))
            return h.hexdigest()[:12]
        except Exception:
            return ""

    def disk_usage(self, path: str = "/") -> dict:
        """Попытка оценить занятость SD (не все прошивки отдают)."""
        try:
            ftp=self._connect()
            try:
                # PWD + LIST даёт размеры, суммируем
                entries=self.list_files(path)
                total=sum(e.get("size",0) for e in entries if not e.get("dir"))
                return {"used": total, "entries": len(entries)}
            finally:
                self._quit(ftp)
        except Exception as exc:
            return {"used": 0, "error": str(exc)}

    def list_tree(self, path: str = "/", depth: int = 1) -> list[dict]:
        """Дерево до depth=2."""
        root=self.list_files(path)
        if depth<=1:
            return root
        out=[]
        for e in root:
            out.append(e)
            if e.get("dir"):
                try:
                    children=self.list_files(e["path"])
                    for c in children:
                        c["parent"] = e["path"]
                    out.extend(children)
                except Exception:
                    pass
        return out

    def batch_delete(self, paths: list[str]) -> dict:
        ok=0; errors=[]
        for pr in paths:
            try:
                self.delete(pr)
                ok+=1
            except Exception as exc:
                errors.append(f"{pr}: {exc}")
        return {"ok": ok, "errors": errors, "total": len(paths)}

    def delete(self, path: str) -> dict:
        ftp = self._connect()
        try:
            ftp.delete(path)
        finally:
            self._quit(ftp)
        return {"ok": True}

    def download(self, path: str, max_bytes: int = 0) -> bytes:
        """Скачать файл с SD-карты целиком (или до max_bytes если >0)."""
        if not path:
            return b""
        chunks: list[bytes] = []

        def cb(block: bytes) -> None:
            chunks.append(block)
            if max_bytes and sum(len(c) for c in chunks) >= max_bytes:
                raise _HeadLimit()

        ftp = None
        try:
            ftp = self._connect()
            try:
                ftp.retrbinary(f"RETR {path}", cb, blocksize=16384)
            except (_HeadLimit, ftplib.error_temp, ftplib.error_perm, OSError, TimeoutError):
                pass
        except Exception:
            pass
        finally:
            if ftp is not None:
                self._quit(ftp)
        data = b"".join(chunks)
        return data[:max_bytes] if max_bytes else data

    def read_head(self, path: str, max_bytes: int = 131072) -> bytes:
        """Прочитать первые байты файла с SD-карты, не качая его целиком.

        Шапка G-code (;TIME, ;Filament used, материал/цвет) лежит в первых
        килобайтах — этого достаточно для оценки веса и времени печати.
        Чтение обрывается после max_bytes через исключение в callback —
        соединение закрывается, но полученные байты сохраняются.
        """
        if not path:
            return b""
        chunks: list[bytes] = []

        def cb(block: bytes) -> None:
            chunks.append(block)
            if sum(len(c) for c in chunks) >= max_bytes:
                raise _HeadLimit()

        ftp = None
        try:
            ftp = self._connect()
            try:
                ftp.retrbinary(f"RETR {path}", cb, blocksize=16384)
            except (_HeadLimit, ftplib.error_temp, ftplib.error_perm,
                    OSError, TimeoutError):
                # лимит достигнут или сервер режет неполное чтение —
                # используем то, что успели получить
                pass
        except Exception:
            pass
        finally:
            if ftp is not None:
                self._quit(ftp)
        return b"".join(chunks)[:max_bytes]
