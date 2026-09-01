"""Подписки сотрудников на события (Н54).

Раньше любое уведомление уходило в один общий чат: мастер по печати получал
сообщения про деньги, бухгалтер — про кончившийся пластик, а владелец читал
поток из всего сразу. Здесь — явный реестр событий и выбор каждого
сотрудника; общий чат остаётся запасным каналом для критичного.

Правила маршрутизации:
  * критичное (`critical=True`) уходит ВСЕГДА: в общий чат и всем
    подписанным — подписка не может стать причиной пропущенной аварии;
  * обычное событие уходит только подписанным, а в общий чат — если
    подписанных нет вообще (иначе уведомление потерялось бы);
  * сотрудник без единой подписки получает всё: это поведение по умолчанию,
    чтобы новый человек не оказался в информационном вакууме.
"""
from __future__ import annotations

from typing import Any

# Реестр событий: машинное имя → человекочитаемая подпись и группа.
EVENTS: dict[str, tuple[str, str]] = {
    "order_new": ("Новый заказ", "Заказы"),
    "order_ready": ("Заказ готов к выдаче", "Заказы"),
    "order_paid": ("Оплата заказа", "Заказы"),
    "order_overdue": ("Заказ просрочен", "Заказы"),
    "print_done": ("Печать завершена", "Печать"),
    "print_failed": ("Печать не удалась", "Печать"),
    "printer_offline": ("Принтер недоступен", "Печать"),
    "printer_error": ("Ошибка принтера (HMS)", "Печать"),
    "filament_low": ("Пластик заканчивается", "Материалы"),
    "stock_low": ("Позиция на исходе", "Материалы"),
    "defect_new": ("Зарегистрирован брак", "Качество"),
    "review_bad": ("Плохой отзыв покупателя", "Клиенты"),
    "client_lead": ("Новая заявка из клиент-бота", "Клиенты"),
    "money_low": ("Касса ниже порога", "Деньги"),
    "backup_failed": ("Бэкап не выполнен", "Система"),
    "diagnostics_alert": ("Самодиагностика: проблема", "Система"),
}

# Что включено новому сотруднику по умолчанию.
DEFAULT_ON = frozenset({
    "order_ready", "print_done", "print_failed", "printer_offline",
    "printer_error", "filament_low", "defect_new",
})

ALL_EVENTS = tuple(EVENTS)


def catalog() -> list[dict]:
    """Список событий для формы подписок (сгруппированный панелью)."""
    return [{"event": name, "label": label, "group": group,
             "default": name in DEFAULT_ON}
            for name, (label, group) in EVENTS.items()]


def is_known(event: str) -> bool:
    return str(event or "") in EVENTS


def get(db, staff_id: str) -> dict[str, bool]:
    """Карта «событие → включено» для сотрудника.

    Если подписок нет вовсе, возвращаем дефолтный набор: новый человек
    должен получать основное, а не пустоту.
    """
    rows = db.query("SELECT event, enabled FROM staff_subscriptions WHERE staff_id=?",
                    (str(staff_id or ""),))
    if not rows:
        return {name: name in DEFAULT_ON for name in EVENTS}
    stored = {str(row.get("event")): bool(int(row.get("enabled") or 0)) for row in rows}
    return {name: stored.get(name, name in DEFAULT_ON) for name in EVENTS}


def set_many(db, staff_id: str, patch: dict) -> dict[str, bool]:
    """Сохранить подписки. Неизвестные события отбрасываются, а не пишутся."""
    from .config import now_iso
    staff_id = str(staff_id or "")
    if not staff_id:
        return get(db, staff_id)
    stamp = now_iso()
    with db.transaction():
        for event, enabled in (patch or {}).items():
            name = str(event or "")
            if name not in EVENTS:
                continue
            db.execute(
                "INSERT INTO staff_subscriptions(staff_id,event,enabled,created_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(staff_id,event) DO UPDATE SET enabled=excluded.enabled",
                (staff_id, name, 1 if enabled else 0, stamp))
    return get(db, staff_id)


def reset(db, staff_id: str) -> dict[str, bool]:
    """Вернуть подписки к заводским (удалить явные строки)."""
    db.execute("DELETE FROM staff_subscriptions WHERE staff_id=?", (str(staff_id or ""),))
    return get(db, staff_id)


def subscribers(db, event: str, *, include_inactive: bool = False) -> list[dict]:
    """Сотрудники, которым нужно событие. Пусто — шлём в общий чат."""
    name = str(event or "")
    if name not in EVENTS:
        return []
    sql = ("SELECT s.id, s.name, s.role, s.chat_id, x.enabled AS enabled"
           " FROM staff s"
           " LEFT JOIN staff_subscriptions x"
           "   ON x.staff_id=s.id AND x.event=?"
           " WHERE s.chat_id!=''")
    if not include_inactive:
        sql += " AND COALESCE(s.active,1)=1"
    rows = db.query(sql, (name,))
    if not rows:
        return []
    # Сотрудник без единой подписки получает всё (см. правила в докстринге).
    chosen = []
    for row in rows:
        # NULL в enabled = у сотрудника нет явной строки на это событие.
        flag = row.get("enabled")
        if flag is None:
            has_any = db.one("SELECT 1 AS n FROM staff_subscriptions WHERE staff_id=? LIMIT 1",
                             (row.get("id"),))
            wanted = (not has_any) or name in DEFAULT_ON
        else:
            wanted = bool(int(flag or 0))
        if wanted:
            chosen.append({"id": row.get("id"), "name": row.get("name") or "",
                           "role": row.get("role") or "",
                           "chat_id": str(row.get("chat_id") or "")})
    return chosen


def route(db, event: str, *, critical: bool = False,
          fallback_chat: str = "") -> dict[str, Any]:
    """Куда отправить событие: список чатов сотрудников и признак общего чата.

    Возвращает `{"chats": [...], "use_fallback": bool, "subscribers": [...]}`.
    """
    people = subscribers(db, event) if event else []
    chats = [p["chat_id"] for p in people if p.get("chat_id")]
    if critical:
        # Авария идёт всем и в общий чат: подписка не повод промолчать.
        return {"chats": chats, "use_fallback": True, "subscribers": people}
    if not event:
        return {"chats": [], "use_fallback": True, "subscribers": []}
    return {"chats": chats, "use_fallback": not chats, "subscribers": people}
