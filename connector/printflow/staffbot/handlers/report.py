"""Отчёты бота текстом и картинками — цех без Mini App (18.12.3).

До 18.12.3 тонкий бот умел только одно: показать кнопку web_app «Открыть цех».
Если внешнего HTTPS-адреса нет (он не нужен до первого телефона), на любой
вопрос отвечало «откройте цех» — а открывать было нечего. Теперь у бота есть
свои глаза и рот:

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
"""
from __future__ import annotations

import re as _re

from ...staff import gate
from .. import report
from ..core.config import get_miniapp_url
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
from ..ui import markup_or_none, report_keyboard


class ReportMixin:
    """Команды, которые отвечают текстом и фото, не открывая Mini App."""

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
        """Те же цифры, что показывает главный экран Mini App."""
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
        try:
            url = get_miniapp_url(self.db)
        except Exception:
            url = ""
        return markup_or_none(report_keyboard(url, self._report_role(chat)))

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
        """Деньги — как в Mini App: только руководителю и владельцу."""
        if self._report_role(chat) not in ("owner", "manager"):
            self._send_report(chat, "💰 Деньги — только руководителю и владельцу.")
            return
        today, week = finance_today_week(self.db)
        self._send_report(chat, report.money_text(today, week))

    def cmd_frame(self, chat: str, raw: str = "", text: str = "") -> None:
        """«кадр» — фото с камеры; живое видео остаётся в Mini App и панели."""
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
