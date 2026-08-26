"""Каталог в Telegram-боте: карточки, цены, витрина клиентского бота.

Проверяется логика команд и inline-кнопок без сети: бот получает базу и
подставной менеджер; исходящие вызовы Telegram перехватываются.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.client_bot import ClientBot  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.manager import PrinterManager  # noqa: E402
from connector.printflow.nomenclature import Nomenclature  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.printflow.staff import ROLE_RIGHTS, Staff, group_for_word  # noqa: E402
from connector.printflow.stock import Stock  # noqa: E402
from connector.printflow.telegram_bot import TelegramBot  # noqa: E402


class FakeManager:
    """Менеджер-заглушка: база, учёт и снимок парка из переданных данных."""

    def __init__(self, db, snapshot=None):
        self.db = db
        self.acc = Accounting(db)
        self.repo = Repo(db)
        self.client_bot = None
        self._snapshot = snapshot or {"printers": []}

    def snapshot(self, printer_id: str = "") -> dict:
        return self._snapshot


class CatalogRightsTests(unittest.TestCase):
    """Права на каталог: просмотр всем, правки — руководителю и владельцу."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.bot = TelegramBot(FakeManager(self.db))

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_catalog_group_in_owner_and_manager_rights(self):
        self.assertIn("catalog", ROLE_RIGHTS["owner"])
        self.assertIn("catalog", ROLE_RIGHTS["manager"])
        self.assertNotIn("catalog", ROLE_RIGHTS["employee"])

    def test_word_groups_mapping(self):
        self.assertEqual(group_for_word("каталог"), "view")
        for word in ("цена", "скрыть", "показать", "товар", "норматив",
                     "минималка", "архив", "вернуть", "удалить", "пересчёт"):
            self.assertEqual(group_for_word(word), "catalog", word)

    def test_callback_groups_mapping(self):
        self.assertEqual(group_for_word("", command="cat:all:0"), "view")
        self.assertEqual(group_for_word("", command="cati:n1"), "view")
        self.assertEqual(group_for_word("", command="cat-hide:n1"), "catalog")
        self.assertEqual(group_for_word("", command="cat-vitrine"), "catalog")
        self.assertEqual(group_for_word("", command="cat-delyes:n1"), "catalog")

    def test_rights_text_mentions_catalog(self):
        text = Staff(self.db).rights_text("manager")
        self.assertIn("каталог", text)


class CatalogCommandTests(unittest.TestCase):
    """Текстовые команды каталога: товар, цена, витрина, карточка, удаление."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.manager = FakeManager(self.db)
        self.bot = TelegramBot(self.manager)
        self.replies: list[str] = []
        self.bot._reply = lambda chat, text, buttons=None: self.replies.append(text)

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _dispatch(self, text: str, chat: str = "111") -> str:
        self.bot._dispatch(chat, text)
        return self.replies[-1]

    def _nom_row(self, name: str) -> dict:
        return self.db.one("SELECT * FROM nomenclature WHERE name=?", (name,)) or {}

    # ------------------------------------------------------------ создание
    def test_create_product_hidden_by_default_with_price(self):
        answer = self._dispatch("товар Адресник для ключей 900р")
        self.assertIn("Адресник для ключей", answer)
        row = self._nom_row("Адресник для ключей")
        self.assertEqual(row.get("kind"), "product")
        self.assertEqual(int(row.get("client_bot_published") or 0), 0)
        price = self.db.one(
            "SELECT price FROM prices WHERE nom_id=? AND price_type_id='retail'",
            (row["id"],))
        self.assertEqual(round(price["price"]), 900)
        self.assertIn("показать", answer)  # подсказка про публикацию

    def test_create_duplicate_name_rejected(self):
        self._dispatch("товар Брелок котик 350р")
        answer = self._dispatch("товар Брелок котик 100")
        self.assertIn("уже есть", answer)
        rows = self.db.query("SELECT id FROM nomenclature WHERE name='Брелок котик'")
        self.assertEqual(len(rows), 1)

    def test_create_without_name_shows_format(self):
        self.assertIn("Формат", self._dispatch("товар"))

    # ---------------------------------------------------------------- цена
    def test_price_sets_base_and_reports_old(self):
        self._dispatch("товар Адресник 900р")
        answer = self._dispatch("цена адресник 1200")
        self.assertIn("1 200", answer)
        nom = self._nom_row("Адресник")
        item = Nomenclature(self.db).item(nom["id"])
        self.assertEqual(item["price"], 1200.0)
        history = self.db.query(
            "SELECT price FROM prices WHERE nom_id=? AND price_type_id='retail'",
            (nom["id"],))
        self.assertEqual(sorted(round(p["price"]) for p in history), [900, 1200])

    def test_price_bad_amount(self):
        self._dispatch("товар Адресник 900р")
        self.assertIn("Не понял сумму", self._dispatch("цена адресник дорого"))

    def test_price_unknown_item(self):
        self.assertIn("не найдена", self._dispatch("цена неведомость 100"))

    # -------------------------------------------------------------- витрина
    def test_hide_and_show_item(self):
        self._dispatch("товар Адресник 900р")
        nom = self._nom_row("Адресник")
        self._dispatch("показать адресник")
        self.assertEqual(int(self._nom_row("Адресник")["client_bot_published"]), 1)
        self._dispatch("скрыть адресник")
        self.assertEqual(int(self._nom_row("Адресник")["client_bot_published"]), 0)
        self.assertIn("скрыт", self.replies[-1])
        # повторное действие отвечает без ошибки
        self._dispatch("скрыть адресник")
        self.assertEqual(int(self._nom_row("Адресник")["client_bot_published"]), 0)

    def test_show_without_price_warns(self):
        self._dispatch("товар Заготовка без цены")
        answer = self._dispatch("показать заготовка")
        self.assertIn("нет цены", answer)
        self.assertEqual(int(self._nom_row("Заготовка без цены")["client_bot_published"]), 0)

    def test_vitrine_toggle_disables_client_catalog(self):
        answer = self._dispatch("скрыть витрину")
        self.assertIn("выключена", answer)
        self.assertEqual(int(self.db.setting("client_bot_catalog", True)), 0)
        self._dispatch("показать витрину")
        self.assertEqual(int(self.db.setting("client_bot_catalog", True)), 1)

    # ------------------------------------------------------------ описание
    def test_description_set_and_read_keeps_case(self):
        self._dispatch("товар Адресник 900р")
        self._dispatch("описание адресник Держатель ключей на 6 крючков")
        row = self._nom_row("Адресник")
        self.assertEqual(row["client_bot_description"],
                         "Держатель ключей на 6 крючков")
        answer = self._dispatch("описание адресник")
        self.assertIn("Держатель ключей", answer)

    def test_description_multiword_name(self):
        self._dispatch("товар Полка для специй 500р")
        self._dispatch("описание полка для Подставка на 4 банок")
        row = self._nom_row("Полка для специй")
        self.assertEqual(row["client_bot_description"], "Подставка на 4 банок")

    # ------------------------------------------------------------ норматив
    def test_norms_update_and_recalc_cost(self):
        self._dispatch("товар Адресник 900р")
        answer = self._dispatch("норматив адресник 25 1.5")
        nom = self._nom_row("Адресник")
        self.assertEqual(round(nom["grams"], 1), 25.0)
        self.assertEqual(round(nom["hours"], 2), 1.5)
        item = Nomenclature(self.db).item(nom["id"])
        self.assertGreater(item["cost"], 0)
        self.assertIn("Себестоимость", answer)

    def test_norms_negative_rejected(self):
        self._dispatch("товар Адресник 900р")
        self.assertIn("отрицательными", self._dispatch("норматив адресник -5 1"))

    # ----------------------------------------------------------- минималка
    def test_minmax_sets_thresholds(self):
        self._dispatch("товар Адресник 900р")
        self._dispatch("минималка адресник 5 20")
        nom = self._nom_row("Адресник")
        self.assertEqual(round(nom["min_qty"], 1), 5.0)
        self.assertEqual(round(nom["max_qty"], 1), 20.0)
        self.assertIn("план пополнения", self.replies[-1])

    # -------------------------------------------------------- архив/удаление
    def test_archive_and_restore(self):
        self._dispatch("товар Адресник 900р")
        self._dispatch("архив адресник")
        self.assertEqual(int(self._nom_row("Адресник")["archived"]), 1)
        self._dispatch("вернуть адресник")
        self.assertEqual(int(self._nom_row("Адресник")["archived"]), 0)

    def test_delete_requires_confirmation(self):
        self._dispatch("товар Адресник 900р")
        answer = self._dispatch("удалить адресник")
        self.assertIn("Точно удалить", answer)
        self.assertIsNotNone(self.db.one("SELECT id FROM nomenclature WHERE name='Адресник'"))
        self._dispatch("удалить адресник да")
        self.assertIsNone(self.db.one("SELECT id FROM nomenclature WHERE name='Адресник'"))

    def test_delete_with_moves_archives_instead(self):
        self._dispatch("товар Адресник 900р")
        nom = self._nom_row("Адресник")
        warehouse = self.db.one("SELECT id FROM warehouses LIMIT 1")
        Stock(self.db).add_move(nom["id"], warehouse["id"], 3)
        answer = self._dispatch("удалить адресник да")
        self.assertIn("архив", answer)
        row = self._nom_row("Адресник")
        self.assertIsNotNone(row)
        self.assertEqual(int(row["archived"]), 1)

    # ------------------------------------------------------------ пересчёт
    def test_recalc_one_from_cost(self):
        self._dispatch("товар Адресник 900р")
        self._dispatch("норматив адресник 25 1.5")
        answer = self._dispatch("пересчёт адресник")
        self.assertIn("Пересчитано", answer)
        item = Nomenclature(self.db).item(self._nom_row("Адресник")["id"])
        self.assertGreater(item["price"], 0)

    def test_recalc_all_requires_confirmation(self):
        self._dispatch("товар Адресник 900р")
        answer = self._dispatch("пересчёт все")
        self.assertIn("пересчёт все да", answer)
        answer = self._dispatch("пересчёт все да")
        self.assertIn("Пересчёт завершён", answer)

    # --------------------------------------------------------------- группы
    def test_groups_listed(self):
        answer = self._dispatch("группы")
        self.assertIn("Группы каталога", answer)

    # ------------------------------------------------------------- права
    def test_employee_can_browse_but_not_edit(self):
        Staff(self.db).add("Ваня", "employee", "222")
        self._dispatch("товар Адресник 900р")
        answer = self._dispatch("цена адресник 100", chat="222")
        self.assertIn("недоступен", answer)
        self.replies.clear()
        answer = self._dispatch("скрыть адресник", chat="222")
        self.assertIn("недоступен", answer)
        # просмотр — доступен
        self.replies.clear()
        self.bot._send_menu = lambda *a, **k: self.replies.append("меню")
        self._dispatch("каталог", chat="222")
        self.assertEqual(self.replies[-1], "меню")


class CatalogCallbackTests(unittest.TestCase):
    """Inline-кнопки каталога: меню, карточка, действия, подтверждения."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.db.set_settings({"telegram_chat_id": "111"})
        self.bot = TelegramBot(FakeManager(self.db))
        self.calls: list[tuple] = []
        self.replies: list[str] = []
        self.bot._call = (lambda method, params, timeout=35:
                          self.calls.append((method, params)) or {"ok": True})
        self.bot._reply = lambda chat, text, buttons=None: self.replies.append(text)
        self.bot._dispatch("111", "товар Брелок котик 350р")
        self.nom_id = self.db.one(
            "SELECT id FROM nomenclature WHERE name='Брелок котик'")["id"]

    def tearDown(self):
        self.bot.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def _callback(self, data: str, chat: str = "111") -> list[dict]:
        self.calls.clear()
        self.bot._handle_callback(
            {"id": "cb", "data": data,
             "message": {"chat": {"id": int(chat)}, "message_id": 5}}, chat)
        return [p for m, p in self.calls
                if m in ("editMessageText", "sendMessage")]

    def _buttons(self, msg: dict) -> list[str]:
        return [b["text"] for row
                in json.loads(msg["reply_markup"])["inline_keyboard"] for b in row]

    def test_menu_lists_items_with_filters(self):
        menu = self._callback("cmd:cat")[0]
        self.assertIn("Каталог", menu["text"])
        self.assertIn("Брелок котик", self._buttons(menu)[0])

    def test_card_shows_price_and_edit_buttons_for_owner(self):
        card = self._callback(f"cmd:cati:{self.nom_id}")[0]
        self.assertIn("Брелок котик", card["text"])
        self.assertIn("350", card["text"])
        buttons = self._buttons(card)
        self.assertIn("👁 Опубликовать в витрине", buttons)
        self.assertIn("🗑 Удалить", buttons)

    def test_hide_show_via_buttons(self):
        self._callback(f"cmd:cat-hide:{self.nom_id}")
        row = self.db.one("SELECT client_bot_published FROM nomenclature WHERE id=?",
                          (self.nom_id,))
        self.assertEqual(int(row["client_bot_published"]), 0)
        card = self._callback(f"cmd:cati:{self.nom_id}")[0]
        self.assertIn("👁 Опубликовать в витрине", self._buttons(card))
        self._callback(f"cmd:cat-show:{self.nom_id}")
        row = self.db.one("SELECT client_bot_published FROM nomenclature WHERE id=?",
                          (self.nom_id,))
        self.assertEqual(int(row["client_bot_published"]), 1)

    def test_delete_via_buttons_needs_yes(self):
        confirm = self._callback(f"cmd:cat-del:{self.nom_id}")[0]
        self.assertIn("Удалить", confirm["text"])
        self._callback(f"cmd:cat-delyes:{self.nom_id}")
        self.assertIsNone(self.db.one("SELECT id FROM nomenclature WHERE id=?",
                                      (self.nom_id,)))

    def test_recalc_all_asks_confirmation(self):
        ask = self._callback("cmd:cat-recalc:all")[0]
        self.assertIn("Пересчитать цены", ask["text"])
        answer = self.replies[-1] if self.replies else ""
        self._callback("cmd:cat-recalc:allgo")
        self.assertTrue(any("Пересчёт" in r for r in self.replies), self.replies)

    def test_vitrine_toggle_button(self):
        self._callback("cmd:cat-vitrine")
        self.assertEqual(int(self.db.setting("client_bot_catalog", True)), 0)
        self.assertIn("выключена", self.replies[-1])
        self._callback("cmd:cat-vitrine")
        self.assertEqual(int(self.db.setting("client_bot_catalog", True)), 1)

    def test_group_assignment_via_buttons(self):
        group_id = self.db.one("SELECT id FROM nom_groups WHERE name='Питомцы'")["id"]
        self._callback(f"cmd:cat-grp:{group_id}:{self.nom_id}")
        row = self.db.one("SELECT group_id FROM nomenclature WHERE id=?",
                          (self.nom_id,))
        self.assertEqual(row["group_id"], group_id)
        self._callback(f"cmd:cat-grp:-:{self.nom_id}")
        row = self.db.one("SELECT group_id FROM nomenclature WHERE id=?",
                          (self.nom_id,))
        self.assertIsNone(row["group_id"])

    def test_employee_denied_edit_buttons_and_card_is_readonly(self):
        Staff(self.db).add("Ваня", "employee", "222")
        card = self._callback(f"cmd:cati:{self.nom_id}", chat="222")[0]
        self.assertIn("Брелок котик", card["text"])
        buttons = self._buttons(card)
        self.assertNotIn("👁 Опубликовать в витрине", buttons)
        self.assertNotIn("🗑 Удалить", buttons)
        denied = self._callback(f"cmd:cat-hide:{self.nom_id}", chat="222")
        self.assertEqual(denied, [])  # карточка не перерисовалась
        answers = [p for m, p in self.calls if m == "answerCallbackQuery"]
        self.assertTrue(any("Недоступно" in str(p) for p in answers), answers)


class CatalogClientBotIntegrationTests(unittest.TestCase):
    """Связка с клиентским ботом: скрытая позиция пропадает из его витрины."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.manager = PrinterManager(self.db, Repo(self.db))
        self.staff_bot = self.manager.bot
        self.client_bot: ClientBot = self.manager.client_bot
        self.replies: list[str] = []
        self.staff_bot._reply = lambda chat, text, buttons=None: self.replies.append(text)

    def tearDown(self):
        self.manager.shutdown()
        self.db.close()
        self._tmp.cleanup()

    def test_hidden_item_leaves_client_catalog_and_vitrine_toggle(self):
        self.staff_bot._dispatch("111", "товар Адресник 900р")
        nom_id = self.db.one("SELECT id FROM nomenclature WHERE name='Адресник'")["id"]
        Nomenclature(self.db).set_price(nom_id, 900)
        # только что созданная позиция скрыта — покупатель её не видит
        self.assertEqual([i["id"] for i in self.client_bot._catalog_rows()], [])
        self.staff_bot._dispatch("111", "показать адресник")
        self.assertIn(nom_id, [i["id"] for i in self.client_bot._catalog_rows()])
        self.staff_bot._dispatch("111", "скрыть витрину")
        self.assertIn("отключён", self.client_bot.text_catalog())
        self.staff_bot._dispatch("111", "показать витрину")
        self.assertIn("Каталог", self.client_bot.text_catalog())


if __name__ == "__main__":
    unittest.main()
