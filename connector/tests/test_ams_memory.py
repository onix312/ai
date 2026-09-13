"""Память слотов AMS живёт в базе, а не в процессе (17.0.25).

Что было не так. Автосинк создавал и обновлял катушки в `spools`, но сам
факт «в слоте 2 стоит PLA чёрный» нигде не сохранял: раскладку слота помнил
процесс (`manager._tray_uuids`), а таблица `spools.ams_slot` очищается, как
только катушку вынули. Поэтому:

* после перезапуска панель показывала «AMS: нет данных», хотя катушки стояли
  в принтере и были занесены в склад;
* снятая катушка исчезала из памяти навсегда — «как в прошлый раз» строилось
  по текущим привязкам и пустело вместе с ними;
* история слотов (`ams_slot_history`) заполнялась только ручными привязками,
  автоматические смены в неё не попадали.

Теперь каждый слот запоминается в `ams_slots` (какая катушка, материал, цвет,
остаток, когда видели), смена катушки пишется в историю, а пустой слот
сохраняет последнюю катушку — видно, что здесь стояло.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import ams_sync  # noqa: E402
from connector.printflow.db import Database  # noqa: E402

_held: list = []


def make_db() -> Database:
    _held.append(tempfile.TemporaryDirectory())
    return Database(pathlib.Path(_held[-1].name) / "test.sqlite3")


def seed_printer(db: Database, printer_id: str = "p1") -> None:
    from connector.printflow.config import now_iso
    db.upsert("printers", {"id": printer_id, "name": "P1S", "host": "10.0.0.5",
                           "created_at": now_iso()})


def snap_with(*trays: dict) -> dict:
    return {"id": "p1", "printer": {"firmware": "01.07", "wifi": "-52"},
            "ams": {"humidity": 22, "trays": list(trays)}}


def tray(slot: int, *, material: str = "PLA", color: str = "#111111",
         remain: float = 80.0, uuid: str = "a" * 32, present: bool = True,
         generic: bool = False, label: str = "") -> dict:
    return {"slot": slot, "type": material, "color": color, "remain": remain,
            "uuid": uuid, "present": present, "generic": generic,
            "label": label or f"Слот {slot}"}


class SlotMemoryTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        seed_printer(self.db)

    def tearDown(self):
        self.db.close()

    def memory(self, slot: str = "0") -> dict:
        return self.db.one("SELECT * FROM ams_slots WHERE printer_id=? AND slot=?",
                           ("p1", slot)) or {}

    def history(self, action: str = "") -> list[dict]:
        if action:
            return self.db.query(
                "SELECT * FROM ams_slot_history WHERE action=? ORDER BY at", (action,))
        return self.db.query("SELECT * FROM ams_slot_history ORDER BY at")

    def test_occupied_slot_is_remembered_with_the_spool(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        spool = self.db.one("SELECT * FROM spools WHERE printer_id=?", ("p1",))
        self.assertIsNotNone(spool, "катушка из AMS не завелась на складе")
        row = self.memory("0")
        self.assertEqual(spool["id"], row["spool_id"])
        self.assertEqual("PLA", row["material"])
        self.assertEqual("live", row["state"])
        self.assertEqual("a" * 32, row["tray_uuid"])
        self.assertGreater(row["grams_left"], 0)
        self.assertEqual(80.0, row["remain_pct"], "остаток слота берётся из AMS")
        self.assertTrue(row["seen_at"], "не записано, когда слот видели")

    def test_history_keeps_automatic_changes(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        actions = [row["action"] for row in self.history()]
        self.assertIn("auto_create", actions, "катушка из AMS заведена без записи в историю")

    def test_existing_binding_gets_into_history_on_first_sync(self):
        """Память только что появилась, а привязки в базе уже есть (обновление):
        первый же синк записывает их и в память, и в историю."""
        from connector.printflow.config import now_iso
        self.db.upsert("spools", {"id": "sp-manual", "material": "PLA",
                                  "color_name": "Чёрный", "total_grams": 1000,
                                  "remaining_grams": 900, "location": "ams",
                                  "printer_id": "p1", "ams_slot": "0",
                                  "tray_uuid": "a" * 32, "ams_sync": 1,
                                  "created_at": now_iso()})
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, remain=90)))
        actions = [row["action"] for row in self.history()]
        self.assertIn("auto_bind", actions, "привязка складской катушки не попала в историю")
        self.assertEqual("sp-manual", self.memory("0")["spool_id"],
                         "память слота не указывает на складскую катушку")

    def test_second_sync_does_not_duplicate_memory_or_history(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        rows = self.db.query("SELECT * FROM ams_slots WHERE printer_id=?", ("p1",))
        self.assertEqual(1, len(rows), "слот запомнился дважды")
        self.assertEqual(1, len(self.history()),
                         "повторный синк дописывает историю без изменений")

    def test_emptied_slot_keeps_the_last_spool(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        spool_id = self.memory("0")["spool_id"]
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, present=False)))
        row = self.memory("0")
        self.assertEqual("empty", row["state"])
        self.assertEqual(spool_id, row["spool_id"], "память о катушке потерялась")
        self.assertEqual("PLA", row["material"])
        self.assertTrue(row["emptied_at"], "не записано, когда слот опустел")
        self.assertEqual(-1.0, row["remain_pct"])
        self.assertIn("auto_unbind", [r["action"] for r in self.history()])
        bound = self.db.one("SELECT * FROM spools WHERE id=?", (spool_id,))
        self.assertEqual("", bound.get("ams_slot") or "",
                         "катушка осталась привязана к пустому слоту")

    def test_swapped_spool_is_recorded_as_a_swap(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, material="PLA")))
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(
            tray(0, material="PETG", color="#2266cc", uuid="b" * 32)))
        actions = [row["action"] for row in self.history()]
        self.assertIn("auto_swap", actions, "смена катушки в слоте не попала в историю")
        row = self.memory("0")
        self.assertEqual("PETG", row["material"])
        self.assertEqual("b" * 32, row["tray_uuid"])
        self.assertNotIn("a" * 32, str(self.db.query(
            "SELECT * FROM ams_slots WHERE tray_uuid=?", ("a" * 32,))))

    def test_memory_survives_restart_and_silent_printer(self):
        """Главный смысл: раскладка AMS читается из базы, а не из процесса."""
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, material="ABS")))
        path = pathlib.Path(self.db.path)
        self.db.close()
        reopened = Database(path)
        try:
            rows = ams_sync.slot_memory(reopened, "p1")
            self.assertEqual(1, len(rows))
            self.assertEqual("ABS", rows[0]["material"])
            self.assertIsNotNone(reopened.one(
                "SELECT * FROM spools WHERE material='ABS'"), "катушка не пережила перезапуск")
        finally:
            reopened.close()

    def test_backfill_fills_memory_from_existing_bindings(self):
        """Главный случай владельца: раскладка уже есть в базе — показать её.

        Принтер может быть выключен, панель только что обновилась, память
        слотов пуста. Привязки из `spools` должны попасть в память сами.
        """
        from connector.printflow.config import now_iso
        self.db.upsert("spools", {"id": "sp1", "material": "PETG",
                                  "color_name": "Синий", "color_hex": "#2266cc",
                                  "total_grams": 1000, "remaining_grams": 450,
                                  "location": "ams", "printer_id": "p1",
                                  "ams_slot": "2", "tray_uuid": "c" * 32,
                                  "synced_at": "2020-01-01T09:00:00+00:00",
                                  "created_at": now_iso()})
        self.assertEqual([], ams_sync.slot_memory(self.db, "p1"),
                         "память должна быть пустой до дозаполнения")
        self.assertEqual(1, ams_sync.backfill_slots(self.db, "p1"))
        rows = ams_sync.slot_memory(self.db, "p1")
        self.assertEqual(1, len(rows))
        self.assertEqual("PETG", rows[0]["material"])
        self.assertEqual("sp1", rows[0]["spool_id"])
        self.assertEqual(450.0, rows[0]["grams_left"])
        self.assertEqual("live", rows[0]["state"])
        self.assertTrue(rows[0]["stale"], "время взято не из последней синхронизации катушки")
        self.assertEqual(0, ams_sync.backfill_slots(self.db, "p1"),
                         "повторное дозаполнение создаёт вторую запись слота")

    def test_backfill_does_not_overwrite_live_memory(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, material="ABS", remain=42)))
        self.db.upsert("spools", {"id": "sp-old", "material": "PLA",
                                  "remaining_grams": 100, "location": "ams",
                                  "printer_id": "p1", "ams_slot": "0"})
        ams_sync.backfill_slots(self.db, "p1")
        row = self.memory("0")
        self.assertEqual("ABS", row["material"], "дозаполнение перезаписало живые данные")
        self.assertEqual(42.0, row["remain_pct"])

    def test_memory_marks_stale_data(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        self.assertFalse(ams_sync.slot_memory(self.db, "p1")[0]["stale"])
        self.db.execute("UPDATE ams_slots SET seen_at=? WHERE printer_id=?",
                        ("2020-01-01T10:00:00+00:00", "p1"))
        rows = ams_sync.slot_memory(self.db, "p1")
        self.assertTrue(rows[0]["stale"],
                        "вчерашняя загрузка выдаётся за живую")

    def test_unbound_tray_is_still_remembered(self):
        """AMS видит пластик, а катушки на складе нет (автосоздание выключено)."""
        self.db.set_settings({"ams_auto_spools": False})
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(
            tray(1, material="TPU", color="#ff8800")))
        self.assertEqual([], self.db.query("SELECT * FROM spools"))
        row = self.memory("1")
        self.assertEqual("TPU", row["material"])
        self.assertEqual("", row["spool_id"])
        self.assertEqual("live", row["state"])

    def test_sync_one_printer_writes_card_and_memory(self):
        ams_sync.sync_one_printer(self.db, "p1", snap_with(tray(0)))
        printer = self.db.one("SELECT * FROM printers WHERE id=?", ("p1",))
        self.assertEqual("01.07", printer.get("firmware"))
        self.assertEqual("22", str(printer.get("ams_humidity")))
        self.assertTrue(self.memory("0").get("material"))


class SlotMemoryRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        seed_printer(self.db)
        from connector.printflow.api import Api
        from connector.printflow.shelf import Shelf
        import time
        import types
        from unittest import mock
        api = Api.__new__(Api)
        api.db = self.db
        api.shelf = Shelf(self.db)
        api.manager = types.SimpleNamespace(printers={}, bot=None)
        api.acc = mock.Mock()
        api.bus = types.SimpleNamespace(publish=lambda *a, **k: None)
        api.started_at = time.time()
        api.last_host = "test"
        self.api = api

    def tearDown(self):
        self.db.close()

    def test_route_returns_memory_with_marks(self):
        # Слот 1 сначала был занят, потом опустел — именно такой слот память
        # и хранит: «здесь стояло вот это».
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0), tray(1)))
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0), tray(1, present=False)))
        code, payload = self.api.get("/api/ams/memory", {"printer_id": ["p1"]})
        self.assertEqual(200, code)
        self.assertEqual("p1", payload["printer_id"])
        slots = {str(s["slot"]): s for s in payload["slots"]}
        self.assertEqual({"0", "1"}, set(slots))
        self.assertEqual("PLA", slots["0"]["material"])
        self.assertIn("stale", slots["0"])
        self.assertIn("grams_left", slots["0"])
        self.assertIn("seen_at", slots["0"])

    def test_clear_forgets_memory_but_keeps_bindings(self):
        """Ручная чистка: забывает раскладку, но не ломает учёт пластика."""
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0)))
        spool_id = self.db.one("SELECT id FROM spools")["id"]
        code, payload = self.api.post("/api/ams/memory/clear", {"printer_id": "p1"}, {})
        self.assertEqual(200, code)
        self.assertEqual(1, payload["removed"])
        self.assertEqual([], payload["slots"])
        self.assertEqual([], ams_sync.slot_memory(self.db, "p1"))
        bound = self.db.one("SELECT * FROM spools WHERE id=?", (spool_id,))
        self.assertEqual("0", bound["ams_slot"],
                         "чистка памяти сняла привязку катушки на складе")
        self.assertEqual("a" * 32, bound["tray_uuid"], "чистка памяти стёрла RFID катушки")

    def test_clear_needs_printer(self):
        code, payload = self.api.post("/api/ams/memory/clear", {}, {})
        self.assertEqual(400, code)
        self.assertIn("принтер", payload["error"].lower())

    def test_clear_one_slot_keeps_the_others(self):
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0), tray(1)))
        code, payload = self.api.post("/api/ams/memory/clear",
                                      {"printer_id": "p1", "slot": 1}, {})
        self.assertEqual(200, code)
        self.assertEqual(1, payload["removed"])
        self.assertEqual(["0"], [str(r["slot"]) for r in payload["slots"]])

    def test_memory_goes_into_backup_and_restores(self):
        """Память слотов — часть данных: выгрузка и восстановление её несут."""
        from connector.printflow.repo import Repo
        ams_sync.sync_ams_spools(self.db, "p1", snap_with(tray(0, material="ASA")))
        payload = Repo(self.db).export_all()
        self.assertEqual(1, len(payload.get("ams_slots") or []))
        self.assertEqual("ASA", payload["ams_slots"][0]["material"])

    def test_route_without_data_is_empty_not_error(self):
        code, payload = self.api.get("/api/ams/memory", {"printer_id": ["p9"]})
        self.assertEqual(200, code)
        self.assertEqual([], payload["slots"])


if __name__ == "__main__":
    unittest.main()
