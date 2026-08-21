"""Список закупок пластика: ручной + автоформируемый.

Дополняет прогноз расхода (`accounting.filament_stats`) постоянным списком,
который можно вести как чек-лист: добавить вручную или заполнить автоматически
по двум источникам — катушки ниже порога и темп расхода за 30 дней (материал
кончится через N дней). Купленное отмечается галочкой и уходит в архив.

Это продолжение автоучёта филамента: раньше система только сообщала «мало
пластика», теперь она сама складывает это в список покупок.
"""
from __future__ import annotations

import json

from .accounting import Accounting, num, uid
from .config import now_iso


class ShoppingList:
    """Постоянный список закупок. Не держит потоков, работает через db."""

    def __init__(self, db):
        self.db = db
        self.acc = Accounting(db)

    # ------------------------------------------------------------- хранилище
    def items(self, include_done: bool = False) -> list[dict]:
        sql = "SELECT * FROM shopping_items"
        if not include_done:
            sql += " WHERE done=0"
        sql += " ORDER BY done, datetime(created_at)"
        return self.db.query(sql)

    def add(self, data: dict) -> dict:
        data = dict(data)
        if data.get("id"):
            existing = self.db.one(
                "SELECT received_at FROM shopping_items WHERE id=?", (data["id"],)
            )
            if existing and existing.get("received_at"):
                raise ValueError("Подтверждённый приход нельзя изменить как строку закупки")
        else:
            data["id"] = uid("shop")
        data.setdefault("created_at", now_iso())
        data["updated_at"] = now_iso()
        return self.db.upsert("shopping_items", data)

    def toggle(self, item_id: str, done: bool) -> dict:
        item = self.db.one("SELECT * FROM shopping_items WHERE id=?", (item_id,))
        if not item:
            raise ValueError("Позиция закупки не найдена")
        if done:
            raise ValueError("Закупка закрывается только после подтверждённого приёма на склад")
        if item.get("received_at"):
            raise ValueError("Принятую закупку нельзя вернуть в открытые без складской корректировки")
        self.db.execute("UPDATE shopping_items SET done=0, updated_at=? WHERE id=?",
                        (now_iso(), item_id))
        return self.db.one("SELECT * FROM shopping_items WHERE id=?", (item_id,)) or {}

    def delete(self, item_id: str) -> None:
        item = self.db.one("SELECT * FROM shopping_items WHERE id=?", (item_id,))
        if not item:
            return
        if item.get("received_at") and item.get("received_at") != "legacy":
            raise ValueError("Принятую закупку нельзя удалить: она связана со складом и кассой")
        self.db.delete("shopping_items", item_id)

    def clear_done(self) -> int:
        # Подтверждённые приходы — часть аудита и хранят ключи идемпотентности.
        # Очищать можно только старые галочки прежней версии без складской связи.
        cur = self.db.execute(
            "DELETE FROM shopping_items WHERE done=1"
            " AND COALESCE(received_at,'') IN ('','legacy')"
        )
        return cur.rowcount if cur else 0

    def _receipt_result(self, item: dict, *, already_received: bool) -> dict:
        try:
            spool_ids = json.loads(item.get("receipt_spool_ids") or "[]")
        except (TypeError, json.JSONDecodeError):
            spool_ids = []
        if not isinstance(spool_ids, list):
            spool_ids = []
        spools = []
        if spool_ids:
            marks = ",".join("?" for _ in spool_ids)
            spools = self.db.query(
                f"SELECT * FROM spools WHERE id IN ({marks}) ORDER BY created_at,id",
                spool_ids,
            )
        tx = None
        if item.get("receipt_tx_id"):
            tx = self.db.one(
                "SELECT * FROM transactions WHERE id=?", (item["receipt_tx_id"],)
            )
        return {
            "ok": True,
            "already_received": already_received,
            "item": item,
            "spools": spools,
            "transaction": tx,
            "received_grams": round(
                num(item.get("received_qty")) * num(item.get("received_spool_grams")), 1
            ),
        }

    def receive(
        self,
        item_id: str,
        *,
        received_confirmed: bool = False,
        payment_confirmed: bool = False,
        material: str = "",
        color_name: str = "",
        color_hex: str = "#4b5563",
        brand: str = "",
        spool_count: float = 1,
        spool_grams: float = 1000,
        total_amount: float = 0,
        account_id: str = "",
        supplier: str = "",
        warehouse_id: str = "",
        request_id: str = "",
    ) -> dict:
        """Атомарно принять закупку: отдельные катушки + расход + закрытие строки.

        Физический приход и факт оплаты подтверждаются оператором. Уникальный
        ``request_id`` делает повторный клик и сетевой retry безопасным.
        """
        if not received_confirmed:
            raise ValueError("Подтвердите, что катушки действительно получены")
        request_id = str(request_id or "").strip()[:120]
        if not request_id:
            raise ValueError("Не указан ключ операции приёмки")

        count_value = num(spool_count)
        count = int(count_value)
        if count < 1 or count > 100 or abs(count_value - count) > 0.0001:
            raise ValueError("Количество катушек должно быть целым числом от 1 до 100")
        grams = round(num(spool_grams), 1)
        if grams <= 0 or grams > 10000:
            raise ValueError("Вес одной катушки должен быть от 1 до 10000 г")
        amount = round(num(total_amount), 2)
        if amount < 0:
            raise ValueError("Сумма закупки не может быть отрицательной")
        if amount > 0 and not payment_confirmed:
            raise ValueError("Подтвердите фактическую оплату закупки")

        item = self.db.one("SELECT * FROM shopping_items WHERE id=?", (item_id,))
        if not item:
            raise ValueError("Позиция закупки не найдена")
        if item.get("received_at"):
            if item.get("receipt_request_id") == request_id:
                return self._receipt_result(item, already_received=True)
            raise ValueError("Эта закупка уже принята на склад")
        used = self.db.one(
            "SELECT id FROM shopping_items WHERE receipt_request_id=?", (request_id,)
        )
        if used and used.get("id") != item_id:
            raise ValueError("Ключ операции уже использован для другой закупки")

        material = str(material or item.get("material") or "").strip().upper()
        if not material:
            raise ValueError("Укажите материал")
        color_name = str(color_name or item.get("color_name") or "").strip()
        brand = str(brand or item.get("brand") or "").strip()
        supplier = str(supplier or "").strip()
        warehouse_id = str(warehouse_id or "").strip()
        color_hex = str(color_hex or "#4b5563").strip()
        if not (len(color_hex) == 7 and color_hex.startswith("#")):
            color_hex = "#4b5563"

        account_id = str(
            account_id or self.db.setting("default_account", "cash") or ""
        ).strip()
        if amount > 0 and not self.db.one(
            "SELECT id FROM accounts WHERE id=? AND archived=0", (account_id,)
        ):
            raise ValueError("Касса для оплаты не найдена")

        stamp = now_iso()
        spool_ids: list[str] = []
        tx = None
        with self.db.transaction():
            # Повторяем проверки под блокировкой до первой записи.
            fresh = self.db.one("SELECT * FROM shopping_items WHERE id=?", (item_id,)) or {}
            if fresh.get("received_at"):
                if fresh.get("receipt_request_id") == request_id:
                    return self._receipt_result(fresh, already_received=True)
                raise ValueError("Эта закупка уже принята на склад")
            used = self.db.one(
                "SELECT id FROM shopping_items WHERE receipt_request_id=?", (request_id,)
            )
            if used and used.get("id") != item_id:
                raise ValueError("Ключ операции уже использован для другой закупки")

            price_each = round(amount / count, 2) if amount else 0.0
            for index in range(count):
                spool_id = uid("sp")
                spool_ids.append(spool_id)
                spool_price = (
                    round(amount - price_each * (count - 1), 2)
                    if amount and index == count - 1 else price_each
                )
                self.db.upsert("spools", {
                    "id": spool_id,
                    "material": material,
                    "brand": brand,
                    "color_name": color_name,
                    "color_hex": color_hex,
                    "total_grams": grams,
                    "remaining_grams": grams,
                    "price": spool_price,
                    "warehouse_id": warehouse_id or None,
                    "supplier": supplier,
                    "ams_sync": 1,
                    "created_at": stamp,
                    "updated_at": stamp,
                })
            if amount:
                tx = self.acc.add_transaction(
                    "expense", "filament", amount,
                    f"Пластик {material} {color_name}".strip(),
                    note=(f"Приём закупки: {count} × {grams:g} г"
                          + (f"; поставщик {supplier}" if supplier else "")),
                    account_id=account_id,
                    auto=False,
                    deductible=True,
                    at=stamp,
                )
            self.db.execute(
                "UPDATE shopping_items SET done=1,material=?,color_name=?,brand=?,"
                "received_at=?,receipt_request_id=?,receipt_spool_ids=?,receipt_amount=?,"
                "receipt_tx_id=?,received_qty=?,received_spool_grams=?,updated_at=? WHERE id=?",
                (
                    material, color_name, brand, stamp, request_id,
                    json.dumps(spool_ids, ensure_ascii=False), amount,
                    (tx or {}).get("id") or "", count, grams, stamp, item_id,
                ),
            )
            self.db.add_event(
                "shopping", "Пластик принят на склад",
                f"{material} {color_name}: {count} × {grams:g} г, {amount:g} ₽".strip(),
                "", {"shopping_id": item_id, "spool_ids": spool_ids,
                     "transaction_id": (tx or {}).get("id") or ""},
            )

        received = self.db.one("SELECT * FROM shopping_items WHERE id=?", (item_id,)) or {}
        return self._receipt_result(received, already_received=False)

    # ------------------------------------------------------------ автозаполнение
    def auto_fill(self, dry_run: bool = False) -> dict:
        """Собрать закупку из двух источников и добавить недостающее.

        Источники:
          1) катушки ниже порога (`filament_low_threshold`) — по остатку;
          2) темп расхода за 30 дней — материал, который закончится
             быстрее, чем через `shopping_runout_days`.

        Повторные записи одного материала и цвета не дублируются: проверяется,
        есть ли уже активная строка по этой паре. Разные цвета не склеиваются.
        """
        added: list[dict] = []
        suggestions: list[dict] = []
        existing = {
            (str(r.get("material") or "").upper(),
             str(r.get("color_name") or "").strip().casefold())
            for r in self.items(include_done=True) if not num(r.get("done"))
        }

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
                "material": mat,
                "color_name": spool.get("color_name") or "",
                "brand": spool.get("brand") or "",
                "qty": 1, "unit": "катушка", "reason": reason,
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
                "name": mat, "material": mat, "color_name": "", "brand": "",
                "qty": 1, "unit": "кг",
                "reason": f"темп {round(rate)} г/дн — хватит на ~{int(days_left)} дн",
            })

        for s in suggestions:
            mat = s["material"]
            key = (mat, str(s.get("color_name") or "").strip().casefold())
            if key in existing:
                continue
            existing.add(key)
            if dry_run:
                added.append(s)
                continue
            row = self.add({
                "name": s["name"], "material": mat,
                "color_name": s.get("color_name") or "",
                "brand": s.get("brand") or "",
                "qty": s["qty"], "unit": s["unit"],
                "reason": s["reason"], "source": "auto",
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
        for index, item in enumerate(items[:12], 1):
            qty = f"{round(num(item.get('qty')), 1)} {item.get('unit') or 'кг'}"
            reason = f" — {item.get('reason')}" if item.get("reason") else ""
            lines.append(
                f"{index}. {item.get('name') or item.get('material')} · {qty}{reason}"
            )
        return "\n".join(lines)
