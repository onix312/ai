"""Мышление с пониманием агента (18.23, идеи И332–И336) и его отзывчивость.

Что держат эти контракты:

  * цепочка команд одной фразой: «открой телеграм и громкость 30» — обе
    команды выполняются, след рассуждения показывает обе; непонятая часть
    отменяет цепочку целиком, а не исполняется наполовину;
  * числа словами и «полчаса»: «громкость тридцать пять», «на пять громче»,
    «таймер на полчаса», «засеки двадцать пять минут»;
  * громкость в любом порядке слов: «на 30 громкость», «сделай тише»,
    «убавь на пять»; «убавь цену» — не про звук;
  * отмена и откат: «отмена» гасит ожидающие действия, «верни как было»
    возвращает прежнюю громкость; отменять нечего — честный ответ;
  * «закрой» без объекта спрашивает «какое окно?» списком, короткий ответ
    «второй» или имя завершают действие; одно окно берётся само;
  * окно агента не грузит компьютер: опрос пропускает перерисовку без
    изменений и не работает у свёрнутого окна;
  * loopback-запросы агента (модель, панель) идут мимо системного прокси;
  * `pf.py assistant` никогда не поднимает второй сервер на той же базе.
"""
from __future__ import annotations

import http.server
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent import brain, capabilities, executor, model, pc, skills  # noqa: E402
from agent.panel_client import Client  # noqa: E402
from agent.store import Store  # noqa: E402
from connector.printflow import app_window  # noqa: E402

DEAD_PANEL = "http://127.0.0.1:1"
MODEL_DOWN = {"ok": False, "reason": "модели нет"}


class FakeAgent:
    """Агент без сети: навыки через Runner, подтверждения — в очереди."""

    def __init__(self, store):
        self.runner = executor.Runner(store=store, panel=Client(DEAD_PANEL))
        self.calls = []
        self.pending_actions = []
        self.cancelled = []

    def run_skill(self, name, params, ask=True):
        self.calls.append((name, params))
        skill = skills.get(name)
        if skill and skills.confirm_required(skill):
            action = {"id": f"a{len(self.pending_actions)}", "kind": "skill",
                      "params": {"name": name, "params": params},
                      "text": executor.describe(skill, params or {})}
            self.pending_actions.append(action)
            return {"ok": True, "queued": True, "id": action["id"], "text": action["text"], "ttl": 60}
        return self.runner.run(name, params)

    def pending(self):
        return list(self.pending_actions)

    def confirm_action(self, action_id, confirmed):
        for row in list(self.pending_actions):
            if row["id"] == action_id:
                self.pending_actions.remove(row)
        self.cancelled.append((action_id, bool(confirmed)))
        return {"ok": True, "done": bool(confirmed)}


class BrainCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self._tmp.name) / "a.sqlite3")
        self.agent = FakeAgent(self.store)
        import datetime as dt
        self.brain = brain.Brain(self.agent, clock=lambda: dt.datetime(2026, 9, 25, 12, 30))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def say(self, text, mode="full", caps=None):
        with patch.object(model, "status", return_value=MODEL_DOWN), \
                patch.dict(self.agent.runner._caps, caps or {"system": True, "windows": True}):
            return self.brain.chat(text, session="window", mode=mode)

    def skills_called(self):
        return [name for name, _params in self.agent.calls]


# ---------------------------------------------------------------------------
# И332. Две команды одной фразой
# ---------------------------------------------------------------------------

class ChainTests(BrainCase):
    def test_two_commands_run_both(self):
        answer = self.say("открой телеграм и громкость 30")
        self.assertEqual(["app.open", "system.volume"], self.skills_called())
        self.assertEqual("rules", answer["source"])
        self.assertTrue(any(step["title"] == "Две команды в одной фразе" for step in answer["steps"]))

    def test_chain_with_comma_and_word_number(self):
        self.say("громкость 30, открой блокнот")
        self.assertEqual(["system.volume", "app.open"], self.skills_called())
        self.assertEqual({"level": 30}, self.agent.calls[0][1])

    def test_chain_of_three(self):
        self.say("громкость 20 и тише и открой блокнот")
        self.assertEqual(["system.volume", "system.volume", "app.open"], self.skills_called())
        self.assertEqual({"level": 20}, self.agent.calls[0][1])
        self.assertEqual({"delta": -10}, self.agent.calls[1][1])

    def test_unmatched_part_cancels_the_chain(self):
        answer = self.say("открой телеграм и квазимодо бла")
        self.assertEqual([], self.skills_called())
        self.assertIn("kind", answer)

    def test_single_command_is_not_a_chain(self):
        self.say("громкость 30")
        self.assertEqual(["system.volume"], self.skills_called())

    def test_chain_works_in_pc_mode(self):
        answer = self.say("громкость 40 и тише", mode="pc")
        self.assertEqual(["system.volume", "system.volume"], self.skills_called())
        self.assertEqual("rules", answer["source"])


# ---------------------------------------------------------------------------
# И333. Числа словами, полчаса
# ---------------------------------------------------------------------------

class WordNumbersTests(unittest.TestCase):
    def test_volume_levels_in_words(self):
        self.assertEqual({"level": 35}, brain.understand("громкость тридцать пять")["params"])
        self.assertEqual({"level": 7}, brain.understand("громкость семь")["params"])
        self.assertEqual({"level": 100}, brain.understand("громкость на максимум")["params"])
        self.assertEqual({"level": 30}, brain.understand("на 30 громкость")["params"])

    def test_volume_delta_in_words(self):
        self.assertEqual({"delta": 5}, brain.understand("на пять громче")["params"])
        self.assertEqual({"delta": -25}, brain.understand("на двадцать пять тише")["params"])
        self.assertEqual({"delta": -5}, brain.understand("убавь на пять")["params"])
        self.assertEqual({"delta": 20}, brain.understand("прибавь на двадцать")["params"])

    def test_timer_words(self):
        self.assertEqual({"minutes": 30, "note": ""}, brain.understand("таймер на полчаса")["params"])
        self.assertEqual({"minutes": 25, "note": "печать"},
                         brain.understand("засеки двадцать пять минут на печать")["params"])
        self.assertEqual({"minutes": 15, "note": ""},
                         brain.understand("поставь таймер на пятнадцать минут")["params"])

    def test_delta_beats_level(self):
        self.assertEqual({"delta": -10}, brain.understand("громкость на 10 тише")["params"])

    def test_follow_up_number_in_words(self):
        history = [{"role": "assistant", "meta": {"skill": "system.volume", "params": {"level": 40},
                                                 "target": {}}}]
        self.assertEqual({"level": 70}, brain.follow_up("а на семьдесят", history)["params"])


# ---------------------------------------------------------------------------
# И335. Громкость в любом порядке слов
# ---------------------------------------------------------------------------

class VolumeOrderTests(unittest.TestCase):
    def test_free_word_order(self):
        self.assertEqual({"level": 30}, brain.understand("на 30 громкость")["params"])
        self.assertEqual({"level": 30}, brain.understand("сделай громкость 30")["params"])
        self.assertEqual({"level": 30}, brain.understand("поставь на 30 громкость")["params"])
        self.assertEqual({"delta": -10}, brain.understand("сделай тише")["params"])
        self.assertEqual({"delta": -10}, brain.understand("давай тише")["params"])
        self.assertEqual({"delta": 10}, brain.understand("громче")["params"])

    def test_not_sound_words(self):
        self.assertIsNone(brain.understand("убавь цену"))
        self.assertIsNone(brain.understand("убавь на пять цену"))
        self.assertIsNone(brain.understand("сделай звук 3d печати"))
        self.assertIsNone(brain.understand("не говори тише"))

    def test_query_still_answers(self):
        self.assertEqual({}, brain.understand("громкость")["params"])
        self.assertEqual({}, brain.understand("какая громкость")["params"])


# ---------------------------------------------------------------------------
# И334. Отмена и «верни как было»
# ---------------------------------------------------------------------------

class CancelUndoTests(BrainCase):
    def test_cancel_drops_pending_actions(self):
        self.say("закрой блокнот")
        self.assertEqual(1, len(self.agent.pending_actions))
        answer = self.say("отмена")
        self.assertIn("Отменил", answer["reply"])
        self.assertEqual([("a0", False)], self.agent.cancelled)
        self.assertEqual([], self.agent.pending_actions)

    def test_undo_restores_volume(self):
        with patch.object(pc, "volume_get", return_value=({"level": 30, "muted": False}, "")), \
                patch.object(pc, "volume_set", side_effect=lambda level: ({"level": level, "muted": False}, "")):
            self.say("громкость 70")
            self.assertEqual({"level": 70}, self.agent.calls[-1][1])
            answer = self.say("верни как было")
        self.assertEqual({"level": 30}, self.agent.calls[-1][1])
        self.assertIn("Громкость", answer["reply"])

    def test_undo_without_history_is_honest(self):
        answer = self.say("верни как было")
        self.assertIn("нечего", answer["reply"].lower())
        self.assertEqual([], self.agent.calls)

    def test_cancel_without_pending_is_honest(self):
        answer = self.say("отмена")
        self.assertIn("нечего", answer["reply"].lower())

    def test_cancel_answers_with_a_question_open(self):
        # «отмена» на вопрос «Когда напомнить?» — прежний ответ «не буду», урок не ставится.
        self.say("напомни позвонить маме")
        answer = self.say("отмена")
        self.assertIn("не буду", answer["reply"].lower())
        self.assertEqual([], self.agent.runner.learning.entries())


# ---------------------------------------------------------------------------
# И336. «Какое окно?» — уточнение с вариантами
# ---------------------------------------------------------------------------

class WindowClarifyTests(BrainCase):
    WINDOWS = ["Блокнот — Безымянный", "Telegram"]

    def test_close_without_object_asks(self):
        with patch("agent.winapi.list_windows", return_value=(list(self.WINDOWS), "")):
            answer = self.say("закрой")
        self.assertEqual("clarify", answer["kind"])
        self.assertIn("1) Блокнот", answer["reply"])
        self.assertIn("2) Telegram", answer["reply"])
        self.assertEqual({"kind": "window", "options": self.WINDOWS},
                         {key: answer["awaiting"][key] for key in ("kind", "options")})

    def test_short_answer_picks_the_window(self):
        with patch("agent.winapi.list_windows", return_value=(list(self.WINDOWS), "")):
            self.say("закрой")
            answer = self.say("второй")
        self.assertEqual(("window.close", {"title": "Telegram"}), self.agent.calls[-1])
        self.assertEqual("pending", answer["kind"])

    def test_answer_by_name(self):
        with patch("agent.winapi.list_windows", return_value=(list(self.WINDOWS), "")):
            self.say("сверни")
            self.say("блокнот")
        self.assertEqual(("window.arrange", {"action": "minimize", "title": self.WINDOWS[0]}),
                         self.agent.calls[-1])

    def test_single_window_is_taken_alone(self):
        with patch("agent.winapi.list_windows", return_value=(["Одно окно"], "")):
            answer = self.say("закрой")
        self.assertEqual(("window.close", {"title": "Одно окно"}), self.agent.calls[-1])
        self.assertEqual("pending", answer["kind"])

    def test_no_windows_is_honest(self):
        with patch("agent.winapi.list_windows", return_value=([], "Окна читаются только в Windows")):
            answer = self.say("закрой")
        self.assertEqual("clarify", answer["kind"])
        self.assertIn("Назовите окно", answer["reply"])

    def test_pick_option_rules(self):
        self.assertEqual("А", brain.pick_option("первый", ["А", "Б"]))
        self.assertEqual("Б", brain.pick_option("2", ["А", "Б"]))
        self.assertEqual("А", brain.pick_option("последний", ["А", "Б", "А"]))
        self.assertEqual("Telegram", brain.pick_option("телега", ["Блокнот", "Telegram"]))
        self.assertIsNone(brain.pick_option("что-то не то", ["А", "Б"]))


# ---------------------------------------------------------------------------
# Отзывчивость: окно агента, прокси, окно помощника
# ---------------------------------------------------------------------------

class _QuietHandler(http.server.BaseHTTPRequestHandler):
    payload = {"version": "test"}

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._answer()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self._answer()

    def _answer(self):
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QuietServer:
    """Локальный HTTP-сервер для проверок loopback и прокси."""

    def __init__(self, payload=None):
        handler = type("Handler", (_QuietHandler,), {"payload": payload or {"version": "test"}})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


class ResponsivenessTests(unittest.TestCase):
    def tearDown(self):
        capabilities._DETECT_CACHE = None

    def test_detect_is_cached(self):
        capabilities._DETECT_CACHE = None
        calls = []
        original = pc.speech_engine

        def counted():
            calls.append(1)
            return original()

        with patch.object(pc, "speech_engine", side_effect=counted):
            first = capabilities.detect()
            second = capabilities.detect()
        self.assertEqual(1, len(calls), "статические проверки повторяются каждый вызов")
        self.assertEqual(first, second)

    def test_dynamic_pings_have_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp) / "a.sqlite3")
            try:
                runner = executor.Runner(store=store, panel=Client(DEAD_PANEL))
                calls = []
                with patch.object(capabilities, "dynamic", side_effect=lambda *a, **k: calls.append(1) or {}):
                    runner.refresh_capabilities(force=True)
                    runner.refresh_capabilities()
                    self.assertEqual(1, len(calls), "пинги внутри TTL повторять нельзя")
                    runner.refresh_capabilities(force=True)
                    self.assertEqual(2, len(calls))
            finally:
                store.close()

    def test_model_request_ignores_system_proxy(self):
        server = QuietServer()
        try:
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
                                         "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}):
                ok, payload, reason = model._get(f"{server.url}/api/tags", 2.0)
                self.assertTrue(ok, reason)
                self.assertEqual({"version": "test"}, payload)
        finally:
            server.close()

    def test_panel_client_request_ignores_system_proxy(self):
        server = QuietServer({"ok": True})
        try:
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}):
                from agent import panel_client
                ok, payload, reason = panel_client._request(f"{server.url}/api/health", {"x": 1}, timeout=2.0)
                self.assertTrue(ok, reason)
                self.assertEqual({"ok": True}, payload)
        finally:
            server.close()


class AppWindowTests(unittest.TestCase):
    def test_assistant_window_never_starts_a_server(self):
        argv = app_window.assistant_window_argv(8765, attach_only=True)
        self.assertIn("--no-server", argv)
        self.assertIn("/assistant.html", argv)
        self.assertNotIn("--no-server", app_window.assistant_window_argv(8765, attach_only=False))

    def test_pick_port_attaches_to_running_printflow(self):
        healthy = QuietServer({"version": "18.23"})
        try:
            port, need_start = app_window.pick_port(healthy.port)
            self.assertEqual(healthy.port, port)
            self.assertFalse(need_start, "окно помощника обязано привязаться, а не поднимать второй сервер")
        finally:
            healthy.close()

    def test_foreign_port_is_not_printflow(self):
        junk = QuietServer({"hello": "world"})  # порт занят, но это не PrintFlow
        try:
            self.assertFalse(app_window.probe_health(junk.port, attempts=1))
            port, need_start = app_window.pick_port(junk.port)
            self.assertTrue(need_start, "чужой порт не повод привязываться — сервер можно запускать")
            self.assertNotEqual(junk.port, port)
        finally:
            junk.close()

    def test_probe_health_says_no_for_free_port(self):
        server = QuietServer()
        port = server.port
        server.close()
        self.assertFalse(app_window.probe_health(port, attempts=1))


class UiPollingTests(unittest.TestCase):
    def test_ui_skips_rebuild_when_nothing_changed(self):
        from agent import ui
        self.assertIn("state.pendingKey", ui.page())
        self.assertIn("document.hidden", ui.page())
        self.assertIn("visibilitychange", ui.page())


if __name__ == "__main__":
    unittest.main()
