"""Подтверждённый разбор брака и безопасная подготовка повторной печати.

Факт расхода уже фиксируется при завершении задания. Этот сервис не списывает
пластик повторно: он связывает причину с фактическими граммами/временем,
рассчитывает аналитическую потерю и, только по явному подтверждению, создаёт
один клон в очереди без физического автостарта.
"""
from __future__ import annotations

from .accounting import num, uid
from .config import now_iso


REASONS = {
    "detached": ("Деталь отклеилась", "Очистите стол и проверьте первый слой."),
    "clog": ("Засор сопла", "Прочистите сопло и проверьте подачу пластика."),
    "shift": ("Смещение слоёв", "Проверьте ремни, механику и отсутствие столкновений."),
    "runout": ("Закончился пластик", "Поставьте достаточную катушку и проверьте резерв."),
    "warp": ("Деформация", "Проверьте температуру стола, обдув и закрытие камеры."),
    "quality": ("Не прошло контроль качества", "Исправьте профиль или модель до повтора."),
    "support": ("Ошибка поддержек", "Измените поддержки и заново подготовьте файл."),
    "wrong_material": ("Неверный материал", "Сверьте файл и назначение слотов AMS."),
    "power": ("Сбой питания/связи", "Проверьте питание и соединение перед повтором."),
    "other": ("Другое", "Опишите причину и изменение перед повторной печатью."),
}


class DefectRecovery:
    def __init__(self, db, manager):
        self.db = db
        self.manager = manager

    def _job(self, job_id: str) -> dict:
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("Задание печати не найдено")
        if job.get("state") not in ("failed", "done"):
            raise ValueError("Разбор брака доступен только после завершения печати")
        return job

    def _loss(self, job: dict, lost_grams: float = 0) -> dict[str, float | str]:
        actual_grams = max(0.0, num(job.get("grams")))
        grams = num(lost_grams)
        if grams <= 0:
            grams = actual_grams
        if actual_grams > 0 and grams > actual_grams + 0.05:
            raise ValueError(f"Потеря не может быть больше факта печати: {actual_grams:g} г")
        grams = round(max(0.0, grams), 1)
        ratio = min(1.0, grams / actual_grams) if actual_grams > 0 else 1.0

        usage = self.db.one(
            "SELECT COALESCE(SUM(cost),0) cost FROM filament_usage WHERE job_id=?",
            (job["id"],),
        ) or {}
        filament_full = num(usage.get("cost"))
        source = "filament-usage"
        if filament_full <= 0 and actual_grams > 0:
            spool = None
            if job.get("spool_id"):
                spool = self.db.one("SELECT price,total_grams FROM spools WHERE id=?",
                                    (job["spool_id"],))
            if spool:
                filament_full = actual_grams * num(spool.get("price")) / max(
                    1.0, num(spool.get("total_grams"), 1000)
                )
                source = "job-spool"
            else:
                filament_full = actual_grams * num(
                    self.db.setting("default_spool_price", 1600), 1600
                ) / max(1.0, num(self.db.setting("default_spool_weight", 1000), 1000))
                source = "default-tariff"

        hours = max(0.0, num(job.get("duration_min"))) / 60.0
        energy_full = num(job.get("energy_kwh")) * num(
            self.db.setting("energy_price", 6), 6
        )
        wear_full = hours * (
            num(self.db.setting("amortization_per_hour", 12), 12)
            + num(self.db.setting("maintenance_per_hour", 3), 3)
        )
        filament = round(filament_full * ratio, 2)
        energy = round(energy_full * ratio, 2)
        wear = round(wear_full * ratio, 2)
        return {
            "grams": grams,
            "minutes": round(num(job.get("duration_min")) * ratio, 1),
            "filament": filament,
            "energy": energy,
            "wear": wear,
            "total": round(filament + energy + wear, 2),
            "source": source,
        }

    def summary(self, job_id: str, lost_grams: float = 0, reason: str = "") -> dict:
        job = self._job(job_id)
        order = None
        if job.get("order_id"):
            order = self.db.one("SELECT * FROM orders WHERE id=?", (job["order_id"],))
        defect = self.db.one(
            "SELECT * FROM defects WHERE job_id=? AND confirmed_at<>''"
            " ORDER BY datetime(confirmed_at) DESC LIMIT 1", (job_id,)
        )
        repeat = self.db.one(
            "SELECT * FROM print_jobs WHERE reprint_of_job_id=?", (job_id,)
        )
        loss = self._loss(job, lost_grams)
        reason_key = str(reason or (defect or {}).get("reason") or "").strip()
        recommendation = REASONS.get(reason_key, ("", ""))[1]
        previous_same = 0
        if reason_key:
            previous_same = int(num((self.db.one(
                "SELECT COUNT(*) n FROM defects WHERE reason=? AND confirmed_at<>''"
                " AND job_id<>? AND (order_id=? OR (?='' AND order_id IS NULL))",
                (reason_key, job_id, job.get("order_id") or "", job.get("order_id") or ""),
            ) or {}).get("n")))
        can_reprint = bool(str(job.get("file") or "").strip())
        blockers = []
        if not can_reprint:
            blockers.append("У задания нет файла для повтора")
        if repeat:
            blockers.append("Повтор уже подготовлен")
        return {
            "job": job,
            "order": order,
            "loss": loss,
            "defect": defect,
            "repeat_job": repeat,
            "already_recorded": bool(defect),
            "can_reprint": can_reprint and not repeat,
            "blockers": blockers,
            "reason": reason_key,
            "reason_title": REASONS.get(reason_key, (reason_key, ""))[0],
            "recommendation": recommendation,
            "previous_same_reason": previous_same,
            "repeat_risk": previous_same >= 1,
            "external_action_performed": False,
        }

    def recover(
        self,
        job_id: str,
        *,
        defect_confirmed: bool = False,
        reason: str = "",
        phase: str = "unknown",
        code: str = "",
        note: str = "",
        lost_grams: float = 0,
        reprint_confirmed: bool = False,
        repeat_risk_confirmed: bool = False,
        printer_id: str = "",
        request_id: str = "",
    ) -> dict:
        if not defect_confirmed:
            raise ValueError("Подтвердите причину и фактический брак")
        reason = str(reason or "").strip()
        if reason not in REASONS:
            raise ValueError("Выберите причину брака")
        note = str(note or "").strip()
        if reason == "other" and not note:
            raise ValueError("Для причины «Другое» добавьте комментарий")
        request_id = str(request_id or "").strip()[:120]
        if not request_id:
            raise ValueError("Не указан ключ операции разбора брака")

        by_request = self.db.one("SELECT * FROM defects WHERE request_id=?", (request_id,))
        if by_request:
            if by_request.get("job_id") != job_id:
                raise ValueError("Ключ операции уже использован для другого брака")
            result = self.summary(job_id, num(by_request.get("grams")), reason)
            result.update({
                "ok": True, "defect": by_request, "already_recorded": True,
                "loss_already_accounted": True,
            })
            return result

        job = self._job(job_id)
        existing = self.db.one(
            "SELECT * FROM defects WHERE job_id=? AND confirmed_at<>''", (job_id,)
        )
        if existing:
            if existing.get("reason") != reason:
                raise ValueError("Причина брака по этому заданию уже подтверждена")
            preview = self.summary(job_id, num(existing.get("grams")), reason)
            if reprint_confirmed and preview["repeat_risk"] and not repeat_risk_confirmed:
                raise ValueError(
                    "Эта причина уже повторялась. Подтвердите, что модель или профиль исправлены"
                )
            repeat = preview.get("repeat_job")
            if reprint_confirmed and not repeat:
                if not preview["can_reprint"]:
                    raise ValueError(
                        "Нельзя подготовить повтор: " + "; ".join(preview["blockers"])
                    )
                repeat = self.manager.reprint_job(
                    job_id,
                    printer_id,
                    confirmed=True,
                    request_id=f"defect-{existing['id']}",
                    defect_id=existing["id"],
                )
            result = self.summary(job_id, num(existing.get("grams")), reason)
            result.update({
                "ok": True, "defect": existing, "repeat_job": repeat,
                "already_recorded": True, "loss_already_accounted": True,
            })
            return result
        preview = self.summary(job_id, lost_grams, reason)
        if reprint_confirmed and not preview["can_reprint"]:
            raise ValueError("Нельзя подготовить повтор: " + "; ".join(preview["blockers"]))
        if reprint_confirmed and preview["repeat_risk"] and not repeat_risk_confirmed:
            raise ValueError(
                "Эта причина уже повторялась. Подтвердите, что модель или профиль исправлены"
            )

        stamp = now_iso()
        defect_id = uid("df")
        repeat = None
        with self.db.transaction():
            defect = self.db.upsert("defects", {
                "id": defect_id,
                "at": stamp,
                "printer_id": job.get("printer_id") or "",
                "job_id": job_id,
                "order_id": job.get("order_id") or None,
                "code": str(code or "").strip(),
                "phase": str(phase or "unknown").strip(),
                "reason": reason,
                "grams": preview["loss"]["grams"],
                "loss": preview["loss"]["total"],
                "note": note,
                "confirmed_at": stamp,
                "request_id": request_id,
                "loss_source": preview["loss"]["source"],
                "reprint_requested": 1 if reprint_confirmed else 0,
            })
            if reprint_confirmed:
                repeat = self.manager.reprint_job(
                    job_id,
                    printer_id,
                    confirmed=True,
                    request_id=f"defect-{defect_id}",
                    defect_id=defect_id,
                )
                defect = self.db.one("SELECT * FROM defects WHERE id=?", (defect_id,)) or defect
            self.db.add_event(
                "defect", "Причина брака подтверждена",
                f"{REASONS[reason][0]} · потеря {preview['loss']['total']:g} ₽",
                job.get("printer_id") or "",
                {"defect_id": defect_id, "job_id": job_id,
                 "reprint_job_id": (repeat or {}).get("id") or "",
                 "loss_already_accounted": True},
            )

        result = self.summary(job_id, num(preview["loss"]["grams"]), reason)
        result.update({
            "ok": True,
            "defect": self.db.one("SELECT * FROM defects WHERE id=?", (defect_id,)),
            "repeat_job": repeat,
            "already_recorded": False,
            "loss_already_accounted": True,
        })
        return result
