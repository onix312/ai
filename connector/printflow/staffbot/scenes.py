"""Диалоги бота сотрудников: состояние ожидания ответа живёт в базе.

Было. Начатая продажа, сверка кассы и «ответ клиенту» жили в dict'ах
оперативной памяти (`_sell_flow`, `_cash_reconcile`, `_client_reply_to`).
Перезапуск коннектора посреди диалога молча терял состояние: сотрудник
вводил цену, а бот отвечал «не понял».

Стало. Один активный диалог на чат лежит в таблице `bot_scenes` с TTL.
Рестарт коннектора диалог не роняет; просроченное молча игнорируется и
подчищается. Сцена — это имя («sell», «cash_reconcile», «client_reply»)
и словарь параметров; интерпретация остаётся за сценариями.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from ..config import now_iso

# TTL диалога: за 15 минут сотрудник успевает отойти к принтеру и вернуться;
# держать ожидание дольше — значит однажды списать продажу по забытой цифре.
DEFAULT_TTL_SECONDS = 15 * 60

# Имена сцен — импортируются сценариями, чтобы не плодить строковые литералы.
SELL = "sell"                    # флоу продажи: позиция → количество → цена → канал
CASH_RECONCILE = "cash_reconcile"  # сверка кассы: ждём фактическую сумму числом
CLIENT_REPLY = "client_reply"    # ответ покупателю: следующее сообщение уйдёт клиенту


class BotScenes:
    """Активные диалоги бота в SQLite: set/active/pop + уборка просроченных."""

    def __init__(self, db, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.db = db
        self.ttl = max(60, int(ttl_seconds))

    # ------------------------------------------------------------ операции
    def set(self, chat: str, scene: str, data: dict | None = None) -> dict:
        """Начать (или перезаписать) диалог чата. Возвращает сохранённое."""
        chat = str(chat or "")
        data = dict(data or {})
        expires = (datetime.now() + timedelta(seconds=self.ttl)).isoformat()
        self.db.upsert("bot_scenes", {
            "chat_id": chat, "scene": str(scene or ""),
            "data": json.dumps(data, ensure_ascii=False),
            "updated_at": now_iso(), "expires_at": expires}, key="chat_id")
        return {"scene": scene, "data": data}

    def active(self, chat: str) -> dict | None:
        """Активный диалог чата или None (нет/завершён/просрочен).

        Просроченная строка не отдаётся и удаляется: диалог, который ждал
        ответа дольше TTL, честно считается забытым.
        """
        chat = str(chat or "")
        row = self.db.one("SELECT * FROM bot_scenes WHERE chat_id=?", (chat,))
        if not row:
            return None
        expires = str(row.get("expires_at") or "")
        if expires and expires < datetime.now().isoformat():
            self.db.execute("DELETE FROM bot_scenes WHERE chat_id=?", (chat,))
            return None
        try:
            data = json.loads(row.get("data") or "{}")
        except (TypeError, ValueError):
            data = {}
        return {"scene": str(row.get("scene") or ""), "data": data}

    def pop(self, chat: str) -> dict | None:
        """Забрать и завершить диалог (возврат — то же, что даёт active)."""
        current = self.active(chat)
        self.db.execute("DELETE FROM bot_scenes WHERE chat_id=?", (str(chat or ""),))
        return current

    def sweep(self) -> int:
        """Удалить все просроченные строки. Возвращает число убранных."""
        try:
            cur = self.db.execute("DELETE FROM bot_scenes WHERE expires_at<?",
                                  (datetime.now().isoformat(),))
            return int(getattr(cur, "rowcount", 0) or 0)
        except Exception:
            return 0
