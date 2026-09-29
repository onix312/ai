"""Точка сборки нативного UI Люмы."""
from __future__ import annotations

import os
import sys
from typing import Any, Callable

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from .backend import BackendClient
from .control_center import ControlCenter
from .hotkeys import HotkeyFilter, QUICK_HOTKEY_ID, STOP_HOTKEY_ID, register_default, register_stop, unregister
from .orb import VoiceOrb
from .quick_panel import QuickPanel
from .state import UiState
from .tray import LumaTray
from .workers import Worker


class NativeApp:
    SESSION = "native"

    def __init__(self, qt: QApplication | None = None,
                 backend: BackendClient | None = None) -> None:
        self.qt = qt or QApplication.instance() or QApplication(sys.argv)
        font_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf")
        if os.path.isfile(font_path) and QFontDatabase.addApplicationFont(font_path) >= 0:
            self.qt.setFont(QFont("Segoe UI", 10))
        self.qt.setApplicationName("Люма")
        self.qt.setOrganizationName("NOZZA")  # legacy QSettings namespace: preserves existing UI preferences
        self.qt.setQuitOnLastWindowClosed(False)
        self.backend = backend or BackendClient()
        self.state = UiState()
        self.pool = QThreadPool.globalInstance()
        self._workers: set[Worker] = set()
        self._busy_status = False
        self._busy_chat = False
        self._hotkey_filter: HotkeyFilter | None = None
        self._stop_hotkey_filter: HotkeyFilter | None = None
        self._hotkey_registered = False
        self._stop_hotkey_registered = False

        self.orb = VoiceOrb()
        self.quick = QuickPanel()
        self.center = ControlCenter()
        self.tray = LumaTray(
            self.open_quick,
            self.open_center,
            self.toggle_mic,
            self.toggle_stop_all,
            self.restart_ui,
            self.quit,
        )

        self.quick.submitted.connect(self.send_chat)
        self.center.chat_submitted.connect(self.send_chat)
        self.center.refresh_page.connect(self.refresh_page)
        self.center.clear_chat.connect(self.clear_chat)
        self.center.mic_toggle.connect(self.toggle_mic)
        self.center.safety_toggle.connect(self.toggle_stop_all)
        self.center.task_decision.connect(self.decide_task)
        self.center.persona_save.connect(self.save_persona)
        self.center.persona_reset.connect(self.reset_persona)
        self.center.autonomy_save.connect(self.save_autonomy)
        self.center.autonomy_reset.connect(self.reset_autonomy)
        self.center.proactivity_save.connect(self.save_proactivity)
        self.center.proactivity_reset.connect(self.reset_proactivity)
        self.center.voice_tune.connect(self.tune_voice)
        self.center.voice_diag_reset.connect(self.reset_voice_diagnostics)
        self.center.tts_save.connect(self.save_tts)
        self.center.tts_reset.connect(self.reset_tts)
        self.center.tts_test.connect(self.test_tts)
        self.center.pronunciation_add.connect(self.add_pronunciation)
        self.center.pronunciation_delete.connect(self.delete_pronunciation)
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
        worker.setAutoDelete(False)
        self._workers.add(worker)
        worker.signals.done.connect(done)
        worker.signals.failed.connect(failed or self._show_error)
        worker.signals.finished.connect(self._workers.discard)
        self.pool.start(worker)

    # --------------------------------------------------------------- status
    def refresh_status(self) -> None:
        if self._busy_status:
            return
        self._busy_status = True

        def done(status: dict[str, Any]) -> None:
            self._busy_status = False
            self.state.apply_status(status)
            if self._busy_chat and not self.state.activity_active:
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
        self.run_async(
            self.backend.providers,
            self.center.set_providers_payload,
            lambda _message: None,
        )
        self.run_async(
            self.backend.persona,
            self.center.set_persona_payload,
            lambda _message: None,
        )
        self.run_async(
            self.backend.autonomy,
            self.center.set_autonomy_payload,
            lambda _message: None,
        )
        self.run_async(
            self.backend.proactivity,
            self.center.set_proactivity_payload,
            lambda _message: None,
        )
        self.run_async(
            self.backend.tts_settings,
            self.center.set_tts_payload,
            lambda _message: None,
        )
        self.run_async(
            self.backend.pronunciation_items,
            self.center.set_pronunciation_payload,
            lambda _message: None,
        )

    def _render_status(self) -> None:
        self.tray.apply_status(
            self.state.connected, self.state.voice_enabled, self.state.model_ok,
            self.state.assistant_state, self.state.last_error,
            self.state.safety_stopped,
        )
        self.center.update_status(
            self.state.connected, self.state.voice_enabled, self.state.model_ok,
            self.state.panel_ok, self.state.last_error,
            self.state.safety_stopped,
        )
        if not self.state.connected:
            if self.orb.isVisible():
                self.orb.set_state("error", 1800)
            return
        self.center.set_tts_payload({
            "engine": self.state.tts_engine,
            "hq_local": self.state.tts_hq_local,
            "model": self.state.tts_model,
            "model_path": self.state.tts_model_path,
            "piper_path": self.state.tts_piper_path,
            "speaker": self.state.tts_speaker,
            "model_ready": self.state.tts_model_ready,
            "last_synth_ms": self.state.tts_last_synth_ms,
            "last_chars": self.state.tts_last_chars,
        })
        self.center.set_voice_diagnostics(
            audio_level=self.state.audio_level,
            echo_floor=self.state.echo_floor,
            echo_threshold=self.state.echo_threshold,
            echo_suppressed=self.state.echo_suppressed,
            multiplier=self.state.echo_gate_multiplier,
            margin=self.state.echo_gate_margin,
            alpha=self.state.echo_floor_alpha,
            vad_threshold=self.state.vad_threshold,
            asr_engine=self.state.asr_engine,
            vocabulary_count=self.state.vocabulary_count,
        )
        if self.state.voice_error:
            self.center.set_voice_diagnostics_message(self.state.voice_error)
        self.center.chat.set_live_activity(
            self.state.activity_phase,
            self.state.activity_heard,
            self.state.activity_reply,
            self.state.activity_skill,
            self.state.activity_detail,
            self.state.activity_task_id,
        )
        if self.state.safety_stopped:
            self.orb.set_state("stopped")
            self.orb.set_activity(0, "")
            self.orb.set_live(detail="STOP ALL активен", recent=self.state.activity_recent)
            return
        activity_state = {
            "thinking": "thinking",
            "speaking": "speaking",
            "executing": "working",
            "task": "working",
            "waiting": "waiting",
            "error": "error",
        }.get(self.state.activity_phase, "")
        voice_state = self.state.assistant_state
        display_state = activity_state if self.state.activity_active and activity_state else voice_state
        if display_state in ("listening", "thinking", "speaking", "working", "waiting", "error"):
            self.orb.set_state(display_state, 1800 if display_state == "error" else 0)
            self.orb.set_activity(self.state.audio_level, self.state.voice_partial)
            self.orb.set_live(
                heard=self.state.activity_heard,
                reply=self.state.activity_reply,
                skill=self.state.activity_skill,
                detail=self.state.activity_detail,
                task_id=self.state.activity_task_id,
                recent=self.state.activity_recent,
            )
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
            self.center.chat.append_local("Люма", reply)
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
            self.center.chat.append_local("Люма", message)
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
            "activity": self._load_activity,
            "memory": self.backend.memory,
            "learning": self.backend.learning,
            "skills": self.backend.skills,
            "journal": lambda: self.backend.journal(80),
        }
        fn = loaders.get(key)
        if fn:
            self.run_async(fn, lambda payload, k=key: self.center.set_page_payload(k, payload))

    def _load_activity(self) -> dict[str, Any]:
        status = self.backend.status()
        tasks = self.backend.tasks(20)
        journal = self.backend.journal(60)
        replans = self.backend.replans()
        activity = status.get("activity") if isinstance(status.get("activity"), dict) else {}
        return {
            "activity": activity,
            "tasks": tasks.get("tasks") or [],
            "journal": journal.get("entries") or [],
            "replans": replans.get("replans") or [],
        }

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

    def save_persona(self, profile: dict[str, Any]) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self.center.set_persona_message("✓ Стиль Люмы сохранён.")
                self.run_async(self.backend.persona, self.center.set_persona_payload,
                               lambda _message: None)
            else:
                self.center.set_persona_message(str(payload.get("reason") or "Профиль не сохранён."))

        self.run_async(lambda: self.backend.persona_update(dict(profile or {})), done)

    def save_autonomy(self, level: str, providers: dict[str, str]) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self.center.set_autonomy_payload(payload)
                self.center.set_autonomy_message("✓ Policy автономности сохранена.")
            else:
                self.center.set_autonomy_message(str(payload.get("reason") or "Policy не сохранена."))
        self.run_async(lambda: self.backend.autonomy_update(level, providers), done)

    def reset_autonomy(self) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self.center.set_autonomy_payload(payload)
                self.center.set_autonomy_message("Автономность возвращена к безопасным значениям по умолчанию.")
            else:
                self.center.set_autonomy_message(str(payload.get("reason") or "Не удалось сбросить policy."))
        self.run_async(self.backend.autonomy_reset, done)

    def save_proactivity(self, settings: dict[str, Any]) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self.center.set_proactivity_payload(payload)
                self.center.set_proactivity_message("✓ Настройки инициативности сохранены.")
            else:
                self.center.set_proactivity_message(str(payload.get("reason") or "Настройки не сохранены."))
        self.run_async(lambda: self.backend.proactivity_update(dict(settings or {})), done)

    def reset_proactivity(self) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self.center.set_proactivity_payload(payload)
                self.center.set_proactivity_message("Инициативность возвращена к значениям по умолчанию.")
            else:
                self.center.set_proactivity_message(str(payload.get("reason") or "Не удалось сбросить настройки."))
        self.run_async(self.backend.proactivity_reset, done)

    def reset_persona(self) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self.run_async(self.backend.persona, self.center.set_persona_payload,
                               lambda _message: None)
                self.center.set_persona_message("Профиль возвращён к значениям по умолчанию.")
            else:
                self.center.set_persona_message(str(payload.get("reason") or "Не удалось сбросить профиль."))

        self.run_async(self.backend.persona_reset, done)

    # --------------------------------------------------------------- mic
    def save_tts(self, piper: str, model: str, speaker: str) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                engine = str(payload.get("engine") or "system")
                model_name = str(payload.get("model") or "")
                label = f"✓ Голос сохранён: {engine}" + (f" · {model_name}" if model_name else "")
                self.center.set_tts_message(label)
                self.refresh_status()
            else:
                self.center.set_tts_message(str(payload.get("reason") or "Не удалось сохранить голос."))

        self.run_async(
            lambda: self.backend.tts_update(str(piper or ""), str(model or ""), str(speaker or "")),
            done,
        )

    def add_pronunciation(self, source: str, target: str) -> None:
        clean_source = str(source or "").strip()
        clean_target = str(target or "").strip()
        if not clean_source or not clean_target:
            self.center.pronunciation_status.setText("Заполните оба поля.")
            return

        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self.center.set_pronunciation_payload(payload)
                self.center.clear_pronunciation_inputs()
                self.center.pronunciation_status.setText(
                    f"✓ {clean_source} → {clean_target}"
                )
            else:
                self.center.pronunciation_status.setText(
                    str(payload.get("reason") or "Не удалось сохранить правило.")
                )

        self.run_async(
            lambda: self.backend.pronunciation_set(clean_source, clean_target),
            done,
        )

    def delete_pronunciation(self, source: str) -> None:
        clean_source = str(source or "").strip()
        if not clean_source:
            self.center.pronunciation_status.setText("Выберите правило для удаления.")
            return

        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self.center.set_pronunciation_payload(payload)
            else:
                self.center.pronunciation_status.setText(
                    str(payload.get("reason") or "Не удалось удалить правило.")
                )

        self.run_async(lambda: self.backend.pronunciation_delete(clean_source), done)

    def test_tts(self) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                synth_ms = int(payload.get("last_synth_ms") or 0)
                engine = str(payload.get("engine") or "tts")
                suffix = f" · synth {synth_ms} ms" if synth_ms else ""
                self.center.set_tts_message(f"🔊 Тест голоса запущен: {engine}{suffix}")
                self.refresh_status()
            else:
                self.center.set_tts_message(str(payload.get("reason") or "Не удалось воспроизвести тест голоса."))

        self.run_async(self.backend.tts_test, done)

    def reset_tts(self) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self.center.set_tts_message(
                    "TTS сброшен к автоопределению. HQ Piper включится автоматически, если модель доступна."
                )
                self.refresh_status()
            else:
                self.center.set_tts_message(str(payload.get("reason") or "Не удалось сбросить TTS."))

        self.run_async(self.backend.tts_reset, done)

    def tune_voice(self, vad_threshold: int, multiplier: float, margin: int, alpha: float) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self.center.set_voice_diagnostics_message(
                    "✓ Калибровка применена в RAM. После перезапуска вернутся defaults."
                )
                self.refresh_status()
            else:
                self.center.set_voice_diagnostics_message(
                    str(payload.get("reason") or "Не удалось применить Voice Diagnostics.")
                )
        self.run_async(
            lambda: self.backend.tune_voice(vad_threshold, multiplier, margin, alpha),
            done,
        )

    def reset_voice_diagnostics(self) -> None:
        def done(payload: dict[str, Any]) -> None:
            if payload.get("ok"):
                self.center.set_voice_diagnostics_message(
                    "Voice Diagnostics сброшены к defaults; echo floor начнёт обучение заново."
                )
                self.refresh_status()
            else:
                self.center.set_voice_diagnostics_message(
                    str(payload.get("reason") or "Не удалось сбросить Voice Diagnostics.")
                )
        self.run_async(self.backend.reset_voice_diagnostics, done)

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

    def toggle_stop_all(self) -> None:
        if self.state.safety_stopped:
            self.run_async(self.backend.safety_resume, self._safety_result)
        else:
            self.run_async(self.backend.safety_stop, self._safety_result)

    def stop_all(self) -> None:
        if not self.state.safety_stopped:
            self.run_async(self.backend.safety_stop, self._safety_result)

    def _safety_result(self, payload: dict[str, Any]) -> None:
        self.state.safety_stopped = bool(payload.get("stopped") or payload.get("latched"))
        if self.state.safety_stopped:
            self.state.voice_enabled = False
            self.state.armed = False
            self.state.assistant_state = "idle"
            self.orb.set_state("idle", 900)
        self._render_status()
        self.refresh_page("tasks")

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
            self._hotkey_filter = HotkeyFilter(self.open_quick, QUICK_HOTKEY_ID)
            self.qt.installNativeEventFilter(self._hotkey_filter)
            self._hotkey_registered = register_default(0, QUICK_HOTKEY_ID)
            self._stop_hotkey_filter = HotkeyFilter(self.stop_all, STOP_HOTKEY_ID)
            self.qt.installNativeEventFilter(self._stop_hotkey_filter)
            self._stop_hotkey_registered = register_stop(0, STOP_HOTKEY_ID)
        except Exception:
            self._hotkey_registered = False
            self._stop_hotkey_registered = False

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
            unregister(0, QUICK_HOTKEY_ID)
        if self._stop_hotkey_registered:
            unregister(0, STOP_HOTKEY_ID)
        os.execv(sys.executable, args)

    def quit(self) -> None:
        self._save_geometry()
        if self._hotkey_registered:
            unregister(0, QUICK_HOTKEY_ID)
        if self._stop_hotkey_registered:
            unregister(0, STOP_HOTKEY_ID)
        self.tray.hide()
        self.qt.quit()

    def exec(self) -> int:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.center.show()
        return int(self.qt.exec())


def run() -> int:
    return NativeApp().exec()
