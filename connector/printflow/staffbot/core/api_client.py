"""API-клиент Telegram — чистый слой транспорта.

Использует общий Transport из ..tg если доступен, иначе прямой urllib.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Callable


class TelegramApiClient:
    def __init__(self, token_provider: Callable[[], str], source: str = "staff"):
        self._token_provider = token_provider
        self._source = source
        # Попытка использовать общий транспорт (идея 73)
        try:
            from ...tg import Transport

            self._transport = Transport(token_provider, source)
        except Exception:
            self._transport = None

    def call(self, method: str, params: dict, timeout: int = 35,
             files: dict[str, tuple[str, bytes]] | None = None) -> dict:
        """Вызвать метод Bot API.

        `files` — отправка с вложением (18.12.3: sendPhoto из текстовых
        отчётов бота). В этом случае идём своим multipart-ом: `Transport`
        умеет только urlencode, а подставлять картинку в текст нельзя.
        """
        if files:
            return self._multipart(method, params, files, timeout)
        if self._transport is not None:
            try:
                return self._transport.call(method, params, timeout=timeout)
            except Exception:
                # fallback к прямому вызову
                pass
        token = str(self._token_provider() or "").strip()
        if not token:
            return {"ok": False, "error": "no token"}
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}", data=data
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                try:
                    return json.loads(raw)
                except Exception:
                    return {"ok": resp.status == 200, "raw": raw}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _multipart(self, method: str, params: dict,
                   files: dict[str, tuple[str, bytes]], timeout: int) -> dict:
        """multipart/form-data на stdlib: файл + текстовые поля."""
        import mimetypes
        import uuid

        token = str(self._token_provider() or "").strip()
        if not token:
            return {"ok": False, "error": "no token"}
        boundary = uuid.uuid4().hex
        parts: list[bytes] = []
        for key, value in params.items():
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            parts.append(f"{value}\r\n".encode())
        for field, (filename, raw) in files.items():
            ctype = mimetypes.guess_type(str(filename))[0] or "application/octet-stream"
            safe = str(filename or "file").replace('"', "")
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(
                f'Content-Disposition: form-data; name="{field}"; filename="{safe}"\r\n'.encode())
            parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
            parts.append(bytes(raw))
            parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=max(20, int(timeout))) as resp:
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def download_file(self, file_id: str) -> bytes | None:
        if self._transport is not None:
            try:
                # transport.download_file требует call
                return self._transport.download_file(file_id, call=self.call)
            except Exception:
                return None
        return None
