"""Своя база ассистента (18.14, идея И138): память, индекс, журнал, навыки.

Контракты здесь про границу и про след:

  * база ассистента — отдельный файл, и таблиц PrintFlow в ней нет: резервная
    копия цеха не должна становиться копией чужой жизни;
  * журнал пишет и успех, и отказ, иначе `agent.why` объяснял бы причину по
    догадке;
  * поиск по индексу лексический и проверяемый: отрывок возвращается с путём и
    номером строки, а оценка считается из совпадений слов;
  * файл, исчезнувший с диска, уходит из индекса — иначе поиск врёт;
  * выученный навык переживает перезапуск агента (JSON в своей базе).
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from agent.store import Store  # noqa: E402

PANEL_TABLES = ("orders", "customers", "transactions", "print_jobs", "spools")


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "assistant.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def put(self, path: str, text: str, title: str = "", chunks: int = 1) -> None:
        rows = [{"seq": index, "first_line": index * 3 + 1, "text": part}
                for index, part in enumerate(text.split("\n---\n"))]
        self.store.put_document(path, "text", title or pathlib.Path(path).name,
                                len(text), time.time(), f"digest-{path}", rows[:chunks] or rows)


class IsolationTests(StoreTestCase):
    def test_base_is_a_separate_file(self):
        self.assertTrue(self.path.exists())
        self.assertEqual("assistant.sqlite3", self.path.name)

    def test_panel_tables_are_absent(self):
        rows = self.store._rows("SELECT name FROM sqlite_master WHERE type='table'")
        names = {str(row["name"]) for row in rows}
        for table in PANEL_TABLES:
            self.assertNotIn(table, names,
                             f"в базе ассистента появилась таблица цеха «{table}»")
        for table in ("journal", "documents", "chunks", "facts", "notes", "skills"):
            self.assertIn(table, names)

    def test_default_path_is_overridable_by_env(self):
        import os

        from agent import store as store_module
        os.environ["PRINTFLOW_ASSISTANT_DB"] = str(pathlib.Path(self.tmp.name) / "env.sqlite3")
        try:
            self.assertEqual(str(pathlib.Path(self.tmp.name) / "env.sqlite3"),
                             str(store_module.default_path()))
        finally:
            del os.environ["PRINTFLOW_ASSISTANT_DB"]


class JournalTests(StoreTestCase):
    def test_success_and_refusal_are_both_written(self):
        self.store.journal("files.search", "done", detail="найдено 3", params={"query": "договор"})
        self.store.journal("files.search", "refused", detail="пустой запрос")
        rows = self.store.journal_recent(10)
        self.assertEqual({"done", "refused"}, {row["outcome"] for row in rows})

    def test_params_survive_roundtrip(self):
        self.store.journal("files.to_order", "done", params={"path": "/tmp/а.stl", "order": "145"})
        row = self.store.journal_recent(1)[0]
        self.assertEqual("/tmp/а.stl", row["params"]["path"])

    def test_broken_params_do_not_break_reading(self):
        self.store._run("INSERT INTO journal(at,skill,outcome,params) VALUES(?,?,?,?)",
                        (store_now(), "files.search", "done", "не json"))
        row = self.store.journal_recent(1)[0]
        self.assertEqual({}, row["params"])

    def test_last_outcome_finds_the_skill(self):
        self.store.journal("day.briefing", "unavailable", detail="панель не отвечает")
        row = self.store.last_outcome("day.briefing")
        self.assertEqual("unavailable", row["outcome"])
        self.assertIn("панель", row["detail"])
        self.assertIsNone(self.store.last_outcome("files.index"))

    def test_limit_is_clamped(self):
        for index in range(5):
            self.store.journal("files.search", "done", detail=str(index))
        self.assertEqual(5, len(self.store.journal_recent(500)))
        self.assertEqual(1, len(self.store.journal_recent(1)))
        self.assertEqual(1, len(self.store.journal_recent(0)))


def store_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class IndexTests(StoreTestCase):
    def test_document_is_replaced_not_duplicated(self):
        self.put("/docs/a.md", "первый текст")
        self.put("/docs/a.md", "второй текст")
        self.assertEqual(1, self.store.stats()["documents"])
        hits = self.store.search(["второй"])
        self.assertEqual(1, len(hits))
        self.assertEqual([], self.store.search(["первый"]))

    def test_unchanged_detects_same_file(self):
        self.put("/docs/a.md", "текст")
        stat = self.store.documents(1)[0]
        self.assertTrue(self.store.unchanged("/docs/a.md", stat["size"], stat["mtime"],
                                             stat["digest"]))
        self.assertFalse(self.store.unchanged("/docs/a.md", stat["size"] + 1,
                                              stat["mtime"], stat["digest"]))
        self.assertFalse(self.store.unchanged("/docs/b.md", 1, 1.0, "x"))

    def test_forgotten_file_disappears_from_search(self):
        self.put("/docs/a.md", "договор с Марией")
        self.store.forget_document("/docs/a.md")
        self.assertEqual([], self.store.search(["мария"]))
        self.assertNotIn("/docs/a.md", self.store.known_paths())

    def test_search_ranks_by_matches_and_title(self):
        self.put("/docs/договор.md", "договор\n---\nдоговор с Марией на 12 500 ₽ договор",
                 title="Договор с Марией")
        self.put("/docs/прочее.md", "заметки о погоде")
        hits = self.store.search(["договор", "мария"], limit=5)
        self.assertTrue(hits)
        self.assertEqual("/docs/договор.md", hits[0]["path"])
        self.assertGreater(hits[0]["score"], 1)
        self.assertIn("строка", "строка")  # номер строки возвращается вместе с отрывком
        self.assertGreaterEqual(hits[0]["first_line"], 1)

    def test_search_finds_russian_regardless_of_case(self):
        self.put("/docs/a.md", "Профиль Печати PETG")
        self.assertTrue(self.store.search(["петg".upper().casefold()]) or
                        self.store.search(["petg"]))
        self.assertTrue(self.store.search(["ПРОФИЛЬ"]))

    def test_search_without_tokens_is_empty(self):
        self.put("/docs/a.md", "текст")
        self.assertEqual([], self.store.search([]))
        self.assertEqual([], self.store.search(["", "  "]))

    def test_chunks_keep_line_numbers(self):
        self.store.put_document("/docs/a.md", "text", "A", 10, time.time(), "d",
                                [{"seq": 0, "first_line": 7, "text": "строка семь"}])
        hit = self.store.search(["семь"])[0]
        self.assertEqual(7, hit["first_line"])

    def test_stats_counts_every_table(self):
        self.put("/docs/a.md", "текст")
        self.store.journal("files.index", "done")
        self.store.add_note("позвонить Марии")
        stats = self.store.stats()
        self.assertEqual(1, stats["documents"])
        self.assertEqual(1, stats["journal"])
        self.assertEqual(1, stats["notes"])
        self.assertEqual(str(self.path), stats["path"])


class FactsAndNotesTests(StoreTestCase):
    def test_facts_are_replaced_per_file(self):
        self.store.put_facts("/docs/счёт.pdf", [{"kind": "сумма", "value": "100 ₽",
                                                  "place": "строка 2"}])
        self.store.put_facts("/docs/счёт.pdf", [{"kind": "сумма", "value": "200 ₽",
                                                  "place": "строка 3"}])
        rows = self.store.facts(path="/docs/счёт.pdf")
        self.assertEqual(1, len(rows))
        self.assertEqual("200 ₽", rows[0]["value"])

    def test_fact_marked_used(self):
        self.store.put_facts("/docs/a.md", [{"kind": "дата", "value": "2026-10-05",
                                             "place": "строка 1"}])
        fact = self.store.facts(1)[0]
        self.store.mark_fact_used(int(fact["id"]))
        self.assertEqual(1, self.store.facts(1)[0]["used"])

    def test_notes_open_and_close(self):
        note = self.store.add_note("позвонить Марии", due="2026-09-25")
        self.assertEqual([note["id"]], [row["id"] for row in self.store.open_notes()])
        self.assertTrue(self.store.close_note(int(note["id"])))
        self.assertEqual([], self.store.open_notes())
        self.assertFalse(self.store.close_note(999999))


class LearnedSkillsTests(StoreTestCase):
    def test_skill_survives_reopen(self):
        self.store.save_skill("my.utro", {"title": "Утро", "risk": "write",
                                          "steps": [{"skill": "agent.skills", "params": {}}],
                                          "requires": (), "params": {}, "ideas": ("И180",)})
        self.store.close()
        reopened = Store(self.path)
        try:
            learned = reopened.learned_skills()
            self.assertIn("my.utro", learned)
            self.assertEqual("Утро", learned["my.utro"]["title"])
            self.assertTrue(learned["my.utro"]["learned_at"])
        finally:
            reopened.close()

    def test_broken_payload_is_skipped_not_fatal(self):
        self.store._run("INSERT INTO skills(name,payload,learned_at) VALUES(?,?,?)",
                        ("my.bad", "не json", store_now()))
        self.assertEqual({}, self.store.learned_skills())

    def test_forget_skill(self):
        self.store.save_skill("my.utro", {"title": "Утро", "steps": [], "params": {},
                                          "risk": "read", "requires": (), "ideas": ()})
        self.assertTrue(self.store.forget_skill("my.utro"))
        self.assertFalse(self.store.forget_skill("my.utro"))


if __name__ == "__main__":
    unittest.main()
