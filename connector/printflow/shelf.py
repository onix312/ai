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
    def stock_available(self) -> list[dict]:
        """Товары учётных складов с остатком ≥ 1 шт — кандидаты на стеллаж.

        Позиции с нулевым или дробным «хвостом» меньше единицы не показываем:
        переместить на полку можно только целую штуку, которая реально есть.
        Полка магазина (склад kind='shelf') исключается — оттуда не «перемещают
        на стеллаж», это и есть стеллаж.
        """
        rows = self.db.query(
            "SELECT m.nom_id, m.warehouse_id, COALESCE(w.name,'Склад') warehouse_name,"
            " n.name, n.photo, n.unit,"
            " COALESCE(SUM(m.qty),0) q, COALESCE(SUM(m.cost),0) c"
            " FROM stock_moves m"
            " JOIN nomenclature n ON n.id=m.nom_id AND n.archived=0"
            " LEFT JOIN warehouses w ON w.id=m.warehouse_id"
            " WHERE COALESCE(w.kind,'') != 'shelf'"
            " GROUP BY m.nom_id, m.warehouse_id HAVING q >= 1"
            " ORDER BY n.name")
        out = []
        for row in rows:
            qty = num(row["q"])
            out.append({
                "nom_id": row["nom_id"], "name": row["name"] or "Без названия",
                "photo": row.get("photo") or "", "unit": row.get("unit") or "шт",
                "warehouse_id": row["warehouse_id"] or "",
                "warehouse_name": row["warehouse_name"],
                "qty": round(qty, 2),
                "avg_cost": round(max(0.0, num(row["c"])) / qty, 2) if qty > 0 else 0.0,
            })
        return out

    def transfer_from_stock(self, nom_id: str, warehouse_id: str, qty: float,
                            item_id: str = "", note: str = "") -> dict:
        """Переместить готовый товар с учётного склада на стеллаж магазина.

        Правила:
        • перемещать можно только то, что есть: остаток на складе-источнике
          должен быть ≥ 1 шт, а запрошенное количество — целое, от 1 и не
          больше остатка;
        • регистр остатков получает пару движений «перемещение» (расход со
          склада + приход на склад-полку) — учёт 3.0 остаётся честным;
        • стеллаж получает приход штук с себестоимостью по средней складской.

        Позиция стеллажа находится по item_id, по связке nomenclature.
        legacy_shelf_id или по имени; если её нет — создаётся автоматически.
        """
        nom = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not nom:
            raise ValueError("Товар не найден в номенклатуре")
        if not warehouse_id:
            raise ValueError("Укажите склад-источник")
        qty = num(qty)
        if qty < 1 or abs(qty - round(qty)) > 1e-9:
            raise ValueError("Перемещать можно целыми штуками: минимум 1")
        qty = float(round(qty))
        from .stock import Stock
        stock = Stock(self.db)
        available = stock.qty(nom_id, warehouse_id)
        if available < 1:
            raise ValueError("На складе меньше 1 шт — перемещать нечего")
        if qty > available:
            raise ValueError(f"На складе только {round(available, 1)} шт, "
                             f"а переместить просят {round(qty)}")
        unit_cost = stock.avg_cost(nom_id, warehouse_id)
        cost = round(unit_cost * qty, 2)
        # 1) регистр остатков: расход со склада + приход на полку магазина
        shelf_wh = (self.db.one(
            "SELECT id FROM warehouses WHERE kind='shelf' AND archived=0"
            " ORDER BY position LIMIT 1") or {}).get("id") or "shelf"
        stock.add_move(nom_id, warehouse_id, -qty, -cost, doc_kind="move",
                       note=note or "перемещение на стеллаж")
        stock.add_move(nom_id, shelf_wh, qty, cost, doc_kind="move",
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
