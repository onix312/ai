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

from .accounting import num
from .config import now_iso

API = "https://api.telegram.org/bot{token}/{method}"

HELP = """PrintFlow — управление печатью с телефона.

Что умею:
• статус — что печатает каждый принтер
• кадр — свежий снимок с камеры
• очередь — что ждёт запуска
• деньги — выручка, прибыль и долги за месяц
• день — итоги сегодняшнего дня
• пауза / продолжить — управление печатью
• свет — включить или выключить подсветку
• стоп — прервать печать (нужно подтверждение: стоп да)

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
                    "allowed_updates": json.dumps(["message"]),
                })
                for update in (result.get("result") or []):
                    self._offset = max(self._offset, num(update.get("update_id")) + 1)
                    self._handle(update, str(settings.get("telegram_chat_id")))
            except Exception:
                self._stop.wait(10)

    # -------------------------------------------------------------- разбор
    def _handle(self, update: dict, owner: str) -> None:
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

    def _dispatch(self, chat: str, raw: str) -> None:
        text = raw.lower().lstrip("/").replace("ё", "е").strip()
        word = text.split()[0] if text else ""

        if word in ("start", "help", "старт", "помощь", "меню", "?"):
            return self._reply(chat, HELP)
        if word in ("статус", "status", "принтер", "принтеры"):
            return self._reply(chat, self.text_status())
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
        if word in ("стоп", "stop"):
            return self._reply(chat, self.do_stop(chat, text))
        self._reply(chat, "Не понял команду. Напишите «помощь» — покажу список.")

    # -------------------------------------------------------------- ответы
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
