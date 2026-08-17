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
    "colors qc_done"
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
        for row in rows:
            row["economics"] = self.acc.order_economics(row)
        return rows

    def order(self, order_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not row:
            return None
        row["economics"] = self.acc.order_economics(row)
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

    def save_order(self, data: dict) -> dict:
        data = dict(data)
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

        row = self.db.upsert("orders", payload)
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
            self.acc.register_order_income(row)
        elif row.get("closed_at"):
            self.db.execute("UPDATE orders SET closed_at='' WHERE id=?", (order_id,))
        if not existing:
            self.db.add_event("order", "Новый заказ",
                              f"№{row.get('number','')} · {row.get('product','')}",
                              data=({"order_id": order_id}))
        return self.order(order_id) or row

    def set_order_status(self, order_id: str, status: str) -> dict:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        return self.save_order({"id": order_id, "status": status})

    def delete_order(self, order_id: str) -> None:
        if not order_id:
            raise ValueError("Не указан заказ")
        # История движения денег должна пережить удаление карточки заказа:
        # платежи и их проводки остаются связанными через tx_id, но отвязываются от заказа.
        with self.db.transaction():
            self.db.execute("UPDATE print_jobs SET order_id=NULL WHERE order_id=?", (order_id,))
            self.db.execute("UPDATE payments SET order_id=NULL WHERE order_id=?", (order_id,))
            self.db.execute("UPDATE transactions SET order_id=NULL WHERE order_id=?", (order_id,))
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
    def spools(self, include_archived: bool = False) -> list[dict]:
        sql = "SELECT * FROM spools"
        if not include_archived:
            sql += " WHERE archived=0"
        sql += " ORDER BY material, color_name"
        rows = self.db.query(sql)
        for row in rows:
            total = max(1.0, num(row["total_grams"], 1000))
            row["percent"] = round(num(row["remaining_grams"]) / total * 100, 1)
            row["value"] = round(num(row["remaining_grams"]) / total * num(row["price"]), 2)
            usage = self.db.one(
                "SELECT COALESCE(SUM(grams),0) g FROM filament_usage WHERE spool_id=?", (row["id"],))
            row["used_grams"] = round(num((usage or {}).get("g")), 1)
        return rows

    def save_spool(self, data: dict) -> dict:
        data = dict(data)
        new = not data.get("id")
        if not data.get("id"):
            data["id"] = uid("sp")
        if new:
            data.setdefault("created_at", now_iso())
            data.setdefault("remaining_grams", data.get("total_grams", 1000))
        data["updated_at"] = now_iso()
        return self.db.upsert("spools", data)

    def delete_spool(self, spool_id: str) -> None:
        if not spool_id:
            raise ValueError("Не указана катушка")
        self.db.delete("spools", spool_id)

    # ---------------------------------------------------------------- каталог
    def catalog(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM catalog WHERE archived=0 ORDER BY name")
        for row in rows:
            economics = self.acc.order_economics({
                "price": row["price"], "grams": row["grams"], "hours": row["hours"], "qty": 1})
            row["economics"] = economics
        return rows

    def save_catalog_item(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("id"):
            data["id"] = uid("cat")
        data.setdefault("created_at", now_iso())
        return self.db.upsert("catalog", data)

    def delete_catalog_item(self, item_id: str) -> None:
        if not item_id:
            raise ValueError("Не указана позиция")
        self.db.delete("catalog", item_id)

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
        if not include_secrets:
            for row in rows:
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
        data["updated_at"] = now_iso()
        if data.get("access_code") in ("", None, "••••••••"):
            data.pop("access_code", None)  # пустое поле не стирает сохранённый код
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
        tables = ["settings", "statuses", "niches", "customers", "orders", "spools",
                  "print_jobs", "transactions", "filament_usage", "catalog", "printer_stats",
                  "accounts", "channels", "expense_categories", "fixed_costs", "payments",
                  "tax_periods"]
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
        for table in ("statuses", "niches", "customers", "orders", "spools",
                      "catalog", "accounts", "channels", "expense_categories",
                      "fixed_costs", "transactions", "payments", "print_jobs",
                      "tax_periods"):
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
        return results[:limit]
