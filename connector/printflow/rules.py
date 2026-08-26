"""Конструктор правил «если-то» — настраиваемая автоматизация поверх событий.

Раньше каждая автоматизация была отдельным флагом в настройках (писать доход
при закрытии, автозапуск очереди, напоминание о долге…). Конструктор делает
то же самое, но видимо и настраиваемо: правило = триггер («если …») + действие
(«то …»). Список правил виден в интерфейсе, каждое включается тумблером, есть
счётчик срабатываний и журнал «что сработало».

События (триггеры) — только те, что уже централизованы в коде:
  * print_failed    — печать завершилась ошибкой;
  * print_complete  — печать успешно завершена;
  * print_pause     — печать поставлена на паузу;
  * filament_low    — катушка ниже порога;
  * debt_overdue    — долг старше N дней (config.days, раз в сутки);
  * order_status    — заказ перешёл в статус (config.status).

Действия:
  * notify  — Telegram-сообщение по шаблону (config.template);
  * event   — запись в журнал событий (config.template как детали);
  * reprint — клон сорванного задания в очередь (только print_failed);
  * pause   — поставить печать на паузу (только print_failed).

Шаблоны подставляют {name}, {printer}, {detail}, {order}, {grams}, {pct} и др.
Всё в пределах компьютера: ничего наружу, кроме уже согласованного Telegram.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from .accounting import num
from .config import now_iso

# Триггеры, доступные в конструкторе: ключ → человекочитаемое имя.
TRIGGERS = {
    "print_failed": "Печать сорвалась",
    "print_complete": "Печать завершена",
    "print_pause": "Печать на паузе",
    "filament_low": "Пластик ниже порога",
    "debt_overdue": "Долг старше N дней",
    "order_status": "Заказ перешёл в статус",
}

# Действия, доступные в конструкторе: ключ → имя + подсказка.
ACTIONS = {
    "notify": "Сообщение в Telegram",
    "event": "Запись в журнал",
    "reprint": "Запросить подтверждение повтора",
    "pause": "Пауза печати",
}

# Стартовые правила-примеры (выключены: пользователь включает то, что нужно).
DEFAULT_RULES = [
    {
        "id": "rule_print_failed_notify", "name": "Ошибка печати → Telegram",
        "event": "print_failed", "action": "notify", "enabled": 0, "position": 1,
        "config": {"template": "⚠ Печать сорвалась: {name}\n{detail}"},
    },
    {
        "id": "rule_print_failed_reprint", "name": "Ошибка печати → запрос повтора",
        "event": "print_failed", "action": "reprint", "enabled": 0, "position": 2,
        "config": {},
    },
    {
        "id": "rule_print_complete_notify", "name": "Печать завершена → Telegram",
        "event": "print_complete", "action": "notify", "enabled": 0, "position": 3,
        "config": {"template": "✅ Печать завершена: {name}\nОсталось {remaining} мин"},
    },
    {
        "id": "rule_filament_low_notify", "name": "Мало пластика → Telegram",
        "event": "filament_low", "action": "notify", "enabled": 0, "position": 4,
        "config": {"template": "🧵 Мало пластика: {material} {color} — {grams} г ({pct}%)"},
    },
    {
        "id": "rule_debt_overdue_notify", "name": "Долг старше 14 дней → Telegram",
        "event": "debt_overdue", "action": "notify", "enabled": 0, "position": 5,
        "config": {"days": 14, "template": "💰 Долги старше {days} дней: {total} ₽ по {count} заказам"},
    },
    {
        "id": "rule_order_ready_notify", "name": "Заказ «Готов» → Telegram",
        "event": "order_status", "action": "notify", "enabled": 0, "position": 6,
        "config": {"status": "ready", "template": "📦 Заказ №{number} перешёл в «Готов»: {product}"},
    },
]


def _render(template: str, ctx: dict[str, Any]) -> str:
    """Подставить {ключ} из контекста; недостающие — пустой строкой."""
    if not template:
        return ""
    out = template
    for key, value in ctx.items():
        out = out.replace("{" + key + "}", "" if value is None else str(value))
    # Остатки вида {неизвестный_ключ} — убираем, чтобы не светить шаблоном.
    return re.sub(r"\{[^{}]*\}", "", out)


class RulesEngine:
    """Хранилище и исполнение правил. Живёт на менеджере, не держит потоков."""

    def __init__(self, manager):
        self.manager = manager
        self.db = manager.db
        self._lock = threading.RLock()
        self._debt_reported: dict[str, str] = {}  # rule_id -> day
        self.seed_defaults()
        self.db.execute(
            "UPDATE automation_rules SET name=?,updated_at=?"
            " WHERE id='rule_print_failed_reprint' AND name=?",
            ("Ошибка печати → запрос повтора", now_iso(),
             "Ошибка печати → авто-повтор"),
        )

    # ------------------------------------------------------------- хранилище
    def seed_defaults(self) -> None:
        """Завести стартовые правила-примеры, если таблица пуста."""
        if self.db.one("SELECT 1 FROM automation_rules LIMIT 1"):
            return
        for rule in DEFAULT_RULES:
            self.db.upsert("automation_rules", {
                **rule, "config": json.dumps(rule.get("config") or {}, ensure_ascii=False),
                "created_at": now_iso(), "updated_at": now_iso(),
            })

    def rules(self) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM automation_rules ORDER BY position, name")
        for row in rows:
            try:
                row["config"] = json.loads(row.get("config") or "{}")
            except json.JSONDecodeError:
                row["config"] = {}
        return rows

    def save_rule(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("id"):
            data["id"] = f"rule_{int(time.time() * 1000)}"
        config = data.get("config")
        if isinstance(config, dict):
            data["config"] = json.dumps(config, ensure_ascii=False)
        elif not config:
            data["config"] = "{}"
        data.setdefault("created_at", now_iso())
        data["updated_at"] = now_iso()
        return self.db.upsert("automation_rules", data)

    def delete_rule(self, rule_id: str) -> None:
        self.db.delete("automation_rules", rule_id)

    def toggle(self, rule_id: str, enabled: bool) -> dict:
        self.db.execute("UPDATE automation_rules SET enabled=?, updated_at=? WHERE id=?",
                        (1 if enabled else 0, now_iso(), rule_id))
        return self.db.one("SELECT * FROM automation_rules WHERE id=?", (rule_id,)) or {}

    def run(self, event: str, ctx: dict[str, Any] | None = None) -> list[dict]:
        """Выполнить все включённые правила для события. Возвращает сработавшие."""
        ctx = dict(ctx or {})
        ctx.setdefault("name", ctx.get("detail", ""))
        ctx.setdefault("printer", ctx.get("printer", ""))
        fired: list[dict] = []
        for rule in self.rules():
            if not int(num(rule.get("enabled"), 0)) or rule.get("event") != event:
                continue
            if not self._match(rule, ctx):
                continue
            self._execute(rule, ctx)
            config = rule.get("config") or {}
            self.db.execute(
                "INSERT INTO automation_rule_runs(rule_id,mode,event,matched,action,preview,at,actor)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (rule["id"], "live", event, 1, str(rule.get("action") or "notify"),
                 _render(str(config.get("template") or ""), ctx)[:2000], now_iso(), "system"))
            self.db.execute(
                "UPDATE automation_rules SET fires=COALESCE(fires,0)+1, last_fired=? WHERE id=?",
                (now_iso(), rule["id"]))
            fired.append(rule)
        return fired

    def simulate(self, event: str, ctx: dict[str, Any] | None = None,
                 rule_id: str = "", actor: str = "panel") -> list[dict]:
        """Показать последствия правил без выполнения действий.

        Это безопасный dry-run: не отправляет Telegram, не меняет статусы,
        очередь или принтер. Результат сохраняется только как технический
        журнал симуляции, чтобы оператор мог проверить его перед включением.
        """
        context = dict(ctx or {})
        context.setdefault("name", context.get("detail", ""))
        result = []
        for rule in self.rules():
            if rule_id and rule.get("id") != rule_id:
                continue
            if rule.get("event") != event:
                continue
            matched = bool(self._match(rule, context))
            config = rule.get("config") or {}
            preview = _render(str(config.get("template") or ""), context)
            action = str(rule.get("action") or "notify")
            if action == "reprint":
                preview = (preview + "\n" if preview else "") + "Будет создан запрос на подтверждение повтора"
            elif action == "pause":
                preview = (preview + "\n" if preview else "") + "Будет запрошена пауза принтера"
            elif action == "event":
                preview = preview or "Будет записано событие"
            elif action == "notify":
                preview = preview or "Будет подготовлено уведомление"
            item = {"id": rule["id"], "name": rule["name"], "matched": matched,
                    "enabled": bool(num(rule.get("enabled"), 0)), "action": action,
                    "preview": preview}
            self.db.execute(
                "INSERT INTO automation_rule_runs(rule_id,mode,event,matched,action,preview,at,actor)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (rule["id"], "dry_run", event, 1 if matched else 0, action,
                 preview[:2000], now_iso(), str(actor or "panel")[:120]))
            result.append(item)
        return result

    def recent_runs(self, limit: int = 50) -> list[dict]:
        """Последние dry-run/live записи без содержимого секретных настроек."""
        return self.db.query(
            "SELECT * FROM automation_rule_runs ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),))

    def _match(self, rule: dict, ctx: dict) -> bool:
        """Дополнительные условия из config (порог дней, статус заказа)."""
        config = rule.get("config") or {}
        if rule.get("event") == "debt_overdue":
            return True  # порог проверяется в check_debts
        if rule.get("event") == "order_status":
            want = str(config.get("status") or "")
            return not want or ctx.get("status") == want
        return True

    def _execute(self, rule: dict, ctx: dict) -> None:
        config = rule.get("config") or {}
        action = rule.get("action") or "notify"
        template = str(config.get("template") or "")
        text = _render(template, ctx)
        if action == "notify":
            if text:
                self.manager.notify_async(f"PrintFlow · правило «{rule['name']}»\n{text}")
        elif action == "event":
            self.db.add_event("rule", f"Правило: {rule['name']}",
                              text or _render("{name} {detail}", ctx), ctx.get("printer_id", ""),
                              {"rule_id": rule["id"], "event": rule["event"]})
        elif action == "reprint":
            # Повтор после брака — исключение, а не безопасная типовая операция:
            # правило лишь поднимает задачу, причину и очередь подтверждает мастер.
            detail = "Нужен разбор причины и подтверждение повтора в карточке заказа"
            self.db.add_event(
                "rule", f"Правило: {rule['name']} — требуется подтверждение",
                detail, str(ctx.get("printer_id") or ""),
                {"rule_id": rule["id"], "job_id": ctx.get("job_id") or ""},
            )
            self.manager.notify_async(
                f"PrintFlow · {rule['name']}\n{detail}" + (f"\n{text}" if text else "")
            )
        elif action == "pause":
            printer = self.manager.get(ctx.get("printer_id") or "")
            if printer:
                try:
                    self.manager.mark_non_resumable_pause(printer.id, "rule_pause")
                    printer.command("pause")
                except Exception as exc:
                    self.db.add_event("rule", f"Правило: {rule['name']} не сработало",
                                      str(exc), "", {"rule_id": rule["id"]})

    # ------------------------------------------------------- периодические
    def check_debts(self) -> None:
        """Долги старше N дней — раз в сутки на правило (не спамим)."""
        today = now_iso()[:10]
        for rule in self.rules():
            if rule.get("event") != "debt_overdue" or not int(num(rule.get("enabled"), 0)):
                continue
            if self._debt_reported.get(rule["id"]) == today:
                continue
            days = int(num((rule.get("config") or {}).get("days"), 14))
            debts = self.manager.acc.debts() if hasattr(self.manager.acc, "debts") else {}
            overdue = num(debts.get("overdue"))
            if overdue <= 0:
                continue
            self._debt_reported[rule["id"]] = today
            ctx = {"days": days, "total": round(overdue, 2),
                   "count": int(num(debts.get("overdue_count", debts.get("count", 0))))}
            self._execute(rule, ctx)
            self.db.execute(
                "UPDATE automation_rules SET fires=COALESCE(fires,0)+1, last_fired=? WHERE id=?",
                (now_iso(), rule["id"]))

    def on_order_status(self, order: dict, old_status: str, new_status: str) -> None:
        """Хук перехода заказа между статусами (из repo.save_order)."""
        if not order or old_status == new_status:
            return
        ctx = {
            "status": new_status,
            "number": order.get("number", ""),
            "product": order.get("product", ""),
            "customer": order.get("customer_name", ""),
            "price": order.get("price", ""),
            "order_id": order.get("id", ""),
        }
        self.run("order_status", ctx)

    # ------------------------------------------------------------- в события
    def on_print_event(self, kind: str, title: str, detail: str, printer_id: str,
                       data: dict | None = None) -> None:
        """Принтер сообщил о событии — сопоставляем с триггерами."""
        mapping = {
            "start": None,
            "complete": "print_complete",
            "error": "print_failed",
            "pause": "print_pause",
            "stop": None,
        }
        event = mapping.get(kind)
        if not event:
            return
        ctx = {
            "name": detail or title,
            "detail": detail or title,
            "title": title,
            "printer": self.manager.get(printer_id).record.get("name", "Принтер")
                if self.manager.get(printer_id) else "",
            "printer_id": printer_id,
            "progress": round(num((data or {}).get("progress")), 1),
            "remaining": round(num((data or {}).get("duration_min")), 1),
        }
        self.run(event, ctx)
