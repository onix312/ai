"""Read-only perception should report its actual source and fail safely."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from agent import perception


WINDOW = {"hwnd": 42, "title": "Example", "process": "example", "minimized": False}


class PerceptionTests(unittest.TestCase):
    def test_prefers_uia_without_capturing_screen(self):
        with patch.object(perception.pc, "find_window", return_value=(WINDOW, "")), \
             patch("agent.capabilities.detect", return_value={"uia": True}), \
             patch.object(perception, "_worker", return_value=({"controls": [
                 {"text": "Save", "type": "Button"}], "focused": {"text": "Save"}}, "")) as worker, \
             patch.object(perception, "read_screen_text") as ocr:
            result = perception.observe()
        self.assertEqual(result["sources"], ["win32", "uia"])
        self.assertEqual(result["focused"]["text"], "Save")
        self.assertNotIn("screenshot", result)
        worker.assert_called_once()
        ocr.assert_not_called()

    def test_falls_back_after_uia_timeout(self):
        with patch.object(perception.pc, "find_window", return_value=(WINDOW, "")), \
             patch("agent.capabilities.detect", return_value={"uia": True}), \
             patch.object(perception, "_worker", return_value=({}, "UIA не ответил вовремя")), \
             patch.object(perception.pc, "controls", return_value=([{"text": "Open"}], "")):
            result = perception.observe()
        self.assertEqual(result["sources"], ["win32", "win32_controls"])
        self.assertIn("UIA не ответил вовремя", result["warnings"])

    def test_ocr_only_when_structured_text_missing(self):
        with patch.object(perception.pc, "find_window", return_value=(WINDOW, "")), \
             patch("agent.capabilities.detect", return_value={"uia": False}), \
             patch.object(perception.pc, "controls", return_value=([], "")), \
             patch.object(perception, "read_screen_text", return_value=([{"text": "Ready"}], "")):
            result = perception.observe()
        self.assertEqual(result["sources"], ["win32", "ocr"])
        self.assertEqual(result["ocr"][0]["text"], "Ready")

    def test_search_reports_actual_method(self):
        with patch.object(perception.pc, "find_window", return_value=(WINDOW, "")), \
             patch.object(perception.pc, "windows", return_value=([WINDOW], "")), \
             patch("agent.capabilities.detect", return_value={"uia": False}), \
             patch.object(perception.pc, "controls", return_value=([], "")), \
             patch.object(perception, "read_screen_text", return_value=([{"text": "Print queue"}], "")):
            found, reason = perception.find_text("queue")
        self.assertEqual(reason, "")
        self.assertEqual(found["method"], "ocr")

    def test_worker_timeout_does_not_block_agent(self):
        from subprocess import TimeoutExpired
        with patch.object(perception.subprocess, "run", side_effect=TimeoutExpired("uia", 6)):
            data, reason = perception._worker("uia", hwnd=42)
        self.assertEqual(data, {})
        self.assertIn("не ответил вовремя", reason)


if __name__ == "__main__":
    unittest.main()
