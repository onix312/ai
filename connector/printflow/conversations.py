"""Единый инбокс диалогов (Н55, конкретизация идеи 75).

Разговоры с людьми лежали в трёх несвязанных местах: переписка с покупателями
в `client_bot_log`, проблемные отзывы в `client_reviews`, а обращения
сотрудников — только в журнале update рабочего бота, где текста сообщения
нет вовсе. Чтобы понять «кому я должен ответить», приходилось обойти три
вкладки.

Здесь — одна лента. Источники приводятся к общему виду:

    {id, channel, chat_id, name, text, at, unread, state, order_id, reply_to}

`channel` — кто пишет (`client` покупатель, `review` отзыв, `staff` сотрудник),
`state` — что с диалогом (`new`, `waiting`, `answered`, `resolved`),
`reply_to` — куда отвечать, чтобы ответ ушёл в тот же канал.

Фильтры (`unread`, `needs_answer`, `channel`, `q`) применяются одним
запросом, поэтому лента не расходится с тем, что видно в отдельных вкладках.
"""
from __future__ import annotations

from typing import Any

CHANNELS = {
    "client": "Покупатели",
    "review": "Отзывы",
    "staff": "Сотрудники",
}

# Состояния, при которых диалог требует ответа мастера.
NEEDS_ANSWER = ("new", "waiting", "needs_attention")


class Conversations:
    """Лента диалогов поверх существующих таблиц. Своих данных не хранит."""

    def __init__(self, db) -> None:
        self.db = db

    # ---------------------------------------------------------------- лента
    def threads(self, limit: int = 50, *, channel: str = "", q: str = "",
                unread_only: bool = False, needs_answer: bool = False) -> list[dict]:
        """Последнее сообщение каждого диалога + признак «ждёт ответа»."""
        limit = max(1, min(int(limit or 50), 200))
        rows: list[dict] = []
        if channel in ("", "client"):
            rows.extend(self._client_threads(limit, q))
        if channel in ("", "review"):
            rows.extend(self._review_threads(limit, q))
        if channel in ("", "staff"):
            rows.extend(self._staff_threads(limit, q))
        if unread_only:
            rows = [r for r in rows if r["unread"]]
        if needs_answer:
            rows = [r for r in rows if r["state"] in NEEDS_ANSWER]
        rows.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
        return rows[:limit]

    def _client_threads(self, limit: int, q: str) -> list[dict]:
        like = f"%{q.strip()}%" if q.strip() else "%"
        rows = self.db.query(
            "SELECT chat_id, MAX(at) AS at, COUNT(*) AS total,"
            " SUM(CASE WHEN direction='in' AND unread=1 THEN 1 ELSE 0 END) AS unread,"
            " MAX(CASE WHEN direction='in' THEN id END) AS last_in_id"
            " FROM client_bot_log WHERE text LIKE ? OR name LIKE ?"
            " GROUP BY chat_id ORDER BY at DESC LIMIT ?",
            (like, like, limit))
        out = []
        for row in rows:
            chat = str(row.get("chat_id") or "")
            last = self.db.one(
                "SELECT id, at, name, text, direction, order_id, operator"
                " FROM client_bot_log WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat,)) or {}
            unread = int(row.get("unread") or 0)
            # Диалог ждёт ответа, если последнее сообщение входящее.
            incoming_last = str(last.get("direction") or "") == "in"
            out.append({
                "id": f"client:{chat}",
                "channel": "client",
                "chat_id": chat,
                "name": str(last.get("name") or chat),
                "text": str(last.get("text") or "")[:280],
                "at": row.get("at"),
                "unread": unread,
                "total": int(row.get("total") or 0),
                "state": "waiting" if (incoming_last or unread) else "answered",
                "order_id": str(last.get("order_id") or ""),
                "reply_to": "client",
                "operator": str(last.get("operator") or ""),
            })
        return out

    def _review_threads(self, limit: int, q: str) -> list[dict]:
        # У client_reviews нет суррогатного id: ключ составной (order_id, chat_id),
        # поэтому в идентификаторе диалога несём обе части.
        like = f"%{q.strip()}%" if q.strip() else "%"
        rows = self.db.query(
            "SELECT r.order_id, r.chat_id, r.rating, r.comment, r.state,"
            " r.created_at, r.operator_note, o.number, o.product"
            " FROM client_reviews r LEFT JOIN orders o ON o.id=r.order_id"
            " WHERE COALESCE(r.comment,'') LIKE ? OR COALESCE(o.number,'') LIKE ?"
            " ORDER BY datetime(r.created_at) DESC LIMIT ?",
            (like, like, limit))
        out = []
        for row in rows:
            state = str(row.get("state") or "new")
            order_id = str(row.get("order_id") or "")
            chat = str(row.get("chat_id") or "")
            out.append({
                "id": f"review:{order_id}:{chat}",
                "channel": "review",
                "chat_id": chat,
                "name": f"Отзыв {row.get('rating') or ''} · заказ №{row.get('number') or '—'}",
                "text": str(row.get("comment") or row.get("product") or "")[:280],
                "at": row.get("created_at"),
                "unread": 1 if state in NEEDS_ANSWER else 0,
                "total": 1,
                "state": "needs_attention" if state in NEEDS_ANSWER else "resolved",
                "order_id": order_id,
                "reply_to": "review",
                "rating": str(row.get("rating") or ""),
            })
        return out

    def _staff_threads(self, limit: int, q: str) -> list[dict]:
        """Обращения сотрудников: журнал update рабочего бота.

        Текста сообщения в этой таблице нет — Telegram его не хранит после
        обработки, поэтому показываем факт обращения и результат. Это всё
        равно лучше, чем ничего: видно, что сотрудник писал и чем кончилось.
        """
        rows = self.db.query(
            "SELECT update_id, state, received_at, processed_at, error"
            " FROM telegram_bot_updates ORDER BY datetime(received_at) DESC LIMIT ?",
            (max(1, min(limit, 200)),))
        needle = q.strip().lower()
        out = []
        for row in rows:
            state = str(row.get("state") or "")
            body = ("обработано" if state == "done"
                    else f"ошибка: {row.get('error') or state}")
            # Поиск обязан работать во всех каналах одинаково: раньше в этой
            # ветке он игнорировался, и «расслоение» находило команды боту.
            if needle and needle not in body.lower() \
                    and needle not in str(row.get("update_id") or "").lower():
                continue
            out.append({
                "id": f"staff:{row.get('update_id')}",
                "channel": "staff",
                "chat_id": "",
                "name": "Сотрудник · команда боту",
                "text": body,
                "at": row.get("received_at"),
                "unread": 1 if state == "failed" else 0,
                "total": 1,
                "state": "answered" if state == "done" else "waiting",
                "order_id": "",
                "reply_to": "staff",
            })
        return out

    # ------------------------------------------------------------ сводка
    def summary(self) -> dict[str, Any]:
        """Счётчики для бейджей: сколько где ждёт ответа."""
        client = self.db.one(
            "SELECT COUNT(DISTINCT chat_id) AS n FROM client_bot_log"
            " WHERE direction='in' AND unread=1") or {}
        reviews = self.db.one(
            "SELECT COUNT(*) AS n FROM client_reviews WHERE state='needs_attention'") or {}
        failed = self.db.one(
            "SELECT COUNT(*) AS n FROM telegram_bot_updates WHERE state='failed'") or {}
        counts = {
            "client": int(client.get("n") or 0),
            "review": int(reviews.get("n") or 0),
            "staff": int(failed.get("n") or 0),
        }
        return {"counts": counts, "total": sum(counts.values()),
                "channels": dict(CHANNELS)}

    # ------------------------------------------------------------ ответ
    def thread(self, key: str, limit: int = 100) -> dict:
        """Полная история одного диалога."""
        channel, _, ident = str(key or "").partition(":")
        if channel == "client":
            messages = self.db.query(
                "SELECT at, name, text, answer, direction, operator, kind"
                " FROM client_bot_log WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (ident, max(1, min(int(limit), 300))))
            profile = self.db.one("SELECT * FROM client_chats WHERE chat_id=?", (ident,)) or {}
            # Имя берём из профиля, а при пустом профиле — из последней строки
            # лога. Иначе список показывал «Мария», а открытая карточка «555».
            # messages здесь ещё в порядке DESC, поэтому первое непустое — свежее.
            logged = next((message.get("name") for message in messages
                           if message.get("name")), "")
            return {"channel": "client", "chat_id": ident,
                    "name": str(profile.get("name") or logged or ident),
                    "profile": {k: profile.get(k) for k in
                                ("phone", "phone_verified", "source", "status_notify",
                                 "banned", "last_seen", "tg_user_id")},
                    "messages": list(reversed(messages))}
        if channel == "review":
            # ident = "<order_id>:<chat_id>" — составной ключ таблицы отзывов.
            order_id, _, chat_id = ident.partition(":")
            row = self.db.one(
                "SELECT r.*, o.number, o.product FROM client_reviews r"
                " LEFT JOIN orders o ON o.id=r.order_id"
                " WHERE r.order_id=? AND r.chat_id=?", (order_id, chat_id)) or {}
            return {"channel": "review", "review": dict(row),
                    "chat_id": str(row.get("chat_id") or ""),
                    "name": f"Отзыв · заказ №{row.get('number') or '—'}",
                    "messages": []}
        return {"channel": channel, "chat_id": "", "name": ident, "messages": []}
