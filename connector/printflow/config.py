"""Пути, каталог данных и значения по умолчанию.

Секреты (Access Code принтеров, Telegram-токен) хранятся только в каталоге
данных пользователя и никогда не попадают в репозиторий, HTML или бэкап
браузера.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import DEFAULT_PORT

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
    # Куда по умолчанию класть новые катушки: shop | home | dry | other.
    "default_location": "shop",
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
    # Контроль НПД (17.0.5): порог тревоги по годовому лимиту и ежедневное
    # напоминание про чеки. Чек по расчёту обязан быть выдан в тот же день,
    # поэтому «день с деньгами и без чеков» — это риск штрафа, а не мелочь.
    "npd_limit_warn_pct": 90.0,   # с какого % лимита считать его «близко»
    "npd_alerts_enabled": True,   # напоминать про чеки и лимит в Telegram
    "npd_alert_time": "21:00",     # с какого часа дня можно напомнить
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
    # --- Оплата картой через банковский терминал (18.0, в работе) -------
    # Куда пишем выручку по терминалу. Карта — не ящик: деньги лежат на счёте
    # карты (эквайринг), поэтому сверка наличных их не ждёт.
    # Настройка уже читается кодом, но сама оплата картой выключена до тех пор,
    # пока страница кассы не покажет кнопку: см. PARKED_METHODS в cashier.py.
    "cashier_card_account_id": "card",
    # --- СБП (система быстрых платежей), Касса 16.0 --------------------
    "sbp_enabled": True,          # принимать оплату по СБП
    "sbp_account_id": "sbp",      # счёт, на который пишем подтверждённый СБП-платёж
    "sbp_shop_qr": "",            # статический QR магазина (строка/ссылка из банка)
    "sbp_bank_name": "",          # подпись банка для клиента («СБП — Т-Банк»)
    "sbp_payment_note": "",       # шаблон назначения перевода; {number} — номер заказа
    "sbp_phone": "",              # телефон для перевода по СБП (показываем под QR)
    "sbp_recipient": "",          # как получателя видит покупатель (пусто — pay_payee_name/legal_name)
    "sbp_purpose_limit": 140,     # макс. длина назначения платежа из товаров (18.0)
    "sbp_hold_hours": 24,         # холд СБП-продажи: часов до автоснятия (И2 единый регистр)
    # Авто-подтверждение СБП по поступлениям из банка (раунд «авто-СБП»):
    # строгое правило — точная сумма + ожидающий платёж в окне времени.
    # По умолчанию ВЫКЛЮЧЕНО (решение заказчика 2026-09-10): совпадение одной
    # суммы без идентификатора платежа подтверждало чужой заказ живым смоуком
    # («OZON выплата 1500,00» закрыла долг клиента на 1500). Касса получает
    # очередь «на сверку» + привязку в один клик; авто включают те, кто готов
    # платить за это риском ложного «оплачено».
    "sbp_auto_confirm": False,    # подтверждать самим при точном совпадении суммы и времени
    "sbp_match_window_hours": 24, # окно сопоставления поступления с платёжом, часов
    # --- Платёжный QR: PrintFlow генерирует код оплаты сам ----------------
    # Динамический QR СБП выпускает только банк-эквайер, а вот QR по
    # реквизитам (ГОСТ Р 56042-2014) собирается локально: сумма и назначение
    # уже внутри, читает любое банковское приложение.
    "pay_qr_mode": "auto",        # auto|gost|link|static|off
    "pay_qr_link": "",            # шаблон ссылки банка: {amount}, {amount_kop}, {number}
    # Камера телефона понимает ссылку и не понимает ГОСТ-текст. По умолчанию
    # выключено: наш собственный ГОСТ-код уже содержит сумму, а ссылка банка —
    # нет (покупатель вводит сумму сам). Включают, когда «открыть банк тапом»
    # важнее, чем сумма «вшитая» в код.
    "pay_qr_camera": False,       # в auto приоритет: ссылка банка → QR магазина → ГОСТ
    "pay_payee_name": "",         # получатель платежа (пусто — берём legal_name)
    "pay_payee_inn": "",          # ИНН получателя (пусто — берём inn)
    "pay_payee_kpp": "",          # КПП получателя (для ООО)
    "pay_account": "",            # расчётный счёт получателя, 20 цифр
    "pay_bank_name": "",          # наименование банка получателя
    "pay_bic": "",                # БИК банка, 9 цифр
    "pay_corr_account": "",       # корреспондентский счёт банка, 20 цифр
    "cashier_code": "",           # общий код входа кассира в мобильную кассу (LAN)
    # Режим смен на кассе (Касса 16.0, решение №6 «без смен» + И3/И4 в коде):
    # auto — смены не видно: открытая смена появляется сама на первой наличной
    #        продаже, а «сверка» делается одним пересчётом ящика; отмена продажи
    #        и выемка при этом продолжают работать (они привязаны к смене);
    # manual — кассир сам открывает и закрывает смену (как было в И3/И4).
    "cashier_shift_mode": "auto",
    # Витрина кассы (17.0.26): True — кассир видит только товары со стеллажа
    # (что физически лежит на полке). False — прежний единый каталог И2:
    # товар со склада виден кассе и доезжает на полку при продаже.
    "cashier_shelf_only": True,
    # «Звонок о платеже» (17.0.4) — вибрация, писк и баннер на кассе. По
    # умолчанию включён: потерянный платёж дороже лишнего звука. На прилавке с
    # одним человеком и тихим залом его можно выключить — тогда останутся
    # только счётчик на вкладке «Входящие» и баннер (визуально не мешает).
    "cashier_ring": True,
    # Что делать с продажей из офлайн-очереди, если товара уже нет на полке:
    # True  — провести и пометить «минус-остаток» (деньги настоящие, расход
    #         виден в журнале, событии и аудите — расхождение разбирают
    #         инвентаризацией);
    # False — вернуть ошибку и оставить строку в очереди до решения старшего.
    # Выбран первый вариант: обрыв в магазине обычно означает «люди в очереди»,
    # и блокировать продажу из-за того, что витрина разошлась со складом, —
    # значит портить кассиру смену ради аккуратности учёта.
    "cashier_offline_negative": True,
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
    "shelf_low_last": "",            # дата последнего напоминания о низком остатке
    # Отметки «уже послано» для расписаний бота: без ключа в DEFAULT_SETTINGS
    # set_settings молча выбрасывает запись — и дайджест дублировался бы весь
    # час отправки каждый цикл опроса (баг 17.0.27, найден тестом вечернего графика).
    "digest_last": "",               # дата последнего утреннего дайджеста
    "weekly_last": "",               # неделя последнего недельного отчёта
    "evening_chart_last": "",        # дата последнего вечернего графика дня
    # Клиентский бот (9.3): отдельный токен, покупатели заказывают и следят
    # за заказами в своём чате; внутренней панели недоступен.
    "client_bot_enabled": False,
    "client_bot_token": "",           # секрет: токен бота для покупателей
    "client_bot_welcome": "",         # свой текст /start (пусто — стандартный)
    "client_bot_templates": [
        {"id": "tpl_quote", "name": "Расчёт готов",
         "text": "Здравствуйте! Расчёт готов: {цена}. Срок — {срок}. Напишите, подходит ли вам цена и срок."},
        {"id": "tpl_status", "name": "Статус заказа",
         "text": "Здравствуйте! Заказ {номер} сейчас на этапе «{статус}». Если срок изменится, сообщим отдельно."},
    ],
    "client_bot_notify": True,        # сообщать об изменении статуса заказа
    "client_bot_catalog": True,       # показывать витрину с ценами
    # 9.3.2: сервис клиентского бота
    "client_bot_faq": "",             # свой текст «Вопрос-ответ» (пусто — стандартный)
    "client_bot_review": True,        # спрашивать отзыв через 2 дня после выдачи
    "client_bot_pickup_days": 3,      # напомнить о невыкупленном «готовом» заказе
    "client_bot_pickup_info": "",     # адрес и часы для кнопки «Как получить»
    "client_bot_ready_photo": True,   # снимать готовую деталь в сообщение «Готов»
    "client_bot_faq_materials": "",   # своя статья «Как выбрать материал» (пусто — стандартная)
    "client_bot_pay_info": "",        # публичные реквизиты оплаты для кнопки «Оплатить»
    "client_bot_pay_qr": "",          # URL/ссылка на QR СБП, без обязательного эквайринга
    "client_bot_payment_purpose": "", # назначение перевода, если отличается от номера заказа
    "client_bot_quiet_hours_enabled": False,
    "client_bot_quiet_from": "22:00", # тихие часы клиентских уведомлений
    "client_bot_quiet_to": "08:00",
    "client_bot_track_url": "",       # базовый URL панели для кнопки «Статус онлайн»
    "shop_season": "none",            # сезон витрины (В67): none|newyear|spring|autumn
    "client_bot_marketing_enabled": False,
    # последний подтверждённый Telegram update_id + 1; техническое состояние,
    # не показывается клиентам и не содержит секретов
    "client_bot_update_offset": 0,
    "telegram_bot_update_offset": 0,
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
    # Web Notifications: пока панель открыта вкладкой, события приходят сразу.
    # Туманное имя ключа осталось из интерфейса — раньше его не было в схеме,
    # и настройка молча отбрасывалась при сохранении.
    "browser_notify_enabled": True,
    "filament_low_threshold": 15.0,   # % остатка катушки, ниже — тревога
    "shopping_runout_days": 7.0,    # «материал кончится через N дней» → в закупку
    "dry_humidity_threshold": 55.0,  # влажность AMS, выше которой пора сушить пластик
    "restock_remind": True,       # напоминать о закупке пластика
    "qc_checklist": ["Замерил размеры", "Сфотографировал изделие",
                     "Проверил качество слоёв", "Упаковал"],  # чек-лист качества
    "digest_time": "09:00",       # время утреннего дайджеста в Telegram
    "evening_chart_time": "20:00",  # вечерний отчёт-картинка дня в Telegram
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
    "camera_fps_max": 0.0,         # предел кадров камеры, 0 = без ограничения (поломка 15.2: weak-key TLS)
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
    "theme": "dark",
    "accent": "indigo",
    # --- 8.0: Мост Bambu Studio ------------------------------------------
    # --- 17.0.19: состояние каналов связи с принтером ---------------------
    # Через сколько секунд тишины канал считается «давно не было вестей».
    # Значения совпадают с CHANNELS в connection_state.py — расхождение ловит
    # test_connection_state.
    "link_stale_mqtt": 60,
    "link_stale_http": 300,
    "link_stale_ftps": 900,
    "link_stale_camera": 900,
    "watch_folder_enabled": False,
    "watch_folder_path": str(Path.home() / "PrintFlow-Inbox"),
    "watch_auto_action": "notify",  # notify | queue | print
    "watch_link_order": True,
    "watch_create_order": False,
    # --- 11.0: шлюз Bambu Studio + библиотека + CLI-слайсер ---------------
    "studio_gateway_enabled": False,
    "studio_gateway_name": "NOZZA-PrintFlow",
    "studio_gateway_host": "",  # адрес для SSDP/PASV; пусто — авто по get_local_ips
    "studio_gateway_mode": "confirm",  # confirm | queue | autostart
    "studio_gateway_autostart": False,
    "studio_gateway_serial": "",
    "studio_gateway_access_code": "",
    "studio_gateway_printer_id": "",
    # Ретранслятор SSDP (18.11): PrintFlow объявляет Studio реальные станки
    # фермы (serial + IP из вкладки «Принтеры») — для Studio на компьютере в
    # другой подсети/VLAN, куда широковещательный SSDP станка не долетает.
    # Адресаты — IPv4 компьютеров со Studio через запятую; они же получают
    # unicast-объявление самого шлюза на UDP :2021.
    "studio_relay_enabled": False,
    "studio_relay_targets": "",
    # --- 18.13: локальный помощник ----------------------------------------
    # Внешний рантайм на этом же компьютере (Ollama, llama.cpp server):
    # коннектор ходит в него через urllib, поэтому в requirements.txt ничего
    # не появляется. Модель не закреплена намеренно — веса ставит владелец, а
    # «из коробки» помощник выключен и ничего не обещает (см. assistant.py).
    "assistant_enabled": False,
    "assistant_url": "http://127.0.0.1:11434",
    "assistant_model": "",
    "assistant_timeout_sec": 90.0,
    # Рантайм речи (срез 2): тоже внешний, тоже loopback, но на процессоре —
    # видеопамять уже делят текстовая модель и WebGPU-нарезка.
    "assistant_speech_enabled": False,
    "assistant_speech_url": "http://127.0.0.1:8791",
    "assistant_speech_model": "",
    "assistant_speech_timeout_sec": 30.0,
    # Агент ОС (срез 3): третья внешняя программа — стоп-слово, захват окна и
    # действия в чужих приложениях. PrintFlow хранит только адрес и статус.
    "assistant_agent_enabled": False,
    "assistant_agent_url": "http://127.0.0.1:8799",
    "slicer_bin": "",
    "slicer_profile_path": "",
    # --- 8.0: 3MF парсер --------------------------------------------------
    "slicer_auto_create_order": False,
    "slicer_filename_template": "{product}_№{number}_{material}",
    # --- Свой слайсер PrintFlow (Stage 1) и FarmLoop -----------------------
    "slicer_provider": "external",       # external | printflow
    "slicer_profile": "bambu-p1s-printflow-stage1",  # профиль нарезки
    "slicer_layer_height": 0.2,
    "slicer_first_layer_height": 0.2,
    "slicer_walls": 3,
    "slicer_infill_percent": 15,
    "slicer_infill_pattern": "grid",
    "slicer_supports": False,
    "slicer_brim": False,
    "slicer_brim_width": 5.0,
    "slicer_nozzle_mm": 0.4,
    "slicer_nozzle_temp": 220,
    "slicer_bed_temp": 60,
    "slicer_speed_mm_s": 100,
    "slicer_material": "PLA",
    "slicer_ams_slot": "",
    "slicer_auto_postprocess_farmloop": False,
    "slicer_watch_auto_slice": False,
    "slicer_watch_auto_queue": False,
    # --- Свой слайсер PrintFlow Stage 1: параметры и гейты -----------------
    "slicer_mode": "manual",             # manual | auto
    "slicer_engine_available": True,
    "slicer_first_print_verified": False,
    "slicer_auto_enqueue": False,
    "slicer_auto_print": False,
    "slicer_max_cycles": 1,
    "slicer_extrusion_width_mm": 0.0,    # 0 = авто: nozzle × 1.125
    "slicer_top_solid_layers": 4,
    "slicer_bottom_solid_layers": 4,
    "slicer_seam": "nearest",            # nearest | aligned
    "slicer_retract_mm": 0.8,
    "slicer_retract_speed_mm_s": 35,
    "slicer_retract_min_travel_mm": 1.5,
    "slicer_zhop_mm": 0.0,
    "slicer_fan_percent": 0,             # 0 = авто по материалу
    "slicer_support_spacing_mm": 2.0,
    "slicer_travel_speed_mm_s": 200,
    "slicer_flow": 1.0,
    "slicer_center_model": True,
    "farmloop_profile": "bambu-p1s-farmloop-stage1",
    "farmloop_mechanics_verified": False,
    "farmloop_template_verified": False,
    "farmloop_sensor_mode": "manual",   # manual | sensor | camera | both
    "farmloop_sensor_timeout_s": 30,
    "farmloop_camera_threshold_pct": 6.0,
    "farmloop_cooldown_s": 120,
    "farmloop_pusher_enabled": False,
    "farmloop_bender_enabled": False,
    "farmloop_auto_next": False,
    "farmloop_unattended_series": False,
    "farmloop_max_cycles": 1,
    "farmloop_max_detach_attempts": 1,
    # --- 8.0: Preflight ---------------------------------------------------
    "preflight_enabled": True,
    "preflight_block_idle": True,
    "preflight_block_hms": True,
    "preflight_block_material": True,
    "preflight_block_filament": True,
    "preflight_block_bed": True,  # Я40: не стартовать, если кадр ≠ пустой стол
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
    "ams_push_settings": True,    # приводить настройки слота к складу, когда принтер не печатает
    # --- 8.0: Безопасность и система --------------------------------------
    "encrypt_access_code": True,
    "settings_profiles": [],  # снапшоты [{id, name, at, data}]
    "ui_density": "normal",  # compact | normal
    "ui_start_view": "dashboard",
    "debug_verbose": False,
    # База для QR-наклеек (катушка, ценник). Пусто — берём LAN IP компьютера.
    # Пример: http://192.168.1.50:8765 или Tailscale http://pc.tailnet.ts.net:8765
    "public_url": "",
    # 18.12.2: адрес Mini App цеха для кнопки в боте сотрудников. Здесь нужен
    # именно HTTPS: Telegram открывает Mini App только по защищённому адресу,
    # LAN-адрес вида http://192.168.1.50:8765 кнопку ломает (BUTTON_URL_INVALID).
    # Пусто — берём public_url + /staff (если он https), иначе кнопки нет вовсе.
    "staff_miniapp_url": "",
    # --- 8.0: Бэкап 2.0 ---------------------------------------------------
    "backup_keep": DEFAULT_BACKUP_KEEP,
    "backup_auto_export": False,
}

SECRET_SETTINGS = {"telegram_token", "cloud_token", "cloud_uid", "client_bot_token",
                   "studio_gateway_access_code"}

# Кассы и счета: где физически лежат деньги.
# (id, название, тип, комиссия при поступлении %, стартовый остаток)
DEFAULT_ACCOUNTS = [
    ("cash", "Наличные", "cash", 0.0, 0.0),
    ("card", "Карта", "card", 0.0, 0.0),
    ("bank", "Расчётный счёт", "bank", 0.0, 0.0),
    # Касса 16.0: отдельный счёт СБП (система быстрых платежей). Настраиваемый
    # через sbp_account_id — владелец может перенаправить СБП на другой счёт.
    ("sbp", "СБП", "bank", 0.0, 0.0),
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

# Дополнительные статусы, добавляемые идемпотентно для существующих баз.
# «На складе» — финальный, но НЕ «Выдан»: товар уложен в учётный регистр
# склада, а не передан клиенту. Его нельзя ставить перетаскиванием в канбане.
EXTRA_STATUSES = [
    ("stocked", "На складе", "#0d9488", 8, 1),
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


# ---------------------------------------------------------------------------
# Сетевые интерфейсы: LAN против VPN
#
# Зачем это нужно. Раньше адреса машины угадывались через «подключиться к
# 8.8.8.8 и посмотреть, какой источник выбрало ядро». С поднятым VPN
# (WireGuard, OpenVPN, Tailscale, Amnezia, Pangolin) ядро выбирает туннель:
# у PrintFlow «появлялся» адрес 10.0.0.1/30, он уходил в SSDP-объявление и в
# PASV, Bambu Studio пыталась достучаться до 10.0.0.1 — и отдавала «код=-1»,
# хотя руками порт 3000 пробивался. Обратная сторона той же ошибки: настоящий
# LAN-адрес (192.168.0.108/24) не находился вовсе, потому что перечислением
# интерфейсов никто не занимался.
#
# Поэтому адреса здесь ПЕРЕЧИСЛЯЮТСЯ вместе с именем интерфейса и длиной
# префикса, а затем фильтруются. Отбрасываются:
#
#   * loopback (127/8) и 0.0.0.0;
#   * APIPA 169.254/16 — DHCP не выдал адрес, LAN нерабочая;
#   * multicast и broadcast (224/4 и выше);
#   * туннельные интерфейсы: tun*, tap*, utun*, wg*, WireGuard, OpenVPN,
#     Tailscale, ZeroTier, PPP/L2TP/PPTP, а также виртуальные мосты
#     Docker/Hyper-V/VirtualBox/VMware/WSL — SSDP-объявление в них уходит
#     в никуда, а Studio получает адрес, по которому принтера нет;
#   * любой префикс /30, /31, /32 — это точка-точка (туннель или аплинк
#     провайдера), а не сегмент LAN: широковещательного адреса там нет,
#     значит и SSDP там работать не может. Именно так выглядит 10.0.0.1/30.
# ---------------------------------------------------------------------------

# Признаки туннельных и виртуальных интерфейсов (регистр не важен).
VPN_IFACE_TOKENS = (
    "tun", "tap", "utun", "wg", "wireguard", "openvpn", "ovpn", "tailscale",
    "zerotier", "nordlynx", "mullvad", "protonvpn", "amnezia", "outline",
    "ppp", "l2tp", "pptp", "sstp", "ipsec", "vpn",
    "docker", "veth", "br-", "virbr", "vboxnet", "vmnet", "venet",
    "vethernet", "hyper-v", "virtualbox", "vmware", "wsl", "loopback",
)
# /30 и уже — точка-точка: ни широковещательного адреса, ни SSDP.
POINT_TO_POINT_PREFIX = 30
# Диапазон CGNAT 100.64/10 (Tailscale и подобные): держим в самом конце
# списка — для владельца «издалека» такой адрес лучше, чем ничего.
CGNAT_PRIORITY = 3
_INTERFACE_CACHE: dict[str, object] = {"at": 0.0, "items": []}
_INTERFACE_CACHE_TTL = 30.0


def _ipv4_parts(ip: str) -> tuple[int, int, int, int] | None:
    """Четыре октета IPv4 или None, если значение адресом не является."""
    parts = str(ip or "").strip().split(".")
    if len(parts) != 4:
        return None
    values: list[int] = []
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255:
            return None
        values.append(int(part))
    return (values[0], values[1], values[2], values[3])


def _is_ipv4(ip: str) -> bool:
    return _ipv4_parts(ip) is not None


def is_vpn_interface(name: str) -> bool:
    """True, если имя/описание интерфейса похоже на туннель или виртуальный мост."""
    text = str(name or "").strip().lower()
    if not text:
        return False
    return any(token in text for token in VPN_IFACE_TOKENS)


def _netmask_to_prefix(mask: str) -> int:
    """``0xffffff00`` / ``255.255.255.0`` → 24. Ноль, если не разобрать."""
    text = str(mask or "").strip()
    try:
        if text.lower().startswith("0x"):
            return bin(int(text, 16)).count("1")
        parts = _ipv4_parts(text)
        if parts:
            value = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
            return bin(value).count("1")
    except ValueError:
        pass
    return 0


def _iface_index_name(index: int) -> str:
    try:
        return socket.if_indextoname(int(index))
    except (OSError, ValueError, AttributeError):
        return ""


def _netlink_addresses() -> list[dict]:
    """IPv4-адреса Linux через netlink (без внешних команд и зависимостей)."""
    RTM_GETADDR, RTM_NEWADDR = 22, 20
    NLM_F_REQUEST, NLM_F_ROOT, NLM_F_MATCH = 1, 0x100, 0x200
    NLMSG_ERROR, NLMSG_DONE = 2, 3
    IFA_ADDRESS, IFA_LOCAL, IFA_LABEL = 1, 2, 3
    sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, socket.NETLINK_ROUTE)
    items: list[dict] = []
    try:
        sock.bind((0, 0))
        sock.settimeout(2.0)
        body = struct.pack("=BHBBi", socket.AF_INET, 0, 0, 0, 0)
        header = struct.pack("=IHHII", 16 + len(body), RTM_GETADDR,
                             NLM_F_REQUEST | NLM_F_ROOT | NLM_F_MATCH, 1, 0)
        sock.send(header + body)
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            offset, finished = 0, False
            while offset + 16 <= len(chunk):
                length, mtype = struct.unpack_from("=IH", chunk, offset)[:2]
                if length < 16 or offset + length > len(chunk):
                    break
                if mtype in (NLMSG_DONE, NLMSG_ERROR):
                    finished = True
                    break
                if mtype == RTM_NEWADDR:
                    payload = chunk[offset + 16:offset + length]
                    family, prefix = struct.unpack_from("=BB", payload, 0)
                    index = struct.unpack_from("=i", payload, 4)[0]
                    attrs: dict[int, bytes] = {}
                    pos = 8
                    while pos + 4 <= len(payload):
                        rta_len, rta_type = struct.unpack_from("=HH", payload, pos)
                        if rta_len < 4:
                            break
                        attrs[rta_type] = payload[pos + 4:pos + rta_len]
                        pos += (rta_len + 3) & ~3
                    raw = attrs.get(IFA_LOCAL) or attrs.get(IFA_ADDRESS) or b""
                    if family == socket.AF_INET and len(raw) >= 4:
                        label = attrs.get(IFA_LABEL, b"").split(b"\x00")[0]
                        items.append({
                            "name": label.decode("utf-8", "replace") or _iface_index_name(index),
                            "ip": socket.inet_ntoa(raw[:4]),
                            "prefix": int(prefix),
                            "index": int(index),
                        })
                offset += (length + 3) & ~3
            if finished:
                break
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return items


def _linux_interfaces() -> list[dict]:
    """Адреса Linux: netlink, запасной вариант — команда ``ip``."""
    try:
        items = _netlink_addresses()
        if items:
            return items
    except (OSError, struct.error, AttributeError):
        pass
    binary = "ip"
    try:
        proc = subprocess.run([binary, "-o", "-4", "addr", "show"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    items = []
    for line in (proc.stdout or "").splitlines():
        fields = line.replace("\\", " ").split()
        if "inet" not in fields:
            continue
        name = fields[1] if len(fields) > 1 else ""
        spot = fields.index("inet")
        value = fields[spot + 1] if spot + 1 < len(fields) else ""
        ip, _slash, prefix = value.partition("/")
        items.append({"name": name, "ip": ip,
                      "prefix": int(prefix) if prefix.isdigit() else 0})
    return items


def _windows_interfaces() -> list[dict]:
    """Адреса Windows: Get-NetIPAddress + Get-NetAdapter одним вызовом.

    Имя адаптера и его описание нужны, чтобы отличить «WireGuard Tunnel» от
    «Realtek PCIe GbE»: адрес 10.x может быть и настоящей LAN, и туннелем.
    Вывод переводим в UTF-8 — на русской Windows консоль иначе отдаёт cp866,
    и JSON с кириллическими именами адаптеров не разбирается.
    """
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "$a=Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress,"
        "PrefixLength,InterfaceAlias,InterfaceIndex;"
        "$n=Get-NetAdapter | Select-Object ifIndex,Name,InterfaceDescription,Status;"
        "@{ip=$a;ad=$n} | ConvertTo-Json -Compress -Depth 4"
    )
    powershell = "powershell.exe" if os.name == "nt" else "powershell"
    raw = b""
    for command in (
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
    ):
        try:
            proc = subprocess.run(command, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and (proc.stdout or b"").strip():
            raw = proc.stdout
            break
    if not raw:
        return []
    text = ""
    for encoding in ("utf-8", "cp1251", "cp866"):
        try:
            text = raw.decode(encoding, "strict")
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text[text.find("{"):text.rfind("}") + 1] or "{}")
    except (ValueError, json.JSONDecodeError):
        return []

    def as_list(value) -> list:
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    adapters: dict[str, dict] = {}
    for adapter in as_list(data.get("ad")):
        if not isinstance(adapter, dict):
            continue
        index = str(adapter.get("ifIndex") or "")
        if index:
            adapters[index] = adapter
    items: list[dict] = []
    for entry in as_list(data.get("ip")):
        if not isinstance(entry, dict):
            continue
        ip = str(entry.get("IPAddress") or "").strip()
        if not _is_ipv4(ip):
            continue
        index = str(entry.get("InterfaceIndex") or "")
        adapter = adapters.get(index, {})
        alias = str(entry.get("InterfaceAlias") or "").strip()
        name = alias or str(adapter.get("Name") or "").strip()
        description = str(adapter.get("InterfaceDescription") or "").strip()
        prefix = entry.get("PrefixLength")
        items.append({
            # Имя и описание склеиваются: VPN-адаптер узнаётся и по «tun0»,
            # и по «WireGuard Tunnel», и по «TAP-Windows Adapter V9».
            "name": " ".join(part for part in (name, description) if part),
            "iface": name,
            "ip": ip,
            "prefix": int(prefix) if str(prefix).isdigit() else 0,
            "status": str(adapter.get("Status") or ""),
        })
    return items


def _bsd_interfaces() -> list[dict]:
    """Адреса macOS/BSD: разбор ``ifconfig -a`` (имя + inet + netmask)."""
    try:
        proc = subprocess.run(["ifconfig", "-a"], capture_output=True,
                              text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    items: list[dict] = []
    name = ""
    for line in (proc.stdout or "").splitlines():
        if not line:
            continue
        if not line[0].isspace():
            name = line.split(":", 1)[0].strip()
            continue
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "inet" and _is_ipv4(fields[1]):
            prefix = 0
            if "netmask" in fields:
                prefix = _netmask_to_prefix(fields[fields.index("netmask") + 1])
            items.append({"name": name, "ip": fields[1], "prefix": prefix})
    return items


def _route_default_interface() -> str:
    """Имя интерфейса, через который идёт маршрут по умолчанию ('' — неизвестно)."""
    if sys.platform.startswith("linux"):
        try:
            text = Path("/proc/net/route").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) > 1 and fields[1] == "00000000":
                return fields[0]
        return ""
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "$ErrorActionPreference='SilentlyContinue';"
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                 "(Get-NetRoute -DestinationPrefix 0.0.0.0/0 | "
                 "Sort-Object RouteMetric | Select-Object -First 1)."
                 "InterfaceAlias"],
                capture_output=True, timeout=15)
            return (proc.stdout or b"").decode("utf-8", "replace").strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    try:
        proc = subprocess.run(["route", "-n", "get", "default"],
                              capture_output=True, text=True, timeout=5)
        for line in (proc.stdout or "").splitlines():
            if "interface:" in line:
                return line.split("interface:", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _probe_addresses() -> list[dict]:
    """Запасной способ: источник, который ядро выбирает для внешнего адреса.

    Префикс здесь неизвестен (0), поэтому такие адреса принимаются только из
    частных диапазонов LAN и только если их нет среди адресов туннелей.
    """
    found: list[dict] = []
    for target in (("8.8.8.8", 80), ("1.1.1.1", 80), ("192.168.1.1", 80)):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.8)
            sock.connect(target)
            ip = sock.getsockname()[0]
            if _is_ipv4(ip):
                found.append({"name": "", "ip": ip, "prefix": 0, "probe": True})
        except OSError:
            continue
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if _is_ipv4(ip):
                found.append({"name": socket.gethostname(), "ip": ip,
                              "prefix": 0, "probe": True})
    except OSError:
        pass
    return found


def network_interfaces(use_cache: bool = True) -> list[dict]:
    """Все IPv4-адреса машины: ``{name, ip, prefix}`` по каждому интерфейсу.

    Список кэшируется на 30 секунд: перечисление в Windows — это вызов
    PowerShell (~0.5 с), а адреса спрашивают и SSDP-рассылка, и карточка
    настроек, и генератор QR-кодов.
    """
    now = time.time()
    cached = _INTERFACE_CACHE.get("items") or []
    if use_cache and cached and now - float(_INTERFACE_CACHE.get("at") or 0.0) \
            < _INTERFACE_CACHE_TTL:
        return list(cached)  # type: ignore[arg-type]
    if os.name == "nt":
        items = _windows_interfaces()
    elif sys.platform.startswith("linux"):
        items = _linux_interfaces()
    else:
        items = _bsd_interfaces()
    if not items:
        items = _probe_addresses()
    else:
        # Туннельные адреса уже перечислены; пробуем только то, чего нет.
        known = {item["ip"] for item in items}
        items.extend(entry for entry in _probe_addresses()
                     if entry["ip"] not in known)
    _INTERFACE_CACHE["items"] = items
    _INTERFACE_CACHE["at"] = now
    return list(items)


def reset_interface_cache() -> None:
    """Сбросить кэш интерфейсов (тесты, смена сети, подключение VPN)."""
    _INTERFACE_CACHE["items"] = []
    _INTERFACE_CACHE["at"] = 0.0


def _address_priority(ip: str, prefix: int, name: str) -> int | None:
    """Приоритет адреса для LAN-анонса; None — адрес не годится.

    Порядок тот же, что и раньше (192.168 → 10 → 172.16-31 → CGNAT), но
    туннели и точка-точка теперь отсеиваются до сортировки.
    """
    parts = _ipv4_parts(ip)
    if parts is None:
        return None
    first, second = parts[0], parts[1]
    if first in (0, 127) or first >= 224:
        return None                      # loopback, «все сети», multicast
    if first == 169 and second == 254:
        return None                      # APIPA: DHCP не выдал адрес
    if prefix and prefix >= POINT_TO_POINT_PREFIX:
        return None                      # /30, /31, /32 — туннель, не LAN
    if is_vpn_interface(name):
        return None
    if first == 192 and second == 168:
        return 0
    if first == 10:
        return 1
    if first == 172 and 16 <= second <= 31:
        return 2
    if first == 100 and 64 <= second <= 127:
        return CGNAT_PRIORITY            # Tailscale и прочий CGNAT
    return None                          # публичный адрес в LAN не анонсируем


def lan_addresses(interfaces: list[dict] | None = None) -> list[str]:
    """IPv4-адреса, годные для LAN: без VPN, без точка-точка, без APIPA."""
    items = list(interfaces) if interfaces is not None else network_interfaces()

    def key(entry: dict):
        priority = _address_priority(str(entry.get("ip") or ""),
                                     int(entry.get("prefix") or 0),
                                     str(entry.get("name") or ""))
        return (priority if priority is not None else 99, str(entry.get("ip")))

    seen: set[str] = set()
    result: list[str] = []
    for entry in sorted(items, key=key):
        priority = _address_priority(str(entry.get("ip") or ""),
                                     int(entry.get("prefix") or 0),
                                     str(entry.get("name") or ""))
        if priority is None:
            continue
        ip = str(entry["ip"])
        if ip in seen:
            continue
        seen.add(ip)
        result.append(ip)
    return result


def vpn_addresses(interfaces: list[dict] | None = None) -> list[dict]:
    """Адреса, которые LAN-анонс обязан пропустить, с причиной отбраковки.

    Нужны для диагностики: владелец должен видеть, ЧТО именно перехватило
    сеть (``tun0 10.0.0.1/30``), а не догадываться по «код=-1».
    """
    items = list(interfaces) if interfaces is not None else network_interfaces()
    found: list[dict] = []
    for entry in items:
        ip = str(entry.get("ip") or "")
        prefix = int(entry.get("prefix") or 0)
        name = str(entry.get("name") or "")
        if _ipv4_parts(ip) is None:
            continue
        if _address_priority(ip, prefix, name) is not None:
            continue
        if ip.startswith("127.") or ip.startswith("0."):
            continue                     # loopback — не «перехват», а норма
        reason = ""
        if is_vpn_interface(name):
            reason = "туннельный/виртуальный интерфейс"
        elif prefix and prefix >= POINT_TO_POINT_PREFIX:
            reason = f"префикс /{prefix} — точка-точка, не сегмент LAN"
        elif ip.startswith("169.254."):
            reason = "APIPA: адрес не выдан DHCP"
        else:
            reason = "не частный LAN-диапазон"
        found.append({"name": name or "?", "ip": ip, "prefix": prefix,
                      "reason": reason})
    return found


def subnet_of(ip: str, prefix: int = 24) -> str:
    """Сеть адреса: ``192.168.0.108`` → ``192.168.0.0/24`` ('' — не IPv4)."""
    parts = _ipv4_parts(ip)
    if parts is None:
        return ""
    prefix = int(prefix) if 0 < int(prefix) <= 32 else 24
    value = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    network = value & mask
    return (f"{(network >> 24) & 255}.{(network >> 16) & 255}."
            f"{(network >> 8) & 255}.{network & 255}/{prefix}")


def prefix_of(ip: str, interfaces: list[dict] | None = None) -> int:
    """Длина префикса адреса на этой машине (0 — неизвестна)."""
    items = list(interfaces) if interfaces is not None else network_interfaces()
    for entry in items:
        if str(entry.get("ip") or "") == str(ip or ""):
            return int(entry.get("prefix") or 0)
    return 0


def interface_of(ip: str, interfaces: list[dict] | None = None) -> str:
    """Имя интерфейса, на котором висит адрес ('' — такого адреса нет)."""
    items = list(interfaces) if interfaces is not None else network_interfaces()
    for entry in items:
        if str(entry.get("ip") or "") == str(ip or ""):
            return str(entry.get("iface") or entry.get("name") or "")
    return ""


def vpn_interception(host: str = "") -> dict:
    """Перехватывает ли VPN локальную сеть (причина «код=-1» при живом шлюзе).

    Возвращает словарь для диагностики и журнала:

    * ``active`` — туннельные адреса на машине вообще есть;
    * ``intercepting`` — они мешают LAN (маршрут по умолчанию уходит в
      туннель, LAN-адресов не осталось, или закреплённый адрес шлюза не
      найден ни на одном интерфейсе);
    * ``names`` — какие именно адаптеры мешают (APIPA сюда не попадает: это
      сбой DHCP, а не туннель, и совет «отключите eth0» только запутает);
    * ``advice`` — готовая строка владельцу, ровно та, что пишется в журнал.
    """
    items = network_interfaces()
    tunnels = vpn_addresses(items)
    lan = lan_addresses(items)
    default_iface = _route_default_interface()
    default_is_vpn = bool(default_iface) and is_vpn_interface(default_iface)
    pinned = str(host or "").strip()
    # Закреплённый loopback — нормальная конфигурация (Studio и PrintFlow на
    # одном ПК, тесты), а не признак перехвата: совет про split-tunnel для
    # 127.0.0.0/8 был бы просто вредным.
    pinned_relevant = bool(_is_ipv4(pinned)) and not pinned.startswith(("127.", "0."))
    pinned_missing = pinned_relevant and pinned not in lan
    intercepting = bool(tunnels) and (
        default_is_vpn or not lan or pinned_missing)
    culprits = [
        entry for entry in tunnels
        if is_vpn_interface(str(entry.get("name")))
        or not str(entry.get("ip") or "").startswith("169.254.")
    ]
    names = ", ".join(dict.fromkeys(
        str(entry.get("iface") or entry.get("name") or "").strip() or "?"
        for entry in culprits)) or (str(tunnels[0].get("name") or "tun0")
                                    if tunnels else "tun0")
    if pinned_relevant:
        network = subnet_of(pinned, prefix_of(pinned, items) or 24)
    elif lan:
        network = subnet_of(lan[0], prefix_of(lan[0], items) or 24)
    else:
        network = ""
    detail = "; ".join(
        f"{entry['name']} {entry['ip']}"
        + (f"/{entry['prefix']}" if entry["prefix"] else "")
        + f" — {entry['reason']}" for entry in tunnels)
    advice = ""
    if intercepting:
        advice = (f"VPN перехватывает LAN, отключите {names}"
                  + (f" или добавьте {network} в split-tunnel" if network else ""))
    return {
        "active": bool(tunnels),
        "intercepting": intercepting,
        "tunnels": tunnels,
        "names": names,
        "culprits": culprits,
        "lan": lan,
        "default_interface": default_iface,
        "default_is_vpn": default_is_vpn,
        "host": pinned,
        "host_missing": pinned_missing,
        "subnet": network,
        "detail": detail,
        "advice": advice,
    }


def get_local_ips() -> list[str]:
    """IPv4-адреса этого ПК в локальной сети (для доступа с телефона/планшета).

    При нескольких роутерах/подсетях полезно видеть все адреса: телефон может
    сидеть в другой Wi-Fi сети, чем та, которую вы подумали первой.

    Адреса перечисляются по интерфейсам, поэтому VPN-туннель (``tun0``
    ``10.0.0.1/30``) в список не попадает: ни QR-код, ни SSDP-объявление не
    должны вести на адрес, по которому машины владельца нет. См. также
    :func:`vpn_interception` — она объясняет, что именно отброшено.
    """
    return lan_addresses()

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


def host_name(host: str) -> str:
    """Имя хоста без порта: ``192.168.1.50:8765`` → ``192.168.1.50``."""
    host = (host or "").strip()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end].lower() if end > 0 else host.lower()
    if host.count(":") == 1:
        return host.split(":", 1)[0].lower()
    return host.lower()


def host_port(host: str, default: int = DEFAULT_PORT) -> int:
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
                lan_ips: list[str] | None = None, listen_port: int = DEFAULT_PORT) -> dict:
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
    port = host_port(host_header, listen_port or DEFAULT_PORT)
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
                    listen_port: int = DEFAULT_PORT) -> dict:
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
