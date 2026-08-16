"""Пути, каталог данных и значения по умолчанию.

Секреты (Access Code принтеров, Telegram-токен) хранятся только в каталоге
данных пользователя и никогда не попадают в репозиторий, HTML или бэкап
браузера.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"

if os.name == "nt":
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "PrintFlow"
else:
    DATA_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "printflow"

DB_FILE = DATA_DIR / "printflow.sqlite3"
UPLOAD_DIR = DATA_DIR / "uploads"
LOG_FILE = DATA_DIR / "connector.log"

# Тарифы и производственные константы. Пользователь меняет их в интерфейсе,
# значения ниже — только стартовые ориентиры, а не обещание рынка.
DEFAULT_SETTINGS: dict[str, object] = {
    "company_name": "NOZZA",
    "currency": "₽",
    # Энергия и амортизация
    "power_kw": 0.15,             # средняя потребляемая мощность P1S, кВт
    "energy_price": 6.0,          # ₽ за кВт·ч
    "amortization_per_hour": 12.0,  # ₽ износа принтера за час печати
    "maintenance_per_hour": 3.0,  # ₽ обслуживания за час печати
    "labor_rate": 400.0,          # ₽ за час ручной работы
    "packaging_cost": 15.0,       # ₽ упаковки на заказ
    "default_spool_price": 1600.0,
    "default_spool_weight": 1000.0,
    "target_profit_per_hour": 250.0,
    "weekly_capacity_hours": 110.0,
    "failure_rate": 5.0,          # % брака, закладывается в себестоимость
    "tax_rate": 6.0,              # % налога с оборота
    # Автоматизация
    "auto_accounting": True,      # писать себестоимость и расход по факту печати
    "auto_link_orders": True,     # связывать печать с заказом по имени файла
    "auto_consume_filament": True,
    "auto_income_on_done": True,  # доход в кассу при переводе заказа в финальный статус
    "auto_queue": False,          # автозапуск следующего задания очереди
    # Уведомления
    "telegram_enabled": False,
    "telegram_token": "",
    "telegram_chat_id": "",
    "notify_complete": True,
    "notify_error": True,
    "notify_pause": True,
    "notify_filament_low": True,
    "filament_low_threshold": 15.0,  # % остатка катушки
    # Интерфейс
    "theme": "system",
    "accent": "indigo",
}

SECRET_SETTINGS = {"telegram_token"}

DEFAULT_STATUSES = [
    ("new", "Новая заявка", "#64748b", 0, 0),
    ("estimate", "Расчёт", "#8b5cf6", 1, 0),
    ("prepay", "Ждём предоплату", "#f59e0b", 2, 0),
    ("queue", "Очередь", "#0ea5e9", 3, 0),
    ("printing", "Печать", "#2563eb", 4, 0),
    ("post", "Постобработка", "#7c3aed", 5, 0),
    ("ready", "Готов", "#10b981", 6, 0),
    ("done", "Выдан", "#166534", 7, 1),
]

DEFAULT_NICHES = [
    ("pets", "Товары для питомцев", "🐾", "#ec4899",
     "Полезные и персонализированные аксессуары для владельцев питомцев",
     "Проверить адресники, держатели и организацию зоны питомца"),
    ("home", "Функциональные товары для дома", "🏠", "#0ea5e9",
     "Органайзеры, крепления и держатели точно под пространство клиента",
     "Найти 3 повторяемых решения с высокой прибылью за час"),
    ("business", "Товары для локального бизнеса", "🏪", "#8b5cf6",
     "Быстрая оснастка, таблички и органайзеры по размерам бизнеса",
     "Получить 5 постоянных B2B-клиентов"),
]


def now_iso() -> str:
    """Локальное время с таймзоной, секундная точность."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
