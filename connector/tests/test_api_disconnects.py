"""Регрессии обработки штатного отключения HTTP-клиента."""
from types import SimpleNamespace
from unittest import TestCase, mock

from connector.printflow.api import Handler


class _AbortOnWrite:
    """Сокет, имитирующий WinError 10053 при первой записи."""

    def write(self, _data: bytes) -> None:
        raise ConnectionAbortedError(10053, "client disconnected")


class _AbortAfterHeaders:
    """Принимает HTTP-заголовки, затем рвёт SSE-поток на первом ping."""

    def __init__(self):
        self.writes = 0

    def write(self, _data: bytes) -> None:
        self.writes += 1
        if self.writes > 1:
            raise ConnectionAbortedError(10053, "client disconnected")

    def flush(self) -> None:
        pass


class _EmptyEventsDatabase:
    def one(self, _query: str):
        return {"m": 0}


def _handler(wfile=None) -> Handler:
    """Создать минимальный Handler без настоящего TCP-сокета."""
    handler = Handler.__new__(Handler)
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.requestline = "GET /api/stream HTTP/1.1"
    handler.server = SimpleNamespace(flags=[])
    handler.close_connection = False
    handler.wfile = wfile
    return handler


class ClientDisconnectTests(TestCase):
    def test_send_json_silently_handles_windows_connection_abort(self):
        handler = _handler(_AbortOnWrite())

        handler.send_json(503, {"error": "irrelevant"})

        self.assertTrue(handler.close_connection)

    def test_sse_silently_handles_windows_connection_abort(self):
        stream = _AbortAfterHeaders()
        handler = _handler(stream)
        handler.api = SimpleNamespace(db=_EmptyEventsDatabase())

        handler.serve_sse()

        self.assertEqual(stream.writes, 2)
        self.assertTrue(handler.close_connection)

    def test_get_does_not_send_503_after_sse_client_disconnect(self):
        handler = _handler()
        handler.path = "/api/stream"
        handler.headers = {"Host": "localhost:8080"}
        handler.api = SimpleNamespace(last_host="")
        handler.serve_sse = mock.Mock(
            side_effect=ConnectionAbortedError(10053, "client disconnected")
        )
        handler.send_json = mock.Mock()

        handler.do_GET()

        handler.send_json.assert_not_called()
