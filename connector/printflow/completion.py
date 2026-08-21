"""Завершение производства: план/факт, приёмка и текст клиенту.

Событие принтера фиксирует объективный факт автоматически. Визуальное качество
и перевод заказа в «Готов» выполняются только явным подтверждением мастера.
Внешние сообщения этот модуль не отправляет — он возвращает безопасный черновик.
"""
from __future__ import annotations

import json
from typing import Any

from .accounting import num
from .config import now_iso
from .db import Database
from .repo import Repo

ACTIVE_JOB_STATES = ("queued", "starting", "running")
TERMINAL_JOB_STATES = ("done", "failed", "cancelled")


class OrderCompletion:
    """Единый сервис производственной приёмки заказа."""

    def __init__(self, db: Database, repo: Repo):
        self.db = db
        self.repo = repo

    @staticmethod
    def _planned(order: dict, has_items: bool) -> dict:
        multiplier = 1.0 if has_items else max(1.0, num(order.get("qty"), 1))
        return {
            "grams": round(num(order.get("grams")) * multiplier, 1),
            "hours": round(num(order.get("hours")) * multiplier, 3),
            "cost": round(num(order.get("cost")), 2),
        }

    @staticmethod
    def _difference(planned: float, actual: float, digits: int = 1) -> dict:
        delta = actual - planned
        return {
            "delta": round(delta, digits),
            "percent": round(delta / planned * 100, 1) if planned else None,
        }

    def _qc(self, order: dict) -> dict:
        raw_steps = self.db.setting("qc_checklist", [])
        steps = [str(step) for step in raw_steps] if isinstance(raw_steps, list) else []
        try:
            done_map = json.loads(str(order.get("qc_done") or "{}"))
        except (json.JSONDecodeError, TypeError):
            done_map = {}
        if not isinstance(done_map, dict):
            done_map = {}
        completed = [step for index, step in enumerate(steps) if done_map.get(str(index))]
        missing = [step for index, step in enumerate(steps) if not done_map.get(str(index))]
        return {
            "total": len(steps),
            "done": len(completed),
            "complete": not missing,
            "completed": completed,
            "missing": missing,
        }

    def _ready_message(self, order: dict) -> tuple[str, str]:
        """Вернуть (текст, источник), не отправляя его во внешний канал."""
        name = str(order.get("customer_name") or "").strip()
        number = str(order.get("number") or "").strip()
        product = str(order.get("product") or "заказ").strip()
        remaining_value = round(max(
            0.0,
            num(order.get("price")) - max(num(order.get("paid")), num(order.get("prepaid"))),
        ), 2)
        def amount_text(value: float) -> str:
            return str(int(value)) if value.is_integer() else str(value)

        values = {
            "name": name,
            "number": number,
            "product": product,
            "due": str(order.get("due") or "").strip(),
            "price": amount_text(round(num(order.get("price")), 2)),
            "remaining": amount_text(remaining_value),
        }

        templates = self.db.setting("reply_templates", [])
        if isinstance(templates, str):
            try:
                templates = json.loads(templates) if templates else []
            except json.JSONDecodeError:
                templates = []
        if isinstance(templates, list):
            for template in templates:
                if not isinstance(template, dict) or not template.get("text"):
                    continue
                marker = f"{template.get('id', '')} {template.get('title', '')}".lower()
                if "готов" not in marker and "ready" not in marker:
                    continue
                text = str(template["text"])
                for key, value in values.items():
                    text = text.replace("{" + key + "}", value)
                return text.strip(), "template"

        greeting = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
        order_ref = f" №{number}" if number else ""
        message = f"{greeting} Ваш заказ{order_ref} «{product}» готов. Можно забирать."
        if remaining_value > 0:
            message += f" Осталось к оплате: {amount_text(remaining_value)} ₽."
        message += " Спасибо!"
        return message, "default"

    def summary(self, order_id: str) -> dict[str, Any]:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        jobs = self.db.query(
            "SELECT * FROM print_jobs WHERE order_id=? ORDER BY datetime(created_at)",
            (order_id,),
        )
        items = self.db.one("SELECT id FROM order_items WHERE order_id=? LIMIT 1", (order_id,))
        planned = self._planned(order, items is not None)
        counted_jobs = [job for job in jobs if job.get("state") in ("done", "failed")]
        job_grams = sum(num(job.get("grams")) for job in counted_jobs)
        job_hours = sum(num(job.get("duration_min")) for job in counted_jobs) / 60.0
        job_cost = sum(num(job.get("cost")) for job in counted_jobs)
        actual = {
            # Поля заказа включают все завершённые попытки; суммы заданий —
            # страховочный источник для старых баз и ручного режима учёта.
            "grams": round(max(num(order.get("actual_grams")), job_grams), 1),
            "hours": round(max(num(order.get("actual_hours")), job_hours), 3),
            "cost": round(max(num(order.get("actual_cost")), job_cost), 2),
        }
        actual["grams_difference"] = self._difference(planned["grams"], actual["grams"], 1)
        actual["hours_difference"] = self._difference(planned["hours"], actual["hours"], 3)
        actual["cost_difference"] = self._difference(planned["cost"], actual["cost"], 2)

        active = [job for job in jobs if job.get("state") in ACTIVE_JOB_STATES]
        successful = [job for job in jobs if job.get("state") == "done"]
        failed = [job for job in jobs if job.get("state") == "failed"]
        cancelled = [job for job in jobs if job.get("state") == "cancelled"]
        unaccounted = [
            job for job in successful + failed if not job.get("accounted_at")
        ]
        qc = self._qc(order)
        status = self.db.one("SELECT * FROM statuses WHERE id=?", (order.get("status"),)) or {}
        accepted = order.get("status") == "ready"
        final = bool(num(status.get("is_final")))

        blocks: list[dict] = []
        warns: list[dict] = []
        if not successful and not accepted:
            blocks.append({"code": "successful_job", "text": "Нет успешно завершённой печати"})
        if active and not accepted:
            blocks.append({
                "code": "active_jobs",
                "text": f"Есть незавершённые задания: {len(active)}",
            })
        if unaccounted and not accepted:
            blocks.append({
                "code": "accounting",
                "text": "Фактический расход по завершённой печати ещё не зафиксирован",
            })
        if final and not accepted:
            blocks.append({"code": "final_status", "text": "Заказ уже закрыт или выдан"})
        if failed:
            warns.append({
                "code": "failed_jobs",
                "text": f"Неудачных попыток печати: {len(failed)}; их расход включён в факт",
            })
        if qc["missing"]:
            warns.append({
                "code": "qc",
                "text": f"В чек-листе не отмечено: {len(qc['missing'])}",
            })
        if planned["grams"] and actual["grams"] > planned["grams"] * 1.1:
            warns.append({
                "code": "grams_overrun",
                "text": "Фактический расход пластика выше плана более чем на 10%",
            })
        if planned["hours"] and actual["hours"] > planned["hours"] * 1.1:
            warns.append({
                "code": "time_overrun",
                "text": "Фактическое время печати выше плана более чем на 10%",
            })

        message, message_source = self._ready_message(order)
        return {
            "ok": True,
            "order_id": order_id,
            "number": order.get("number") or "",
            "status": {
                "id": order.get("status") or "",
                "name": status.get("name") or order.get("status") or "",
            },
            "accepted": accepted,
            "can_accept": accepted or not blocks,
            "plan": planned,
            "actual": actual,
            "jobs": {
                "total": len(jobs),
                "active": len(active),
                "successful": len(successful),
                "failed": len(failed),
                "cancelled": len(cancelled),
                "accounting_pending": len(unaccounted),
            },
            "qc": qc,
            "blocks": blocks,
            "warns": warns,
            "message": message,
            "message_source": message_source,
            "external_sent": False,
        }

    def accept(self, order_id: str, *, quality_confirmed: bool = False) -> dict[str, Any]:
        """Подтвердить визуальное качество и идемпотентно поставить «Готов»."""
        if not quality_confirmed:
            raise ValueError("Подтвердите визуальную приёмку результата")
        with self.db.transaction():
            summary = self.summary(order_id)
            if summary["accepted"]:
                summary["already_accepted"] = True
                return summary
            if summary["blocks"]:
                raise ValueError("Нельзя принять заказ: " + "; ".join(
                    item["text"] for item in summary["blocks"]
                ))
            ready = self.db.one("SELECT id FROM statuses WHERE id='ready' AND is_final=0")
            if not ready:
                raise ValueError("Не настроен статус «Готов»")
            self.repo.save_order({
                "id": order_id,
                "status": ready["id"],
                "quality": "passed",
                "author": "production-acceptance",
            })
            self.db.add_event(
                "order",
                "Результат принят — заказ готов",
                f"№{summary['number']} · сообщение клиенту подготовлено, но не отправлено",
                data={"order_id": order_id},
            )
            accepted = self.summary(order_id)
            accepted["already_accepted"] = False
            accepted["accepted_at"] = now_iso()
            return accepted
