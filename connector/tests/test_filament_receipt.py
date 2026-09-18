import unittest
from connector.printflow.db import Database
from connector.printflow.repo import Repo
from connector.printflow.accounting import Accounting
from connector.printflow.shopping import ShoppingList
from connector.printflow.workshop_v9 import WorkshopV9
from connector.printflow.router import router, register_all
from connector.tests.test_phase11 import make_db


class FilamentReceiptTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)
        self.shopping = ShoppingList(self.db)
        self.workshop = WorkshopV9(self.db, self.repo, self.shopping, acc=self.acc)
        self.workshop.ensure_schema()
        # Создадим тестовый склад и кассу
        self.db.upsert("accounts", {"id": "bank_card", "name": "Банковская карта", "archived": 0})
        self.db.upsert("warehouses", {"id": "wh_main", "name": "Основной склад", "archived": 0})

    def tearDown(self):
        self.db.close()

    def test_filament_receipt_bulk_items_and_location(self):
        """Пакетный приход пластика: создание катушек на выбранном складе/месте."""
        items = [
            {
                "material": "PLA",
                "color_name": "Белый",
                "color_hex": "#ffffff",
                "brand": "Bambu Lab",
                "spool_count": 3,
                "spool_grams": 1000,
                "price_per_kg": 1500,
                "total_amount": 4500,
            },
            {
                "material": "PETG",
                "color_name": "Чёрный",
                "color_hex": "#000000",
                "brand": "FDPlast",
                "spool_count": 2,
                "spool_grams": 1000,
                "price_per_kg": 1800,
                "total_amount": 3600,
            },
            {
                "material": "ABS",
                "color_name": "Серый",
                "color_hex": "#888888",
                "brand": "Filamentarno",
                "spool_count": 1,
                "spool_grams": 800,
                "price_per_kg": 2000,
                "total_amount": 1600,
            },
        ]
        res = self.workshop.filament_receipt(
            items=items,
            location="home",
            location_note="Полка А2",
            warehouse_id="wh_main",
            supplier="ПолимерТорг",
            account_id="bank_card",
            note="Накладная №789",
            confirmed=True,
            request_id="bulk-fr-1",
        )
        self.assertTrue(res["ok"])
        self.assertFalse(res["already"])
        doc = res["document"]
        self.assertEqual(doc["kind"], "filament_receipt")
        self.assertEqual(doc["total_amount"], 9700.0)
        self.assertEqual(doc["grams"], 5800.0)
        self.assertEqual(doc["location"], "home")
        self.assertEqual(doc["warehouse_id"], "wh_main")

        # Проверяем, что создано ровно 6 катушек (3 + 2 + 1)
        spools = res["spools"]
        self.assertEqual(len(spools), 6)

        # Проверяем атрибуты катушек в базе данных
        db_spools = self.repo.spools()
        self.assertEqual(len(db_spools), 6)
        for s in db_spools:
            self.assertEqual(s["location"], "home")
            self.assertEqual(s["location_note"], "Полка А2")
            self.assertEqual(s["warehouse_id"], "wh_main")
            self.assertEqual(s["supplier"], "ПолимерТорг")
            self.assertEqual(s["received_doc_id"], doc["id"])

        pla_spools = [s for s in db_spools if s["material"] == "PLA"]
        self.assertEqual(len(pla_spools), 3)
        for s in pla_spools:
            self.assertEqual(s["color_name"], "Белый")
            self.assertEqual(s["total_grams"], 1000)
            self.assertEqual(s["remaining_grams"], 1000)
            self.assertEqual(s["price"], 1500.0)
            self.assertEqual(s["price_per_kg"], 1500.0)

        abs_spools = [s for s in db_spools if s["material"] == "ABS"]
        self.assertEqual(len(abs_spools), 1)
        self.assertEqual(abs_spools[0]["total_grams"], 800)
        self.assertEqual(abs_spools[0]["remaining_grams"], 800)
        self.assertEqual(abs_spools[0]["price"], 1600.0)
        self.assertEqual(abs_spools[0]["price_per_kg"], 2000.0)

    def test_filament_receipt_financial_expense(self):
        """Приход пластика создаёт расход в финансах: kind='expense', category='filament'."""
        res = self.workshop.filament_receipt(
            material="PETG",
            color_name="Красный",
            spool_count=2,
            spool_grams=1000,
            total_amount=3200,
            account_id="bank_card",
            supplier="3DПоставка",
            confirmed=True,
            request_id="fin-exp-1",
        )
        tx = res.get("transaction")
        self.assertIsNotNone(tx)
        self.assertEqual(tx["kind"], "expense")
        self.assertEqual(tx["category"], "filament")
        self.assertEqual(tx["amount"], 3200.0)
        self.assertEqual(tx["account_id"], "bank_card")
        self.assertIn("Приход пластика", tx["title"])

        # Проверка транзакции в таблице transactions
        db_tx = self.db.one("SELECT * FROM transactions WHERE id=?", (tx["id"],))
        self.assertIsNotNone(db_tx)
        self.assertEqual(db_tx["kind"], "expense")
        self.assertEqual(db_tx["category"], "filament")
        self.assertEqual(db_tx["amount"], 3200.0)

    def test_filament_receipt_custom_location_dry(self):
        """Катушки попадают в указанное место хранения (dry / ams / home / other)."""
        res = self.workshop.filament_receipt(
            material="PA-CF",
            color_name="Антрацит",
            spool_count=1,
            spool_grams=1000,
            total_amount=4500,
            location="dry",
            location_note="Сушилка 1, 65°C",
            confirmed=True,
            request_id="loc-dry-1",
        )
        spool = res["spools"][0]
        self.assertEqual(spool["location"], "dry")
        self.assertEqual(spool["location_note"], "Сушилка 1, 65°C")

    def test_filament_receipt_idempotency(self):
        """Повторный запрос с тем же request_id не дублирует катушки и проводки."""
        first = self.workshop.filament_receipt(
            material="PLA",
            spool_count=2,
            total_amount=2000,
            confirmed=True,
            request_id="idem-1",
        )
        self.assertFalse(first["already"])
        self.assertEqual(len(self.repo.spools()), 2)
        tx_count_before = len(self.db.query("SELECT id FROM transactions WHERE category='filament'"))
        self.assertEqual(tx_count_before, 1)

        # Повторный вызов
        second = self.workshop.filament_receipt(
            material="PLA",
            spool_count=2,
            total_amount=2000,
            confirmed=True,
            request_id="idem-1",
        )
        self.assertTrue(second["already"])
        self.assertEqual(len(self.repo.spools()), 2)
        tx_count_after = len(self.db.query("SELECT id FROM transactions WHERE category='filament'"))
        self.assertEqual(tx_count_after, 1)

    def test_route_workshop_receipt_via_api(self):
        """Маршрут /api/workshop/receipt корректно принимает пакетный приход и параметры места."""
        register_all()
        class DummyApi:
            def __init__(self, db, repo, shopping, acc, workshop):
                self.db = db
                self.repo = repo
                self.shopping = shopping
                self.acc = acc
                self.workshop = workshop
                self.bus = None

        dummy_api = DummyApi(self.db, self.repo, self.shopping, self.acc, self.workshop)
        status, body = router.dispatch(
            dummy_api,
            "POST",
            "/api/workshop/receipt",
            body={
                "items": [
                    {"material": "TPU 95A", "color_name": "Жёлтый", "spool_count": 2, "price_per_kg": 2500, "total_amount": 5000},
                ],
                "location": "other",
                "location_note": "Спец-бокс",
                "warehouse_id": "wh_main",
                "supplier": "ГибкиеПластики",
                "account_id": "bank_card",
                "confirmed": True,
                "request_id": "api-rcpt-1",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["spools"]), 2)
        for s in body["spools"]:
            self.assertEqual(s["location"], "other")
            self.assertEqual(s["location_note"], "Спец-бокс")
            self.assertEqual(s["warehouse_id"], "wh_main")

    def test_shopping_receive_with_location(self):
        """Приёмка позиции закупки сохраняет выбранное местоположение."""
        item = self.shopping.add({"name": "ASA", "material": "ASA", "qty": 1})
        res = self.shopping.receive(
            item["id"],
            received_confirmed=True,
            payment_confirmed=True,
            material="ASA",
            color_name="Белый",
            spool_count=1,
            spool_grams=1000,
            total_amount=1900,
            account_id="bank_card",
            location="home",
            request_id="shop-recv-1",
        )
        self.assertFalse(res["already_received"])
        spool = self.db.one("SELECT * FROM spools WHERE id=?", (res["spools"][0]["id"],))
        self.assertEqual(spool["location"], "home")

    def test_filament_receipt_skips_empty_table_rows(self):
        """Пустая строка таблицы прихода не превращается в катушку PLA.

        В модалке «+ Позиция» добавляет строку; если владелец оставил её
        незаполненной и нажал «Оформить приход» — раньше создавалась
        лишняя катушка PLA с дефолтами, и расход задваивался.
        """
        res = self.workshop.filament_receipt(
            items=[
                {"material": "", "color_name": "", "spool_count": "", "total_amount": ""},
                {"material": "PETG", "color_name": "Чёрный", "spool_count": 2,
                 "spool_grams": 1000, "total_amount": 3600},
            ],
            location="shop",
            supplier="ТестПластик",
            confirmed=True,
            request_id="empty-row-1",
        )
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["spools"]), 2, "пустая строка создала лишнюю катушку")
        materials = {s["material"] for s in res["spools"]}
        self.assertEqual(materials, {"PETG"}, "пустая строка уехала в катушки как PLA")

    def test_filament_receipt_route_via_router_registry(self):
        """POST /api/workshop/receipt идёт через реестр маршрутов и отвечает 4xx без подтверждения."""
        register_all()
        from connector.tests.test_phase11 import make_api
        api = make_api(self.db)
        api.repo = self.repo
        api.shopping = self.shopping
        api.acc = self.acc
        api.workshop = self.workshop

        # Без подтверждения маршрут возбуждает ValueError — транспортный слой
        # (http_handler) превращает его в ответ 400 с полем error.
        with self.assertRaises(ValueError) as caught:
            router.dispatch(
                api, "POST", "/api/workshop/receipt",
                body={"items": [{"material": "PLA", "spool_count": 1}],
                      "request_id": "no-confirm-1", "confirmed": False})
        self.assertIn("Подтвердите", str(caught.exception))

        status, body = router.dispatch(
            api, "POST", "/api/workshop/receipt",
            body={"items": [{"material": "PLA", "color_name": "Синий",
                             "spool_count": 1, "total_amount": 1500}],
                  "supplier": "РоутПоставщик", "confirmed": True,
                  "request_id": "no-confirm-2"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["spools"]), 1)


if __name__ == "__main__":
    unittest.main()
