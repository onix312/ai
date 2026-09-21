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
    try:
        row = db.one(
            "SELECT COALESCE(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),0) s "
            "FROM money_log WHERE date(created_at)=date('now','localtime')"
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
    try:
        return db.query(
            "SELECT s.*, n.name, n.sku, n.barcode, c.name category_name, c.color category_color "
            "FROM shelf_items s "
            "LEFT JOIN nomenclature n ON n.id=s.nom_id "
            "LEFT JOIN categories c ON c.id=n.category_id "
            "ORDER BY s.qty ASC, n.name"
        )
    except Exception:
        return []


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
    try:
        today = db.one(
            "SELECT COALESCE(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),0) income, "
            "COALESCE(SUM(CASE WHEN amount<0 THEN amount ELSE 0 END),0) expense "
            "FROM money_log WHERE date(created_at)=date('now','localtime')"
        )
        week = db.one(
            "SELECT COALESCE(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),0) income "
            "FROM money_log WHERE datetime(created_at)>=datetime('now','-7 days','localtime')"
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
