"""The optional browser extension uses the same read-only browser skills as CDP."""
import threading
import json
import urllib.error
import urllib.request
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from agent import browser, browser_provider, server


class BrowserBridgeTests(unittest.TestCase):
    def test_pairing_and_read_only_command_exchange(self):
        bridge = browser_provider.BrowserBridge("test-secret")
        self.assertTrue(bridge.paired("test-secret"))
        self.assertFalse(bridge.paired("other"))
        bridge.exchange({"op": "poll", "client_id": "one", "browser": "yandex"})
        result = {}

        def request():
            result.update(bridge.request("observe", {"browser": "yandex", "tab_id": 7}, timeout=2))

        thread = threading.Thread(target=request)
        thread.start()
        command = None
        for _ in range(100):
            response = bridge.exchange({"op": "poll", "client_id": "one", "browser": "yandex"})
            command = response.get("command")
            if command:
                break
            thread.join(0.01)
        self.assertIsNotNone(command)
        self.assertEqual("observe", command["kind"])
        bridge.exchange({"op": "result", "client_id": "one", "browser": "yandex",
                         "id": command["id"], "result": {"ok": True}})
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual({"ok": True}, result)
        self.assertFalse(bridge.request("fill", {"tab_id": 7})["ok"])

    def test_bridge_origin_rejects_web_pages(self):
        handler = server.AgentHandler.__new__(server.AgentHandler)
        handler.role = "agent"
        handler.client_address = ("127.0.0.1", 12345)
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 8799))
        extension = "chrome-extension://" + "a" * 32
        handler.headers = {"Host": "127.0.0.1:8799", "Origin": extension}
        self.assertEqual(extension, handler._browser_extension_origin())
        handler.headers["Origin"] = "https://example.com"
        self.assertEqual("", handler._browser_extension_origin())
        handler.headers["Origin"] = extension
        handler.headers["Host"] = "example.com:8799"
        self.assertEqual("", handler._browser_extension_origin())

    def test_http_bridge_requires_extension_origin_and_key(self):
        bridge = browser_provider.BrowserBridge("test-secret")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._handler("agent", object()))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/browser/bridge"
        extension = "chrome-extension://" + "a" * 32

        def post(origin: str, token: str):
            request = urllib.request.Request(url, data=json.dumps({
                "op": "poll", "client_id": "one", "browser": "chrome", "token": token,
            }).encode(), headers={"Origin": origin, "Content-Type": "text/plain"})
            return urllib.request.urlopen(request, timeout=2)

        try:
            with patch.object(server.browser_provider, "bridge", bridge):
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    post("https://example.com", "test-secret")
                self.assertEqual(403, denied.exception.code)
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    post(extension, "wrong")
                self.assertEqual(403, denied.exception.code)
                with post(extension, "test-secret") as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual(extension, response.headers["Access-Control-Allow-Origin"])
                    self.assertTrue(json.load(response)["ok"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)


class BrowserExtensionFallbackTests(unittest.TestCase):
    def test_extension_makes_browser_capability_available(self):
        with patch.object(browser.bridge, "connected", return_value=True):
            self.assertEqual((True, ""), browser.probe())

    def test_tabs_have_browser_qualified_ids(self):
        with patch.object(browser.bridge, "connected", return_value=True), \
             patch.object(browser.bridge, "sessions", return_value=[{"id": "one", "browser": "edge"}]), \
             patch.object(browser.bridge, "request", return_value={
                 "ok": True, "tabs": [{"id": 7, "title": "Example", "url": "https://example.test", "active": True}]}):
            result = browser.tabs()
        self.assertTrue(result["ok"])
        self.assertEqual("edge:7", result["tabs"][0]["id"])

    def test_page_excludes_form_values_and_uses_selected_tab(self):
        payload = {"ok": True, "tab": {"id": 7, "title": "Example", "url": "https://example.test"},
                   "page": {"text": "Hello", "selected_text": "Hello", "links": [],
                            "buttons": [], "fields": [{"type": "text", "label": "Name"}]}}
        with patch.object(browser.bridge, "connected", return_value=True), \
             patch.object(browser.bridge, "request", return_value=payload) as request:
            result = browser.page("edge:7")
        self.assertEqual("edge:7", result["target_id"])
        self.assertEqual("Hello", result["selection"])
        self.assertEqual("Name", result["forms"][0]["fields"][0]["label"])
        request.assert_called_once_with("observe", {"browser": "edge", "tab_id": 7})


if __name__ == "__main__":
    unittest.main()
