"""The final Ollama stream packet must not erase accumulated reply text."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from agent import model


class _Response:
    def __iter__(self):
        for content, done in (('{"reply":"Привет', False), ('!"}', False), ('', True)):
            yield (json.dumps({"message": {"content": content}, "done": done}) + "\n").encode()

    def close(self):
        pass


class ModelStreamTests(unittest.TestCase):
    def test_final_empty_packet_preserves_reply(self):
        with patch.object(model._LOCAL_OPENER, "open", return_value=_Response()):
            ok, payload, reason = model._post_stream("http://127.0.0.1:11434/api/chat", {}, 5)
        self.assertTrue(ok)
        self.assertEqual("", reason)
        self.assertEqual('{"reply":"Привет!"}', payload["message"]["content"])
