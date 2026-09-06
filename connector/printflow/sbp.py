"""СБП-ядро: платежи со статусами, ручное подтверждение, возврат, аудит.

Касса 16.0. Правило «деньги не ломаются»:

* платёж живёт отдельной сущностью ``sbp_payments`` со статусами
  ``new → pending → confirmed / rejected / refunded``. Выручка (проводка в
  журнал) и долг заказа меняются ТОЛЬКО при подтверждении;
* запись денег идёт только через ``accounting.add_payment`` /
  ``add_transaction`` и защищена ``request_id`` — двойное нажатие или
  обновление страницы не создаёт второй платёж и не пишет две проводки;
* отмена (reject) и возврат (refund) — явные операции с аудитом, без тихих
  правок. Возврат в v1 — ручная отметка «возврат выполнен в банке»;
* неподтверждённый платёж не закрывает долг и не разблокирует выдачу заказа.
"""
from __future__ import annotations

import json
from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso

# Статусы платежа и терминальные состояния.
STATUS_NEW = "new"
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUS_REFUNDED = "refunded"

STATUSES = (STATUS_NEW, STATUS_PENDING, STATUS_CONFIRMED, STATUS_REJECTED, STATUS_REFUNDED)
TERMINAL = (STATUS_CONFIRMED, STATUS_REJECTED, STATUS_REFUNDED)

METHOD_NAME = "СБП"
CHANNEL = "sbp"


class Sbp:
    """Сервис платежей по СБП: создание, экран входящих, подтверждение, возврат."""

    def __init__(self, db, acc: Accounting):
        self.db = db
        self.acc = acc

    # ------------------------------------------------------------- настройки
    def enabled(self) -> bool:
        return bool(self.db.setting("sbp_enabled", True))

    def account_id(self) -> str:
        """Счёт, на который пишем подтверждённый СБП-платёж (настраиваемый).

        По умолчанию — отдельный счёт «СБП». Владелец может перенаправить на
        любой существующий счёт через настройку ``sbp_account_id``.
        """
        acc = str(self.db.setting("sbp_account_id", "sbp") or "sbp").strip()
        row = self.db.one("SELECT id FROM accounts WHERE id=? AND archived=0", (acc,))
        return acc if row else ""

    def settings(self) -> dict[str, Any]:
        """Настройки СБП для панели и кассы."""
        acc = self.account_id()
        account = self.db.one("SELECT id,name,kind FROM accounts WHERE id=?", (acc,)) or {}
        return {
            "enabled": self.enabled(),
            "account_id": acc or "",
            "account_name": account.get("name") or "",
            "shop_qr": str(self.db.setting("sbp_shop_qr", "") or "").strip(),
            "bank_name": str(self.db.setting("sbp_bank_name", "") or "").strip(),
            "payment_note": str(self.db.setting("sbp_payment_note", "") or "").strip(),
            "accounts": [
                {"id": r["id"], "name": r["name"]}
                for r in self.db.query(
                    "SELECT id,name FROM accounts WHERE archived=0 ORDER BY position,name")
            ],
        }

    def purpose_text(self, number: str = "") -> str:
        """Назначение перевода для клиента: шаблон или стандартная строка."""
        template = str(self.db.setting("sbp_payment_note", "") or "").strip()
        if template:
            return template.replace("{number}", str(number or ""))
        if number:
            return f"Оплата заказа №{number}"
        return "Оплата заказа PrintFlow"

    # ------------------------------------------------------------- вспомогат.
    def _require_enabled(self) -> None:
        if not self.enabled():
            raise ValueError("СБП-оплата отключена в настройках")

    def _get(self, payment_id: str) -> dict:
        row = self.db.one("SELECT * FROM sbp_payments WHERE id=?", (payment_id,))
        if not row:
            raise ValueError("СБП-платёж не найден")
        return row

    def _audit(self, entity_id: str, action: str, title: str,
               detail: str = "", data: dict | None = None, actor: str = "panel") -> None:
        try:
            self.db.execute(
                "INSERT INTO audit_log(at,entity,entity_id,action,title,detail,data)"
                " VALUES(?,?,?,?,?,?,?)",
                (now_iso(), "sbp_payment", entity_id, action, title, detail,
                 json.dumps({"actor": actor or "panel", **(data or {})}, ensure_ascii=False)))
        except Exception:
            pass

    def _next_number(self) -> str:
        row = self.db.one("SELECT COALESCE(MAX(CAST(number AS INTEGER)),0) n"
                          " FROM sbp_payments WHERE number GLOB '[0-9]*'") or {}
        return str(int(num(row.get("n"))) + 1)

    # ------------------------------------------------------------- создание
    def create(self, *, amount: float, order_id: str = "", sale_id: str = "",
               chat_id: str = "", purpose: str = "", note: str = "",
               request_id: str = "", actor: str = "panel",
               qr_kind: str = "dynamic", qr_payload: str = "") -> dict:
        """Создать СБП-платёж (new). Деньги и долг на этом шаге НЕ меняются.

        Идемпотентно по ``request_id``: повтор с тем же ключом вернёт уже
        созданный платёж с ``already_recorded=True``.
        """
        self._require_enabled()
        amount = round(num(amount), 2)
        if amount <= 0:
            raise ValueError("Сумма платежа должна быть больше нуля")
        order = None
        if order_id:
            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if not order:
                raise ValueError("Заказ не найден")
            due = self.acc.order_economics(order).get("debt", 0.0)
            if due <= 0:
                raise ValueError("По заказу нет долга — платёж не нужен")
            if amount > round(due, 2) + 0.005:
                raise ValueError(f"Платёж больше долга заказа: осталось {due:g} ₽")
        if sale_id:
            sale = self.db.one("SELECT * FROM shelf_moves WHERE id=?", (sale_id,))
            if not sale:
                raise ValueError("Продажа не найдена")
        request_id = str(request_id or "").strip()[:120]
        with self.db.transaction():
            if request_id:
                existing = self.db.one(
                    "SELECT * FROM sbp_payments WHERE request_id=?", (request_id,))
                if existing:
                    if abs(num(existing.get("amount")) - amount) > 0.005 \
                            or str(existing.get("order_id") or "") != str(order_id or ""):
                        raise ValueError("Ключ запроса уже использован для другого платежа")
                    existing["already_recorded"] = True
                    return existing
            pid = uid("sbp")
            stamp = now_iso()
            number = self._next_number()
            qr_kind = str(qr_kind or "dynamic").strip().lower()
            if qr_kind not in ("static", "dynamic"):
                qr_kind = "dynamic"
            purpose = (str(purpose or "").strip()
                       or self.purpose_text(str(order.get("number") or "") if order else ""))
            self.db.execute(
                "INSERT INTO sbp_payments"
                "(id,number,order_id,sale_id,chat_id,amount,currency,purpose,status,"
                " request_id,account_id,qr_kind,qr_payload,note,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, number, order_id or "", sale_id or "", chat_id or "",
                 amount, "RUB", purpose, STATUS_NEW, request_id,
                 self.account_id(), qr_kind, str(qr_payload or "")[:2000],
                 str(note or "")[:1000], stamp, stamp))
        row = self._get(pid)
        self._audit(pid, "create", "СБП-платёж создан",
                    f"{amount:g} RUB · {purpose}", actor=actor)
        self.db.add_event("finance", "СБП-платёж создан",
                          f"{amount:g} RUB · {purpose}",
                          data={"payment_id": pid, "order_id": order_id or "",
                                "status": STATUS_NEW, "actor": actor})
        return row

    # --------------------------------------------------------------- список
    def list(self, status: str = "", order_id: str = "", limit: int = 100) -> list[dict]:
        """Платежи для экрана «Входящие» и журнала."""
        sql = "SELECT * FROM sbp_payments WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if order_id:
            sql += " AND order_id=?"
            params.append(order_id)
        sql += " ORDER BY datetime(created_at) DESC LIMIT ?"
        params.append(int(limit))
        rows = self.db.query(sql, params)
        for row in rows:
            if row.get("order_id"):
                o = self.db.one("SELECT number,product FROM orders WHERE id=?", (row["order_id"],))
                if o:
                    row["order_number"] = o.get("number") or ""
                    row["order_product"] = o.get("product") or ""
        return rows

    def state(self) -> dict[str, Any]:
        """Сводка для экрана «Входящие платежи»."""
        counts = {s: 0 for s in STATUSES}
        for row in self.db.query("SELECT status, COUNT(*) n FROM sbp_payments GROUP BY status"):
            counts[str(row["status"])] = int(row["n"])
        return {
            "enabled": self.enabled(),
            "counts": counts,
            "pending": self.list(status=STATUS_PENDING, limit=50),
            "recent": self.list(status="", limit=20),
            "settings": self.settings(),
        }

    # --------------------------------------------------------- подтверждение
    def confirm(self, payment_id: str, *, actor: str = "panel",
                account_id: str = "", note: str = "") -> dict:
        """Подтвердить платёж: записать деньги в журнал и закрыть долг заказа.

        Единственная точка, где СБП-платёж становится выручкой. Повторный вызов
        (двойное нажатие/обновление) не пишет вторую проводку: защита по статусу
        внутри транзакции и по ``request_id`` в ``add_payment``.
        """
        self._require_enabled()
        row = self._get(payment_id)
        if row["status"] == STATUS_CONFIRMED:
            return {**row, "already_recorded": True}
        if row["status"] in (STATUS_REJECTED, STATUS_REFUNDED):
            raise ValueError(f"Платёж уже {row['status']} — подтвердить нельзя")
        acc_id = str(account_id or "").strip() or self.account_id()
        if not acc_id:
            acc_id = str(self.db.setting("default_account", "cash") or "cash")
        acc = self.db.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
        if not acc:
            raise ValueError("Счёт для СБП не найден")
        with self.db.transaction():
            row = self._get(payment_id)
            if row["status"] == STATUS_CONFIRMED:
                return {**row, "already_recorded": True}
            if row["status"] in (STATUS_REJECTED, STATUS_REFUNDED):
                raise ValueError(f"Платёж уже {row['status']} — подтвердить нельзя")
            amount = num(row["amount"])
            note = str(note or "").strip() or "СБП-платёж подтверждён вручную"
            if row["order_id"]:
                # Долг заказа закрывается штатным путём: payments + проводка.
                payment = self.acc.add_payment(
                    row["order_id"], amount, "payment", acc_id, METHOD_NAME,
                    note, request_id=f"sbp:{payment_id}")
                tx_id = str(payment.get("tx_id") or "")
                pay_id = str(payment.get("id") or "")
            else:
                # Продажа без заказа (с полки): доход на счёт СБП.
                tx = self.acc.add_transaction(
                    "income", "sale", amount,
                    f"СБП: {row['purpose'] or 'продажа'}",
                    note=note, account_id=acc_id, channel=CHANNEL,
                    payer="person")
                tx_id = str(tx["id"])
                pay_id = ""
            stamp = now_iso()
            self.db.execute(
                "UPDATE sbp_payments SET status=?,account_id=?,confirmed_at=?,"
                "confirmed_by=?,payment_id=?,tx_id=?,updated_at=? WHERE id=?",
                (STATUS_CONFIRMED, acc_id, stamp, actor or "panel",
                 pay_id, tx_id, stamp, payment_id))
        row = self._get(payment_id)
        self._audit(payment_id, "confirm", "СБП-оплата подтверждена",
                    f"{num(row['amount']):g} RUB", {"account_id": acc_id, "tx_id": tx_id}, actor)
        self.db.add_event("finance", "СБП-оплата подтверждена",
                          f"{num(row['amount']):g} RUB · счёт {acc['name']}",
                          data={"payment_id": payment_id, "order_id": row["order_id"] or "",
                                "tx_id": tx_id, "actor": actor})
        return row

    # -------------------------------------------------------------- отмена
    def reject(self, payment_id: str, *, reason: str = "", actor: str = "panel") -> dict:
        """Отклонить неподтверждённый платёж. Денег не трогает (выручки не было)."""
        row = self._get(payment_id)
        if row["status"] in TERMINAL:
            raise ValueError(f"Платёж уже {row['status']} — отклонить нельзя")
        reason = str(reason or "").strip()[:500] or "Оплата не подтверждена"
        stamp = now_iso()
        with self.db.transaction():
            row = self._get(payment_id)
            if row["status"] in TERMINAL:
                raise ValueError(f"Платёж уже {row['status']} — отклонить нельзя")
            self.db.execute(
                "UPDATE sbp_payments SET status=?,rejected_at=?,rejected_by=?,"
                "reject_reason=?,updated_at=? WHERE id=?",
                (STATUS_REJECTED, stamp, actor or "panel", reason, stamp, payment_id))
        row = self._get(payment_id)
        self._audit(payment_id, "reject", "СБП-платёж отклонён", reason, actor=actor)
        self.db.add_event("finance", "СБП-платёж отклонён",
                          f"{num(row['amount']):g} RUB · {reason}",
                          data={"payment_id": payment_id, "actor": actor})
        return row

    # -------------------------------------------------------------- возврат
    def refund(self, payment_id: str, *, actor: str = "panel", note: str = "",
               bank_done: bool = False) -> dict:
        """Вернуть подтверждённый СБП-платёж (руководитель/владелец).

        В v1 возврат — ручная отметка: ``bank_done=True`` значит «возврат
        выполнен в банке». Пишет обратную проводку (refund) и снимает оплату
        с заказа. Защита от двойного возврата — по статусу внутри транзакции.
        """
        if not bank_done:
            raise ValueError("Подтвердите, что возврат выполнен в банке")
        row = self._get(payment_id)
        if row["status"] == STATUS_REFUNDED:
            return {**row, "already_recorded": True}
        if row["status"] != STATUS_CONFIRMED:
            raise ValueError("Возврат возможен только для подтверждённого платежа")
        acc_id = str(row["account_id"] or "").strip() or self.account_id()
        acc = self.db.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
        if not acc:
            raise ValueError("Счёт платежа не найден")
        note = str(note or "").strip() or "Возврат СБП"
        with self.db.transaction():
            row = self._get(payment_id)
            if row["status"] == STATUS_REFUNDED:
                return {**row, "already_recorded": True}
            if row["status"] != STATUS_CONFIRMED:
                raise ValueError("Возврат возможен только для подтверждённого платежа")
            amount = num(row["amount"])
            if row["order_id"]:
                self.acc.add_payment(
                    row["order_id"], amount, "refund", acc_id, METHOD_NAME,
                    note, request_id=f"sbp-refund:{payment_id}")
            else:
                self.acc.add_transaction(
                    "expense", "refund", amount,
                    f"Возврат СБП: {row['purpose'] or ''}",
                    note=note, account_id=acc_id, channel=CHANNEL, payer="person")
            stamp = now_iso()
            self.db.execute(
                "UPDATE sbp_payments SET status=?,refunded_at=?,refunded_by=?,"
                "refund_note=?,updated_at=? WHERE id=?",
                (STATUS_REFUNDED, stamp, actor or "panel", note, stamp, payment_id))
        row = self._get(payment_id)
        self._audit(payment_id, "refund", "Возврат СБП выполнен",
                    f"{num(row['amount']):g} RUB · {note}",
                    {"account_id": acc_id, "bank_done": True}, actor)
        self.db.add_event("finance", "Возврат СБП выполнен",
                          f"{num(row['amount']):g} RUB · {note}",
                          data={"payment_id": payment_id, "actor": actor})
        return row
