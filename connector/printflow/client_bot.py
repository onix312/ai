"""Клиентский Telegram-бот NOZZA: витрина и заказы в кармане покупателя.

Отдельный бот с собственным токеном (client_bot_token) — внутренний бот
сотрудников и публичный бот покупателей не пересекаются. Работает на long
polling, как и внутренний: ни белого IP, ни проброса портов.

Что умеет покупатель:
• «каталог» — витрина с ценами и остатками, страницами и карточками товаров
  (у позиций с фото карточка приходит картинкой);
• «заказ 3» или кнопка — заказ по номеру позиции, заказ попадает в панель
  как новая заявка (канал Telegram) и получает номер;
• «индивидуальный …» — заявка на печать по задаче: текст уходит в панель;
• фото — заявка с референсом: снимок скачивается, крепится к заказу и
  уходит мастеру; подпись к фото работает как текст команды;
• «мои заказы» / «статус 1001» — статусы своих заказов (тоже кнопкой),
  к своей карточке — кнопки «Оплатить» и «Статус онлайн»;
• «телефон +7…» или кнопка «Отправить номер» — привязать телефон, чтобы
  видеть заказы с полки и Авито;
• «вопрос-ответ» — материалы, сроки и как заказать;
• уведомления: статус изменился — бот напишет сам; после выдачи спросит
  отзыв, а «готовый» заказ мягко напомнит о себе, если его не забрали.

Вопросы мимо команд не теряются: мастер получает уведомление и отвечает
из внутреннего бота командой «кответ <chat_id> <текст>».

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

• каталог — что есть на полке, с ценами и кнопками
• заказ 3 — заказать позицию №3 из каталога
• индивидуальный <задача> — печать по вашему заданию (можно фото)
• мои заказы — статусы ваших заказов
• статус 1001 — статус заказа по номеру
• телефон +7… — привязать телефон к заказам с полки
• вопрос-ответ — материалы, сроки, как заказать
• помощь — это сообщение

Как считается индивидуальный заказ: пришлите задачу, размеры или фото —
мастер посчитает цену и срок, и вы получите ответ здесь же. Кнопки под
сообщениями работают так же, как команды."""

# Частые вопросы: только подтверждённые формулировки из контекста «О нас» —
# без обещаний свойств материалов и сроков, которых нет.
FAQ_TEXT = """❓ Частые вопросы

Материалы. Базовые — PLA и PETG, выбор зависит от задачи: PLA — для сухого
интерьера, PETG — для более функциональных и влажных задач.

Сроки. Обычный ориентир после согласования — 1–3 дня; точный срок зависит
от модели, размера, материала и очереди.

Цена. Считаем до печати и не меняем задним числом. Расчёт бесплатный:
пришлите фото, размеры и назначение — мастер ответит прямо здесь.

Как заказать. Напишите «индивидуальный» + задача или выберите готовую
вещь в «каталоге». Для заказов с полки пригодится «телефон +7…».

Чего мы не делаем. Не выдаём печать за гарантированно безопасное решение
для еды, детей, медицины и ответственных узлов без отдельной проверки."""

SHARE_TEXT = "Каталог NOZZA — 3D-печать под задачу"
PAGE_SIZE = 8  # позиций каталога на странице (кнопки-карточки, одна в ряд)

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
        # анти-спам уведомлений мастеру: {chat: время последней}
        self._master_alerts: dict[str, float] = {}
        # чаты, где ждём описание проблемы после оценки «не очень»:
        # {chat: (order_id, до какого времени)}
        self._await_problem: dict[str, tuple[str, float]] = {}
        self._bot_username = ""      # @username бота, для кнопки «Поделиться»
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

    def _send_photo(self, chat: str, caption: str, raw: bytes,
                    buttons: dict | None = None) -> None:
        """Карточка товара картинкой: sendPhoto с подписью и кнопками.

        Без фото (или без токена) молча превращаемся в обычный текст —
        карточка остаётся рабочей даже без картинки."""
        token = self._settings().get("client_bot_token", "")
        if not raw or not token:
            return self._reply(chat, caption, buttons)
        boundary = f"pfcb{int(time.time() * 1000)}"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data;"
                         f' name="{name}"\r\n\r\n{value}\r\n'.encode())

        field("chat_id", chat)
        field("caption", caption[:1000])
        if buttons:
            field("reply_markup", json.dumps(buttons))
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data;"
                     f' name="photo"; filename="item.jpg"\r\n'
                     f"Content-Type: image/jpeg\r\n\r\n".encode())
        parts.append(raw)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        url = API.format(token=token, method="sendPhoto")
        try:
            request = urllib.request.Request(
                url, data=b"".join(parts),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urllib.request.urlopen(request, timeout=20):
                pass
        except Exception:
            self._reply(chat, caption, buttons)  # сеть подвела — текст лучше тишины

    def _download_file(self, file_id: str) -> bytes | None:
        """Скачать файл покупателя (getFile → скачивание), как во внутреннем боте."""
        token = self._settings().get("client_bot_token", "")
        if not token or not file_id:
            return None
        try:
            info = self._call("getFile", {"file_id": file_id})
            path = str((info.get("result") or {}).get("file_path") or "")
            if not path:
                return None
            url = f"https://api.telegram.org/file/bot{token}/{path}"
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read()
        except Exception:
            return None

    def _username(self) -> str:
        """@username бота для кнопки «Поделиться» (getMe, кэшируется)."""
        if not self._bot_username:
            me = self.me()
            if me.get("ok"):
                self._bot_username = str(me.get("username") or "")
        return self._bot_username

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
                self._maybe_ask_reviews()
                self._maybe_remind_pickup()
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
        # Подпись к фото работает как текст: покупатель шлёт снимок эскиза
        # с подписью «индивидуальный …» — это уже готовая заявка.
        caption = (message.get("caption") or "").strip()
        text = (message.get("text") or "").strip()
        try:
            if message.get("photo"):
                answer, buttons = self._photo_reply(chat, row, message, caption)
            elif text:
                answer, buttons = self._dispatch(chat, row, text)
            else:
                answer, buttons = self._no_text_reply(chat, row, message)
            self._reply(chat, answer, buttons)
            self._log(chat, row.get("name") or "",
                      text or (f"📷 {caption}" if caption else
                               self._media_label(message)), answer)
        except Exception as exc:
            answer = f"Не получилось: {exc}"
            self._reply(chat, answer)
            self._log(chat, row.get("name") or "", text or caption, answer)

    def _photo_reply(self, chat: str, row: dict, message: dict,
                     caption: str) -> tuple[str, dict | None]:
        """Фото покупателя — заявка с референсом.

        Снимок скачивается, крепится к заказу через order_photos и уходит
        мастеру в уведомлении: он видит задачу глазами покупателя.
        """
        file_id = str(((message.get("photo") or [{}])[-1] or {}).get("file_id") or "")
        raw = self._download_file(file_id) if file_id else None
        low = caption.lower().replace("ё", "е").strip()
        if low.startswith("индивидуальн"):
            answer = self._custom_order(chat, row, caption)
            order = self._latest_linked(chat)
            if order:
                self._attach_photo(order["id"], raw, chat)
            return answer, self._menu()
        # «фото 1001» — приложить снимок к своему заказу
        number = next((w for w in low.split() if w.isdigit()), "")
        if number:
            order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
            if order and any(o["id"] == order["id"]
                             for o in self._linked_orders(chat, row)):
                self._attach_photo(order["id"], raw, chat)
                return (f"Фото получил ✓ Прикрепил к заказу №{number} — "
                        "мастер увидит его в карточке."), self._menu()
            return ("Такой заказ не нашёл среди ваших. Номер — из подтверждения, "
                    "например «фото 1001».", self._menu())
        # без подписи: крепим к последнему активному заказу чата, а если
        # активных нет — заводим лид-фотозаявку и просим описать задачу
        active = [o for o in self._linked_orders(chat, row)
                  if not self._is_final(o)]
        if active:
            self._attach_photo(active[0]["id"], raw, chat)
            return (f"Фото получил ✓ Прикрепил к заказу "
                    f"№{active[0].get('number')}. Если это новая задача — "
                    "напишите «индивидуальный» + описание."), self._menu()
        order = self.manager.repo.save_order({
            "product": "Фотозаявка — описание уточняется",
            "customer_name": row.get("name") or "Покупатель",
            "phone": row.get("phone") or "",
            "messenger": (f"tg:@{row.get('username')}" if row.get("username")
                          else f"tg:{chat}"),
            "channel": "telegram", "status": "new", "qty": 1,
            "notes": "Фотозаявка из клиентского бота (фото без описания)",
        })
        number = order.get("number") or ""
        self.db.execute(
            "INSERT OR IGNORE INTO client_orders(chat_id,order_id,number,created_at)"
            " VALUES(?,?,?,?)", (chat, order.get("id"), number, now_iso()))
        self._notified[(chat, order.get("id", ""))] = "new"
        self._attach_photo(order.get("id"), raw, chat, lead=True)
        return ("Фото получил ✓ Мастерская уже видит снимок. Опишите задачу — "
                "что напечатать, размеры и назначение — и мастер посчитает "
                "цену и срок.", self._menu())

    def _attach_photo(self, order_id: str, raw: bytes | None, chat: str,
                      lead: bool = False) -> None:
        """Сохранить снимок покупателя в order_photos и показать мастеру."""
        from .config import PHOTO_DIR
        name = ""
        if raw:
            try:
                PHOTO_DIR.mkdir(parents=True, exist_ok=True)
                name = f"client_{order_id}_{int(time.time() * 1000)}.jpg"
                (PHOTO_DIR / name).write_bytes(raw)
            except Exception:
                name = ""
        if name:
            self.db.upsert("order_photos", {
                "id": uid("ph"), "order_id": order_id, "at": now_iso(),
                "file": name, "note": "фото из клиентского бота",
                "kind": "client"})
        row = self.db.one("SELECT number FROM orders WHERE id=?", (order_id,))
        number = (row or {}).get("number") or ""
        try:
            self.manager.notify_async(
                f"📷 Фото от покупателя — заказ №{number}\n"
                f"Чат {chat} · ответить: кответ {chat} <текст>",
                photo=raw or None, critical=bool(lead))
        except Exception:
            pass

    def _latest_linked(self, chat: str) -> dict | None:
        order_id = (self.db.one(
            "SELECT order_id FROM client_orders WHERE chat_id=?"
            " ORDER BY datetime(created_at) DESC LIMIT 1", (chat,)) or {}
        ).get("order_id")
        return self.db.one("SELECT * FROM orders WHERE id=?", (order_id,)) \
            if order_id else None

    def _is_final(self, order: dict) -> bool:
        meta = self.db.one("SELECT is_final FROM statuses WHERE id=?",
                           (order.get("status") or "",))
        return bool(meta and num(meta.get("is_final")))

    def _no_text_reply(self, chat: str, row: dict,
                       message: dict) -> tuple[str, dict | None]:
        """Ответ на сообщение без текста: контакт привязываем, медиа — подсказка.

        Раньше фото и голосовые молча игнорировались, хотя помощь просит
        «пришлите фото» — покупатель решал, что бот сломался.
        """
        contact = message.get("contact") or {}
        phone = str(contact.get("phone_number") or "")
        if phone:
            return self._save_phone(chat, row, f"телефон {phone}"), self._menu()
        label = self._media_label(message)
        return (f"{label.capitalize()} получил ✓ Опишите задачу текстом — "
                "«индивидуальный держатель 120 мм» — и мастер посчитает "
                "цену и срок.", self._menu())

    def _media_label(self, message: dict) -> str:
        """Короткое имя вложения для ответа и журнала диалогов."""
        for key, label in (("photo", "фото"), ("document", "файл"),
                           ("video", "видео"), ("video_note", "видео"),
                           ("voice", "голосовое"), ("sticker", "стикер"),
                           ("location", "локацию"), ("contact", "контакт")):
            if message.get(key):
                return label
        return "сообщение"

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
            if data.startswith("item:"):
                # карточка товара может уйти картинкой — отдельный путь отправки
                self._send_item_card(chat, data)
                answer = "карточка товара"
            else:
                answer, buttons = self._run_callback(chat, row, data)
                # Кнопка обязана ответить: раньше текст только писался в журнал,
                # и покупатель не видел реакции — «кнопки не работают».
                self._reply(chat, answer, buttons)
            self._log(chat, row.get("name") or "", data, answer, kind="status")
        except Exception as exc:
            answer = f"Не получилось: {exc}"
            self._reply(chat, answer)
            self._log(chat, row.get("name") or "", data, answer, kind="status")

    def _run_callback(self, chat: str, row: dict,
                      data: str) -> tuple[str, dict | None]:
        if data.startswith("buy:"):
            return self._order_item(chat, row, data.split(":", 1)[1]), \
                self._menu()
        if data.startswith("status:"):
            number = data.split(":", 1)[1]
            return self._order_card(chat, row, number)
        if data.startswith("pay:"):
            return self._pay_card(chat, row, data.split(":", 1)[1])
        if data.startswith("paid:"):
            return self._paid_notice(chat, row, data.split(":", 1)[1])
        if data.startswith("review:"):
            _, order_id, rating = data.split(":", 2)
            return self._review_rating(chat, row, order_id, rating)
        if data.startswith("catalog:"):
            page = max(1, int(num(data.split(":", 1)[1])))
            return self.text_catalog(page), self._catalog_keyboard(page)
        if data == "catalog":
            return self.text_catalog(), self._catalog_keyboard()
        if data == "mine":
            return self.text_my_orders(chat, row), self._orders_keyboard(chat, row)
        if data == "faq":
            return self.text_faq(), self._menu()
        if data == "help":
            return HELP, self._menu()
        return "Не понял кнопку — напишите «помощь».", self._menu()

    def _menu(self) -> dict:
        return _keyboard(
            [("🛍 Каталог", "catalog"), ("📦 Мои заказы", "mine")],
            [("❓ Вопрос-ответ", "faq"), ("❔ Помощь", "help")],
        )

    def _contact_keyboard(self) -> dict:
        """Обычная (не inline) клавиатура с кнопкой отправки контакта:
        Telegram сам предлагает номер телефона одним касанием."""
        return {"keyboard": [[{"text": "📱 Отправить номер",
                               "request_contact": True}]],
                "resize_keyboard": True, "one_time_keyboard": True}

    def _dispatch(self, chat: str, row: dict, raw: str) -> tuple[str, dict | None]:
        """Разбор текстовой команды. Возвращает текст и кнопки; отправляет
        только вызывающий (_handle) — одна точка отправки вместо пяти."""
        text = raw.lower().lstrip("/").replace("ё", "е")
        text = _re.sub(r"^nozza\S*\s+", "", text).strip()
        word = text.split()[0] if text else ""

        if word in ("start", "старт", "начать"):
            welcome = str(self._settings().get("client_bot_welcome") or "").strip()
            return welcome or WELCOME, self._menu()
        if word in ("help", "помощь", "?", "меню"):
            return HELP, self._menu()
        if word in ("faq", "вопрос", "вопросы", "вопрос-ответ"):
            return self.text_faq(), self._menu()
        if word in ("каталог", "товары", "витрина", "полка", "catalog"):
            return self.text_catalog(), self._catalog_keyboard()
        if word in ("мои", "мои заказы", "заказы") or text == "мои заказы":
            answer = self.text_my_orders(chat, row)
            # заказов нет и телефон не привязан — вместо пустого списка даём
            # кнопку отправки контакта, чтобы связать заказы с полки
            if not self._linked_orders(chat, row) \
                    and not str(row.get("phone") or "").strip():
                return answer, self._contact_keyboard()
            return answer, self._orders_keyboard(chat, row)
        if word in ("статус", "status", "заказ", "order"):
            number = next((w for w in text.split() if w.isdigit()), "")
            if word in ("заказ", "order") and number:
                return self._order_item(chat, row, number), self._menu()
            return self._order_card(chat, row, number)
        if text.startswith("индивидуальн") or word in ("печать", "задача"):
            return self._custom_order(chat, row, raw), self._menu()
        if word in ("телефон", "phone", "номер") or text.startswith("+7") or text.startswith("8 9"):
            answer = self._save_phone(chat, row, text)
            # пока телефон не привязан — предлагаем отправить контакт кнопкой,
            # это одно касание вместо набора номера
            buttons = self._menu() if row.get("phone") else self._contact_keyboard()
            return answer, buttons
        contact = str(self._settings().get("company_name", "NOZZA") or "NOZZA")
        # Ждём описание проблемы после оценки «не очень»? Это не «не понял»,
        # а продолжение отзыва — сохраняем комментарий и передаём мастеру.
        pending = self._await_problem.get(chat)
        if pending and time.time() < pending[1]:
            return self._review_comment(chat, row, pending[0], raw), self._menu()
        if pending:
            self._await_problem.pop(chat, None)
        answer = ("Не понял сообщение. Напишите «помощь» — покажу, что я умею. "
                  f"Мастерская {contact} ответит и без команд.")
        self._alert_master(chat, row, raw)
        return answer, self._menu()

    def _alert_master(self, chat: str, row: dict, text: str) -> None:
        """Вопрос мимо команд — уведомление команде, чтобы лид не умер в журнале.

        Редкая болтовня не должна превращаться в спам: один чат будит мастера
        не чаще раза в 10 минут.
        """
        last = self._master_alerts.get(chat, 0.0)
        if time.time() - last < 600:
            return
        self._master_alerts[chat] = time.time()
        name = row.get("name") or "Без имени"
        try:
            self.manager.notify_async(
                f"💬 Покупатель ждёт ответа\n{name} · чат {chat}\n"
                f"«{text[:300]}»\nОтветить: кответ {chat} <текст>")
        except Exception:
            pass

    # -------------------------------------------------------------- тексты
    def _catalog_rows(self) -> list[dict]:
        """Позиции витрины: товары номенклатуры с ценой, по порядку."""
        try:
            from .nomenclature import Nomenclature
            items = Nomenclature(self.db).items(kind="product")
        except Exception:
            items = []
        return [item for item in items if num(item.get("price")) > 0]

    def text_catalog(self, page: int = 1) -> str:
        if not bool(self._settings().get("client_bot_catalog", True)):
            return ("Каталог по запросу отключён. Напишите, что ищете — "
                    "мастер подскажет и посчитает.")
        rows = self._catalog_rows()
        if not rows:
            return ("На полке сейчас нет опубликованных позиций. "
                    "Напишите «индивидуальный» + задача — посчитаем печать под вас.")
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(1, page), pages)
        start = (page - 1) * PAGE_SIZE
        chunk = rows[start:start + PAGE_SIZE]
        lines = [f"🛍 Каталог NOZZA · страница {page} из {pages}:", ""]
        for i, item in enumerate(chunk, start + 1):
            qty = num(item.get("qty"))
            tail = " · есть на полке" if qty > 0 else " · под заказ"
            lines.append(f"{i}. {item.get('name')} — {_money(item.get('price'))}{tail}")
        lines += ["", f"Показаны {start + 1}–{start + len(chunk)} из {len(rows)}. "
                  "Заказ: «заказ <номер>» или кнопкой-карточкой."]
        return "\n".join(lines)

    def _catalog_keyboard(self, page: int = 1) -> dict:
        # Витрину могли выключить настройкой — тогда кнопок заказа не даём,
        # иначе каталог «закрыт», а кнопки под ним всё ещё продают.
        if not bool(self._settings().get("client_bot_catalog", True)):
            return self._menu()
        rows = self._catalog_rows()
        if not rows:
            return self._menu()
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(1, page), pages)
        start = (page - 1) * PAGE_SIZE
        keys: list[list[dict]] = []
        for item in rows[start:start + PAGE_SIZE]:
            qty = num(item.get("qty"))
            tail = " · есть" if qty > 0 else " · под заказ"
            keys.append([{"text": f"🛍 {item.get('name')} · "
                                  f"{_money(item.get('price'))}{tail}"[:64],
                          "callback_data": f"item:{item['id']}:{page}"}])
        if pages > 1:
            nav: list[dict] = []
            if page > 1:
                nav.append({"text": "⬅️", "callback_data": f"catalog:{page - 1}"})
            nav.append({"text": f"{page}/{pages}", "callback_data": f"catalog:{page}"})
            if page < pages:
                nav.append({"text": "➡️", "callback_data": f"catalog:{page + 1}"})
            keys.append(nav)
        keys.append([{"text": "📦 Мои заказы", "callback_data": "mine"},
                     {"text": "❓ Вопрос-ответ", "callback_data": "faq"}])
        share = self._share_button()
        if share:
            keys.append([share])
        return {"inline_keyboard": keys}

    def _share_button(self) -> dict | None:
        """Кнопка «Поделиться»: системный экран пересылки со ссылкой на бота."""
        username = self._username()
        if not username:
            return None
        link = f"https://t.me/{username}"
        url = ("https://t.me/share/url?url=" + urllib.parse.quote(link, safe="")
               + "&text=" + urllib.parse.quote(SHARE_TEXT, safe=""))
        return {"text": "📤 Поделиться каталогом", "url": url}

    def _item_card_payload(self, nom_id: str,
                           page: int = 1) -> tuple[str, bytes, dict]:
        """Текст, фото и кнопки карточки товара каталога."""
        item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        rows = self._catalog_rows()
        index = next((i for i, r in enumerate(rows, 1) if r["id"] == nom_id), 0)
        price = num((item or {}).get("price"))
        if price <= 0:
            price = num(next((r.get("price") for r in rows if r["id"] == nom_id), 0))
        qty = num((item or {}).get("qty"))
        caption = [f"🛍 {item.get('name') if item else 'Товар'}"]
        caption.append(f"Цена: {_money(price)}")
        caption.append("Есть на полке" if qty > 0 else "Под заказ")
        if (item or {}).get("material"):
            caption.append(f"Материал: {item.get('material')}")
        if index:
            caption.append(f"Заказ: кнопкой ниже или «заказ {index}»")
        keys = [
            [{"text": "🛒 Заказать", "callback_data": f"buy:{nom_id}"}],
            [{"text": "⬅️ Каталог", "callback_data": f"catalog:{page}"}],
        ]
        raw = b""
        if (item or {}).get("photo"):
            from .config import PHOTO_DIR
            path = PHOTO_DIR / str(item["photo"])
            if path.is_file():
                try:
                    raw = path.read_bytes()
                except Exception:
                    raw = b""
        return "\n".join(caption), raw, {"inline_keyboard": keys}

    def _send_item_card(self, chat: str, data: str) -> None:
        """Отправить карточку товара (фото, если есть) без текстового дубля."""
        parts = data.split(":")
        nom_id = parts[1] if len(parts) > 1 else ""
        page = max(1, int(num(parts[2]))) if len(parts) > 2 else 1
        caption, raw, buttons = self._item_card_payload(nom_id, page)
        if raw:
            self._send_photo(chat, caption, raw, buttons)
        else:
            self._reply(chat, caption, buttons)

    def text_faq(self) -> str:
        custom = str(self._settings().get("client_bot_faq") or "").strip()
        return custom or FAQ_TEXT

    def _orders_keyboard(self, chat: str, row: dict,
                         orders: list[dict] | None = None) -> dict:
        """К каждому заказу — кнопка-карточка: покупатель касается номера и
        видит статус со сроком, без набора «статус 1001»."""
        if orders is None:
            orders = self._linked_orders(chat, row)
        keys = [[(f"№{o.get('number')} · {self._status_label(o)}"[:60],
                 f"status:{o['number']}")]
                for o in orders[:6] if o.get("number")]
        keys.append([("🛍 Каталог", "catalog"), ("❔ Помощь", "help")])
        return _keyboard(*keys)

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
            return ("Формат: «телефон +7 978 000-00-00» — или нажмите "
                    "«Отправить номер». По номеру найдутся заказы, "
                    "оформленные на полке и в переписке.")
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

    def _order_card(self, chat: str, row: dict,
                    number: str) -> tuple[str, dict | None]:
        """Карточка своего заказа: текст статуса + кнопки оплаты и трекинга."""
        text = self.text_order_status(chat, row, number)
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number or "",))
        if not order or not any(o["id"] == order["id"]
                                for o in self._linked_orders(chat, row)):
            return text, self._menu()
        return text, self._order_card_keyboard(order)

    def _order_card_keyboard(self, order: dict) -> dict:
        """Кнопки карточки заказа: оплатить (если есть реквизиты и долг),
        страница статуса (если задан публичный адрес панели) и меню."""
        settings = self._settings()
        keys: list[list[dict]] = []
        extra: list[dict] = []
        due = num(order.get("price")) - max(num(order.get("paid")),
                                            num(order.get("prepaid")))
        pay_info = str(settings.get("client_bot_pay_info") or "").strip()
        if due > 0 and pay_info and not self._is_final(order):
            extra.append({"text": "💳 Оплатить", "callback_data":
                          f"pay:{order['id']}"})
        track = str(settings.get("client_bot_track_url") or "").strip()
        if track and order.get("number"):
            url = (track.rstrip("/") + "/track?number="
                   + urllib.parse.quote(str(order["number"]), safe=""))
            extra.append({"text": "🔗 Статус онлайн", "url": url})
        if extra:
            keys.append(extra)
        keys.append([{"text": "🛍 Каталог", "callback_data": "catalog"},
                     {"text": "📦 Мои заказы", "callback_data": "mine"}])
        return {"inline_keyboard": keys}

    def _own_order(self, chat: str, row: dict, order_id: str) -> dict | None:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if order and any(o["id"] == order["id"]
                         for o in self._linked_orders(chat, row)):
            return order
        return None

    def _pay_card(self, chat: str, row: dict,
                  order_id: str) -> tuple[str, dict | None]:
        """«Оплатить»: реквизиты владельца + сумма и код платежа.

        Никакого эквайринга: мастер сверяет поступление сам, бот только
        передаёт покупателю публичные реквизиты и комментарий платежа.
        """
        order = self._own_order(chat, row, order_id)
        if not order:
            return ("Это не ваш заказ — оплата доступна в его карточке.",
                    self._menu())
        pay_info = str(self._settings().get("client_bot_pay_info") or "").strip()
        if self._is_final(order):
            return (f"Заказ №{order.get('number')} уже закрыт — оплата не нужна.",
                    self._menu())
        if not pay_info:
            return ("Оплату принимаем при получении — мастер согласует "
                    "способ при выдаче.", self._menu())
        due = num(order.get("price")) - max(num(order.get("paid")),
                                            num(order.get("prepaid")))
        number = order.get("number")
        text = (f"💳 Заказ №{number} — к оплате {_money(due)}\n\n"
                f"{pay_info}\n\nВ комментарии к переводу напишите: "
                f"NOZZA {number}\nПосле перевода нажмите «✅ Я оплатил» — "
                "мастер подтвердит получение.")
        keys = {"inline_keyboard": [
            [{"text": "✅ Я оплатил", "callback_data": f"paid:{order['id']}"}],
            [{"text": "⬅️ К заказу", "callback_data": f"status:{number}"}],
        ]}
        return text, keys

    def _paid_notice(self, chat: str, row: dict,
                     order_id: str) -> tuple[str, dict | None]:
        """Покупатель нажал «Я оплатил»: сверяет мастер, деньги пишет он же —
        бот ничего не проводит по кассе, чтобы не выдумывать платежи."""
        order = self._own_order(chat, row, order_id)
        if not order:
            return "Это не ваш заказ.", self._menu()
        number = order.get("number")
        due = num(order.get("price")) - max(num(order.get("paid")),
                                            num(order.get("prepaid")))
        try:
            self.manager.notify_async(
                f"💳 Покупатель сообщил об оплате\n№{number} · "
                f"{_money(max(0.0, due))}\nЧат {chat}\n"
                f"Сверьте поступление: «оплата {round(max(0.0, due))} по {number}»",
                critical=True)
        except Exception:
            pass
        self.db.add_event("lead", "Покупатель сообщил об оплате",
                          f"№{number}", data={"order_id": order["id"],
                                              "chat_id": chat})
        return ("Передал мастеру ✓ Подтвердим получение оплаты здесь же. "
                "Статус: «мои заказы»."), self._menu()

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
                              f"{self._status_label(order)}",
                        self._menu())
            self._log(chat, "", "→ push", f"№{order.get('number')}: {status}",
                      kind="push")

    def _maybe_ask_reviews(self) -> None:
        """Через 2 дня после выдачи — спросить отзыв (один раз на заказ).

        Оценка приходит кнопкой, «есть проблема» — просим описать и будим
        мастера: плохой отзыв без реакции стоит дороже самого отзыва.
        """
        if not bool(self._settings().get("client_bot_review", True)):
            return
        links = self.db.query(
            "SELECT l.chat_id, l.order_id FROM client_orders l"
            " JOIN orders o ON o.id=l.order_id"
            " WHERE o.status IN (SELECT id FROM statuses WHERE is_final=1)"
            "   AND o.updated_at <= datetime('now', '-2 days')")
        for link in links:
            chat, order_id = link["chat_id"], link["order_id"]
            if self.db.one("SELECT 1 FROM client_reviews"
                           " WHERE order_id=? AND chat_id=?", (order_id, chat)):
                continue
            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if not order:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO client_reviews(order_id,chat_id,asked_at)"
                " VALUES(?,?,?)", (order_id, chat, now_iso()))
            self._reply(chat,
                        f"Заказ №{order.get('number')} выдан. Всё ли хорошо?",
                        _keyboard([("👍 Всё хорошо", f"review:{order_id}:good"),
                                   ("👎 Есть проблема", f"review:{order_id}:bad")]))
            self._log(chat, "", "→ review", f"№{order.get('number')}: вопрос",
                      kind="push")

    def _review_rating(self, chat: str, row: dict, order_id: str,
                       rating: str) -> tuple[str, dict | None]:
        self.db.execute(
            "UPDATE client_reviews SET rating=?, created_at=?"
            " WHERE order_id=? AND chat_id=?",
            (rating, now_iso(), order_id, chat))
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        number = (order or {}).get("number") or ""
        if rating == "good":
            return ("Спасибо! Если понадобится ещё — «каталог» всегда рядом.",
                    self._menu())
        self._await_problem[chat] = (order_id, time.time() + 86400)
        try:
            self.manager.notify_async(
                f"👎 Покупатель недоволен\nЗаказ №{number} · чат {chat}\n"
                f"Спросили подробности — ответить: кответ {chat} <текст>",
                critical=True)
        except Exception:
            pass
        return ("Сожалею 🙏 Опишите, что не так, одним сообщением — передам "
                "мастеру, и мы это поправим."), self._menu()

    def _review_comment(self, chat: str, row: dict, order_id: str,
                        text: str) -> str:
        self._await_problem.pop(chat, None)
        self.db.execute(
            "UPDATE client_reviews SET comment=? WHERE order_id=? AND chat_id=?",
            (text[:1000], order_id, chat))
        order = self.db.one("SELECT number FROM orders WHERE id=?", (order_id,))
        number = (order or {}).get("number") or ""
        try:
            self.manager.notify_async(
                f"👎 Детали проблемы — заказ №{number}\nЧат {chat}\n«{text[:400]}»\n"
                f"Ответить: кответ {chat} <текст>", critical=True)
        except Exception:
            pass
        self.db.add_event("order", "Отзыв с деталями проблемы",
                          f"№{number}", "", {"order_id": order_id, "chat_id": chat})
        return ("Спасибо за подробности — мастер свяжется и поправит. "
                "Хорошего дня!")

    def _maybe_remind_pickup(self) -> None:
        """«Готов» висит дольше N дней — мягкое напоминание покупателю."""
        days = int(num(self._settings().get("client_bot_pickup_days"), 3))
        if days <= 0:
            return
        links = self.db.query(
            "SELECT l.chat_id, l.order_id FROM client_orders l"
            " JOIN orders o ON o.id=l.order_id"
            " WHERE o.status='ready' AND COALESCE(l.reminded_at,'')=''"
            f"   AND o.updated_at <= datetime('now', '-{days} days')")
        for link in links:
            chat, order_id = link["chat_id"], link["order_id"]
            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if not order:
                continue
            self.db.execute("UPDATE client_orders SET reminded_at=?"
                            " WHERE chat_id=? AND order_id=?",
                            (now_iso(), chat, order_id))
            self._reply(chat, f"Заказ №{order.get('number')} "
                              f"«{order.get('product')}» готов и ждёт вас. "
                              "Если забрали — извините за беспокойство 🙂",
                        self._menu())
            self._log(chat, "", "→ pickup", f"№{order.get('number')}: ждёт",
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
            "reviews_good": count(
                "SELECT COUNT(*) n FROM client_reviews WHERE rating='good'"),
            "reviews_bad": count(
                "SELECT COUNT(*) n FROM client_reviews WHERE rating='bad'"),
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
