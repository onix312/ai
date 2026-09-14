"""Предохранитель автозапуска: сбои подряд останавливают очередь (18.0.9).

Этап 3 «автономность», шаг второй. Автомат без тормоза — не автоматика, а
лотерея: если печать срывается раз за разом, очередь обязана встать и позвать
человека. Авто-перепечатки в PrintFlow нет (отклонена владельцем), поэтому
единственный правильный ход — остановка до ручного запуска, а не «попробуем
ещё разок».

Счёт ведётся по фактам журнала (`print_jobs`), а не по памяти процесса: его
нельзя забыть сбросить и он не теряется при перезапуске коннектора.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.manager import (  # noqa: E402
    FAILED_STREAK_LIMIT, STREAK_EVENT, PrinterManager)


class FakePrinter:
    """Принтер на связи и без задания: дальше решает очередь."""

    def __init__(self, printer_id: str = "prn1", name: str = "Цех-1",
                 state: str = "IDLE", connected: bool = True):
        self.id = printer_id
        self.record = {"id": printer_id, "name": name}
        self.connected = connected
        self.state = state

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "name": self.record["name"],
            "connection": {"connected": self.connected, "configured": True, "mode": "local"},
            "printer": {"state": self.state, "state_label": "Свободен",
                        "problems": [], "progress": 0, "remaining_min": 0},
            "temperature": {"nozzle": 25.0, "bed": 25.0},
            "ams": {"trays": [{"slot": 0, "label": "Слот 1", "type": "PETG",
                               "active": True, "present": True}]},
            "camera": {"available": False},
            "guard": {"alerts": []},
        }


class StreakGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.manager = PrinterManager.__new__(PrinterManager)
        self.manager.db = self.db
        self.manager.printers = {"prn1": FakePrinter()}
        self.manager.lock = mock.MagicMock()
        self.made = 0

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def job(self, state: str, finished: str, job_id: str = "") -> dict:
        """Запись в журнал печати: id уникален, время — как передали."""
        self.made += 1
        job_id = job_id or f"job_{self.made}"
        job = {"id": job_id, "printer_id": "prn1", "name": job_id, "state": state,
               "finished_at": finished, "created_at": finished}
        return self.db.upsert("print_jobs", job)

    def test_streak_counts_from_the_journal_and_breaks_on_a_good_print(self):
        self.job("failed", "2026-09-14T05:00:00+00:00")
        self.job("failed", "2026-09-14T07:00:00+00:00")
        self.assertEqual(2, self.manager.failed_streak("prn1"))
        # Отменённая оператором печать — не сбой принтера: серия прервана.
        self.job("cancelled", "2026-09-14T08:00:00+00:00")
        self.assertEqual(0, self.manager.failed_streak("prn1"))
        # Удачная печать тоже обнуляет серию.
        self.job("failed", "2026-09-14T09:00:00+00:00")
        self.assertEqual(1, self.manager.failed_streak("prn1"))
        self.job("done", "2026-09-14T10:00:00+00:00")
        self.assertEqual(0, self.manager.failed_streak("prn1"))
        self.assertEqual(0, self.manager.failed_streak(""), "без принтера серии нет")

    def test_guard_stops_autostart_but_never_a_manual_start(self):
        self.job("failed", "2026-09-14T05:00:00+00:00")
        self.assertEqual("", self.manager.auto_start_blocked("prn1"),
                         "одного сбоя мало: очередь пробует дальше")
        self.job("failed", "2026-09-14T07:00:00+00:00")
        blocked = self.manager.auto_start_blocked("prn1")
        self.assertIn("автозапуск", blocked)
        self.assertEqual(FAILED_STREAK_LIMIT, self.manager.failed_streak("prn1"))

        self.db.set_settings({"auto_queue": True, "unattended_dangerous_actions": True})
        with mock.patch.object(self.manager, "next_job", return_value=None) as pick:
            self.manager._maybe_start_next("prn1")
            pick.assert_not_called()
        # Один сбой — предохранитель не мешает: очередь доходит до выбора задания.
        self.job("done", "2026-09-14T09:00:00+00:00")
        with mock.patch.object(self.manager, "next_job", return_value=None) as pick:
            self.manager._maybe_start_next("prn1")
            pick.assert_called_once_with("prn1", mock.ANY)

    def test_guard_lives_in_autostart_only(self):
        """Ручной запуск предохранителя не спрашивает.

        Здесь строковый контракт, а не прогон: `start_job` идёт дальше в сеть
        принтера (FTPS, MQTT), и поднимать это ради проверки одного условия
        значит проверять не то. Важно ровно одно: предохранитель стоит в
        автозапуске и больше нигде — иначе оператор у станка не смог бы
        запустить печать руками.
        """
        import inspect

        auto = inspect.getsource(PrinterManager._maybe_start_next)
        manual = inspect.getsource(PrinterManager.start_job)
        self.assertIn("auto_start_blocked", auto,
                      "автозапуск обязан спрашивать предохранитель")
        self.assertNotIn("auto_start_blocked", manual)
        self.assertNotIn("failed_streak", manual)

    def test_stop_is_written_to_the_journal_exactly_once(self):
        self.job("failed", "2026-09-14T05:00:00+00:00")
        second = self.job("failed", "2026-09-14T07:00:00+00:00")
        self.manager._note_streak_stop("prn1", second)
        events = self.db.query("SELECT * FROM events WHERE title=?", (STREAK_EVENT,))
        self.assertEqual(1, len(events), "остановка автозапуска пишется один раз")
        # Третий сбой (если оператор печатал вручную) записи не добавляет:
        # журнал не должен тонуть в одинаковых строках.
        self.job("failed", "2026-09-14T09:00:00+00:00")
        self.manager._note_streak_stop("prn1", second)
        events = self.db.query("SELECT * FROM events WHERE title=?", (STREAK_EVENT,))
        self.assertEqual(1, len(events))
        self.assertIn("вручную", events[0].get("detail") or "")

    def test_autonomy_report_explains_the_stop(self):
        self.job("failed", "2026-09-14T05:00:00+00:00")
        self.job("failed", "2026-09-14T07:00:00+00:00")
        report = self.manager.autonomy_report("prn1")
        self.assertFalse(report["armed"], "флаги по умолчанию выключены")
        printer = report["printers"][0]
        self.assertEqual(2, printer["failed_streak"])
        self.assertFalse(printer["ready"], "при серии сбоев автозапуск встаёт")
        self.assertIn("автозапуск встал", printer["reason"])
        self.assertTrue(any("автозапуск встал" in r for r in report["reasons"]),
                        report["reasons"])
        self.assertTrue(any("сорванных печатей подряд" in r for r in report["rules"]),
                        "правило о предохранителе должно быть видно оператору")


if __name__ == "__main__":
    unittest.main()
