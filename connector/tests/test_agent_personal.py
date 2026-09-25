"""Личный помощник и обучение агента (18.22, идеи И316–И331).

Что держат эти контракты:

  * время по-русски понимается без модели: «через 20 минут», «завтра в 10»,
    «в пятницу вечером», «по будням в 8:30», «каждые 2 часа»; уже прошедшее
    не превращается молча в завтрашнее;
  * планировщик сам поднимает напоминания, закончившиеся таймеры, заметки со
    сроком и привычки — ровно одно уведомление на срабатывание;
  * списки, цели с темпом, привычки с сериями, расходы с категориями, дневник;
  * обучение: урок, поправка, «это значит …» после непонятого, синонимы,
    самообучение на плане модели с шаблоном чисел, 👍/👎; самовыученное не
    перетирает урок, противоположный глагол не находит выученное;
  * выученная команда проходит тот же реестр и то же подтверждение;
  * окно агента показывает дела, обучение, оценки и уведомления, не нарушая
    правил экранирования.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent import brain, config, executor, intents, learning, model, server, skills, ui, when  # noqa: E402
from agent.panel_client import Client  # noqa: E402
from agent.personal import Personal, money, split_items  # noqa: E402
from agent.store import Store  # noqa: E402

DEAD_PANEL = "http://127.0.0.1:1"
FRIDAY = dt.datetime(2026, 9, 25, 12, 30)  # пятница, полдень
MODEL_DOWN = {"ok": False, "reason": "модели нет"}


class TempStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self._tmp.name) / "a.sqlite3")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# Время по-русски
# ---------------------------------------------------------------------------

class WhenParseTests(unittest.TestCase):
    def at(self, text, now=FRIDAY):
        found = when.parse(text, now)
        self.assertIsNotNone(found, text)
        return found

    def test_relative_time(self):
        self.assertEqual(dt.datetime(2026, 9, 25, 12, 50), self.at("напомни через 20 минут выключить чайник")["at"])
        self.assertEqual(dt.datetime(2026, 9, 25, 13, 0), self.at("через полчаса проверить печать")["at"])
        self.assertEqual(dt.datetime(2026, 9, 25, 14, 0), self.at("через 1,5 часа снять деталь")["at"])
        self.assertEqual(dt.datetime(2026, 9, 25, 12, 55), self.at("через двадцать пять минут")["at"])

    def test_text_is_what_is_left(self):
        found = self.at("напомни мне завтра в 10 утра позвонить маме")
        self.assertEqual(("позвонить маме", dt.datetime(2026, 9, 26, 10, 0)), (found["text"], found["at"]))
        self.assertEqual("забрать 2 заказа", self.at("напомни в 5 забрать 2 заказа")["text"])

    def test_part_of_day_and_words(self):
        self.assertEqual(dt.datetime(2026, 9, 25, 19, 0), self.at("напомни в семь вечера позвонить Ане")["at"])
        self.assertEqual(dt.datetime(2026, 9, 25, 19, 30), self.at("в половине восьмого вечера включить сушилку")["at"])
        self.assertEqual(dt.datetime(2026, 9, 26, 8, 0), self.at("утром в 8 отправить посылку")["at"])
        self.assertEqual(dt.datetime(2026, 9, 25, 13, 0), self.at("в час дня обед")["at"])

    def test_unqualified_past_hour_means_evening(self):
        # «в 7», сказанное в полдень, — это 19:00 сегодня, а не 7 утра завтра.
        self.assertEqual(dt.datetime(2026, 9, 25, 19, 0), self.at("напомни в 7 полить цветы")["at"])

    def test_same_weekday_after_the_hour_is_next_week(self):
        self.assertEqual(dt.datetime(2026, 10, 2, 10, 0), self.at("напомни в пятницу в 10 сдать отчёт")["at"])
        self.assertEqual(dt.datetime(2026, 9, 25, 19, 0), self.at("в пятницу вечером про оплату")["at"])

    def test_dates(self):
        self.assertEqual(dt.datetime(2026, 10, 1, 14, 0), self.at("1 октября в 14:00 созвон с Петей")["at"])
        self.assertEqual(dt.datetime(2026, 12, 25, 9, 0), self.at("25.12 поздравить Олю")["at"])
        self.assertEqual(dt.datetime(2027, 3, 1, 9, 0), self.at("1 марта купить цветы")["at"])

    def test_repeats(self):
        found = self.at("по будням в 8:30 делать зарядку")
        self.assertEqual(("weekdays", dt.datetime(2026, 9, 28, 8, 30)), (found["repeat"], found["at"]))
        self.assertEqual("weekly:4", self.at("каждую пятницу в 18 выносить мусор")["repeat"])
        self.assertEqual("every:120", self.at("каждые 2 часа пить воду")["repeat"])
        self.assertEqual("monthly:5", self.at("каждое 5 число платить за квартиру")["repeat"])
        daily = self.at("каждый день в 9 таблетки")
        self.assertEqual(("daily", "каждый день в 09:00"), (daily["repeat"], daily["label"]))

    def test_advance_skips_to_next_matching_day(self):
        morning = dt.datetime(2026, 9, 25, 8, 30)
        self.assertEqual(dt.datetime(2026, 9, 28, 8, 30), when.advance(morning, "weekdays", FRIDAY))
        self.assertEqual(dt.datetime(2026, 9, 25, 14, 0), when.advance(dt.datetime(2026, 9, 25, 10, 0), "every:120", FRIDAY))
        self.assertEqual(dt.datetime(2026, 10, 31, 9, 0), when.advance(dt.datetime(2026, 9, 30, 9, 0), "monthly:31",
                                                                       dt.datetime(2026, 10, 1)))
        self.assertIsNone(when.advance(FRIDAY, "", FRIDAY))

    def test_past_is_marked_not_moved(self):
        self.assertTrue(self.at("напомни сегодня в 8 утра")["past"])

    def test_no_time_is_none(self):
        self.assertIsNone(when.parse("напомни позвонить маме", FRIDAY))
        self.assertIsNone(when.parse("напомни про клиента Иванова", FRIDAY))

    def test_labels(self):
        self.assertEqual("через 20 минут, в 12:50", when.label(dt.datetime(2026, 9, 25, 12, 50), "", FRIDAY))
        self.assertEqual("завтра в 10:00", when.label(dt.datetime(2026, 9, 26, 10, 0), "", FRIDAY))
        self.assertEqual("каждые 2 часа", when.repeat_label("every:120"))

    def test_deadlines(self):
        today = FRIDAY.date()
        self.assertEqual(dt.date(2026, 12, 31), when.target_date("12 книг до конца года", today)[0])
        self.assertEqual(dt.date(2026, 12, 1), when.target_date("100 км к 1 декабря", today)[0])
        self.assertEqual(dt.date(2027, 1, 1), when.target_date("до нового года", today)[0])
        self.assertEqual(dt.date(2026, 12, 31), when.target_date("к новому году", today, new_year_is_first=False)[0])
        self.assertEqual(dt.date(2027, 6, 1), when.target_date("накопить к лету", today)[0])


# ---------------------------------------------------------------------------
# Личные данные и планировщик
# ---------------------------------------------------------------------------

class PersonalTests(TempStore):
    def setUp(self):
        super().setUp()
        self.me = Personal(self.store)

    def test_one_off_reminder_fires_once_and_waits_for_done(self):
        row = self.me.add_reminder("выключить чайник", FRIDAY + dt.timedelta(minutes=5))
        self.assertEqual([], self.me.tick(FRIDAY))
        fired = self.me.tick(FRIDAY + dt.timedelta(minutes=6))
        self.assertEqual([("reminder", "выключить чайник")], [(n["kind"], n["text"]) for n in fired])
        self.assertEqual([], self.me.tick(FRIDAY + dt.timedelta(minutes=7)), "второй раз не звонит")
        self.assertEqual("fired", self.me.reminder(row["id"])["status"])
        self.assertTrue(self.me.complete_reminder(row["id"]))
        self.assertEqual("done", self.me.reminder(row["id"])["status"])

    def test_repeat_moves_to_next_time(self):
        row = self.me.add_reminder("зарядка", dt.datetime(2026, 9, 25, 8, 30), "weekdays")
        fired = self.me.tick(dt.datetime(2026, 9, 25, 8, 31))
        self.assertIn("Следующий раз", fired[0]["text"])
        fresh = self.me.reminder(row["id"])
        self.assertEqual(("active", "2026-09-28 08:30:00"), (fresh["status"], fresh["due_at"]))

    def test_missed_reminder_is_marked(self):
        self.me.add_reminder("оплатить счёт", dt.datetime(2026, 9, 24, 10, 0))
        fired = self.me.tick(FRIDAY)
        self.assertTrue(fired[0]["text"].startswith("Пропущено (было на 24.09 10:00)"), fired[0]["text"])

    def test_snooze_and_cancel(self):
        row = self.me.add_reminder("позвонить", FRIDAY - dt.timedelta(minutes=1))
        self.me.tick(FRIDAY)
        snoozed = self.me.snooze_reminder(row["id"], 10, FRIDAY)
        self.assertEqual(("active", "2026-09-25 12:40:00"), (snoozed["status"], snoozed["due_at"]))
        self.assertTrue(self.me.cancel_reminder(row["id"]))
        self.assertEqual([], self.me.tick(FRIDAY + dt.timedelta(hours=1)))

    def test_focus_timer_and_dated_note_ring(self):
        self.store._run("INSERT INTO focus_timers(at, duration_min, status, end_at, note) VALUES(?,?,?,?,?)",
                        ("2026-09-25 12:00:00", 25, "running", "2026-09-25 12:25:00", "отчёт"))
        self.store.add_note("купить плёнку", "2026-09-25 12:10")
        kinds = {n["kind"]: n["text"] for n in self.me.tick(FRIDAY)}
        self.assertEqual("Прошло 25 минут: отчёт", kinds["timer"])
        self.assertEqual("купить плёнку", kinds["note"])
        self.assertEqual([], self.me.tick(FRIDAY + dt.timedelta(minutes=1)))

    def test_habit_streak_and_single_nudge(self):
        habit = self.me.add_habit("зарядка", "08:00")
        self.store._run("UPDATE habits SET at='2026-09-01 07:00:00' WHERE id=?", (habit["id"],))
        for offset in (3, 2, 1):
            self.me.check_habit(habit["id"], FRIDAY.date() - dt.timedelta(days=offset))
        self.assertEqual(3, self.me.streak(habit["id"], FRIDAY.date()), "сегодняшний пропуск ещё не рвёт серию")
        nudges = [n for n in self.me.tick(FRIDAY) if n["kind"] == "habit"]
        self.assertEqual(1, len(nudges))
        self.assertIn("Серия — 3 дня", nudges[0]["text"])
        self.assertEqual([], [n for n in self.me.tick(FRIDAY + dt.timedelta(minutes=5)) if n["kind"] == "habit"])
        self.assertEqual(4, self.me.check_habit(habit["id"], FRIDAY.date())["streak"])

    def test_habit_created_after_its_hour_is_not_nudged_today(self):
        habit = self.me.add_habit("вода", "08:00")
        self.store._run("UPDATE habits SET at='2026-09-25 11:00:00' WHERE id=?", (habit["id"],))
        self.assertEqual([], [n for n in self.me.tick(FRIDAY) if n["kind"] == "habit"])

    def test_lists(self):
        added = self.me.add_items("покупок", split_items("молоко, хлеб и яйца"))
        self.assertEqual(("покупки", ["молоко", "хлеб", "яйца"]), (added["list"], added["added"]))
        self.assertEqual(["Молоко"], self.me.add_items("покупки", ["Молоко"])["repeated"], "повтор не плодит дубль")
        self.assertEqual("хлеб", self.me.remove_item("хлеба")["item"], "пункт ищется по основе слова")
        self.assertIsNone(self.me.remove_item("звук"))
        self.assertEqual(2, self.me.clear_list("покупки"))

    def test_goal_pace(self):
        goal = self.me.add_goal("прочитать 12 книг", 12, "книг", dt.date(2026, 12, 31))
        self.store._run("UPDATE goals SET at='2026-01-01 09:00:00' WHERE id=?", (goal["id"],))
        behind = self.me.goal_view(self.me.progress_goal(goal["id"], 3), FRIDAY.date())
        self.assertEqual((3.0, 25, True), (behind["progress"], behind["percent"], behind["behind"]))
        self.assertIn("отстаёте", behind["text"])
        done = self.me.progress_goal(goal["id"], 12, absolute=True)
        self.assertEqual("done", done["status"])
        self.assertIn("достигнута", self.me.goal_view(done, FRIDAY.date())["text"])

    def test_expenses_categories_and_learned_override(self):
        self.assertEqual("транспорт", self.me.add_expense(450, "такси", day=FRIDAY.date())["category"])
        self.assertEqual("кафе", self.me.add_expense(300, "кофе", day=FRIDAY.date())["category"])
        self.me.add_expense(200, "кофе с собой", "перекусы", day=FRIDAY.date())
        self.assertEqual("перекусы", self.me.categorize("кофе"), "названная категория важнее словаря")
        report = self.me.expenses(FRIDAY.date(), FRIDAY.date())
        self.assertEqual((950.0, 3), (report["total"], report["count"]))
        self.assertEqual("1 200 ₽", money(1200))

    def test_diary_mood(self):
        self.assertEqual(8, Personal.mood_of("настроение 8 из 10"))
        self.assertEqual(9, Personal.mood_of("сегодня был отличный день"))
        self.me.add_diary("ездил на дачу", 7, FRIDAY.date())
        self.assertEqual("ездил на дачу", self.me.diary(day=FRIDAY.date())[0]["text"])

    def test_notifications_are_marked_seen(self):
        note = self.me.notify("reminder", "Напоминание", "текст", 1)
        self.assertEqual([note["id"]], [row["id"] for row in self.me.unseen()])
        self.assertEqual(1, self.me.mark_seen([note["id"], "oops"]))
        self.assertEqual([], self.me.unseen())


# ---------------------------------------------------------------------------
# Обучение
# ---------------------------------------------------------------------------

class LearningTests(TempStore):
    def setUp(self):
        super().setUp()
        self.learn = learning.Learning(self.store)

    def test_phrase_norm_drops_politeness(self):
        self.assertEqual("включи рабочий режим", learning.norm_phrase("Ну, включи рабочий режим, пожалуйста!"))

    def test_exact_and_fuzzy_match_but_not_the_opposite(self):
        self.learn.teach("рабочий режим", [{"skill": "system.volume", "params": {"level": 30}}], meaning="громкость 30")
        self.assertEqual("точно", self.learn.match("Рабочий режим!")["how"])
        self.assertEqual("по смыслу слов", self.learn.match("включи рабочий режим")["how"])
        self.assertIsNone(self.learn.match("выключи рабочий режим"), "противоположный глагол не находит урок")
        self.learn.teach("свет", [{"skill": "system.volume", "params": {"level": 10}}])
        self.assertIsNone(self.learn.match("выключи свет"), "короткое выученное — только точно")

    def test_numbers_become_a_template_for_self_learned(self):
        self.learn.teach("сделай звук на 40", [{"skill": "system.volume", "params": {"level": 40}}], source="self")
        found = self.learn.match("сделай звук на 70")
        self.assertEqual(("по шаблону", {"level": 70}), (found["how"], found["steps"][0]["params"]))

    def test_self_does_not_overwrite_lesson(self):
        self.learn.teach("кофе", [{"skill": "scheduler.focus_timer", "params": {"minutes": 15}}], source="taught")
        kept = self.learn.teach("кофе", [{"skill": "system.volume", "params": {"level": 5}}], source="self")
        self.assertFalse(kept["ok"])
        self.assertEqual("scheduler.focus_timer", self.learn.match("кофе")["steps"][0]["skill"])
        fixed = self.learn.teach("кофе", [{"skill": "list.add", "params": {"items": "кофе"}}], source="correction")
        self.assertTrue(fixed["ok"] and fixed["replaced"])

    def test_bad_marks_switch_off_learned(self):
        own = self.learn.teach("звук на 5", [{"skill": "system.volume", "params": {"level": 5}}], source="self")["entry"]
        self.assertEqual(0, self.learn.bad(own["id"])["active"], "самовыученное гаснет от первого минуса")
        lesson = self.learn.teach("обед", [{"skill": "scheduler.focus_timer", "params": {"minutes": 45}}])["entry"]
        self.assertEqual(1, self.learn.bad(lesson["id"])["active"])
        self.assertEqual(0, self.learn.bad(lesson["id"])["active"], "урок — после двух минусов")

    def test_unknown_phrases_resolve_when_taught(self):
        self.learn.note_unknown("квазимодо бла")
        self.learn.note_unknown("Квазимодо бла!")
        self.assertEqual([("квазимодо бла", 2)], [(row["text"], row["count"]) for row in self.learn.unknowns()])
        self.learn.teach("квазимодо бла", [{"say": "мои цели"}])
        self.assertEqual([], self.learn.unknowns())

    def test_aliases_by_word_stem(self):
        self.assertTrue(self.learn.set_alias("телега", "телеграм")["ok"])
        text, applied = self.learn.apply_aliases("открой телегу")
        self.assertEqual(("открой телеграм", [("телега", "телеграм")]), (text, applied))
        self.assertFalse(self.learn.set_alias("очень длинная фраза синонима", "x")["ok"])

    def test_habits_become_suggestions_not_actions(self):
        for day in (21, 22, 23, 24):
            self.learn.record_usage("app.open", {"target": "telegram"}, "открой телеграм", dt.datetime(2026, 9, day, 9, 5))
        insight = self.learn.insights(dt.datetime(2026, 9, 25, 9, 10))[0]
        self.assertEqual((9, 4), (insight["hour"], insight["near"]))
        self.assertEqual(["открой телеграм"], self.learn.suggestions(dt.datetime(2026, 9, 25, 9, 10)))
        self.assertEqual([], self.learn.suggestions(dt.datetime(2026, 9, 25, 15, 0)), "не в свой час — не подсказываем")
        self.learn.record_usage("agent.why", {}, "почему", dt.datetime(2026, 9, 25, 9, 0))
        self.assertNotIn("agent.why", [item["skill"] for item in self.learn.insights(dt.datetime(2026, 9, 25, 9, 10))])


# ---------------------------------------------------------------------------
# Намерения без модели
# ---------------------------------------------------------------------------

class IntentTests(TempStore):
    def setUp(self):
        super().setUp()
        self.me = Personal(self.store)

    def intent(self, text):
        return intents.personal_intent(brain.normalize_phrase(text), self.me, FRIDAY)

    def test_lessons(self):
        lesson = intents.teach_command("научись: когда я говорю «рабочий режим», открой телеграм и громкость 30")
        self.assertEqual(("teach", "рабочий режим", True), (lesson["op"], lesson["phrase"], lesson["explicit"]))
        self.assertEqual(["открой телеграм", "громкость 30"], intents.split_meaning(lesson["meaning"]))
        self.assertEqual("answer", intents.teach_command("когда я спрашиваю как дела у цеха, отвечай всё отлично")["op"])
        self.assertFalse(intents.teach_command("«Рыжик» — это клиент из Питера")["explicit"])
        self.assertEqual({"op": "forget", "phrase": "рабочий режим"}, intents.teach_command("забудь команду рабочий режим"))
        self.assertEqual("list", intents.teach_command("чему ты научился")["op"])
        self.assertIsNone(intents.teach_command("открой блокнот"))

    def test_corrections(self):
        self.assertEqual({"meaning": "громкость 25", "marked": True}, intents.correction("нет, я имел в виду громкость 25"))
        self.assertEqual({"meaning": "", "marked": False}, intents.correction("не то"))
        self.assertFalse(intents.correction("нет звука")["marked"])

    def test_reminders(self):
        plan = self.intent("напомни завтра в 10 позвонить маме")
        self.assertEqual(("reminder.add", {"text": "позвонить маме", "due": "2026-09-26 10:00:00"}),
                         (plan["skill"], plan["params"]))
        asked = self.intent("напомни позвонить маме")
        self.assertEqual({"kind": "remind_when", "text": "позвонить маме"}, asked["awaiting"])
        self.assertIsNone(self.intent("напомни про клиента Иванова"), "это вопрос к памяти и панели")
        self.assertEqual("remind_text", self.intent("напомни через час")["awaiting"]["kind"])

    def test_lists_and_guards(self):
        self.assertEqual({"list": "покупки", "items": "молоко, яйца, сыр"},
                         self.intent("добавь молоко, яйца и сыр в список покупок")["params"])
        self.assertEqual("list.add", self.intent("надо купить батарейки")["skill"])
        self.assertIsNone(self.intent("убери звук"), "команду компьютеру не съедает список")
        self.me.add_items("покупки", ["молоко"])
        self.assertEqual("list.remove", self.intent("купил молоко")["skill"])

    def test_money_goals_habits_diary(self):
        self.assertEqual({"amount": 3000.0, "note": "интернет", "day": "2026-09-24"},
                         self.intent("вчера заплатил 3000 рублей за интернет")["params"])
        self.assertEqual("транспорт", self.intent("траты на такси за месяц")["params"]["category"])
        goal = self.intent("моя цель — пробежать 100 км до 1 декабря")["params"]
        self.assertEqual((100.0, "км", "2026-12-01"), (goal["target"], goal["unit"], goal["deadline"]))
        self.assertEqual("08:00", self.intent("новая привычка: зарядка в 8")["params"]["remind_at"],
                         "час привычки буквальный, даже если сказано в полдень")
        self.me.add_habit("зарядка")
        self.assertEqual("habit.check", self.intent("я сделал зарядку")["skill"])
        self.assertEqual(9, self.intent("запиши в дневник: сегодня был отличный день")["params"]["mood"])
        self.assertEqual("me.today", self.intent("доброе утро")["skill"])

    def test_small_calculations(self):
        self.assertEqual("До 31 декабря — 97 дней (13 недель и 6 дней).",
                         intents.util_answer("сколько дней до 31 декабря", FRIDAY)[0])
        self.assertEqual("1 декабря — вторник.", intents.util_answer("какой день недели 1 декабря", FRIDAY)[0])
        self.assertEqual("5 км = 3,11 мили.", intents.util_answer("5 км в милях", FRIDAY)[0])
        self.assertEqual("32 °F = 0 °C.", intents.util_answer("32 фаренгейта в цельсии", FRIDAY)[0])
        with patch.object(intents._RANDOM, "random", return_value=0.1):
            self.assertEqual("Орёл.", intents.util_answer("подбрось монетку", FRIDAY)[0])
        self.assertIsNone(intents.util_answer("который час", FRIDAY))


# ---------------------------------------------------------------------------
# Мозг: уроки, поправки, личное — целиком
# ---------------------------------------------------------------------------

class FakeAgent:
    def __init__(self, store):
        self.runner = executor.Runner(store=store, panel=Client(DEAD_PANEL))
        self.calls = []

    def run_skill(self, name, params, ask=True):
        self.calls.append((name, params))
        skill = skills.get(name)
        if skill and skills.confirm_required(skill):
            return {"ok": True, "queued": True, "id": "abc", "text": executor.describe(skill, params)}
        return self.runner.run(name, params)


class BrainLearningTests(TempStore):
    def setUp(self):
        super().setUp()
        self.agent = FakeAgent(self.store)
        self.brain = brain.Brain(self.agent, clock=lambda: FRIDAY)

    def say(self, text, status=MODEL_DOWN, mode="full"):
        with patch.object(model, "status", return_value=status):
            return self.brain.chat(text, session="window", mode=mode)

    def skills_called(self):
        return [name for name, _params in self.agent.calls]

    def test_lesson_with_two_steps_runs_through_registry(self):
        taught = self.say("научись: когда я говорю «кофе-брейк», поставь таймер на 15 минут и добавь чай в покупки")
        self.assertEqual("teach", taught["source"])
        answer = self.say("кофе-брейк")
        self.assertEqual("learned", answer["source"])
        self.assertEqual(["scheduler.focus_timer", "list.add"], self.skills_called())
        self.assertEqual({"minutes": 15, "note": ""}, self.agent.calls[0][1])
        self.assertEqual(1, self.agent.runner.learning.entries()[0]["uses"])

    def test_learned_command_still_needs_confirmation(self):
        self.say("научись: когда я говорю «уборка», очисти список покупок")
        answer = self.say("уборка")
        self.assertEqual("pending", answer["kind"], "обучение не открывает обход подтверждения")

    def test_fact_with_dash_is_not_a_lesson(self):
        self.say("«Рыжик» — это клиент из Питера")
        self.assertEqual([], self.agent.runner.learning.entries())

    def test_alias_is_applied_to_commands(self):
        self.assertIn("телеграм", self.say("«телега» — это телеграм")["reply"])
        self.say("открой телегу")
        self.assertEqual(("app.open", {"target": "телеграм"}), self.agent.calls[-1])

    def test_teach_after_a_miss(self):
        missed = self.say("квазимодо бла")
        self.assertIn("это значит", missed["reply"])
        self.assertEqual({"kind": "teach", "phrase": "квазимодо бла"}, missed["awaiting"])
        self.say("новая привычка: зарядка")
        self.say("квазимодо бла")  # всё ещё не понято
        answer = self.say("это значит мои привычки")
        self.assertIn("Запомнил", answer["reply"])
        self.assertEqual("habit.list", answer["skill"])
        self.assertEqual("habit.list", self.say("квазимодо бла")["skill"])
        self.assertEqual([], self.agent.runner.learning.unknowns())

    def test_moving_on_after_a_miss_teaches_nothing(self):
        self.say("квазимодо бла")
        self.say("мои цели")
        self.assertEqual([], self.agent.runner.learning.entries())

    def test_correction_overrides_rules_next_time(self):
        self.say("мои цели")
        fixed = self.say("нет, я имел в виду мои привычки")
        self.assertIn("Запомнил", fixed["reply"])
        again = self.say("мои цели")
        self.assertEqual(("habit.list", "learned"), (again["skill"], again["source"]))

    def test_bare_no_asks_what_was_meant(self):
        self.say("мои цели")
        asked = self.say("не то")
        self.assertEqual({"kind": "correction", "phrase": "мои цели"}, asked["awaiting"])
        self.assertEqual("habit.list", self.say("мои привычки")["skill"])
        self.assertEqual("correction", self.agent.runner.learning.entries()[0]["source"])

    def test_complaint_is_not_a_correction(self):
        self.say("мои цели")
        self.say("нет звука")
        self.assertEqual([], self.agent.runner.learning.entries())

    def test_reminder_clarification_is_answered_next_turn(self):
        asked = self.say("напомни позвонить маме")
        self.assertEqual("clarify", asked["kind"])
        done = self.say("завтра в 10")
        self.assertEqual("Напомню завтра в 10:00: позвонить маме.", done["reply"])
        row = self.agent.runner.personal.reminders()[0]
        self.assertEqual(("позвонить маме", "2026-09-26 10:00:00"), (row["text"], row["due_at"]))

    def test_personal_flows_answer_in_words(self):
        self.assertIn("Добавил в «покупки»", self.say("добавь молоко и хлеб в список покупок")["reply"])
        self.assertIn("Вычеркнул: хлеб", self.say("купил хлеб")["reply"])
        self.assertIn("Цель записана", self.say("моя цель — прочитать 12 книг до конца года")["reply"])
        self.assertIn("3 из 12", self.say("прочитал 3 книги")["reply"])
        self.assertIn("450 ₽ — такси (транспорт)", self.say("потратил 450 на такси")["reply"])
        self.assertTrue(self.say("доброе утро")["reply"].startswith("Доброе утро! Сегодня пятница, 25 сентября."))
        self.assertEqual("util", self.say("сколько дней до нового года")["source"])
        self.say("напомни в 18 полить цветы")
        self.assertIn("По личному: на сегодня 18:00 — полить цветы", self.say("привет")["reply"])

    def test_personal_commands_work_for_the_panel_too(self):
        answer = self.say("напомни завтра в 10 позвонить маме", mode="pc")
        self.assertTrue(answer["handled"])
        self.assertEqual("reminder.add", answer["skill"])
        self.assertFalse(self.say("какая погода в Москве", mode="pc")["handled"])

    def test_model_plan_is_learned_and_reused_without_model(self):
        plan = {"skill": "scheduler.focus_timer", "params": {"minutes": 25}, "reply": "Ставлю"}
        with patch.object(model, "chat", return_value={"ok": True, "text": json.dumps(plan), "model": "m"}):
            first = self.say("таймер помидорка 25", status={"ok": True, "model": "m", "reason": ""})
        self.assertTrue(first.get("learned"))
        with patch.object(model, "chat", side_effect=AssertionError("модель не нужна")):
            again = self.say("таймер помидорка 40")
        self.assertEqual(("learned", {"minutes": 40}), (again["source"], self.agent.calls[-1][1]))

    def test_thumbs(self):
        self.say("научись: когда я говорю «обед», поставь таймер на 45 минут")
        answer = self.say("обед")
        self.assertTrue(answer["turn_id"])
        self.assertTrue(self.brain.feedback(answer["turn_id"], 1)["ok"])
        entry = self.agent.runner.learning.entries()[0]
        self.assertEqual(1, entry["good"])
        down = self.brain.feedback(answer["turn_id"], -1)
        self.assertIn("Что нужно было сделать", down["message"]["reply"])
        self.assertEqual("list.show", self.say("мои списки")["skill"])
        self.assertEqual("correction", self.agent.runner.learning.match("обед")["entry"]["source"])
        self.assertFalse(self.brain.feedback(999999, 1)["ok"])

    def test_learning_questions_are_skills(self):
        self.say("квазимодо бла")
        self.assertIn("квазимодо", self.say("что ты не понял?")["reply"])
        self.say("научись: когда я говорю «обед», поставь таймер на 45 минут")
        self.assertIn("«обед»", self.say("чему ты научился")["reply"])
        self.assertIn("Забыл", self.say("забудь команду обед")["reply"])


class VoiceLoopTests(TempStore):
    def test_voice_keeps_the_assistants_own_question(self):
        """«Когда напомнить?» голосом не перебивается повторным вопросом к панели."""
        agent = server.Agent()
        agent._runner = executor.Runner(store=self.store, panel=Client(DEAD_PANEL))
        agent.capabilities = {"speech_out": False}
        question = {"kind": "clarify", "reply": "Когда напомнить?", "source": "personal",
                    "awaiting": {"kind": "remind_when", "text": "позвонить маме"}}
        with patch.object(agent, "chat", return_value=question), patch.object(agent._runner.panel, "chat") as asked:
            answer = agent.voice_phrase("напомни позвонить маме")
        asked.assert_not_called()
        self.assertEqual("Когда напомнить?", answer["reply"])


class NoteBecomesReminderTests(TempStore):
    def test_voice_note_with_due_rings(self):
        runner = executor.Runner(store=self.store, panel=Client(DEAD_PANEL))
        runner.clock = lambda: FRIDAY
        result = runner.run("voice.note", {"text": "купить плёнку", "due": "завтра в 9"})
        self.assertEqual("Заметка сохранена, напомню завтра в 09:00.", brain.summarize("voice.note", result))
        self.assertEqual("2026-09-26 09:00:00", runner.personal.reminders()[0]["due_at"])


# ---------------------------------------------------------------------------
# Сервер: планировщик и маршруты
# ---------------------------------------------------------------------------

class ServerPersonalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        path = pathlib.Path(cls._tmp.name) / "a.sqlite3"
        cls._env = patch.dict(os.environ, {"PRINTFLOW_ASSISTANT_DB": str(path)})
        cls._env.start()
        cls._ports = patch.multiple(config, SPEECH_PORT=0, AGENT_PORT=0, PRINTFLOW_URL=DEAD_PANEL)
        cls._ports.start()
        cls.agent = server.Agent()
        cls.agent._runner = executor.Runner(store=Store(path), panel=Client(DEAD_PANEL))
        cls.speech, cls.server = server.serve(cls.agent)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.speech.server_close()
        cls._ports.stop()
        cls._env.stop()
        cls._tmp.cleanup()

    def request(self, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                         method="POST" if data is not None else "GET",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as answer:
            return json.loads(answer.read() or b"{}")

    def test_tick_turns_due_reminder_into_notification(self):
        personal = self.agent.runner.personal
        row = personal.add_reminder("проверить печать", dt.datetime.now() - dt.timedelta(minutes=1))
        with patch.object(server.pc, "speak", return_value=({}, "")):
            fired = self.agent.tick()
        self.assertIn("проверить печать", [item["text"] for item in fired])
        notes = self.request("/notifications")["notifications"]
        self.assertIn(row["id"], [item["ref_id"] for item in notes if item["kind"] == "reminder"])
        self.assertTrue(self.request("/notifications/seen", {"ids": [item["id"] for item in notes]})["ok"])
        self.assertEqual([], self.request("/notifications")["notifications"])
        done = self.request("/personal", {"op": "reminder_done", "id": row["id"]})
        self.assertTrue(done["ok"])
        self.assertIn("сделано", done["say"])

    def test_life_and_learning_routes(self):
        self.request("/chat", {"text": "новая привычка: растяжка", "session": "r"})
        life = self.request("/personal")
        habit = next(item for item in life["habits"] if item["title"] == "растяжка")
        self.assertTrue(self.request("/personal", {"op": "habit_check", "id": habit["id"]})["ok"])
        self.assertTrue(next(item for item in self.request("/personal")["habits"]
                             if item["title"] == "растяжка")["done_today"])
        taught = self.request("/learning", {"op": "teach", "phrase": "перерыв", "meaning": "поставь таймер на 10 минут"})
        self.assertIn("Запомнил: «перерыв»", taught["say"])
        payload = self.request("/learning")
        entry = next(item for item in payload["learned"] if item["phrase"] == "перерыв")
        self.assertEqual("поставь таймер на 10 минут", entry["meaning_text"])
        self.assertTrue(self.request("/learning", {"op": "forget", "id": entry["id"]})["ok"])
        self.assertFalse(self.request("/personal", {"op": "nonsense"})["ok"])

    def test_feedback_route(self):
        answer = self.request("/chat", {"text": "мои цели", "session": "f"})
        self.assertTrue(answer["turn_id"])
        down = self.request("/feedback", {"turn_id": answer["turn_id"], "rating": -1})
        self.assertIn("Что нужно было сделать", down["message"]["reply"])


class WindowLifeTests(unittest.TestCase):
    def test_page_has_life_learning_feedback_and_notifications(self):
        page = ui.page()
        for needle in ('id="pane-life"', 'id="pane-learn"', "'/feedback'", "'/notifications'", "'/personal'",
                       "'/learning'", 'id="toasts"', 'id="teach-save"'):
            self.assertIn(needle, page)

    def test_new_scripts_keep_escaping_rules(self):
        script = ui.page().split("<script>")[-1]
        self.assertNotIn("${", script)
        for line in script.splitlines():
            if ".innerHTML" in line and "row." in line:
                self.assertIn("esc(", line, line.strip())


if __name__ == "__main__":
    unittest.main()
