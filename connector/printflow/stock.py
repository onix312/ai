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

# Ручная корректировка одной кнопкой («−1»/«+1» на складе): это не продажа
# и не документ, а правка остатка с основанием-движением. Финансовые отчёты,
# касса, выручка и статистика продаж такие движения не видят.
MANUAL_KIND = "manual"
MANUAL_NOTE_MINUS = "ручное списание"
MANUAL_NOTE_PLUS = "ручное оприходование"
# Причины ручной корректировки (идея 2): короткий список + своя заметка.
MANUAL_REASONS = ("брак", "потеря", "найдено", "подарок", "пересчёт", "своё")
REASON_LABEL = {"брак": "Брак", "потеря": "Потеря", "найдено": "Найдено",
                "подарок": "Подарок", "пересчёт": "Пересчёт", "своё": "Своё"}


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

    # --------------------------------------------- ручные корректировки «−N/+N»
    def manual_adjust(self, nom_id: str, warehouse_id: str, delta: float,
                      note: str = "", reason: str = "") -> dict:
        """Ручная корректировка остатка: «−1» — списание одной штуки,
        «−N»/«+N» — корректировка количества (идея 1). Не продажа: не
        трогает кассу, выручку, долги и статистику продаж — только движение
        регистра и аудит.

        Списание защищено: уходить может только свободный остаток
        (остаток минус активные резервы) — резерв под заказ сломать нельзя.
        `reason` — причина из списка MANUAL_REASONS (идея 2); попадает
        в заметку движения для фильтра и в текст события аудита.
        """
        delta = round(num(delta), 3)
        if delta == 0:
            raise ValueError("Корректировка не меняет остаток (0)")
        nom = self.db.one("SELECT name, unit FROM nomenclature WHERE id=?",
                          (nom_id,))
        if not nom:
            raise ValueError("Позиция не найдена")
        wh = self.db.one("SELECT name FROM warehouses WHERE id=? AND archived=0",
                         (warehouse_id,))
        if not wh:
            raise ValueError("Склад не найден")
        reason = (reason or "").strip().lower()
        if reason and reason not in MANUAL_REASONS:
            reason = "своё"
        amount = abs(delta)
        unit = str(nom.get("unit") or "шт")
        qty = self.qty(nom_id, warehouse_id)
        reserved = self.reserved(nom_id, warehouse_id)
        free = qty - reserved
        reason_tag = f"[{REASON_LABEL.get(reason, reason)}] " if reason else ""
        if delta < 0:
            # Списываем только свободное: зарезервированное под заказ —
            # неприкосновенно. Сервер — последний рубеж, кнопка на фронте
            # уже неактивна.
            if free < amount - 1e-9:
                if reserved > 0 and free < 1e-9:
                    raise ValueError(
                        f"Списать нельзя: {round(reserved, 3)} {unit} "
                        "в резерве под заказы")
                raise ValueError(
                    f"На складе «{wh['name']}» свободно {round(free, 3)} {unit}, "
                    f"а списать просят {round(amount, 3)}")
            avg = self.avg_cost(nom_id, warehouse_id)
            cost = -round(avg * amount, 2)   # расход по средней
            action, note_default = "Списание", MANUAL_NOTE_MINUS
        else:
            # Оприходование найденных штук приходуем по текущей средней:
            # если средней нет (новая позиция) — по нулевой цене, деньги
            # корректировка не двигает в любом случае.
            avg = self.avg_cost(nom_id, warehouse_id)
            cost = round(avg * amount, 2)
            action, note_default = "Оприходование", MANUAL_NOTE_PLUS
        full_note = (reason_tag + (str(note).strip() or note_default)).strip()
        move = self.add_move(nom_id, warehouse_id, delta, cost,
                             doc_kind=MANUAL_KIND, note=full_note)
        self.db.add_event(
            "stock", f"Склад: {action.lower()} {round(amount, 3)} {unit}",
            f"{nom.get('name') or nom_id} · склад «{wh['name']}» · "
            f"движение {move['id']} · {full_note}",
            "", {"move_id": move["id"], "nom_id": nom_id,
                 "warehouse_id": warehouse_id, "delta": delta,
                 "reason": reason})
        return move

    def manual_stats(self, nom_id: str = "", days: int = 7) -> dict:
        """Ручные списания за период (идеи 5 и 6): штуки и сумма по позициям
        и в целом. Причина видна в заметке движения ([Брак] и т.п.)."""
        since = (datetime.now() - timedelta(days=days)).isoformat()
        sql = ("SELECT nom_id, COALESCE(SUM(-qty),0) q, COALESCE(SUM(-cost),0) c"
               " FROM stock_moves WHERE doc_kind=? AND qty<0 AND at>=?")
        params: list[Any] = [MANUAL_KIND, since]
        if nom_id:
            sql += " AND nom_id=?"
            params.append(nom_id)
        sql += " GROUP BY nom_id"
        per_nom = {}
        total_q = total_c = 0.0
        for r in self.db.query(sql, params):
            q = round(num(r["q"]), 3)
            c = round(max(0.0, num(r["c"])), 2)
            per_nom[r["nom_id"]] = {"qty": q, "value": c}
            total_q += q
            total_c += c
        return {"days": days, "total_qty": round(total_q, 3),
                "total_value": round(total_c, 2), "per_nom": per_nom}

    def manual_recent(self, days: int = 30, limit: int = 50) -> list[dict]:
        """Последние ручные списания — для панели «куда девается» (идея 6)."""
        since = (datetime.now() - timedelta(days=days)).isoformat()
        return self.db.query(
            "SELECT m.nom_id, m.warehouse_id, m.qty, m.cost, m.at, m.note,"
            " n.name nom_name, w.name warehouse_name FROM stock_moves m"
            " LEFT JOIN nomenclature n ON n.id=m.nom_id"
            " LEFT JOIN warehouses w ON w.id=m.warehouse_id"
            " WHERE m.doc_kind=? AND m.qty<0 AND m.at>=?"
            " ORDER BY datetime(m.at) DESC LIMIT ?",
            (MANUAL_KIND, since, int(limit)))

    def revert_manual(self, move_id: str) -> dict:
        """Откат ручной корректировки — удаление движения (как распроведение
        документа): остаток и средняя себестоимость восстанавливаются
        пересчётом регистра, пара «списание/возврат» не висит в оборотке.
        Факт отката остаётся в журнале аудита.
        """
        move = self.db.one("SELECT * FROM stock_moves WHERE id=?", (move_id,))
        if not move:
            raise ValueError("Движение уже возвращено или не найдено")
        if move.get("doc_kind") != MANUAL_KIND:
            raise ValueError("Вернуть можно только ручную корректировку — "
                             "для документов есть распроведение")
        nom = self.db.one("SELECT name FROM nomenclature WHERE id=?",
                          (move.get("nom_id"),)) or {}
        wh = self.db.one("SELECT name FROM warehouses WHERE id=?",
                         (move.get("warehouse_id"),)) or {}
        delta = num(move.get("qty"))
        if delta > 0:
            # Откатываем оприходование (+1) — штука из регистра уходит.
            # Нельзя, если свободного остатка уже нет: найденную штуку
            # успели зарезервировать под заказ или продать, откат создал бы
            # минус/недобор по резерву.
            free = (self.qty(move["nom_id"], move.get("warehouse_id") or "")
                    - self.reserved(move["nom_id"], move.get("warehouse_id") or ""))
            if free < 1 - 1e-9:
                raise ValueError(
                    "Вернуть нельзя: штуки уже нет в свободном остатке "
                    "(ушла продажей или в резерв под заказ)")
        # Откат списания (−1) только добавляет штуку обратно на склад —
        # минус он создать не может, поэтому ограничений по остатку нет:
        # если после списания остаток распродан до нуля, откат корректно
        # показывает, что списанная по ошибке штука всё же на месте.
        self.db.execute("DELETE FROM stock_moves WHERE id=?", (move_id,))
        self.db.add_event(
            "stock", "Склад: корректировка возвращена",
            f"{nom.get('name') or move.get('nom_id')} · склад "
            f"«{wh.get('name') or move.get('warehouse_id')}» · "
            f"откат движения {move_id} ({'+' if delta > 0 else '−'}1 шт)",
            "", {"move_id": move_id, "nom_id": move.get("nom_id"),
                 "warehouse_id": move.get("warehouse_id"),
                 "reverted_delta": delta})
        return {"id": move_id, "ok": True}

    def warehouse_positions(self, warehouse_id: str) -> list[dict]:
        """Позиции одного склада для экрана «Позиции»: остаток, резерв,
        свободное, себестоимость и стоимость. Архивные позиции скрываем."""
        reserved_map = self.reserved_all(warehouse_id)
        sql = ("SELECT m.nom_id, COALESCE(n.name,'Удалённая позиция') name,"
               " COALESCE(n.archived,0) archived, COALESCE(n.unit,'шт') unit,"
               " COALESCE(SUM(m.qty),0) q, COALESCE(SUM(m.cost),0) c"
               " FROM stock_moves m LEFT JOIN nomenclature n ON n.id=m.nom_id"
               " WHERE m.warehouse_id=? GROUP BY m.nom_id HAVING q<>0"
               " ORDER BY name")
        out: list[dict] = []
        for r in self.db.query(sql, (warehouse_id,)):
            if num(r.get("archived")):
                continue
            q = round(num(r["q"]), 3)
            reserved = round(num(reserved_map.get(r["nom_id"], 0.0)), 3)
            out.append({
                "nom_id": r["nom_id"], "name": r["name"],
                "unit": r["unit"] or "шт", "qty": q,
                "reserved": reserved, "free": round(q - reserved, 3),
                "value": round(max(0.0, num(r["c"])), 2),
                "cost": round(max(0.0, num(r["c"])) / q, 2) if q > 0 else 0.0,
            })
        return out

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

    def value(self, nom_id: str, warehouse_id: str = "", variant_id: str = "") -> float:
        """Стоимость остатка по накопленной себестоимости."""
        sql = "SELECT COALESCE(SUM(cost),0) v FROM stock_moves WHERE nom_id=?"
        params: list[Any] = [nom_id]
        if warehouse_id:
            sql += " AND warehouse_id=?"
            params.append(warehouse_id)
        if variant_id:
            sql += " AND variant_id=?"
            params.append(variant_id)
        row = self.db.one(sql, params) or {}
        return round(max(0.0, num(row.get("v"))), 2)

    def avg_cost(self, nom_id: str, warehouse_id: str = "", variant_id: str = "") -> float:
        """Средняя себестоимость единицы на складе."""
        q = self.qty(nom_id, warehouse_id, variant_id)
        if q <= 0:
            # склад пуст — берём последнюю цену прихода
            sql = ("SELECT cost, qty FROM stock_moves WHERE nom_id=? AND qty>0"
                   + (" AND variant_id=?" if variant_id else "")
                   + " ORDER BY datetime(at) DESC LIMIT 1")
            row = self.db.one(sql, (nom_id, variant_id) if variant_id else (nom_id,)) or {}
            if num(row.get("qty")) > 0:
                return round(num(row.get("cost")) / num(row.get("qty")), 2)
            return 0.0
        return round(self.value(nom_id, warehouse_id, variant_id) / q, 2)

    def balances(self, warehouse_id: str = "", nom_id: str = "") -> dict[str, dict]:
        """Остатки всей номенклатуры: {nom_id: {qty, value}}.

        С `nom_id` считает только одну позицию — точечный запрос для карточки
        товара, без свертки движений по всему справочнику."""
        sql = ("SELECT nom_id, COALESCE(SUM(qty),0) q, COALESCE(SUM(cost),0) c"
               " FROM stock_moves WHERE 1=1")
        params: list[Any] = []
        if warehouse_id:
            sql += " AND warehouse_id=?"
            params.append(warehouse_id)
        if nom_id:
            sql += " AND nom_id=?"
            params.append(nom_id)
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
        """Разрез остатка по складам для карточки товара (с резервом и
        свободным остатком — на них опираются кнопки «−1»/«+1»)."""
        rows = self.db.query(
            "SELECT m.warehouse_id, w.name, COALESCE(SUM(m.qty),0) q,"
            " COALESCE(SUM(m.cost),0) c FROM stock_moves m"
            " LEFT JOIN warehouses w ON w.id=m.warehouse_id"
            " WHERE m.nom_id=? GROUP BY m.warehouse_id HAVING q<>0", (nom_id,))
        out = []
        for r in rows:
            q = round(num(r["q"]), 3)
            reserved = round(self.reserved(nom_id, r["warehouse_id"]), 3)
            out.append({"warehouse_id": r["warehouse_id"],
                        "name": r["name"] or "Склад",
                        "qty": q, "reserved": reserved,
                        "free": round(q - reserved, 3),
                        "value": round(max(0.0, num(r["c"])), 2)})
        return out

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
    def reserved_all(self, warehouse_id: str = "") -> dict[str, float]:
        """Активные резервы всех позиций одним запросом (списки товаров)."""
        sql = ("SELECT nom_id, COALESCE(SUM(qty),0) v FROM reserves"
               " WHERE state='active'")
        params: list[Any] = []
        if warehouse_id:
            sql += " AND warehouse_id=?"
            params.append(warehouse_id)
        sql += " GROUP BY nom_id"
        return {r["nom_id"]: round(num(r["v"]), 3) for r in self.db.query(sql, params)}

    def reserved(self, nom_id: str, warehouse_id: str = "", variant_id: str = "") -> float:
        sql = ("SELECT COALESCE(SUM(qty),0) v FROM reserves"
               " WHERE nom_id=? AND state='active'")
        params: list[Any] = [nom_id]
        if warehouse_id:
            sql += " AND warehouse_id=?"
            params.append(warehouse_id)
        if variant_id:
            sql += " AND variant_id=?"
            params.append(variant_id)
        row = self.db.one(sql, params) or {}
        return round(num(row.get("v")), 3)

    def reserve(self, nom_id: str, qty: float, order_id: str = "",
                warehouse_id: str = "", note: str = "", variant_id: str = "") -> dict:
        qty = num(qty)
        if qty <= 0:
            raise ValueError("Количество резерва должно быть больше нуля")
        free = (self.qty(nom_id, warehouse_id, variant_id)
                - self.reserved(nom_id, warehouse_id, variant_id))
        if free < qty:
            raise ValueError(f"Свободно только {round(free, 1)} шт — зарезервировать {round(qty, 1)} нельзя")
        return self.db.upsert("reserves", {
            "id": uid("rsv"), "at": now_iso(), "nom_id": nom_id,
            "variant_id": variant_id or None, "warehouse_id": warehouse_id or None,
            "qty": round(qty, 3), "order_id": order_id or None,
            "state": "active", "note": note})

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
    def sales_stats_all(self) -> dict[str, dict[str, Any]]:
        """Статистика продаж для всех позиций одним запросом.

        Результат совпадает с sales_stats(nom_id) по каждой позиции, но
        вместо трёх запросов на товар в списке товаров выполняется один.
        Дополнительно считается посуточная динамика за 7 дней (sold_days)
        для мини-спарклайна в плитке товара (13.1, идея 16)."""
        since7 = (datetime.now() - timedelta(days=SALE_DAYS)).isoformat()
        since30 = (datetime.now() - timedelta(days=30)).isoformat()
        rows = self.db.query(
            "SELECT nom_id,"
            " COALESCE(SUM(CASE WHEN at>=? THEN -qty ELSE 0 END),0) s7,"
            " COALESCE(SUM(CASE WHEN at>=? THEN -qty ELSE 0 END),0) s30,"
            " MAX(at) last_sale"
            " FROM stock_moves WHERE doc_kind='sale' AND qty<0"
            " GROUP BY nom_id", (since7, since30))
        out: dict[str, dict[str, Any]] = {}
        # Посуточные продажи одним запросом: {nom_id: {date: qty}}
        day_rows = self.db.query(
            "SELECT nom_id, substr(at, 1, 10) d, COALESCE(SUM(-qty),0) v"
            " FROM stock_moves WHERE doc_kind='sale' AND qty<0 AND at>=?"
            " GROUP BY nom_id, d", (since7,))
        per_day: dict[str, dict[str, float]] = {}
        for r in day_rows:
            per_day.setdefault(r["nom_id"], {})[r["d"]] = num(r["v"])
        for row in rows:
            sold7 = num(row["s7"])
            days = []
            for i in range(SALE_DAYS - 1, -1, -1):
                key = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                days.append(round((per_day.get(row["nom_id"]) or {}).get(key, 0.0), 1))
            out[row["nom_id"]] = {
                "sold_7": round(sold7, 1),
                "sold_30": round(num(row["s30"]), 1),
                "rate_per_day": round(sold7 / SALE_DAYS, 2) if sold7 else 0.0,
                "last_sale": row.get("last_sale") or "",
                "sold_days": days,
            }
        return out

    def sales_stats(self, nom_id: str) -> dict[str, Any]:
        """Продажи за 7 и 30 дней, скорость, дата последней продажи.

        Формат совпадает с sales_stats_all (включая sold_days — посуточную
        динамику за 7 дней для спарклайна, 13.1 идея 16)."""
        since7 = (datetime.now() - timedelta(days=SALE_DAYS)).isoformat()
        since30 = (datetime.now() - timedelta(days=30)).isoformat()
        sold7 = self._sold(nom_id, since7)
        sold30 = self._sold(nom_id, since30)
        last = self.db.one(
            "SELECT MAX(at) a FROM stock_moves WHERE nom_id=? AND doc_kind='sale'",
            (nom_id,)) or {}
        rate = sold7 / SALE_DAYS if sold7 else 0.0
        day_rows = self.db.query(
            "SELECT substr(at, 1, 10) d, COALESCE(SUM(-qty),0) v"
            " FROM stock_moves WHERE nom_id=? AND doc_kind='sale' AND qty<0 AND at>=?"
            " GROUP BY d", (nom_id, since7))
        per_day = {r["d"]: num(r["v"]) for r in day_rows}
        days = []
        for i in range(SALE_DAYS - 1, -1, -1):
            key = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            days.append(round(per_day.get(key, 0.0), 1))
        return {"sold_7": round(sold7, 1), "sold_30": round(sold30, 1),
                "rate_per_day": round(rate, 2), "last_sale": last.get("a") or "",
                "sold_days": days}

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
