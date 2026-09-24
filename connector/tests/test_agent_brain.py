"""Мозг и руки агента компьютера (18.21, идеи И301–И314).

Что держат эти контракты:

  * правила понимают частые команды без модели — одинаково каждый раз, и
    фразы про цех («запусти печать», «закрой заказ») не превращаются в
    команды компьютеру;
  * контекст разговора: «ещё громче», «а на 50», «закрой его», «открой первый»;
  * память агента: «Марии» = «Мария», повтор не плодит дубль;
  * руки на компьютере — белый список: чужой исполняемый файл не запускается,
    папка вне разрешённых не открывается, выход из системы не нажимается;
  * пустой вызов навыка ничего не меняет: без координат нет клика, без
    «что ставить» нет pip install, без действия нет питания;
  * агент не отвечает чужим сайтам: Origin и Host проверяются, CORS «*» нет;
  * окно агента экранирует всё, что пришло с сервера.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent import brain, capabilities, config, executor, model, pc, server, skills, ui  # noqa: E402
from agent.panel_client import Client  # noqa: E402
from agent.store import Store, stems  # noqa: E402

DEAD_PANEL = "http://127.0.0.1:1"


class PcHelpersTests(unittest.TestCase):
    def test_combo_understands_russian_names_and_layout(self):
        self.assertEqual((["ctrl", "shift", "esc"], ""), pc.parse_combo("ctrl+shift+esc"))
        self.assertEqual((["ctrl", "c"], ""), pc.parse_combo("Контрл с"))
        self.assertEqual((["win", "d"], ""), pc.parse_combo("вин d"))
        self.assertEqual((["pagedown"], ""), pc.parse_combo("page down"))

    def test_dangerous_and_unknown_combos_are_refused(self):
        for combo in ("alt+f4", "ctrl+alt+delete", "win+l"):
            keys, reason = pc.parse_combo(combo)
            self.assertEqual([], keys, combo)
            self.assertIn("не нажимает", reason)
        self.assertIn("не известна", pc.parse_combo("ctrl+йух")[1])
        self.assertIn("четырёх", pc.parse_combo("ctrl+alt+shift+win+a")[1])

    def test_apps_and_sites_resolve_from_whitelist(self):
        self.assertEqual("notepad", pc.resolve_app("блокнот")[0])
        self.assertEqual("control", pc.resolve_app("панель управления")[0])
        self.assertEqual("orca", pc.resolve_app("открой орку")[0])
        self.assertIsNone(pc.resolve_app("format c:"))
        self.assertEqual("https://www.avito.ru/", pc.resolve_site("авито"))
        self.assertEqual("https://avito.ru/moskva", pc.resolve_site("avito.ru/moskva"))
        self.assertEqual("", pc.resolve_site("просто слова"))

    def test_launch_plan_never_runs_foreign_executables(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = pathlib.Path(tmp) / "run.bat"
            script.write_text("echo", encoding="utf-8")
            document = pathlib.Path(tmp) / "a.txt"
            document.write_text("x", encoding="utf-8")
            self.assertIn("Исполняемые", pc.launch_plan(str(script), roots=(tmp,))[1])
            self.assertEqual("path", pc.launch_plan(str(document), roots=(tmp,))[0]["kind"])
            self.assertIn("вне разрешённых", pc.launch_plan("/etc/passwd", roots=(tmp,))[1])
        self.assertEqual(["calc.exe"], pc.launch_plan("калькулятор", platform="win32")[0]["command"])
        self.assertEqual("exe", pc.launch_plan("bambu", platform="win32")[0]["kind"])
        self.assertIn("нет в списке", pc.launch_plan("rm -rf /")[1])
        self.assertIn("Скажите", pc.launch_plan("")[1])

    def test_window_choice_by_words_process_and_synonym(self):
        rows = [{"title": "Безымянный — Блокнот", "process": "notepad"},
                {"title": "Помощник NOZZA", "process": "msedgewebview2"},
                {"title": "Telegram", "process": "Telegram"},
                {"title": "YouTube - Google Chrome", "process": "chrome"}]
        self.assertEqual("Telegram", pc.best_window("телеграм", rows)["title"])
        self.assertEqual("Безымянный — Блокнот", pc.best_window("блокнот", rows)["title"])
        self.assertEqual("YouTube - Google Chrome", pc.best_window("хром", rows)["title"])
        self.assertIsNone(pc.best_window("помощник", rows), "окно помощника не выбирается")
        self.assertEqual("Безымянный — Блокнот", pc.user_window(rows)["title"])

    def test_health_math_is_pure(self):
        self.assertEqual(50.0, pc.cpu_from_samples((100, 1000), (150, 1100)))
        self.assertEqual(0.0, pc.cpu_from_samples((1, 1), (1, 1)))
        busy, total = pc.parse_proc_stat("cpu  100 0 100 700 100 0 0 0\ncpu0 1 1 1 1")
        self.assertEqual((200, 1000), (busy, total))
        memory = pc.parse_meminfo("MemTotal: 16000000 kB\nMemAvailable: 4000000 kB\n")
        self.assertEqual(75, memory["load"])
        notes = pc.health_warnings({"cpu_percent": 97, "memory": {"load": 90},
                                    "disks": [{"mount": "C:\\", "free_gb": 2, "used_percent": 99}],
                                    "battery": {"percent": 10, "plugged": False}})
        self.assertEqual(4, len(notes))

    def test_power_commands_have_a_minute_to_cancel(self):
        self.assertIn("60", pc.power_command("restart", "win32"))
        self.assertEqual(["shutdown", "/a"], pc.power_command("cancel", "win32"))
        self.assertEqual(["shutdown", "-h", "+1"], pc.power_command("shutdown", "linux"))
        self.assertEqual([], pc.power_command("format", "linux"))
        self.assertIn("не из списка", pc.power("format")[1])

    def test_speech_is_honest_without_engine(self):
        with patch.object(pc, "speech_engine", return_value=""):
            state, reason = pc.speak("привет")
        self.assertEqual({}, state)
        self.assertIn("Нет движка", reason)
        self.assertIn("Пустой", pc.speak("  ")[1])

    @unittest.skipIf(pc.IS_WINDOWS, "проверка честного отказа вне Windows")
    def test_windows_only_hands_refuse_with_reason(self):
        self.assertIn("Windows", pc.volume_get()[1])
        self.assertIn("Windows", pc.media("next")[1])
        self.assertIn("Windows", pc.windows()[1])
        self.assertIn("не из списка", pc.media("dance")[1])


class ModelClientTests(unittest.TestCase):
    def test_parse_json_takes_first_balanced_object(self):
        text = 'Вот план: ```json\n{"skill": "system.volume", "params": {"level": 30}, "reply": "Ставлю {30}"}\n``` Надеюсь, помог!'
        self.assertEqual({"skill": "system.volume", "params": {"level": 30}, "reply": "Ставлю {30}"}, model.parse_json(text))
        self.assertEqual({"a": [1, 2]}, model.parse_json('<think>{нет}</think>{"a": [1,2]} хвост'))
        self.assertEqual({"items": [1, 2]}, model.parse_json("[1,2] и ещё"))
        self.assertEqual({}, model.parse_json("без json"))

    def test_default_model_skips_embeddings_and_vision(self):
        self.assertEqual("qwen2.5:3b", model.pick_default(["nomic-embed-text", "llava:7b", "qwen2.5:3b", "llama3.2"]))
        self.assertEqual("", model.pick_default(["nomic-embed-text"]))

    def test_visible_text_drops_reasoning(self):
        self.assertEqual("Привет!\nКак дела?", model.visible_text("<think>долго</think>\nПривет!\n\nКак дела?"))

    def test_chat_sends_system_role_json_mode_and_images(self):
        seen = {}

        def post(url, payload, timeout):
            seen.update(url=url, payload=payload)
            return True, {"message": {"content": '{"ok": true}'}}, ""

        with patch.object(model, "status", return_value={"ok": True, "url": "http://127.0.0.1:11434",
                                                         "model": "qwen2.5vl", "reason": ""}), \
                patch.object(model, "_post", side_effect=post):
            reply = model.chat([{"role": "user", "content": "что на экране?"}], system="правила", fmt="json",
                               images=[b"PNG"])
        self.assertTrue(reply["ok"])
        messages = seen["payload"]["messages"]
        self.assertEqual("system", messages[0]["role"])
        self.assertEqual("json", seen["payload"]["format"])
        self.assertEqual(["UE5H"], messages[-1]["images"])

    def test_vision_is_asked_from_runtime(self):
        with patch.object(model, "status", return_value={"ok": True, "url": "http://127.0.0.1:11434",
                                                         "model": "qwen2.5:3b", "reason": ""}), \
                patch.object(model, "_post", return_value=(True, {"capabilities": ["completion"]}, "")):
            seeing, why = model.vision_ok()
        self.assertFalse(seeing)
        self.assertIn("не видит", why)

    def test_model_name_comes_from_panel_when_env_is_empty(self):
        model._NAME_CACHE.update(at=0.0, name="", source="")
        with patch.object(config, "MODEL_NAME", ""), \
                patch.object(Client, "status", return_value={"alive": True, "model": "gemma3:4b"}):
            self.assertEqual(("gemma3:4b", "panel"), model.resolve_name(models=["gemma3:4b", "qwen2.5:3b"]))
        model._NAME_CACHE.update(at=0.0, name="", source="")
        with patch.object(config, "MODEL_NAME", ""), \
                patch.object(Client, "status", return_value={"alive": False}):
            self.assertEqual(("qwen2.5:3b", "runtime"), model.resolve_name(models=["qwen2.5:3b"]))
        model._NAME_CACHE.update(at=0.0, name="", source="")


class StoreMemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self._tmp.name) / "a.sqlite3")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_stems_are_stable_across_cases(self):
        self.assertEqual(stems("Мария"), stems("Марии"))
        self.assertEqual(stems("чёрный"), stems("черного"))

    def test_memory_life_cycle(self):
        self.assertTrue(self.store.remember("Мария любит синий PETG")["ok"])
        self.assertTrue(self.store.remember("мария любит синий PETG!")["duplicate"])
        self.assertEqual(1, len(self.store.recall("что любит Марией?")))
        self.assertEqual([], self.store.forget("совсем другое"))
        self.assertEqual(1, len(self.store.forget("Марию")))
        self.assertEqual(0, self.store.stats()["memories"])

    def test_dialog_is_trimmed_per_session(self):
        from agent import store as store_module
        with patch.object(store_module, "MAX_DIALOG_TURNS", 3):
            for index in range(6):
                self.store.add_turn("s", "user", f"фраза {index}")
        self.assertEqual(["фраза 3", "фраза 4", "фраза 5"], [row["text"] for row in self.store.dialog("s", 10)])
        self.assertEqual(3, self.store.clear_dialog("s"))


class UnderstandTests(unittest.TestCase):
    CASES = {
        "Ноза, пожалуйста, сделай громкость 30": ("system.volume", {"level": 30}),
        "громче": ("system.volume", {"delta": 10}),
        "выключи звук": ("system.volume", {"mute": "on"}),
        "следующий трек": ("system.media", {"action": "next"}),
        "переключись на телеграм": ("window.focus", {"title": "телеграм"}),
        "перейди на сайт авито": ("app.open", {"target": "авито"}),
        "закрой блокнот": ("window.close", {"title": "блокнот"}),
        "нажми контрл с": ("system.hotkey", {"keys": "ctrl+c"}),
        "открой калькулятор": ("app.open", {"target": "калькулятор"}),
        "как там компьютер": ("system.health", {}),
        "что грузит компьютер": ("system.process_list", {"limit": 8}),
        "поставь таймер на 25 минут": ("scheduler.focus_timer", {"minutes": 25, "note": ""}),
        "найди файл договор": ("files.quick_open", {"name": "договор", "limit": 8}),
        "скажи вслух: печать готова": ("voice.say", {"text": "печать готова"}),
        "заблокируй компьютер": ("system.power", {"action": "lock"}),
    }

    def test_frequent_commands_are_understood_without_model(self):
        for phrase, (skill, params) in self.CASES.items():
            plan = brain.understand(phrase)
            self.assertIsNotNone(plan, phrase)
            self.assertEqual((skill, params), (plan["skill"], plan["params"]), phrase)
            self.assertIn(plan["skill"], skills.SKILLS, "правило ведёт в несуществующий навык")

    def test_workshop_phrases_are_not_pc_commands(self):
        for phrase in ("запусти печать", "закрой заказ 15", "открой склад", "убавь цену", "сделай звук 3d печати",
                       "какая погода"):
            self.assertIsNone(brain.understand(phrase), phrase)

    def test_follow_ups_use_previous_turn(self):
        history = [{"role": "assistant", "meta": {"skill": "system.volume", "params": {"level": 40},
                                                   "target": {"delta": 10}}}]
        self.assertEqual({"delta": 10}, brain.follow_up("ещё", history)["params"])
        self.assertEqual({"delta": -10}, brain.follow_up("ещё тише", history)["params"])
        self.assertEqual({"level": 50}, brain.follow_up("а на 50", history)["params"])
        windows = [{"role": "assistant", "meta": {"skill": "window.focus", "target": {"window": "Telegram"}}}]
        plan = brain.follow_up("закрой его", windows)
        self.assertEqual(("window.close", {"title": "Telegram"}), (plan["skill"], plan["params"]))
        files = [{"role": "assistant", "meta": {"skill": "files.quick_open", "target": {"files": ["/a.pdf", "/b.pdf"]}}}]
        self.assertEqual({"target": "/b.pdf"}, brain.follow_up("открой второй", files)["params"])

    def test_pronoun_without_context_asks(self):
        plan = brain.resolve_pronoun(brain.understand("сверни его"), [])
        self.assertIn("Какое окно", plan["clarify"])

    def test_math_and_clock(self):
        self.assertEqual("2+2 = 4", brain.math_answer("2+2"))
        self.assertEqual("15*3,5 = 52,5", brain.math_answer("посчитай 15*3,5"))
        self.assertEqual("На ноль делить нельзя.", brain.math_answer("1/0"))
        self.assertEqual("", brain.math_answer("9**9**9"))
        self.assertEqual("", brain.math_answer("привет"))
        moment = datetime.datetime(2026, 9, 24, 14, 5)
        self.assertEqual("Сейчас 14:05.", brain.clock_answer("который час", moment))
        self.assertEqual("Сегодня четверг, 24 сентября 2026 года.", brain.clock_answer("какое сегодня число", moment))

    def test_summaries_are_sentences_not_json(self):
        self.assertEqual("Громкость 35%.", brain.summarize("system.volume", {"ok": True, "level": 35}))
        self.assertEqual("Звук выключен.", brain.summarize("system.volume", {"ok": True, "muted": True}))
        listing = brain.summarize("window.list", {"ok": True, "windows": ["Telegram", "Помощник NOZZA", "Блокнот"]})
        self.assertIn("2 окна", listing)
        self.assertNotIn("Помощник", listing)
        self.assertIn("подтверждение", brain.summarize("window.close", {"queued": True, "text": "Закрыть окно"}))
        self.assertIn("Не получилось: нет прав", brain.summarize("app.open", {"ok": False, "reason": "нет прав"}))
        self.assertIn("процессор 12%", brain.summarize("system.health", {"ok": True, "cpu_percent": 12, "memory": {}}))


class FakeAgent:
    """Агент без сети: тот же Runner, подтверждение — как у настоящего `run_skill`."""

    def __init__(self, store):
        self.runner = executor.Runner(store=store, panel=Client(DEAD_PANEL))
        self.calls = []

    def run_skill(self, name, params):
        self.calls.append((name, params))
        skill = skills.get(name)
        if skill and skills.confirm_required(skill):
            return {"ok": True, "queued": True, "id": "abc", "text": executor.describe(skill, params)}
        return self.runner.run(name, params)


class BrainChatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self._tmp.name) / "a.sqlite3")
        self.agent = FakeAgent(self.store)
        self.brain = brain.Brain(self.agent, clock=lambda: datetime.datetime(2026, 9, 24, 9, 30))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_memory_and_name(self):
        self.assertEqual("memory", self.brain.chat("запомни, что Мария любит PETG")["kind"])
        self.assertIn("PETG", self.brain.chat("что ты помнишь про Марию?")["reply"])
        self.brain.chat("меня зовут Олег")
        self.assertEqual("Вас зовут Олег.", self.brain.chat("как меня зовут")["reply"])

    def test_capabilities_and_clock_answer_locally(self):
        self.assertIn("навыков", self.brain.chat("что ты умеешь")["reply"])
        self.assertEqual("Сейчас 09:30.", self.brain.chat("который час")["reply"])

    def test_pc_mode_hands_back_what_it_does_not_understand(self):
        answer = self.brain.chat("какая погода в Москве", mode="pc")
        self.assertFalse(answer["handled"])
        self.assertEqual([], self.store.dialog("main"))

    def test_confirm_skill_becomes_pending(self):
        with patch.dict(self.agent.runner._caps, {"windows": True}):
            answer = self.brain.chat("закрой блокнот")
        self.assertEqual("pending", answer["kind"])
        self.assertEqual({"id": "abc", "text": answer["pending"]["text"]}, answer["pending"])
        self.assertEqual(("window.close", {"title": "блокнот"}), self.agent.calls[-1])

    def test_unavailable_skill_is_explained(self):
        with patch.dict(self.agent.runner._caps, {"windows": False, "windows_reason": "не Windows"}):
            answer = self.brain.chat("какие окна открыты")
        self.assertEqual("error", answer["kind"])
        self.assertIn("не Windows", answer["reply"])

    def test_panel_plan_goes_through_registry(self):
        answer = self.brain.chat("", plan={"skill": "delete.everything", "params": {}})
        self.assertEqual("error", answer["kind"])
        self.assertIn("нет в реестре", answer["reply"])

    def test_model_plan_is_checked_by_registry(self):
        with patch.object(model, "status", return_value={"ok": True, "model": "qwen2.5:3b", "reason": ""}), \
                patch.object(model, "chat", return_value={"ok": True, "text": json.dumps(
                    {"skill": "format.disk", "params": {}, "reply": "Форматирую"}), "model": "qwen2.5:3b"}):
            answer = self.brain.chat("сделай что-нибудь необычное с диском")
        self.assertIn("несуществующий навык", answer["reply"])
        self.assertEqual([], [call for call in self.agent.calls if call[0] == "format.disk"])

    def test_voice_command_skill_only_parses(self):
        result = self.agent.runner.run("voice.command", {"text": "громкость 30"})
        self.assertEqual(("system.volume", {"level": 30}), (result["plan"]["skill"], result["plan"]["params"]))
        self.assertEqual("voice.command", result["skill"])
        self.assertEqual([], self.agent.calls, "разбор команды ничего не исполняет")


class ExecutorRefusalTests(unittest.TestCase):
    """Пустой вызов ничего не меняет: так диспетчерный тест не трогает машину разработчика."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self._tmp.name) / "a.sqlite3")
        self.runner = executor.Runner(store=self.store, panel=Client(DEAD_PANEL))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def dispatch(self, name, params=None):
        return self.runner._dispatch({"name": name, **skills.SKILLS[name]}, params or {})

    def test_empty_calls_refuse_with_reason(self):
        for name, fragment in (("system.install", "что ставить"), ("window.click", "x и y"),
                               ("voice.listen", "секунд"), ("system.power", "Скажите"), ("app.open", "что открыть"),
                               ("system.media", "Скажите"), ("window.arrange", "что сделать"),
                               ("window.close", "какое окно"), ("memory.remember", "Что запомнить"),
                               ("memory.forget", "Что забыть"), ("system.hotkey", "клавиши")):
            result = self.dispatch(name)
            self.assertFalse(result["ok"], name)
            self.assertIn(fragment, result["reason"], name)

    def test_autostart_without_flag_does_not_write(self):
        from agent import system as sys_mod
        with patch.object(sys_mod, "autostart_enable") as enable, patch.object(sys_mod, "autostart_disable") as disable:
            result = self.dispatch("system.autostart")
        enable.assert_not_called()
        disable.assert_not_called()
        self.assertFalse(result["ok"])

    def test_install_without_what_does_not_run_pip(self):
        from agent import install as install_module
        with patch.object(install_module, "install") as pip:
            self.dispatch("system.install")
        pip.assert_not_called()

    def test_memory_skills_round_trip(self):
        self.assertTrue(self.dispatch("memory.remember", {"text": "PETG сушить 4 часа"})["ok"])
        self.assertEqual(1, self.dispatch("memory.recall", {"query": "сушить"})["count"])
        self.assertTrue(self.dispatch("memory.forget", {"what": "сушить PETG"})["ok"])

    def test_app_open_respects_roots(self):
        result = self.dispatch("app.open", {"target": "/etc/passwd"})
        self.assertFalse(result["ok"])
        self.assertIn("вне разрешённых", result["reason"])

    def test_new_skills_are_described_for_confirmation(self):
        for name, params in (("system.hotkey", {"keys": "ctrl+s"}), ("window.close", {"title": "Блокнот"}),
                             ("system.power", {"action": "restart"}), ("app.open", {"target": "блокнот"})):
            text = executor.describe({"name": name, **skills.SKILLS[name]}, params)
            self.assertTrue(text and len(text) < 400, name)
        self.assertIn("Перезагрузить", executor.describe({"name": "system.power", **skills.SKILLS["system.power"]},
                                                         {"action": "restart"}))

    def test_soft_risk_does_not_ask_but_input_does(self):
        self.assertFalse(skills.confirm_required({"name": "app.open", **skills.SKILLS["app.open"]}))
        self.assertFalse(skills.confirm_required({"name": "system.volume", **skills.SKILLS["system.volume"]}))
        self.assertTrue(skills.confirm_required({"name": "system.hotkey", **skills.SKILLS["system.hotkey"]}))
        self.assertTrue(skills.confirm_required({"name": "window.close", **skills.SKILLS["window.close"]}))
        self.assertTrue(skills.confirm_required({"name": "voice.listen", **skills.SKILLS["voice.listen"]}))

    def test_voice_capabilities_are_computed(self):
        caps = capabilities.detect()
        for key in ("speech_in", "speech_in_reason", "speech_out", "speech_out_reason"):
            self.assertIn(key, caps)
        if not caps["speech_in"]:
            self.assertTrue(caps["speech_in_reason"])
        if not pc.IS_WINDOWS:
            self.assertFalse(caps["clipboard"])


class ServerSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._env = patch.dict(os.environ, {"PRINTFLOW_ASSISTANT_DB": str(pathlib.Path(cls._tmp.name) / "a.sqlite3")})
        cls._env.start()
        cls._ports = patch.multiple(config, SPEECH_PORT=0, AGENT_PORT=0, PRINTFLOW_URL=DEAD_PANEL)
        cls._ports.start()
        agent = server.Agent()
        agent._runner = executor.Runner(store=Store(pathlib.Path(cls._tmp.name) / "a.sqlite3"), panel=Client(DEAD_PANEL))
        cls.speech, cls.server = server.serve(agent)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.speech.server_close()
        cls._ports.stop()
        cls._env.stop()
        cls._tmp.cleanup()

    def request(self, path, body=None, headers=None, method=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                         method=method or ("POST" if data is not None else "GET"),
                                         headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                return answer.status, dict(answer.headers), json.loads(answer.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), json.loads(exc.read() or b"{}")

    def test_foreign_origin_is_refused(self):
        code, _headers, payload = self.request("/chat", {"text": "который час"}, {"Origin": "https://evil.example"})
        self.assertEqual(403, code)
        self.assertFalse(payload["ok"])

    def test_dns_rebinding_host_is_refused(self):
        code, _headers, _payload = self.request("/skills", headers={"Host": "evil.example:8799"})
        self.assertEqual(403, code)

    def test_own_window_origin_is_allowed_and_no_cors(self):
        code, headers, payload = self.request("/chat", {"text": "2+2", "session": "w"},
                                              {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(200, code)
        self.assertEqual("2+2 = 4", payload["reply"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_preflight_from_other_site_gets_no_permission(self):
        code, headers, _payload = self.request("/skill", method="OPTIONS", headers={"Origin": "https://evil.example"})
        self.assertEqual(403, code)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_memory_and_history_routes(self):
        code, _h, saved = self.request("/memory", {"op": "remember", "text": "Сушить PETG 4 часа"})
        self.assertTrue(saved["ok"])
        code, _h, listed = self.request("/memory?q=сушить".replace("сушить", "%D1%81%D1%83%D1%88%D0%B8%D1%82%D1%8C"))
        self.assertEqual(1, listed["count"])
        self.request("/chat", {"text": "2+2", "session": "h"})
        code, _h, history = self.request("/chat/history?session=h")
        self.assertEqual(["user", "assistant"], [turn["role"] for turn in history["turns"]])
        code, _h, cleared = self.request("/chat/clear", {"session": "h"})
        self.assertEqual(2, cleared["cleared"])

    def test_pc_mode_does_not_answer_workshop_questions(self):
        code, _h, payload = self.request("/chat", {"text": "сколько у нас долгов", "mode": "pc"})
        self.assertFalse(payload["handled"])


class WindowPageTests(unittest.TestCase):
    def test_page_escapes_server_strings(self):
        page = ui.page()
        script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
        self.assertIn("var esc = function (value)", script)
        self.assertNotIn("${", script, "шаблонные строки с данными сервера запрещены — только esc() или textContent")
        for line in script.splitlines():
            if ".innerHTML" in line and "row." in line:
                self.assertIn("esc(", line, line.strip())

    def test_page_is_chat_first_and_self_contained(self):
        page = ui.page()
        self.assertIn("'/chat'", page)
        self.assertIn("Подтвердить", page)
        self.assertEqual([], re.findall(r"https?://(?!127\.0\.0\.1|localhost)[a-z0-9.-]+", page))


if __name__ == "__main__":
    unittest.main()
