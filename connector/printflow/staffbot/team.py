"""Команда NOZZA: участники и приглашения из чата.

Владелец делает всё; руководитель смотрит список и приглашает сотрудников
(но не руководителей и не убирает людей); сотруднику раздел недоступен.
Права тонкой настройки остаются в `staff.Staff` — модуль только связывает
чат с его API.
"""
from __future__ import annotations

from ..staff import Staff, gate


class TeamMixin:
    """Управление командой: список, добавить, пригласить, убрать."""

    def _staff_command(self, chat: str, action: str, text: str,
                       role: str = "") -> str:
        """Управление командой из чата: список, добавить, пригласить, убрать."""
        who = gate(self.db, chat)
        staff = Staff(self.db)
        if not who["role"]:
            return "Недоступно."
        is_owner = who["role"] == "owner"
        if action == "list":
            if not is_owner and who["role"] != "manager":
                return "Список команды — для владельца и руководителя."
            return staff.text_list()
        words = str(text).split()
        if action == "invite":
            if not (is_owner or who["role"] == "manager"):
                return "Приглашать может владелец или руководитель."
            role = "employee"
            name = ""
            for w in words[1:]:
                lowered = w.lower().replace("ё", "е")
                if lowered in ("сотрудник", "руководитель", "менеджер"):
                    role = "manager" if lowered == "руководитель" else "employee"
                else:
                    name += (" " if name else "") + w
            if role == "manager" and not is_owner:
                return "Руководителя может пригласить только владелец."
            code = staff.invite(role, name, created_by=str(chat))
            return (f"Код приглашения: {code.get('code')}\n"
                    f"Роль: {code.get('role_name')}"
                    + (f", имя: {name}" if name else "") +
                    "\nОтправьте код человеку — он пишет боту «старт "
                    f"{code.get('code')}» и попадает в команду. Код одноразовый.")
        if action == "add":
            if not is_owner:
                return "Добавлять участников может только владелец."
            digits = next((w for w in words if w.lstrip("-").isdigit()), "")
            name_words = [w for w in words[1:]
                          if not w.lstrip("-").isdigit()
                          and w.lower() != role]
            if not digits or not name_words:
                return ("Формат: «сотрудник Имя 123456» или "
                        "«руководитель Имя 123456».\n"
                        "chat_id человек узнает у бота командой «код».")
            try:
                member = staff.add(" ".join(name_words), role, digits)
            except ValueError as exc:
                return str(exc)
            return (f"✅ {member.get('name')} — {member.get('role_name')} "
                    f"(chat_id {member.get('chat_id')}).\n"
                    "Права: " + staff.rights_text(member.get("role")))
        if action == "remove":
            if not is_owner:
                return "Убирать участников может только владелец."
            ident = next((w for w in words[1:] if w), "")
            if not ident:
                return "Формат: «убрать 123456» (chat_id или имя)."
            try:
                row = staff.remove(ident)
            except ValueError as exc:
                return str(exc)
            return (f"✅ {row.get('name')} отключён от бота "
                    "(в панели можно вернуть).")
        return "Не понял команду команды 🙂 Напишите «команда»."

    # ------------------------------------------------------- кнопки (callback)
    def cb_team(self, chat: str, params: str) -> str:
        return self._staff_command(chat, "list", "")

    # ---------------------------------------------------- текстовые команды
    def cmd_team(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._staff_command(chat, "list", text))

    def cmd_invite(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._staff_command(chat, "invite", raw))

    def cmd_add_member(self, chat: str, raw: str, text: str) -> None:
        word = text.split()[0] if text else "сотрудник"
        self._reply(chat, self._staff_command(chat, "add", raw, role=word))

    def cmd_remove_member(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._staff_command(chat, "remove", raw))
