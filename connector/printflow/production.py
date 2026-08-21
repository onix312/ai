"""Безопасная подготовка заказа к производству.

Модуль ничего не запускает на принтере. Он собирает требования заказа,
учитывает уже зарезервированный очередью пластик, выбирает принтер и катушку,
а затем идемпотентно создаёт подготовленное задание.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .accounting import num
from .config import UPLOAD_DIR, now_iso
from .db import Database

_PRIORITY = {"urgent": 100, "high": 50, "normal": 0, "low": -10}
_ACTIVE_JOB_STATES = ("queued", "uploading", "starting", "running")


def _norm(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


class ProductionPreparation:
    """Проверка готовности и постановка заказа в очередь без автозапуска."""

    def __init__(self, db: Database, manager):
        self.db = db
        self.manager = manager

    def _has_items(self, order_id: str) -> bool:
        return self.db.one(
            "SELECT 1 FROM order_items WHERE order_id=? LIMIT 1", (order_id,)) is not None

    def _requirements(self, order: dict) -> dict[str, float]:
        multiplier = 1.0 if self._has_items(order["id"]) else max(1.0, num(order.get("qty"), 1))
        return {
            "qty": max(1.0, num(order.get("qty"), 1)),
            "grams": round(num(order.get("grams")) * multiplier, 1),
            "hours": round(num(order.get("hours")) * multiplier, 2),
            "minutes": round(num(order.get("hours")) * multiplier * 60, 1),
        }

    def _reserved_grams(self) -> dict[str, float]:
        """Сколько граммов катушки уже обещано активным заданиям."""
        marks = ",".join("?" for _ in _ACTIVE_JOB_STATES)
        rows = self.db.query(
            f"SELECT j.spool_id, j.est_grams, j.order_id, o.grams, o.qty"
            f" FROM print_jobs j LEFT JOIN orders o ON o.id=j.order_id"
            f" WHERE j.state IN ({marks}) AND j.spool_id IS NOT NULL",
            _ACTIVE_JOB_STATES,
        )
        reserved: dict[str, float] = {}
        for row in rows:
            grams = num(row.get("est_grams"))
            if not grams and row.get("order_id"):
                multiplier = (1.0 if self._has_items(row["order_id"])
                              else max(1.0, num(row.get("qty"), 1)))
                grams = num(row.get("grams")) * multiplier
            spool_id = str(row.get("spool_id") or "")
            reserved[spool_id] = reserved.get(spool_id, 0.0) + max(0.0, grams)
        return reserved

    def _printers(self) -> list[dict]:
        rows = self.db.query(
            "SELECT id, name, model, enabled, mode FROM printers"
            " WHERE enabled=1 ORDER BY name, id")
        out = []
        for row in rows:
            runtime = self.manager.get(row["id"]) if self.manager else None
            state = ""
            connected = bool(getattr(runtime, "connected", False)) if runtime else False
            if runtime:
                try:
                    state = str(runtime.snapshot().get("printer", {}).get("state") or "")
                except Exception:
                    state = str(getattr(runtime, "state", "") or "")
            out.append({
                "id": row["id"], "name": row.get("name") or row["id"],
                "model": row.get("model") or "", "connected": connected,
                "state": state,
            })
        return out

    def _spools(self, material: str, color: str, required: float,
                reserved: dict[str, float]) -> list[dict]:
        sql = "SELECT * FROM spools WHERE archived=0 AND remaining_grams>0"
        params: list[Any] = []
        if material:
            sql += " AND pylower(material)=?"
            params.append(material.casefold())
        rows = self.db.query(sql, params)
        wanted_color = _norm(color)
        out = []
        for row in rows:
            held = round(reserved.get(row["id"], 0.0), 1)
            available = round(max(0.0, num(row.get("remaining_grams")) - held), 1)
            row_color = _norm(row.get("color_name") or row.get("color_hex"))
            color_match = bool(wanted_color and row_color and
                               (wanted_color in row_color or row_color in wanted_color))
            slot = row.get("ams_slot")
            bound = bool(row.get("printer_id") and slot not in (None, ""))
            out.append({
                "id": row["id"],
                "material": row.get("material") or "",
                "color": row.get("color_name") or row.get("color_hex") or "",
                "remaining_grams": round(num(row.get("remaining_grams")), 1),
                "reserved_grams": held,
                "available_grams": available,
                "enough": available >= required if required > 0 else True,
                "color_match": color_match,
                "printer_id": row.get("printer_id") or "",
                "ams_slot": str(slot) if slot not in (None, "") else "",
                "in_ams": bound,
            })
        out.sort(key=lambda spool: (
            not spool["enough"], not spool["color_match"], not spool["in_ams"],
            -spool["available_grams"],
        ))
        return out

    @staticmethod
    def _multi_colors(order: dict) -> list[dict]:
        try:
            values = json.loads(str(order.get("colors") or "[]"))
        except (json.JSONDecodeError, TypeError):
            return []
        return values if isinstance(values, list) else []

    def readiness(self, order_id: str, printer_id: str = "",
                  spool_id: str = "") -> dict[str, Any]:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        requirements = self._requirements(order)
        active = self.db.one(
            "SELECT * FROM print_jobs WHERE order_id=?"
            " AND state IN ('queued','uploading','starting','running')"
            " ORDER BY datetime(created_at) LIMIT 1", (order_id,))
        printers = self._printers()
        reserved = self._reserved_grams()
        spools = self._spools(str(order.get("material") or ""),
                             str(order.get("color") or ""),
                             requirements["grams"], reserved)

        selected_spool = next((spool for spool in spools if spool["id"] == spool_id), None)
        if not selected_spool:
            selected_spool = next((spool for spool in spools if spool["enough"]), None)
        selected_printer = next((printer for printer in printers if printer["id"] == printer_id), None)
        if not selected_printer and selected_spool and selected_spool["printer_id"]:
            selected_printer = next(
                (printer for printer in printers
                 if printer["id"] == selected_spool["printer_id"]), None)
        if not selected_printer:
            selected_printer = next((printer for printer in printers if printer["connected"]), None)
        if not selected_printer and printers:
            selected_printer = printers[0]

        blocks: list[dict] = []
        warns: list[dict] = []
        infos: list[dict] = []
        file = str(order.get("file") or "").strip()
        material = str(order.get("material") or "").strip()
        if not file:
            blocks.append({"code": "file", "text": "Не указан файл модели"})
        elif not (UPLOAD_DIR / Path(file).name).exists():
            infos.append({"code": "file_remote", "text":
                          "Локальной копии файла нет — перед запуском будет проверена SD-карта"})
        if not printers:
            blocks.append({"code": "printer", "text": "Не настроен активный принтер"})
        if requirements["grams"] <= 0:
            blocks.append({"code": "grams", "text": "Не указан расход пластика"})
        if requirements["hours"] <= 0:
            warns.append({"code": "hours", "text": "Не указано время — срок очереди будет неточным"})
        if not material:
            blocks.append({"code": "material", "text": "Не указан материал"})
        if len(self._multi_colors(order)) > 1:
            blocks.append({"code": "multicolor", "text":
                           "Для многоцветного заказа сначала назначьте катушки и AMS-слоты вручную"})
        if requirements["grams"] > 0 and material:
            total_available = round(sum(spool["available_grams"] for spool in spools), 1)
            if not selected_spool or not selected_spool["enough"]:
                available = num((selected_spool or {}).get("available_grams"))
                blocks.append({
                    "code": "filament",
                    "text": (f"Нет одной свободной катушки {material} с запасом "
                             f"{requirements['grams']:.0f} г; на выбранной доступно "
                             f"{available:.0f} г, суммарно {total_available:.0f} г"),
                })
            elif order.get("color") and not selected_spool["color_match"]:
                warns.append({"code": "color", "text":
                              f"Выбрана катушка цвета «{selected_spool['color'] or 'не указан'}»,"
                              f" заказ — «{order.get('color')}»"})
            if (selected_spool and selected_spool["in_ams"] and selected_printer
                    and selected_spool["printer_id"] != selected_printer["id"]):
                blocks.append({"code": "spool_printer", "text":
                               "Выбранная катушка установлена в AMS другого принтера"})
            elif selected_spool and not selected_spool["in_ams"]:
                warns.append({"code": "ams", "text":
                              "Катушка есть на складе, но не привязана к слоту AMS"})
        if active:
            infos.append({"code": "queued", "text": "Заказ уже подготовлен в очереди"})

        return {
            "ok": bool(active) or not blocks,
            "already_queued": bool(active),
            "order": {"id": order["id"], "number": order.get("number") or "",
                      "product": order.get("product") or "", "file": file,
                      "material": material, "color": order.get("color") or ""},
            "requirements": requirements,
            "blocks": blocks,
            "warns": warns,
            "infos": infos,
            "printers": printers,
            "spools": spools,
            "selected_printer": selected_printer,
            "selected_spool": selected_spool,
            "job": ({"id": active["id"], "state": active.get("state") or "",
                     "name": active.get("name") or ""} if active else None),
        }

    def prepare(self, order_id: str, printer_id: str = "",
                spool_id: str = "") -> dict[str, Any]:
        with self.db.transaction():
            ready = self.readiness(order_id, printer_id, spool_id)
            if ready["already_queued"]:
                return {"ok": True, "already_queued": True,
                        "job": ready["job"], "readiness": ready}
            if ready["blocks"]:
                raise ValueError("Нельзя подготовить: " + "; ".join(
                    item["text"] for item in ready["blocks"]))
            printer = ready["selected_printer"] or {}
            spool = ready["selected_spool"] or {}
            order = ready["order"]
            req = ready["requirements"]
            source_order = self.db.one(
                "SELECT priority,status FROM orders WHERE id=?", (order_id,)) or {}
            queue_status = self.db.one("SELECT id FROM statuses WHERE id='queue'")
            target_status = (queue_status or {}).get("id") or source_order.get("status") or "new"
            job = self.manager.enqueue({
                "name": order["product"] or Path(order["file"]).stem,
                "file": order["file"],
                "order_id": order_id,
                "printer_id": printer.get("id") or "",
                "spool_id": spool.get("id") or "",
                "source": "order-prepared",
                "priority": _PRIORITY.get(source_order.get("priority") or "normal", 0),
                "est_grams": req["grams"],
                "est_minutes": req["minutes"],
                "allow_auto_start": False,
            })
            self.db.execute(
                "UPDATE orders SET status=?, spools=?, updated_at=? WHERE id=?",
                (target_status,
                 json.dumps([{"spool_id": spool.get("id"), "grams": req["grams"],
                              "note": "автоподбор при подготовке"}], ensure_ascii=False),
                 now_iso(), order_id),
            )
        result = self.readiness(order_id, printer.get("id") or "", spool.get("id") or "")
        return {"ok": True, "already_queued": False, "job": job, "readiness": result}
