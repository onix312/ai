"""Укладка готового товара заказа на учётный склад.

Это не выдача клиенту и не продажа: готовое изделие из заказа, доведённого
до статуса «Готов», оприходуется в учётный регистр склада (3.0) документом
прихода. Заказ при этом закрывается финальным статусом «На складе», оплата
и долг остаются под контролем мастера — никакой автоматической кассы здесь
нет. Так же закрываются заказы «напечатали на склад» — производство для
пополнения запаса, а не под конкретного покупателя.

Особенности:
    • документ проводится в той же транзакции, что и закрытие заказа;
    • повторный запрос безопасен (идемпотентен);
    • активный резерв заказа снимается, но не списывается как продажа.
"""
from __future__ import annotations

from typing import Any

from .accounting import Accounting, num
from .config import now_iso
from .db import Database
from .repo import Repo
from .stock import Stock
from .documents import Documents


class OrderStocker:
    """Единый сервис «положить готовый заказ на склад»."""

    def __init__(self, db: Database, repo: Repo, stock: Stock,
                 documents: Documents, accounting: Accounting):
        self.db = db
        self.repo = repo
        self.stock = stock
        self.docs = documents
        self.acc = accounting

    # ------------------------------------------------------------- состав
    def _items(self, order: dict) -> list[dict]:
        """Позиции готового товара: состав мультизаказа или одиночное изделие."""
        rows = self.db.query(
            "SELECT * FROM order_items WHERE order_id=? ORDER BY position",
            (order.get("id"),))
        if rows:
            return [{
                "nom_id": r.get("nom_id") or "",
                "name": r.get("name") or "",
                "qty": max(0.0, num(r.get("qty"))),
                "grams": num(r.get("grams")),
                "hours": num(r.get("hours")),
                "price": num(r.get("price")),
            } for r in rows]
        nom_id = str(order.get("nom_id") or "").strip()
        if not nom_id:
            return []
        nom = self.db.one("SELECT name, grams, hours FROM nomenclature WHERE id=?",
                          (nom_id,)) or {}
        return [{
            "nom_id": nom_id,
            "name": nom.get("name") or str(order.get("product") or ""),
            "qty": max(0.0, num(order.get("qty"), 1)),
            "grams": num(order.get("grams")) or num(nom.get("grams")),
            "hours": num(order.get("hours")) or num(nom.get("hours")),
            "price": num(order.get("price")),
        }]

    def _unit_cost(self, order: dict, item: dict, index: int, total: int) -> float:
        """Себестоимость штуки для строки документа прихода."""
        qty = max(1.0, num(item.get("qty"), 1))
        total_cost = num(order.get("actual_cost")) or num(order.get("cost"))
        if total_cost > 0:
            if total > 1:
                # Мультизаказ: доля по весу (иначе поровну), как в экономике.
                eco = self.acc.order_items_economics(order)
                unit = eco[index].get("cost", 0.0) if index < len(eco) else 0.0
                return round(num(unit) / qty, 2) if num(unit) else 0.0
            return round(total_cost / qty, 2)
        if item.get("grams") or item.get("hours"):
            br = self.acc.cost_breakdown(
                num(item.get("grams")) * qty, num(item.get("hours")) * qty, qty=qty)
            return round(num(br["total"]) / max(1.0, qty), 2)
        return 0.0

    # ----------------------------------------------------------- чтение
    def summary(self, order_id: str) -> dict[str, Any]:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        status = self.db.one("SELECT * FROM statuses WHERE id=?",
                             (order.get("status"),)) or {}
        final = bool(num(status.get("is_final")))

        items = self._items(order)
        missing = any(not it.get("nom_id") for it in items)
        items = [it for it in items if it.get("nom_id") and it.get("qty") > 0]

        blocks: list[dict] = []
        if not final and order.get("status") != "ready":
            blocks.append({
                "code": "status",
                "text": "Сначала примите результат и переведите заказ в статус «Готов»",
            })
        if not final and (not items or missing):
            blocks.append({
                "code": "goods",
                "text": "В заказе не указано готовое изделие из базы товаров",
            })

        warehouses = self.db.query(
            "SELECT id,name FROM warehouses WHERE archived=0 ORDER BY position, name")
        default_wh = str(order.get("warehouse_id") or "").strip()
        if not default_wh:
            cand = self.db.one(
                "SELECT id FROM warehouses WHERE archived=0 ORDER BY position LIMIT 1")
            default_wh = (cand or {}).get("id") or ""

        return {
            "ok": True,
            "order_id": order_id,
            "number": order.get("number") or "",
            "status": {"id": order.get("status") or "",
                       "name": status.get("name") or ""},
            "stocked": final,
            "already_final": final,
            "can_stock": not final and not blocks,
            "items": items,
            "quantity": round(sum(num(it.get("qty")) for it in items), 2),
            "warehouse_id": default_wh,
            "warehouses": warehouses,
            "blocks": blocks,
        }

    # ------------------------------------------------------------ запись
    def stock_to_warehouse(
        self,
        order_id: str,
        *,
        warehouse_id: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Атомарно оприходовать готовое изделие на склад и закрыть заказ."""
        with self.db.transaction():
            summary = self.summary(order_id)
            if summary["stocked"]:
                summary["already_stocked"] = True
                return summary
            if summary["blocks"]:
                raise ValueError("Нельзя положить на склад: " + "; ".join(
                    item["text"] for item in summary["blocks"]
                ))

            wh = str(warehouse_id or "").strip() or summary["warehouse_id"]
            if not wh:
                raise ValueError("Не настроен ни один склад")

            # Если товар был зарезервирован, он уже лежит в регистре: снимаем
            # резерв и не создаём второй приход, иначе остаток удвоится.
            reserve = self.db.one(
                "SELECT id FROM reserves WHERE order_id=? AND state='active' LIMIT 1",
                (order_id,))
            doc = None
            if reserve:
                self.stock.release(order_id=order_id)
            else:
                order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,)) or {}
                items = summary["items"]
                lines = []
                total = len(items)
                for index, item in enumerate(items):
                    unit_cost = self._unit_cost(order, item, index, total)
                    lines.append({
                        "nom_id": item["nom_id"],
                        "qty": num(item["qty"]),
                        "cost": unit_cost,
                        "price": num(item["price"]) or unit_cost,
                    })
                doc = self.docs.save({
                    "kind": "receipt",
                    "warehouse_id": wh,
                    "order_id": order_id,
                    "note": note or f"Готовый заказ №{summary['number'] or ''}".strip(),
                    "at": now_iso(),
                    "items": lines,
                })
                doc = self.docs.post(doc["id"])

            final = self.db.one(
                "SELECT id FROM statuses WHERE id='stocked'")
            if not final:
                final = self.db.one(
                    "SELECT id FROM statuses WHERE is_final=1 ORDER BY position LIMIT 1")
            if not final:
                raise ValueError("Не настроен финальный статус заказа")

            self.repo.save_order({
                "id": order_id,
                "status": final["id"],
                "reserved": 0,
                "_allow_final_status": True,
                "_skip_auto_income": True,
                "author": "order-to-warehouse",
            })

            self.db.add_event(
                "order",
                "Готовый заказ на склад",
                f"№{summary['number']} · " + (
                    f"{doc.get('number')} · " if doc else "резерв снят · "
                ) + f"{round(summary['quantity'], 1)} шт",
                data={"order_id": order_id, "doc_id": doc.get("id") if doc else "",
                      "warehouse_id": wh},
            )

            result = self.summary(order_id)
            result["already_stocked"] = False
            result["document"] = {
                "id": doc.get("id") if doc else "",
                "number": doc.get("number") if doc else "",
                "kind": doc.get("kind") if doc else "release",
                "warehouse_id": wh,
            }
            result["order"] = self.repo.order(order_id)
            return result
