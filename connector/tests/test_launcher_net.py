"""Лаунчер `pf.py`: порт по умолчанию, адреса для телефона и разбор «кто занял порт».

Волна 17.0.27 закрывала жалобу «мобильная касса не подключается и не может найти
сервер». На стороне ПК причин две: сервер слушал 8080, а касса показывала в
подсказке 8765 (владелец вводил адрес из подсказки и не попадал), и автопоиска не
существовало вовсе. Здесь закреплено то, что видно без телефона: один порт на всю
систему, готовый адрес для приложения, проверка автопоиска и разбор ответов
системы («кто занял порт», «разрешён ли порт в брандмауэре»).
"""
from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import json
import pathlib
import re
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
PORT_RE = re.compile(r"\b(?:8080|8765)\b")
sys.path.insert(0, str(ROOT))

import pf  # noqa: E402


def args(**overrides) -> argparse.Namespace:
    values = {"port": None, "local": False, "no_qr": False, "json": False, "force": False,
              "no_browser": True, "auto_port": False, "system": True,
              "verbose": False, "background": False, "lines": 20, "file": "",
              "startup_delay": 10, "no_autostart": False, "autostart_action": "status"}
    values.update(overrides)
    return argparse.Namespace(**values)


def capture(function, *positional, **kwargs) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        function(*positional, **kwargs)
    return buffer.getvalue()


def capture_result(function, *positional, **kwargs) -> tuple[int | None, str]:
    """Вывод и код возврата одной командой: вызов команды дважды искажает счёт
    обращений к внешнему миру (браузер, процессы, поток вывода)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = function(*positional, **kwargs)
    return code, buffer.getvalue()


class PortPolicyTests(unittest.TestCase):
    def test_default_port_matches_the_register_hint(self):
        """Порт по умолчанию один: 8765 — он же в подсказке приложения кассы."""
        self.assertEqual(pf.DEFAULT_PORT, 8765)
        self.assertEqual(pf.DISCOVERY_PORT, 8765)
        for path in (ROOT / "android/app/src/main/res/values/strings.xml",
                     ROOT / "android/pult/src/main/res/values/strings.xml"):
            text = path.read_text(encoding="utf-8")
            self.assertIn(":8765", text, f"{path.name}: подсказка порта разошлась с pf.py")
            self.assertNotIn(":8790", text, f"{path.name}: остался старый порт пульта")
        connector = (ROOT / "connector" / "printflow_connector.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_PORT = 8765", connector)
        kassa_net = (ROOT / "android/app/src/main/java/ai/printflow/kassa/Net.kt"
                     ).read_text(encoding="utf-8")
        self.assertIn("intArrayOf(8765, 8080, 8766, 8790)", kassa_net)

    def test_one_port_for_the_whole_system(self):
        """Одно число и на сервере, и на лаунчере, и на телефоне.

        До 17.0.27 сервер слушал 8080, а касса искала 8765 — «телефон не может
        найти сервер». Теперь порт живёт в одном месте (`printflow.DEFAULT_PORT`),
        а этот тест ловит расхождение до того, как его увидит владелец.
        """
        from connector.printflow import DEFAULT_PORT, api, app_window, config

        self.assertEqual(pf.DEFAULT_PORT, DEFAULT_PORT)
        self.assertEqual(DEFAULT_PORT, app_window.DEFAULT_PORT)
        self.assertEqual(DEFAULT_PORT, inspect.signature(api.serve).parameters["port"].default)
        self.assertEqual(DEFAULT_PORT, inspect.signature(config.host_port).parameters["default"].default)

        connector = (ROOT / "connector" / "printflow_connector.py").read_text(encoding="utf-8")
        self.assertIn(f"DEFAULT_PORT = {pf.DEFAULT_PORT}", connector)
        self.assertIn('parser.add_argument("--port", type=int, default=DEFAULT_PORT',
                      connector)

        kassa_net = (ROOT / "android/app/src/main/java/ai/printflow/kassa/Net.kt").read_text(
            encoding="utf-8")
        self.assertIn(f"intArrayOf({pf.DEFAULT_PORT},", kassa_net,
                      "порт по умолчанию должен проверяться кассой первым")

    def test_explicit_port_wins(self):
        self.assertEqual(9090, pf.resolve_port(9090))
        with mock.patch.object(pf, "load_autostart_config", return_value={"port": 9000}):
            self.assertEqual(9090, pf.resolve_port(9090))

    def test_installed_port_is_reused(self):
        """`install --port 9000` и следующий `pf.py` — один и тот же порт."""
        with mock.patch.object(pf, "load_autostart_config", return_value={"port": 9000}):
            self.assertEqual(9000, pf.resolve_port(None))

    def test_no_installation_falls_back_to_the_default(self):
        with mock.patch.object(pf, "load_autostart_config", return_value={}):
            self.assertEqual(pf.DEFAULT_PORT, pf.resolve_port(None))
        with mock.patch.object(pf, "load_autostart_config", side_effect=OSError):
            self.assertEqual(pf.DEFAULT_PORT, pf.resolve_port(None))

    def test_candidates_keep_the_order_and_have_no_duplicates(self):
        with mock.patch.object(pf, "load_autostart_config", return_value={"port": 9000}):
            candidates = pf.port_candidates()
        self.assertEqual(candidates[0], 9000, "порт установки ищем первым")
        self.assertIn(pf.DEFAULT_PORT, candidates)
        self.assertIn(8080, candidates, "установки до 17.0.27 не должны потеряться")
        self.assertIn(8790, candidates)
        self.assertEqual(len(candidates), len(set(candidates)))

    def test_parser_accepts_the_new_commands(self):
        parser = pf.build_parser()
        for command in ("net", "phone", "status", "start"):
            self.assertEqual(command, parser.parse_args([command]).command)
        self.assertIsNone(parser.parse_args(["net"]).port,
                          "порт без --port должен решаться позже: install → default")
        self.assertTrue(parser.parse_args(["status", "--json"]).json)

    def test_misspelled_command_gets_a_hint(self):
        self.assertEqual("status", pf.suggest_command("statsu"))
        self.assertEqual("", pf.suggest_command("совсем-не-команда"))
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            code = pf.main(["statsu"])
        self.assertEqual(2, code)
        printed = stdout.getvalue()
        self.assertIn("Не знаю команду", printed)
        self.assertIn("python pf.py status", printed)
        # английского «invalid choice» владелец видеть не должен
        self.assertNotIn("invalid choice", printed + stderr.getvalue())


class PortOwnerParsingTests(unittest.TestCase):
    """Разбор ответов системы — без запуска netstat/tasklist/lsof."""

    NETSTAT = """
Активные подключения

  Имя    Локальный адрес        Внешний адрес          Состояние           PID
  TCP    0.0.0.0:8765           0.0.0.0:0              LISTENING           4321
  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING           4321
  TCP    0.0.0.0:49152          0.0.0.0:0              LISTENING           999
  TCP    192.168.1.50:8765      192.168.1.9:51000      ESTABLISHED         4321
  TCP    [::]:8765              [::]:0                 LISTENING           7777
"""

    def test_netstat_pids_for_listening_sockets_only(self):
        self.assertEqual([4321, 7777], pf._pids_from_netstat(self.NETSTAT, 8765))
        self.assertEqual([999], pf._pids_from_netstat(self.NETSTAT, 49152))
        self.assertEqual([], pf._pids_from_netstat(self.NETSTAT, 8766))
        self.assertEqual([], pf._pids_from_netstat("", 8765))
        # порт как суффикс, а не как подстрока: 18765 ≠ 8765
        self.assertEqual([], pf._pids_from_netstat(
            "  TCP    0.0.0.0:18765   0.0.0.0:0   LISTENING   5\n", 8765))

    def test_tasklist_name(self):
        self.assertEqual("python.exe", pf._name_from_tasklist(
            "python.exe                   4321 Console                    1     12 345 КБ\n"))
        self.assertEqual("", pf._name_from_tasklist("ИНФОРМАЦИЯ: нет задач\n"))

    def test_owner_from_posix_listings(self):
        lsof = ("COMMAND   PID   USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
                "python3  4321  user    3u  IPv4  12345      0t0  TCP *:8765 (LISTEN)\n")
        self.assertEqual("python3 (pid 4321)", pf._owner_from_listing(lsof))
        ss = ("State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
              "LISTEN 0      128    0.0.0.0:8765       0.0.0.0:*     "
              "users:((\"python3\",pid=4321,fd=3))\n")
        self.assertEqual("python3 (pid 4321)", pf._owner_from_listing(ss))
        self.assertEqual("", pf._owner_from_listing(""))
        self.assertEqual("", pf._owner_from_listing("COMMAND PID USER\n"))


class BatchLauncherTests(unittest.TestCase):
    """`ЗАПУСТИТЬ.bat` — первый файл, который видит владелец на Windows.

    Проверять его нечем, кроме текста (cmd.exe в Linux-тестах не запустить),
    поэтому контракт держим на строках: переводы строк, кодировка и то, что
    адреса в батнике не дублируются.
    """

    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "ЗАПУСТИТЬ.bat"
        cls.raw = cls.path.read_bytes()
        cls.text = cls.raw.decode("utf-8")

    def test_is_a_real_batch_file(self):
        self.assertTrue(self.path.exists())
        self.assertIn(b"\r\n", self.raw, "cmd.exe не понимает LF-переносы в .bat")
        self.assertEqual(0, self.raw.count(b"\n") - self.raw.count(b"\r\n"),
                         "каждая строка .bat должна заканчиваться парой CRLF")

    def test_gitattributes_do_not_normalize_the_file(self):
        """`*.bat text eol=crlf` — ловушка: блоб в репозитории хранится с LF, а
        рабочая копия с CRLF, и git считает файл изменённым навсегда. Батник
        лежит с CRLF как есть, а `diff --check` успокаивает `cr-at-eol`."""
        attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        rules = [line for line in attrs.splitlines() if "*.bat" in line]
        self.assertEqual(1, len(rules), "правило для .bat должно быть одно")
        self.assertIn("cr-at-eol", rules[0])
        self.assertNotIn("eol=crlf", rules[0])
        self.assertNotIn(" -text", rules[0])

    def test_switches_the_console_to_utf8(self):
        """Без chcp 65001 русский текст в cmd превращается в кракозябры, и раньше
        по этой причине батник был написан английскими строками."""
        self.assertIn("chcp 65001", self.text)
        self.assertIn("Python не найден", self.text)
        self.assertIn("pause", self.text, "окно без pause закрывается раньше, чем его прочтут")

    def test_verifies_python_instead_of_trusting_where(self):
        """Windows отдаёт заглушку python.exe, которая открывает Store."""
        self.assertIn('-c "import sys"', self.text)
        for candidate in ("py -3", "python", "python3"):
            with self.subTest(candidate=candidate):
                self.assertIn(candidate, self.text)

    def test_does_not_promise_its_own_port(self):
        """Порт и версию печатает pf.py; второй список в батнике уже расходился
        с кодом (там жило 8080, пока касса искала 8765). В комментариях-`rem`
        числа остаются как объяснение этой истории — они ничего не печатают."""
        code = "\n".join(line for line in self.text.splitlines()
                          if not line.strip().lower().startswith("rem"))
        ports = re.findall(PORT_RE, code)
        self.assertEqual([], ports, f"в батнике зашит порт: {ports}")
        self.assertIn("pf.py", code)

    def test_errors_point_to_the_right_command(self):
        import pf

        self.assertIn('%PY% "pf.py"', self.text)
        for hint in ("doctor", "net"):
            with self.subTest(hint=hint):
                self.assertIn(f"pf.py {hint}", self.text)
        self.assertIn("pause", self.text)
        self.assertTrue(callable(pf.main))


class FirewallTests(unittest.TestCase):
    """Брандмауэр — вторая по частоте причина «телефон не видит сервер»."""

    RU = """
Правила брандмауэра
----------------------------------------------------------------------
Имя правила:                             PrintFlow 8765
----------------------------------------------------------------------
Включено:                                Да
Направление:                             Входящие
Профили:                                 Частный
Действие:                                Разрешить
Протокол:                                TCP
Локальный порт:                          8765

Имя правила:                             Другая программа
----------------------------------------------------------------------
Включено:                                Да
Действие:                                Блокировать
Протокол:                                TCP
Локальный порт:                          8765
"""

    EN = """
Rule Name:                            PrintFlow
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Action:                               Allow
Protocol:                             TCP
LocalPort:                            8000-9000
"""

    def test_russian_allow_rule_is_found(self):
        self.assertIs(True, pf.firewall_allows(self.RU, 8765))

    def test_disabled_or_other_ports_are_not_an_allowance(self):
        self.assertIs(False, pf.firewall_allows(self.RU, 8766))
        disabled = self.RU.replace("Включено:                                Да",
                                   "Включено:                                Нет")
        self.assertIs(False, pf.firewall_allows(disabled, 8765))

    def test_english_ranges_still_count(self):
        self.assertIs(True, pf.firewall_allows(self.EN, 8765))
        self.assertIs(False, pf.firewall_allows(self.EN, 9001))

    def test_unknown_output_is_unknown_not_forbidden(self):
        """Чужая локаль или другой файрвол — не повод пугать владельца."""
        self.assertIsNone(pf.firewall_allows("", 8765))
        self.assertIsNone(pf.firewall_allows("Какой-то другой вывод\n", 8765))

    def test_port_field_matches_ranges_and_lists(self):
        self.assertTrue(pf._port_field_matches("8765", 8765))
        self.assertTrue(pf._port_field_matches("8000-9000", 8500))
        self.assertTrue(pf._port_field_matches("80, 8765, 443", 8765))
        self.assertFalse(pf._port_field_matches("8766", 8765))
        self.assertFalse(pf._port_field_matches("", 8765))

    def test_fix_command_is_given_even_when_the_check_is_impossible(self):
        state = pf.firewall_state(8765)
        self.assertIn("fix", state)
        self.assertIn("8765", state["fix"])


class PhoneAddressTests(unittest.TestCase):
    def test_urls_for_each_shell(self):
        urls = pf.phone_urls(8765, ["192.168.1.50", "10.0.0.7"])
        self.assertEqual("192.168.1.50:8765", urls["manual"])
        self.assertEqual("http://192.168.1.50:8765/cashier.html", urls["kassa"])
        self.assertEqual("http://192.168.1.50:8765/pult", urls["pult"])
        self.assertEqual("http://10.0.0.7:8765/cashier.html",
                         urls["by_ip"]["10.0.0.7"]["/cashier.html"])

    def test_no_network_no_urls(self):
        urls = pf.phone_urls(8765, [])
        self.assertEqual("", urls["primary"])
        self.assertEqual("", urls["manual"])
        self.assertEqual({}, urls["by_ip"])

    def test_report_without_network_says_what_to_do(self):
        with mock.patch.object(pf, "running_port", return_value=None), \
                mock.patch.object(pf, "local_ips", return_value=[]), \
                mock.patch.object(pf, "health", return_value=None), \
                mock.patch.object(pf, "port_busy", return_value=False), \
                mock.patch.object(pf, "auto_discovery", return_value=[]), \
                mock.patch.object(pf, "firewall_state", return_value={"known": False}):
            report = pf.net_report(8765)
        json.dumps(report)  # --json обязан работать без исключений
        texts = [check["text"] for check in report["checks"]]
        self.assertTrue(any("нет сетевого адреса" in text.lower() for text in texts))
        self.assertFalse(report["running"])
        self.assertFalse(report["urls"]["primary"])

    def test_report_sees_a_working_server_and_its_discovery(self):
        with mock.patch.object(pf, "running_port", return_value=8765), \
                mock.patch.object(pf, "local_ips", return_value=["192.168.1.50"]), \
                mock.patch.object(pf, "health", return_value={"version": "17.0.27"}), \
                mock.patch.object(pf, "probe_tcp", return_value=True), \
                mock.patch.object(pf, "port_busy", return_value=True), \
                mock.patch.object(pf, "apk_info", return_value={"available": False}), \
                mock.patch.object(pf, "auto_discovery",
                                  return_value=[{"base": "http://192.168.1.50:8765",
                                                 "port": 8765, "version": "17.0.27"}]), \
                mock.patch.object(pf, "firewall_state",
                                  return_value={"known": True, "allowed": True, "fix": ""}):
            report = pf.net_report(8765)
        self.assertTrue(report["running"])
        self.assertTrue(report["lan_reachable"])
        self.assertFalse(report["local_only"])
        self.assertEqual("192.168.1.50:8765", report["urls"]["manual"])
        texts = " ".join(check["text"] for check in report["checks"])
        self.assertIn("Автопоиск работает", texts)
        self.assertIn("брандмауэре есть разрешение", texts)

    def test_local_only_server_is_reported_as_unreachable(self):
        with mock.patch.object(pf, "running_port", return_value=8765), \
                mock.patch.object(pf, "local_ips", return_value=["192.168.1.50"]), \
                mock.patch.object(pf, "health", return_value={"version": "17.0.27"}), \
                mock.patch.object(pf, "probe_tcp", return_value=False), \
                mock.patch.object(pf, "port_busy", return_value=True), \
                mock.patch.object(pf, "apk_info", return_value={"available": False}), \
                mock.patch.object(pf, "auto_discovery", return_value=[]), \
                mock.patch.object(pf, "firewall_state", return_value={"known": False}):
            report = pf.net_report(8765)
        self.assertTrue(report["local_only"])
        texts = " ".join(check["text"] for check in report["checks"])
        self.assertIn("только этот компьютер", texts)

    def test_discovery_bases_drop_the_check_own_addresses(self):
        hits = [{"base": "http://127.0.0.1:8765", "port": 8765},
                {"base": "http://169.254.0.21:8765", "port": 8765},
                {"base": "http://192.168.1.50:8765", "port": 8765}]
        self.assertEqual(["http://192.168.1.50:8765"], pf.discovery_bases(hits))
        # если другого адреса нет — показываем что есть, а не пустоту
        self.assertEqual(["http://127.0.0.1:8765"],
                         pf.discovery_bases([hits[0]]))


class OutputTests(unittest.TestCase):
    """Вывод команд — то, по чему владелец настраивает телефон."""

    def test_banner_shows_the_addresses_for_both_shells(self):
        with mock.patch.object(pf, "apk_info", return_value={"available": False}), \
                mock.patch.object(pf, "firewall_state", return_value={"known": False}):
            text = capture(pf.banner, "0.0.0.0", 8765, ["192.168.1.50"],
                           show_qr=False,
                           report={"urls": pf.phone_urls(8765, ["192.168.1.50"]),
                                   "apk": {"available": False}})
        self.assertIn("http://192.168.1.50:8765/cashier.html", text)
        self.assertIn("http://192.168.1.50:8765/pult", text)
        self.assertIn("python pf.py net", text)
        self.assertIn(str(pf.DATA_DIR), text)
        self.assertIn("Найти сервер в сети", text)

    def test_banner_draws_the_qr_for_the_register(self):
        with mock.patch.object(pf, "apk_info", return_value={"available": False}), \
                mock.patch.object(pf, "firewall_state", return_value={"known": False}):
            text = capture(pf.banner, "0.0.0.0", 8765, ["192.168.1.50"],
                           report={"urls": pf.phone_urls(8765, ["192.168.1.50"]),
                                   "apk": {"available": False}})
        self.assertIn("█", text, "QR-код не нарисовался")

    def test_banner_warns_about_local_only_mode(self):
        text = capture(pf.banner, "127.0.0.1", 8765, [], show_qr=False, report={})
        self.assertIn("только этот компьютер", text)

    def test_net_command_prints_json_for_scripts(self):
        report = {"port": 8765, "running": True, "version": "17.0.27",
                  "lan_reachable": True, "local_only": False,
                  "urls": pf.phone_urls(8765, ["192.168.1.50"]),
                  "apk": {"available": False}, "discovery": [], "checks": []}
        with mock.patch.object(pf, "net_report", return_value=report):
            code, text = capture_result(pf.cmd_net, args(json=True))
        payload = json.loads(text)
        self.assertEqual(8765, payload["port"])
        self.assertEqual(0, code)

    def test_net_command_reports_trouble_with_a_non_zero_code(self):
        empty = {"port": 8765, "running": False, "version": "", "urls": pf.phone_urls(8765, []),
                 "apk": {"available": False}, "discovery": [], "checks": [],
                 "local_only": False, "lan_reachable": False}
        with mock.patch.object(pf, "net_report", return_value=empty):
            self.assertEqual(1, pf.cmd_net(args()))

    def test_status_json_is_machine_readable(self):
        report = {"port": 8765, "running": True, "version": "17.0.27",
                  "urls": pf.phone_urls(8765, ["192.168.1.50"]), "apk": {"available": False},
                  "discovery": [{"base": "http://192.168.1.50:8765"}], "checks": [],
                  "local_only": False, "lan_reachable": True}
        with mock.patch.object(pf, "net_report", return_value=report), \
                mock.patch.object(pf, "read_pid", return_value=4321):
            code, text = capture_result(pf.cmd_status, args(json=True))
        payload = json.loads(text)
        self.assertEqual(4321, payload["pid"])
        self.assertEqual(0, code)

    def test_start_says_who_took_the_port(self):
        with mock.patch.object(pf, "resolve_port", return_value=8765), \
                mock.patch.object(pf, "running_port", return_value=None), \
                mock.patch.object(pf, "port_busy", return_value=True), \
                mock.patch.object(pf, "health", return_value=None), \
                mock.patch.object(pf, "port_owner", return_value="python.exe (pid 4321)"), \
                mock.patch.object(pf, "free_port", return_value=8766):
            code, text = capture_result(pf.cmd_start, args())
        self.assertEqual(1, code)
        self.assertIn("python.exe (pid 4321)", text)
        self.assertIn("--auto-port", text)

    def test_start_refuses_a_second_copy_on_another_port(self):
        """Живой сервер на 8080 и запуск «по умолчанию» на 8765 — не второй
        экземпляр, а тот же самый: две базы в одной SQLite удвоят задания."""
        with mock.patch.object(pf, "resolve_port", return_value=8765), \
                mock.patch.object(pf, "running_port", return_value=8080), \
                mock.patch.object(pf, "health", return_value={"version": "17.0.27"}), \
                mock.patch.object(pf, "port_busy", return_value=False), \
                mock.patch.object(pf, "local_ips", return_value=["192.168.1.50"]), \
                mock.patch.object(pf, "webbrowser") as browser:
            code, text = capture_result(pf.cmd_start, args(no_browser=False))
        self.assertEqual(0, code)
        self.assertIn("уже работает на порту 8080", text)
        browser.open.assert_called_once_with("http://localhost:8080/")

    def test_force_ignores_the_other_instance(self):
        """`--force` — осознанный выход: доходим до реальной проверки порта."""
        def only_the_old_instance(port):
            return {"version": "17.0.27"} if port == 8080 else None

        with mock.patch.object(pf, "resolve_port", return_value=8765), \
                mock.patch.object(pf, "running_port", return_value=8080), \
                mock.patch.object(pf, "health", side_effect=only_the_old_instance), \
                mock.patch.object(pf, "port_busy", return_value=True), \
                mock.patch.object(pf, "port_owner", return_value=""), \
                mock.patch.object(pf, "free_port", return_value=8799):
            code, text = capture_result(pf.cmd_start, args(force=True))
        self.assertEqual(1, code, "порт 8765 занят чужой программой — запуск не состоялся")
        self.assertIn("занят", text)

    def test_start_when_already_running_shows_the_phone_address(self):
        with mock.patch.object(pf, "resolve_port", return_value=8765), \
                mock.patch.object(pf, "running_port", return_value=8765), \
                mock.patch.object(pf, "port_busy", return_value=True), \
                mock.patch.object(pf, "health", return_value={"version": "17.0.27"}), \
                mock.patch.object(pf, "local_ips", return_value=["192.168.1.50"]), \
                mock.patch.object(pf, "webbrowser") as browser:
            code, text = capture_result(pf.cmd_start, args(no_browser=True))
        self.assertEqual(0, code)
        self.assertIn("уже работает", text)
        self.assertIn("http://192.168.1.50:8765/cashier.html", text)
        browser.open.assert_not_called()

    def test_help_mentions_the_phone_command(self):
        text = capture(pf.cmd_help, args())
        self.assertIn("python pf.py net", text)
        self.assertIn("8765", text)


if __name__ == "__main__":
    unittest.main()
