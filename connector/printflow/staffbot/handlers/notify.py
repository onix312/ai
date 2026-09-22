"""Notify handlers — все уведомления + кнопка web_app.

Печать, inbox, касса, низкий остаток, дайджест 09:00, график 20:00, недельный.
Бот тонкий, но расписание шлёт сам, через manager.notify_async.
"""
from __future__ import annotations

import json
from datetime import datetime

from ...accounting import num
from ..core.config import miniapp_state
from ..ui import web_app_keyboard


class NotifyMixin:
    """Миксин с тиками расписания и текстами уведомлений."""

    def _notify_with_button(self, text: str, event: str = "", photo: bytes | None = None) -> None:
        """Отправить уведомление с кнопкой «Открыть цех».

        У самой кнопки — callback на меню, а не web_app: меню уже показывает
        web_app, когда адрес Mini App настроен, а в уведомлении не-HTTPS адрес
        уронил бы всё сообщение (``BUTTON_URL_INVALID``). Если адреса нет,
        сообщение всё равно уходит — но без мёртвой кнопки на example.com.
        """
        buttons = [("🏭 Открыть цех", "cmd:menu")]
        if hasattr(self, "manager") and hasattr(self.manager, "notify_async"):
            try:
                self.manager.notify_async(text, photo=photo, buttons=buttons, event=event)
                return
            except Exception:
                pass
        # fallback — шлём напрямую в owner chat
        try:
            chat = str(self._settings().get("telegram_chat_id") or "")
            if not chat:
                return
            state = miniapp_state(self.db)
            payload: dict = {
                "chat_id": chat,
                "text": text[:3800],
                "disable_web_page_preview": "true",
            }
            if state["ready"]:
                payload["reply_markup"] = json.dumps(web_app_keyboard(state["url"]),
                                                    ensure_ascii=False)
            self._call("sendMessage", payload, timeout=15)
        except Exception:
            pass

    # ---------------------------- тексты дайджестов (упрощённые, но честные)
    def text_digest(self) -> str:
        """Утренний дайджест 09:00."""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            # заказы с дедлайном сегодня
            due_today = self.db.query(
                "SELECT number, product FROM orders WHERE due=? AND status NOT IN (SELECT id FROM statuses WHERE is_final=1) LIMIT 5",
                (today,),
            )
            queue = []
            try:
                queue = self.manager.queue()[:5] if hasattr(self.manager, "queue") else []
            except Exception:
                queue = []
            low = []
            try:
                low = self.db.query(
                    "SELECT name, qty, min_qty FROM shelf_items WHERE active=1 AND min_qty>0 AND qty<=min_qty ORDER BY qty LIMIT 5"
                )
            except Exception:
                low = []
            lines = ["☀ Доброе утро! Дайджест цеха:"]
            if due_today:
                lines.append(f"📌 Срок сегодня: {len(due_today)}")
                for o in due_today[:3]:
                    lines.append(f"  №{o.get('number')} {o.get('product') or ''}")
            if queue:
                lines.append(f"🖨 В очереди {len(queue)} заданий")
            if low:
                lines.append(f"⚠ Низкий остаток: {len(low)} поз.")
                for r in low[:3]:
                    lines.append(f"  • {r.get('name')} — {r.get('qty')} шт")
            if len(lines) == 1:
                lines.append("Всё спокойно — цех готов к работе.")
            lines.append("\nОткройте цех — там детали.")
            return "\n".join(lines)
        except Exception as exc:
            return f"☀ Дайджест: {exc}"

    def text_weekly(self) -> str:
        try:
            summary = self.manager.acc.summary(7) if hasattr(self.manager, "acc") else {}
            lines = [
                "📊 Недельный отчёт:",
                f"Выручка {summary.get('income', 0)}",
                f"Прибыль {summary.get('profit', 0)}",
            ]
            return "\n".join(lines)
        except Exception as exc:
            return f"📊 Недельный отчёт: {exc}"

    def _maybe_digest(self, settings: dict) -> None:
        digest_time = str(settings.get("digest_time") or "09:00")
        now = datetime.now()
        if now.strftime("%H:%M") != digest_time:
            return
        last = str(settings.get("digest_last") or "")
        today = now.strftime("%Y-%m-%d")
        if last == today:
            return
        self.db.set_settings({"digest_last": today})
        if str(settings.get("telegram_chat_id") or ""):
            self._notify_with_button(self.text_digest(), event="digest")

    def _maybe_shelf_low(self, settings: dict) -> None:
        try:
            rows = self.db.query(
                "SELECT * FROM shelf_items WHERE active=1 AND min_qty>0 AND qty<=min_qty ORDER BY qty LIMIT 10"
            )
        except Exception:
            return
        if not rows:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if str(settings.get("shelf_low_last") or "") == today:
            return
        self.db.set_settings({"shelf_low_last": today})
        lines = [f"⚠ На полке заканчивается ({len(rows)}):"]
        for row in rows[:6]:
            lines.append(f"• {row.get('name')} — {row.get('qty')} шт (минимум {row.get('min_qty')})")
        lines.append("\nОткройте цех — пополните.")
        self._notify_with_button("\n".join(lines), event="shelf:low")

    def _maybe_weekly(self, settings: dict) -> None:
        day = int(num(settings.get("weekly_report_day", 1), 1))
        at = str(settings.get("weekly_report_time") or "20:00")
        now = datetime.now()
        if now.isoweekday() != day or now.strftime("%H:%M") != at:
            return
        key = f"{now.isocalendar().year}-W{now.isocalendar().week}"
        if str(settings.get("weekly_last") or "") == key:
            return
        self.db.set_settings({"weekly_last": key})
        if str(settings.get("telegram_chat_id") or ""):
            self._notify_with_button(self.text_weekly(), event="weekly")

    def _maybe_evening_chart(self, settings: dict) -> None:
        at = str(settings.get("evening_chart_time") or "20:00")
        now = datetime.now()
        if now.strftime("%H:%M") != at:
            return
        today = now.strftime("%Y-%m-%d")
        if str(settings.get("evening_chart_last") or "") == today:
            return
        self.db.set_settings({"evening_chart_last": today})
        chat = str(settings.get("telegram_chat_id") or "")
        if not chat:
            return
        # пытаемся собрать график, если есть charts
        try:
            from ..charts import daily_report

            png, caption = daily_report(self.db)
            self._notify_with_button(caption, photo=png)
        except Exception:
            # fallback — просто текст
            self._notify_with_button("📈 Итоги дня — откройте цех, там график.", event="evening")

    # совместимость: старые методы из views, которые могут вызываться из тестов/менеджера
    def text_shelf(self, only_needs: bool = False) -> str:
        return "Полка — откройте цех."

    def text_queue(self) -> str:
        return "Очередь — откройте цех."
