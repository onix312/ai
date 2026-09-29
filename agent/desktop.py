"""Single desktop entry point for packaged Luma on Windows.

Runs the local speech/agent HTTP servers in background threads and keeps the
PySide6 Native UI on the main thread. If an agent is already listening on the
configured loopback port, this process only opens the UI instead of starting a
second backend.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import urllib.error
import urllib.request
from typing import Any

from . import config


def _bundle_root() -> pathlib.Path:
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parents[1]


def _apply_packaged_defaults() -> None:
    root = _bundle_root()
    model = root / "models" / "tts" / "luma.onnx"
    piper_candidates = (
        root / "piper" / "piper.exe",
        root / "piper.exe",
    )
    if model.is_file() and not os.environ.get("LUMA_TTS_MODEL_PATH"):
        os.environ["LUMA_TTS_MODEL_PATH"] = str(model)
    if not os.environ.get("LUMA_TTS_PIPER"):
        for candidate in piper_candidates:
            if candidate.is_file():
                os.environ["LUMA_TTS_PIPER"] = str(candidate)
                break


def _agent_alive(timeout: float = 0.7) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{config.AGENT_PORT}/health",
            timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        return isinstance(payload, dict) and bool(payload.get("ok"))
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        return False


class _OwnedBackend:
    def __init__(self) -> None:
        self.agent: Any = None
        self.speech_server: Any = None
        self.agent_server: Any = None

    def start(self) -> None:
        from . import server

        self.speech_server, self.agent_server = server.serve()
        self.agent = self.agent_server.RequestHandlerClass.agent
        threading.Thread(
            target=self.speech_server.serve_forever,
            daemon=True,
            name="luma-speech-http",
        ).start()
        threading.Thread(
            target=self.agent_server.serve_forever,
            daemon=True,
            name="luma-agent-http",
        ).start()
        if config.VOICE_ALWAYS_ON:
            try:
                self.agent.enable_voice()
            except Exception:
                pass
        try:
            self.agent.start_scheduler()
        except Exception:
            pass

    def stop(self) -> None:
        if self.agent is not None:
            try:
                self.agent.stop_scheduler()
            except Exception:
                pass
            try:
                self.agent.microphone.shutdown()
            except Exception:
                pass
        for server in (self.speech_server, self.agent_server):
            if server is not None:
                try:
                    server.shutdown()
                except Exception:
                    pass
                try:
                    server.server_close()
                except Exception:
                    pass


def main() -> int:
    _apply_packaged_defaults()
    os.environ.setdefault("PRINTFLOW_ASSISTANT_WINDOW", "0")

    owned: _OwnedBackend | None = None
    if not _agent_alive():
        owned = _OwnedBackend()
        try:
            owned.start()
        except OSError as exc:
            # A parallel launch may have won the race between the health check
            # and bind(). Treat a now-live loopback backend as success.
            if not _agent_alive():
                if sys.stderr is not None:
                    print(f"Люма не запустилась: {exc}", file=sys.stderr)
                return 1
            owned = None

    try:
        from .native_ui.app import run
        return int(run())
    finally:
        if owned is not None:
            owned.stop()


if __name__ == "__main__":
    raise SystemExit(main())
