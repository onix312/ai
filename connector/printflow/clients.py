"""Аналитика клиентов PrintFlow 5.0: RFM-сегменты, дубли и объединение."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .accounting import num
from .db import Database


class Clients:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ RFM
    def rfm(self, days: int = 90) -> list[dict]:
        """Recency / Frequency / Monetary по клиентам за период."""
        since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
        rows = self.db.query("SELECT * FROM customers ORDER BY name")
        out = []
        for c in rows:
            orders = self.db.query(
                "SELECT * FROM orders WHERE customer_id=? AND datetime(created_at)>=datetime(?)",
                (c["id"], since))
            # prepaid — старое поле совместимости старого каталога; принять
            # во внимание максимум, как это делает экономика заказа.
            paid = sum(max(num(o.get("paid")), num(o.get("prepaid"))) for o in orders)
            last = max((o.get("created_at") or "") for o in orders) if orders else ""
            seg = self._segment(len(orders), paid, last)
            out.append({
                "id": c["id"], "name": c.get("name") or "Без имени",
                "phone": c.get("phone") or "", "messenger": c.get("messenger") or "",
                "orders": len(orders), "paid": round(paid, 2),
                "last_order": last, "segment": seg,
            })
        # сортируем: кто пропал — вниз, кто платит — вверх
        order = {"Активный": 0, "Постоянный": 1, "Новый": 2, "Затухающий": 3, "Потерянный": 4}
        out.sort(key=lambda r: (order.get(r["segment"], 9), -r["paid"]))
        return out

    def _segment(self, count: int, paid: float, last: str) -> str:
        if count == 0:
            return "Новый"
        if count >= 3 and paid > 0:
            return "Постоянный"
        parsed = datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None
        if parsed and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).days if parsed else 0
        if parsed and age <= 30:
            return "Активный"
        if parsed and age > 90:
            return "Потерянный"
        return "Затухающий"

    # ---------------------------------------------------------------- дубли
    def duplicates(self) -> list[dict]:
        """Похожие клиенты: одинаковый телефон или одинаковое имя."""
        groups: dict[str, list[dict]] = {}
        for c in self.db.query("SELECT * FROM customers ORDER BY created_at"):
            key = (c.get("phone") or "").strip().replace(" ", "")
            if key:
                groups.setdefault("phone:" + key, []).append(c)
            name = (c.get("name") or "").strip().lower()
            if name:
                groups.setdefault("name:" + name, []).append(c)
        out = []
        seen: set[frozenset[str]] = set()
        for _, members in groups.items():
            ids = {m["id"] for m in members}
            if len(ids) < 2:
                continue
            # не дублируем одну и ту же группу дважды (по имени и по телефону)
            signature = frozenset(ids)
            if signature in seen:
                continue
            seen.add(signature)
            out.append([{k: m[k] for k in ("id", "name", "phone", "messenger")}
                        for m in members])
        return out

    def merge(self, keep_id: str, drop_ids: list[str]) -> dict:
        """Перенести историю из дублей в основного клиента и удалить дубли."""
        keep = self.db.one("SELECT * FROM customers WHERE id=?", (keep_id,))
        if not keep:
            raise ValueError("Основной клиент не найден")
        moved = 0
        with self.db.transaction():
            for drop_id in drop_ids or []:
                if drop_id == keep_id:
                    continue
                if not self.db.one("SELECT id FROM customers WHERE id=?", (drop_id,)):
                    continue
                for table in ("orders", "payments", "transactions", "customer_feedback"):
                    self.db.execute(
                        f"UPDATE {table} SET customer_id=? WHERE customer_id=?",
                        (keep_id, drop_id))
                    moved += 1
                self.db.delete("customers", drop_id)
        return {"ok": True, "kept": keep_id, "removed": len(drop_ids or []),
                "moved": moved}
