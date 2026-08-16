"""Работа с файлами принтера по FTPS (implicit TLS, порт 990).

Позволяет читать список моделей на SD-карте, загружать 3MF/G-code с
компьютера и удалять файлы. Используется только в локальной сети.
"""
from __future__ import annotations

import ftplib
import socket
import ssl
from pathlib import Path
from typing import Callable

PRINT_EXT = (".3mf", ".gcode", ".gcode.3mf")


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

    def __init__(self, host: str, access_code: str, port: int = 990, timeout: int = 8):
        self.host = host
        self.access_code = access_code
        self.port = port
        self.timeout = timeout

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
        ftp = self._connect()
        entries: list[dict] = []
        try:
            lines: list[str] = []
            try:
                ftp.retrlines(f"LIST {path}", lines.append)
            except (ftplib.error_perm, socket.timeout):
                lines = []
            for line in lines:
                parsed = self._parse_line(line, path)
                if parsed:
                    entries.append(parsed)
            if not entries:  # некоторые прошивки отвечают только на NLST
                try:
                    for name in ftp.nlst(path):
                        clean = name.rsplit("/", 1)[-1]
                        if clean in (".", ".."):
                            continue
                        entries.append({"name": clean, "path": self._join(path, clean),
                                        "size": 0, "dir": not clean.lower().endswith(PRINT_EXT),
                                        "modified": ""})
                except ftplib.error_perm:
                    pass
        finally:
            self._quit(ftp)
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return entries

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
        sent = 0
        ftp = self._connect()
        try:
            with local.open("rb") as fp:
                def callback(block: bytes):
                    nonlocal sent
                    sent += len(block)
                    if progress:
                        progress(sent)
                ftp.storbinary(f"STOR {remote_name}", fp, blocksize=262144, callback=callback)
        finally:
            self._quit(ftp)
        return {"ok": True, "name": remote_name, "size": sent}

    def upload_bytes(self, data: bytes, remote_name: str) -> dict:
        import io
        ftp = self._connect()
        try:
            ftp.storbinary(f"STOR {remote_name}", io.BytesIO(data), blocksize=262144)
        finally:
            self._quit(ftp)
        return {"ok": True, "name": remote_name, "size": len(data)}

    def delete(self, path: str) -> dict:
        ftp = self._connect()
        try:
            ftp.delete(path)
        finally:
            self._quit(ftp)
        return {"ok": True}
