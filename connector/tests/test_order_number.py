"""Нумерация заказов: номер не откатывается и не дублируется.

Раньше следующий номер считался от количества строк (``1000 + COUNT(orders) + 1``),
и удаление заказа ломало нумерацию в обе стороны: после удаления последнего
номер откатывался назад, после удаления среднего новый заказ получал номер
уже существующего. Дубль номера опасен не «красотой»: по номеру клиентский бот
ищет «выдать 1003» и «оплата подтвердить 1003», по нему открывается трекинг
заказа и он же попадает в назначение СБП-перевода — оплата и выдача уходят
не в тот заказ.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402

_held: list = []


def make_db() -> tuple[Database, pathlib.Path]:
    folder = tempfile.TemporaryDirectory()
    _held.append(folder)
    path = pathlib.Path(folder.name) / "orders.sqlite3"
    return Database(path), path


class OrderNumberTests(unittest.TestCase):
    def setUp(self):
        self.db, self.path = make_db()
        self.repo = Repo(self.db)

    def _create(self, product: str, **extra) -> dict:
        data = {"product": product, "price": 100, "status": "new"}
        data.update(extra)
        return self.repo.save_order(data)

    def _numbers(self) -> list[str]:
        return [row["number"] for row in
                self.db.query("SELECT number FROM orders ORDER BY CAST(number AS INTEGER)")]

    def test_first_order_starts_from_1001(self):
        self.assertEqual(self._create("первый")["number"], "1001")
        self.assertEqual(self._create("второй")["number"], "1002")

    def test_number_does_not_roll_back_after_deleting_last_order(self):
        """Удаление последнего заказа не возвращает счётчик назад."""
        self._create("первый")                                    # 1001
        last = self._create("последний")                           # 1002
        self.repo.delete_order(last["id"])
        self.assertEqual(self._create("новый")["number"], "1003",
                         "номер после удаления не должен повторять удалённый")

    def test_no_duplicate_after_middle_delete(self):
        """Удаление середины не выдаёт номер живого заказа."""
        a, b, c = (self._create(f"заказ {i}") for i in (1, 2, 3))
        self.repo.delete_order(b["id"])
        new = self._create("после удаления среднего")
        self.assertNotIn(new["number"], [a["number"], c["number"]])
        numbers = self._numbers()
        self.assertEqual(len(numbers), len(set(numbers)), "дубли номеров в базе")

    def test_deleted_numbers_are_never_reused(self):
        """Полная зачистка не обнуляет нумерацию."""
        for order in (self._create("один"), self._create("два"), self._create("три")):
            self.repo.delete_order(order["id"])
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM orders")["n"], 0)
        self.assertEqual(self._create("после зачистки")["number"], "1004")

    def test_manual_number_is_kept_and_counter_jumps_over_it(self):
        """Ручной номер впереди счётчика не порождает коллизию."""
        self._create("обычный")                                # 1001
        self._create("вручную", number="1004")                  # занят вручную
        self.assertEqual(self._create("следующий")["number"], "1005")
        self.assertEqual(self._numbers(), ["1001", "1004", "1005"])

    def test_duplicate_explicit_number_rejected(self):
        self._create("оригинал", number="2027")
        with self.assertRaises(ValueError):
            self._create("дубль", number="2027")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM orders")["n"], 1)

    def test_edit_into_taken_number_rejected(self):
        """Переименовать существующий заказ в чужой номер нельзя — это тот же дубль."""
        first = self._create("первый", number="3001")
        second = self._create("второй", number="3002")
        with self.assertRaises(ValueError):
            self.repo.save_order({"id": second["id"], "number": "3001"})
        self.assertEqual(self.db.one(
            "SELECT number FROM orders WHERE id=?", (second["id"],))["number"], "3002")
        # своя карточка со своим номером сохраняется спокойно
        self.assertEqual(self.repo.save_order(
            {"id": first["id"], "number": "3001", "product": "первый"})["product"], "первый")

    def test_counter_survives_reopen(self):
        """Счётчик в базе, а не в памяти процесса: рестарт не сбрасывает номера."""
        self._create("до перезапуска")
        self.db.close()
        reopened = Database(self.path)
        try:
            self.assertEqual(Repo(reopened).save_order(
                {"product": "после перезапуска", "price": 100, "status": "new"})["number"],
                "1002")
        finally:
            self.db = reopened

    def test_parallel_saves_take_unique_numbers(self):
        """Параллельные создание и удаление не выдают один номер двум заказам."""
        created: list[str] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            order = self._create(f"поток {index}")
            with lock:
                created.append(order["number"])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(created), 8)
        self.assertEqual(len(set(created)), 8, f"дубли: {sorted(created)}")


class OrderNumberMigrationTests(unittest.TestCase):
    def test_existing_base_with_duplicates_catches_up(self):
        """База с «дырявой» историей: правка не наступает на живой номер."""
        db, _ = make_db()
        for index, (order_id, number) in enumerate(
                [("o1", "1001"), ("o2", "1003"), ("o3", "1003")]):
            db.upsert("orders", {"id": order_id, "number": number,
                                 "product": f"история {index}", "price": 100,
                                 "status": "new", "created_at": "2026-09-10T10:00:00",
                                 "updated_at": "2026-09-10T10:00:00"})
        repo = Repo(db)
        self.assertEqual(repo.save_order(
            {"product": "новый", "price": 100, "status": "new"})["number"], "1004")


if __name__ == "__main__":
    unittest.main()
