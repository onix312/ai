"""Схема в одной точке: `ADDED_COLUMNS` идемпотентна и ничего не ломает.

Правило репозитория: колонки только добавляются (`ADDED_COLUMNS` в
`connector/printflow/db.py`), идемпотентно, без удаления и переименования —
живая база на ПК является источником правды, и потеря колонки там
необратима. Здесь четыре контракта на это правило:

1. каждая объявленная колонка реально есть в свежей базе;
2. повторное открытие той же базы ничего не меняет (идемпотентность);
3. база «старой» версии догоняется: колонка добавляется, а существующие
   строки получают значение по умолчанию, а не ломаются;
4. в коде миграции нет разрушающих конструкций.
"""
from __future__ import annotations

import pathlib
import re
import sqlite3
import tempfile
import unittest

from connector.printflow.config import now_iso
from connector.printflow.db import ADDED_COLUMNS, SCHEMA_VERSION, Database


class AddedColumnsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = pathlib.Path(self.folder.name) / "schema.sqlite3"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)

    def columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.db.query(f"PRAGMA table_info({table})")}

    def test_every_declared_column_exists(self):
        for table, columns in ADDED_COLUMNS.items():
            with self.subTest(table=table):
                have = self.columns(table)
                self.assertTrue(have, f"таблицы {table} нет в схеме")
                missing = [name for name, _decl in columns if name not in have]
                self.assertEqual([], missing, f"в {table} не хватает колонок")

    def test_reopening_the_same_database_changes_nothing(self):
        before = {table: self.columns(table) for table in ADDED_COLUMNS}
        version = self.db.conn.execute("PRAGMA user_version").fetchone()[0]
        self.db.close()
        again = Database(self.path)
        self.addCleanup(again.close)
        after = {table: {row["name"] for row in
                         again.query(f"PRAGMA table_info({table})")}
                 for table in ADDED_COLUMNS}
        self.assertEqual(before, after, "повторная миграция изменила состав колонок")
        self.assertEqual(version,
                         again.conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(SCHEMA_VERSION,
                         again.conn.execute("PRAGMA user_version").fetchone()[0])

    def test_old_database_is_upgraded_and_rows_survive(self):
        """База без колонки догоняется, а строка в таблице остаётся живой."""
        table, (column, decl) = "cashier_tokens", ("last_seen", "TEXT DEFAULT ''")
        self.assertIn((column, decl), ADDED_COLUMNS[table])
        self.db.upsert(table, {"token_hash": "t-old", "cashier": "Старая касса",
                               "created_at": now_iso(), "expires_at": now_iso()},
                       key="token_hash")
        row_before = self.db.one(f"SELECT cashier FROM {table} WHERE token_hash='t-old'")
        create = self.db.one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))["sql"]
        self.db.close()
        # Собираем «базу прежней версии»: та же таблица, но без новой колонки.
        # DROP COLUMN здесь не годится — SQLite отказывается снимать колонку
        # со значением по умолчанию, поэтому пересоздаём таблицу по её же DDL.
        # Снимаем колонку вместе с запятой перед ней и комментарием к ней:
        # она стоит последней, и без запятой DDL не соберётся.
        old_ddl = re.sub(r",\s*(?:--[^\n]*\n\s*)*" + column + r"\s[^,\n)]*", "", create)
        self.assertNotEqual(old_ddl, create, "колонки нет в DDL — тест ничего не проверяет")
        self.assertNotIn(column, old_ddl)
        raw = sqlite3.connect(self.path)
        raw.execute(f"DROP TABLE {table}")
        raw.execute(old_ddl)
        raw.execute(f"INSERT INTO {table}(token_hash, cashier, created_at, expires_at)"
                    " VALUES('t-old', ?, ?, ?)",
                    (row_before["cashier"], now_iso(), now_iso()))
        raw.commit()
        raw.close()
        upgraded = Database(self.path)
        self.addCleanup(upgraded.close)
        self.assertIn(column,
                      {row["name"] for row in upgraded.query(f"PRAGMA table_info({table})")},
                      "колонка не вернулась при открытии старой базы")
        row = upgraded.one(f"SELECT token_hash, cashier, {column} FROM {table} WHERE token_hash='t-old'")
        self.assertIsNotNone(row, "строка потерялась при миграции")
        self.assertEqual(row_before["cashier"], row["cashier"],
                         "миграция изменила данные строки")
        self.assertEqual("", row[column], "существующая строка не получила значение по умолчанию")

    def test_migration_code_has_no_destructive_statements(self):
        source = pathlib.Path(Database.__module__.replace(".", "/") + ".py")
        if not source.exists():
            source = pathlib.Path(__file__).resolve().parents[1] \
                / "printflow" / "db.py"
        text = source.read_text(encoding="utf-8")
        for pattern in (r"DROP\s+COLUMN", r"DROP\s+TABLE", r"RENAME\s+TO"):
            self.assertIsNone(re.search(pattern, text, re.I),
                              f"в db.py появилась разрушающая конструкция: {pattern}")
        # Единственный способ менять схему — ADD COLUMN из ADDED_COLUMNS.
        self.assertIn("ALTER TABLE {table} ADD COLUMN {name} {decl}", text)


if __name__ == "__main__":
    unittest.main()
