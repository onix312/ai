from __future__ import annotations

import argparse
import base64
import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pf


class AutostartGenerationTests(unittest.TestCase):
    def args(self, **overrides) -> argparse.Namespace:
        values = {
            "port": 8080,
            "local": False,
            "system": False,
            "verbose": False,
            "startup_delay": 10,
            "no_autostart": False,
            "autostart_action": "status",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_parser_supports_autostart_actions_and_validates_delay(self) -> None:
        parsed = pf.build_parser().parse_args(
            ["autostart", "repair", "--port", "9000", "--local", "--startup-delay", "25"]
        )
        self.assertEqual(parsed.command, "autostart")
        self.assertEqual(parsed.autostart_action, "repair")
        self.assertEqual(parsed.port, 9000)
        self.assertTrue(parsed.local)
        self.assertEqual(parsed.startup_delay, 25)
        with self.assertRaises(SystemExit):
            pf.build_parser().parse_args(["install", "--startup-delay", "301"])

    def test_service_command_persists_install_options(self) -> None:
        args = self.args(port=9123, local=True, system=True, verbose=True, startup_delay=17)
        command = pf.service_command(args)
        self.assertEqual(command[1:3], [str(pf.ROOT / "pf.py"), "service"])
        self.assertIn("9123", command)
        self.assertIn("17", command)
        self.assertIn("--local", command)
        self.assertIn("--system", command)
        self.assertIn("--verbose", command)
        self.assertNotIn("--background", command)

    def test_systemd_unit_quotes_paths_and_supervises_foreground_service(self) -> None:
        original_root = pf.ROOT
        try:
            pf.ROOT = Path('/tmp/Print Flow/%instance/with "quote"')
            unit = pf.render_systemd_unit(["/usr/bin/python 3", str(pf.ROOT / "pf.py"), "service"])
        finally:
            pf.ROOT = original_root
        self.assertIn("Wants=network-online.target", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("TimeoutStopSec=30", unit)
        self.assertIn("%%instance", unit)
        self.assertIn('\\"quote\\"', unit)
        self.assertIn(r'WorkingDirectory=/tmp/Print\x20Flow/%%instance/with\x20\x22quote\x22',
                      unit)
        self.assertNotIn('WorkingDirectory="', unit)
        self.assertNotIn("--background", unit)

    def test_launchd_plist_is_valid_with_xml_metacharacters(self) -> None:
        original_root, original_log = pf.ROOT, pf.RUN_LOG
        try:
            pf.ROOT = Path("/tmp/PrintFlow & QA <local>")
            pf.RUN_LOG = Path("/tmp/log & output.txt")
            payload = plistlib.dumps(
                pf.launchd_configuration(["/usr/bin/python3", str(pf.ROOT / "pf.py"), "service"])
            )
            parsed = plistlib.loads(payload)
        finally:
            pf.ROOT, pf.RUN_LOG = original_root, original_log
        self.assertEqual(parsed["Label"], pf.AUTOSTART_LABEL)
        self.assertEqual(parsed["WorkingDirectory"], "/tmp/PrintFlow & QA <local>")
        self.assertEqual(parsed["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(parsed["RunAtLoad"])

    def test_windows_task_script_escapes_values_as_powershell_literals(self) -> None:
        original_root = pf.ROOT
        try:
            pf.ROOT = Path("C:/Users/O'Brien/Print Flow")
            script = pf.render_windows_task_script(
                ["C:/Python/pythonw.exe", str(pf.ROOT / "pf.py"), "service", "--port", "8080"]
            )
        finally:
            pf.ROOT = original_root
        self.assertIn("O''Brien", script)
        self.assertIn("Register-ScheduledTask", script)
        self.assertIn("-RestartCount 3", script)
        self.assertIn("-MultipleInstances IgnoreNew", script)
        self.assertIn("Start-ScheduledTask", script)

    def test_powershell_runner_uses_encoded_command_not_inline_script(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(pf, "_powershell_executable", return_value="powershell.exe"), \
             mock.patch.object(pf.subprocess, "run", return_value=completed) as run:
            pf.run_powershell("Write-Output 'Привет'")
        command = run.call_args.args[0]
        self.assertIn("-EncodedCommand", command)
        encoded = command[command.index("-EncodedCommand") + 1]
        self.assertEqual(base64.b64decode(encoded).decode("utf-16le"), "Write-Output 'Привет'")
        self.assertNotIn("Write-Output 'Привет'", command)

    def test_xdg_entry_exec_is_not_wrapped_in_a_shell(self) -> None:
        original_root = pf.ROOT
        try:
            pf.ROOT = Path("/opt/Print Flow/100%")
            entry = pf.render_xdg_entry(
                ["/opt/Print Flow/python", "/opt/Print Flow/100%/pf.py", "service"])
        finally:
            pf.ROOT = original_root
        self.assertIn('Exec="/opt/Print Flow/python" "/opt/Print Flow/100%%/pf.py" "service"',
                      entry)
        self.assertIn("Path=/opt/Print Flow/100%", entry)
        self.assertNotIn('Path="', entry)
        self.assertIn("X-GNOME-Autostart-enabled=true", entry)
        self.assertNotIn("sh -c", entry)


class AutostartOperationsTests(unittest.TestCase):
    def args(self, **overrides) -> argparse.Namespace:
        values = {
            "port": 8080,
            "local": False,
            "system": False,
            "verbose": False,
            "startup_delay": 10,
            "no_autostart": False,
            "autostart_action": "status",
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_service_execs_connector_directly_and_clears_pid_on_exec_error(self) -> None:
        args = self.args(startup_delay=0, system=True, port=8765, local=True)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(pf, "DATA_DIR", Path(tmp) / "data"), \
             mock.patch.object(pf, "health", return_value=None), \
             mock.patch.object(pf, "port_busy", return_value=False), \
             mock.patch.object(pf, "interpreter", return_value=Path("/test/python")), \
             mock.patch.object(pf, "write_pid") as write_pid, \
             mock.patch.object(pf, "clear_pid") as clear_pid, \
             mock.patch.object(pf.os, "chdir"), \
             mock.patch.object(pf.os, "execv", side_effect=OSError("exec denied")) as execv:
            result = pf.cmd_service(args)
        self.assertEqual(result, 1)
        write_pid.assert_called_once()
        clear_pid.assert_called_once()
        command = execv.call_args.args[1]
        self.assertEqual(command[0], "/test/python")
        self.assertIn(str(pf.ENTRYPOINT), command)
        self.assertIn("127.0.0.1", command)
        self.assertIn("--no-browser", command)
        self.assertNotIn("--background", command)

    def test_enable_rolls_back_backend_when_config_cannot_be_saved(self) -> None:
        with mock.patch.object(pf, "IS_WINDOWS", False), \
             mock.patch.object(pf, "IS_MACOS", False), \
             mock.patch.object(pf, "_enable_linux_autostart",
                               return_value=(True, "systemd-user", "ok")), \
             mock.patch.object(pf, "_atomic_write", side_effect=OSError("disk full")), \
             mock.patch.object(pf, "_disable_linux_autostart", return_value=(True, "")) as disable:
            success, mechanism, detail = pf.enable_autostart(self.args())
        self.assertFalse(success)
        self.assertEqual(mechanism, "systemd-user")
        self.assertIn("disk full", detail)
        disable.assert_called_once_with()

    def test_enable_writes_config_only_after_backend_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "state/autostart.json"
            with mock.patch.object(pf, "AUTOSTART_CONFIG", config), \
                 mock.patch.object(pf, "IS_WINDOWS", False), \
                 mock.patch.object(pf, "IS_MACOS", False), \
                 mock.patch.object(pf, "_enable_linux_autostart",
                                   return_value=(True, "systemd-user", "ok")):
                success, mechanism, _ = pf.enable_autostart(self.args(port=9010, local=True))
                loaded = json.loads(config.read_text(encoding="utf-8"))
            self.assertTrue(success)
            self.assertEqual(mechanism, "systemd-user")
            self.assertEqual(loaded["port"], 9010)
            self.assertTrue(loaded["local"])
            self.assertEqual(loaded["root"], str(pf.ROOT))

            config.unlink()
            with mock.patch.object(pf, "AUTOSTART_CONFIG", config), \
                 mock.patch.object(pf, "IS_WINDOWS", False), \
                 mock.patch.object(pf, "IS_MACOS", False), \
                 mock.patch.object(pf, "_enable_linux_autostart",
                                   return_value=(False, "systemd-user", "denied")):
                success, _, _ = pf.enable_autostart(self.args())
            self.assertFalse(success)
            self.assertFalse(config.exists())

    def test_windows_fallback_removes_old_task_before_creating_startup_link(self) -> None:
        failed = subprocess.CompletedProcess([], 1, "", "task cmdlets unavailable")
        cleaned = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(pf.os.environ, {"APPDATA": tmp}), \
             mock.patch.object(pf, "health", return_value=None), \
             mock.patch.object(pf, "run_powershell", side_effect=[failed, cleaned]) as powershell, \
             mock.patch.object(pf, "create_windows_shortcut", return_value=(True, "")) as shortcut:
            success, mechanism, detail = pf._enable_windows_autostart(self.args())
        self.assertTrue(success)
        self.assertEqual(mechanism, "windows-startup")
        self.assertIn("Startup", detail)
        self.assertEqual(powershell.call_count, 2)
        self.assertIn("Unregister-ScheduledTask", powershell.call_args_list[1].args[0])
        shortcut.assert_called_once()

    def test_macos_uses_bootstrap_with_legacy_load_fallback(self) -> None:
        failed = subprocess.CompletedProcess([], 1, "", "bootstrap failed")
        succeeded = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.object(Path, "home", return_value=home), \
                 mock.patch.object(pf, "DATA_DIR", home / "data"), \
                 mock.patch.object(pf, "RUN_LOG", home / "data/launcher.log"), \
                 mock.patch.object(pf.subprocess, "run",
                                   side_effect=[succeeded, succeeded, failed, succeeded]) as run:
                success, mechanism, _ = pf._enable_macos_autostart(self.args())
            plist = home / "Library/LaunchAgents/ru.nozza.printflow.plist"
            parsed = plistlib.loads(plist.read_bytes())
        self.assertTrue(success)
        self.assertEqual(mechanism, "launchd")
        self.assertEqual(parsed["Label"], pf.AUTOSTART_LABEL)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][1], "bootout")
        self.assertEqual(commands[1][1], "enable")
        self.assertEqual(commands[2][1], "bootstrap")
        self.assertEqual(commands[3][1:3], ["load", "-w"])

    def test_linux_uses_systemd_enable_now_when_user_manager_is_available(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(pf, "xdg_config_home", return_value=Path(tmp) / ".config"), \
             mock.patch.object(pf, "_systemd_available", return_value=(True, "")), \
             mock.patch.object(pf.subprocess, "run", return_value=completed) as run:
            success, mechanism, _ = pf._enable_linux_autostart(self.args())
            unit = Path(tmp) / ".config/systemd/user/printflow.service"
            fallback = Path(tmp) / ".config/autostart/printflow.desktop"
            self.assertTrue(unit.exists())
            self.assertFalse(fallback.exists())
        self.assertTrue(success)
        self.assertEqual(mechanism, "systemd-user")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["systemctl", "--user", "daemon-reload"], commands)
        self.assertIn(["systemctl", "--user", "enable", "--now", "printflow.service"], commands)

    def test_linux_falls_back_to_xdg_when_user_systemd_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(pf, "xdg_config_home", return_value=Path(tmp) / ".config"), \
             mock.patch.object(pf, "_systemd_available", return_value=(False, "no user bus")):
            success, mechanism, detail = pf._enable_linux_autostart(self.args())
            fallback = Path(tmp) / ".config/autostart/printflow.desktop"
            content = fallback.read_text(encoding="utf-8")
        self.assertTrue(success)
        self.assertEqual(mechanism, "xdg-autostart")
        self.assertIn("no user bus", detail)
        self.assertIn('"service"', content)

    def test_reinstall_replaces_xdg_fallback_when_systemd_recovers(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            fallback = home / ".config/autostart/printflow.desktop"
            fallback.parent.mkdir(parents=True)
            fallback.write_text("old", encoding="utf-8")
            with mock.patch.object(pf, "xdg_config_home", return_value=home / ".config"), \
                 mock.patch.object(pf, "_systemd_available", return_value=(True, "")), \
                 mock.patch.object(pf.subprocess, "run", return_value=completed):
                success, mechanism, _ = pf._enable_linux_autostart(self.args())
            self.assertTrue(success)
            self.assertEqual(mechanism, "systemd-user")
            self.assertFalse(fallback.exists())

    def test_install_returns_nonzero_for_partial_failure(self) -> None:
        args = self.args()
        with mock.patch.object(pf, "ensure_venv"), \
             mock.patch.object(pf, "IS_WINDOWS", False), \
             mock.patch.object(pf, "IS_MACOS", False), \
             mock.patch.object(pf, "install_linux", return_value=(["menu"], ["desktop denied"])), \
             mock.patch.object(pf, "enable_autostart",
                               return_value=(False, "systemd-user", "user bus denied")):
            result = pf.cmd_install(args)
        self.assertEqual(result, 1)

    def test_repair_preserves_saved_port_and_network_mode(self) -> None:
        args = self.args(autostart_action="repair")
        saved = {
            "port": 9777,
            "local": True,
            "system": True,
            "verbose": True,
            "startup_delay": 42,
        }
        with mock.patch.object(pf, "load_autostart_config", return_value=saved), \
             mock.patch.object(pf, "ensure_venv"), \
             mock.patch.object(pf, "enable_autostart", return_value=(True, "test", "ok")) as enable, \
             mock.patch.object(pf, "autostart_status",
                               return_value={"installed": True, "enabled": True,
                                             "running": False, "mechanism": "test",
                                             "port": 9777, "root_matches": True, "detail": ""}):
            result = pf.cmd_autostart(args)
        self.assertEqual(result, 0)
        installed_args = enable.call_args.args[0]
        self.assertEqual(installed_args.port, 9777)
        self.assertTrue(installed_args.local)
        self.assertEqual(installed_args.startup_delay, 42)

    def test_install_no_autostart_actively_disables_previous_setup(self) -> None:
        args = self.args(no_autostart=True)
        with mock.patch.object(pf, "ensure_venv"), \
             mock.patch.object(pf, "IS_WINDOWS", False), \
             mock.patch.object(pf, "IS_MACOS", False), \
             mock.patch.object(pf, "install_linux", return_value=(["menu"], [])), \
             mock.patch.object(pf, "disable_autostart", return_value=(True, "")) as disable, \
             mock.patch.object(pf, "enable_autostart") as enable:
            result = pf.cmd_install(args)
        self.assertEqual(result, 0)
        disable.assert_called_once_with()
        enable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
