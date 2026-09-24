"""Мозг помощника панели (18.21, идеи И305–И310, И315): память, контекст, сущности цеха.

Контракты здесь про то, чему владелец доверяет в разговоре:

  * память живёт в базе цеха и переживает перезагрузку страницы; «Марии» и
    «Мария» — одна и та же запись, стирается только уверенное совпадение;
  * станок и заказ находятся в базе по имени, модели, номеру, порядковому
    слову или местоимению из прошлой реплики — модель их не угадывает;
  * два подходящих станка — вопрос «какой?», и короткий ответ завершает
    начатое действие;
  * действие с печатью и деньгами только предлагается карточкой каталога:
    мозг ничего не исполняет сам, адрес берётся из `assistant.ACTIONS`;
  * команды компьютеру уходят агенту, и честный отказ, если агент выключен;
  * планировщик на модели не может предложить действие вне каталога.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import assistant, assistant_brain as brain, assistant_memory as memory  # noqa: E402
from connector.printflow.api import register_routes, router  # noqa: E402
from connector.printflow.db import Database  # noqa: E402
from connector.printflow.repo import Repo  # noqa: E402


class FakeManager:
    """Снимок парка как у `PrinterManager.snapshot()`: три станка, два печатают."""

    def __init__(self, states=("RUNNING", "RUNNING", "PAUSE")):
        self.states = states

    def snapshot(self):
        names = (("p1", "Альфа", "P1S", "Ваза"), ("p2", "Бета", "P2S", "Брелок"), ("p3", "Гамма", "A1", "Кашпо"))
        printers = []
        for (pid, name, model, task), state in zip(names, self.states):
            printers.append({"id": pid, "name": name, "model": model, "job": {"order": {"product": task}},
                             "printer": {"state": state, "progress": 40, "remaining_min": 90, "task": task}})
        return {"printers": printers, "queue": [{"name": "Подставка"}], "farm": {}}


class FakeAccounting:
    def debts(self):
        return {"total": 2400, "count": 1, "overdue": 0,
                "rows": [{"customer": "Мария", "debt": 2400, "number": "1042", "days": 3, "overdue": False}]}

    def summary(self, days):
        return {"income": 50000, "expense": 20000, "profit": 30000, "margin": 60}


class BrainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "brain.sqlite3")
        self.api = SimpleNamespace(db=self.db, manager=FakeManager(), repo=Repo(self.db), acc=FakeAccounting())

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def say(self, text, session="t", **kwargs):
        return brain.chat(self.api, text, session=session, **kwargs)


class MemoryTests(BrainTestCase):
    def test_stems_join_word_forms(self):
        self.assertEqual(memory.stems("Мария"), memory.stems("Марии"))
        self.assertEqual(memory.stems("Марию"), memory.stems("Марией"))
        self.assertEqual(memory.stems("пластик"), memory.stems("пластиком"))

    def test_remember_dedups_and_recall_finds_other_word_form(self):
        first = memory.remember(self.db, "Мария берёт только синий PETG")
        again = memory.remember(self.db, "мария берёт только синий PETG!")
        self.assertTrue(first["ok"])
        self.assertTrue(again["duplicate"])
        found = memory.recall(self.db, "что любит Марией?")
        self.assertEqual(1, len(found))
        self.assertIn("PETG", found[0]["text"])

    def test_profile_name_is_replaced_not_duplicated(self):
        memory.remember(self.db, "Владельца зовут Саша", kind="profile", subject="имя")
        memory.remember(self.db, "Владельца зовут Олег", kind="profile", subject="имя")
        self.assertEqual("Олег", memory.owner_name(self.db))
        self.assertEqual(1, len(memory.memories(self.db, kind="profile")))

    def test_forget_needs_confident_match(self):
        memory.remember(self.db, "Мария берёт только PETG")
        memory.remember(self.db, "По пятницам не печатаем")
        self.assertEqual([], memory.forget(self.db, "что-то совсем другое"))
        gone = memory.forget(self.db, "про Марию")
        self.assertEqual(1, len(gone))
        self.assertEqual(1, len(memory.memories(self.db)))

    def test_pin_moves_memory_to_top(self):
        first = memory.remember(self.db, "Первая запись")["memory"]
        memory.remember(self.db, "Вторая запись")
        self.assertTrue(memory.pin(self.db, first["id"]))
        self.assertEqual(first["id"], memory.memories(self.db)[0]["id"])

    def test_dialog_survives_and_is_trimmed(self):
        for index in range(5):
            memory.add_turn(self.db, "s", "user", f"реплика {index}", {"entities": {"printer": {"id": "p1"}}})
        turns = memory.dialog(self.db, "s", 3)
        self.assertEqual(["реплика 2", "реплика 3", "реплика 4"], [turn["text"] for turn in turns])
        self.assertEqual("p1", turns[-1]["meta"]["entities"]["printer"]["id"])
        self.assertEqual(5, memory.clear_dialog(self.db, "s"))
        self.assertEqual([], memory.dialog(self.db, "s"))

    def test_session_key_is_sanitized(self):
        self.assertEqual("main", memory.session_key(""))
        self.assertEqual("abc", memory.session_key("a<b>c"))

    def test_memory_command_patterns(self):
        self.assertEqual(("remember", "Мария берёт PETG"), memory.memory_command("Запомни, что Мария берёт PETG"))
        self.assertEqual(("forget", "Марию"), memory.memory_command("забудь про Марию"))
        self.assertEqual(("recall", "Марию"), memory.memory_command("что ты помнишь про Марию?"))
        self.assertEqual(("recall", ""), memory.memory_command("что ты помнишь"))
        self.assertEqual(("name", "Олег"), memory.memory_command("меня зовут олег"))
        self.assertEqual(("whoami", ""), memory.memory_command("как меня зовут?"))
        self.assertEqual(("", ""), memory.memory_command("поставь на паузу"))

    def test_stats_counts_both_tables(self):
        memory.remember(self.db, "факт")
        memory.add_turn(self.db, "s", "user", "привет")
        self.assertEqual({"memories": 1, "turns": 1}, memory.stats(self.db))


class ConversationTests(BrainTestCase):
    def test_memory_round_trip_through_chat(self):
        saved = self.say("запомни, что Мария берёт только синий PETG")
        self.assertEqual("memory", saved["kind"])
        recalled = self.say("что ты помнишь про Марию?")
        self.assertIn("синий PETG", recalled["reply"])
        self.assertEqual("memory", recalled["source"])

    def test_owner_name_is_used_in_greeting(self):
        self.say("меня зовут Олег")
        self.assertIn("Олег", self.say("как меня зовут")["reply"])
        greeting = self.say("привет")
        self.assertIn("Олег", greeting["reply"])
        self.assertIn("Печатают 2 из 3", greeting["reply"])

    def test_math_and_clock_do_not_need_model(self):
        with patch.object(assistant, "status") as status:
            self.assertEqual("6", self.say("2+2*2")["reply"])
            self.assertIn("Сейчас", self.say("который час")["reply"])
        status.assert_not_called()

    def test_capabilities_answer_lists_what_brain_does(self):
        reply = self.say("что ты умеешь")["reply"]
        self.assertIn("Подтвердить", reply)
        self.assertIn("запомни", reply)

    def test_every_answer_carries_steps_and_is_saved(self):
        answer = self.say("что ты умеешь")
        self.assertTrue(answer["steps"])
        turns = memory.dialog(self.db, "t")
        self.assertEqual(["user", "assistant"], [turn["role"] for turn in turns])

    def test_empty_phrase_is_a_clarification_not_saved(self):
        answer = self.say("   ")
        self.assertEqual("clarify", answer["kind"])
        self.assertEqual([], memory.dialog(self.db, "t"))


class PrinterGroundingTests(BrainTestCase):
    def test_name_is_found_in_any_case_form(self):
        self.assertTrue(brain.name_in("Альфа", "что с Альфой?"))
        self.assertTrue(brain.name_in("Гамма", "продолжи печать на Гамме"))
        self.assertFalse(brain.name_in("Альфа", "что с Бетой?"))

    def test_bare_pause_with_two_printing_asks_which(self):
        answer = self.say("поставь на паузу")
        self.assertEqual("clarify", answer["kind"])
        self.assertIn("Альфа", answer["reply"])
        self.assertIn("Бета", answer["reply"])
        self.assertIsNone(answer["action"])

    def test_short_answer_completes_clarified_action(self):
        self.say("поставь на паузу")
        answer = self.say("второй")
        self.assertEqual("action", answer["kind"])
        self.assertEqual("printer_command", answer["action"]["id"])
        self.assertEqual({"printer_id": "p2", "command": "pause"}, answer["params"])
        self.assertTrue(answer["action"]["confirm"])
        self.assertEqual(assistant.ACTIONS["printer_command"]["path"], answer["action"]["path"])

    def test_status_follow_up_by_ordinal_and_pronoun(self):
        first = self.say("что с Альфой?")
        self.assertIn("Альфа", first["reply"])
        second = self.say("а у второго?")
        self.assertIn("Бета", second["reply"])
        action = self.say("поставь его на паузу")
        self.assertEqual({"printer_id": "p2", "command": "pause"}, action["params"])

    def test_resume_picks_the_only_paused_printer(self):
        answer = self.say("продолжи печать")
        self.assertEqual({"printer_id": "p3", "command": "resume"}, answer["params"])

    def test_stop_carries_warning(self):
        answer = self.say("останови печать на P2S")
        self.assertEqual({"printer_id": "p2", "command": "stop"}, answer["params"])
        self.assertTrue(any("прерывает" in warning for warning in answer["warnings"]))

    def test_music_pause_is_not_a_printer_command(self):
        answer = self.say("поставь музыку на паузу")
        self.assertNotEqual("printer_command", (answer.get("action") or {}).get("id"))

    def test_brain_never_executes_actions_itself(self):
        with patch.object(assistant, "_post_json") as post:
            self.say("поставь на паузу Альфу")
        post.assert_not_called()


class OrderAndCustomerTests(BrainTestCase):
    def setUp(self):
        super().setUp()
        self.order = self.api.repo.save_order({"product": "Ваза", "customer_name": "Мария", "price": 2400,
                                               "status": "ready", "phone": "+79990001122"})

    def test_order_by_number_is_described(self):
        number = self.order["number"]
        answer = self.say(f"что с заказом {number}?")
        self.assertIn(f"№{number}", answer["reply"])
        self.assertIn("готов", answer["reply"])
        self.assertEqual(number, answer["entities"]["order"]["number"])

    def test_fulfil_pronoun_uses_last_order(self):
        self.say(f"статус заказа {self.order['number']}")
        answer = self.say("выдай его")
        self.assertEqual("order_fulfill", answer["action"]["id"])
        self.assertEqual({"id": self.order["id"]}, answer["params"])

    def test_status_change_maps_russian_word(self):
        answer = self.say(f"переведи заказ {self.order['number']} в печать")
        self.assertEqual("order_status", answer["action"]["id"])
        self.assertEqual("printing", answer["params"]["status"])

    def test_unknown_order_number_is_honest(self):
        answer = self.say("что с заказом 99999?")
        self.assertIn("нет", answer["reply"])

    def test_customer_card_includes_orders_and_memory(self):
        memory.remember(self.db, "Мария берёт только синий PETG")
        answer = self.say("что с Марией?")
        self.assertIn("Мария", answer["reply"])
        self.assertIn("Помню", answer["reply"])


class ReadsAndNavigationTests(BrainTestCase):
    def test_debts_answer_from_accounting(self):
        answer = self.say("кто должен денег?")
        self.assertIn("2 400 ₽", answer["reply"])
        self.assertIn("Мария", answer["reply"])

    def test_money_summary(self):
        self.assertIn("прибыль 30 000 ₽", self.say("какая выручка?")["reply"])

    def test_navigation_returns_panel_link(self):
        answer = self.say("открой склад")
        self.assertEqual("navigate", answer["kind"])
        self.assertTrue(answer["link"]["href"].startswith("/#"))

    def test_printing_question_uses_live_park(self):
        answer = self.say("что сейчас печатается?")
        self.assertIn("Альфа", answer["reply"])
        self.assertEqual("farm", answer["source"])

    def test_context_summary_for_sidebar(self):
        memory.remember(self.db, "Владельца зовут Олег", kind="profile", subject="имя")
        summary = brain.context_summary(self.api)
        self.assertEqual("Олег", summary["owner"])
        self.assertEqual(3, len(summary["printers"]))
        self.assertEqual(1, summary["queue"])
        self.assertEqual(2400, summary["debts"]["total"])


class ComputerDelegationTests(BrainTestCase):
    def test_pc_command_with_agent_disabled_is_honest(self):
        answer = self.say("громкость 30")
        self.assertEqual("agent", answer["source"])
        self.assertIn("агент выключен", answer["reply"])

    def test_pc_command_goes_to_agent_chat(self):
        self.db.set_settings({"assistant_agent_enabled": True})
        reply = {"ok": True, "handled": True, "kind": "action", "reply": "Громкость 30%.", "skill": "system.volume",
                 "steps": [{"title": "Понял без модели", "detail": "звук: уровень"}], "suggestions": ["Громче"]}
        with patch.object(assistant, "agent_chat", return_value=reply) as agent:
            answer = self.say("громкость 30")
        agent.assert_called_once()
        self.assertEqual("pc", answer["kind"])
        self.assertEqual("Громкость 30%.", answer["reply"])
        self.assertTrue(any(step["title"].startswith("Агент") for step in answer["steps"]))

    def test_agent_down_offers_start(self):
        self.db.set_settings({"assistant_agent_enabled": True})
        with patch.object(assistant, "agent_chat", return_value={"ok": False, "handled": False, "reason": "не запущен"}):
            answer = self.say("открой блокнот")
        self.assertTrue(answer.get("agent_down"))

    def test_voice_phrase_route_does_not_delegate_back_to_agent(self):
        self.db.set_settings({"assistant_agent_enabled": True})
        register_routes()
        with patch.object(assistant, "agent_chat") as agent:
            code, payload = router.dispatch(self.api, "POST", "/api/assistant/phrase",
                                            {"text": "громкость 30", "source": "agent-wake-word"})
        agent.assert_not_called()
        self.assertEqual(200, code)
        self.assertIn("reply", payload)

    def test_agent_chat_refuses_foreign_address(self):
        self.db.set_settings({"assistant_agent_enabled": True, "assistant_agent_url": "http://192.168.1.5:8799"})
        with patch.object(assistant, "_post_json") as post:
            reply = assistant.agent_chat(self.db, "громкость 30")
        post.assert_not_called()
        self.assertFalse(reply["handled"])


class PlannerTests(BrainTestCase):
    def _model_on(self):
        self.db.set_settings({"assistant_enabled": True, "assistant_model": "qwen2.5:3b"})
        return patch.object(assistant, "status", return_value={"available": True, "model": "qwen2.5:3b"})

    def test_planner_cannot_invent_actions(self):
        answer_json = json.dumps({"action": "delete_everything", "params": {}, "reply": "Удаляю"})

        def post(url, payload, timeout):
            if url.endswith("/api/chat") and payload.get("format") == "json":
                return True, {"message": {"content": answer_json}}, ""
            return True, {"message": {"content": "Обычный ответ"}}, ""

        with self._model_on(), patch.object(assistant, "_post_json", side_effect=post):
            answer = self.say("сделай что-нибудь со станками в парке")
        self.assertIsNone(answer.get("action"))
        self.assertTrue(any("отклонено" in step["detail"] for step in answer["steps"]))

    def test_planner_confirm_action_is_grounded_to_known_printer(self):
        answer_json = json.dumps({"action": "printer_command", "params": {"printer_id": "выдуманный", "command": "pause"},
                                  "reply": "Поставлю паузу на Бете"})
        with self._model_on(), patch.object(assistant, "_post_json",
                                            return_value=(True, {"message": {"content": answer_json}}, "")):
            answer = self.say("притормози пожалуйста станок Бета")
        self.assertEqual("printer_command", answer["action"]["id"])
        self.assertEqual("p2", answer["params"]["printer_id"])


class RouteTests(BrainTestCase):
    def setUp(self):
        super().setUp()
        register_routes()

    def dispatch(self, method, path, body=None, query=None):
        return router.dispatch(self.api, method, path, body, query)

    def test_chat_route_requires_text(self):
        code, payload = self.dispatch("POST", "/api/assistant/chat", {"text": " "})
        self.assertEqual(400, code)
        self.assertFalse(payload["ok"])

    def test_chat_route_answers(self):
        code, payload = self.dispatch("POST", "/api/assistant/chat", {"text": "2+2", "session": "r"})
        self.assertEqual(200, code)
        self.assertEqual("4", payload["reply"])

    def test_memory_routes_round_trip(self):
        code, saved = self.dispatch("POST", "/api/assistant/memory", {"op": "remember", "text": "по пятницам не печатаем"})
        self.assertEqual(200, code)
        memory_id = saved["memory"]["id"]
        code, listed = self.dispatch("GET", "/api/assistant/memory", query={"q": ["пятница"]})
        self.assertEqual(1, listed["count"])
        code, pinned = self.dispatch("POST", "/api/assistant/memory", {"op": "pin", "id": memory_id})
        self.assertTrue(pinned["ok"])
        code, gone = self.dispatch("POST", "/api/assistant/memory", {"op": "forget", "id": memory_id})
        self.assertTrue(gone["ok"])
        code, bad = self.dispatch("POST", "/api/assistant/memory", {"op": "drop"})
        self.assertEqual(400, code)

    def test_dialog_routes(self):
        self.dispatch("POST", "/api/assistant/chat", {"text": "2+2", "session": "d"})
        code, payload = self.dispatch("GET", "/api/assistant/dialog", query={"session": ["d"]})
        self.assertEqual(2, len(payload["turns"]))
        code, cleared = self.dispatch("POST", "/api/assistant/dialog", {"op": "clear", "session": "d"})
        self.assertEqual(2, cleared["cleared"])

    def test_context_route(self):
        code, payload = self.dispatch("GET", "/api/assistant/context")
        self.assertEqual(200, code)
        self.assertIn("farm", payload)

    def test_volume_read_does_not_set_level(self):
        with patch.object(assistant, "_call_agent_skill", return_value={"ok": True}) as skill:
            self.dispatch("GET", "/api/assistant/system/volume")
        skill.assert_called_once_with(self.db, "system.volume", {})

    def test_volume_write_passes_level(self):
        with patch.object(assistant, "_call_agent_skill", return_value={"ok": True}) as skill:
            self.dispatch("POST", "/api/assistant/system/volume", {"level": 35})
        skill.assert_called_once_with(self.db, "system.volume", {"level": 35})

    def test_power_requires_named_action(self):
        with patch.object(assistant, "_call_agent_skill") as skill:
            code, payload = self.dispatch("POST", "/api/assistant/system/power", {"confirmed": True})
        skill.assert_not_called()
        self.assertEqual(400, code)


if __name__ == "__main__":
    unittest.main()
