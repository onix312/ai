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
GROUPS = {
    "workshop": "Цех и производство",
    "money": "Деньги и тарифы",
    "tax": "Налоги и режим",
    "printers": "Принтеры и Bambu",
    "telegram": "Telegram и уведомления",
    "clientbot": "Клиентский бот и витрина",
    "automation": "Автоматизация и правила",
    "storage": "Склад и материалы",
    "documents": "Документы и реквизиты",
    "interface": "Интерфейс",
    "system": "Система и данные",
}

# Ключ → (группа, подпись, ограничения). Ограничения: min/max для чисел,
# choices для строк-перечислений.
META: dict[str, dict] = {
    # --- деньги и тарифы
    "target_profit_per_hour": ("money", "Целевая прибыль в час", {"min": 0, "max": 100000}),
    "electricity_rate": ("money", "Тариф электроэнергии, ₽/кВт·ч", {"min": 0, "max": 100}),
    "machine_cost_per_hour": ("money", "Амортизация станка, ₽/час", {"min": 0, "max": 100000}),
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
    "usn_limit": ("tax", "Предел применения УСН, ₽", {"min": 0}),
    "vat_threshold": ("tax", "Порог освобождения от НДС, ₽", {"min": 0}),
    # --- принтеры
    "printer_investment": ("printers", "Вложения в парк, ₽", {"min": 0}),
    "camera_fps_max": ("printers", "Предел FPS камеры (0 = без предела)",
                       {"min": 0, "max": 30}),
    "encrypt_access_code": ("printers", "Шифровать access-коды принтеров", {}),
    "auto_start_next": ("printers", "Автозапуск следующего задания", {}),
    "auto_resume": ("printers", "Авто-возобновление после сбоя", {}),
    # --- telegram
    "telegram_token": ("telegram", "Токен бота сотрудников", {"secret": True}),
    "telegram_chat_id": ("telegram", "Chat ID владельца", {"max_len": 64}),
    "client_bot_token": ("clientbot", "Токен клиентского бота", {"secret": True}),
    "client_bot_enabled": ("clientbot", "Клиентский бот включён", {}),
    "client_quiet_from": ("clientbot", "Тихие часы: начало", {"max_len": 8}),
    "client_quiet_to": ("clientbot", "Тихие часы: конец", {"max_len": 8}),
    # --- облако и шлюз
    "cloud_email": ("printers", "Аккаунт Bambu Cloud", {"max_len": 128}),
    "cloud_region": ("printers", "Регион Bambu Cloud", {"choices": ("global", "china")}),
    "cloud_token": ("printers", "Токен облака", {"secret": True}),
    "cloud_uid": ("printers", "UID облака", {"secret": True}),
    "studio_gateway_access_code": ("printers", "Access Code шлюза Studio", {"secret": True}),
    "studio_gateway_enabled": ("printers", "Шлюз Bambu Studio включён", {}),
    # --- склад
    "target_stock_days": ("storage", "Целевой запас, дней", {"min": 0, "max": 365}),
    # --- документы
    "legal_name": ("documents", "Наименование для документов", {"max_len": 200}),
    "inn": ("documents", "ИНН", {"max_len": 32}),
    # --- интерфейс
    "theme": ("interface", "Тема оформления", {"choices": ("system", "light", "dark")}),
    "accent": ("interface", "Акцентный цвет", {"max_len": 32}),
    "density": ("interface", "Плотность интерфейса",
                {"choices": ("desk", "compact", "shop")}),
    # --- система
    "public_url": ("system", "Публичный адрес панели", {"max_len": 300}),
    "backup_keep": ("system", "Сколько бэкапов хранить", {"min": 1, "max": 200}),
    "backup_auto_export": ("system", "Автоэкспорт бэкапов", {}),
    "lan_mode": ("system", "Доступ из локальной сети", {}),
}

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
