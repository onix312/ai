"""Проверки новых безопасных поверхностей PrintFlow 10.0."""
from __future__ import annotations

import pathlib
import tempfile
import unittest

from connector.printflow.db import Database
from connector.printflow.manager import PrinterManager


class PrintFlow10Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "pf.sqlite3")
        self.manager = PrinterManager(self.db, None)  # type: ignore[arg-type]

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self.tmp.cleanup()

    def test_inbox_pipeline_column_migrates_with_default(self):
        columns = {row["name"] for row in self.db.query("PRAGMA table_info(client_chats)")}
        self.assertIn("pipeline_stage", columns)
        self.db.execute("INSERT INTO client_chats(chat_id,name) VALUES(?,?)", ("42", "Тест"))
        row = self.db.one("SELECT pipeline_stage FROM client_chats WHERE chat_id='42'")
        self.assertEqual(row["pipeline_stage"], "new")

    def test_rules_simulation_never_executes_action(self):
        engine = self.manager.rules
        rule = engine.save_rule({
            "name": "Тест dry-run", "event": "print_failed", "action": "notify",
            "config": {"template": "Ошибка {name}"}, "enabled": 1,
        })
        self.manager.notify_async = lambda *_args: self.fail("dry-run не должен отправлять уведомление")
        result = engine.simulate("print_failed", {"name": "модель", "detail": "проверка"}, rule["id"])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["matched"])
        self.assertEqual(result[0]["preview"], "Ошибка модель")
        row = self.db.one("SELECT mode,matched FROM automation_rule_runs WHERE rule_id=?", (rule["id"],))
        self.assertEqual(row["mode"], "dry_run")
        self.assertEqual(row["matched"], 1)
        saved = self.db.one("SELECT fires FROM automation_rules WHERE id=?", (rule["id"],))
        self.assertEqual(saved["fires"], 0)

    def test_rule_simulation_reports_non_matching_status(self):
        engine = self.manager.rules
        rule = engine.save_rule({
            "name": "Только готовые", "event": "order_status", "action": "event",
            "config": {"status": "ready", "template": "№{number}"}, "enabled": 1,
        })
        result = engine.simulate("order_status", {"status": "printing", "number": "7"}, rule["id"])
        self.assertFalse(result[0]["matched"])
        self.assertEqual(result[0]["preview"], "№7")


if __name__ == "__main__":
    unittest.main()
