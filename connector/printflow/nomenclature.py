"""Номенклатура PrintFlow 3.0 — единый справочник товаров.

Заменяет разрозненные `catalog` (модель) и `shelf_items` (полка): теперь это
одна карточка, у которой есть нормативы производства, остатки по складам,
цены по типам и экономика. Остаток берётся из регистра `stock_moves`,
а не хранится полем.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso
from .db import Database
from .schema_v3 import NOM_KINDS
from .stock import Stock


class Nomenclature:
    """Справочник номенклатуры: карточки, группы, цены, спецификации."""

    def __init__(self, db: Database):
        self.db = db
        self.stock = Stock(db)
        self.acc = Accounting(db)

    # ------------------------------------------------------------ код позиции
    def next_code(self) -> str:
        row = self.db.one("SELECT COUNT(*) n FROM nomenclature") or {}
        return f"{int(num(row.get('n'))) + 1:06d}"

    # -------------------------------------------------------------- список
    def items(self, group_id: str = "", kind: str = "", search: str = "",
              warehouse_id: str = "", include_archived: bool = False) -> list[dict]:
        sql = "SELECT * FROM nomenclature WHERE 1=1"
        params: list[Any] = []
        if not include_archived:
            sql += " AND archived=0"
        if group_id:
            groups = self._group_tree(group_id)
            sql += f" AND group_id IN ({','.join('?' * len(groups))})"
            params += groups
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if search:
            like = f"%{search.lower()}%"
            sql += (" AND (pylower(name) LIKE ? OR pylower(sku) LIKE ?"
                    " OR pylower(code) LIKE ? OR pylower(barcode) LIKE ?)")
            params += [like, like, like, like]
        sql += " ORDER BY name"
        rows = self.db.query(sql, params)

        balances = self.stock.balances(warehouse_id)
        prices = self._all_prices()
        target = num(self.db.setting("target_profit_per_hour", 250), 250)
        out = []
        for row in rows:
            out.append(self._decorate(row, balances, prices, target, warehouse_id))
        return out

    def _decorate(self, row: dict, balances: dict, prices: dict,
                  target: float, warehouse_id: str = "") -> dict:
        nom_id = row["id"]
        bal = balances.get(nom_id) or {"qty": 0.0, "value": 0.0, "cost": 0.0}
        qty = num(bal["qty"])
        stats = self.stock.sales_stats(nom_id)
        status, days_left, plan = self.stock.status_of(
            qty, num(row.get("min_qty")), stats, num(row.get("max_qty")))
        reserved = self.stock.reserved(nom_id, warehouse_id)
        item_prices = prices.get(nom_id, {})
        price = num(item_prices.get(self._base_type()))
        cost = num(bal["cost"]) or self._norm_cost(row)
        margin = price - cost if price else 0.0
        hours = num(row.get("hours"))
        profit_per_hour = round(margin / hours, 2) if hours else 0.0
        return {
            **row,
            "kind_label": NOM_KINDS.get(row.get("kind") or "product", "Товар"),
            "qty": qty,
            "reserved": reserved,
            "free": round(qty - reserved, 3),
            "stock_value": num(bal["value"]),
            "cost": round(cost, 2),
            "price": price,
            "prices": item_prices,
            "margin": round(margin, 2),
            "margin_pct": round(margin / price * 100, 1) if price else 0.0,
            "profit_per_hour": profit_per_hour,
            "profitable": profit_per_hour >= target if hours else None,
            "sold_7": stats["sold_7"],
            "sold_30": stats["sold_30"],
            "rate_per_day": stats["rate_per_day"],
            "last_sale": stats["last_sale"],
            "days_left": days_left,
            "status": status,
            "plan_qty": plan,
        }

    def _norm_cost(self, row: dict) -> float:
        """Нормативная себестоимость по граммам и часам, если факта ещё нет."""
        grams = num(row.get("grams"))
        hours = num(row.get("hours"))
        if not grams and not hours:
            return 0.0
        br = self.acc.cost_breakdown(grams, hours,
                                     manual_minutes=num(row.get("post_minutes")))
        return round(num(br.get("total")), 2)

    def _base_type(self) -> str:
        row = self.db.one("SELECT id FROM price_types WHERE is_base=1 LIMIT 1")
        return (row or {}).get("id") or "retail"

    def _all_prices(self) -> dict[str, dict[str, float]]:
        """Последние цены всех позиций по всем типам цен."""
        rows = self.db.query(
            "SELECT p.nom_id, p.price_type_id, p.price FROM prices p"
            " JOIN (SELECT nom_id, price_type_id, MAX(datetime(at)) mx, MAX(rowid) mr"
            "       FROM prices GROUP BY nom_id, price_type_id) t"
            " ON t.nom_id=p.nom_id AND t.price_type_id=p.price_type_id"
            "    AND p.rowid=t.mr")
        out: dict[str, dict[str, float]] = {}
        for row in rows:
            out.setdefault(row["nom_id"], {})[row["price_type_id"]] = round(num(row["price"]), 2)
        return out

    def _group_tree(self, group_id: str) -> list[str]:
        """Группа со всеми вложенными подгруппами."""
        result = [group_id]
        queue = [group_id]
        while queue:
            current = queue.pop()
            for row in self.db.query("SELECT id FROM nom_groups WHERE parent_id=?", (current,)):
                if row["id"] not in result:
                    result.append(row["id"])
                    queue.append(row["id"])
        return result

    # -------------------------------------------------------------- карточка
    def item(self, nom_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not row:
            return None
        balances = self.stock.balances()
        item = self._decorate(row, balances, self._all_prices(),
                              num(self.db.setting("target_profit_per_hour", 250), 250))
        item["warehouses"] = self.stock.by_warehouse(nom_id)
        item["moves"] = self.stock.moves(nom_id, limit=50)
        item["variants"] = self.db.query(
            "SELECT * FROM nom_variants WHERE nom_id=? AND archived=0 ORDER BY position, name",
            (nom_id,))
        item["price_history"] = self.db.query(
            "SELECT p.*, t.name type_name FROM prices p"
            " LEFT JOIN price_types t ON t.id=p.price_type_id"
            " WHERE p.nom_id=? ORDER BY datetime(p.at) DESC LIMIT 30", (nom_id,))
        item["spec"] = self.spec_of(nom_id)
        item["batches"] = self.db.query(
            "SELECT * FROM batches WHERE nom_id=? ORDER BY datetime(at) DESC LIMIT 20",
            (nom_id,))
        item["fact"] = self._fact_stats(nom_id)
        return item

    def _fact_stats(self, nom_id: str) -> dict:
        """Факт против норматива по завершённым партиям."""
        row = self.db.one(
            "SELECT COUNT(*) n, COALESCE(AVG(NULLIF(est_grams,0)),0) g,"
            " COALESCE(AVG(NULLIF(est_minutes,0)),0) m,"
            " COALESCE(SUM(qty_done),0) done, COALESCE(SUM(qty_scrap),0) scrap"
            " FROM batches WHERE nom_id=? AND state IN ('done','partial')",
            (nom_id,)) or {}
        done = num(row.get("done"))
        scrap = num(row.get("scrap"))
        return {
            "batches": int(num(row.get("n"))),
            "avg_grams": round(num(row.get("g")), 1),
            "avg_minutes": round(num(row.get("m")), 1),
            "produced": round(done, 1),
            "scrap": round(scrap, 1),
            "scrap_pct": round(scrap / (done + scrap) * 100, 1) if (done + scrap) else 0.0,
        }

    # --------------------------------------------------------------- запись
    def save(self, data: dict) -> dict:
        data = dict(data)
        prices = data.pop("prices", None)
        new = not data.get("id")
        if new:
            data["id"] = uid("nom")
            data["created_at"] = now_iso()
            data.setdefault("code", self.next_code())
        data["updated_at"] = now_iso()
        if not (data.get("name") or "").strip():
            raise ValueError("Укажите наименование")
        for key in ("grams", "hours", "post_minutes", "min_qty", "max_qty", "vat"):
            if key in data:
                data[key] = round(num(data[key]), 3)
        if "fit_per_plate" in data:
            data["fit_per_plate"] = max(1, int(num(data["fit_per_plate"], 1)))

        with self.db.transaction():
            row = self.db.upsert("nomenclature", data)
            if isinstance(prices, dict):
                for type_id, value in prices.items():
                    if value in ("", None):
                        continue
                    self.set_price(row["id"], num(value), type_id)
        self.db.add_event("nom", "Номенклатура создана" if new else "Номенклатура изменена",
                          row.get("name") or "", data={"nom_id": row["id"]})
        return self.item(row["id"]) or row

    def archive(self, nom_id: str, archived: bool = True) -> dict:
        self.db.execute("UPDATE nomenclature SET archived=?, updated_at=? WHERE id=?",
                        (1 if archived else 0, now_iso(), nom_id))
        return self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,)) or {}

    def delete(self, nom_id: str) -> None:
        """Удалить можно только позицию без движений — иначе архивируем."""
        row = self.db.one("SELECT COUNT(*) n FROM stock_moves WHERE nom_id=?", (nom_id,)) or {}
        if int(num(row.get("n"))):
            self.archive(nom_id, True)
            return
        with self.db.transaction():
            self.db.execute("DELETE FROM prices WHERE nom_id=?", (nom_id,))
            self.db.execute("DELETE FROM nom_variants WHERE nom_id=?", (nom_id,))
            self.db.delete("nomenclature", nom_id)

    # ----------------------------------------------------------------- цены
    def set_price(self, nom_id: str, price: float, price_type_id: str = "",
                  note: str = "", variant_id: str = "") -> dict:
        price_type_id = price_type_id or self._base_type()
        current = self.db.one(
            "SELECT price FROM prices WHERE nom_id=? AND price_type_id=?"
            " ORDER BY datetime(at) DESC, rowid DESC LIMIT 1", (nom_id, price_type_id))
        if current and abs(num(current["price"]) - num(price)) < 0.005:
            return dict(current)
        return self.db.upsert("prices", {
            "id": uid("prc"), "at": now_iso(), "nom_id": nom_id,
            "variant_id": variant_id or None, "price_type_id": price_type_id,
            "price": round(num(price), 2), "note": note})

    def recalc_prices(self, price_type_id: str = "", group_id: str = "") -> dict:
        """Пересчитать цены от себестоимости, наценки и нормы прибыли за час.

        Одной наценки мало: у быстрой мелочи она даёт копейки за час работы
        принтера, а у долгих изделий — наоборот, задирает цену. Поэтому берём
        максимум из двух цен: «себестоимость + наценка» и «себестоимость плюс
        целевая прибыль за занятые часы печати». Итог округляем вверх до шага.
        """
        price_type_id = price_type_id or self._base_type()
        ptype = self.db.one("SELECT * FROM price_types WHERE id=?", (price_type_id,))
        if not ptype:
            raise ValueError("Тип цен не найден")
        rounding = max(1.0, num(self.db.setting("price_rounding", 10), 10))
        target = num(self.db.setting("target_profit_per_hour", 250), 250)
        base_type = self._base_type()
        changed = 0
        for item in self.items(group_id=group_id):
            if item.get("kind") in ("service",):
                continue
            cost = num(item.get("cost"))
            if cost <= 0:
                continue
            group = self.db.one("SELECT markup FROM nom_groups WHERE id=?",
                                (item.get("group_id"),)) if item.get("group_id") else None
            markup = num((group or {}).get("markup")) or num(ptype.get("markup"))
            raw = cost * (1 + markup / 100.0)
            # Норму прибыли за час держим только по основной цене продажи:
            # опт и B2B сознательно дешевле розницы.
            hours = num(item.get("hours"))
            if hours and price_type_id == base_type:
                raw = max(raw, cost + target * hours)
            price = round(-(-raw // rounding) * rounding, 2)
            if abs(price - num(item.get("prices", {}).get(price_type_id))) >= 0.01:
                self.set_price(item["id"], price, price_type_id, "автопересчёт")
                changed += 1
        return {"ok": True, "changed": changed, "price_type_id": price_type_id}

    # -------------------------------------------------------------- группы
    def groups(self) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM nom_groups WHERE archived=0 ORDER BY position, name")
        counts = {r["group_id"]: int(num(r["n"])) for r in self.db.query(
            "SELECT group_id, COUNT(*) n FROM nomenclature WHERE archived=0"
            " GROUP BY group_id") if r["group_id"]}
        for row in rows:
            row["items"] = counts.get(row["id"], 0)
        return rows

    def save_group(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("id"):
            data["id"] = uid("grp")
        if not (data.get("name") or "").strip():
            raise ValueError("Укажите название группы")
        if data.get("parent_id") == data.get("id"):
            raise ValueError("Группа не может быть родителем самой себе")
        return self.db.upsert("nom_groups", data)

    def delete_group(self, group_id: str) -> None:
        kids = self.db.one("SELECT COUNT(*) n FROM nom_groups WHERE parent_id=?",
                           (group_id,)) or {}
        if int(num(kids.get("n"))):
            raise ValueError("Сначала удалите или перенесите подгруппы")
        self.db.execute("UPDATE nomenclature SET group_id=NULL WHERE group_id=?", (group_id,))
        self.db.delete("nom_groups", group_id)

    # -------------------------------------------------------- спецификации
    def spec_of(self, nom_id: str) -> dict | None:
        spec = self.db.one(
            "SELECT * FROM specs WHERE nom_id=? AND active=1 ORDER BY rowid LIMIT 1",
            (nom_id,))
        if not spec:
            return None
        spec["items"] = self.db.query(
            "SELECT si.*, n.name nom_name, n.unit, n.kind FROM spec_items si"
            " LEFT JOIN nomenclature n ON n.id=si.nom_id"
            " WHERE si.spec_id=? ORDER BY si.line", (spec["id"],))
        return spec

    def save_spec(self, data: dict) -> dict:
        data = dict(data)
        items = data.pop("items", [])
        if not data.get("nom_id"):
            raise ValueError("Не указано изделие")
        if not data.get("id"):
            data["id"] = uid("spc")
            data["created_at"] = now_iso()
        data.setdefault("active", 1)
        with self.db.transaction():
            spec = self.db.upsert("specs", data)
            self.db.execute("DELETE FROM spec_items WHERE spec_id=?", (spec["id"],))
            for index, item in enumerate(items or []):
                if not isinstance(item, dict) or not item.get("nom_id"):
                    continue
                self.db.upsert("spec_items", {
                    "id": item.get("id") or uid("spi"), "spec_id": spec["id"],
                    "line": index + 1, "nom_id": item["nom_id"],
                    "variant_id": item.get("variant_id") or None,
                    "qty": round(num(item.get("qty"), 1), 3),
                    "note": item.get("note", "")})
        return self.spec_of(data["nom_id"]) or {}

    def delete_spec(self, spec_id: str) -> None:
        with self.db.transaction():
            self.db.execute("DELETE FROM spec_items WHERE spec_id=?", (spec_id,))
            self.db.delete("specs", spec_id)

    # ---------------------------------------------------------- варианты
    def save_variant(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("nom_id"):
            raise ValueError("Не указана номенклатура")
        if not data.get("id"):
            data["id"] = uid("var")
        return self.db.upsert("nom_variants", data)

    def delete_variant(self, variant_id: str) -> None:
        self.db.delete("nom_variants", variant_id)

    # ----------------------------------------------------------- сводка
    def summary(self, warehouse_id: str = "") -> dict[str, Any]:
        items = self.items(warehouse_id=warehouse_id)
        goods = [i for i in items if i.get("kind") in ("product", "kit", "semi")]
        qty = sum(num(i["qty"]) for i in goods)
        value = sum(num(i["stock_value"]) for i in goods)
        low = [i for i in goods if i["status"] == "low"]
        dead = [i for i in goods if i["status"] == "dead"]
        empty = [i for i in goods if i["status"] == "empty"]
        plan = sum(int(num(i["plan_qty"])) for i in goods)
        sold7 = sum(num(i["sold_7"]) for i in goods)
        sold7_money = sum(num(i["sold_7"]) * num(i["price"]) for i in goods)
        unprofitable = [i for i in goods if i.get("profitable") is False]
        return {
            "items": len(items),
            "goods": len(goods),
            "qty": round(qty, 1),
            "value": round(value, 2),
            "low": len(low),
            "dead": len(dead),
            "dead_value": round(sum(num(i["stock_value"]) for i in dead), 2),
            "empty": len(empty),
            "plan_qty": plan,
            "sold_7": round(sold7, 1),
            "sold_7_money": round(sold7_money, 2),
            "unprofitable": len(unprofitable),
            "reserved": round(sum(num(i["reserved"]) for i in goods), 1),
        }

    def replenishment(self, warehouse_id: str = "") -> list[dict]:
        """Что и сколько печатать: позиции с дефицитом, отсортированные по срочности."""
        out = []
        for item in self.items(warehouse_id=warehouse_id):
            if item.get("kind") not in ("product", "kit", "semi"):
                continue
            if not num(item.get("plan_qty")):
                continue
            out.append({
                "nom_id": item["id"], "name": item["name"],
                "qty": item["qty"], "plan_qty": item["plan_qty"],
                "days_left": item["days_left"], "status": item["status"],
                "fit_per_plate": item.get("fit_per_plate") or 1,
                "grams": item.get("grams"), "hours": item.get("hours"),
                "file": item.get("file"), "material": item.get("material"),
                "price": item.get("price"), "cost": item.get("cost"),
            })
        out.sort(key=lambda x: (x["days_left"] if x["days_left"] is not None else 999))
        return out
