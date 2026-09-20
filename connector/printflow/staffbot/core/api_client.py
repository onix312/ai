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

    def call(self, method: str, params: dict, timeout: int = 35) -> dict:
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

    def download_file(self, file_id: str) -> bytes | None:
        if self._transport is not None:
            try:
                # transport.download_file требует call
                return self._transport.download_file(file_id, call=self.call)
            except Exception:
                return None
        return None
