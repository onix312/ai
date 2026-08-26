"""Telegram-бот PrintFlow: принтер в кармане.

Работает на long polling — никаких белых IP и проброса портов не нужно.
Бот сам ходит к api.telegram.org и спрашивает: «есть новые сообщения?».
Отвечает только владельцу (chat_id из настроек), поэтому посторонний,
даже узнав имя бота, ничего не увидит и не нажмёт.

Команды намеренно на русском и без слэша тоже понимаются: «камера»,
«статус», «пауза» — с телефона так быстрее, чем искать латиницу.
"""
from __future__ import annotations

import json
import re as _re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .accounting import num, uid
from .config import now_iso

API = "https://api.telegram.org/bot{token}/{method}"

HELP = """PrintFlow 8.2 — панель в кармане.

Кнопки внизу или команды (без слэша, в любом регистре):
• панель — всё сразу: печать, деньги, план, долги
• статус · кадр · очередь — что происходит сейчас
• филамент · пластик — остатки катушек и прогноз закупки
• закупка — список покупок · закупка авто — автозаполнить
• таймлапс · кадры — последние снимки печати одним сообщением
• живой — автообновляемый дашборд (стоп живой — выключить)
• деньги · день — финансы
• итоги недели · итоги месяца — отчёты по запросу
• долги · брак · рейтинг — должники, потери на браке, ABC изделий
• сколько осталось / что печатает / когда закончит / сколько заработал
• стеллаж / полка — остаток, дефицит, приход, продажи и движения с кнопками
• продажа — продать со стеллажа (−1 шт)
• приход — меню прихода; «приход Адресник 5» — приход на позицию
• движения стеллажа — последние приходы/продажи/списания
• продажи стеллажа — что ушло за 7 дней, по позициям
• план — что печатать сегодня
• выдать 1001 — закрыть заказ, зачислить оплату, текст клиенту
• оплата 1500 по 1001 — принять оплату
• статус 1001 печать — сменить статус заказа
• новый адресник 2шт 900р Мария — заказ из текста
• принтер — список парка · принтер 2 — выбрать принтер
• пауза / продолжить / свет — управление
• пропустить 2 — исключить объект N из печати
• выше 1001 · ниже 1001 — порядок заданий в очереди
• поток 90 — процент подачи филамента (50–150%)
• повторить / повторить 1001 — подготовить повтор после разбора брака
• следи 1001 — прогресс заказа каждые 10%
• простой — сколько простаивает принтер и что теряем
• фото — прикрепить фото к заказу (или «фото 1001»)
• стоп — прервать печать (подтверждение: стоп да)

Можно писать без слэша и в любом регистре."""

STATE_RU = {
    "RUNNING": "печатает", "IDLE": "свободен", "PAUSE": "на паузе",
    "PAUSED": "на паузе", "FINISH": "печать завершена", "PREPARE": "готовится",
    "FAILED": "ошибка", "OFFLINE": "не в сети", "UNKNOWN": "нет данных",
}


def _hm(minutes: float) -> str:
    """Минуты → «2 ч 15 мин»: так читается быстрее, чем «135 мин»."""
    total = int(max(0, num(minutes)))
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


def _money(value: float) -> str:
    return f"{round(num(value)):,}".replace(",", " ") + " ₽"


def _keyboard(*rows: list[tuple[str, str]]) -> dict:
    """Inline-клавиатура: [[(текст, callback_data)], ...]."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


class TelegramBot:
    """Фоновый слушатель команд. Запускается только когда включён в настройках."""

    def __init__(self, manager, client_mode=False):
        self.manager = manager
        self.client_mode = client_mode
        self.token_key = "client_bot_token" if client_mode else "telegram_token"
        self.chat_key = "client_bot_chat" if client_mode else "telegram_chat_id"
        self.db = manager.db
        self._stop = threading.Event()
        self._offset = 0
        self.last_poll = 0.0  # время успешного опроса (сердцебиение, идея 36)
        self._pending_stop: dict[str, float] = {}
        self._live: dict[str, dict] = {}  # chat -> {message_id, text} живого дашборда
        self._printer_choice: dict[str, str] = {}  # chat -> printer_id
        self._watched: dict[str, dict] = {}  # chat -> {number, last_milestone}
        self._client_sessions: dict[str, dict] = {}  # client chat -> intake draft
        self._thread = threading.Thread(target=self._loop, name="pf-bot", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- транспорт
    def shutdown(self) -> None:
        self._stop.set()

    def _settings(self) -> dict:
        return self.db.settings(include_secrets=True)

    def _call(self, method: str, params: dict, timeout: int = 35) -> dict:
        token = self._settings().get(self.token_key, "")
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

    def _reply(self, chat: str, text: str) -> None:
        self._call("sendMessage", {"chat_id": chat, "text": text[:3800],
                                   "disable_web_page_preview": "true"}, timeout=15)

    def _reply_keyboard(self, chat: str, text: str) -> None:
        """Сообщение с постоянной клавиатурой-меню внизу."""
        # Постоянное меню — только безопасные обзоры и переходы. Продажа
        # открывает отдельный список с явными кнопками «−1», а не списывает
        # товар случайным нажатием на клавиатуре.
        keyboard = [
            [{"text": "🖨 Панель"}, {"text": "📷 Кадр"}, {"text": "≡ Очередь"}],
            [{"text": "🛍 Стеллаж"}, {"text": "🛒 Продажа"}, {"text": "⚑ План"}],
            [{"text": "📦 Пластик"}, {"text": "₽ Деньги"}, {"text": "⚠ Хвосты"}],
            [{"text": "📊 Итоги"}, {"text": "❔ Помощь"}],
        ]
        self._call("sendMessage", {
            "chat_id": chat, "text": text[:3800], "disable_web_page_preview": "true",
            "reply_markup": json.dumps({"keyboard": keyboard, "resize_keyboard": True}),
        }, timeout=15)

    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = self._settings()
            if not ((settings.get("client_bot_enabled") if self.client_mode else settings.get("telegram_enabled"))
                    and (True if self.client_mode else settings.get("telegram_bot"))
                    and settings.get(self.token_key) and (self.client_mode or settings.get(self.chat_key))):
                self._stop.wait(20)          # бот выключен — просто ждём
                continue
            try:
                result = self._call("getUpdates", {
                    "offset": self._offset, "timeout": 25,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                })
                self.last_poll = time.time()
                for update in (result.get("result") or []):
                    self._offset = max(self._offset, num(update.get("update_id")) + 1)
                    self._handle(update, str(settings.get(self.chat_key)))
                self._maybe_digest(settings)
                self._maybe_weekly(settings)
                self._maybe_live()
                self._maybe_watch()
            except Exception:
                self._stop.wait(10)

    # ------------------------------------------------- расписания рассылок
    def _maybe_digest(self, settings: dict) -> None:
        """Утренний дайджест в digest_time, раз в сутки."""
        digest_time = str(settings.get("digest_time") or "09:00")
        now = datetime.now()
        if now.strftime("%H:%M") != digest_time:
            return
        last = str(settings.get("digest_last") or "")
        today = now.strftime("%Y-%m-%d")
        if last == today:
            return
        self.db.set_settings({"digest_last": today})
        chat = str(settings.get(self.chat_key) or "")
        if chat:
            self.manager.notify_async(self.text_digest())

    def _maybe_weekly(self, settings: dict) -> None:
        """Еженедельный отчёт: день недели (1=пн) и время."""
        day = int(num(settings.get("weekly_report_day", 1), 1))
        at = str(settings.get("weekly_report_time") or "20:00")
        now = datetime.now()
        if now.isoweekday() != day or now.strftime("%H:%M") != at:
            return
        key = f"{now.isocalendar().year}-W{now.isocalendar().week}"
        if str(settings.get("weekly_last") or "") == key:
            return
        self.db.set_settings({"weekly_last": key})
        chat = str(settings.get(self.chat_key) or "")
        if chat:
            self.manager.notify_async(self.text_weekly())

    # -------------------------------------------------------------- разбор
    def _handle(self, update: dict, owner: str) -> None:
        # Кнопки управления: «пауза», «продолжить», «свет», «стоп», «кадр»
        callback = update.get("callback_query") or {}
        if callback:
            return self._handle_callback(callback, owner)
        message = update.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        if not chat:
            return
        if self.client_mode:
            return self._handle_client(message, chat)
        if chat != owner:
            # Чужой чат: вежливо отказываем и пишем в журнал.
            text = (message.get("text") or "").strip()
            self._reply(chat, "Этот бот приватный и отвечает только владельцу.")
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

    def _handle_client(self, message: dict, chat: str) -> None:
        """Безопасный публичный сценарий: только собирает заявку, без команд цеха."""
        text = (message.get("text") or message.get("caption") or "").strip()
        if text.lower() in ("/start", "start", "начать", "помощь"):
            self._client_sessions[chat] = {"step": "product", "chat_id": chat}
            self._reply(chat, self._settings().get("client_bot_welcome") or "Опишите изделие и прикрепите фото, если оно есть.")
            return
        session = self._client_sessions.setdefault(chat, {"step": "product", "chat_id": chat})
        photo = message.get("photo") or []
        if not text and not photo:
            return self._reply(chat, "Пришлите описание задачи или фото изделия.")
        fields = {"product": "Что нужно изготовить?", "dimensions": "Какие размеры или фото образца?", "quantity": "Какое количество нужно?", "deadline": "К какому сроку?", "contact": "Как с вами связаться?"}
        step = session.get("step", "product")
        session[step] = text or "Фото приложено"
        if photo: session["photo_file_id"] = str(photo[-1].get("file_id", ""))
        order = list(fields)
        idx = order.index(step)
        if idx < len(order) - 1:
            session["step"] = order[idx + 1]
            return self._reply(chat, fields[order[idx + 1]])
        summary = "\n".join(f"{k}: {session.get(k, '—')}" for k in fields)
        photo = message.get("photo") or []
        file_id = str(photo[-1].get("file_id")) if photo and isinstance(photo[-1], dict) else ""
        self.db.add_event("client_bot", "Новая заявка клиента", summary[:500], "", {"chat_id": chat, "has_photo": bool(file_id)})
        target = str(self._settings().get("client_bot_chat") or "")
        if target:
            if file_id:
                self._call("sendPhoto", {"chat_id": target, "photo": file_id,
                                          "caption": "💬 Новая заявка клиента\\n\\n" + summary[:900]})
            else:
                self._call("sendMessage", {"chat_id": target, "text": "💬 Новая заявка клиента\\n\\n" + summary[:3500]})
        self._reply(chat, "Заявка принята ✅ Сотрудник свяжется с вами для уточнения цены и срока.")

    def _handle_callback(self, callback: dict, owner: str) -> None:
        """Нажатие inline-кнопки. Отвечаем владельцу, чужому — отказ."""
        message = callback.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id") or "")
        if not chat or not data:
            return
        if chat != owner:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id,
                                                "text": "Этот бот приватный."})
            return
        command = data.replace("cmd:", "", 1)
        # Меню стеллажа и продаж отправляют новое сообщение с актуальными
        # остатками и кнопками; исходное не превращаем в неактуальный чек.
        if command in ("shelf", "sell-menu", "shelf-prod-menu"):
            self._call("answerCallbackQuery", {"callback_query_id": callback_id})
            if command == "shelf":
                return self.shelf_keyboard(chat)
            if command == "sell-menu":
                return self.sell_keyboard(chat)
            return self.shelf_produce_keyboard(chat)
        text = self._run_command(command, chat)
        self._call("answerCallbackQuery", {"callback_query_id": callback_id})
        # Управление печатью обновляем в том же сообщении; продажа и панель — новым.
        control = command in ("pause", "resume", "light", "frame", "stop")
        if control:
            try:
                self._call("editMessageText", {"chat_id": chat,
                                                "message_id": str(message.get("message_id")),
                                                "text": text[:3800],
                                                "reply_markup": json.dumps(_keyboard(
                                                    [("❙❙ Пауза", "cmd:pause"), ("▶ Продолжить", "cmd:resume")],
                                                    [("☀ Свет", "cmd:light"), ("◉ Кадр", "cmd:frame")],
                                                    [("■ Стоп", "cmd:stop")]))})
                return
            except Exception:
                pass
        self._reply(chat, text)

    def _run_command(self, command: str, chat: str = "") -> str:
        """Выполнить команду (текстовую или inline-кнопки) и вернуть ответ."""
        if command == "pause":
            return self.do_command("pause", "Печать поставлена на паузу", chat=chat)
        if command == "resume":
            return self.do_command("resume", "Печать продолжена", chat=chat)
        if command == "light":
            return self.do_command("light", "Подсветка переключена", chat=chat)
        if command == "stop":
            return self.do_stop(chat or "", "стоп" if chat else "стоп да")
        if command == "frame" or command == "кадр":
            if chat:
                self.send_frame(chat)
                return "Кадр отправлен."
            return "Кадр недоступен"
        if command == "next":
            return self._start_next(chat)
        if command == "removed":
            printer = self._pick_printer(chat)
            result = self.manager.part_removed(printer.id if printer else "")
            return f"✅ Деталь снята. Простой после печати {result.get('idle_min', 0)} мин."
        if command == "reprint":
            try:
                row = self.manager.reprint_last_failed(
                    confirmed=True, request_id=uid("tg-reprint")
                )
                return f"↻ Повтор «{row.get('name')}» подготовлен. Запуск подтвердите отдельно."
            except Exception as exc:
                return f"Не получилось подготовить повтор: {exc}"
        if command == "panel":
            return self.text_panel()
        if command == "plan":
            return self.text_plan()
        if command == "shelf:needs":
            return self.text_shelf(only_needs=True)
        if command.startswith("sell:"):
            return self.do_sell(command.split(":", 1)[1])
        if command.startswith("shelf-sell:"):
            return self.do_shelf_sell(command.split(":", 1)[1])
        if command.startswith("shelf-prod:"):
            return self.do_shelf_produce(command.split(":", 1)[1])
        if command == "shelf-moves":
            return self.text_shelf_moves()
        if command == "shelf-sales7":
            return self.text_shelf_sales(7)
        if command == "shelf-sales30":
            return self.text_shelf_sales(30)
        return "Не понял команду."

    def _start_next(self, chat: str) -> str:
        """Запустить следующее задание очереди на свободном принтере."""
        printer = self._pick_printer(chat)
        if not printer:
            return "Принтеры не добавлены."
        if not printer.connected:
            return f"{printer.record.get('name', 'Принтер')} не на связи."
        snap = printer.snapshot()
        if snap["printer"]["state"] not in ("IDLE", "FINISH"):
            return "Принтер занят — сначала завершите или остановите печать."
        job = self.manager.next_job(printer.id, snap)
        if not job:
            return "Очередь пуста — запускать нечего."
        try:
            self.manager.start_job(job["id"], printer.id)
            return f"▶ Запускаю «{job.get('name')}»."
        except Exception as exc:
            return f"Не удалось запустить: {exc}"

    def _reorder_queue(self, text: str, direction: str) -> str:
        """«выше 1001» / «ниже 1001» — передвинуть задание в очереди печати."""
        number = next((w for w in text.split()[1:] if w.isdigit()), "")
        if not number:
            return f"Формат: «{direction} 1001» — передвинуть задание заказа №1001 в очереди."
        jobs = self.db.query(
            "SELECT j.*, o.number AS order_number FROM print_jobs j"
            " LEFT JOIN orders o ON o.id=j.order_id"
            " WHERE j.state='queued'"
            " ORDER BY j.priority DESC, datetime(j.created_at)")
        if len(jobs) < 2:
            return "В очереди меньше двух заданий — двигать нечего."
        index = next((i for i, j in enumerate(jobs)
                      if str(j.get("order_number") or "") == number), None)
        if index is None:
            return f"Заказ №{number} не стоит в очереди."
        neighbor_index = index - 1 if direction == "выше" else index + 1
        if neighbor_index < 0:
            return "Задание уже первое в очереди."
        if neighbor_index >= len(jobs):
            return "Задание уже последнее в очереди."
        target, neighbor = jobs[index], jobs[neighbor_index]
        step = 1 if direction == "выше" else -1
        new_priority = int(num(neighbor.get("priority"))) + step
        self.db.execute("UPDATE print_jobs SET priority=? WHERE id=?",
                        (new_priority, target["id"]))
        self.db.add_event("queue", "Очередь: задание передвинуто",
                          f"{target.get('name') or ''} {direction}",
                          "", {"job_id": target["id"], "direction": direction})
        return (f"Задание заказа №{number} «{target.get('name') or ''}» "
                f"передвинуто {direction}.")

    def _watch_order(self, chat: str, number: str) -> str:
        """«следи 1001» — уведомления о прогрессе заказа каждые 10%."""
        if not number:
            return "Формат: «следи 1001» — пришлю прогресс каждые 10%."
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        self._watched[chat] = {"number": number, "last_milestone": 0}
        return f"👁 Слежу за заказом №{number}. Прогресс буду присылать каждые 10%."

    def _maybe_watch(self) -> None:
        """Разослать уведомления о прогрессе заказов, за которыми следят."""
        if not self._watched:
            return
        for chat, sub in list(self._watched.items()):
            number = sub.get("number", "")
            order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
            if not order:
                self._watched.pop(chat, None)
                self._reply(chat, f"Заказ №{number} не найден — слежка снята.")
                continue
            job = self.db.one(
                "SELECT * FROM print_jobs WHERE order_id=? AND state='running' LIMIT 1",
                (order["id"],))
            if not job:
                continue
            progress = int(num(job.get("progress") or 0))
            milestone = progress // 10 * 10
            if milestone <= sub.get("last_milestone", 0):
                continue
            sub["last_milestone"] = milestone
            # Прогресс уходит в тот же чат, из которого попросили следить, —
            # раньше уведомление шло через manager.notify_async в чат по умолчанию,
            # и слежка из другого чата молчала.
            self._reply(chat,
                        f"PrintFlow · заказ №{number}\n"
                        f"Прогресс {milestone}% — {order.get('product') or ''}")
            # По завершении снимаем слежку.
            if progress >= 100:
                self._watched.pop(chat, None)

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
        from .config import PHOTO_DIR
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        name = f"order_{order_id}_{int(time.time())}.jpg"
        (PHOTO_DIR / name).write_bytes(raw)
        self.db.upsert("order_photos", {
            "id": f"ph{int(time.time() * 1000)}", "order_id": order_id, "at": now_iso(),
            "file": name, "note": "фото из Telegram", "kind": "upload"})
        self.db.add_event("order", "Фото к заказу (Telegram)",
                          f"Заказ {self._order_number(order_id)}", "",
                          {"order_id": order_id})
        self._reply(chat, f"📷 Фото прикреплено к заказу №{self._order_number(order_id)}.")

    def _order_number(self, order_id: str) -> str:
        order = self.db.one("SELECT number FROM orders WHERE id=?", (order_id,))
        return str((order or {}).get("number") or "")

    def _download_file(self, file_id: str) -> bytes | None:
        """Скачать файл из Telegram (getFile → скачивание)."""
        token = self._settings().get(self.token_key, "")
        if not token:
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

    def _dispatch(self, chat: str, raw: str) -> None:
        text = raw.lower().lstrip("/").replace("ё", "е")
        for emoji in "🖨📷≡₽⚑🛍🛒📦📊⚠❔▦▤·":
            text = text.replace(emoji, " ")
        text = text.strip()
        word = text.split()[0] if text else ""

        if word in ("start", "help", "старт", "помощь", "меню", "?"):
            return self._reply_keyboard(chat, HELP)
        if text.startswith("закрыть месяц"):
            return self._reply(chat, self._month_close(text))
        if word in ("панель", "panel", "дашборд"):
            return self._reply_keyboard(chat, self.text_panel())
        if word in ("план", "plan", "печатать"):
            return self._reply(chat, self.text_plan())
        if word in ("стеллаж", "полка", "витрина", "shelf"):
            return self.shelf_keyboard(chat)
        # Отчёты стеллажа ловим до общей команды «продажа»:
        # «продажи стеллажа» — это сводка, а не меню быстрой продажи.
        if text.startswith("движения стеллаж") or text in ("движения", "стеллаж движения"):
            return self._reply(chat, self.text_shelf_moves(15))
        if text.startswith("продажи стеллаж") or text in ("продажи за неделю", "продажи полки"):
            return self._reply(chat, self.text_shelf_sales(7))
        if word in ("продажа", "продать", "продажи", "sell"):
            return self.sell_keyboard(chat)
        if word in ("приход", "положить", "пополнить"):
            # «приход» — меню быстрого прихода; «приход Адресник 5» — конкретной позиции.
            return self._shelf_produce_text(chat, text)
        if word in ("оплата", "оплатить", "payment"):
            return self._reply(chat, self._pay(text))
        if word in ("статус", "status", "принтер", "принтеры"):
            # «принтер 2» — выбрать принтер для команд; «статус 1001 печать» —
            # смена статуса заказа; иначе состояние принтеров.
            if word in ("принтер", "принтеры"):
                number = next((w for w in text.split()[1:] if w.isdigit()), "")
                if number:
                    return self._reply(chat, self._select_printer(chat, int(number)))
                if any(w.isdigit() for w in text.split()[1:]):
                    return self._reply(chat, self._set_status(text))
                return self._reply(chat, self._list_printers(chat))
            digits = any(w.isdigit() for w in text.split()[1:])
            return self._reply(chat, self._set_status(text) if digits else self.text_status())
        if word in ("выдать", "выдал", "выдан", "закрыть"):
            return self._reply(chat, self._fulfill(text))
        if word in ("новый", "заказ", "создать"):
            return self._reply(chat, self._new_order(text))
        if word in ("кадр", "камера", "фото", "photo", "cam"):
            return self.send_frame(chat)
        if word in ("таймлапс", "кадры", "гиф", "timelapse", "видео"):
            return self.send_timelapse(chat)
        if word in ("живой", "live", "дашборд"):
            return self.start_live(chat)
        if word in ("стоп-живой", "стопживой", "стоп живой"):
            return self.stop_live(chat)
        if word in ("очередь", "queue"):
            return self._reply(chat, self.text_queue())
        if word in ("выше", "ниже"):
            return self._reply(chat, self._reorder_queue(text, word))
        if word in ("деньги", "финансы", "money", "прибыль"):
            return self._reply(chat, self.text_money())
        if word in ("день", "сегодня", "итоги"):
            if "недел" in text:
                return self._reply(chat, self.text_weekly())
            if "месяц" in text:
                return self._reply(chat, self.text_month_report())
            return self._reply(chat, self.text_today())
        if word in ("долги", "должники", "debt", "долг"):
            return self._reply(chat, self.text_debts())
        if word in ("брак", "дефект", "defect", "дефекты"):
            return self._reply(chat, self.text_defects())
        if word in ("рейтинг", "топ", "abc", "изделия"):
            return self._reply(chat, self.text_rating())
        if word in ("хвосты", "хвост", "дыры", "проверка"):
            return self._reply(chat, self.text_loose_ends())
        if word in ("сколько", "что", "когда", "заработал", "заработано"):
            return self._reply(chat, self.text_ask(text))
        if word in ("филамент", "пластик", "катушки", "спул"):
            return self._reply(chat, self.text_filament())
        if word in ("закупить", "закупка", "закупки", "шоппинг", "покупки"):
            return self._reply(chat, self._shopping(text))
        if word in ("пауза", "pause"):
            return self._reply(chat, self.do_command("pause", "Печать поставлена на паузу", chat=chat))
        if word in ("продолжить", "resume", "старт-печати"):
            return self._reply(chat, self.do_command("resume", "Печать продолжена", chat=chat))
        if word in ("свет", "light"):
            return self._reply(chat, self.do_command("light", "Подсветка переключена", chat=chat))
        if word in ("пропустить", "скип", "исключить", "skip"):
            number = next((w for w in text.split()[1:] if w.isdigit()), "")
            if not number:
                return self._reply(chat, "Формат: «пропустить 2» — исключить объект N из печати.")
            return self._reply(chat, self.do_command("skip_objects", [int(number)],
                                                     f"Объект {number} исключён из печати", chat=chat))
        if word in ("поток", "flow"):
            m = _re.search(r"(\d+)", text)
            if not m:
                return self._reply(chat, "Формат: «поток 90» — процент подачи филамента (50–150%).")
            return self._reply(chat, self.do_command("flow", int(m.group(1)),
                                                     f"Поток {m.group(1)}%", chat=chat))
        if word in ("повторить", "перепечатать", "reprint", "повтор"):
            number = next((w for w in text.split()[1:] if w.isdigit()), "")
            try:
                row = self.manager.reprint_last_failed(
                    number, confirmed=True, request_id=uid("tg-reprint")
                )
                return self._reply(
                    chat,
                    f"↻ Повтор «{row.get('name')}» подготовлен. Запуск подтвердите отдельно.",
                )
            except Exception as exc:
                return self._reply(chat, f"Не получилось подготовить повтор: {exc}")
        if word in ("простой", "idle"):
            stats = self.manager.idle_stats()
            return self._reply(chat,
                f"⏳ Простой: {stats.get('idle_hours')} ч "
                f"({stats.get('idle_minutes')} мин)\n"
                f"Упущено ~{_money(stats.get('lost_profit'))} "
                f"по норме {_money(stats.get('rate_per_hour'))}/ч")
        if word in ("следи", "подпишись", "watch", "следить"):
            number = next((w for w in text.split()[1:] if w.isdigit()), "")
            return self._reply(chat, self._watch_order(chat, number))
        if word in ("снял", "снято", "забрал"):
            result = self.manager.part_removed()
            return self._reply(chat, f"✅ Деталь снята. Простой после печати {result.get('idle_min', 0)} мин.")
        if word in ("стоп", "stop"):
            return self._reply(chat, self.do_stop(chat, text))
        if word in ("готов", "ready"):
            return self._reply(chat, self.order_ready(text))
        self._reply(chat, "Не понял команду. Напишите «помощь» — покажу список.")

    # -------------------------------------------------------------- ответы
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
        from .completion import OrderCompletion
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
            f"{round(num(actual.get('hours')), 2)} ч · {_money(actual.get('cost'))}\n\n"
            f"Текст для клиента (не отправлен):\n{result.get('message') or ''}"
        )

    def text_status(self) -> str:
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        if not printers:
            return "Принтеры не добавлены."
        blocks = []
        for snap in printers:
            info = snap["printer"]
            head = f"{snap['name']} — {STATE_RU.get(info['state'], info.get('state_label') or info['state'])}"
            if not snap["connection"]["connected"]:
                blocks.append(head + "\nНет связи по локальной сети.")
                continue
            lines = [head]
            if info["state"] in ("RUNNING", "PAUSE", "PREPARE"):
                lines.append(f"{info.get('task') or 'задание'} · {round(num(info.get('progress')))}%")
                if info.get("layer"):
                    lines.append(f"Слой {info['layer']} из {info.get('total_layers') or '—'}")
                if num(info.get("remaining_min")):
                    lines.append(f"Осталось {_hm(info['remaining_min'])}"
                                 + (f", финиш в {str(info.get('eta'))[11:16]}" if info.get("eta") else ""))
                job = snap.get("job") or {}
                order = job.get("order") or {}
                if order:
                    lines.append(f"Заказ №{order.get('number')} · {order.get('product') or ''}")
                if num(job.get("spent")):
                    lines.append(f"Потрачено {_money(job['spent'])}"
                                 + (f", всего будет ≈ {_money(job['cost_total'])}"
                                    if num(job.get("cost_total")) else ""))
                if job.get("profit") is not None and num(job.get("price")):
                    lines.append(f"Прибыль {_money(job['profit'])}"
                                 + (f" ({round(num(job.get('margin_pct')))}%)"
                                    if job.get("margin_pct") is not None else ""))
            lines.append(f"Сопло {round(num(snap['temperature'].get('nozzle')))}°, "
                         f"стол {round(num(snap['temperature'].get('bed')))}°")
            for alert in (snap.get("guard") or {}).get("alerts", [])[:3]:
                lines.append(f"⚠ {alert.get('title')}: {alert.get('reason', '')}")
            due = (snap.get("maintenance") or {}).get("due", 0)
            if due:
                lines.append(f"🔧 Просрочено работ по обслуживанию: {due}")
            blocks.append("\n".join(lines))
        farm = state.get("farm") or {}
        blocks.append(f"Парк: печатают {farm.get('printing', 0)} из {len(printers)}, "
                      f"в очереди {farm.get('queued', 0)}.")
        return "\n\n".join(blocks)

    def text_queue(self) -> str:
        jobs = [j for j in self.manager.queue() if j.get("state") in ("queued", "running", "starting")]
        if not jobs:
            return "Очередь пуста."
        lines = ["Очередь печати:"]
        for index, job in enumerate(jobs[:12], 1):
            order = job.get("order") or {}
            title = order.get("number") and f"№{order['number']} {order.get('product') or ''}" or job.get("name") or "задание"
            mark = {"running": "▶", "starting": "▶"}.get(job.get("state"), f"{index}.")
            lines.append(f"{mark} {title}")
        if self.manager.quiet_now():
            lines.append("\nСейчас тихие часы — автозапуск отложен до утра.")
        return "\n".join(lines)

    def text_money(self) -> str:
        summary = self.manager.acc.summary(30)
        debts = self.manager.acc.debts()
        lines = [
            "Деньги за 30 дней:",
            f"Выручка {_money(summary.get('income'))}",
            f"Расходы {_money(summary.get('expense'))}",
            f"Прибыль {_money(summary.get('profit'))}",
        ]
        if summary.get("orders"):
            lines.append(f"Заказов {summary['orders']}, средний чек {_money(summary.get('avg_check'))}")
        if num(debts.get("total")) > 0:
            lines.append(f"\nЖдём оплату: {_money(debts['total'])} по {debts.get('count', 0)} заказам")
            if num(debts.get("overdue")) > 0:
                lines.append(f"Из них просрочено: {_money(debts['overdue'])}")
        return "\n".join(lines)

    def text_today(self) -> str:
        today = now_iso()[:10]
        rows = self.db.query(
            "SELECT kind, SUM(amount) AS total FROM transactions WHERE substr(at,1,10)=? GROUP BY kind",
            (today,))
        money = {r["kind"]: num(r["total"]) for r in rows}
        jobs = self.db.query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(grams),0) AS g, COALESCE(SUM(duration_min),0) AS m"
            " FROM print_jobs WHERE substr(COALESCE(finished_at,queued_at),1,10)=? AND state='done'",
            (today,))
        job = jobs[0] if jobs else {}
        lines = [
            f"Итоги дня {today}:",
            f"Приход {_money(money.get('income'))}, расход {_money(money.get('expense'))}",
            f"Напечатано заданий: {int(num(job.get('n')))}, пластика {round(num(job.get('g')))} г, "
            f"время печати {_hm(num(job.get('m')))}",
        ]
        # Хвосты учёта: заказы без цены и т.п. — чтобы не копились до конца месяца.
        try:
            from .repo import Repo
            problems = Repo(self.db).data_check().get("problems") or []
        except Exception:
            problems = []
        if problems:
            lines.append(f"⚠ Хвосты учёта: {len(problems)} — «хвосты» покажет список")
        return "\n".join(lines)

    def text_loose_ends(self) -> str:
        """«хвосты» — незакрытые дыры в учёте: заказы без цены и т.п."""
        from .repo import Repo
        problems = Repo(self.db).data_check().get("problems") or []
        if not problems:
            return "✅ Хвостов нет — учёт чистый."
        lines = [f"⚠ Хвосты учёта: {len(problems)}"]
        for problem in problems[:12]:
            lines.append(f"· {problem.get('title')}"
                         + (f" — {problem.get('detail')}" if problem.get("detail") else ""))
        return "\n".join(lines)

    def text_filament(self) -> str:
        """Остатки филамента на складе и в AMS: граммы, %, прогноз окончания."""
        spools = self.db.query("SELECT * FROM spools WHERE archived=0 ORDER BY remaining_grams DESC")
        if not spools:
            return "Катушек на складе нет — добавьте через раздел «Склад»."
        threshold = num(self.db.setting("filament_low_threshold", 15.0), 15.0)
        lines = ["🧵 Филамент на складе:"]
        for s in spools[:12]:
            total = max(1.0, num(s.get("total_grams"), 1000))
            left = num(s.get("remaining_grams"))
            pct = left / total * 100
            mark = "⚠" if pct <= threshold else "·"
            slot = f" · AMS слот {s.get('ams_slot')}" if str(s.get("ams_slot") or "") != "" else ""
            lines.append(f"{mark} {s.get('material')} {s.get('color_name') or ''} — "
                         f"{round(left)} г ({round(pct)}%){slot}")
        # Прогноз закупки по темпу расхода за 30 дней
        usage = self.db.one(
            "SELECT UPPER(material) m, SUM(grams) g FROM filament_usage"
            " WHERE at>=? GROUP BY UPPER(material) ORDER BY g DESC LIMIT 3",
            ((datetime.now() - timedelta(days=30)).isoformat(),))
        hints = []
        for row in usage or []:
            rate = num(row.get("g")) / 30.0  # г/день
            stock = num(self.db.one(
                "SELECT COALESCE(SUM(remaining_grams),0) v FROM spools"
                " WHERE archived=0 AND UPPER(material)=?", (row.get("m"),))["v"])
            if rate > 0:
                days = stock / rate
                hints.append(f"{row.get('m')}: хватит на ~{int(days)} дн (темп {round(rate)} г/дн)")
        if hints:
            lines.append("\nПрогноз:")
            lines.extend("  " + h for h in hints)
        return "\n".join(lines)

    def _shopping(self, text: str) -> str:
        """Список закупок: показать, автозаполнить или добавить вручную."""
        from .shopping import ShoppingList
        shop = ShoppingList(self.db)
        words = text.lower().replace("ё", "е").split()
        # «закупка авто» — автозаполнение из низких катушек и темпа расхода
        if len(words) > 1 and words[1] in ("авто", "обновить", "заполнить"):
            result = shop.auto_fill()
            if result.get("count"):
                return f"🛒 Добавлено в закупку: {result['count']} позиций.\n\n" + shop.text()
            return "🛒 Добавлять нечего — катушки и запас в порядке.\n\n" + shop.text()
        # Одной команды недостаточно для честной приёмки: нужны фактические
        # количество/вес, сумма и касса. Не закрываем строку без этих данных.
        if len(words) > 1 and words[1] in ("купил", "получил", "принял"):
            return ("Оформите приём в панели: Склад пластика → Список закупок → «Принять». "
                    "Там PrintFlow создаст катушки и запишет подтверждённый расход без дублей.")
        # «закупка <материал> <qty>» — добавить вручную
        if len(words) > 1 and words[1] not in ("авто", "купил", "обновить", "заполнить"):
            material = words[1].upper()
            qty = next((w for w in words[2:] if w.isdigit()), "1")
            shop.add({"name": material, "material": material, "qty": float(qty),
                      "unit": "кг", "source": "manual"})
            return f"🛒 Добавлено: {material} {qty} кг.\n\n" + shop.text()
        return shop.text()

    def text_digest(self) -> str:
        """Утренний дайджест: дедлайны, очередь, остатки, кому написать."""
        today = now_iso()[:10]
        finals = {r["id"] for r in self.db.query("SELECT id FROM statuses WHERE is_final=1")}
        orders = self.db.query("SELECT * FROM orders")
        active = [o for o in orders if o["status"] not in finals]
        due_today = [o for o in active if o.get("due") == today]
        late = [o for o in active if o.get("due") and o["due"] < today]
        lines = ["☀ Доброе утро! Дайджест PrintFlow:"]
        if late:
            lines.append(f"⚠ Просрочено заказов: {len(late)}")
            for o in late[:3]:
                lines.append(f"  №{o.get('number')} {o.get('product') or ''} (срок {o.get('due')})")
        if due_today:
            lines.append(f"📌 Срок сегодня: {len(due_today)}")
            for o in due_today[:3]:
                lines.append(f"  №{o.get('number')} {o.get('product') or ''}")
        else:
            lines.append("📌 Заказов со сроком на сегодня нет.")
        queue = [j for j in self.manager.queue() if j.get("state") == "queued"]
        if queue:
            lines.append(f"🖨 В очереди {len(queue)} заданий:")
            for j in queue[:5]:
                order = j.get("order") or {}
                lines.append(f"  · {order.get('number') and ('№' + str(order['number']) + ' ') or ''}{j.get('name') or ''}")
        else:
            lines.append("🖨 Очередь печати пуста.")
        low = self.db.query(
            "SELECT material, color_name, remaining_grams FROM spools WHERE archived=0")
        low = [s for s in low
               if num(s["remaining_grams"]) / max(1.0, num(self.db.setting("default_spool_weight", 1000))) * 100
               <= num(self.db.setting("filament_low_threshold", 15.0))]
        if low:
            lines.append("🧵 Мало пластика:")
            for s in low[:5]:
                lines.append(f"  · {s['material']} {s['color_name']} — {round(num(s['remaining_grams']))} г")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            lines.append(f"💰 Ждут оплаты {_money(debts['total'])} по {debts.get('count', 0)} заказам")
        return "\n".join(lines)

    def text_weekly(self) -> str:
        """Еженедельный отчёт: деньги, печать, брак, пластик."""
        summary = self.manager.acc.summary(7)
        jobs = self.db.query(
            "SELECT state, COUNT(*) n, COALESCE(SUM(grams),0) g, COALESCE(SUM(duration_min),0) m"
            " FROM print_jobs WHERE finished_at>=? GROUP BY state",
            ((datetime.now() - timedelta(days=7)).isoformat(),))
        by_state = {r["state"]: r for r in jobs}
        lines = [
            "📊 Недельный отчёт PrintFlow:",
            f"Выручка {_money(summary.get('income'))}, расход {_money(summary.get('expense'))}",
            f"Прибыль {_money(summary.get('profit'))} (маржа {round(num(summary.get('margin')))}%)",
            f"Печать: {int(num((by_state.get('done') or {}).get('n')))} заданий, "
            f"{round(num((by_state.get('done') or {}).get('g')))} г, "
            f"{_hm(num((by_state.get('done') or {}).get('m')))}",
        ]
        failed = int(num((by_state.get("failed") or {}).get("n")))
        if failed:
            lines.append(f"⚠ Брак: {failed} печатей")
        stock = self.db.one("SELECT COALESCE(SUM(remaining_grams),0) v FROM spools WHERE archived=0") or {}
        lines.append(f"🧵 Пластика на складе: {round(num(stock.get('v')))} г")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            lines.append(f"💰 Долги: {_money(debts['total'])}")
        return "\n".join(lines)

    # ------------------------------------------------- отчётность по запросу
    def _month_close(self, text: str) -> str:
        """«закрыть месяц» — состояние мастера; «закрыть месяц fixed» — шаг."""
        from .month_close import MonthClose, STEP_ORDER
        master = MonthClose(self.db)
        parts = text.split()
        if len(parts) > 2 and parts[2] in STEP_ORDER:
            step = parts[2]
            result = master.run("", step)
            if not result.get("ok") and result.get("done"):
                return f"Шаг «{step}» уже выполнен в этом месяце."
            if not result.get("ok"):
                return f"Шаг не выполнен: {result.get('error')}"
            return f"✅ {result.get('message') or 'готово'}"
        state = master.state()
        lines = [f"🧾 Закрыть месяц {state['key']}:"]
        for step in state["order"]:
            mark = "✅" if state["done"][step] else ("▸" if state["next"] == step else "·")
            lines.append(f"{mark} {state['titles'][step]}")
        if state["next"]:
            lines.append(f"\nШаг: «закрыть месяц {state['next']}»")
        return "\n".join(lines)

    def text_debts(self) -> str:
        """«долги» — кто и сколько должен, с просрочкой."""
        debts = self.manager.acc.debts()
        if not debts.get("rows"):
            return "💰 Долгов нет — всё оплачено."
        lines = [f"💰 Долги клиентов: {_money(debts['total'])} "
                 f"по {debts.get('count', 0)} заказам"]
        if num(debts.get("overdue")) > 0:
            lines.append(f"Просрочено: {_money(debts['overdue'])}")
        for row in debts["rows"][:10]:
            who = (row.get("customer") or "").strip() or "без имени"
            age = f" · {row['days']} дн" if row.get("days") else ""
            lines.append(f"· №{row.get('number')} {who} — {_money(row['debt'])}{age}")
        return "\n".join(lines)

    def text_defects(self, days: int = 30) -> str:
        """«брак» — сорванные печати и сколько денег они съели."""
        since = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat()
        jobs = self.db.query(
            "SELECT * FROM print_jobs WHERE state='failed' AND finished_at>=?"
            " ORDER BY finished_at DESC", (since,))
        facts = self.manager.acc.defects_cost(days)
        lines = [f"❌ Брак за {int(days)} дней: {len(jobs)} печатей"]
        lines.append(
            f"Потеряно: {round(num(facts.get('grams')))} г пластика, "
            f"{_hm(num(facts.get('minutes')))} времени"
        )
        lines.append(
            f"≈ {_money(num(facts.get('cost')))}; подтверждённые разборы взяты по факту"
        )
        for job in jobs[:6]:
            error = str(job.get("error") or "").strip()[:70]
            lines.append(f"· {job.get('name') or 'без названия'}"
                         + (f" — {error}" if error else ""))
        return "\n".join(lines)

    def text_rating(self) -> str:
        """«рейтинг» — ABC изделий: что приносит деньги, что висит балластом."""
        items = self.manager.acc.abc_report(30).get("items", [])
        if not items:
            return "За 30 дней продаж не было — рейтинг пуст."
        lines = ["🏆 Рейтинг изделий (30 дней):"]
        for item in items[:10]:
            lines.append(
                f"{item.get('cls', '')} · {item.get('name')} — {_money(item.get('revenue'))}"
                f" ({item.get('share')}%), прибыль {_money(item.get('profit'))}")
        return "\n".join(lines)

    def text_month_report(self) -> str:
        """«итоги месяца» — P&L месяца, печать, брак, долги."""
        key = now_iso()[:7]
        pnl = self.manager.acc.pnl_month(key)
        jobs = self.db.query(
            "SELECT state, COUNT(*) n, COALESCE(SUM(grams),0) g,"
            " COALESCE(SUM(duration_min),0) m FROM print_jobs"
            " WHERE finished_at>=? GROUP BY state",
            (f"{key}-01",))
        by_state = {row["state"]: row for row in jobs}
        done = by_state.get("done") or {}
        failed = by_state.get("failed") or {}
        lines = [
            f"📊 Итоги месяца {key}:",
            f"Выручка {_money(pnl.get('income'))}, расход {_money(pnl.get('expense'))}",
            f"Прибыль {_money(pnl.get('profit'))} (маржа {round(num(pnl.get('margin')))}%)",
            f"Печать: {int(num(done.get('n')))} заданий, {round(num(done.get('g')))} г,"
            f" {_hm(num(done.get('m')))}",
        ]
        if int(num(failed.get("n"))):
            lines.append(f"⚠ Брак: {int(num(failed.get('n')))} печатей")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            lines.append(f"💰 Долги: {_money(debts['total'])}")
        tax = self.month_tax_estimate(key)
        if num(tax) > 0:
            lines.append(f"🧾 Налог месяца (оценка): {_money(tax)}")
        return "\n".join(lines)

    def month_tax_estimate(self, key: str) -> float:
        """Оценка налога месяца — из мастера «Закрыть месяц»."""
        try:
            from .month_close import MonthClose
            return num(MonthClose(self.db).month_tax(key).get("tax"))
        except Exception:
            return 0.0

    def text_ask(self, text: str) -> str:
        """«Спроси принтер»: сколько осталось / что печатает / когда закончит /
        сколько заработал — разбор по ключевым словам, без ИИ."""
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        printer = next((p for p in printers
                        if p["printer"]["state"] in ("RUNNING", "PAUSE", "PREPARE")), None)
        if "заработал" in text or "заработано" in text or "прибыль" in text:
            summary = self.manager.acc.summary(1)
            return (f"Сегодня: приход {_money(summary.get('income'))}, "
                    f"прибыль {_money(summary.get('profit'))}, "
                    f"часов печати {round(num(summary.get('print_hours')), 1)}")
        if not printer:
            return "Сейчас ничего не печатается."
        info = printer["printer"]
        if "когда" in text or "закончит" in text or "во сколько" in text:
            if num(info.get("remaining_min")):
                eta = str(info.get("eta") or "")
                return (f"Готово через {_hm(info['remaining_min'])}"
                        + (f", примерно в {eta[11:16]}" if eta else ""))
            return "Прогноз времени пока не готов — печать только началась."
        if "осталось" in text or "сколько" in text or "прогресс" in text:
            return (f"{printer['name']}: {round(num(info.get('progress')))}% · "
                    f"слой {info.get('layer')} из {info.get('total_layers') or '—'}"
                    + (f" · осталось {_hm(info['remaining_min'])}"
                       if num(info.get("remaining_min")) else ""))
        if "печатает" in text or "задание" in text or "задача" in text:
            job = printer.get("job") or {}
            order = job.get("order") or {}
            extra = f" · заказ №{order.get('number')}" if order else ""
            return (f"{printer['name']} печатает «{info.get('task') or job.get('name') or '—'}»{extra}"
                    f" · {round(num(info.get('progress')))}%")
        # не распознали вопрос — покажем компактный статус
        return self.text_status()

    def send_frame(self, chat: str) -> None:
        printer = self._pick_printer(chat)
        if not printer:
            return self._reply(chat, "Принтеры не добавлены.")
        frame = printer.camera.frame
        if not frame:
            return self._reply(chat, "Камера сейчас не отдаёт кадр. "
                                     "Проверьте, что принтер включён и в сети.")
        snap = printer.snapshot()
        info = snap["printer"]
        caption = f"{snap['name']} — {STATE_RU.get(info['state'], info['state'])}"
        if info["state"] in ("RUNNING", "PAUSE"):
            caption += f", {round(num(info.get('progress')))}%"
            if num(info.get("remaining_min")):
                caption += f", осталось {_hm(info['remaining_min'])}"
        if snap.get("camera", {}).get("demo"):
            caption += " (демо-кадр: принтер не подключён)"
        token = self._settings().get(self.token_key, "")
        try:
            self.manager._send_photo(token, chat, caption, frame)
            printer.camera.snapshot(note="Запрос из Telegram")
        except Exception as exc:
            self._reply(chat, f"Не удалось отправить кадр: {exc}")

    def send_timelapse(self, chat: str) -> None:
        """Отправить последние кадры печати как медиа-группу (мини-таймлапс)."""
        printer = self._pick_printer(chat)
        if not printer:
            return self._reply(chat, "Принтеры не добавлены.")
        shots = printer.camera.snapshot_list()
        # Берём последние кадры в прямом хронологическом порядке.
        frames = [s for s in shots if s.get("at")][:10]
        if not frames:
            return self._reply(chat, "Снимков пока нет. Камера делает их по событиям "
                                     "печати и по запросу «кадр».")
        photos = []
        for shot in reversed(frames):
            frame = printer.camera.snapshot_frame(shot["id"])
            if frame:
                photos.append(frame)
        if not photos:
            return self._reply(chat, "Не удалось прочитать сохранённые кадры.")
        token = self._settings().get(self.token_key, "")
        if len(photos) == 1:
            self.manager._send_photo(token, chat, "Последний кадр печати", photos[0])
            return None
        self._send_media_group(token, chat, photos)

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

    # ----------------------------------------------------- живой дашборд
    def start_live(self, chat: str) -> str:
        """Включить автообновляющийся дашборд-сообщение на время печати."""
        printer = self._pick_printer(chat)
        if not printer:
            return "Принтеры не добавлены."
        self._live[chat] = {"message_id": 0, "text": ""}
        text = self.text_status()
        try:
            sent = self._call("sendMessage", {"chat_id": chat, "text": text[:3800],
                                              "disable_web_page_preview": "true"}, timeout=15)
            message_id = sent.get("result", {}).get("message_id", 0)
            self._live[chat] = {"message_id": message_id, "text": text}
            return ("Дашборд включён — будет обновляться во время печати.\n"
                    "Выключить: «стоп живой».")
        except Exception:
            self._live.pop(chat, None)
            return "Не удалось запустить дашборд."

    def stop_live(self, chat: str) -> str:
        self._live.pop(chat, None)
        return "Дашборд выключен."

    def _maybe_live(self) -> None:
        """Обновить живые дашборды, если печать активна и текст изменился."""
        if not self._live:
            return
        for chat, state in list(self._live.items()):
            text = self.text_status()
            if text == state.get("text"):
                continue
            message_id = state.get("message_id", 0)
            state["text"] = text
            if message_id:
                try:
                    self._call("editMessageText", {"chat_id": chat,
                                                   "message_id": str(message_id),
                                                   "text": text[:3800]}, timeout=15)
                except Exception:
                    pass

    # -------------------------------------------------- 5.0: панель и продажи
    def text_panel(self) -> str:
        """Главная панель: печать, деньги, план, долги — одним сообщением."""
        blocks = []
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        farm = state.get("farm") or {}
        if printers:
            active = next((p for p in printers if p["printer"]["state"] in ("RUNNING", "PAUSE", "PREPARE")), None)
            if active:
                info = active["printer"]
                blocks.append(f"🖨 {active['name']} — {STATE_RU.get(info['state'], info['state'])} "
                              f"{round(num(info.get('progress')))}%"
                              + (f", осталось {_hm(info['remaining_min'])}" if num(info.get("remaining_min")) else ""))
            else:
                blocks.append(f"🖨 Парк свободен ({len(printers)} принтер(ов), в очереди {farm.get('queued', 0)})")
        summary = self.manager.acc.summary(30)
        blocks.append(f"₽ За 30 дней: доход {_money(summary.get('income'))}, "
                      f"прибыль {_money(summary.get('profit'))}")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            blocks.append(f"💰 Ждут оплаты {_money(debts['total'])} по {debts.get('count', 0)} заказам")
        # что печатать следующим
        try:
            from .planner import Planner
            from .batches import Batches
            planner = Planner(self.db, Batches(self.db))
            plan = planner.day_plan()
            next_task = plan.get("suggested_next")
            if next_task:
                label = "заказ" if next_task.get("kind") == "order" else "полка"
                blocks.append(f"⚑ Следующее ({label}): {next_task.get('title')} · {_hm(next_task.get('hours', 0) * 60)}")
            elif plan.get("sequence"):
                blocks.append("⚑ Очередь пуста, но есть план — откройте «план».")
            else:
                blocks.append("⚑ Печатать нечего: очередь и полка в порядке.")
        except Exception:
            pass
        alerts = []
        for p in printers:
            for a in (p.get("guard") or {}).get("alerts", [])[:2]:
                alerts.append(f"⚠ {a.get('title')}")
        if alerts:
            blocks.append("\n".join(alerts[:3]))
        return "\n".join(blocks)

    def text_plan(self) -> str:
        """Что печатать сегодня — из мастер-плана производства."""
        try:
            from .planner import Planner
            from .batches import Batches
            planner = Planner(self.db, Batches(self.db))
            plan = planner.day_plan()
        except Exception:
            return "План сейчас недоступен."
        lines = [f"⚑ План на сегодня ({plan.get('verdict_text') or ''})",
                 f"Занято {plan.get('in_progress_hours')} ч, план {plan.get('planned_hours')} ч, "
                 f"загрузка {round(num(plan.get('load_pct')))}%"]
        for i, t in enumerate(plan.get("sequence")[:8], 1):
            kind = "▦" if t.get("kind") == "order" else "▤"
            issues = " ✕" if not t.get("ready") else ""
            lines.append(f"{kind} {t.get('title')} · {_hm(num(t.get('hours')) * 60)}{issues}")
        if not plan.get("sequence"):
            lines.append("Печатать нечего — полка и очередь в порядке.")
        return "\n".join(lines)

    def text_shelf(self, only_needs: bool = False) -> str:
        """Короткий честный срез стеллажа для телефона.

        Не подменяет инвентаризацию: показывает учётный остаток и помечает
        позиции, которые надо проверить или пополнить физически.
        """
        from .shelf import Shelf
        items = Shelf(self.db).items()
        if not items:
            return "🛍 Стеллаж пуст: активных позиций пока нет."
        needs = [i for i in items if i.get("status") in ("empty", "low", "dead") or num(i.get("plan_qty")) > 0]
        total_qty = sum(num(i.get("qty")) for i in items)
        total_value = sum(num(i.get("stock_value")) for i in items)
        if only_needs:
            if not needs:
                return "✅ Стеллаж в порядке: пустых, низких и залежавшихся позиций нет."
            title = f"⚠ Внимание к стеллажу: {len(needs)}"
            rows = needs
        else:
            title = (f"🛍 Стеллаж: {len(items)} поз. · {round(total_qty, 1)} шт · "
                     f"учётная стоимость {_money(total_value)}")
            rows = sorted(needs, key=lambda i: (i.get("status") == "empty", num(i.get("plan_qty"))), reverse=True) or items
        lines = [title]
        for item in rows[:8]:
            # В базовой аналитике «нет продаж» приоритетнее low. Для человека
            # на полке это две разные причины действия, поэтому не прячем
            # низкий остаток за статусом залежавшегося товара.
            marks = []
            if num(item.get("qty")) <= 0:
                marks.append("нет")
            elif item.get("low"):
                marks.append("мало")
            elif not item.get("dead"):
                marks.append("в норме")
            if item.get("dead"):
                marks.append("нет продаж")
            status = " · ".join(marks) or "проверить"
            extra = f" · печать +{int(num(item.get('plan_qty')))}" if num(item.get("plan_qty")) > 0 else ""
            lines.append(f"• {item.get('name') or 'Без названия'} — {round(num(item.get('qty')), 1)} шт · {status}{extra}")
        if len(rows) > 8:
            lines.append(f"… ещё {len(rows) - 8} поз. в полной панели.")
        return "\n".join(lines)

    def shelf_keyboard(self, chat: str) -> None:
        """Панель стеллажа: обзор, дефицит, продажи, приход, движения, план."""
        text = self.text_shelf()
        buttons = [
            [{"text": "⚠ Нужны на полку", "callback_data": "cmd:shelf:needs"},
             {"text": "🛒 Продать", "callback_data": "cmd:sell-menu"}],
            [{"text": "📥 Приход +1", "callback_data": "cmd:shelf-prod-menu"},
             {"text": "🧾 Движения", "callback_data": "cmd:shelf-moves"}],
            [{"text": "📊 Продажи 7 дн", "callback_data": "cmd:shelf-sales7"},
             {"text": "🔄 Обновить", "callback_data": "cmd:shelf"}],
        ]
        self._call("sendMessage", {"chat_id": chat, "text": text[:3800],
                                   "reply_markup": json.dumps({"inline_keyboard": buttons})}, timeout=15)

    def _sell_rows(self) -> list[dict]:
        """Позиции стеллажа с остатком для быстрой продажи."""
        from .shelf import Shelf
        items = Shelf(self.db).items()
        rows = [i for i in items if num(i.get("qty")) > 0]
        rows.sort(key=lambda i: -num(i["qty"]))
        return rows[:8]

    def sell_keyboard(self, chat: str) -> None:
        rows = self._sell_rows()
        if not rows:
            self._reply(chat, "На стеллаже нет товара. Сделайте приход или перенесите со склада.")
            return
        lines = ["🛍 Продажа со стеллажа — нажмите «−1» (деньги по цене ценника):"]
        buttons = []
        for i in rows:
            lines.append(f"• {i['name']} — {round(num(i['qty']),1)} шт · {_money(i.get('price'))}")
            label = f"−1 · {i['name'][:22]} · {_money(i.get('price'))}"
            buttons.append([{"text": label[:60], "callback_data": f"cmd:shelf-sell:{i['id']}"}])
        self._call("sendMessage", {"chat_id": chat, "text": "\n".join(lines)[:3800],
                                   "reply_markup": json.dumps({"inline_keyboard": buttons})}, timeout=15)

    def do_sell(self, nom_id: str) -> str:
        from .documents import Documents
        item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not item:
            return "Позиция не найдена."
        warehouse = self.db.one(
            "SELECT id FROM warehouses WHERE archived=0 AND retail=1 ORDER BY position LIMIT 1") \
            or self.db.one("SELECT id FROM warehouses WHERE archived=0 ORDER BY position LIMIT 1")
        if not warehouse:
            return "Не настроен склад."
        docs = Documents(self.db)
        try:
            docs.quick_sale([{"nom_id": nom_id, "qty": 1}], warehouse["id"],
                            "shop", "", "продажа из Telegram")
            name = item.get("name") or "позиция"
            return f"Продано 1 шт «{name}» — проведено и учтено в кассе."
        except Exception as exc:
            return f"Не получилось продать: {exc}"

    # ----------------------------------------------------- стеллаж из ТГ
    def do_shelf_sell(self, item_id: str, qty: float = 1) -> str:
        """Списать штуки с физической позиции стеллажа и записать доход."""
        from .shelf import Shelf
        shelf = Shelf(self.db)
        item = self.db.one("SELECT * FROM shelf_items WHERE id=? AND active=1", (item_id,))
        if not item:
            return "Позиция стеллажа не найдена."
        qty = num(qty) or 1
        try:
            shelf.sale(item_id, qty, 0, channel="shelf", note="продажа из Telegram")
            left = num(item.get("qty")) - qty
            price = num(item.get("price"))
            money = f" · {_money(price * qty)}" if price else " (без цены)"
            return (f"✅ Продано {round(qty)} шт «{item.get('name')}»{money}. "
                    f"Осталось {round(max(0, left),1)} шт.")
        except Exception as exc:
            return f"Не получилось продать: {exc}"

    def shelf_produce_keyboard(self, chat: str) -> None:
        """Быстрый приход +1 на позиции с планом пополнения или низким остатком."""
        from .shelf import Shelf
        items = Shelf(self.db).items()
        candidates = [i for i in items if num(i.get("plan_qty")) > 0 or i.get("low") or i.get("status") == "empty"]
        candidates = candidates or items
        if not candidates:
            self._reply(chat, "Стеллаж пуст — сначала добавьте позицию.")
            return
        lines = ["📥 Приход на стеллаж (+1 шт). Для другого количества — «приход Адресник 5»."]
        buttons = []
        for i in candidates[:8]:
            name = i.get("name") or "позиция"
            lines.append(f"• {name} — {round(num(i.get('qty')),1)} шт"
                         + (f" · нужно +{int(num(i.get('plan_qty')))}" if num(i.get("plan_qty")) else ""))
            buttons.append([{"text": f"+1 · {name[:24]}", "callback_data": f"cmd:shelf-prod:{i['id']}"}])
        self._call("sendMessage", {"chat_id": chat, "text": "\n".join(lines)[:3800],
                                   "reply_markup": json.dumps({"inline_keyboard": buttons})}, timeout=15)

    def do_shelf_produce(self, item_id: str, qty: float = 1) -> str:
        from .shelf import Shelf
        item = self.db.one("SELECT * FROM shelf_items WHERE id=? AND active=1", (item_id,))
        if not item:
            return "Позиция стеллажа не найдена."
        qty = num(qty) or 1
        try:
            Shelf(self.db).produce(item_id, qty, note="приход из Telegram")
            return f"📥 Приход +{round(qty)} шт «{item.get('name')}» записан."
        except Exception as exc:
            return f"Не получилось оприходовать: {exc}"

    def _shelf_produce_text(self, chat: str, text: str) -> None:
        """«приход Адресник 5» — найти позицию по подстроке и сделать приход."""
        from .shelf import Shelf
        parts = text.split()
        # Последнее слово может быть количеством.
        qty = 1.0
        name_words = parts[1:]
        if name_words:
            try:
                qty = float(name_words[-1].replace(",", "."))
                name_words = name_words[:-1]
            except ValueError:
                qty = 1.0
        query = " ".join(name_words).strip().lower()
        if not query:
            return self.shelf_produce_keyboard(chat)
        items = Shelf(self.db).items()
        matches = [i for i in items if query in (i.get("name") or "").lower()]
        if not matches:
            self._reply(chat, f"Позиция, содержащая «{query}», не найдена на стеллаже.")
            return
        if len(matches) > 1:
            names = "; ".join((i.get("name") or "")[:30] for i in matches[:5])
            self._reply(chat, f"Уточните позицию: совпало несколько — {names}.")
            return
        self._reply(chat, self.do_shelf_produce(matches[0]["id"], qty))

    def text_shelf_moves(self, limit: int = 12) -> str:
        """Последние движения стеллажа одной лентой."""
        from .shelf import Shelf
        moves = Shelf(self.db).moves(limit=limit)
        if not moves:
            return "Движений стеллажа пока нет."
        labels = {"produce": "приход", "sale": "продажа", "online": "онлайн",
                  "writeoff": "списание", "inventory": "инв."}
        lines = ["🧾 Последние движения стеллажа:"]
        for m in moves:
            q = num(m.get("qty"))
            sign = "+" if q > 0 else ""
            name = m.get("item_name") or "позиция"
            day = str(m.get("at") or "")[:16].replace("T", " ")
            price = num(m.get("price"))
            tail = f" · {_money(price * abs(q))}" if price and q < 0 else ""
            lines.append(f"{day} · {labels.get(m.get('kind'), m.get('kind'))} · "
                         f"{name} {sign}{round(q,1)} шт{tail}")
        return "\n".join(lines)

    def text_shelf_sales(self, days: int = 7) -> str:
        """Что реально продалось со стеллажа за период, по позициям."""
        from .shelf import Shelf
        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self.db.query(
            "SELECT m.item_id, i.name, SUM(-m.qty) qty, SUM(-m.qty*m.price) money"
            " FROM shelf_moves m LEFT JOIN shelf_items i ON i.id=m.item_id"
            " WHERE m.kind IN ('sale','online') AND m.qty<0 AND m.at>=?"
            " GROUP BY m.item_id ORDER BY money DESC", (since,))
        if not rows:
            return f"За {days} дн продаж со стеллажа не было."
        total_qty = sum(num(r.get("qty")) for r in rows)
        total_money = sum(num(r.get("money")) for r in rows)
        lines = [f"📊 Продажи стеллажа за {days} дн: {round(total_qty,1)} шт · {_money(total_money)}"]
        for r in rows[:15]:
            lines.append(f"• {r.get('name') or 'позиция'} — {round(num(r.get('qty')),1)} шт · {_money(r.get('money'))}")
        if len(rows) > 15:
            lines.append(f"… ещё {len(rows) - 15} поз.")
        return "\n".join(lines)

    def _final_status_id(self) -> str | None:
        row = self.db.one("SELECT id FROM statuses WHERE is_final=1 ORDER BY position LIMIT 1")
        return (row or {}).get("id")

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
            return (f"По заказу №{number} осталось {_money(due)}.\n"
                    f"Подтвердите: «выдать {number} оплачен» или «выдать {number} в долг».")
        if num(due) <= 0:
            action = "none"
        else:
            action = "received" if paid_confirmed else "debt"
        method = ("cash" if any("налич" in word for word in words) else
                  "card" if any("карт" in word for word in words) else
                  "transfer" if any("перевод" in word for word in words) else "other")
        from .fulfillment import OrderFulfillment
        from .stock import Stock
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
        repeated = " (уже был выдан)" if result.get("already_fulfilled") else ""
        money = (f"получено {_money(result.get('collected'))}"
                 if num(result.get("collected")) > 0 else
                 f"оставлен долг {_money(result.get('debt'))}"
                 if num(result.get("debt")) > 0 else "оплачен ранее")
        return (f"✅ Заказ №{number} выдан{repeated} · {money}.\n\n"
                f"Текст клиенту (не отправлен):\n{result.get('message') or ''}")

    def _pay(self, text: str) -> str:
        import re as _re
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
            )
        except ValueError as exc:
            return f"Не получилось записать оплату: {exc}"
        left = max(0.0, num(order.get("price")) -
                   (max(num(order.get("paid")), num(order.get("prepaid"))) + amount))
        return f"💰 Принято {_money(amount)} по заказу №{number}." + \
            (f" Осталось {_money(left)}." if left > 0 else " Оплачен полностью.")

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
        from .order_intake import parse_order_text
        parsed = parse_order_text(text)
        return {**parsed, "client": parsed.get("client", "")}

    def _new_order(self, text: str) -> str:
        from .order_intake import OrderIntake
        preview = OrderIntake(self.db).preview(text, "telegram")
        parsed = preview["parsed"]
        draft = preview["draft"]
        if not draft["product"]:
            return "Формат: «новый адресник 2шт 900р Мария»."
        order = self.manager.repo.save_order(draft)
        return (f"📝 Создан заказ №{order.get('number')} «{draft['product']}»"
                + (f" · {int(parsed['qty'])} шт" if parsed["qty"] > 1 else "")
                + (f" · {_money(draft['price'])}" if draft["price"] else "")
                + (f" · {draft['customer_name']}" if draft["customer_name"] else ""))

    # -------------------------------------------------------------- команды
    def _pick_printer(self, chat: str = ""):
        """Выбранный принтер чата или первый подключённый (мультипринтер)."""
        printer_id = self._printer_choice.get(chat, "") if chat else ""
        if printer_id:
            printer = self.manager.get(printer_id)
            if printer and printer.connected:
                return printer
        return self.manager.get()

    def _list_printers(self, chat: str) -> str:
        """Список принтеров с номерами для выбора команды «принтер N»."""
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        if not printers:
            return "Принтеры не добавлены."
        chosen = self._printer_choice.get(chat, "")
        lines = ["🖨 Принтеры (выбор: «принтер N»):"]
        for i, snap in enumerate(printers, 1):
            info = snap["printer"]
            mark = "◉" if snap["id"] == chosen else "·"
            lines.append(f"{mark} {i}. {snap['name']} — "
                         f"{STATE_RU.get(info['state'], info.get('state_label') or info['state'])}"
                         + (" · нет связи" if not snap["connection"]["connected"] else ""))
        return "\n".join(lines)

    def _select_printer(self, chat: str, index: int) -> str:
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        if not printers:
            return "Принтеры не добавлены."
        if index < 1 or index > len(printers):
            return f"Номер от 1 до {len(printers)}."
        snap = printers[index - 1]
        self._printer_choice[chat] = snap["id"]
        return f"◉ Выбран {snap['name']}. Команды (пауза/кадр/свет…) теперь идут на него."

    def do_command(self, command: str, ok_text: str, value=None, chat: str = "") -> str:
        printer = self._pick_printer(chat)
        if not printer:
            return "Принтеры не добавлены."
        if not printer.connected:
            return f"{printer.record.get('name', 'Принтер')} не на связи."
        if command == "pause":
            self.manager.mark_user_paused(printer.id)
        printer.command(command, value)
        if command == "resume":
            self.manager.clear_user_paused(printer.id)
        self.db.add_event("command", f"Telegram: {command}", ok_text, printer.id, {})
        return f"{ok_text} — {printer.record.get('name', 'принтер')}."

    def do_stop(self, chat: str, text: str) -> str:
        """Остановка печати — только с подтверждением: случайное «стоп» дорого стоит."""
        confirmed = any(word in text for word in ("да", "точно", "подтверждаю"))
        pending = self._pending_stop.get(chat, 0)
        if confirmed or time.time() - pending < 120:
            self._pending_stop.pop(chat, None)
            return self.do_command("stop", "Печать остановлена")
        self._pending_stop[chat] = time.time()
        return ("Остановить печать? Задание прервётся, деталь придётся печатать заново.\n"
                "Для подтверждения напишите: стоп да")
