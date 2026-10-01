"""PrintFlow 19 human-first Printers screen contracts."""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site"


class PrintersV19Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (SITE / "index.html").read_text(encoding="utf-8")
        cls.js = (SITE / "assets" / "printer.js").read_text(encoding="utf-8")
        cls.css = (SITE / "assets" / "v19-shell.css").read_text(encoding="utf-8")

    def _view(self) -> str:
        return self.index.split('id="view-printers"', 1)[1].split(
            '<!-- ================================================== ОЧЕРЕДЬ', 1)[0]

    def test_header_keeps_only_add_as_primary_action(self):
        view = self._view()
        head = view.split('id="pr_park"', 1)[0]
        self.assertIn('id="pr_add"', head)
        self.assertIn('<summary class="btn">Ещё ▾</summary>', head)
        self.assertIn('id="pr_edit"', head)
        self.assertIn('id="pr_reconnect"', head)
        self.assertLess(head.index('id="pr_add"'), head.index('<summary class="btn">Ещё ▾</summary>'))

    def test_selected_printer_has_operator_action_strip(self):
        view = self._view()
        self.assertIn('id="pr_operator"', view)
        self.assertIn('id="pr_operator_name"', view)
        self.assertIn('id="pr_operator_state"', view)
        self.assertIn('id="pr_quick_pause"', view)
        self.assertIn('id="pr_quick_resume"', view)
        self.assertIn('id="pr_quick_stop"', view)
        self.assertIn('data-pr-tab="camera"', view)
        self.assertIn('data-pr-tab="ams"', view)

    def test_quick_commands_use_existing_command_pipeline(self):
        self.assertIn("const quickPause = $('pr_quick_pause')", self.js)
        self.assertIn("quickPause.hidden = !isRun", self.js)
        self.assertIn("quickResume.hidden = !isPaused", self.js)
        self.assertIn("quickStop.hidden = !(isRun || isPaused)", self.js)
        self.assertIn("const cmd = e.target.closest('[data-cmd]')", self.js)
        self.assertIn("command(name, value, { label: cmd.textContent.trim(), button: cmd })", self.js)

    def test_context_tabs_jump_without_new_api(self):
        self.assertIn("const tabJump = e.target.closest('[data-pr-tab]')", self.js)
        self.assertIn("selectPtab(tabJump.dataset.prTab)", self.js)
        block = self.js.split("const tabJump = e.target.closest('[data-pr-tab]')", 1)[1].split(
            "const cmd =", 1)[0]
        self.assertNotIn("/api/", block)

    def test_visual_hierarchy_is_fleet_operator_details(self):
        self.assertIn("body.pf-v19 #view-printers #pr_park", self.css)
        self.assertIn(".v19-pr-operator", self.css)
        self.assertIn("body.pf-v19 #view-printers .ptab-bar", self.css)
        self.assertIn("grid-template-columns: minmax(0,1.7fr) minmax(300px,.8fr)", self.css)
        self.assertIn("@media (max-width: 900px)", self.css)


if __name__ == "__main__":
    unittest.main()
