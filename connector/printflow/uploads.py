"""Приём и отдача загружаемых файлов (Н18).

Вынесено из `api.py` в примесь: загрузка 3MF/G-code, оценка стоимости и
универсальный `/api/upload` — это транспорт, а не логика маршрутов. Класс
`Handler` подключает примесь и ничего о ней не знает, кроме имён методов.

`self` здесь — HTTP-обработчик: примесь ожидает `send_json`, `_send_bytes`,
`api` (контекст) и стандартные `headers`/`rfile`.
"""
from __future__ import annotations

import json
import mimetypes
import time
import urllib.parse
from pathlib import Path

from .accounting import num, uid
from .config import UPLOAD_DIR, now_iso
from .http_helpers import (MAX_UPLOAD, _form_bool, _upload_filename,
                           parse_multipart, request_length, safe_file,
                           save_upload)


class UploadMixin:
    """Методы приёма файлов. Подмешивается в HTTP-обработчик."""

    def serve_upload(self, name: str):
        """Скачать сохранённый 3MF/G-code из папки uploads."""
        safe_name = Path(name or "").name
        target = safe_file(UPLOAD_DIR, safe_name) if safe_name else None
        if not target or not target.is_file():
            return self.send_json(404, {"error": "Файл не найден в uploads"})
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send_bytes(target.read_bytes(), ctype, download=target.name)

    def _multipart_upload(self) -> tuple[dict[str, str], tuple[str, bytes] | None]:
        """Прочитать один multipart-файл с общей проверкой размера и boundary."""
        length, too_large = request_length(self.headers.get("Content-Length"), MAX_UPLOAD)
        if too_large:
            raise ValueError("Файл слишком большой")
        content_type = self.headers.get("Content-Type", "")
        boundary = ""
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
        if not boundary:
            raise ValueError("Ожидается multipart/form-data")
        return parse_multipart(self.rfile.read(length), boundary)

    def handle_job_upload(self):
        """Сохранить локальный файл и сразу поставить его в очередь.

        Очередь не должна требовать, чтобы модель сначала лежала на SD-карте:
        при ручном старте менеджер сам загрузит локальную копию на выбранный
        принтер (или передаст её Bambu Cloud)."""
        fields, upload = self._multipart_upload()
        if not upload:
            return self.send_json(400, {"error": "Файл не передан"})
        if not upload[1]:
            return self.send_json(400, {"error": "Файл пустой"})
        try:
            requested_name = _upload_filename(upload[0])
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        if not requested_name.lower().endswith((".3mf", ".gcode", ".gcode.3mf")):
            return self.send_json(400, {"error": "Поддерживаются только 3MF и G-code"})
        name, local, created = save_upload(requested_name, upload[1])
        payload = {
            "name": str(fields.get("name") or Path(name).stem).strip() or Path(name).stem,
            "file": name,
            "printer_id": str(fields.get("printer_id") or "").strip(),
            "order_id": str(fields.get("order_id") or "").strip(),
            "plate": max(1, int(num(fields.get("plate"), 1) or 1)),
            "priority": int(num(fields.get("priority"), 0)),
            "use_ams": _form_bool(fields.get("use_ams"), True),
            "bed_level": _form_bool(fields.get("bed_level"), True),
            "flow_cali": _form_bool(fields.get("flow_cali"), False),
            "timelapse": _form_bool(fields.get("timelapse"), False),
            "source": "local-upload",
            "allow_auto_start": _form_bool(fields.get("allow_auto_start"), True),
        }
        try:
            job = self.api.manager.enqueue(payload)
        except Exception:
            # Если именно эта загрузка не стала заданием, не оставляем мусор.
            if created:
                try:
                    local.unlink()
                except OSError:
                    pass
            raise
        return self.send_json(200, {"ok": True, "file": name, "saved": name,
                                    "job": job, "source": "upload"})

    def handle_estimate_upload(self):
        """Сохранить выбранный 3MF/G-code в uploads и вернуть вес плиты."""
        length, too_large = request_length(self.headers.get("Content-Length"), MAX_UPLOAD)
        if too_large:
            return self.send_json(413, {"error": "Файл слишком большой"})
        content_type = self.headers.get("Content-Type", "")
        boundary = ""
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
        if not boundary:
            return self.send_json(400, {"error": "Ожидается multipart/form-data"})
        fields, upload = parse_multipart(self.rfile.read(length), boundary)
        if not upload:
            return self.send_json(400, {"error": "Файл не передан"})
        if not upload[1]:
            return self.send_json(400, {"error": "Файл пустой"})
        try:
            requested_name = _upload_filename(upload[0])
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        if not requested_name.lower().endswith((".3mf", ".gcode", ".gcode.3mf")):
            return self.send_json(400, {"error": "Поддерживаются только 3MF и G-code"})
        name, local, _created = save_upload(requested_name, upload[1])
        estimate = {}
        try:
            from .estimate import estimate_file
            estimate = estimate_file(local) or {}
        except Exception:
            estimate = {}
        grams = num(estimate.get("total_grams")) or num(estimate.get("grams"))
        minutes = num(estimate.get("total_minutes")) or num(estimate.get("minutes"))
        order_id = (fields or {}).get("order_id", "")
        if order_id:
            order = self.api.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if order:
                sets, params = ["file=?", "updated_at=?"], [name, now_iso()]
                if grams and not num(order.get("grams")):
                    sets.append("grams=?")
                    params.append(grams)
                if minutes and not num(order.get("hours")):
                    sets.append("hours=?")
                    params.append(round(minutes / 60.0, 2))
                params.append(order_id)
                self.api.db.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", params)
        return self.send_json(200, {
            "ok": True, "file": name, "saved": name, "source": "upload",
            "grams": grams, "minutes": minutes,
            "hours": round(minutes / 60.0, 2) if minutes else 0.0,
            "material": estimate.get("material") or "",
            "color": estimate.get("color") or "",
            "estimate": estimate,
        })

    def handle_upload(self, query: dict):
        """Приём файла модели и отправка его на принтер по FTPS."""
        length, too_large = request_length(self.headers.get("Content-Length"), MAX_UPLOAD)
        if too_large:
            return self.send_json(413, {"error": "Файл слишком большой"})
        content_type = self.headers.get("Content-Type", "")
        boundary = ""
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
        if not boundary:
            return self.send_json(400, {"error": "Ожидается multipart/form-data"})
        fields, upload = parse_multipart(self.rfile.read(length), boundary)
        if not upload:
            return self.send_json(400, {"error": "Файл не передан"})
        if not upload[1]:
            return self.send_json(400, {"error": "Файл пустой"})
        try:
            requested_name = _upload_filename(upload[0])
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        if not requested_name.lower().endswith((".3mf", ".gcode", ".gcode.3mf")):
            return self.send_json(400, {"error": "Поддерживаются только 3MF и G-code"})
        name, local, _created = save_upload(requested_name, upload[1])
        printer_id = fields.get("printer_id", "") or (query.get("printer_id") or [""])[0]
        printer = self.api.manager.get(printer_id)
        if not printer:
            return self.send_json(400, {"error": "Принтер не настроен"})
        # 8.0: FTPS прогресс через шину
        def _progress(sent, total=0):
            try:
                pct = round(sent / total * 100) if total else 0
                self.api.bus.publish("upload_progress", {"name": name, "sent": sent, "total": total, "percent": pct})
            except Exception:
                pass
        # Облачный принтер: файл уходит в облачное хранилище Bambu, принтер
        # скачает его сам при запуске (диспетчеризация через /my/task).
        # При недоступности облака и живом LAN — прежний FTPS-путь.
        if printer.mode == "cloud":
            from . import bambu_cloud
            token, uid, region = self.api._cloud_session()
            cloud_error = ""
            if token and uid:
                try:
                    manifest = bambu_cloud.upload_project(
                        local, token, uid, region, progress=self.api._cloud_progress(name))
                    manifest["at"] = now_iso()
                    self.api.manager.cloud_manifest_path(name).write_text(
                        json.dumps(manifest), encoding="utf-8")
                    result = {"ok": True, "cloud": True, "name": name,
                              "size": len(upload[1]), **manifest}
                    self.api.db.add_event("upload", "Файл загружен в Bambu Cloud",
                                          name, printer.id, {"bytes": len(upload[1])})
                    cloud_sent = True
                except bambu_cloud.CloudError as exc:
                    cloud_error = str(exc)
                    cloud_sent = False
            else:
                cloud_sent = False
            if not cloud_sent:
                # Прежде чем сдаться, пробуем дозаполнить IP/Access Code
                # облачного принтера — тогда FTPS-фолбэк сработает.
                if not (printer.record.get("host") and printer.record.get("access_code")):
                    try:
                        self.api._ensure_lan_access(printer)
                        printer = self.api.manager.get(printer_id) or printer
                    except Exception:
                        pass
                if printer.record.get("host") and printer.record.get("access_code"):
                    self.api.db.add_event("cloud", "Облачная заливка не удалась — FTPS",
                                          cloud_error or "нет входа в Bambu Cloud",
                                          printer.id, {"file": name})
                    try:
                        result = printer.files.upload(local, name, progress=_progress)
                    except TypeError:
                        result = printer.files.upload(local, name)
                    self.api.bus.publish("upload_progress",
                                         {"name": name, "sent": result.get("size", 0),
                                          "total": result.get("total", 0) or len(upload[1]),
                                          "percent": 100})
                    self.api.db.add_event("upload", "Файл загружен на принтер (FTPS)",
                                          name, printer.id, result)
                else:
                    return self.send_json(502, {
                        "error": ("Не удалось загрузить файл: облако Bambu недоступно, "
                                  "а локальная сеть принтера не настроена. "
                                  + (cloud_error or "Выполните вход в Bambu Cloud."))})
        else:
            try:
                result = printer.files.upload(local, name, progress=_progress)
            except TypeError:
                result = printer.files.upload(local, name)
            try:
                self.api.bus.publish("upload_progress", {"name": name, "sent": result.get("size",0), "total": result.get("total",0) or len(upload[1]), "percent": 100})
            except Exception:
                pass
            self.api.db.add_event("upload", "Файл загружен на принтер", name, printer.id, result)
        # оценка печати до запуска: время и граммы из 3MF/G-code
        estimate = {}
        try:
            from .estimate import estimate_file
            estimate = estimate_file(local)
        except Exception:
            estimate = {}
        order_id = fields.get("order_id", "")
        if order_id:
            # 3MF/G-code автозаполняет заказ: файл всегда, а вес/время/материал/
            # цвет — только если поле пустое (ручные правки не перетираем).
            sets, params = ["file=?", "updated_at=?"], [name, now_iso()]
            order = self.api.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if order:
                for field in ("grams", "hours", "material", "color"):
                    value = estimate.get(field)
                    if value and not (order.get(field) or "").strip():
                        sets.append(f"{field}=?")
                        params.append(value if field in ("material", "color")
                                      else num(value))
            params.append(order_id)
            self.api.db.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", params)
        return self.send_json(200, {"ok": True, "estimate": estimate, **result})


