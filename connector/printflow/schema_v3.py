"""Схема PrintFlow 3.0 — учёт в духе 1С.

Ключевое отличие от версии 2: остаток товара больше не хранится числом в
карточке. Источник правды — регистр накопления `stock_moves`, а карточка
показывает сумму движений. Любое расхождение лечится пересчётом, а не
ручной правкой.

Сущности:
    • номенклатура (единый справочник вместо catalog + shelf_items);
    • группы номенклатуры — дерево с наследованием наценки и ниши;
    • характеристики — цвет и размер как измерения одной номенклатуры;
    • склады — полка, дом, витрина, брак;
    • документы — приход, продажа, перемещение, списание, инвентаризация,
      производство, возврат; проводятся и распроводятся;
    • регистр остатков и регистр резервов;
    • типы цен и история их установки;
    • спецификации (BOM) для комплектов и полуфабрикатов.
"""
from __future__ import annotations

SCHEMA_V3 = """
-- ------------------------------------------------------------ справочники
CREATE TABLE IF NOT EXISTS nom_groups (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    name TEXT DEFAULT '',
    code TEXT DEFAULT '',
    niche_id TEXT,
    markup REAL DEFAULT 0,          -- наценка группы по умолчанию, %
    vat REAL DEFAULT 0,
    position INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_nomgroup_parent ON nom_groups(parent_id);

CREATE TABLE IF NOT EXISTS nomenclature (
    id TEXT PRIMARY KEY,
    code TEXT DEFAULT '',           -- внутренний код 000001
    sku TEXT DEFAULT '',            -- артикул
    barcode TEXT DEFAULT '',
    name TEXT DEFAULT '',
    group_id TEXT,
    kind TEXT DEFAULT 'product',    -- product|material|service|kit|semi
    unit TEXT DEFAULT 'шт',
    niche_id TEXT,
    material TEXT DEFAULT '',
    grams REAL DEFAULT 0,           -- норматив пластика на штуку
    hours REAL DEFAULT 0,           -- норматив печати на штуку
    fit_per_plate INTEGER DEFAULT 1,
    post_minutes REAL DEFAULT 0,    -- постобработка, мин на штуку
    file TEXT DEFAULT '',           -- имя 3MF/gcode на принтере
    model_url TEXT DEFAULT '',      -- откуда модель
    license TEXT DEFAULT '',        -- коммерческая лицензия
    vat REAL DEFAULT 0,
    marked INTEGER DEFAULT 0,       -- подлежит обязательной маркировке
    min_qty REAL DEFAULT 0,         -- минимальный запас
    max_qty REAL DEFAULT 0,         -- максимальный запас
    shelf_life_days REAL DEFAULT 0,
    photo TEXT DEFAULT '',
    note TEXT DEFAULT '',
    archived INTEGER DEFAULT 0,
    legacy_catalog_id TEXT,         -- откуда приехало при миграции
    legacy_shelf_id TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_nom_group ON nomenclature(group_id);
CREATE INDEX IF NOT EXISTS idx_nom_kind ON nomenclature(kind, archived);
CREATE INDEX IF NOT EXISTS idx_nom_sku ON nomenclature(sku);

CREATE TABLE IF NOT EXISTS nom_variants (
    id TEXT PRIMARY KEY,
    nom_id TEXT,
    name TEXT DEFAULT '',           -- «Чёрный / L»
    color_name TEXT DEFAULT '',
    color_hex TEXT DEFAULT '',
    size TEXT DEFAULT '',
    sku TEXT DEFAULT '',
    barcode TEXT DEFAULT '',
    grams REAL DEFAULT 0,           -- переопределение норматива
    hours REAL DEFAULT 0,
    file TEXT DEFAULT '',
    position INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_variant_nom ON nom_variants(nom_id);

CREATE TABLE IF NOT EXISTS warehouses (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    kind TEXT DEFAULT 'shelf',      -- shelf|home|window|defect|transit|material
    address TEXT DEFAULT '',
    retail INTEGER DEFAULT 0,       -- розничная точка: продажи идут отсюда
    position INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    note TEXT DEFAULT ''
);

-- ------------------------------------------------------------ типы цен
CREATE TABLE IF NOT EXISTS price_types (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    markup REAL DEFAULT 0,          -- наценка к себестоимости, %
    vat_included INTEGER DEFAULT 1,
    is_base INTEGER DEFAULT 0,      -- основная цена продажи
    position INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prices (
    id TEXT PRIMARY KEY,
    at TEXT,
    nom_id TEXT,
    variant_id TEXT,
    price_type_id TEXT,
    price REAL DEFAULT 0,
    doc_id TEXT,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_prices_nom ON prices(nom_id, price_type_id, at);

-- ------------------------------------------------------------ спецификации
CREATE TABLE IF NOT EXISTS specs (
    id TEXT PRIMARY KEY,
    nom_id TEXT,                    -- что производим
    name TEXT DEFAULT 'Основная',
    active INTEGER DEFAULT 1,
    note TEXT DEFAULT '',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_specs_nom ON specs(nom_id);

CREATE TABLE IF NOT EXISTS spec_items (
    id TEXT PRIMARY KEY,
    spec_id TEXT,
    line INTEGER DEFAULT 0,
    nom_id TEXT,                    -- из чего
    variant_id TEXT,
    qty REAL DEFAULT 1,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_specitems_spec ON spec_items(spec_id);

-- ------------------------------------------------------------ документы
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    number TEXT DEFAULT '',
    kind TEXT DEFAULT 'receipt',    -- receipt|sale|move|writeoff|inventory|
                                    -- production|return|pricing
    at TEXT,
    state TEXT DEFAULT 'draft',     -- draft|posted
    warehouse_id TEXT,
    warehouse_to_id TEXT,           -- для перемещения
    counterparty_id TEXT,
    order_id TEXT,
    batch_id TEXT,
    account_id TEXT,
    channel TEXT DEFAULT '',
    price_type_id TEXT,
    qty_total REAL DEFAULT 0,
    amount REAL DEFAULT 0,          -- сумма продажи
    cost_total REAL DEFAULT 0,      -- себестоимость движения
    discount REAL DEFAULT 0,
    tx_id TEXT,                     -- проводка в кассе
    reason TEXT DEFAULT '',         -- причина списания / брака
    note TEXT DEFAULT '',
    author TEXT DEFAULT '',
    created_at TEXT,
    posted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_kind ON documents(kind, at);
CREATE INDEX IF NOT EXISTS idx_docs_state ON documents(state);

CREATE TABLE IF NOT EXISTS doc_items (
    id TEXT PRIMARY KEY,
    doc_id TEXT,
    line INTEGER DEFAULT 0,
    nom_id TEXT,
    variant_id TEXT,
    qty REAL DEFAULT 0,
    qty_fact REAL DEFAULT 0,        -- инвентаризация: факт
    price REAL DEFAULT 0,
    cost REAL DEFAULT 0,            -- себестоимость за единицу
    amount REAL DEFAULT 0,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_docitems_doc ON doc_items(doc_id);

-- ------------------------------------------------- регистр остатков
CREATE TABLE IF NOT EXISTS stock_moves (
    id TEXT PRIMARY KEY,
    at TEXT,
    doc_id TEXT,
    doc_kind TEXT DEFAULT '',
    nom_id TEXT,
    variant_id TEXT,
    warehouse_id TEXT,
    qty REAL DEFAULT 0,             -- знак: + приход, − расход
    cost REAL DEFAULT 0,            -- сумма движения по себестоимости
    batch_id TEXT,
    job_id TEXT,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_moves_nom ON stock_moves(nom_id, warehouse_id, at);
CREATE INDEX IF NOT EXISTS idx_moves_doc ON stock_moves(doc_id);

CREATE TABLE IF NOT EXISTS reserves (
    id TEXT PRIMARY KEY,
    at TEXT,
    nom_id TEXT,
    variant_id TEXT,
    warehouse_id TEXT,
    qty REAL DEFAULT 0,
    order_id TEXT,
    doc_id TEXT,
    state TEXT DEFAULT 'active',    -- active|released
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_reserves_nom ON reserves(nom_id, state);

-- ------------------------------------------------- нумерация документов
CREATE TABLE IF NOT EXISTS doc_counters (
    kind TEXT,
    year TEXT,
    last INTEGER DEFAULT 0,
    PRIMARY KEY (kind, year)
);

-- ------------------------------------------------- партии печати
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    number TEXT DEFAULT '',
    at TEXT,
    nom_id TEXT,
    variant_id TEXT,
    warehouse_id TEXT,
    order_id TEXT,
    name TEXT DEFAULT '',
    qty_planned REAL DEFAULT 0,
    qty_done REAL DEFAULT 0,
    qty_scrap REAL DEFAULT 0,
    fit_per_plate INTEGER DEFAULT 1,
    plates INTEGER DEFAULT 1,
    mode TEXT DEFAULT 'full',       -- full|exact|manual
    file TEXT DEFAULT '',
    printer_id TEXT,
    spool_id TEXT,
    material TEXT DEFAULT '',
    est_grams REAL DEFAULT 0,
    est_minutes REAL DEFAULT 0,
    cost REAL DEFAULT 0,
    state TEXT DEFAULT 'planned',   -- planned|printing|partial|done|cancelled
    note TEXT DEFAULT '',
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_state ON batches(state, at);

-- ------------------------------------------------- журнал действий
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    entity TEXT DEFAULT '',
    entity_id TEXT DEFAULT '',
    action TEXT DEFAULT '',
    title TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    data TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id);
"""

# Склады по умолчанию: (id, название, вид, розница, позиция)
DEFAULT_WAREHOUSES = [
    ("shelf", "Полка магазина", "shelf", 1, 0),
    ("home", "Домашний склад", "home", 0, 1),
    ("defect", "Брак", "defect", 0, 2),
]

# Типы цен: (id, название, наценка %, основная, позиция)
DEFAULT_PRICE_TYPES = [
    ("retail", "Розница", 150.0, 1, 0),
    ("shelf", "Полка", 150.0, 0, 1),
    ("wholesale", "Опт", 80.0, 0, 2),
    ("b2b", "B2B по счёту", 100.0, 0, 3),
]

# Группы номенклатуры: (id, родитель, название, код, ниша, позиция)
DEFAULT_NOM_GROUPS = [
    ("g_products", None, "Товары", "01", "", 0),
    ("g_pets", "g_products", "Питомцы", "0101", "pets", 0),
    ("g_home", "g_products", "Дом", "0102", "home", 1),
    ("g_business", "g_products", "Бизнес", "0103", "business", 2),
    ("g_materials", None, "Материалы", "02", "", 1),
    ("g_services", None, "Услуги", "03", "", 2),
]

# Виды номенклатуры для интерфейса
NOM_KINDS = {
    "product": "Товар",
    "semi": "Полуфабрикат",
    "kit": "Комплект",
    "material": "Материал",
    "service": "Услуга",
}

# Виды документов: код → (название, префикс номера, влияет на склад)
DOC_KINDS = {
    "receipt": ("Приход", "ПР", True),
    "sale": ("Продажа", "РН", True),
    "move": ("Перемещение", "ПМ", True),
    "writeoff": ("Списание", "СП", True),
    "inventory": ("Инвентаризация", "ИН", True),
    "production": ("Производство", "ПЗ", True),
    "return": ("Возврат", "ВЗ", True),
    "pricing": ("Установка цен", "ЦН", False),
}

DOC_STATES = {"draft": "Черновик", "posted": "Проведён"}
