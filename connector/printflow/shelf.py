"""Стеллаж магазина: готовая продукция на полке.

Модель учёта:
    • производство (партия печати) → приход штук на стеллаж;
    • продажа (вручную, по данным 1С кассы или по QR-ценнику) → списание штук
      и проводка дохода с пометкой «стеллаж»;
    • инвентаризация → сверка «должно быть» с фактом, расхождение в журнал;
    • общий остаток штук — один на стеллаж и онлайн-продажи (Авито/Telegram).

Деньги от стеллажа попадают в кассу PrintFlow только если при продаже указана
цена; быстрое списание «−N шт» движений денег не делает.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta
from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso
from .db import Database

# Периоды аналитики стеллажа
SALE_DAYS = 7        # окно «продано за N дней» для оборачиваемости
DEAD_DAYS = 14       # после скольких дней без продаж позиция — «мёртвый сток»
PLAN_DAYS = 7        # на сколько дней вперёд планировать пополнение

# Физические форматы витрины.  Старые идентификаторы не выбрасываем: они уже
# лежат в пользовательских shelf_items и безопасно приводятся к обычному ценнику.
# Это позволяет обновить раскладку старых записей без «пустых»
# ценников у существующих позиций.
TAG_TEMPLATES = {"standard", "promo"}
LEGACY_TAG_TEMPLATE_ALIASES = {
    "classic": "standard",
    "compact": "standard",
    "minimal": "standard",
}
SUPPORTED_TAG_TEMPLATES = TAG_TEMPLATES | set(LEGACY_TAG_TEMPLATE_ALIASES)
DEFAULT_TAG_TEMPLATE = "standard"
# Visual variants never change the physical sheet geometry.  This lets a shop
# switch between retail, sale and photo-led looks without creating a wrong-size
# label on A4.
TAG_VARIANTS = {"clean", "accent", "sale", "mono", "photo"}
DEFAULT_TAG_VARIANT = "clean"
LEGACY_TAG_VARIANT_ALIASES = {"classic": "clean", "minimal": "mono"}
DEFAULT_TAG_COLOR = "#4f46e5"
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalize_tag_template(value: Any, fallback: str = DEFAULT_TAG_TEMPLATE) -> str:
    """Return a current physical tag format, accepting records from older builds.

    ``standard`` prints as 66 × 31 mm, ``promo`` as 66 × 56 mm.  ``classic``,
    ``compact`` and ``minimal`` were historical visual IDs with obsolete paper
    dimensions; treating all of them as ``standard`` keeps old shelf records
    printable and upgrades them when their card is next saved.
    """
    template = str(value or "").strip().lower()
    if template in TAG_TEMPLATES:
        return template
    return LEGACY_TAG_TEMPLATE_ALIASES.get(template, fallback)


def normalize_tag_variant(value: Any, fallback: str = DEFAULT_TAG_VARIANT) -> str:
    """Return a supported visual tag style without affecting paper dimensions."""
    variant = str(value or "").strip().lower()
    if variant in TAG_VARIANTS:
        return variant
    return LEGACY_TAG_VARIANT_ALIASES.get(variant, fallback)


class Shelf:
    """Учёт позиций стеллажа и их движений."""

    def __init__(self, db: Database):
        self.db = db
        self.acc = Accounting(db)

    # ------------------------------------------------------------ позиции
    def items(self) -> list[dict]:
        """Позиции стеллажа с аналитикой: продажи, оборачиваемость, статус."""
        rows = self.db.query("SELECT * FROM shelf_items WHERE active=1 ORDER BY name")
        since7 = (datetime.now() - timedelta(days=SALE_DAYS)).isoformat()
        since30 = (datetime.now() - timedelta(days=30)).isoformat()
        since_dead = (datetime.now() - timedelta(days=DEAD_DAYS)).isoformat()
        out = []
        for raw_row in rows:
            row = self._with_cashier_data(raw_row)
            qty = num(row["qty"])
            sold7 = num(self._sum_sold(row["id"], since7))
            sold30 = num(self._sum_sold(row["id"], since30))
            sold_dead = num(self._sum_sold(row["id"], since_dead))
            last = self.db.one(
                "SELECT MAX(at) a FROM shelf_moves WHERE item_id=? AND kind IN ('sale','online')",
                (row["id"],)) or {}
            # скорость продаж, шт/день
            rate = sold7 / SALE_DAYS if sold7 else 0.0
            days_left = round(qty / rate, 1) if rate and qty > 0 else None
            cost = num(row["cost_per_unit"])
            dead = qty > 0 and sold_dead <= 0
            low = qty > 0 and num(row["min_qty"]) > 0 and qty <= num(row["min_qty"])
            status = "dead" if dead else ("low" if low else ("ok" if qty > 0 else "empty"))
            # план пополнения: сколько напечатать, чтобы хватило на PLAN_DAYS
            plan = 0
            if rate and qty < rate * PLAN_DAYS:
                plan = max(1, int(rate * PLAN_DAYS - qty + 0.999))
            out.append({
                **row,
                "qty": round(qty, 1),
                "sold_7": round(sold7, 1),
                "sold_30": round(sold30, 1),
                "rate_per_day": round(rate, 2),
                "days_left": days_left,
                "stock_value": round(qty * cost, 2),
                "margin": round(num(row["price"]) - cost, 2) if num(row["price"]) else 0.0,
                "last_sale": last.get("a") or "",
                "dead": dead,
                "low": low,
                "status": status,
                "plan_qty": plan,
            })
        return out

    def _linked_nomenclature(self, row: dict) -> dict | None:
        """Найти canonical-карточку товара для полочной позиции.

        Старые базы связывали полку с ``catalog``, новые — прямо с
        ``nomenclature``. Поддерживаем оба варианта, чтобы штрихкод 1С не
        пришлось переносить руками после обновления.
        """
        nom_id = str(row.get("nom_id") or "").strip()
        if nom_id:
            found = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
            if found:
                return found
        catalog_id = str(row.get("catalog_id") or "").strip()
        if catalog_id:
            catalog = self.db.one("SELECT nom_id FROM catalog WHERE id=?", (catalog_id,)) or {}
            catalog_nom_id = str(catalog.get("nom_id") or "").strip()
            if catalog_nom_id:
                found = self.db.one("SELECT * FROM nomenclature WHERE id=?", (catalog_nom_id,))
                if found:
                    return found
            found = self.db.one(
                "SELECT * FROM nomenclature WHERE legacy_catalog_id=? LIMIT 1",
                (catalog_id,))
            if found:
                return found
        return self.db.one(
            "SELECT * FROM nomenclature WHERE legacy_shelf_id=? LIMIT 1",
            (row.get("id") or "",))

    def _with_cashier_data(self, row: dict) -> dict:
        """Добавить эффективные артикул/штрихкод и данные печатного ценника."""
        result = dict(row)
        nom = self._linked_nomenclature(result)
        own_barcode = str(result.get("barcode") or "").strip()
        own_sku = str(result.get("sku") or "").strip()
        if nom:
            result["nom_id"] = result.get("nom_id") or nom.get("id") or ""
            result["barcode"] = own_barcode or str(nom.get("barcode") or "").strip()
            result["sku"] = own_sku or str(nom.get("sku") or nom.get("code") or "").strip()
            result["material"] = nom.get("material") or ""
            result["grams"] = num(nom.get("grams"))
        else:
            result["barcode"] = own_barcode
            result["sku"] = own_sku
            result.setdefault("material", "")
            result.setdefault("grams", 0.0)
        result["barcode_source"] = ("shelf" if own_barcode else
                                    ("nomenclature" if result.get("barcode") else ""))
        # API always returns a current physical format.  Raw legacy values remain
        # readable in old databases and become ``standard`` after the next save.
        result["tag_template"] = normalize_tag_template(result.get("tag_template"))
        result["tag_variant"] = normalize_tag_variant(result.get("tag_variant"))
        color = str(result.get("tag_color") or DEFAULT_TAG_COLOR).strip()
        result["tag_color"] = color if _HEX_COLOR.fullmatch(color) else DEFAULT_TAG_COLOR
        result["tag_old_price"] = max(0.0, round(num(result.get("tag_old_price")), 2))
        return result

    def _sum_sold(self, item_id: str, since: str) -> float:
        row = self.db.one(
            "SELECT COALESCE(SUM(-qty),0) v FROM shelf_moves"
            " WHERE item_id=? AND kind IN ('sale','online') AND qty<0 AND at>=?",
            (item_id, since)) or {}
        return num(row.get("v"))

    def item(self, item_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM shelf_items WHERE id=?", (item_id,))
        if not row:
            return None
        row = self._with_cashier_data(row)
        row["moves"] = self.moves(item_id, limit=40)
        return row

    def save_item(self, data: dict) -> dict:
        data = dict(data)
        new = not data.get("id")
        if not data.get("id"):
            data["id"] = uid("shf")
        item_id = str(data["id"])
        if not str(data.get("name") or "").strip():
            raise ValueError("Укажите название позиции")
        data["name"] = str(data["name"]).strip()[:200]

        # Если позиция выбрана из старого каталога, сразу запоминаем canonical id.
        # Штрихкод при этом остаётся «живым»: пока в полке он пуст, берём его из
        # номенклатуры, поэтому изменение в карточке товара доходит до ценника.
        linked = self._linked_nomenclature(data)
        if linked and not data.get("nom_id"):
            data["nom_id"] = linked.get("id") or ""

        for key, limit in (("barcode", 80), ("sku", 80), ("tag_badge", 40),
                           ("tag_note", 180), ("note", 500)):
            if key in data:
                data[key] = str(data.get(key) or "").strip()[:limit]
        barcode = str(data.get("barcode") or "").strip()
        if barcode:
            from .barcode import validate
            validate(barcode)  # понятная ошибка для кириллицы/непечатных символов
            duplicate = self.db.one(
                "SELECT id,name FROM shelf_items"
                " WHERE active=1 AND barcode=? AND id<>? LIMIT 1",
                (barcode, item_id))
            if duplicate:
                raise ValueError(
                    f"Штрихкод уже привязан к позиции «{duplicate.get('name') or duplicate['id']}»")

        if "tag_template" in data:
            template = str(data.get("tag_template") or "").strip().lower()
            if template not in SUPPORTED_TAG_TEMPLATES:
                raise ValueError("Неизвестный тип ценника")
            # Persist only current formats for newly edited/new positions while
            # accepting legacy clients and data exports during the transition.
            data["tag_template"] = normalize_tag_template(template)
        if "tag_variant" in data:
            variant = str(data.get("tag_variant") or "").strip().lower()
            if variant not in TAG_VARIANTS and variant not in LEGACY_TAG_VARIANT_ALIASES:
                raise ValueError("Неизвестный вариант оформления ценника")
            data["tag_variant"] = normalize_tag_variant(variant)
        if "tag_color" in data:
            color = str(data.get("tag_color") or DEFAULT_TAG_COLOR).strip()
            if not _HEX_COLOR.fullmatch(color):
                raise ValueError("Цвет ценника должен быть в формате #4f46e5")
            data["tag_color"] = color.lower()

        if new:
            data.setdefault("created_at", now_iso())
        data["updated_at"] = now_iso()
        for key in ("qty", "price", "cost_per_unit", "min_qty", "tag_old_price"):
            if key in data:
                data[key] = round(num(data[key]), 2)
        if "tag_old_price" in data:
            data["tag_old_price"] = max(0.0, data["tag_old_price"])
        row = self.db.upsert("shelf_items", data)
        self.db.add_event("shelf", "Позиция стеллажа создана" if new else "Позиция стеллажа изменена",
                          row.get("name") or "", data={"item_id": row["id"]})
        return self._with_cashier_data(row)

    def delete_item(self, item_id: str) -> None:
        if not item_id:
            raise ValueError("Не указана позиция")
        self.db.delete("shelf_items", item_id)

    # ------------------------------------------------------------ движения
    def moves(self, item_id: str = "", limit: int = 100) -> list[dict]:
        sql = ("SELECT m.*, i.name item_name FROM shelf_moves m"
               " LEFT JOIN shelf_items i ON i.id=m.item_id WHERE 1=1")
        params: list[Any] = []
        if item_id:
            sql += " AND m.item_id=?"
            params.append(item_id)
        sql += " ORDER BY datetime(m.at) DESC LIMIT ?"
        params.append(int(limit))
        return self.db.query(sql, params)

    def _move(self, item_id: str, kind: str, qty: float, price: float = 0.0,
              job_id: str = "", tx_id: str = "", note: str = "",
              source: str = "", external_id: str = "") -> dict:
        row = self.db.upsert("shelf_moves", {
            "id": uid("shm"), "at": now_iso(), "item_id": item_id, "kind": kind,
            "qty": round(num(qty), 2), "price": round(num(price), 2),
            "job_id": job_id or None, "tx_id": tx_id or None,
            "source": str(source or "").strip(),
            "external_id": str(external_id or "").strip(),
            "note": note or ""})
        self.db.execute("UPDATE shelf_items SET qty=?, updated_at=? WHERE id=?",
                        (round(num(qty) + self._qty(item_id), 2), now_iso(), item_id))
        return row

    def _qty(self, item_id: str) -> float:
        row = self.db.one("SELECT qty FROM shelf_items WHERE id=?", (item_id,)) or {}
        return num(row.get("qty"))

    def produce(self, item_id: str, qty: float, job_id: str = "", note: str = "",
                cost_per_unit: float = 0.0) -> dict:
        """Приход готовой продукции на стеллаж (с партии печати или вручную)."""
        item = self.db.one("SELECT * FROM shelf_items WHERE id=?", (item_id,))
        if not item:
            raise ValueError("Позиция стеллажа не найдена")
        qty = num(qty)
        if qty <= 0:
            raise ValueError("Количество должно быть больше нуля")
        cost = num(cost_per_unit)
        if job_id:
            job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
            if job and num(job.get("cost")) > 0:
                cost = num(job["cost"]) / qty
                note = (note + f" · из печати {job.get('name') or job_id}").strip()
        if cost and not num(item["cost_per_unit"]):
            self.db.execute("UPDATE shelf_items SET cost_per_unit=? WHERE id=?",
                            (round(cost, 2), item_id))
        move = self._move(item_id, "produce", qty, job_id=job_id,
                          note=note or "Приход на стеллаж")
        self.db.add_event("shelf", "Приход на стеллаж",
                          f"{item.get('name') or ''} +{round(qty)} шт",
                          data={"item_id": item_id, "qty": qty})
        return {"ok": True, "move": move, "item": self.db.one(
            "SELECT * FROM shelf_items WHERE id=?", (item_id,))}

    def sale(self, item_id: str, qty: float, price: float = 0.0,
             channel: str = "shelf", note: str = "", *,
             record_income: bool = True, source: str = "",
             external_id: str = "") -> dict:
        """Продажа штук со стеллажа.

        Обычная ручная продажа пишет доход в PrintFlow. Интеграция с 1С передаёт
        ``record_income=False``: 1С остаётся источником денег, а PrintFlow только
        уменьшает физический остаток. ``source + external_id`` защищают от
        повторной отправки одной строки чека.
        """
        item = self.db.one("SELECT * FROM shelf_items WHERE id=?", (item_id,))
        if not item:
            raise ValueError("Позиция стеллажа не найдена")
        qty = num(qty)
        if qty <= 0:
            raise ValueError("Количество должно быть больше нуля")
        left = self._qty(item_id)
        if left < qty:
            raise ValueError(f"На стеллаже только {round(left)} шт — продать {round(qty)} нельзя")
        price = num(price) if num(price) > 0 else num(item.get("price"))
        tx = None
        kind = "online" if channel == "online" else "sale"
        if price > 0 and record_income:
            tx = self.acc.add_transaction(
                "income", "sale", price * qty,
                f"Стеллаж: {item.get('name') or ''} × {round(qty)}",
                note=f"{note} · {channel}" .strip(), auto=False,
                channel="online" if channel == "online" else "shelf",
                payer="person")
        move = self._move(item_id, kind, -qty, price=price,
                          tx_id=tx.get("id") if tx else "",
                          note=note or f"Продажа ({channel})",
                          source=source, external_id=external_id)
        self.db.add_event("shelf", "Продажа со стеллажа",
                          f"{item.get('name') or ''} −{round(qty)} шт"
                          + (f" на {round(price * qty)} ₽" if price else ""),
                          data={"item_id": item_id, "qty": qty, "price": price,
                                "source": source, "external_id": external_id,
                                "income_recorded": bool(tx)})
        return {"ok": True, "move": move, "tx": tx,
                "item": self.item(item_id)}

    def sales_many(self, rows: list[dict], channel: str = "shelf") -> list[dict]:
        """Продажи раз в день: [{item_id, qty, price?}]. Одна операция — несколько позиций."""
        results = []
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("item_id"):
                continue
            qty = num(row.get("qty"))
            if qty <= 0:
                continue
            results.append(self.sale(row["item_id"], qty,
                                     num(row.get("price")), channel,
                                     row.get("note", "")))
        return results

    # ------------------------------------------------------- касса и 1С
    def cashier_lookup(self, code: str) -> dict | None:
        """Найти позицию по коду, который прислал кассовый сканер/1С."""
        code = str(code or "").strip()
        if not code:
            raise ValueError("Передайте штрихкод или артикул")
        matches = [item for item in self.items()
                   if code in {str(item.get("barcode") or "").strip(),
                               str(item.get("sku") or "").strip(),
                               str(item.get("id") or "").strip()}]
        if len(matches) > 1:
            names = ", ".join(str(item.get("name") or item["id"]) for item in matches[:3])
            raise ValueError(f"Код неоднозначен: найдено несколько позиций ({names})")
        return matches[0] if matches else None

    def sale_from_1c(self, barcode: str, qty: float, external_id: str,
                     price: float = 0.0) -> dict:
        """Принять строку чека из 1С без повторной финансовой проводки.

        ``external_id`` — уникальный ключ *строки* чека, например
        ``ККМ-000184:2``. При сетевом retry возвращаем прежний результат и не
        уменьшаем остаток второй раз.
        """
        external_id = str(external_id or "").strip()
        if not external_id:
            raise ValueError("1С должна передать external_id строки чека")
        if len(external_id) > 160:
            raise ValueError("external_id слишком длинный")
        qty = num(qty)
        if qty <= 0:
            raise ValueError("Количество должно быть больше нуля")
        with self.db.transaction():
            existing = self.db.one(
                "SELECT * FROM shelf_moves WHERE source='1c' AND external_id=? LIMIT 1",
                (external_id,))
            if existing:
                return {"ok": True, "duplicate": True, "move": existing,
                        "item": self.item(existing.get("item_id") or "")}
            item = self.cashier_lookup(barcode)
            if not item:
                raise ValueError("Штрихкод не привязан ни к одной позиции стеллажа")
            result = self.sale(
                item["id"], qty, num(price), channel="shelf",
                note=f"чек 1С · {external_id}", record_income=False,
                source="1c", external_id=external_id)
            result["duplicate"] = False
            result["money_source"] = "1c"
            return result

    def one_c_export_csv(self) -> str:
        """CSV-справочник для загрузки/сверки номенклатуры в 1С.

        Разделитель ``;``, UTF-8 с BOM и десятичная запятая нормально
        открываются в русской 1С и Excel. Конфигурации 1С отличаются, поэтому
        имена колонок оставлены явными, без притворства «универсальным обменом».
        """
        stream = io.StringIO()
        writer = csv.writer(stream, delimiter=";", lineterminator="\r\n")
        writer.writerow(("ID PrintFlow", "Код", "Артикул", "Штрихкод",
                         "Наименование", "Цена", "Остаток", "Единица"))
        for item in self.items():
            writer.writerow((
                item.get("id") or "", item.get("nom_id") or "",
                item.get("sku") or "", item.get("barcode") or "",
                item.get("name") or "",
                f"{num(item.get('price')):.2f}".replace(".", ","),
                f"{num(item.get('qty')):.2f}".replace(".", ","), "шт",
            ))
        return "\ufeff" + stream.getvalue()

    def writeoff(self, item_id: str, qty: float, note: str = "Списание") -> dict:
        """Списание штук без продажи: порча, потеря, подарок."""
        item = self.db.one("SELECT * FROM shelf_items WHERE id=?", (item_id,))
        if not item:
            raise ValueError("Позиция стеллажа не найдена")
        qty = num(qty)
        if qty <= 0:
            raise ValueError("Количество должно быть больше нуля")
        if self._qty(item_id) < qty:
            raise ValueError("Списать больше, чем есть на стеллаже")
        move = self._move(item_id, "writeoff", -qty, note=note)
        self.db.add_event("shelf", "Списание со стеллажа",
                          f"{item.get('name') or ''} −{round(qty)} шт · {note}",
                          data={"item_id": item_id, "qty": qty})
        return {"ok": True, "move": move}

    def inventory(self, item_id: str, actual: float, note: str = "") -> dict:
        """Инвентаризация позиции: ожидалось X, посчитали Y → расхождение в журнал."""
        item = self.db.one("SELECT * FROM shelf_items WHERE id=?", (item_id,))
        if not item:
            raise ValueError("Позиция стеллажа не найдена")
        expected = self._qty(item_id)
        actual = num(actual)
        if actual < 0:
            raise ValueError("Факт не может быть отрицательным")
        diff = round(actual - expected, 2)
        move = self._move(item_id, "inventory", diff,
                          note=note or f"Инвентаризация: было {round(expected)} шт, стало {round(actual)} шт")
        self.db.add_event(
            "shelf", "Инвентаризация",
            f"{item.get('name') or ''}: ожидалось {round(expected)} шт, факт {round(actual)} шт"
            + (f", расхождение {round(diff):+d} шт" if diff else ", всё сошлось"),
            data={"item_id": item_id, "expected": expected, "actual": actual, "diff": diff})
        return {"ok": True, "move": move, "diff": diff,
                "item": self.db.one("SELECT * FROM shelf_items WHERE id=?", (item_id,))}

    # ------------------------------------------------------------- сводка
    def summary(self) -> dict[str, Any]:
        items = self.items()
        qty = sum(num(i["qty"]) for i in items)
        value = sum(num(i["stock_value"]) for i in items)
        sold7 = sum(num(i["sold_7"]) for i in items)
        sold7_money = sum(num(i["sold_7"]) * num(i["price"]) for i in items)
        dead = [i for i in items if i["dead"]]
        low = [i for i in items if i["low"]]
        plan = sum(int(num(i["plan_qty"])) for i in items)
        return {
            "items": len(items),
            "qty": round(qty, 1),
            "value": round(value, 2),
            "sold_7": round(sold7, 1),
            "sold_7_money": round(sold7_money, 2),
            "dead": len(dead),
            "dead_value": round(sum(num(i["stock_value"]) for i in dead), 2),
            "low": len(low),
            "plan_qty": plan,
        }

    # -------------------------------------------------- касса магазина (выемка)
    def _shelf_income(self) -> float:
        """Доход от продаж со стеллажа (channel='shelf') по всей истории.

        Онлайн-продажи со стеллажа уходят каналом 'online' и в кассу магазина
        не попадают — деньги там лежат на счёте, а не в магазине.
        """
        row = self.db.one(
            "SELECT COALESCE(SUM(amount),0) v FROM transactions"
            " WHERE kind='income' AND channel='shelf'") or {}
        return num(row.get("v"))

    def collections(self, limit: int = 50) -> list[dict]:
        """Последние выемки из кассы магазина, свежие сверху."""
        return self.db.query(
            "SELECT * FROM shelf_collections"
            " ORDER BY datetime(at) DESC, id DESC LIMIT ?", (max(1, int(limit)),))

    def shop_cash(self) -> dict[str, Any]:
        """Сколько денег от стеллажа лежит в магазине и сколько уже забрали.

        Деньги от продаж со стеллажа учитываются доходом в PrintFlow, но
        физически остаются в кассе магазина. Разница между накопленным доходом
        и выемками — это остаток, который должен быть в кассе (для сверки).
        """
        income = self._shelf_income()
        collected = sum(num(c.get("amount")) for c in self.collections())
        return {
            "shelf_income": round(income, 2),
            "collected_total": round(collected, 2),
            "in_shop": round(income - collected, 2),
            "collections": self.collections(),
        }

    def add_collection(self, amount: float, note: str = "") -> dict:
        """Выемка «забрали из магазина»: уменьшает остаток, не трогая доход.

        Проводки не создаём — это перемещение физических денег, а не новая
        операция. Нельзя забрать больше, чем накоплено от стеллажа: суммарный
        остаток магазина не должен уходить в минус по невнимательности.
        """
        amount = num(amount)
        if amount <= 0:
            raise ValueError("Сумма выемки должна быть больше нуля")
        state = self.shop_cash()
        if amount > num(state["in_shop"]) + 0.005:
            raise ValueError(
                f"В магазине от стеллажа лежит {round(num(state['in_shop']), 2)} ₽ — "
                f"забрать {round(amount, 2)} ₽ нельзя")
        row = self.db.upsert("shelf_collections", {
            "id": uid("shc"), "at": now_iso(), "amount": round(amount, 2),
            "note": str(note or "").strip()})
        self.db.add_event("money", "Забрали из кассы магазина",
                          f"{round(amount, 2)} ₽", "",
                          data={"collection_id": row["id"], "amount": amount})
        return row

    def delete_collection(self, collection_id: str) -> None:
        """Отменить выемку (ошиблись суммой) — вернуть деньги в остаток магазина."""
        if not collection_id:
            raise ValueError("Не указана выемка")
        self.db.delete("shelf_collections", collection_id)

    # ------------------------------------------------- прогноз и таблички
    def forecast(self, days: int = 7) -> list[dict[str, Any]]:
        """Симуляция полки: при текущей скорости продаж сколько будет через N дней.

        Идея 13. Скорость берётся за 7 дней (как в `items()`); дефицит —
        красная полоса. Позиции без продаж показываются с нулевой скоростью:
        это честно, а не домысел.
        """
        days = max(1, min(int(days or 7), 30))
        out = []
        for i in self.items():
            if num(i["qty"]) <= 0 and num(i["sold_7"]) <= 0:
                continue  # пусто и не продаётся — в прогноз не входит
            rate = num(i["rate_per_day"])
            projected = num(i["qty"]) - rate * days
            gap = max(0.0, rate * days - num(i["qty"]))
            out.append({
                "id": i["id"], "name": i["name"], "qty": i["qty"],
                "rate_per_day": rate,
                "projected": round(max(0.0, projected), 1),
                "gap": round(gap, 1),
                "days_left": i["days_left"],
                "empty": projected <= 0,
                "low": 0 < projected <= num(i.get("min_qty") or 0),
            })
        out.sort(key=lambda r: (not r["empty"], r["projected"] / (r["rate_per_day"] or 1)))
        return out

    def live_tags(self, limit: int = 5) -> dict[str, list[dict[str, Any]]]:
        """Живые таблички полки: хит / новинка / последний. Идея 102.

        • хит — топ продаж за 30 дней;
        • новинка — позиция создана (или пришла) не более 14 дней назад;
        • «последний!» — остаток 1 штука.
        """
        items = self.items()
        since_new = (datetime.now() - timedelta(days=DEAD_DAYS)).isoformat()
        hits = sorted([i for i in items if num(i["sold_30"]) > 0],
                      key=lambda x: num(x["sold_30"]), reverse=True)[:max(1, limit)]
        news = [i for i in items
                if str(i.get("created_at") or "") >= since_new or
                str(i.get("updated_at") or "") >= since_new]
        news = news[:max(1, limit)]
        last = [i for i in items if 0 < num(i["qty"]) <= 1][:max(1, limit)]
        slim = lambda i: {"id": i["id"], "name": i["name"], "price": num(i["price"]),
                          "qty": i["qty"],
                          "sold_30": i["sold_30"], "sold_7": i["sold_7"]}
        return {"hit": [slim(i) for i in hits],
                "new": [slim(i) for i in news],
                "last": [slim(i) for i in last]}

    # ------------------------------------------------- перемещение со склада
    def stock_available(self, goods_only: bool = False) -> list[dict]:
        """Товары учётных складов с остатком ≥ 1 шт — кандидаты на стеллаж.

        Позиции с нулевым или дробным «хвостом» меньше единицы не показываем:
        переместить на полку можно только целую штуку, которая реально есть.
        Полка магазина (склад kind='shelf') исключается — оттуда не «перемещают
        на стеллаж», это и есть стеллаж. ``goods_only`` оставляет только готовые
        товары (product/kit/semi) — их переносят в новые позиции стеллажа.
        """
        sql = ("SELECT m.nom_id, m.warehouse_id, COALESCE(w.name,'Склад') warehouse_name,"
               " n.name, n.photo, n.unit, n.kind,"
               " COALESCE(SUM(m.qty),0) q, COALESCE(SUM(m.cost),0) c"
               " FROM stock_moves m"
               " JOIN nomenclature n ON n.id=m.nom_id AND n.archived=0"
               " LEFT JOIN warehouses w ON w.id=m.warehouse_id"
               " WHERE COALESCE(w.kind,'') != 'shelf'")
        if goods_only:
            sql += " AND n.kind IN ('product','kit','semi')"
        sql += " GROUP BY m.nom_id, m.warehouse_id HAVING q > 0 ORDER BY n.name"
        rows = self.db.query(sql)
        # Базовая цена готовых товаров — для подстановки в новый ценник.
        from .nomenclature import Nomenclature
        nom = Nomenclature(self.db)
        prices = nom._all_prices()
        base_type = nom._base_type()
        out = []
        for row in rows:
            qty = num(row["q"])
            unit = str(row.get("unit") or "шт")
            # Штучные товары показываем только когда есть целая штука; весовые
            # и метражные (кг/м/л) можно вынести хоть грамм — это не «дробная штука».
            if unit in ("шт", "шт.", "piece", "pcs") and qty < 1:
                continue
            out.append({
                "nom_id": row["nom_id"], "name": row["name"] or "Без названия",
                "photo": row.get("photo") or "", "unit": unit,
                "kind": str(row.get("kind") or "product"),
                "warehouse_id": row["warehouse_id"] or "",
                "warehouse_name": row["warehouse_name"],
                "qty": round(qty, 3),
                "avg_cost": round(max(0.0, num(row["c"])) / qty, 2) if qty > 0 else 0.0,
                "price": round(num((prices.get(row["nom_id"]) or {}).get(base_type, 0)), 2),
            })
        return out

    def transfer_from_stock(self, nom_id: str, warehouse_id: str, qty: float,
                            item_id: str = "", note: str = "") -> dict:
        """Переместить готовый товар с учётного склада на стеллаж магазина.

        Правила:
        • перемещать можно только то, что есть: остаток на складе-источнике
          должен быть ≥ 1 шт, а запрошенное количество — целое, от 1 и не
          больше остатка;
        • регистр остатков: расход со склада-источника. Стеллаж — витрина,
          не склад: штуки уходят из учёта склада и появляются в ``shelf_items``.
          Приход на склад kind='shelf' не пишем — продажа со стеллажа регистр
          не трогает, и остаток иначе зависал бы в «Товарах» навсегда;
        • стеллаж получает приход штук с себестоимостью по средней складской.

        Позиция стеллажа находится по item_id, по связке nomenclature.
        legacy_shelf_id или по имени; если её нет — создаётся автоматически.
        """
        nom = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not nom:
            raise ValueError("Товар не найден в номенклатуре")
        if not warehouse_id:
            raise ValueError("Укажите склад-источник")
        unit = str(nom.get("unit") or "шт")
        piece_unit = unit in ("шт", "шт.", "piece", "pcs")
        qty = num(qty)
        if qty <= 0:
            raise ValueError("Перемещать нужно больше нуля")
        if piece_unit and (abs(qty - round(qty)) > 1e-9):
            raise ValueError("Штучные товары перемещаются целыми штуками")
        qty = float(round(qty)) if piece_unit else round(qty, 3)
        from .stock import Stock
        stock = Stock(self.db)
        available = stock.qty(nom_id, warehouse_id)
        if available < qty:
            raise ValueError(f"На складе только {round(available, 3)} {unit}, "
                             f"а переместить просят {round(qty, 3)} {unit}")
        if piece_unit and available < 1:
            raise ValueError(f"На складе только {round(available, 1)} {unit}, "
                             f"а переместить просят {round(qty)} {unit}")
        display_only = str(nom.get("kind") or "product") == "showcase"
        # Витрина без производственного учёта: даже если при оприходовании
        # указали цену, себестоимость на стеллаже остаётся нулевой.
        unit_cost = 0.0 if display_only else stock.avg_cost(nom_id, warehouse_id)
        cost = round(unit_cost * qty, 2)
        # Расход со склада-источника. На стеллаж — только shelf_items, без
        # второго движения на склад kind='shelf'.
        stock.add_move(nom_id, warehouse_id, -qty, -cost, doc_kind="move",
                       note=note or "перемещение на стеллаж")
        # 2) позиция стеллажа: найти или создать
        item = None
        if item_id:
            item = self.db.one("SELECT * FROM shelf_items WHERE id=?", (item_id,))
        if not item and nom.get("legacy_shelf_id"):
            item = self.db.one("SELECT * FROM shelf_items WHERE id=? AND active=1",
                               (nom["legacy_shelf_id"],))
        if not item:
            item = self.db.one(
                "SELECT * FROM shelf_items WHERE active=1 AND lower(name)=lower(?)",
                (str(nom.get("name") or ""),))
        if not item:
            from .nomenclature import Nomenclature
            price = 0.0
            try:
                prices = Nomenclature(self.db)._all_prices().get(nom_id) or {}
                price = num(prices.get(Nomenclature(self.db)._base_type()))
            except Exception:
                price = 0.0
            item = self.save_item({
                "name": nom.get("name") or "Товар со склада",
                "nom_id": nom_id,
                "barcode": nom.get("barcode") or "",
                "sku": nom.get("sku") or nom.get("code") or "",
                "price": price, "cost_per_unit": unit_cost,
                "photo": nom.get("photo") or "",
                "note": "создано перемещением со склада",
            })
        if unit_cost and not num(item.get("cost_per_unit")):
            self.db.execute("UPDATE shelf_items SET cost_per_unit=? WHERE id=?",
                            (round(unit_cost, 2), item["id"]))
        move = self._move(item["id"], "produce", qty,
                          note=(note or f"перемещение со склада "
                                        f"«{warehouse_id}»").strip())
        self.db.add_event("shelf", "Перемещение со склада на стеллаж",
                          f"{nom.get('name') or ''} +{round(qty)} шт",
                          data={"nom_id": nom_id, "warehouse_id": warehouse_id,
                                "item_id": item["id"], "qty": qty, "cost": cost})
        return {"ok": True, "move": move, "qty": qty, "cost": cost,
                "item": self.db.one("SELECT * FROM shelf_items WHERE id=?",
                                    (item["id"],))}

    def create_item_from_stock(self, data: dict, nom_id: str, warehouse_id: str,
                               qty: float) -> dict:
        """Новая позиция стеллажа сразу с готовым товаром со склада.

        Создаёт позицию по полям формы (название, цена ценника, ценник,
        заметка…) и в одной транзакции переносит готовые штуки с учётного
        склада на полку: остаток и себестоимость приходят движением, а не
        «начальным остатком» без проводки. Пустые название, цена,
        себестоимость и штрихкод берутся из номенклатуры.
        """
        from .nomenclature import Nomenclature
        from .stock import Stock
        nom = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not nom:
            raise ValueError("Товар не найден в номенклатуре")
        if not warehouse_id:
            raise ValueError("Укажите склад-источник")
        qty = num(qty)
        if qty <= 0:
            raise ValueError("Переносить нужно больше нуля")
        data = dict(data)
        data["id"] = ""  # всегда новая позиция
        if not str(data.get("name") or "").strip():
            data["name"] = str(nom.get("name") or "Товар со склада").strip()
        if not data.get("nom_id"):
            data["nom_id"] = nom_id
        if not str(data.get("barcode") or "").strip():
            data["barcode"] = str(nom.get("barcode") or "").strip()
        if not str(data.get("sku") or "").strip():
            data["sku"] = str(nom.get("sku") or nom.get("code") or "").strip()
        if not num(data.get("price")):
            try:
                prices = Nomenclature(self.db)._prices_of(nom_id)
                data["price"] = num(prices.get(Nomenclature(self.db)._base_type()))
            except Exception:
                data["price"] = 0.0
        display_only = str(nom.get("kind") or "product") == "showcase"
        unit_cost = 0.0 if display_only else Stock(self.db).avg_cost(nom_id, warehouse_id)
        if not num(data.get("cost_per_unit")):
            data["cost_per_unit"] = round(unit_cost, 2)
        data["qty"] = 0  # остаток придёт переносом
        with self.db.transaction():
            item = self.save_item(data)
            moved = self.transfer_from_stock(nom_id, warehouse_id, qty,
                                             item_id=item["id"],
                                             note="перенос готового товара при создании позиции")
            return {"ok": True, "item": self.item(item["id"]),
                    "move": moved.get("move"), "qty": moved.get("qty"),
                    "cost": moved.get("cost")}

    # ------------------------------------------------------------- QR-ценник
    def qr_link(self, item_id: str, host: str = "", public_url: str = "",
                listen_port: int = 8080) -> dict:
        """URL страницы позиции для QR-ценника (телефон в той же сети).

        Раньше подставлялся Host текущего запроса — если панель открыта как
        localhost, в QR попадал localhost и телефон его не открывал.
        Теперь берём LAN-IP (или настройку public_url).
        """
        from .config import public_page_url
        from urllib.parse import quote
        return public_page_url(
            "/shelf.html", f"id={quote(str(item_id), safe='')}",
            host_header=host, public_url=public_url, listen_port=listen_port)
