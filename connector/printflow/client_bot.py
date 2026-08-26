"""Клиентский Telegram-бот NOZZA: витрина и заказы в кармане покупателя.

Отдельный бот с собственным токеном (client_bot_token) — внутренний бот
сотрудников и публичный бот покупателей не пересекаются. Работает на long
polling, как и внутренний: ни белого IP, ни проброса портов.

Что умеет покупатель:
• «каталог» — витрина с ценами и остатками (номенклатура со статусом товара);
• «заказ 3» или кнопка — заказ по номеру позиции, заказ попадает в панель
  как новая заявка (канал Telegram) и получает номер;
• «индивидуальный …» — заявка на печать по задаче: текст уходит в панель;
• «мои заказы» / «статус 1001» — статусы своих заказов;
• «телефон +7…» — привязать телефон, чтобы видеть заказы с полки и Авито;
• уведомления: статус заказа изменился — бот сам напишет (если включено).

Диалоги пишутся в client_bot_log и видны на вкладке «Клиент-бот» в панели.
Бот никогда не показывает цены чужих заказов, себестоимость и внутренние
данные — только имя изделия, статус и срок.
"""
from __future__ import annotations

import json
import re as _re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .accounting import num, uid
from .config import now_iso

API = "https://api.telegram.org/bot{token}/{method}"

HELP = """NOZZA — 3D-печать на заказ. Что я умею:

• каталог — что есть на полке, с ценами
• заказ 3 — заказать позицию №3 из каталога
• индивидуальный <задача> — печать по вашему заданию
• мои заказы — статусы ваших заказов
• статус 1001 — статус заказа по номеру
• телефон +7… — привязать телефон к заказам с полки
• помощь — это сообщение

Как считается индивидуальный заказ: пришлите задачу, размеры или фото —
мастер посчитает цену и срок, и вы получите ответ здесь же."""

WELCOME = ("Здравствуйте! Это бот мастерской NOZZA — «Там, где рождается форма».\n\n"
           "Здесь можно посмотреть готовые вещи на полке, заказать печать по своей "
           "задаче и следить за статусом заказа.\n\nНапишите «помощь» — покажу команды.")


def _money(value: float) -> str:
    return f"{round(num(value)):,}".replace(",", " ") + " ₽"


def _keyboard(*rows: list[tuple[str, str]]) -> dict:
    return {"inline_keyboard": [
        [{"text": text, "callback_data": data} for text, data in row] for row in rows]}


class ClientBot:
    """Фоновый слушатель клиентского бота. Активен при включённой настройке."""

    def __init__(self, manager):
        self.manager = manager
        self.db = manager.db
        self._stop = threading.Event()
        self._offset = 0
        self.last_poll = 0.0
        # заказы, о которых уже сообщили в этом чате: {(chat, order_id): status}
        self._notified: dict[tuple[str, str], str] = {}
        self._thread = threading.Thread(target=self._loop, name="pf-client-bot",
                                        daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- транспорт
    def shutdown(self) -> None:
        self._stop.set()

    def _settings(self) -> dict:
        return self.db.settings(include_secrets=True)

    def _call(self, method: str, params: dict, timeout: int = 35) -> dict:
        token = self._settings().get("client_bot_token", "")
        if not token:
            return {}
        url = API.format(token=token, method=method)
        data = urllib.parse.urlencode(params).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                        timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", "ignore"))
        except Exception:
            return {}

    def _reply(self, chat: str, text: str, buttons: dict | None = None) -> None:
        params: dict = {"chat_id": chat, "text": text[:3800],
                        "disable_web_page_preview": "true"}
        if buttons:
            params["reply_markup"] = json.dumps(buttons)
        self._call("sendMessage", params, timeout=15)

    def _log(self, chat: str, name: str, text: str, answer: str,
             kind: str = "message") -> None:
        try:
            self.db.execute(
                "INSERT INTO client_bot_log(at,chat_id,name,text,answer,kind)"
                " VALUES(?,?,?,?,?,?)",
                (now_iso(), chat, name or "", (text or "")[:500],
                 (answer or "")[:1000], kind))
        except Exception:
            pass  # журнал не должен ломать ответы клиенту

    def _touch_chat(self, chat: str, message: dict) -> dict:
        """Обновить/создать запись чата и вернуть её.

        Профиль может прийти пустым (например, у callback-сообщения от бота) —
        в этом случае сохранённые имя и username не затираем.
        """
        profile = message.get("from") or {}
        row = self.db.one("SELECT * FROM client_chats WHERE chat_id=?", (chat,))
        patch = {
            "chat_id": chat,
            "last_seen": now_iso(),
        }
        if str(profile.get("first_name") or profile.get("title") or ""):
            patch["name"] = str(profile.get("first_name") or profile.get("title"))
        if str(profile.get("username") or ""):
            patch["username"] = str(profile.get("username"))
        if row:
            patch.setdefault("name", row.get("name") or "")
            patch.setdefault("username", row.get("username") or "")
            patch["phone"] = row.get("phone") or ""
            patch["created_at"] = row.get("created_at") or now_iso()
        else:
            patch.setdefault("name", "")
            patch.setdefault("username", "")
            patch["created_at"] = now_iso()
        return self.db.upsert("client_chats", patch, key="chat_id")

    # ---------------------------------------------------------------- цикл
    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = self._settings()
            if not (settings.get("client_bot_enabled")
                    and settings.get("client_bot_token")):
                self._stop.wait(20)
                continue
            try:
                result = self._call("getUpdates", {
                    "offset": self._offset, "timeout": 25,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                })
                self.last_poll = time.time()
                for update in (result.get("result") or []):
                    self._offset = max(self._offset, num(update.get("update_id")) + 1)
                    self._handle(update)
                self._maybe_push_statuses()
            except Exception:
                self._stop.wait(10)

    # ------------------------------------------------------------ обработка
    def _handle(self, update: dict) -> None:
        callback = update.get("callback_query") or {}
        if callback:
            return self._handle_callback(callback)
        message = update.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        if not chat:
            return
        row = self._touch_chat(chat, message)
        text = (message.get("text") or "").strip()
        if not text:
            return
        try:
            answer = self._dispatch(chat, row, text)
            self._log(chat, row.get("name") or "", text, answer)
        except Exception as exc:
            answer = f"Не получилось: {exc}"
            self._reply(chat, answer)
            self._log(chat, row.get("name") or "", text, answer)

    def _handle_callback(self, callback: dict) -> None:
        message = callback.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        data = str(callback.get("data") or "")
        if not chat or not data:
            return
        self._call("answerCallbackQuery", {"callback_query_id":
                                           str(callback.get("id") or "")})
        row = self.db.one("SELECT * FROM client_chats WHERE chat_id=?", (chat,))
        if not row:
            # Профиль берём из callback (у сообщения от бота поля from нет).
            row = self._touch_chat(chat, {"from": callback.get("from") or {}})
        try:
            answer = self._run_callback(chat, row, data)
            self._log(chat, row.get("name") or "", data, answer, kind="status")
        except Exception as exc:
            answer = f"Не получилось: {exc}"
            self._reply(chat, answer)

    def _run_callback(self, chat: str, row: dict, data: str) -> str:
        if data.startswith("buy:"):
            return self._order_item(chat, row, data.split(":", 1)[1])
        if data == "catalog":
            return self.text_catalog()
        if data == "mine":
            return self.text_my_orders(chat, row)
        if data == "help":
            return HELP
        return "Не понял кнопку — напишите «помощь»."

    def _menu(self) -> dict:
        return _keyboard(
            [("🛍 Каталог", "catalog"), ("📦 Мои заказы", "mine")],
            [("❔ Помощь", "help")],
        )

    def _dispatch(self, chat: str, row: dict, raw: str) -> str:
        text = raw.lower().lstrip("/").replace("ё", "е")
        text = _re.sub(r"^nozza\S*\s+", "", text).strip()
        word = text.split()[0] if text else ""

        if word in ("start", "старт", "начать"):
            welcome = str(self._settings().get("client_bot_welcome") or "").strip()
            self._reply(chat, welcome or WELCOME, self._menu())
            return welcome or WELCOME
        if word in ("help", "помощь", "?", "меню"):
            self._reply(chat, HELP, self._menu())
            return HELP
        if word in ("каталог", "товары", "витрина", "полка", "catalog"):
            answer = self.text_catalog()
            self._reply(chat, answer, self._catalog_keyboard())
            return answer
        if word in ("мои", "мои заказы", "заказы") or text == "мои заказы":
            return self.text_my_orders(chat, row)
        if word in ("статус", "status", "заказ", "order"):
            number = next((w for w in text.split() if w.isdigit()), "")
            if word in ("заказ", "order") and number:
                return self._order_item(chat, row, number)
            return self.text_order_status(chat, row, number)
        if text.startswith("индивидуальн") or word in ("печать", "задача"):
            return self._custom_order(chat, row, raw)
        if word in ("телефон", "phone", "номер") or text.startswith("+7") or text.startswith("8 9"):
            return self._save_phone(chat, row, text)
        contact = str(self._settings().get("company_name", "NOZZA") or "NOZZA")
        answer = ("Не понял сообщение. Напишите «помощь» — покажу, что я умею. "
                  f"Мастерская {contact} ответит и без команд.")
        self._reply(chat, answer, self._menu())
        return answer

    # -------------------------------------------------------------- тексты
    def _catalog_rows(self) -> list[dict]:
        """Позиции витрины: товары номенклатуры с ценой, по порядку."""
        try:
            from .nomenclature import Nomenclature
            items = Nomenclature(self.db).items(kind="product")
        except Exception:
            items = []
        return [item for item in items if num(item.get("price")) > 0]

    def text_catalog(self) -> str:
        if not bool(self._settings().get("client_bot_catalog", True)):
            return ("Каталог по запросу отключён. Напишите, что ищете — "
                    "мастер подскажет и посчитает.")
        rows = self._catalog_rows()
        if not rows:
            return ("На полке сейчас нет опубликованных позиций. "
                    "Напишите «индивидуальный» + задача — посчитаем печать под вас.")
        lines = ["🛍 Каталог NOZZA:", ""]
        for i, item in enumerate(rows, 1):
            qty = num(item.get("qty"))
            tail = " · есть на полке" if qty > 0 else " · под заказ"
            lines.append(f"{i}. {item.get('name')} — {_money(item.get('price'))}{tail}")
        lines += ["", "Заказ: «заказ <номер>» или кнопкой под каталогом."]
        return "\n".join(lines)

    def _catalog_keyboard(self) -> dict:
        rows = self._catalog_rows()
        buttons = []
        for i, item in enumerate(rows[:18], 1):
            buttons.append((f"Заказать · {item.get('name')}"[:60], f"buy:{item['id']}"))
        keys = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        keys.append([("📦 Мои заказы", "mine")])
        return _keyboard(*keys) if keys else self._menu()

    def _order_item(self, chat: str, row: dict, nom_id: str) -> str:
        """Заказ позиции каталога: создаём заявку в панели и связываем чат."""
        rows = self._catalog_rows()
        item = next((r for r in rows if r["id"] == nom_id), None)
        if item is None and nom_id.isdigit():
            index = int(nom_id)
            if 1 <= index <= len(rows):
                item = rows[index - 1]
        if not item:
            return ("Такой позиции в каталоге нет. Напишите «каталог» и "
                    "закажите по номеру из списка (например, «заказ 2»). "
                    "Если вы про номер заказа — «статус <номер>».")
        qty = 1
        order = self.manager.repo.save_order({
            "product": item.get("name") or "",
            "customer_name": row.get("name") or "Покупатель",
            "phone": row.get("phone") or "",
            "messenger": (f"tg:@{row.get('username')}" if row.get("username")
                          else f"tg:{chat}"),
            "channel": "telegram",
            "status": "new",
            "qty": qty,
            "material": item.get("material") or "",
            "grams": num(item.get("grams")),
            "hours": num(item.get("hours")),
            "price": round(num(item.get("price")) * qty, 2),
            "notes": "Заказ из клиентского бота",
            "nom_id": item.get("id"),
        })
        number = order.get("number") or ""
        self.db.execute(
            "INSERT OR IGNORE INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES(?,?,?,?)", (chat, order.get("id"), number, now_iso()))
        self._notified[(chat, order.get("id", ""))] = "new"
        self.db.add_event("lead", "Заказ из клиентского бота",
                          f"№{number} · {item.get('name')}",
                          data={"order_id": order.get("id"), "chat_id": chat})
        # Мастеру — уведомление, покупателю — подтверждение с номером.
        try:
            self.manager.notify_async(
                f"🛎 Новый заказ из клиентского бота\n№{number} · "
                f"{item.get('name')} · {_money(num(item.get('price')))}",
                critical=True)
        except Exception:
            pass
        return (f"Заказ принят ✓\nНомер заказа — №{number}.\n"
                "Мастер подтвердит цену и срок. Статус: «мои заказы».")

    def _custom_order(self, chat: str, row: dict, raw: str) -> str:
        """«индивидуальный держатель для …» — заявка на печать по задаче."""
        task = _re.sub(r"^\s*индивидуальн\w*\s*[:\-]?\s*", "", raw.strip(),
                       flags=_re.IGNORECASE)
        task = task.strip() or "печать по заданию покупателя"
        order = self.manager.repo.save_order({
            "product": task[:120],
            "customer_name": row.get("name") or "Покупатель",
            "phone": row.get("phone") or "",
            "messenger": (f"tg:@{row.get('username')}" if row.get("username")
                          else f"tg:{chat}"),
            "channel": "telegram",
            "status": "new",
            "qty": 1,
            "notes": "Заявка из клиентского бота (индивидуальная)",
        })
        number = order.get("number") or ""
        self.db.execute(
            "INSERT OR IGNORE INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES(?,?,?,?)", (chat, order.get("id"), number, now_iso()))
        self._notified[(chat, order.get("id", ""))] = "new"
        self.db.add_event("lead", "Заявка из клиентского бота",
                          f"№{number} · {task[:80]}",
                          data={"order_id": order.get("id"), "chat_id": chat})
        try:
            self.manager.notify_async(
                f"🛎 Индивидуальная заявка из бота\n№{number} · {task[:200]}",
                critical=True)
        except Exception:
            pass
        return ("Заявка принята ✓ Номер — №"
                f"{number}.\nДля расчёта пришлите сюда размеры (Д×Ш×В), "
                "назначение и фото/эскиз. Мастер ответит с ценой и сроком.")

    def _save_phone(self, chat: str, row: dict, text: str) -> str:
        match = _re.search(r"(\+?\d[\d\s()\-]{8,17}\d)", text)
        if not match:
            return ("Формат: «телефон +7 978 000-00-00». По номеру найдутся "
                    "заказы, оформленные на полке и в переписке.")
        phone = _re.sub(r"[^\d+]", "", match.group(1))
        self.db.execute("UPDATE client_chats SET phone=? WHERE chat_id=?",
                        (phone, chat))
        row["phone"] = phone  # та же сессия диалога уже видит заказы по телефону
        return f"Телефон {phone} привязан. Напишите «мои заказы» — покажу всё."

    def _linked_orders(self, chat: str, row: dict) -> list[dict]:
        """Заказы чата: прямые ссылки + совпадение по телефону."""
        orders: dict[str, dict] = {}
        for link in self.db.query(
                "SELECT order_id FROM client_orders WHERE chat_id=?", (chat,)):
            order = self.db.one("SELECT * FROM orders WHERE id=?",
                                (link["order_id"],))
            if order:
                orders[order["id"]] = order
        phone = str(row.get("phone") or "").strip()
        if phone:
            # Телефоны в заказах пишут по-разному (+7…, 8…, с дефисами),
            # поэтому сравниваем последние 10 цифр в Python, а не через LIKE.
            digits = _re.sub(r"\D", "", phone)[-10:]
            if len(digits) == 10:
                for order in self.db.query(
                        "SELECT * FROM orders WHERE phone IS NOT NULL AND phone!=''"
                        " ORDER BY datetime(created_at) DESC LIMIT 50"):
                    order_digits = _re.sub(r"\D", "", str(order.get("phone") or ""))
                    if order_digits.endswith(digits):
                        orders.setdefault(order["id"], order)
        return sorted(orders.values(),
                      key=lambda o: str(o.get("created_at") or ""), reverse=True)

    def _status_label(self, order: dict) -> str:
        status = self.db.one("SELECT name FROM statuses WHERE id=?",
                             (order.get("status") or "",))
        return (status or {}).get("name") or str(order.get("status") or "новый")

    def text_my_orders(self, chat: str, row: dict) -> str:
        orders = self._linked_orders(chat, row)
        if not orders:
            return ("Ваших заказов не нашёл. Если заказывали на полке или в "
                    "переписке — пришлите «телефон +7…» и повторите.")
        lines = ["📦 Ваши заказы:", ""]
        for order in orders[:10]:
            due = f" · к {str(order.get('due') or '')[:10]}" if order.get("due") else ""
            lines.append(f"№{order.get('number')} · {order.get('product')}"
                         f" — {self._status_label(order)}{due}")
        if len(orders) > 10:
            lines.append(f"…и ещё {len(orders) - 10}")
        lines.append("\nПодробнее: «статус <номер>».")
        return "\n".join(lines)

    def text_order_status(self, chat: str, row: dict, number: str) -> str:
        if not number:
            return "Формат: «статус 1001» — номер заказа из подтверждения."
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден. Проверьте номер из подтверждения."
        allowed = any(o["id"] == order["id"] for o in self._linked_orders(chat, row))
        if not allowed:
            return ("Это не ваш заказ. Если вы заказывали его на полке — "
                    "пришлите «телефон +7…», привяжу номер.")
        due = f"\nОжидаем к: {str(order.get('due'))[:10]}" if order.get("due") else ""
        qty = num(order.get("qty"))
        return (f"Заказ №{order.get('number')}\n{order.get('product')}\n"
                f"Статус: {self._status_label(order)}"
                + (f"\nКоличество: {qty:g}" if qty > 1 else "") + due)

    # ------------------------------------------------------------ уведомления
    def _maybe_push_statuses(self) -> None:
        """Сообщить покупателям об изменении статуса их заказов."""
        if not bool(self._settings().get("client_bot_notify", True)):
            return
        links = self.db.query(
            "SELECT l.chat_id, l.order_id FROM client_orders l"
            " JOIN orders o ON o.id=l.order_id"
            " WHERE o.status NOT IN (SELECT id FROM statuses WHERE is_final=1)"
            "    OR o.updated_at >= datetime('now', '-2 days')")
        for link in links:
            chat, order_id = link["chat_id"], link["order_id"]
            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if not order:
                continue
            status = str(order.get("status") or "")
            if self._notified.get((chat, order_id)) == status:
                continue
            first_push = (chat, order_id) not in self._notified
            self._notified[(chat, order_id)] = status
            if first_push:
                continue  # о создании заказа уже сказали при приёме
            self._reply(chat, f"Заказ №{order.get('number')}"
                              f" «{order.get('product')}» — "
                              f"{self._status_label(order)}")
            self._log(chat, "", "→ push", f"№{order.get('number')}: {status}",
                      kind="push")

    # ------------------------------------------------------------- для панели
    def stats(self) -> dict:
        def count(sql: str, params: tuple = ()) -> int:
            try:
                return int(num(self.db.one(sql, params)["n"]))
            except Exception:
                return 0
        day = datetime.now().strftime("%Y-%m-%d")
        return {
            "chats": count("SELECT COUNT(*) n FROM client_chats"),
            "orders": count("SELECT COUNT(*) n FROM client_orders"),
            "messages": count("SELECT COUNT(*) n FROM client_bot_log"),
            "messages_today": count(
                "SELECT COUNT(*) n FROM client_bot_log WHERE at LIKE ?", (day + "%",)),
            "last_poll": self.last_poll,
        }

    def me(self) -> dict:
        """Проверка токена: getMe — панель показывает имя бота."""
        result = self._call("getMe", {}, timeout=10)
        user = (result.get("result") or {})
        if not user:
            return {"ok": False, "error": "Токен не принят или сети нет"}
        return {"ok": True, "username": user.get("username") or "",
                "name": user.get("first_name") or ""}
