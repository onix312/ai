"""Локальный помощник: внешний рантайм, черновик вместо действия.

Что здесь держится контрактом, а не словами из документации:

  * помощник выключен по умолчанию и при выключенном рантайме не мешает
    разбору заказа (отказ — это ответ, а не исключение);
  * адрес рантайма обязан быть loopback: текст сообщения содержит телефон и
    сумму, уезжать за пределы машины он не должен;
  * модель не считает деньги: любое число, присланное ею, отбрасывается, а
    заполняются только те поля, которые детерминированный разбор оставил
    пустыми;
  * мусор вместо JSON не роняет маршрут и не портит черновик;
  * `suggest` ничего не пишет в базу.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import assistant  # noqa: E402
from connector.printflow.db import Database  # noqa: E402

DRAFT = {
    "product": "Адресник",
    "customer_name": "",
    "phone": "+79991234567",
    "messenger": "",
    "material": "PETG",
    "color": "",
    "due": "",
    "qty": 20,
    "price": 3600,
    "notes": "Входящий текст (telegram): нужно 20 адресников",
}


def _db(path: pathlib.Path) -> Database:
    db = Database(path)
    db.set_settings({"assistant_enabled": True,
                     "assistant_url": "http://127.0.0.1:11434",
                     "assistant_model": "qwen2.5:3b"})
    return db


def _chat_reply(payload: dict) -> tuple[bool, dict, str]:
    """Ответ рантайма в том виде, в котором его ждёт `_post_json`."""
    return True, {"message": {"role": "assistant",
                              "content": json.dumps(payload, ensure_ascii=False)}}, ""


def _tags_reply(models: list[str]) -> tuple[bool, dict, str]:
    return True, {"models": [{"name": name} for name in models]}, ""


class LoopbackGuardTests(unittest.TestCase):
    def test_loopback_addresses_pass(self):
        for url in ("http://127.0.0.1:11434", "http://localhost:11434",
                    "http://[::1]:11434"):
            local, why = assistant._loopback_ok(url)
            self.assertTrue(local, f"{url}: {why}")

    def test_foreign_address_is_refused(self):
        for url in ("http://192.168.1.50:11434", "https://api.openai.com/v1",
                    "http://example.com"):
            local, why = assistant._loopback_ok(url)
            self.assertFalse(local, url)
            self.assertTrue(why, url)

    def test_non_http_scheme_is_refused(self):
        self.assertFalse(assistant._loopback_ok("ftp://127.0.0.1")[0])
        self.assertFalse(assistant._loopback_ok("127.0.0.1:11434")[0])


class StatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "assistant.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_disabled_by_default(self):
        state = assistant.status(self.db)
        self.assertFalse(state["available"])
        self.assertFalse(state["enabled"])
        self.assertIn("выключен", state["reason"].lower())

    def test_foreign_url_refused_before_any_request(self):
        self.db.set_settings({"assistant_enabled": True,
                              "assistant_url": "http://10.0.0.7:11434"})
        with patch.object(assistant, "_get_json") as get:
            state = assistant.status(self.db)
        get.assert_not_called()
        self.assertFalse(state["loopback"])
        self.assertIn("этим компьютером", state["reason"])

    def test_dead_runtime_is_a_reason_not_an_exception(self):
        self.db.set_settings({"assistant_enabled": True,
                              "assistant_model": "qwen2.5:3b"})
        with patch.object(assistant, "_get_json",
                          return_value=(False, None, "рантайм недоступен")):
            state = assistant.status(self.db)
        self.assertFalse(state["available"])
        self.assertIn("не отвечает", state["reason"])

    def test_model_must_exist_in_runtime(self):
        self.db.set_settings({"assistant_enabled": True,
                              "assistant_model": "llama3:70b"})
        with patch.object(assistant, "_get_json",
                          return_value=_tags_reply(["qwen2.5:3b"])):
            state = assistant.status(self.db)
        self.assertFalse(state["available"])
        self.assertIn("нет", state["reason"])

    def test_available_when_enabled_model_and_runtime_alive(self):
        self.db.set_settings({"assistant_enabled": True,
                              "assistant_model": "qwen2.5:3b"})
        with patch.object(assistant, "_get_json",
                          return_value=_tags_reply(["qwen2.5:3b"])):
            state = assistant.status(self.db)
        self.assertTrue(state["available"])
        self.assertEqual(state["reason"], "")
        self.assertEqual(state["models"], ["qwen2.5:3b"])

    def test_no_local_models_is_not_ready(self):
        self.db.set_settings({"assistant_enabled": True,
                              "assistant_model": "qwen2.5:3b"})
        with patch.object(assistant, "_get_json", return_value=_tags_reply([])):
            state = assistant.status(self.db)
        self.assertFalse(state["available"])
        self.assertIn("ollama pull", state["reason"])

    def test_empty_model_name_is_reported_with_hint(self):
        self.db.set_settings({"assistant_enabled": True, "assistant_model": ""})
        with patch.object(assistant, "_get_json",
                          return_value=_tags_reply(["qwen2.5:3b"])):
            state = assistant.status(self.db)
        self.assertFalse(state["available"])
        self.assertIn("qwen2.5:3b", state["reason"])


class OllamaVerbTests(unittest.TestCase):
    """Вместо Ollama — HTTP-сервер, который отвечает 405 на POST /api/tags."""

    def test_every_model_probe_gets_tags_without_post_body(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from agent import model as agent_model
        import pf

        methods = []

        class OllamaStub(BaseHTTPRequestHandler):
            def do_GET(self):
                methods.append(("GET", self.path))
                if self.path != "/api/tags":
                    self.send_error(404)
                    return
                body = b'{"models":[{"name":"qwen2.5:3b"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                methods.append(("POST", self.path))
                self.send_error(405)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), OllamaStub)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            with tempfile.TemporaryDirectory() as folder:
                db = _db(pathlib.Path(folder) / "t.sqlite3")
                try:
                    db.set_settings({"assistant_enabled": True, "assistant_url": url,
                                     "assistant_model": "qwen2.5:3b"})
                    self.assertTrue(assistant.status(db)["available"])
                    self.assertEqual(["qwen2.5:3b"], assistant.list_models(db))
                finally:
                    db.close()
            self.assertEqual(["qwen2.5:3b"], pf.assistant_models(url)[0])
            self.assertTrue(agent_model.status(url, "qwen2.5:3b")["ok"])
            self.assertEqual([("GET", "/api/tags")] * 4, methods)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class SuggestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = _db(pathlib.Path(self._tmp.name) / "assistant.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _run(self, fields: dict, reply: str = "", draft: dict | None = None,
             models: list[str] | None = None):
        """Два вызова рантайма: проба моделей, затем чат."""
        with patch.object(assistant, "_get_json",
                          return_value=_tags_reply(models if models is not None
                                                   else ["qwen2.5:3b"])), \
             patch.object(assistant, "_post_json",
                          return_value=_chat_reply({"fields": fields, "reply": reply})):
            return assistant.suggest(self.db, dict(draft or DRAFT),
                                     "нужно 20 адресников, чёрных, для Марии")

    def test_fills_only_empty_fields(self):
        result = self._run({"customer_name": "Мария", "color": "Чёрный",
                            "phone": "+79000000000", "material": "PLA"})
        self.assertTrue(result["ok"])
        draft = result["draft"]
        self.assertEqual(draft["customer_name"], "Мария")
        self.assertEqual(draft["color"], "Чёрный")
        # разобранные поля модель не пересматривает
        self.assertEqual(draft["phone"], "+79991234567")
        self.assertEqual(draft["material"], "PETG")
        suggested = {item["field"] for item in result["suggestions"]}
        self.assertEqual(suggested, {"customer_name", "color"})
        self.assertTrue(all(item["source"] == "assistant"
                            for item in result["suggestions"]))

    def test_numbers_from_model_are_dropped(self):
        """Модель не считает деньги: qty, price, grams, hours — не её поля."""
        result = self._run({"qty": 500, "price": 99999, "grams": 12,
                            "hours": 3, "customer_name": "Мария"})
        self.assertEqual(result["draft"]["qty"], 20)
        self.assertEqual(result["draft"]["price"], 3600)
        self.assertNotIn("grams", result["draft"])
        self.assertNotIn("hours", result["draft"])
        self.assertEqual([item["field"] for item in result["suggestions"]],
                         ["customer_name"])

    def test_numeric_values_are_refused_even_for_allowed_fields(self):
        result = self._run({"customer_name": 42, "product": ["адресник"]})
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["draft"]["product"], "Адресник")

    def test_material_and_color_are_whitelisted(self):
        result = self._run({"material": "древесина", "color": "индиго"})
        self.assertEqual(result["draft"]["material"], "PETG")
        self.assertEqual(result["draft"]["color"], "")
        self.assertEqual(result["suggestions"], [])

    def test_color_is_normalised_to_canonical_label(self):
        result = self._run({"color": "черный"})
        self.assertEqual(result["draft"]["color"], "Чёрный")

    def test_phone_is_normalised_and_garbage_refused(self):
        result = self._run({"phone": "8 (900) 111-22-33"},
                           draft={**DRAFT, "phone": ""})
        self.assertEqual(result["draft"]["phone"], "+79001112233")
        result = self._run({"phone": "позвоните мне"}, draft={**DRAFT, "phone": ""})
        self.assertEqual(result["draft"]["phone"], "")

    def test_messenger_gets_canonical_form(self):
        result = self._run({"messenger": "maria_shop"},
                           draft={**DRAFT, "messenger": ""})
        self.assertEqual(result["draft"]["messenger"], "@maria_shop")
        result = self._run({"messenger": "не ник"}, draft={**DRAFT, "messenger": ""})
        self.assertEqual(result["draft"]["messenger"], "")

    def test_due_requires_iso_date(self):
        result = self._run({"due": "2026-09-30"})
        self.assertEqual(result["draft"]["due"], "2026-09-30")
        result = self._run({"due": "как можно скорее"})
        self.assertEqual(result["draft"]["due"], "")
        result = self._run({"due": "2026-13-45"})
        self.assertEqual(result["draft"]["due"], "")

    def test_reply_with_digits_is_flagged(self):
        result = self._run({"customer_name": "Мария"},
                           reply="Готово к 25.09, сумма 3600 ₽")
        self.assertTrue(any("числа" in warning for warning in result["warnings"]))

    def test_suggestions_are_flagged_for_human_review(self):
        result = self._run({"customer_name": "Мария"})
        self.assertTrue(any("Помощник предложил" in warning
                            for warning in result["warnings"]))

    def test_garbage_instead_of_json_keeps_draft(self):
        with patch.object(assistant, "_get_json", return_value=_tags_reply(["qwen2.5:3b"])), \
             patch.object(assistant, "_post_json",
                          return_value=(True, {"message": {"content": "Извините, я не могу помочь"}}, "")):
            result = assistant.suggest(self.db, dict(DRAFT), "текст")
        self.assertTrue(result["ok"])
        self.assertEqual(result["draft"], DRAFT)
        self.assertEqual(result["suggestions"], [])

    def test_json_wrapped_in_markdown_fence_is_parsed(self):
        content = ("Вот результат:\n```json\n"
                   + json.dumps({"fields": {"customer_name": "Мария"},
                                 "reply": "Здравствуйте"}, ensure_ascii=False)
                   + "\n```\n")

        with patch.object(assistant, "_get_json", return_value=_tags_reply(["qwen2.5:3b"])), \
             patch.object(assistant, "_post_json",
                          return_value=(True, {"message": {"content": content}}, "")):
            result = assistant.suggest(self.db, dict(DRAFT), "текст")
        self.assertEqual(result["draft"]["customer_name"], "Мария")
        self.assertEqual(result["reply"], "Здравствуйте")

    def test_runtime_refusal_returns_reason_and_same_draft(self):
        with patch.object(assistant, "_get_json",
                          return_value=(False, None, "рантайм недоступен")):
            result = assistant.suggest(self.db, dict(DRAFT), "текст")
        self.assertFalse(result["ok"])
        self.assertEqual(result["draft"], DRAFT)
        self.assertIn("Рантайм", result["reason"])

    def test_disabled_assistant_is_not_called(self):
        self.db.set_settings({"assistant_enabled": False})
        with patch.object(assistant, "_post_json") as post:
            result = assistant.suggest(self.db, dict(DRAFT), "текст")
        post.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["draft"], DRAFT)

    def test_empty_text_is_refused_without_calling_model(self):
        with patch.object(assistant, "_post_json") as post:
            result = assistant.suggest(self.db, {"product": "Адресник", "notes": ""}, "")
        post.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "Нет текста для разбора")

    def test_suggest_writes_nothing_to_database(self):
        before = self.db.one("SELECT COUNT(*) n FROM orders")["n"]
        customers = self.db.one("SELECT COUNT(*) n FROM customers")["n"]
        self._run({"customer_name": "Мария"}, reply="Здравствуйте")
        self.assertEqual(before, self.db.one("SELECT COUNT(*) n FROM orders")["n"])
        self.assertEqual(customers,
                         self.db.one("SELECT COUNT(*) n FROM customers")["n"])

    def test_long_reply_is_cut(self):
        result = self._run({}, reply="о" * 2000)
        self.assertLessEqual(len(result["reply"]), assistant.MAX_REPLY_CHARS)


class PromptTests(unittest.TestCase):
    def test_prompt_is_capped_and_lists_only_missing_fields(self):
        draft = dict(DRAFT)
        prompt = assistant._prompt("ы" * 20000, draft)
        self.assertLess(len(prompt), assistant.MAX_INPUT_CHARS + 2500)
        self.assertIn("customer_name", prompt)
        self.assertIn("Не предлагай числа", prompt)
        # разобранные поля перечислены как факт, а не как задание
        self.assertIn("+79991234567", prompt)

    def test_prompt_forbids_inventing_facts(self):
        prompt = assistant._prompt("нужно 20 адресников", dict(DRAFT))
        self.assertIn("Не придумывай факты", prompt)
        self.assertIn("JSON", prompt)


class SettingsContractTests(unittest.TestCase):
    """Настройки помощника обязаны проходить через схему (иначе не сохранятся)."""

    def test_keys_exist_with_safe_defaults(self):
        from connector.printflow.config import DEFAULT_SETTINGS
        self.assertFalse(DEFAULT_SETTINGS["assistant_enabled"])
        self.assertEqual(DEFAULT_SETTINGS["assistant_url"], assistant.DEFAULT_URL)
        self.assertEqual(DEFAULT_SETTINGS["assistant_model"], "")
        self.assertEqual(float(DEFAULT_SETTINGS["assistant_timeout_sec"]),
                         assistant.TIMEOUT_SEC)

    def test_schema_describes_every_key(self):
        from connector.printflow.settings_schema import get_schema
        spec = get_schema()
        for key in ("assistant_enabled", "assistant_url", "assistant_model",
                    "assistant_timeout_sec"):
            self.assertIn(key, spec)
            self.assertTrue(spec[key]["label"], key)
            self.assertTrue(spec[key]["hint"], f"{key}: нет подсказки")

    def test_foreign_url_is_clamped_only_by_validation_not_by_schema(self):
        """Схема не знает про loopback — его держит assistant._loopback_ok."""
        from connector.printflow.settings_schema import validate
        clean, _warnings, unknown = validate(
            {"assistant_url": "http://10.0.0.7:11434"})
        self.assertEqual(unknown, [])
        self.assertEqual(clean["assistant_url"], "http://10.0.0.7:11434")
        self.assertFalse(assistant._loopback_ok(clean["assistant_url"])[0])


class RouteTests(unittest.TestCase):
    """Маршруты помощника: объявлены реестром, приватные, без записи в базу."""

    def test_routes_are_registered_and_private(self):
        from connector.printflow.api import register_routes, router
        register_routes()
        found = {(r["method"], r["path"]): r for r in router.reference()
                 if r["path"].startswith("/api/assistant")}
        self.assertIn(("GET", "/api/assistant/status"), found)
        self.assertIn(("POST", "/api/assistant/suggest"), found)
        for route in found.values():
            self.assertFalse(route["public"], route["path"])

    def test_route_module_has_no_sql(self):
        source = (ROOT / "connector" / "printflow"
                  / "routes_assistant.py").read_text(encoding="utf-8")
        for keyword in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(keyword, source.upper())


class PlasticAnswerTests(unittest.TestCase):
    """«Сколько пластика» — катушки склада, не JSON стеллажа и не модель."""

    def test_empty_warehouse_does_not_mention_shelf_products(self):
        from connector.printflow.assistant_knowledge import format_plastic

        text = format_plastic([])
        self.assertIn("катуш", text.casefold())
        self.assertNotIn("{", text)
        self.assertIn("стеллаж", text.casefold())

    def test_groups_remaining_grams_by_material(self):
        from connector.printflow.assistant_knowledge import format_plastic

        text = format_plastic([
            {"material": "PLA", "color_name": "Чёрный", "remaining_grams": 1400},
            {"material": "PLA", "color_name": "Белый", "remaining_grams": 600},
            {"material": "PETG", "color_name": "Чёрный", "remaining_grams": 500},
            {"material": "PLA", "color_name": "Жёлтый", "remaining_grams": 0},
        ])
        self.assertIn("2,5 кг", text)
        self.assertIn("PLA", text)
        self.assertIn("PETG", text)
        self.assertNotIn("Жёлтый", text)
        self.assertNotIn("shf_", text)

    def test_question_does_not_call_the_model(self):
        from connector.printflow import assistant_knowledge as knowledge

        class Repo:
            def spools(self):
                return [{"id": "sp1", "material": "PLA", "color_name": "Чёрный",
                         "remaining_grams": 800, "brand": "NOZZA"}]

        api = type("Api", (), {"repo": Repo(), "db": None})()
        with patch.object(knowledge.assistant, "complete") as complete:
            result = knowledge.answer(api, "Сколько у нас пластика?")
        complete.assert_not_called()
        self.assertTrue(result["answered"])
        self.assertIn("800 г", result["answer"])
        self.assertEqual("spools", result["source"])
        self.assertEqual("катушка", result["facts"][0]["kind"])


class ConversationTests(unittest.TestCase):
    """«2+2» — ответ, а не GET /api/state. Общий вопрос идёт в Ollama, не в каталог."""

    def test_arithmetic_is_not_a_park_action(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(pathlib.Path(tmp.name) / "chat.sqlite3")
        self.addCleanup(db.close)
        db.set_settings({"assistant_enabled": True, "assistant_model": "qwen2.5:3b"})
        with patch.object(assistant, "_post_json") as post, \
             patch.object(assistant, "status") as status:
            result = assistant.parse_intent(db, "2+2")
        post.assert_not_called()
        status.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertIsNone(result["action"])
        self.assertEqual("4", assistant.simple_math("2+2"))
        self.assertEqual("4", assistant.simple_math("сколько будет 2+2?"))

    def test_chat_answers_arithmetic_without_the_model(self):
        from connector.printflow import assistant_knowledge as knowledge

        api = type("Api", (), {"db": None})()
        with patch.object(assistant, "web_search") as search, \
             patch.object(assistant, "status") as status:
            result = knowledge.answer(api, "2+2", chat=True)
        search.assert_not_called()
        status.assert_not_called()
        self.assertTrue(result["answered"])
        self.assertEqual("4", result["answer"])
        self.assertEqual("math", result["source"])
        self.assertFalse(result["web"])

    def test_weather_uses_ollama_search_and_not_customer_data(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(pathlib.Path(tmp.name) / "weather.sqlite3")
        self.addCleanup(db.close)
        db.set_settings({"assistant_enabled": True, "assistant_model": "qwen2.5:3b",
                         "assistant_url": "http://127.0.0.1:11434"})
        seen = []

        def post(url, payload, timeout):
            seen.append(url)
            self.assertNotIn("ollama.com", url)
            content = payload["messages"][-1]["content"]
            self.assertIn("18 градусов", content)
            self.assertNotIn("Мария", content)
            self.assertNotIn("₽", content)
            return True, {"message": {"content": "В Софии около 18 градусов."}}, ""

        with patch.object(assistant, "status", return_value={
                "available": True, "model": "qwen2.5:3b", "reason": ""}), \
             patch.object(assistant, "web_search", return_value={
                 "ok": True, "text": "- Погода (https://example.com): 18 градусов",
                 "sources": [{"title": "Погода", "url": "https://example.com"}],
                 "reason": ""}), \
             patch.object(assistant, "_post_json", side_effect=post):
            result = assistant.converse(db, "какая погода в Софии сегодня")
        self.assertTrue(result["ok"])
        self.assertIn("18", result["answer"])
        self.assertTrue(result["web"])
        self.assertTrue(seen)
        self.assertTrue(all(url.startswith("http://127.0.0.1:11434/") for url in seen))

    def test_web_search_stays_on_loopback(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(pathlib.Path(tmp.name) / "search.sqlite3")
        self.addCleanup(db.close)
        urls = []

        def post(url, payload, timeout):
            urls.append(url)
            if url.endswith("/api/experimental/web_search"):
                return True, {"results": [{
                    "title": "Погода", "url": "https://example.com",
                    "content": "18 градусов"}]}, ""
            return False, None, "рантайм ответил 404"

        with patch.object(assistant, "_post_json", side_effect=post):
            found = assistant.web_search(db, "погода София")
        self.assertTrue(found["ok"])
        self.assertEqual("Погода", found["sources"][0]["title"])
        self.assertTrue(urls)
        self.assertTrue(all("127.0.0.1" in url for url in urls))
        self.assertNotIn("ollama.com", " ".join(urls))
        db.set_settings({"assistant_url": "http://10.0.0.8:11434"})
        refused = assistant.web_search(db, "погода София")
        self.assertFalse(refused["ok"])
        self.assertIn("не этот компьютер", refused["reason"])

    def test_page_sends_plain_questions_to_chat_not_intent(self):
        page = (ROOT / "site" / "assistant.html").read_text(encoding="utf-8")
        self.assertIn("function looksLikeCommand", page)
        self.assertNotIn("looksLikeQuestion", page)
        self.assertIn("chat: true", page)
        self.assertIn('data-q="2+2"', page)
        ask = page[page.find("function ask(text)"):page.find("function intake(")]
        gate = ask.find("if (!looksLikeCommand(text))")
        self.assertGreater(gate, 0)
        self.assertLess(ask.find("askChat(text)", gate), ask.find("/api/assistant/intent", gate))


class WorkshopIdeasTests(unittest.TestCase):
    def test_ideas_come_from_orders_when_agent_is_down(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Database(pathlib.Path(tmp.name) / "ideas.sqlite3")
        self.addCleanup(db.close)
        db.set_settings({"assistant_agent_enabled": True})
        db.execute(
            "INSERT INTO orders (id, product, material, created_at) VALUES (?,?,?,?)",
            ("o1", "Адресник", "PETG", "2026-09-20T10:00:00"))
        with patch.object(assistant, "_call_agent_skill",
                          return_value={"ok": False, "reason": "агент не запущен"}):
            result = assistant.tg_ideas(db, limit=6)
        self.assertTrue(result["ok"])
        self.assertTrue(any("Адресник" in line for line in result["ideas"]))
        self.assertNotIn("не запущен", result.get("reason") or "")
        self.assertEqual("workshop", result["source"])


if __name__ == "__main__":
    unittest.main()
