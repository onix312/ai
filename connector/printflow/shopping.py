"""Список закупок пластика: ручной + автоформируемый.

Дополняет прогноз расхода (`accounting.filament_stats`) постоянным списком,
который можно вести как чек-лист: добавить вручную или заполнить автоматически
по двум источникам — катушки ниже порога и темп расхода за 30 дней (материал
кончится через N дней). Купленное отмечается галочкой и уходит в архив.

Это продолжение автоучёта филамента: раньше система только сообщала «мало
пластика», теперь она сама складывает это в список покупок.
"""
from __future__ import annotations

import time
from typing import Any

from .accounting import num
from .config import now_iso


class ShoppingList:
    """Постоянный список закупок. Не держит потоков, работает через db."""

    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------- хранилище
    def items(self, include_done: bool = False) -> list[dict]:
        sql = "SELECT * FROM shopping_items"
        if not include_done:
            sql += " WHERE done=0"
        sql += " ORDER BY done, datetime(created_at)"
        return self.db.query(sql)

    def add(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("id"):
            data["id"] = f"shop_{int(time.time() * 1000)}"
        data.setdefault("created_at", now_iso())
        data["updated_at"] = now_iso()
        return self.db.upsert("shopping_items", data)

    def toggle(self, item_id: str, done: bool) -> dict:
        self.db.execute("UPDATE shopping_items SET done=?, updated_at=? WHERE id=?",
                        (1 if done else 0, now_iso(), item_id))
        return self.db.one("SELECT * FROM shopping_items WHERE id=?", (item_id,)) or {}

    def delete(self, item_id: str) -> None:
        self.db.delete("shopping_items", item_id)

    def clear_done(self) -> int:
        cur = self.db.execute("DELETE FROM shopping_items WHERE done=1")
        return cur.rowcount if cur else 0

    # ------------------------------------------------------------ автозаполнение
    def auto_fill(self, dry_run: bool = False) -> dict:
        """Собрать закупку из двух источников и добавить недостающее.

        Источники:
          1) катушки ниже порога (`filament_low_threshold`) — по остатку;
          2) темп расхода за 30 дней — материал, который закончится
             быстрее, чем через `shopping_runout_days`.

        Повторные записи одного материала не дублируются: проверяется,
        есть ли уже активная (не купленная) строка по этому материалу.
        """
        added: list[dict] = []
        suggestions: list[dict] = []
        existing = {str(r.get("material") or "").upper()
                    for r in self.items(include_done=True) if not num(r.get("done"))}

        # 1) катушки ниже порога
        threshold = num(self.db.setting("filament_low_threshold", 15.0), 15.0)
        for spool in self.db.query("SELECT * FROM spools WHERE archived=0"):
            total = max(1.0, num(spool.get("total_grams"), 1000))
            left = num(spool.get("remaining_grams"))
            if left / total * 100 > threshold:
                continue
            mat = str(spool.get("material") or "PLA").upper()
            reason = f"{spool.get('color_name') or mat}: осталось {round(left)} г ({round(left/total*100)}%)"
            suggestions.append({
                "name": f"{mat} {spool.get('color_name') or ''}".strip(),
                "material": mat, "qty": 1, "unit": "катушка", "reason": reason,
            })

        # 2) темп расхода за 30 дней
        runout_days = num(self.db.setting("shopping_runout_days", 7.0), 7.0)
        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(days=30)).isoformat()
        usage_rows = self.db.query(
            "SELECT UPPER(s.material) m, SUM(f.grams) g FROM filament_usage f"
            " LEFT JOIN spools s ON s.id=f.spool_id"
            " WHERE f.at>=? AND s.material IS NOT NULL AND s.material<>''"
            " GROUP BY UPPER(s.material)", (since,))
        for row in usage_rows:
            mat = str(row.get("m") or "").upper()
            rate = num(row.get("g")) / 30.0  # г/день
            if rate <= 0:
                continue
            stock = num(self.db.one(
                "SELECT COALESCE(SUM(remaining_grams),0) v FROM spools"
                " WHERE archived=0 AND UPPER(material)=?", (mat,))["v"])
            days_left = stock / rate
            if days_left > runout_days:
                continue
            suggestions.append({
                "name": mat, "material": mat, "qty": 1, "unit": "кг",
                "reason": f"темп {round(rate)} г/дн — хватит на ~{int(days_left)} дн",
            })

        for s in suggestions:
            mat = s["material"]
            if mat in existing:
                continue
            existing.add(mat)
            if dry_run:
                added.append(s)
                continue
            row = self.add({
                "name": s["name"], "material": mat, "qty": s["qty"],
                "unit": s["unit"], "reason": s["reason"], "source": "auto",
            })
            added.append(row)

        if added:
            self.db.add_event("shopping", "Список закупок пополнен",
                              f"Добавлено позиций: {len(added)}")
        return {"ok": True, "added": added, "count": len(added),
                "total_open": len(self.items())}

    def summary(self) -> dict:
        """Сводка для обзора и Telegram: сколько открытых позиций и на что."""
        open_items = self.items()
        return {
            "open": len(open_items),
            "items": open_items[:10],
        }

    def text(self) -> str:
        """Текст списка для Telegram."""
        items = self.items()
        if not items:
            return "🛒 Список закупок пуст."
        lines = ["🛒 Список закупок:"]
        for i in items[:12]:
            mark = "·"
            qty = f"{round(num(i.get('qty')), 1)} {i.get('unit') or 'кг'}"
            reason = f" — {i.get('reason')}" if i.get("reason") else ""
            lines.append(f"{mark} {i.get('name') or i.get('material')} · {qty}{reason}")
        return "\n".join(lines)
