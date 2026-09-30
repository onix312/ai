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

    def fetch_local_image(self, url: str, limit: int = 8 * 1024 * 1024) -> bytes:
        """Fetch a loopback JPEG referenced by a chat response."""
        target = str(url or "").strip()
        try:
            parsed = urllib.parse.urlsplit(target)
        except ValueError as exc:
            raise BackendError("Некорректный адрес изображения") from exc
        host = str(parsed.hostname or "").casefold()
        if parsed.scheme not in ("http", "https") or host not in ("127.0.0.1", "localhost", "::1"):
            raise BackendError("Изображение должно быть локальным")
        if parsed.path not in ("/api/printer/camera.jpg", "/api/printer/shot.jpg"):
            raise BackendError("Неподдерживаемый локальный источник изображения")
        req = urllib.request.Request(
            target,
            headers={"User-Agent": "Luma-NativeUI/1", "Accept": "image/jpeg"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=min(self.timeout, 8.0)) as res:
                content_type = str(res.headers.get("Content-Type") or "").casefold()
                raw = res.read(int(limit) + 1)
        except urllib.error.HTTPError as exc:
            raise BackendError(f"Камера ответила {int(exc.code or 0)}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BackendError(f"Кадр камеры недоступен: {exc.__class__.__name__}") from exc
        if len(raw) > int(limit):
            raise BackendError("Кадр камеры слишком большой")
        if not raw:
            raise BackendError("Камера вернула пустой кадр")
        if "image/jpeg" not in content_type and not raw.startswith(b"\xff\xd8\xff"):
            raise BackendError("Камера вернула не JPEG")
        return raw

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

    def plans(self) -> dict[str, Any]:
        return self.get("/plans")

    def plan_op(self, op: str, plan_id: str = "", **payload: Any) -> dict[str, Any]:
        return self.post("/plans", {"op": op, "id": str(plan_id), **payload})

    def replans(self) -> dict[str, Any]:
        return self.get("/replans")

    def replan_op(self, op: str, replan_id: str = "", **payload: Any) -> dict[str, Any]:
        return self.post("/replans", {"op": op, "id": str(replan_id), **payload})

    def decide(self, action_id: str, confirmed: bool) -> dict[str, Any]:
        return self.post("/action/confirm", {"id": action_id, "confirmed": bool(confirmed)})

    def notifications(self) -> dict[str, Any]:
        return self.get("/notifications")

    def personal(self) -> dict[str, Any]:
        return self.get("/personal")

    def personal_op(self, op: str, item_id: int = 0, **payload: Any) -> dict[str, Any]:
        return self.post("/personal", {"op": str(op), "id": int(item_id or 0), **payload})

    def learning(self) -> dict[str, Any]:
        return self.get("/learning")

    def learning_op(self, op: str, **payload: Any) -> dict[str, Any]:
        return self.post("/learning", {"op": str(op), **payload})

    def memory(self, query: str = "") -> dict[str, Any]:
        suffix = "?" + urllib.parse.urlencode({"q": query, "session": "native"})
        return self.get("/memory" + suffix)

    def memory_op(self, op: str, memory_id: int = 0, **payload: Any) -> dict[str, Any]:
        return self.post("/memory", {"op": str(op), "id": int(memory_id or 0), **payload})

    def skills(self) -> dict[str, Any]:
        return self.get("/skills")

    def journal(self, limit: int = 50) -> dict[str, Any]:
        return self.get(f"/journal?limit={int(limit)}")

    def capabilities(self) -> dict[str, Any]:
        return self.get("/capabilities")

    def providers(self) -> dict[str, Any]:
        return self.get("/providers")

    def persona(self) -> dict[str, Any]:
        return self.get("/persona")

    def persona_update(self, profile: dict[str, Any]) -> dict[str, Any]:
        return self.post("/persona", {"op": "update", "profile": profile})

    def persona_reset(self) -> dict[str, Any]:
        return self.post("/persona", {"op": "reset"})

    def autonomy(self) -> dict[str, Any]:
        return self.get("/autonomy")

    def autonomy_update(self, level: str, providers: dict[str, str]) -> dict[str, Any]:
        return self.post("/autonomy", {"op": "update", "level": level, "providers": providers})

    def autonomy_reset(self) -> dict[str, Any]:
        return self.post("/autonomy", {"op": "reset"})

    def proactivity(self) -> dict[str, Any]:
        return self.get("/proactivity")

    def proactivity_update(self, settings: dict[str, Any]) -> dict[str, Any]:
        return self.post("/proactivity", {"op": "update", "settings": settings})

    def proactivity_reset(self) -> dict[str, Any]:
        return self.post("/proactivity", {"op": "reset"})

    def events(self, limit: int = 50, state: str = "") -> dict[str, Any]:
        q = urllib.parse.urlencode({"limit": int(limit), "state": str(state or "")})
        return self.get(f"/events?{q}")

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

    def tune_voice(self, vad_threshold: int, multiplier: float, margin: int, alpha: float) -> dict[str, Any]:
        return self.speech_post("/voice/tune", {
            "vad_threshold": int(vad_threshold),
            "multiplier": float(multiplier),
            "margin": int(margin),
            "alpha": float(alpha),
        })

    def reset_voice_diagnostics(self) -> dict[str, Any]:
        return self.speech_post("/voice/diagnostics/reset", {})

    def tts_settings(self) -> dict[str, Any]:
        return self.speech_get("/voice/tts")

    def tts_update(self, piper: str, model: str, speaker: str) -> dict[str, Any]:
        return self.speech_post("/voice/tts", {
            "op": "update", "piper": piper, "model": model, "speaker": speaker,
        })

    def tts_reset(self) -> dict[str, Any]:
        return self.speech_post("/voice/tts", {"op": "reset"})

    def tts_test(self) -> dict[str, Any]:
        return self.speech_post("/voice/tts", {"op": "test"})

    def set_output_device(self, device_id: str) -> dict[str, Any]:
        return self.speech_post("/voice/tts", {"op": "output_set", "device_id": device_id})

    def pronunciation_items(self) -> dict[str, Any]:
        return self.speech_post("/voice/tts", {"op": "pronunciations"})

    def pronunciation_set(self, source: str, target: str) -> dict[str, Any]:
        return self.speech_post("/voice/tts", {
            "op": "pronunciation_set", "source": source, "target": target,
        })

    def pronunciation_delete(self, source: str) -> dict[str, Any]:
        return self.speech_post("/voice/tts", {
            "op": "pronunciation_delete", "source": source,
        })

    def safety_stop(self) -> dict[str, Any]:
        return self.post("/safety/stop", {})

    def safety_resume(self) -> dict[str, Any]:
        return self.post("/safety/resume", {})

    def safety_status(self) -> dict[str, Any]:
        return self.get("/safety/status")
