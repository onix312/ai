"""Расписания бота: дайджесты и напоминания, которые бот шлёт сам.

Утренний дайджест в заданное время, недельный отчёт и напоминание о
низком остатке полки — не чаще раза в сутки каждое. Отметка «уже послано»
живёт в настройках (digest_last и т.п.), поэтому рестарт коннектора не
дублирует рассылку.
"""
from __future__ import annotations

from datetime import datetime

from ..accounting import num


class SchedulesMixin:
    """Тики цикла опроса: что бот присылает без просьбы."""

    def _maybe_digest(self, settings: dict) -> None:
        """Утренний дайджест в digest_time, раз в сутки."""
        digest_time = str(settings.get("digest_time") or "09:00")
        now = datetime.now()
        if now.strftime("%H:%M") != digest_time:
            return
        last = str(settings.get("digest_last") or "")
        today = now.strftime("%Y-%m-%d")
        if last == today:
            return
        self.db.set_settings({"digest_last": today})
        chat = str(settings.get("telegram_chat_id") or "")
        if chat:
            self.manager.notify_async(self.text_digest())

    def _maybe_shelf_low(self, settings: dict) -> None:
        """Низкий остаток полки: напоминание раз в день + кнопка «Полка»."""
        try:
            rows = self.db.query(
                "SELECT * FROM shelf_items WHERE active=1 AND min_qty>0"
                " AND qty<=min_qty ORDER BY qty LIMIT 10")
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
            lines.append(f"• {row.get('name')} — {round(num(row.get('qty')),1)} шт "
                         f"(минимум {round(num(row.get('min_qty')),1)})")
        lines.append("\nЗаказать или пополнить — кнопкой.")
        self.manager.notify_async(
            "\n".join(lines),
            buttons=[("📦 Полка", "cmd:shelf"), ("🛒 Продать", "cmd:sell-home")],
            event="shelf:low")

    def _maybe_weekly(self, settings: dict) -> None:
        """Еженедельный отчёт: день недели (1=пн) и время."""
        day = int(num(settings.get("weekly_report_day", 1), 1))
        at = str(settings.get("weekly_report_time") or "20:00")
        now = datetime.now()
        if now.isoweekday() != day or now.strftime("%H:%M") != at:
            return
        key = f"{now.isocalendar().year}-W{now.isocalendar().week}"
        if str(settings.get("weekly_last") or "") == key:
            return
        self.db.set_settings({"weekly_last": key})
        chat = str(settings.get("telegram_chat_id") or "")
        if chat:
            self.manager.notify_async(self.text_weekly())

    def _maybe_evening_chart(self, settings: dict) -> None:
        """Вечерний отчёт-картинка: итоги дня (включая мобильную кассу) в ТГ.

        Просьба владельца: «отчётность с кассы мобильной кидать в тг».
        Раз в сутки в ``evening_chart_time`` (по умолчанию 20:00) в чат
        уходит тот же PNG-график, что и по команде «график».
        """
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
        try:
            from .charts import daily_report
            png, caption = daily_report(self.db)
        except Exception:
            return
        self.manager.notify_async(caption, photo=png)
