"""Безопасная выдача заказа: передача, оплата, склад и закрытие.

Статус заказа не является доказательством оплаты. Поэтому остаток попадает в
кассу только после явного выбора мастера: «оплата получена» или «выдать в долг».
Все изменения выполняются одной SQLite-транзакцией и повторный запрос безопасен.
"""
from __future__ import annotations

from typing import Any

from .accounting import Accounting, num
from .config import now_iso
from .db import Database
from .repo import Repo
from .stock import Stock

PAYMENT_ACTIONS = ("received", "debt", "none")
PAYMENT_METHODS = ("cash", "card", "transfer", "other")


class OrderFulfillment:
    """Единый сервис предварительной проверки и подтверждения выдачи."""

    def __init__(self, db: Database, repo: Repo, stock: Stock, accounting: Accounting):
        self.db = db
        self.repo = repo
        self.stock = stock
        self.acc = accounting

    @staticmethod
    def _message(order: dict, debt: float) -> str:
        name = str(order.get("customer_name") or "").strip()
        greeting = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
        number = str(order.get("number") or "").strip()
        ref = f" №{number}" if number else ""
        text = f"{greeting} Заказ{ref} передан. Спасибо, что выбрали нас!"
        # Подарочный режим (идея 33): цену и долг в сообщение не пишем —
        # получателю это не адресовано, платит даритель.
        if debt > 0 and not order.get("gift"):
            amount = str(int(debt)) if float(debt).is_integer() else str(round(debt, 2))
            text += f" Осталось к оплате: {amount} ₽."
        if order.get("gift"):
            text += " Внутри — с пожеланиями. Хорошего дня!"
        return text

    def summary(self, order_id: str) -> dict[str, Any]:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        status = self.db.one("SELECT * FROM statuses WHERE id=?", (order.get("status"),)) or {}
        final = bool(num(status.get("is_final")))
        economics = self.acc.order_economics(order)
        due = round(num(economics.get("debt")), 2)

        jobs = self.db.query(
            "SELECT id,name,file,state FROM print_jobs WHERE order_id=?", (order_id,)
        )
        printing = [job for job in jobs if job.get("state") in ("starting", "running")]
        queued_jobs = sum(job.get("state") == "queued" for job in jobs)
        active_jobs = len(printing)
        successful_jobs = sum(job.get("state") == "done" for job in jobs)

        stock_info: dict[str, Any] = {
            "required": False, "ok": True, "available": 0.0, "qty": 0.0,
            "warehouse_id": order.get("warehouse_id") or "",
            "items": [],
        }
        if num(order.get("reserved")):
            reserves = self.db.query(
                "SELECT * FROM reserves WHERE order_id=? AND state='active' ORDER BY id",
                (order_id,),
            )
            stock_items = []
            for reserve in reserves:
                reserve_nom_id = str(reserve.get("nom_id") or order.get("nom_id") or "")
                if not reserve_nom_id:
                    continue
                warehouse_id = reserve.get("warehouse_id") or order.get("warehouse_id") or ""
                variant_id = str(reserve.get("variant_id") or order.get("client_variant_id") or "")
                qty = max(1.0, num(reserve.get("qty"), num(order.get("qty"), 1)))
                available = self.stock.qty(reserve_nom_id, warehouse_id, variant_id)
                stock_items.append({
                    "available": round(available, 3), "qty": round(qty, 3),
                    "warehouse_id": warehouse_id, "variant_id": variant_id,
                    "nom_id": reserve_nom_id, "ok": available >= qty,
                })
            if stock_items:
                first = stock_items[0]
                stock_info = {
                    "required": True,
                    "ok": all(item["ok"] for item in stock_items),
                    "available": first["available"], "qty": first["qty"],
                    "warehouse_id": first["warehouse_id"],
                    "variant_id": first["variant_id"], "nom_id": first["nom_id"],
                    "items": stock_items,
                }

        blocks: list[dict] = []
        warns: list[dict] = []
        if not final and order.get("status") != "ready":
            blocks.append({
                "code": "status",
                "text": "Сначала примите результат и переведите заказ в статус «Готов»",
            })
        if not final and printing:
            names = ", ".join(
                str(job.get("name") or job.get("file") or "печать") for job in printing[:3]
            )
            blocks.append({
                "code": "active_jobs",
                "text": f"Печать ещё идёт: {names}",
            })
        if not final and successful_jobs and order.get("quality") != "passed":
            blocks.append({
                "code": "quality",
                "text": "Результат печати ещё не прошёл визуальную приёмку",
            })
        if not final and not stock_info["ok"]:
            blocks.append({
                "code": "stock",
                "text": f"На складе {stock_info['available']} шт, нужно {stock_info['qty']} шт",
            })
        if due > 0 and not final:
            warns.append({
                "code": "payment",
                "text": f"Перед выдачей нужно отметить оплату или явно оставить долг {due:g} ₽",
            })

        accounts = self.db.query(
            "SELECT id,name,kind,fee_percent FROM accounts WHERE archived=0 ORDER BY position,name"
        )
        default_account = str(
            order.get("account_id") or self.db.setting("default_account", "cash") or ""
        )
        return {
            "ok": True,
            "order_id": order_id,
            "number": order.get("number") or "",
            "status": {"id": order.get("status") or "", "name": status.get("name") or ""},
            "fulfilled": final,
            "can_fulfill": final or not blocks,
            "payment": {
                "price": round(num(economics.get("price")), 2),
                "paid": round(num(economics.get("paid")), 2),
                "due": due,
                "required": due > 0,
                "default_account_id": default_account,
                "accounts": accounts,
            },
            "stock": stock_info,
            "jobs": {
                "total": len(jobs), "active": active_jobs,
                "queued": queued_jobs, "successful": successful_jobs,
            },
            "economics": economics,
            "blocks": blocks,
            "warns": warns,
            "message": self._message(order, due if final else 0.0),
            "external_sent": False,
        }

    def _mirror_shelf_issue(self, order: dict, nom_id: str,
                            warehouse_id: str, qty: float) -> None:
        """Зеркало выдачи со склада-витрины на карточки полки.

        Регистр — источник правды, но физика ушла с полки: связанные карточки
        списываем уходом (без ноги регистра — её уже написала выдача).
        Не хватает на карточках — честная ошибка: выдача откатывается целиком,
        чинится инвентаризацией/перемещением и повторяется.
        """
        from .shelf import Shelf
        if not nom_id or not self.stock.is_shelf_zone(warehouse_id):
            return
        items = Shelf(self.db).items_for_nom(nom_id)
        if not items:
            return  # витрина без карточки — зеркалить нечего
        left = round(num(qty), 3)
        shelf = Shelf(self.db)
        note = f"выдача заказа №{order.get('number') or ''}"
        for item in items:
            if left <= 1e-9:
                break
            have = num(item.get("qty"))
            if have <= 0:
                continue
            take = round(min(left, have), 3)
            shelf.writeoff(str(item.get("id") or ""), take, note,
                           register_leg=False)
            left = round(left - take, 3)
        if left > 1e-9:
            raise ValueError(
                f"На полке не хватает «{items[0].get('name') or nom_id}» "
                f"для выдачи заказа №{order.get('number') or ''}: "
                f"нужно {num(qty):g}, на карточках меньше. "
                f"Проверьте полку инвентаризацией и повторите выдачу")

    def fulfill(
        self,
        order_id: str,
        *,
        handoff_confirmed: bool = False,
        payment_action: str = "",
        account_id: str = "",
        payment_method: str = "",
    ) -> dict[str, Any]:
        """Атомарно выдать заказ и явно отразить оплату либо долг."""
        with self.db.transaction():
            summary = self.summary(order_id)
            if summary["fulfilled"]:
                summary["already_fulfilled"] = True
                summary["collected"] = 0.0
                summary["debt"] = round(num(summary["payment"]["due"]), 2)
                summary["order"] = self.repo.order(order_id)
                return summary
            if not handoff_confirmed:
                raise ValueError("Подтвердите, что заказ передан клиенту")
            if summary["blocks"]:
                raise ValueError("Нельзя выдать заказ: " + "; ".join(
                    item["text"] for item in summary["blocks"]
                ))
            self.db.execute(
                "UPDATE print_jobs SET state='cancelled', finished_at=? "
                "WHERE order_id=? AND state='queued'",
                (now_iso(), order_id),
            )

            due = num(summary["payment"]["due"])
            action = str(payment_action or "").strip().lower()
            if action not in PAYMENT_ACTIONS:
                raise ValueError("Выберите: оплата получена или выдать в долг")
            if due > 0 and action not in ("received", "debt"):
                raise ValueError("Укажите, получена ли оставшаяся оплата")
            if due <= 0:
                action = "none"

            collected = 0.0
            if action == "received":
                # Старые базы могли хранить предоплату отдельно. Переносим её
                # в основной счётчик до добавления остатка, иначе paid начался
                # бы с нуля и заказ ошибочно сохранил бы часть долга.
                legacy = self.db.one(
                    "SELECT paid,prepaid FROM orders WHERE id=?", (order_id,)
                ) or {}
                if num(legacy.get("prepaid")) > num(legacy.get("paid")):
                    self.db.execute(
                        "UPDATE orders SET paid=?,prepaid=0,updated_at=? WHERE id=?",
                        (num(legacy["prepaid"]), now_iso(), order_id),
                    )
                method = str(payment_method or "").strip().lower()
                if method not in PAYMENT_METHODS:
                    raise ValueError("Выберите способ оплаты")
                self.acc.add_payment(
                    order_id,
                    due,
                    "payment",
                    account_id or summary["payment"]["default_account_id"],
                    method,
                    "Подтверждено при выдаче заказа",
                )
                collected = due

            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,)) or {}
            active_reserves = self.db.query(
                "SELECT * FROM reserves WHERE order_id=? AND state='active' ORDER BY id",
                (order_id,),
            )
            if num(order.get("reserved")) and active_reserves:
                already = self.db.one(
                    "SELECT id FROM stock_moves WHERE doc_id=? AND doc_kind='sale' LIMIT 1",
                    (order_id,),
                )
                if not already:
                    for reserve in active_reserves:
                        nom_id = str(reserve.get("nom_id") or order.get("nom_id") or "")
                        if not nom_id:
                            continue
                        variant_id = str(reserve.get("variant_id") or order.get("client_variant_id") or "")
                        warehouse_id = reserve.get("warehouse_id") or order.get("warehouse_id") or ""
                        qty = max(1.0, num(reserve.get("qty"), num(order.get("qty"), 1)))
                        unit_cost = self.stock.avg_cost(nom_id, warehouse_id, variant_id)
                        self.stock.add_move(
                            nom_id, warehouse_id, -qty, -unit_cost * qty,
                            doc_id=order_id, doc_kind="sale", variant_id=variant_id,
                            note=f"выдача заказа №{order.get('number') or ''}",
                        )
                        # Выдача со склада-витрины забирает штуки физически с
                        # полки: зеркалим уходом карточке (только нога полки —
                        # зону уже списало движение выше). Всё в транзакции
                        # выдачи: нехватка на карточке откатывает выдачу.
                        self._mirror_shelf_issue(
                            order, nom_id, str(warehouse_id or ""), qty)
                self.stock.release(order_id=order_id)

            final = self.db.one(
                "SELECT id FROM statuses WHERE is_final=1 ORDER BY position LIMIT 1"
            )
            if not final:
                raise ValueError("Не настроен финальный статус заказа")
            # Финальный статус сам по себе не доказывает оплату: запрещаем
            # старому автодоходу создавать платёж при варианте «в долг».
            self.repo.save_order({
                "id": order_id,
                "status": final["id"],
                "reserved": 0,
                "client_delivered_at": now_iso(),
                "_allow_final_status": True,
                "_skip_auto_income": True,
                "author": "order-fulfillment",
            })
            debt = 0.0 if action == "received" else due
            self.db.add_event(
                "order",
                "Заказ выдан",
                f"№{summary['number']} · " + (
                    f"получено {collected:g} ₽" if collected else
                    f"оставлен долг {debt:g} ₽" if debt else "оплачен ранее"
                ),
                data={
                    "order_id": order_id,
                    "payment_action": action,
                    "collected": collected,
                    "debt": debt,
                },
            )
            result = self.summary(order_id)
            result["already_fulfilled"] = False
            result["collected"] = round(collected, 2)
            result["debt"] = round(debt, 2)
            result["fulfilled_at"] = now_iso()
            # summary после полного платежа уже возвращает корректный текст;
            # при выдаче в долг добавляем явное напоминание клиенту.
            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,)) or {}
            result["message"] = self._message(order, debt)
            result["order"] = self.repo.order(order_id)
            return result
