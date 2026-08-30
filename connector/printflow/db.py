"""SQLite-хранилище PrintFlow — источник правды для всей системы.

Все данные лежат в одном файле в каталоге пользователя. Схема версионируется
через user_version, чтобы обновления не теряли данные.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import (BACKUP_DIR, DB_FILE, DEFAULT_ACCOUNTS, DEFAULT_CHANNELS,
                     DEFAULT_EXPENSE_CATEGORIES, DEFAULT_NICHES, DEFAULT_SETTINGS,
                     DEFAULT_STATUSES, EXTRA_STATUSES, RESTORE_REQUEST, ensure_dirs,
                     now_iso, rotate_backups)

SCHEMA_VERSION = 14

# Колонки, добавленные после первой версии схемы. Ключ — таблица,
# значение — список (колонка, SQL-тип со значением по умолчанию).
ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "materials": [
        # встроенный тип из каталога (можно править под себя; 0 — свой материал)
        ("builtin", "INTEGER DEFAULT 0"),
    ],
    "nomenclature": [
        # фактическая себестоимость штуки из завершённых партий
        ("cost", "REAL DEFAULT 0"),
        # печатная группа мелких товаров: позиции с одинаковой группой
        # сворачиваются в одну строку в печатных формах (схема 13)
        ("print_group", "TEXT DEFAULT ''"),
        # публикация во внешней витрине/клиентском Telegram-боте
        ("client_bot_published", "INTEGER DEFAULT 1"),
        ("client_bot_description", "TEXT DEFAULT ''"),
    ],
    "client_orders": [
        # отметка о напоминании «заказ ждёт на полке» (9.3.2): пусто — не писали
        ("reminded_at", "TEXT DEFAULT ''"),
        ("update_id", "TEXT DEFAULT ''"),
        ("source", "TEXT DEFAULT ''"),
        ("last_notified_status", "TEXT DEFAULT ''"),
    ],
    "batches": [
        # смешанная плита: разные товары на одном столе, JSON-состав
        # [{"nom_id": "...", "qty_per_plate": 3, "grams": 40, "hours": 1.2}]
        ("items", "TEXT DEFAULT ''"),
    ],
    "client_chats": [
        # Воронка 10.0: обращение проходит путь от нового лида до заказа.
        ("pipeline_stage", "TEXT DEFAULT 'new'"),
        ("last_contact_at", "TEXT DEFAULT ''"),
        ("tg_user_id", "TEXT DEFAULT ''"),
        ("customer_id", "TEXT DEFAULT ''"),
        ("phone_verified", "INTEGER DEFAULT 0"),
        ("source", "TEXT DEFAULT ''"),
        ("status_notify", "INTEGER DEFAULT 1"),
        ("marketing_opt_in", "INTEGER DEFAULT 0"),
        ("quiet_from", "TEXT DEFAULT ''"),
        ("quiet_to", "TEXT DEFAULT ''"),
        ("inbox_status", "TEXT DEFAULT 'open'"),
        ("assigned_to", "TEXT DEFAULT ''"),
        ("last_error", "TEXT DEFAULT ''"),
    ],
    "client_bot_log": [
        ("update_id", "TEXT DEFAULT ''"),
        ("direction", "TEXT DEFAULT 'in'"),
        ("event", "TEXT DEFAULT ''"),
        ("order_id", "TEXT DEFAULT ''"),
        ("source", "TEXT DEFAULT ''"),
        ("unread", "INTEGER DEFAULT 0"),
        ("operator", "TEXT DEFAULT ''"),
    ],
    "client_reviews": [
        ("state", "TEXT DEFAULT 'new'"),
        ("sent_at", "TEXT DEFAULT ''"),
        ("resolved_at", "TEXT DEFAULT ''"),
        ("operator_note", "TEXT DEFAULT ''"),
    ],
    "order_items": [
        ("variant_id", "TEXT DEFAULT ''"),
    ],
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
    "payments": [
        # Клиентский ключ запроса: повторный клик/сетевой retry не создаёт
        # вторую оплату по тому же подтверждению.
        ("request_id", "TEXT DEFAULT ''"),
        ("updated_at", "TEXT DEFAULT ''"),
    ],
    "shopping_items": [
        # Фактический приход закупки: одна подтверждённая операция создаёт
        # отдельные катушки и финансовый расход, сетевой retry её не дублирует.
        ("color_name", "TEXT DEFAULT ''"),
        ("brand", "TEXT DEFAULT ''"),
        ("received_at", "TEXT DEFAULT ''"),
        ("receipt_request_id", "TEXT DEFAULT ''"),
        ("receipt_spool_ids", "TEXT DEFAULT ''"),
        ("receipt_amount", "REAL DEFAULT 0"),
        ("receipt_tx_id", "TEXT DEFAULT ''"),
        ("received_qty", "INTEGER DEFAULT 0"),
        ("received_spool_grams", "REAL DEFAULT 0"),
        ("receipt_doc_id", "TEXT DEFAULT ''"),
        ("price_per_kg", "REAL DEFAULT 0"),
        ("supplier_id", "TEXT DEFAULT ''"),
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
        ("spools", "TEXT DEFAULT ''"),       # катушки заказа: JSON [{spool_id,grams,note}]
        ("qc_done", "TEXT DEFAULT ''"),      # чек-лист качества: JSON {step: true}
        ("nom_id", "TEXT"),                  # позиция номенклатуры (3.0)
        ("warehouse_id", "TEXT"),            # с какого склада отгружаем
        ("reminded_at", "TEXT DEFAULT ''"),  # когда напоминали о долге (B4)
        ("reserved", "INTEGER DEFAULT 0"),   # зарезервирован ли товар
        ("items_override", "INTEGER DEFAULT 0"),  # явное переопределение состава
        ("gift", "INTEGER DEFAULT 0"),       # подарочный режим (идея 33): цена скрыта
        ("client_source", "TEXT DEFAULT ''"),
        ("client_request_id", "TEXT DEFAULT ''"),
        ("client_track_token_hash", "TEXT DEFAULT ''"),
        ("client_track_token_at", "TEXT DEFAULT ''"),
        ("client_quote_status", "TEXT DEFAULT ''"),
        ("client_quote_version", "INTEGER DEFAULT 0"),
        ("client_quote_sent_at", "TEXT DEFAULT ''"),
        ("client_quote_accepted_at", "TEXT DEFAULT ''"),
        ("client_variant_id", "TEXT DEFAULT ''"),
        ("client_ready_at", "TEXT DEFAULT ''"),
        ("client_delivered_at", "TEXT DEFAULT ''"),
    ],
    "print_jobs": [
        ("est_minutes", "REAL DEFAULT 0"),   # оценка из слайсера (3MF/G-code)
        ("est_grams", "REAL DEFAULT 0"),
        ("batch_id", "TEXT"),                # к какой партии относится запуск
        ("batch_qty", "REAL DEFAULT 0"),     # сколько годных штук даёт запуск
        # Идентификатор задания, который сообщает принтер. Нужен, чтобы
        # повторное MQTT-событие FINISH не создало вторую завершённую печать.
        ("remote_task_id", "TEXT DEFAULT ''"),
        # Ставится в той же транзакции, что списание пластика и себестоимости.
        # Пока поле пусто, оборванную финализацию можно безопасно повторить.
        ("accounted_at", "TEXT DEFAULT ''"),
        # Подтверждённый повтор: связь с исходным заданием/браком и ключ,
        # который не даст двойному клику создать несколько клонов.
        ("reprint_of_job_id", "TEXT DEFAULT ''"),
        ("reprint_request_id", "TEXT DEFAULT ''"),
        ("defect_id", "TEXT DEFAULT ''"),
        # Восстановление только после подтверждённой потери связи/питания.
        ("resume_eligible", "INTEGER DEFAULT 1"),
        ("manual_paused", "INTEGER DEFAULT 0"),
        ("power_loss_at", "TEXT DEFAULT ''"),
        ("resume_attempts", "INTEGER DEFAULT 0"),
        ("resume_reason", "TEXT DEFAULT ''"),
        ("file_version", "TEXT DEFAULT ''"),
        ("power_loss_state", "TEXT DEFAULT ''"),
        ("power_loss_progress", "REAL DEFAULT 0"),
        ("power_loss_layer", "INTEGER DEFAULT 0"),
        ("power_loss_total_layers", "INTEGER DEFAULT 0"),
        ("power_loss_task", "TEXT DEFAULT ''"),
        ("start_request_id", "TEXT DEFAULT ''"),
        ("mixed_label", "TEXT DEFAULT ''"),
        ("no_auto", "INTEGER DEFAULT 0"),
        ("plate_preset_id", "TEXT DEFAULT ''"),
    ],
    "defects": [
        ("note", "TEXT DEFAULT ''"),
        ("confirmed_at", "TEXT DEFAULT ''"),
        ("request_id", "TEXT DEFAULT ''"),
        ("loss_source", "TEXT DEFAULT ''"),
        ("reprint_requested", "INTEGER DEFAULT 0"),
        ("reprint_job_id", "TEXT DEFAULT ''"),
    ],
    "customers": [
        ("tg_user_id", "TEXT DEFAULT ''"),
        ("kind", "TEXT DEFAULT 'person'"),   # person | company
        ("inn", "TEXT DEFAULT ''"),
        ("segment", "TEXT DEFAULT ''"),
        ("discount", "REAL DEFAULT 0"),
        ("price_type_id", "TEXT"),
        ("credit_limit", "REAL DEFAULT 0"),
        ("address", "TEXT DEFAULT ''"),
        ("email", "TEXT DEFAULT ''"),
        ("portal_code", "TEXT DEFAULT ''"),  # код «Мой NOZZA» (идея 94)
    ],
    "spools": [
        ("warehouse_id", "TEXT"),            # где физически лежит катушка
        ("cell", "TEXT DEFAULT ''"),         # адрес хранения
        ("opened_at", "TEXT"),               # когда вскрыта
        ("supplier", "TEXT DEFAULT ''"),
        ("ams_sync", "INTEGER DEFAULT 1"),   # обновлять остаток/слот из AMS
        ("synced_at", "TEXT"),               # когда AMS последний раз обновлял
        ("verified", "INTEGER DEFAULT 1"),   # импорт AMS требует проверки бренда/цены/веса
        ("location", "TEXT DEFAULT 'shop'"),  # ярлык места: shop/home/ams/dry/other
        ("location_note", "TEXT DEFAULT ''"),
        ("label_note", "TEXT DEFAULT ''"),
        ("qr_payload", "TEXT DEFAULT ''"),
        ("price_per_kg", "REAL DEFAULT 0"),
        ("supplier_id", "TEXT DEFAULT ''"),
        ("received_doc_id", "TEXT DEFAULT ''"),
    ],
    "catalog": [
        # Старые финансовые поля плюс связь с canonical nomenclature.
        ("cost", "REAL DEFAULT 0"),
        ("sold", "INTEGER DEFAULT 0"),
        ("nom_id", "TEXT DEFAULT ''"),
        ("updated_at", "TEXT DEFAULT ''"),
    ],
    "shelf_items": [
        # Код и артикул должны совпадать с карточкой номенклатуры в 1С:
        # кассовый сканер читает Code 128 с ценника без отдельной переклейки.
        ("nom_id", "TEXT DEFAULT ''"),
        ("barcode", "TEXT DEFAULT ''"),
        ("sku", "TEXT DEFAULT ''"),
        # Физический формат: standard (67×32) или promo (67×57). Старые
        # classic/compact/minimal нормализуются в Shelf при чтении/сохранении.
        ("tag_template", "TEXT DEFAULT 'standard'"),
        # Вариант — только визуальный: физический формат остаётся 67×32/67×57.
        ("tag_variant", "TEXT DEFAULT 'clean'"),
        ("tag_badge", "TEXT DEFAULT ''"),
        ("tag_color", "TEXT DEFAULT '#4f46e5'"),
        ("tag_note", "TEXT DEFAULT ''"),
        ("tag_old_price", "REAL DEFAULT 0"),
    ],
    "shelf_moves": [
        # Внешний ключ строки чека делает повторную отправку из 1С безопасной.
        ("source", "TEXT DEFAULT ''"),
        ("external_id", "TEXT DEFAULT ''"),
    ],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Команда: сотрудник и руководитель в Telegram-боте. Владелец остаётся
-- в настройке telegram_chat_id; staff — дополнительные чаты с ролями.
CREATE TABLE IF NOT EXISTS staff (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    role TEXT DEFAULT 'employee',     -- employee | manager
    chat_id TEXT DEFAULT '',
    tg_user_id TEXT DEFAULT '',
    note TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_staff_chat ON staff(chat_id);

-- Одноразовые коды приглашения: «старт PF-XXXX» в чат бота — и человек
-- автоматически становится сотрудником/руководителем без ручного ввода chat_id.
CREATE TABLE IF NOT EXISTS staff_invites (
    code TEXT PRIMARY KEY,
    role TEXT DEFAULT 'employee',
    name TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    used INTEGER DEFAULT 0,
    used_by TEXT DEFAULT '',
    created_at TEXT
);

-- Клиентский бот: чаты покупателей (9.4).
CREATE TABLE IF NOT EXISTS client_chats (
    chat_id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    username TEXT DEFAULT '',
    tg_user_id TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    phone_verified INTEGER DEFAULT 0,
    customer_id TEXT DEFAULT '',
    source TEXT DEFAULT '',
    status_notify INTEGER DEFAULT 1,
    marketing_opt_in INTEGER DEFAULT 0,
    quiet_from TEXT DEFAULT '',
    quiet_to TEXT DEFAULT '',
    inbox_status TEXT DEFAULT 'open',
    assigned_to TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    created_at TEXT,
    last_seen TEXT
);
-- Связь «чат ↔ заказ» для отслеживания и уведомлений о статусе.
CREATE TABLE IF NOT EXISTS client_orders (
    chat_id TEXT DEFAULT '',
    order_id TEXT DEFAULT '',
    number TEXT DEFAULT '',
    update_id TEXT DEFAULT '',
    source TEXT DEFAULT '',
    reminded_at TEXT DEFAULT '',
    last_notified_status TEXT DEFAULT '',
    created_at TEXT,
    PRIMARY KEY (chat_id, order_id)
);

-- Журнал диалогов клиентского бота (для вкладки панели, без засорения events).
CREATE TABLE IF NOT EXISTS client_bot_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    chat_id TEXT DEFAULT '',
    name TEXT DEFAULT '',
    text TEXT DEFAULT '',
    answer TEXT DEFAULT '',
    kind TEXT DEFAULT 'message',      -- message | order | status | push | join | answer | paid
    update_id TEXT DEFAULT '',
    direction TEXT DEFAULT 'in',      -- in | out | system
    event TEXT DEFAULT '',
    order_id TEXT DEFAULT '',
    source TEXT DEFAULT '',
    unread INTEGER DEFAULT 0,
    operator TEXT DEFAULT ''
);

-- Отзывы покупателей после выдачи: рейтинг — кнопкой, комментарий — следующим
-- сообщением. Отдельная таблица остаётся совместимой с ручным customer_feedback.
CREATE TABLE IF NOT EXISTS client_reviews (
    order_id TEXT,
    chat_id TEXT DEFAULT '',
    asked_at TEXT,
    rating TEXT DEFAULT '',          -- good | bad | ''
    comment TEXT DEFAULT '',
    state TEXT DEFAULT 'new',
    sent_at TEXT DEFAULT '',
    resolved_at TEXT DEFAULT '',
    operator_note TEXT DEFAULT '',
    created_at TEXT,
    PRIMARY KEY (order_id, chat_id)
);

-- Реестр обработанных Telegram update_id. Удаляет повторную доставку после
-- retry сети и позволяет продолжить polling после перезапуска.
CREATE TABLE IF NOT EXISTS client_bot_updates (
    update_id TEXT PRIMARY KEY,
    chat_id TEXT DEFAULT '',
    kind TEXT DEFAULT '',
    state TEXT DEFAULT 'processing', -- processing | done | failed
    received_at TEXT,
    processed_at TEXT DEFAULT '',
    result TEXT DEFAULT '',
    error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_client_updates_state ON client_bot_updates(state, update_id);

-- Дедупликация обновлений рабочего Telegram-бота. Отдельная таблица не
-- смешивает внутренние команды и клиентские сообщения.
CREATE TABLE IF NOT EXISTS telegram_bot_updates (
    update_id TEXT PRIMARY KEY,
    state TEXT DEFAULT 'processing', -- processing | done | failed
    received_at TEXT,
    processed_at TEXT DEFAULT '',
    error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_telegram_updates_state ON telegram_bot_updates(state, update_id);

-- Избранное покупателя — отдельная от заказа сущность, не раскрывает каталог
-- другим чатам.
CREATE TABLE IF NOT EXISTS client_wishlist (
    chat_id TEXT,
    nom_id TEXT,
    created_at TEXT,
    PRIMARY KEY (chat_id, nom_id)
);

-- Корзина покупателя: черновик переживает перезапуск и не создаёт заказ до
-- явного подтверждения «Оформить заявку».
CREATE TABLE IF NOT EXISTS client_bot_cart (
    chat_id TEXT,
    nom_id TEXT,
    variant_id TEXT DEFAULT '',
    qty REAL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    PRIMARY KEY (chat_id, nom_id, variant_id)
);
CREATE INDEX IF NOT EXISTS idx_client_cart_chat ON client_bot_cart(chat_id, updated_at);

-- Черновик мастера индивидуальной заявки: переживает перезапуск бота.
CREATE TABLE IF NOT EXISTS client_bot_drafts (
    chat_id TEXT PRIMARY KEY,
    step TEXT DEFAULT '',
    data TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT
);

-- Durable outbox: Telegram может быть недоступен, но сообщение не теряется.
CREATE TABLE IF NOT EXISTS client_bot_outbox (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT UNIQUE,
    chat_id TEXT DEFAULT '',
    method TEXT DEFAULT 'sendMessage',
    payload TEXT DEFAULT '{}',
    file_path TEXT DEFAULT '',
    state TEXT DEFAULT 'pending',   -- pending | sending | sent | failed
    attempts INTEGER DEFAULT 0,
    available_at TEXT,
    last_error TEXT DEFAULT '',
    created_at TEXT,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_client_outbox_due
    ON client_bot_outbox(state, available_at, id);

-- Подтверждение ручной оплаты: заявка клиента отдельно от кассового платежа.
CREATE TABLE IF NOT EXISTS client_payment_intents (
    id TEXT PRIMARY KEY,
    order_id TEXT DEFAULT '',
    chat_id TEXT DEFAULT '',
    request_id TEXT UNIQUE,
    amount REAL DEFAULT 0,
    currency TEXT DEFAULT 'RUB',
    purpose TEXT DEFAULT '',
    status TEXT DEFAULT 'pending', -- pending | confirmed | rejected
    client_note TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    confirmed_at TEXT DEFAULT '',
    confirmed_by TEXT DEFAULT '',
    payment_id TEXT DEFAULT '',
    reject_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_client_payment_order
    ON client_payment_intents(order_id, status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_client_payment_pending
    ON client_payment_intents(order_id, chat_id) WHERE status='pending';

-- UTM/deep-link и продуктовая воронка без внешних сервисов.
CREATE TABLE IF NOT EXISTS client_bot_funnel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    chat_id TEXT DEFAULT '',
    event TEXT DEFAULT '',
    source TEXT DEFAULT '',
    order_id TEXT DEFAULT '',
    nom_id TEXT DEFAULT '',
    data TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_client_funnel_at ON client_bot_funnel(at, event, source);

-- Durable-идемпотентность рассылок: повтор запроса панели возвращает тот же
-- результат и не создаёт вторую пачку сообщений.
CREATE TABLE IF NOT EXISTS client_broadcasts (
    request_id TEXT PRIMARY KEY,
    text TEXT DEFAULT '',
    audience TEXT DEFAULT 'opt_in',
    response TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT
);

-- Универсальная идемпотентность публичных заявок.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    request_id TEXT PRIMARY KEY,
    kind TEXT DEFAULT '',
    entity_id TEXT DEFAULT '',
    response TEXT DEFAULT '{}',
    created_at TEXT
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
    tg_user_id TEXT DEFAULT '',
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
    auto_cost INTEGER DEFAULT 1,
    items_override INTEGER DEFAULT 0,
    client_variant_id TEXT DEFAULT '',
    client_source TEXT DEFAULT '',
    client_request_id TEXT DEFAULT '',
    client_track_token_hash TEXT DEFAULT '',
    client_track_token_at TEXT DEFAULT '',
    client_quote_status TEXT DEFAULT '',
    client_quote_version INTEGER DEFAULT 0,
    client_quote_sent_at TEXT DEFAULT '',
    client_quote_accepted_at TEXT DEFAULT '',
    client_ready_at TEXT DEFAULT '',
    client_delivered_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_number ON orders(number);

CREATE TABLE IF NOT EXISTS order_items (
    id TEXT PRIMARY KEY,
    order_id TEXT DEFAULT '',
    position INTEGER DEFAULT 0,
    nom_id TEXT DEFAULT '',      -- позиция номенклатуры (база товаров)
    name TEXT DEFAULT '',
    qty REAL DEFAULT 1,
    price REAL DEFAULT 0,        -- цена за штуку
    grams REAL DEFAULT 0,        -- норматив пластика на штуку (из базы)
    hours REAL DEFAULT 0,        -- норматив печати на штуку (из базы)
    variant_id TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);

CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY,
    key TEXT UNIQUE,                -- короткий ключ: PLA, PETG, MY_PETG (для формул и AMS)
    name TEXT DEFAULT '',           -- как показывать в списках
    full_name TEXT DEFAULT '',
    base TEXT DEFAULT '',           -- шаблон: ключ встроенного материала или ''
    builtin INTEGER DEFAULT 0,      -- 1 = встроенный тип из каталога (можно править под себя)
    density REAL DEFAULT 1.24,      -- г/см³
    speed_factor REAL DEFAULT 1.0,  -- во сколько раз медленнее PLA
    support_factor REAL DEFAULT 0.10,
    price_per_kg REAL DEFAULT 0,    -- стоимость кг (0 = из справочника-шаблона)
    temp_nozzle_min REAL DEFAULT 210,
    temp_nozzle_max REAL DEFAULT 240,
    temp_bed_min REAL DEFAULT 45,
    temp_bed_max REAL DEFAULT 65,
    chamber TEXT DEFAULT 'open',    -- open | closed | closed_hot
    fan INTEGER DEFAULT 100,        -- % обдува
    shrinkage REAL DEFAULT 0.25,    -- усадка, %
    dry_temp REAL DEFAULT 50,
    dry_hours REAL DEFAULT 5,
    heat_resistance REAL DEFAULT 58,
    uv_resistant INTEGER DEFAULT 0,
    food_safe INTEGER DEFAULT 0,
    abrasive INTEGER DEFAULT 0,
    strengths TEXT DEFAULT '',
    weaknesses TEXT DEFAULT '',
    use_cases TEXT DEFAULT '',
    note TEXT DEFAULT '',
    archived INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

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
    verified INTEGER DEFAULT 1,
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
    remote_task_id TEXT DEFAULT '',
    accounted_at TEXT DEFAULT '',
    reprint_of_job_id TEXT DEFAULT '',
    reprint_request_id TEXT DEFAULT '',
    defect_id TEXT DEFAULT '',
    resume_eligible INTEGER DEFAULT 1,
    manual_paused INTEGER DEFAULT 0,
    power_loss_at TEXT DEFAULT '',
    resume_attempts INTEGER DEFAULT 0,
    resume_reason TEXT DEFAULT '',
    file_version TEXT DEFAULT '',
    power_loss_state TEXT DEFAULT '',
    power_loss_progress REAL DEFAULT 0,
    power_loss_layer INTEGER DEFAULT 0,
    power_loss_total_layers INTEGER DEFAULT 0,
    power_loss_task TEXT DEFAULT '',
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
    tx_id TEXT,
    request_id TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
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
    created_at TEXT,
    nom_id TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
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
    photo TEXT DEFAULT '',       -- путь к кадру камеры
    note TEXT DEFAULT '',
    confirmed_at TEXT DEFAULT '',
    request_id TEXT DEFAULT '',
    loss_source TEXT DEFAULT '',
    reprint_requested INTEGER DEFAULT 0,
    reprint_job_id TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS customer_feedback (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    customer_id TEXT,
    order_number TEXT DEFAULT '',
    product TEXT DEFAULT '',
    customer_name TEXT DEFAULT '',
    request_message TEXT DEFAULT '',
    request_sent_at TEXT DEFAULT '',
    request_id TEXT DEFAULT '',
    rating INTEGER DEFAULT 0,
    feedback_text TEXT DEFAULT '',
    feedback_received_at TEXT DEFAULT '',
    response_request_id TEXT DEFAULT '',
    publish_permission TEXT DEFAULT 'not_asked',
    repeat_interest TEXT DEFAULT 'not_asked',
    repeat_order_id TEXT DEFAULT '',
    repeat_request_id TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_order
    ON customer_feedback(order_id) WHERE order_id IS NOT NULL AND order_id<>'';
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_request
    ON customer_feedback(request_id) WHERE request_id<>'';
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_response_request
    ON customer_feedback(response_request_id) WHERE response_request_id<>'';
CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_repeat_request
    ON customer_feedback(repeat_request_id) WHERE repeat_request_id<>'';

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
    color_name TEXT DEFAULT '',
    brand TEXT DEFAULT '',
    qty REAL DEFAULT 1,           -- сколько закупить (катушек/кг)
    unit TEXT DEFAULT 'кг',       -- кг | шт | катушка
    reason TEXT DEFAULT '',       -- авто-причина: «осталось N г» / «темп N г/дн»
    source TEXT DEFAULT 'manual', -- manual | auto
    done INTEGER DEFAULT 0,
    received_at TEXT DEFAULT '',
    receipt_request_id TEXT DEFAULT '',
    receipt_spool_ids TEXT DEFAULT '',
    receipt_amount REAL DEFAULT 0,
    receipt_tx_id TEXT DEFAULT '',
    received_qty INTEGER DEFAULT 0,
    received_spool_grams REAL DEFAULT 0,
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
    nom_id TEXT DEFAULT '',         -- canonical номенклатура PrintFlow / 1С
    qty REAL DEFAULT 0,             -- штук на стеллаже
    price REAL DEFAULT 0,           -- цена ценника, ₽
    cost_per_unit REAL DEFAULT 0,   -- себестоимость штуки, ₽
    min_qty REAL DEFAULT 0,         -- минимальный остаток для предупреждения
    photo TEXT DEFAULT '',          -- имя файла в DATA_DIR/photos/
    note TEXT DEFAULT '',
    barcode TEXT DEFAULT '',        -- штрихкод ровно как в номенклатуре 1С
    sku TEXT DEFAULT '',            -- артикул 1С
    tag_template TEXT DEFAULT 'standard', -- standard (67×32) | promo (67×57); legacy IDs normalize to standard
    tag_variant TEXT DEFAULT 'clean', -- clean | accent | sale | mono | photo (только визуал)
    tag_badge TEXT DEFAULT '',      -- своя плашка: «Хит», «Новинка», «−20%»
    tag_color TEXT DEFAULT '#4f46e5',
    tag_note TEXT DEFAULT '',       -- короткое описание только для ценника
    tag_old_price REAL DEFAULT 0,   -- старая цена для акционного промостенда
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
    source TEXT DEFAULT '',        -- printflow | 1c | другое внешнее ПО
    external_id TEXT DEFAULT '',   -- уникальный id строки внешнего чека
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_shelf_moves_item ON shelf_moves(item_id, at);

-- ------------------------------------------------- касса магазина (стеллаж)
-- Деньги от продаж со стеллажа физически лежат в кассе магазина. Каждая
-- строка — выемка «забрали из магазина», чтобы видеть, сколько накопилось
-- и сколько ещё лежит в магазине (сверка с инвентаризацией кассы).
CREATE TABLE IF NOT EXISTS shelf_collections (
    id TEXT PRIMARY KEY,
    at TEXT,
    amount REAL DEFAULT 0,
    note TEXT DEFAULT ''
);

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

-- 10.0: журнал симуляций и фактических срабатываний правил. Симуляция
-- никогда не выполняет действие и нужна для проверки правила на данных.
CREATE TABLE IF NOT EXISTS automation_rule_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT DEFAULT '',
    mode TEXT DEFAULT 'live',             -- dry_run | live
    event TEXT DEFAULT '',
    matched INTEGER DEFAULT 0,
    action TEXT DEFAULT '',
    preview TEXT DEFAULT '',
    at TEXT,
    actor TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rule_runs_rule ON automation_rule_runs(rule_id, id DESC);

-- ------------------------------------------- 8.5: wish-list клиентов (идея 72)
CREATE TABLE IF NOT EXISTS wishes (
    id TEXT PRIMARY KEY,
    customer_id TEXT,
    order_id TEXT DEFAULT '',
    text TEXT NOT NULL,
    status TEXT DEFAULT 'pending',   -- pending | done | declined
    created_at TEXT,
    resolved_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wishes_customer ON wishes(customer_id, status);

-- ------------------------------------------- 9.0: цех, приход пластика, AMS
CREATE TABLE IF NOT EXISTS workshop_docs (
    id TEXT PRIMARY KEY,
    number TEXT DEFAULT '',
    kind TEXT DEFAULT 'filament_receipt',
    at TEXT,
    state TEXT DEFAULT 'posted',
    title TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    total_amount REAL DEFAULT 0,
    grams REAL DEFAULT 0,
    supplier TEXT DEFAULT '',
    supplier_id TEXT DEFAULT '',
    shopping_id TEXT DEFAULT '',
    request_id TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workshop_docs_request
    ON workshop_docs(request_id) WHERE request_id<>'';
CREATE INDEX IF NOT EXISTS idx_workshop_docs_kind ON workshop_docs(kind, at);

CREATE TABLE IF NOT EXISTS ams_slot_history (
    id TEXT PRIMARY KEY,
    at TEXT,
    printer_id TEXT DEFAULT '',
    slot TEXT DEFAULT '',
    spool_id TEXT DEFAULT '',
    action TEXT DEFAULT '',
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ams_slot_hist ON ams_slot_history(printer_id, at);

CREATE TABLE IF NOT EXISTS filament_scrap (
    id TEXT PRIMARY KEY,
    at TEXT,
    spool_id TEXT,
    grams REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    note TEXT DEFAULT '',
    request_id TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_filament_scrap_request
    ON filament_scrap(request_id) WHERE request_id<>'';
CREATE INDEX IF NOT EXISTS idx_filament_scrap_spool ON filament_scrap(spool_id, at);

CREATE TABLE IF NOT EXISTS suppliers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT DEFAULT '',
    note TEXT DEFAULT '',
    price_per_kg REAL DEFAULT 0,
    archived INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS plate_presets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS shift_checks (
    id TEXT PRIMARY KEY,
    day TEXT,
    item_id TEXT,
    at TEXT,
    note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_shift_checks_day ON shift_checks(day, item_id);
"""


# ------------------------------------------------ целостность и аварийный запуск

class DatabaseRecoveryError(RuntimeError):
    """Базу нельзя безопасно открыть или восстановить автоматически."""


_CORRUPTION_MARKERS = (
    "database disk image is malformed",
    "malformed database",
    "file is not a database",
    "not a database",
    "database corruption",
    "unsupported file format",
    "file is encrypted",
)


def _is_corruption_message(message: object) -> bool:
    text = str(message or "").casefold()
    return any(marker in text for marker in _CORRUPTION_MARKERS)


def friendly_sqlite_error(error: object) -> str:
    """Не показывать пользователю внутренние англоязычные ошибки SQLite."""
    text = str(error or "").casefold()
    if _is_corruption_message(text):
        return ("База данных повреждена. Перезапустите PrintFlow — при запуске "
                "он проверит базу и восстановит последнюю исправную копию.")
    if "database is locked" in text or "database table is locked" in text:
        return "База данных занята другим процессом. Закройте второй экземпляр PrintFlow и повторите."
    if "readonly" in text or "read-only" in text:
        return "Нет прав на запись в базу данных. Проверьте доступ к папке данных PrintFlow."
    if "database or disk is full" in text or "disk full" in text:
        return "На диске закончилось место. Освободите место и перезапустите PrintFlow."
    if "unable to open database file" in text:
        return "Не удалось открыть базу данных. Проверьте права и доступность папки PrintFlow."
    if "disk i/o error" in text:
        return "Ошибка чтения или записи диска. Проверьте накопитель и папку данных PrintFlow."
    return "Ошибка базы данных. Перезапустите PrintFlow и выполните: python pf.py doctor"


def database_integrity(path: str | Path, *, ignore_sidecars: bool = False,
                       thorough: bool = True) -> dict[str, object]:
    """Проверить SQLite только для чтения и классифицировать результат.

    ``ignore_sidecars`` включает immutable-режим: он нужен, чтобы отличить
    повреждение основного файла от битого/чужого WAL-журнала. Функция никогда
    не создаёт отсутствующий файл и не меняет проверяемую базу.
    """
    target = Path(path)
    if not target.is_file():
        return {"ok": False, "kind": "missing", "error": "файл базы не найден"}
    try:
        if target.stat().st_size == 0:
            return {"ok": False, "kind": "corrupt", "error": "файл базы пуст"}
    except OSError as exc:
        return {"ok": False, "kind": "error", "error": str(exc)}

    uri = target.resolve().as_uri() + "?mode=ro"
    if ignore_sidecars:
        uri += "&immutable=1"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3)
        conn.execute("PRAGMA query_only=ON")
        pragma = "PRAGMA integrity_check(20)" if thorough else "PRAGMA quick_check(20)"
        rows = conn.execute(pragma).fetchall()
        messages = [str(row[0]) for row in rows if row and str(row[0]).casefold() != "ok"]
        if not messages:
            return {"ok": True, "kind": "ok", "error": ""}
        return {"ok": False, "kind": "corrupt", "error": "; ".join(messages)[:2000]}
    except sqlite3.Error as exc:
        return {"ok": False,
                "kind": "corrupt" if _is_corruption_message(exc) else "error",
                "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "kind": "error", "error": str(exc)}
    finally:
        if conn is not None:
            conn.close()


def _sidecar_paths(path: str | Path) -> list[Path]:
    target = Path(path)
    return [Path(str(target) + suffix) for suffix in ("-wal", "-shm", "-journal")]


def _clear_sidecars(path: str | Path) -> None:
    """Удалить журналы старой базы перед установкой другого основного файла."""
    for sidecar in _sidecar_paths(path):
        try:
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            raise DatabaseRecoveryError(
                f"Не удалось убрать старый журнал базы {sidecar.name}: {exc}") from exc


def _temporary_sibling(target: Path, label: str) -> Path:
    token = f"{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    return target.with_name(f".{target.name}.{label}-{token}.tmp")


def _unique_backup_path(target: Path) -> Path:
    """Не перезаписывать две копии, созданные в одну и ту же секунду."""
    if not target.exists():
        return target
    for index in range(2, 10_000):
        candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError("не удалось подобрать свободное имя резервной копии")


def _backup_connection(source: sqlite3.Connection, target: str | Path) -> Path:
    """Атомарно сделать и проверить копию открытого SQLite-соединения."""
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_sibling(destination, "backup")
    dest: sqlite3.Connection | None = None
    try:
        dest = sqlite3.connect(str(temporary))
        source.backup(dest)
        dest.close()
        dest = None
        check = database_integrity(temporary, ignore_sidecars=True, thorough=True)
        if not check["ok"]:
            raise sqlite3.DatabaseError(
                "Созданная резервная копия не прошла проверку целостности")
        os.replace(temporary, destination)
        return destination
    finally:
        if dest is not None:
            dest.close()
        temporary.unlink(missing_ok=True)
        _clear_sidecars(temporary)


def backup_database_file(source: str | Path, target: str | Path) -> Path:
    """Консистентно скопировать закрытую или работающую SQLite-базу."""
    source_path = Path(source)
    uri = source_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return _backup_connection(conn, target)
    finally:
        conn.close()


def install_database_copy(source: str | Path, target: str | Path) -> None:
    """Проверить копию и атомарно установить её без старых WAL/SHM-файлов."""
    source_path, target_path = Path(source), Path(target)
    check = database_integrity(source_path, ignore_sidecars=True, thorough=True)
    if not check["ok"]:
        detail = friendly_sqlite_error(check.get("error")) if check["kind"] == "corrupt" \
            else "Копия не читается: проверьте диск и права доступа."
        raise ValueError(f"Выбранная копия повреждена. {detail}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_sibling(target_path, "restore")
    try:
        shutil.copy2(source_path, temporary)
        copied = database_integrity(temporary, ignore_sidecars=True, thorough=True)
        if not copied["ok"]:
            raise ValueError("Не удалось получить целый файл из выбранной копии")
        # Старый WAL относится к прежнему основному файлу. Если оставить его,
        # SQLite может воспроизвести чужие страницы поверх исправной копии.
        _clear_sidecars(target_path)
        os.replace(temporary, target_path)
        _clear_sidecars(target_path)
    finally:
        temporary.unlink(missing_ok=True)
        _clear_sidecars(temporary)


def preserve_damaged_database(path: str | Path = None,
                               backup_dir: str | Path = None) -> Path:
    """Сохранить повреждённую базу и все журналы отдельно, не выдавая их за бэкап."""
    source = Path(path if path is not None else DB_FILE)
    root = Path(backup_dir if backup_dir is not None else BACKUP_DIR) / "damaged"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder = root / f"{stamp}-{time.time_ns() % 1_000_000_000:09d}"
    folder.mkdir(parents=True, exist_ok=False)
    copied_main: Path | None = None
    try:
        for item in [source, *_sidecar_paths(source)]:
            if not item.is_file():
                continue
            copied = folder / item.name
            shutil.copy2(item, copied)
            if item == source:
                copied_main = copied
        if copied_main is None:
            raise OSError("основной файл базы не найден")
        return copied_main
    except Exception:
        # Неполную папку оставляем для диагностики, но исходник не трогаем.
        raise


def recover_database_if_needed(path: str | Path = None,
                               backup_dir: str | Path = None) -> dict | None:
    """На старте изолировать повреждение и вернуть PrintFlow в рабочее состояние.

    Приоритет восстановления: целый основной файл без сломанного WAL, затем
    самая свежая исправная резервная копия, затем новая пустая база. Исходные
    повреждённые файлы всегда предварительно сохраняются в ``backups/damaged``.
    """
    target = Path(path if path is not None else DB_FILE)
    backups = Path(backup_dir if backup_dir is not None else BACKUP_DIR)
    if not target.is_file():
        return None

    current = database_integrity(target, thorough=True)
    if current["ok"]:
        return None
    main_only = database_integrity(target, ignore_sidecars=True, thorough=True)

    definite_corruption = current["kind"] == "corrupt" or main_only["kind"] == "corrupt"
    if not definite_corruption:
        # Успешная immutable-проверка ещё не доказывает, что WAL повреждён:
        # обычной проверке мог помешать второй процесс, блокировка или I/O-сбой.
        # В таком случае ничего не удаляем и не подменяем автоматически.
        raise DatabaseRecoveryError(
            "Не удалось безопасно проверить базу данных. "
            + friendly_sqlite_error(current.get("error") or main_only.get("error")))

    try:
        quarantined = preserve_damaged_database(target, backups)
    except Exception as exc:
        raise DatabaseRecoveryError(
            "База повреждена, но сохранить её аварийную копию не удалось. "
            "Исходный файл не изменён. Проверьте свободное место и права на папку данных."
        ) from exc

    common = {
        "quarantine": str(quarantined),
        "detected": str(current.get("error") or "нарушена целостность"),
    }
    if main_only["ok"]:
        _clear_sidecars(target)
        recheck = database_integrity(target, thorough=True)
        if recheck["ok"]:
            return {
                **common,
                "action": "wal",
                "message": ("Повреждённый журнал SQLite изолирован; основной файл базы "
                            "исправен, данные из него сохранены."),
            }

    candidates: list[Path] = []
    if backups.exists():
        try:
            candidates = sorted(
                backups.glob("*.sqlite3"),
                key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)
        except OSError:
            candidates = []
    skipped: list[str] = []
    for candidate in candidates:
        check = database_integrity(candidate, ignore_sidecars=True, thorough=True)
        if not check["ok"]:
            skipped.append(candidate.name)
            continue
        try:
            install_database_copy(candidate, target)
        except (OSError, ValueError, DatabaseRecoveryError):
            skipped.append(candidate.name)
            continue
        return {
            **common,
            "action": "backup",
            "backup": candidate.name,
            "skipped": skipped,
            "message": f"База автоматически восстановлена из исправной копии {candidate.name}.",
        }

    # Ни одной исправной копии нет. Приложение всё равно запускается, но старый
    # файл не уничтожается: его байт-в-байт снимок уже лежит в damaged.
    try:
        target.unlink(missing_ok=True)
        _clear_sidecars(target)
    except OSError as exc:
        raise DatabaseRecoveryError(
            "Повреждённая база сохранена, но подготовить новую базу не удалось. "
            "Проверьте права на папку данных PrintFlow."
        ) from exc
    return {
        **common,
        "action": "new",
        "skipped": skipped,
        "message": ("Повреждённая база изолирована. Исправных резервных копий нет, "
                    "поэтому создана новая база; исходные файлы сохранены для восстановления."),
    }


class Database:
    """Потокобезопасная обёртка над SQLite."""

    def __init__(self, path=None):
        ensure_dirs()
        path = DB_FILE if path is None else path
        self.path = path
        self.lock = threading.RLock()
        self._local = threading.local()
        # Автовосстановление разрешено только для основной пользовательской
        # базы. Временные/тестовые/страховочные файлы никогда не подменяем.
        self.recovery: dict | None = None
        if str(path) != ":memory:":
            try:
                is_primary = Path(path).resolve() == Path(DB_FILE).resolve()
            except (OSError, TypeError, ValueError):
                is_primary = False
            if is_primary:
                self.recovery = recover_database_if_needed(path)
        # Шину подключает сервер (api.py). Пока её нет — события просто
        # пишутся в базу, поэтому база остаётся самостоятельной в тестах.
        self.bus = None
        # Кэш settings(): экономика заказов и карточки товаров читают все
        # настройки по несколько раз на строку списка. Сбрасывается при любой
        # записи в таблицу settings (см. execute/executemany).
        self._settings_cache: dict | None = None
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            # 12.0: очередь ожидания вместо мгновенного SQLITE_BUSY при
            # конкурентной записи (панель + боты + бэкап), и обычный для WAL
            # режим синхронизации — записи не заставляют ждать fsync.
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            # SQLite lower() умеет только ASCII — регистронезависимый поиск по
            # кириллице делаем средствами Python.
            self.conn.create_function(
                "pylower", 1, lambda v: v.lower() if isinstance(v, str) else v)
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
            try:
                from .library import FileLibrary
                FileLibrary(self).ensure_schema()
            except Exception:
                pass
        except Exception:
            self.conn.close()
            raise

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
            # Старые завершённые задания уже были учтены кодом прежней версии.
            # Запоминаем сам факт добавления маркера: выполнять backfill на каждом
            # старте нельзя — пустое поле у нового задания означает незавершённую
            # транзакцию, которую менеджер должен восстановить.
            add_accounted_marker = any(
                name == "accounted_at"
                for name, _decl in pending.get("print_jobs", [])
            )
            add_shopping_receipt_marker = any(
                name == "received_at"
                for name, _decl in pending.get("shopping_items", [])
            )
            for table, missing in pending.items():
                for name, decl in missing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            if add_accounted_marker:
                self.conn.execute(
                    "UPDATE print_jobs SET accounted_at=COALESCE(finished_at,created_at,'migrated')"
                    " WHERE state IN ('done','failed','cancelled')"
                    " AND COALESCE(accounted_at,'')=''"
                )
            if add_shopping_receipt_marker:
                # Старые галочки означали «куплено» без складского прихода.
                # Оставляем архив закрытым, но явно помечаем его как legacy,
                # чтобы новый API не мог случайно создать по нему катушки.
                self.conn.execute(
                    "UPDATE shopping_items SET received_at='legacy'"
                    " WHERE done=1 AND COALESCE(received_at,'')=''"
                )
            # Индексы создаём после догоняющего ALTER: у старой базы ключевых
            # колонок ещё не было во время первоначального executescript.
            # Новые индексы создаём только после ALTER TABLE: старые базы
            # приходят сюда без колонок 9.4, а executescript(SCHEMA) уже завершён.
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_client_chats_tg_user"
                " ON client_chats(tg_user_id)"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_client_orders_update"
                " ON client_orders(chat_id, update_id) WHERE update_id<>''"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_client_log_chat"
                " ON client_bot_log(chat_id, id DESC)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_client_log_inbox"
                " ON client_bot_log(unread, direction, id DESC)"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_client_order_request"
                " ON orders(client_request_id) WHERE client_request_id<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_client_payment_request"
                " ON client_payment_intents(request_id) WHERE request_id<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_client_payment_pending"
                " ON client_payment_intents(order_id, chat_id) WHERE status='pending'"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_idempotency_request"
                " ON idempotency_keys(request_id)"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_client_bot_update"
                " ON client_bot_updates(update_id)"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_client_outbox_dedupe"
                " ON client_bot_outbox(dedupe_key) WHERE dedupe_key<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_client_wishlist"
                " ON client_wishlist(chat_id, nom_id)"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_pay_request"
                " ON payments(request_id) WHERE request_id<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_shopping_receipt_request"
                " ON shopping_items(receipt_request_id) WHERE receipt_request_id<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_defect_request"
                " ON defects(request_id) WHERE request_id<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_defect_confirmed_job"
                " ON defects(job_id) WHERE job_id<>'' AND confirmed_at<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_reprint_request"
                " ON print_jobs(reprint_request_id) WHERE reprint_request_id<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_reprint_source"
                " ON print_jobs(reprint_of_job_id) WHERE reprint_of_job_id<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_shelf_move_external"
                " ON shelf_moves(source, external_id)"
                " WHERE source<>'' AND external_id<>''"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_shelf_item_barcode"
                " ON shelf_items(barcode) WHERE barcode<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_start_request"
                " ON print_jobs(start_request_id) WHERE start_request_id<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_workshop_docs_request"
                " ON workshop_docs(request_id) WHERE request_id<>''"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_filament_scrap_request"
                " ON filament_scrap(request_id) WHERE request_id<>''"
            )
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
            # Догоняющие статусы (например, «На складе»): в готовую базу не
            # встраиваются через DEFAULT_STATUSES, поэтому добавляем точечно и
            # идемпотентно — существующую колонку пользователя не трогаем.
            for row in EXTRA_STATUSES:
                cur.execute(
                    "INSERT OR IGNORE INTO statuses(id,name,color,position,is_final)"
                    " VALUES(?,?,?,?,?)", row)
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

    @staticmethod
    def _writes_settings(sql: str) -> bool:
        """Пишет ли запрос в таблицу settings (для сброса кэша settings())."""
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        return head in ("INSERT", "UPDATE", "DELETE", "REPLACE") and "settings" in sql

    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            if self._writes_settings(sql):
                self._settings_cache = None
            if not getattr(self._local, "transaction_depth", 0):
                self.conn.commit()
            return cur

    def executemany(self, sql: str, seq: Iterable[Iterable]) -> None:
        with self.lock:
            self.conn.executemany(sql, [tuple(x) for x in seq])
            if self._writes_settings(sql):
                self._settings_cache = None
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
        cached = self._settings_cache
        if cached is None:
            cached = dict(DEFAULT_SETTINGS)
            for row in self.query("SELECT key,value FROM settings"):
                try:
                    cached[row["key"]] = json.loads(row["value"])
                except json.JSONDecodeError:
                    cached[row["key"]] = row["value"]
            self._settings_cache = cached
        data = dict(cached)  # копия: вызывающий код не должен править кэш
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
        """Консистентная, проверенная и атомарная копия базы под нагрузкой."""
        with self.lock:
            _backup_connection(self.conn, target)

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


def make_backup(prefix: str = "printflow-manual", keep: object | None = None) -> dict:
    """Консистентная проверенная копия базы с общей ротацией снимков."""
    ensure_dirs()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = _unique_backup_path(BACKUP_DIR / f"{prefix}-{stamp}.sqlite3")
    if not DB_FILE.exists():
        return {"ok": False, "error": "База ещё не создана"}
    uri = Path(DB_FILE).resolve().as_uri() + "?mode=ro"
    source: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(uri, uri=True, timeout=5)
        _backup_connection(source, target)
        if keep is None:
            try:
                row = source.execute(
                    "SELECT value FROM settings WHERE key='backup_keep'").fetchone()
                keep = json.loads(row[0]) if row else DEFAULT_SETTINGS["backup_keep"]
            except (sqlite3.Error, json.JSONDecodeError, TypeError):
                keep = DEFAULT_SETTINGS["backup_keep"]
    except (sqlite3.Error, OSError, DatabaseRecoveryError) as exc:
        target.unlink(missing_ok=True)
        return {"ok": False, "error": friendly_sqlite_error(exc)}
    finally:
        if source is not None:
            source.close()
    rotate_backups(BACKUP_DIR, keep)
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
    check = database_integrity(source, ignore_sidecars=True, thorough=True)
    if not check["ok"]:
        raise ValueError("Выбранная копия повреждена; откат отменён")

    safety = ""
    if DB_FILE.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safety_file = _unique_backup_path(
            BACKUP_DIR / f"before-restore-{stamp}.sqlite3")
        try:
            backup_database_file(DB_FILE, safety_file)
            safety = safety_file.name
        except (sqlite3.Error, OSError, DatabaseRecoveryError):
            # Даже повреждённую текущую базу нельзя молча потерять. Сохраняем
            # её байт-в-байт вместе с WAL/SHM в отдельный карантин.
            try:
                quarantined = preserve_damaged_database(DB_FILE, BACKUP_DIR)
            except Exception as exc:
                raise DatabaseRecoveryError(
                    "Текущая база не читается, а сохранить её аварийную копию не удалось. "
                    "Откат отменён; исходный файл не изменён."
                ) from exc
            safety = str(quarantined)
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
    """Выполнить отложенное восстановление базы из проверенной копии.

    Вызывается при старте, до открытия `Database`. Возвращает итог или None,
    если восстанавливать нечего. Маркер удаляется в любом случае.
    """
    marker_exists = RESTORE_REQUEST.exists()
    request = pending_restore()
    if request is None and not marker_exists:
        return None
    result: dict = {"restored": ""}
    try:
        if request is None:
            result["error"] = "маркер восстановления повреждён"
            return result
        filename = str(request.get("file") or "")
        if filename != Path(filename).name or not filename.endswith(".sqlite3"):
            result["error"] = "недопустимое имя копии в маркере"
            return result
        source = BACKUP_DIR / filename
        if not source.is_file():
            result["error"] = f"копия {filename} не найдена"
            return result
        try:
            install_database_copy(source, DB_FILE)
            result["restored"] = filename
        except (OSError, ValueError, DatabaseRecoveryError) as exc:
            result["error"] = str(exc)
        return result
    finally:
        try:
            RESTORE_REQUEST.unlink()
        except OSError:
            pass
