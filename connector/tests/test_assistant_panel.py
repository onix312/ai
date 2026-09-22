"""Панель помощника: каталог действий, намерение, журнал и своё окно (18.13).

Помощник получил «полный доступ к панели» решением владельца, и доступ без входа
(вопрос 2 допроса). Поэтому здесь держатся три контракта, без которых такой
доступ превращается в дыру:

  * каталог действий состоит только из маршрутов, которые реально существуют
    (мёртвый маршрут в каталоге — кнопка, которая молча ничего не делает);
  * деньги и печать помечены `confirm`, и модель не может это понизить:
    признак берётся из каталога, а адреса маршрута в её ответе нет вовсе;
  * каждое действие оставляет след в журнале — единственную замену отсутствующему
    входу.

Плюс то, что проверяется без Windows: страница отдаётся, в ней нет чужих
доменов, окно лаунчера открывает именно её, диагностика читает настройки.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

import pf  # noqa: E402
from connector.printflow import assistant  # noqa: E402
from connector.printflow.api import Handler, register_routes, router  # noqa: E402
from connector.printflow.db import Database  # noqa: E402

PAGE = (SITE / "assistant.html").read_text(encoding="utf-8")
PATH_RE = re.compile(r"""['"`](/api/[A-Za-z0-9_\-/.]+)['"`]""")


def server_routes() -> set[tuple[str, str]]:
    """Все пары (метод, путь) сервера: реестр плюс if-цепочки."""
    register_routes()
    known = {(r["method"], r["path"]) for r in router.reference()}
    for name in ("api.py", "http_handler.py"):
        method = "GET"
        for line in (ROOT / "connector" / "printflow" / name).read_text(
                encoding="utf-8").splitlines():
            head = re.match(r"    def (get|post)\(", line)
            if head:
                method = head.group(1).upper()
            if re.search(r"if path (==|in )", line):
                for path in re.findall(r'"(/api/[^"]+)"', line):
                    known.add((method, path))
    return known


def pf_args(**overrides) -> argparse.Namespace:
    values = {"port": None, "local": False, "no_qr": False, "json": False,
              "force": False, "no_browser": True, "auto_port": False,
              "system": True, "verbose": False, "background": False,
              "lines": 20, "file": "", "startup_delay": 10,
              "no_autostart": False, "autostart_action": "status"}
    values.update(overrides)
    return argparse.Namespace(**values)


class ActionCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.known = server_routes()

    def test_catalog_size_matches_documents(self):
        """12 чтений + 8 действий с подтверждением = 20.

        Число записано в `CHANGELOG.md` и в отчёте версии, поэтому состав
        каталога pinned: добавили действие — обновите документы.
        """
        reading = [name for name, action in assistant.ACTIONS.items()
                   if not action["confirm"]]
        confirmed = [name for name, action in assistant.ACTIONS.items()
                     if action["confirm"]]
        self.assertEqual(12, len(reading), sorted(reading))
        self.assertEqual(8, len(confirmed), sorted(confirmed))
        self.assertEqual(20, len(assistant.ACTIONS))

    def test_every_action_route_exists(self):
        """Половина «готового» обычно мертва — здесь это ловится тестом."""
        missing = sorted(f"{a['method']} {a['path']}" for a in assistant.ACTIONS.values()
                         if (a["method"], a["path"]) not in self.known)
        self.assertEqual([], missing,
                         "каталог помощника ссылается на несуществующие маршруты")

    def test_every_action_is_complete(self):
        for name, action in assistant.ACTIONS.items():
            with self.subTest(action=name):
                self.assertTrue(action["title"], name)
                self.assertIn(action["method"], ("GET", "POST"), name)
                self.assertTrue(action["path"].startswith("/api/"), name)
                self.assertIsInstance(action["params"], tuple, name)
                self.assertIsInstance(action["confirm"], bool, name)
                self.assertTrue(action["doc"], name)

    def test_money_and_print_actions_require_confirmation(self):
        """Правило репозитория: подтверждение — только для денег и печати."""
        confirmed = set(assistant.CONFIRMED_ACTIONS)
        for name in ("printer_command", "job_start", "job_cancel", "order_save",
                     "order_status", "order_fulfill", "shelf_sale", "settings_save"):
            self.assertIn(name, confirmed, f"{name} двигает деньги или станок")
        for name in ("park", "orders", "queue", "insights", "finance", "search"):
            self.assertNotIn(name, confirmed, f"{name} — чтение, кнопка не нужна")

    def test_reading_actions_are_get(self):
        for name, action in assistant.ACTIONS.items():
            if not action["confirm"]:
                self.assertEqual("GET", action["method"], name)

    def test_payload_hides_nothing_and_exposes_confirm(self):
        payload = assistant.actions_payload()
        self.assertEqual(len(payload), len(assistant.ACTIONS))
        for row in payload:
            self.assertIn("confirm", row)
            self.assertIn("path", row)
            self.assertIsInstance(row["params"], list)


class IntentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "panel.sqlite3")
        self.db.set_settings({"assistant_enabled": True,
                              "assistant_model": "qwen2.5:3b"})

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _run(self, answer: dict, text: str = "поставь паузу на первом станке"):
        def fake_post(url, payload, timeout):
            if url.endswith("/api/tags"):
                return True, {"models": [{"name": "qwen2.5:3b"}]}, ""
            import json
            return True, {"message": {"content": json.dumps(answer, ensure_ascii=False)}}, ""

        with patch.object(assistant, "_post_json", side_effect=fake_post):
            return assistant.parse_intent(self.db, text)

    def test_prompt_never_contains_route_addresses(self):
        """Модель выбирает действие по имени, адрес она не знает вовсе."""
        prompt = assistant._intent_prompt("что печатается")
        self.assertNotIn("/api/", prompt)
        self.assertIn("printer_command", prompt)
        self.assertIn("Не выдумывай действия", prompt)

    def test_action_is_taken_from_catalog_not_from_model(self):
        result = self._run({"action": "printer_command",
                            "params": {"printer_id": "prn-1", "command": "pause"},
                            "explain": "Ставлю паузу"})
        self.assertTrue(result["ok"])
        self.assertEqual("/api/printer/command", result["action"]["path"])
        self.assertEqual("POST", result["action"]["method"])
        self.assertTrue(result["action"]["confirm"])
        self.assertEqual({"printer_id": "prn-1", "command": "pause"}, result["params"])
        self.assertTrue(any("требует подтверждения" in w for w in result["warnings"]))

    def test_invented_action_is_refused(self):
        result = self._run({"action": "delete_everything", "params": {}})
        self.assertTrue(result["ok"])
        self.assertIsNone(result["action"])

    def test_unknown_params_are_dropped(self):
        result = self._run({"action": "printer_command",
                            "params": {"printer_id": "prn-1", "command": "pause",
                                      "price": 1, "sql": "DROP TABLE orders"}})
        self.assertEqual({"printer_id": "prn-1", "command": "pause"}, result["params"])

    def test_structure_param_allowed_only_where_route_expects_it(self):
        result = self._run({"action": "search", "params": {"q": {"$ne": ""}}})
        self.assertEqual({}, result["params"])
        result = self._run({"action": "settings_save",
                            "params": {"patch": {"goal_profit_month": 100000}}})
        self.assertEqual({"goal_profit_month": 100000}, result["params"]["patch"])

    def test_missing_required_param_is_reported(self):
        result = self._run({"action": "job_start", "params": {}})
        self.assertTrue(any("Не хватает параметров" in w for w in result["warnings"]))

    def test_empty_phrase_does_not_call_runtime(self):
        with patch.object(assistant, "_post_json") as post:
            result = assistant.parse_intent(self.db, "   ")
        post.assert_not_called()
        self.assertFalse(result["ok"])

    def test_dead_runtime_is_a_reason(self):
        with patch.object(assistant, "_post_json",
                          return_value=(False, None, "рантайм недоступен")):
            result = assistant.parse_intent(self.db, "что печатается")
        self.assertFalse(result["ok"])
        self.assertIn("Рантайм", result["reason"])

    def test_status_carries_catalog(self):
        with patch.object(assistant, "_post_json",
                          return_value=(True, {"models": [{"name": "qwen2.5:3b"}]}, "")):
            state = assistant.status(self.db)
        self.assertEqual(len(assistant.ACTIONS), len(state["actions"]))


class JournalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "journal.sqlite3")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_action_is_recorded_with_source(self):
        event = assistant.journal(self.db, "printer_command", "Команда станку",
                                  "done", "пауза", {"printer_id": "prn-1"},
                                  printer_id="prn-1")
        self.assertTrue(event.get("id"))
        rows = assistant.journal_recent(self.db)
        self.assertEqual(1, len(rows))
        data = rows[0]["data"]
        self.assertEqual("assistant", data["source"])
        self.assertEqual("printer_command", data["action"])
        self.assertEqual("done", data["outcome"])
        self.assertEqual("prn-1", rows[0]["printer_id"])

    def test_reading_is_not_journaled_by_contract(self):
        """Журнал — для действий: чтение пишется панелью только у каталожных confirm."""
        self.assertEqual(0, len(assistant.journal_recent(self.db)))

    def test_broken_journal_does_not_raise(self):
        with patch.object(self.db, "add_event", side_effect=RuntimeError("база закрыта")):
            event = assistant.journal(self.db, "x", "y", "done")
        self.assertFalse(event.get("ok", True))

    def test_routes_are_registered(self):
        register_routes()
        found = {(r["method"], r["path"]) for r in router.reference()
                 if r["path"].startswith("/api/assistant")}
        self.assertEqual({("GET", "/api/assistant/status"),
                          ("POST", "/api/assistant/suggest"),
                          ("POST", "/api/assistant/intent"),
                          ("POST", "/api/assistant/phrase"),
                          ("GET", "/api/assistant/agent"),
                          ("GET", "/api/assistant/journal"),
                          ("POST", "/api/assistant/journal"),
                          ("POST", "/api/assistant/ask"),
                          ("GET", "/api/assistant/day"),
                          ("GET", "/api/assistant/skills"),
                          ("POST", "/api/assistant/avito/watch"),
                          ("GET", "/api/assistant/avito/watches"),
                          ("POST", "/api/assistant/avito/search"),
                          ("POST", "/api/assistant/avito/check"),
                          ("POST", "/api/assistant/avito/reply"),
                          ("POST", "/api/assistant/tg/draft"),
                          ("GET", "/api/assistant/tg/drafts"),
                          ("POST", "/api/assistant/tg/ideas"),
                          ("POST", "/api/assistant/tg/post"),
                          ("GET", "/api/assistant/avito/threads"),
                          ("POST", "/api/assistant/avito/thread/save"),
                          ("POST", "/api/assistant/avito/to-order"),
                          ("POST", "/api/assistant/avito/schedule"),
                          ("POST", "/api/assistant/avito/notify"),
                          ("GET", "/api/assistant/avito/dedup"),
                          ("POST", "/api/assistant/tg/schedule"),
                          ("GET", "/api/assistant/tg/schedules"),
                          ("POST", "/api/assistant/tg/template/save"),
                          ("GET", "/api/assistant/tg/templates"),
                          ("POST", "/api/assistant/tg/template/apply"),
                          ("POST", "/api/assistant/tg/hashtags"),
                          ("GET", "/api/assistant/tg/search"),
                          ("GET", "/api/assistant/tg/export"),
                          ("GET", "/api/assistant/tg/stats"),
                          ("POST", "/api/assistant/tg/idea/save"),
                          ("GET", "/api/assistant/tg/ideas/history")}, found)


class PageTests(unittest.TestCase):
    """Страница помощника: отдаётся, самодостаточна, не хардкодит каталог."""

    def test_page_is_served_by_short_alias(self):
        from connector.printflow.static_serve import resolve_target
        for path in ("/assistant.html", "/assist", "/помощник"):
            target = resolve_target(path)
            self.assertIsNotNone(target, path)
            self.assertEqual("assistant.html", pathlib.Path(target).name)

    def test_page_is_served_with_200(self):
        handler = Handler.__new__(Handler)
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"
        handler.requestline = "GET /assistant.html HTTP/1.1"
        handler.path = "/assistant.html"
        handler.headers = SimpleNamespace(get=lambda key, default=None: default)
        handler.server = SimpleNamespace(flags=[])
        handler.close_connection = False

        class Recorder:
            def __init__(self):
                self.chunks = []

            def write(self, data):
                self.chunks.append(data)

            def flush(self):
                pass

        handler.wfile = Recorder()
        handler.api = SimpleNamespace(last_host="")
        state = {"code": 0}
        handler.send_response = lambda code, *_a, **_k: state.update(code=code)
        handler.send_header = lambda *a, **k: None
        handler.end_headers = lambda: None
        handler.send_error = lambda code, *a, **k: state.update(code=code)
        handler.serve_static("/assistant.html")
        self.assertEqual(200, state["code"])

    def test_no_duplicate_ids(self):
        ids = re.findall(r'\sid="([^"]+)"', PAGE)
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual([], duplicates)

    def test_no_external_domains(self):
        foreign = re.findall(r"https?://(?!localhost|127\.0\.0\.1)[a-z0-9.-]+", PAGE)
        self.assertEqual([], foreign, "в странице помощника чужие домены")

    def test_every_api_path_called_by_page_exists(self):
        known = {path for _method, path in server_routes()}
        called = set(PATH_RE.findall(PAGE))
        self.assertTrue(called, "страница не зовёт ни одного маршрута")
        self.assertEqual([], sorted(called - known),
                         "страница помощника зовёт несуществующие маршруты")

    def test_catalog_is_not_hardcoded_in_page(self):
        """Адреса действий приходят с сервера: мёртвый маршрут не появится молча."""
        self.assertNotIn("/api/printer/command", PAGE)
        self.assertNotIn("/api/jobs/start", PAGE)
        self.assertIn("/api/assistant/status", PAGE)

    def test_confirm_flag_comes_from_server(self):
        self.assertIn("action.confirm", PAGE)
        self.assertIn("Подтвердить", PAGE)

    def test_journal_is_written_after_action(self):
        self.assertIn("/api/assistant/journal", PAGE)
        self.assertIn("source", (ROOT / "connector" / "printflow"
                                 / "assistant.py").read_text(encoding="utf-8"))


class LauncherWindowTests(unittest.TestCase):
    """Окно помощника: та же страница сервера, свой вход (вопрос 7)."""

    def test_command_is_registered(self):
        self.assertIn("assistant", pf.COMMANDS)
        self.assertIs(pf.cmd_assistant, pf.COMMANDS["assistant"])

    def test_help_mentions_assistant(self):
        self.assertIn("pf.py assistant", pf.HELP)
        self.assertIn("Ollama", pf.HELP)

    def test_window_opens_assistant_page(self):
        seen = {}

        def fake_app_main(argv):
            seen["argv"] = argv
            return 0

        with mock.patch("connector.printflow.app_window.main", fake_app_main):
            code = pf.cmd_assistant(pf_args(port=8765))
        self.assertEqual(0, code)
        self.assertIn("/assistant.html", seen["argv"])
        self.assertIn("--path", seen["argv"])

    def test_app_window_accepts_path_argument(self):
        source = (ROOT / "connector" / "printflow"
                  / "app_window.py").read_text(encoding="utf-8")
        self.assertIn('--path', source)
        self.assertIn("http://localhost:{port}{page}", source)


class DoctorTests(unittest.TestCase):
    """Диагностика помощника читает настройки и не падает без базы."""

    def test_defaults_match_connector(self):
        settings = pf.read_assistant_settings()
        self.assertFalse(settings["assistant_enabled"])
        self.assertEqual(assistant.DEFAULT_URL, settings["assistant_url"])
        self.assertEqual("", settings["assistant_model"])

    def test_settings_are_read_from_database(self):
        with tempfile.TemporaryDirectory() as folder:
            db_file = pathlib.Path(folder) / "printflow.sqlite3"
            db = Database(db_file)
            db.set_settings({"assistant_enabled": True,
                             "assistant_model": "qwen2.5:3b",
                             "assistant_url": "http://127.0.0.1:11434"})
            db.close()
            with patch.object(pf, "DB_FILE", db_file):
                settings = pf.read_assistant_settings()
        self.assertTrue(settings["assistant_enabled"])
        self.assertEqual("qwen2.5:3b", settings["assistant_model"])

    def test_models_probe_refuses_foreign_address(self):
        models, reason = pf.assistant_models("http://10.0.0.7:11434")
        self.assertEqual([], models)
        self.assertIn("этим компьютером", reason)

    def test_models_probe_reports_dead_runtime(self):
        with patch.object(assistant, "_post_json",
                          return_value=(False, None, "connection refused")):
            models, reason = pf.assistant_models("http://127.0.0.1:11434")
        self.assertEqual([], models)
        self.assertIn("connection refused", reason)

    def test_report_says_disabled_without_probing(self):
        buffer = io.StringIO()
        with patch.object(pf, "read_assistant_settings",
                          return_value={"assistant_enabled": False,
                                        "assistant_url": assistant.DEFAULT_URL,
                                        "assistant_model": ""}), \
             patch.object(pf, "assistant_models") as probe, \
             contextlib.redirect_stdout(buffer):
            pf.assistant_report()
        probe.assert_not_called()
        self.assertIn("Выключен", buffer.getvalue())

    def test_report_warns_about_missing_model(self):
        buffer = io.StringIO()
        with patch.object(pf, "read_assistant_settings",
                          return_value={"assistant_enabled": True,
                                        "assistant_url": assistant.DEFAULT_URL,
                                        "assistant_model": ""}), \
             patch.object(pf, "assistant_models", return_value=(["qwen2.5:3b"], "")), \
             contextlib.redirect_stdout(buffer):
            pf.assistant_report()
        text = buffer.getvalue()
        self.assertIn("Модель не выбрана", text)
        self.assertIn("qwen2.5:3b", text)

    def test_report_is_green_when_ready(self):
        buffer = io.StringIO()
        with patch.object(pf, "read_assistant_settings",
                          return_value={"assistant_enabled": True,
                                        "assistant_url": assistant.DEFAULT_URL,
                                        "assistant_model": "qwen2.5:3b"}), \
             patch.object(pf, "assistant_models", return_value=(["qwen2.5:3b"], "")), \
             contextlib.redirect_stdout(buffer):
            pf.assistant_report()
        self.assertIn("помощник готов", buffer.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
