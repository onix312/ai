"""18.25: помощник продолжает разговор, как человек.

Сценарии «человека в ПК»:
  * «а за прошлую неделю?» после «Какой доход за неделю?» — тот же ответ
    для другого окна, без повторения вопроса;
  * «а по штукам?», «а клиенты?», «а доход?» — смена среза в том же окне;
  * период без темы и без контекста — живое уточнение с тремя срезами;
  * новые срезы на том же движке периода: «сколько напечатали»,
    «сколько заказов», «сколько потратили на пластик»;
  * тема и окно пишутся в мета реплики — продолжение не гадает.
"""
import datetime
import unittest

from connector.printflow import assistant_brain as brain
from connector.printflow import periods
from connector.tests.test_assistant_periods import ShopTestCase


class PeriodFromDictTests(unittest.TestCase):
    """Окно из мета реплики — тот же период, что был в ответе."""

    def test_round_trip(self):
        now = datetime.datetime(2026, 9, 25, 12, 0, 0)
        period = periods.parse("Топ товаров с 21.09 по 25.09?", now=now)
        back = periods.Period.from_dict(period.as_dict())
        self.assertIsNotNone(back)
        self.assertEqual(period.start, back.start)
        self.assertEqual(period.until, back.until)
        self.assertEqual(period.kind, back.kind)
        self.assertEqual(period.phrase, back.phrase)
        self.assertEqual(5, back.days)

    def test_garbage_meta_is_none(self):
        self.assertIsNone(periods.Period.from_dict(None))
        self.assertIsNone(periods.Period.from_dict({}))
        self.assertIsNone(periods.Period.from_dict({"start": "2026-09-21"}))
        self.assertIsNone(periods.Period.from_dict({"first": "25.09.2026", "last": "x",
                                                     "start": "2026-09-21", "until": "2026-09-25"}))


class PurePeriodTests(unittest.TestCase):
    """«а за прошлую неделю?» — период и только период."""

    def test_pure_phrases(self):
        self.assertTrue(brain._is_pure_period("А за прошлую неделю?"))
        self.assertTrue(brain._is_pure_period("сколько за вчера"))
        self.assertTrue(brain._is_pure_period("Как за 2026?"))
        self.assertTrue(brain._is_pure_period("а за эту неделю?"))
        self.assertTrue(brain._is_pure_period("за сентябрь было"))

    def test_not_pure_when_theme_present(self):
        self.assertFalse(brain._is_pure_period("За прошлую неделю доход"))
        self.assertFalse(brain._is_pure_period("а по штукам?"))
        self.assertFalse(brain._is_pure_period("Какой доход за неделю?"))
        self.assertFalse(brain._is_pure_period("Топ товаров за 2020 год"))

    def test_prev_period_question_by_kind(self):
        now = datetime.datetime(2026, 9, 25, 12, 0, 0)
        self.assertEqual("А на прошлой неделе?",
                         brain._prev_period_question(periods.parse("за неделю", now=now)))
        self.assertEqual("А в прошлом месяце?",
                         brain._prev_period_question(periods.parse("за сентябрь", now=now)))
        self.assertEqual("А вчера?",
                         brain._prev_period_question(periods.parse("доход сегодня", now=now)))
        self.assertEqual("А за 14 дней до этого?",
                      brain._prev_period_question(periods.parse("за 2 недели", now=now)))


class FollowupTests(ShopTestCase):
    """Продолжение разговора: тема и окно — из прошлой реплики помощника."""

    def test_money_followup_previous_week(self):
        self.say("Какой доход за неделю?")
        reply = self.say("А за прошлую неделю?")["reply"]
        self.assertIn("а прошлой неделе", reply)
        # Прошлый календарный понедельник в окне «прошлая неделя»: ваза ×3,
        # расход 100 → прибыль 2 900. Это и есть проверка, что окно другое.
        self.assertIn("доход 3 000 ₽", reply)
        self.assertIn("прибыль 2 900 ₽", reply)

    def test_money_followup_wider_span(self):
        self.say("Какой доход за неделю?")
        reply = self.say("а за 2 недели?")["reply"]
        # Спан 14 захватывает и прошлый понедельник: 6 500 / 600.
        self.assertIn("доход 6 500 ₽", reply)
        self.assertIn("расход 600 ₽", reply)

    def test_money_followup_explicit_range(self):
        question = f"Какой доход с {self.dm(1)} по {self.dm(0)}?"
        self.say(question)
        reply = self.say("а за прошлую неделю?")["reply"]
        self.assertIn("а прошлой неделе", reply)
        self.assertIn("доход 3 000 ₽", reply)

    def test_top_followup_rank_switch(self):
        self.say("Топ товаров за неделю?")
        reply = self.say("А по штукам?")["reply"]
        self.assertIn("по штукам", reply)
        # По штукам впереди брелок (10), хотя по сумме — ваза.
        self.assertLess(reply.index("Брелок"), reply.index("Ваза"))

    def test_top_followup_to_customers(self):
        self.db.execute(
            "INSERT INTO orders(id, number, product, customer_name, status, price,"
            " created_at, closed_at) VALUES(?,?,?,?,?,?,?,?)",
            ("ord-fu-cust", "88", "Набор органайзеров", "Мария", "done", 5000,
             self._at(3), self._at(3)))
        self.say("Топ товаров за неделю?")
        reply = self.say("а клиенты?")["reply"]
        self.assertIn("Кто покупал", reply)
        self.assertIn("Мария", reply)
        self.assertIn("5 000 ₽", reply)

    def test_followup_topic_switch_to_money(self):
        self.say("Топ товаров за неделю?")
        reply = self.say("а доход?")["reply"]
        self.assertIn("За неделю", reply)
        self.assertIn("доход 3 500 ₽", reply)

    def test_full_question_not_stolen_by_context(self):
        # После контекста полноценный вопрос отвечает тот же слой, что и без
        # контекста: сумма за 2 недели, а не повтор прошлой недели.
        self.say("Какой доход за неделю?")
        reply = self.say("Какой доход за 2 недели?")["reply"]
        self.assertIn("доход 6 500 ₽", reply)

    def test_no_change_is_not_parroted(self):
        # «а по сумме?» после топа по сумме — ничего не поменялось: слой
        # продолжения молчит, разговор идёт дальше (в модель), не зацикливаясь.
        first = self.say("Топ товаров за неделю?")
        second = self.say("А по сумме?")
        self.assertNotEqual(first["kind"], "error")
        self.assertNotIn("Топ товаров", second.get("reply") or "") \
            if second.get("source") == "facts" else None

    def test_pure_period_without_context_is_clarify(self):
        answer = self.say("А за прошлую неделю?")
        self.assertEqual("clarify", answer["kind"])
        self.assertIn("Что посчитать", answer["reply"])
        self.assertIn("доход", answer["reply"])
        suggestions = answer["suggestions"] or []
        self.assertTrue(any("доход" in s for s in suggestions))
        self.assertTrue(any("Топ товаров" in s for s in suggestions))

    def test_pure_period_then_clicked_suggestion(self):
        # Уточнение → человек выбирает срез → тот же движок считает окно.
        self.say("А за прошлую неделю?")
        reply = self.say("Какой доход на прошлой неделе?")["reply"]
        self.assertIn("доход 3 000 ₽", reply)

    def test_meta_carries_topic_and_period(self):
        from connector.printflow import assistant_memory as memory
        self.say("Какой доход за неделю?")
        turns = memory.dialog(self.db, "periods", 12)
        last = next(turn for turn in reversed(turns) if turn["role"] == "assistant")
        self.assertEqual("money", last["meta"].get("topic"))
        self.assertEqual(7, (last["meta"].get("period") or {}).get("days"))
        self.say("Топ товаров за неделю?")
        turns = memory.dialog(self.db, "periods", 12)
        last = next(turn for turn in reversed(turns) if turn["role"] == "assistant")
        self.assertEqual("top", last["meta"].get("topic"))
        self.assertEqual("products", last["meta"].get("subject"))


class PrintReportTests(ShopTestCase):
    """«Сколько напечатали?» — журнал заданий за окно."""

    def setUp(self):
        super().setUp()
        for index in range(3):  # три готовых задания сегодня: 6 ч, 1.5 кг
            self.db.execute(
                "INSERT INTO print_jobs(id, name, state, finished_at, duration_min,"
                " grams, cost, energy_kwh) VALUES(?,?,?,?,?,?,?,?)",
                (f"job-fu-{index}", "Тест", "done", self._at(0), 120, 500, 100, 1.5))
        self.db.execute(
            "INSERT INTO print_jobs(id, name, state, finished_at, duration_min,"
            " grams, cost, energy_kwh) VALUES(?,?,?,?,?,?,?,?)",
            ("job-fu-fail", "Падение", "failed", self._at(1), 60, 0, 0, 0.5))

    def test_print_report_for_period(self):
        reply = self.say("Сколько напечатали за неделю?")["reply"]
        self.assertIn("За неделю", reply)
        self.assertIn("3 задания", reply)
        self.assertIn("6.0 ч печати", reply)
        self.assertIn("1.5 кг пластика", reply)
        self.assertIn("1 неудача", reply)

    def test_print_report_default_window_names_dates(self):
        reply = self.say("Сколько напечатали?")["reply"]
        self.assertIn("За месяц", reply)
        self.assertIn("3 задания", reply)

    def test_print_followup_wider_span(self):
        self.say("Сколько напечатали за неделю?")
        reply = self.say("а за 2 недели?")["reply"]
        # Все четыре задания внутри 14 дней: 3 готовых, 1 неудачный.
        self.assertIn("3 задания", reply)
        self.assertIn("1 неудача", reply)


class SpendCategoryTests(ShopTestCase):
    """«Сколько потратили на пластик?» — расходы категории за окно."""

    def test_plastic_spending_for_period(self):
        reply = self.say("Сколько потратили на пластик за 30 дней?")["reply"]
        self.assertIn("на пластик ушло 600 ₽", reply)
        self.assertIn("2 покупки", reply)

    def test_plastic_spending_week_only_today(self):
        # Спан 7 берёт только сегодняшний расход 500; понедельник — вне окна.
        reply = self.say("Сколько ушло на пластик за неделю?")["reply"]
        self.assertIn("на пластик ушло 500 ₽", reply)
        self.assertIn("1 покупка", reply)

    def test_spend_followup_category_switch(self):
        self.say("Сколько потратили на пластик за неделю?")
        reply = self.say("а на электричество?")["reply"]
        self.assertIn("на электричество ничего не ушло", reply)

    def test_spend_followup_wider_span(self):
        self.say("Сколько потратили на пластик за неделю?")
        reply = self.say("а за 2 недели?")["reply"]
        self.assertIn("на пластик ушло 600 ₽", reply)


class OrdersCountTests(ShopTestCase):
    """«Сколько заказов за месяц?» — доска заказов за окно."""

    def setUp(self):
        super().setUp()
        self.db.execute(
            "INSERT INTO orders(id, number, product, customer_name, status, price,"
            " created_at) VALUES(?,?,?,?,?,?,?)",
            ("ord-fu-91", "91", "Ваза большая", "Иван", "new", 2000, self._at(0)))
        self.db.execute(
            "INSERT INTO orders(id, number, product, customer_name, status, price,"
            " created_at) VALUES(?,?,?,?,?,?,?)",
            ("ord-fu-92", "92", "Чехол", "Пётр", "printing", 800, self._at(0)))
        self.db.execute(
            "INSERT INTO orders(id, number, product, customer_name, status, price,"
            " created_at, closed_at) VALUES(?,?,?,?,?,?,?,?)",
            ("ord-fu-93", "93", "Брелок", "Мария", "done", 1500, self._at(1), self._at(1)))

    def test_orders_count_for_period(self):
        # Спан 7: два заказа сегодня + один вчера; «done» — финальный статус.
        reply = self.say("Сколько заказов за неделю?")["reply"]
        self.assertIn("За неделю", reply)
        self.assertIn("3 заказа", reply)
        self.assertIn("1 выдан", reply)
        self.assertIn("2 в работе", reply)

    def test_orders_count_empty_window_is_honest(self):
        reply = self.say("Сколько заказов за 2020 год?")["reply"]
        self.assertIn("новых заказов не было", reply)

    def test_orders_followup_previous_week(self):
        self.say("Сколько заказов за неделю?")
        reply = self.say("а за прошлую неделю?")["reply"]
        # В прошлой календарной неделе заказов нет — окно названо, ноль честный.
        self.assertIn("а прошлой неделе", reply)
        self.assertIn("новых заказов не было", reply)


if __name__ == "__main__":
    unittest.main()
