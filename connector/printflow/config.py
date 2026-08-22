"""Пути, каталог данных и значения по умолчанию.

Секреты (Access Code принтеров, Telegram-токен) хранятся только в каталоге
данных пользователя и никогда не попадают в репозиторий, HTML или бэкап
браузера.
"""
from __future__ import annotations

import os
import socket
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

if getattr(sys, "frozen", False):
    # Собранный PyInstaller-бинарь: ресурсы (папка site) распакованы во
    # временный каталог _MEIPASS, а не лежат рядом с исходниками.
    ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"

if os.name == "nt":
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "PrintFlow"
else:
    DATA_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "printflow"

DB_FILE = DATA_DIR / "printflow.sqlite3"
BACKUP_DIR = DATA_DIR / "backups"
DEFAULT_BACKUP_KEEP = 20
RESTORE_REQUEST = DATA_DIR / "restore.request"  # маркер отложенного восстановления
UPLOAD_DIR = DATA_DIR / "uploads"
PHOTO_DIR = DATA_DIR / "photos"
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
    "count_labor_in_cost": False,  # своя работа — не расход, только ориентир
    "packaging_cost": 15.0,       # ₽ упаковки на заказ
    "default_spool_price": 1600.0,
    "default_spool_weight": 1000.0,
    "target_profit_per_hour": 250.0,
    "weekly_capacity_hours": 110.0,
    "failure_rate": 5.0,          # % брака, закладывается в себестоимость
    "tax_rate": 6.0,              # % налога с оборота (ручной запасной вариант)
    # --- Форма деятельности и налоги -----------------------------------
    # tax_mode: none | npd | usn6 | usn15 | patent | manual
    "tax_mode": "none",
    "legal_name": "",             # ИП Иванов И. И. / ООО «Нозза»
    "inn": "",
    "npd_rate_person": 4.0,       # НПД с оплат от физлиц
    "npd_rate_company": 6.0,      # НПД с оплат от юрлиц и ИП
    "npd_limit": 2400000.0,       # годовой лимит НПД
    "npd_bonus_left": 10000.0,    # остаток стартового вычета 10 000 ₽
    "usn_income_rate": 6.0,       # УСН «Доходы»
    "usn_profit_rate": 15.0,      # УСН «Доходы минус расходы»
    "usn_min_tax_rate": 1.0,      # минимальный налог на УСН 15
    "usn_limit": 490500000.0,     # предел применения УСН
    "patent_cost_year": 0.0,      # стоимость патента за год
    "insurance_fixed": 57390.0,   # фиксированные взносы ИП за себя, 2026
    "insurance_extra_rate": 1.0,  # % с дохода свыше порога
    "insurance_extra_base": 300000.0,
    "insurance_extra_cap": 321818.0,
    "insurance_reduces_tax": True,  # уменьшать налог УСН на взносы
    "vat_enabled": False,         # НДС на УСН после порога
    "vat_rate": 5.0,
    "vat_threshold": 20000000.0,
    "tax_reserve_enabled": True,  # откладывать налог с каждого дохода
    "tax_reserve_extra": 0.0,     # доп. % сверх ставки «на всякий случай»
    # --- Ценообразование ------------------------------------------------
    "default_markup": 150.0,      # наценка к себестоимости, %
    "min_order_price": 300.0,     # минимальный чек
    "price_rounding": 10.0,       # округление цены вверх, ₽
    "rush_surcharge": 30.0,       # надбавка за срочность, %
    "design_rate": 800.0,         # моделирование, ₽/ч
    "bulk_discount_10": 5.0,      # скидка от 10 шт, %
    "bulk_discount_50": 10.0,     # скидка от 50 шт, %
    "acquiring_fee": 2.5,         # эквайринг по умолчанию, %
    "delivery_cost": 0.0,         # средняя доставка на заказ, ₽
    # --- Учёт денег -----------------------------------------------------
    "default_account": "cash",    # касса по умолчанию
    "allocate_fixed_costs": False,  # разносить постоянные расходы на час печати
    "fixed_costs_auto": True,     # начислять постоянные расходы по расписанию
    "debt_alert_days": 14,        # через сколько дней долг считается просроченным
    "debt_reminder_cooldown_days": 3,  # не напоминать одному заказу слишком часто
    "low_margin_alert": 20.0,      # % маржи, ниже которого заказ подсвечивается
    "goal_profit_month": 60000.0,  # цель по прибыли за месяц
    # Автоматизация
    "auto_accounting": True,      # писать себестоимость и расход по факту печати
    "auto_link_orders": True,     # связывать печать с заказом по имени файла
    "auto_consume_filament": True,
    # Устаревший режим для серверного импорта. Обычная выдача всегда требует
    # явного выбора «оплата получена» / «в долг», поэтому по умолчанию выключен.
    "auto_income_on_done": False,
    "auto_queue": False,          # автозапуск следующего задания очереди
    # Единый safety-gate: без явного включения в настройках расписания,
    # автозапуск очереди и обычная unattended-автоматизация не могут выполнять
    # физические действия. Power-loss recovery — отдельная строгая политика;
    # старые флаги сохраняются для совместимости, но не обходят safety-gate.
    "unattended_dangerous_actions": False,
    "auto_resume_paused": False,  # отдельный gate: только подтверждённый power-loss recovery
    "auto_resume_max_delay_minutes": 1440,  # не восстанавливать старую печать позже суток
    # --- Сторож печати ---------------------------------------------------
    "guard_enabled": True,          # реагировать на ошибки принтера
    "guard_pause_on_error": True,   # ставить печать на паузу при серьёзной ошибке
    "guard_pause_severity": "error",  # с какого уровня вмешиваться: warn|error|fatal
    "guard_stall_minutes": 20.0,    # прогресс не растёт столько минут — тревога
    "guard_cold_minutes": 10.0,     # сопло холодное при статусе «печать» — тревога
    "guard_count_loss": True,       # записывать стоимость брака в расходы
    "guard_snapshot": True,         # сохранять кадр камеры в момент тревоги
    "guard_cost_limit": 0.0,        # стоп/пауза, если живая себестоимость выше ₽ (0=выкл)
    "guard_overrun_pct": 15.0,      # перерасход пластика против сметы, % — тревога
    # --- Спагетти-детект по камере ----------------------------------------
    "spaghetti_enabled": False,     # следить за «мешаниной» в кадре (нужен pillow)
    "spaghetti_sensitivity": 3.0,   # во сколько раз кромки должны превысить базу
    # --- 5.0: конверты, ночная смена, окупаемость, авто-эжектор ----------
    "envelope_auto": False,         # автоматически откладывать % с дохода в конверты
    "printer_investment": 0.0,      # во что обошёлся принтер (для окупаемости)
    "printer_invested_at": "",      # дата ввода в эксплуатацию
    "night_shift_enabled": True,    # планировать длинное на ночь, срочное днём
    "ejector_enabled": False,       # авто-эжектор (DIY): режим снятия деталей
    "month_close": {},              # журнал шагов мастера «Закрыть месяц» по месяцам
    "bank_rules": [],               # правила импорта банковской выписки (M1)
    "auto_backup_days": 1,          # автобэкап раз в N дней (0 = выключен)
    # --- Очередь и планирование -----------------------------------------
    "queue_check_filament": True,   # не запускать, если пластика не хватит
    "queue_check_material": True,   # не запускать, если в AMS не тот материал
    "queue_group_material": True,   # подряд печатать задания одного материала
    "quiet_hours_enabled": False,   # не запускать печать ночью
    "quiet_from": "23:00",
    "quiet_to": "08:00",
    # --- Наработка и обслуживание ----------------------------------------
    "maintenance_enabled": True,    # напоминать о ТО по часам печати
    "telemetry_enabled": True,      # писать историю температур и скорости
    "telemetry_keep_days": 14,      # сколько дней хранить телеметрию
    # Уведомления
    "telegram_enabled": False,
    "telegram_bot": True,
    "telegram_token": "",
    "telegram_chat_id": "",
    "telegram_quiet_from": "23:00",  # тихие часы бота: не шлём некритичное
    "telegram_quiet_to": "07:00",
    # Bambu Cloud: управление принтером без LAN Only Mode / Developer Mode.
    # Токен и uid — секреты (маскируются, как telegram_token); email хранится
    # для повторного входа, пароль не хранится — при истечении токена Bambu
    # присылает код на почту, и вход повторяется кодом.
    "cloud_email": "",
    "cloud_region": "global",     # global | china
    "cloud_token": "",
    "cloud_uid": "",
    "cloud_history_sync": True,   # дополнять журнал из облачной истории печатей
    "cloud_sync_minutes": 5.0,    # как часто сверяться с облачной историей

    "notify_complete": True,
    "notify_error": True,
    "notify_pause": True,
    "notify_filament_low": True,
    "notify_guard": True,         # тревоги сторожа печати
    "notify_firmware": True,      # сообщать об обновлении прошивки принтера
    "notify_maintenance": True,   # напоминания об обслуживании
    "notify_photo": True,         # прикладывать кадр камеры к сообщению
    "notify_finish_remind_min": 10.0,  # напомнить о финише за N минут (0 = выкл)
    "filament_low_threshold": 15.0,   # % остатка катушки, ниже — тревога
    "shopping_runout_days": 7.0,    # «материал кончится через N дней» → в закупку
    "dry_humidity_threshold": 55.0,  # влажность AMS, выше которой пора сушить пластик
    "restock_remind": True,       # напоминать о закупке пластика
    "qc_checklist": ["Замерил размеры", "Сфотографировал изделие",
                     "Проверил качество слоёв", "Упаковал"],  # чек-лист качества
    "digest_time": "09:00",       # время утреннего дайджеста в Telegram
    "reply_templates": [],        # шаблоны ответов клиентам [{id,title,text}]
    "feedback_delay_days": 2,     # через сколько дней после выдачи просить отзыв
    "weekly_report_day": 1,       # день недели еженедельного отчёта (1 = понедельник)
    "weekly_report_time": "20:00",
    # --- 8.5: Фаза 11 ------------------------------------------------------
    # Виртуальный принтер (идея 7): симуляция P1S для тестов и демо.
    "demo_printer_enabled": False,
    "demo_speed": 1.0,            # минут печати за одну реальную секунду
    # Ночной сброс цеха (идея 85): итоги дня событием в базу.
    "night_reset_enabled": True,
    "night_reset_time": "23:00",
    # Видео печати: кадры-кейфреймы во время задания (идея 61, 87).
    "keyframe_interval_min": 0.0,  # 0 = выключено; иначе кадр раз в N минут
    # Деталь на столе (идея 10): сравнение кадра с эталоном пустого стола.
    "bed_watch_enabled": False,
    "bed_watch_threshold": 6.0,   # % пиксельного различия, выше — тревога
    # Первый слой (идея 60): сколько минут после старта следить за кадром.
    "first_layer_watch_min": 5.0,
    # --- Обновления -------------------------------------------------------
    "update_check_enabled": True,   # спрашивать GitHub о новых версиях
    "auto_update_enabled": False,   # ставить обновления самостоятельно
    "update_check_hours": 6.0,      # как часто проверять, часов
    "update_branch": "main",        # ветка, за которой следим
    "update_seen_sha": "",          # о какой версии уже сообщили
    "installed_sha": "",            # что установлено (для режима без git)
    "last_update_at": "",           # когда обновлялись в последний раз
    # Интерфейс
    "theme": "system",
    "accent": "indigo",
    # --- 8.0: Мост Bambu Studio ------------------------------------------
    "watch_folder_enabled": False,
    "watch_folder_path": str(Path.home() / "PrintFlow-Inbox"),
    "watch_auto_action": "notify",  # notify | queue | print
    "watch_link_order": True,
    "watch_create_order": False,
    # --- 8.0: 3MF парсер --------------------------------------------------
    "slicer_auto_create_order": False,
    "slicer_filename_template": "{product}_№{number}_{material}",
    # --- 8.0: Preflight ---------------------------------------------------
    "preflight_enabled": True,
    "preflight_block_idle": True,
    "preflight_block_hms": True,
    "preflight_block_material": True,
    "preflight_block_filament": True,
    "preflight_warn_sd": True,
    "preflight_warn_nozzle": True,
    "preflight_warn_humidity": True,
    "preflight_warn_calibration": True,
    # --- 8.0: FTPS --------------------------------------------------------
    "ftps_timeout": 12,
    "ftps_retries": 3,
    "ftps_block_kb": 256,
    "ftps_queue": True,
    "ftps_dedup": True,
    # --- 8.0: MQTT --------------------------------------------------------
    "mqtt_keepalive": 30,
    "mqtt_backoff": True,
    "mqtt_fallback_1883": True,
    # --- 8.0: Очередь и камера --------------------------------------------
    "queue_smart_group": True,
    "queue_smart_deadline": True,
    "queue_offline_defer": True,
    "camera_timelapse_interval": 2.5,
    "camera_keep_shots": 60,
    "camera_roi_center": 60,
    # --- 8.0: AMS ---------------------------------------------------------
    "ams_auto_map": True,
    "ams_delta_e_threshold": 30,
    # --- 8.2: автосбор с принтера и AMS в базу -----------------------------
    "printer_info_sync": True,    # прошивка, Wi-Fi, влажность → карточка принтера
    "ams_auto_spools": True,      # заводить катушки из AMS на складе автоматически
    "ams_sync_remaining": True,   # обновлять остаток катушки по датчику AMS
    # --- 8.0: Безопасность и система --------------------------------------
    "encrypt_access_code": True,
    "settings_profiles": [],  # снапшоты [{id, name, at, data}]
    "ui_density": "normal",  # compact | normal
    "ui_start_view": "dashboard",
    "debug_verbose": False,
    # База для QR-наклеек (катушка, ценник). Пусто — берём LAN IP компьютера.
    # Пример: http://192.168.1.50:8080 или Tailscale http://pc.tailnet.ts.net:8080
    "public_url": "",
    # --- 8.0: Бэкап 2.0 ---------------------------------------------------
    "backup_keep": DEFAULT_BACKUP_KEEP,
    "backup_auto_export": False,
}

SECRET_SETTINGS = {"telegram_token", "cloud_token", "cloud_uid"}

# Кассы и счета: где физически лежат деньги.
# (id, название, тип, комиссия при поступлении %, стартовый остаток)
DEFAULT_ACCOUNTS = [
    ("cash", "Наличные", "cash", 0.0, 0.0),
    ("card", "Карта", "card", 0.0, 0.0),
    ("bank", "Расчётный счёт", "bank", 0.0, 0.0),
]

# Каналы продаж со своей комиссией и стоимостью привлечения.
# (id, название, комиссия %, фикс. сбор ₽, реклама ₽/заказ, плательщик: person|company)
# Команды, которые нельзя выполнять в фоне без отдельного safety-gate.
DANGEROUS_AUTOMATION_COMMANDS = frozenset({
    "pause", "resume", "stop", "nozzle_temp", "bed_temp", "load_filament", "unload_filament",
    "extrude", "ams_filament", "home", "move", "bed_level", "calibration",
    "print_gcode", "project_file", "start", "start_job",
})

DEFAULT_CHANNELS = [
    ("direct", "Напрямую / сарафан", 0.0, 0.0, 0.0, "person"),
    ("shop", "Витрина NOZZA", 0.0, 0.0, 0.0, "person"),
    ("telegram", "Telegram", 0.0, 0.0, 0.0, "person"),
    ("avito", "Авито", 5.0, 0.0, 0.0, "person"),
    ("ozon", "Ozon / маркетплейс", 15.0, 0.0, 0.0, "company"),
    ("b2b", "B2B по счёту", 0.0, 0.0, 0.0, "company"),
]

# Статьи расходов: группировка отчёта и признак постоянных затрат.
# (id, название, группа, постоянный)
DEFAULT_EXPENSE_CATEGORIES = [
    ("filament", "Пластик", "variable", 0),
    ("energy", "Электричество", "variable", 0),
    ("packaging", "Упаковка", "variable", 0),
    ("delivery", "Доставка", "variable", 0),
    ("fee", "Комиссии площадок", "variable", 0),
    ("equipment", "Оборудование и запчасти", "invest", 0),
    ("rent", "Аренда", "fixed", 1),
    ("subscription", "Подписки и сервисы", "fixed", 1),
    ("ads", "Реклама и продвижение", "fixed", 0),
    ("tax", "Налоги", "tax", 0),
    ("insurance", "Страховые взносы", "tax", 0),
    ("withdrawal", "Вывод себе", "owner", 0),
    ("other", "Прочее", "variable", 0),
]

# Регламент обслуживания: (id, задача, период в часах печати, подсказка)
DEFAULT_MAINTENANCE = [
    ("clean_bed", "Протереть стол спиртом", 24.0,
     "Жир от пальцев — причина №1 отклеивания детали."),
    ("check_nozzle", "Осмотреть сопло на налипания", 50.0,
     "Комок пластика на сопле портит верхние слои."),
    ("lube_rods", "Смазать направляющие и винт Z", 150.0,
     "Сухие валы дают полосы на стенках и пропуск шагов."),
    ("clean_fans", "Продуть вентиляторы и фильтр", 200.0,
     "Забитый обдув хотэнда ведёт к засорам."),
    ("belts", "Проверить натяжение ремней", 300.0,
     "Слабый ремень — смещение слоёв и овальные отверстия."),
    ("nozzle_wear", "Заменить сопло", 600.0,
     "Латунь стачивается, особенно после абразивных материалов."),
]

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
    """Локальное время с таймзоной.

    Микросекунды нужны не только для журнала, но и как optimistic-locking
    маркер: два сохранения в одну секунду обязаны получать разные версии.
    """
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")


def backup_keep(value: object = DEFAULT_BACKUP_KEEP) -> int:
    """Безопасный лимит ротации копий из настройки пользователя."""
    try:
        return max(1, min(200, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_BACKUP_KEEP


def rotate_backups(directory: Path = BACKUP_DIR,
                   keep: object = DEFAULT_BACKUP_KEEP) -> list[Path]:
    """Оставить последние ``keep`` SQLite-копий независимо от их источника.

    Ручные копии, автобэкапы, снимки перед обновлением/миграцией и страховки
    отката лежат в одной папке. Единая ротация не даёт разным механизмам
    спорить между лимитами 10/14/20 и бесконтрольно расходовать диск.
    """
    if not directory.exists():
        return []
    items: list[tuple[int, str, Path]] = []
    for path in directory.glob("*.sqlite3"):
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        items.append((mtime, path.name, path))
    items.sort(reverse=True)
    removed: list[Path] = []
    for _, _, path in items[backup_keep(keep):]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            continue
    return removed


def get_local_ips() -> list[str]:
    """IPv4-адреса этого ПК в локальной сети (для доступа с телефона/планшета).

    При нескольких роутерах/подсетях полезно видеть все адреса: телефон может
    сидеть в другой Wi-Fi сети, чем та, которую вы подумали первой.
    """
    ips: set[str] = set()
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if ip and not ip.startswith("127.") and "." in ip:
                ips.add(ip)
    except Exception:
        pass
    for target in [("8.8.8.8", 80), ("1.1.1.1", 80), ("192.168.1.1", 80)]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.8)
            s.connect(target)
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127.") and "." in ip:
                ips.add(ip)
        except Exception:
            pass

    def sort_key(ip: str):
        if ip.startswith("192.168."):
            return (0, ip)
        if ip.startswith("10."):
            return (1, ip)
        if ip.startswith("172."):
            try:
                second = int(ip.split(".")[1])
                if 16 <= second <= 31:
                    return (2, ip)
            except (IndexError, ValueError):
                pass
        if ip.startswith("100."):  # CGNAT, в том числе Tailscale
            try:
                second = int(ip.split(".")[1])
                if 64 <= second <= 127:
                    return (3, ip)
            except (IndexError, ValueError):
                pass
        return (99, ip)

    # APIPA 169.254/16 появляется, когда DHCP не выдал адрес. Такой QR почти
    # всегда бесполезен для телефона и не должен выглядеть как исправная LAN.
    return sorted((ip for ip in ips if sort_key(ip)[0] < 99), key=sort_key)


_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


def host_name(host: str) -> str:
    """Имя хоста без порта: ``192.168.1.50:8080`` → ``192.168.1.50``."""
    host = (host or "").strip()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end].lower() if end > 0 else host.lower()
    if host.count(":") == 1:
        return host.split(":", 1)[0].lower()
    return host.lower()


def host_port(host: str, default: int = 8080) -> int:
    """Порт из ``Host``-заголовка. Без порта — ``default``."""
    host = (host or "").strip()
    if host.startswith("["):
        rest = host[host.find("]") + 1:]
        if rest.startswith(":") and rest[1:].isdigit():
            return int(rest[1:])
        return default
    if host.count(":") == 1:
        tail = host.split(":", 1)[1]
        if tail.isdigit():
            return int(tail)
    return default


def is_loopback_host(host: str) -> bool:
    """True, если адрес указывает на этот же компьютер, а не на LAN."""
    name = host_name(host)
    return name in _LOOPBACK_NAMES or name.startswith("127.")


def normalize_base(url_or_host: str) -> str:
    """``http://host:port`` без хвостового слэша. Пустая строка, если не разобрать."""
    raw = (url_or_host or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        return ""
    netloc = parsed.netloc or parsed.path.split("/", 1)[0]
    if not netloc:
        return ""
    return f"{parsed.scheme}://{netloc}"


def public_base(host_header: str = "", public_url: str = "",
                lan_ips: list[str] | None = None, listen_port: int = 8080) -> dict:
    """Базовый URL для QR, который откроется с телефона в той же сети.

    Приоритет:
      1. Настройка ``public_url`` (свой IP, Tailscale, имя ПК).
      2. ``Host`` запроса, если это не localhost.
      3. Первый LAN-IP компьютера + порт панели.
      4. localhost — только запасной вариант, ``reachable=False``.
    """
    ips = list(lan_ips) if lan_ips is not None else get_local_ips()
    override = normalize_base(public_url)
    if override:
        parsed = urllib.parse.urlparse(override)
        return {
            "base": override,
            "host": parsed.netloc,
            "reachable": not is_loopback_host(parsed.netloc),
            "source": "setting",
            "ips": ips,
        }
    port = host_port(host_header, listen_port or 8080)
    if host_header and not is_loopback_host(host_header):
        base = normalize_base(host_header)
        return {
            "base": base,
            "host": urllib.parse.urlparse(base).netloc,
            "reachable": True,
            "source": "request",
            "ips": ips,
        }
    if ips:
        host = f"{ips[0]}:{port}"
        return {
            "base": f"http://{host}",
            "host": host,
            "reachable": True,
            "source": "lan",
            "ips": ips,
        }
    fallback = (host_header or f"127.0.0.1:{port}").replace("http://", "").replace("https://", "")
    return {
        "base": f"http://{fallback}",
        "host": fallback,
        "reachable": False,
        "source": "loopback",
        "ips": ips,
    }


def public_page_url(path: str, query: str = "", host_header: str = "",
                    public_url: str = "", lan_ips: list[str] | None = None,
                    listen_port: int = 8080) -> dict:
    """Полный URL страницы для QR-наклейки + служебные поля ``public_base``."""
    info = public_base(host_header, public_url, lan_ips, listen_port)
    path = path if str(path).startswith("/") else "/" + str(path)
    url = info["base"] + path
    if query:
        url += ("&" if "?" in url else "?") + query
    return {**info, "url": url}


def tcp_reachable(host: str, port: int, timeout: float = 2.0) -> tuple[bool, float]:
    """Проверить, что TCP-порт принтера отвечает. Возвращает (ok, время_мс).

    Для MQTT/камеры/FTPS достаточно TCP-рукопожатия: если пакет доходит,
    порт слушается; TLS-обмен при диагностике не нужен. Помогает разобраться,
    в одной ли сети компьютер и принтер (частая беда при нескольких роутерах).
    """
    if not host:
        return False, 0.0
    started = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, round((time.time() - started) * 1000, 1)
    except Exception:
        return False, round((time.time() - started) * 1000, 1)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
