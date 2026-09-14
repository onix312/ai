"""Цифровая нить заказа (идея 12): заказ → печать → полка → продажа → отзыв.

Отдельный модуль, а не часть контент-студии: нить показывает оператору
всё, что случилось с изделием, и её читает карточка заказа
(`site/assets/ops.js`). Тексты постов и генераторы публикаций жили рядом,
но к заказу отношения не имеют.
"""
from __future__ import annotations

from typing import Any

from .accounting import num
from .config import now_iso
from .db import Database


def order_thread(db: Database, order_id: str) -> dict[str, Any]:
    """Цифровая нить изделия (идея 12): заказ → печать → полка → продажа → отзыв."""
    order = db.one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not order:
        raise ValueError("Заказ не найден")
    jobs = db.query("SELECT * FROM print_jobs WHERE order_id=?"
                    " ORDER BY created_at", (order_id,))
    product = str(order.get("product") or "")
    shelf_item = None
    if product:
        shelf_item = db.one("SELECT * FROM shelf_items WHERE active=1"
                            " AND lower(name)=lower(?) LIMIT 1", (product,))
    shelf_sales = []
    if shelf_item:
        shelf_sales = db.query(
            "SELECT at, qty, price, kind FROM shelf_moves WHERE item_id=?"
            " AND kind IN ('sale','online') AND at>=? ORDER BY at DESC LIMIT 10",
            (shelf_item["id"], str(order.get("created_at") or "")[:10]))
    income = db.query("SELECT at, amount, category FROM transactions"
                      " WHERE kind='income' AND order_id=? ORDER BY at", (order_id,))
    feedback = db.one(
        "SELECT rating, feedback_text, feedback_received_at, publish_permission"
        " FROM customer_feedback WHERE order_id=? ORDER BY created_at DESC", (order_id,))
    return {
        "order": {
            "id": order["id"], "number": order.get("number"),
            "product": product, "customer_name": order.get("customer_name"),
            "status": order.get("status"), "price": num(order.get("price")),
            "created_at": order.get("created_at") or "",
            "gift": bool(order.get("gift")),
        },
        "print": [{
            "id": j["id"], "name": j.get("name") or "", "state": j.get("state"),
            "grams": num(j.get("grams")), "duration_min": num(j.get("duration_min")),
            "started_at": j.get("started_at") or "",
            "finished_at": j.get("finished_at") or "",
        } for j in jobs],
        "shelf": {
            "item_id": shelf_item["id"] if shelf_item else "",
            "name": shelf_item.get("name") if shelf_item else "",
            "qty": shelf_item.get("qty") if shelf_item else None,
            "recent_sales": [{"at": r["at"], "qty": -num(r["qty"]),
                              "price": num(r.get("price"))} for r in shelf_sales],
        },
        "income": [{"at": r["at"], "amount": num(r["amount"])} for r in income],
        "feedback": ({
            "rating": feedback.get("rating") or 0,
            "text": feedback.get("feedback_text") or "",
            "received_at": feedback.get("feedback_received_at") or "",
            "publish_permission": feedback.get("publish_permission") or "",
        } if feedback else None),
        "generated_at": now_iso(),
    }


