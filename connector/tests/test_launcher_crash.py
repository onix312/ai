"""Лаунчер не имеет права исчезать молча (18.6.4).

Жалоба владельца: «вместо открытия появляется чёрная панель и сразу
закрывается». Окно, открытое двойным кликом, живёт ровно столько, сколько
процесс: трассировка уходила в консоль, которая закрывалась вместе с pf.py, а
при запуске из ярлыка (`pythonw.exe`, его ставит `pf.py install`) консоли не
было вовсе — `Style.setup()` падал на `sys.stdout.isatty()` в первой же строке
`main()`, и PrintFlow не подавал никаких признаков жизни.

Здесь закреплено то, что проверяется без Windows:

* оформление переживает запуск без консоли;
* сбой команды пишется в журнал запуска, печатается и возвращает код 1;
* когда печатать некуда — показывается системное окно;
* `pf.py gui` без tkinter и без консоли открывает панель в браузере, а не
  завершается молча;
* текстовое меню без ввода не притворяется удачным завершением;
* консоль держится только тогда, когда её открыл Проводник, а не человек;
* «браузер не открылся» больше не выглядит как запуск без ошибки.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pf  # noqa: E402


def args(**overrides) -> argparse.Namespace:
    values = {"port": 8765, "local": False, "no_qr": True, "json": False, "force": False,
              "no_browser": False, "auto_port": False, "system": False, "verbose": False,
              "background": False, "startup_delay": 10, "lines": 20, "file": "",
              "no_autostart": False, "autostart_action": "status", "want_help": False,
              "watchdog": False, "no_autostart_server": False}
    values.update(overrides)
    return argparse.Namespace(**values)


class HeadlessSetupTests(unittest.TestCase):
    """Ярлык на рабочем столе запускает pythonw.exe — без консоли."""

    def test_style_setup_survives_missing_console(self) -> None:
        original = pf.Style.enabled
        try:
            with mock.patch.object(sys, "stdout", None), \
                    mock.patch.object(sys, "stderr", None):
                pf.Style.setup()  # раньше здесь был AttributeError
                self.assertFalse(pf.Style.enabled)
        finally:
            pf.Style.enabled = original

    def test_console_helpers_agree_with_the_streams(self) -> None:
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertTrue(pf.console_alive())
        with mock.patch.object(sys, "stdout", None):
            self.assertFalse(pf.console_alive())

    def test_interactive_console_needs_a_live_terminal(self) -> None:
        with mock.patch.object(sys, "stdin", None):
            self.assertFalse(pf.interactive_console())
        with mock.patch.object(sys, "stdin", io.StringIO()):
            self.assertFalse(pf.interactive_console(),
                             "перенаправленный ввод — ещё не диалог")

    def test_gui_without_console_does_not_die_before_the_window(self) -> None:
        commands = dict(pf.COMMANDS)
        commands["gui"] = mock.Mock(return_value=0)
        with mock.patch.object(sys, "stdout", None), \
                mock.patch.object(sys, "stderr", None), \
                mock.patch.object(pf, "COMMANDS", commands):
            self.assertEqual(0, pf.main(["gui"]))


class CrashReportTests(unittest.TestCase):
    def test_crash_log_keeps_the_command_and_the_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "launcher.log"
            try:
                raise ValueError("окно не поднялось")
            except ValueError as exc:
                with mock.patch.object(pf, "RUN_LOG", log):
                    written = pf.write_crash_log(exc, "gui")
            text = log.read_text(encoding="utf-8")
        self.assertEqual(log, written)
        self.assertIn("pf.py gui", text)
        self.assertIn("ValueError: окно не поднялось", text)
        self.assertIn("Traceback", text)

    def test_crash_log_does_not_break_on_an_unwritable_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = pathlib.Path(tmp) / "launcher.log"
            blocker.write_text("это файл, а не папка", encoding="utf-8")
            target = blocker / "launcher.log"
            try:
                raise RuntimeError("нет доступа")
            except RuntimeError as exc:
                with mock.patch.object(pf, "RUN_LOG", target):
                    self.assertEqual(target, pf.write_crash_log(exc, "gui"))
        self.assertFalse(target.exists())

    def test_main_returns_one_and_logs_the_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "launcher.log"
            commands = dict(pf.COMMANDS)
            commands["start"] = mock.Mock(side_effect=RuntimeError("нет консоли"))
            out = io.StringIO()
            with mock.patch.object(pf, "COMMANDS", commands), \
                    mock.patch.object(pf, "RUN_LOG", log), \
                    mock.patch.object(pf, "hold_console") as hold, \
                    mock.patch.object(pf, "show_message") as dialog, \
                    contextlib.redirect_stdout(out):
                code = pf.main(["start"])
            text = log.read_text(encoding="utf-8")
        self.assertEqual(1, code)
        hold.assert_called_once()
        dialog.assert_not_called()
        self.assertIn("нет консоли", text)
        printed = out.getvalue()
        self.assertIn("нет консоли", printed)
        self.assertIn("python pf.py doctor", printed)

    def test_failure_without_console_is_shown_as_a_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "launcher.log"
            commands = dict(pf.COMMANDS)
            commands["start"] = mock.Mock(side_effect=RuntimeError("нет консоли"))
            with mock.patch.object(pf, "COMMANDS", commands), \
                    mock.patch.object(pf, "RUN_LOG", log), \
                    mock.patch.object(pf, "hold_console"), \
                    mock.patch.object(pf, "show_message") as dialog, \
                    mock.patch.object(sys, "stdout", None), \
                    mock.patch.object(sys, "stderr", None):
                code = pf.main(["start"])
        self.assertEqual(1, code)
        dialog.assert_called_once()
        title, text = dialog.call_args.args
        self.assertIn("start", title)
        self.assertIn("doctor", text)

    def test_main_keeps_the_system_exit_code(self) -> None:
        commands = dict(pf.COMMANDS)
        commands["start"] = mock.Mock(side_effect=SystemExit(1))
        with mock.patch.object(pf, "COMMANDS", commands), \
                mock.patch.object(pf, "hold_console") as hold, \
                mock.patch.object(pf, "show_message"), \
                contextlib.redirect_stdout(io.StringIO()):
            code = pf.main(["start"])
        self.assertEqual(1, code)
        hold.assert_called_once()

    def test_successful_system_exit_does_not_hold_the_window(self) -> None:
        commands = dict(pf.COMMANDS)
        commands["start"] = mock.Mock(side_effect=SystemExit(0))
        with mock.patch.object(pf, "COMMANDS", commands), \
                mock.patch.object(pf, "hold_console") as hold, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, pf.main(["start"]))
        hold.assert_not_called()


class GuiFallbackTests(unittest.TestCase):
    """Окно не поднялось — панель всё равно должна открыться."""

    def test_without_tkinter_and_console_the_panel_opens_in_browser(self) -> None:
        with mock.patch.dict(sys.modules, {"tkinter": None}), \
                mock.patch.object(pf, "interactive_console", return_value=False), \
                mock.patch.object(pf, "cmd_start", return_value=0) as start, \
                mock.patch.object(pf, "cmd_menu") as menu, \
                contextlib.redirect_stdout(io.StringIO()):
            code = pf.cmd_gui(args())
        self.assertEqual(0, code)
        start.assert_called_once()
        menu.assert_not_called()

    def test_without_tkinter_but_in_a_console_the_menu_is_offered(self) -> None:
        with mock.patch.dict(sys.modules, {"tkinter": None}), \
                mock.patch.object(pf, "interactive_console", return_value=True), \
                mock.patch.object(pf, "cmd_start") as start, \
                mock.patch.object(pf, "cmd_menu", return_value=0) as menu, \
                contextlib.redirect_stdout(io.StringIO()):
            code = pf.cmd_gui(args())
        self.assertEqual(0, code)
        menu.assert_called_once()
        start.assert_not_called()

    def test_broken_window_falls_back_to_the_browser(self) -> None:
        broken = types.ModuleType("launcher_window")
        broken.run_window = mock.Mock(side_effect=RuntimeError("no display name"))
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "launcher.log"
            with mock.patch.dict(sys.modules, {"tkinter": types.ModuleType("tkinter"),
                                               "launcher_window": broken}), \
                    mock.patch.object(pf, "RUN_LOG", log), \
                    mock.patch.object(pf, "cmd_start", return_value=0) as start, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                code = pf.cmd_gui(args())
            text = log.read_text(encoding="utf-8")
        self.assertEqual(0, code)
        start.assert_called_once()
        self.assertIn("no display name", out.getvalue())
        self.assertIn("no display name", text,
                      "причина сбоя окна должна остаться в журнале запуска")

    def test_menu_without_input_is_a_failure_not_a_silent_exit(self) -> None:
        with mock.patch.object(pf, "interactive_console", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = pf.cmd_menu(args())
        self.assertEqual(1, code, "меню без ввода раньше возвращало 0 — окно гасло молча")
        self.assertIn("python pf.py", out.getvalue())


class HoldConsoleTests(unittest.TestCase):
    def test_holds_when_the_explorer_opened_the_window(self) -> None:
        with mock.patch.object(pf, "IS_WINDOWS", True), \
                mock.patch.object(pf, "interactive_console", return_value=True), \
                mock.patch.object(pf, "parent_process_name", return_value="explorer.exe"), \
                mock.patch("builtins.input", return_value="") as prompt, \
                contextlib.redirect_stdout(io.StringIO()):
            pf.hold_console()
        prompt.assert_called_once()

    def test_stays_silent_in_a_terminal_opened_by_a_human(self) -> None:
        with mock.patch.object(pf, "IS_WINDOWS", True), \
                mock.patch.object(pf, "interactive_console", return_value=True), \
                mock.patch.object(pf, "parent_process_name", return_value="cmd.exe"), \
                mock.patch("builtins.input", side_effect=AssertionError("ждать не нужно")) as prompt, \
                contextlib.redirect_stdout(io.StringIO()):
            pf.hold_console()
        prompt.assert_not_called()

    def test_is_not_needed_outside_windows(self) -> None:
        with mock.patch.object(pf, "IS_WINDOWS", False), \
                mock.patch("builtins.input", side_effect=AssertionError("не Windows")):
            pf.hold_console()

    def test_parent_is_never_asked_when_console_is_absent(self) -> None:
        with mock.patch.object(pf, "IS_WINDOWS", True), \
                mock.patch.object(pf, "interactive_console", return_value=False), \
                mock.patch.object(pf, "parent_process_name",
                                  side_effect=AssertionError("консоли нет")):
            pf.hold_console()


class BrowserOpeningTests(unittest.TestCase):
    def test_open_in_browser_reports_the_failure(self) -> None:
        with mock.patch.object(pf.webbrowser, "open", return_value=True):
            self.assertTrue(pf.open_in_browser("http://localhost:8765/"))
        with mock.patch.object(pf.webbrowser, "open", return_value=False):
            self.assertFalse(pf.open_in_browser("http://localhost:8765/"))
        with mock.patch.object(pf.webbrowser, "open", side_effect=OSError("нет браузера")):
            self.assertFalse(pf.open_in_browser("http://localhost:8765/"))

    def test_running_server_with_a_silent_browser_is_reported(self) -> None:
        """PrintFlow работает, а панель не открылась — это надо показать."""
        with mock.patch.object(pf, "check_python_version"), \
                mock.patch.object(pf, "resolve_port", return_value=8765), \
                mock.patch.object(pf, "running_port", return_value=8766), \
                mock.patch.object(pf, "health", return_value={"version": "18.6.4"}), \
                mock.patch.object(pf, "say_phone_hint"), \
                mock.patch.object(pf, "open_in_browser", return_value=False), \
                mock.patch.object(pf, "hold_console") as hold, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = pf.cmd_start(args(port=8765))
        self.assertEqual(1, code)
        hold.assert_called_once()
        self.assertIn("Браузер не открылся", out.getvalue())


if __name__ == "__main__":
    unittest.main()
