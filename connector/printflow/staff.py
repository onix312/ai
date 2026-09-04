"""Команда NOZZA: роли в Telegram-боте сотрудников.

Владелец — это chat_id из настроек (telegram_chat_id). Дополнительно можно
добавить руководителя и сотрудников: каждый со своим chat_id и ролью.
Роли не меняют панель (она локальная), они разграничивают команды бота:

• сотрудник — обзоры (панель, статус, очередь, кадр), полка: продажа и приход,
  каталог (просмотр), inbox клиентского бота, фото к заказу, слежка за заказом;
• руководитель — всё перечисленное плюс деньги, заказы (новый, статус, выдать,
  оплата), управление принтерами и правки каталога (цены, витрина);
• владелец — всё, плюс управление командой.

Пригласительные коды: «пригласить» в боте или кнопка в панели создаёт код
PF-XXXX; новый человек пишет боту «старт PF-XXXX» и автоматически получает
роль кода — не нужно узнавать и вводить chat_id руками.
"""
from __future__ import annotations

import secrets
from typing import Any

from .accounting import uid
from .config import now_iso

ROLES = ("owner", "manager", "employee")
ROLE_NAMES = {"owner": "владелец", "manager": "руководитель",
              "employee": "сотрудник"}

# Группы команд бота и права ролей. Группа — не «красивое имя», а способ
# запретить деньгам и управлению принтером утекать в чаты без полномочий.
# «catalog» — правки каталога (цены, публикация витрины, карточки позиций):
# это деньги и наружу видимый контент, поэтому сотруднику не выдаётся.
ROLE_RIGHTS: dict[str, set[str]] = {
    "owner": {"view", "shelf", "catalog", "orders", "finance", "printers", "staff", "inbox"},
    "manager": {"view", "shelf", "catalog", "orders", "finance", "printers", "inbox"},
    "employee": {"view", "shelf", "inbox"},
}

# Слово команды → группа прав (первое слово сообщения или имя callback-команды).
WORD_GROUPS: dict[str, str] = {
    # обзоры
    "панель": "view", "panel": "view", "дашборд": "view", "план": "view",
    "plan": "view", "печатать": "view", "очередь": "view", "queue": "view",
    "кадр": "view", "камера": "view", "фото": "view", "photo": "view", "cam": "view",
    "таймлапс": "view", "кадры": "view", "гиф": "view", "timelapse": "view",
    "видео": "view", "живой": "view", "live": "view", "стоп-живой": "view",
    "стопживой": "view", "филамент": "view", "пластик": "view", "катушки": "view",
    "спул": "view", "брак": "view", "дефект": "view", "дефекты": "view",
    # датчики и доктор: телеметрия и диагностика без изменения данных
    "датчики": "view", "сенсоры": "view", "ams": "view", "амс": "view", "доктор": "view",
    "диагностика": "view",
    "рейтинг": "view", "топ": "view", "abc": "view", "изделия": "view",
    "хвосты": "view", "хвост": "view", "дыры": "view", "проверка": "view",
    "сколько": "view", "что": "view", "когда": "view", "следи": "view",
    "подпишись": "view", "watch": "view", "следить": "view", "простой": "view",
    "idle": "view", "закупить": "view", "закупка": "view", "закупки": "view",
    "шоппинг": "view", "покупки": "view", "стеллаж": "view", "полка": "view",
    "витрина": "view", "shelf": "view", "движения": "view", "принтеры": "view",
    "статус": "view", "status": "view",
    # деньги
    "деньги": "finance", "финансы": "finance", "money": "finance",
    "прибыль": "finance", "день": "finance", "сегодня": "finance",
    "итоги": "finance", "долги": "finance", "должники": "finance",
    "debt": "finance", "долг": "finance", "заработал": "finance",
    "заработано": "finance", "касса": "finance", "забрали": "finance",
    # полка: продажи и приход
    "продажа": "shelf", "продать": "shelf", "продажи": "shelf", "sell": "shelf",
    "приход": "shelf", "положить": "shelf", "пополнить": "shelf",
    # каталог: просмотр — всем, правки — отдельная группа «catalog»
    "каталог": "view", "catalog": "view", "номенклатура": "view",
    "цена": "catalog", "скрыть": "catalog", "показать": "catalog",
    "описание": "catalog", "товар": "catalog", "норматив": "catalog",
    "минималка": "catalog", "архив": "catalog", "вернуть": "catalog",
    "удалить": "catalog", "пересчёт": "catalog", "пересчет": "catalog",
    "группы": "catalog",
    # заказы и клиенты
    "выдать": "orders", "выдал": "orders", "выдан": "orders",
    "закрыть": "orders", "готов": "orders", "ready": "orders",
    "новый": "orders", "заказ": "orders", "создать": "orders",
    "оплата": "orders", "оплатить": "orders", "payment": "orders",
    "чаты": "inbox", "диалоги": "inbox", "inbox": "inbox", "клиенты": "inbox",
    "кответ": "inbox", "ответить": "inbox", "creply": "inbox",
    "отзыв": "inbox", "клиент": "inbox",
    # 12.1: включение/выключение клиентского бота — только владелец
    "клиент-бот": "staff", "кбот": "staff",
    "подтвердить": "orders", "отклонить": "orders",
    # принтер
    "пауза": "printers", "pause": "printers", "продолжить": "printers",
    "resume": "printers", "старт-печати": "printers", "свет": "printers",
    "light": "printers", "стоп": "printers", "stop": "printers",
    "пропустить": "printers", "скип": "printers", "исключить": "printers",
    "skip": "printers", "поток": "printers", "flow": "printers",
    "повторить": "printers", "перепечатать": "printers", "reprint": "printers",
    "повтор": "printers", "принтер": "printers", "выше": "printers",
    "ниже": "printers",
    # команда
    "команда": "staff", "сотрудники": "staff", "team": "staff",
    "пригласить": "staff", "сотрудник": "staff", "руководитель": "staff",
    "убрать": "staff",
}

# Callback-команды (inline-кнопки) → группа прав.
CALLBACK_GROUPS: dict[str, str] = {
    "pause": "printers", "resume": "printers", "light": "printers",
    "stop": "printers", "next": "printers", "reprint": "printers",
    "removed": "view",
    "frame": "view", "panel": "view", "plan": "view", "shelf:needs": "view",
    "sensors": "view", "doctor": "view", "cbot_tpl": "inbox",
    "shelf": "shelf", "sell-menu": "shelf", "shelf-prod-menu": "shelf",
    "shelf-moves": "shelf", "shelf-sales7": "shelf", "shelf-sales30": "shelf",
    "shelf-cash": "shelf", "shelf-cash-w": "shelf",
    "help": "view", "goto": "view",
    # каталог: список и карточка — просмотр, действия — правки
    "cat": "view", "cati": "view",
    "cat-hide": "catalog", "cat-show": "catalog", "cat-archive": "catalog",
    "cat-restore": "catalog", "cat-recalc": "catalog", "cat-del": "catalog",
    "cat-delyes": "catalog", "cat-grps": "catalog", "cat-grp": "catalog",
    "cat-vitrine": "catalog",
}


def normalize_role(value: object, default: str = "employee") -> str:
    role = str(value or "").strip().lower()
    aliases = {"руководитель": "manager", "менеджер": "manager",
               "сотрудник": "employee", "владелец": "owner"}
    role = aliases.get(role, role)
    return role if role in ROLES else default


class Staff:
    """Работа с таблицей staff и приглашениями поверх Database."""

    def __init__(self, db):
        self.db = db

    # -------------------------------------------------------------- список
    def all(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM staff"
        if active_only:
            sql += " WHERE active=1"
        rows = self.db.query(sql + " ORDER BY datetime(created_at), name")
        for row in rows:
            row["role_name"] = ROLE_NAMES.get(row.get("role"), "сотрудник")
        return rows

    def by_chat(self, chat_id: str) -> dict | None:
        if not chat_id:
            return None
        row = self.db.one(
            "SELECT * FROM staff WHERE chat_id=? AND active=1", (str(chat_id),))
        if row:
            row["role_name"] = ROLE_NAMES.get(row.get("role"), "сотрудник")
        return row

    def add(self, name: str, role: str, chat_id: str, note: str = "",
            tg_user_id: str = "") -> dict:
        name = (name or "").strip() or "Без имени"
        role = normalize_role(role)
        if role == "owner":
            raise ValueError("Владелец задаётся настройкой Chat ID — роль не выдаётся кодом")
        chat_id = str(chat_id or "").strip()
        if not chat_id or not chat_id.lstrip("-").isdigit():
            raise ValueError("chat_id должен быть числом Telegram (узнать: «код» в боте)")
        existing = self.db.one("SELECT * FROM staff WHERE chat_id=?", (chat_id,))
        if existing:
            row = self.db.upsert("staff", {
                "id": existing["id"], "name": name, "role": role,
                "chat_id": chat_id, "note": note or existing.get("note") or "",
                "tg_user_id": tg_user_id or existing.get("tg_user_id") or "",
                "active": 1})
        else:
            row = self.db.upsert("staff", {
                "id": uid("stf"), "name": name, "role": role,
                "chat_id": chat_id, "note": note, "tg_user_id": tg_user_id,
                "active": 1, "created_at": now_iso()})
        self.db.add_event("bot", "Команда: участник добавлен",
                          f"{name} ({ROLE_NAMES.get(role, role)}) chat_id {chat_id}")
        row["role_name"] = ROLE_NAMES.get(row.get("role"), role)
        return row

    def remove(self, ident: str) -> dict:
        """Деактивировать по chat_id, имени или id (увольнение обратимо)."""
        ident = str(ident or "").strip()
        row = None
        if ident.lstrip("-").isdigit():
            row = self.db.one("SELECT * FROM staff WHERE chat_id=?", (ident,))
        row = row or self.db.one("SELECT * FROM staff WHERE id=?", (ident,))
        if not row:
            row = self.db.one("SELECT * FROM staff WHERE name LIKE ?", (f"%{ident}%",))
        if not row:
            raise ValueError(f"«{ident}» в команде не найден")
        self.db.execute("UPDATE staff SET active=0 WHERE id=?", (row["id"],))
        self.db.add_event("bot", "Команда: участник отключён",
                          f"{row.get('name')} chat_id {row.get('chat_id')}")
        return row

    def restore(self, staff_id: str) -> dict:
        self.db.execute("UPDATE staff SET active=1 WHERE id=?", (staff_id,))
        return self.db.one("SELECT * FROM staff WHERE id=?", (staff_id,)) or {}

    # ------------------------------------------------------------- приглашения
    def invites(self) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM staff_invites ORDER BY datetime(created_at) DESC")
        for row in rows:
            row["role_name"] = ROLE_NAMES.get(row.get("role"), "сотрудник")
        return rows

    def invite(self, role: str, name: str = "", created_by: str = "") -> dict:
        role = normalize_role(role)
        if role == "owner":
            raise ValueError("Владелец задаётся настройкой Chat ID, код не нужен")
        code = "PF-" + secrets.token_hex(3).upper()[:5]
        row = self.db.upsert("staff_invites", {
            "code": code, "role": role, "name": (name or "").strip(),
            "created_by": created_by, "used": 0, "used_by": "",
            "created_at": now_iso()}, key="code")
        row["role_name"] = ROLE_NAMES.get(role, role)
        return row

    def invite_delete(self, code: str) -> None:
        self.db.execute("DELETE FROM staff_invites WHERE code=?", (code,))

    def use_invite(self, code: str, chat_id: str, name: str = "",
                   tg_user_id: str = "") -> dict:
        """Присоединиться по коду: «старт PF-XXXX» в чате бота."""
        code = str(code or "").strip().upper()
        row = self.db.one("SELECT * FROM staff_invites WHERE code=? AND used=0",
                          (code,))
        if not row:
            raise ValueError("Код не найден или уже использован")
        member = self.add(name or row.get("name") or "Новичок", row.get("role"),
                          chat_id, "по приглашению", tg_user_id)
        self.db.execute("UPDATE staff_invites SET used=1, used_by=? WHERE code=?",
                        (chat_id, code))
        return member

    # --------------------------------------------------------------- справка
    def text_list(self) -> str:
        owner_chat = str(self.db.setting("telegram_chat_id", "") or "")
        lines = ["Команда NOZZA:", f"• владелец — chat_id {owner_chat or 'не задан'}"]
        for row in self.all(active_only=True):
            lines.append(f"• {row.get('name')} — {row['role_name']}"
                         f" (chat_id {row.get('chat_id')})")
        lines.append("\nДобавить: «сотрудник Имя 123456» · «руководитель Имя 123456».")
        lines.append("Приглашение: «пригласить сотрудник Имя» — код на 1 вход.")
        return "\n".join(lines)

    def rights_text(self, role: str) -> str:
        rights = ROLE_RIGHTS.get(normalize_role(role), set())
        names = {"view": "обзоры и камера", "shelf": "полка: продажа и приход",
                 "catalog": "каталог: цены и витрина",
                 "orders": "заказы и оплата", "finance": "деньги и отчёты",
                 "printers": "управление принтерами", "staff": "управление командой"}
        return " · ".join(names[g] for g in ("view", "shelf", "catalog", "orders",
                                             "finance", "printers", "staff")
                          if g in rights)


def gate(db, chat_id: str) -> dict[str, Any]:
    """Кто пишет в бот: роль для доступа или отказ с подсказкой.

    Возвращает {"role": ..., "row": ..., "allowed": set[str]} либо
    {"role": None} для постороннего чата.
    """
    chat = str(chat_id or "")
    owner = str(db.setting("telegram_chat_id", "") or "")
    if owner and chat == owner:
        return {"role": "owner", "row": None,
                "allowed": set(ROLE_RIGHTS["owner"])}
    row = Staff(db).by_chat(chat)
    if row:
        role = normalize_role(row.get("role"))
        return {"role": role, "row": row, "allowed": set(ROLE_RIGHTS.get(role, set()))}
    return {"role": None, "row": None, "allowed": set()}


def group_for_word(word: str, text_has_digits: bool = False,
                   command: str = "") -> str:
    """Группа прав для текстовой команды или callback-имени."""
    if command:
        name = command.split(":", 1)[0]
        base = CALLBACK_GROUPS.get(command) or CALLBACK_GROUPS.get(name)
        if base:
            return base
        # «sell:<id>» и «shelf-sell:<id>» начинаются как shelf-команды
        if name.startswith(("sell", "shelf")):
            return "shelf"
        return "view"
    group = WORD_GROUPS.get(word, "")
    if word in ("статус", "status") and text_has_digits:
        return "orders"  # «статус 1001 печать» — смена статуса заказа
    return group
