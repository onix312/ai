"""Входящие заказы: текст из любого канала → проверенный черновик.

Парсер намеренно локальный и детерминированный: исходный текст не уходит во
внешние сервисы. Он извлекает очевидные реквизиты, затем сопоставляет клиента,
номенклатуру и последний похожий заказ. Неуверенные решения остаются человеку.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .accounting import num
from .db import Database

_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_PHONE_RE = re.compile(
    r"(?<!\d)(?P<phone>(?:\+?7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}"
    r"[\s()\-]*\d{2}[\s()\-]*\d{2})(?!\d)")
_MESSENGER_RE = re.compile(r"(?<![\w@])@[a-zA-Z0-9_]{3,}")
_QTY_RE = re.compile(
    r"(?<!\d)(?P<qty>\d+(?:[.,]\d+)?)\s*(?:шт(?:ук(?:и|а)?|\.)?|pcs?)(?!\w)",
    re.IGNORECASE)
_QTY_X_RE = re.compile(r"(?:^|\s)[xх×]\s*(?P<qty>\d+(?:[.,]\d+)?)\b", re.IGNORECASE)
_PRICE_RE = re.compile(
    r"(?:(?P<prefix>по|итого|всего|бюджет|за)\s*)?"
    r"(?P<price>\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:₽|руб(?:лей|ля|ль|\.)?|р\.?)(?!\w)",
    re.IGNORECASE)
_MATERIAL_RE = re.compile(
    r"\b(PLA|PETG|PET-G|ABS|ASA|TPU|TPE|PA(?:6|12)?|PC|PVA|HIPS)\b", re.IGNORECASE)
_COLOR_WORD_RE = re.compile(
    r"\b(?:ч[её]рн|бел|красн|син|зел[её]н|ж[её]лт|оранж|сер(?:ый|ая|ое)|"
    r"розов|фиолет|золот|сереб|прозрач|black|white|red|blue|green|yellow|"
    r"orange|gray|grey|pink|purple|gold|silver|transparent|clear)\w*\b",
    re.IGNORECASE)

_COLORS = {
    "чёрный": ("чёрн", "черн", "black"),
    "белый": ("бел", "white"),
    "красный": ("красн", "red"),
    "синий": ("син", "blue"),
    "зелёный": ("зел", "green"),
    "жёлтый": ("жёлт", "желт", "yellow"),
    "оранжевый": ("оранж", "orange"),
    "серый": ("серый", "серая", "серое", "gray", "grey"),
    "розовый": ("розов", "pink"),
    "фиолетовый": ("фиолет", "purple"),
    "золотой": ("золот", "gold"),
    "серебристый": ("сереб", "silver"),
    "прозрачный": ("прозрач", "transparent", "clear"),
}
_WEEKDAYS = {
    "понедельник": 0, "понедельника": 0,
    "вторник": 1, "вторника": 1,
    "среда": 2, "среды": 2,
    "четверг": 3, "четверга": 3,
    "пятница": 4, "пятницы": 4,
    "суббота": 5, "субботы": 5,
    "воскресенье": 6, "воскресенья": 6,
}


def _norm(value: object) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return " ".join(_WORD_RE.findall(text))


def _stems(value: object) -> set[str]:
    """Грубые основы слов для устойчивости к «Мария»/«для Марии»."""
    return {word[:max(4, len(word) - 2)] for word in _norm(value).split() if len(word) > 1}


def _phone_digits(value: object) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def _display_phone(value: str) -> str:
    digits = _phone_digits(value)
    return "+" + digits if len(digits) == 11 and digits.startswith("7") else value.strip()


def _number(value: str) -> float:
    return num(value.replace(" ", "").replace(",", "."))


def _due_date(text: str, today: date) -> tuple[str, str]:
    low = text.casefold().replace("ё", "е")
    relative = (("послезавтра", 2), ("завтра", 1), ("сегодня", 0))
    for word, shift in relative:
        match = re.search(rf"\b(?:до\s+)?{word}\b", low)
        if match:
            return (today + timedelta(days=shift)).isoformat(), match.group(0)

    iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", low)
    if iso:
        try:
            parsed = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
            return parsed.isoformat(), iso.group(0)
        except ValueError:
            pass

    dotted = re.search(
        r"(?:\bдо\s+)?\b(\d{1,2})[./](\d{1,2})(?:[./](20\d{2}|\d{2}))?\b", low)
    if dotted:
        try:
            year = int(dotted.group(3) or today.year)
            if year < 100:
                year += 2000
            parsed = date(year, int(dotted.group(2)), int(dotted.group(1)))
            if not dotted.group(3) and parsed < today:
                parsed = parsed.replace(year=parsed.year + 1)
            return parsed.isoformat(), dotted.group(0)
        except ValueError:
            pass

    weekday = re.search(
        r"\b(?:до\s+)?(" + "|".join(map(re.escape, _WEEKDAYS)) + r")\b", low)
    if weekday:
        target = _WEEKDAYS[weekday.group(1)]
        shift = (target - today.weekday()) % 7 or 7
        return (today + timedelta(days=shift)).isoformat(), weekday.group(0)
    return "", ""


def parse_order_text(text: str, today: date | None = None) -> dict[str, Any]:
    """Извлечь только явно распознаваемые поля без доступа к базе."""
    raw = _SPACE_RE.sub(" ", str(text or "").strip())
    if not raw:
        raise ValueError("Вставьте сообщение или описание заказа")
    if len(raw) > 10_000:
        raise ValueError("Текст заказа слишком большой")
    today = today or date.today()
    work = re.sub(r"^\s*(?:/)?(?:новый|заказ|new)\s*[:\-]?\s*", "", raw,
                  flags=re.IGNORECASE)
    warnings: list[str] = []

    phone_match = _PHONE_RE.search(work)
    phone = _display_phone(phone_match.group("phone")) if phone_match else ""
    messenger_match = _MESSENGER_RE.search(work)
    messenger = messenger_match.group(0) if messenger_match else ""

    qty_match = _QTY_RE.search(work) or _QTY_X_RE.search(work)
    qty = max(1.0, _number(qty_match.group("qty"))) if qty_match else 1.0

    price_match = _PRICE_RE.search(work)
    price_raw = _number(price_match.group("price")) if price_match else 0.0
    prefix = (price_match.group("prefix") or "").casefold() if price_match else ""
    price_kind = "unit" if prefix == "по" else "total"
    # Старый короткий формат «2шт 900р» исторически означал цену всего заказа.
    price = price_raw
    if price_match and prefix == "по":
        price = round(price_raw * qty, 2)
    elif price_match and qty > 1 and not prefix:
        warnings.append(f"{price_raw:g} ₽ распознано как цена всего заказа — проверьте сумму")

    due, due_token = _due_date(work, today)
    priority = "urgent" if re.search(r"\b(?:срочно|urgent|горит)\b", work, re.IGNORECASE) else "normal"
    material_match = _MATERIAL_RE.search(work)
    material = material_match.group(1).upper().replace("PET-G", "PETG") if material_match else ""
    normalized = _norm(work)
    color = ""
    color_token = ""
    for label, roots in _COLORS.items():
        found = next((root for root in roots if re.search(rf"\b{re.escape(root)}\w*", normalized)), "")
        if found:
            color, color_token = label.capitalize(), found
            break

    explicit_client = re.search(
        r"\b(?:клиент|заказчик|для)\s*[:\-]?\s*"
        r"([А-ЯЁA-Z][а-яёa-z-]+(?:\s+[А-ЯЁA-Z][а-яёa-z-]+){0,2})",
        work)
    client = explicit_client.group(1).strip() if explicit_client else ""

    # Для названия оставляем непризнанную часть сообщения. Это черновик:
    # сопоставление с номенклатурой ниже заменит её каноническим названием.
    product = work
    patterns = [
        _PHONE_RE, _MESSENGER_RE, _QTY_RE, _QTY_X_RE, _PRICE_RE, _MATERIAL_RE,
        re.compile(r"\b(?:срочно|urgent|горит)\b", re.IGNORECASE),
    ]
    for pattern in patterns:
        product = pattern.sub(" ", product)
    if due_token:
        product = re.sub(re.escape(due_token), " ", product, flags=re.IGNORECASE)
    if explicit_client:
        product = product.replace(explicit_client.group(0), " ")
    if color_token:
        product = _COLOR_WORD_RE.sub(" ", product)
    product = re.sub(r"\b(?:цена|бюджет|телефон|тел|контакт|материал|цвет)\s*[:\-]?", " ", product,
                     flags=re.IGNORECASE)
    product = re.sub(r"^\s*(?:мне\s+)?(?:нужно|нужен|нужна|хочу|сделайте|требуется|заказать)\b",
                     " ", product, flags=re.IGNORECASE)
    product = _SPACE_RE.sub(" ", product).strip(" ,.;:-")

    # Совместимость с короткой Telegram-командой: последнее имя отделяем от
    # изделия, только если оно выглядит как имя собственное.
    if not client:
        tokens = product.split()
        if len(tokens) >= 2 and re.fullmatch(r"[А-ЯЁA-Z][а-яёa-z-]+", tokens[-1]):
            client = tokens[-1]
            product = " ".join(tokens[:-1]).strip()

    return {
        "raw": raw,
        "product": product or "Заказ",
        "client": client,
        "phone": phone,
        "messenger": messenger,
        "qty": qty,
        "price": round(price, 2),
        "price_entered": round(price_raw, 2),
        "price_kind": price_kind,
        "due": due,
        "priority": priority,
        "material": material,
        "color": color,
        "warnings": warnings,
    }


class OrderIntake:
    """Обогащает локальный разбор данными PrintFlow, ничего не записывая."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _candidate_score(text: str, name: str, *codes: str) -> int:
        source = _norm(text)
        target = _norm(name)
        if not target:
            return 0
        if target == source:
            return 100
        if re.search(rf"(?:^| ){re.escape(target)}(?: |$)", source):
            return 94
        for code in codes:
            code_norm = _norm(code)
            if code_norm and re.search(rf"(?:^| ){re.escape(code_norm)}(?: |$)", source):
                return 98
        words = set(target.split())
        source_words = set(source.split())
        if not words:
            return 0
        overlap = len(words & source_words) / len(words)
        if overlap < 0.5:
            target_stems = _stems(target)
            source_stems = _stems(source)
            overlap = len(target_stems & source_stems) / max(1, len(target_stems))
        return round(overlap * 86) if overlap >= 0.5 else 0

    def _match_customer(self, parsed: dict) -> tuple[dict | None, int]:
        customers = self.db.query("SELECT * FROM customers ORDER BY name")
        phone = _phone_digits(parsed.get("phone"))
        messenger = str(parsed.get("messenger") or "").casefold()
        exact = [customer for customer in customers
                 if ((phone and phone == _phone_digits(customer.get("phone")))
                     or (messenger and messenger == str(
                         customer.get("messenger") or "").casefold()))]
        if len(exact) == 1:
            return exact[0], 100
        if len(exact) > 1:
            return None, 0
        probe = parsed.get("client") or parsed.get("raw")
        ranked = sorted(
            ((self._candidate_score(str(probe), str(customer.get("name") or "")), customer)
             for customer in customers),
            key=lambda pair: pair[0], reverse=True,
        )
        if not ranked or ranked[0][0] < 75:
            return None, 0
        # Два одноимённых клиента без телефона — не повод выбирать случайного.
        if len(ranked) > 1 and ranked[1][0] == ranked[0][0]:
            return None, 0
        return ranked[0][1], ranked[0][0]

    def _products(self) -> list[dict]:
        rows = self.db.query(
            "SELECT n.*, COALESCE((SELECT p.price FROM prices p"
            " LEFT JOIN price_types t ON t.id=p.price_type_id"
            " WHERE p.nom_id=n.id ORDER BY t.is_base DESC, datetime(p.at) DESC,"
            " p.rowid DESC LIMIT 1),0) price"
            " FROM nomenclature n WHERE n.archived=0")
        for row in rows:
            row["source"] = "nomenclature"
        legacy = self.db.query("SELECT * FROM catalog WHERE archived=0")
        for row in legacy:
            row["source"] = "catalog"
        return rows + legacy

    def _match_product(self, parsed: dict) -> tuple[dict | None, int]:
        # В исходном тексте сохраняются полные названия/артикулы, которые могли
        # исчезнуть из очищенного product при извлечении цвета и материала.
        probe = f"{parsed.get('product', '')} {parsed.get('raw', '')}"
        best: tuple[int, dict | None] = (0, None)
        for item in self._products():
            score = self._candidate_score(
                probe, str(item.get("name") or ""),
                str(item.get("sku") or ""), str(item.get("code") or ""),
                str(item.get("barcode") or ""))
            if score > best[0]:
                best = score, item
        return (best[1], best[0]) if best[0] >= 70 else (None, 0)

    def _previous_order(self, customer: dict | None, product: str) -> dict | None:
        if not customer:
            return None
        rows = self.db.query(
            "SELECT * FROM orders WHERE customer_id=?"
            " ORDER BY datetime(created_at) DESC LIMIT 20", (customer["id"],))
        if not rows:
            return None
        best = max(rows, key=lambda row: self._candidate_score(
            product, str(row.get("product") or "")))
        return best if self._candidate_score(product, str(best.get("product") or "")) >= 60 else None

    def preview(self, text: str, channel: str = "") -> dict[str, Any]:
        parsed = parse_order_text(text)
        customer, customer_score = self._match_customer(parsed)
        product, product_score = self._match_product(parsed)
        previous = self._previous_order(customer, parsed["product"])
        warnings = list(parsed["warnings"])

        qty = max(1.0, num(parsed["qty"], 1))
        draft: dict[str, Any] = {
            "product": parsed["product"],
            "status": "new",
            "priority": parsed["priority"],
            "channel": str(channel or "direct").strip() or "direct",
            "qty": qty,
            "due": parsed["due"],
            "customer_name": parsed["client"],
            "phone": parsed["phone"],
            "messenger": parsed["messenger"],
            "material": parsed["material"],
            "color": parsed["color"],
            "price": parsed["price"],  # orders.price — сумма всего заказа
            "notes": f"Входящий текст ({channel or 'другое'}): {parsed['raw']}",
        }

        if customer:
            draft.update({
                "customer_id": customer["id"],
                "customer_name": customer.get("name") or draft["customer_name"],
                "phone": customer.get("phone") or draft["phone"],
                "messenger": customer.get("messenger") or draft["messenger"],
            })

        if product:
            draft.update({
                "product": product.get("name") or draft["product"],
                "niche_id": product.get("niche_id") or "",
                "material": draft["material"] or product.get("material") or "",
                # Нормативы простого заказа хранятся на единицу; qty учитывается
                # в экономике, плане и списании материала.
                "grams": num(product.get("grams")),
                "hours": num(product.get("hours")),
                "manual_minutes": num(product.get("post_minutes")),
                "file": product.get("file") or "",
            })
            if product.get("source") == "nomenclature":
                draft["nom_id"] = product["id"]
            if not draft["price"] and num(product.get("price")):
                draft["price"] = round(num(product["price"]) * qty, 2)

        # История дополняет только пустые поля и только для похожего изделия.
        if previous:
            for key in ("niche_id", "material", "color", "grams", "hours", "file"):
                if not draft.get(key) and previous.get(key):
                    draft[key] = previous[key]
            if not draft["price"] and num(previous.get("price")):
                draft["price"] = num(previous["price"])
            if not channel and previous.get("channel"):
                draft["channel"] = previous["channel"]

        if not customer and (parsed["phone"] or parsed["messenger"] or parsed["client"]):
            warnings.append("Клиент не найден — будет создан при сохранении")
        if not product:
            warnings.append("Изделие не найдено в базе — проверьте название и расчёт")
        if not draft["price"]:
            warnings.append("Цена не определена")

        confidence = round((
            (35 if draft["product"] else 0)
            + (20 if customer else (8 if draft["customer_name"] else 0))
            + (25 if product else 0)
            + (10 if draft["price"] else 0)
            + (10 if parsed["qty"] else 0)
        ))
        return {
            "ok": True,
            "draft": draft,
            "parsed": parsed,
            "confidence": min(100, confidence),
            "warnings": warnings,
            "matches": {
                "customer": ({"id": customer["id"], "name": customer.get("name", ""),
                              "score": customer_score} if customer else None),
                "product": ({"id": product["id"], "name": product.get("name", ""),
                             "source": product.get("source", ""), "score": product_score}
                            if product else None),
                "previous_order": ({"id": previous["id"], "number": previous.get("number", "")}
                                   if previous else None),
            },
        }
