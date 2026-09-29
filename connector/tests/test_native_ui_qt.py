"""Smoke-test реальных PySide6 окон без интерактивного рабочего стола."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QProgressBar, QPushButton

from agent.native_ui.app import NativeApp
from agent.native_ui.components import LumaPortrait
from agent.native_ui.control_center import ActivityPage, ChatPage, ControlCenter, HomePage, JournalPage, LearningPage, MemoryPage, SkillsPage, TasksPage, TextPage, TodayPage, VoicePage
from agent.native_ui.orb import LumaOrbCore, VoiceOrb
from agent.native_ui.quick_panel import QuickPanel


class NativeQtSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_chat_welcome_and_readable_data_view(self):
        chat = ChatPage()
        self.assertEqual(chat.content.currentIndex(), 0)
        chat.set_history([{"role": "user", "text": "<b>привет</b>"}])
        self.assertEqual(chat.content.currentIndex(), 1)
        self.assertIn("<b>привет</b>", chat.feed.toPlainText())
        self.assertIn("ВЫ", chat.feed.toPlainText())
        chat.append_local("Люма", "Готово", source="model", skill="system.health")
        visible = chat.feed.toPlainText()
        self.assertIn("LUMA", visible)
        self.assertIn("Готово", visible)
        self.assertIn("MODEL", visible)
        self.assertIn("system.health", visible)
        chat.append_local("Люма", "Проверила и выполнила.", source="agent-loop", skill="app.open")
        self.assertIn("AGENT LOOP", chat.feed.toPlainText())
        chat.set_history([
            {"role": "assistant", "text": "Открыла.", "meta": {"source": "rules", "skill": "app.open"}},
        ])
        restored = chat.feed.toPlainText()
        self.assertIn("RULES", restored)
        self.assertIn("app.open", restored)
        chat.set_history([])
        self.assertEqual(chat.content.currentIndex(), 0)

        page = TextPage("Память")
        page.set_payload({"ok": True, "memories": [{"text": "Мой факт"}]})
        self.assertIn("Мой факт", page.browser.toPlainText())
        page._toggle_raw()
        self.assertIn('"memories"', page.browser.toPlainText())
        chat.deleteLater()
        page.deleteLater()

    def test_restart_relaunches_backend_too(self):
        fake = SimpleNamespace(_save_geometry=lambda: None,
                               _hotkey_registered=False, _stop_hotkey_registered=False)
        with patch("agent.native_ui.app.os.execv") as execv:
            NativeApp.restart_ui(fake)
        self.assertIn("agent.desktop", execv.call_args.args[1])

    def test_voice_orb_stays_compact_without_activity(self):
        orb = VoiceOrb()
        self.assertEqual(orb.height(), 72)
        orb.set_live(heard="Проверка", reply="Готово")
        self.assertEqual(orb.height(), 112)
        orb.set_live()
        self.assertEqual(orb.height(), 72)
        orb.deleteLater()

    def test_home_hero_and_orb_accept_live_runtime(self):
        center = ControlCenter()
        self.assertIsInstance(center.pages["home"], HomePage)
        self.assertIsInstance(center.pages["voice"], VoicePage)
        self.assertIsInstance(center.home.orb, LumaOrbCore)
        self.assertIsInstance(center.home.portrait, LumaPortrait)
        self.assertFalse(center.home.portrait._portrait.isNull())
        self.assertFalse(center.ambient._background.isNull())
        submitted = []
        center.home.submitted.connect(submitted.append)
        center.home.search_input.setText("Открой загрузки")
        center.home._submit_search()
        self.assertEqual(["Открой загрузки"], submitted)
        self.assertEqual("", center.home.search_input.text())

        center.set_home_runtime(
            connected=True,
            state="thinking",
            audio_level=1700,
            heard="люма открой загрузки",
            reply="",
            skill="app.open",
            detail="Открываю папку",
            task_id=3,
            safety_stopped=False,
        )
        center.set_tts_payload({
            "engine": "silero",
            "hq_local": True,
            "model": "silero_v5_5_ru.pt",
            "model_path": "C:/voices/silero_v5_5_ru.pt",
            "piper_path": "",
            "piper_model_path": "",
            "piper_speaker": "",
            "speaker": "baya",
            "sample_rate": 48000,
            "model_ready": True,
            "last_synth_ms": 120,
            "last_chars": 24,
        })
        self.assertEqual("thinking", center.home.orb.state())
        self.assertIn("app.open", center.home.activity_value.text())
        self.assertIn("baya", center.home.voice_value.text().casefold())
        self.assertIn("48 kHz", center.home.voice_meta.text())
        self.assertIn("baya", center.voice_page.voice_name.text().casefold())
        self.assertIn("48 kHz", center.voice_page.voice_profile.text())
        center.home.set_brain(model_ok=True, ready=84, total=96, route="model", repaired=False)
        self.assertEqual("MODEL", center.home.brain_value.text())
        self.assertIn("84/96", center.home.brain_meta.text())
        center.home.set_brain(model_ok=True, ready=84, total=96, route="model", repaired=True)
        self.assertEqual("SELF-CORRECTED", center.home.brain_value.text())
        self.assertIn("repair loop", center.home.brain_meta.text())
        center.home.set_brain(
            model_ok=True, ready=84, total=96, route="agent-loop",
            repaired=False, agent_iterations=2,
        )
        self.assertEqual("AGENT LOOP", center.home.brain_value.text())
        self.assertIn("2 итерац.", center.home.brain_meta.text())
        center.deleteLater()
        self.app.processEvents()

    def test_chat_can_render_printer_camera_jpeg(self):
        center = ControlCenter()
        image = QImage(32, 24, QImage.Format_RGB32)
        image.fill(QColor("#8B5CF6"))
        payload = QByteArray()
        buffer = QBuffer(payload)
        self.assertTrue(buffer.open(QIODevice.WriteOnly))
        self.assertTrue(image.save(buffer, "JPEG"))
        buffer.close()

        self.assertTrue(center.chat.show_camera_image(bytes(payload), "P1S"))
        self.assertFalse(center.chat.camera_card.isHidden())
        self.assertIn("P1S", center.chat.camera_title.text())
        self.assertIsNotNone(center.chat.camera_frame.pixmap())
        center.deleteLater()
        self.app.processEvents()

    def test_chat_shows_explicit_execution_trace(self):
        center = ControlCenter()
        center.chat.show_action_trace([
            {"kind": "rule", "title": "Понял без модели", "detail": "открыть программу"},
            {"kind": "check", "title": "Проверка реестром", "detail": "навык и параметры в порядке"},
            {"kind": "task", "title": "Task Engine", "detail": "задача 7: 2 шагов"},
        ])
        self.assertFalse(center.chat.trace_card.isHidden())
        trace = center.chat.trace_text.text()
        self.assertIn("Понял без модели", trace)
        self.assertIn("Проверка реестром", trace)
        self.assertIn("Task Engine", trace)
        self.assertNotIn("<think", trace.casefold())
        center.chat.clear_action_trace()
        self.assertTrue(center.chat.trace_card.isHidden())
        center.deleteLater()
        self.app.processEvents()

    def test_memory_workspace_emits_exact_record_actions(self):
        page = MemoryPage()
        page.set_payload({
            "memories": [{
                "id": 77, "text": "Любит PETG", "kind": "preference",
                "subject": "Мария", "source": "chat", "origin": "explicit",
                "confidence": 1.0, "pinned": False, "layer": "user_model",
                "observed_count": 1,
            }]
        })
        pins = []
        forgotten = []
        page.pin_requested.connect(lambda memory_id, value: pins.append((memory_id, value)))
        page.forget_requested.connect(lambda memory_id: forgotten.append(memory_id))

        pin = next(
            button for button in page.findChildren(QPushButton)
            if button.objectName() == "memoryPin"
        )
        forget = next(
            button for button in page.findChildren(QPushButton)
            if button.objectName() == "danger" and button.text() == "Забыть"
        )
        pin.click()
        forget.click()
        self.assertEqual([], forgotten, "первый клик только вооружает удаление")
        self.assertEqual("Подтвердить удаление", forget.text())
        forget.click()
        self.assertEqual([(77, True)], pins)
        self.assertEqual([77], forgotten)
        page.deleteLater()
        self.app.processEvents()

    def test_today_dashboard_metrics_and_actions(self):
        page = TodayPage()
        page.set_payload({
            "reminders": [{
                "id": 10, "text": "Позвонить", "label": "сегодня в 18:00",
                "today": True, "repeat": "", "status": "active",
            }],
            "fired": [{
                "id": 11, "text": "Проверить печать", "label": "сработало 17:30",
                "repeat": "", "status": "fired",
            }],
            "habits": [
                {"id": 7, "title": "Вода", "done_today": True, "streak": 5, "week": 6, "remind_at": "10:00"},
                {"id": 8, "title": "Чтение", "done_today": False, "streak": 2, "week": 4, "remind_at": ""},
            ],
            "goals": [{
                "id": 9, "title": "Прочитать книги", "progress": 3, "target": 12,
                "percent": 25, "pace": "отстаёте на 1 книгу", "behind": True,
                "unit": "книг", "status": "active",
            }],
            "lists": [{
                "name": "покупки", "count": 1,
                "items": [{"id": 12, "item": "PLA белый"}],
            }],
            "expenses": {
                "month": 1234, "month_text": "1 234 ₽", "today": 250,
                "top": [["еда", "800 ₽"], ["транспорт", "434 ₽"]],
            },
            "mood": 7.4,
        })
        self.assertEqual("2", page.reminder_metric.text())
        self.assertEqual("1/2", page.habit_metric.text())
        self.assertEqual("1", page.goal_metric.text())
        self.assertEqual("7.4/10", page.mood_metric.text())
        self.assertIn("1 234 ₽", page.expense_month.text())
        self.assertIn("250 ₽", page.expense_today.text())

        actions = []
        page.action_requested.connect(lambda op, payload: actions.append((op, dict(payload))))
        next(button for button in page.findChildren(QPushButton) if button.text() == "Отметить").click()
        next(button for button in page.findChildren(QPushButton) if button.text() == "+1 к прогрессу").click()
        next(button for button in page.findChildren(QPushButton) if button.text() == "Вычеркнуть").click()
        next(button for button in page.findChildren(QPushButton) if button.text() == "Отложить 10 мин").click()
        self.assertIn(("habit_check", {"id": 8}), actions)
        self.assertIn(("goal_progress", {"id": 9, "amount": 1}), actions)
        self.assertIn(("list_remove", {"id": 12}), actions)
        self.assertTrue(any(op == "reminder_snooze" and payload.get("minutes") == 10
                            for op, payload in actions))
        page.deleteLater()
        self.app.processEvents()

    def test_learning_workspace_teaches_and_confirms_forget(self):
        page = LearningPage()
        page.set_payload({
            "learned": [{
                "id": 17, "phrase": "рабочий режим", "meaning_text": "открыть Telegram",
                "source": "taught", "source_title": "научили", "uses": 3,
                "good": 2, "bad": 0, "active": 1,
            }],
            "unknown": [{
                "id": 31, "text": "вруби рабочку", "norm": "вруби рабочку",
                "count": 3, "last_at": "2026-09-29 21:00:00",
            }],
            "aliases": [{"word": "телега", "meaning": "телеграм", "uses": 5}],
            "insights": [{
                "text": "Около 20:00 вы обычно просите «открой Steam» — 4 дня.",
                "skill": "app.open", "phrase": "открой Steam", "days": 4, "hour": 20,
            }],
            "feedback": {"good": 8, "bad": 2},
        })
        self.assertEqual("1", page.learned_metric.text())
        self.assertEqual("1", page.unknown_metric.text())
        self.assertEqual("1", page.alias_metric.text())
        self.assertEqual("1", page.insight_metric.text())
        self.assertIn("👍 8", page.feedback_status.text())

        actions = []
        page.action_requested.connect(lambda op, payload: actions.append((op, dict(payload))))

        meaning = next(
            field for field in page.findChildren(QLineEdit)
            if "Что эта фраза" in field.placeholderText()
        )
        meaning.setText("открой Steam")
        teach = next(button for button in page.findChildren(QPushButton) if button.text() == "Научить")
        teach.click()
        self.assertIn(("teach", {"phrase": "вруби рабочку", "meaning": "открой Steam"}), actions)

        forget = next(button for button in page.findChildren(QPushButton) if button.text() == "Забыть урок")
        forget.click()
        self.assertFalse(any(op == "forget" for op, _payload in actions))
        self.assertEqual("Подтвердить удаление", forget.text())
        forget.click()
        self.assertIn(("forget", {"id": 17}), actions)

        page.search.setText("телега")
        self.app.processEvents()
        visible = []
        for index in range(page.host.count()):
            card = page.host.itemAt(index).widget()
            if card is not None:
                visible.extend(label.text() for label in card.findChildren(QLabel))
        self.assertTrue(any("телега" in value for value in visible))
        self.assertFalse(any("рабочий режим" in value for value in visible))
        page.deleteLater()
        self.app.processEvents()

    def test_journal_workspace_is_filterable_read_only_audit(self):
        page = JournalPage()
        page.set_payload({
            "entries": [
                {
                    "id": 1, "at": "2026-09-29 20:00:00", "skill": "app.open",
                    "outcome": "ok", "detail": "Steam открыт", "target": "Steam",
                    "params": {"target": "steam"},
                },
                {
                    "id": 2, "at": "2026-09-29 20:01:00", "skill": "window.close",
                    "outcome": "denied", "detail": "нужное окно не найдено", "target": "Блокнот",
                    "params": {"title": "Блокнот"},
                },
            ],
            "count": 2,
            "stats": {"journal": 20},
        })
        self.assertEqual("2", page.total_metric.text())
        self.assertEqual("1", page.ok_metric.text())
        self.assertEqual("1", page.error_metric.text())
        self.assertEqual("2", page.skills_metric.text())
        visible = page.browser.toPlainText()
        self.assertIn("app.open", visible)
        self.assertIn("window.close", visible)
        self.assertIn("Steam открыт", visible)

        page.outcome_filter.setCurrentIndex(page.outcome_filter.findData("denied"))
        self.app.processEvents()
        denied = page.browser.toPlainText()
        self.assertIn("window.close", denied)
        self.assertNotIn("app.open", denied)

        page.outcome_filter.setCurrentIndex(0)
        page.search.setText("Steam")
        self.app.processEvents()
        searched = page.browser.toPlainText()
        self.assertIn("app.open", searched)
        self.assertNotIn("window.close", searched)
        page.deleteLater()
        self.app.processEvents()

    def test_skills_page_is_searchable_capability_map(self):
        center = ControlCenter()
        self.assertIsInstance(center.pages["skills"], SkillsPage)
        center.set_page_payload("skills", {
            "ok": True,
            "skills": [
                {
                    "name": "app.open", "title": "Открыть программу",
                    "description": "Открывает локальную программу.", "available": True,
                    "reason": "", "risk": "soft", "confirm": False,
                    "provider": "core", "params": {"target": "text"}, "learned": True,
                },
                {
                    "name": "screen.describe", "title": "Описать экран",
                    "description": "Видит экран локально.", "available": False,
                    "reason": "vision-модель не установлена", "risk": "read", "confirm": False,
                    "provider": "desktop", "params": {"question": "text"},
                },
            ],
            "ready": 1,
        })
        page = center.pages["skills"]
        self.assertEqual("1", page.ready_metric.text())
        self.assertEqual("2", page.total_metric.text())
        self.assertEqual("1", page.off_metric.text())
        self.assertEqual("1", page.learned_metric.text())
        self.assertGreaterEqual(page.provider_filter.findData("core"), 0)
        self.assertGreaterEqual(page.provider_filter.findData("desktop"), 0)
        visible = page.browser.toPlainText()
        self.assertIn("Открыть программу", visible)
        self.assertIn("Описать экран", visible)
        self.assertIn("vision-модель не установлена", visible)

        page.search.setText("экран")
        self.app.processEvents()
        filtered = page.browser.toPlainText()
        self.assertIn("Описать экран", filtered)
        self.assertNotIn("Открыть программу", filtered)
        page.search.clear()
        page._select_group("app")
        self.assertIn("Открыть программу", page.browser.toPlainText())
        self.assertNotIn("Описать экран", page.browser.toPlainText())
        page._select_group("app")
        self.assertIn("Описать экран", page.browser.toPlainText())

        page.search.clear()
        page.provider_filter.setCurrentIndex(page.provider_filter.findData("core"))
        self.app.processEvents()
        by_provider = page.browser.toPlainText()
        self.assertIn("Открыть программу", by_provider)
        self.assertNotIn("Описать экран", by_provider)

        page.provider_filter.setCurrentIndex(0)
        page.risk_filter.setCurrentIndex(page.risk_filter.findData("read"))
        self.app.processEvents()
        by_risk = page.browser.toPlainText()
        self.assertIn("Описать экран", by_risk)
        self.assertNotIn("Открыть программу", by_risk)
        center.deleteLater()
        self.app.processEvents()

    def test_windows_construct_and_accept_state(self):
        orb = VoiceOrb()
        quick = QuickPanel()
        center = ControlCenter()

        orb.set_state("thinking")
        orb.set_live(
            heard="открой телеграм",
            reply="Открываю.",
            skill="app.open",
            detail="Открыть Telegram",
            task_id=7,
        )
        quick.show_answer("готово")
        center.update_status(True, False, True, True)
        center.set_voice_diagnostics(
            audio_level=900,
            echo_floor=220,
            echo_threshold=610,
            echo_suppressed=12,
            multiplier=1.8,
            margin=220,
            alpha=0.3,
            vad_threshold=320,
            asr_engine="vosk",
            vocabulary_count=42,
        )
        center.chat.set_live_activity(
            "executing", "открой телеграм", "", "app.open", "Открыть Telegram", 7
        )
        center.set_providers_payload({"ok": True, "providers": [
            {"name": "browser", "title": "Браузер", "available": True, "reason": "",
             "skills": [{"name": "browser.page"}]},
            {"name": "desktop", "title": "Рабочий стол", "available": False,
             "reason": "нужен Windows", "skills": [{"name": "desktop.observe"}]},
        ]})
        center.set_autonomy_payload({
            "ok": True,
            "level": "operator",
            "levels": ["observer", "assistant", "operator", "agent", "autopilot"],
            "labels": {
                "observer": "Observer", "assistant": "Assistant",
                "operator": "Operator", "agent": "Agent", "autopilot": "Autopilot",
            },
            "providers": {
                "browser": {"level": "autopilot", "hard_max": "autopilot"},
                "desktop": {"level": "agent", "hard_max": "agent"},
                "printflow": {"level": "assistant", "hard_max": "agent"},
                "personal": {"level": "autopilot", "hard_max": "autopilot"},
                "core": {"level": "agent", "hard_max": "agent"},
            },
            "invariant": "confirmations остаются обязательными",
        })
        center.set_proactivity_payload({
            "ok": True,
            "settings": {
                "mode": "active",
                "max_nonurgent_per_hour": 0,
                "cooldown_minutes": 45,
                "quiet_start": "23:00",
                "quiet_end": "07:00",
            },
            "modes": ["silent", "important", "balanced", "active"],
            "urgencies": ["low", "normal", "important", "urgent"],
            "autopilot": True,
            "invariant": "Event Engine 1.0 создаёт только уведомления.",
            "recent": [
                {"state": "suppressed", "title": "Цель", "reason": "quiet hours"},
                {"state": "delivered", "title": "Задача остановилась", "reason": ""},
            ],
        })
        center.set_persona_payload({
            "ok": True,
            "profile": {
                "address": "informal", "verbosity": "normal", "humor": "off",
                "initiative": "balanced", "relationship": "friendly",
            },
            "options": {
                "address": ["formal", "informal"],
                "verbosity": ["brief", "normal", "detailed"],
                "humor": ["off", "light", "playful"],
                "initiative": ["quiet", "balanced", "active"],
                "relationship": ["professional", "friendly", "warm"],
            },
            "labels": {
                "address": {"formal": "на «вы»", "informal": "на «ты»"},
                "verbosity": {"brief": "кратко", "normal": "обычно", "detailed": "подробно"},
                "humor": {"off": "без юмора", "light": "лёгкий юмор", "playful": "игриво"},
                "initiative": {"quiet": "тихо", "balanced": "умеренно", "active": "активно"},
                "relationship": {"professional": "профессионально", "friendly": "дружелюбно", "warm": "тепло"},
            },
        })
        center.set_pronunciation_payload({
            "ok": True,
            "items": {"Bambu": "бэмбу", "MIKHAIL": "михаил"},
            "custom_terms": 2,
            "builtin_terms": 24,
            "dictionary_path": "C:/Users/test/.printflow/tts-pronunciations.json",
        })
        self.assertEqual(2, center.pronunciation_list.count())
        self.assertIn("Пользовательских правил: 2", center.pronunciation_status.text())

        center.set_page_payload("today", {
            "reminders": [{"id": 10, "text": "Позвонить", "label": "сегодня в 18:00",
                           "today": True, "repeat": "", "status": "active"}],
            "fired": [],
            "habits": [{"id": 7, "title": "Вода", "done_today": False,
                        "streak": 5, "week": 6, "remind_at": "10:00"}],
            "goals": [{"id": 9, "title": "Прочитать книги", "progress": 3,
                       "target": 12, "percent": 25, "pace": "идёте в графике",
                       "behind": False, "unit": "книг", "status": "active"}],
            "lists": [{"name": "покупки", "count": 1,
                       "items": [{"id": 12, "item": "PLA белый"}]}],
            "expenses": {"month": 1234, "month_text": "1 234 ₽", "today": 250,
                         "top": [["еда", "800 ₽"]]},
            "mood": 7.4,
        })
        center.set_page_payload("memory", {
            "memories": [
                {
                    "id": 11, "text": "Мария любит PETG", "kind": "preference",
                    "subject": "Мария", "source": "chat", "origin": "explicit",
                    "confidence": 1.0, "pinned": True, "layer": "user_model",
                    "observed_count": 1, "uses": 3, "provenance": "chat",
                },
                {
                    "id": 12, "text": "Обычно печать начинается вечером", "kind": "fact",
                    "subject": "печать", "source": "observed", "origin": "observed",
                    "confidence": 0.8, "pinned": False, "layer": "semantic",
                    "observed_count": 4, "uses": 0, "provenance": "наблюдения",
                },
            ],
            "count": 2,
            "layers": {"working": [], "episodic": [], "semantic": [], "user_model": []},
        })
        center.set_page_payload("learning", {
            "learned": [{
                "id": 17, "phrase": "рабочий режим", "meaning_text": "открыть Telegram",
                "source": "taught", "source_title": "научили", "uses": 3,
                "good": 2, "bad": 0, "active": 1,
            }],
            "unknown": [{"id": 31, "text": "вруби рабочку", "count": 2,
                         "last_at": "2026-09-29 21:00:00"}],
            "aliases": [{"word": "телега", "meaning": "телеграм", "uses": 5}],
            "insights": [{"text": "Обычно вечером вы открываете Steam.",
                          "skill": "app.open", "phrase": "открой Steam",
                          "days": 4, "hour": 20}],
            "feedback": {"good": 8, "bad": 2},
        })
        center.set_page_payload("journal", {
            "entries": [{
                "id": 1, "at": "2026-09-29 20:00:00", "skill": "app.open",
                "outcome": "ok", "detail": "Steam открыт", "target": "Steam",
                "params": {"target": "steam"},
            }],
            "count": 1,
            "stats": {"journal": 20},
        })
        center.set_page_payload("activity", {
            "activity": {
                "current": {
                    "phase": "executing", "heard": "открой телеграм",
                    "reply": "", "skill": "app.open", "task_id": 4,
                    "detail": "Открыть Telegram", "active": True,
                },
                "recent": [{"phase": "done", "reply": "Готово.", "active": False}],
            },
            "tasks": [{
                "id": 4, "title": "Подготовить рабочее место", "status": "paused",
                "progress": 1, "total_steps": 2,
                "steps": [
                    {"seq": 0, "skill": "app.open", "status": "done",
                     "finished_at": "2026-09-29 05:00:00",
                     "result": {"ok": True, "title": "Telegram",
                                "_verification": {"status": "verified"}},
                     "verification": {"status": "verified", "reason": "окно найдено",
                                      "evidence": {"active": "Telegram"}}},
                    {"seq": 1, "skill": "window.focus", "status": "pending",
                     "verification": {}},
                ],
                "replans": [{
                    "id": 1, "created_at": "2026-09-29 05:01:00",
                    "replace_from": 1, "reason": "Проверка окна не прошла",
                    "old_tail": [{"skill": "window.focus"}],
                    "new_tail": [{"skill": "system.health"}, {"skill": "window.focus"}],
                }],
            }],
            "journal": [{
                "at": "2026-09-29 05:00:01", "skill": "app.open",
                "outcome": "ok", "detail": "Telegram открыт", "target": "Telegram",
            }],
            "replans": [{
                "id": "draft-1", "task_id": 4, "replace_from": 1,
                "reason": "Нужна дополнительная проверка",
                "summary": "Проверить здоровье системы",
                "steps": [{"skill": "system.health"}],
            }],
        })

        center.set_page_payload("tasks", {
            "plans": [{
                "id": "p1", "title": "Рабочее место", "summary": "Открыть нужные программы",
                "steps": [{
                    "seq": 0, "skill": "app.open", "title": "Открыть программу",
                    "params": {"target": "telegram"},
                    "risk": "soft", "confirm": False, "why": "связь",
                }],
            }],
            "tasks": [{
                "id": 4, "title": "Подготовить рабочее место", "status": "paused",
                "progress": 1, "total_steps": 3, "error": "Ждёт продолжения",
                "steps": [
                    {"seq": 0, "skill": "app.open",
                     "verification": {"status": "verified", "reason": "окно найдено"}},
                    {"seq": 1, "skill": "system.media",
                     "verification": {"status": "assumed", "reason": "нет независимой проверки"}},
                    {"seq": 2, "skill": "window.focus", "verification": {}},
                ],
            }],
            "pending": [{"id": "a1", "text": "Подтвердить действие"}],
            "replans": [{
                "id": "r1", "task_id": 4, "reason": "Проверка не прошла",
                "summary": "Проверить состояние", "old_tail": [{
                    "seq": 2, "skill": "window.focus", "status": "failed",
                    "verification": {"status": "failed"}}],
                "steps": [{"seq": 2, "skill": "system.health", "title": "Здоровье ПК",
                           "risk": "read", "confirm": False}],
            }],
            "notifications": [],
        })

        self.assertEqual(orb.label.text(), "Люма · Думаю")
        self.assertIn("открой телеграм", orb.context_label.text())
        self.assertIn("Открываю", orb.reply_label.text())
        self.assertIn("app.open", center.chat.live.text())
        self.assertIn("задача #7", center.chat.live.text())
        self.assertIn("готово", quick.answer.text())
        self.assertEqual(320, center.voice_vad.value())
        self.assertAlmostEqual(1.8, center.voice_multiplier.value(), places=2)
        self.assertEqual(220, center.voice_margin.value())
        self.assertAlmostEqual(0.3, center.voice_alpha.value(), places=2)
        self.assertIn("ASR: vosk", center.voice_diag_meta.text())
        self.assertIn("echo suppressed: 12", center.voice_diag_meta.text())
        self.assertIn("Люма", center.footer.text())
        self.assertEqual(900, center.voice_level.value())
        self.assertEqual(220, center.voice_floor.value())
        self.assertEqual(610, center.voice_threshold.value())
        self.assertAlmostEqual(1.8, center.voice_multiplier.value())
        self.assertEqual(220, center.voice_margin.value())
        self.assertAlmostEqual(0.3, center.voice_alpha.value())
        self.assertIn("vosk", center.voice_diag_meta.text())
        self.assertIn("42", center.voice_diag_meta.text())
        self.assertIn("Браузер", center.providers_view.toPlainText())
        self.assertIn("browser.page", center.providers_view.toPlainText())
        self.assertIn("нужен Windows", center.providers_view.toPlainText())
        self.assertEqual("informal", center.persona_boxes["address"].currentData())
        self.assertEqual("normal", center.persona_boxes["verbosity"].currentData())
        self.assertEqual("off", center.persona_boxes["humor"].currentData())
        self.assertIn("не на права", center.persona_status.text())
        self.assertEqual("operator", center.autonomy_level.currentData())
        self.assertEqual("assistant", center.autonomy_provider_boxes["printflow"].currentData())
        self.assertEqual(4, center.autonomy_provider_boxes["desktop"].count())
        self.assertIn("обязательными", center.autonomy_status.text())
        self.assertEqual("active", center.proactivity_mode.currentData())
        self.assertEqual(0, center.proactivity_budget.currentData())
        self.assertEqual(45, center.proactivity_cooldown.currentData())
        self.assertEqual("23:00", center.proactivity_quiet_start.text())
        self.assertEqual("07:00", center.proactivity_quiet_end.text())
        self.assertIn("Autopilot включён", center.proactivity_status.text())
        self.assertIn("[suppressed] Цель", center.events_view.toPlainText())
        self.assertIn("quiet hours", center.events_view.toPlainText())
        self.assertIsInstance(center.pages["today"], TodayPage)
        self.assertEqual("1", center.pages["today"].reminder_metric.text())
        self.assertEqual("0/1", center.pages["today"].habit_metric.text())
        self.assertIn("Позвонить", " ".join(
            label.text() for label in center.pages["today"].findChildren(QLabel)
        ))
        self.assertIsInstance(center.pages["memory"], MemoryPage)
        memory_page = center.pages["memory"]
        self.assertEqual("2", memory_page.total_metric.text())
        self.assertEqual("1", memory_page.pinned_metric.text())
        self.assertEqual("1", memory_page.model_metric.text())
        self.assertEqual("1", memory_page.semantic_metric.text())
        memory_labels = [w.text() for w in memory_page.findChildren(QLabel)]
        self.assertTrue(any("Мария любит PETG" in value for value in memory_labels))
        self.assertTrue(any("Обычно печать начинается вечером" in value for value in memory_labels))
        memory_page.search.setText("Мария")
        self.app.processEvents()
        filtered_memory = []
        for index in range(memory_page.host.count()):
            card = memory_page.host.itemAt(index).widget()
            if card is not None:
                filtered_memory.extend(label.text() for label in card.findChildren(QLabel))
        self.assertTrue(any("Мария любит PETG" in value for value in filtered_memory))
        self.assertFalse(any("Обычно печать начинается вечером" in value for value in filtered_memory))
        self.assertIsInstance(center.pages["learning"], LearningPage)
        self.assertEqual("1", center.pages["learning"].learned_metric.text())
        self.assertEqual("1", center.pages["learning"].unknown_metric.text())
        self.assertIsInstance(center.pages["journal"], JournalPage)
        self.assertIn("Steam открыт", center.pages["journal"].browser.toPlainText())
        self.assertIsInstance(center.pages["activity"], ActivityPage)
        timeline = center.pages["activity"].browser.toPlainText()
        self.assertIn("открой телеграм", timeline)
        self.assertIn("app.open", timeline)
        self.assertIn("проверка: verified", timeline)
        self.assertIn("окно найдено", timeline)
        self.assertIn("Telegram открыт", timeline)
        activity_page = center.pages["activity"]
        self.assertEqual("EXECUTING", activity_page.activity_metric.text())
        self.assertEqual("0", activity_page.running_metric.text())
        self.assertEqual("1", activity_page.verified_metric.text())
        self.assertEqual("0", activity_page.failed_metric.text())
        task_index = activity_page.task_filter.findData(4)
        self.assertGreaterEqual(task_index, 0)
        activity_page.task_filter.setCurrentIndex(task_index)
        self.app.processEvents()
        drill = activity_page.browser.toPlainText()
        self.assertIn("evidence:", drill)
        self.assertIn("Telegram", drill)
        self.assertIn("provider:", drill)
        self.assertIn("Проверка окна не прошла", drill)
        self.assertIn("было: window.focus", drill)
        self.assertIn("стало: system.health → window.focus", drill)
        self.assertIn("Проверить здоровье системы", drill)
        activity_page.search_filter.setText("app.open")
        self.app.processEvents()
        filtered = activity_page.browser.toPlainText()
        self.assertIn("app.open", filtered)
        self.assertNotIn("window.focus · pending", filtered)
        self.assertIsInstance(center.pages["tasks"], TasksPage)
        task_page = center.pages["tasks"]
        self.assertEqual("0", task_page.tasks_running_metric.text())
        self.assertEqual("1", task_page.tasks_waiting_metric.text())
        self.assertEqual("1", task_page.tasks_verified_metric.text())
        self.assertEqual("1", task_page.tasks_pending_metric.text())
        labels = [w.text() for w in task_page.findChildren(QLabel)]
        self.assertTrue(any("Подготовить рабочее место" in text for text in labels))
        self.assertTrue(any(text == "Рабочее место" for text in labels))
        self.assertTrue(any("target=telegram" in text for text in labels))
        self.assertTrue(any("verified 1" in text and "assumed 1" in text for text in labels))
        self.assertTrue(any("Новый маршрут" in text for text in labels))
        self.assertTrue(any("Здоровье ПК" in text for text in labels))
        progress_bars = [w for w in task_page.findChildren(QProgressBar)
                         if w.objectName() == "taskProgress"]
        self.assertTrue(progress_bars)
        self.assertEqual((1, 3), (progress_bars[0].value(), progress_bars[0].maximum()))
        current_steps = [w.text() for w in task_page.findChildren(QLabel)
                         if w.objectName() == "taskStepCurrent"]
        self.assertTrue(any("system.media" in text for text in current_steps))
        done_steps = [w.text() for w in task_page.findChildren(QLabel)
                      if w.objectName() == "taskStepDone"]
        self.assertTrue(any("app.open" in text for text in done_steps))
        self.assertTrue(any("Сейчас выполняю" in w.text()
                            for w in task_page.findChildren(QLabel)
                            if w.objectName() == "taskNow"))

        center.deleteLater()
        quick.deleteLater()
        orb.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
