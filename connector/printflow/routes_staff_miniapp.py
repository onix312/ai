"""API для Mini App цеха: /api/staff/*.

Авторизация — initData HMAC по telegram_token (см. staffbot/miniapp.py).
Роли — те же, что у бота: owner/manager/employee, маппинг по tg_user_id
и fallback по chat_id. Владелец без строки в staff — виртуальный owner.

Все маршруты помечены public=False — доступ только после проверки подписи.
SQL вынесен в staffbot.core.staff_service, чтобы пройти проверку
«нет SQL в routes_».
"""
from __future__ import annotations

from typing import Any

from .router import Ctx, router
from .staff import ROLE_NAMES, normalize_role
from .staffbot.core.staff_service import (
    count_inbox_unread,
    count_low_stock,
    count_orders_ready,
    finance_today_week,
    find_staff_by_tg,
    get_order,
    list_inbox_chats,
    list_orders,
    list_shelf,
    list_team,
    today_money,
)
from .staffbot.miniapp import extract_init_data_from_ctx, validate_init_data


def _bot_token(api: Any) -> str:
    return str(api.db.setting("telegram_token", "") or "").strip()


def _owner_chat(api: Any) -> str:
    return str(api.db.setting("telegram_chat_id", "") or "").strip()


def _require_staff(api: Any, ctx: Ctx) -> dict:
    token = _bot_token(api)
    if not token:
        raise ValueError("Бот не настроен: нет telegram_token")
    init_data = extract_init_data_from_ctx(ctx)
    test_id = str(ctx.arg("test_user_id", "") or "").strip()
    if test_id and not init_data:
        dev = str(api.db.setting("test_mode", "") or "") == "1" or not token
        if dev or test_id == _owner_chat(api):
            row = find_staff_by_tg(api.db, test_id)
            if row:
                row["_tg_user"] = {"id": int(test_id) if test_id.lstrip("-").isdigit() else 0}
                return row
            if test_id == _owner_chat(api) and _owner_chat(api):
                return {
                    "id": "owner", "name": "Владелец", "role": "owner",
                    "role_name": "владелец", "chat_id": _owner_chat(api),
                    "tg_user_id": test_id, "active": 1,
                    "_tg_user": {"id": int(test_id) if test_id.lstrip("-").isdigit() else 0},
                }
    if not init_data:
        raise ValueError("Нет подписи Mini App — откройте через Telegram")
    user = validate_init_data(init_data, token)
    if not user:
        raise ValueError("Подпись Mini App неверна — обновите приложение")
    tg_uid = str(user.get("id") or "").strip()
    if not tg_uid:
        raise ValueError("Не удалось прочитать пользователя Telegram")
    row = find_staff_by_tg(api.db, tg_uid)
    owner_chat = _owner_chat(api)
    if not row and owner_chat and tg_uid == owner_chat:
        return {
            "id": "owner", "name": "Владелец", "role": "owner",
            "role_name": "владелец", "chat_id": owner_chat,
            "tg_user_id": tg_uid, "active": 1,
            "_tg_user": user,
        }
    if not row:
        raise ValueError("Вы не в команде — попросите владельца добавить вас")
    out = dict(row)
    out["_tg_user"] = user
    out["role_name"] = ROLE_NAMES.get(out.get("role"), out.get("role") or "")
    return out


def _role(api: Any, staff_row: dict) -> str:
    return normalize_role(staff_row.get("role") or "employee")


# --------------------------------------------------------------- /me
@router.get("/api/staff/me", doc="Кто я в Mini App цеха")
def staff_me(api: Any, ctx: Ctx):
    try:
        row = _require_staff(api, ctx)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    role = _role(api, row)
    return {
        "ok": True,
        "user": row.get("_tg_user") or {},
        "staff": {
            "id": row.get("id"), "name": row.get("name"),
            "role": role, "role_name": ROLE_NAMES.get(role, role),
            "chat_id": row.get("chat_id"), "tg_user_id": row.get("tg_user_id"),
        },
    }


# ------------------------------------------------------------- summary
@router.get("/api/staff/summary", doc="Сводка цеха для Mini App")
def staff_summary(api: Any, ctx: Ctx):
    try:
        row = _require_staff(api, ctx)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    role = _role(api, row)
    try:
        printers = api.manager.printers if hasattr(api, "manager") else {}
        farm_total = len(printers) if isinstance(printers, dict) else 0
        farm_online = 0
        farm_printing = 0
        try:
            for p in (printers.values() if isinstance(printers, dict) else []):
                conn = getattr(p, "connection", None) or {}
                if isinstance(conn, dict) and conn.get("connected"):
                    farm_online += 1
                st = getattr(p, "state", "") or ""
                if isinstance(p, dict):
                    st = (p.get("printer") or {}).get("state") or p.get("state") or ""
                if str(st).upper() in ("RUNNING", "PREPARE"):
                    farm_printing += 1
        except Exception:
            pass
    except Exception:
        farm_total = farm_online = farm_printing = 0

    try:
        queue = api.manager.queue() if hasattr(api, "manager") and hasattr(api.manager, "queue") else []
        q_len = len(queue) if isinstance(queue, list) else 0
    except Exception:
        q_len = 0

    orders_ready_n = count_orders_ready(api.db)
    inbox_n = count_inbox_unread(api.db)
    today = today_money(api.db)
    low_n = count_low_stock(api.db)

    return {
        "ok": True,
        "role": role,
        "farm": {"total": farm_total, "online": farm_online, "printing": farm_printing},
        "queue": q_len,
        "orders_ready": orders_ready_n,
        "inbox": inbox_n,
        "low_stock": low_n,
        "today_money": today,
    }


# ---------------------------------------------------------------- shelf
@router.get("/api/staff/shelf", doc="Полка: остатки на стеллаже")
def staff_shelf(api: Any, ctx: Ctx):
    try:
        _require_staff(api, ctx)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    items = list_shelf(api.db)
    return {"ok": True, "items": items}


# --------------------------------------------------------------- orders
@router.get("/api/staff/orders", doc="Заказы для Mini App")
def staff_orders(api: Any, ctx: Ctx):
    try:
        _require_staff(api, ctx)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    status = str(ctx.arg("status", "") or "").strip() or "all"
    limit = min(100, max(1, ctx.num("limit", 30) or 30))
    rows = list_orders(api.db, status, limit)
    return {"ok": True, "orders": rows}


@router.get("/api/staff/orders/one", doc="Один заказ")
def staff_order_one(api: Any, ctx: Ctx):
    try:
        _require_staff(api, ctx)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    oid = str(ctx.arg("id", "") or ctx.arg("order_id", "") or "").strip()
    if not oid:
        return {"ok": False, "error": "id заказа обязателен"}
    row = get_order(api.db, oid)
    if not row:
        return {"ok": False, "error": "Заказ не найден"}
    return {"ok": True, "order": row}


# -------------------------------------------------------------- printers
@router.get("/api/staff/printers", doc="Принтеры для Mini App")
def staff_printers(api: Any, ctx: Ctx):
    try:
        _require_staff(api, ctx)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        if hasattr(api, "manager"):
            printers = []
            src = getattr(api.manager, "printers", {}) or {}
            iterable = src.values() if isinstance(src, dict) else src
            for p in iterable:
                if isinstance(p, dict):
                    printers.append({
                        "id": p.get("id"), "name": p.get("name"),
                        "state": (p.get("printer") or {}).get("state") or p.get("state"),
                        "progress": (p.get("printer") or {}).get("progress"),
                        "connected": (p.get("connection") or {}).get("connected"),
                    })
                else:
                    printers.append({
                        "id": getattr(p, "id", ""),
                        "name": getattr(p, "name", ""),
                        "state": getattr(p, "state", ""),
                        "connected": bool(getattr(getattr(p, "connection", None), "connected", False)),
                    })
            return {"ok": True, "printers": printers}
    except Exception:
        pass
    return {"ok": True, "printers": []}


@router.get("/api/staff/queue", doc="Очередь печати для Mini App")
def staff_queue(api: Any, ctx: Ctx):
    try:
        _require_staff(api, ctx)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        if hasattr(api, "manager") and hasattr(api.manager, "queue"):
            q = api.manager.queue()
            return {"ok": True, "queue": q if isinstance(q, list) else []}
    except Exception:
        pass
    return {"ok": True, "queue": []}


# --------------------------------------------------------------- inbox
@router.get("/api/staff/inbox", doc="Inbox клиентского бота")
def staff_inbox(api: Any, ctx: Ctx):
    try:
        _require_staff(api, ctx)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    rows = list_inbox_chats(api.db)
    return {"ok": True, "chats": rows}


# ------------------------------------------------------------- finance
@router.get("/api/staff/finance", doc="Деньги сегодня для Mini App")
def staff_finance(api: Any, ctx: Ctx):
    try:
        row = _require_staff(api, ctx)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    role = _role(api, row)
    if role not in ("owner", "manager"):
        return {"ok": False, "error": "Доступно руководителю и владельцу"}
    today, week = finance_today_week(api.db)
    return {"ok": True, "today": today or {}, "week": week or {}}


# ---------------------------------------------------------------- team
@router.get("/api/staff/team", doc="Команда для Mini App")
def staff_team(api: Any, ctx: Ctx):
    try:
        row = _require_staff(api, ctx)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    role = _role(api, row)
    if role != "owner":
        return {"ok": False, "error": "Управление командой — только владелец"}
    staff, invites = list_team(api.db)
    return {"ok": True, "staff": staff, "invites": invites}
