"""Notify handlers — все уведомления + кнопка web_app.

Печать, inbox, касса, низкий остаток, дайджест 09:00, график 20:00, недельный.
Бот тонкий, но расписание шлёт сам, через manager.notify_async.
"""
from __future__ import annotations

import json
from datetime import datetime

from ...accounting import num
from ..core.config import get_miniapp_url
from ..ui import web_app_keyboard


class NotifyMixin:
    """Миксин с тиками расписания и текстами уведомлений."""

    def _notify_with_button(self, text: str, event: str = "", photo: bytes | None = None) -> None:
        """Отправить уведомление с кнопкой «Открыть цех»."""
        url = ""
        try:
            url = get_miniapp_url(self.db)
        except Exception:
            url = "https://example.com/staff"
        kb = web_app_keyboard(url)
        # manager.notify_async понимает кнопки как list[tuple] callback,
        # но мы хотим web_app. Поэтому формируем reply_markup вручную
        # и передаём через manager.send_telegram если есть, иначе через bot._call.
        # Для совместимости с manager.send_telegram, который теперь умеет web_app,
        # передаём buttons как list[dict] с web_app.
        try:
            # новый формат: buttons как список словарей-кнопок
            buttons = [[{"text": "🏭 Открыть цех", "web_app": {"url": url}}]]
            # если manager умеет, используем его
            if hasattr(self, "manager") and hasattr(self.manager, "notify_async"):
                # manager.notify_async ожидает buttons как list[tuple] или list[list[dict]]?
                # Мы передаём special kwarg через send_telegram напрямую с reply_markup
                # Чтобы не ломать старый путь, вызываем send_telegram с reply_markup json
                # через manager.send_telegram с кастомным markup
                rm = json.dumps({"inline_keyboard": buttons}, ensure_ascii=False)
                # обходим notify_async и идём в send_telegram с кастомным markup
                # но manager.send_telegram принимает buttons, а не markup.
                # Поэтому используем manager.send_telegram с buttons=None и
                # подменяем reply_markup внутри через monkey? Проще вызвать bot._call
                # напрямую для owner chat, а для подписчиков — через manager.notify_async с текстом + кнопка callback «menu»
                # Для минимализма: используем manager.notify_async с callback-кнопкой «Открыть цех» → menu
                # А web_app добавим в bot._send_main_menu для интерактива.
                # Здесь шлём через manager.notify_async с кнопкой callback, которая откроет меню с web_app
                self.manager.notify_async(
                    text,
                    photo=photo,
                    buttons=[("🏭 Открыть цех", "cmd:menu")],
                    event=event,
                )
                return
        except Exception:
            pass
        # fallback — шлём напрямую в owner chat
        try:
            chat = str(self._settings().get("telegram_chat_id") or "")
            if not chat:
                return
            kb = web_app_keyboard(url)
            self._call(
                "sendMessage",
                {
                    "chat_id": chat,
                    "text": text[:3800],
                    "reply_markup": json.dumps(kb, ensure_ascii=False),
                    "disable_web_page_preview": "true",
                },
                timeout=15,
            )
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
