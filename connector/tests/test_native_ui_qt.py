"""Smoke-test реальных PySide6 окон без интерактивного рабочего стола."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from agent.native_ui.app import NativeApp
from agent.native_ui.control_center import ActivityPage, ChatPage, ControlCenter, TasksPage, TextPage
from agent.native_ui.orb import VoiceOrb
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

        center.set_page_payload("memory", {"memories": [{"text": "пример"}]})
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
        self.assertIsInstance(center.pages["activity"], ActivityPage)
        timeline = center.pages["activity"].browser.toPlainText()
        self.assertIn("открой телеграм", timeline)
        self.assertIn("app.open", timeline)
        self.assertIn("проверка: verified", timeline)
        self.assertIn("окно найдено", timeline)
        self.assertIn("Telegram открыт", timeline)
        activity_page = center.pages["activity"]
        task_index = activity_page.task_filter.findData(4)
        self.assertGreaterEqual(task_index, 0)
        activity_page.task_filter.setCurrentIndex(task_index)
        self.app.processEvents()
        drill = activity_page.browser.toPlainText()
        self.assertIn("evidence:", drill)
        self.assertIn("Telegram", drill)
        self.assertIn("provider result:", drill)
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
        labels = [w.text() for w in task_page.findChildren(QLabel)]
        self.assertTrue(any("Подготовить рабочее место" in text for text in labels))
        self.assertTrue(any("План: Рабочее место" in text for text in labels))
        self.assertTrue(any("target=telegram" in text for text in labels))
        self.assertTrue(any("verified 1" in text and "assumed 1" in text for text in labels))
        self.assertTrue(any("Новый маршрут" in text for text in labels))
        self.assertTrue(any("Здоровье ПК" in text for text in labels))

        center.deleteLater()
        quick.deleteLater()
        orb.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
