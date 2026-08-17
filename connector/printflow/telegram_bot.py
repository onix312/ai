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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .accounting import num
from .config import now_iso

API = "https://api.telegram.org/bot{token}/{method}"

HELP = """PrintFlow 5.0 — панель в кармане.

Кнопки внизу или команды (без слэша, в любом регистре):
• панель — всё сразу: печать, деньги, план, долги
• статус · кадр · очередь — что происходит сейчас
• деньги · день — финансы
• план — что печатать сегодня
• продажа — продать со стеллажа (−1 шт)
• выдать 1001 — закрыть заказ, зачислить оплату, текст клиенту
• оплата 1500 по 1001 — принять оплату
• статус 1001 печать — сменить статус заказа
• новый адресник 2шт 900р Мария — заказ из текста
• пауза / продолжить / свет — управление
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

    def __init__(self, manager):
        self.manager = manager
        self.db = manager.db
        self._stop = threading.Event()
        self._offset = 0
        self._pending_stop: dict[str, float] = {}
        self._thread = threading.Thread(target=self._loop, name="pf-bot", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- транспорт
    def shutdown(self) -> None:
        self._stop.set()

    def _settings(self) -> dict:
        return self.db.settings(include_secrets=True)

    def _call(self, method: str, params: dict, timeout: int = 35) -> dict:
        token = self._settings().get("telegram_token", "")
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
        keyboard = [
            [{"text": "🖨 Панель"}, {"text": "📷 Кадр"}],
            [{"text": "≡ Очередь"}, {"text": "₽ Деньги"}],
            [{"text": "⚑ План"}, {"text": "🛍 Продажа"}],
            [{"text": "📊 Итоги"}],
        ]
        self._call("sendMessage", {
            "chat_id": chat, "text": text[:3800], "disable_web_page_preview": "true",
            "reply_markup": json.dumps({"keyboard": keyboard, "resize_keyboard": True}),
        }, timeout=15)

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
                for update in (result.get("result") or []):
                    self._offset = max(self._offset, num(update.get("update_id")) + 1)
                    self._handle(update, str(settings.get("telegram_chat_id")))
                self._maybe_digest(settings)
                self._maybe_weekly(settings)
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
        chat = str(settings.get("telegram_chat_id") or "")
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
        chat = str(settings.get("telegram_chat_id") or "")
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
        text = (message.get("text") or "").strip()
        if not chat or not text:
            return
        if chat != owner:
            # Чужой чат: вежливо отказываем и пишем в журнал.
            self._reply(chat, "Этот бот приватный и отвечает только владельцу.")
            self.db.add_event("bot", "Посторонний в Telegram-боте",
                              f"chat_id {chat}: {text[:80]}", "", {})
            return
        try:
            self._dispatch(chat, text)
        except Exception as exc:
            self._reply(chat, f"Не получилось: {exc}")

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
            return self.do_command("pause", "Печать поставлена на паузу")
        if command == "resume":
            return self.do_command("resume", "Печать продолжена")
        if command == "light":
            return self.do_command("light", "Подсветка переключена")
        if command == "stop":
            return self.do_stop(chat or "", "стоп" if chat else "стоп да")
        if command == "frame" or command == "кадр":
            if chat:
                self.send_frame(chat)
                return "Кадр отправлен."
            return "Кадр недоступен"
        if command == "panel":
            return self.text_panel()
        if command == "plan":
            return self.text_plan()
        if command.startswith("sell:"):
            return self.do_sell(command.split(":", 1)[1])
        return "Не понял команду."

    def _dispatch(self, chat: str, raw: str) -> None:
        text = raw.lower().lstrip("/").replace("ё", "е")
        for emoji in "🖨📷≡₽⚑🛍📊▦▤·":
            text = text.replace(emoji, " ")
        text = text.strip()
        word = text.split()[0] if text else ""

        if word in ("start", "help", "старт", "помощь", "меню", "?"):
            return self._reply_keyboard(chat, HELP)
        if word in ("панель", "panel", "дашборд"):
            return self._reply_keyboard(chat, self.text_panel())
        if word in ("план", "plan", "печатать"):
            return self._reply(chat, self.text_plan())
        if word in ("продажа", "продать", "продажи", "sell"):
            return self.sell_keyboard(chat)
        if word in ("оплата", "оплатить", "payment"):
            return self._reply(chat, self._pay(text))
        if word in ("статус", "status", "принтер", "принтеры"):
            # «статус 1001 печать» — смена статуса; иначе состояние принтеров
            digits = any(w.isdigit() for w in text.split()[1:])
            return self._reply(chat, self._set_status(text) if digits else self.text_status())
        if word in ("выдать", "выдал", "закрыть"):
            number = next((w for w in text.split()[1:] if w.isdigit()), "")
            return self._reply(chat, self._fulfill(number) if number
                               else "Укажите номер: «выдать 1001».")
        if word in ("новый", "заказ", "создать"):
            return self._reply(chat, self._new_order(text))
        if word in ("кадр", "камера", "фото", "photo", "cam"):
            return self.send_frame(chat)
        if word in ("очередь", "queue"):
            return self._reply(chat, self.text_queue())
        if word in ("деньги", "финансы", "money", "прибыль"):
            return self._reply(chat, self.text_money())
        if word in ("день", "сегодня", "итоги"):
            return self._reply(chat, self.text_today())
        if word in ("пауза", "pause"):
            return self._reply(chat, self.do_command("pause", "Печать поставлена на паузу"))
        if word in ("продолжить", "resume", "старт-печати"):
            return self._reply(chat, self.do_command("resume", "Печать продолжена"))
        if word in ("свет", "light"):
            return self._reply(chat, self.do_command("light", "Подсветка переключена"))
        if word in ("снял", "снято", "забрал"):
            result = self.manager.part_removed()
            return self._reply(chat, f"✅ Деталь снята. Простой после печати {result.get('idle_min', 0)} мин.")
        if word in ("стоп", "stop"):
            return self._reply(chat, self.do_stop(chat, text))
        if word in ("готов", "ready", "выдан"):
            return self._reply(chat, self.order_ready(text))
        self._reply(chat, "Не понял команду. Напишите «помощь» — покажу список.")

    # -------------------------------------------------------------- ответы
    def order_ready(self, raw: str) -> str:
        """«готов 1001» — перевести заказ в статус ready и дать шаблон клиенту."""
        words = raw.lower().replace("ё", "е").split()
        number = next((w for w in words[1:] if w.isdigit()), "")
        if not number:
            return ("Укажите номер заказа: «готов 1001».\n"
                    "Заказ перейдёт в статус «Готов», я пришлю текст для клиента.")
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        ready = self.db.one("SELECT id FROM statuses WHERE id='ready'")
        if ready and order.get("status") != "ready":
            self.db.execute("UPDATE orders SET status='ready', updated_at=? WHERE id=?",
                            (now_iso(), order["id"]))
            self.db.add_event("order", "Заказ готов (Telegram)",
                              f"№{order.get('number')} · {order.get('product')}",
                              data={"order_id": order["id"]})
        name = (order.get("customer_name") or "").strip()
        hello = f", {name}," if name else ","
        return (f"Заказ №{order.get('number')} готов ✓\n\nШаблон для клиента:\n"
                f"Здравствуйте{hello} ваш заказ «{order.get('product') or ''}» готов.\n"
                f"Можно забрать {order.get('due') or 'в удобное время'}. Спасибо!")
        """Отправить сообщение в канал клиента PrintFlow не может — это шаблон для копирования."""

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
                if job.get("spent"):
                    lines.append(f"Потрачено материалов на {_money(job['spent'])}")
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
        return "\n".join(lines)

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

    def send_frame(self, chat: str) -> None:
        printer = self.manager.get()
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
        token = self._settings().get("telegram_token", "")
        try:
            self.manager._send_photo(token, chat, caption, frame)
            printer.camera.snapshot(note="Запрос из Telegram")
        except Exception as exc:
            self._reply(chat, f"Не удалось отправить кадр: {exc}")

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

    def _sell_rows(self) -> list[dict]:
        """Позиции номенклатуры с остатком для продажи со стеллажа."""
        from .nomenclature import Nomenclature
        items = Nomenclature(self.db).items()
        rows = [i for i in items if num(i.get("qty")) > 0 and num(i.get("price")) > 0]
        rows.sort(key=lambda i: -num(i["qty"]))
        return rows[:8]

    def sell_keyboard(self, chat: str) -> None:
        rows = self._sell_rows()
        if not rows:
            self._reply(chat, "Нет позиций с остатком для продажи. Добавьте приход на склад.")
            return
        lines = ["🛍 Продажа со стеллажа — нажмите «−1»:"]
        buttons = []
        for i in rows:
            lines.append(f"• {i['name']} — {int(num(i['qty']))} шт · {_money(i['price'])}")
            buttons.append([{"text": f"−1 · {i['name'][:24]}", "callback_data": f"cmd:sell:{i['id']}"}])
        self._call("sendMessage", {"chat_id": chat, "text": "\n".join(lines)[:3800],
                                   "reply_markup": json.dumps({"inline_keyboard": buttons})}, timeout=15)

    def do_sell(self, nom_id: str) -> str:
        from .documents import Documents
        from .nomenclature import Nomenclature
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

    def _final_status_id(self) -> str | None:
        row = self.db.one("SELECT id FROM statuses WHERE is_final=1 ORDER BY position LIMIT 1")
        return (row or {}).get("id")

    def _fulfill(self, number: str) -> str:
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        final = self._final_status_id()
        if not final:
            return "Не настроен финальный статус."
        if order.get("status") == final:
            return f"Заказ №{number} уже выдан."
        rest = round(max(0.0, num(order.get("price")) -
                          max(num(order.get("paid")), num(order.get("prepaid")))), 2)
        if rest > 0 and self.db.setting("auto_income_on_done", True):
            self.manager.acc.add_payment(order["id"], rest, "payment",
                                         order.get("account_id") or "", "выдача из Telegram")
        self.db.execute("UPDATE orders SET status=?, closed_at=?, updated_at=? WHERE id=?",
                        (final, now_iso(), now_iso(), order["id"]))
        self.db.add_event("order", "Заказ выдан (Telegram)",
                          f"№{order.get('number')} · {order.get('product')}",
                          data={"order_id": order["id"]})
        name = (order.get("customer_name") or "").strip()
        return (f"✅ Заказ №{order.get('number')} выдан."
                + (f" Зачислено {_money(rest)}." if rest > 0 else "")
                + (f"\n\nТекст клиенту:\nЗдравствуйте{', ' + name if name else ''}! "
                   f"Ваш заказ «{order.get('product') or ''}» готов, можно забирать. Спасибо!"))

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
        self.manager.acc.add_payment(order["id"], amount, "payment",
                                     order.get("account_id") or "", "оплата из Telegram")
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
        import re as _re
        body = text.split(None, 1)[1] if " " in text else ""
        price = 0.0
        m = _re.search(r"(\d[\d\s.,]*)\s*(?:р|руб|₽)", body)
        if m:
            price = num(m.group(1).replace(" ", "").replace(",", "."))
            body = body.replace(m.group(0), " ")
        qty = 1.0
        m = _re.search(r"(\d+)\s*шт", body)
        if m:
            qty = float(m.group(1))
            body = body.replace(m.group(0), " ")
        tokens = [t for t in body.split() if t.strip()]
        product, client = " ".join(tokens), ""
        if len(tokens) >= 2 and tokens[-1].isalpha() and len(tokens[-1]) > 1:
            client = tokens[-1]
            product = " ".join(tokens[:-1])
        return {"product": product.strip() or "Заказ", "client": client,
                "price": price, "qty": qty}

    def _new_order(self, text: str) -> str:
        parsed = self._parse_new_order(text)
        if not parsed["product"]:
            return "Формат: «новый адресник 2шт 900р Мария»."
        order = self.manager.repo.save_order({
            "product": parsed["product"], "customer_name": parsed["client"],
            "status": "new", "qty": parsed["qty"], "price": parsed["price"],
            "channel": "telegram", "notes": "заказ из Telegram",
        })
        return (f"📝 Создан заказ №{order.get('number')} «{parsed['product']}»"
                + (f" · {int(parsed['qty'])} шт" if parsed["qty"] > 1 else "")
                + (f" · {_money(parsed['price'])}" if parsed["price"] else "")
                + (f" · {parsed['client']}" if parsed["client"] else ""))

    # -------------------------------------------------------------- команды
    def do_command(self, command: str, ok_text: str) -> str:
        printer = self.manager.get()
        if not printer:
            return "Принтеры не добавлены."
        if not printer.connected:
            return f"{printer.record.get('name', 'Принтер')} не на связи."
        printer.command(command)
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
