"""Цех 9.0: катушки, AMS, поставщики, пресеты плит, чек-лист, документы.

Правила учёта 3.0:
  * нет второго «устного» остатка;
  * пластик приходуется только документом workshop_docs / shopping.receive;
  * печать не стартует без preflight (это в manager/api).
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .accounting import num, uid
from .config import now_iso


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw: Any, default: Any = None):
    if raw in (None, ""):
        return default if default is not None else {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default if default is not None else {}


class WorkshopV9:
    def __init__(self, db, repo=None, shopping=None, manager=None, acc=None):
        self.db = db
        self.repo = repo
        self.shopping = shopping
        self.manager = manager
        self.acc = acc

    # ---------------------------------------------------------------- schema
    def ensure_schema(self) -> None:
        """Идемпотентно: таблицы 9.0 создаёт db.py, здесь — мягкие колонки."""
        extras = {
            "spools": (
                ("location", "TEXT DEFAULT 'shop'"),
                ("location_note", "TEXT DEFAULT ''"),
                ("label_note", "TEXT DEFAULT ''"),
                ("qr_payload", "TEXT DEFAULT ''"),
                ("price_per_kg", "REAL DEFAULT 0"),
                ("supplier_id", "TEXT DEFAULT ''"),
                ("received_doc_id", "TEXT DEFAULT ''"),
            ),
            "shopping_items": (
                ("receipt_doc_id", "TEXT DEFAULT ''"),
                ("price_per_kg", "REAL DEFAULT 0"),
                ("supplier_id", "TEXT DEFAULT ''"),
            ),
            "print_jobs": (
                ("start_request_id", "TEXT DEFAULT ''"),
                ("mixed_label", "TEXT DEFAULT ''"),
                ("no_auto", "INTEGER DEFAULT 0"),
                ("plate_preset_id", "TEXT DEFAULT ''"),
            ),
        }
        for table, cols in extras.items():
            have = {r["name"] for r in self.db.query(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # -------------------------------------------------------------- inventory
    def inventory_summary(self, spools: list[dict] | None = None) -> dict:
        rows = list(spools if spools is not None else (self.repo.spools() if self.repo else []))
        threshold = num(self.db.setting("filament_low_threshold", 15), 15)
        low = []
        by_mat: dict[str, dict] = {}
        grams_total = 0.0
        for s in rows:
            grams = num(s.get("remaining_grams"))
            grams_total += grams
            pct = num(s.get("percent"))
            if pct < threshold and grams > 0:
                low.append({
                    "id": s.get("id"),
                    "material": s.get("material") or "",
                    "color_name": s.get("color_name") or "",
                    "color_hex": s.get("color_hex") or "#888888",
                    "remaining_grams": grams,
                    "percent": pct,
                    "location": s.get("location") or "shop",
                })
            mat = (s.get("material") or "PLA").upper()
            bucket = by_mat.setdefault(mat, {"material": mat, "grams": 0.0, "spools": 0})
            bucket["grams"] += grams
            bucket["spools"] += 1
        return {
            "count": len(rows),
            "grams": round(grams_total, 1),
            "low": low,
            "low_count": len(low),
            "by_material": sorted(by_mat.values(), key=lambda x: -x["grams"]),
            "threshold": threshold,
        }

    def enough_for_next(self, printer_id: str = "") -> dict:
        """Хватит ли катушек на следующее задание очереди."""
        jobs = []
        if self.manager:
            try:
                jobs = [j for j in self.manager.queue() if j.get("state") == "queued"]
            except Exception:
                jobs = []
        if printer_id:
            jobs = [j for j in jobs if not j.get("printer_id") or j.get("printer_id") == printer_id]
        nxt = jobs[0] if jobs else None
        if not nxt:
            return {"ok": True, "job": None, "enough": True, "message": "Очередь пуста"}
        need = num(nxt.get("est_grams") or nxt.get("grams"))
        mapping = _loads(nxt.get("ams_mapping"), [])
        spools = self.repo.spools() if self.repo else []
        if mapping:
            slots = {str(x) for x in mapping if x not in (None, "", -1, "-1")}
            pool = [s for s in spools if str(s.get("ams_slot") or "") in slots]
        else:
            mat = str(nxt.get("material") or "").upper()
            pool = [s for s in spools if not mat or str(s.get("material") or "").upper() == mat]
            if printer_id:
                same = [s for s in pool if s.get("printer_id") == printer_id]
                if same:
                    pool = same
        have = round(sum(num(s.get("remaining_grams")) for s in pool), 1)
        enough = (need <= 0) or (have >= need)
        return {
            "ok": True,
            "job": {"id": nxt.get("id"), "name": nxt.get("name"), "grams": need,
                    "material": nxt.get("material") or ""},
            "have": have,
            "need": need,
            "enough": enough,
            "spools": [{"id": s.get("id"), "remaining_grams": num(s.get("remaining_grams")),
                        "material": s.get("material"), "color_name": s.get("color_name")}
                       for s in pool[:8]],
            "message": ("Хватит" if enough else f"Мало пластика: есть {have} г, нужно {need} г"),
        }

    def set_spool_location(self, spool_id: str, location: str, note: str = "") -> dict:
        """Метка «магазин / дом» — это ярлык, не второй склад."""
        loc = (location or "shop").strip().lower()
        if loc not in ("shop", "home", "ams", "dry", "other"):
            raise ValueError("Место: shop / home / ams / dry / other")
        spool = self.repo.spool(spool_id) if self.repo else None
        if not spool:
            raise ValueError("Катушка не найдена")
        self.db.execute(
            "UPDATE spools SET location=?, location_note=?, updated_at=? WHERE id=?",
            (loc, (note or "")[:200], now_iso(), spool_id),
        )
        return {"ok": True, "spool": self.repo.spool(spool_id)}

    # --------------------------------------------------------------- AMS 151
    def bind_unique_slot(
        self,
        spool_id: str,
        slot,
        printer_id: str = "",
        tray_uuid: str = "",
        force: bool = False,
        note: str = "",
    ) -> dict:
        """Жёстко уникальный слот AMS: одна катушка на слот принтера."""
        spool = self.repo.spool(spool_id) if self.repo else None
        if not spool:
            raise ValueError("Катушка не найдена")
        printer_id = str(printer_id or spool.get("printer_id") or "").strip()
        if slot in (None, ""):
            self.db.execute(
                "UPDATE spools SET printer_id=?, ams_slot='', tray_uuid='', updated_at=? WHERE id=?",
                (printer_id or None, now_iso(), spool_id),
            )
            self._slot_history(printer_id, "", spool_id, "unbind", note)
            return {"ok": True, "spool": self.repo.spool(spool_id), "cleared": True}
        try:
            slot_n = int(float(slot))
        except (TypeError, ValueError) as exc:
            raise ValueError("Слот AMS: 0–15") from exc
        if not 0 <= slot_n <= 15:
            raise ValueError("Слот AMS: 0–15")
        slot_s = str(slot_n)
        if not printer_id:
            raise ValueError("Чтобы занять слот, укажите принтер")
        other = self.db.one(
            "SELECT * FROM spools WHERE id<>? AND printer_id=? AND ams_slot=? "
            "AND remaining_grams>0",
            (spool_id, printer_id, slot_s),
        )
        if other and not force:
            raise ValueError(
                f"Слот {slot_s} уже занят катушкой "
                f"{other.get('material') or ''} {other.get('color_name') or other['id']}. "
                "Снимите её или подтвердите force."
            )
        if other and force:
            self.db.execute(
                "UPDATE spools SET ams_slot='', tray_uuid='', updated_at=? WHERE id=?",
                (now_iso(), other["id"]),
            )
            self._slot_history(printer_id, slot_s, other["id"], "evict", f"освободили для {spool_id}")
        self.db.execute(
            "UPDATE spools SET printer_id=?, ams_slot=?, tray_uuid=?, location='ams', updated_at=? WHERE id=?",
            (printer_id, slot_s, tray_uuid or "", now_iso(), spool_id),
        )
        self._slot_history(printer_id, slot_s, spool_id, "bind", note)
        return {
            "ok": True,
            "spool": self.repo.spool(spool_id),
            "evicted": other["id"] if other else "",
        }

    def _slot_history(self, printer_id: str, slot: str, spool_id: str, action: str, note: str = "") -> None:
        self.db.upsert("ams_slot_history", {
            "id": uid("ash"),
            "at": now_iso(),
            "printer_id": printer_id or "",
            "slot": str(slot or ""),
            "spool_id": spool_id or "",
            "action": action,
            "note": (note or "")[:300],
        })

    def slot_history(self, printer_id: str = "", limit: int = 80) -> list[dict]:
        if printer_id:
            return self.db.query(
                "SELECT * FROM ams_slot_history WHERE printer_id=? "
                "ORDER BY datetime(at) DESC LIMIT ?",
                (printer_id, int(limit)),
            )
        return self.db.query(
            "SELECT * FROM ams_slot_history ORDER BY datetime(at) DESC LIMIT ?",
            (int(limit),),
        )

    # --------------------------------------------------------------- scrap 154
    def record_scrap(
        self,
        spool_id: str,
        grams,
        reason: str = "",
        note: str = "",
        confirmed: bool = False,
        request_id: str = "",
    ) -> dict:
        if confirmed is not True:
            raise ValueError("Подтвердите списание обрезков")
        grams = num(grams)
        if grams <= 0:
            raise ValueError("Укажите граммы обрезков")
        spool = self.repo.spool(spool_id) if self.repo else None
        if not spool:
            raise ValueError("Катушка не найдена")
        rid = (request_id or "").strip()[:120] or uid("scrq")
        existing = self.db.one("SELECT * FROM filament_scrap WHERE request_id=?", (rid,))
        if existing:
            return {"ok": True, "scrap": existing, "already": True}
        left = max(0.0, num(spool.get("remaining_grams")) - grams)
        self.db.execute(
            "UPDATE spools SET remaining_grams=?, updated_at=? WHERE id=?",
            (round(left, 1), now_iso(), spool_id),
        )
        row = self.db.upsert("filament_scrap", {
            "id": uid("scr"),
            "at": now_iso(),
            "spool_id": spool_id,
            "grams": round(grams, 1),
            "reason": (reason or "обрезки AMS")[:80],
            "note": (note or "")[:300],
            "request_id": rid,
        })
        if self.acc:
            try:
                self.acc.add_transaction(
                    "expense", "filament", 0,
                    f"Обрезки AMS {spool.get('material')} {spool.get('color_name')}",
                    f"{grams} г · {reason or ''}",
                    auto=True)
            except Exception:
                pass
        self.db.add_event(
            "spool", "Обрезки AMS",
            f"{spool.get('material')} {spool.get('color_name')} · −{grams} г",
            spool.get("printer_id") or "",
            {"spool_id": spool_id, "grams": grams},
        )
        return {"ok": True, "scrap": row, "spool": self.repo.spool(spool_id), "already": False}

    def scrap_list(self, spool_id: str = "", limit: int = 80) -> list[dict]:
        if spool_id:
            return self.db.query(
                "SELECT * FROM filament_scrap WHERE spool_id=? ORDER BY datetime(at) DESC LIMIT ?",
                (spool_id, int(limit)),
            )
        return self.db.query(
            "SELECT * FROM filament_scrap ORDER BY datetime(at) DESC LIMIT ?",
            (int(limit),),
        )

    # ---------------------------------------------------------- suppliers 156
    def suppliers(self) -> list[dict]:
        return self.db.query("SELECT * FROM suppliers WHERE archived=0 ORDER BY name")

    def save_supplier(self, body: dict) -> dict:
        data = dict(body or {})
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("Укажите название поставщика")
        if not data.get("id"):
            data["id"] = uid("sup")
            data.setdefault("created_at", now_iso())
        data["name"] = name[:120]
        data["url"] = str(data.get("url") or "")[:240]
        data["note"] = str(data.get("note") or "")[:400]
        data["price_per_kg"] = num(data.get("price_per_kg"))
        data["archived"] = int(bool(data.get("archived")))
        data["updated_at"] = now_iso()
        return self.db.upsert("suppliers", data)

    def delete_supplier(self, sid: str) -> None:
        self.db.execute("UPDATE suppliers SET archived=1, updated_at=? WHERE id=?",
                        (now_iso(), sid))

    def apply_supplier_price(self, supplier_id: str, material: str = "") -> dict:
        sup = self.db.one("SELECT * FROM suppliers WHERE id=?", (supplier_id,))
        if not sup:
            raise ValueError("Поставщик не найден")
        price = num(sup.get("price_per_kg"))
        if price <= 0:
            raise ValueError("У поставщика нет цены ₽/кг")
        sql = "UPDATE spools SET price_per_kg=?, supplier_id=?, updated_at=? WHERE remaining_grams>0"
        params: list = [price, supplier_id, now_iso()]
        if material:
            sql += " AND upper(material)=?"
            params.append(material.upper())
        self.db.execute(sql, params)
        return {"ok": True, "price_per_kg": price, "supplier": sup}

    # ----------------------------------------------------- workshop docs 146
    def next_doc_number(self) -> str:
        row = self.db.one("SELECT COUNT(*) n FROM workshop_docs") or {}
        return f"Ф-{int(num(row.get('n'))) + 1:04d}"

    def filament_receipt(
        self,
        *,
        items: list[dict] | None = None,
        material: str = "",
        color_name: str = "",
        color_hex: str = "",
        brand: str = "",
        spool_count=1,
        spool_grams=1000,
        total_amount=0,
        price_per_kg=0,
        supplier: str = "",
        supplier_id: str = "",
        shopping_id: str = "",
        account_id: str = "",
        note: str = "",
        confirmed: bool = False,
        request_id: str = "",
    ) -> dict:
        """Приход пластика — документ workshop_docs, не номенклатура 3.0."""
        if confirmed is not True:
            raise ValueError("Подтвердите приход пластика")
        rid = (request_id or "").strip()[:120] or uid("frq")
        existing = self.db.one("SELECT * FROM workshop_docs WHERE request_id=?", (rid,))
        if existing:
            return {"ok": True, "document": existing, "already": True}
        rows = list(items or [])
        if not rows:
            rows = [{
                "material": material, "color_name": color_name, "color_hex": color_hex,
                "brand": brand, "spool_count": spool_count, "spool_grams": spool_grams,
                "total_amount": total_amount, "price_per_kg": price_per_kg,
            }]
        created_spools = []
        payload = []
        total = 0.0
        grams_all = 0.0
        for raw in rows:
            sc = max(1, int(num(raw.get("spool_count"), 1)))
            sg = num(raw.get("spool_grams"), 1000) or 1000
            amount = num(raw.get("total_amount"))
            ppk = num(raw.get("price_per_kg"))
            if ppk <= 0 and amount > 0:
                kg = (sc * sg) / 1000.0
                ppk = round(amount / kg, 2) if kg else 0
            if amount <= 0 and ppk > 0:
                amount = round(ppk * (sc * sg) / 1000.0, 2)
            total += amount
            grams_all += sc * sg
            payload.append({**raw, "spool_count": sc, "spool_grams": sg,
                            "total_amount": amount, "price_per_kg": ppk})
            if self.repo:
                for _ in range(sc):
                    spool = self.repo.save_spool({
                        "material": str(raw.get("material") or material or "PLA"),
                        "color_name": str(raw.get("color_name") or color_name or ""),
                        "color_hex": str(raw.get("color_hex") or color_hex or "#888888"),
                        "brand": str(raw.get("brand") or brand or ""),
                        "remaining_grams": sg,
                        "spool_weight": sg,
                        "price": round(amount / sc, 2) if sc else amount,
                        "price_per_kg": ppk,
                        "supplier_id": supplier_id or "",
                        "location": "shop",
                    })
                    created_spools.append(spool)
        doc = self.db.upsert("workshop_docs", {
            "id": uid("wd"),
            "number": self.next_doc_number(),
            "kind": "filament_receipt",
            "at": now_iso(),
            "state": "posted",
            "title": f"Приход пластика {material or (rows[0].get('material') if rows else '')}".strip(),
            "payload": _json({
                "items": payload, "supplier": supplier, "supplier_id": supplier_id,
                "shopping_id": shopping_id, "account_id": account_id, "note": note,
                "spool_ids": [s["id"] for s in created_spools],
            }),
            "total_amount": round(total, 2),
            "grams": round(grams_all, 1),
            "supplier": supplier,
            "supplier_id": supplier_id or "",
            "shopping_id": shopping_id or "",
            "request_id": rid,
            "note": note[:400],
            "created_at": now_iso(),
        })
        if created_spools:
            ids = [s["id"] for s in created_spools]
            marks = ",".join("?" * len(ids))
            self.db.execute(
                f"UPDATE spools SET received_doc_id=? WHERE id IN ({marks})",
                [doc["id"], *ids],
            )
        if shopping_id:
            have = {r["name"] for r in self.db.query("PRAGMA table_info(shopping_items)")}
            sets = ["receipt_doc_id=?"]
            params: list = [doc["id"]]
            if "price_per_kg" in have and payload:
                sets.append("price_per_kg=?")
                params.append(payload[0].get("price_per_kg") or 0)
            if "supplier_id" in have and supplier_id:
                sets.append("supplier_id=?")
                params.append(supplier_id)
            params.append(shopping_id)
            self.db.execute(
                f"UPDATE shopping_items SET {', '.join(sets)} WHERE id=?",
                params,
            )
        if self.acc and total > 0:
            try:
                self.acc.add_transaction(
                    "expense", "filament", total,
                    f"Приход пластика {doc['number']}",
                    note or supplier, account_id=account_id, auto=True)
            except Exception:
                pass
        self.db.add_event("stock", "Приход пластика",
                          f"{doc['number']} · {round(grams_all)} г · {round(total, 2)} ₽",
                          "", {"doc_id": doc["id"]})
        return {"ok": True, "document": doc, "spools": created_spools, "already": False}

    def workshop_docs(self, kind: str = "", limit: int = 80) -> list[dict]:
        if kind:
            return self.db.query(
                "SELECT * FROM workshop_docs WHERE kind=? ORDER BY datetime(at) DESC LIMIT ?",
                (kind, int(limit)),
            )
        return self.db.query(
            "SELECT * FROM workshop_docs ORDER BY datetime(at) DESC LIMIT ?",
            (int(limit),),
        )

    def workshop_doc(self, doc_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM workshop_docs WHERE id=?", (doc_id,))
        if not row:
            return None
        row = dict(row)
        row["payload"] = _loads(row.get("payload"), {})
        return row

    # ------------------------------------------------------- plate presets 162
    def plate_presets(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM plate_presets ORDER BY name")
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = _loads(r.get("payload"), {})
            out.append(item)
        return out

    def save_plate_preset(self, body: dict) -> dict:
        data = dict(body or {})
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("Укажите название пресета плиты")
        if not data.get("id"):
            data["id"] = uid("pp")
            data.setdefault("created_at", now_iso())
        payload = data.get("payload")
        if not isinstance(payload, (dict, list, str)):
            payload = {
                "use_ams": data.get("use_ams", True),
                "bed_level": data.get("bed_level", True),
                "flow_cali": data.get("flow_cali", False),
                "timelapse": data.get("timelapse", False),
                "ams_mapping": data.get("ams_mapping") or [],
                "plate": data.get("plate") or 1,
            }
        data["name"] = name[:80]
        data["payload"] = payload if isinstance(payload, str) else _json(payload)
        data["updated_at"] = now_iso()
        row = self.db.upsert("plate_presets", data)
        row["payload"] = _loads(row.get("payload"), {})
        return row

    def delete_plate_preset(self, pid: str) -> None:
        self.db.delete("plate_presets", pid)

    def apply_plate_preset(self, job_id: str, preset_id: str) -> dict:
        preset = self.db.one("SELECT * FROM plate_presets WHERE id=?", (preset_id,))
        if not preset:
            raise ValueError("Пресет не найден")
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("Задание не найдено")
        payload = _loads(preset.get("payload"), {})
        patch = {"id": job_id, "plate_preset_id": preset_id}
        if "use_ams" in payload:
            patch["use_ams"] = 1 if payload.get("use_ams") else 0
        if "bed_level" in payload:
            patch["bed_level"] = 1 if payload.get("bed_level") else 0
        if "flow_cali" in payload:
            patch["flow_cali"] = 1 if payload.get("flow_cali") else 0
        if "timelapse" in payload:
            patch["timelapse"] = 1 if payload.get("timelapse") else 0
        if payload.get("ams_mapping") is not None:
            mapping = payload.get("ams_mapping")
            patch["ams_mapping"] = mapping if isinstance(mapping, str) else _json(mapping)
        if payload.get("plate"):
            patch["plate"] = int(num(payload.get("plate"), 1) or 1)
        self.db.upsert("print_jobs", patch)
        return {"ok": True, "job": self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,)),
                "preset": {**preset, "payload": payload}}

    # ---------------------------------------------------------- mixed plate 139
    def mixed_plate_label(self, items: list[dict] | None = None, plates: int = 1) -> str:
        rows = [x for x in (items or []) if isinstance(x, dict)]
        if not rows:
            return ""
        bits = []
        for row in rows:
            name = str(row.get("name") or row.get("product") or row.get("nom") or "").strip()
            qty = int(num(row.get("qty"), 1) or 1)
            if name:
                bits.append(f"{name} ×{qty}")
        if not bits:
            return ""
        text = " + ".join(bits)
        if int(plates or 1) > 1:
            text += f" · {int(plates)} плит"
        return text[:180]

    def attach_mixed_label(self, job_id: str, items: list[dict] | None = None, plates: int = 1,
                           label: str = "") -> dict:
        text = (label or self.mixed_plate_label(items, plates)).strip()
        if not text:
            raise ValueError("Нет состава смешанной плиты")
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("Задание не найдено")
        self.db.execute(
            "UPDATE print_jobs SET mixed_label=? WHERE id=?",
            (text, job_id),
        )
        return {"ok": True, "label": text, "job_id": job_id}

    # --------------------------------------------------------------- QR 142
    def qr_wizard(self, spool_id: str = "", kind: str = "spool") -> dict:
        """Мастер QR: ссылка + текст наклейки + payload для печати."""
        from urllib.parse import quote
        kind = (kind or "spool").strip().lower()
        if kind == "spool":
            spool = self.repo.spool(spool_id) if self.repo and spool_id else None
            if not spool:
                raise ValueError("Катушка не найдена")
            payload = f"pf:spool:{spool['id']}"
            title = f"{spool.get('material') or 'PLA'} {spool.get('color_name') or ''}".strip()
            lines = [
                title,
                (spool.get("brand") or "").strip(),
                f"{round(num(spool.get('remaining_grams')))} г",
                f"слот {spool.get('ams_slot')}" if spool.get("ams_slot") else "",
                "магазин" if (spool.get("location") or "shop") == "shop" else "дом",
            ]
            lines = [x for x in lines if x]
            self.db.execute(
                "UPDATE spools SET qr_payload=?, updated_at=? WHERE id=?",
                (payload, now_iso(), spool["id"]),
            )
            return {
                "ok": True,
                "kind": "spool",
                "id": spool["id"],
                "payload": payload,
                "path": "/spool.html",
                "query": f"id={quote(spool['id'], safe='')}",
                "title": title,
                "lines": lines,
                "color_hex": spool.get("color_hex") or "#333333",
                "spool": {
                    "id": spool["id"],
                    "material": spool.get("material"),
                    "color_name": spool.get("color_name"),
                    "brand": spool.get("brand") or "",
                    "remaining_grams": spool.get("remaining_grams"),
                    "ams_slot": spool.get("ams_slot"),
                    "location": spool.get("location") or "shop",
                },
            }
        raise ValueError("Этот мастер печатает QR катушки")

    def spool_label_html(self, spool_id: str) -> str:
        info = self.qr_wizard(spool_id, "spool")
        from .qrgen import svg as qr_svg
        try:
            mark = qr_svg(info["payload"], scale=4)
        except Exception:
            mark = ""
        hex_color = info.get("color_hex") or "#333"
        lines = "".join(f"<div class='ln'>{_esc(x)}</div>" for x in info["lines"])
        return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Наклейка катушки</title>
<style>
@page {{ size: 62mm 40mm; margin: 2mm; }}
body {{ font-family: Arial, sans-serif; margin: 0; color: #111; }}
.card {{ width: 58mm; height: 36mm; border: 0.4mm solid #111; border-radius: 2mm;
         display: flex; padding: 1.5mm; box-sizing: border-box; gap: 2mm; }}
.qr {{ width: 22mm; height: 22mm; }}
.qr svg {{ width: 100%; height: 100%; }}
.meta {{ flex: 1; font-size: 8pt; line-height: 1.25; }}
.sw {{ width: 6mm; height: 6mm; border-radius: 50%; border: 0.3mm solid #000;
       background: {hex_color}; margin-bottom: 1mm; }}
.ln {{ margin: 0 0 0.6mm; }}
h1 {{ font-size: 9pt; margin: 0 0 1mm; }}
@media print {{ button {{ display: none; }} }}
</style></head><body>
<button onclick="window.print()">Печать</button>
<div class="card">
  <div class="qr">{mark}</div>
  <div class="meta"><div class="sw"></div><h1>{_esc(info['title'])}</h1>{lines}</div>
</div></body></html>"""

    # ------------------------------------------------------- shift checklist
    def shift_state(self, day: str = "") -> dict:
        day = day or now_iso()[:10]
        items = _loads(self.db.setting("shift_checklist", "[]"), [])
        if not isinstance(items, list) or not items:
            items = [
                {"id": "table", "title": "Стол чистый"},
                {"id": "ams", "title": "AMS совпадает с катушками"},
                {"id": "dry", "title": "Влажный пластик в сушке"},
                {"id": "queue", "title": "Очередь на сегодня понятна"},
                {"id": "backup", "title": "Вчерашняя копия базы на месте"},
            ]
        rows = self.db.query("SELECT * FROM shift_checks WHERE day=?", (day,))
        done = {r["item_id"]: r for r in rows}
        out = []
        for it in items:
            iid = str(it.get("id") or "")
            rec = done.get(iid)
            out.append({
                "id": iid,
                "title": it.get("title") or iid,
                "done": bool(rec),
                "at": (rec or {}).get("at") or "",
                "note": (rec or {}).get("note") or "",
            })
        return {"day": day, "items": out, "done": sum(1 for x in out if x["done"]),
                "total": len(out)}

    def check_shift(self, item_id: str, done: bool = True, note: str = "", day: str = "") -> dict:
        day = day or now_iso()[:10]
        item_id = str(item_id or "").strip()
        if not item_id:
            raise ValueError("Нет пункта чек-листа")
        existing = self.db.one(
            "SELECT * FROM shift_checks WHERE day=? AND item_id=?", (day, item_id))
        if done:
            if existing:
                self.db.execute(
                    "UPDATE shift_checks SET at=?, note=? WHERE id=?",
                    (now_iso(), (note or "")[:200], existing["id"]),
                )
            else:
                self.db.upsert("shift_checks", {
                    "id": uid("shc"), "day": day, "item_id": item_id,
                    "at": now_iso(), "note": (note or "")[:200],
                })
        elif existing:
            self.db.delete("shift_checks", existing["id"])
        return self.shift_state(day)

    # --------------------------------------------------------------- about
    def about(self) -> dict:
        from . import APP_VERSION
        from .db import SCHEMA_VERSION
        printers = 0
        try:
            printers = len(self.repo.printers()) if self.repo else 0
        except Exception:
            printers = 0
        return {
            "app": "PrintFlow",
            "version": APP_VERSION,
            "schema": SCHEMA_VERSION,
            "printers": printers,
            "rules": [
                "Остаток только по документам — второго склада нет.",
                "Печать только после подтверждения и preflight.",
                "Логи и таймлапс с SD не печатаются.",
                "Один слот AMS — одна катушка.",
            ],
        }

    # ---------------------------------------------------------- job helpers
    def clone_job(self, job_id: str) -> dict:
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("Задание не найдено")
        skip = {"id", "state", "started_at", "finished_at", "progress",
                "remote_task_id", "start_request_id"}
        data = {k: v for k, v in dict(job).items() if k not in skip}
        data["id"] = uid("job")
        data["state"] = "queued"
        data["created_at"] = now_iso()
        data["name"] = (str(job.get("name") or "печать") + " (копия)")[:180]
        data["allow_auto_start"] = 0
        row = self.db.upsert("print_jobs", data)
        return {"ok": True, "job": row}

    def set_no_auto(self, job_id: str, no_auto: bool = True) -> dict:
        job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))
        if not job:
            raise ValueError("Задание не найдено")
        cols = self.db.columns("print_jobs")
        sets = ["no_auto=?"]
        params: list = [1 if no_auto else 0]
        if "allow_auto_start" in cols:
            sets.append("allow_auto_start=?")
            params.append(0 if no_auto else 1)
        if "updated_at" in cols:
            sets.append("updated_at=?")
            params.append(now_iso())
        params.append(job_id)
        self.db.execute(
            f"UPDATE print_jobs SET {', '.join(sets)} WHERE id=?",
            params,
        )
        return {"ok": True, "job": self.db.one("SELECT * FROM print_jobs WHERE id=?", (job_id,))}

    def claim_start_request(self, request_id: str, job_id: str = "") -> dict | None:
        rid = (request_id or "").strip()[:120]
        if not rid:
            return None
        existing = self.db.one(
            "SELECT * FROM print_jobs WHERE start_request_id=?", (rid,))
        if existing:
            return existing
        if job_id:
            self.db.execute(
                "UPDATE print_jobs SET start_request_id=? WHERE id=?",
                (rid, job_id),
            )
        return None


def _esc(value: Any) -> str:
    return (str(value or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def heartbeat_channels(manager, db) -> dict:
    """MQTT / FTPS / диск для офлайн-полосы (идея 191)."""
    mqtt = {"ok": True, "printers": []}
    ftps = {"ok": True, "printers": []}
    printers = getattr(manager, "printers", {}) or {}
    for p in printers.values():
        rec = getattr(p, "record", {}) or {}
        name = rec.get("name") or getattr(p, "id", "")
        snap = {}
        try:
            snap = p.snapshot() if hasattr(p, "snapshot") else {}
        except Exception:
            snap = {}
        conn = snap.get("connection") or {}
        mqtt_ok = bool(getattr(p, "connected", False) or conn.get("connected")
                       or getattr(p, "mode", "") == "virtual")
        last_err = str(conn.get("last_error") or "")
        mqtt["printers"].append({
            "id": getattr(p, "id", ""), "name": name, "ok": mqtt_ok,
            "error": "" if mqtt_ok else last_err or "MQTT молчит",
        })
        host = rec.get("host") or ""
        code = rec.get("access_code") or ""
        ftps_ok = True
        ftps_err = ""
        if getattr(p, "mode", "") == "virtual":
            ftps_ok = True
        elif not host or not code:
            ftps_ok = False
            ftps_err = "Нет IP или Access Code"
        else:
            files = getattr(p, "files", None)
            last = getattr(files, "last_ok", None)
            last_error = getattr(files, "last_error", "") or ""
            if last_error and last is False:
                ftps_ok = False
                ftps_err = str(last_error)[:160]
            elif last is False:
                ftps_ok = False
                ftps_err = "FTPS не ответил"
        ftps["printers"].append({
            "id": getattr(p, "id", ""), "name": name, "ok": ftps_ok, "error": ftps_err,
        })
    mqtt["ok"] = all(x["ok"] for x in mqtt["printers"]) if mqtt["printers"] else True
    ftps["ok"] = all(x["ok"] for x in ftps["printers"]) if ftps["printers"] else True
    disk = {"ok": True, "free_gb": None, "used_pct": None, "error": ""}
    try:
        import shutil
        from .config import DB_FILE
        target = DB_FILE if getattr(DB_FILE, "exists", lambda: False)() else getattr(DB_FILE, "parent", None)
        if target is None or not getattr(target, "exists", lambda: False)():
            target = "."
        usage = shutil.disk_usage(target)
        free_gb = usage.free / 1024 ** 3
        used_pct = (usage.used / usage.total * 100) if usage.total else 0
        disk = {
            "ok": free_gb >= 0.5,
            "free_gb": round(free_gb, 1),
            "used_pct": round(used_pct, 1),
            "error": "" if free_gb >= 0.5 else "Мало места на диске",
        }
    except Exception as exc:
        disk = {"ok": False, "free_gb": None, "used_pct": None, "error": str(exc)[:160]}
    return {"mqtt": mqtt, "ftps": ftps, "disk": disk}
