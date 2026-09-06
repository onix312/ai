"""Мобильная касса в LAN (Касса 16.0, итерация 2).

Кассир на телефоне/планшете в локальной сети магазина продаёт с полки за
наличные или по СБП. Деньги не дублируются и не расходятся с учётом:

* наличные — продажа сразу через ``shelf.sale`` (канал in_shop, как в панели
  и боте — один учёт);
* СБП — платёж создаётся со статусом new/pending, склад НЕ списывается и
  выручка НЕ пишется до подтверждения; при подтверждении кассиром в одной
  транзакции списывается склад и записывается доход на счёт СБП;
* вход — общий код магазина (настройка ``cashier_code``), сессия в памяти;
* возвраты СБП кассиру недоступны — только руководитель/владелец в панели.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso
from .sbp import Sbp, STATUS_PENDING
from .shelf import Shelf

SESSION_TTL = 12 * 3600  # смена 12 часов, затем код вводится заново
METHODS = ("cash", "sbp")


class Cashier:
    def __init__(self, db, acc: Accounting, shelf: Shelf | None = None,
                 sbp: Sbp | None = None):
        self.db = db
        self.acc = acc
        self.shelf = shelf or Shelf(db)
        self.sbp = sbp or Sbp(db, acc)
        self._sessions: dict[str, dict] = {}  # token -> {at, role}

    # ------------------------------------------------------------- сессии
    def _gc(self) -> None:
        now = time.time()
        stale = [t for t, s in self._sessions.items()
                 if now - float(s.get("ts", now)) > SESSION_TTL]
        for t in stale:
            self._sessions.pop(t, None)

    def login(self, code: str) -> dict:
        expected = str(self.db.setting("cashier_code", "") or "").strip()
        if not expected:
            raise ValueError("Код кассы не задан — настройте его в панели (Касса и СБП)")
        if str(code or "").strip() != expected:
            raise ValueError("Неверный код кассы")
        self._gc()
        token = uid("ck")
        self._sessions[token] = {"ts": time.time(), "role": "employee"}
        return {"token": token, "role": "employee", "expires_in": SESSION_TTL}

    def logout(self, token: str) -> dict:
        self._sessions.pop(str(token or "").strip(), None)
        return {"ok": True}

    def require(self, token: str) -> dict:
        self._gc()
        session = self._sessions.get(str(token or "").strip())
        if not session:
            raise ValueError("Сессия кассы истекла — введите код снова")
        return session

    # ------------------------------------------------------------- каталог
    def catalog(self) -> dict[str, Any]:
        items = []
        for it in self.shelf.items():
            if not it.get("active"):
                continue
            items.append({
                "id": it["id"], "name": it.get("name") or "", "price": num(it.get("price")),
                "qty": num(it.get("qty")), "status": it.get("status") or "ok",
                "photo": bool(it.get("photo")), "barcode": it.get("barcode") or "",
            })
        return {
            "items": items,
            "sbp_enabled": self.sbp.enabled(),
            "shop_cash": self.shelf.shop_cash(),
        }

    # ------------------------------------------------------------- продажа
    def sell(self, items: list, method: str, token: str, *,
             request_id: str = "", cashier_name: str = "") -> dict:
        """Продажа корзины. Наличные — сразу в журнал; СБП — платёж до сверки.

        Идемпотентно по ``request_id`` (двойное нажатие не создаёт две продажи).
        """
        self.require(token)
        method = str(method or "cash").strip().lower()
        if method not in METHODS:
            raise ValueError("Способ оплаты: наличные (cash) или СБП (sbp)")
        payload: list[dict] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            item_id = str(it.get("item_id") or "").strip()
            qty = num(it.get("qty"))
            if not item_id or qty <= 0:
                continue
            payload.append({"item_id": item_id, "qty": round(qty, 2)})
        if not payload:
            raise ValueError("Корзина пуста")
        cashier = str(cashier_name or "кассир")[:120]
        request_id = str(request_id or "").strip()[:120]

        with self.db.transaction():
            if request_id:
                existing = self.db.one(
                    "SELECT * FROM cashier_sales WHERE request_id=?", (request_id,))
                if existing:
                    return self._sale_result(existing, already_recorded=True)
            # валидация остатков и цены до любых изменений
            rows = []
            total = 0.0
            for entry in payload:
                item = self.db.one(
                    "SELECT * FROM shelf_items WHERE id=? AND active=1", (entry["item_id"],))
                if not item:
                    raise ValueError("Позиция не найдена")
                left = num(item["qty"])
                if left < entry["qty"]:
                    raise ValueError(
                        f"«{item.get('name') or entry['item_id']}» осталось {left:g} — "
                        f"продать {entry['qty']:g} нельзя")
                price = num(item["price"])
                rows.append({"item_id": entry["item_id"], "qty": entry["qty"],
                             "price": round(price, 2), "name": str(item.get("name") or "")})
                total += price * entry["qty"]
            total = round(total, 2)
            if total <= 0:
                raise ValueError("Сумма продажи должна быть больше нуля")
            sale_id = uid("cs")
            stamp = now_iso()
            if method == "cash":
                for row in rows:
                    self.shelf.sale(row["item_id"], row["qty"], row["price"],
                                    channel="shelf", note="Касса: наличные")
                self.db.execute(
                    "INSERT INTO cashier_sales"
                    "(id,payment_id,method,amount,items,cashier,request_id,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (sale_id, "", "cash", total, json.dumps(rows, ensure_ascii=False),
                     cashier, request_id, stamp))
                result = self.db.one("SELECT * FROM cashier_sales WHERE id=?", (sale_id,))
                payload_out = self._sale_result(result)
                payload_out["paid"] = True
            else:
                payment = self.sbp.create(
                    amount=total, order_id="", purpose=f"Продажа на кассе · {len(rows)} поз.",
                    note="Касса", request_id=f"cashier:{sale_id}" if not request_id else request_id,
                    actor=cashier, qr_kind="dynamic")
                self.db.execute(
                    "INSERT INTO cashier_sales"
                    "(id,payment_id,method,amount,items,cashier,request_id,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (sale_id, payment["id"], "sbp", total,
                     json.dumps(rows, ensure_ascii=False), cashier, request_id, stamp))
                result = self.db.one("SELECT * FROM cashier_sales WHERE id=?", (sale_id,))
                payload_out = self._sale_result(result)
                payload_out["paid"] = False
                payload_out["payment"] = payment
        self._audit(sale_id, "sell", "Продажа на кассе", f"{method} · {total:g} ₽", actor=cashier)
        self.db.add_event("shelf", "Продажа на кассе",
                          f"{method} · {total:g} ₽ · {len(rows)} поз.",
                          data={"sale_id": sale_id, "method": method, "cashier": cashier})
        return payload_out

    def _sale_result(self, sale: dict, already_recorded: bool = False) -> dict:
        try:
            items = json.loads(sale.get("items") or "[]")
        except json.JSONDecodeError:
            items = []
        return {
            "ok": True,
            "sale_id": sale["id"],
            "method": sale.get("method") or "cash",
            "amount": round(num(sale.get("amount")), 2),
            "items": items,
            "cashier": sale.get("cashier") or "",
            "payment_id": sale.get("payment_id") or "",
            "confirmed": bool(str(sale.get("confirmed_at") or "")),
            "already_recorded": already_recorded,
        }

    # ------------------------------------------------- подтверждение СБП
    def incoming(self) -> dict:
        """СБП-продажи кассы, ожидающие сверки (только продажи, не заказы)."""
        rows = self.db.query(
            "SELECT s.*, p.number, p.status, p.purpose FROM cashier_sales s"
            " JOIN sbp_payments p ON p.id=s.payment_id"
            " WHERE s.method='sbp' AND COALESCE(s.confirmed_at,'')=''"
            " ORDER BY datetime(s.created_at) DESC LIMIT 50")
        out = []
        for row in rows:
            try:
                row["items"] = json.loads(row.get("items") or "[]")
            except json.JSONDecodeError:
                row["items"] = []
            out.append(row)
        return {"payments": out, "sbp_enabled": self.sbp.enabled()}

    def confirm_sbp(self, payment_id: str, token: str, *, note: str = "") -> dict:
        """Подтвердить СБП-продажу: списать склад и записать выручку.

        Одна транзакция: остатки проверяются заново (за время сверки полку мог
        продать другой кассир), списание и деньги проходят вместе. Повторный
        вызов не списывает склад и не пишет проводку повторно.
        """
        self.require(token)
        sale = self.db.one("SELECT * FROM cashier_sales WHERE payment_id=?", (payment_id,))
        if not sale:
            raise ValueError("Продажа не найдена")
        if str(sale.get("method") or "") != "sbp":
            raise ValueError("Это не СБП-продажа")
        if str(sale.get("confirmed_at") or ""):
            return {**self._sale_result(sale), "already_recorded": True}
        try:
            rows = json.loads(sale.get("items") or "[]")
        except json.JSONDecodeError:
            rows = []
        cashier = str(sale.get("cashier") or "кассир")[:120]
        with self.db.transaction():
            sale = self.db.one("SELECT * FROM cashier_sales WHERE payment_id=?", (payment_id,))
            if str(sale.get("confirmed_at") or ""):
                return {**self._sale_result(sale), "already_recorded": True}
            # 1) остатки проверяем до денег: не хватает — ничего не трогаем
            for row in rows:
                item = self.db.one("SELECT * FROM shelf_items WHERE id=?", (row["item_id"],))
                if not item:
                    raise ValueError(f"Позиция «{row.get('name') or row['item_id']}» не найдена")
                if num(item["qty"]) < num(row["qty"]):
                    raise ValueError(
                        f"«{item.get('name')}» осталось {num(item['qty']):g} — "
                        f"продать {num(row['qty']):g} нельзя")
            # 2) списываем склад без отдельной проводки (деньги — через СБП)
            for row in rows:
                self.shelf.sale(row["item_id"], row["qty"], row["price"],
                                channel="shelf", note="Касса: СБП", record_income=False)
            # 3) деньги на счёт СБП + статус платежа
            payment = self.sbp.confirm(payment_id, actor=cashier, note=note or "")
            self.db.execute(
                "UPDATE cashier_sales SET confirmed_at=? WHERE id=?", (now_iso(), sale["id"]))
        sale = self.db.one("SELECT * FROM cashier_sales WHERE payment_id=?", (payment_id,))
        result = self._sale_result(sale)
        result["payment"] = payment
        self._audit(sale["id"], "confirm_sbp", "СБП-продажа подтверждена",
                    f"{num(sale['amount']):g} ₽", actor=cashier)
        return result

    def reject_sbp(self, payment_id: str, token: str, *, reason: str = "") -> dict:
        """Отклонить СБП-продажу: деньги не трогаем, склад не списывали."""
        self.require(token)
        sale = self.db.one("SELECT * FROM cashier_sales WHERE payment_id=?", (payment_id,))
        if not sale:
            raise ValueError("Продажа не найдена")
        if str(sale.get("method") or "") != "sbp":
            raise ValueError("Это не СБП-продажа")
        cashier = str(sale.get("cashier") or "кассир")[:120]
        payment = self.sbp.reject(payment_id, reason=reason or "Оплата не поступила",
                                  actor=cashier)
        self._audit(sale["id"], "reject_sbp", "СБП-продажа отклонена",
                    f"{num(sale['amount']):g} ₽", actor=cashier)
        return {**self._sale_result(sale), "payment": payment}

    # ---------------------------------------------------------------- аудит
    def _audit(self, entity_id: str, action: str, title: str,
               detail: str = "", data: dict | None = None, actor: str = "panel") -> None:
        try:
            self.db.execute(
                "INSERT INTO audit_log(at,entity,entity_id,action,title,detail,data)"
                " VALUES(?,?,?,?,?,?,?)",
                (now_iso(), "cashier_sale", entity_id, action, title, detail,
                 json.dumps({"actor": actor or "panel", **(data or {})}, ensure_ascii=False)))
        except Exception:
            pass
