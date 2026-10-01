"""Final PrintFlow 19 surface polish contracts."""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site"


class FinalSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (SITE / "index.html").read_text(encoding="utf-8")
        cls.queue = (SITE / "assets" / "queue.js").read_text(encoding="utf-8")
        cls.ops = (SITE / "assets" / "ops.js").read_text(encoding="utf-8")
        cls.app = (SITE / "assets" / "app.js").read_text(encoding="utf-8")
        cls.css = (SITE / "assets" / "v19-shell.css").read_text(encoding="utf-8")

    def test_queue_has_operator_pulse_reusing_existing_filter(self):
        view = self.index.split('id="view-queue"', 1)[1].split(
            '<!-- ================================================== ЗАКАЗЫ', 1
        )[0]
        for key in ("all", "active", "queued", "unassigned"):
            self.assertIn(f'data-queue-pulse="{key}"', view)
            self.assertIn(f'id="queue_pulse_{key}"', view)
        self.assertIn("button.dataset.queuePulse || 'all'", self.queue)
        self.assertIn("queueFilter = button.dataset.queuePulse || 'all'", self.queue)
        self.assertIn(".v19-queue-pulse", self.css)
        self.assertIn("#view-queue #queue_filter { display: none; }", self.css)

    def test_settings_use_quicknav_as_single_visible_navigation(self):
        view = self.index.split('id="view-settings"', 1)[1]
        for pane in ("printers", "production", "storage", "pricing", "business", "cashier", "system", "all"):
            self.assertIn(f'data-set-shortcut="{pane}"', view)
        self.assertIn("#view-settings .settings-tabs { display: none; }", self.css)
        self.assertIn("const pane = $('setpane-' + settingsPane);", self.app)
        self.assertIn("selectSettingsPane(btn.dataset.setShortcut)", self.app)

    def test_primary_crm_language_is_printflow_neutral(self):
        view = self.index.split('id="view-customers"', 1)[1].split(
            '<!-- ================================================== ФИНАНСЫ', 1
        )[0]
        self.assertIn("<th>Кабинет</th>", view)
        self.assertNotIn("<th>NOZZA 8.5</th>", view)
        self.assertIn("🔑 Кабинет</button>", self.ops)
        self.assertNotIn("title=\"Страница «Мой NOZZA»", self.ops)

    def test_demo_feature_uses_printflow_name_in_settings(self):
        settings = self.index.split('id="view-settings"', 1)[1]
        self.assertIn("PrintFlow tour — демо для гостей", settings)
        self.assertIn("Завершить PrintFlow tour?", self.app)
        self.assertIn("Запустить PrintFlow tour", self.app)

    def test_queue_and_settings_collapse_on_mobile(self):
        self.assertIn("@media (max-width: 760px)", self.css)
        self.assertIn(".v19-queue-pulse { grid-template-columns: repeat(2,minmax(0,1fr)); }", self.css)
        self.assertIn("#view-settings .settings-quicknav { position: static; }", self.css)


if __name__ == "__main__":
    unittest.main()
