"""Схема настроек PrintFlow (идея 10).

Проблема. `DEFAULT_SETTINGS` — плоский словарь на 213 ключей: строки, числа
и флаги вперемешку, без типа, без группы и без допустимых значений.
Форма в панели собрана вручную и давно не соответствует словарю, а
`set_settings` принимает всё, что прислали: опечатка в имени ключа молча
создавала новую настройку, а строка «abc» в числовом тарифе уезжала в
расчёты и превращалась в 0.

Решение. Схема выводится из `DEFAULT_SETTINGS` автоматически (тип — по
значению по умолчанию), дополняется группами и ограничениями, и
используется в трёх местах:

* `validate(patch)` — при сохранении: неизвестные ключи отбрасываются,
  значения приводятся к своему типу, выход за границы режется;
* `describe()` — `/api/settings/schema`: форма и подсказки строятся из
  схемы, а не руками в HTML;
* `diff_defaults()` — «что у меня изменено относительно завода» для
  диагностики и переноса настроек на другую машину.

Схема не заменяет `DEFAULT_SETTINGS`, а описывает его: новый ключ в
словаре автоматически становится валидным, просто без группы и подписи.
"""
from __future__ import annotations

from typing import Any

from .config import DEFAULT_SETTINGS, SECRET_SETTINGS

BOOL_LIKE = (bool,)
INT_LIKE = (int,)
FLOAT_LIKE = (float,)

# Группы настроек: порядок задаёт порядок секций в форме.
# 17.0 (решение №9): касса разделена на три вкладки — Касса, СБП, Банк.
GROUPS = {
    "workshop": "Цех и производство",
    "money": "Деньги и тарифы",
    "tax": "Налоги и режим",
    "printers": "Принтеры и Bambu",
    "telegram": "Telegram и уведомления",
    "clientbot": "Клиентский бот и витрина",
    "automation": "Автоматизация и правила",
    "storage": "Склад и материалы",
    "cashier": "Касса",
    "sbp": "СБП",
    "bank": "Банк",
    "documents": "Документы и реквизиты",
    "interface": "Интерфейс",
    "system": "Система и данные",
}

# Технические ключи (17.0): хранятся и валидируются как обычно, но в форме
# свёрнуты в режим «Показать все» — это смещения, хэши версий, пути, профили.
# Решение заказчика №8: все 222 настройки видимы, технические помечены.
TECHNICAL_KEYS = frozenset({
    "installed_sha", "update_seen_sha", "last_update_at", "update_branch",
    "update_check_hours", "update_check_enabled", "auto_update_enabled",
    "client_bot_update_offset", "telegram_bot_update_offset",
    "settings_profiles", "month_close", "bank_rules", "shelf_low_last",
    "printer_invested_at", "ui_density", "ui_start_view", "debug_verbose",
    "watch_folder_path", "slicer_bin", "slicer_filename_template",
    "studio_gateway_serial", "studio_gateway_printer_id",
    "reply_templates", "client_bot_templates",
})

# Ключ → (группа, подпись, ограничения). Ограничения: min/max для чисел,
# choices для строк-перечислений.
META: dict[str, dict] = {
    # --- деньги и тарифы
    "target_profit_per_hour": ("money", "Целевая прибыль в час", {"min": 0, "max": 100000}),
    "labor_rate": ("money", "Ставка оператора, ₽/час", {"min": 0, "max": 100000}),
    "currency": ("money", "Валюта отображения", {"max_len": 8}),
    # --- налоги
    "tax_mode": ("tax", "Режим налогообложения",
                 {"choices": ("none", "npd", "usn6", "usn15", "patent", "manual")}),
    # --- витрина (В67): сезонное оформление публичных страниц
    "shop_season": ("clientbot", "Сезон витрины",
                    {"choices": ("none", "newyear", "spring", "autumn")}),
    "npd_rate_person": ("tax", "НПД с физлиц, %", {"min": 0, "max": 100}),
    "npd_rate_company": ("tax", "НПД с юрлиц, %", {"min": 0, "max": 100}),
    "usn_income_rate": ("tax", "УСН «доходы», %", {"min": 0, "max": 100}),
    "usn_profit_rate": ("tax", "УСН «доходы минус расходы», %", {"min": 0, "max": 100}),
    "tax_rate": ("tax", "Ручная ставка, %", {"min": 0, "max": 100}),
    "npd_limit": ("tax", "Годовой лимит НПД, ₽", {"min": 0}),
    "npd_limit_warn_pct": ("tax", "Тревога по лимиту НПД, %", {"min": 50, "max": 100}),
    "npd_alerts_enabled": ("tax", "Напоминать про чеки НПД", {}),
    "npd_alert_time": ("tax", "Время напоминания о чеках (ЧЧ:ММ)", {"max_len": 5}),
    "usn_limit": ("tax", "Предел применения УСН, ₽", {"min": 0}),
    "vat_threshold": ("tax", "Порог освобождения от НДС, ₽", {"min": 0}),
    # --- принтеры
    "printer_investment": ("printers", "Вложения в парк, ₽", {"min": 0}),
    "camera_fps_max": ("printers", "Предел FPS камеры (0 = без предела)",
                       {"min": 0, "max": 30}),
    "encrypt_access_code": ("printers", "Шифровать access-коды принтеров", {}),
    # --- telegram
    "telegram_token": ("telegram", "Токен бота сотрудников", {"secret": True}),
    "telegram_chat_id": ("telegram", "Chat ID владельца", {"max_len": 64}),
    "client_bot_token": ("clientbot", "Токен клиентского бота", {"secret": True}),
    "client_bot_enabled": ("clientbot", "Клиентский бот включён", {}),
    # --- облако и шлюз
    "cloud_email": ("printers", "Аккаунт Bambu Cloud", {"max_len": 128}),
    "cloud_region": ("printers", "Регион Bambu Cloud", {"choices": ("global", "china")}),
    "cloud_token": ("printers", "Токен облака", {"secret": True}),
    "cloud_uid": ("printers", "UID облака", {"secret": True}),
    "studio_gateway_access_code": ("printers", "Access Code шлюза Studio", {"secret": True}),
    "studio_gateway_enabled": ("printers", "Шлюз Bambu Studio включён", {}),
    # --- касса (Касса 16.0): вход кассира
    "cashier_code": ("cashier", "Код кассы (вход кассира)", {"max_len": 32}),
    "cashier_shift_mode": ("cashier", "Смены на кассе",
                           {"choices": ("auto", "manual")}),
    "cashier_offline_negative": ("cashier", "Офлайн-продажа: разрешить минус-остаток",
                                 {"hint": "Когда товар из очереди после обрыва "
                                          "не влезает в остаток — провести и "
                                          "пометить «минус», а не блокировать "
                                          "продажу. Снятие флажка вернёт жёсткий "
                                          "контроль: строка останется в очереди "
                                          "до решения старшего."}),
    "cashier_ring": ("cashier", "Звенеть о платежах на кассе",
                     {"hint": "Вибрация и звук на телефоне кассира, когда "
                              "платёж подтверждён или пришёл из банка. "
                              "Баннер и счётчик остаются в любом случае."}),
    # --- СБП (Касса 16.0)
    "sbp_enabled": ("sbp", "Приём СБП включён", {}),
    "sbp_account_id": ("sbp", "Счёт для СБП", {"max_len": 32}),
    "sbp_shop_qr": ("sbp", "Статический QR магазина (СБП)", {"max_len": 300}),
    "sbp_bank_name": ("sbp", "Название банка СБП", {"max_len": 80}),
    "sbp_payment_note": ("sbp", "Назначение перевода СБП", {"max_len": 200}),
    "sbp_purpose_limit": ("sbp", "Длина назначения из товаров", {"min": 20, "max": 210}),
    "sbp_hold_hours": ("sbp", "Холд СБП: часов до автоснятия", {"min": 1, "max": 168}),
    # --- платёжный QR (генерируем сами)
    "pay_qr_mode": ("sbp", "Режим платёжного QR",
                    {"choices": ("auto", "gost", "link", "static", "off")}),
    "pay_qr_link": ("sbp", "Ссылка для оплаты ({amount})", {"max_len": 300}),
    "pay_qr_camera": ("sbp", "QR для камеры: ссылка важнее ГОСТ-текста",
                      {"hint": "Обычная камера телефона открывает приложение банка по "
                               "ссылке и показывает текст у ГОСТ-кода. Ссылкой "
                               "считается то, что дал банк: шаблон ссылки с суммой "
                               "или статический QR магазина. Наш ГОСТ-код остаётся "
                               "запасным — в нём сумма уже внутри, покупатель её не "
                               "введёт неправильно. Работает в режиме «авто»; "
                               "принудительный режим ГОСТ-кода остаётся текстом."}),
    "pay_payee_name": ("sbp", "Получатель платежа (для ГОСТ QR)", {"max_len": 160}),
    "pay_payee_inn": ("sbp", "ИНН получателя платежа", {"max_len": 12}),
    "pay_payee_kpp": ("sbp", "КПП получателя платежа", {"max_len": 9}),
    "pay_account": ("sbp", "Расчётный счёт", {"max_len": 20}),
    "pay_bank_name": ("sbp", "Банк получателя", {"max_len": 100}),
    "pay_bic": ("sbp", "БИК банка", {"max_len": 9}),
    "pay_corr_account": ("sbp", "Корреспондентский счёт", {"max_len": 20}),
    "sbp_auto_confirm": ("bank", "Авто-подтверждение СБП по банку", {}),
    "sbp_match_window_hours": ("bank", "Окно сопоставления поступлений, ч",
                               {"min": 1, "max": 168}),
    # --- банк (поступления и импорт выписок)
    # bank_rules — json технический, описан ниже в разделе системы/кассы.
    # --- документы
    "legal_name": ("documents", "Наименование для документов", {"max_len": 200}),
    "inn": ("documents", "ИНН", {"max_len": 32}),
    # --- интерфейс
    "theme": ("interface", "Тема оформления", {"choices": ("system", "light", "dark")}),
    "accent": ("interface", "Акцентный цвет", {"max_len": 32}),
    # --- система
    "public_url": ("system", "Публичный адрес панели", {"max_len": 300}),
    "backup_keep": ("system", "Сколько бэкапов хранить", {"min": 1, "max": 200}),
    "backup_auto_export": ("system", "Автоэкспорт бэкапов", {}),
}

# 17.0 (И4): META покрывает ВСЕ 222 ключа DEFAULT_SETTINGS — раньше было 50.
# Технические/служебные ключи помечены advanced (плюс TECHNICAL_KEYS) и в
# форме свёрнуты до режима «Показать все». Группы: см. GROUPS выше.
META.update({
    # --- документы и реквизиты
    "company_name": ("documents", "Название компании", {"max_len": 120}),
    # --- цех: мощность, брак, ёмкость
    "power_kw": ("workshop", "Мощность принтера, кВт", {"min": 0, "max": 2}),
    "weekly_capacity_hours": ("workshop", "Доступно часов печати в неделю", {"min": 0, "max": 500}),
    "failure_rate": ("workshop", "Ожидаемый процент брака, %", {"min": 0, "max": 100}),
    "maintenance_per_hour": ("workshop", "Обслуживание станка, ₽/час", {"min": 0, "max": 100000}),
    # --- деньги и тарифы
    "energy_price": ("money", "Цена электроэнергии, ₽/кВт·ч", {"min": 0, "max": 100}),
    "amortization_per_hour": ("money", "Амортизация станка, ₽/час", {"min": 0, "max": 100000}),
    "count_labor_in_cost": ("money", "Учитывать труд оператора в себестоимости", {}),
    "packaging_cost": ("money", "Упаковка, ₽ на заказ", {"min": 0, "max": 10000}),
    "default_markup": ("money", "Наценка по умолчанию, %", {"min": 0, "max": 1000}),
    "min_order_price": ("money", "Минимальная сумма заказа, ₽", {"min": 0}),
    "price_rounding": ("money", "Округление цены, ₽", {"min": 0, "max": 1000}),
    "rush_surcharge": ("money", "Наценка за срочность, %", {"min": 0, "max": 200}),
    "design_rate": ("money", "Ставка моделирования, ₽/час", {"min": 0, "max": 100000}),
    "bulk_discount_10": ("money", "Скидка от 10 шт., %", {"min": 0, "max": 100}),
    "bulk_discount_50": ("money", "Скидка от 50 шт., %", {"min": 0, "max": 100}),
    "acquiring_fee": ("money", "Комиссия эквайринга по умолчанию, %", {"min": 0, "max": 50}),
    "delivery_cost": ("money", "Стоимость доставки по умолчанию, ₽", {"min": 0, "max": 100000}),
    "allocate_fixed_costs": ("money", "Распределять постоянные расходы на заказы", {}),
    "fixed_costs_auto": ("money", "Начислять постоянные расходы автоматически", {}),
    "debt_alert_days": ("money", "Долг: предупреждать после, дней", {"min": 0, "max": 365}),
    "debt_reminder_cooldown_days": ("money", "Долг: повторное напоминание, дней", {"min": 0, "max": 60}),
    "low_margin_alert": ("money", "Порог низкой маржи, %", {"min": 0, "max": 100}),
    "goal_profit_month": ("money", "Цель по прибыли в месяц, ₽", {"min": 0}),
    # --- налоги
    "npd_bonus_left": ("tax", "Остаток вычета НПД, ₽", {"min": 0}),
    "usn_min_tax_rate": ("tax", "Минимальная ставка УСН, %", {"min": 0, "max": 100}),
    "patent_cost_year": ("tax", "Стоимость патента в год, ₽", {"min": 0}),
    "insurance_fixed": ("tax", "Фиксированные страховые взносы, ₽/год", {"min": 0}),
    "insurance_extra_rate": ("tax", "Доп. взносы, %", {"min": 0, "max": 100}),
    "insurance_extra_base": ("tax", "База доп. взносов, ₽", {"min": 0}),
    "insurance_extra_cap": ("tax", "Потолок дохода для доп. взносов, ₽", {"min": 0}),
    "insurance_reduces_tax": ("tax", "Страховые взносы уменьшают налог", {}),
    "vat_enabled": ("tax", "Плательщик НДС", {}),
    "vat_rate": ("tax", "Ставка НДС, %", {"min": 0, "max": 100}),
    "tax_reserve_enabled": ("tax", "Резерв под налоги включён", {}),
    "tax_reserve_extra": ("tax", "Дополнительный резерв под налоги, ₽", {"min": 0}),
    # --- склад и материалы
    "default_spool_price": ("storage", "Цена катушки по умолчанию, ₽", {"min": 0}),
    "default_spool_weight": ("storage", "Вес катушки по умолчанию, г", {"min": 1}),
    "filament_low_threshold": ("storage", "Порог низкого остатка катушки, %", {"min": 0, "max": 100}),
    "shopping_runout_days": ("storage", "Закупки: горизонт расхода, дней", {"min": 1, "max": 365}),
    "dry_humidity_threshold": ("storage", "Сушка: порог влажности, %", {"min": 0, "max": 100}),
    "restock_remind": ("storage", "Напоминать о пополнении склада", {}),
    "shelf_low_last": ("storage", "Стеллаж: время последней проверки", {"advanced": True}),
    # --- касса (счёт по умолчанию)
    "default_account": ("cashier", "Касса/счёт по умолчанию", {"max_len": 32}),
    # --- банк
    "bank_rules": ("bank", "Правила разнесения банковской выписки", {"advanced": True}),
    # --- автоматизация
    "auto_accounting": ("automation", "Автоматический учёт проводок", {}),
    "auto_link_orders": ("automation", "Автопривязка печатей к заказам", {}),
    "auto_consume_filament": ("automation", "Автосписание пластика по завершении", {}),
    "auto_income_on_done": ("automation", "Записывать доход при завершении заказа", {}),
    "auto_queue": ("automation", "Автопостановка заданий в очередь", {}),
    "unattended_dangerous_actions": ("automation", "Разрешить опасные действия без присмотра", {}),
    "auto_resume_paused": ("automation", "Автовозобновление после паузы", {}),
    "auto_resume_max_delay_minutes": ("automation", "Автовозобновление: макс. задержка, мин", {"min": 0, "max": 10080}),
    "night_shift_enabled": ("automation", "Ночная смена разрешена", {}),
    "night_reset_enabled": ("automation", "Ночное обновление плана очереди", {}),
    "night_reset_time": ("automation", "Время ночного обновления", {"max_len": 8}),
    # --- принтеры: сторож, префлайт, камера, AMS, протоколы
    "guard_enabled": ("printers", "Сторож печати включён", {}),
    "guard_pause_on_error": ("printers", "Ставить паузу при ошибке", {}),
    "guard_pause_severity": ("printers", "Порог паузы сторожа",
                             {"choices": ("error", "warning")}),
    "guard_stall_minutes": ("printers", "Сторож: порог простоя, мин", {"min": 0, "max": 240}),
    "guard_cold_minutes": ("printers", "Сторож: порог холодного старта, мин", {"min": 0, "max": 240}),
    "guard_count_loss": ("printers", "Сторож: учитывать потери пластика", {}),
    "guard_snapshot": ("printers", "Сторож: снимки камеры при ошибке", {}),
    "guard_cost_limit": ("printers", "Сторож: лимит потерь, ₽ (0 = без лимита)", {"min": 0}),
    "guard_overrun_pct": ("printers", "Сторож: допустимый перерасход времени, %", {"min": 0, "max": 500}),
    "spaghetti_enabled": ("printers", "Детектор «спагетти» включён", {}),
    "spaghetti_sensitivity": ("printers", "Чувствительность детектора спагетти", {"min": 1, "max": 10}),
    "envelope_auto": ("printers", "Автоконверт заданий под конфигурацию", {}),
    "ejector_enabled": ("printers", "Авто-эжектор деталей включён", {}),
    "maintenance_enabled": ("printers", "Напоминания о техобслуживании", {}),
    "telemetry_enabled": ("printers", "Сбор телеметрии включён", {}),
    "telemetry_keep_days": ("printers", "Хранить телеметрию, дней", {"min": 1, "max": 365}),
    "printer_invested_at": ("printers", "Дата вложений в парк", {"advanced": True}),
    "keyframe_interval_min": ("printers", "Видео печати: интервал кадра, мин", {"min": 0, "max": 120}),
    "bed_watch_enabled": ("printers", "Контроль детали на столе", {}),
    "bed_watch_threshold": ("printers", "Контроль стола: порог изменения", {"min": 0, "max": 100}),
    "first_layer_watch_min": ("printers", "Наблюдение первого слоя, мин", {"min": 0, "max": 60}),
    "camera_timelapse_interval": ("printers", "Таймлапс: интервал кадра, мин", {"min": 0, "max": 120}),
    "camera_keep_shots": ("printers", "Хранить кадров камеры", {"min": 0, "max": 10000}),
    "camera_roi_center": ("printers", "Камера: центральная зона, %", {"min": 0, "max": 100}),
    "ams_auto_map": ("printers", "AMS: авто-маппинг слотов", {}),
    "ams_delta_e_threshold": ("printers", "AMS: порог различия цвета (ΔE)", {"min": 0, "max": 100}),
    "ams_auto_spools": ("printers", "AMS: автоматические катушки", {}),
    "ams_sync_remaining": ("printers", "AMS: синхронизировать остаток", {}),
    "printer_info_sync": ("printers", "Синхронизировать сведения о принтерах", {}),
    "cloud_history_sync": ("printers", "Облако: синхронизация истории", {}),
    "cloud_sync_minutes": ("printers", "Облако: период опроса, мин", {"min": 1, "max": 240}),
    "ftps_timeout": ("printers", "FTPS: таймаут, с", {"min": 1, "max": 300}),
    "ftps_retries": ("printers", "FTPS: число повторов", {"min": 0, "max": 10}),
    "ftps_block_kb": ("printers", "FTPS: размер блока, КБ", {"min": 16, "max": 4096}),
    "ftps_queue": ("printers", "FTPS: очередь загрузки", {}),
    "ftps_dedup": ("printers", "FTPS: не заливать дубли", {}),
    "mqtt_keepalive": ("printers", "MQTT: keepalive, с", {"min": 10, "max": 600}),
    "mqtt_backoff": ("printers", "MQTT: повторные подключения", {}),
    "mqtt_fallback_1883": ("printers", "MQTT: запасной порт 1883", {}),
    "watch_folder_enabled": ("printers", "Watch Folder включён", {}),
    "watch_folder_path": ("printers", "Папка Watch Folder", {"advanced": True, "max_len": 400}),
    "watch_auto_action": ("printers", "Watch Folder: действие",
                          {"choices": ("notify", "queue", "order")}),
    "watch_link_order": ("printers", "Watch Folder: привязывать заказ", {}),
    "watch_create_order": ("printers", "Watch Folder: создавать заказ", {}),
    "studio_gateway_name": ("printers", "Шлюз Bambu Studio: имя", {"max_len": 64}),
    "studio_gateway_mode": ("printers", "Шлюз Bambu Studio: режим",
                            {"choices": ("queue", "printer")}),
    "studio_gateway_autostart": ("printers", "Шлюз Studio: автозапуск", {}),
    "studio_gateway_serial": ("printers", "Шлюз Studio: серийный номер", {"advanced": True, "max_len": 64}),
    "studio_gateway_printer_id": ("printers", "Шлюз Studio: привязанный принтер", {"advanced": True, "max_len": 64}),
    "slicer_bin": ("printers", "Путь к слайсеру", {"advanced": True, "max_len": 400}),
    "slicer_auto_create_order": ("printers", "Слайсер: создавать заказ из файла", {}),
    "slicer_filename_template": ("printers", "Слайсер: шаблон имени файла", {"advanced": True, "max_len": 200}),
    "preflight_enabled": ("printers", "Префлайт включён", {}),
    "preflight_block_idle": ("printers", "Префлайт: блокировать простой принтера", {}),
    "preflight_block_hms": ("printers", "Префлайт: блокировать без HMS", {}),
    "preflight_block_material": ("printers", "Префлайт: проверять материал", {}),
    "preflight_block_filament": ("printers", "Префлайт: проверять пластик", {}),
    "preflight_block_bed": ("printers", "Префлайт: проверять стол", {}),
    "preflight_warn_sd": ("printers", "Префлайт: предупреждать о SD-карте", {}),
    "preflight_warn_nozzle": ("printers", "Префлайт: предупреждать о сопле", {}),
    "preflight_warn_humidity": ("printers", "Префлайт: предупреждать о влажности", {}),
    "preflight_warn_calibration": ("printers", "Префлайт: предупреждать о калибровке", {}),
    # --- очередь
    "queue_check_filament": ("workshop", "Очередь: проверять наличие пластика", {}),
    "queue_check_material": ("workshop", "Очередь: проверять тип материала", {}),
    "queue_group_material": ("workshop", "Очередь: группировать по материалу", {}),
    "queue_smart_group": ("workshop", "Очередь: умная группировка заданий", {}),
    "queue_smart_deadline": ("workshop", "Очередь: приоритет по сроку", {}),
    "queue_offline_defer": ("workshop", "Очередь: откладывать, если принтер офлайн", {}),
    "qc_checklist": ("workshop", "Чек-лист контроля качества", {"advanced": True}),
    # --- Telegram и уведомления
    "telegram_enabled": ("telegram", "Telegram-уведомления включены", {}),
    "telegram_bot": ("telegram", "Бот сотрудников включён", {}),
    "telegram_quiet_from": ("telegram", "Тихие часы сотрудников: начало", {"max_len": 8}),
    "telegram_quiet_to": ("telegram", "Тихие часы сотрудников: конец", {"max_len": 8}),
    "telegram_bot_update_offset": ("telegram", "Offset бота сотрудников", {"advanced": True}),
    "quiet_hours_enabled": ("telegram", "Тихие часы включены", {}),
    "quiet_from": ("telegram", "Тихие часы: начало", {"max_len": 8}),
    "quiet_to": ("telegram", "Тихие часы: конец", {"max_len": 8}),
    "digest_time": ("telegram", "Время дневного дайджеста", {"max_len": 8}),
    "weekly_report_day": ("telegram", "Еженедельный отчёт: день недели", {"min": 0, "max": 6}),
    "weekly_report_time": ("telegram", "Еженедельный отчёт: время", {"max_len": 8}),
    "notify_complete": ("telegram", "Уведомлять о завершении печати", {}),
    "notify_error": ("telegram", "Уведомлять об ошибках", {}),
    "notify_pause": ("telegram", "Уведомлять о паузах", {}),
    "notify_filament_low": ("telegram", "Уведомлять о малом остатке пластика", {}),
    "notify_guard": ("telegram", "Уведомлять о срабатываниях сторожа", {}),
    "notify_firmware": ("telegram", "Уведомлять о прошивке", {}),
    "notify_maintenance": ("telegram", "Уведомлять о ТО", {}),
    "notify_photo": ("telegram", "Прикладывать фото к уведомлениям", {}),
    "notify_finish_remind_min": ("telegram", "Напомнить о завершении за, мин", {"min": 0, "max": 600}),
    # --- клиентский бот и витрина
    "client_bot_welcome": ("clientbot", "Приветствие /start", {"max_len": 2000}),
    "client_bot_notify": ("clientbot", "Уведомлять клиента о статусе", {}),
    "client_bot_catalog": ("clientbot", "Показывать каталог в боте", {}),
    "client_bot_faq": ("clientbot", "Вопрос-ответ (FAQ)", {"max_len": 4000}),
    "client_bot_review": ("clientbot", "Запрашивать отзыв после выдачи", {}),
    "client_bot_pickup_days": ("clientbot", "Напоминание о выдаче: дней в статусе «Готов»", {"min": 0, "max": 30}),
    "client_bot_pickup_info": ("clientbot", "Текст «Как получить»", {"max_len": 2000}),
    "client_bot_ready_photo": ("clientbot", "Отправлять фото готового заказа", {}),
    "client_bot_faq_materials": ("clientbot", "Статья «Как выбрать материал»", {"max_len": 4000}),
    "client_bot_pay_info": ("clientbot", "Реквизиты оплаты", {"max_len": 2000}),
    "client_bot_pay_qr": ("clientbot", "QR СБП (ссылка на изображение)", {"max_len": 500}),
    "client_bot_payment_purpose": ("clientbot", "Назначение перевода", {"max_len": 200}),
    "client_bot_quiet_hours_enabled": ("clientbot", "Тихие часы клиентского бота", {}),
    "client_bot_quiet_from": ("clientbot", "Тихие часы клиентов: начало", {"max_len": 8}),
    "client_bot_quiet_to": ("clientbot", "Тихие часы клиентов: конец", {"max_len": 8}),
    "client_bot_track_url": ("clientbot", "Базовый адрес страницы статуса", {"max_len": 300}),
    "client_bot_marketing_enabled": ("clientbot", "Рассылки по согласию", {}),
    "client_bot_templates": ("clientbot", "Шаблоны ответов клиентского бота", {"advanced": True}),
    "client_bot_update_offset": ("clientbot", "Offset клиентского бота", {"advanced": True}),
    "reply_templates": ("clientbot", "Шаблоны ответов оператора", {"advanced": True}),
    "feedback_delay_days": ("clientbot", "Запрос обратной связи через, дней", {"min": 0, "max": 60}),
    # --- интерфейс
    "ui_density": ("interface", "Плотность интерфейса (служебное)",
                   {"choices": ("normal", "compact"), "advanced": True}),
    "ui_start_view": ("interface", "Стартовый раздел (служебное)", {"advanced": True, "max_len": 32}),
    # --- система, обновления, диагностика
    "demo_printer_enabled": ("system", "Демо-принтер включён", {"advanced": True}),
    "demo_speed": ("system", "Скорость демо-режима", {"advanced": True, "min": 0.1, "max": 10}),
    "auto_backup_days": ("system", "Автобэкап: раз в N дней", {"min": 1, "max": 90}),
    "update_check_enabled": ("system", "Проверять обновления", {"advanced": True}),
    "auto_update_enabled": ("system", "Автообновление", {"advanced": True}),
    "update_check_hours": ("system", "Период проверки обновлений, ч", {"advanced": True, "min": 1, "max": 168}),
    "update_branch": ("system", "Ветка обновлений", {"advanced": True, "max_len": 32}),
    "update_seen_sha": ("system", "Последняя показанная версия", {"advanced": True, "max_len": 64}),
    "installed_sha": ("system", "Установленная версия (SHA)", {"advanced": True, "max_len": 64}),
    "last_update_at": ("system", "Время последнего обновления", {"advanced": True, "max_len": 32}),
    "settings_profiles": ("system", "Профили настроек (снапшоты)", {"advanced": True}),
    "month_close": ("system", "Состояние закрытий месяцев", {"advanced": True}),
    "debug_verbose": ("system", "Подробный журнал (debug)", {"advanced": True}),
})

TYPES = {"bool": bool, "int": int, "float": float, "str": str}


def kind_of(value: Any) -> str:
    """Тип ключа по его значению по умолчанию."""
    if isinstance(value, BOOL_LIKE):
        return "bool"
    if isinstance(value, INT_LIKE):
        return "int"
    if isinstance(value, FLOAT_LIKE):
        return "float"
    if isinstance(value, (list, tuple, dict)):
        return "json"
    return "str"


def schema() -> dict[str, dict]:
    """Полная схема: ключ → тип, группа, подпись, ограничения, секретность."""
    out: dict[str, dict] = {}
    for key, default in DEFAULT_SETTINGS.items():
        meta = META.get(key) or {}
        group = meta[0] if meta else "system"
        label = meta[1] if meta and len(meta) > 1 else key.replace("_", " ")
        limits = dict(meta[2]) if meta and len(meta) > 2 else {}
        out[key] = {
            "key": key,
            "type": kind_of(default),
            "default": default,
            "group": group,
            "label": label,
            "secret": bool(limits.get("secret")) or key in SECRET_SETTINGS,
            "advanced": bool(limits.get("advanced")) or key in TECHNICAL_KEYS,
            "hint": limits.get("hint"),
            "min": limits.get("min"),
            "max": limits.get("max"),
            "max_len": limits.get("max_len"),
            "choices": list(limits["choices"]) if limits.get("choices") else None,
        }
    return out


_SCHEMA: dict[str, dict] | None = None


def get_schema() -> dict[str, dict]:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = schema()
    return _SCHEMA


def describe() -> dict:
    """Ответ `/api/settings/schema`: схема без значений по умолчанию-секретов."""
    spec = get_schema()
    groups: dict[str, list[dict]] = {}
    for key, item in spec.items():
        row = dict(item)
        row.pop("default", None)
        if row["secret"]:
            row.pop("label", None) or None
            row["label"] = item["label"]
            row["hidden"] = True
        groups.setdefault(item["group"], []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda row: row["key"])
    return {
        "groups": [{"id": gid, "title": GROUPS.get(gid, gid)} for gid in GROUPS
                   if gid in groups] +
                  [{"id": gid, "title": gid} for gid in sorted(groups) if gid not in GROUPS],
        "fields": groups,
        "count": len(spec),
    }


def _coerce(item: dict, value: Any) -> tuple[Any, str | None]:
    """Привести значение к типу ключа. Возвращает (значение, ошибка)."""
    kind = item["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, (int, float)):
            return bool(value), None
        text = str(value or "").strip().lower()
        if text in ("1", "true", "on", "yes", "да", "y"):
            return True, None
        if text in ("0", "false", "off", "no", "нет", "n", ""):
            return False, None
        return False, f"{item['key']}: ожидался флаг, получено {value!r}"
    if kind in ("int", "float"):
        try:
            number = float(str(value).replace(",", ".").strip())
        except (TypeError, ValueError):
            default = item["default"]
            return default, f"{item['key']}: ожидалось число, получено {value!r}"
        if item.get("min") is not None and number < item["min"]:
            return item["min"], f"{item['key']}: значение ниже минимума"
        if item.get("max") is not None and number > item["max"]:
            return item["max"], f"{item['key']}: значение выше максимума"
        return int(number) if kind == "int" else number, None
    if kind == "json":
        if isinstance(value, (list, dict)):
            return value, None
        return item["default"], f"{item['key']}: ожидался список или объект"
    text = "" if value is None else str(value)
    if item.get("max_len") and len(text) > int(item["max_len"]):
        return text[:int(item["max_len"])], f"{item['key']}: значение длиннее допустимого"
    choices = item.get("choices")
    if choices and text and text not in choices:
        return item["default"], f"{item['key']}: недопустимое значение {text!r}"
    return text, None


def validate(patch: dict) -> tuple[dict, list[str], list[str]]:
    """Проверить входящий патч настроек.

    Возвращает ``(чистый_патч, предупреждения, отброшенные_ключи)``.
    Неизвестные ключи в базу не пишутся: раньше опечатка в имени создавала
    настройку-призрак, которую невозможно было увидеть в форме.
    """
    spec = get_schema()
    clean: dict[str, Any] = {}
    warnings: list[str] = []
    unknown: list[str] = []
    if not isinstance(patch, dict):
        return clean, ["Ожидался объект настроек"], unknown
    for key, value in patch.items():
        item = spec.get(key)
        if item is None:
            unknown.append(str(key))
            continue
        coerced, error = _coerce(item, value)
        if error:
            warnings.append(error)
        clean[key] = coerced
    return clean, warnings, unknown


def diff_defaults(current: dict) -> list[dict]:
    """Что изменено относительно заводских значений (для диагностики)."""
    spec = get_schema()
    changed = []
    for key, item in spec.items():
        if key not in current:
            continue
        if item["secret"]:
            continue
        if current[key] != item["default"]:
            changed.append({"key": key, "label": item["label"],
                            "group": item["group"], "value": current[key],
                            "default": item["default"]})
    changed.sort(key=lambda row: (row["group"], row["key"]))
    return changed


def unknown_keys(patch: dict) -> list[str]:
    """Только список неизвестных ключей — для тестов и диагностики."""
    spec = get_schema()
    return sorted(str(key) for key in (patch or {}) if key not in spec)
