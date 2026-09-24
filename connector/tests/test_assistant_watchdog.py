"""Надзор за процессами (18.19, идея И263): пульс, проход, эскалация, самопроверка.

Контракты здесь про то, чему вообще можно верить в надзирателе:

  * пульс виден целиком или не виден вовсе: запись атомарная, читатель не ловит
    половину файла;
  * «процесс не отвечает» и «пульс старше порога» — разные причины, потому что
    лечатся они по-разному;
  * после порога падений надзиратель перестаёт поднимать роль и говорит об этом
    вслух — молчаливый бесконечный цикл хуже падения;
  * `dry` ничего не поднимает: посмотреть правду и сделать что-то — разные кнопки;
  * надзор не подменяет человека в деньгах и печати: он видит пульс и поднимает
    процесс, и только.

Процессы в тестах не запускаются: там, где нужен «упавший», в конфиг кладётся
уже исчерпанный счётчик падений, а не живая команда.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from agent import watchdog  # noqa: E402
from connector.printflow.api import register_routes, router  # noqa: E402


class WatchdogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"PRINTFLOW_WATCHDOG_DIR": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)


class PulseTests(WatchdogTestCase):
    def test_own_pid_is_alive_and_absurd_pid_is_not(self):
        self.assertTrue(watchdog.pid_alive(os.getpid()))
        self.assertFalse(watchdog.pid_alive(999999))
        self.assertFalse(watchdog.pid_alive(0))
        self.assertFalse(watchdog.pid_alive("не число"))

    def test_beat_writes_full_pulse_atomically(self):
        row = watchdog.beat("agent", note="тест")
        self.assertTrue(row["ok"])
        self.assertEqual(row["pid"], os.getpid())
        data = watchdog.heartbeats()
        self.assertIn("agent", data)
        self.assertEqual(data["agent"]["note"], "тест")
        self.assertNotIn("0", data)  # половины файла не бывает
        self.assertFalse(pathlib.Path(watchdog.paths()["heartbeat"] + ".tmp").exists())

    def test_status_separates_dead_process_from_old_pulse(self):
        state = watchdog.status(roles=["agent"])
        self.assertTrue(state["roles"][0]["stale"])
        self.assertIn("Пульса не было ни разу", state["roles"][0]["reason"])

        watchdog.beat("agent")
        fresh = watchdog.status(roles=["agent"])
        self.assertFalse(fresh["roles"][0]["stale"])
        self.assertTrue(fresh["ok"])
        self.assertEqual(fresh["stale"], [])

        # Пульс живой процесс имеет, но время в нём старое: это «висит», а не «упал».
        path = watchdog.paths()["heartbeat"]
        data = watchdog._read_json(path, {})
        data["agent"]["ts"] = time.time() - 3600
        watchdog._write_json(path, data)
        hung = watchdog.status(stale_sec=90, roles=["agent"])
        self.assertTrue(hung["roles"][0]["stale"])
        self.assertIn("Пульс старше", hung["roles"][0]["reason"])
        self.assertIn("надзор", fresh["hint"].lower())

    def test_dead_pid_is_reported_as_dead(self):
        path = watchdog.paths()["heartbeat"]
        watchdog._write_json(path, {"agent": {"pid": 999999, "at": watchdog.now_iso(),
                                              "ts": time.time(), "note": ""}})
        state = watchdog.status(roles=["agent"])
        self.assertTrue(state["roles"][0]["stale"])
        self.assertIn("не отвечает", state["roles"][0]["reason"])


class ConfigTests(WatchdogTestCase):
    def test_defaults_are_visible_without_file(self):
        config = watchdog.load_config()
        self.assertEqual(config["roles"], list(watchdog.DEFAULT_ROLES))
        self.assertEqual(config["interval_sec"], watchdog.DEFAULT_INTERVAL_SEC)
        self.assertFalse(config["enabled"])

    def test_save_clamps_values_and_disarm_clears_counters(self):
        saved = watchdog.save_config(roles=["agent", "panel"], interval_sec=1, stale_sec=99999,
                                     max_restarts=1000, enabled=True)
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["interval_sec"], 5)          # не чаще пяти секунд
        self.assertEqual(saved["stale_sec"], 7200)          # не реже двух часов
        self.assertEqual(saved["max_restarts"], 100)
        watchdog._bump_restart("agent", "тест")
        self.assertEqual(watchdog.load_config()["restarts"]["agent"], 1)
        off = watchdog.save_config(enabled=False)
        self.assertFalse(off["enabled"])
        self.assertEqual(off["restarts"], {})

    def test_config_survives_broken_file(self):
        pathlib.Path(watchdog.paths()["config"]).write_text("{это не json", encoding="utf-8")
        config = watchdog.load_config()
        self.assertEqual(config["roles"], list(watchdog.DEFAULT_ROLES))

    def test_role_commands_point_at_this_repository(self):
        config = watchdog.load_config()
        agent_cmd = watchdog.role_command("agent", config)
        panel_cmd = watchdog.role_command("panel", config)
        self.assertEqual(agent_cmd[0], sys.executable)
        self.assertEqual(agent_cmd[-1], "agent")
        self.assertIn("pf.py", panel_cmd[-2] if len(panel_cmd) > 1 else "")
        config["commands"] = {"panel": ["my-server", "--port", "1"]}
        self.assertEqual(watchdog.role_command("panel", config), ["my-server", "--port", "1"])


class PassTests(WatchdogTestCase):
    def test_dry_pass_sees_but_does_not_touch(self):
        report = watchdog.pass_once(roles=["agent"], restart=False)
        self.assertFalse(report["ok"] is None)
        self.assertEqual(report["stale"], ["agent"])
        self.assertEqual([a["action"] for a in report["actions"]], ["seen"])
        kinds = [row["kind"] for row in watchdog.tail_log(10)]
        self.assertIn("stale", kinds)

    def test_stale_pulse_is_restarted_and_counted(self):
        started = {"agent": {"ok": True, "pid": 4242, "command": ["fake"], "reason": ""}}
        with mock.patch.object(watchdog, "start_role", side_effect=lambda role, command=None: started[role]), \
             mock.patch.object(watchdog, "stop_role", return_value={"ok": False, "reason": "уже мёртв"}):
            report = watchdog.pass_once(roles=["agent"], restart=True)
        actions = {a["role"]: a for a in report["actions"]}
        self.assertEqual(actions["agent"]["action"], "restarted")
        self.assertEqual(actions["agent"]["pid"], 4242)
        self.assertEqual(watchdog.load_config()["restarts"]["agent"], 1)
        self.assertEqual(watchdog.tail_log(10)[0]["kind"], "stale")

    def test_cooldown_blocks_second_restart(self):
        watchdog.save_config(enabled=True)
        watchdog._bump_restart("agent", "первый")
        allowed, why = watchdog.cooldown_ok("agent")
        self.assertFalse(allowed)
        self.assertIn("Отдых", why)

    def test_after_threshold_watchdog_gives_up_loudly(self):
        watchdog.save_config(max_restarts=1, enabled=True)
        watchdog._bump_restart("agent", "уже падал")
        with mock.patch.object(watchdog, "start_role") as start:
            report = watchdog.pass_once(roles=["agent"], restart=True)
        start.assert_not_called()
        self.assertEqual(report["actions"][0]["action"], "escalated")
        self.assertIn("человек", report["actions"][0]["reason"])
        self.assertIn("agent", watchdog.load_config()["escalated"])
        self.assertIn("escalated", [row["kind"] for row in watchdog.tail_log(10)])

    def test_watch_stops_by_itself_with_pass_limit(self):
        # Подъём роли подменён: без этого проход надзора запускал настоящий
        # `python -m agent`, и после тестов на машине оставался живой агент,
        # занявший порты 8791 и 8799 (найдено в 18.21).
        with mock.patch.object(watchdog.time, "sleep", return_value=None) as pause, \
                mock.patch.object(watchdog, "start_role",
                                  return_value={"ok": True, "role": "agent", "pid": 0}) as start:
            result = watchdog.watch(roles=["agent"], interval_sec=5, max_passes=2)
        self.assertTrue(start.called, "без пульса надзор обязан попытаться поднять роль")
        self.assertEqual(result["passes"], 2)
        self.assertEqual(pause.call_count, 1)
        self.assertIn("watching", [row["kind"] for row in watchdog.tail_log(10)])

    def test_journal_is_readable_newest_first(self):
        watchdog.log_event("agent", "stale", "первое")
        watchdog.log_event("panel", "started", "второе")
        rows = watchdog.tail_log(5)
        self.assertEqual(rows[0]["kind"], "started")
        self.assertEqual(rows[0]["role"], "panel")
        self.assertTrue(watchdog.clear_log())
        self.assertEqual(watchdog.tail_log(5), [])


class SelfCheckTests(WatchdogTestCase):
    def test_self_check_answers_eight_questions(self):
        report = watchdog.self_check()
        self.assertEqual(len(report["checks"]), 8)
        self.assertTrue(report["ok"], report["reason"])
        names = [row["check"] for row in report["checks"]]
        self.assertIn("проверка pid", names)
        self.assertIn("надзор не следит за собой", names)

    def test_beating_thread_marks_role_without_failure(self):
        watchdog.beat_forever("panel", 5)
        # Поток демонический: даём ему мгновение отметиться, но не ждём интервала.
        for _ in range(50):
            if "panel" in watchdog.heartbeats():
                break
            time.sleep(0.02)
        self.assertIn("panel", watchdog.heartbeats())


class WiringTests(WatchdogTestCase):
    """Навыки, маршруты и панель: без них надзор есть, но до него не дотянуться."""

    def test_three_skills_are_declared_with_risk_and_confirmation(self):
        from agent import skills
        read = skills.SKILLS["system.watchdog"]
        arm = skills.SKILLS["system.watchdog_arm"]
        once = skills.SKILLS["system.watchdog_once"]
        self.assertEqual(read["risk"], "read")
        self.assertEqual(arm["risk"], "write")
        self.assertEqual(once["risk"], "write")
        for skill in (read, arm, once):
            self.assertIn("И263", skill["ideas"])
        self.assertEqual(arm["params"]["enabled"], "oneof:on|off")
        self.assertIn("confirm_text", once["params"])

    def test_routes_are_registered(self):
        register_routes()
        found = {(r["method"], r["path"]) for r in router.reference()
                 if r["path"].startswith("/api/assistant/system/watchdog")}
        self.assertEqual(found, {
            ("GET", "/api/assistant/system/watchdog"),
            ("POST", "/api/assistant/system/watchdog/arm"),
            ("POST", "/api/assistant/system/watchdog/once"),
        })

    def test_page_has_watchdog_card_wired(self):
        page = (ROOT / "site" / "assistant.html").read_text(encoding="utf-8")
        self.assertIn('id="card-watchdog"', page)
        for element in ("wd_status", "wd_log", "wd_self", "wd_arm", "wd_disarm", "wd_once"):
            self.assertIn(f'id="{element}"', page)
        # Карточка обязана стоять ВЫШЕ скрипта: иначе слушатели не найдут кнопок.
        self.assertLess(page.index('id="card-watchdog"'), page.index("<script>\n(function () {"))

    def test_pf_command_mentions_watchdog(self):
        source = (ROOT / "pf.py").read_text(encoding="utf-8")
        self.assertIn('"watchdog": cmd_watchdog', source)
        self.assertIn("def cmd_watchdog(", source)
        self.assertIn("start_beat_thread", source)

    def test_agent_reports_its_own_pulse(self):
        source = (ROOT / "agent" / "server.py").read_text(encoding="utf-8")
        self.assertIn('wd.beat("agent"', source)
        self.assertIn('beat_forever("agent"', source)


if __name__ == "__main__":
    unittest.main()
