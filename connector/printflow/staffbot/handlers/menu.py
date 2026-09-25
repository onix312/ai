"""Menu handler — меню, помощь и код участника.

19.0: Mini App убран — меню это сообщение с кнопками (ui.main_menu_keyboard):
ассистент, отчёты, деньги по роли, помощь и код. Никаких адресов и web_app.
"""
from __future__ import annotations

import json

from .. import report
from ..ui import HELP, help_keyboard, main_menu_keyboard, markup_or_none


class MenuMixin:
    """Примесь с командами меню для StaffBot."""

    def _menu_keyboard(self, chat: str) -> dict:
        return main_menu_keyboard(self._report_role(chat))

    def _send_main_menu(self, chat: str, text: str = "") -> None:
        kb = self._menu_keyboard(chat)
        txt = text or (
            "🏭 Цех NOZZA — кнопки под сообщением: статус, принтеры, заказы, "
            "полка, очередь, кадр. 🤖 Ассистент ответит на вопрос словами."
        )
        # Строка сводки: что в цеху происходит прямо сейчас.
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
        kb = markup_or_none(help_keyboard(self._report_role(chat)))
        txt = HELP
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
        self._reply(chat, txt, self._menu_keyboard(chat))

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
