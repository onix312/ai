"""Отчёты бота текстом и картинками.

Тонкий бот отвечает сам, без внешних окон и адресов — у него есть свои глаза
и рот:

* «статус», «принтеры» — сводка цеха словами, плюс кадр камеры печатающего
  станка;
* «заказы» (можно номером: «заказ 1043») — список или карточка заказа, плюс
  снимок изделия из фотоальбома заказа;
* «полка» — остатки, начиная с тех, что заканчиваются, плюс фото позиции;
* «очередь», «деньги», «кадр» — очередь печати, касса дня и кадр камеры.

Слои не нарушены: SQL живёт в `core/staff_service.py`, формулировки — в
`staffbot/report.py`, здесь только сборка данных и отправка. Картинка уходит
первым сообщением, текст — вторым: подпись к фото Telegram режет, а полный
текст в подпись не влезает, поэтому последним всегда остаётся sendMessage.

19.0: здесь же действия из кнопок уведомлений — «Следующее», «Продолжить»,
«Снял», «Повторить». Каждая кнопка отвечает в чате; физическую команду
«Продолжить» нажимает человек, поэтому она равна кнопке панели.
"""
from __future__ import annotations

import re as _re

from ...staff import gate
from .. import report
from ..core.staff_service import (
    count_inbox_unread,
    count_low_stock,
    count_orders_ready,
    finance_today_week,
    get_order,
    list_orders,
    list_shelf,
    order_photo_file,
    today_money,
)
from ..ui import main_menu_keyboard, markup_or_none


class ReportMixin:
    """Команды, которые отвечают текстом и фото прямо в чате."""

    ORDERS_LIMIT = 10
    SHELF_LIMIT = 12

    # ------------------------------------------------------------- данные
    def _snapshot(self) -> dict:
        """Снимок парка принтеров. Заглушки и ошибки — пустой снимок."""
        try:
            snapshot = self.manager.snapshot() if hasattr(self.manager, "snapshot") else {}
        except Exception:
            return {}
        return snapshot if isinstance(snapshot, dict) else {}

    def _report_printers(self) -> list[dict]:
        return report.printers_from_snapshot(self._snapshot())

    def _report_queue(self) -> list[dict]:
        try:
            queue = self.manager.queue() if hasattr(self.manager, "queue") else []
        except Exception:
            return []
        return queue if isinstance(queue, list) else []

    def _report_summary(self) -> dict:
        """Те же цифры, что на главном экране панели."""
        printers = self._report_printers()
        return {
            "total": len(printers),
            "online": len([p for p in printers if p.get("connected")]),
            "printing": len([p for p in printers if p.get("state") in ("RUNNING", "PREPARE")]),
            "queue": len(self._report_queue()),
            "orders_ready": count_orders_ready(self.db),
            "inbox": count_inbox_unread(self.db),
            "low_stock": count_low_stock(self.db),
            "today_money": today_money(self.db),
        }

    def _report_role(self, chat: str) -> str:
        try:
            return str(gate(self.db, str(chat)).get("role") or "")
        except Exception:
            return ""

    def _report_keyboard(self, chat: str) -> dict | None:
        return markup_or_none(main_menu_keyboard(self._report_role(chat)))

    # ---------------------------------------------------------- картинки
    def _camera_frame(self) -> tuple[bytes | None, str]:
        """Кадр камеры: сначала печатающий станок, потом любой живой.

        Возвращает сами байты JPEG и имя станка — по имени подписываем кадр,
        чтобы «кадр» не превращался в загадку из чьего он окна.
        """
        source = getattr(self.manager, "printers", None) or {}
        items = list(source.values()) if isinstance(source, dict) else list(source)
        printers = self._report_printers()

        def rank(printer) -> int:
            state = str(getattr(printer, "state", "") or "").upper()
            return {"RUNNING": 0, "PREPARE": 1}.get(state, 2)

        for printer in sorted(items, key=rank):
            frame = getattr(getattr(printer, "camera", None), "frame", None)
            if frame:
                return bytes(frame), report.printer_name(printer, printers)
        return None, ""

    def _photo_bytes(self, file_name: str) -> bytes | None:
        """Файл картинки из фото-каталога. Берём только базовое имя.

        В поле базы лежит имя файла, но данные могут приехать откуда угодно;
        `Path(...).name` не даёт выйти за каталог («../../etc/passwd»).
        """
        if not file_name:
            return None
        from pathlib import Path

        from ...config import PHOTO_DIR
        try:
            path = PHOTO_DIR / Path(str(file_name)).name
            return path.read_bytes() if path.is_file() else None
        except OSError:
            return None

    # ----------------------------------------------------------- отправка
    def _send_photo(self, chat: str, raw: bytes | None, caption: str = "") -> bool:
        """Отправить фото. Неудача не отменяет текст: фото — дополнение."""
        if not raw:
            return False
        try:
            self._call(
                "sendPhoto",
                {"chat_id": str(chat), "caption": report.limit(caption, report.CAPTION_LIMIT)},
                files={"photo": ("printflow.jpg", raw)},
            )
            return True
        except Exception:
            return False

    def _send_report(self, chat: str, text: str) -> None:
        try:
            self._reply(chat, report.limit(text), self._report_keyboard(chat))
        except Exception:
            pass

    # ----------------------------------------------------------- команды
    def cmd_status(self, chat: str, raw: str = "", text: str = "") -> None:
        """«статус» — сводка цеха и кадр с камеры, если есть что показать."""
        summary = self._report_summary()
        printers = self._report_printers()
        frame, name = self._camera_frame()
        if frame:
            self._send_photo(chat, frame, report.camera_caption(name, printers))
        self._send_report(chat, report.status_text(summary, printers))

    def cmd_printers(self, chat: str, raw: str = "", text: str = "") -> None:
        printers = self._report_printers()
        frame, name = self._camera_frame()
        if frame:
            self._send_photo(chat, frame, report.camera_caption(name, printers))
        self._send_report(chat, report.printers_text(printers))

    def cmd_queue(self, chat: str, raw: str = "", text: str = "") -> None:
        queue = self._report_queue()
        printers = self._report_printers()
        printing = [p for p in printers if p.get("state") in ("RUNNING", "PREPARE")]
        frame, name = self._camera_frame()
        if frame and printing:
            self._send_photo(chat, frame, report.camera_caption(name, printers))
        self._send_report(chat, report.queue_text(queue))

    def cmd_orders(self, chat: str, raw: str = "", text: str = "") -> None:
        """«заказы» — список; «заказ 1043» — карточка одного заказа с фото."""
        source = f"{raw or ''} {text or ''}"
        number = next((digits for digits in _re.findall(r"\d{2,}", source)), "")
        if number:
            order = get_order(self.db, number)
            if order:
                photo = self._photo_bytes(order_photo_file(self.db, str(order.get("id"))))
                if photo:
                    self._send_photo(chat, photo, report.order_caption(order))
                self._send_report(chat, report.order_card(order))
                return
            self._send_report(chat, f"📦 Заказ №{number} не найден — проверьте номер в панели.")
            return
        orders = list_orders(self.db, "all", self.ORDERS_LIMIT)
        if orders:
            order = next((row for row in orders if row.get("status") == "ready"), orders[0])
            photo = self._photo_bytes(order_photo_file(self.db, str(order.get("id"))))
            if photo:
                self._send_photo(chat, photo, report.order_caption(order))
        self._send_report(chat, report.orders_text(orders))

    def cmd_shelf(self, chat: str, raw: str = "", text: str = "") -> None:
        items = list_shelf(self.db)
        if items:
            photo_item = next((row for row in items if row.get("photo")), None)
            photo = self._photo_bytes((photo_item or {}).get("photo") or "")
            if photo:
                name = str((photo_item or {}).get("name") or "позиция")
                self._send_photo(chat, photo, report.limit(f"🛒 {name}", report.CAPTION_LIMIT))
        self._send_report(chat, report.shelf_text(items[: self.SHELF_LIMIT]))

    def cmd_money(self, chat: str, raw: str = "", text: str = "") -> None:
        """Деньги — только руководителю и владельцу."""
        if self._report_role(chat) not in ("owner", "manager"):
            self._send_report(chat, "💰 Деньги — только руководителю и владельцу.")
            return
        today, week = finance_today_week(self.db)
        self._send_report(chat, report.money_text(today, week))

    def cmd_frame(self, chat: str, raw: str = "", text: str = "") -> None:
        """«кадр» — фото с камеры; живое видео — в панели."""
        printers = self._report_printers()
        frame, name = self._camera_frame()
        if frame:
            self._send_photo(chat, frame, report.camera_caption(name, printers))
        else:
            self._send_report(chat, report.camera_absent_text(printers))

    # --------------------------------------------------------- callbacks
    # Кнопки меню зовут те же команды: у callback и слова один код.
    def cb_status(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_status(chat)

    def cb_printers(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_printers(chat)

    def cb_queue(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_queue(chat)

    def cb_orders(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_orders(chat)

    def cb_shelf(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_shelf(chat)

    def cb_money(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_money(chat)

    def cb_frame(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_frame(chat)

    # ------------------------------------------------- действия по уведомлениям
    # Кнопки в уведомлениях менеджера: «Следующее», «Продолжить», «Снял»,
    # «Повторить». Каждая отвечает в чате; физическая команда одна —
    # «Продолжить», и нажимает её человек, как кнопку панели.

    def _manager_printers(self) -> list:
        source = getattr(self.manager, "printers", None) or {}
        return list(source.values()) if isinstance(source, dict) else list(source)

    def cmd_next(self, chat: str, raw: str = "", text: str = "") -> None:
        """«Следующее» — что встанет на освободившийся станок (только превью)."""
        lines: list[str] = []
        for printer in self._manager_printers():
            state = str(getattr(printer, "state", "") or "").upper()
            if state in ("RUNNING", "PREPARE"):
                continue
            try:
                job = self.manager.next_job(getattr(printer, "id", ""))
            except Exception:
                job = None
            if job:
                lines.append(f"▸ {report.printer_name(printer, self._report_printers())}: "
                             + report.queue_line(job, 1))
        self._send_report(chat, "\n".join(lines) if lines else "🧾 Очередь пуста — станки свободны.")

    def cb_next(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_next(chat)

    def cmd_resume(self, chat: str, raw: str = "", text: str = "") -> None:
        """«Продолжить» — снять паузу с тех станков, что на паузе."""
        resumed, failed = [], []
        for printer in self._manager_printers():
            state = str(getattr(printer, "state", "") or "").upper()
            if state not in ("PAUSE", "PAUSED"):
                continue
            name = report.printer_name(printer, self._report_printers())
            try:
                printer.command("resume")
            except Exception as exc:
                failed.append(f"{name}: {exc}")
                continue
            resumed.append(name)
        if resumed:
            self._send_report(chat, "▶ Продолжаю: " + ", ".join(resumed) + ".")
        elif failed:
            self._send_report(chat, "Не получилось: " + "; ".join(failed) + ".")
        else:
            self._send_report(chat, "На паузе никого нет — все станки работают или свободны.")

    def cb_resume(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_resume(chat)

    def cmd_removed(self, chat: str, raw: str = "", text: str = "") -> None:
        """«Снял» — зафиксировать, что деталь снята со стола."""
        printers = self._report_printers()
        target = ""
        for printer in self._manager_printers():
            state = str(getattr(printer, "state", "") or "").upper()
            if state in ("FINISH", "IDLE"):
                target = getattr(printer, "id", "")
                break
        if not target:
            try:
                job = self.db.one(
                    "SELECT printer_id FROM print_jobs WHERE state='done'"
                    " ORDER BY datetime(finished_at) DESC LIMIT 1")
                target = str((job or {}).get("printer_id") or "")
            except Exception:
                target = ""
        try:
            if hasattr(self.manager, "part_removed"):
                self.manager.part_removed(target)
        except Exception as exc:
            return self._send_report(chat, f"Не получилось отметить: {exc}")
        name = ""
        if target and hasattr(self.manager, "get"):
            try:
                name = report.printer_name(self.manager.get(target), printers)
            except Exception:
                name = ""
        self._send_report(chat, "🤚 Зафиксировал: деталь снята" + (f" — {name}." if name else "."))

    def cb_removed(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_removed(chat)

    def cmd_reprint(self, chat: str, raw: str = "", text: str = "") -> None:
        """«Повторить» — честно: повтор требует разбора причины в панели."""
        try:
            job = self.db.one(
                "SELECT j.*, o.number AS order_number FROM print_jobs j"
                " LEFT JOIN orders o ON o.id=j.order_id"
                " WHERE j.state='failed' ORDER BY datetime(j.finished_at) DESC LIMIT 1")
        except Exception:
            job = None
        if not job:
            return self._send_report(chat, "✅ Сорванных заданий нет — повторять нечего.")
        name = str(job.get("file") or job.get("name") or "задание")
        order = str(job.get("order_number") or "")
        self._send_report(
            chat,
            "↻ Последнее сорванное: " + name + (f" (заказ №{order})" if order else "") + ".\n"
            "Повтор запускается в панели: сначала разберитесь с причиной брака,\n"
            "потом «Повторить» в карточке задания — станок получит его заново.",
        )

    def cb_reprint(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_reprint(chat)
