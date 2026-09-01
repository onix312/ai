"""CRUD-операции над данными PrintFlow и импорт старых резервных копий.

Здесь собрана вся работа с заказами, клиентами, нишами, статусами, складом
и каталогом, чтобы HTTP-слой оставался тонким.
"""
from __future__ import annotations

import json
from typing import Any

from .accounting import Accounting, num, uid
from .config import DEFAULT_SETTINGS, now_iso
from .db import Database

ORDER_FIELDS = (
    "number product customer_id customer_name phone messenger channel niche_id status "
    "priority qty material color grams hours price cost prepaid manual_minutes file notes "
    "quality quality_note due auto_cost "
    # деньги и условия сделки
    "paid discount delivery fee rush payer account_id design_minutes "
    # многоцветная печать и чек-лист качества
    "colors qc_done "
    # катушки со склада, привязанные к заказу: [{spool_id, grams, note}]
    "spools "
    # связь с номенклатурой и складом (3.0+)
    "nom_id warehouse_id reserved items_override "
    # публичный Telegram-контур: источник, идемпотентность, токен ссылки,
    # согласование цены/срока и отметки выдачи
    "client_source client_request_id client_track_token_hash client_track_token_at "
    "client_quote_status client_quote_version client_quote_sent_at "
    "client_quote_accepted_at client_variant_id client_ready_at client_delivered_at"
).split()


class Repo:
    def __init__(self, db: Database):
        self.db = db
        self.acc = Accounting(db)

    # ------------------------------------------------------------------ заказы
    def orders(self, status: str = "", search: str = "", niche_id: str = "") -> list[dict]:
        sql, params = "SELECT * FROM orders WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status)
        if niche_id:
            sql += " AND niche_id=?"
            params.append(niche_id)
        if search:
            like = f"%{search.lower()}%"
            sql += (" AND (pylower(number) LIKE ? OR pylower(product) LIKE ?"
                    " OR pylower(customer_name) LIKE ? OR pylower(phone) LIKE ?)")
            params += [like, like, like, like]
        sql += " ORDER BY datetime(created_at) DESC"
        rows = self.db.query(sql, params)
        # Сначала счётчики позиций: экономика заказа использует items_count
        # и не делает пробный запрос в order_items для каждой строки списка.
        if rows:
            marks = ",".join("?" for _ in rows)
            counts = {r["order_id"]: int(num(r["n"])) for r in self.db.query(
                f"SELECT order_id, COUNT(*) n FROM order_items"
                f" WHERE order_id IN ({marks}) GROUP BY order_id",
                [r["id"] for r in rows])}
            for row in rows:
                row["items_count"] = counts.get(row["id"], 0)
        for row in rows:
            row["economics"] = self.acc.order_economics(row)
        return rows

    def order(self, order_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not row:
            return None
        row["economics"] = self.acc.order_economics(row)
        row["items"] = self.db.query(
            "SELECT * FROM order_items WHERE order_id=? ORDER BY position", (order_id,))
        row["items_economics"] = self.acc.order_items_economics(row)
        row["jobs"] = self.db.query(
            "SELECT * FROM print_jobs WHERE order_id=? ORDER BY datetime(created_at) DESC", (order_id,))
        row["transactions"] = self.db.query(
            "SELECT * FROM transactions WHERE order_id=? ORDER BY datetime(at) DESC", (order_id,))
        row["defects"] = self.db.query(
            "SELECT * FROM defects WHERE order_id=? ORDER BY datetime(at) DESC", (order_id,))
        row["photos"] = self.db.query(
            "SELECT * FROM order_photos WHERE order_id=? ORDER BY datetime(at) DESC", (order_id,))
        return row

    def next_order_number(self) -> str:
        row = self.db.one("SELECT COUNT(*) n FROM orders") or {"n": 0}
        return str(1000 + int(num(row["n"])) + 1)

    def _save_order_items(self, order_id: str, items: list | None) -> dict:
        """Сохранить состав заказа (мультизаказ: разные товары на одной плите).

        Цена заказа = сумма цен позиций; количество = сумма количеств.
        Недостающие у позиции название/цена/граммы/часы добираются из базы
        товаров по nom_id — база остаётся источником веса каждой позиции.
        Возвращает {"total_price": ..., "total_qty": ..., "names": [...]}.
        """
        summary: dict = {"total_price": 0.0, "total_qty": 0.0,
                        "total_grams": 0.0, "total_hours": 0.0, "names": []}
        if items is None:
            return summary
        if not isinstance(items, list):
            raise ValueError("Состав заказа должен быть списком позиций")
        self.db.execute("DELETE FROM order_items WHERE order_id=?", (order_id,))
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            nom_id = str(raw.get("nom_id") or "").strip()
            variant_id = str(raw.get("variant_id") or "").strip()
            qty = num(raw.get("qty"), 1)
            if qty <= 0:
                continue
            price = num(raw.get("price"))
            grams = num(raw.get("grams"))
            hours = num(raw.get("hours"))
            if nom_id and (not name or not grams or not hours):
                if variant_id:
                    variant = self.db.one(
                        "SELECT name, grams, hours FROM nom_variants WHERE id=? AND nom_id=? AND archived=0",
                        (variant_id, nom_id)) or {}
                    name = name or str(variant.get("name") or "")
                    grams = grams or num(variant.get("grams"))
                    hours = hours or num(variant.get("hours"))
                nom = self.db.one(
                    "SELECT name, grams, hours FROM nomenclature WHERE id=?",
                    (nom_id,))
                if nom:
                    name = name or str(nom.get("name") or "")
                    grams = grams or num(nom.get("grams"))
                    hours = hours or num(nom.get("hours"))
            if nom_id and not price:
                row = None
                if variant_id:
                    row = self.db.one(
                        "SELECT p.price FROM prices p"
                        " JOIN price_types t ON t.id=p.price_type_id"
                        " WHERE p.variant_id=? AND t.is_base=1"
                        " ORDER BY datetime(p.at) DESC LIMIT 1", (variant_id,))
                if not row:
                    row = self.db.one(
                        "SELECT p.price FROM prices p"
                        " JOIN price_types t ON t.id=p.price_type_id"
                        " WHERE p.nom_id=? AND t.is_base=1"
                        " ORDER BY datetime(p.at) DESC LIMIT 1", (nom_id,))
                price = num(row.get("price")) if row else 0.0
            if not name:
                continue
            self.db.upsert("order_items", {
                "id": uid("oit"), "order_id": order_id, "position": index,
                "nom_id": nom_id, "name": name, "qty": qty, "price": price,
                "grams": grams, "hours": hours,
                "variant_id": str(raw.get("variant_id") or ""),
                "note": str(raw.get("note") or "")})
            summary["total_price"] += round(price * qty, 2)
            summary["total_qty"] += qty
            summary["total_grams"] += grams * qty
            summary["total_hours"] += hours * qty
            summary["names"].append(f"{name} ×{qty:g}")
        summary["total_price"] = round(summary["total_price"], 2)
        return summary

    def save_order(self, data: dict) -> dict:
        """Сохранить заказ одной транзакцией (Н5).

        Заказ, состав, клиент, история полей, платежи и приход дохода —
        раньше это было 5–7 отдельных commit'ов: обрыв питания в середине
        оставлял заказ без состава или с платежом без заказа. Теперь группа
        либо проходит целиком, либо откатывается.
        """
        with self.db.transaction():
            return self._save_order_atomic(data)

    def _save_order_atomic(self, data: dict) -> dict:
        data = dict(data)
        # Служебные разрешения доступны только серверным процессам. Обычный
        # редактор/канбан не может закрыть заказ в обход подтверждения выдачи.
        allow_final_status = bool(data.pop("_allow_final_status", False))
        skip_auto_income = bool(data.pop("_skip_auto_income", False))
        expected_updated_at = str(data.pop("expected_updated_at", "") or "").strip()
        request_key = str(data.get("client_request_id") or "").strip()
        if request_key and not data.get("id"):
            # Telegram/public clients may retry after a timeout. The unique
            # request key turns a retry into a read, not a second order.
            existing_request = self.db.one(
                "SELECT id FROM orders WHERE client_request_id=? LIMIT 1", (request_key,))
            if existing_request:
                return self.order(existing_request["id"]) or existing_request
        # Поле «Оплачено» в карточке — удобный ввод, но источник правды здесь
        # журнал payments. Прямую запись orders.paid больше не допускаем.
        payment_requested = "paid" in data or "prepaid" in data
        requested_paid = max(0.0, num(data.get("paid", data.get("prepaid", 0))))
        data.pop("paid", None)
        data.pop("prepaid", None)
        order_id = data.get("id") or uid("ord")
        existing = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if data.get("id") and not existing:
            # правка несуществующего заказа — это ошибка вызова, а не повод
            # молча завести новый заказ с чужим идентификатором
            raise ValueError("Заказ не найден")
        if existing and expected_updated_at and expected_updated_at != str(existing.get("updated_at") or ""):
            raise ValueError("Заказ уже изменён — обновите карточку перед сохранением")
        payload: dict[str, Any] = {"id": order_id}
        for field in ORDER_FIELDS:
            if field in data:
                payload[field] = data[field]
        if not existing:
            payload.setdefault("number", data.get("number") or self.next_order_number())
            payload["created_at"] = now_iso()
        payload["updated_at"] = now_iso()

        # клиент подтягивается или создаётся автоматически
        name = (payload.get("customer_name") or (existing or {}).get("customer_name") or "").strip()
        phone = (payload.get("phone") or (existing or {}).get("phone") or "").strip()
        if name or phone:
            customer = None
            if phone:
                customer = self.db.one("SELECT * FROM customers WHERE phone=?", (phone,))
            if not customer and name:
                customer = self.db.one("SELECT * FROM customers WHERE name=?", (name,))
            if not customer:
                customer = self.db.upsert("customers", {
                    "id": uid("cus"), "name": name, "phone": phone,
                    "messenger": payload.get("messenger", ""), "created_at": now_iso()})
            payload["customer_id"] = customer["id"]

        # Состав заказа (мультизаказ): по умолчанию позиции — источник цены,
        # количества и нормативов. Ручной override хранится явно, чтобы не
        # было скрытого второго ввода тех же итогов.
        if "items" in data:
            items_summary = self._save_order_items(order_id, data.get("items"))
            if items_summary["names"]:
                if not bool(num(data.get("items_override"), 0)):
                    payload["price"] = items_summary["total_price"]
                    payload["qty"] = items_summary["total_qty"]
                    if items_summary["total_grams"] > 0:
                        payload["grams"] = round(items_summary["total_grams"], 3)
                    if items_summary["total_hours"] > 0:
                        payload["hours"] = round(items_summary["total_hours"], 3)
                if not (payload.get("product") or "").strip():
                    payload["product"] = ", ".join(items_summary["names"])
            data.pop("items", None)

        target_status = payload.get("status") or (existing or {}).get("status") or ""
        status_changed = bool(existing) and target_status != (existing or {}).get("status")
        target_meta = self.db.one("SELECT is_final FROM statuses WHERE id=?", (target_status,))
        final_without_handoff = (
            target_meta and num(target_meta.get("is_final")) and not allow_final_status
            and (not existing or status_changed)
        )
        if final_without_handoff:
            raise ValueError(
                "Финальный статус ставится через «Выдать заказ» с подтверждением оплаты"
            )

        row = self.db.upsert("orders", payload)
        # Конструктор правил: переход заказа между статусами.
        hook = getattr(self, "_on_status_change", None)
        if hook and existing and (existing.get("status") or "") != (payload.get("status") or ""):
            try:
                hook(row, existing.get("status") or "", payload.get("status") or "")
            except Exception:
                pass
        # История изменений: фиксируем только реально поменявшиеся поля.
        if existing:
            for field, value in payload.items():
                if field in ("id", "updated_at"):
                    continue
                old = existing.get(field)
                if str(old) != str(value):
                    self.db.execute(
                        "INSERT INTO order_history(at,order_id,field,old_value,new_value,author)"
                        " VALUES(?,?,?,?,?,?)",
                        (now_iso(), order_id, field, str(old or ""), str(value or ""),
                         data.get("author", "user")))
        if payment_requested:
            current_paid = max(num(row.get("paid")), num(row.get("prepaid")))
            # Старое поле prepaid переносим в текущий счётчик без создания
            # задним числом новой кассовой операции.
            if num(row.get("prepaid")) > num(row.get("paid")):
                self.db.execute("UPDATE orders SET paid=?, prepaid=0 WHERE id=?",
                                (current_paid, order_id))
            delta = round(requested_paid - current_paid, 2)
            if delta > 0:
                self.acc.add_payment(order_id, delta,
                                     "prepay" if requested_paid < num(row.get("price")) else "payment",
                                     row.get("account_id") or "", "карточка заказа")
            elif delta < 0:
                self.acc.add_payment(order_id, -delta, "refund",
                                     row.get("account_id") or "", "карточка заказа")
            row = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,)) or row
        status = row.get("status")
        final = self.db.one("SELECT is_final FROM statuses WHERE id=?", (status,))
        if final and final["is_final"]:
            if not row.get("closed_at"):
                self.db.execute("UPDATE orders SET closed_at=? WHERE id=?", (now_iso(), order_id))
            if not skip_auto_income:
                self.acc.register_order_income(row)
        elif row.get("closed_at"):
            self.db.execute("UPDATE orders SET closed_at='' WHERE id=?", (order_id,))
        if not existing:
            self.db.add_event("order", "Новый заказ",
                              f"№{row.get('number','')} · {row.get('product','')}",
                              data=({"order_id": order_id}))
        return self.order(order_id) or row

    def duplicate_order(self, order_id: str) -> dict:
        """«Повторить заказ»: копия с новым номером, статусом «Новая заявка» и датой.

        Цена подтягивается текущая, закрытие и оплата не переносятся.
        """
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        data = dict(order)
        data.pop("id", None)
        data["number"] = self.next_order_number()
        data["status"] = "new"
        data["created_at"] = now_iso()
        data["updated_at"] = now_iso()
        data["closed_at"] = ""
        data["paid"] = 0.0
        data["prepaid"] = 0.0
        data["actual_grams"] = 0.0
        data["actual_hours"] = 0.0
        data["actual_cost"] = 0.0
        # Резерв готового товара уникален для исходного заказа. Копия может
        # повторно выбрать склад после проверки актуального остатка.
        data["reserved"] = 0
        data["notes"] = f"повтор заказа №{order.get('number')}" + (
            f"\n{order.get('notes')}" if order.get("notes") else "")
        data["author"] = "duplicate"
        # Мультизаказ: состав копируется вместе с карточкой.
        items = self.db.query(
            "SELECT nom_id, name, qty, price, grams, hours, variant_id, note"
            " FROM order_items WHERE order_id=? ORDER BY position", (order_id,))
        if items:
            data["items"] = items
        return self.save_order(data)

    def order_history(self, order_id: str) -> list[dict]:
        return self.db.query(
            "SELECT * FROM order_history WHERE order_id=? ORDER BY id DESC LIMIT 100",
            (order_id,))

    def data_check(self) -> dict:
        """Авто-проверка данных: заказы без цены, платежи без заказа, катушки без привязки."""
        problems: list[dict] = []
        finals = {r["id"] for r in self.db.query("SELECT id FROM statuses WHERE is_final=1")}
        for o in self.db.query("SELECT * FROM orders"):
            if o["status"] not in finals and num(o.get("price")) <= 0:
                problems.append({"kind": "order_no_price", "id": o["id"],
                                 "title": f"Заказ №{o.get('number')} без цены",
                                 "detail": o.get("product") or ""})
        for p in self.db.query("SELECT * FROM payments WHERE order_id IS NOT NULL"):
            if not self.db.one("SELECT id FROM orders WHERE id=?", (p["order_id"],)):
                problems.append({"kind": "payment_no_order", "id": p["id"],
                                 "title": "Платёж без заказа",
                                 "detail": f"{round(num(p.get('amount')))} ₽"})
        for j in self.db.query("SELECT * FROM print_jobs WHERE state IN ('done','failed')"):
            if not j.get("spool_id") and num(j.get("grams")) > 0:
                problems.append({"kind": "job_no_spool", "id": j["id"],
                                 "title": "Печать без катушки",
                                 "detail": j.get("name") or j.get("file") or ""})
        return {"count": len(problems), "problems": problems[:50]}

    def set_order_status(self, order_id: str, status: str) -> dict:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        target = str(status or "").strip()
        if not self.db.one("SELECT id FROM statuses WHERE id=?", (target,)):
            raise ValueError("Неизвестный статус заказа")
        current = str(order.get("status") or "new")
        if current == target:
            return order
        allowed = {
            "new": {"estimate", "prepay", "queue"},
            "estimate": {"new", "prepay", "queue"},
            "prepay": {"new", "queue"},
            "queue": {"new", "printing"},
            "printing": {"queue", "post"},
            "post": {"printing", "ready"},
            "ready": {"post"},
            "done": set(),
        }
        if target not in allowed.get(current, set()):
            raise ValueError(f"Переход «{current}» → «{target}» запрещён; используйте допустимый следующий этап")
        # done — финальный статус: сохранить его можно только через выдачу с
        # подтверждением передачи/оплаты, а не перетаскиванием карточки.
        return self.save_order({"id": order_id, "status": target})

    def delete_order(self, order_id: str) -> None:
        if not order_id:
            raise ValueError("Не указан заказ")
        # История движения денег должна пережить удаление карточки заказа:
        # платежи и их проводки остаются связанными через tx_id, но отвязываются от заказа.
        with self.db.transaction():
            self.db.execute("UPDATE print_jobs SET order_id=NULL WHERE order_id=?", (order_id,))
            self.db.execute("UPDATE payments SET order_id=NULL WHERE order_id=?", (order_id,))
            self.db.execute("UPDATE transactions SET order_id=NULL WHERE order_id=?", (order_id,))
            self.db.execute("DELETE FROM order_items WHERE order_id=?", (order_id,))
            self.db.delete("orders", order_id)

    # ---------------------------------------------------------------- клиенты
    def customers(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM customers ORDER BY name")
        for row in rows:
            stats = self.db.one(
                "SELECT COUNT(*) n, COALESCE(SUM(price),0) total, MAX(created_at) last"
                " FROM orders WHERE customer_id=?", (row["id"],)) or {}
            row.update({"orders": int(num(stats.get("n"))),
                        "revenue": round(num(stats.get("total")), 2),
                        "last_order": stats.get("last") or ""})
        return rows

    def save_customer(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("id"):
            data["id"] = uid("cus")
        data.setdefault("created_at", now_iso())
        return self.db.upsert("customers", data)

    # ------------------------------------------------------- статусы и ниши
    def statuses(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM statuses ORDER BY position, name")
        for row in rows:
            count = self.db.one("SELECT COUNT(*) n FROM orders WHERE status=?", (row["id"],))
            row["orders"] = int(num((count or {}).get("n")))
        return rows

    def save_status(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("id"):
            data["id"] = uid("st")
        return self.db.upsert("statuses", data)

    def delete_status(self, status_id: str) -> None:
        used = self.db.one("SELECT COUNT(*) n FROM orders WHERE status=?", (status_id,))
        if used and int(num(used["n"])) > 0:
            raise ValueError("Статус используется в заказах")
        self.db.delete("statuses", status_id)

    def niches(self) -> list[dict]:
        return self.acc.niche_report()

    def save_niche(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("id"):
            data["id"] = uid("nch")
        return self.db.upsert("niches", data)

    def delete_niche(self, niche_id: str) -> None:
        if not niche_id:
            raise ValueError("Не указана ниша")
        self.db.execute("UPDATE orders SET niche_id=NULL WHERE niche_id=?", (niche_id,))
        self.db.delete("niches", niche_id)

    # ------------------------------------------------------------------ склад
    def _decorate_spool(self, row: dict) -> dict:
        total = max(1.0, num(row["total_grams"], 1000))
        row["percent"] = round(num(row["remaining_grams"]) / total * 100, 1)
        row["value"] = round(num(row["remaining_grams"]) / total * num(row["price"]), 2)
        usage = self.db.one(
            "SELECT COALESCE(SUM(grams),0) g FROM filament_usage WHERE spool_id=?", (row["id"],))
        row["used_grams"] = round(num((usage or {}).get("g")), 1)
        dry = self.db.one(
            "SELECT at, minutes, temp FROM drying_sessions WHERE spool_id=?"
            " ORDER BY datetime(at) DESC LIMIT 1", (row["id"],))
        row["last_dry"] = (dry or {}).get("at") or ""
        row["last_dry_min"] = num((dry or {}).get("minutes"))
        row["last_dry_temp"] = num((dry or {}).get("temp"))
        return row

    # ------------------------------------------------------------- материалы
    def materials(self) -> list[dict]:
        """Все материалы из базы: каталог (builtin=1) + свои (builtin=0)."""
        from .materials import material_from_row, seed_builtin_materials
        seed_builtin_materials(self.db)
        rows = self.db.query(
            "SELECT * FROM materials WHERE archived=0 ORDER BY builtin DESC, name")
        return [material_from_row(row) for row in rows]

    def save_material(self, data: dict) -> dict:
        """Создать/изменить свой пластик или настроить встроенный под себя.

        Пустые поля добираются из шаблона (base — ключ встроенного материала),
        поэтому можно «скопировать PETG» и поменять только температуру и цену.
        Встроенный материал можно править (цена, температуры…) — его ключ
        остаётся каталогным, а «⟲ Сбросить» вернёт заводские значения.
        """
        from .materials import MATERIALS, _norm_key, material_from_row
        data = dict(data)
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("Укажите название материала")
        key = _norm_key(str(data.get("key") or name))
        if not key:
            key = f"MY_{uid('mat').upper()}"

        editing = None
        if data.get("id"):
            editing = self.db.one(
                "SELECT * FROM materials WHERE id=?", (str(data["id"]),))
        is_builtin = bool((editing or {}).get("builtin"))
        if is_builtin:
            # Встроенный: ключ не меняется, запись остаётся «каталожной».
            key = str(editing["key"])
            base_key = _norm_key(str(data.get("base") or "")) or key
        else:
            base_key = _norm_key(str(data.get("base") or ""))
        if key in MATERIALS and not is_builtin:
            raise ValueError(
                f"Ключ {key} занят встроенным материалом — "
                f"выберите другой (например, MY_{key})")
        base = MATERIALS.get(base_key, {})

        def pick(field: str, default):
            value = data.get(field)
            if value is None or value == "":
                return base.get(field, default)
            return num(value)

        def pick_pair(min_field: str, max_field: str, base_key: str,
                      fallback: tuple) -> tuple:
            b = tuple(base.get(base_key) or fallback)
            vmin = data.get(min_field)
            vmax = data.get(max_field)
            return (num(vmin) if vmin not in (None, "") else b[0],
                    num(vmax) if vmax not in (None, "") else b[1])

        existing = self.db.one(
            "SELECT * FROM materials WHERE key=?", (key,))
        if existing and existing.get("id") != str(data.get("id") or ""):
            # Архивная запись с тем же ключом — «оживляем» её: пользователь
            # пересоздаёт убранный материал, а не получает конфликт UNIQUE.
            if not num(existing.get("archived")):
                raise ValueError(
                    f"Материал с ключом {key} уже есть — измените его или выберите другой ключ")
        mat_id = (existing or {}).get("id") or str(data.get("id") or "") or uid("mat")
        nozzle = pick_pair("temp_nozzle_min", "temp_nozzle_max",
                           "temp_nozzle", (210, 240))
        bed = pick_pair("temp_bed_min", "temp_bed_max", "temp_bed", (45, 65))
        row = {
            "id": mat_id,
            "key": key,
            "name": name,
            "builtin": 1 if is_builtin else 0,
            "full_name": str(data.get("full_name") or base.get("full_name") or ""),
            "base": base_key,
            "density": pick("density", 1.24),
            "speed_factor": pick("speed_factor", 1.0),
            "support_factor": pick("support_factor", 0.10),
            "price_per_kg": pick("price_per_kg", 0),
            "temp_nozzle_min": nozzle[0],
            "temp_nozzle_max": nozzle[1],
            "temp_bed_min": bed[0],
            "temp_bed_max": bed[1],
            "chamber": str(data.get("chamber") or base.get("chamber") or "open"),
            "fan": pick("fan", 100),
            "shrinkage": pick("shrinkage", 0.25),
            "dry_temp": pick("dry_temp", 50),
            "dry_hours": pick("dry_hours", 5),
            "heat_resistance": pick("heat_resistance", 58),
            "uv_resistant": 1 if data.get("uv_resistant") else 0,
            "food_safe": 1 if data.get("food_safe") else 0,
            "abrasive": 1 if data.get("abrasive") else 0,
            "strengths": str(data.get("strengths") or ""),
            "weaknesses": str(data.get("weaknesses") or ""),
            "use_cases": str(data.get("use_cases") or ""),
            "note": str(data.get("note") or ""),
            "archived": 0,
            "created_at": (existing or {}).get("created_at") or now_iso(),
            "updated_at": now_iso(),
        }
        saved = self.db.upsert("materials", row)
        self.db.add_event("mat", "Материал сохранён",
                          f"{name} ({key})", "", {"material_id": saved["id"]})
        return material_from_row(saved)

    def reset_material(self, mat_id: str) -> None:
        """Вернуть встроенному материалу заводские параметры каталога."""
        row = self.db.one("SELECT * FROM materials WHERE id=?", (mat_id,))
        if not row:
            raise ValueError("Материал не найден")
        if not num(row.get("builtin")):
            raise ValueError("Сбросить можно только встроенный материал")
        # Удаляем строку — справочник снова возьмёт значения из каталога.
        self.db.delete("materials", mat_id)
        self.db.add_event("mat", "Материал возвращён к заводским параметрам",
                          str(row.get("name") or ""), "", {"material_id": mat_id})

    def delete_material(self, mat_id: str) -> None:
        """Убрать материал. Свой — архивируем; встроенный — удаляем настройку
        (каталог вернёт заводские значения, сам тип остаётся в справочнике)."""
        row = self.db.one("SELECT * FROM materials WHERE id=?", (mat_id,))
        if not row:
            raise ValueError("Материал не найден")
        if num(row.get("builtin")):
            self.db.delete("materials", mat_id)
        else:
            self.db.execute(
                "UPDATE materials SET archived=1, updated_at=? WHERE id=?",
                (now_iso(), mat_id))
        self.db.add_event("mat", "Материал убран из справочника",
                          str(row.get("name") or ""), "", {"material_id": mat_id})

    def spools(self, include_archived: bool = False) -> list[dict]:
        sql = "SELECT * FROM spools"
        if not include_archived:
            sql += " WHERE archived=0"
        sql += " ORDER BY material, color_name"
        return [self._decorate_spool(row) for row in self.db.query(sql)]

    def spool(self, spool_id: str) -> dict | None:
        """Одна катушка по id — для страницы QR, без выгрузки всего склада."""
        if not spool_id:
            return None
        row = self.db.one("SELECT * FROM spools WHERE id=?", (spool_id,))
        return self._decorate_spool(row) if row else None

    def save_spool(self, data: dict) -> dict:
        data = dict(data)
        expected_updated_at = str(data.pop("expected_updated_at", "") or "").strip()
        new = not data.get("id")
        if not new and expected_updated_at:
            current = self.db.one("SELECT updated_at FROM spools WHERE id=?", (data["id"],))
            if not current:
                raise ValueError("Катушка не найдена")
            if expected_updated_at != str(current.get("updated_at") or ""):
                raise ValueError("Катушка уже изменена — обновите склад перед сохранением")
        if not data.get("id"):
            data["id"] = uid("sp")
        if new:
            data.setdefault("created_at", now_iso())
            data.setdefault("remaining_grams", data.get("total_grams", 1000))

        # Нормализация AMS слота: 0-15 строка, иначе пусто
        raw_slot = str(data.get("ams_slot") or "").strip()
        norm_slot = ""
        if raw_slot != "":
            try:
                # допускаем "AMS 1 · слот 2" или просто число
                # ищем последнее число в строке
                import re
                m = re.search(r"(\d+)\s*$", raw_slot)
                candidate = m.group(1) if m else raw_slot
                iv = int(candidate)
                if 0 <= iv <= 15 or iv == 254:  # 254 = внешний слот
                    norm_slot = str(iv)
            except Exception:
                norm_slot = ""
        data["ams_slot"] = norm_slot

        # Синхронизация location и tray_uuid с наличием слота
        if norm_slot == "":
            data["tray_uuid"] = ""
            if str(data.get("location") or "").strip() == "ams":
                data["location"] = "shop"
        else:
            # Если слот указан — катушка в AMS
            if not str(data.get("location") or "").strip():
                data["location"] = "ams"
            else:
                # если явно указали shop но слот есть — всё равно ams, чтобы не плодить фантомы
                if str(data.get("location")).strip() not in ("ams", "shop", "home", "dry", "other"):
                    data["location"] = "ams"
                elif str(data.get("location")).strip() != "ams":
                    # пользователь указал слот — считаем что в AMS, но не затираем если dry/other?
                    # Логика: если слот есть, location должен быть ams, иначе склад покажет AMS отдельно
                    if str(data.get("location")).strip() == "shop":
                        # shop + slot = противоречие, но пользователь явно привязывает — ставим ams
                        data["location"] = "ams"
            # printer_id обязателен при слоте, но не блокируем сохранение — проверит API

        # location по умолчанию
        if not str(data.get("location") or "").strip():
            data["location"] = "shop"

        data["updated_at"] = now_iso()
        return self.db.upsert("spools", data)

    def delete_spool(self, spool_id: str) -> None:
        if not spool_id:
            raise ValueError("Не указана катушка")
        self.db.delete("spools", spool_id)

    def cleanup_ams_phantoms(self) -> dict:
        """Убрать фантомные катушки AMS: дубли слотов и пустые материалы.

        Возвращает счётчики архивированных и очищенных.
        """
        archived = 0
        cleared = 0
        # 1. Дубли одного слота на одном принтере: оставляем самую свежую
        dup_groups = self.db.query(
            "SELECT printer_id, ams_slot, COUNT(*) cnt FROM spools "
            "WHERE archived=0 AND ams_slot<>'' AND printer_id<>'' "
            "GROUP BY printer_id, ams_slot HAVING cnt>1"
        )
        for g in dup_groups:
            pid = g["printer_id"]
            slot = g["ams_slot"]
            rows = self.db.query(
                "SELECT id, updated_at, remaining_grams FROM spools "
                "WHERE archived=0 AND printer_id=? AND ams_slot=? "
                "ORDER BY datetime(updated_at) DESC",
                (pid, slot),
            )
            # оставляем первый, остальные архивируем если пустые или старые
            for old in rows[1:]:
                # если у старой катушки 0 грамм или нет tray_uuid — точно фантом
                self.db.execute(
                    "UPDATE spools SET archived=1, ams_slot='', tray_uuid='', location='shop', updated_at=? WHERE id=?",
                    (now_iso(), old["id"]),
                )
                archived += 1

        # 2. Фантомы с пустым материалом и нулевым остатком в AMS
        phantoms = self.db.query(
            "SELECT * FROM spools WHERE archived=0 AND ams_slot<>'' AND "
            "(COALESCE(material,'')='' OR remaining_grams<=0) AND "
            "(COALESCE(tray_uuid,'')='' OR verified=0)"
        )
        for ph in phantoms:
            # Если катушка никогда не проверялась и без цены — архивируем
            if num(ph.get("price")) == 0 and num(ph.get("verified")) == 0:
                self.db.execute(
                    "UPDATE spools SET archived=1, ams_slot='', tray_uuid='', location='shop', updated_at=? WHERE id=?",
                    (now_iso(), ph["id"]),
                )
                archived += 1
            else:
                # иначе просто отвязываем от AMS
                self.db.execute(
                    "UPDATE spools SET ams_slot='', tray_uuid='', location='shop', updated_at=? WHERE id=?",
                    (now_iso(), ph["id"]),
                )
                cleared += 1

        # 3. Катушки с location=ams но без слота — исправляем location
        no_slot_ams = self.db.query(
            "SELECT id FROM spools WHERE archived=0 AND location='ams' AND (ams_slot IS NULL OR ams_slot='')"
        )
        for r in no_slot_ams:
            self.db.execute(
                "UPDATE spools SET location='shop', updated_at=? WHERE id=?",
                (now_iso(), r["id"]),
            )
            cleared += 1

        # 4. Катушки с slot но location=shop — исправляем на ams
        slot_shop = self.db.query(
            "SELECT id FROM spools WHERE archived=0 AND ams_slot<>'' AND location='shop' AND remaining_grams>0"
        )
        for r in slot_shop:
            self.db.execute(
                "UPDATE spools SET location='ams', updated_at=? WHERE id=?",
                (now_iso(), r["id"]),
            )
            cleared += 1

        if archived or cleared:
            self.db.add_event(
                "spool",
                "Очистка фантомов AMS",
                f"архивировано {archived}, отвязано/исправлено {cleared}",
                "",
                {"archived": archived, "cleared": cleared},
            )
        return {"archived": archived, "cleared": cleared, "dup_groups": len(dup_groups), "phantoms": len(phantoms)}

    # ---------------------------------------------------------------- каталог
    def catalog(self) -> list[dict]:
        # Legacy catalog остаётся совместимым представлением канонической
        # nomenclature. Если миграция уже связала строку, отдаём свежие данные
        # canonical, чтобы два экрана не показывали разные нормативы.
        rows = self.db.query(
            "SELECT c.*, n.id AS canonical_id, n.updated_at AS canonical_updated_at"
            " FROM catalog c LEFT JOIN nomenclature n"
            " ON n.id=COALESCE(NULLIF(c.nom_id,''),"
            " (SELECT id FROM nomenclature WHERE legacy_catalog_id=c.id LIMIT 1))"
            " WHERE c.archived=0 ORDER BY c.name")
        for row in rows:
            nom_id = row.get("canonical_id") or ""
            if nom_id:
                nom = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,)) or {}
                row["nom_id"] = nom_id
                row["name"] = nom.get("name") or row.get("name")
                row["niche_id"] = nom.get("niche_id") or row.get("niche_id")
                row["material"] = nom.get("material") or row.get("material")
                row["grams"] = num(nom.get("grams"))
                row["hours"] = num(nom.get("hours"))
                row["fit_per_plate"] = max(1, int(num(nom.get("fit_per_plate"), 1)))
                row["file"] = nom.get("file") or row.get("file")
                row["notes"] = nom.get("note") or row.get("notes")
            economics = self.acc.order_economics({
                "price": row["price"], "grams": row["grams"], "hours": row["hours"], "qty": 1})
            row["economics"] = economics

        # 9.3.2: товары, созданные сразу во вкладке «Товары» (номенклатура),
        # не имели legacy-строки и потому не попадали в «Изделия», генераторы
        # ценников и наклеек — хотя это тот же канонический каталог.
        # Дополняем представление, ничего не дублируя.
        from .nomenclature import Nomenclature
        covered = {str(r.get("canonical_id") or r.get("nom_id") or "") for r in rows}
        for nom in Nomenclature(self.db).items(kind="product"):
            nom_id = str(nom.get("id") or "")
            if not nom_id or nom_id in covered:
                continue
            row = {
                "id": nom_id, "name": nom.get("name") or "",
                "niche_id": nom.get("niche_id") or "",
                "grams": num(nom.get("grams")), "hours": num(nom.get("hours")),
                "fit_per_plate": max(1, int(num(nom.get("fit_per_plate"), 1))),
                "price": num(nom.get("price")),
                "material": nom.get("material") or "",
                "file": nom.get("file") or "",
                "notes": nom.get("note") or "",
                "archived": 0, "nom_id": nom_id,
                "created_at": nom.get("created_at"),
                "updated_at": nom.get("updated_at"),
            }
            row["economics"] = self.acc.order_economics({
                "price": row["price"], "grams": row["grams"],
                "hours": row["hours"], "qty": 1})
            rows.append(row)
        rows.sort(key=lambda r: str(r.get("name") or "").lower())
        return rows

    def save_catalog_item(self, data: dict) -> dict:
        """Сохранить legacy-запись и её каноническую nomenclature-позицию.

        Старый экран остаётся рабочим для импорта/совместимости, но не создаёт
        вторую сущность: у каждой строки есть ``nom_id``. Режимы:
        ``sync`` (по умолчанию) зеркалит введённые legacy-поля в canonical;
        ``canonical`` только обновляет legacy-зеркало из уже изменённой
        nomenclature-карточки.
        """
        data = dict(data)
        mode = str(data.pop("sync_mode", "sync") or "sync").strip().lower()
        expected_updated_at = str(data.pop("expected_updated_at", "") or "").strip()
        item_id = str(data.get("id") or uid("cat"))
        legacy = self.db.one("SELECT * FROM catalog WHERE id=?", (item_id,))
        if legacy and expected_updated_at and expected_updated_at != str(legacy.get("updated_at") or ""):
            raise ValueError("База изделий уже изменена — обновите карточку перед сохранением")
        # 9.3.2: позиция могла прийти из представления номенклатуры (вкладка
        # «Товары») — её id и есть id канонической карточки, иначе правка
        # из «Изделий» плодила бы дубликат вместо обновления.
        nom = self.db.one(
            "SELECT * FROM nomenclature WHERE id=? OR legacy_catalog_id=? LIMIT 1",
            ((legacy or {}).get("nom_id") or item_id, item_id),
        )
        nom_id = (legacy or {}).get("nom_id") or (nom or {}).get("id") or uid("nom")
        now = now_iso()
        if mode == "canonical" and nom:
            data.update({
                "name": nom.get("name") or data.get("name") or "",
                "niche_id": nom.get("niche_id") or data.get("niche_id") or "",
                "material": nom.get("material") or data.get("material") or "PLA",
                "grams": num(nom.get("grams")), "hours": num(nom.get("hours")),
                "fit_per_plate": max(1, int(num(nom.get("fit_per_plate"), 1))),
                "file": nom.get("file") or data.get("file") or "",
                "notes": nom.get("note") or data.get("notes") or "",
            })
        else:
            if not str(data.get("name") or "").strip():
                raise ValueError("Укажите название позиции")
            nom_payload = {
                "id": nom_id, "name": data.get("name"), "niche_id": data.get("niche_id") or "",
                "kind": "product", "unit": "шт", "material": data.get("material") or "PLA",
                "grams": num(data.get("grams")), "hours": num(data.get("hours")),
                "fit_per_plate": max(1, int(num(data.get("fit_per_plate"), 1))),
                "file": data.get("file") or "", "note": data.get("notes") or "",
                "legacy_catalog_id": item_id,
                "created_at": (nom or {}).get("created_at") or now, "updated_at": now,
            }
            self.db.upsert("nomenclature", nom_payload)
        data["id"] = item_id
        data["nom_id"] = nom_id
        data["created_at"] = (legacy or {}).get("created_at") or now
        data["updated_at"] = now
        data.setdefault("archived", 0)
        # У канонической карточки цена хранится в таблице prices; legacy
        # `price` остаётся зеркалом для старых расчётов.
        saved = self.db.upsert("catalog", data)
        if mode != "canonical" and num(data.get("price")) > 0:
            self.db.upsert("prices", {
                "id": uid("prc"), "at": now, "nom_id": nom_id,
                "price_type_id": "retail", "price": round(num(data["price"]), 2),
                "note": "синхронизация legacy catalog"})
        self.db.add_event("catalog", "Позиция изделий синхронизирована",
                          str(saved.get("name") or ""), data={"catalog_id": item_id, "nom_id": nom_id})
        return saved

    def delete_catalog_item(self, item_id: str) -> None:
        if not item_id:
            raise ValueError("Не указана позиция")
        row = self.db.one("SELECT * FROM catalog WHERE id=?", (item_id,))
        if not row:
            # 9.3.2: позиция может существовать только в номенклатуре
            # (создана во вкладке «Товары») — архивируем каноническую
            # карточку, история цен и партий сохраняется.
            nom = self.db.one("SELECT id FROM nomenclature WHERE id=?", (item_id,))
            if not nom:
                raise ValueError("Позиция не найдена")
            self.db.execute(
                "UPDATE nomenclature SET archived=1, updated_at=? WHERE id=?",
                (now_iso(), item_id))
            return
        now = now_iso()
        # Не удаляем canonical-источник: архивирование сохраняет историю и
        # не оставляет двойную запись при следующем импорте.
        nom_id = row.get("nom_id") or ""
        if nom_id:
            self.db.execute("UPDATE nomenclature SET archived=1, updated_at=? WHERE id=?", (now, nom_id))
        self.db.execute("UPDATE catalog SET archived=1, updated_at=? WHERE id=?", (now, item_id))

    # ------------------------------------------------------------------ касса
    def transactions(self, limit: int = 200) -> list[dict]:
        return self.db.query(
            "SELECT * FROM transactions ORDER BY datetime(at) DESC LIMIT ?", (int(limit),))

    def delete_transaction(self, tx_id: str) -> None:
        if not tx_id:
            raise ValueError("Не указана проводка")
        payment = self.db.one("SELECT id FROM payments WHERE tx_id=?", (tx_id,))
        if payment:
            self.delete_payment(payment["id"])
        else:
            self.db.delete("transactions", tx_id)

    def save_transaction_fields(self, data: dict) -> dict:
        """Правка проводки вручную (сумма, статья, касса, налоговые флаги)."""
        data = dict(data)
        if not data.get("id"):
            raise ValueError("Не указана проводка")
        if self.db.one("SELECT id FROM payments WHERE tx_id=?", (data["id"],)):
            raise ValueError("Эта проводка создана платежом; удалите платёж и запишите его заново")
        data.pop("auto", None)
        if "amount" in data:
            amount = abs(float(data.get("amount") or 0))
            if amount <= 0:
                raise ValueError("Сумма проводки должна быть больше нуля")
            data["amount"] = round(amount, 2)
        if data.get("at"):
            data["period"] = str(data["at"])[:7]
        if "category" in data and not str(data.get("category") or "").strip():
            cur = self.db.one("SELECT kind FROM transactions WHERE id=?", (data["id"],)) or {}
            kind = str(data.get("kind") or cur.get("kind") or "expense")
            data["category"] = "sale" if kind == "income" else "other"
        return self.db.upsert("transactions", data)

    # ------------------------------------------------------- кассы и счета
    def accounts(self) -> list[dict]:
        return self.db.query("SELECT * FROM accounts ORDER BY archived, position, name")

    def save_account(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("name"):
            raise ValueError("Нужно название кассы")
        if not data.get("id"):
            data["id"] = uid("acc")
        return self.db.upsert("accounts", data)

    def delete_account(self, account_id: str) -> None:
        if not account_id:
            raise ValueError("Не указана касса")
        used = self.db.one("SELECT COUNT(*) AS n FROM transactions WHERE account_id=?",
                           (account_id,)) or {}
        if int(used.get("n") or 0):
            # касса с историей не удаляется, а уходит в архив
            self.db.execute("UPDATE accounts SET archived=1 WHERE id=?", (account_id,))
            return
        self.db.delete("accounts", account_id)

    # ------------------------------------------------------- каналы продаж
    def channels(self) -> list[dict]:
        return self.db.query("SELECT * FROM channels ORDER BY position, name")

    def save_channel(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("name"):
            raise ValueError("Нужно название канала")
        if not data.get("id"):
            data["id"] = uid("ch")
        return self.db.upsert("channels", data)

    def delete_channel(self, channel_id: str) -> None:
        if not channel_id:
            raise ValueError("Не указан канал продаж")
        self.db.delete("channels", channel_id)

    # -------------------------------------------------- статьи и постоянные расходы
    def expense_categories(self) -> list[dict]:
        return self.db.query("SELECT * FROM expense_categories ORDER BY position, name")

    def save_expense_category(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("name"):
            raise ValueError("Нужно название статьи")
        if not data.get("id"):
            data["id"] = uid("cat")
        return self.db.upsert("expense_categories", data)

    def delete_expense_category(self, cat_id: str) -> None:
        if not cat_id:
            raise ValueError("Не указана статья расходов")
        self.db.delete("expense_categories", cat_id)

    def fixed_costs(self) -> list[dict]:
        return self.db.query("SELECT * FROM fixed_costs ORDER BY active DESC, name")

    def save_fixed_cost(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("name"):
            raise ValueError("Нужно название расхода")
        if not data.get("id"):
            data["id"] = uid("fix")
        if not data.get("started_at"):
            data["started_at"] = now_iso()[:10]
        return self.db.upsert("fixed_costs", data)

    def delete_fixed_cost(self, cost_id: str) -> None:
        if not cost_id:
            raise ValueError("Не указан постоянный расход")
        self.db.delete("fixed_costs", cost_id)

    def payments(self, order_id: str = "", limit: int = 200) -> list[dict]:
        if order_id:
            return self.db.query(
                "SELECT * FROM payments WHERE order_id=? ORDER BY datetime(at) DESC",
                (order_id,))
        return self.db.query(
            "SELECT * FROM payments ORDER BY datetime(at) DESC LIMIT ?", (int(limit),))

    def delete_payment(self, payment_id: str) -> None:
        if not payment_id:
            raise ValueError("Не указан платёж")
        row = self.db.one("SELECT * FROM payments WHERE id=?", (payment_id,))
        if not row:
            raise ValueError("Платёж не найден")
        sign = -1 if row["kind"] == "refund" else 1
        with self.db.transaction():
            if row.get("order_id"):
                self.db.execute(
                    "UPDATE orders SET paid=MAX(0,COALESCE(paid,0)-?), updated_at=? WHERE id=?",
                    (sign * float(row["amount"] or 0), now_iso(), row["order_id"]))
            self.db.delete("payments", payment_id)
            if row.get("tx_id"):
                self.db.delete("transactions", row["tx_id"])

    # --------------------------------------------------------------- принтеры
    def printers(self, include_secrets: bool = False) -> list[dict]:
        rows = self.db.query("SELECT * FROM printers ORDER BY position, name")
        from .crypto import decrypt, is_encrypted
        for row in rows:
            if include_secrets and is_encrypted(row.get("access_code") or ""):
                row["access_code"] = decrypt(row["access_code"])
            if not include_secrets:
                row["has_access_code"] = bool(row.get("access_code"))
                row["access_code"] = ""
        return rows

    def save_printer(self, data: dict) -> dict:
        data = dict(data)
        new = not data.get("id")
        if not data.get("id"):
            data["id"] = uid("prn")
        if new:
            data.setdefault("created_at", now_iso())
            data.setdefault("name", "Принтер")
        # Режим связи: новые принтеры — облако (без LAN Only Mode),
        # у существующих режим сохраняется, если не передан явно.
        if not data.get("mode"):
            if new:
                data["mode"] = "cloud"
            else:
                existing = self.db.one("SELECT mode FROM printers WHERE id=?",
                                       (data["id"],))
                data["mode"] = (existing or {}).get("mode") or "lan"
        data["updated_at"] = now_iso()
        if data.get("access_code") in ("", None, "••••••••"):
            data.pop("access_code", None)  # пустое поле не стирает сохранённый код
        elif data.get("access_code") and self.db.setting("encrypt_access_code", False):
            # Access Code хранится зашифрованным (роадмап 10.10) — опционально,
            # чтобы не менять поведение существующих LAN-установок без запроса.
            from .crypto import encrypt
            data["access_code"] = encrypt(str(data["access_code"]))
        if data.get("serial") == "••••••••":
            data.pop("serial", None)
        return self.db.upsert("printers", data)

    def delete_printer(self, printer_id: str) -> None:
        if not printer_id:
            raise ValueError("Не указан принтер")
        self.db.delete("printers", printer_id)

    def reset_settings(self, keys: list[str] | None = None) -> dict:
        """Сброс настроек к заводским: всех или только указанной группы."""
        if keys:
            values = {k: DEFAULT_SETTINGS[k] for k in keys if k in DEFAULT_SETTINGS}
        else:
            values = {k: v for k, v in DEFAULT_SETTINGS.items()
                      if k not in ("telegram_token", "telegram_chat")}
        self.db.set_settings(values)
        self.db.add_event("settings", "Настройки сброшены к заводским",
                          ", ".join(sorted(values)[:12]))
        return self.db.settings()

    # --------------------------------------------------------- бэкап и импорт
    def export_all(self) -> dict:
        tables = ["settings", "statuses", "niches", "customers", "orders", "order_items",
                  "spools", "print_jobs", "transactions", "filament_usage", "catalog",
                  "printer_stats", "accounts", "channels", "expense_categories",
                  "fixed_costs", "payments", "tax_periods",
                  "workshop_docs", "ams_slot_history", "filament_scrap",
                  "suppliers", "plate_presets", "shift_checks"]
        data: dict[str, Any] = {"format": "printflow-backup", "version": 2, "exported_at": now_iso()}
        for table in tables:
            data[table] = self.db.query(f"SELECT * FROM {table}")
        # принтеры выгружаются без секретов
        printers = self.db.query("SELECT * FROM printers")
        for p in printers:
            p["access_code"] = ""
            p["serial"] = ""
        data["printers"] = printers
        data["settings"] = self.db.settings()  # уже с маскировкой секретов
        return data

    def import_backup(self, payload: dict) -> dict:
        """Импорт нового бэкапа или старых данных localStorage."""
        if not isinstance(payload, dict):
            raise ValueError("Ожидается объект JSON")
        if payload.get("format") == "printflow-backup" or "orders" in payload and isinstance(
                payload.get("orders"), list) and payload.get("version"):
            return self._import_native(payload)
        return self.import_local_storage(payload)

    def _import_native(self, payload: dict) -> dict:
        stats: dict[str, int] = {}
        for table in ("statuses", "niches", "customers", "orders", "order_items",
                      "spools", "catalog", "accounts", "channels",
                      "expense_categories", "fixed_costs", "transactions",
                      "payments", "print_jobs", "tax_periods",
                      "workshop_docs", "ams_slot_history", "filament_scrap",
                      "suppliers", "plate_presets", "shift_checks"):
            rows = payload.get(table)
            if not isinstance(rows, list):
                continue
            count = 0
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    try:
                        self.db.upsert(table, row)
                        count += 1
                    except Exception:
                        continue
            stats[table] = count
        if isinstance(payload.get("settings"), dict):
            self.db.set_settings(payload["settings"])
            stats["settings"] = 1
        self.db.add_event("import", "Импорт резервной копии", json.dumps(stats, ensure_ascii=False))
        return stats

    def import_local_storage(self, data: dict) -> dict:
        """Перенос данных из браузерной версии (ключи ops_*, catalog1, spool1, shelf3)."""
        def parse(key: str, default):
            raw = data.get(key, default)
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return default
            return raw if raw is not None else default

        stats = {"statuses": 0, "niches": 0, "customers": 0, "orders": 0, "spools": 0, "catalog": 0}

        for index, st in enumerate(parse("ops_statuses1", []) or []):
            if not isinstance(st, dict) or not st.get("id"):
                continue
            self.db.upsert("statuses", {
                "id": str(st["id"]), "name": st.get("name") or st["id"],
                "color": st.get("color", "#64748b"),
                "position": int(num(st.get("pos"), index)),
                "is_final": 1 if st.get("final") or st.get("is_final") else 0})
            stats["statuses"] += 1

        for index, n in enumerate(parse("ops_niches1", []) or []):
            if not isinstance(n, dict) or not n.get("id"):
                continue
            self.db.upsert("niches", {
                "id": str(n["id"]), "name": n.get("name") or n["id"],
                "icon": n.get("icon", "◆"), "color": n.get("color", "#2563eb"),
                "hypothesis": n.get("hyp") or n.get("hypothesis", ""),
                "target": n.get("goal") or n.get("target", ""),
                "views": int(num(n.get("views"))), "leads": int(num(n.get("leads"))),
                "active": 0 if n.get("active") is False else 1, "position": index})
            stats["niches"] += 1

        for c in parse("ops_customers1", []) or []:
            if not isinstance(c, dict):
                continue
            self.db.upsert("customers", {
                "id": str(c.get("id") or uid("cus")), "name": c.get("name", ""),
                "phone": c.get("phone", ""), "messenger": c.get("tg") or c.get("messenger", ""),
                "company": c.get("company", ""), "notes": c.get("note") or c.get("notes", ""),
                "created_at": c.get("created") or now_iso()})
            stats["customers"] += 1

        for o in parse("ops_orders1", []) or []:
            if not isinstance(o, dict):
                continue
            self.db.upsert("orders", {
                "id": str(o.get("id") or uid("ord")),
                "number": str(o.get("num") or o.get("number") or ""),
                "product": o.get("item") or o.get("product", ""),
                "customer_name": o.get("client") or o.get("customer_name", ""),
                "phone": o.get("phone", ""),
                "messenger": o.get("tg") or o.get("messenger", ""),
                "channel": o.get("src") or o.get("channel", ""),
                "niche_id": o.get("niche") or o.get("niche_id"),
                "status": o.get("st") or o.get("status") or "new",
                "priority": o.get("priority", "normal"),
                "qty": num(o.get("qty"), 1) or 1,
                "material": o.get("mat") or o.get("material", ""),
                "color": o.get("color", ""),
                "grams": num(o.get("g") or o.get("grams")),
                "hours": num(o.get("h") or o.get("hours")),
                "price": num(o.get("price")),
                "cost": num(o.get("cost")),
                "prepaid": num(o.get("pre") or o.get("prepaid")),
                "notes": o.get("note") or o.get("notes", ""),
                "due": o.get("due", ""),
                "created_at": o.get("created") or o.get("date") or now_iso(),
                "updated_at": now_iso()})
            stats["orders"] += 1

        spool = parse("spool1", None)
        if isinstance(spool, dict) and (spool.get("left") or spool.get("remaining")):
            self.db.upsert("spools", {
                "id": "spool-legacy", "material": spool.get("mat", "PLA"),
                "brand": spool.get("brand", ""), "color_name": spool.get("color", "Импорт"),
                "total_grams": num(spool.get("total"), 1000) or 1000,
                "remaining_grams": num(spool.get("left") or spool.get("remaining"), 1000),
                "price": num(spool.get("price"), 1600),
                "created_at": now_iso(), "updated_at": now_iso()})
            stats["spools"] += 1
        elif isinstance(spool, list):
            # старый формат склада: [{c: цвет, t: тип, g: остаток, pr: цена}]
            for index, row in enumerate(spool):
                if not isinstance(row, dict):
                    continue
                grams = num(row.get("g") or row.get("remaining_grams"))
                self.db.upsert("spools", {
                    "id": str(row.get("id") or f"spool-legacy-{index}"),
                    "material": row.get("t") or row.get("material") or "PLA",
                    "brand": row.get("brand", ""),
                    "color_name": row.get("c") or row.get("color_name") or "Импорт",
                    "total_grams": max(grams, num(row.get("total_grams"), 1000) or 1000),
                    "remaining_grams": grams,
                    "price": num(row.get("pr") or row.get("price"), 1600) or 1600,
                    "created_at": now_iso(), "updated_at": now_iso()})
                stats["spools"] += 1

        for item in parse("catalog1", []) or []:
            if not isinstance(item, dict):
                continue
            # старый каталог: {c: категория, n: название, w: вес, h: часы,
            # p: цена, a: отход AMS, f: штук на стол}
            name = item.get("name") or item.get("title") or item.get("n") or ""
            if not name:
                continue
            self.db.upsert("catalog", {
                "id": str(item.get("id") or uid("cat")),
                "name": name,
                "niche_id": item.get("niche"),
                "grams": num(item.get("g") or item.get("grams") or item.get("w")),
                "hours": num(item.get("h") or item.get("hours")),
                "fit_per_plate": int(num(item.get("fit") or item.get("f"), 1) or 1),
                "price": num(item.get("price") or item.get("p")),
                "material": item.get("mat") or item.get("material", "PLA"),
                "notes": (item.get("note") or item.get("c") or ""),
                "created_at": now_iso()})
            stats["catalog"] += 1

        self.db.add_event("import", "Импорт данных из браузера",
                          json.dumps(stats, ensure_ascii=False))
        return stats

    # -------------------------------------------------------------- таймлайн
    def timeline(self, day: str = "") -> list[dict]:
        """Задания печати за день (для таймлайна на панели)."""
        day = day or now_iso()[:10]
        rows = self.db.query(
            "SELECT * FROM print_jobs WHERE"
            " substr(COALESCE(started_at, queued_at, created_at),1,10)=?"
            " OR substr(COALESCE(finished_at,''),1,10)=?"
            " ORDER BY datetime(COALESCE(started_at, queued_at, created_at))",
            (day, day))
        for row in rows:
            if row.get("order_id"):
                order = self.db.one("SELECT number, product FROM orders WHERE id=?",
                                    (row["order_id"],))
                if order:
                    row["order"] = order
        return rows

    # ------------------------------------------------------------------ поиск
    def search(self, text: str, limit: int = 20) -> list[dict]:
        """Глобальный поиск по заказам, клиентам, катушкам и принтерам."""
        text = (text or "").strip().lower()
        if not text:
            return []
        like = f"%{text}%"
        results: list[dict] = []
        for row in self.db.query(
                "SELECT id, number, product, customer_name, status FROM orders"
                " WHERE pylower(number) LIKE ? OR pylower(product) LIKE ?"
                " OR pylower(customer_name) LIKE ? LIMIT ?", (like, like, like, limit)):
            results.append({"type": "order", "id": row["id"],
                            "title": f"№{row['number']} · {row['product']}",
                            "subtitle": row["customer_name"] or "", "status": row["status"]})
        for row in self.db.query(
                "SELECT id, name, phone FROM customers"
                " WHERE pylower(name) LIKE ? OR phone LIKE ? LIMIT ?",
                (like, like, limit)):
            results.append({"type": "customer", "id": row["id"], "title": row["name"],
                            "subtitle": row["phone"] or ""})
        for row in self.db.query(
                "SELECT id, material, color_name, remaining_grams FROM spools"
                " WHERE pylower(material) LIKE ? OR pylower(color_name) LIKE ? LIMIT ?",
                (like, like, limit)):
            results.append({"type": "spool", "id": row["id"],
                            "title": f"{row['material']} {row['color_name']}",
                            "subtitle": f"{round(num(row['remaining_grams']))} г"})
        for row in self.db.query(
                "SELECT id, name, model, host FROM printers"
                " WHERE pylower(name) LIKE ? OR pylower(model) LIKE ? OR host LIKE ? LIMIT ?",
                (like, like, like, limit)):
            results.append({"type": "printer", "id": row["id"], "title": row["name"],
                            "subtitle": f"{row['model'] or ''} {row['host'] or ''}".strip()})
        # 13.1 (12): товары номенклатуры и документы — единый поиск по панели
        for row in self.db.query(
                "SELECT id, name, code, sku, unit, archived FROM nomenclature"
                " WHERE archived=0 AND (pylower(name) LIKE ? OR pylower(code) LIKE ?"
                " OR pylower(sku) LIKE ?) LIMIT ?",
                (like, like, like, limit)):
            results.append({"type": "product", "id": row["id"],
                            "title": row["name"] or row["code"] or "",
                            "subtitle": " · ".join(x for x in
                                                    [row["code"], row["sku"], row["unit"] or "шт"] if x)})
        for row in self.db.query(
                "SELECT id, number, kind, note FROM documents"
                " WHERE pylower(number) LIKE ? OR pylower(note) LIKE ? LIMIT ?",
                (like, like, limit)):
            results.append({"type": "document", "id": row["id"],
                            "title": f"Документ {row['number'] or row['id']} · {row['kind']}",
                            "subtitle": (row["note"] or "")[:60]})
        return results[:limit]
