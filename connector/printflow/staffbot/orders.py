"""Заказы и покупатели из Telegram: список, карточка, выдача, оплата.

Опасные развилки (выдать в долг, принять оплату) требуют явного слова
подтверждения — «выдать 1001 оплачен» или «выдать 1001 в долг»: сумма
вопроса больше стоимости одного лишнего касания.
"""
from __future__ import annotations

import re as _re

from ..accounting import num
from ..config import now_iso
from .ui import home_row, money


class OrdersMixin:
    """Экраны заказов и действия, которые меняют их состояние."""

    def _active_orders(self, limit: int = 12) -> list[dict]:
        return self.db.query(
            "SELECT * FROM orders WHERE COALESCE(closed_at,'')=''"
            " ORDER BY datetime(created_at) DESC LIMIT ?", (int(limit),))

    def _final_status_id(self) -> str | None:
        row = self.db.one("SELECT id FROM statuses WHERE is_final=1 ORDER BY position LIMIT 1")
        return (row or {}).get("id")

    # -------------------------------------------------------------- экраны
    def orders_keyboard(self, chat: str, message_id: str = "") -> None:
        """Список активных заказов кнопками → карточка заказа."""
        rows = self._active_orders()
        if not rows:
            text = "📦 Активных заказов нет.\nНовый: «новый адресник 2шт 900р Мария»."
            buttons = [home_row()]
            return self._send_menu(chat, text, buttons, message_id)
        lines = [f"📦 Активные заказы: {len(rows)}", ""]
        buttons = []
        for order in rows:
            title = f"№{order.get('number')} · {str(order.get('product') or '')[:18]}"
            if order.get("customer_name"):
                title += f" · {str(order['customer_name'])[:14]}"
            lines.append(title)
            buttons.append([{"text": title[:60],
                             "callback_data": f"cmd:order:{order['number']}"}])
        buttons.append(home_row())
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def order_card(self, chat: str, number: str, message_id: str = "") -> None:
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return self._reply(chat, f"Заказ №{number} не найден.")
        due = num(self.manager.acc.order_economics(order).get("debt"))
        lines = [f"📦 Заказ №{number}",
                 f"• {order.get('product') or '—'} × {round(num(order.get('qty')),1)} шт",
                 f"• Клиент: {order.get('customer_name') or '—'}"
                 + (f" · {order.get('phone')}" if order.get("phone") else ""),
                 f"• Статус: {order.get('status') or 'новый'}",
                 f"• Цена: {money(order.get('price'))} · Оплачено: "
                 f"{money(max(num(order.get('prepaid')), num(order.get('paid'))))}",
                 f"• Долг: {money(due)}",
                 f"• Срок: {order.get('due') or '—'}",
                 f"• Задание: {order.get('file') or 'файл не привязан'}"]
        buttons = []
        if due > 0:
            buttons.append([{"text": f"💰 Оплатить долг · {money(due)}",
                             "callback_data": f"cmd:order-pay:{number}"}])
        buttons.append([{"text": "📝 В работу", "callback_data": f"cmd:order-status:{number}:printing"},
                        {"text": "✅ Готов", "callback_data": f"cmd:order-ready:{number}"}])
        buttons.append([{"text": "📦 Выдать (оплачен)", "callback_data": f"cmd:order-fulfill:{number}:paid"},
                        {"text": "📦 Выдать (в долг)", "callback_data": f"cmd:order-fulfill:{number}:debt"}])
        buttons.append([{"text": "◉ Следить", "callback_data": f"cmd:watch:{number}"},
                        {"text": "🏠 В меню", "callback_data": "cmd:menu"}])
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    # ------------------------------------------------------------ операции
    def order_ready(self, raw: str) -> str:
        """«готов 1001» — подтвердить приёмку через общий сервис завершения."""
        words = raw.lower().replace("ё", "е").split()
        number = next((w for w in words[1:] if w.isdigit()), "")
        if not number:
            return ("Укажите номер заказа: «готов 1001».\n"
                    "Команда подтвердит визуальную приёмку и подготовит текст клиенту.")
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        from ..completion import OrderCompletion
        try:
            result = OrderCompletion(self.db, self.manager.repo).accept(
                order["id"], quality_confirmed=True
            )
        except ValueError as exc:
            return f"Не получилось принять заказ №{number}: {exc}"
        actual = result.get("actual") or {}
        repeated = " (уже был принят)" if result.get("already_accepted") else ""
        return (
            f"Заказ №{number} готов ✓{repeated}\n"
            f"Факт: {round(num(actual.get('grams')), 1)} г · "
            f"{round(num(actual.get('hours')), 2)} ч · {money(actual.get('cost'))}\n\n"
            f"Текст для клиента (не отправлен):\n{result.get('message') or ''}"
        )

    def _fulfill(self, raw: str) -> str:
        """Подтверждаемая выдача: «выдать 1001 оплачен» или «… в долг»."""
        words = raw.lower().replace("ё", "е").split()
        number = next((word for word in words[1:] if word.isdigit()), "")
        if not number:
            return "Укажите номер: «выдать 1001 оплачен» или «выдать 1001 в долг»."
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        paid_confirmed = any(token in words for token in (
            "оплачен", "оплачено", "оплата", "получено", "получена",
        ))
        debt_confirmed = "долг" in words
        due = self.manager.acc.order_economics(order).get("debt", 0)
        if num(due) > 0 and not (paid_confirmed or debt_confirmed):
            return (f"По заказу №{number} осталось {money(due)}.\n"
                    f"Подтвердите: «выдать {number} оплачен» или «выдать {number} в долг».")
        if num(due) <= 0:
            action = "none"
        else:
            action = "received" if paid_confirmed else "debt"
        method = ("cash" if any("налич" in word for word in words) else
                  "card" if any("карт" in word for word in words) else
                  "transfer" if any("перевод" in word for word in words) else "other")
        from ..fulfillment import OrderFulfillment
        from ..stock import Stock
        try:
            result = OrderFulfillment(
                self.db, self.manager.repo, Stock(self.db), self.manager.acc
            ).fulfill(
                order["id"],
                handoff_confirmed=True,
                payment_action=action,
                payment_method=method if action == "received" else "",
            )
        except ValueError as exc:
            return f"Не получилось выдать заказ №{number}: {exc}"
        if not result.get("already_fulfilled"):
            client = getattr(self.manager, "client_bot", None)
            link = self.db.one("SELECT * FROM client_orders WHERE order_id=? ORDER BY datetime(created_at) LIMIT 1",
                               (order["id"],))
            if client and link:
                try:
                    client._reply_keyed(link["chat_id"],
                                        f"Заказ №{number} выдан ✓ Спасибо, что выбрали NOZZA!",
                                        client._menu(), dedupe_key=f"delivered:{order['id']}")
                    client._funnel(link["chat_id"], "delivered", source=link.get("source") or "telegram",
                                   order_id=order["id"])
                except Exception:
                    pass
        repeated = " (уже был выдан)" if result.get("already_fulfilled") else ""
        money_line = (f"получено {money(result.get('collected'))}"
                      if num(result.get("collected")) > 0 else
                      f"оставлен долг {money(result.get('debt'))}"
                      if num(result.get("debt")) > 0 else "оплачен ранее")
        return (f"✅ Заказ №{number} выдан{repeated} · {money_line}.\n\n"
                f"Текст клиенту (не отправлен):\n{result.get('message') or ''}")

    def _pay(self, text: str) -> str:
        """«оплата 1500 по 1001» — записать приход денег по заказу."""
        amount_m = _re.search(r"(\d[\d\s.,]*)\s*(?:р|руб|₽)?\s*по\s*(\d+)", text.lower())
        if not amount_m:
            amount_m = _re.search(r"по\s*(\d+).*?(\d[\d\s.,]*)\s*(?:р|руб|₽)?", text.lower())
        if not amount_m:
            return "Формат: «оплата 1500 по 1001»."
        amount = num(amount_m.group(1).replace(" ", "").replace(",", "."))
        number = amount_m.group(2)
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        if amount <= 0:
            return "Сумма должна быть больше нуля."
        try:
            self.manager.acc.add_payment(
                order["id"], amount, "payment",
                order.get("account_id") or "", "other", "оплата из Telegram",
                request_id=(f"staff-tg-payment:{self._current_update_id}"
                            if self._current_update_id else ""),
            )
        except ValueError as exc:
            return f"Не получилось записать оплату: {exc}"
        left = max(0.0, num(order.get("price")) -
                   (max(num(order.get("paid")), num(order.get("prepaid"))) + amount))
        return f"💰 Принято {money(amount)} по заказу №{number}." + \
            (f" Осталось {money(left)}." if left > 0 else " Оплачен полностью.")

    def _set_status(self, text: str) -> str:
        parts = text.split()
        number = next((w for w in parts[1:] if w.isdigit()), "")
        if not number:
            return "Формат: «статус 1001 печать»."
        target = next((w for w in parts[1:] if w.isalpha() and not w.isdigit()), "")
        if not target:
            return "Укажите статус: «статус 1001 печать»."
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        status = self.db.one(
            "SELECT id FROM statuses WHERE pylower(name) LIKE ? LIMIT 1",
            (f"%{target}%",))
        if not status:
            return f"Статус «{target}» не найден."
        self.db.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?",
                        (status["id"], now_iso(), order["id"]))
        self.db.add_event("order", "Статус изменён (Telegram)",
                          f"№{number} → {status['id']}", data={"order_id": order["id"]})
        return f"✅ Заказ №{number} → «{status['id']}»."

    def _parse_new_order(self, text: str) -> dict:
        """Единый локальный парсер для Telegram и панели «Заказ из текста»."""
        from ..order_intake import parse_order_text
        parsed = parse_order_text(text)
        return {**parsed, "client": parsed.get("client", "")}

    def _new_order(self, text: str) -> str:
        from ..order_intake import OrderIntake
        preview = OrderIntake(self.db).preview(text, "telegram")
        parsed = preview["parsed"]
        draft = preview["draft"]
        if not draft["product"]:
            return "Формат: «новый адресник 2шт 900р Мария»."
        if self._current_update_id:
            draft["client_request_id"] = f"staff-tg:{self._current_update_id}"
        order = self.manager.repo.save_order(draft)
        return (f"📝 Создан заказ №{order.get('number')} «{draft['product']}»"
                + (f" · {int(parsed['qty'])} шт" if parsed["qty"] > 1 else "")
                + (f" · {money(draft['price'])}" if draft["price"] else "")
                + (f" · {draft['customer_name']}" if draft["customer_name"] else ""))

    # ---------------------------------------------------- текстовые команды
    def cmd_new_order(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._new_order(text))

    def cmd_fulfill(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._fulfill(text))

    def cmd_ready(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.order_ready(text))

    def cmd_pay(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._pay(text))

    def cmd_month_close(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._month_close(text))

    def cmd_payment_confirm(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._client_payment_action(text, "confirm", chat))

    def cmd_payment_reject(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._client_payment_action(text, "reject", chat))

    # -------------------------------------------------------------- фото
    def _order_number(self, order_id: str) -> str:
        order = self.db.one("SELECT number FROM orders WHERE id=?", (order_id,))
        return str((order or {}).get("number") or "")

    def _attach_photo(self, chat: str, photo: list, caption: str = "") -> None:
        """Прислал фото — прикрепляем к заказу (по подписи или последнему активному)."""
        order_id = ""
        caption = (caption or "").strip()
        number = next((w for w in caption.split() if w.isdigit()), "")
        if number:
            order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
            order_id = order["id"] if order else ""
        if not order_id:
            order = self.db.one(
                "SELECT * FROM orders WHERE status NOT IN"
                " (SELECT id FROM statuses WHERE is_final=1)"
                " ORDER BY datetime(updated_at) DESC LIMIT 1")
            if not order:
                return self._reply(chat, "Нет активного заказа — укажите номер: «фото 1001».")
            order_id = order["id"]
        # Скачиваем самый крупный из присланных размеров.
        file_id = str((photo[-1] or {}).get("file_id") or "")
        if not file_id:
            return self._reply(chat, "Не удалось получить фото.")
        raw = self._download_file(file_id)
        if not raw:
            return self._reply(chat, "Не удалось скачать фото.")
        from ..config import PHOTO_DIR
        import time as _time
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        name = f"order_{order_id}_{int(_time.time())}.jpg"
        (PHOTO_DIR / name).write_bytes(raw)
        self.db.upsert("order_photos", {
            "id": f"ph{int(_time.time() * 1000)}", "order_id": order_id, "at": now_iso(),
            "file": name, "note": "фото из Telegram", "kind": "upload"})
        self.db.add_event("order", "Фото к заказу (Telegram)",
                          f"Заказ {self._order_number(order_id)}", "",
                          {"order_id": order_id})
        self._reply(chat, f"📷 Фото прикреплено к заказу №{self._order_number(order_id)}.")

    # ------------------------------------------------------- кнопки (callback)
    def cb_orders(self, chat: str, message_id: str, params: str) -> None:
        self.orders_keyboard(chat, message_id)

    def cb_order(self, chat: str, message_id: str, params: str) -> None:
        self.order_card(chat, params, message_id)

    def cb_order_pay(self, chat: str, message_id: str, params: str) -> None:
        number = params
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        due = num(self.manager.acc.order_economics(order).get("debt")) if order else 0
        self._reply(chat, self._pay(f"оплата {due} по {number}") if due > 0
                    else "Долга нет — заказ уже оплачен.")
        self.order_card(chat, number, message_id)

    def cb_order_status(self, chat: str, message_id: str, params: str) -> None:
        number, _, status = params.partition(":")
        self._reply(chat, self._set_status(f"статус {number} {status}"))
        self.order_card(chat, number, message_id)

    def cb_order_ready(self, chat: str, message_id: str, params: str) -> None:
        self._reply(chat, self.order_ready(f"готов {params}"))
        self.order_card(chat, params, message_id)

    def cb_order_fulfill(self, chat: str, message_id: str, params: str) -> None:
        number, _, mode = params.partition(":")
        token = "оплачен" if mode == "paid" else "в долг"
        self._reply(chat, self._fulfill(f"выдать {number} {token}"))
        self.orders_keyboard(chat, message_id)

    def cb_watch(self, chat: str, message_id: str, params: str) -> None:
        self._reply(chat, self._watch_order(chat, params))
