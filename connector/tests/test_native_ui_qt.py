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
            "notifications": [],
        })

        self.assertEqual(orb.label.text(), "Думаю")
        self.assertIn("готово", quick.answer.text())
        self.assertIn("агент", center.footer.text())
        self.assertIsInstance(center.pages["tasks"], TasksPage)
        task_page = center.pages["tasks"]
        labels = [w.text() for w in task_page.findChildren(QLabel)]
        self.assertTrue(any("Подготовить рабочее место" in text for text in labels))
        self.assertTrue(any("План: Рабочее место" in text for text in labels))
        self.assertTrue(any("target=telegram" in text for text in labels))
        self.assertTrue(any("verified 1" in text and "assumed 1" in text for text in labels))

        center.deleteLater()
        quick.deleteLater()
        orb.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
