"""SQLite-хранилище PrintFlow — источник правды для всей системы.

Все данные лежат в одном файле в каталоге пользователя. Схема версионируется
через user_version, чтобы обновления не теряли данные.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import (BACKUP_DIR, DB_FILE, DEFAULT_ACCOUNTS, DEFAULT_CHANNELS,
                     DEFAULT_EXPENSE_CATEGORIES, DEFAULT_NICHES, DEFAULT_SETTINGS,
                     DEFAULT_STATUSES, RESTORE_REQUEST, ensure_dirs, now_iso)

SCHEMA_VERSION = 4

# Колонки, добавленные после первой версии схемы. Ключ — таблица,
# значение — список (колонка, SQL-тип со значением по умолчанию).
ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "printers": [
        ("camera_demo", "INTEGER DEFAULT 0"),      # показывать демо-поток без принтера
        ("guard_enabled", "INTEGER DEFAULT 1"),    # сторож печати следит за ошибками
        ("total_minutes", "REAL DEFAULT 0"),       # наработка, минуты
        ("total_grams", "REAL DEFAULT 0"),         # израсходовано пластика, г
        ("nozzle_size", "REAL DEFAULT 0.4"),
        ("nozzle_type", "TEXT DEFAULT 'steel'"),
        # автосбор с принтера: прошивка, Wi-Fi, влажность AMS, последняя связь
        ("firmware", "TEXT DEFAULT ''"),
        ("wifi", "TEXT DEFAULT ''"),
        ("ams_humidity", "TEXT DEFAULT ''"),
        ("last_seen", "TEXT"),
        # режим связи: cloud (через аккаунт Bambu, без LAN Only Mode) | lan.
        # Существующие принтеры остаются на 'lan' — поведение не меняется;
        # новые принтеры по умолчанию создаются в 'cloud' (см. repo.save_printer).
        ("mode", "TEXT DEFAULT 'lan'"),
    ],
    "transactions": [
        ("account_id", "TEXT"),
        ("customer_id", "TEXT"),
        ("channel", "TEXT DEFAULT ''"),
        ("payer", "TEXT DEFAULT ''"),
        ("fee", "REAL DEFAULT 0"),
        ("taxable", "INTEGER DEFAULT 1"),
        ("deductible", "INTEGER DEFAULT 1"),
        ("fixed_cost_id", "TEXT"),
        ("period", "TEXT DEFAULT ''"),
    ],
    "orders": [
        ("paid", "REAL DEFAULT 0"),          # фактически полученные деньги
        ("discount", "REAL DEFAULT 0"),      # скидка, ₽
        ("delivery", "REAL DEFAULT 0"),      # доставка за счёт мастера, ₽
        ("fee", "REAL DEFAULT 0"),           # комиссия площадки, ₽
        ("rush", "INTEGER DEFAULT 0"),       # срочный заказ
        ("payer", "TEXT DEFAULT 'person'"),  # физлицо или юрлицо
        ("account_id", "TEXT"),
        ("design_minutes", "REAL DEFAULT 0"),
        ("colors", "TEXT DEFAULT ''"),       # многоцвет: JSON [{material,color,grams}]
        ("qc_done", "TEXT DEFAULT ''"),      # чек-лист качества: JSON {step: true}
        ("nom_id", "TEXT"),                  # позиция номенклатуры (3.0)
        ("warehouse_id", "TEXT"),            # с какого склада отгружаем
        ("reserved", "INTEGER DEFAULT 0"),   # зарезервирован ли товар
    ],
    "print_jobs": [
        ("est_minutes", "REAL DEFAULT 0"),   # оценка из слайсера (3MF/G-code)
        ("est_grams", "REAL DEFAULT 0"),
        ("batch_id", "TEXT"),                # к какой партии относится запуск
        ("batch_qty", "REAL DEFAULT 0"),     # сколько годных штук даёт запуск
    ],
    "customers": [
        ("kind", "TEXT DEFAULT 'person'"),   # person | company
        ("inn", "TEXT DEFAULT ''"),
        ("segment", "TEXT DEFAULT ''"),
        ("discount", "REAL DEFAULT 0"),
        ("price_type_id", "TEXT"),
        ("credit_limit", "REAL DEFAULT 0"),
        ("address", "TEXT DEFAULT ''"),
        ("email", "TEXT DEFAULT ''"),
    ],
    "spools": [
        ("warehouse_id", "TEXT"),            # где физически лежит катушка
        ("cell", "TEXT DEFAULT ''"),         # адрес хранения
        ("opened_at", "TEXT"),               # когда вскрыта
        ("supplier", "TEXT DEFAULT ''"),
        ("ams_sync", "INTEGER DEFAULT 1"),   # обновлять остаток/слот из AMS
        ("synced_at", "TEXT"),               # когда AMS последний раз обновлял
    ],
    "catalog": [
        ("cost", "REAL DEFAULT 0"),
        ("sold", "INTEGER DEFAULT 0"),
    ],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS printers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model TEXT DEFAULT 'P1S',
    host TEXT DEFAULT '',
    serial TEXT DEFAULT '',
    access_code TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    has_ams INTEGER DEFAULT 1,
    position INTEGER DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS statuses (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    color TEXT DEFAULT '#64748b',
    position INTEGER DEFAULT 0,
    is_final INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS niches (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    icon TEXT DEFAULT '◆',
    color TEXT DEFAULT '#2563eb',
    hypothesis TEXT DEFAULT '',
    target TEXT DEFAULT '',
    views INTEGER DEFAULT 0,
    leads INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    position INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    messenger TEXT DEFAULT '',
    company TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    number TEXT DEFAULT '',
    product TEXT DEFAULT '',
    customer_id TEXT,
    customer_name TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    messenger TEXT DEFAULT '',
    channel TEXT DEFAULT '',
    niche_id TEXT,
    status TEXT DEFAULT 'new',
    priority TEXT DEFAULT 'normal',
    qty REAL DEFAULT 1,
    material TEXT DEFAULT '',
    color TEXT DEFAULT '',
    grams REAL DEFAULT 0,
    hours REAL DEFAULT 0,
    price REAL DEFAULT 0,
    cost REAL DEFAULT 0,
    prepaid REAL DEFAULT 0,
    manual_minutes REAL DEFAULT 0,
    file TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    quality TEXT DEFAULT 'pending',
    quality_note TEXT DEFAULT '',
    due TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    closed_at TEXT,
    actual_grams REAL DEFAULT 0,
    actual_hours REAL DEFAULT 0,
    actual_cost REAL DEFAULT 0,
    auto_cost INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_number ON orders(number);

CREATE TABLE IF NOT EXISTS spools (
    id TEXT PRIMARY KEY,
    material TEXT DEFAULT 'PLA',
    brand TEXT DEFAULT '',
    color_name TEXT DEFAULT '',
    color_hex TEXT DEFAULT '#4b5563',
    total_grams REAL DEFAULT 1000,
    remaining_grams REAL DEFAULT 1000,
    price REAL DEFAULT 1600,
    printer_id TEXT,
    ams_slot TEXT DEFAULT '',
    tray_uuid TEXT DEFAULT '',
    archived INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS print_jobs (
    id TEXT PRIMARY KEY,
    printer_id TEXT,
    order_id TEXT,
    name TEXT DEFAULT '',
    file TEXT DEFAULT '',
    state TEXT DEFAULT 'queued',
    source TEXT DEFAULT 'printer',
    ams_mapping TEXT DEFAULT '',
    plate INTEGER DEFAULT 1,
    use_ams INTEGER DEFAULT 1,
    bed_level INTEGER DEFAULT 1,
    flow_cali INTEGER DEFAULT 0,
    timelapse INTEGER DEFAULT 0,
    priority INTEGER DEFAULT 0,
    queued_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_min REAL DEFAULT 0,
    grams REAL DEFAULT 0,
    layers INTEGER DEFAULT 0,
    progress REAL DEFAULT 0,
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    cost REAL DEFAULT 0,
    energy_kwh REAL DEFAULT 0,
    spool_id TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON print_jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_printer ON print_jobs(printer_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    printer_id TEXT,
    kind TEXT,
    title TEXT,
    detail TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at DESC);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    at TEXT,
    kind TEXT,              -- income | expense
    category TEXT,          -- order | filament | energy | equipment | other
    amount REAL DEFAULT 0,
    title TEXT DEFAULT '',
    note TEXT DEFAULT '',
    order_id TEXT,
    job_id TEXT,
    auto INTEGER DEFAULT 0,
    account_id TEXT,        -- касса или счёт
    customer_id TEXT,
    channel TEXT DEFAULT '',
    payer TEXT DEFAULT '',  -- person | company (важно для ставки НПД)
    fee REAL DEFAULT 0,     -- удержанная комиссия площадки или эквайринга
    taxable INTEGER DEFAULT 1,   -- попадает ли в налоговую базу
    deductible INTEGER DEFAULT 1,  -- принимается ли в расходы на УСН 15
    fixed_cost_id TEXT,     -- если проводка создана постоянным расходом
    period TEXT DEFAULT ''  -- YYYY-MM, к какому месяцу отнести
);
CREATE INDEX IF NOT EXISTS idx_tx_at ON transactions(at DESC);
CREATE INDEX IF NOT EXISTS idx_tx_order ON transactions(order_id);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT DEFAULT 'cash',      -- cash | card | bank | other
    fee_percent REAL DEFAULT 0,    -- комиссия при поступлении
    opening_balance REAL DEFAULT 0,
    archived INTEGER DEFAULT 0,
    position INTEGER DEFAULT 0,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    fee_percent REAL DEFAULT 0,    -- комиссия площадки
    fee_fixed REAL DEFAULT 0,      -- фиксированный сбор за заказ
    ads_per_order REAL DEFAULT 0,  -- средние затраты на привлечение
    payer TEXT DEFAULT 'person',   -- person | company
    active INTEGER DEFAULT 1,
    position INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS expense_categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    grp TEXT DEFAULT 'variable',   -- variable | fixed | invest | tax | owner
    is_fixed INTEGER DEFAULT 0,
    position INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fixed_costs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    amount REAL DEFAULT 0,
    category TEXT DEFAULT 'rent',
    period TEXT DEFAULT 'month',   -- month | quarter | year
    day INTEGER DEFAULT 1,         -- день начисления
    account_id TEXT,
    active INTEGER DEFAULT 1,
    deductible INTEGER DEFAULT 1,
    started_at TEXT DEFAULT '',
    last_charged TEXT DEFAULT '',  -- YYYY-MM последнего начисления
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    at TEXT,
    order_id TEXT,
    customer_id TEXT,
    amount REAL DEFAULT 0,
    kind TEXT DEFAULT 'payment',   -- prepay | payment | refund
    account_id TEXT,
    method TEXT DEFAULT '',
    fee REAL DEFAULT 0,
    note TEXT DEFAULT '',
    tx_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_pay_order ON payments(order_id);

CREATE TABLE IF NOT EXISTS tax_periods (
    id TEXT PRIMARY KEY,           -- 2026-Q1 или 2026-05
    kind TEXT DEFAULT 'quarter',   -- month | quarter | year
    income REAL DEFAULT 0,
    expense REAL DEFAULT 0,
    tax_due REAL DEFAULT 0,
    tax_paid REAL DEFAULT 0,
    insurance_paid REAL DEFAULT 0,
    note TEXT DEFAULT '',
    closed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS filament_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    spool_id TEXT,
    job_id TEXT,
    order_id TEXT,
    grams REAL DEFAULT 0,
    cost REAL DEFAULT 0,
    note TEXT DEFAULT '',
    auto INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS catalog (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    niche_id TEXT,
    grams REAL DEFAULT 0,
    hours REAL DEFAULT 0,
    fit_per_plate INTEGER DEFAULT 1,
    price REAL DEFAULT 0,
    material TEXT DEFAULT 'PLA',
    file TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    archived INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    printer_id TEXT,
    job_id TEXT,
    state TEXT DEFAULT '',
    progress REAL DEFAULT 0,
    layer INTEGER DEFAULT 0,
    nozzle REAL DEFAULT 0,
    nozzle_target REAL DEFAULT 0,
    bed REAL DEFAULT 0,
    bed_target REAL DEFAULT 0,
    chamber REAL DEFAULT 0,
    fan_part REAL DEFAULT 0,
    fan_aux REAL DEFAULT 0,
    speed REAL DEFAULT 0,
    wifi TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_telemetry_printer ON telemetry(printer_id, at);

CREATE TABLE IF NOT EXISTS maintenance (
    id TEXT PRIMARY KEY,
    printer_id TEXT,
    task TEXT DEFAULT '',
    every_hours REAL DEFAULT 0,
    last_at TEXT,
    last_hours REAL DEFAULT 0,
    note TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    position INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS printer_stats (
    day TEXT,
    printer_id TEXT,
    print_minutes REAL DEFAULT 0,
    grams REAL DEFAULT 0,
    jobs_done INTEGER DEFAULT 0,
    jobs_failed INTEGER DEFAULT 0,
    energy_kwh REAL DEFAULT 0,
    PRIMARY KEY (day, printer_id)
);

CREATE TABLE IF NOT EXISTS defects (
    id TEXT PRIMARY KEY,
    at TEXT,
    printer_id TEXT,
    job_id TEXT,
    order_id TEXT,
    code TEXT DEFAULT '',
    phase TEXT DEFAULT '',       -- слой / фаза печати
    reason TEXT DEFAULT '',      -- разбор причины
    grams REAL DEFAULT 0,
    loss REAL DEFAULT 0,
    photo TEXT DEFAULT ''        -- путь к кадру камеры
);

CREATE TABLE IF NOT EXISTS ams_profiles (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    slots TEXT DEFAULT '[]',     -- JSON [{slot, material, color, label}]
    note TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS scheduled_commands (
    id TEXT PRIMARY KEY,
    at TEXT,                     -- когда выполнить (ISO)
    printer_id TEXT,
    command TEXT DEFAULT '',
    value TEXT DEFAULT '',       -- JSON
    note TEXT DEFAULT '',
    done INTEGER DEFAULT 0,
    result TEXT DEFAULT '',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_commands(done, at);

CREATE TABLE IF NOT EXISTS order_photos (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    at TEXT,
    file TEXT DEFAULT '',        -- имя файла в DATA_DIR/photos/
    note TEXT DEFAULT '',
    kind TEXT DEFAULT 'upload'   -- upload | camera
);
CREATE INDEX IF NOT EXISTS idx_photos_order ON order_photos(order_id);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    order_id TEXT,
    product TEXT DEFAULT '',
    price REAL DEFAULT 0,
    catalog_id TEXT
);

CREATE TABLE IF NOT EXISTS shopping_items (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    material TEXT DEFAULT '',
    qty REAL DEFAULT 1,           -- сколько закупить (катушек/кг)
    unit TEXT DEFAULT 'кг',       -- кг | шт | катушка
    reason TEXT DEFAULT '',       -- авто-причина: «осталось N г» / «темп N г/дн»
    source TEXT DEFAULT 'manual', -- manual | auto
    done INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_shopping_done ON shopping_items(done, created_at);

CREATE TABLE IF NOT EXISTS drying_sessions (
    id TEXT PRIMARY KEY,
    at TEXT,
    spool_id TEXT,
    material TEXT DEFAULT '',
    color_name TEXT DEFAULT '',
    minutes REAL DEFAULT 0,
    temp REAL DEFAULT 0,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shelf_items (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    catalog_id TEXT,
    qty REAL DEFAULT 0,            -- штук на стеллаже
    price REAL DEFAULT 0,          -- цена ценника, ₽
    cost_per_unit REAL DEFAULT 0,  -- себестоимость штуки, ₽
    min_qty REAL DEFAULT 0,        -- минимальный остаток для предупреждения
    photo TEXT DEFAULT '',         -- имя файла в DATA_DIR/photos/
    note TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS shelf_moves (
    id TEXT PRIMARY KEY,
    at TEXT,
    item_id TEXT,
    kind TEXT DEFAULT 'produce',   -- produce | sale | writeoff | inventory | online
    qty REAL DEFAULT 0,            -- знак: + приход, − расход
    price REAL DEFAULT 0,          -- цена продажи для sale
    job_id TEXT,
    tx_id TEXT,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_shelf_moves_item ON shelf_moves(item_id, at);

-- ------------------------------------------------- 5.0: конверты-накопления
CREATE TABLE IF NOT EXISTS envelopes (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    pct REAL DEFAULT 0,            -- % с каждого дохода
    goal REAL DEFAULT 0,           -- цель накопления, ₽
    color TEXT DEFAULT '#4f46e5',
    position INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS envelope_moves (
    id TEXT PRIMARY KEY,
    at TEXT,
    envelope_id TEXT,
    amount REAL DEFAULT 0,         -- + отложили, − забрали
    note TEXT DEFAULT '',
    tx_id TEXT,
    order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_envmoves_env ON envelope_moves(envelope_id, at);

-- ------------------------------------------------- 5.0: история изменений
CREATE TABLE IF NOT EXISTS order_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    order_id TEXT,
    field TEXT DEFAULT '',
    old_value TEXT DEFAULT '',
    new_value TEXT DEFAULT '',
    author TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_orderhist_order ON order_history(order_id, id);

-- ------------------------------------------- 8.2: конструктор правил «если-то»
CREATE TABLE IF NOT EXISTS automation_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    event TEXT DEFAULT '',
    config TEXT DEFAULT '{}',
    action TEXT DEFAULT 'notify',
    enabled INTEGER DEFAULT 1,
    position INTEGER DEFAULT 0,
    last_fired TEXT DEFAULT '',
    fires INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rules_event ON automation_rules(event);
"""


class Database:
    """Потокобезопасная обёртка над SQLite."""

    def __init__(self, path=DB_FILE):
        ensure_dirs()
        self.path = path
        self.lock = threading.RLock()
        self._local = threading.local()
        # Шину подключает сервер (api.py). Пока её нет — события просто
        # пишутся в базу, поэтому база остаётся самостоятельной в тестах.
        self.bus = None
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # SQLite lower() умеет только ASCII — регистронезависимый поиск по
        # кириллице делаем средствами Python.
        self.conn.create_function("pylower", 1, lambda v: v.lower() if isinstance(v, str) else v)
        self.conn.executescript(SCHEMA)
        from .schema_v3 import SCHEMA_V3
        self.conn.executescript(SCHEMA_V3)
        self._migrate()
        self._seed()
        self._seed_v3()
        self._migrate_v3_data()
        # Реестр моделей 6.0
        try:
            from .model_registry import ModelRegistry
            ModelRegistry(self).ensure_schema()
        except Exception:
            pass

    # ------------------------------------------------------------------ ядро
    def _migrate(self) -> None:
        """Догоняющие миграции: новые колонки добавляются без потери данных.

        Перед любым изменением схемы делается страховочная копия файла базы в
        `backups/pre-migration-*.sqlite3` — откат одной командой, даже если
        миграция оборвётся на середине.
        """
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            pending: dict[str, list[tuple[str, str]]] = {}
            for table, columns in ADDED_COLUMNS.items():
                existing = {r["name"] for r in
                            self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if not existing:
                    continue  # таблицы ещё нет — её создаст SCHEMA
                missing = [(name, decl) for name, decl in columns
                           if name not in existing]
                if missing:
                    pending[table] = missing
            if version < SCHEMA_VERSION or pending:
                # Страховка: копия файла до миграции. Ошибка копирования не
                # должна блокировать саму миграцию.
                try:
                    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    self.backup_to(BACKUP_DIR / f"pre-migration-{stamp}.sqlite3")
                except Exception:
                    pass
            for table, missing in pending.items():
                for name, decl in missing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            if version < SCHEMA_VERSION:
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()

    def _seed(self) -> None:
        with self.lock:
            cur = self.conn
            if not cur.execute("SELECT 1 FROM statuses LIMIT 1").fetchone():
                cur.executemany(
                    "INSERT INTO statuses(id,name,color,position,is_final) VALUES(?,?,?,?,?)",
                    DEFAULT_STATUSES,
                )
            if not cur.execute("SELECT 1 FROM niches LIMIT 1").fetchone():
                cur.executemany(
                    "INSERT INTO niches(id,name,icon,color,hypothesis,target,views,leads,active,position)"
                    " VALUES(?,?,?,?,?,?,0,0,1,0)",
                    DEFAULT_NICHES,
                )
            for i, row in enumerate(DEFAULT_ACCOUNTS):
                cur.execute(
                    "INSERT OR IGNORE INTO accounts(id,name,kind,fee_percent,opening_balance,position)"
                    " VALUES(?,?,?,?,?,?)", (*row, i))
            for i, row in enumerate(DEFAULT_CHANNELS):
                cur.execute(
                    "INSERT OR IGNORE INTO channels(id,name,fee_percent,fee_fixed,ads_per_order,payer,position)"
                    " VALUES(?,?,?,?,?,?,?)", (*row, i))
            for i, row in enumerate(DEFAULT_EXPENSE_CATEGORIES):
                cur.execute(
                    "INSERT OR IGNORE INTO expense_categories(id,name,grp,is_fixed,position)"
                    " VALUES(?,?,?,?,?)", (*row, i))
            for key, value in DEFAULT_SETTINGS.items():
                cur.execute(
                    "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
            cur.commit()

    def _seed_v3(self) -> None:
        """Справочники версии 3.0: склады, типы цен, группы номенклатуры."""
        from .schema_v3 import DEFAULT_NOM_GROUPS, DEFAULT_PRICE_TYPES, DEFAULT_WAREHOUSES
        with self.lock:
            cur = self.conn
            for row in DEFAULT_WAREHOUSES:
                cur.execute(
                    "INSERT OR IGNORE INTO warehouses(id,name,kind,retail,position)"
                    " VALUES(?,?,?,?,?)", row)
            for row in DEFAULT_PRICE_TYPES:
                cur.execute(
                    "INSERT OR IGNORE INTO price_types(id,name,markup,is_base,position)"
                    " VALUES(?,?,?,?,?)", row)
            for row in DEFAULT_NOM_GROUPS:
                cur.execute(
                    "INSERT OR IGNORE INTO nom_groups(id,parent_id,name,code,niche_id,position)"
                    " VALUES(?,?,?,?,?,?)", row)
            cur.commit()

    def _migrate_v3_data(self) -> None:
        """Перенос catalog + shelf_items в номенклатуру и регистр остатков.

        Выполняется один раз: признак — настройка `migrated_v3`. Старые таблицы
        не удаляются, чтобы можно было сверить данные.
        """
        with self.lock:
            done = self.conn.execute(
                "SELECT value FROM settings WHERE key='migrated_v3'").fetchone()
        if done and json.loads(done[0]) is True:
            return
        try:
            self._do_migrate_v3()
        except Exception as exc:  # миграция не должна ронять запуск
            self.add_event("error", "Миграция данных 3.0 не завершена", str(exc))
            return
        self.execute(
            "INSERT INTO settings(key,value) VALUES('migrated_v3','true')"
            " ON CONFLICT(key) DO UPDATE SET value='true'")

    def _do_migrate_v3(self) -> None:
        from .config import now_iso as _now
        import uuid as _uuid

        def new_id(prefix: str) -> str:
            return f"{prefix}_{_uuid.uuid4().hex[:10]}"

        def fnum(value, default=0.0) -> float:
            try:
                result = float(str(value).replace(",", "."))
                return result if result == result else default
            except (TypeError, ValueError):
                return default

        groups = {
            "pets": "g_pets", "home": "g_home", "business": "g_business",
        }
        base_type = "retail"
        shelf_wh = "shelf"
        counter = self.query("SELECT COUNT(*) n FROM nomenclature")
        index = int(fnum((counter[0] if counter else {}).get("n")))

        # 1) каталог моделей → номенклатура
        catalog_map: dict[str, str] = {}
        for row in self.query("SELECT * FROM catalog"):
            exists = self.one("SELECT id FROM nomenclature WHERE legacy_catalog_id=?",
                              (row["id"],))
            if exists:
                catalog_map[row["id"]] = exists["id"]
                continue
            index += 1
            nom_id = new_id("nom")
            self.upsert("nomenclature", {
                "id": nom_id, "code": f"{index:06d}", "name": row.get("name") or "Без названия",
                "group_id": groups.get(row.get("niche_id") or "", "g_products"),
                "kind": "product", "unit": "шт", "niche_id": row.get("niche_id"),
                "material": row.get("material") or "PLA",
                "grams": fnum(row.get("grams")), "hours": fnum(row.get("hours")),
                "fit_per_plate": max(1, int(fnum(row.get("fit_per_plate"), 1))),
                "file": row.get("file") or "", "note": row.get("notes") or "",
                "archived": int(fnum(row.get("archived"))),
                "legacy_catalog_id": row["id"],
                "created_at": row.get("created_at") or _now(), "updated_at": _now(),
            })
            catalog_map[row["id"]] = nom_id
            if fnum(row.get("price")) > 0:
                self.upsert("prices", {
                    "id": new_id("prc"), "at": row.get("created_at") or _now(),
                    "nom_id": nom_id, "price_type_id": base_type,
                    "price": round(fnum(row.get("price")), 2), "note": "перенос из базы изделий"})

        # 2) позиции стеллажа → номенклатура (или связь с уже перенесённой моделью)
        shelf_map: dict[str, str] = {}
        for row in self.query("SELECT * FROM shelf_items"):
            exists = self.one("SELECT id FROM nomenclature WHERE legacy_shelf_id=?",
                              (row["id"],))
            if exists:
                shelf_map[row["id"]] = exists["id"]
                continue
            linked = catalog_map.get(row.get("catalog_id") or "")
            if linked:
                nom_id = linked
                self.execute(
                    "UPDATE nomenclature SET legacy_shelf_id=?, min_qty=?, photo=?,"
                    " updated_at=? WHERE id=?",
                    (row["id"], fnum(row.get("min_qty")), row.get("photo") or "",
                     _now(), nom_id))
            else:
                index += 1
                nom_id = new_id("nom")
                self.upsert("nomenclature", {
                    "id": nom_id, "code": f"{index:06d}",
                    "name": row.get("name") or "Позиция полки",
                    "group_id": "g_products", "kind": "product", "unit": "шт",
                    "min_qty": fnum(row.get("min_qty")), "photo": row.get("photo") or "",
                    "note": row.get("note") or "", "legacy_shelf_id": row["id"],
                    "archived": 0 if int(fnum(row.get("active"), 1)) else 1,
                    "created_at": row.get("created_at") or _now(), "updated_at": _now(),
                })
            shelf_map[row["id"]] = nom_id
            if fnum(row.get("price")) > 0:
                self.upsert("prices", {
                    "id": new_id("prc"), "at": row.get("updated_at") or _now(),
                    "nom_id": nom_id, "price_type_id": base_type,
                    "price": round(fnum(row.get("price")), 2), "note": "перенос со стеллажа"})

        # 3) движения стеллажа → регистр остатков
        kind_map = {"produce": "production", "sale": "sale", "online": "sale",
                    "writeoff": "writeoff", "inventory": "inventory"}
        for row in self.query("SELECT * FROM shelf_moves ORDER BY datetime(at)"):
            nom_id = shelf_map.get(row.get("item_id") or "")
            if not nom_id:
                continue
            if self.one("SELECT id FROM stock_moves WHERE note LIKE ? AND nom_id=?",
                        (f"%перенос:{row['id']}%", nom_id)):
                continue
            qty = fnum(row.get("qty"))
            item = self.one("SELECT cost_per_unit FROM shelf_items WHERE id=?",
                            (row.get("item_id"),)) or {}
            unit_cost = fnum(item.get("cost_per_unit"))
            self.upsert("stock_moves", {
                "id": new_id("mv"), "at": row.get("at") or _now(),
                "doc_kind": kind_map.get(row.get("kind") or "", "receipt"),
                "nom_id": nom_id, "warehouse_id": shelf_wh,
                "qty": round(qty, 3), "cost": round(qty * unit_cost, 2),
                "job_id": row.get("job_id"),
                "note": f"перенос:{row['id']} {row.get('note') or ''}".strip()})

        # 4) остатки, которые есть в карточке, но не набрались движениями
        for legacy_id, nom_id in shelf_map.items():
            item = self.one("SELECT * FROM shelf_items WHERE id=?", (legacy_id,)) or {}
            want = fnum(item.get("qty"))
            have_row = self.one(
                "SELECT COALESCE(SUM(qty),0) v FROM stock_moves WHERE nom_id=? AND warehouse_id=?",
                (nom_id, shelf_wh)) or {}
            have = fnum(have_row.get("v"))
            diff = round(want - have, 3)
            if abs(diff) < 0.001:
                continue
            unit_cost = fnum(item.get("cost_per_unit"))
            self.upsert("stock_moves", {
                "id": new_id("mv"), "at": _now(), "doc_kind": "inventory",
                "nom_id": nom_id, "warehouse_id": shelf_wh,
                "qty": diff, "cost": round(diff * unit_cost, 2),
                "note": "перенос: выравнивание остатка при переходе на 3.0"})

        self.add_event("system", "Данные перенесены в учёт 3.0",
                       f"Номенклатура: {len(set(list(catalog_map.values()) + list(shelf_map.values())))} позиций")

    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def one(self, sql: str, params: Iterable = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Атомарная группа операций; вложенные вызовы используют одну транзакцию."""
        with self.lock:
            depth = getattr(self._local, "transaction_depth", 0)
            if depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._local.transaction_depth = depth + 1
            try:
                yield
            except Exception:
                self._local.transaction_depth = depth
                if depth == 0:
                    self.conn.rollback()
                raise
            else:
                self._local.transaction_depth = depth
                if depth == 0:
                    self.conn.commit()

    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            if not getattr(self._local, "transaction_depth", 0):
                self.conn.commit()
            return cur

    def executemany(self, sql: str, seq: Iterable[Iterable]) -> None:
        with self.lock:
            self.conn.executemany(sql, [tuple(x) for x in seq])
            if not getattr(self._local, "transaction_depth", 0):
                self.conn.commit()

    def upsert(self, table: str, data: dict[str, Any], key: str = "id") -> dict:
        """Вставить или обновить запись по первичному ключу."""
        columns = self.columns(table)
        payload = {k: v for k, v in data.items() if k in columns}
        if key not in payload or not payload[key]:
            raise ValueError(f"{table}: не указан {key}")
        placeholders = ",".join("?" for _ in payload)
        names = ",".join(payload)
        updates = ",".join(f"{k}=excluded.{k}" for k in payload if k != key)
        sql = f"INSERT INTO {table}({names}) VALUES({placeholders})"
        if updates:
            sql += f" ON CONFLICT({key}) DO UPDATE SET {updates}"
        self.execute(sql, list(payload.values()))
        return self.one(f"SELECT * FROM {table} WHERE {key}=?", (payload[key],)) or {}

    def columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.query(f"PRAGMA table_info({table})")}

    def delete(self, table: str, ident: str, key: str = "id") -> None:
        self.execute(f"DELETE FROM {table} WHERE {key}=?", (ident,))

    # -------------------------------------------------------------- настройки
    def settings(self, include_secrets: bool = False) -> dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        for row in self.query("SELECT key,value FROM settings"):
            try:
                data[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                data[row["key"]] = row["value"]
        from .config import SECRET_SETTINGS
        if include_secrets:
            # Расшифровываем секреты только явно запрошенным читателям.
            from .crypto import decrypt
            for key in SECRET_SETTINGS:
                if key in data and isinstance(data[key], str):
                    data[key] = decrypt(data[key])
        else:
            for key in SECRET_SETTINGS:
                data[f"has_{key}"] = bool(data.get(key))
                data[key] = "••••••••" if data.get(key) else ""
        return data

    def setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if not row:
            return DEFAULT_SETTINGS.get(key, default)
        value = row["value"]
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
        from .config import SECRET_SETTINGS
        if key in SECRET_SETTINGS and isinstance(value, str):
            from .crypto import decrypt
            value = decrypt(value)
        return value

    def set_settings(self, patch: dict[str, Any]) -> dict:
        from .config import SECRET_SETTINGS
        from .crypto import encrypt
        for key, value in patch.items():
            if key not in DEFAULT_SETTINGS:
                continue
            if key in SECRET_SETTINGS and (value == "" or value == "••••••••"):
                continue  # пустое поле означает «не менять сохранённый секрет»
            if key in SECRET_SETTINGS and isinstance(value, str) and value:
                value = encrypt(value)  # секрет в базе — только зашифрованным
            self.execute(
                "INSERT INTO settings(key,value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        return self.settings()

    def clear_settings(self, keys: Iterable[str]) -> None:
        """Удалить настройки целиком — в т.ч. секреты (set_settings их не трогает)."""
        for key in keys:
            self.execute("DELETE FROM settings WHERE key=?", (key,))

    # ------------------------------------------------------------------ events
    def add_event(self, kind: str, title: str, detail: str = "",
                  printer_id: str = "", data: dict | None = None) -> dict:
        cur = self.execute(
            "INSERT INTO events(at,printer_id,kind,title,detail,data) VALUES(?,?,?,?,?,?)",
            (now_iso(), printer_id, kind, title, detail,
             json.dumps(data or {}, ensure_ascii=False)),
        )
        row = {"id": cur.lastrowid, "at": now_iso(), "kind": kind, "title": title,
               "detail": detail, "printer_id": printer_id, "data": data or {}}
        # Открытые вкладки узнают о событии сразу, без опроса по таймеру.
        if self.bus is not None:
            try:
                self.bus.publish("event", row)
            except Exception:
                pass
        return row

    def events(self, limit: int = 100, printer_id: str = "", kind: str = "") -> list[dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if printer_id:
            sql += " AND printer_id=?"
            params.append(printer_id)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        rows = self.query(sql, params)
        for row in rows:
            try:
                row["data"] = json.loads(row.get("data") or "{}")
            except json.JSONDecodeError:
                row["data"] = {}
        return rows

    def backup_to(self, target) -> None:
        """Консистентная копия базы через SQLite API (безопасна под нагрузкой)."""
        import sqlite3
        with self.lock:
            dest = sqlite3.connect(str(target))
            try:
                self.conn.backup(dest)
            finally:
                dest.close()

    def close(self) -> None:
        with self.lock:
            self.conn.close()


# ------------------------------------------------------- копии и откат базы

def list_backups() -> list[dict]:
    """Список файлов-копий базы в каталоге backups (новые сверху)."""
    if not BACKUP_DIR.exists():
        return []
    items: list[dict] = []
    for path in BACKUP_DIR.glob("*.sqlite3"):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({
            "name": path.name,
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        })
    items.sort(key=lambda item: (item["mtime"], item["name"]), reverse=True)
    return items


def make_backup(prefix: str = "printflow-manual") -> dict:
    """Консистентная копия базы файлом (безопасна при работающем сервере)."""
    ensure_dirs()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{prefix}-{stamp}.sqlite3"
    if not DB_FILE.exists():
        return {"ok": False, "error": "База ещё не создана"}
    source = sqlite3.connect(str(DB_FILE))
    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return {"ok": True, "file": target.name}


def request_restore(filename: str) -> dict:
    """Запланировать откат: страховочная копия текущей базы + маркер.

    Сам откат выполняет `apply_pending_restore()` при следующем запуске —
    до открытия базы, поэтому файл никогда не подменяется под живым
    соединением.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if filename != Path(filename).name or not filename.endswith(".sqlite3"):
        raise ValueError("Недопустимое имя копии")
    source = BACKUP_DIR / filename
    if not source.is_file():
        raise ValueError("Копия не найдена")
    safety = ""
    if DB_FILE.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safety_file = BACKUP_DIR / f"before-restore-{stamp}.sqlite3"
        src = sqlite3.connect(str(DB_FILE))
        dest = sqlite3.connect(str(safety_file))
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        safety = safety_file.name
    RESTORE_REQUEST.write_text(
        json.dumps({"file": filename, "at": now_iso()}, ensure_ascii=False),
        encoding="utf-8")
    return {"ok": True, "file": filename, "safety": safety}


def pending_restore() -> dict | None:
    """Прочитать маркер отложенного восстановления (без выполнения)."""
    if not RESTORE_REQUEST.exists():
        return None
    try:
        data = json.loads(RESTORE_REQUEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def apply_pending_restore() -> dict | None:
    """Выполнить отложенное восстановление базы из копии.

    Вызывается при старте, до открытия `Database`. Возвращает итог или None,
    если восстанавливать нечего. Маркер удаляется в любом случае.
    """
    request = pending_restore()
    if request is None:
        return None
    filename = str(request.get("file") or "")
    result: dict = {"restored": ""}
    if filename == Path(filename).name and filename.endswith(".sqlite3"):
        source = BACKUP_DIR / filename
        if source.is_file():
            try:
                shutil.copyfile(source, DB_FILE)
                result["restored"] = filename
            except OSError as exc:
                result["error"] = str(exc)
        else:
            result["error"] = f"копия {filename} не найдена"
    else:
        result["error"] = "недопустимое имя копии в маркере"
    try:
        RESTORE_REQUEST.unlink()
    except OSError:
        pass
    return result
