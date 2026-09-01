"""Общий слой Telegram (идеи 73, 74, 76): транспорт, журнал, очередь, команды.

Раньше у рабочего и клиентского ботов были свои копии этих трёх подсистем,
и надёжная доставка была только у клиентского: ответы сотруднику терялись
при обрыве сети. Здесь проверяется общее поведение обоих ботов.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.db import Database  # noqa: E402
from connector.printflow.tg import (  # noqa: E402
    Outbox, Transport, UpdateLedger, keyboard, money_text, parse_command)


class TransportTests(unittest.TestCase):
    def test_without_token_no_call_is_made(self):
        """Пустой токен — нет сети: бот просто молчит."""
        transport = Transport(lambda: "")
        self.assertEqual(transport.call("getUpdates", {"offset": 0}), {})
        self.assertEqual(transport.calls, 0)

    def test_stats_report_name_and_errors(self):
        transport = Transport(lambda: "токен", name="staff")
        transport.call("getMe", {})     # сети нет → ошибка, но не исключение
        stats = transport.stats()
        self.assertEqual(stats["name"], "staff")
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["errors"], 1)

    def test_download_without_file_id_is_none(self):
        transport = Transport(lambda: "токен")
        self.assertIsNone(transport.download_file(""))

    def test_download_uses_injected_caller(self):
        """Боты подменяют `_call`; скачивание обязано идти через подмену."""
        seen = []

        def fake(method, params, timeout=35):
            seen.append((method, params))
            return {"ok": True, "result": {}}

        transport = Transport(lambda: "токен")
        self.assertIsNone(transport.download_file("abc", call=fake))
        self.assertEqual(seen, [("getFile", {"file_id": "abc"})])

    def test_send_message_truncates_long_text(self):
        sent = []
        transport = Transport(lambda: "токен")
        transport.call = lambda method, params, timeout=35: sent.append((method, params)) or {}
        transport.send_message(555, "а" * 9000)
        method, params = sent[0]
        self.assertEqual(method, "sendMessage")
        self.assertLessEqual(len(params["text"]), 4096)
        self.assertEqual(params["chat_id"], 555)


class UpdateLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.ledger = UpdateLedger(self.db, "telegram_bot_updates")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_first_claim_wins_second_is_busy(self):
        update = {"update_id": "11"}
        self.assertIs(self.ledger.claim(update), True)
        self.assertIs(self.ledger.claim(update), False)

    def test_finished_update_is_skipped_not_repeated(self):
        """Повторная доставка от Telegram не повторяет побочный эффект."""
        update = {"update_id": "12"}
        self.ledger.claim(update)
        self.ledger.finish("12", True)
        self.assertIsNone(self.ledger.claim(update))

    def test_failed_update_can_be_reclaimed(self):
        update = {"update_id": "13"}
        self.ledger.claim(update)
        self.ledger.finish("13", False, "сеть")
        self.assertIs(self.ledger.claim(update), True)

    def test_missing_update_id_is_always_processable(self):
        self.assertIs(self.ledger.claim({}), True)

    def test_stuck_processing_is_reset(self):
        self.db.execute(
            "UPDATE telegram_bot_updates SET received_at=datetime('now','-1 hour')"
            " WHERE update_id='14'") if False else None
        self.ledger.claim({"update_id": "14"})
        self.db.execute(
            "UPDATE telegram_bot_updates SET received_at='2020-01-01T00:00:00+00:00'"
            " WHERE update_id='14'")
        self.assertIs(self.ledger.claim({"update_id": "14"}), True)

    def test_reset_stuck_returns_rows(self):
        self.ledger.claim({"update_id": "15"})
        self.db.execute(
            "UPDATE telegram_bot_updates SET received_at='2020-01-01T00:00:00+00:00'"
            " WHERE update_id='15'")
        self.assertGreaterEqual(self.ledger.reset_stuck(), 1)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "t.sqlite3")
        self.sent: list[tuple[str, dict]] = []
        self.outbox = Outbox(
            self.db,
            sender=lambda method, payload, timeout=15:
                self.sent.append((method, payload)) or {"ok": True},
            table="telegram_outbox")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_add_then_send_delivers_once(self):
        row = self.outbox.add("555", "sendMessage", {"chat_id": "555", "text": "привет"})
        self.assertTrue(self.outbox.send(row))
        self.assertEqual([m for m, _ in self.sent], ["sendMessage"])

    def test_dedupe_key_prevents_second_delivery(self):
        """Двойной клик не отправляет два одинаковых сообщения."""
        first = self.outbox.add("555", "sendMessage", {"text": "раз"},
                                dedupe_key="same")
        second = self.outbox.add("555", "sendMessage", {"text": "раз"},
                                 dedupe_key="same")
        self.assertEqual(first["id"], second["id"])
        self.outbox.send(second)
        self.assertEqual(len(self.sent), 1)

    def test_failed_send_is_retried_by_drain(self):
        """Обрыв сети: строка возвращается в очередь и уходит позже."""
        attempts = {"n": 0}

        def flaky(method, payload, timeout=15):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return {"ok": False, "description": "сеть"}
            return {"ok": True}

        outbox = Outbox(self.db, sender=flaky, table="telegram_outbox")
        row = outbox.add("555", "sendMessage", {"text": "важное"})
        self.assertFalse(outbox.send(row))
        self.assertEqual(outbox.stats()["pending"], 1)
        # откат экспоненциальной задержки: в тесте ждать 2 с не за что
        self.db.execute("UPDATE telegram_outbox SET available_at=''")
        self.assertEqual(outbox.drain(batch=5), 1)
        self.assertEqual(outbox.stats()["pending"], 0)
        self.assertEqual(attempts["n"], 2)

    def test_drain_delivers_pending_rows(self):
        self.outbox.add("555", "sendMessage", {"text": "одно"})
        self.outbox.add("555", "sendMessage", {"text": "два"})
        self.assertEqual(self.outbox.drain(batch=10), 2)
        self.assertEqual(len(self.sent), 2)

    def test_stats_show_queue_depth(self):
        self.outbox.add("555", "sendMessage", {"text": "ждёт"})
        stats = self.outbox.stats()
        self.assertEqual(stats["pending"], 1)
        self.outbox.drain(batch=5)
        self.assertEqual(self.outbox.stats()["pending"], 0)

    def test_photo_without_file_falls_back_to_text_method(self):
        """Нет файла на диске — отправляем как есть, не роняем очередь."""
        row = self.outbox.add("555", "sendPhoto", {"chat_id": "555"},
                              file_path="/несуществующий/файл.jpg")
        self.outbox.send(row)
        self.assertEqual([m for m, _ in self.sent], ["sendPhoto"])


class CommandDslTests(unittest.TestCase):
    """Идея 76: одна грамматика команд для обоих ботов."""

    def test_known_command_with_arguments(self):
        parsed = parse_command("пауза P1")
        self.assertEqual(parsed["action"], "pause")
        self.assertEqual(parsed["args"], "P1")

    def test_slash_prefix_is_stripped(self):
        self.assertEqual(parse_command("/стоп")["action"], "stop")

    def test_plain_text_is_not_a_command(self):
        """Заявка покупателя не обязана выглядеть как команда."""
        parsed = parse_command("хочу держатель для телефона")
        self.assertEqual(parsed["action"], "")
        self.assertEqual(parsed["raw"], "хочу держатель для телефона")

    def test_empty_input(self):
        self.assertEqual(parse_command(""), {"action": "", "args": "", "raw": ""})

    def test_keyboard_shape(self):
        keys = keyboard([("Стоп", "cmd:stop"), ("Пауза", "cmd:pause")],
                        [("Свет", "cmd:light")])
        self.assertEqual(len(keys["inline_keyboard"]), 2)
        self.assertEqual(keys["inline_keyboard"][0][0]["callback_data"], "cmd:stop")

    def test_money_text_has_no_kopecks(self):
        self.assertEqual(money_text(1234.7), "1 235 ₽")
        self.assertEqual(money_text("0"), "0 ₽")


if __name__ == "__main__":
    unittest.main()
