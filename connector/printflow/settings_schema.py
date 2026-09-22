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
    "slicer_engine_available", "slicer_first_print_verified",
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
    "studio_gateway_host": ("printers", "Шлюз Studio: адрес в сети", {"max_len": 45}),
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
    "cashier_shelf_only": ("cashier", "Касса: показывать только товары со стеллажа",
                          {"hint": "Включено — в кассе видны только товары, "
                                   "лежащие на стеллаже: что на полке, то и "
                                   "продаётся. Выключите, чтобы вернуть единый "
                                   "каталог: товар со склада тоже виден кассиру "
                                   "и при продаже приезжает на полку."}),
    "cashier_ring": ("cashier", "Звенеть о платежах на кассе",
                     {"hint": "Вибрация и звук на телефоне кассира, когда "
                              "платёж подтверждён или пришёл из банка. "
                              "Баннер и счётчик остаются в любом случае."}),
    # 17.0.13: панель показывает список касс, но за молчанием телефона следит
    # сторож — сообщением в Telegram. 0 = выключено: ночью телефон уносят домой.
    "cashier_offline_alert_min": ("cashier", "Сообщать, если касса молчит (минут)",
                                  {"min": 0, "max": 240, "hint": "0 — не сообщать. "
                                   "Сообщение придёт один раз за обрыв, когда "
                                   "касса не отвечает дольше указанного времени."}),
    # --- оплата картой через терминал (18.0, касса)
    "cashier_card_account_id": ("cashier", "Касса: счёт для оплаты картой",
                                {"max_len": 32,
                                 "hint": "Куда пишется выручка по банковскому терминалу. "
                                         "По умолчанию — счёт «Карта». Сверка наличных "
                                         "эти деньги не ждёт: они не в ящике. Оплата "
                                         "картой на кассе ещё не включена."}),
    # --- СБП (Касса 16.0)
    "sbp_enabled": ("sbp", "Приём СБП включён", {}),
    "sbp_account_id": ("sbp", "Счёт для СБП", {"max_len": 32}),
    "sbp_shop_qr": ("sbp", "Статический QR магазина (СБП)", {"max_len": 300}),
    "sbp_bank_name": ("sbp", "Название банка СБП", {"max_len": 80}),
    "sbp_phone": ("sbp", "СБП: телефон для перевода", {"max_len": 32}),
    "sbp_recipient": ("sbp", "СБП: имя получателя", {"max_len": 160}),
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
    "default_location": ("storage", "Место хранения по умолчанию",
                         {"choices": ("shop", "home", "dry", "other")}),
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
                            {"choices": ("confirm", "queue", "printer", "autostart")}),
    "studio_gateway_autostart": ("printers", "Шлюз Studio: автозапуск", {}),
    "studio_gateway_serial": ("printers", "Шлюз Studio: серийный номер", {"advanced": True, "max_len": 64}),
    "studio_gateway_printer_id": ("printers", "Шлюз Studio: привязанный принтер", {"advanced": True, "max_len": 64}),
    "studio_relay_enabled": ("printers", "Шлюз Studio: ретранслятор станков", {}),
    "studio_relay_targets": ("printers", "Шлюз Studio: адреса компьютеров со Studio", {"max_len": 400}),
    "slicer_bin": ("printers", "Путь к слайсеру", {"advanced": True, "max_len": 400}),
    "slicer_profile_path": ("printers", "Путь к профилю CuraEngine", {"advanced": True, "max_len": 400}),
    "slicer_provider": ("printers", "Движок слайсинга",
                        {"choices": ("external", "printflow")}),
    "slicer_profile": ("printers", "Профиль нарезки", {"max_len": 100}),
    "slicer_layer_height": ("printers", "Высота слоя, мм", {"min": 0.04, "max": 1.0}),
    "slicer_first_layer_height": ("printers", "Высота первого слоя, мм", {"min": 0.04, "max": 1.0}),
    "slicer_walls": ("printers", "Количество стенок", {"min": 1, "max": 20}),
    "slicer_infill_percent": ("printers", "Заполнение, %", {"min": 0, "max": 100}),
    "slicer_infill_pattern": ("printers", "Тип заполнения", {"choices": ("grid", "lines", "triangles", "gyroid")}),
    "slicer_supports": ("printers", "Поддержки", {}),
    "slicer_brim": ("printers", "Brim", {}),
    "slicer_brim_width": ("printers", "Ширина brim, мм", {"min": 0, "max": 30}),
    "slicer_nozzle_mm": ("printers", "Диаметр сопла, мм", {"min": 0.2, "max": 1.2}),
    "slicer_nozzle_temp": ("printers", "Температура сопла, °C", {"min": 0, "max": 350}),
    "slicer_bed_temp": ("printers", "Температура стола, °C", {"min": 0, "max": 130}),
    "slicer_speed_mm_s": ("printers", "Скорость печати, мм/с", {"min": 1, "max": 500}),
    "slicer_material": ("printers", "Материал слайсера", {"max_len": 40}),
    "slicer_ams_slot": ("printers", "AMS: слот слайсера", {"max_len": 20}),
    "slicer_auto_postprocess_farmloop": ("printers", "Слайсер: постобработка FarmLoop", {}),
    "slicer_watch_auto_slice": ("printers", "Watch Folder: автоматически слайсить", {}),
    "slicer_watch_auto_queue": ("printers", "Watch Folder: ставить в очередь", {}),
    "slicer_mode": ("printers", "Слайсер: режим", {"choices": ("manual", "auto")}),
    "slicer_engine_available": ("printers", "Слайсер: движок доступен", {}),
    "slicer_first_print_verified": ("printers", "Слайсер: первый прогон принят", {}),
    "slicer_auto_enqueue": ("printers", "Слайсер: ставить в очередь автоматически", {}),
    "slicer_auto_print": ("printers", "Слайсер: печать без оператора", {}),
    "slicer_max_cycles": ("printers", "Слайсер: максимум повторов", {"min": 1, "max": 100}),
    "slicer_extrusion_width_mm": ("printers", "Ширина экструзии, мм", {"min": 0, "max": 2}),
    "slicer_top_solid_layers": ("printers", "Сплошных слоёв сверху", {"min": 0, "max": 20}),
    "slicer_bottom_solid_layers": ("printers", "Сплошных слоёв снизу", {"min": 0, "max": 20}),
    "slicer_seam": ("printers", "Положение шва", {"choices": ("nearest", "aligned")}),
    "slicer_retract_mm": ("printers", "Ретракт, мм", {"min": 0, "max": 5}),
    "slicer_retract_speed_mm_s": ("printers", "Скорость ретракта, мм/с", {"min": 1, "max": 200}),
    "slicer_retract_min_travel_mm": ("printers", "Ретракт от переезда, мм", {"min": 0, "max": 50}),
    "slicer_zhop_mm": ("printers", "Подъём по Z, мм", {"min": 0, "max": 5}),
    "slicer_fan_percent": ("printers", "Обдув, %", {"min": 0, "max": 100}),
    "slicer_support_spacing_mm": ("printers", "Шаг поддержек, мм", {"min": 0.5, "max": 10}),
    "slicer_travel_speed_mm_s": ("printers", "Скорость переездов, мм/с", {"min": 10, "max": 500}),
    "slicer_flow": ("printers", "Поток, коэффициент", {"min": 0.5, "max": 1.5}),
    "slicer_center_model": ("printers", "Слайсер: центрировать модель", {}),
    "farmloop_profile": ("printers", "FarmLoop: профиль", {"max_len": 100}),
    "farmloop_mechanics_verified": ("printers", "FarmLoop: механика проверена", {}),
    "farmloop_template_verified": ("printers", "FarmLoop: шаблон проверен", {}),
    "farmloop_sensor_mode": ("printers", "FarmLoop: подтверждение пустой платформы", {"choices": ("manual", "sensor", "camera", "both")}),
    "farmloop_sensor_timeout_s": ("printers", "FarmLoop: таймаут подтверждения, с", {"min": 1, "max": 600}),
    "farmloop_camera_threshold_pct": ("printers", "FarmLoop: порог камеры, %", {"min": 0.1, "max": 100}),
    "farmloop_cooldown_s": ("printers", "FarmLoop: охлаждение, с", {"min": 0, "max": 3600}),
    "farmloop_pusher_enabled": ("printers", "FarmLoop: толкатель установлен", {}),
    "farmloop_bender_enabled": ("printers", "FarmLoop: изгибатель установлен", {}),
    "farmloop_auto_next": ("printers", "FarmLoop: следующий цикл автоматически", {}),
    "farmloop_unattended_series": ("printers", "FarmLoop: бесконтрольная серия", {}),
    "farmloop_max_cycles": ("printers", "FarmLoop: максимум циклов", {"min": 1, "max": 1000}),
    "farmloop_max_detach_attempts": ("printers", "FarmLoop: попытки снятия", {"min": 1, "max": 5}),
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
    "evening_chart_time": ("telegram", "Вечерний отчёт-картинка: время", {"max_len": 8}),
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
    "browser_notify_enabled": ("telegram", "Уведомления в браузере", {}),
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
    # --- локальный помощник (18.13): внешний рантайм, вывод — черновик
    "assistant_enabled": ("system", "Локальный помощник включён", {}),
    "assistant_url": ("system", "Адрес рантайма помощника", {"max_len": 200}),
    "assistant_model": ("system", "Модель помощника", {"max_len": 120}),
    "assistant_timeout_sec": ("system", "Ожидание ответа модели, с",
                              {"min": 5, "max": 600, "advanced": True}),
    "assistant_speech_enabled": ("system", "Голосовой вход включён", {}),
    "assistant_speech_url": ("system", "Адрес рантайма речи", {"max_len": 200}),
    "assistant_speech_model": ("system", "Модель речи", {"advanced": True, "max_len": 120}),
    "assistant_speech_timeout_sec": ("system", "Ожидание расшифровки, с",
                                     {"min": 3, "max": 300, "advanced": True}),
    "assistant_agent_enabled": ("system", "Агент компьютера включён", {}),
    "assistant_agent_url": ("system", "Адрес агента компьютера", {"max_len": 200}),
})

TYPES = {"bool": bool, "int": int, "float": float, "str": str}


# Описания настроек (18.6.3): у каждого ключа панели есть пояснение,
# которое видно рядом с полем во вкладках настроек и в «Все настройки».
# Заполняются по просьбе владельца: «сделать описания для настроек для всех».
# Подсказка из META (limits.hint) приоритетнее — здесь только дополнение.
HINTS: dict[str, str] = {
    "accent": "Акцентный цвет интерфейса: подсветка кнопок, графиков и активных вкладок.",
    "acquiring_fee": "Комиссия эквайринга/СБП в процентах — вычитается из выручки в отчётах.",
    "allocate_fixed_costs": "Распределять аренду, связь и другие постоянные расходы на себестоимость заказов.",
    "amortization_per_hour": "Износ станка в час: стоимость парка, делённая на ресурс часов.",
    "ams_auto_map": "Автоматически сопоставлять слоты AMS с материалами заданий.",
    "ams_auto_spools": "Создавать катушки на складе автоматически по данным AMS.",
    "ams_delta_e_threshold": "Порог различия цвета (ΔE): дальше — слот считается «другим цветом» при маппинге.",
    "ams_sync_remaining": "Синхронизировать остаток катушки со складом по данным AMS (для катушек с галочкой ⟳).",
    "assistant_enabled": "Локальный помощник: предлагает значения для полей, которые не разобрал парсер входящего сообщения, и черновик ответа клиенту. Ничего не сохраняет и не трогает принтеры — фактом предложение становится только после вашего «Сохранить». Модель крутится на этом же компьютере во внешней программе (например, Ollama): в репозитории и в требованиях Python её нет.",
    "assistant_model": "Имя модели, которое отдаёт рантайм (например, qwen2.5:3b). Пусто — помощник выключен: веса моделей ставит владелец, и «из коробки» здесь нечего запускать. Какие модели уже скачаны — показывает кнопка «Проверить» рядом.",
    "assistant_timeout_sec": "Сколько секунд ждать ответа модели. Локальная модель на 8 ГБ видеопамяти отвечает десятками секунд: короткое значение обрывает полезный ответ, длинное держит запрос открытым.",
    "assistant_agent_enabled": "Агент компьютера: внешняя программа, которая слушает стоп-слово, смотрит в активное окно и делает в нём действия. PrintFlow хранит только адрес агента и его статус — зависимости агента живут в папке agent/ и в окружение коннектора не ставятся.",
    "assistant_agent_url": "Адрес агента компьютера на этом компьютере (по умолчанию 127.0.0.1:8799). Принимается только loopback.",
    "assistant_speech_enabled": "Голосовой вход: запись с микрофона уходит в рантайм речи на этом компьютере и возвращается текстом. Запись не покидает машину и не пишется в базу.",
    "assistant_speech_model": "Имя модели речи — только для показа в диагностике: рантайм сам решает, чем распознавать.",
    "assistant_speech_timeout_sec": "Сколько секунд ждать расшифровку фразы. Распознавание идёт на процессоре, поэтому секунды, а не минуты.",
    "assistant_speech_url": "Адрес рантайма речи на этом компьютере. Принимается только loopback: звук цеха не должен уезжать к вендору.",
    "assistant_url": "Адрес рантайма на этом компьютере. Принимается только loopback (127.0.0.1 или localhost): текст сообщения клиента содержит телефон и сумму, и уезжать за пределы машины он не должен. Ollama по умолчанию слушает 127.0.0.1:11434.",
    "auto_accounting": "Проводки (себестоимость, доход) считаются автоматически по факту печати.",
    "auto_backup_days": "Раз в сколько дней делать полную копию базы автоматически.",
    "auto_consume_filament": "По завершении печати списывать фактический вес пластика с катушки из AMS.",
    "auto_income_on_done": "Записывать доход сразу при завершении заказа — без ручной проводки.",
    "auto_link_orders": "Печати привязываются к заказам сами: по имени файла и номеру заказа.",
    "auto_queue": "Следующее задание стартует само, когда принтер освободился. Работает вместе с предохранителем сбоев.",
    "auto_resume_max_delay_minutes": "Не возобновлять печать, если она простояла дольше этого срока: пластик уже остыл.",
    "auto_resume_paused": "Продолжать печать после сбоя питания, если принтер сообщил про восстановление. Ручную паузу не трогает.",
    "auto_update_enabled": "Ставить обновления автоматически, когда принтеры свободны, и перезапускаться.",
    "backup_auto_export": "Регулярно выгружать копию базы в выбранную папку — страховка на случай поломки диска.",
    "backup_keep": "Сколько последних копий базы хранить; старые удаляются автоматически.",
    "bank_rules": "Правила разнесения банковской выписки: match — регулярное выражение, kind — доход/расход, category — статья.",
    "bed_watch_enabled": "Контроль «деталь на столе»: камера проверяет, что стол пуст перед новым заданием.",
    "bed_watch_threshold": "Насколько сильно должен измениться кадр стола, чтобы сторож решил: деталь ещё там.",
    "bulk_discount_10": "Скидка при заказе от 10 одинаковых штук.",
    "bulk_discount_50": "Скидка при заказе от 50 одинаковых штук.",
    "camera_fps_max": "Ограничение кадров камеры: ниже — меньше нагрузка на сеть и коннектор. 0 — без ограничения.",
    "camera_keep_shots": "Сколько кадров камеры хранить: старые чистятся автоматически.",
    "camera_roi_center": "Центральная зона кадра в процентах — где камера ищет деталь и спагетти.",
    "camera_timelapse_interval": "Интервал кадров таймлапса печати.",
    "cashier_code": "Код входа в мобильную кассу. Вводится кассиром на телефоне один раз.",
    "cashier_shift_mode": "auto — смена открывается и закрывается сама по времени; manual — кассир управляет сменой кнопками.",
    "client_bot_catalog": "Показывать каталог товаров прямо в боте с кнопкой заказа.",
    "client_bot_enabled": "Выключатель клиентского бота целиком: удобно на время отладки или отпуска.",
    "client_bot_faq": "Раздел «Вопрос-ответ» в боте: частые вопросы покупателей.",
    "client_bot_faq_materials": "Статья «Как выбрать материал» в FAQ бота.",
    "client_bot_marketing_enabled": "Рассылки по согласию: новинки и акции только тем, кто подписался.",
    "client_bot_notify": "Покупателю приходят статусы заказа: принят, печатается, готов.",
    "client_bot_pay_info": "Реквизиты оплаты, показываемые покупателю в боте.",
    "client_bot_pay_qr": "Ссылка на картинку QR СБП для оплаты в боте.",
    "client_bot_payment_purpose": "Назначение платежа для покупателя: подставляется номер заказа.",
    "client_bot_pickup_days": "Сколько дней напоминать забрать заказ в статусе «Готов».",
    "client_bot_pickup_info": "Текст «Как получить заказ»: адрес, часы работы, контакт.",
    "client_bot_quiet_from": "Начало тихих часов клиентов, ЧЧ:ММ.",
    "client_bot_quiet_hours_enabled": "Тихие часы для покупателей: не беспокоить сообщениями ночью.",
    "client_bot_quiet_to": "Конец тихих часов клиентов, ЧЧ:ММ.",
    "client_bot_ready_photo": "Отправлять фото готового заказа — покупатель видит, что забирает.",
    "client_bot_review": "После выдачи заказа бот просит оставить отзыв.",
    "client_bot_templates": "Служебное: шаблоны сообщений клиентского бота (JSON).",
    "client_bot_token": "Токен клиентского бота для покупателей: витрина, заказы, статусы в Telegram.",
    "client_bot_track_url": "Базовый адрес страницы «Статус заказа» — бот присылает ссылку на неё.",
    "client_bot_update_offset": "Служебное: offset long-polling клиентского бота.",
    "client_bot_welcome": "Текст приветствия покупателя по /start в клиентском боте.",
    "cloud_email": "Почта аккаунта Bambu Cloud — для управления принтером, когда LAN Only Mode не включён.",
    "cloud_history_sync": "Подтягивать историю печатей из облака Bambu, даже если задание стартовали с флешки.",
    "cloud_region": "Регион облака Bambu: global для России и Европы, china — для устройств из CN-склада.",
    "cloud_sync_minutes": "Как часто опрашивать облако Bambu (в минутах).",
    "cloud_token": "Токен Bambu Cloud — вводится один раз, хранится только на вашем компьютере.",
    "cloud_uid": "UID пользователя Bambu Cloud — подставляется автоматически после входа.",
    "company_name": "Короткое название компании для заголовков и документов.",
    "count_labor_in_cost": "Добавлять работу оператора в себестоимость каждого заказа.",
    "currency": "Знак валюты для всей панели, кассы и витрины: ₽, $, €, ₸.",
    "debt_alert_days": "Через сколько дней неоплаченный заказ считается проблемным и попадает в «Долги».",
    "debt_reminder_cooldown_days": "Как часто напоминать клиенту о долге повторно, чтобы не спамить.",
    "debug_verbose": "Подробный журнал debug в connector.log — включайте по просьбе поддержки.",
    "digest_last": "Служебное: когда последний раз уходил дневной дайджест.",
    "weekly_last": "Служебное: когда последний раз уходил еженедельный отчёт.",
    "evening_chart_last": "Служебное: когда последний раз уходила вечерняя картинка-отчёт.",
    "link_stale_mqtt": "Служебное: порог «связь с принтером устарела» по MQTT (в секундах).",
    "link_stale_http": "Служебное: порог устаревания связи по HTTP (в секундах).",
    "link_stale_ftps": "Служебное: порог устаревания связи по FTPS (в секундах).",
    "link_stale_camera": "Служебное: порог устаревания кадров камеры (в секундах).",
    "default_account": "Куда по умолчанию попадают деньги: наличные, карта, счёт.",
    "default_markup": "Наценка к себестоимости по умолчанию — применяется к новым товарам и расчётам.",
    "default_spool_price": "Цена новой катушки по умолчанию — подставляется при приходе пластика.",
    "default_spool_weight": "Вес полной катушки по умолчанию, грамм: 1000, 750, 500.",
    "default_location": "Куда по умолчанию попадают новые катушки: магазин/склад, дом, сушильный шкаф или другое место.",
    "delivery_cost": "Базовая стоимость доставки, добавляемая в заказ кнопкой «Доставка».",
    "demo_printer_enabled": "Виртуальный демо-принтер: показывает, как выглядит печать, без реального станка.",
    "demo_speed": "Скорость демо-принтера: 1 — как настоящий, больше — быстрее для витрины.",
    "design_rate": "Стоимость часа работы дизайнера/моделлера для заявок на дизайн.",
    "digest_time": "Время дневного дайджеста: сводка дня по печати и деньгам одним сообщением.",
    "dry_humidity_threshold": "Влажность AMS выше порога — панель напомнит просушить пластик.",
    "ejector_enabled": "Авто-эжектор: после завершения деталь снимается со стола автоматически (с доработкой станка).",
    "encrypt_access_code": "Хранить access-коды принтеров зашифрованными. Выключайте только для отладки MQTT вручную.",
    "energy_price": "Цена электричества за кВт·ч по вашему счётчику.",
    "envelope_auto": "Автоматически подбирать конфигурацию задания под принтер и AMS.",
    "evening_chart_time": "Время вечернего отчёта-картинки с графиками дня.",
    "failure_rate": "Ожидаемый процент брака: закладывается в плановое время и себестоимость.",
    "farmloop_auto_next": "Запускать следующий цикл конвейера автоматически.",
    "farmloop_bender_enabled": "Изгибатель установлен: детали отгибаются от стола после печати.",
    "farmloop_camera_threshold_pct": "Насколько (%) должен опустеть кадр платформы, чтобы цикл продолжился.",
    "farmloop_cooldown_s": "Пауза между циклами конвейера в секундах.",
    "farmloop_max_cycles": "Максимум циклов конвейера за серию.",
    "farmloop_max_detach_attempts": "Сколько попыток снять деталь, прежде чем позвать человека.",
    "farmloop_mechanics_verified": "Отметьте после проверки механики конвейера: толкатель и направляющие выставлены.",
    "farmloop_profile": "Профиль конвейера FarmLoop под вашу обвязку.",
    "farmloop_pusher_enabled": "Толкатель установлен: конвейер может снимать детали сам.",
    "farmloop_sensor_mode": "Как подтверждается пустая платформа: по датчику, по камере или вручную.",
    "farmloop_sensor_timeout_s": "Сколько секунд ждать подтверждения пустой платформы.",
    "farmloop_template_verified": "Отметьте после проверки шаблона серии: задание уходит в цикл корректно.",
    "farmloop_unattended_series": "Разрешить серию без присмотра: конвейер печатает, пока есть заготовки.",
    "feedback_delay_days": "Через сколько дней после выдачи спрашивать обратную связь.",
    "filament_low_threshold": "При каком остатке катушка считается «заканчивается» и попадает в уведомления и закупки.",
    "first_layer_watch_min": "Сколько минут следить за первым слоем: недолив виден на кадрах, тревога придёт сразу.",
    "fixed_costs_auto": "Начислять постоянные расходы автоматически раз в месяц, без ручных проводок.",
    "ftps_block_kb": "Размер блока передачи FTPS в килобайтах: больше — быстрее по кабелю, меньше — надёжнее по Wi-Fi.",
    "ftps_dedup": "Не заливать на принтер файл, который уже там есть (по имени и размеру).",
    "ftps_queue": "Отправлять файлы в очередь печати по FTPS по одному, без параллельных загрузок.",
    "ftps_retries": "Сколько раз повторять загрузку файла на принтер при обрыве.",
    "ftps_timeout": "Таймаут FTPS-операций в секундах: за это время файл должен начать передаваться.",
    "goal_profit_month": "Цель по чистой прибыли в месяц: от неё считается прогресс «Цель месяца» на дашборде.",
    "guard_cold_minutes": "Порог холодного старта: сопло/стол холодеют при активной печати — признак сбоя питания.",
    "guard_cost_limit": "Предупреждать, когда потери на брак превысили сумму. 0 — без лимита.",
    "guard_count_loss": "Учитывать потери пластика при браке в отчётах и себестоимости.",
    "guard_enabled": "Сторож печати: следит за температурой, прогрессом и камерой, ловит брак раньше вас.",
    "guard_overrun_pct": "Если печать идёт дольше плана на этот процент — сторож сообщит.",
    "guard_pause_on_error": "При ошибке/HMS ставить печать на паузу вместо продолжения.",
    "guard_pause_severity": "Насколько серьёзная проблема нужна для автоматической паузы: только критичные или любые.",
    "guard_snapshot": "Сохранять кадр с камеры при каждой ошибке — потом видно, что случилось.",
    "guard_stall_minutes": "Если прогресс не двигается столько минут — сторож поднимает тревогу.",
    "inn": "ИНН для документов и QR платежей.",
    "installed_sha": "Служебное: SHA установленной сборки.",
    "insurance_extra_base": "С какой суммы дохода начинаются дополнительные взносы (по умолчанию 300 000 ₽).",
    "insurance_extra_cap": "Потолок дополнительных взносов — после него доп. процент не начисляется.",
    "insurance_extra_rate": "Процент дополнительных взносов с дохода сверх 300 000 ₽.",
    "insurance_fixed": "Фиксированные страховые взносы ИП «за себя» за год.",
    "insurance_reduces_tax": "Вычитать уплаченные взносы из налога (УСН 6% и НПД-вычет).",
    "keyframe_interval_min": "Как часто сохранять ключевые кадры печати для видео и контроля первого слоя.",
    "labor_rate": "Стоимость часа работы оператора: забирается в себестоимость, если включено «Учитывать труд оператора».",
    "last_update_at": "Служебное: когда установлено последнее обновление.",
    "legal_name": "Наименование для печатных документов: счёта, акта, товарного чека.",
    "low_margin_alert": "Заказы с маржой ниже этого процента подсвечиваются как малоприбыльные.",
    "maintenance_enabled": "Напоминания о техобслуживании по наработке часов каждого принтера.",
    "maintenance_per_hour": "Стоимость обслуживания станка в час: смазка, сопла, ремни — добавляется к себестоимости.",
    "min_order_price": "Заказы дешевле этой суммы автоматически поднимаются до минимума.",
    "month_close": "Служебное: какие месяцы закрыты для правок.",
    "mqtt_backoff": "Пауза между повторными подключениями MQTT: растёт при неудачах, чтобы не грузить принтер.",
    "mqtt_fallback_1883": "Если TLS :8883 недоступен — пробовать незащищённый порт 1883 (только для отладки).",
    "mqtt_keepalive": "Keepalive MQTT-соединения в секундах: как часто принтер подтверждает связь.",
    "night_reset_enabled": "Пересчитывать план очереди каждую ночь — план утром всегда актуален.",
    "night_reset_time": "Во сколько ночью пересчитывать план очереди, ЧЧ:ММ.",
    "night_shift_enabled": "Разрешать печати ночью: очередь не будет ждать утра.",
    "browser_notify_enabled": "Пока PrintFlow открыт вкладкой в браузере, события печати приходят всплывающим уведомлением сразу, не дожидаясь Telegram.",
    "notify_complete": "Сообщение, когда задание дошло до конца: имя файла, время и вес пластика.",
    "notify_error": "Коды ошибок принтера и сбои задания — чтобы реагировать, пока брак не разросся.",
    "notify_filament_low": "Остаток катушки упал ниже порога из блока «Склад и пластик».",
    "notify_finish_remind_min": "Напомнить заглянуть к принтеру за столько минут до конца печати.",
    "notify_firmware": "У принтера вышла новая прошивка — панель сообщит, что можно обновить.",
    "notify_guard": "Срабатывания защиты: перегрев, зависание, спагетти-детектор.",
    "notify_maintenance": "Регламент ТО: смазка, протяжка, чистка — по наработке часов парка.",
    "notify_pause": "Печать встала на паузу (вручную или по сторожу) — уведомление придёт сразу.",
    "notify_photo": "К уведомлениям о событиях прикладывается снимок с камеры принтера.",
    "npd_alert_time": "Во сколько приходить напоминание о чеках НПД, формат ЧЧ:ММ.",
    "npd_alerts_enabled": "Ежедневное напоминание выставить чеки НПД в приложении «Мой налог».",
    "npd_bonus_left": "Остаток налогового вычета НПД — панель подсказывает, сколько ещё можно вернуть.",
    "npd_limit": "Годовой лимит дохода по НПД. После превышения НПД недоступен — панель предупредит заранее.",
    "npd_limit_warn_pct": "За сколько процентов до лимита НПД панель начинает напоминать: «до лимита осталось N ₽».",
    "npd_rate_company": "Ставка НПД при продаже юрлицам (по умолчанию 6%) — применяется к заказам компаний и B2B.",
    "npd_rate_person": "Ставка НПД при продаже физлицам (по умолчанию 4%) — для расчёта налога с каждой продажи.",
    "packaging_cost": "Стоимость упаковки одного заказа: коробка, пакет, наполнитель.",
    "patent_cost_year": "Стоимость патента за год — раскладывается на месяц в расчётах налогов.",
    "pay_account": "Расчётный счёт получателя для ГОСТ QR.",
    "pay_bank_name": "Название банка получателя для ГОСТ QR.",
    "pay_bic": "БИК банка получателя для ГОСТ QR.",
    "pay_corr_account": "Корреспондентский счёт банка для ГОСТ QR.",
    "pay_payee_inn": "ИНН получателя платежа — обязателен для ГОСТ QR.",
    "pay_payee_kpp": "КПП получателя платежа — для юрлиц в ГОСТ QR.",
    "pay_payee_name": "Имя получателя для ГОСТ QR и квитанций.",
    "pay_qr_link": "Ссылка платёжного QR с подстановкой суммы {amount} — из приложения банка или платёжного сервиса.",
    "pay_qr_mode": "Режим платёжного QR: с суммой в QR или ГОСТ (реквизиты по стандарту ЦБ).",
    "power_kw": "Средняя мощность принтера в кВт — считается расход электроэнергии на заказ.",
    "preflight_block_bed": "Проверять, чист ли стол: деталь с прошлой печати не даст печатать новую.",
    "preflight_block_filament": "Проверять, хватит ли пластика на задание по остатку катушки.",
    "preflight_block_hms": "Блокировать старт при активных кодах ошибок HMS.",
    "preflight_block_idle": "Не запускать печать, если принтер «не проснулся» и не прошёл калибровку.",
    "preflight_block_material": "Проверять, что материал в AMS совпадает с материалом задания.",
    "preflight_enabled": "Preflight: перед стартом проверять принтер, материал и настройки — блокировать явные проблемы.",
    "preflight_warn_calibration": "Предупреждать, если принтер давно не калибровался.",
    "preflight_warn_humidity": "Предупреждать о влажном пластике: влажность AMS выше порога.",
    "preflight_warn_nozzle": "Предупреждать, если задание рассчитано на другое сопло (0.4 vs 0.6…).",
    "preflight_warn_sd": "Предупреждать, если файл лежит только на SD-карте и недоступен по сети.",
    "price_rounding": "До какой суммы округлять итоговую цену: 1 ₽, 5 ₽, 10 ₽.",
    "printer_info_sync": "Синхронизировать сведения о принтерах: версия прошивки, наработка, состояние.",
    "printer_invested_at": "Дата, когда парк начал работать на вас — точка отсчёта окупаемости.",
    "printer_investment": "Все вложения в парк (станки, AMS, обвязка). Из этой суммы считается окупаемость на дашборде.",
    "public_url": "Адрес, по которому панель доступна извне — подставляется в ссылки для клиентов и ботов.",
    "qc_checklist": "Чек-лист контроля качества перед выдачей заказа клиенту.",
    "queue_check_filament": "Очередь не поставит задание, если пластика не хватает.",
    "queue_check_material": "Очередь сверяет материал задания с тем, что в AMS.",
    "queue_group_material": "Группировать задания по материалу: меньше смен прутка — меньше продувок.",
    "queue_offline_defer": "Откладывать задание, если целевой принтер офлайн, а не вешать его в очередь.",
    "queue_smart_deadline": "Сначала задания с горящим сроком, даже если материал другой.",
    "queue_smart_group": "Умная группировка: минимизирует перенастройки с учётом сроков заказов.",
    "quiet_from": "Начало тихих часов, ЧЧ:ММ.",
    "quiet_hours_enabled": "Тихие часы: уведомления копятся и приходят утром.",
    "quiet_to": "Конец тихих часов, ЧЧ:ММ.",
    "reply_templates": "Служебное: шаблоны быстрых ответов оператора (JSON).",
    "restock_remind": "Напоминать пополнить склад, когда катушек с каким-то материалом мало.",
    "rush_surcharge": "Надбавка за срочность в процентах — включается галочкой в заказе.",
    "sbp_account_id": "Счёт (касса), на который падают деньги входящих СБП-платежей.",
    "sbp_auto_confirm": "Сверять входящие СБП с выпиской банка и подтверждать заказы автоматически.",
    "sbp_bank_name": "Название банка для реквизитов перевода СБП, показывается покупателю.",
    "sbp_enabled": "Приём платежей по QR СБП: покупателю показывается QR, оплата подтверждается по банку.",
    "sbp_hold_hours": "Сколько часов бронировать заказ после выставленного СБП-счёта, пока оплата не пришла. Потом бронь снимается.",
    "sbp_match_window_hours": "Сколько часов искать поступление по заказу: пришло позже окна — подтвердите вручную.",
    "sbp_payment_note": "Шаблон назначения платежа: подставляется номер заказа, чтобы находить оплату в выписке.",
    "sbp_phone": "Телефон, привязанный к СБП: покупатель может перевести по номеру вместо QR.",
    "sbp_purpose_limit": "Ограничение длины назначения платежа из товаров: длинные названия укорачиваются до лимита банка.",
    "sbp_recipient": "Имя получателя платежа СБП — покупатель видит его перед переводом.",
    "sbp_shop_qr": "Статический QR магазина для СБП: покупатель платит сам сумму — подходит для стойки и витрины.",
    "settings_profiles": "Служебное: снапшоты настроек для быстрого отката.",
    "shelf_low_last": "Служебное: когда в последний раз проверялись остатки стеллажа.",
    "shop_season": "Сезонное оформление публичных страниц: шапка витрины меняется к Новому году, весне или осени.",
    "shopping_runout_days": "На сколько дней вперёд планировать закупку пластика по скорости расхода.",
    "slicer_ams_slot": "Какой слот AMS использовать при слайсинге.",
    "slicer_auto_create_order": "Создавать заказ из файла, положенного в Watch Folder / загруженного на принтер.",
    "slicer_auto_enqueue": "Ставить нарезанные задания в очередь автоматически.",
    "slicer_auto_postprocess_farmloop": "Добавлять в G-code постобработку для конвейера FarmLoop.",
    "slicer_auto_print": "Запускать печать без оператора после нарезки.",
    "slicer_bed_temp": "Температура стола по умолчанию, °C.",
    "slicer_bin": "Путь к исполняемому файлу слайсера (Bambu Studio CLI / CuraEngine).",
    "slicer_bottom_solid_layers": "Сколько сплошных слоёв снизу модели.",
    "slicer_brim": "Рисовать brim (юбку-подложку) по контуру модели.",
    "slicer_brim_width": "Ширина brim, мм.",
    "slicer_center_model": "Центрировать модель на столе при слайсинге.",
    "slicer_engine_available": "Служебное: найден ли движок слайсинга на этой машине.",
    "slicer_extrusion_width_mm": "Ширина линии экструзии, мм.",
    "slicer_fan_percent": "Обдув модели в процентах по умолчанию.",
    "slicer_filename_template": "Шаблон имени файла: подставляйте {order}, {material}, {date} для авто-привязки.",
    "slicer_first_layer_height": "Высота первого слоя, мм: толще — надёжнее прилепится.",
    "slicer_first_print_verified": "Служебное: первый прогон слайсера принят владельцем.",
    "slicer_flow": "Коэффициент потока (flow) — тонкая калибровка подачи.",
    "slicer_infill_pattern": "Узор заполнения: сетка, соты, линии.",
    "slicer_infill_percent": "Заполнение в процентах.",
    "slicer_layer_height": "Высота слоя по умолчанию, мм.",
    "slicer_material": "Материал по умолчанию для слайсинга.",
    "slicer_max_cycles": "Максимум повторов нарезки при сбоях, чтобы не крутить цикл зря.",
    "slicer_mode": "Режим слайсера: ручной запуск или автоматический конвейер.",
    "slicer_nozzle_mm": "Диаметр сопла, мм — влияет на расчёт линий и времени.",
    "slicer_nozzle_temp": "Температура сопла по умолчанию, °C — если материал не задал свою.",
    "slicer_profile": "Какой профиль нарезки использовать по умолчанию.",
    "slicer_profile_path": "Путь к профилю слайсера для CuraEngine.",
    "slicer_provider": "Движок нарезки: встроенный, Bambu Studio CLI или CuraEngine.",
    "slicer_retract_min_travel_mm": "Минимальный переезд, после которого делается ретракт, мм.",
    "slicer_retract_mm": "Длина ретракта, мм — против соплей при переездах.",
    "slicer_retract_speed_mm_s": "Скорость ретракта, мм/с.",
    "slicer_seam": "Где прятать шов: по углам, сзади, по выравниванию.",
    "slicer_speed_mm_s": "Скорость печати по умолчанию, мм/с.",
    "slicer_support_spacing_mm": "Шаг поддержек, мм: реже — легче снимать.",
    "slicer_supports": "Генерировать поддержки по умолчанию.",
    "slicer_top_solid_layers": "Сколько сплошных слоёв сверху модели.",
    "slicer_travel_speed_mm_s": "Скорость холостых переездов, мм/с.",
    "slicer_walls": "Количество периметров (стенок) модели.",
    "slicer_watch_auto_queue": "Сразу ставить нарезанное задание в очередь.",
    "slicer_watch_auto_slice": "Автоматически нарезать 3MF из Watch Folder.",
    "slicer_zhop_mm": "Подъём по Z при переезде (z-hop), мм.",
    "spaghetti_enabled": "Детектор «спагетти» по камере: узнал клубок нитей — поднимет тревогу и поставит паузу.",
    "spaghetti_sensitivity": "Чувствительность детектора: выше — ловит раньше, но чаще ошибается.",
    "studio_gateway_access_code": "Код, который Bambu Studio спросит при подключении к шлюзу PrintFlow. Пусто при включении — сгенерируется сам.",
    "studio_gateway_autostart": "Поднимать шлюз Studio при запуске коннектора.",
    "studio_gateway_enabled": "Шлюз делает Studio видимой PrintFlow как принтер: Slice → Print уходит в очередь, а не напрямую на станок.",
    "studio_gateway_host": "Адрес, который шлюз объявляет в SSDP и отдаёт в ответе PASV. Пусто — первый адрес этого компьютера (192.168.* → 10.* → 172.16–31). Задайте вручную (например, 192.168.1.50), если на компьютере есть VirtualBox, Hyper-V, Docker или VPN: их виртуальные адреса уводят Studio не туда.",
    "studio_gateway_mode": "Режим шлюза: полный (с AMS и очередью) или лёгкий (только приём заданий).",
    "studio_gateway_name": "Имя виртуального принтера, под которым PrintFlow виден в Bambu Studio.",
    "studio_gateway_printer_id": "На какой реальный принтер направлять печать из Studio, если не выбран «любой свободный».",
    "studio_gateway_serial": "Серийный номер виртуального принтера для Studio — меняйте, если Studio кэширует старый.",
    "studio_relay_enabled": "PrintFlow объявляет Bambu Studio реальные станки фермы (адрес и серийный номер из вкладки «Принтеры»). Нужно, когда Studio стоит в другой подсети или VLAN и не видит принтеры сама: широковещательный SSDP станка туда не доходит, а объявление от PrintFlow — да.",
    "studio_relay_targets": "IPv4-адреса компьютеров с Bambu Studio через запятую (например, 192.168.10.5, 10.0.2.17). Каждый получает объявления шлюза и станков напрямую (UDP :2021) — работает через маршрутизатор, где broadcast не проходит. Пусто — только своя подсеть.",
    "target_profit_per_hour": "Сколько вы хотите зарабатывать за час печати станка. От этой цифры считается наценка и «Цель месяца» на дашборде.",
    "tax_mode": "Режим налогообложения: от него зависят ставки, взносы и поля расчёта налогов в разделе «Финансы».",
    "tax_rate": "Ручная ставка налога в процентах — используется только в режиме «Ручной».",
    "tax_reserve_enabled": "Резервировать долю выручки под налоги: видно, сколько денег реально можно тратить.",
    "tax_reserve_extra": "Дополнительная сумма, откладываемая под налоги ежемесячно сверх процентов.",
    "telegram_bot": "Бот сотрудников: принимает «статус», «кадр», «пауза» с телефона.",
    "telegram_bot_update_offset": "Служебное: offset long-polling бота сотрудников.",
    "telegram_chat_id": "Ваш chat_id в Telegram — куда бот присылает сообщения. Виден через @userinfobot.",
    "telegram_enabled": "Включить Telegram-уведомления о печати и заказах.",
    "telegram_quiet_from": "С какого часа сотрудникам не пишут (кроме критичных событий).",
    "telegram_quiet_to": "До какого часа длится тихий режим сотрудников.",
    "telegram_token": "Токен бота сотрудников от @BotFather: уведомления о печати и команды с телефона.",
    "telemetry_enabled": "Собирать телеметрию принтеров: температуры, мощность, прогресс — для графиков и отчётов.",
    "telemetry_keep_days": "Сколько дней хранить телеметрию, потом она сжимается и чистится.",
    "theme": "Тема панели: светлая, тёмная или как в системе.",
    "ui_density": "Служебное: плотность интерфейса (compact/normal).",
    "ui_start_view": "Служебное: раздел, который открывается при входе в панель.",
    "unattended_dangerous_actions": "Разрешить автоматике опасные шаги без человека: автозапуск очереди, нагрев, подачу пластика.",
    "update_branch": "Ветка обновлений: main — стабильная, develop — ранний доступ.",
    "update_check_enabled": "Проверять обновления PrintFlow на GitHub и сообщать о новой версии.",
    "update_check_hours": "Как часто проверять обновления, в часах (минимум раз в 10 минут).",
    "update_seen_sha": "Служебное: какая версия уже показывалась в окне «что нового».",
    "usn_income_rate": "Ставка УСН «доходы» — доля выручки, резервируемая под налог.",
    "usn_limit": "Годовой предел дохода для УСН. Приближение к пределу подсвечивается в разделе «Налоги».",
    "usn_min_tax_rate": "Минимальная ставка УСН «доходы минус расходы» в вашем регионе (обычно 1%).",
    "usn_profit_rate": "Ставка УСН «доходы минус расходы» — считается от маржи, если выбран этот режим.",
    "vat_enabled": "Отметьте, если вы плательщик НДС: в расчётах появится выделение НДС.",
    "vat_rate": "Ставка НДС в процентах для выделения из суммы.",
    "vat_threshold": "Порог освобождения от НДС: ниже лимита НДС не начисляется, выше — считается.",
    "watch_auto_action": "Что делать с найденным 3MF: только показать, слайсить или сразу ставить в очередь.",
    "watch_create_order": "Создавать черновик заказа для каждого файла из Watch Folder.",
    "watch_folder_enabled": "Watch Folder: следить за папкой и забирать 3MF, экспортированные из Bambu Studio / OrcaSlicer.",
    "watch_folder_path": "Папка для 3MF-файлов Watch Folder. Кнопки-пресеты — в настройках рядом.",
    "watch_link_order": "Привязывать файл из Watch Folder к заказу по имени файла.",
    "weekly_capacity_hours": "Сколько часов печати реально доступно в неделю — от этого считается загрузка парка и план.",
    "weekly_report_day": "День недели еженедельного отчёта.",
    "weekly_report_time": "Время еженедельного отчёта, ЧЧ:ММ.",
}


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
            "hint": limits.get("hint") or HINTS.get(key),
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
    # Cross-setting safety gates. They are validated on the complete patch only
    # when the related values are explicitly changed; the server still keeps
    # the conservative defaults in config.py.
    effective = dict(DEFAULT_SETTINGS)
    effective.update(clean)
    if effective.get("slicer_provider") not in ("external", "printflow"):
        warnings.append("slicer_provider: неизвестный движок — оставлен external")
        clean["slicer_provider"] = "external"
    # Свой движок нарезает сам, но без оператора не печатает: авто-очередь и
    # авто-печать разрешены только после принятого первого прогона.
    slicer_auto = (effective.get("slicer_mode") == "auto"
                   and effective.get("slicer_first_print_verified"))
    if effective.get("slicer_auto_enqueue") and not slicer_auto:
        warnings.append("Слайсер: авто-очередь выключена — сначала ручной режим "
                        "и принятый первый прогон")
        clean["slicer_auto_enqueue"] = False
    if effective.get("slicer_auto_print") and not (
            slicer_auto and effective.get("slicer_auto_enqueue")):
        warnings.append("Слайсер: печать без оператора выключена — нужны режим "
                        "auto и разрешённая авто-очередь")
        clean["slicer_auto_print"] = False
    if effective.get("farmloop_auto_next") and not (
        effective.get("farmloop_mechanics_verified")
        and effective.get("farmloop_template_verified")
        and effective.get("farmloop_pusher_enabled")
        and effective.get("farmloop_bender_enabled")
        and effective.get("farmloop_sensor_mode") in {"sensor", "camera", "both"}
    ):
        warnings.append("FarmLoop auto-next заблокирован: нужны проверенные механика, шаблон, толкатель, изгибатель и датчик/камера")
        clean["farmloop_auto_next"] = False
    if effective.get("farmloop_unattended_series") and not (
        effective.get("farmloop_auto_next")
        and int(effective.get("farmloop_max_cycles") or 0) > 1
    ):
        warnings.append("Бесконтрольная серия заблокирована: сначала нужен разрешённый auto-next и лимит больше одного цикла")
        clean["farmloop_unattended_series"] = False
    if effective.get("slicer_auto_postprocess_farmloop") and not effective.get("farmloop_template_verified"):
        warnings.append("Автопостобработка FarmLoop выключена: шаблон не подтверждён")
        clean["slicer_auto_postprocess_farmloop"] = False
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
