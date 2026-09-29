"""Smoke-test реальных PySide6 окон без интерактивного рабочего стола."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from agent.native_ui.control_center import ControlCenter, TasksPage
from agent.native_ui.orb import VoiceOrb
from agent.native_ui.quick_panel import QuickPanel


class NativeQtSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_windows_construct_and_accept_state(self):
        orb = VoiceOrb()
        quick = QuickPanel()
        center = ControlCenter()

        orb.set_state("thinking")
        quick.show_answer("готово")
        center.update_status(True, False, True, True)
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
        center.set_page_payload("memory", {"memories": [{"text": "пример"}]})
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
        self.assertIn("готово", quick.answer.text())
        self.assertIn("агент", center.footer.text())
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
