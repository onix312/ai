"""Понимание периода и топ продаж в мозге помощника (18.24, идеи И337–И341).

Жалоба, которую держат эти тесты. Владелец спросил:

  * «Какой доход с 21.09 по 25.09?»
  * «Какой доход за неделю?»
  * «Топ товаров за неделю?»
  * «Топ товаров которые берут за неделю?»

Помощник на первые фразы отвечал одним и тем же «За 30 дней: доход …, расход …,
прибыль …» — период во фразе не читался ни одним слоем, а «топ товаров»
превращался в дамп остатков стеллажа с ценниками: что лежит на полке, а не что
у неё купили. Модель (Ollama) в моменте была недоступна, поэтому видал не
«модель ошиблась», а «помощник не понимает по-русски».

Контракты:

  * период — из фразы, детерминированно и без модели: «с 21.09 по 25.09»,
    «за неделю», «на прошлой неделе», «за сентябрь», «в 2025 году», «вчера»;
    без периода — 30 дней, и ответ всё равно называет даты;
  * деньги — за то окно, что попросили: «доход сегодня» не равен
    «доходу за неделю»; сравнение «больше, чем прошлая?» — с окном той же длины;
  * «топ товаров / что берут / топ клиентов» — реестр продаж с границами
    фразы (та же правда, что рисует «Финансы»): «берут» — по штукам,
    «топ» — по сумме;
  * числа — из учёта (`acc.summary`/`sales_details`), не выдуманы.

Фикстура продаёт в три окна, которые не пересекаются, — сегодня, вчера и
«прошлый понедельник» (по дню недели), поэтому ожидания не зависят от дня,
в который прогоняют тест.
"""
from __future__ import annotations

import datetime
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import assistant_brain as brain  # noqa: E402
from connector.printflow import assistant_knowledge as knowledge  # noqa: E402
from connector.printflow import periods  # noqa: E402
from connector.printflow.accounting import Accounting  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402
from connector.printflow.shelf import Shelf  # noqa: E402

NOW = datetime.datetime(2026, 9, 25, 16, 0)  # пятница


def _p(text: str) -> periods.Period:
    return periods.parse(text, NOW)


class PeriodParseTests(unittest.TestCase):
    """Период из русской фразы: границы, подпись, честный признак «без периода»."""

    def test_explicit_digit_range(self):
        p = _p("Какой доход с 21.09 по 25.09?")
        self.assertTrue(p.explicit)
        self.assertEqual(("2026-09-21", "2026-09-25"), (p.first.isoformat(), p.last.isoformat()))
        self.assertEqual("с 21 по 25 сентября", p.phrase)
        # Полуоткрытое окно: начало включается, конец — нет (как ждут SQL-запросы).
        self.assertEqual("2026-09-21T00:00:00", p.start)
        self.assertEqual("2026-09-26T00:00:00", p.until)

    def test_week_and_this_week(self):
        week = _p("Какой доход за неделю?")
        self.assertTrue(week.explicit)
        self.assertEqual(7, week.days)
        self.assertEqual(NOW.date(), week.last)
        self.assertIn("за неделю", week.phrase)
        this = _p("на этой неделе сколько заработали")
        self.assertEqual("2026-09-21", this.first.isoformat())
        self.assertEqual(NOW.date(), this.last)

    def test_last_week_is_full_previous_monday_to_sunday(self):
        last = _p("доход на прошлой неделе")
        self.assertEqual(7, last.days)
        self.assertEqual("2026-09-14", last.first.isoformat())
        self.assertEqual("2026-09-20", last.last.isoformat())
        self.assertIn("на прошлой неделе", last.phrase)

    def test_month_words_and_years(self):
        september = _p("прибыль за сентябрь")
        self.assertEqual(("2026-09-01", "2026-09-25"),
                         (september.first.isoformat(), september.last.isoformat()))
        august = _p("в августе сколько продали")
        self.assertEqual(("2026-08-01", "2026-08-31"),
                         (august.first.isoformat(), august.last.isoformat()))
        old = _p("за сентябрь 2025 доход")
        self.assertEqual(("2025-09-01", "2025-09-30"),
                         (old.first.isoformat(), old.last.isoformat()))
        year = _p("доход за 2026")
        self.assertEqual(("2026-01-01", "2026-09-25"),
                         (year.first.isoformat(), year.last.isoformat()))
        prev_year = _p("в 2025 году выручка")
        self.assertEqual(("2025-01-01", "2025-12-31"),
                         (prev_year.first.isoformat(), prev_year.last.isoformat()))

    def test_single_days(self):
        self.assertEqual(NOW.date(), _p("доход сегодня").last)
        self.assertEqual("2026-09-24", _p("сколько продали вчера").first.isoformat())
        self.assertEqual("2026-09-23", _p("позавчера продажи").first.isoformat())

    def test_plain_day_range_in_current_month(self):
        p = _p("доход с 21 по 25")
        self.assertEqual(("2026-09-21", "2026-09-25"),
                         (p.first.isoformat(), p.last.isoformat()))

    def test_range_across_months_and_years(self):
        p = _p("доход с 28 по 3")
        self.assertEqual(("2026-08-28", "2026-09-03"),
                         (p.first.isoformat(), p.last.isoformat()))
        p = _p("с 25.12 по 05.01 доход")
        self.assertEqual(("2025-12-25", "2026-01-05"),
                         (p.first.isoformat(), p.last.isoformat()))

    def test_n_days_and_multipliers(self):
        self.assertEqual(10, _p("доход за 10 дней").days)
        self.assertEqual(14, _p("заработали за 2 недели").days)
        self.assertEqual(90, _p("за три месяца выручка").days)
        self.assertEqual(180, _p("за полгода продажи").days)

    def test_no_period_means_default_window(self):
        p = _p("доход")
        self.assertFalse(p.explicit)
        self.assertEqual(30, p.days)
        self.assertEqual(NOW.date(), p.last)

    def test_plastic_consumption_is_not_money(self):
        self.assertFalse(periods.money_question("расход пластика за неделю"))
        self.assertTrue(periods.money_question("Какой расход за месяц?"))

    def test_previous_window_is_adjacent_same_length(self):
        week = _p("доход за неделю")
        prev = week.previous()
        self.assertEqual(7, prev.days)
        self.assertEqual(week.first - datetime.timedelta(days=1), prev.last)
        digit = _p("доход с 21.09 по 25.09")
        self.assertEqual("2026-09-20", digit.previous().last.isoformat())
        self.assertEqual("2026-09-16", digit.previous().first.isoformat())


class TopicTests(unittest.TestCase):
    """О чём фраза: деньги или то, что покупают."""

    def test_money_phrases(self):
        for text in ("Какой доход с 21.09 по 25.09?", "Какой доход за неделю?",
                     "выручка за месяц", "прибыль за сентябрь", "маржа за квартал"):
            self.assertTrue(periods.money_question(text), text)

    def test_top_subject(self):
        self.assertEqual("products", periods.top_subject("Топ товаров за неделю?"))
        self.assertEqual("products", periods.top_subject("Топ товаров которые берут за неделю?"))
        self.assertEqual("products", periods.top_subject("что чаще всего берут?"))
        self.assertEqual("customers", periods.top_subject("топ клиентов за месяц"))
        self.assertEqual("products", periods.top_subject("топ за неделю"))
        # «Сколько продали» — про сумму, «самый большой заказ» — не про товары.
        self.assertIsNone(periods.top_subject("сколько продали за неделю"))
        self.assertIsNone(periods.top_subject("какой самый большой заказ был?"))

    def test_rank_by(self):
        self.assertEqual("qty", periods.rank_by("Топ товаров которые берут за неделю?"))
        self.assertEqual("qty", periods.rank_by("что популярнее всего за месяц?"))
        self.assertEqual("amount", periods.rank_by("Топ товаров за неделю?"))


class _EmptyManager:
    def snapshot(self):
        return {"printers": [], "queue": [], "farm": {}}


class ShopTestCase(unittest.TestCase):
    """Цех для e2e: реальные база, учёт и полка; даты — от сегодняшнего дня.

    Три Sales-окна, которые не пересекаются ни при каком дне недели:
      • сегодня (offset 0) — ваза ×2 (2 000 ₽);
      • вчера (offset 1) — брелок ×10 (1 500 ₽);
      • прошлый понедельник (offset 7+weekday) — ваза ×3 (3 000 ₽).
    Расход: сегодня 500 ₽, прошлый понедельник 100 ₽.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "periods.sqlite3")
        self.acc = Accounting(self.db)
        self.shelf = Shelf(self.db)
        self.today = datetime.date.today()
        self.weekday = self.today.weekday()  # пн=0 … вс=6

        self.vase = self.shelf.save_item({"name": "Ваза", "price": 1000, "cost_per_unit": 300})["id"]
        self.key = self.shelf.save_item({"name": "Брелок", "price": 150, "cost_per_unit": 50})["id"]
        self.shelf.produce(self.vase, 10, note="приход")
        self.shelf.produce(self.key, 30, note="приход")

        self._sell(self.vase, 2, days_ago=0)     # сегодня, 2 000 ₽
        self._sell(self.key, 10, days_ago=1)     # вчера, 1 500 ₽
        self._sell(self.vase, 3, days_ago=7 + self.weekday)  # прошлый пн, 3 000 ₽
        self.acc.add_transaction("expense", "filament", 500, "Пластик", at=self._at(0))
        self.acc.add_transaction("expense", "filament", 100, "Пластик",
                                 at=self._at(7 + self.weekday))

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _at(self, days_ago: int) -> str:
        day = self.today - datetime.timedelta(days=days_ago)
        return day.isoformat() + "T12:00:00"

    def _sell(self, item_id: str, qty: int, days_ago: int) -> None:
        move = self.shelf.sale(item_id, qty)
        at = self._at(days_ago)
        self.db.execute("UPDATE shelf_moves SET at=? WHERE id=?", (at, move["move"]["id"]))
        if move.get("tx"):
            self.db.execute("UPDATE transactions SET at=? WHERE id=?", (at, move["tx"]["id"]))

    def dm(self, days_ago: int) -> str:
        """«22.09» — дата N дней назад в формате для фразы."""
        return (self.today - datetime.timedelta(days=days_ago)).strftime("%d.%m")

    def say(self, text: str) -> dict:
        api = SimpleNamespace(db=self.db, acc=self.acc, repo=Repo(self.db),
                              shelf=self.shelf, manager=_EmptyManager())
        return brain.chat(api, text, session="periods", delegate=False)

    def api(self):
        return SimpleNamespace(db=self.db, acc=self.acc, repo=Repo(self.db),
                               shelf=self.shelf, manager=_EmptyManager())


class MoneyPeriodTests(ShopTestCase):
    """Деньги — за то окно, которое назвали в фразе."""

    def test_week_income_is_not_default_30_days(self):
        # Неделя = спан 7 (вчера+сегодня…): вазы сегодня + брелоки вчера.
        reply = self.say("Какой доход за неделю?")["reply"]
        self.assertIn("За неделю", reply)
        self.assertIn("доход 3 500 ₽", reply)
        self.assertIn("расход 500 ₽", reply)
        self.assertIn("прибыль 3 000 ₽", reply)
        self.assertNotIn("За 30 дней", reply)

    def test_today_window_differs_from_week(self):
        # «Сегодня» — только ваза (2 000), расход 500 → прибыль 1 500.
        reply = self.say("Какой доход сегодня?")["reply"]
        self.assertIn("Сегодня", reply)
        self.assertIn("доход 2 000 ₽", reply)
        self.assertIn("прибыль 1 500 ₽", reply)

    def test_explicit_two_day_range(self):
        # Вчера+сегодня — вазы и брелоки: 3 500, расход 500.
        question = f"Какой доход с {self.dm(1)} по {self.dm(0)}?"
        reply = self.say(question)["reply"]
        self.assertIn("доход 3 500 ₽", reply)
        self.assertIn("прибыль 3 000 ₽", reply)

    def test_default_window_stays_30_days_and_names_dates(self):
        reply = self.say("Какой доход?")["reply"]
        # Период не назван — 30 дней, но ответ не молчит о датах. Всё (3 500
        # + 3 000) попадает в 30 дней, расход 600.
        self.assertIn("За месяц", reply)
        self.assertIn("доход 6 500 ₽", reply)
        self.assertIn("расход 600 ₽", reply)

    def test_comparison_with_previous_period(self):
        reply = self.say("Доход за неделю больше, чем прошлая неделя?")["reply"]
        # Неделя: 3 500 − 500 = 3 000. Окно той же длины назад: ваза 3 000 −
        # 100 = 2 900. Значит «больше на 100».
        self.assertIn("прибыль 3 000 ₽", reply)
        self.assertIn("прибыль была 2 900 ₽", reply)
        self.assertIn("больше на 100 ₽", reply)

    def test_period_is_in_answer_extra(self):
        # Окно, которое помощник понял, отдаётся вызывающему — агент и бот
        # могут показать «за какое окно ответ» без повторного разбора фразы.
        answer = self.say("Какой доход за неделю?")
        period = answer.get("period") or {}
        self.assertTrue(period.get("explicit"))
        self.assertEqual(7, period.get("days"))


class TopSalesTests(ShopTestCase):
    """«Топ товаров» — что продалось в окне, а не что лежит на полке."""

    def test_top_by_amount(self):
        reply = self.say("Топ товаров за неделю?")["reply"]
        self.assertIn("Топ товаров", reply)
        # По сумме впереди ваза (2 × 1 000), хотя брелоков втрое больше.
        self.assertLess(reply.index("Ваза"), reply.index("Брелок"))
        self.assertIn("2 шт", reply)
        self.assertIn("2 000 ₽", reply)
        self.assertIn("Итого", reply)

    def test_top_by_quantity_when_asking_what_they_take(self):
        reply = self.say("Топ товаров которые берут за неделю?")["reply"]
        self.assertIn("по штукам", reply)
        self.assertLess(reply.index("Брелок"), reply.index("Ваза"))
        self.assertIn("10 шт", reply)

    def test_top_wider_window_sees_more_sales(self):
        # «За 2 недели» (спан 14) захватывает и прошлый понедельник: ваз
        # суммарно 5 шт на 5 000.
        reply = self.say("Топ товаров за 2 недели?")["reply"]
        self.assertIn("Ваза", reply)
        self.assertIn("5 шт", reply)
        self.assertIn("5 000 ₽", reply)

    def test_top_without_sales_in_window_is_honest(self):
        reply = self.say("Топ товаров за 2020 год")["reply"]
        self.assertIn("нет ни одной строки", reply)

    def test_top_customers_by_amount(self):
        self.db.execute(
            "INSERT INTO orders(id, number, product, customer_name, status, price,"
            " created_at, closed_at) VALUES(?,?,?,?,?,?,?,?)",
            ("ord-test-top", "77", "Набор органайзеров", "Мария", "done", 5000,
             self._at(3), self._at(3)))
        reply = self.say("Топ клиентов за 30 дней")["reply"]
        self.assertIn("Мария", reply)
        self.assertIn("5 000 ₽", reply)
        # Продажи с полки без имени клиента в топ клиентов не лезут.
        self.assertNotIn("Без имени", reply)


class KnowledgeFactsPeriodTests(ShopTestCase):
    """Факты для модели тоже читают период из вопроса, а не 30 дней по привычке."""

    def test_money_fact_carries_the_phrase_period(self):
        question = f"Какой доход с {self.dm(1)} по {self.dm(0)}?"
        found = knowledge.answer(self.api(), question, fast=True)
        self.assertTrue(found["answered"], found.get("reason"))
        self.assertIn("Финансы с", found["answer"])
        self.assertNotIn("Финансы за 30 дней", found["answer"])
        self.assertIn("доход 3500", found["answer"])


if __name__ == "__main__":
    unittest.main()
