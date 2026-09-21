"""Menu handler — единственный интерактив тонкого бота.

Все сообщения ведут в Mini App цеха.
"""
from __future__ import annotations

import json

from ..core.config import get_miniapp_url
from ..ui import HELP, main_menu_keyboard, help_keyboard, web_app_keyboard


class MenuMixin:
    """Примесь с командами меню для StaffBot."""

    def _miniapp_url(self) -> str:
        try:
            return get_miniapp_url(self.db)
        except Exception:
            return "https://example.com/staff"

    def _send_main_menu(self, chat: str, text: str = "") -> None:
        url = self._miniapp_url()
        kb = main_menu_keyboard(url)
        txt = text or "🏭 Цех NOZZA — откройте Mini App, там всё: полка, касса, очередь, принтеры, заказы, inbox, деньги."
        try:
            self._call(
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": txt[:3800],
                    "reply_markup": json.dumps(kb, ensure_ascii=False),
                    "disable_web_page_preview": "true",
                },
                timeout=15,
            )
        except Exception:
            try:
                self._reply(chat, txt, kb)
            except Exception:
                pass

    def _send_help(self, chat: str) -> None:
        url = self._miniapp_url()
        kb = help_keyboard(url)
        try:
            self._call(
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": HELP[:3800],
                    "reply_markup": json.dumps(kb, ensure_ascii=False),
                    "disable_web_page_preview": "true",
                },
                timeout=15,
            )
        except Exception:
            self._reply(chat, HELP, kb)

    def _send_code(self, chat: str) -> None:
        txt = f"Ваш chat_id: {chat}\nПередайте его владельцу или используйте код приглашения: /start pf-XXXX"
        url = self._miniapp_url()
        kb = web_app_keyboard(url)
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
