"""Menu handler — меню, помощь и код участника.

Меню по-прежнему ведёт в Mini App цеха, если адрес настроен. Если нет — бот не
притворяется, что кнопка работает: он объясняет, что заполнить в панели
(18.12.2) и показывает строку сводки, а рядом кнопки текстовых отчётов —
статус, заказы, полка, кадр отвечают в чате без внешнего адреса (18.12.3).
"""
from __future__ import annotations

import json

from .. import report
from ..core.config import get_miniapp_url, miniapp_hint, miniapp_state
from ..ui import HELP, help_keyboard, main_menu_keyboard, markup_or_none, web_app_keyboard


class MenuMixin:
    """Примесь с командами меню для StaffBot."""

    def _miniapp_url(self) -> str:
        try:
            return get_miniapp_url(self.db)
        except Exception:
            return ""

    def _miniapp_ready(self) -> bool:
        try:
            return bool(miniapp_state(self.db)["ready"])
        except Exception:
            return False

    def _miniapp_hint(self) -> str:
        try:
            return miniapp_hint(self.db)
        except Exception:
            return ""

    def _send_main_menu(self, chat: str, text: str = "") -> None:
        url = self._miniapp_url()
        ready = self._miniapp_ready()
        kb = markup_or_none(main_menu_keyboard(url, self._report_role(chat)))
        if ready:
            txt = text or "🏭 Цех NOZZA — откройте Mini App, там всё: полка, касса, очередь, принтеры, заказы, inbox, деньги."
        else:
            # Кнопку web_app Telegram не примет (не https или пусто) — вместо
            # мёртвой кнопки объясняем, что настроить.
            txt = (text + "\n\n" if text else "") + self._miniapp_hint()
        # Строка сводки: даже без Mini App видно, что в цеху происходит.
        line = self._summary_line()
        if line:
            txt = txt + "\n\n" + line
        try:
            self._call(
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": txt[:3800],
                    **({"reply_markup": json.dumps(kb, ensure_ascii=False)} if kb else {}),
                    "disable_web_page_preview": "true",
                },
                timeout=15,
            )
        except Exception:
            try:
                self._reply(chat, txt, kb)
            except Exception:
                pass

    def _summary_line(self) -> str:
        """Одна строка «что в цеху» для меню: цифры те же, что в отчёте."""
        try:
            return report.summary_line(self._report_summary())
        except Exception:
            return ""

    def _send_help(self, chat: str) -> None:
        url = self._miniapp_url()
        kb = markup_or_none(help_keyboard(url))
        txt = HELP
        if not self._miniapp_ready():
            txt = HELP + "\n\n" + self._miniapp_hint()
        try:
            self._call(
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": txt[:3800],
                    **({"reply_markup": json.dumps(kb, ensure_ascii=False)} if kb else {}),
                    "disable_web_page_preview": "true",
                },
                timeout=15,
            )
        except Exception:
            self._reply(chat, txt, kb)

    def _send_code(self, chat: str) -> None:
        txt = f"Ваш chat_id: {chat}\nПередайте его владельцу или используйте код приглашения: /start pf-XXXX"
        url = self._miniapp_url()
        kb = markup_or_none(web_app_keyboard(url))
        self._reply(chat, txt, kb)

    # --- команды, на которые указывает router
    def cmd_menu(self, chat: str, raw: str, text: str) -> None:
        self._send_main_menu(chat)

    def cmd_help(self, chat: str, raw: str, text: str) -> None:
        self._send_help(chat)

    def cmd_code(self, chat: str, raw: str, text: str) -> None:
        self._send_code(chat)

    def cb_menu(self, chat: str, message_id: str, params: str) -> None:
        self._send_main_menu(chat)

    def cb_help(self, chat: str, message_id: str, params: str) -> None:
        self._send_help(chat)

    def cb_code(self, chat: str, message_id: str, params: str) -> None:
        self._send_code(chat)

    def cb_goto(self, chat: str, message_id: str, params: str) -> None:
        # goto всегда в меню
        self._send_main_menu(chat)

    # совместимость со старыми callback kind=text
    def cb_open(self, chat: str, message_id: str, params: str) -> None:
        self._send_main_menu(chat)
