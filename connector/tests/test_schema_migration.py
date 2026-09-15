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

import ast
import collections
import pathlib
import re
import sqlite3
import tempfile
import unittest

from connector.printflow.accounting import num
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

    def test_added_columns_has_no_duplicate_table_keys(self):
        """Словарь без повторных ключей: второй ключ молча съедает первый.

        Так уже случилось с `batches`: колонка состава смешанной партии была
        объявлена в одном месте, а `variant_id` дописали вторым ключом той же
        таблицы — Python оставил только последний, и свежая база осталась без
        `items`. Партии собирались, но состав терялся.
        """
        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "printflow" / "db.py").read_text(encoding="utf-8")
        literal = None
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.AnnAssign)
                    and getattr(node.target, "id", "") == "ADDED_COLUMNS"):
                literal = node.value
        self.assertIsNotNone(literal, "ADDED_COLUMNS не найдена в db.py")
        keys = [key.value for key in literal.keys]
        duplicates = sorted(name for name, count in collections.Counter(keys).items()
                            if count > 1)
        self.assertEqual([], duplicates, "таблица объявлена в ADDED_COLUMNS дважды — "
                                         "колонки из первой записи пропадут")

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

class SchemaIndexGuardTests(unittest.TestCase):
    """Индекс по колонке, которую добавляет миграция, ломает старт приложения.

    `SCHEMA` и `SCHEMA_V3` выполняются целиком до `ALTER TABLE`. Если индекс в
    схеме ссылается на колонку из `ADDED_COLUMNS`, старая база падает на
    «no such column» ещё до того, как колонка появится: ровно так PrintFlow не
    открылся у владельца (`no such column: spool_id`). Такие индексы обязаны
    создаваться в `_migrate`, после догоняющих ALTER.
    """

    def indexes(self, script: str) -> list[tuple[str, str]]:
        found = []
        for ddl in re.findall(r"CREATE (?:UNIQUE )?INDEX[^;]+;", script, re.I):
            on = re.search(r"ON\s+([a-z_0-9]+)\s*\(([^)]*)\)", ddl, re.I)
            if on:
                for column in on.group(2).split(","):
                    found.append((on.group(1), column.strip().split()[0]))
        return found

    def test_no_index_in_schema_touches_migrated_columns(self):
        from connector.printflow.db import SCHEMA
        from connector.printflow.schema_v3 import SCHEMA_V3
        risky = []
        for script in (SCHEMA, SCHEMA_V3):
            for table, column in self.indexes(script):
                if column in {name for name, _decl in ADDED_COLUMNS.get(table, [])}:
                    risky.append(f"{table}.{column}")
        self.assertEqual([], risky,
                         "индекс по колонке из ADDED_COLUMNS выполнится раньше ALTER: "
                         "перенесите его в блок индексов после миграции")

    def test_variant_spool_index_is_created_after_migration(self):
        """Отдельно про тот самый индекс: он обязан быть в миграции, не в схеме."""
        source = pathlib.Path(Database.__module__.replace(".", "/") + ".py")
        if not source.exists():
            source = pathlib.Path(__file__).resolve().parents[1] / "printflow" / "db.py"
        text = source.read_text(encoding="utf-8")
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_variant_spool", text)


class LegacyVariantsDatabaseTests(unittest.TestCase):
    """Живая база владельца пришла без полей вариаций — и не открывалась.

    `no such column: spool_id` на старте: `SCHEMA_V3` создаёт индекс
    `idx_variant_spool` раньше, чем миграция добавляет колонку. База на ПК —
    источник правды, и обновление кода обязано её догонять, а не падать:
    иначе владелец видит «Ошибка базы данных» вместо своей кассы.

    Контракт: таблицы старой формы (без вариационных полей) открываются,
    колонки добавляются со значениями по умолчанию, строки и данные целы,
    индексы по новым колонкам создаются — и повторное открытие ничего не меняет.
    """

    # Так выглядели таблицы, пока у вариаций не было пластика, катушки и цены.
    LEGACY = """
    CREATE TABLE nom_variants (
        id TEXT PRIMARY KEY, nom_id TEXT, name TEXT DEFAULT '',
        color_name TEXT DEFAULT '', color_hex TEXT DEFAULT '', size TEXT DEFAULT '',
        sku TEXT DEFAULT '', barcode TEXT DEFAULT '', grams REAL DEFAULT 0,
        hours REAL DEFAULT 0, file TEXT DEFAULT '', position INTEGER DEFAULT 0,
        archived INTEGER DEFAULT 0, updated_at TEXT DEFAULT '');
    CREATE TABLE stock_moves (
        id TEXT PRIMARY KEY, at TEXT, doc_id TEXT, doc_kind TEXT DEFAULT '',
        nom_id TEXT, warehouse_id TEXT, qty REAL DEFAULT 0, cost REAL DEFAULT 0,
        batch_id TEXT, job_id TEXT, note TEXT DEFAULT '');
    CREATE INDEX IF NOT EXISTS idx_variant_nom ON nom_variants(nom_id);
    """

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = pathlib.Path(self.folder.name) / "legacy.sqlite3"
        raw = sqlite3.connect(self.path)
        raw.executescript(self.LEGACY)
        raw.execute("INSERT INTO nom_variants(id, nom_id, name, color_name, size)"
                    " VALUES('v1','nom1','Красный / L','Красный','L')")
        raw.execute("INSERT INTO stock_moves(id, nom_id, qty, cost)"
                    " VALUES('m1','nom1',3,300)")
        raw.commit()
        raw.close()

    def columns(self, db, table: str) -> set[str]:
        return {row["name"] for row in db.query(f"PRAGMA table_info({table})")}

    def test_legacy_database_opens_and_catches_up(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        for table, names in (("nom_variants", ("material", "spool_id", "price", "cost")),
                             ("stock_moves", ("variant_id",))):
            with self.subTest(table=table):
                have = self.columns(db, table)
                self.assertTrue(set(names) <= have,
                                f"{table}: не догнались колонки {sorted(set(names) - have)}")
        indexes = {row["name"] for row in db.query(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_variant_spool", indexes,
                      "индекс по spool_id не создан после ALTER TABLE")

    def test_legacy_rows_survive_with_defaults(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        variant = db.one("SELECT * FROM nom_variants WHERE id='v1'")
        self.assertEqual("Красный / L", variant["name"], "строка вариации потерялась")
        self.assertEqual("Красный", variant["color_name"])
        self.assertEqual("", variant["spool_id"], "новая колонка без значения по умолчанию")
        self.assertEqual(0.0, num(variant["price"]))
        move = db.one("SELECT * FROM stock_moves WHERE id='m1'")
        self.assertEqual(3.0, num(move["qty"]), "движение склада потерялось")
        self.assertIsNone(move["variant_id"], "variant_id старого движения должен быть NULL")

    def test_second_open_changes_nothing(self):
        first = Database(self.path)
        before = {table: self.columns(first, table)
                  for table in ("nom_variants", "stock_moves")}
        first.close()
        again = Database(self.path)
        self.addCleanup(again.close)
        after = {table: self.columns(again, table)
                 for table in ("nom_variants", "stock_moves")}
        self.assertEqual(before, after, "повторное открытие старой базы что-то меняет")


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
