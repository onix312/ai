"""Паспорт печати (роадмап B.1.2): всё об одном задании в одном месте.

К заданию собираются: заказ, смета слайсера (план), факт, отклонения,
события сторожа (HMS) за время печати, фото заказа и катушка пластика.
Так видно, почему конкретная печать оказалась дороже или дольше плана —
без раскопок по разным разделам.
"""
from __future__ import annotations

from typing import Any

from .accounting import num
from .config import UPLOAD_DIR


def _pct(plan: float, fact: float) -> float:
    if plan <= 0:
        return 0.0
    return round((fact - plan) / plan * 100, 1)


def _verdict(plan: float, fact: float, kind: str) -> str:
    diff = _pct(plan, fact)
    if plan <= 0:
        return "сметы не было"
    if diff <= 5:
        return f"{kind} по плану"
    if diff < 0:
        return f"{kind} на {abs(diff)}% меньше плана"
    return f"{kind} на {diff}% больше плана"


def job_passport(db, job_id: str) -> dict[str, Any]:
    """Собрать паспорт задания печати. Вызывает ValueError, если задания нет."""
    job = db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
    if not job:
        raise ValueError("Задание не найдено")
    out: dict[str, Any] = {"job": job, "order": {}, "estimate": {},
                           "plan_vs_fact": {}, "guard": [], "photos": [],
                           "spool": {}, "error_decoded": {}}
    # --- заказ, к которому относится печать
    if job.get("order_id"):
        order = db.one("SELECT * FROM orders WHERE id=?", (job["order_id"],))
        if order:
            out["order"] = {key: order.get(key) for key in
                            ("id", "number", "product", "material", "color",
                             "price", "customer_name")}
            out["photos"] = db.query(
                "SELECT * FROM order_photos WHERE order_id=? ORDER BY at DESC",
                (order["id"],))
    # --- смета слайсера (план)
    estimate: dict[str, Any] = {}
    if job.get("file"):
        local = UPLOAD_DIR / str(job["file"])
        if local.exists():
            try:
                from .estimate import estimate_file
                estimate = estimate_file(local)
            except Exception:
                estimate = {}
    out["estimate"] = estimate
    # --- план против факта
    # Многоплитный проект: план — сумма по плитам (total_×), а не первая плита,
    # иначе факт всей печати сравнится с планом одной плиты.
    plan_min = num(estimate.get("total_minutes")) or num(estimate.get("minutes"))
    fact_min = num(job.get("duration_min"))
    plan_grams = num(estimate.get("total_grams")) or num(estimate.get("grams"))
    fact_grams = num(job.get("grams"))
    out["plan_vs_fact"] = {
        "minutes": {"plan": round(plan_min, 1), "fact": round(fact_min, 1),
                    "diff_pct": _pct(plan_min, fact_min),
                    "verdict": _verdict(plan_min, fact_min, "время")},
        "grams": {"plan": round(plan_grams, 1), "fact": round(fact_grams, 1),
                  "diff_pct": _pct(plan_grams, fact_grams),
                  "verdict": _verdict(plan_grams, fact_grams, "пластик")},
    }
    # --- события сторожа за время печати
    since = job.get("started_at") or job.get("queued_at") or ""
    if since:
        rows = db.query(
            "SELECT * FROM events WHERE kind='guard' AND printer_id=?"
            " AND at>=? AND at<=COALESCE(?, datetime('now')) ORDER BY at",
            (job.get("printer_id") or "", since,
             job.get("finished_at") or None))
        for row in rows:
            try:
                import json
                row["data"] = json.loads(row.get("data") or "{}")
            except Exception:
                row["data"] = {}
        out["guard"] = rows
    # --- катушка, с которой печатали
    if job.get("spool_id"):
        out["spool"] = db.one("SELECT * FROM spools WHERE id=?",
                              (job["spool_id"],)) or {}
    # --- расшифровка ошибки печати
    if job.get("error"):
        try:
            from .hms import decode
            out["error_decoded"] = decode(str(job["error"]))
        except Exception:
            pass
    return out
