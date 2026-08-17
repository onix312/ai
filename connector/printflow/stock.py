"""Регистр остатков PrintFlow 3.0.

Остаток — это не поле в карточке, а сумма движений регистра `stock_moves`.
Такой учёт нельзя «сломать» правкой числа: любое расхождение восстанавливается
пересчётом движений, а каждое движение ссылается на документ-основание.

Себестоимость считается методом средней скользящей: при каждом приходе
пересчитывается средняя по складу, расход уходит по текущей средней.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .accounting import num, uid
from .config import now_iso
from .db import Database

SALE_KINDS = ("sale",)          # движения, которые считаются продажей
SALE_DAYS = 7                   # окно расчёта скорости продаж
DEAD_DAYS = 14                  # без продаж столько дней — мёртвый сток
PLAN_DAYS = 7                   # на сколько дней вперёд планируем запас


class Stock:
    """Остатки, себестоимость и аналитика по регистру движений."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- движения
    def add_move(self, nom_id: str, warehouse_id: str, qty: float,
                 cost: float = 0.0, doc_id: str = "", doc_kind: str = "",
                 variant_id: str = "", batch_id: str = "", job_id: str = "",
                 note: str = "", at: str = "") -> dict:
        """Записать движение регистра. Знак qty: + приход, − расход."""
        row = {
            "id": uid("mv"), "at": at or now_iso(), "doc_id": doc_id or None,
            "doc_kind": doc_kind, "nom_id": nom_id, "variant_id": variant_id or None,
            "warehouse_id": warehouse_id or None, "qty": round(num(qty), 3),
            "cost": round(num(cost), 2), "batch_id": batch_id or None,
            "job_id": job_id or None, "note": note,
        }
        return self.db.upsert("stock_moves", row)

    def drop_doc_moves(self, doc_id: str) -> None:
        """Убрать движения документа — используется при распроведении."""
        self.db.execute("DELETE FROM stock_moves WHERE doc_id=?", (doc_id,))

    # -------------------------------------------------------------- остатки
    def qty(self, nom_id: str, warehouse_id: str = "", variant_id: str = "") -> float:
        sql = "SELECT COALESCE(SUM(qty),0) v FROM stock_moves WHERE nom_id=?"
        params: list[Any] = [nom_id]
        if warehouse_id:
            sql += " AND warehouse_id=?"
            params.append(warehouse_id)
        if variant_id:
            sql += " AND variant_id=?"
            params.append(variant_id)
        row = self.db.one(sql, params) or {}
        return round(num(row.get("v")), 3)

    def value(self, nom_id: str, warehouse_id: str = "") -> float:
        """Стоимость остатка по накопленной себестоимости."""
        sql = "SELECT COALESCE(SUM(cost),0) v FROM stock_moves WHERE nom_id=?"
        params: list[Any] = [nom_id]
        if warehouse_id:
            sql += " AND warehouse_id=?"
            params.append(warehouse_id)
        row = self.db.one(sql, params) or {}
        return round(max(0.0, num(row.get("v"))), 2)

    def avg_cost(self, nom_id: str, warehouse_id: str = "") -> float:
        """Средняя себестоимость единицы на складе."""
        q = self.qty(nom_id, warehouse_id)
        if q <= 0:
            # склад пуст — берём последнюю цену прихода
            row = self.db.one(
                "SELECT cost, qty FROM stock_moves WHERE nom_id=? AND qty>0"
                " ORDER BY datetime(at) DESC LIMIT 1", (nom_id,)) or {}
            if num(row.get("qty")) > 0:
                return round(num(row.get("cost")) / num(row.get("qty")), 2)
            return 0.0
        return round(self.value(nom_id, warehouse_id) / q, 2)

    def balances(self, warehouse_id: str = "") -> dict[str, dict]:
        """Остатки всей номенклатуры: {nom_id: {qty, value}}."""
        sql = ("SELECT nom_id, COALESCE(SUM(qty),0) q, COALESCE(SUM(cost),0) c"
               " FROM stock_moves WHERE 1=1")
        params: list[Any] = []
        if warehouse_id:
            sql += " AND warehouse_id=?"
            params.append(warehouse_id)
        sql += " GROUP BY nom_id"
        out: dict[str, dict] = {}
        for row in self.db.query(sql, params):
            q = round(num(row["q"]), 3)
            out[row["nom_id"]] = {
                "qty": q, "value": round(max(0.0, num(row["c"])), 2),
                "cost": round(max(0.0, num(row["c"])) / q, 2) if q > 0 else 0.0,
            }
        return out

    def warehouse_totals(self) -> list[dict]:
        """Свод по складам: сколько штук и на какую сумму лежит на каждом."""
        rows = {r["id"]: {**r, "qty": 0.0, "value": 0.0, "positions": 0}
                for r in self.db.query(
                    "SELECT * FROM warehouses WHERE archived=0 ORDER BY position, name")}
        for row in self.db.query(
                "SELECT warehouse_id, nom_id, COALESCE(SUM(qty),0) q,"
                " COALESCE(SUM(cost),0) c FROM stock_moves"
                " GROUP BY warehouse_id, nom_id HAVING q<>0"):
            target = rows.get(row["warehouse_id"])
            if not target:
                continue
            target["qty"] = round(target["qty"] + num(row["q"]), 3)
            target["value"] = round(target["value"] + max(0.0, num(row["c"])), 2)
            target["positions"] += 1
        return list(rows.values())

    def by_warehouse(self, nom_id: str) -> list[dict]:
        """Разрез остатка по складам для карточки товара."""
        rows = self.db.query(
            "SELECT m.warehouse_id, w.name, COALESCE(SUM(m.qty),0) q,"
            " COALESCE(SUM(m.cost),0) c FROM stock_moves m"
            " LEFT JOIN warehouses w ON w.id=m.warehouse_id"
            " WHERE m.nom_id=? GROUP BY m.warehouse_id HAVING q<>0", (nom_id,))
        return [{"warehouse_id": r["warehouse_id"], "name": r["name"] or "Склад",
                 "qty": round(num(r["q"]), 3),
                 "value": round(max(0.0, num(r["c"])), 2)} for r in rows]

    def moves(self, nom_id: str = "", warehouse_id: str = "", limit: int = 100) -> list[dict]:
        sql = ("SELECT m.*, n.name nom_name, w.name warehouse_name,"
               " d.number doc_number, d.kind doc_kind_real"
               " FROM stock_moves m"
               " LEFT JOIN nomenclature n ON n.id=m.nom_id"
               " LEFT JOIN warehouses w ON w.id=m.warehouse_id"
               " LEFT JOIN documents d ON d.id=m.doc_id WHERE 1=1")
        params: list[Any] = []
        if nom_id:
            sql += " AND m.nom_id=?"
            params.append(nom_id)
        if warehouse_id:
            sql += " AND m.warehouse_id=?"
            params.append(warehouse_id)
        sql += " ORDER BY datetime(m.at) DESC, m.rowid DESC LIMIT ?"
        params.append(int(limit))
        return self.db.query(sql, params)

    # -------------------------------------------------------------- резервы
    def reserved(self, nom_id: str, warehouse_id: str = "") -> float:
        sql = ("SELECT COALESCE(SUM(qty),0) v FROM reserves"
               " WHERE nom_id=? AND state='active'")
        params: list[Any] = [nom_id]
        if warehouse_id:
            sql += " AND warehouse_id=?"
            params.append(warehouse_id)
        row = self.db.one(sql, params) or {}
        return round(num(row.get("v")), 3)

    def reserve(self, nom_id: str, qty: float, order_id: str = "",
                warehouse_id: str = "", note: str = "") -> dict:
        qty = num(qty)
        if qty <= 0:
            raise ValueError("Количество резерва должно быть больше нуля")
        free = self.qty(nom_id, warehouse_id) - self.reserved(nom_id, warehouse_id)
        if free < qty:
            raise ValueError(f"Свободно только {round(free, 1)} шт — зарезервировать {round(qty, 1)} нельзя")
        return self.db.upsert("reserves", {
            "id": uid("rsv"), "at": now_iso(), "nom_id": nom_id,
            "warehouse_id": warehouse_id or None, "qty": round(qty, 3),
            "order_id": order_id or None, "state": "active", "note": note})

    def release(self, reserve_id: str = "", order_id: str = "") -> int:
        if reserve_id:
            self.db.execute("UPDATE reserves SET state='released' WHERE id=?", (reserve_id,))
            return 1
        if order_id:
            cur = self.db.execute(
                "UPDATE reserves SET state='released' WHERE order_id=? AND state='active'",
                (order_id,))
            return cur.rowcount or 0
        return 0

    def reserves(self, active_only: bool = True) -> list[dict]:
        sql = ("SELECT r.*, n.name nom_name, o.number order_number FROM reserves r"
               " LEFT JOIN nomenclature n ON n.id=r.nom_id"
               " LEFT JOIN orders o ON o.id=r.order_id WHERE 1=1")
        if active_only:
            sql += " AND r.state='active'"
        sql += " ORDER BY datetime(r.at) DESC LIMIT 200"
        return self.db.query(sql)

    # ------------------------------------------------------------ аналитика
    def sales_stats(self, nom_id: str) -> dict[str, Any]:
        """Продажи за 7 и 30 дней, скорость, дата последней продажи."""
        since7 = (datetime.now() - timedelta(days=SALE_DAYS)).isoformat()
        since30 = (datetime.now() - timedelta(days=30)).isoformat()
        sold7 = self._sold(nom_id, since7)
        sold30 = self._sold(nom_id, since30)
        last = self.db.one(
            "SELECT MAX(at) a FROM stock_moves WHERE nom_id=? AND doc_kind='sale'",
            (nom_id,)) or {}
        rate = sold7 / SALE_DAYS if sold7 else 0.0
        return {"sold_7": round(sold7, 1), "sold_30": round(sold30, 1),
                "rate_per_day": round(rate, 2), "last_sale": last.get("a") or ""}

    def _sold(self, nom_id: str, since: str) -> float:
        row = self.db.one(
            "SELECT COALESCE(SUM(-qty),0) v FROM stock_moves"
            " WHERE nom_id=? AND doc_kind='sale' AND qty<0 AND at>=?",
            (nom_id, since)) or {}
        return num(row.get("v"))

    def status_of(self, qty: float, min_qty: float, stats: dict,
                  max_qty: float = 0.0) -> tuple[str, float, int]:
        """Статус позиции, «хватит на N дней» и план пополнения."""
        rate = num(stats.get("rate_per_day"))
        days_left = round(qty / rate, 1) if rate and qty > 0 else None
        dead = qty > 0 and num(stats.get("sold_30")) <= 0
        low = qty > 0 and ((num(min_qty) > 0 and qty <= num(min_qty))
                           or (days_left is not None and days_left < 3))
        if qty <= 0:
            status = "empty" if num(stats.get("sold_30")) else "none"
        elif dead:
            status = "dead"
        elif low:
            status = "low"
        else:
            status = "ok"
        # План пополнения — максимум из двух правил: «дотянуть до недельного
        # спроса» и «вернуться к минимальному запасу». Иначе позиция может
        # висеть со статусом «Мало», но с нулевым планом печати.
        plan = 0
        if rate and qty < rate * PLAN_DAYS:
            plan = max(1, int(rate * PLAN_DAYS - qty + 0.999))
        if num(min_qty) > 0 and qty < num(min_qty):
            target = num(max_qty) if num(max_qty) > num(min_qty) else num(min_qty)
            plan = max(plan, int(target - qty + 0.999))
        return status, days_left, plan

    # ------------------------------------------------------ оборотная ведомость
    def turnover(self, date_from: str = "", date_to: str = "",
                 warehouse_id: str = "") -> list[dict]:
        """Оборотно-сальдовая ведомость: начало, приход, расход, конец."""
        date_from = date_from or (datetime.now() - timedelta(days=30)).isoformat()
        # Верхняя граница по умолчанию — «конец сегодняшнего дня», иначе движения,
        # созданные в ту же секунду, что и запрос, не попали бы в оборот.
        date_to = date_to or (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        noms = {n["id"]: n for n in self.db.query(
            "SELECT id, name, code, unit FROM nomenclature WHERE archived=0")}
        out: dict[str, dict] = {}
        wsql = " AND warehouse_id=?" if warehouse_id else ""
        wpar = [warehouse_id] if warehouse_id else []

        for row in self.db.query(
                "SELECT nom_id, COALESCE(SUM(qty),0) q, COALESCE(SUM(cost),0) c"
                f" FROM stock_moves WHERE at<?{wsql} GROUP BY nom_id",
                [date_from, *wpar]):
            out.setdefault(row["nom_id"], self._empty_turn(noms, row["nom_id"]))
            out[row["nom_id"]]["start_qty"] = round(num(row["q"]), 2)
            out[row["nom_id"]]["start_value"] = round(num(row["c"]), 2)

        for row in self.db.query(
                "SELECT nom_id, COALESCE(SUM(CASE WHEN qty>0 THEN qty END),0) inq,"
                " COALESCE(SUM(CASE WHEN qty<0 THEN -qty END),0) outq,"
                " COALESCE(SUM(CASE WHEN qty>0 THEN cost END),0) inc,"
                " COALESCE(SUM(CASE WHEN qty<0 THEN -cost END),0) outc"
                f" FROM stock_moves WHERE at>=? AND at<?{wsql} GROUP BY nom_id",
                [date_from, date_to, *wpar]):
            out.setdefault(row["nom_id"], self._empty_turn(noms, row["nom_id"]))
            item = out[row["nom_id"]]
            item["in_qty"] = round(num(row["inq"]), 2)
            item["out_qty"] = round(num(row["outq"]), 2)
            item["in_value"] = round(num(row["inc"]), 2)
            item["out_value"] = round(num(row["outc"]), 2)

        for item in out.values():
            item["end_qty"] = round(item["start_qty"] + item["in_qty"] - item["out_qty"], 2)
            item["end_value"] = round(item["start_value"] + item["in_value"] - item["out_value"], 2)
        return sorted(out.values(), key=lambda x: x["name"])

    @staticmethod
    def _empty_turn(noms: dict, nom_id: str) -> dict:
        nom = noms.get(nom_id) or {}
        return {"nom_id": nom_id, "name": nom.get("name") or "Удалённая позиция",
                "code": nom.get("code") or "", "unit": nom.get("unit") or "шт",
                "start_qty": 0.0, "start_value": 0.0, "in_qty": 0.0, "in_value": 0.0,
                "out_qty": 0.0, "out_value": 0.0, "end_qty": 0.0, "end_value": 0.0}
