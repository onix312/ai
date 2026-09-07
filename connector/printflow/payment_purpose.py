"""Назначение платежа из состава корзины (Склад+СБП 18.0, итерация 1).

Зачем модуль. Банковское назначение ``Продажа на кассе · 3 поз.`` ничего не
говорит владельцу: какой товар оплатили — видно только в журнале кассы.
Модуль собирает короткое ``payment_purpose`` для банка и QR **на сервере**
из серверных данных (названия из каталога/заказа, а не от клиента):

``NOZZA: Органайзер × 1; Адресник × 2; Брелок × 1``

Правила (зафиксированы в ТЗ):

* названия и количества — только серверные; клиентские игнорируются;
* управляющие символы, переносы, HTML — вычищаются, пробелы нормализуются;
* обрезка — только целыми позициями (``+N товара``), никогда посередине
  названия; один непомещающийся товар режется по границе слов с ``…``;
* лимит длины — настройка ``sbp_purpose_limit`` (по умолчанию 140);
* полный состав всегда хранится отдельно (``cashier_sales.items``,
  ``sbp_payments.items``) — лимит банка состав не теряет;
* никаких персональных данных, токенов и секретов — только названия и штуки.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

DEFAULT_LIMIT = 140
MULT = "×"

_SPACES = re.compile(r"\s+")


def sanitize(text: Any, limit: int = 0) -> str:
    """Чистое название: без управляющих символов, переносов и мусора.

    Срез по символам (не по байтам) — Unicode-символ не режется пополам.
    HTML-теги не вырезаются специально: ``<`` и ``>`` остаются текстом,
    экранирование — на стороне отображения (фронт и так экранирует).
    """
    raw = str(text or "")
    # Управляющие символы (\\x00-\\x1f без пробельных, \\x7f-\\x9f) — вон,
    # пробельные схлопнутся в один пробел ниже.
    cleaned = "".join(ch for ch in raw
                        if ch in " \t\n\r\f\v" or unicodedata.category(ch) != "Cc")
    cleaned = _SPACES.sub(" ", cleaned).strip()
    if limit and len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip()
    return cleaned


def format_qty(qty: Any) -> str:
    """Количество одной позицией: целые — без дроби, дробные — компактно."""
    try:
        value = float(qty)
    except (TypeError, ValueError):
        return "1"
    if value == int(value):
        return str(int(value))
    return ("%.3f" % value).rstrip("0").rstrip(".")


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русская plural-форма: 1 товар, 3 товара, 5 товаров."""
    count = abs(int(count)) % 100
    tens = count % 10
    if 11 <= count <= 14:
        return many
    if tens == 1:
        return one
    if 2 <= tens <= 4:
        return few
    return many


def _cut_word(text: str, limit: int) -> str:
    """Аккуратный срез длинного названия: по границе слов, если разумно."""
    if len(text) <= limit or limit <= 0:
        return text[:max(limit, 0)]
    if limit <= 8:
        return text[:limit]
    head = text[:limit]
    space = head.rfind(" ")
    # Откатываемся к пробелу, только если остаётся больше половины.
    if space >= limit * 0.5:
        head = head[:space]
    return head.rstrip()


def build(items: list[dict[str, Any]] | None, brand: str = "",
          ref: str = "", limit: int = DEFAULT_LIMIT) -> str:
    """Собрать назначение из позиций ``[{name, qty}]``.

    ``brand`` — префикс магазина (настройка ``company_name``), ``ref`` —
    номер продажи/заказа (``CS-1042``, ``заказ №12``). Пустые названия
    пропускаются; позиций нет — возвращается только ``brand + ref``.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    if limit <= 0:
        limit = DEFAULT_LIMIT
    head = sanitize(brand)
    ref = sanitize(ref)
    if ref:
        head = f"{head} {ref}".strip()
    lines: list[str] = []
    for row in items or []:
        if not isinstance(row, dict):
            continue
        name = sanitize(row.get("name"))
        if not name:
            continue
        try:
            qty = float(row.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1.0
        if qty <= 0:
            continue
        lines.append(f"{name} {MULT} {format_qty(qty)}")
    if not lines:
        return head[:limit] if head else "Оплата"
    prefix = f"{head}: " if head else ""
    if len(prefix) >= limit:
        # Не влез даже префикс — режем его самого.
        return (_cut_word(head, limit - 1) + "…")[:limit]
    full = prefix + "; ".join(lines)
    if len(full) <= limit:
        return full
    # Убираем позиции с конца целиком, хвост — счётчиком.
    for keep in range(len(lines) - 1, 0, -1):
        dropped = len(lines) - keep
        tail = f"+{dropped} {plural(dropped, 'товар', 'товара', 'товаров')}"
        candidate = prefix + "; ".join(lines[:keep]) + "; " + tail
        if len(candidate) <= limit:
            return candidate
    # Не влезла даже первая позиция — режем её по словам.
    room = limit - len(prefix) - 1  # −1 под «…»
    return prefix + _cut_word(lines[0], room) + "…"
