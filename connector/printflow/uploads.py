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
from pathlib import Path

from .accounting import num
from .config import DATA_DIR, UPLOAD_DIR, now_iso
from .http_helpers import (MAX_UPLOAD, _form_bool, _upload_filename,
                           parse_multipart, request_length, safe_file,
                           save_upload)


def _form_ams_mapping(raw: str) -> list[int]:
    """Раскладка AMS из поля формы: «0,1», «[0, 1]» или мусор → пусто.

    Позиция в списке — материал файла, значение — номер слота AMS (с нуля).
    -1 в PrintFlow значит «слот не назначен» — так его отдаёт `auto_ams_map`,
    поэтому -1 остаётся в списке: по нему видно, какому материалу слота не
    нашлось. Разбор терпимый: поле необязательное, мусор не должен ломать
    загрузку файла.
    """
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = [part for part in text.replace(";", ",").split(",")]
    if not isinstance(parsed, list):
        parsed = [parsed]
    out: list[int] = []
    for value in parsed:
        try:
            slot = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if -1 <= slot <= 15:
            out.append(slot)
    return out


def _farmloop_template(profile: str) -> Path:
    """Проверенный шаблон ищется только в data/farmloop-templates.

    Никогда не принимаем произвольный путь из multipart: шаблон управляет
    физическим движением P1S и должен быть установлен владельцем отдельно.
    """
    safe = "".join(ch for ch in str(profile or "") if ch.isalnum() or ch in "-_" )
    if safe != str(profile or "") or not safe:
        raise ValueError("Некорректный профиль FarmLoop")
    path = DATA_DIR / "farmloop-templates" / f"{safe}.gcode"
    if not path.is_file():
        raise ValueError(
            f"Профиль FarmLoop «{safe}» ещё не установлен. "
            "Сначала проверьте механику и положите подтверждённый шаблон в "
            "data/farmloop-templates.")
    return path


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
        farmloop_profile = str(fields.get("farmloop_profile") or "").strip()
        farmloop_report = None
        if farmloop_profile:
            if not name.lower().endswith(".gcode"):
                if created:
                    local.unlink(missing_ok=True)
                return self.send_json(400, {
                    "error": "FarmLoop Stage 1 принимает экспортированный .gcode из Bambu Studio; "
                             ".3mf пока нужно экспортировать в G-code",
                })
            try:
                from .farmloop import prepare_file
                template = _farmloop_template(farmloop_profile)
                prepared_name = f"{Path(name).stem}.farmloop.gcode"
                prepared = UPLOAD_DIR / prepared_name
                farmloop_report = prepare_file(
                    local, prepared, template.read_text(encoding="utf-8", errors="replace"),
                    metadata={
                        "job_id": fields.get("job_id") or "",
                        "cycle": fields.get("cycle") or "",
                        "cycles": fields.get("cycles") or "",
                        "ams": fields.get("ams") or fields.get("ams_mapping") or "",
                        "material": fields.get("material") or "",
                        "color": fields.get("color") or "",
                    },
                )
                # 18.8 (вкладка «Конвейер»): подготовка файла для серии —
                # событие журнала, иначе у истории конвейера нечего показать.
                try:
                    self.api.db.add_event(
                        "farmloop", "Файл подготовлен для конвейера",
                        prepared_name, "",
                        {"profile": farmloop_profile,
                         "source_file": name})
                except Exception:
                    pass
                if created:
                    local.unlink(missing_ok=True)
                name, local = prepared_name, prepared
            except (ValueError, OSError) as exc:
                if created:
                    local.unlink(missing_ok=True)
                return self.send_json(409, {"error": str(exc)})
        # Раскладка AMS: пульт отдаёт файл и раскладку одним запросом, как
        # слайсер. Пустое поле — «пусть решит принтер»: поведение прежнее.
        mapping = _form_ams_mapping(fields.get("ams_mapping"))
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
            "farmloop_profile": farmloop_profile,
            "farmloop_report": farmloop_report or {},
        }
        if mapping:
            payload["ams_mapping"] = mapping
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
                                    "job": job, "source": "upload",
                                    "farmloop": farmloop_report or None})

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

    def handle_library_upload(self):
        """Сохранить модель (STL/OBJ) в библиотеку — вход для карточки слайсера.

        Карточка «Свой слайсер» (18.8) нарезает модель своим движком, а
        движок Stage 1 принимает только STL. До этого маршрута у панели не
        было способа привести в библиотеку модель с компьютера: 3MF/G-code
        принимают `/api/jobs/upload` и `/api/estimate/upload`, а на них —
        «Поддерживаются только 3MF и G-code». Файл попадает в SHA-хранилище
        библиотеки и в копия в uploads — дальше её резает `/api/slicer/plan`
        и `/api/slicer/slice` по `id`, как и любые другие файлы библиотеки.
        """
        fields, upload = self._multipart_upload()
        if not upload:
            return self.send_json(400, {"error": "Файл не передан"})
        if not upload[1]:
            return self.send_json(400, {"error": "Файл пустой"})
        try:
            requested_name = _upload_filename(upload[0])
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        if not requested_name.lower().endswith((".stl", ".obj")):
            return self.send_json(400, {
                "error": "В библиотеку для слайсера принимается модель STL или OBJ; "
                         "G-code и 3MF — через загрузку в очередь",
            })
        from .library import FileLibrary
        source = str(fields.get("source") or "").strip() or "upload"
        note = str(fields.get("note") or "").strip()
        record = FileLibrary(self.api.db).put(requested_name, upload[1],
                                              source=source, note=note)
        return self.send_json(200, {
            "ok": True, "file": record["name"], "saved": record["upload_name"],
            "source": source, "library": record,
        })

    def handle_intake_upload(self):
        """Сообщение клиента вместе с его файлом — в один черновик заказа.

        Зачем отдельный маршрут. Текст входящего заказа разбирает
        `/api/order/intake/preview`, а файлы принимают `/api/jobs/upload` и
        `/api/estimate/upload` — и ни один из них не умеет оба сразу. Клиент
        присылает сообщение с моделью в мессенджере, и мастер переносит файл
        руками: сначала в очередь или оценку, потом в карточку заказа. Здесь
        файл и текст проходят один путь и дают один черновик.

        Маршрут байтовый, поэтому живёт в цепочке `do_POST` до реестра, как
        остальные загрузки. Он ничего не сохраняет: заказ появляется только
        после того, как человек нажал «Сохранить» в карточке.

        Порядок заполнения намеренный и совпадает с `OrderIntake.preview`:
        сначала детерминированный разбор текста, потом оценка файла дополняет
        пустые поля, и только потом — если помощник включён — модель
        предлагает значения для того, что не разобрали первые два слоя.
        """
        fields, upload = self._multipart_upload()
        text = " ".join(str(fields.get("text") or "").split())
        channel = str(fields.get("channel") or "").strip() or "direct"
        if not text and not upload:
            return self.send_json(400, {"error": "Вставьте сообщение или приложите файл"})

        from .order_intake import OrderIntake
        intake = OrderIntake(self.api.db)
        parsed: dict = {}
        try:
            preview = intake.preview(text, channel) if text else None
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        if preview is None:
            # Файл без сообщения: черновик собирается так же, как это делает
            # Watch Folder для 3MF, — изделие по имени файла, всё остальное
            # остаётся человеку.
            preview = intake.preview(f"{channel} файл", channel)
            preview["draft"].update({
                "product": "Изделие из файла",
                "notes": f"Входящий файл ({channel}): сообщение не приложено",
            })
            preview["parsed"] = {}
        draft = dict(preview.get("draft") or {})
        parsed = dict(preview.get("parsed") or {})
        warnings = list(preview.get("warnings") or [])

        file_info: dict = {}
        estimate: dict = {}
        if upload:
            if not upload[1]:
                return self.send_json(400, {"error": "Файл пустой"})
            try:
                requested_name = _upload_filename(upload[0])
            except ValueError as exc:
                return self.send_json(400, {"error": str(exc)})
            if not requested_name.lower().endswith((".3mf", ".gcode", ".stl", ".obj")):
                return self.send_json(400, {
                    "error": "Из сообщения принимаются 3MF, G-code, STL и OBJ; "
                             "фото клиента — в карточку заказа",
                })
            name, local, _created = save_upload(requested_name, upload[1])
            try:
                from .estimate import estimate_file
                estimate = estimate_file(local) or {}
            except Exception:
                estimate = {}
            draft["file"] = name
            grams = num(estimate.get("total_grams")) or num(estimate.get("grams"))
            minutes = num(estimate.get("total_minutes")) or num(estimate.get("minutes"))
            # Файл дополняет только пустые поля: значение, разобранное из
            # сообщения или взятое из карточки товара, он не перетирает.
            if grams and not num(draft.get("grams")):
                draft["grams"] = grams
            if minutes and not num(draft.get("hours")):
                draft["hours"] = round(minutes / 60.0, 2)
            for key in ("material", "color"):
                value = str(estimate.get(key) or "").strip()
                if value and not str(draft.get(key) or "").strip():
                    draft[key] = value
            file_info = {"name": name, "size": len(upload[1]),
                         "extension": Path(name).suffix.lower().lstrip("."),
                         "estimate": bool(estimate)}
            warnings.append("Файл из сообщения подставлен в черновик — проверьте его до сохранения")

        assistant: dict = {"ok": False, "available": False, "suggestions": [], "reply": ""}
        try:
            from . import assistant as assistant_service
            if assistant_service.config(self.api.db)["enabled"]:
                assistant = assistant_service.suggest(
                    self.api.db, draft, text or str(draft.get("notes") or ""), channel)
                if assistant.get("ok"):
                    draft = dict(assistant.get("draft") or draft)
                    warnings.extend(assistant.get("warnings") or [])
        except Exception as exc:  # помощник не имеет права ронять разбор заказа
            from .logging_setup import log
            log().warning("Помощник не отработал на входящем заказе: %s", exc)
            assistant = {"ok": False, "available": False, "suggestions": [], "reply": "",
                         "reason": "Помощник не отработал — черновик собран без него"}

        return self.send_json(200, {
            "ok": True, "draft": draft, "parsed": parsed,
            "confidence": preview.get("confidence", 0),
            "warnings": warnings, "matches": preview.get("matches") or {},
            "file": file_info or None, "estimate": estimate,
            "assistant": assistant,
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



    def handle_speech_upload(self):
        """Запись голоса → текст (срез 2).

        Байтовый маршрут, поэтому в цепочке `do_POST` до реестра. Звук не
        сохраняется на диске и не пишется в базу: он передаётся внешнему
        рантайму речи на этом же компьютере и забывается. Расшифрованный текст
        ничего не выполняет — панель подставляет его в поле фразы, а действие
        человек подтверждает как обычно.
        """
        fields, upload = self._multipart_upload()
        if not upload or not upload[1]:
            return self.send_json(400, {"error": "Запись не передана"})
        from . import assistant as service
        result = service.transcribe(self.api.db, upload[1],
                                    str(fields.get("language") or "ru"))
        if not result.get("ok"):
            return self.send_json(200, result)
        return self.send_json(200, result)
