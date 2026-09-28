"""Memory 2.0: provenance, confidence and evidence."""
from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import unittest
import math

from agent.store import Store


class MemoryV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "memory.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_chat_memory_is_explicit_and_certain(self):
        saved = self.store.remember("Мария любит PETG", source="chat")
        row = saved["memory"]
        self.assertEqual("explicit", row["origin"])
        self.assertEqual(1.0, float(row["confidence"]))
        self.assertEqual(1, int(row["evidence_count"]))
        self.assertEqual("chat", row["provenance"])

    def test_self_memory_is_inferred_with_lower_confidence(self):
        saved = self.store.remember("Пользователь предпочитает короткие ответы", source="self")
        row = saved["memory"]
        self.assertEqual("inferred", row["origin"])
        self.assertAlmostEqual(0.55, float(row["confidence"]), places=2)

    def test_observation_has_separate_origin(self):
        saved = self.store.remember(
            "Telegram обычно открыт утром", source="observed",
            provenance="наблюдение окна Telegram")
        row = saved["memory"]
        self.assertEqual("observed", row["origin"])
        self.assertAlmostEqual(0.8, float(row["confidence"]), places=2)
        self.assertIn("Telegram", row["provenance"])

    def test_repeated_fact_increments_evidence_without_duplicate(self):
        first = self.store.remember(
            "Пользователь любит PETG", source="self", confidence=0.55)
        second = self.store.remember(
            "Пользователь любит PETG", source="chat")
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["memory"]["id"], second["memory"]["id"])
        row = second["memory"]
        self.assertEqual("explicit", row["origin"])
        self.assertEqual(1.0, float(row["confidence"]))
        self.assertEqual(2, int(row["evidence_count"]))
        self.assertEqual(1, len(self.store.memories(20)))

    def test_weaker_repeat_never_downgrades_explicit_memory(self):
        self.store.remember("Владельца зовут Михаил", kind="profile",
                            subject="имя", source="chat")
        row = self.store.remember(
            "Владельца зовут Михаил", kind="profile", subject="имя",
            source="self", confidence=0.4)["memory"]
        self.assertEqual("explicit", row["origin"])
        self.assertEqual(1.0, float(row["confidence"]))
        self.assertEqual(2, int(row["evidence_count"]))

    def test_profile_replacement_gets_new_provenance(self):
        self.store.remember("Владельца зовут Саша", kind="profile",
                            subject="имя", source="self")
        saved = self.store.remember("Владельца зовут Олег", kind="profile",
                                    subject="имя", source="chat")
        self.assertEqual("Владельца зовут Саша", saved["replaced"])
        row = saved["memory"]
        self.assertEqual("Владельца зовут Олег", row["text"])
        self.assertEqual("explicit", row["origin"])
        self.assertEqual(1.0, float(row["confidence"]))
        self.assertEqual(1, int(row["evidence_count"]))

    def test_recall_breaks_equal_text_score_by_confidence(self):
        low = self.store.remember("Мария любит PLA", source="self", confidence=0.4)["memory"]
        high = self.store.remember("Мария любит ABS", source="observed", confidence=0.9)["memory"]
        rows = self.store.recall("Мария любит", 2, touch=False)
        self.assertEqual(high["id"], rows[0]["id"])
        self.assertEqual(low["id"], rows[1]["id"])

    def test_old_memory_schema_is_migrated(self):
        self.store.close()
        self.path.unlink(missing_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE memories("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "kind TEXT NOT NULL DEFAULT 'fact', subject TEXT DEFAULT '', text TEXT NOT NULL, "
            "norm TEXT NOT NULL, source TEXT DEFAULT '', pinned INTEGER DEFAULT 0, "
            "uses INTEGER DEFAULT 0, last_used TEXT DEFAULT '')")
        conn.execute(
            "INSERT INTO memories(at,updated_at,kind,subject,text,norm,source,pinned,uses,last_used) "
            "VALUES('a','a','fact','','Выучил сам','выучил сам','self',0,0,'')")
        conn.commit()
        conn.close()

        self.store = Store(self.path)
        row = self.store.memories(1)[0]
        self.assertEqual("inferred", row["origin"])
        self.assertAlmostEqual(0.55, float(row["confidence"]), places=2)
        self.assertEqual(1, int(row["evidence_count"]))
        self.assertEqual("self", row["provenance"])

    def test_inference_cannot_replace_explicit_profile(self):
        first = self.store.remember("Владельца зовут Олег", kind="profile",
                                    subject="имя", source="chat")["memory"]
        rejected = self.store.remember("Владельца зовут Саша", kind="profile",
                                       subject="имя", source="self")
        self.assertFalse(rejected["ok"])
        self.assertTrue(rejected["conflict"])
        self.assertEqual(first["id"], rejected["memory"]["id"])
        self.assertEqual("Владельца зовут Олег", self.store.memories(1)[0]["text"])

    def test_window_is_explicit_and_origin_cannot_be_spoofed_by_inference(self):
        window = self.store.remember("Владелец любит PLA", source="window")["memory"]
        guessed = self.store.remember("Владелец любит ABS", source="self",
                                      origin="explicit")["memory"]
        self.assertEqual("explicit", window["origin"])
        self.assertEqual("inferred", guessed["origin"])
        self.assertEqual(window["at"], window["created_at"])
        self.assertEqual(1, window["observed_count"])
        skill = self.store.remember("Модель думает, что нужен ABS", source="skill")["memory"]
        self.assertEqual("inferred", skill["origin"])

    def test_nonfinite_confidence_is_replaced_with_default(self):
        row = self.store.remember("Небезопасная уверенность", source="self",
                                  confidence=math.nan)["memory"]
        self.assertAlmostEqual(0.55, row["confidence"])

    def test_same_text_in_different_kinds_keeps_both_records(self):
        self.store.remember("Олег любит PLA", kind="fact", source="chat")
        self.store.remember("Олег любит PLA", kind="preference", subject="материал",
                            source="chat")
        self.assertEqual(2, len(self.store.memories(10)))

    def test_four_layers_use_existing_data_without_exposing_journal_params(self):
        self.store.add_turn("native", "user", "Привет")
        self.store.journal("files.search", "done", detail="Найдено", params={"secret": "x"})
        self.store.remember("PLA плавится", kind="fact", source="chat")
        self.store.remember("Любит короткие ответы", kind="preference", subject="стиль",
                            source="self")
        layers = self.store.memory_layers("native")
        self.assertEqual("Привет", layers["working"][0]["text"])
        self.assertEqual("PLA плавится", layers["semantic"][0]["text"])
        self.assertEqual("Любит короткие ответы", layers["user_model"][0]["text"])
        self.assertNotIn("params", layers["episodic"][0])

    def test_reopen_does_not_reset_observed_confidence(self):
        self.store.remember("Вижу окно", source="observed", confidence=0.93)
        self.store.close()
        self.store = Store(self.path)
        row = self.store.memories(1)[0]
        self.assertAlmostEqual(0.93, row["confidence"])
        self.assertEqual("observed", row["origin"])


if __name__ == "__main__":
    unittest.main()
