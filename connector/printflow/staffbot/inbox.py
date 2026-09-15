"""Inbox покупателя: диалоги, ответы, шаблоны, заявки оплаты.

Мост между ботом сотрудников и клиентским ботом: уведомления о вопросах
приходят владельцу, ответы уходят покупателю от имени клиентского бота и
попадают в журнал диалогов панели. Ответ клиенту пишется следующим
сообщением после кнопки «Ответить» — без копирования chat_id руками.
"""
from __future__ import annotations

import json
import time

from ..accounting import num
from ..config import now_iso
from .scenes import CLIENT_REPLY
from .ui import home_row


class InboxMixin:
    """Клиенты клиентского бота глазами сотрудника."""

    # -------------------------------------------------------------- экраны
    def clients_keyboard(self, chat: str, message_id: str = "") -> None:
        """Клиенты клиентского бота кнопками → карточка клиента."""
        rows = self.db.query(
            "SELECT chat_id, name, phone, last_seen FROM client_chats"
            " WHERE COALESCE(banned,0)=0 ORDER BY datetime(last_seen) DESC LIMIT 12")
        if not rows:
            text = "👥 Клиентов пока нет — они появятся после первого вопроса в клиентском боте."
            buttons = [home_row()]
            return self._send_menu(chat, text, buttons, message_id)
        lines = [f"👥 Клиенты: {len(rows)}", ""]
        buttons = []
        for row in rows:
            label = f"{str(row.get('name') or 'Клиент')[:24]} · {row.get('phone') or '—'}"
            lines.append(label)
            buttons.append([{"text": label[:60],
                             "callback_data": f"cmd:client:{row['chat_id']}"}])
        buttons.append(home_row())
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def client_card(self, chat: str, client_chat: str, message_id: str = "") -> None:
        row = self.db.one("SELECT * FROM client_chats WHERE chat_id=?", (client_chat,))
        if not row:
            return self._reply(chat, "Клиент не найден.")
        orders = self.db.query(
            "SELECT number, product, status, price, prepaid FROM orders"
            " WHERE customer_id=? ORDER BY datetime(created_at) DESC LIMIT 5",
            (row.get("customer_id") or "",))
        lines = [f"👤 {row.get('name') or 'Клиент'}",
                 f"• Телефон: {row.get('phone') or '—'}",
                 f"• @{row.get('username')}" if row.get("username") else "",
                 f"• Последний визит: {str(row.get('last_seen') or '—')[:16].replace('T',' ')}",
                 f"• Источник: {row.get('source') or '—'}"]
        if orders:
            lines.append("")
            lines.append("Последние заказы:")
            for o in orders:
                lines.append(f"• №{o.get('number')} {o.get('product') or ''} · {o.get('status') or ''}")
        buttons = [[{"text": "💬 Ответить клиенту", "callback_data": f"cmd:client-reply:{client_chat}"}],
                   [{"text": "📦 Заказы", "callback_data": "cmd:orders"},
                    {"text": "🏠 В меню", "callback_data": "cmd:menu"}]]
        self._send_menu(chat, "\n".join([l for l in lines if l]), buttons, message_id)

    def client_reply_prompt(self, chat: str, client_chat: str) -> None:
        """Кнопка «Ответить»: следующее сообщение уйдёт покупателю."""
        self.scenes.set(chat, CLIENT_REPLY, {"target": client_chat})
        self._reply(chat, f"✏️ Напишите ответ клиенту {client_chat[:8]}… "
                          "одним сообщением.")

    def _client_inbox(self, limit: int = 12) -> str:
        """Короткий inbox для рабочего Telegram-бота."""
        rows = self.db.query(
            "SELECT l.chat_id,l.name,l.text,l.at,c.inbox_status FROM client_bot_log l"
            " LEFT JOIN client_chats c ON c.chat_id=l.chat_id"
            " WHERE l.direction='in' AND l.unread=1 ORDER BY l.id DESC LIMIT ?", (limit,))
        if not rows:
            return "💬 Непрочитанных диалогов нет."
        lines = [f"💬 Непрочитанные диалоги: {len(rows)}", ""]
        for row in rows:
            when = str(row.get("at") or "")[5:16].replace("T", " ")
            status = f" · {row.get('inbox_status')}" if row.get("inbox_status") else ""
            lines.append(f"• чат {row.get('chat_id')} · {row.get('name') or 'клиент'}{status} · {when}")
            lines.append(f"  «{str(row.get('text') or '')[:180]}»")
        lines.append("\nОтвет: «кответ <chat_id> <текст>». После ответа диалог отметьте прочитанным в панели.")
        return "\n".join(lines)

    # ------------------------------------------------------------ диалоги
    def _client_answer(self, raw: str) -> str:
        """«кответ <chat_id> <текст>» — ответить покупателю клиентского бота.

        Уведомления о вопросах приходят с chat_id; ответ уходит от имени
        клиентского бота и попадает в журнал диалогов вкладки «Клиент-бот».
        """
        parts = raw.strip().split(None, 2)
        if len(parts) < 3 or not parts[1].isdigit():
            return ("Формат: «кответ <chat_id> <текст>» — chat_id виден "
                    "в уведомлении о вопросе покупателя.")
        target, message = parts[1], parts[2].strip()
        client = getattr(self.manager, "client_bot", None)
        if not client:
            return "Клиентский бот не запущен — проверьте вкладку «Клиент-бот»."
        row = self.db.one("SELECT * FROM client_chats WHERE chat_id=?", (target,))
        if not row:
            return f"Чат {target} не найден среди покупателей."
        dedupe_key = (f"staff-reply:{self._current_update_id}:{target}"
                      if self._current_update_id else "")
        client._reply_keyed(target, message, client._menu(), dedupe_key=dedupe_key)
        client._log(target, row.get("name") or "", "← мастер", message,
                    kind="answer", direction="out", unread=0, operator=str(self.db.setting("telegram_chat_id", "") or ""))
        self.db.execute("UPDATE client_bot_log SET unread=0 WHERE chat_id=? AND direction='in'", (target,))
        try:
            self.db.execute("INSERT INTO audit_log(at,entity,entity_id,action,title,detail,data) VALUES(?,?,?,?,?,?,?)",
                            (now_iso(), "client_chat", target, "reply", "Ответ покупателю из рабочего Telegram",
                             message[:400], json.dumps({"actor": str(self.db.setting("telegram_chat_id", "") or "")}, ensure_ascii=False)))
        except Exception:
            pass
        self.db.add_event("bot", "Ответ покупателю из бота",
                          f"{row.get('name') or target}", "",
                          {"chat_id": target})
        return f"Отправлено ✓ {row.get('name') or target} · «{message[:60]}»"

    def _review_answer(self, raw: str) -> str:
        """КБ4: «отзыв ответ 555 <текст>» — ответ покупателю на плохой отзыв."""
        parts = raw.strip().split(None, 3)
        if len(parts) < 4 or not parts[2].isdigit():
            return ("Формат: «отзыв ответ 555 <текст>» — chat_id из "
                    "уведомления об отзыве.")
        target, message = parts[2], parts[3].strip()
        client = getattr(self.manager, "client_bot", None)
        if not client:
            return "Клиентский бот не запущен — ответьте в панели (Отзывы)."
        review = self.db.one(
            "SELECT * FROM client_reviews WHERE chat_id=?"
            " AND state IN ('needs_attention','rated')"
            " ORDER BY datetime(COALESCE(created_at, asked_at)) DESC LIMIT 1",
            (target,))
        if not review:
            return f"У чата {target} нет отзыва, ждущего ответа."
        order = self.db.one("SELECT number FROM orders WHERE id=?",
                            (review.get("order_id") or "",)) or {}
        dedupe = (f"reviewreply:{review['order_id']}:{self._current_update_id}"
                  if self._current_update_id else "")
        client._reply_keyed(target, message, client._menu(), dedupe_key=dedupe)
        row = self.db.one("SELECT name FROM client_chats WHERE chat_id=?",
                          (target,))
        client._log(target, (row or {}).get("name") or "", "← ответ на отзыв",
                    message, kind="answer", direction="out", unread=0,
                    operator=str(self.db.setting("telegram_chat_id", "") or ""))
        self.db.execute(
            "UPDATE client_reviews SET state='answered',operator_note=?,"
            "resolved_at=? WHERE order_id=? AND chat_id=?",
            (message[:500], now_iso(), review["order_id"], target))
        self.db.add_event("order", "Ответ на отзыв покупателю",
                          f"№{order.get('number') or ''}", message[:200],
                          {"chat_id": target})
        return (f"Отправлено ✓ Ответ на отзыв ушёл покупателю "
                f"{(row or {}).get('name') or target}.")

    def _client_template_button(self, command: str) -> str:
        """КБ2: кнопка-шаблон из уведомления — готовый ответ покупателю."""
        parts = command.split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            return "Кнопка устарела — напишите «кответ <chat> <текст>»."
        target, template_id = parts[1], parts[2]
        client = getattr(self.manager, "client_bot", None)
        if not client:
            return "Клиентский бот не запущен."
        own = [item for item in client.templates() if item.get("enabled", True)]
        template = next((item for item in own if item["id"] == template_id), None)
        if not template:
            template = next((item for item in client.default_templates()
                             if item["id"] == template_id), None)
        if not template:
            return f"Шаблон удалён — напишите «кответ {target} <текст>»."
        if bool(num((self.db.one(
                "SELECT banned n FROM client_chats WHERE chat_id=?",
                (target,)) or {}).get("n"))):
            return f"Чат {target} заблокирован — сначала «клиент разблок {target}»."
        message = str(template.get("text") or "")
        dedupe = (f"tpl:{self._current_update_id}:{target}:{template_id}"
                  if self._current_update_id else "")
        client._reply_keyed(target, message, client._menu(), dedupe_key=dedupe)
        row = self.db.one("SELECT name FROM client_chats WHERE chat_id=?",
                          (target,))
        client._log(target, (row or {}).get("name") or "",
                    f"← шаблон «{template['name']}»", message, kind="answer",
                    direction="out", unread=0,
                    operator=str(self.db.setting("telegram_chat_id", "") or ""))
        self.db.execute("UPDATE client_bot_log SET unread=0"
                        " WHERE chat_id=? AND direction='in'", (target,))
        return (f"Отправлено ✓ «{template['name']}» → "
                f"{(row or {}).get('name') or target}")

    def _client_payment_action(self, text: str, action: str, actor: str) -> str:
        """«оплата подтвердить/отклонить <заявка>» — ручная сверка СБП."""
        import json
        parts = text.split()
        ident = next((w for w in parts[2:] if w), "")
        if not ident:
            return "Формат: «оплата подтвердить 1001» или укажите id заявки из панели."
        intent = self.db.one(
            "SELECT p.*,o.number FROM client_payment_intents p LEFT JOIN orders o ON o.id=p.order_id"
            " WHERE p.status='pending' AND (p.id=? OR o.number=?) ORDER BY datetime(p.created_at) DESC LIMIT 1",
            (ident, ident))
        if not intent:
            return f"Ожидающая заявка оплаты «{ident}» не найдена."
        if action == "reject":
            self.db.execute("UPDATE client_payment_intents SET status='rejected',reject_reason=?,confirmed_at=?,confirmed_by=?,updated_at=? WHERE id=?",
                            ("Отклонено сотрудником Telegram", now_iso(), actor, now_iso(), intent["id"]))
            client = getattr(self.manager, "client_bot", None)
            if client:
                client._reply_keyed(intent["chat_id"],
                                    f"По заказу №{intent.get('number') or ''} оплату пока не подтвердили. Напишите мастеру, если это ошибка.",
                                    client._menu(), dedupe_key=f"payment:{intent['id']}:rejected")
            result = "Заявка оплаты отклонена."
        else:
            try:
                payment = self.manager.acc.add_payment(
                    intent["order_id"], num(intent.get("amount")), "payment", "",
                    "СБП (ручная сверка)", f"Подтверждено из рабочего Telegram: {intent['id']}",
                    request_id=f"client-intent:{intent['id']}")
            except ValueError as exc:
                return f"Не получилось подтвердить: {exc}"
            self.db.execute("UPDATE client_payment_intents SET status='confirmed',confirmed_at=?,confirmed_by=?,payment_id=?,updated_at=? WHERE id=?",
                            (now_iso(), actor, payment.get("id") or "", now_iso(), intent["id"]))
            client = getattr(self.manager, "client_bot", None)
            if client:
                client._reply_keyed(intent["chat_id"],
                                    f"Оплата по заказу №{intent.get('number') or ''} подтверждена мастером ✓",
                                    client._menu(), dedupe_key=f"payment:{intent['id']}:confirmed")
            result = f"Оплата по заказу №{intent.get('number') or ''} подтверждена и проведена."
        try:
            self.db.execute("INSERT INTO audit_log(at,entity,entity_id,action,title,detail,data) VALUES(?,?,?,?,?,?,?)",
                            (now_iso(), "payment_intent", intent["id"], action,
                             "Заявка оплаты обработана из Telegram", result,
                             json.dumps({"actor": actor}, ensure_ascii=False)))
        except Exception:
            pass
        return result

    def _client_bot_control(self, text: str) -> str:
        """КБ5: «клиент-бот пауза/старт/статус» — управление витриной с телефона."""
        parts = text.split()
        action = parts[1] if len(parts) > 1 else "статус"
        settings = self.db.settings()
        enabled = bool(settings.get("client_bot_enabled"))
        has_token = bool(settings.get("client_bot_token"))
        if action in ("пауза", "стоп", "выключить", "pause", "stop"):
            self.db.set_settings({"client_bot_enabled": False})
            self.db.add_event("bot", "Клиентский бот выключен из Telegram",
                              "«клиент-бот пауза»", "", {})
            return ("Клиентский бот выключен — покупателям бот не отвечает. "
                    "Включить: «клиент-бот старт».")
        if action in ("старт", "включить", "start"):
            if not has_token:
                return ("Сначала задайте токен клиентского бота в панели: "
                        "Настройки → Клиент-бот.")
            self.db.set_settings({"client_bot_enabled": True})
            self.db.add_event("bot", "Клиентский бот включён из Telegram",
                              "«клиент-бот старт»", "", {})
            return "Клиентский бот включён ✓ Каталог и заявки снова работают."
        bot = getattr(self.manager, "client_bot", None)
        alive = bool(bot and bot.last_poll
                     and time.time() - bot.last_poll < 120)
        return ("Клиентский бот: "
                f"{'включён' if enabled else 'выключен'} · "
                f"опрос {'жив' if alive else 'молчит'}\n"
                "«клиент-бот пауза» — выключить · «клиент-бот старт» — включить.")

    def _client_ban(self, text: str) -> str:
        """КБ6: «клиент блок 555» / «клиент разблок 555» — спам-фильтр чатов."""
        parts = text.split()
        if len(parts) < 3 or not parts[2].isdigit():
            return "Формат: «клиент блок 555» или «клиент разблок 555»."
        action, target = parts[1], parts[2]
        row = self.db.one("SELECT * FROM client_chats WHERE chat_id=?", (target,))
        if not row:
            return f"Чат {target} не найден среди покупателей."
        if action in ("блок", "бан", "блокировка"):
            self.db.execute("UPDATE client_chats SET banned=1 WHERE chat_id=?",
                            (target,))
            self.db.add_event("bot", "Чат покупателя заблокирован",
                              str(row.get("name") or target), "",
                              {"chat_id": target})
            return (f"Чат {target} заблокирован: бот молча игнорирует "
                    "сообщения и не шлёт уведомления. Разблок: "
                    f"«клиент разблок {target}».")
        self.db.execute("UPDATE client_chats SET banned=0 WHERE chat_id=?",
                        (target,))
        self.db.add_event("bot", "Чат покупателя разблокирован",
                          str(row.get("name") or target), "",
                          {"chat_id": target})
        return f"Чат {target} разблокирован — бот снова отвечает."

    # ---------------------------------------------------- текстовые команды
    def cmd_inbox(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._client_inbox())

    def cmd_client_answer(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._client_answer(raw))

    def cmd_review_answer(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._review_answer(raw))

    def cmd_client_bot(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._client_bot_control(text))

    def cmd_client(self, chat: str, raw: str, text: str) -> None:
        """«клиент блок/разблок <id>» — спам-фильтр; иначе — подсказка."""
        words = text.split()
        if len(words) > 1 and words[1] in ("блок", "разблок", "бан", "разбан",
                                           "блокировка"):
            return self._reply(chat, self._client_ban(text))
        self._reply(chat, "Клиенты: «клиенты» — список и карточки.\n"
                          "Спам-фильтр: «клиент блок 555» / «клиент разблок 555».")

    # ------------------------------------------------------- кнопки (callback)
    def cb_clients(self, chat: str, message_id: str, params: str) -> None:
        self.clients_keyboard(chat, message_id)

    def cb_client(self, chat: str, message_id: str, params: str) -> None:
        self.client_card(chat, params, message_id)

    def cb_client_reply(self, chat: str, message_id: str, params: str) -> None:
        self.client_reply_prompt(chat, params)

    def cb_inbox(self, chat: str, params: str) -> str:
        return self._client_inbox()

    def cb_client_template(self, chat: str, params: str) -> str:
        return self._client_template_button(f"cbot_tpl:{params}")
