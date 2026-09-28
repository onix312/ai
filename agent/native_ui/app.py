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
        self.center.task_command.connect(self.task_command)
        self.center.plan_preview.connect(self.plan_preview)
        self.center.plan_command.connect(self.plan_command)
        self.center.replan_command.connect(self.replan_command)
        self.center.replan_preview.connect(self.replan_preview)

        self._restore_geometry()
        self._install_hotkey()

        self.poll = QTimer()
        self.poll.timeout.connect(self.refresh_status)
        self.poll.start(900)
        self.caps_poll = QTimer()
        self.caps_poll.timeout.connect(self.refresh_capabilities)
        self.caps_poll.start(15000)
        self.refresh_status()
        self.refresh_capabilities()
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

        def done(status: dict[str, Any]) -> None:
            self._busy_status = False
            self.state.apply_status(status)
            if self._busy_chat:
                self.state.assistant_state = "thinking"
            self._render_status()

        def failed(message: str) -> None:
            self._busy_status = False
            self.state.set_error(message)
            self._render_status()

        self.run_async(self.backend.status, done, failed)

    def refresh_capabilities(self) -> None:
        def done(payload: dict[str, Any]) -> None:
            caps = payload.get("capabilities") or {}
            self.state.model_ok = bool(caps.get("model"))
            self.state.panel_ok = bool(caps.get("panel"))
            self._render_status()

        self.run_async(self.backend.capabilities, done, lambda _message: None)

    def _render_status(self) -> None:
        self.tray.apply_status(
            self.state.connected, self.state.voice_enabled, self.state.model_ok,
            self.state.assistant_state, self.state.last_error,
        )
        self.center.update_status(
            self.state.connected, self.state.voice_enabled, self.state.model_ok,
            self.state.panel_ok, self.state.last_error,
        )
        if not self.state.connected:
            if self.orb.isVisible():
                self.orb.set_state("error", 1800)
            return
        voice_state = self.state.assistant_state
        if voice_state in ("listening", "thinking", "speaking", "error"):
            self.orb.set_state(voice_state, 1800 if voice_state == "error" else 0)
        elif self.orb.isVisible() and not self._busy_chat:
            self.orb.hide()

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
        tasks = self.backend.tasks(50)
        plans = self.backend.plans()
        replans = self.backend.replans()
        return {
            "pending": pending.get("pending") or [],
            "notifications": notes.get("notifications") or [],
            "tasks": tasks.get("tasks") or [],
            "plans": plans.get("plans") or [],
            "replans": replans.get("replans") or [],
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

    def task_command(self, task_id: int, op: str) -> None:
        self.run_async(
            lambda: self.backend.task_op(op, task_id),
            lambda _payload: self.refresh_page("tasks"),
        )

    def plan_preview(self, goal: str) -> None:
        clean = str(goal or "").strip()
        if not clean:
            return

        def done(payload: dict[str, Any]) -> None:
            if payload.get("needs_clarification"):
                message = str(payload.get("ask") or "Нужно уточнение.")
                self.center.set_planner_message(message)
            elif not payload.get("ok"):
                message = str(payload.get("reason") or "План не построен.")
                self.center.set_planner_message(message)
            else:
                self.center.set_planner_message("План готов. Проверьте шаги и нажмите «Запустить план».")
            self.refresh_page("tasks")

        self.run_async(
            lambda: self.backend.plan_op("preview", goal=clean),
            done,
        )

    def plan_command(self, plan_id: str, op: str) -> None:
        def done(payload: dict[str, Any]) -> None:
            if not payload.get("ok") and payload.get("reason"):
                self.center.set_planner_message(str(payload.get("reason")))
            elif op == "approve":
                self.center.set_planner_message("План передан в Task Engine.")
            else:
                self.center.set_planner_message("")
            self.refresh_page("tasks")

        self.run_async(
            lambda: self.backend.plan_op(op, plan_id),
            done,
        )

    def replan_command(self, replan_id: str, op: str) -> None:
        def done(payload: dict[str, Any]) -> None:
            if not payload.get("ok"):
                self.center.set_planner_message(str(payload.get("reason") or "Не удалось изменить план."))
            elif op == "approve":
                self.center.set_planner_message("Новый маршрут принят. Задача на паузе; продолжите её вручную.")
            else:
                self.center.set_planner_message("Старый маршрут сохранён.")
            self.refresh_page("tasks")

        self.run_async(lambda: self.backend.replan_op(op, replan_id), done)

    def replan_preview(self, task_id: int) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("needs_clarification"):
                message = str(payload.get("ask") or "Требуется уточнение.")
            elif payload.get("ok"):
                message = "Новый маршрут готов для проверки."
            else:
                message = str(payload.get("reason") or "Не удалось предложить маршрут.")
            self.center.set_planner_message(message)
            self.refresh_page("tasks")

        self.run_async(lambda: self.backend.replan_op("preview", task_id=task_id), done)

    # --------------------------------------------------------------- mic
    def toggle_mic(self) -> None:
        if self.state.voice_enabled:
            self.run_async(self.backend.disable_voice, self._mic_result)
        else:
            self.orb.set_state("listening")
            self.run_async(self.backend.enable_voice, self._mic_result)

    def _mic_result(self, payload: dict[str, Any]) -> None:
        self.state.voice_enabled = bool(payload.get("enabled"))
        self.state.armed = bool(payload.get("armed"))
        self.state.connected = bool(payload.get("ok", True))
        self.state.assistant_state = str(payload.get("state") or (
            "listening" if self.state.voice_enabled else "idle"))
        self._render_status()

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
