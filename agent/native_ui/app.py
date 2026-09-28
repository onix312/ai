"""Точка сборки нативного NOZZA UI."""
from __future__ import annotations

import os
import sys
from typing import Any, Callable

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from .backend import BackendClient
from .control_center import ControlCenter
from .hotkeys import HotkeyFilter, register_default, unregister
from .orb import VoiceOrb
from .quick_panel import QuickPanel
from .state import UiState
from .tray import NozzaTray
from .workers import Worker


class NativeApp:
    SESSION = "native"

    def __init__(self, qt: QApplication | None = None,
                 backend: BackendClient | None = None) -> None:
        self.qt = qt or QApplication.instance() or QApplication(sys.argv)
        self.qt.setApplicationName("NOZZA Assistant")
        self.qt.setOrganizationName("NOZZA")
        self.qt.setQuitOnLastWindowClosed(False)
        self.backend = backend or BackendClient()
        self.state = UiState()
        self.pool = QThreadPool.globalInstance()
        self._busy_status = False
        self._busy_chat = False
        self._hotkey_filter: HotkeyFilter | None = None
        self._hotkey_registered = False

        self.orb = VoiceOrb()
        self.quick = QuickPanel()
        self.center = ControlCenter()
        self.tray = NozzaTray(
            self.open_quick,
            self.open_center,
            self.toggle_mic,
            self.restart_ui,
            self.quit,
        )

        self.quick.submitted.connect(self.send_chat)
        self.center.chat_submitted.connect(self.send_chat)
        self.center.refresh_page.connect(self.refresh_page)
        self.center.clear_chat.connect(self.clear_chat)
        self.center.mic_toggle.connect(self.toggle_mic)
        self.center.task_decision.connect(self.decide_task)

        self._restore_geometry()
        self._install_hotkey()

        self.poll = QTimer()
        self.poll.timeout.connect(self.refresh_status)
        self.poll.start(4000)
        self.refresh_status()
        self.refresh_history()
        self.tray.show()

    # ---------------------------------------------------------------- worker
    def run_async(self, fn: Callable[[], Any], done: Callable[[Any], None],
                  failed: Callable[[str], None] | None = None) -> None:
        worker = Worker(fn)
        worker.signals.done.connect(done)
        worker.signals.failed.connect(failed or self._show_error)
        self.pool.start(worker)

    # --------------------------------------------------------------- status
    def refresh_status(self) -> None:
        if self._busy_status:
            return
        self._busy_status = True

        def load() -> dict[str, Any]:
            status = self.backend.status()
            try:
                caps = self.backend.capabilities()
            except Exception:
                caps = {}
            return {"status": status, "caps": caps}

        def done(payload: dict[str, Any]) -> None:
            self._busy_status = False
            status = payload.get("status") or {}
            cap_payload = payload.get("caps") or {}
            caps = cap_payload.get("capabilities") or status.get("capabilities") or {}
            self.state.apply_status(status)
            self.state.model_ok = bool(caps.get("model"))
            self.state.panel_ok = bool(caps.get("panel"))
            if self.state.armed:
                self.state.assistant_state = "listening"
            elif self.state.assistant_state not in ("thinking", "speaking"):
                self.state.assistant_state = "idle"
            self._render_status()

        def failed(message: str) -> None:
            self._busy_status = False
            self.state.set_error(message)
            self._render_status()

        self.run_async(load, done, failed)

    def _render_status(self) -> None:
        self.tray.apply_status(
            self.state.connected, self.state.armed, self.state.model_ok,
            self.state.assistant_state, self.state.last_error,
        )
        self.center.update_status(
            self.state.connected, self.state.armed, self.state.model_ok,
            self.state.panel_ok, self.state.last_error,
        )
        if self.state.armed and self.state.connected and not self.orb.isVisible():
            self.orb.set_state("listening", 1800)

    # ----------------------------------------------------------------- chat
    def send_chat(self, text: str) -> None:
        clean = str(text or "").strip()
        if not clean or self._busy_chat:
            return
        self._busy_chat = True
        self.state.assistant_state = "thinking"
        self._render_status()
        self.quick.set_busy(True)
        self.quick.show_answer("Думаю…")
        self.orb.set_state("thinking")
        self.center.chat.append_local("Вы", clean)

        def done(payload: dict[str, Any]) -> None:
            self._busy_chat = False
            reply = str(payload.get("reply") or payload.get("reason") or "Готово.")
            self.quick.set_busy(False)
            self.quick.show_answer(reply)
            self.center.chat.append_local("NOZZA", reply)
            pending = payload.get("pending")
            if isinstance(pending, dict) and pending.get("id"):
                self.center.set_page_payload("tasks", {"pending": [pending]})
            self.state.assistant_state = "idle"
            self.orb.set_state("idle", 1400)
            self._render_status()
            QTimer.singleShot(300, self.refresh_history)

        def failed(message: str) -> None:
            self._busy_chat = False
            self.quick.set_busy(False)
            self.quick.show_answer(message)
            self.center.chat.append_local("NOZZA", message)
            self.state.set_error(message)
            self.orb.set_state("error", 2400)
            self._render_status()

        self.run_async(lambda: self.backend.chat(clean, self.SESSION), done, failed)

    def refresh_history(self) -> None:
        self.run_async(
            lambda: self.backend.history(self.SESSION, 60),
            lambda payload: self.center.chat.set_history(list(payload.get("turns") or [])),
            lambda _message: None,
        )

    def clear_chat(self) -> None:
        def done(_payload: dict[str, Any]) -> None:
            self.center.chat.set_history([])
            self.quick.show_answer("Диалог очищен.")

        self.run_async(
            lambda: self.backend.post("/chat/clear", {"session": self.SESSION}),
            done,
        )

    # ---------------------------------------------------------------- pages
    def refresh_page(self, key: str) -> None:
        if key == "settings":
            return
        loaders: dict[str, Callable[[], Any]] = {
            "today": self.backend.personal,
            "tasks": self._load_tasks,
            "memory": self.backend.memory,
            "learning": self.backend.learning,
            "skills": self.backend.skills,
            "journal": lambda: self.backend.journal(80),
        }
        fn = loaders.get(key)
        if fn:
            self.run_async(fn, lambda payload, k=key: self.center.set_page_payload(k, payload))

    def _load_tasks(self) -> dict[str, Any]:
        pending = self.backend.pending()
        notes = self.backend.notifications()
        return {
            "pending": pending.get("pending") or [],
            "notifications": notes.get("notifications") or [],
        }

    def decide_task(self, action_id: str, confirmed: bool) -> None:
        if not action_id:
            return

        def done(payload: dict[str, Any]) -> None:
            message = str(payload.get("reason") or ("Выполнено." if payload.get("done") else "Отменено."))
            self.quick.show_answer(message)
            self.refresh_page("tasks")
            self.refresh_history()

        self.run_async(lambda: self.backend.decide(action_id, confirmed), done)

    # --------------------------------------------------------------- mic
    def toggle_mic(self) -> None:
        if self.state.armed:
            self.run_async(self.backend.disarm_mic, self._mic_result)
        else:
            self.orb.set_state("listening")
            self.run_async(lambda: self.backend.arm_mic(25), self._mic_result)

    def _mic_result(self, payload: dict[str, Any]) -> None:
        self.state.armed = bool(payload.get("armed"))
        self.state.connected = bool(payload.get("ok", True))
        self.state.assistant_state = "listening" if self.state.armed else "idle"
        self._render_status()
        self.orb.set_state(self.state.assistant_state, 1700 if not self.state.armed else 0)

    # --------------------------------------------------------------- windows
    def open_quick(self) -> None:
        self.quick.show_centered()

    def open_center(self) -> None:
        self.center.show()
        self.center.raise_()
        self.center.activateWindow()
        self.refresh_history()

    def _show_error(self, message: str) -> None:
        self.state.set_error(message)
        self._render_status()
        self.orb.set_state("error", 2400)
        self.quick.show_answer(message)

    # --------------------------------------------------------------- hotkey
    def _install_hotkey(self) -> None:
        try:
            self._hotkey_filter = HotkeyFilter(self.open_quick)
            self.qt.installNativeEventFilter(self._hotkey_filter)
            self._hotkey_registered = register_default(0)
        except Exception:
            self._hotkey_registered = False

    # ------------------------------------------------------------- lifecycle
    def _restore_geometry(self) -> None:
        from .settings import settings
        saved = settings().value("control_geometry")
        if saved is not None:
            try:
                self.center.restoreGeometry(saved)
            except Exception:
                pass

    def _save_geometry(self) -> None:
        from .settings import settings
        settings().setValue("control_geometry", self.center.saveGeometry())

    def restart_ui(self) -> None:
        self._save_geometry()
        args = [sys.executable, "-m", "agent.native_ui", *sys.argv[1:]]
        if self._hotkey_registered:
            unregister(0)
        os.execv(sys.executable, args)

    def quit(self) -> None:
        self._save_geometry()
        if self._hotkey_registered:
            unregister(0)
        self.tray.hide()
        self.qt.quit()

    def exec(self) -> int:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.center.show()
        return int(self.qt.exec())


def run() -> int:
    return NativeApp().exec()
