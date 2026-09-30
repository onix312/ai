"""Local, token-paired bridge to the NOZZA Chrome/Edge extension.

Page content is collected only for a requested skill and kept in memory until
that request finishes. The extension never receives a raw Python command.
"""
from __future__ import annotations

import hmac
import os
import pathlib
import secrets
import threading
import time
import uuid


class BrowserBridge:
    def __init__(self, token: str = "") -> None:
        self._token = token
        self._lock = threading.Condition()
        self._clients: dict[str, dict] = {}
        self._pending: dict[str, dict] = {}

    @property
    def token(self) -> str:
        with self._lock:
            if not self._token:
                path = pathlib.Path.home() / ".printflow" / "browser-bridge.key"
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        stream.write(secrets.token_urlsafe(32))
                self._token = path.read_text(encoding="utf-8").strip()
                if not self._token:
                    self._token = secrets.token_urlsafe(32)
                    path.write_text(self._token, encoding="utf-8")
            return self._token

    def paired(self, provided: str) -> bool:
        return bool(provided) and hmac.compare_digest(self.token, str(provided))

    def sessions(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            return [{"id": key, "browser": value["browser"]} for key, value in self._clients.items()
                    if now - value["seen"] < 75]

    def connected(self) -> bool:
        return bool(self.sessions())

    def exchange(self, payload: dict) -> dict:
        """Called only after HTTP token and origin checks."""
        client_id = str(payload.get("client_id") or "")
        if not client_id or len(client_id) > 80:
            return {"ok": False, "reason": "Не указан браузер"}
        browser = str(payload.get("browser") or "chrome").casefold()
        if browser not in ("chrome", "edge", "yandex"):
            browser = "chrome"
        with self._lock:
            self._clients[client_id] = {"browser": browser, "seen": time.monotonic()}
            if payload.get("op") == "result":
                key = str(payload.get("id") or "")
                command = self._pending.get(key)
                if (command and command["client_id"] == client_id and command.get("sent")
                        and command["result"] is None):
                    command["result"] = payload.get("result") if isinstance(payload.get("result"), dict) else {}
                    self._lock.notify_all()
                return {"ok": True}
            if payload.get("op") != "poll":
                return {"ok": False, "reason": "Неизвестная операция моста"}
            for key, command in self._pending.items():
                if command["client_id"] == client_id and not command["sent"]:
                    command["sent"] = True
                    return {"ok": True, "command": {"id": key, "kind": command["kind"],
                                                     "args": command["args"]}}
            return {"ok": True, "command": None}

    def request(self, kind: str, args: dict | None = None, timeout: float = 35) -> dict:
        if kind not in ("tabs", "observe"):
            return {"ok": False, "reason": "Команда браузера недоступна"}
        sessions = self.sessions()
        if not sessions:
            return {"ok": False, "reason": "Расширение браузера не подключено"}
        args = dict(args or {})
        requested = str(args.pop("browser", "") or "").casefold()
        matches = [session for session in sessions if session["browser"] == requested] if requested else sessions
        if not matches:
            return {"ok": False, "reason": f"Браузер «{requested}» не подключён"}
        if len(matches) > 1 and not requested:
            return {"ok": False, "reason": "Подключено несколько браузеров: укажите chrome, edge или yandex",
                    "browsers": sessions}
        client_id = matches[-1]["id"]
        key = uuid.uuid4().hex
        deadline = time.monotonic() + timeout
        with self._lock:
            self._pending[key] = {"kind": kind, "args": args, "client_id": client_id,
                                  "sent": False, "result": None}
            self._lock.notify_all()
            try:
                while self._pending[key]["result"] is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return {"ok": False, "reason": "Расширение браузера не ответило вовремя"}
                    self._lock.wait(remaining)
                return self._pending[key]["result"]
            finally:
                self._pending.pop(key, None)


bridge = BrowserBridge()
