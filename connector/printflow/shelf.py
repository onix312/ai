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

from datetime import datetime, timedelta
from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso
from .db import Database

# Периоды аналитики стеллажа
SALE_DAYS = 7        # окно «продано за N дней» для оборачиваемости
DEAD_DAYS = 14       # после скольких дней без продаж позиция — «мёртвый сток»
PLAN_DAYS = 7        # на сколько дней вперёд планировать пополнение


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
        for row in rows:
            qty = num(row["qty"])
            sold7 = num(self._sum_sold(row["id"], since7))
            sold30 = num(self._sum_sold(row["id"], since30))
            last = self.db.one(
                "SELECT MAX(at) a FROM shelf_moves WHERE item_id=? AND kind IN ('sale','online')",
                (row["id"],)) or {}
            # скорость продаж, шт/день
            rate = sold7 / SALE_DAYS if sold7 else 0.0
            days_left = round(qty / rate, 1) if rate and qty > 0 else None
            cost = num(row["cost_per_unit"])
            dead = qty > 0 and sold30 <= 0
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
        row["moves"] = self.moves(item_id, limit=40)
        return row

    def save_item(self, data: dict) -> dict:
        data = dict(data)
        new = not data.get("id")
        if not data.get("id"):
            data["id"] = uid("shf")
        if new:
            data.setdefault("created_at", now_iso())
        data["updated_at"] = now_iso()
        for key in ("qty", "price", "cost_per_unit", "min_qty"):
            if key in data:
                data[key] = round(num(data[key]), 2)
        row = self.db.upsert("shelf_items", data)
        self.db.add_event("shelf", "Позиция стеллажа создана" if new else "Позиция стеллажа изменена",
                          row.get("name") or "", data={"item_id": row["id"]})
        return row

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
              job_id: str = "", tx_id: str = "", note: str = "") -> dict:
        row = self.db.upsert("shelf_moves", {
            "id": uid("shm"), "at": now_iso(), "item_id": item_id, "kind": kind,
            "qty": round(num(qty), 2), "price": round(num(price), 2),
            "job_id": job_id or None, "tx_id": tx_id or None, "note": note or ""})
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
             channel: str = "shelf", note: str = "") -> dict:
        """Продажа штук со стеллажа.

        price > 0 → создаётся проводка дохода в кассу (с пометкой «стеллаж»);
        price = 0 → только списание штук (деньги видит 1С магазина).
        channel: shelf — стеллаж, online — Авито/Telegram (единый остаток).
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
        if price > 0:
            tx = self.acc.add_transaction(
                "income", "sale", price * qty,
                f"Стеллаж: {item.get('name') or ''} × {round(qty)}",
                note=f"{note} · {channel}" .strip(), auto=False,
                channel="online" if channel == "online" else "shelf",
                payer="person")
        move = self._move(item_id, kind, -qty, price=price,
                          tx_id=tx.get("id") if tx else "",
                          note=note or f"Продажа ({channel})")
        self.db.add_event("shelf", "Продажа со стеллажа",
                          f"{item.get('name') or ''} −{round(qty)} шт"
                          + (f" на {round(price * qty)} ₽" if price else ""),
                          data={"item_id": item_id, "qty": qty, "price": price})
        return {"ok": True, "move": move, "tx": tx,
                "item": self.db.one("SELECT * FROM shelf_items WHERE id=?", (item_id,))}

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

    # ------------------------------------------------------------- QR-ценник
    def qr_link(self, item_id: str, host: str = "") -> str:
        """URL страницы позиции для QR-ценника (телефон в той же сети)."""
        host = (host or "127.0.0.1:8080").replace("http://", "").replace("https://", "")
        return f"http://{host}/shelf.html?id={item_id}"
