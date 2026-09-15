"""Ядро бота сотрудников: цикл опроса, приём обновлений, отправка.

Здесь нет ни одной бизнес-команды. Ядро отвечает на три вопроса:
* как услышать Telegram (long polling через общий транспорт `tg.py`,
  журнал обновлений и очередь исходящих — две копии коннектора или обрыв
  сети не теряют ни вход, ни ответ);
* кому отвечать (гейт ролей `staff.gate`: посторонний чат видит только
  свой chat_id и приглашение по коду);
* чему передать управление (роутер `router.py` — таблицы команд, диспетчер
  в несколько строк вместо цепочек if/elif).

Сценарии (продажа, каталог, принтеры…) — примеси в соседних модулях;
класс `StaffBot` в `bot.py` собирает их вместе.
"""
from __future__ import annotations

import json
import re as _re
import threading
import time
import urllib.request

from ..accounting import num, uid
from ..staff import ROLE_NAMES, Staff, gate, group_for_word
from .router import ROUTER, normalize, suggest_command
from .scenes import BotScenes
from .ui import HELP, keyboard, paginate

API = "https://api.telegram.org/bot{token}/{method}"


class BotCore:
    """Фоновый слушатель команд. Запускается только когда включён в настройках."""

    def __init__(self, manager):
        self.manager = manager
        self.db = manager.db
        self._stop = threading.Event()
        try:
            self._offset = max(0, int(num(self.db.setting("telegram_bot_update_offset", 0))))
        except (TypeError, ValueError):
            self._offset = 0
        self._current_update_id = ""
        self._callback_id = ""  # answerCallbackQuery текущего нажатия
        self._answered = False  # отвечаем на нажатие ровно один раз
        self.last_poll = 0.0  # время успешного опроса (сердцебиение, идея 36)
        # Эфемерное состояние одного процесса: страницы листания, выбранный
        # принтер, слежка и живые дашборды. Диалоги, которые должны пережить
        # перезапуск, живут в self.scenes (SQLite).
        self._pending_stop: dict[str, float] = {}
        self._live: dict[str, dict] = {}  # chat -> {message_id, text} живого дашборда
        self._printer_choice: dict[str, str] = {}  # chat -> printer_id
        self._watched: dict[str, dict] = {}  # chat -> {number, last_milestone}
        self._sell_page: dict[str, int] = {}  # chat -> страница меню продаж
        self._prod_page: dict[str, int] = {}  # chat -> страница меню прихода
        self._cat_page: dict[str, int] = {}   # chat -> страница меню каталога
        self._cat_filter: dict[str, str] = {}  # chat -> фильтр списка каталога
        self._cat_query: dict[str, str] = {}   # chat -> поисковый запрос каталога
        self._pending_del: dict[str, tuple] = {}  # chat -> (nom_id, время запроса)
        self._pending_recalc_all: dict[str, float] = {}  # chat -> время запроса
        # 18.1: диалоги (флоу продажи, сверка, ответ клиенту) — в базе.
        self.scenes = BotScenes(self.db)
        # 14.0 (идеи 73, 74): транспорт, журнал update и очередь исходящих —
        # общие с клиентским ботом (модуль tg), а не своя копия в каждом боте.
        from ..tg import Outbox, Transport, UpdateLedger
        self.transport = Transport(
            lambda: str(self._settings().get("telegram_token", "") or ""), "staff")
        self.ledger = UpdateLedger(self.db, "telegram_bot_updates")
        self.outbox = Outbox(
            self.db,
            sender=lambda method, payload, timeout=15: self._call(
                method, payload, timeout=timeout),
            table="telegram_outbox",
            token_provider=self.transport.token_provider)
        self._thread = threading.Thread(target=self._loop, name="pf-bot", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- транспорт
    def shutdown(self) -> None:
        self._stop.set()

    def _settings(self) -> dict:
        return self.db.settings(include_secrets=True)

    def _call(self, method: str, params: dict, timeout: int = 35) -> dict:
        """Вызов Bot API через общий транспорт (идея 73)."""
        return self.transport.call(method, params, timeout=timeout)

    def _claim_update(self, update: dict) -> bool | None:
        """Занять update рабочего бота в общем журнале (идея 73)."""
        return self.ledger.claim(update)

    def _finish_update(self, update_id: str, ok: bool = True, error: str = "") -> None:
        """Отметить update обработанным или ошибкой."""
        self.ledger.finish(str(update_id or ""), ok, error)

    def _reply(self, chat: str, text: str, buttons: dict | None = None) -> None:
        """Ответ сотруднику через очередь исходящих (идея 74).

        Сообщение кладётся в `telegram_outbox` и сразу отправляется; при
        обрыве сети строка остаётся в очереди и уйдёт на следующем витке
        опроса — раньше ответ терялся молча.
        """
        payload = {"chat_id": chat, "text": str(text or "")[:3800],
                   "disable_web_page_preview": "true"}
        if buttons:
            payload["reply_markup"] = json.dumps(buttons, ensure_ascii=False)
        row = self.outbox.add(str(chat), "sendMessage", payload,
                              dedupe_key=f"reply:{chat}:{hash(payload['text'])}")
        self.outbox.send(row)

    def _reply_photo(self, chat: str, path, caption: str = "") -> None:
        """Фото сотруднику через ту же очередь."""
        payload = {"chat_id": chat, "caption": str(caption or "")[:1024]}
        row = self.outbox.add(str(chat), "sendPhoto", payload, file_path=str(path))
        self.outbox.send(row)

    def _send_menu(self, chat: str, text: str, buttons: list, message_id: str = "") -> None:
        """Отправить inline-меню; если есть message_id — правим старое сообщение."""
        markup = json.dumps({"inline_keyboard": buttons}, ensure_ascii=False)
        if message_id:
            self._call("editMessageText", {
                "chat_id": chat, "message_id": message_id,
                "text": text[:3800], "reply_markup": markup}, timeout=15)
            return
        self._call("sendMessage", {"chat_id": chat, "text": text[:3800],
                                   "reply_markup": markup}, timeout=15)

    def _edit_or_reply(self, chat: str, message: dict, text: str,
                       buttons: dict | None = None) -> None:
        """Редактировать inline-карточку вместо нового сообщения.

        Новое сообщение используется только если Telegram уже не позволяет
        изменить старое или callback пришёл без message_id.
        """
        message_id = str(message.get("message_id") or "")
        if not message_id:
            return self._reply(chat, text, buttons)
        params = {"chat_id": chat, "message_id": message_id, "text": text[:3800]}
        if buttons:
            params["reply_markup"] = json.dumps(buttons, ensure_ascii=False)
        result = self._call("editMessageText", params)
        if not result.get("ok"):
            self._reply(chat, text, buttons)

    def _download_file(self, file_id: str) -> bytes | None:
        """Скачать файл из Telegram через общий транспорт (идея 73)."""
        return self.transport.download_file(file_id, call=self._call)

    def _send_media_group(self, token: str, chat: str, photos: list[bytes]) -> None:
        """Несколько фото одним сообщением (sendMediaGroup, multipart)."""
        boundary = "----printflow" + uid("b").replace("b_", "")
        parts: list[bytes] = []
        media = []
        for i in range(len(photos)):
            attach = f"attach://photo{i}"
            media.append({"type": "photo", "media": attach})
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo{i}\";"
                f" filename=\"frame{i}.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode())
            parts.append(photos[i])
            parts.append(b"\r\n")
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
                f"{chat}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"media\"\r\n\r\n"
                f"{json.dumps(media)}\r\n").encode()
        tail = f"--{boundary}--\r\n".encode()
        body = head + b"".join(parts) + tail
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMediaGroup", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()

    def _paginate(self, rows: list, page: int, per_page: int = 8) -> tuple:
        """Нарезка списка на страницы (общая в ui.paginate, оставлена здесь
        как метод для совместимости внутренних вызовов и тестов)."""
        return paginate(rows, page, per_page)

    def _answer_callback(self, text: str = "") -> None:
        """Ответить на текущее нажатие inline-кнопки (снять «часики»).

        Ровно один раз на нажатие: повторный ответ Telegram молча
        отклоняет, а отказ роли обязан дойти до человека текстом.
        """
        if not self._callback_id or self._answered:
            return
        self._answered = True
        params = {"callback_query_id": self._callback_id}
        if text:
            params["text"] = text[:190]
        self._call("answerCallbackQuery", params)

    # ---------------------------------------------------------- главное меню
    def _inline_menu(self) -> dict:
        """Главное inline-меню сотрудника, общее для всех карточек."""
        return keyboard(
            [("🖨 Принтеры", "cmd:printers"), ("≡ Очередь", "cmd:queue")],
            [("🛍 Стеллаж", "cmd:shelf"), ("📥 Inbox", "cmd:inbox")],
            [("🧵 AMS / пластик", "cmd:filament"), ("💰 Касса", "cmd:shelf-cash")],
            [("⚑ План", "cmd:plan"), ("₽ Деньги", "cmd:money")],
            [("🗂 Каталог", "cmd:cat"), ("📊 Итоги", "cmd:today")],
            [("🩺 Доктор", "cmd:doctor"), ("❔ Помощь", "cmd:help")],
        )

    def _main_reply_keyboard(self) -> dict:
        """Нижняя reply-панель главного меню: 6 кнопок, без команд.

        Reply-клавиатура всегда на виду — именно она делает бота «кнопочным».
        Вложенные списки и действия — inline-кнопками (см. shelf_keyboard).
        """
        return {
            "keyboard": [
                ["🛒 Продать", "📦 Полка", "💰 Касса"],
                ["📷 Кадр", "📊 Итоги", "⚙️ Ещё"],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
            "one_time_keyboard": False,
        }

    def _send_main_menu(self, chat: str, text: str = HELP) -> None:
        """Главное меню: короткий текст + постоянная reply-панель."""
        self._call("sendMessage", {
            "chat_id": chat, "text": str(text or HELP)[:3800],
            "disable_web_page_preview": "true",
            "reply_markup": json.dumps(self._main_reply_keyboard(),
                                       ensure_ascii=False),
        }, timeout=15)

    def _reply_keyboard(self, chat: str, text: str) -> None:
        """Ответ с нижней панелью главного меню (кнопки — основной вход)."""
        self._send_main_menu(chat, text)

    def more_keyboard(self, chat: str, message_id: str = "") -> None:
        """«Ещё» — остальные разделы кнопками.

        Права проверяются на каждой кнопке отдельно: сотрудник увидит
        очередь и катушки, а «Деньги» получит отказ-подсказку (кнопкой
        «Показать владельцу» в будущем — сейчас честный отказ роли).
        """
        buttons = [
            [{"text": "🖨 Очередь печати", "callback_data": "cmd:queue"},
             {"text": "🏭 Принтеры", "callback_data": "cmd:printers"}],
            [{"text": "📦 Заказы", "callback_data": "cmd:orders"},
             {"text": "👥 Клиенты", "callback_data": "cmd:clients"}],
            [{"text": "🧵 Катушки / AMS", "callback_data": "cmd:filament"},
             {"text": "📋 Каталог", "callback_data": "cmd:cat"}],
            [{"text": "📅 План печати", "callback_data": "cmd:plan"},
             {"text": "💰 Деньги", "callback_data": "cmd:money"}],
            [{"text": "🔧 Доктор", "callback_data": "cmd:doctor"},
             {"text": "👥 Команда", "callback_data": "cmd:team"}],
            [{"text": "🏠 В меню", "callback_data": "cmd:menu"},
             {"text": "❔ Помощь", "callback_data": "cmd:help"}],
        ]
        self._send_menu(chat, "⚙️ Ещё — остальное тоже кнопками:", buttons,
                        message_id)

    def queue_keyboard(self, chat: str, message_id: str = "") -> None:
        """Очередь печати кнопками: задания + управление принтером."""
        text = self.text_queue()
        buttons = []
        for job in self.manager.queue()[:6]:
            order = job.get("order") or {}
            title = (order.get("number") and f"№{order['number']} "
                     f"{order.get('product') or ''}" or job.get("name") or "задание")
            if job.get("state") == "running":
                buttons.append([{"text": f"▶ {str(title)[:40]} (печатается)",
                                 "callback_data": "cmd:queue"}])
            else:
                buttons.append([{"text": f"⏳ {str(title)[:40]}",
                                 "callback_data": "cmd:queue"}])
        buttons.append([{"text": "▶ Следующее", "callback_data": "cmd:next"},
                        {"text": "✅ Снял деталь", "callback_data": "cmd:removed"}])
        buttons.append([{"text": "❙❙ Пауза", "callback_data": "cmd:pause"},
                        {"text": "▶ Продолжить", "callback_data": "cmd:resume"},
                        {"text": "◉ Кадр", "callback_data": "cmd:frame"}])
        buttons.append([{"text": "■ Стоп", "callback_data": "cmd:stop"},
                        {"text": "🏠 В меню", "callback_data": "cmd:menu"}])
        self._send_menu(chat, text, buttons, message_id)

    # --------------------------------------------------- кнопки-навигация
    def cb_menu(self, chat: str, message_id: str, params: str) -> None:
        self._send_main_menu(chat)

    def cb_more(self, chat: str, message_id: str, params: str) -> None:
        self.more_keyboard(chat, message_id=message_id)

    def cb_goto(self, chat: str, message_id: str, params: str) -> None:
        """Кнопка-подсказка «выполнить исправленную команду».

        Права проверяем по целевой команде до диспетчера — отказ уходит
        коротким тостом на нажатие, а не новым сообщением.
        """
        target = params.strip()
        if not target:
            return self._answer_callback("Команда пуста")
        word = target.split()[0]
        digits = any(w.isdigit() for w in target.split()[1:])
        group = group_for_word(word, text_has_digits=digits)
        who = gate(self.db, chat)
        if group and who["role"] and group not in who["allowed"]:
            return self._answer_callback(
                f"Недоступно для роли «{ROLE_NAMES.get(who['role'])}»")
        self._answer_callback()
        self._dispatch(chat, target)

    # ------------------------------------------------------------------ цикл
    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = self._settings()
            if not (settings.get("telegram_enabled") and settings.get("telegram_bot")
                    and settings.get("telegram_token") and settings.get("telegram_chat_id")):
                self._stop.wait(20)          # бот выключен — просто ждём
                continue
            try:
                result = self._call("getUpdates", {
                    "offset": self._offset, "timeout": 25,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                })
                if result.get("ok"):
                    self.last_poll = time.time()
                    self.outbox.drain(batch=10)  # дозаправка недосланных (идея 74)
                for update in (result.get("result") or []):
                    update_id = str(update.get("update_id") or "")
                    claim = self._claim_update(update)
                    if claim is False:
                        # Не подтверждаем более новые update поверх живой
                        # обработки: порядок Telegram должен сохраниться.
                        break
                    try:
                        previous = self._current_update_id
                        self._current_update_id = update_id
                        if claim is not None:
                            self._handle(update, str(settings.get("telegram_chat_id")))
                            self._finish_update(update_id, True)
                        self._current_update_id = previous
                        try:
                            next_offset = int(num(update_id)) + 1
                        except (TypeError, ValueError):
                            next_offset = self._offset
                        self._offset = max(self._offset, next_offset)
                        self.db.set_settings({"telegram_bot_update_offset": self._offset})
                    except Exception as exc:
                        self._current_update_id = ""
                        self._finish_update(update_id, False, str(exc))
                        # Не подтверждаем offset: следующий polling повторит
                        # failed update после безопасного reclaim.
                        continue
                self.scenes.sweep()  # уборка просроченных диалогов
                self._maybe_digest(settings)
                self._maybe_weekly(settings)
                self._maybe_live()
                self._maybe_watch()
                self._maybe_shelf_low(settings)
            except Exception:
                self._stop.wait(10)

    # ---------------------------------------------------------------- приём
    def _handle(self, update: dict, owner: str) -> None:
        # Кнопки управления: «пауза», «продолжить», «свет», «стоп», «кадр»
        callback = update.get("callback_query") or {}
        if callback:
            return self._handle_callback(callback, owner)
        message = update.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        if not chat:
            return
        text = (message.get("text") or "").strip()
        who = gate(self.db, chat)
        if not who["role"]:
            # Посторонний чат: единственное, что можно — показать свой chat_id
            # (чтобы владелец добавил в команду) или войти по коду приглашения.
            lowered = text.lower().lstrip("/").replace("ё", "е").strip()
            invite = _re.match(r"(?:старт|start|код|code)?\s*(pf-[a-z0-9]{4,12})$",
                               lowered)
            if invite:
                profile = message.get("from") or {}
                try:
                    member = Staff(self.db).use_invite(
                        invite.group(1), chat,
                        str(profile.get("first_name") or ""),
                        str(profile.get("id") or ""))
                    role = member.get("role_name") or "сотрудник"
                    self._reply(chat,
                                f"Добро пожаловать, {member.get('name')}! "
                                f"Вы в команде NOZZA как {role}.\n"
                                "Права: " + Staff(self.db).rights_text(member.get("role"))
                                + "\n\nНапишите «помощь» — покажу команды.")
                    self.db.add_event("bot", "Новый участник по приглашению",
                                      f"{member.get('name')} — {role}", "", {})
                    return
                except ValueError as exc:
                    return self._reply(chat, str(exc))
            if lowered in ("код", "code", "мой код", "id"):
                return self._reply(
                    chat, f"Ваш chat_id: {chat}\nПередайте его владельцу — "
                          "он добавит вас в команду в настройках панели или "
                          "пришлёт код приглашения.")
            self._reply(chat,
                        "Этот бот — рабочий инструмент команды NOZZA.\n"
                        f"Ваш chat_id: {chat} — попросите владельца добавить вас.\n"
                        "Если у вас есть код приглашения, напишите: старт КОД")
            self.db.add_event("bot", "Посторонний в Telegram-боте",
                              f"chat_id {chat}: {text[:80]}", "", {})
            return
        # Фото без подписи: прикрепляем к последнему активному заказу.
        photo = message.get("photo")
        text = (message.get("text") or "").strip()
        caption = (message.get("caption") or "").strip()
        if photo and not text:
            try:
                return self._attach_photo(chat, photo, caption)
            except Exception as exc:
                return self._reply(chat, f"Не получилось: {exc}")
        if not text:
            return
        try:
            self._dispatch(chat, text)
        except Exception as exc:
            self._reply(chat, f"Не получилось: {exc}")

    # ------------------------------------------------------------ диспетчер
    def _dispatch(self, chat: str, raw: str) -> None:
        """Разбор текстового сообщения: сцена → права → маршрут → подсказка."""
        text = normalize(raw)
        # Активный диалог забирает ввод раньше команд: цена продажи — число,
        # факт сверки — число, ответ клиенту — любое сообщение.
        scene = self.scenes.active(chat)
        if scene:
            if scene["scene"] == "sell" and scene["data"].get("await") == "price" \
                    and _re.fullmatch(r"[\d\s.,]+", text):
                value = num(text.replace(" ", "").replace(",", "."))
                return self.sell_flow_channel(chat, value)
            if scene["scene"] == "cash_reconcile" \
                    and _re.fullmatch(r"[\d\s.,]+", text):
                return self._reply(chat, self._cash_fact(chat, text))
            if scene["scene"] == "client_reply":
                target = scene["data"].get("target") or ""
                self.scenes.pop(chat)
                return self._reply(
                    chat, self._client_answer(f"кответ {target} {raw}"))
        route = ROUTER.match_text(text)
        who = gate(self.db, chat)
        if route is not None:
            group = ROUTER.group_for(route, text)
            if who["role"] and group and group not in who["allowed"]:
                word = text.split()[0]
                return self._reply(
                    chat, f"🚫 «{word}» недоступен для роли "
                          f"«{ROLE_NAMES.get(who['role'])}».\n"
                          "Доступно сейчас: " + Staff(self.db).rights_text(who["role"]))
            handler = getattr(self, route.method)
            return handler(chat, raw, text)
        self._unknown(chat, raw)

    def _handle_callback(self, callback: dict, owner: str) -> None:
        """Нажатие inline-кнопки: гейт → права → маршрут таблицы."""
        message = callback.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id") or "")
        if not chat or not data:
            return
        who = gate(self.db, chat)
        if not who["role"]:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id,
                                               "text": "Этот бот приватный."})
            return
        command = data.replace("cmd:", "", 1)
        group = ROUTER.group_for_callback(command)
        if group and group not in who["allowed"]:
            self._call("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": f"Недоступно для роли «{ROLE_NAMES.get(who['role'])}»"})
            return
        self._callback_id = callback_id
        self._answered = False
        try:
            self._run_callback(command, chat, message)
        finally:
            self._callback_id = ""
            self._answered = False

    def _run_callback(self, command: str, chat: str, message: dict) -> None:
        """Выполнить callback-команду по таблице маршрутов."""
        message_id = str(message.get("message_id") or "")
        match = ROUTER.match_callback(command)
        if match is None:
            self._answer_callback()
            return self._reply(chat, "Не понял команду.")
        route, params = match
        handler = getattr(self, route.method)
        if route.kind == "text":
            answer = handler(chat, params) or ""
            self._answer_callback()
            # Управление печатью обновляем в том же сообщении своей панелью;
            # остальные текстовые ответы — главным меню под исходной карточкой.
            if route.prefix in ("pause", "resume", "light", "frame", "stop"):
                try:
                    self._call("editMessageText", {
                        "chat_id": chat, "message_id": message_id,
                        "text": answer[:3800],
                        "reply_markup": json.dumps(self.control_keyboard(),
                                                   ensure_ascii=False)})
                    return
                except Exception:
                    pass
            return self._edit_or_reply(chat, message, answer, self._inline_menu())
        # Обработчик рисует экран сам. Нажатие отвечаем сразу — «часики»
        # не висят, пока собирается экран; исключение — late_answer (goto):
        # его отказ роли должен уйти текстом, а не пустым тостом.
        if not route.late_answer:
            self._answer_callback()
        handler(chat, message_id, params)

    def _run_command(self, command: str, chat: str = "") -> str:
        """Совместимый мост для текстовых callback-команд старого формата.

        Основной путь кнопок — `_run_callback`; этот метод остаётся для
        внутренних вызовов и тестов, которые проверяют тексты ответов.
        """
        match = ROUTER.match_callback(command)
        if match is not None and match[0].kind == "text":
            return getattr(self, match[0].method)(chat, match[1]) or ""
        if command == "queue":
            return self.text_queue()
        if command == "help":
            return HELP
        return "Не понял команду."

    # ------------------------------------------------------------- подсказка
    def _unknown(self, chat: str, raw: str) -> None:
        """Непонятое сообщение: подсказка + кнопки, а не сухой отказ."""
        suggestion = suggest_command(raw)
        if suggestion:
            lines = [
                f"Не понял «{raw.strip()[:60]}».",
                f"Возможно, вы имели в виду «{suggestion}» — нажмите кнопку, "
                "и я выполню.",
                "",
                "Или вернитесь в меню — там всё кнопками.",
            ]
            buttons = [[{"text": suggestion, "callback_data": f"cmd:goto:{suggestion}"}],
                       [{"text": "🏠 В меню", "callback_data": "cmd:menu"},
                        {"text": "❔ Помощь", "callback_data": "cmd:help"}]]
        else:
            lines = [
                "Не узнал такую команду.",
                "Нажмите кнопку ниже — я покажу, что умею.",
            ]
            buttons = [[{"text": "🏠 В меню", "callback_data": "cmd:menu"},
                        {"text": "❔ Помощь", "callback_data": "cmd:help"}],
                       [{"text": "🖥 Что происходит", "callback_data": "cmd:panel"}]]
        self._call("sendMessage", {
            "chat_id": chat, "text": "\n".join(lines)[:3800],
            "reply_markup": json.dumps({"inline_keyboard": buttons}, ensure_ascii=False),
        }, timeout=15)
