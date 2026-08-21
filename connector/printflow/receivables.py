"""Дебиторка: честное напоминание и безопасное погашение долга.

PrintFlow только готовит текст. ``reminded_at`` меняется после отдельного
подтверждения пользователя, что сообщение действительно отправлено снаружи.
Платёж ограничен текущим остатком и защищён клиентским request_id от дублей.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .accounting import Accounting, num
from .config import now_iso
from .db import Database
from .repo import Repo, uid

PAYMENT_METHODS = ("cash", "card", "transfer", "other")


class Receivables:
    def __init__(self, db: Database, repo: Repo, accounting: Accounting):
        self.db = db
        self.repo = repo
        self.acc = accounting

    @staticmethod
    def _amount(value: float) -> str:
        value = round(num(value), 2)
        return str(int(value)) if value.is_integer() else str(value)

    def _message(self, order: dict, debt: float) -> str:
        name = str(order.get("customer_name") or "").strip()
        greeting = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
        number = str(order.get("number") or "").strip()
        product = str(order.get("product") or "заказ").strip()
        return (
            f"{greeting} Напоминаем об остатке оплаты по заказу №{number} "
            f"«{product}»: {self._amount(debt)} ₽. Если вы уже оплатили, "
            "пожалуйста, сообщите нам. Спасибо!"
        )

    def summary(self, order_id: str) -> dict[str, Any]:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        economics = self.acc.order_economics(order)
        debt = round(num(economics.get("debt")), 2)
        last = str(order.get("reminded_at") or "")
        cooldown_days = max(0, int(num(self.db.setting("debt_reminder_cooldown_days", 3), 3)))
        cooldown_left = 0
        if last and cooldown_days:
            try:
                stamp = datetime.fromisoformat(last)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
                cooldown_left = max(0, cooldown_days - int(elapsed // 86400))
            except ValueError:
                cooldown_left = 0
        accounts = self.db.query(
            "SELECT id,name,kind,fee_percent FROM accounts WHERE archived=0 ORDER BY position,name"
        )
        return {
            "ok": True,
            "order_id": order_id,
            "number": order.get("number") or "",
            "customer": order.get("customer_name") or "",
            "phone": order.get("phone") or "",
            "messenger": order.get("messenger") or "",
            "debt": debt,
            "paid": round(num(economics.get("paid")), 2),
            "price": round(num(economics.get("price")), 2),
            "settled": debt <= 0,
            "last_reminded_at": last,
            "cooldown_days": cooldown_days,
            "cooldown_left_days": cooldown_left,
            "can_mark_reminded": debt > 0 and cooldown_left == 0,
            "message": self._message(order, debt) if debt > 0 else "",
            "accounts": accounts,
            "default_account_id": str(
                order.get("account_id") or self.db.setting("default_account", "cash") or ""
            ),
            "external_sent_by_printflow": False,
        }

    def mark_reminded(
        self, order_id: str, *, sent_confirmed: bool = False, force: bool = False
    ) -> dict[str, Any]:
        """Отметить внешнюю отправку только после явного подтверждения."""
        if not sent_confirmed:
            raise ValueError("Подтвердите, что напоминание действительно отправлено")
        with self.db.transaction():
            summary = self.summary(order_id)
            if summary["settled"]:
                raise ValueError("Заказ уже оплачен полностью")
            if summary["cooldown_left_days"] and not force:
                raise ValueError(
                    f"Напоминание уже отправляли недавно; повтор через "
                    f"{summary['cooldown_left_days']} дн."
                )
            stamp = now_iso()
            self.db.execute(
                "UPDATE orders SET reminded_at=?,updated_at=? WHERE id=?",
                (stamp, stamp, order_id),
            )
            self.db.add_event(
                "order", "Напоминание о долге отмечено отправленным",
                f"№{summary['number']} · {self._amount(summary['debt'])} ₽",
                data={"order_id": order_id, "debt": summary["debt"]},
            )
            result = self.summary(order_id)
            result["marked_sent"] = True
            return result

    def settle(
        self,
        order_id: str,
        *,
        payment_confirmed: bool = False,
        amount: float = 0.0,
        account_id: str = "",
        payment_method: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        """Записать подтверждённую оплату долга без риска двойного платежа."""
        if not payment_confirmed:
            raise ValueError("Подтвердите получение денег")
        request_id = str(request_id or "").strip()[:120] or uid("payreq")
        with self.db.transaction():
            prior = self.db.one("SELECT * FROM payments WHERE request_id=?", (request_id,))
            summary = self.summary(order_id)
            if prior:
                if prior.get("order_id") != order_id:
                    raise ValueError("Ключ запроса уже использован для другого заказа")
                result = self.summary(order_id)
                result.update({
                    "payment": prior,
                    "already_recorded": True,
                    "received": num(prior.get("amount")),
                })
                return result
            if summary["settled"]:
                summary.update({"already_recorded": True, "received": 0.0, "payment": None})
                return summary
            amount = round(num(amount) or num(summary["debt"]), 2)
            if amount <= 0:
                raise ValueError("Сумма должна быть больше нуля")
            if amount > num(summary["debt"]) + 0.005:
                raise ValueError(f"Оплата больше долга: осталось {summary['debt']:g} ₽")
            method = str(payment_method or "").strip().lower()
            if method not in PAYMENT_METHODS:
                raise ValueError("Выберите способ оплаты")
            payment = self.acc.add_payment(
                order_id, amount, "payment",
                account_id or summary["default_account_id"], method,
                "Подтверждённая оплата долга", request_id,
            )
            left = self.summary(order_id)
            self.db.add_event(
                "order", "Получена оплата долга",
                f"№{summary['number']} · {self._amount(amount)} ₽ · "
                f"остаток {self._amount(left['debt'])} ₽",
                data={"order_id": order_id, "payment_id": payment.get("id")},
            )
            left.update({
                "payment": payment,
                "already_recorded": False,
                "received": amount,
                "message_after_payment": (
                    f"Спасибо! Оплату {self._amount(amount)} ₽ по заказу "
                    f"№{summary['number']} получили."
                    + (f" Остаток: {self._amount(left['debt'])} ₽." if left["debt"] else "")
                ),
            })
            return left
