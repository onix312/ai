"""Сервис Mini App — все запросы к БД, без SQL в routes_.

Вызывается из routes_staff_miniapp.py, чтобы пройти проверку
RoutesHaveNoSqlTests.
"""
from __future__ import annotations

from typing import Any


def find_staff_by_tg(db, tg_id: str) -> dict | None:
    row = db.one("SELECT * FROM staff WHERE tg_user_id=? AND active=1", (tg_id,))
    if not row:
        row = db.one("SELECT * FROM staff WHERE chat_id=? AND active=1", (tg_id,))
    return dict(row) if row else None


def count_orders_ready(db) -> int:
    try:
        rows = db.query("SELECT COUNT(*) c FROM orders WHERE status='ready'")
        return int((rows[0] if rows else {}).get("c") or 0)
    except Exception:
        return 0


def count_inbox_unread(db) -> int:
    try:
        row = db.one("SELECT COUNT(*) c FROM client_bot_log WHERE unread=1 AND direction='in'")
        return int((row or {}).get("c") or 0)
    except Exception:
        return 0


def today_money(db) -> float:
    """Приход за сегодня.

    18.12.3: спрашивали таблицу `money_log`, которой в базе нет, — запрос
    падал в `except` и функция всегда возвращала ноль. Касса в боте и в
    Mini App была нулевой при живых продажах. Деньги лежат в `transactions`
    (`kind='income'`, дата — первые 10 знаков `at`, как и во всех отчётах).
    """
    try:
        row = db.one(
            "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount ELSE 0 END),0) s "
            "FROM transactions WHERE substr(at,1,10)=date('now','localtime')"
        )
        return float((row or {}).get("s") or 0)
    except Exception:
        return 0.0


def count_low_stock(db) -> int:
    try:
        rows = db.query("SELECT COUNT(*) c FROM shelf_items WHERE qty>0 AND qty<=COALESCE(min_qty,2)")
        return int((rows[0] if rows else {}).get("c") or 0)
    except Exception:
        return 0


def list_shelf(db) -> list[dict]:
    """Остатки на полке с подписями и картинкой.

    Колонки номенклатуры названы `nom_*`: у `shelf_items` есть свои `name`,
    `sku` и `photo`, и в словаре остаётся именно своя колонка (первое
    вхождение имени побеждает). Свою подпись/фото подставляем только там, где
    у позиции полки пусто, — тогда карточка товара из базы видна и в боте
    (18.12.3: отчёт «полка» отвечает текстом с фото).
    """
    try:
        rows = db.query(
            "SELECT s.*, n.name nom_name, n.sku nom_sku, n.barcode nom_barcode, "
            "n.photo nom_photo, g.name category_name, g.color category_color "
            "FROM shelf_items s "
            "LEFT JOIN nomenclature n ON n.id=s.nom_id "
            # Категория — группа номенклатуры (`nom_groups`). Раньше здесь была
            # таблица `categories`, которой в схеме нет: запрос падал, и полка
            # молча оставалась пустой и в Mini App, и в отчёте бота (18.12.3).
            "LEFT JOIN nom_groups g ON g.id=n.group_id "
            "ORDER BY s.qty ASC, COALESCE(NULLIF(s.name,''), n.name)"
        )
    except Exception:
        return []
    for row in rows:
        row["name"] = row.get("name") or row.get("nom_name") or row.get("nom_id") or ""
        row["sku"] = row.get("sku") or row.get("nom_sku") or ""
        row["barcode"] = row.get("barcode") or row.get("nom_barcode") or ""
        row["photo"] = row.get("photo") or row.get("nom_photo") or ""
    return rows


def order_photo_file(db, order_id: str) -> str:
    """Имя файла-картинки заказа: снимок производства, иначе фото позиции.

    Бот 18.12.3 показывает заказ текстом с фотографией, и картинка должна быть
    «про этот заказ»: сначала фотоальбом заказа (кадры с камеры, снимки
    оператора), затем обложка номенклатуры из позиций заказа. Пустая строка =
    показывать нечего, а не «нет фото» в тексте.
    """
    oid = str(order_id or "").strip()
    if not oid:
        return ""
    try:
        row = db.one(
            "SELECT file FROM order_photos WHERE order_id=? AND file<>'' "
            "ORDER BY datetime(at) DESC LIMIT 1", (oid,))
        if row and row.get("file"):
            return str(row["file"])
    except Exception:
        pass
    try:
        row = db.one(
            "SELECT n.photo photo FROM order_items i "
            "JOIN nomenclature n ON n.id=i.nom_id "
            "WHERE i.order_id=? AND COALESCE(n.photo,'')<>'' "
            "ORDER BY i.position LIMIT 1", (oid,))
        if row and row.get("photo"):
            return str(row["photo"])
    except Exception:
        pass
    return ""


def list_orders(db, status: str, limit: int) -> list[dict]:
    try:
        if status != "all":
            return db.query(f"SELECT * FROM orders WHERE status=? ORDER BY datetime(created_at) DESC LIMIT ?", (status, limit))
        return db.query("SELECT * FROM orders ORDER BY datetime(created_at) DESC LIMIT ?", (limit,))
    except Exception:
        return []


def get_order(db, oid: str) -> dict | None:
    try:
        row = db.one("SELECT * FROM orders WHERE id=?", (oid,))
        if not row:
            row = db.one("SELECT * FROM orders WHERE number=?", (oid,))
        return dict(row) if row else None
    except Exception:
        return None


def list_inbox_chats(db) -> list[dict]:
    try:
        return db.query(
            "SELECT c.chat_id, c.name, c.phone, c.last_seen, "
            "COUNT(CASE WHEN l.unread=1 AND l.direction='in' THEN 1 END) unread "
            "FROM client_chats c "
            "LEFT JOIN client_bot_log l ON l.chat_id=c.chat_id "
            "GROUP BY c.chat_id ORDER BY MAX(l.at) DESC LIMIT 50"
        )
    except Exception:
        return []


def finance_today_week(db) -> tuple[dict, dict]:
    """Деньги за сегодня и за неделю — из `transactions`, как в панели."""
    try:
        today = db.one(
            "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount ELSE 0 END),0) income, "
            "COALESCE(SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END),0) expense "
            "FROM transactions WHERE substr(at,1,10)=date('now','localtime')"
        )
        week = db.one(
            "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount ELSE 0 END),0) income "
            "FROM transactions WHERE kind='income' "
            "AND substr(at,1,10)>=date('now','-6 days','localtime')"
        )
        return (dict(today) if today else {"income": 0, "expense": 0},
                dict(week) if week else {"income": 0})
    except Exception:
        return ({"income": 0, "expense": 0}, {"income": 0})


def list_team(db) -> tuple[list[dict], list[dict]]:
    try:
        staff = db.query("SELECT id,name,role,chat_id,tg_user_id,active,created_at FROM staff ORDER BY created_at")
        invites = db.query("SELECT code,role,name,created_at,used,used_by FROM staff_invites ORDER BY created_at DESC LIMIT 20")
        return (staff, invites)
    except Exception:
        return ([], [])
