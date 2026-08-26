"""После продажи: честный запрос отзыва, ответ и подтверждённый повтор.

PrintFlow только готовит тексты. Внешняя отправка, полученный ответ, разрешение
на публикацию и намерение повторить заказ фиксируются оператором явно.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso


_PERMISSIONS = {"not_asked", "granted", "denied"}
_REPEAT_INTEREST = {"not_asked", "yes", "no"}


def _moment(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class CustomerAftercare:
    def __init__(self, db, repo):
        self.db = db
        self.repo = repo
        self.acc = Accounting(db)

    def _order(self, order_id: str) -> dict:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        status = self.db.one("SELECT is_final FROM statuses WHERE id=?", (order.get("status"),))
        if not status or not status.get("is_final"):
            raise ValueError("Запрос отзыва доступен только после выдачи заказа")
        debt = num(self.acc.order_economics(order).get("debt"))
        if debt > 0.005:
            raise ValueError(f"Сначала закройте долг по заказу: {debt:g} ₽")
        return order

    def _age(self, order: dict) -> tuple[int, int]:
        closed = _moment(order.get("closed_at") or order.get("updated_at") or order.get("created_at"))
        age = max(0, (datetime.now(timezone.utc) - closed).days) if closed else 0
        delay = max(0, int(num(self.db.setting("feedback_delay_days", 2), 2)))
        return age, max(0, delay - age)

    def _message(self, order: dict) -> str:
        name = str(order.get("customer_name") or "").strip()
        hello = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
        number = str(order.get("number") or "").strip()
        product = str(order.get("product") or "заказ").strip()
        return (
            f"{hello} Спасибо за заказ №{number} «{product}». Всё ли подошло? "
            "Будем благодарны за короткую обратную связь. Если что-то нужно "
            "исправить, пожалуйста, напишите — обязательно разберёмся."
        )

    def _after_message(self, feedback: dict) -> str:
        name = str(feedback.get("customer_name") or "").strip()
        hello = f"{name}, спасибо" if name else "Спасибо"
        if int(num(feedback.get("rating"))) <= 3:
            return (
                f"{hello} за честную обратную связь. Нам важно исправить ситуацию. "
                "Уточните, пожалуйста, что именно не подошло — предложим решение."
            )
        return f"{hello} за обратную связь! Очень рады, что заказ вам подошёл."

    def summary(self, order_id: str) -> dict:
        order = self._order(order_id)
        feedback = self.db.one("SELECT * FROM customer_feedback WHERE order_id=?", (order_id,))
        age, wait = self._age(order)
        message = (feedback or {}).get("request_message") or self._message(order)
        contact = str(order.get("messenger") or order.get("phone") or "").strip()
        state = "ready"
        if feedback and feedback.get("feedback_received_at"):
            state = "received"
        elif feedback and feedback.get("request_sent_at"):
            state = "waiting"
        elif not contact:
            state = "no_contact"
        elif wait:
            state = "scheduled"
        return {
            "order": order,
            "feedback": feedback,
            "state": state,
            "age_days": age,
            "wait_days": wait,
            "contact": contact,
            "message": message,
            "message_after_feedback": self._after_message(feedback) if feedback and feedback.get("feedback_received_at") else "",
            "external_sent_by_printflow": False,
            "publish_action_performed": False,
            "can_prepare_repeat": bool(
                feedback and feedback.get("feedback_received_at")
                and feedback.get("repeat_interest") == "yes"
                and not feedback.get("repeat_order_id")
            ),
        }

    def queue(self, limit: int = 80) -> dict:
        wanted = max(1, min(int(limit), 300))
        rows = self.db.query(
            "SELECT o.* FROM orders o JOIN statuses s ON s.id=o.status"
            " WHERE s.is_final=1 ORDER BY datetime(COALESCE(NULLIF(o.closed_at,''),o.updated_at,o.created_at)) DESC"
            " LIMIT ?", (max(300, wanted),)
        )
        items = []
        counts = {"ready": 0, "waiting": 0, "received": 0, "scheduled": 0, "no_contact": 0}
        for order in rows:
            try:
                item = self.summary(order["id"])
            except ValueError:
                continue  # долг — сначала дебиторка, а не просьба об отзыве
            counts[item["state"]] = counts.get(item["state"], 0) + 1
            # Полученные отзывы держим рядом только за последние 30 дней.
            if item["state"] == "received" and item["age_days"] > 30:
                continue
            items.append(item)
        priority = {"ready": 0, "waiting": 1, "scheduled": 2, "received": 3, "no_contact": 4}
        items.sort(key=lambda item: (priority.get(item["state"], 9), -item["age_days"]))
        return {"items": items[:wanted], "counts": counts, "total": len(items)}

    def confirm_request(
        self,
        order_id: str,
        *,
        sent_confirmed: bool = False,
        force: bool = False,
        request_id: str = "",
    ) -> dict:
        if not sent_confirmed:
            raise ValueError("Подтвердите, что запрос действительно отправлен клиенту")
        request_id = str(request_id or "").strip()[:120]
        if not request_id:
            raise ValueError("Не указан ключ отправки запроса")
        used = self.db.one("SELECT * FROM customer_feedback WHERE request_id=?", (request_id,))
        if used:
            if used.get("order_id") != order_id:
                raise ValueError("Ключ отправки уже использован для другого заказа")
            result = self.summary(order_id)
            result.update({"ok": True, "already_recorded": True})
            return result

        preview = self.summary(order_id)
        existing = preview.get("feedback") or {}
        if existing.get("request_sent_at"):
            preview.update({"ok": True, "already_recorded": True})
            return preview
        if preview["wait_days"] and not force:
            raise ValueError(
                f"Просить отзыв пока рано; рекомендуемый срок через {preview['wait_days']} дн."
            )
        if not preview["contact"]:
            raise ValueError("У заказа нет телефона или мессенджера для связи")

        order = preview["order"]
        stamp = now_iso()
        with self.db.transaction():
            used = self.db.one("SELECT id,order_id FROM customer_feedback WHERE request_id=?", (request_id,))
            if used and used.get("order_id") != order_id:
                raise ValueError("Ключ отправки уже использован для другого заказа")
            current = self.db.one("SELECT * FROM customer_feedback WHERE order_id=?", (order_id,))
            if current and current.get("request_sent_at"):
                result = self.summary(order_id)
                result.update({"ok": True, "already_recorded": True})
                return result
            row = self.db.upsert("customer_feedback", {
                "id": (current or existing).get("id") or uid("fb"),
                "order_id": order_id,
                "customer_id": order.get("customer_id") or None,
                "order_number": order.get("number") or "",
                "product": order.get("product") or "",
                "customer_name": order.get("customer_name") or "",
                "request_message": preview["message"],
                "request_sent_at": stamp,
                "request_id": request_id,
                "created_at": (current or existing).get("created_at") or stamp,
                "updated_at": stamp,
            })
            self.db.add_event(
                "customer", "Запрос отзыва отмечен отправленным",
                f"Заказ №{order.get('number') or ''} · {order.get('customer_name') or 'клиент'}",
                "", {"order_id": order_id, "feedback_id": row["id"],
                     "external_sent_by_printflow": False},
            )
        result = self.summary(order_id)
        result.update({"ok": True, "already_recorded": False})
        return result

    def record_response(
        self,
        order_id: str,
        *,
        response_received: bool = False,
        rating: float = 0,
        text: str = "",
        publish_permission: str = "not_asked",
        repeat_interest: str = "not_asked",
        request_id: str = "",
    ) -> dict:
        if not response_received:
            raise ValueError("Подтвердите, что ответ действительно получен от клиента")
        request_id = str(request_id or "").strip()[:120]
        if not request_id:
            raise ValueError("Не указан ключ сохранения ответа")
        rating_value = num(rating)
        rating_int = int(rating_value)
        if rating_int < 1 or rating_int > 5 or abs(rating_value - rating_int) > 0.0001:
            raise ValueError("Оценка должна быть целым числом от 1 до 5")
        if publish_permission not in _PERMISSIONS:
            raise ValueError("Уточните разрешение на публикацию")
        if repeat_interest not in _REPEAT_INTEREST:
            raise ValueError("Уточните интерес к повторному заказу")
        text = str(text or "").strip()[:5000]

        feedback = self.db.one("SELECT * FROM customer_feedback WHERE order_id=?", (order_id,))
        if not feedback or not feedback.get("request_sent_at"):
            raise ValueError("Сначала подтвердите отправку запроса клиенту")
        used = self.db.one(
            "SELECT * FROM customer_feedback WHERE response_request_id=?", (request_id,)
        )
        if used:
            if used.get("order_id") != order_id:
                raise ValueError("Ключ ответа уже использован для другого заказа")
            result = self.summary(order_id)
            result.update({"ok": True, "already_recorded": True})
            return result
        if feedback.get("feedback_received_at"):
            raise ValueError("Ответ клиента по этому заказу уже сохранён")

        stamp = now_iso()
        with self.db.transaction():
            current = self.db.one("SELECT * FROM customer_feedback WHERE order_id=?", (order_id,))
            if current and current.get("feedback_received_at"):
                if current.get("response_request_id") == request_id:
                    result = self.summary(order_id)
                    result.update({"ok": True, "already_recorded": True})
                    return result
                raise ValueError("Ответ клиента по этому заказу уже сохранён")
            self.db.execute(
                "UPDATE customer_feedback SET rating=?,feedback_text=?,feedback_received_at=?,"
                "response_request_id=?,publish_permission=?,repeat_interest=?,updated_at=?"
                " WHERE id=?",
                (rating_int, text, stamp, request_id, publish_permission,
                 repeat_interest, stamp, feedback["id"]),
            )
            # Единый слой отзывов: ручной ответ из aftercare также виден во
            # вкладке клиентского бота и не будет запрошен повторно.
            for link in self.db.query("SELECT chat_id FROM client_orders WHERE order_id=?", (order_id,)):
                chat_id = link.get("chat_id") or ""
                if not chat_id:
                    continue
                bot_rating = "good" if rating_int >= 4 else "bad"
                self.db.execute(
                    "INSERT OR IGNORE INTO client_reviews(order_id,chat_id,asked_at,rating,comment,state,created_at)"
                    " VALUES(?,?,?, ?,?,'rated',?)",
                    (order_id, chat_id, feedback.get("request_sent_at") or stamp,
                     bot_rating, text, stamp),
                )
                self.db.execute(
                    "UPDATE client_reviews SET rating=?,comment=?,state=?,created_at=?"
                    " WHERE order_id=? AND chat_id=?",
                    (bot_rating, text, "rated" if rating_int >= 4 else "needs_attention",
                     stamp, order_id, chat_id),
                )
            self.db.add_event(
                "customer", "Получена обратная связь",
                f"Заказ №{feedback.get('order_number') or ''} · оценка {rating_int}/5",
                "", {"order_id": order_id, "feedback_id": feedback["id"],
                     "rating": rating_int, "publish_permission": publish_permission,
                     "repeat_interest": repeat_interest},
            )
        result = self.summary(order_id)
        result.update({
            "ok": True,
            "already_recorded": False,
            "needs_attention": rating_int <= 3,
        })
        return result

    def prepare_repeat(
        self,
        order_id: str,
        *,
        repeat_confirmed: bool = False,
        request_id: str = "",
    ) -> dict:
        if not repeat_confirmed:
            raise ValueError("Подтвердите создание черновика повторного заказа")
        request_id = str(request_id or "").strip()[:120]
        if not request_id:
            raise ValueError("Не указан ключ повторного заказа")
        feedback = self.db.one("SELECT * FROM customer_feedback WHERE order_id=?", (order_id,))
        if not feedback or feedback.get("repeat_interest") != "yes":
            raise ValueError("Клиент не подтвердил интерес к повторному заказу")
        used = self.db.one("SELECT * FROM customer_feedback WHERE repeat_request_id=?", (request_id,))
        if used and used.get("order_id") != order_id:
            raise ValueError("Ключ повтора уже использован для другого заказа")
        if feedback.get("repeat_order_id"):
            repeat = self.repo.order(feedback["repeat_order_id"])
            return {"ok": True, "already_prepared": True, "order": repeat,
                    "feedback": feedback}

        with self.db.transaction():
            current = self.db.one("SELECT * FROM customer_feedback WHERE order_id=?", (order_id,))
            if current and current.get("repeat_order_id"):
                repeat = self.repo.order(current["repeat_order_id"])
                return {"ok": True, "already_prepared": True, "order": repeat,
                        "feedback": current}
            repeat = self.repo.duplicate_order(order_id)
            self.db.execute(
                "UPDATE customer_feedback SET repeat_order_id=?,repeat_request_id=?,updated_at=?"
                " WHERE id=?",
                (repeat["id"], request_id, now_iso(), feedback["id"]),
            )
            self.db.add_event(
                "customer", "Подготовлен черновик повторного заказа",
                f"№{repeat.get('number')} из заказа №{feedback.get('order_number')}",
                "", {"source_order_id": order_id, "order_id": repeat["id"],
                     "feedback_id": feedback["id"]},
            )
        return {"ok": True, "already_prepared": False, "order": repeat,
                "feedback": self.db.one("SELECT * FROM customer_feedback WHERE id=?", (feedback["id"],))}
