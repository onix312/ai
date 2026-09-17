"""Управление принтерами из Telegram: команды, камера, слежка, дашборд.

Опасные для печати действия (стоп) требуют подтверждения; пауза/продолжить/
свет идут сразу — это обычная рутина у станка. «Живой» дашборд обновляет
одно сообщение, пока идёт печать; слежка за заказом присылает прогресс
каждые 10 % в тот чат, из которого попросили.
"""
from __future__ import annotations

import re as _re
import time

from ..accounting import num, uid
from .ui import STATE_RU, hm, money


class PrintersMixin:
    """Команды принтерам и всё, что смотрит на печать."""

    # ---------------------------------------------------------- выбор парка
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

    def control_keyboard(self) -> dict:
        """Панель управления печатью для карточки принтера."""
        from .ui import keyboard
        return keyboard(
            [("❙❙ Пауза", "cmd:pause"), ("▶ Продолжить", "cmd:resume")],
            [("☀ Свет", "cmd:light"), ("◉ Кадр", "cmd:frame")],
            [("■ Стоп", "cmd:stop")])

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

    # -------------------------------------------------------------- камера
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
                caption += f", осталось {hm(info['remaining_min'])}"
        if snap.get("camera", {}).get("demo"):
            caption += " (демо-кадр: принтер не подключён)"
        token = self._settings().get("telegram_token", "")
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
        token = self._settings().get("telegram_token", "")
        if len(photos) == 1:
            self.manager._send_photo(token, chat, "Последний кадр печати", photos[0])
            return None
        self._send_media_group(token, chat, photos)

    # ------------------------------------------------------- живой дашборд
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

    # -------------------------------------------------------------- слежка
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

    # ---------------------------------------------------- текстовые команды
    def cmd_queue(self, chat: str, raw: str, text: str) -> None:
        self.queue_keyboard(chat)

    def cmd_frame(self, chat: str, raw: str, text: str) -> None:
        self.send_frame(chat)

    def cmd_timelapse(self, chat: str, raw: str, text: str) -> None:
        self.send_timelapse(chat)

    def cmd_live(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.start_live(chat))

    def cmd_live_stop(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.stop_live(chat))

    def cmd_status(self, chat: str, raw: str, text: str) -> None:
        """«принтер N» — выбор; «статус 1001 печать» — смена статуса; иначе обзор."""
        words = text.split()
        word = words[0] if words else ""
        if word in ("принтер", "принтеры"):
            number = next((w for w in words[1:] if w.isdigit()), "")
            if number:
                return self._reply(chat, self._select_printer(chat, int(number)))
            if any(w.isdigit() for w in words[1:]):
                return self._reply(chat, self._set_status(text))
            return self._reply(chat, self._list_printers(chat))
        digits = any(w.isdigit() for w in words[1:])
        return self._reply(chat, self._set_status(text) if digits else self.text_status())

    def cmd_pause(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.do_command("pause", "Печать поставлена на паузу", chat=chat))

    def cmd_resume(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.do_command("resume", "Печать продолжена", chat=chat))

    def cmd_light(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.do_command("light", "Подсветка переключена", chat=chat))

    def cmd_stop(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.do_stop(chat, text))

    def cmd_skip(self, chat: str, raw: str, text: str) -> None:
        number = next((w for w in text.split()[1:] if w.isdigit()), "")
        if not number:
            return self._reply(chat, "Формат: «пропустить 2» — исключить объект N из печати.")
        self._reply(chat, self.do_command("skip_objects", [int(number)],
                                          f"Объект {number} исключён из печати", chat=chat))

    def cmd_flow(self, chat: str, raw: str, text: str) -> None:
        m = _re.search(r"(\d+)", text)
        if not m:
            return self._reply(chat, "Формат: «поток 90» — процент подачи филамента (50–150%).")
        self._reply(chat, self.do_command("flow", int(m.group(1)),
                                          f"Поток {m.group(1)}%", chat=chat))

    def cmd_reprint(self, chat: str, raw: str, text: str) -> None:
        number = next((w for w in text.split()[1:] if w.isdigit()), "")
        try:
            row = self.manager.reprint_last_failed(
                number, confirmed=True, request_id=uid("tg-reprint"))
            return self._reply(
                chat,
                f"↻ Повтор «{row.get('name')}» подготовлен. Запуск подтвердите отдельно.",
            )
        except Exception as exc:
            return self._reply(chat, f"Не получилось подготовить повтор: {exc}")

    def cmd_reorder(self, chat: str, raw: str, text: str) -> None:
        word = text.split()[0] if text else ""
        self._reply(chat, self._reorder_queue(text, word))

    def cmd_removed(self, chat: str, raw: str, text: str) -> None:
        result = self.manager.part_removed()
        self._reply(chat, f"✅ Деталь снята. Простой после печати {result.get('idle_min', 0)} мин.")

    def cmd_idle(self, chat: str, raw: str, text: str) -> None:
        stats = self.manager.idle_stats()
        self._reply(chat,
                    f"⏳ Простой: {stats.get('idle_hours')} ч "
                    f"({stats.get('idle_minutes')} мин)\n"
                    f"Упущено ~{money(stats.get('lost_profit'))} "
                    f"по норме {money(stats.get('rate_per_hour'))}/ч")

    def cmd_watch(self, chat: str, raw: str, text: str) -> None:
        number = next((w for w in text.split()[1:] if w.isdigit()), "")
        self._reply(chat, self._watch_order(chat, number))

    # ------------------------------------------------------- кнопки (callback)
    def cb_queue(self, chat: str, message_id: str, params: str) -> None:
        self.queue_keyboard(chat, message_id=message_id)

    def cb_pause(self, chat: str, params: str) -> str:
        return self.do_command("pause", "Печать поставлена на паузу", chat=chat)

    def cb_resume(self, chat: str, params: str) -> str:
        return self.do_command("resume", "Печать продолжена", chat=chat)

    def cb_light(self, chat: str, params: str) -> str:
        return self.do_command("light", "Подсветка переключена", chat=chat)

    def cb_stop(self, chat: str, params: str) -> str:
        return self.do_stop(chat, "стоп" if chat else "стоп да")

    def cb_frame(self, chat: str, params: str) -> str:
        if chat:
            self.send_frame(chat)
            return "Кадр отправлен."
        return "Кадр недоступен"

    def cb_next(self, chat: str, params: str) -> str:
        return self._start_next(chat)

    def cb_removed(self, chat: str, params: str) -> str:
        printer = self._pick_printer(chat)
        result = self.manager.part_removed(printer.id if printer else "")
        return f"✅ Деталь снята. Простой после печати {result.get('idle_min', 0)} мин."

    def cb_reprint(self, chat: str, params: str) -> str:
        try:
            row = self.manager.reprint_last_failed(
                confirmed=True, request_id=uid("tg-reprint"))
            return f"↻ Повтор «{row.get('name')}» подготовлен. Запуск подтвердите отдельно."
        except Exception as exc:
            return f"Не получилось подготовить повтор: {exc}"

    def cb_printers(self, chat: str, params: str) -> str:
        return self._list_printers(chat)

    def _handle_bed_photo_clearance(self, chat: str, photo: list, caption: str = "", printer=None) -> None:
        """Подтверждение очистки стола по присланному фото (Photo Clearance)."""
        file_id = str((photo[-1] or {}).get("file_id") or "")
        if not file_id:
            return self._reply(chat, "Не удалось получить фото.")
        raw = self._download_file(file_id)
        if not raw:
            return self._reply(chat, "Не удалось скачать фото.")

        p = printer or self._pick_printer(chat)
        pid = p.id if p else ""
        p_name = p.record.get("name", "Принтер") if p else "Принтер"

        # Сохраняем фото в директорию снимков стола/заказов
        from ..config import PHOTO_DIR
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"bed_clear_{pid or 'p'}_{int(time.time())}.jpg"
        (PHOTO_DIR / filename).write_bytes(raw)

        # Вызываем подтверждение снятия детали в диспетчере
        res = self.manager.part_removed(pid)
        idle_min = res.get("idle_min", 0)

        self.db.add_event(
            "production", "Стол очищен по фото (Telegram)",
            f"{p_name} · фото подтверждено оператором (файл {filename})",
            pid, {"photo": filename, "idle_min": idle_min}
        )

        msg = f"✅ Фото получено: стол {p_name} подтверждён чистым!\nПростой: {idle_min} мин."
        # Если есть задания в очереди, проверяем автозапуск следующего
        next_job = self.manager.next_job(pid) if pid else None
        if next_job and bool(self.db.setting("auto_queue", False)):
            msg += f"\n▶ Очередь разблокирована: готово к запуску «{next_job.get('name')}»."
        self._reply(chat, msg)
