"""Документы PrintFlow 3.0 — как в 1С: провёл, распровёл, перепровёл.

Документ сам по себе ничего не меняет: пока он «черновик», это только
намерение. Движения по складу и деньги появляются при проведении и
исчезают при распроведении. Поэтому ошибку всегда можно откатить, а
история остаётся полной.

Виды:
    receipt     приход на склад (закупка, ручное оприходование)
    sale        продажа со склада (розница, онлайн, отгрузка по заказу)
    move        перемещение между складами
    writeoff    списание (брак, порча, подарок)
    inventory   инвентаризация (факт против учёта)
    production  производство (выпуск + списание по спецификации)
    return      возврат от покупателя
    pricing     установка цен (склад не трогает)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso
from .db import Database
from .schema_v3 import DOC_KINDS
from .stock import Stock


class Documents:
    """Создание, проведение и отмена документов."""

    def __init__(self, db: Database):
        self.db = db
        self.stock = Stock(db)
        self.acc = Accounting(db)

    # ------------------------------------------------------------ нумерация
    def next_number(self, kind: str) -> str:
        prefix = (DOC_KINDS.get(kind) or ("", "ДК", True))[1]
        year = datetime.now().strftime("%y")
        with self.db.lock:
            row = self.db.one("SELECT last FROM doc_counters WHERE kind=? AND year=?",
                              (kind, year))
            last = int(num((row or {}).get("last"))) + 1
            self.db.execute(
                "INSERT INTO doc_counters(kind,year,last) VALUES(?,?,?)"
                " ON CONFLICT(kind,year) DO UPDATE SET last=excluded.last",
                (kind, year, last))
        return f"{prefix}-{year}-{last:05d}"

    # --------------------------------------------------------------- чтение
    def list(self, kind: str = "", state: str = "", warehouse_id: str = "",
             search: str = "", limit: int = 200, order_id: str = "") -> list[dict]:
        sql = ("SELECT d.*, w.name warehouse_name, w2.name warehouse_to_name,"
               " c.name counterparty_name FROM documents d"
               " LEFT JOIN warehouses w ON w.id=d.warehouse_id"
               " LEFT JOIN warehouses w2 ON w2.id=d.warehouse_to_id"
               " LEFT JOIN customers c ON c.id=d.counterparty_id WHERE 1=1")
        params: list[Any] = []
        if kind:
            sql += " AND d.kind=?"
            params.append(kind)
        if state:
            sql += " AND d.state=?"
            params.append(state)
        if warehouse_id:
            sql += " AND (d.warehouse_id=? OR d.warehouse_to_id=?)"
            params += [warehouse_id, warehouse_id]
        if order_id:
            sql += " AND d.order_id=?"
            params.append(order_id)
        if search:
            sql += " AND (pylower(d.number) LIKE ? OR pylower(d.note) LIKE ?)"
            like = f"%{search.lower()}%"
            params += [like, like]
        sql += " ORDER BY datetime(d.at) DESC, d.rowid DESC LIMIT ?"
        params.append(int(limit))
        rows = self.db.query(sql, params)
        for row in rows:
            row["kind_label"] = (DOC_KINDS.get(row["kind"]) or ("Документ",))[0]
            cnt = self.db.one("SELECT COUNT(*) n FROM doc_items WHERE doc_id=?",
                              (row["id"],)) or {}
            row["lines"] = int(num(cnt.get("n")))
        return rows

    def get(self, doc_id: str) -> dict | None:
        doc = self.db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
        if not doc:
            return None
        doc["kind_label"] = (DOC_KINDS.get(doc["kind"]) or ("Документ",))[0]
        doc["items"] = self.db.query(
            "SELECT i.*, n.name nom_name, n.code nom_code, n.unit"
            " FROM doc_items i LEFT JOIN nomenclature n ON n.id=i.nom_id"
            " WHERE i.doc_id=? ORDER BY i.line, i.rowid", (doc_id,))
        doc["moves"] = self.db.query(
            "SELECT m.*, n.name nom_name FROM stock_moves m"
            " LEFT JOIN nomenclature n ON n.id=m.nom_id"
            " WHERE m.doc_id=? ORDER BY m.rowid", (doc_id,))
        return doc

    # --------------------------------------------------------------- запись
    def save(self, data: dict) -> dict:
        """Сохранить документ вместе с табличной частью (только черновик)."""
        data = dict(data)
        items = data.pop("items", None)
        doc_id = data.get("id") or ""
        existing = self.db.one("SELECT * FROM documents WHERE id=?", (doc_id,)) if doc_id else None
        if existing and existing["state"] == "posted":
            raise ValueError("Документ проведён. Сначала отмените проведение.")

        kind = data.get("kind") or (existing or {}).get("kind") or "receipt"
        if kind not in DOC_KINDS:
            raise ValueError(f"Неизвестный вид документа: {kind}")

        if not doc_id:
            data["id"] = uid("doc")
            data["created_at"] = now_iso()
            data.setdefault("at", now_iso())
            data.setdefault("state", "draft")
            data["number"] = data.get("number") or self.next_number(kind)
        data["kind"] = kind

        with self.db.transaction():
            doc = self.db.upsert("documents", {**(existing or {}), **data})
            if items is not None:
                self.db.execute("DELETE FROM doc_items WHERE doc_id=?", (doc["id"],))
                for index, item in enumerate(items):
                    if not isinstance(item, dict) or not item.get("nom_id"):
                        continue
                    qty = num(item.get("qty"))
                    price = num(item.get("price"))
                    self.db.upsert("doc_items", {
                        "id": item.get("id") or uid("di"), "doc_id": doc["id"],
                        "line": index + 1, "nom_id": item["nom_id"],
                        "variant_id": item.get("variant_id") or None,
                        "qty": round(qty, 3),
                        "qty_fact": round(num(item.get("qty_fact")), 3),
                        "price": round(price, 2),
                        "cost": round(num(item.get("cost")), 2),
                        "amount": round(qty * price, 2),
                        "note": item.get("note", "")})
                self._refresh_totals(doc["id"])
        self._audit(doc["id"], "save", f"{doc['kind_label'] if 'kind_label' in doc else kind} {doc.get('number')}")
        return self.get(doc["id"]) or {}

    def delete(self, doc_id: str) -> None:
        doc = self.db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
        if not doc:
            raise ValueError("Документ не найден")
        if doc["state"] == "posted":
            raise ValueError("Нельзя удалить проведённый документ")
        with self.db.transaction():
            self.db.execute("DELETE FROM doc_items WHERE doc_id=?", (doc_id,))
            self.db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        self._audit(doc_id, "delete", f"Удалён документ {doc.get('number')}")

    def _refresh_totals(self, doc_id: str) -> None:
        row = self.db.one(
            "SELECT COALESCE(SUM(qty),0) q, COALESCE(SUM(amount),0) a"
            " FROM doc_items WHERE doc_id=?", (doc_id,)) or {}
        self.db.execute("UPDATE documents SET qty_total=?, amount=? WHERE id=?",
                        (round(num(row.get("q")), 3), round(num(row.get("a")), 2), doc_id))

    # ------------------------------------------------------------ проведение
    def post(self, doc_id: str) -> dict:
        """Провести документ: создать движения склада и деньги."""
        doc = self.get(doc_id)
        if not doc:
            raise ValueError("Документ не найден")
        if doc["state"] == "posted":
            raise ValueError("Документ уже проведён")
        items = doc.get("items") or []
        if doc["kind"] != "pricing" and not items:
            raise ValueError("В документе нет ни одной строки")

        handler = {
            "receipt": self._post_receipt, "sale": self._post_sale,
            "move": self._post_move, "writeoff": self._post_writeoff,
            "inventory": self._post_inventory, "production": self._post_production,
            "return": self._post_return, "pricing": self._post_pricing,
        }.get(doc["kind"])
        if not handler:
            raise ValueError(f"Не умею проводить: {doc['kind']}")

        with self.db.transaction():
            cost_total = handler(doc, items)
            self.db.execute(
                "UPDATE documents SET state='posted', posted_at=?, cost_total=? WHERE id=?",
                (now_iso(), round(num(cost_total), 2), doc_id))
        self.db.add_event("doc", f"{doc['kind_label']} проведён",
                          f"{doc.get('number')} · {round(num(doc.get('qty_total')), 1)} шт",
                          data={"doc_id": doc_id})
        self._audit(doc_id, "post", f"Проведён {doc.get('number')}")
        return self.get(doc_id) or {}

    def unpost(self, doc_id: str) -> dict:
        """Отменить проведение: снять движения и вернуть в черновики."""
        doc = self.db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
        if not doc:
            raise ValueError("Документ не найден")
        if doc["state"] != "posted":
            raise ValueError("Документ и так не проведён")
        with self.db.transaction():
            self.stock.drop_doc_moves(doc_id)
            if doc.get("tx_id"):
                self.db.execute("DELETE FROM transactions WHERE id=?", (doc["tx_id"],))
                self.db.execute("UPDATE documents SET tx_id=NULL WHERE id=?", (doc_id,))
            if doc["kind"] == "pricing":
                self.db.execute("DELETE FROM prices WHERE doc_id=?", (doc_id,))
            self.db.execute("UPDATE documents SET state='draft', posted_at=NULL WHERE id=?",
                            (doc_id,))
        self._audit(doc_id, "unpost", f"Отменено проведение {doc.get('number')}")
        return self.get(doc_id) or {}

    # ------------------------------------------------------- виды документов
    def _post_receipt(self, doc: dict, items: list[dict]) -> float:
        total = 0.0
        wh = doc.get("warehouse_id") or self._default_warehouse()
        for item in items:
            cost = num(item.get("cost")) or num(item.get("price"))
            amount = cost * num(item["qty"])
            self.stock.add_move(item["nom_id"], wh, num(item["qty"]), amount,
                                doc["id"], "receipt", item.get("variant_id") or "",
                                note=doc.get("note", ""), at=doc.get("at", ""))
            total += amount
        return total

    def _post_sale(self, doc: dict, items: list[dict]) -> float:
        wh = doc.get("warehouse_id") or self._default_warehouse()
        cost_total = 0.0
        revenue = 0.0
        for item in items:
            qty = num(item["qty"])
            available = self.stock.qty(item["nom_id"], wh)
            if available < qty:
                name = item.get("nom_name") or "позиция"
                raise ValueError(f"«{name}»: на складе {round(available, 1)} шт, продаём {round(qty, 1)}")
            unit_cost = num(item.get("cost")) or self.stock.avg_cost(item["nom_id"], wh)
            cost = unit_cost * qty
            self.stock.add_move(item["nom_id"], wh, -qty, -cost, doc["id"], "sale",
                                item.get("variant_id") or "", note=doc.get("note", ""),
                                at=doc.get("at", ""))
            cost_total += cost
            revenue += num(item.get("amount")) or num(item.get("price")) * qty
        revenue -= num(doc.get("discount"))
        if revenue > 0:
            tx = self.acc.add_transaction(
                "income", "sale", revenue,
                f"Продажа {doc.get('number')}",
                note=doc.get("note", ""), auto=False,
                channel=doc.get("channel") or "shop", payer="person",
                account_id=doc.get("account_id") or "")
            self.db.execute("UPDATE documents SET tx_id=? WHERE id=?", (tx["id"], doc["id"]))
        if doc.get("order_id"):
            self.stock.release(order_id=doc["order_id"])
        return cost_total

    def _post_move(self, doc: dict, items: list[dict]) -> float:
        src = doc.get("warehouse_id")
        dst = doc.get("warehouse_to_id")
        if not src or not dst:
            raise ValueError("Укажите склад-источник и склад-получатель")
        if src == dst:
            raise ValueError("Склады совпадают — перемещать некуда")
        total = 0.0
        for item in items:
            qty = num(item["qty"])
            available = self.stock.qty(item["nom_id"], src)
            if available < qty:
                name = item.get("nom_name") or "позиция"
                raise ValueError(f"«{name}»: на складе-источнике {round(available, 1)} шт")
            unit_cost = self.stock.avg_cost(item["nom_id"], src)
            cost = unit_cost * qty
            self.stock.add_move(item["nom_id"], src, -qty, -cost, doc["id"], "move",
                                item.get("variant_id") or "", note="перемещение",
                                at=doc.get("at", ""))
            self.stock.add_move(item["nom_id"], dst, qty, cost, doc["id"], "move",
                                item.get("variant_id") or "", note="перемещение",
                                at=doc.get("at", ""))
            total += cost
        return total

    def _post_writeoff(self, doc: dict, items: list[dict]) -> float:
        wh = doc.get("warehouse_id") or self._default_warehouse()
        total = 0.0
        for item in items:
            qty = num(item["qty"])
            available = self.stock.qty(item["nom_id"], wh)
            if available < qty:
                name = item.get("nom_name") or "позиция"
                raise ValueError(f"«{name}»: списываем {round(qty, 1)}, а есть {round(available, 1)}")
            unit_cost = self.stock.avg_cost(item["nom_id"], wh)
            cost = unit_cost * qty
            self.stock.add_move(item["nom_id"], wh, -qty, -cost, doc["id"], "writeoff",
                                item.get("variant_id") or "",
                                note=doc.get("reason") or "списание", at=doc.get("at", ""))
            total += cost
        if total > 0:
            self.acc.add_transaction(
                "expense", "other", total,
                f"Списание {doc.get('number')}",
                note=doc.get("reason", ""), auto=True)
        return total

    def _post_inventory(self, doc: dict, items: list[dict]) -> float:
        wh = doc.get("warehouse_id") or self._default_warehouse()
        total = 0.0
        for item in items:
            fact = num(item.get("qty_fact"))
            expected = self.stock.qty(item["nom_id"], wh)
            diff = round(fact - expected, 3)
            if not diff:
                continue
            unit_cost = self.stock.avg_cost(item["nom_id"], wh)
            cost = unit_cost * diff
            self.stock.add_move(item["nom_id"], wh, diff, cost, doc["id"], "inventory",
                                item.get("variant_id") or "",
                                note=f"инвентаризация: учёт {round(expected, 1)}, факт {round(fact, 1)}",
                                at=doc.get("at", ""))
            total += abs(cost)
        return total

    def _post_production(self, doc: dict, items: list[dict]) -> float:
        """Выпуск продукции: приход готового + списание по спецификации."""
        wh = doc.get("warehouse_id") or self._default_warehouse()
        total = 0.0
        for item in items:
            qty = num(item["qty"])
            unit_cost = num(item.get("cost"))
            components = self._spec_components(item["nom_id"])
            comp_cost = 0.0
            for comp in components:
                need = num(comp["qty"]) * qty
                if need <= 0:
                    continue
                comp_unit = self.stock.avg_cost(comp["nom_id"], wh)
                comp_sum = comp_unit * need
                available = self.stock.qty(comp["nom_id"], wh)
                if available < need:
                    raise ValueError(
                        f"Не хватает «{comp.get('name') or comp['nom_id']}»:"
                        f" нужно {round(need, 1)}, есть {round(available, 1)}")
                self.stock.add_move(comp["nom_id"], wh, -need, -comp_sum, doc["id"],
                                    "production", note="списано в производство",
                                    at=doc.get("at", ""))
                comp_cost += comp_sum
            cost = (unit_cost * qty) if unit_cost else comp_cost
            self.stock.add_move(item["nom_id"], wh, qty, cost, doc["id"], "production",
                                item.get("variant_id") or "",
                                batch_id=doc.get("batch_id") or "",
                                note="выпуск продукции", at=doc.get("at", ""))
            total += cost
        return total

    def _post_return(self, doc: dict, items: list[dict]) -> float:
        wh = doc.get("warehouse_id") or self._default_warehouse()
        total = 0.0
        refund = 0.0
        for item in items:
            qty = num(item["qty"])
            unit_cost = num(item.get("cost")) or self.stock.avg_cost(item["nom_id"], wh)
            cost = unit_cost * qty
            self.stock.add_move(item["nom_id"], wh, qty, cost, doc["id"], "return",
                                item.get("variant_id") or "", note="возврат от покупателя",
                                at=doc.get("at", ""))
            total += cost
            refund += num(item.get("amount")) or num(item.get("price")) * qty
        if refund > 0:
            tx = self.acc.add_transaction(
                "expense", "other", refund, f"Возврат {doc.get('number')}",
                note=doc.get("reason", ""), auto=False,
                account_id=doc.get("account_id") or "")
            self.db.execute("UPDATE documents SET tx_id=? WHERE id=?", (tx["id"], doc["id"]))
        return total

    def _post_pricing(self, doc: dict, items: list[dict]) -> float:
        price_type = doc.get("price_type_id") or "retail"
        for item in items:
            self.db.upsert("prices", {
                "id": uid("prc"), "at": doc.get("at") or now_iso(),
                "nom_id": item["nom_id"], "variant_id": item.get("variant_id") or None,
                "price_type_id": price_type, "price": round(num(item.get("price")), 2),
                "doc_id": doc["id"], "note": doc.get("note", "")})
        return 0.0

    # ------------------------------------------------------------- помощники
    def _spec_components(self, nom_id: str) -> list[dict]:
        spec = self.db.one(
            "SELECT * FROM specs WHERE nom_id=? AND active=1 ORDER BY rowid LIMIT 1",
            (nom_id,))
        if not spec:
            return []
        return self.db.query(
            "SELECT si.*, n.name FROM spec_items si"
            " LEFT JOIN nomenclature n ON n.id=si.nom_id"
            " WHERE si.spec_id=? ORDER BY si.line", (spec["id"],))

    def _default_warehouse(self) -> str:
        row = self.db.one(
            "SELECT id FROM warehouses WHERE archived=0 ORDER BY position LIMIT 1")
        if not row:
            raise ValueError("Не настроен ни один склад")
        return row["id"]

    def _audit(self, entity_id: str, action: str, title: str) -> None:
        self.db.execute(
            "INSERT INTO audit_log(at,entity,entity_id,action,title) VALUES(?,?,?,?,?)",
            (now_iso(), "document", entity_id, action, title))

    # ------------------------------------------------- быстрые операции
    def quick_sale(self, rows: list[dict], warehouse_id: str = "",
                   channel: str = "shop", account_id: str = "",
                   note: str = "", discount: float = 0.0) -> dict:
        """Розничная продажа в одно действие: создать и сразу провести."""
        items = []
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("nom_id"):
                continue
            qty = num(row.get("qty"))
            if qty <= 0:
                continue
            price = num(row.get("price"))
            if price <= 0:
                price = self.price_of(row["nom_id"])
            items.append({"nom_id": row["nom_id"], "qty": qty, "price": price,
                          "variant_id": row.get("variant_id", "")})
        if not items:
            raise ValueError("Не выбрано ни одной позиции")
        doc = self.save({"kind": "sale", "warehouse_id": warehouse_id,
                         "channel": channel, "account_id": account_id,
                         "discount": num(discount), "note": note, "items": items})
        return self.post(doc["id"])

    def quick_receipt(self, nom_id: str, qty: float, cost: float = 0.0,
                      warehouse_id: str = "", batch_id: str = "",
                      note: str = "") -> dict:
        """Приход одной позиции: создать и сразу провести."""
        doc = self.save({"kind": "receipt", "warehouse_id": warehouse_id,
                         "batch_id": batch_id, "note": note,
                         "items": [{"nom_id": nom_id, "qty": num(qty),
                                    "cost": num(cost), "price": num(cost)}]})
        return self.post(doc["id"])

    def _order_lines(self, order: dict) -> list[dict]:
        """Строки накладной: состав мультизаказа или одиночное изделие."""
        rows = self.db.query(
            "SELECT * FROM order_items WHERE order_id=? ORDER BY position",
            (order.get("id"),))
        if rows:
            return [{
                "nom_id": r.get("nom_id") or "",
                "name": r.get("name") or "",
                "qty": max(0.0, num(r.get("qty"))),
                "price": num(r.get("price")),
            } for r in rows]
        nom_id = str(order.get("nom_id") or "").strip()
        if not nom_id:
            return []
        nom = self.db.one("SELECT name FROM nomenclature WHERE id=?", (nom_id,)) or {}
        qty = max(0.0, num(order.get("qty"), 1))
        total = num(order.get("price"))
        unit = round(total / qty, 2) if qty else total
        return [{
            "nom_id": nom_id,
            "name": nom.get("name") or str(order.get("product") or ""),
            "qty": qty,
            "price": unit,
        }]

    def for_order(self, order_id: str) -> dict[str, Any]:
        """Складские документы заказа и доступные печатные формы."""
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        docs = self.list(order_id=order_id, limit=50)
        lines = [it for it in self._order_lines(order) if it.get("nom_id") and it.get("qty") > 0]
        existing_sale = next((d for d in docs if d.get("kind") == "sale"), None)
        return {
            "ok": True,
            "order_id": order_id,
            "number": order.get("number") or "",
            "documents": docs,
            "can_create_waybill": bool(lines),
            "has_waybill": bool(existing_sale),
            "waybill_id": (existing_sale or {}).get("id") or "",
            "print": [
                {"kind": "invoice", "title": "Счёт на оплату"},
                {"kind": "cp", "title": "Коммерческое предложение"},
                {"kind": "receipt", "title": "Товарный чек"},
                {"kind": "waybill", "title": "Товарная накладная"},
            ],
        }

    def waybill_from_order(self, order_id: str, warehouse_id: str = "",
                           post: bool = False) -> dict:
        """Создать расходную накладную (продажу) по составу заказа.

        Повторный вызов возвращает уже существующую накладную заказа.
        Проведение не выполняется само: на складе может не быть остатка.
        """
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        existing = self.db.one(
            "SELECT id FROM documents WHERE order_id=? AND kind='sale' "
            "ORDER BY datetime(at) DESC, rowid DESC LIMIT 1",
            (order_id,))
        if existing:
            doc = self.get(existing["id"]) or {}
            if post and doc.get("state") != "posted":
                return self.post(doc["id"])
            return doc

        lines = [it for it in self._order_lines(order)
                 if it.get("nom_id") and it.get("qty") > 0]
        if not lines:
            raise ValueError(
                "В заказе нет позиций из базы товаров — накладную не из чего собрать")

        wh = str(warehouse_id or order.get("warehouse_id") or "").strip()
        items = []
        for line in lines:
            items.append({
                "nom_id": line["nom_id"],
                "qty": num(line["qty"]),
                "price": num(line["price"]) or self.price_of(line["nom_id"]),
            })
        note = f"Накладная по заказу №{order.get('number') or ''}".strip()
        doc = self.save({
            "kind": "sale",
            "warehouse_id": wh,
            "order_id": order_id,
            "counterparty_id": order.get("customer_id") or "",
            "channel": order.get("channel") or "",
            "note": note,
            "items": items,
        })
        if post:
            return self.post(doc["id"])
        return doc

    def price_of(self, nom_id: str, price_type_id: str = "") -> float:
        """Текущая цена позиции по типу цен (последняя установленная)."""
        if not price_type_id:
            base = self.db.one("SELECT id FROM price_types WHERE is_base=1 LIMIT 1")
            price_type_id = (base or {}).get("id") or "retail"
        row = self.db.one(
            "SELECT price FROM prices WHERE nom_id=? AND price_type_id=?"
            " ORDER BY datetime(at) DESC, rowid DESC LIMIT 1", (nom_id, price_type_id))
        return round(num((row or {}).get("price")), 2)
