"""Packaging 1.0 contracts for the standalone Luma Windows app."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from agent import desktop

ROOT = pathlib.Path(__file__).resolve().parents[2]


class DesktopHostTests(unittest.TestCase):
    def test_packaged_defaults_find_assets_next_to_install_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            app = root / "app"
            model = root / "models" / "tts" / "luma.onnx"
            piper = root / "piper" / "piper.exe"
            app.mkdir(parents=True)
            model.parent.mkdir(parents=True)
            piper.parent.mkdir(parents=True)
            (app / "Luma.exe").write_bytes(b"MZ")
            model.write_bytes(b"model")
            piper.write_bytes(b"MZ")

            with patch.object(desktop, "_bundle_root", return_value=app), \
                 patch.dict("os.environ", {}, clear=True):
                desktop._apply_packaged_defaults()
                import os
                self.assertEqual(str(model), os.environ.get("LUMA_TTS_MODEL_PATH"))
                self.assertEqual(str(piper), os.environ.get("LUMA_TTS_PIPER"))

    def test_desktop_host_reuses_live_backend(self):
        fake_app = types.ModuleType("agent.native_ui.app")
        calls = []
        fake_app.run = lambda: calls.append("ui") or 0
        with patch.object(desktop, "_apply_packaged_defaults"), \
             patch.object(desktop, "_agent_alive", return_value=True), \
             patch.dict(sys.modules, {"agent.native_ui.app": fake_app}), \
             patch.object(desktop._OwnedBackend, "start") as start:
            self.assertEqual(0, desktop.main())
        self.assertEqual(["ui"], calls)
        start.assert_not_called()


class PackagingFilesTests(unittest.TestCase):
    def test_build_and_installer_contracts_exist(self):
        required = (
            ROOT / "luma.py",
            ROOT / "agent" / "luma.spec",
            ROOT / "scripts" / "build_luma_windows.ps1",
            ROOT / "scripts" / "install_luma_windows.ps1",
        )
        for path in required:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())

    def test_installer_is_per_user_and_preserves_data_root(self):
        text = (ROOT / "scripts" / "install_luma_windows.ps1").read_text(encoding="utf-8")
        self.assertIn('$env:LOCALAPPDATA "Luma"', text)
        self.assertIn("Microsoft\\Windows\\Start Menu\\Programs\\Startup", text)
        self.assertNotIn("Remove-Item -Recurse -Force $installRoot", text)

    def test_build_outputs_package_with_app_and_installer(self):
        text = (ROOT / "scripts" / "build_luma_windows.ps1").read_text(encoding="utf-8")
        self.assertIn('"LumaPackage"', text)
        self.assertIn('"app"', text)
        self.assertIn('"install.ps1"', text)
        self.assertIn('"INSTALL-LUMA.bat"', text)
        self.assertIn("PyInstaller", text)


if __name__ == "__main__":
    unittest.main()
