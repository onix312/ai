"""Асинхронный-friendly клиент нативного UI к loopback API агента."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class BackendError(Exception):
    message: str
    status: int = 0

    def __str__(self) -> str:
        return self.message


class BackendClient:
    def __init__(self, agent_url: str = "http://127.0.0.1:8799",
                 speech_url: str = "http://127.0.0.1:8791",
                 timeout: float = 2.5) -> None:
        self.agent_url = agent_url.rstrip("/")
        self.speech_url = speech_url.rstrip("/")
        self.timeout = float(timeout)

    def _request(self, base: str, path: str, method: str = "GET",
                 body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = base + path
        data = None
        headers = {"User-Agent": "NOZZA-NativeUI/0.1"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                raw = res.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8") or "{}")
                reason = str(payload.get("reason") or payload.get("error") or exc.reason)
            except Exception:
                reason = str(exc.reason)
            raise BackendError(reason, int(exc.code or 0)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BackendError(f"Агент недоступен: {exc.__class__.__name__}") from exc
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise BackendError("Агент вернул некорректный JSON") from exc
        if isinstance(payload, dict):
            return payload
        raise BackendError("Агент вернул неожиданный ответ")

    def get(self, path: str) -> dict[str, Any]:
        return self._request(self.agent_url, path)

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request(self.agent_url, path, "POST", body or {})

    def speech_get(self, path: str) -> dict[str, Any]:
        return self._request(self.speech_url, path)

    def speech_post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request(self.speech_url, path, "POST", body or {})

    def status(self) -> dict[str, Any]:
        return self.get("/status")

    def chat(self, text: str, session: str = "native") -> dict[str, Any]:
        return self.post("/chat", {"text": text, "session": session, "contract_version": 1})

    def history(self, session: str = "native", limit: int = 40) -> dict[str, Any]:
        q = urllib.parse.urlencode({"session": session, "limit": int(limit)})
        return self.get(f"/chat/history?{q}")

    def pending(self) -> dict[str, Any]:
        return self.get("/pending")

    def tasks(self, limit: int = 50) -> dict[str, Any]:
        return self.get(f"/tasks?limit={int(limit)}")

    def task_op(self, op: str, task_id: int = 0, **payload: Any) -> dict[str, Any]:
        return self.post("/tasks", {"op": op, "id": int(task_id), **payload})

    def decide(self, action_id: str, confirmed: bool) -> dict[str, Any]:
        return self.post("/action/confirm", {"id": action_id, "confirmed": bool(confirmed)})

    def notifications(self) -> dict[str, Any]:
        return self.get("/notifications")

    def personal(self) -> dict[str, Any]:
        return self.get("/personal")

    def learning(self) -> dict[str, Any]:
        return self.get("/learning")

    def memory(self, query: str = "") -> dict[str, Any]:
        suffix = "?" + urllib.parse.urlencode({"q": query}) if query else ""
        return self.get("/memory" + suffix)

    def skills(self) -> dict[str, Any]:
        return self.get("/skills")

    def journal(self, limit: int = 50) -> dict[str, Any]:
        return self.get(f"/journal?limit={int(limit)}")

    def capabilities(self) -> dict[str, Any]:
        return self.get("/capabilities")

    def arm_mic(self, seconds: float = 25.0) -> dict[str, Any]:
        return self.speech_post("/mic/arm", {"seconds": float(seconds)})

    def disarm_mic(self) -> dict[str, Any]:
        return self.speech_post("/mic/disarm", {})

    def voice_status(self) -> dict[str, Any]:
        return self.speech_get("/voice/status")

    def enable_voice(self) -> dict[str, Any]:
        return self.speech_post("/voice/enable", {})

    def disable_voice(self) -> dict[str, Any]:
        return self.speech_post("/voice/disable", {})

    def stop_voice(self) -> dict[str, Any]:
        return self.speech_post("/voice/stop", {})
