"""HTTP-сервер PrintFlow: JSON-API и раздача сайта.

По умолчанию слушает только 127.0.0.1. Все изменяющие запросы проверяют
заголовок Origin, чтобы посторонняя страница не смогла управлять принтером.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import re
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_VERSION
from .accounting import Accounting, num, uid
from .bambu import BambuPrinter
from .bus import EventBus, LiveBroadcaster
from .config import (DANGEROUS_AUTOMATION_COMMANDS, SITE, UPLOAD_DIR,
                     ensure_dirs, now_iso)
from .db import Database, friendly_sqlite_error
from .manager import PrinterManager
from .repo import Repo

MAX_UPLOAD = 400 * 1024 * 1024  # 400 МБ — с запасом на крупные 3MF
MAX_JSON = 2 * 1024 * 1024      # JSON не содержит моделей и не должен занимать сотни МБ

# Браузер штатно закрывает долгие SSE/MJPEG-соединения при обновлении страницы,
# закрытии вкладки и переходе в сон. На разных ОС это проявляется разными
# подклассами ConnectionError (на Windows в том числе ConnectionAbortedError,
# WinError 10053), поэтому все эти варианты должны завершаться без traceback.
CLIENT_DISCONNECT_ERRORS = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)


def safe_file(root: Path, name: str) -> Path | None:
    """Вернуть путь только если он действительно находится внутри root."""
    base = root.resolve()
    target = (base / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def request_length(raw: str | None, limit: int) -> tuple[int, bool]:
    """Проверить Content-Length до чтения тела: ``(размер, превышен_ли)``."""
    try:
        length = int(raw or 0)
    except (TypeError, ValueError):
        raise ValueError("Некорректный Content-Length") from None
    if length < 0:
        raise ValueError("Некорректный Content-Length")
    return length, length > limit


def request_origin_allowed(origin: str | None, host: str | None) -> bool:
    """Разрешить API-клиент без Origin или браузер строго с того же Host.

    Разрешение любого адреса из частной подсети недостаточно: вредоносная
    страница на соседнем устройстве иначе могла бы управлять принтером.
    """
    if not origin:
        return True
    parsed = urllib.parse.urlparse(origin)
    return (parsed.scheme in ("http", "https") and bool(host)
            and parsed.netloc.casefold() == host.strip().casefold())


def parse_multipart(body: bytes, boundary: str) -> tuple[dict[str, str], tuple[str, bytes] | None]:
    """Минимальный разбор multipart/form-data: текстовые поля и один файл."""
    fields: dict[str, str] = {}
    upload: tuple[str, bytes] | None = None
    marker = b"--" + boundary.encode()
    for chunk in body.split(marker):
        if not chunk or chunk in (b"--\r\n", b"--", b"\r\n"):
            continue
        head, _, data = chunk.partition(b"\r\n\r\n")
        if not _:
            continue
        data = data[:-2] if data.endswith(b"\r\n") else data
        headers = head.decode("utf-8", "ignore")
        disposition = next((line for line in headers.splitlines()
                            if line.lower().startswith("content-disposition")), "")
        name_match = re.search(r'name="([^"]*)"', disposition)
        file_match = re.search(r'filename="([^"]*)"', disposition)
        if not name_match:
            continue
        if file_match and file_match.group(1):
            upload = (file_match.group(1), data)
        else:
            fields[name_match.group(1)] = data.decode("utf-8", "ignore")
    return fields, upload


def _upload_filename(raw_name: str) -> str:
    """Нормализовать имя файла из multipart независимо от ОС клиента."""
    # Браузер Windows иногда присылает полный путь с обратными слешами.
    name = Path(str(raw_name or "").replace("\\", "/")).name.strip()
    if not name or name in {".", ".."} or "\x00" in name:
        raise ValueError("Некорректное имя файла")
    return name


def save_upload(name: str, data: bytes) -> tuple[str, Path, bool]:
    """Сохранить модель в uploads и не затереть другой файл с тем же именем.

    Возвращает фактическое имя, путь и признак создания нового файла. Файлы
    с одинаковым именем, но разным содержимым получают короткий суффикс хеша:
    задания в очереди никогда не начинают печатать содержимое чужой загрузки.
    """
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    requested = _upload_filename(name)
    suffix = Path(requested).suffix
    stem = requested[:-len(suffix)] if suffix else requested
    target = UPLOAD_DIR / requested
    digest = hashlib.sha256(data).hexdigest()[:10]
    if target.exists():
        try:
            same = target.stat().st_size == len(data) and hashlib.sha256(
                target.read_bytes()).hexdigest()[:10] == digest
        except OSError:
            same = False
        if not same:
            target = UPLOAD_DIR / f"{stem}-{digest}{suffix}"
            # Коллизия хеша крайне маловероятна, но не затираем файл и при ней.
            counter = 2
            while target.exists():
                target = UPLOAD_DIR / f"{stem}-{digest}-{counter}{suffix}"
                counter += 1
    created = not target.exists()
    if created:
        target.write_bytes(data)
    return target.name, target, created


def _form_bool(value: object, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().casefold() in {"1", "true", "yes", "on", "да"}


class Api:
    """Логика маршрутов, отделённая от транспорта."""

    def __init__(self):
        ensure_dirs()
        self.db = Database()
        # Шина событий: сервер сам сообщает вкладкам, что изменилось,
        # вместо того чтобы каждая из них опрашивала его по таймеру.
        self.bus = EventBus()
        self.db.bus = self.bus
        if self.db.recovery:
            recovery = self.db.recovery
            self.db.add_event(
                "database_recovery", "База данных восстановлена",
                str(recovery.get("message") or "Выполнено аварийное восстановление."),
                "", recovery)
            try:
                from .logging_setup import log
                log().warning("%s Карантин: %s",
                              recovery.get("message"), recovery.get("quarantine"))
            except Exception:
                pass
        self.repo = Repo(self.db)
        self.acc = Accounting(self.db)
        self.manager = PrinterManager(self.db, self.repo)
        from .shelf import Shelf
        self.shelf = Shelf(self.db)
        # --- учёт 3.0: номенклатура, склады, документы, партии
        from .batches import Batches
        from .documents import Documents
        from .nomenclature import Nomenclature
        from .stock import Stock
        self.stock = Stock(self.db)
        self.nom = Nomenclature(self.db)
        self.docs = Documents(self.db)
        self.batches = Batches(self.db, self.manager)
        self.manager.batches = self.batches
        from .planner import Planner
        self.planner = Planner(self.db, self.batches)
        from .insights import Insights
        self.insights = Insights(self.db)
        from .envelopes import Envelopes
        self.envelopes = Envelopes(self.db)
        from .shopping import ShoppingList
        self.shopping = ShoppingList(self.db)
        from .month_close import MonthClose
        self.month_close = MonthClose(self.db)
        from .clients import Clients
        self.clients = Clients(self.db)
        from .order_intake import OrderIntake
        self.order_intake = OrderIntake(self.db)
        from .production import ProductionPreparation
        self.production = ProductionPreparation(self.db, self.manager)
        from .completion import OrderCompletion
        self.completion = OrderCompletion(self.db, self.repo)
        from .fulfillment import OrderFulfillment
        self.fulfillment = OrderFulfillment(self.db, self.repo, self.stock, self.acc)
        from .stocking import OrderStocker
        self.stocker = OrderStocker(self.db, self.repo, self.stock, self.docs, self.acc)
        from .receivables import Receivables
        self.receivables = Receivables(self.db, self.repo, self.acc)
        from .defect_recovery import DefectRecovery
        self.defect_recovery = DefectRecovery(self.db, self.manager)
        from .aftercare import CustomerAftercare
        self.aftercare = CustomerAftercare(self.db, self.repo)
        from .b2b import B2B
        self.b2b = B2B(self.db)
        from .updater import UpdateChecker
        self.updater = UpdateChecker(APP_VERSION, self.db, self.manager)
        self.updater.start_auto()
        from .workshop_v9 import WorkshopV9
        self.workshop = WorkshopV9(self.db, self.repo, self.shopping, self.manager, self.acc)
        try:
            self.workshop.ensure_schema()
        except Exception:
            pass
        self.live = LiveBroadcaster(self.bus, self.manager)
        self.live.start()
        self.last_host = ""
        self.listen_host = "127.0.0.1"
        self.listen_port = 8080
        self.started_at = time.time()

    def restart_process(self) -> None:
        """Перезапустить процесс (маркер восстановления применится на старте)."""
        import os
        import sys
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception:
            pass  # перезапуск вручную: python pf.py

    def qr_target(self, path: str, query: str = "") -> dict:
        """URL для QR-наклейки: LAN, а не localhost панели."""
        from .config import public_page_url
        return public_page_url(
            path, query,
            host_header=getattr(self, "last_host", ""),
            public_url=str(self.db.setting("public_url", "") or ""),
            listen_port=int(getattr(self, "listen_port", 8080) or 8080))

    def _ams_slot_num(self, tray: dict) -> int:
        return int(num(tray.get("unit"))) * 4 + int(num(tray.get("slot")))

    def suggest_spool_slot(self, spool: dict) -> dict:
        """Свободный или активный слот AMS — чтобы с телефона не угадывать номер."""
        manager = getattr(self, "manager", None)
        printer = manager.get(spool.get("printer_id") or "") if manager else None
        if not printer:
            return {"slot": "", "reason": "", "printer_id": "", "trays": []}
        try:
            snap = printer.snapshot()
        except Exception:
            return {"slot": "", "reason": "", "printer_id": printer.id, "trays": []}
        trays = (snap.get("ams") or {}).get("trays") or []
        empty = [t for t in trays if not t.get("type") or num(t.get("remain"), -1) == 0]
        if empty:
            return {"slot": str(self._ams_slot_num(empty[0])), "reason": "свободный слот",
                    "printer_id": printer.id, "trays": trays}
        need = str(spool.get("material") or "").upper()
        same = [t for t in trays if need and str(t.get("type") or "").upper() == need]
        if same:
            return {"slot": str(self._ams_slot_num(same[0])), "reason": f"уже стоит {need}",
                    "printer_id": printer.id, "trays": trays}
        active = next((t for t in trays if t.get("active")), None)
        if active:
            return {"slot": str(self._ams_slot_num(active)), "reason": "активный слот",
                    "printer_id": printer.id, "trays": trays}
        return {"slot": "", "reason": "", "printer_id": printer.id, "trays": trays}

    def labels(self, kind: str = "all") -> dict:
        """Все ссылки для листа наклеек: катушки и ценники стеллажа."""
        from urllib.parse import quote
        kind = (kind or "all").strip().lower()
        base = self.qr_target("/")
        spools, shelf = [], []
        if kind in ("", "all", "spool", "spools"):
            for s in self.repo.spools():
                info = self.qr_target("/spool.html", f"id={quote(str(s['id']), safe='')}")
                spools.append({
                    "id": s["id"], "url": info["url"],
                    "material": s.get("material") or "",
                    "color_name": s.get("color_name") or "",
                    "brand": s.get("brand") or "",
                    "color_hex": s.get("color_hex") or "#333333",
                    "remaining_grams": s.get("remaining_grams"),
                    "ams_slot": s.get("ams_slot"),
                })
        if kind in ("", "all", "shelf"):
            from .barcode import svg as barcode_svg
            for item in self.shelf.items():
                info = self.shelf.qr_link(
                    item["id"], getattr(self, "last_host", ""),
                    str(self.db.setting("public_url", "") or ""),
                    int(getattr(self, "listen_port", 8080) or 8080))
                barcode = str(item.get("barcode") or "").strip()
                try:
                    code_svg = barcode_svg(barcode, width_mm=38, height_mm=7, show_text=False) if barcode else ""
                except ValueError:
                    code_svg = ""
                shelf.append({
                    "id": item["id"], "url": info["url"],
                    "name": item.get("name") or "",
                    "price": item.get("price"),
                    "qty": item.get("qty"),
                    "sku": item.get("sku") or "",
                    "barcode": barcode,
                    "barcode_svg": code_svg,
                    "barcode_source": item.get("barcode_source") or "",
                    "material": item.get("material") or "",
                    "grams": item.get("grams") or 0,
                    "note": item.get("note") or "",
                    "tag_note": item.get("tag_note") or "",
                    "tag_badge": item.get("tag_badge") or "",
                    "tag_template": item.get("tag_template") or "standard",
                    "tag_variant": item.get("tag_variant") or "clean",
                    "tag_color": item.get("tag_color") or "#4f46e5",
                    "tag_old_price": item.get("tag_old_price") or 0,
                    "photo": bool(item.get("photo")),
                })
        return {"base": base["base"], "reachable": base["reachable"], "source": base["source"],
                "spools": spools, "shelf": shelf,
                "one_c": {"linked": sum(1 for item in shelf if item.get("barcode")),
                           "total": len(shelf)}}

    def ops_today(self) -> dict:
        """Сводка для телефона у станка: печать, выдача, пластик, LAN."""
        lan = self.qr_target("/")
        manager = getattr(self, "manager", None)
        snap = manager.snapshot() if manager and hasattr(manager, "snapshot") else {}
        active = snap.get("active") or {}
        info = active.get("printer") or {}
        trays = (active.get("ams") or {}).get("trays") or []
        threshold = num(self.db.setting("filament_low_threshold", 15), 15)
        ams_low = [t for t in trays
                   if t.get("remain") is not None and num(t.get("remain")) < threshold]
        ready_ids = [r["id"] for r in self.db.query(
            "SELECT id FROM statuses WHERE is_final=0 AND"
            " (id='ready' OR name LIKE 'Готов%')")]
        ready = []
        if ready_ids:
            marks = ",".join("?" * len(ready_ids))
            ready = self.db.query(
                f"SELECT id, number, product, customer_name, due, price, paid, prepaid"
                f" FROM orders WHERE status IN ({marks})"
                f" ORDER BY COALESCE(due,'9999') LIMIT 8", ready_ids)
        low_spools = []
        for s in self.repo.spools():
            if num(s.get("percent")) < threshold:
                low_spools.append({
                    "id": s["id"], "material": s.get("material"),
                    "color_name": s.get("color_name"),
                    "remaining_grams": s.get("remaining_grams"),
                    "percent": s.get("percent"),
                })
            if len(low_spools) >= 6:
                break
        queued = [j for j in (snap.get("queue") or []) if j.get("state") == "queued"]
        nxt = queued[0] if queued else {}
        return {
            "lan": lan,
            "printer": {
                "id": active.get("id") or "",
                "name": active.get("name") or "",
                "state": info.get("state") or "",
                "state_label": info.get("state_label") or "",
                "task": info.get("task") or "",
                "progress": info.get("progress") or 0,
                "remaining_min": info.get("remaining_min") or 0,
                "camera": bool((active.get("camera") or {}).get("available")),
            },
            "ams_low": ams_low,
            "ready": ready,
            "queue": len(queued),
            "next": nxt.get("name") or "",
            "next_id": nxt.get("id") or "",
            "low_spools": low_spools,
            "quiet": bool(snap.get("quiet")),
        }

    # --------------------------------------------------------------- хелперы
    def printer_or_fail(self, printer_id: str = "") -> BambuPrinter:
        printer = self.manager.get(printer_id)
        if not printer:
            raise ValueError("Принтер не настроен. Добавьте его в разделе «Принтеры».")
        return printer

    # ---------------------------------------------------------- Bambu Cloud
    def _cloud_session(self) -> tuple[str, str, str]:
        """(token, uid, region) из настроек. Пустые строки, если входа не было."""
        settings = self.db.settings(include_secrets=True)
        return (str(settings.get("cloud_token") or ""),
                str(settings.get("cloud_uid") or ""),
                str(settings.get("cloud_region") or "global"))

    def cloud_devices(self) -> list[dict]:
        """Принтеры аккаунта Bambu Cloud (без Access Code — его подставляет сервер)."""
        from . import bambu_cloud
        token, uid, region = self._cloud_session()
        if not token or not uid:
            return []
        try:
            devices = bambu_cloud.get_devices(token, region)
        except bambu_cloud.CloudError:
            return []
        # Access Code — секрет: в браузер он не уходит, сервер подставит его
        # сам при добавлении принтера (см. _enrich_cloud_device).
        for device in devices:
            device.pop("access_code", None)
        return devices

    def cloud_status(self) -> dict:
        from .cloud_bridge import CloudBridge
        settings = self.db.settings(include_secrets=True)
        token, uid, region = self._cloud_session()
        logged = bool(token and uid)
        status: dict = {
            "configured": bool(settings.get("cloud_email")),
            "email": str(settings.get("cloud_email") or ""),
            "region": region,
            "logged": logged,
            "devices": self.cloud_devices() if logged else [],
            "bridge": CloudBridge.state_all(),
            "cloud_print_ok": logged,
        }
        if not logged and settings.get("cloud_email"):
            status["hint"] = "Вход не выполнен или токен устарел — войдите заново"
        return status

    def cloud_tasks(self, printer_id: str = "") -> list[dict]:
        """Облачная история печатей — вкладка «Файлы» облачного принтера."""
        from . import bambu_cloud
        token, uid, region = self._cloud_session()
        if not token or not uid:
            return []
        device_id = ""
        printer = self.manager.get(printer_id)
        if printer:
            device_id = printer.record.get("serial", "")
        try:
            return bambu_cloud.get_tasks(token, region, device_id, 20)
        except bambu_cloud.CloudError:
            return []

    def _cloud_progress(self, name: str):
        def progress(sent: int, total: int = 100) -> None:
            try:
                pct = round(sent / total * 100) if total else 0
                self.bus.publish("upload_progress", {"name": name, "sent": sent,
                                                     "total": total, "percent": pct})
            except Exception:
                pass
        return progress

    def _enrich_cloud_device(self, data: dict) -> dict:
        """Добавление принтера из списка аккаунта: сервер сам подставляет
        serial, имя, модель, LAN Access Code и IP из облака (в браузер код не уходит)."""
        serial = str(data.get("cloud_device") or "").strip()
        if not serial:
            return data
        from . import bambu_cloud
        token, uid, region = self._cloud_session()
        if token and uid:
            for device in bambu_cloud.get_devices(token, region, include_access_code=True):
                if str(device.get("serial")) == serial:
                    data.setdefault("serial", device.get("serial") or "")
                    data.setdefault("name", device.get("name") or data.get("name") or "Принтер")
                    data.setdefault("model", device.get("model") or data.get("model") or "P1S")
                    data.setdefault("mode", "cloud")
                    # Access Code и IP подставляются сервером — камера (6000) и
                    # FTPS (990) заработают по локальной сети даже в облачном режиме.
                    if device.get("access_code"):
                        data.setdefault("access_code", device["access_code"])
                    host = device.get("host") or self._discover_host(serial)
                    if host:
                        data.setdefault("host", host)
                    data.pop("cloud_device", None)
                    break
        return data

    def _discover_host(self, serial: str) -> str:
        """Локальный IP принтера по серийному номеру (SSDP) — для камеры/FTPS."""
        if not serial:
            return ""
        try:
            from .bambu import BambuPrinter
            for found in BambuPrinter.discover(timeout=2.0):
                if str(found.get("serial") or "") == serial and found.get("host"):
                    return str(found["host"])
        except Exception:
            pass
        return ""

    def _ensure_lan_access(self, printer) -> bool:
        """Дозаполнить IP и Access Code облачного принтера для FTPS/камеры.

        Файлы SD-карты доступны только по локальной сети (порт 990). У
        принтера, добавленного из облака, часто нет IP или кода: IP ищем
        SSDP-сканом, Access Code берём из облачного списка устройств.
        Найденное сохраняется в базе, чтобы не искать каждый раз.
        Возвращает True, если у принтера в итоге есть и IP, и код.
        """
        record = printer.record
        host = str(record.get("host") or "")
        code = str(record.get("access_code") or "")
        if host and code:
            return True
        serial = str(record.get("serial") or "")
        if not serial:
            return False
        updates: dict = {}
        if not code:
            from . import bambu_cloud
            token, uid, region = self._cloud_session()
            if token and uid:
                try:
                    for device in bambu_cloud.get_devices(
                            token, region, include_access_code=True):
                        if str(device.get("serial")) == serial:
                            if device.get("access_code"):
                                updates["access_code"] = str(device["access_code"])
                            if not host and device.get("host"):
                                host = str(device["host"])
                                updates["host"] = host
                            break
                except bambu_cloud.CloudError:
                    pass
        if not host:
            found = self._discover_host(serial)
            if found:
                host = found
                updates["host"] = host
        if updates:
            try:
                self.repo.save_printer({"id": printer.id, **updates})
                self.manager.reload()
            except Exception:
                printer.record.update(updates)
        refreshed = self.manager.get(printer.id)
        record = refreshed.record if refreshed else printer.record
        return bool(record.get("host") and record.get("access_code"))

    def _templates(self) -> list[dict]:
        """Шаблоны ответов клиентам: список {id, title, text} из настроек."""
        try:
            raw = self.db.setting("reply_templates", "[]")
            if isinstance(raw, str):
                raw = json.loads(raw) if raw else []
            if isinstance(raw, list):
                return [t for t in raw if isinstance(t, dict) and t.get("text")]
        except Exception:
            pass
        return []

    def _audit(self, entity: str, entity_id: str, action: str, title: str,
               detail: str = "", data: dict | None = None, actor: str = "panel") -> None:
        """Единый журнал операторских действий панели/бота."""
        try:
            self.db.execute(
                "INSERT INTO audit_log(at,entity,entity_id,action,title,detail,data)"
                " VALUES(?,?,?,?,?,?,?)",
                (now_iso(), entity, str(entity_id or ""), action, title,
                 detail, json.dumps({"actor": actor or "panel", **(data or {})}, ensure_ascii=False)))
        except Exception:
            pass

    def order_save_photo(self, order_id: str, data_url: str, note: str = "", kind: str = "upload") -> dict:
        """Сохранить фото заказа (загрузка или кадр камеры)."""
        import base64
        from .config import PHOTO_DIR
        if "," not in data_url:
            raise ValueError("Не похоже на data URL")
        head, _, b64 = data_url.partition(",")
        ext = "png" if "png" in head else "jpg"
        try:
            raw = base64.b64decode(b64)
        except Exception as exc:
            raise ValueError(f"Не удалось разобрать фото: {exc}")
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Фото больше 8 МБ")
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        name = f"order_{order_id}_{int(time.time())}.{ext}"
        (PHOTO_DIR / name).write_bytes(raw)
        row = self.db.upsert("order_photos", {
            "id": uid("ph"), "order_id": order_id, "at": now_iso(),
            "file": name, "note": note or ("кадр с камеры" if kind == "camera" else "фото"),
            "kind": kind})
        return {"ok": True, "photo": row}

    # ---------------------------------------------------- витрина для покупателя
    def public_catalog(self) -> dict:
        """Каталог NOZZA для покупателя: товары с ценой, остатком и фото.

        Используется страницами order.html и shelf.html — это честная витрина,
        а не внутренний справочник: показываем только то, что продаётся.
        """
        groups = {r["id"]: r["name"] for r in self.db.query(
            "SELECT id, name FROM nom_groups WHERE archived=0")}
        niches = {r["id"]: {"name": r["name"], "icon": r["icon"], "color": r["color"]}
                  for r in self.db.query("SELECT id, name, icon, color FROM niches")}
        goods = []
        for item in self.nom.items(kind="product"):
            if num(item.get("archived")) or not bool(num(item.get("client_bot_published"), 1)):
                continue
            variants = self.db.query(
                "SELECT v.id,v.name,v.color_name,v.color_hex,v.size,v.sku,"
                " COALESCE((SELECT p.price FROM prices p JOIN price_types t ON t.id=p.price_type_id"
                " WHERE p.variant_id=v.id AND t.is_base=1 ORDER BY datetime(p.at) DESC,p.rowid DESC LIMIT 1),0) price,"
                " COALESCE((SELECT SUM(m.qty) FROM stock_moves m WHERE m.variant_id=v.id),0) qty"
                " FROM nom_variants v WHERE v.nom_id=? AND v.archived=0"
                " ORDER BY v.position,v.name", (item["id"],))
            base_price = num(item.get("price"))
            variant_prices = [num(v.get("price")) for v in variants if num(v.get("price")) > 0]
            display_price = base_price or (min(variant_prices) if variant_prices else 0)
            if display_price <= 0:
                continue
            goods.append({
                "id": item["id"],
                "name": item["name"],
                "price": display_price,
                "qty": item["qty"],
                "status": item["status"],
                "photo": item.get("photo") or "",
                "group": groups.get(item.get("group_id") or "", ""),
                "niche_id": item.get("niche_id") or "",
                "material": item.get("material") or "",
                # Внутренняя заметка не попадает во внешнюю витрину.
                "description": item.get("client_bot_description") or "",
                "variants": variants,
            })
        return {
            "company": str(self.db.setting("company_name", "NOZZA") or "NOZZA"),
            "currency": str(self.db.setting("currency", "₽") or "₽"),
            "items": goods,
            "niches": niches,
        }

    def public_order(self, body: dict) -> dict:
        """Атомарно принять заявку с витрины.

        Проверка идемпотентности, уникальный ключ заказа, связанные записи и
        ответ выполняются под BEGIN IMMEDIATE. Поэтому два параллельных retry
        не могут пройти одинаковую проверку «ключ ещё не встречался».
        """
        with self.db.transaction():
            return self._public_order_impl(body)

    def _reserve_public_order(self, order: dict) -> None:
        """Зарезервировать доступный готовый вариант заявки с витрины.

        Отсутствие остатка не блокирует заявку: позиция остаётся «под заказ».
        Сам вызов находится внутри транзакции public_order, поэтому проверка
        свободного остатка и вставка резерва сериализованы с соседним retry.
        """
        nom_id = str(order.get("nom_id") or "")
        if not nom_id:
            return
        existing = self.db.one(
            "SELECT id FROM reserves WHERE order_id=? AND state='active' LIMIT 1",
            (order.get("id") or "",))
        if existing:
            return
        variant_id = str(order.get("client_variant_id") or "")
        qty = max(1.0, num(order.get("qty"), 1))
        try:
            from .stock import Stock
            stock = Stock(self.db)
            warehouse_sql = ("SELECT warehouse_id, SUM(qty) balance FROM stock_moves"
                             " WHERE nom_id=?"
                             + (" AND variant_id=?" if variant_id else "")
                             + " GROUP BY warehouse_id HAVING balance>=?"
                             " ORDER BY balance DESC LIMIT 1")
            params = ((nom_id, variant_id, qty) if variant_id else (nom_id, qty))
            warehouse = self.db.one(warehouse_sql, params) or {}
            warehouse_id = str(warehouse.get("warehouse_id") or "")
            if (stock.qty(nom_id, warehouse_id, variant_id)
                    - stock.reserved(nom_id, warehouse_id, variant_id) < qty):
                return
            stock.reserve(nom_id, qty, order.get("id") or "", warehouse_id,
                          "резерв с публичной витрины", variant_id)
            self.db.execute(
                "UPDATE orders SET reserved=1,warehouse_id=?,updated_at=? WHERE id=?",
                (warehouse_id, now_iso(), order.get("id")))
        except Exception:
            # Заказ не должен теряться из-за временно неполного учёта склада.
            return

    def _public_order_impl(self, body: dict) -> dict:
        """Заявка с витрины (QR-заказ): создаёт заказ в статусе «Новая заявка».

        Вход строго валидируется: минимум полей, никаких внутренних ссылок.
        Клиент создаётся автоматически по имени и контакту. Поддерживает
        корзину: ``items: [{nom_id, qty, color}]`` — создаётся по заказу
        на каждую позицию, все привязываются к одному клиенту. Старый
        формат с одним ``nom_id`` сохранён.
        """
        request_id = str(body.get("request_id") or body.get("idempotency_key") or "").strip()[:160]
        if request_id:
            cached = self.db.one("SELECT response FROM idempotency_keys WHERE request_id=? AND kind='public_order'",
                                 (request_id,))
            if cached:
                try:
                    response = json.loads(cached.get("response") or "{}")
                except (TypeError, ValueError):
                    response = {}
                response["already_recorded"] = True
                return response
        source = str(body.get("source") or body.get("utm_source") or body.get("ref") or "storefront").strip()[:120]
        name = str(body.get("name") or "").strip()
        contact = str(body.get("phone") or body.get("messenger") or "").strip()
        if not name:
            raise ValueError("Укажите имя")
        if not contact:
            raise ValueError("Оставьте телефон или мессенджер")
        phone = str(body.get("phone") or "").strip()
        messenger = str(body.get("messenger") or "").strip()
        channel = str(body.get("channel") or "shop").strip() or "shop"
        note = str(body.get("note") or "").strip()

        # Собираем позиции: новый формат items[] или старый — единичный nom_id.
        items = body.get("items")
        if not items and body.get("nom_id"):
            items = [{"nom_id": body.get("nom_id"),
                      "qty": body.get("qty", 1),
                      "color": body.get("color", "")}]
        # Третий путь: индивидуальный заказ (custom:true) — старые заявки
        # без nom_id создавались как «свободный» текст, поддерживаем.
        if not items and str(body.get("custom") or "").lower() in ("1", "true", "yes"):
            text = str(body.get("product") or body.get("note") or "Индивидуальный заказ").strip()
            order = self.repo.save_order({
                "product": text, "customer_name": name, "phone": phone,
                "messenger": messenger, "channel": channel, "status": "new",
                "client_source": source, "client_request_id": request_id,
                "qty": max(1, int(num(body.get("qty"), 1))),
                "notes": note or "Заявка с витрины",
            })
            self.db.add_event("lead", "Заявка с витрины",
                              f"{text} · {name}", data={"order_id": order.get("id"),
                                                        "source": source})
            response = {"ok": True, "order_number": order.get("number"),
                        "order_id": order.get("id"), "count": 1}
            if request_id:
                self.db.execute("INSERT OR IGNORE INTO idempotency_keys(request_id,kind,entity_id,response,created_at) VALUES(?,?,?,?,?)",
                                (request_id, "public_order", order.get("id") or "", json.dumps(response, ensure_ascii=False), now_iso()))
            return response
        if not isinstance(items, list) or not items:
            raise ValueError("Добавьте хотя бы одну позицию")
        rows = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            nom_id = str(raw.get("nom_id") or "").strip()
            if not nom_id:
                continue
            item = self.nom.item(nom_id)
            if (not item or num(item.get("archived"))
                    or not bool(num(item.get("client_bot_published"), 1))):
                raise ValueError("Товар из заявки не опубликован или недоступен")
            qty = max(1, min(100, int(num(raw.get("qty"), 1))))
            color = str(raw.get("color") or "").strip()
            variant_id = str(raw.get("variant_id") or "").strip()
            variant = None
            if variant_id:
                variant = self.db.one(
                    "SELECT * FROM nom_variants WHERE id=? AND nom_id=? AND archived=0",
                    (variant_id, nom_id))
                if not variant:
                    raise ValueError("Вариант товара не найден")
                color = color or str(variant.get("name") or variant.get("color_name") or "").strip()
            variant_price = self.db.one(
                "SELECT p.price FROM prices p JOIN price_types t ON t.id=p.price_type_id"
                " WHERE p.variant_id=? AND t.is_base=1 ORDER BY datetime(p.at) DESC LIMIT 1",
                (variant_id,)) if variant_id else None
            unit_price = num((variant_price or {}).get("price")) or num(item.get("price"))
            if unit_price <= 0:
                raise ValueError("У товара нет опубликованной цены")
            rows.append({"item": item, "qty": qty, "color": color,
                         "variant": variant or {}, "unit_price": unit_price,
                         "variant_id": variant_id, "nom_id": nom_id})
        if not rows:
            raise ValueError("Добавьте хотя бы одну позицию")

        order_ids, numbers = [], []
        for row in rows:
            item, qty, color = row["item"], row["qty"], row["color"]
            nom_id, variant_id = row["nom_id"], row.get("variant_id") or ""
            variant = row.get("variant") or {}
            display_name = item.get("name") or ""
            if variant:
                display_name += " · " + str(variant.get("name") or variant.get("color_name") or variant.get("size") or "вариант")
            grams = num(variant.get("grams")) or num(item.get("grams"))
            hours = num(variant.get("hours")) or num(item.get("hours"))
            order = self.repo.save_order({
                "product": display_name,
                "customer_name": name,
                "phone": phone, "messenger": messenger, "channel": channel,
                "client_source": source,
                "client_request_id": request_id if not order_ids else "",
                "client_variant_id": variant_id,
                "niche_id": item.get("niche_id") or None,
                "status": "new",
                "qty": qty,
                "material": item.get("material") or "",
                "color": color,
                "grams": grams,
                "hours": hours,
                "manual_minutes": num(item.get("post_minutes")),
                "file": item.get("file") or "",
                "price": round(row.get("unit_price", num(item.get("price"))) * qty, 2),
                "notes": note or "Заявка с витрины",
                "nom_id": nom_id or None,
            })
            self._reserve_public_order(order)
            order_ids.append(order.get("id"))
            numbers.append(order.get("number"))
        first = numbers[0] if numbers else ""
        title = f"{first} (+{len(numbers) - 1})" if len(numbers) > 1 else first
        self.db.add_event("lead", "Заявка с витрины",
                          f"{len(numbers)} поз. · {name}" + (f" · {phone}" if phone else ""),
                          data={"order_ids": order_ids, "source": source})
        response = {"ok": True, "order_number": title, "order_id": order_ids[0] if order_ids else None,
                    "order_ids": order_ids, "order_numbers": numbers, "count": len(numbers)}
        if request_id:
            self.db.execute("INSERT OR IGNORE INTO idempotency_keys(request_id,kind,entity_id,response,created_at) VALUES(?,?,?,?,?)",
                            (request_id, "public_order", order_ids[0] if order_ids else "",
                             json.dumps(response, ensure_ascii=False), now_iso()))
        return response

    def save_order(self, body: dict) -> dict:
        """Сохранить заказ и синхронизировать резерв готового товара.

        Денежные поля не принимаются из формы заказа: старые значения остаются
        читаемыми для совместимости, а новые операции проходят через единый
        журнал ``/api/payment/save``.
        """
        if "paid" in body or "prepaid" in body:
            raise ValueError(
                "Оплата не редактируется в карточке заказа; используйте журнал платежей"
            )
        with self.db.transaction():
            order = self.repo.save_order(body)
            # У заказа может быть только один актуальный резерв. При изменении
            # количества/товара старую строку закрываем и создаём новую.
            self.stock.release(order_id=order["id"])
            if num(order.get("reserved")) and order.get("nom_id"):
                warehouse_id = order.get("warehouse_id") or ""
                if not warehouse_id:
                    variant_id = str(order.get("client_variant_id") or "")
                    candidate_sql = ("SELECT warehouse_id, SUM(qty) balance FROM stock_moves"
                                     " WHERE nom_id=?"
                                     + (" AND variant_id=?" if variant_id else "")
                                     + " GROUP BY warehouse_id HAVING balance>=?"
                                     " ORDER BY balance DESC LIMIT 1")
                    candidate_params = ((order["nom_id"], variant_id, max(1.0, num(order.get("qty"), 1)))
                                        if variant_id else
                                        (order["nom_id"], max(1.0, num(order.get("qty"), 1))))
                    candidate = self.db.one(candidate_sql, candidate_params)
                    warehouse_id = (candidate or {}).get("warehouse_id") or ""
                    if warehouse_id:
                        self.db.execute("UPDATE orders SET warehouse_id=? WHERE id=?",
                                        (warehouse_id, order["id"]))
                self.stock.reserve(order["nom_id"], max(1.0, num(order.get("qty"), 1)),
                                   order["id"], warehouse_id, "готовый товар заказа",
                                   str(order.get("client_variant_id") or ""))
            order = self.repo.order(order["id"]) or order
        return {"ok": True, "order": order}

    def fulfill_order(
        self,
        order_id: str,
        account_id: str = "",
        *,
        handoff_confirmed: bool = False,
        payment_action: str = "",
        payment_method: str = "",
    ) -> dict:
        """Совместимый вход в единый сервис подтверждаемой выдачи."""
        service = getattr(self, "fulfillment", None)
        if service is None:
            from .fulfillment import OrderFulfillment
            service = OrderFulfillment(self.db, self.repo, self.stock, self.acc)
        return service.fulfill(
            order_id,
            handoff_confirmed=handoff_confirmed,
            payment_action=payment_action,
            account_id=account_id,
            payment_method=payment_method,
        )

    def network_diagnose(self, host: str) -> dict:
        from . import network
        host = host or ""
        if not host:
            printers = self.repo.printers()
            host = (printers[0] or {}).get("host") if printers else ""
        if not host:
            return {"error": "Укажите IP принтера", "host": ""}
        return network.diagnose(host)

    def track_order(self, number: str, phone: str = "", token: str = "") -> dict:
        """Приватный статус по одноразовой ссылке из клиентского бота.

        Номер и телефон сами по себе больше не являются ключом доступа: старый
        публичный перебор заказов закрыт. В базе хранится только SHA-256 токена.
        """
        number = (number or "").strip()
        token = (token or "").strip()
        if not number:
            return {"found": False, "error": "Укажите номер заказа"}
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        # Единый ответ не раскрывает, существует ли номер без корректного ключа.
        if not order or not token:
            return {"found": False, "error": "Откройте статус по ссылке из Telegram-бота"}
        expected = str(order.get("client_track_token_hash") or "")
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not expected or not hmac.compare_digest(expected, digest):
            return {"found": False, "error": "Ссылка статуса недействительна или устарела"}
        status = self.db.one("SELECT * FROM statuses WHERE id=?", (order.get("status", ""),))
        photos = self.db.query(
            "SELECT * FROM order_photos WHERE order_id=? ORDER BY datetime(at) DESC LIMIT 4",
            (order["id"],))
        return {
            "found": True,
            "number": order.get("number"),
            "product": order.get("product"),
            "status": (status or {}).get("name") or order.get("status"),
            "status_color": (status or {}).get("color") or "#64748b",
            "due": order.get("due") or "",
            "qty": order.get("qty"),
            "photos": [p.get("id") for p in photos],
        }

    def shelf_save_photo(self, item_id: str, data_url: str) -> dict:
        """Сохранить фото позиции стеллажа из data URL (jpeg/png)."""
        from .config import PHOTO_DIR
        import base64
        if "," not in data_url:
            raise ValueError("Не похоже на data URL")
        head, _, b64 = data_url.partition(",")
        ext = "png" if "png" in head else "jpg"
        try:
            raw = base64.b64decode(b64)
        except Exception as exc:
            raise ValueError(f"Не удалось разобрать фото: {exc}")
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Фото больше 8 МБ")
        name = f"shelf_{item_id}.{ext}"
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        (PHOTO_DIR / name).write_bytes(raw)
        self.db.execute("UPDATE shelf_items SET photo=?, updated_at=? WHERE id=?",
                        (name, now_iso(), item_id))
        return {"ok": True, "photo": name}

    # ------------------------------------------------------------------- GET
    def get(self, path: str, query: dict) -> tuple[int, object]:
        one = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731
        from .v9_api import dispatch_get
        v9 = dispatch_get(self, path, query)
        if v9 is not None:
            return v9

        if path == "/api/health":
            return 200, {"ok": True, "version": APP_VERSION,
                         "uptime": round(time.time() - self.started_at)}
        if path == "/api/month-close":
            return 200, self.month_close.state(one("key"))
        if path == "/api/job/passport":
            from .passport import job_passport
            return 200, job_passport(self.db, one("id"))
        if path == "/api/catalog/recalc":
            return 200, self.acc.recalc_catalog(False)
        if path == "/api/camera/diagnose":
            from .camera import diagnose
            return 200, diagnose(self.printer_or_fail(one("printer_id")))
        if path == "/api/printer/rtsp-link":
            # Ссылка содержит Access Code — отдаём только по явному запросу.
            from .camera import rtsp_link
            printer = self.printer_or_fail(one("printer_id"))
            link = rtsp_link(printer)
            if not link:
                return 200, {"link": "", "error":
                             "Нужны IP и Access Code в карточке принтера"}
            self.db.add_event("printer", "Запрошена RTSP-ссылка",
                              printer.record.get("name") or "Принтер",
                              printer.id, {})
            return 200, {"link": link}
        if path == "/api/system/backups":
            from .db import list_backups, pending_restore
            db_stat = None
            from .config import DB_FILE
            if DB_FILE.exists():
                stat = DB_FILE.stat()
                db_stat = {"size": stat.st_size,
                           "at": time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(stat.st_mtime))}
            return 200, {"backups": list_backups(),
                         "db": db_stat,
                         "pending": pending_restore()}
        if path == "/api/bootstrap":
            return 200, {
                "version": APP_VERSION,
                "settings": self.db.settings(),
                "printers": self.repo.printers(),
                "statuses": self.repo.statuses(),
                "niches": self.repo.niches(),
                "summary": self.acc.summary(30),
                "state": self.manager.snapshot(),
            }
        if path == "/api/state":
            return 200, self.manager.snapshot(one("printer_id"))
        if path == "/api/printers":
            return 200, {"printers": self.repo.printers()}
        if path == "/api/printer/discover":
            # SSDP в локальной сети + принтеры аккаунта Bambu Cloud.
            # Access Code облачных устройств в браузер не отдаётся: при
            # добавлении из облака сервер подставляет его сам.
            return 200, {"found": BambuPrinter.discover(),
                         "cloud": self.cloud_devices()}
        if path == "/api/cloud/status":
            return 200, self.cloud_status()
        if path == "/api/printer/cloud-files":
            return 200, {"tasks": self.cloud_tasks(one("printer_id"))}
        if path == "/api/printer/telemetry":
            return 200, {"points": self.manager.guard.telemetry(
                one("printer_id"), int(num(one("minutes", "180"), 180)))}
        if path == "/api/printer/maintenance":
            return 200, {"tasks": self.manager.guard.maintenance(one("printer_id")),
                         "hours": self.manager.guard.runtime_hours(one("printer_id"))}
        if path == "/api/printer/alerts":
            return 200, {"alerts": self.manager.guard.alerts(one("printer_id"))}
        if path == "/api/printer/shots":
            printer = self.printer_or_fail(one("printer_id"))
            return 200, {"shots": printer.camera.snapshot_list()}
        if path == "/api/wall":
            return 200, self.manager.wall()
        if path == "/api/printer/files":
            printer = self.printer_or_fail(one("printer_id"))
            # Файлы SD — это FTPS по локальной сети. У облачного принтера
            # IP/Access Code могли не заполниться при добавлении: пробуем
            # дозаполнить (облачный список устройств + SSDP) прямо сейчас.
            if not (printer.record.get("host") and printer.record.get("access_code")):
                if not self._ensure_lan_access(printer):
                    return 200, {"path": one("path", "/"), "files": [],
                                 "error": ("Файлы SD-карты доступны только по локальной "
                                           "сети: укажите IP принтера (экран → Настройки → "
                                           "WLAN) и Access Code в карточке принтера.")}
                printer = self.printer_or_fail(one("printer_id"))
            from .v9_api import _files_payload
            return 200, _files_payload(printer, one("path", "/"))
        if path == "/api/orders":
            return 200, {"orders": self.repo.orders(one("status"), one("q"), one("niche_id"))}
        if path == "/api/order":
            order = self.repo.order(one("id"))
            return (200, order) if order else (404, {"error": "Заказ не найден"})
        if path == "/api/order/readiness":
            return 200, self.production.readiness(
                one("id"), one("printer_id"), one("spool_id"))
        if path == "/api/order/completion":
            return 200, self.completion.summary(one("id"))
        if path == "/api/order/fulfillment":
            return 200, self.fulfillment.summary(one("id"))
        if path == "/api/order/stock":
            return 200, self.stocker.summary(one("id"))
        if path == "/api/debt/summary":
            return 200, self.receivables.summary(one("id"))
        if path == "/api/aftercare/queue":
            return 200, self.aftercare.queue(int(num(one("limit", "80"), 80)))
        if path == "/api/aftercare/summary":
            return 200, self.aftercare.summary(one("id"))
        if path == "/api/customers":
            return 200, {"customers": self.repo.customers()}
        if path == "/api/statuses":
            return 200, {"statuses": self.repo.statuses()}
        if path == "/api/niches":
            return 200, {"niches": self.repo.niches()}
        if path == "/api/spools":
            return 200, {"spools": self.repo.spools(one("all") == "1")}
        if path == "/api/spool":
            spool = self.repo.spool(one("id"))
            if not spool:
                return 404, {"error": "Катушка не найдена"}
            suggest = self.suggest_spool_slot(spool)
            return 200, {"spool": spool, "printers": self.repo.printers(),
                         "suggest": suggest}
        if path == "/api/spool/qr-link":
            spool = self.repo.spool(one("id"))
            if not spool:
                return 404, {"error": "Катушка не найдена"}
            from urllib.parse import quote
            info = self.qr_target("/spool.html", f"id={quote(spool['id'], safe='')}")
            return 200, {**info, "spool": {
                "id": spool["id"], "material": spool.get("material"),
                "color_name": spool.get("color_name"),
                "brand": spool.get("brand") or "",
            }}
        if path == "/api/catalog":
            return 200, {"catalog": self.repo.catalog()}
        if path == "/api/transactions":
            return 200, {"transactions": self.repo.transactions(int(num(one("limit", "200"), 200)))}
        if path == "/api/finance":
            days = int(num(one("days", "30"), 30))
            self.acc.run_fixed_costs()
            return 200, {"summary": self.acc.summary(days),
                         "hour_cost": self.acc.actual_hour_cost(max(days, 30)),
                         "series": self.acc.daily_series(days),
                         "transactions": self.repo.transactions(50),
                         "niches": self.acc.niche_report(),
                         "accounts": self.acc.accounts_state(),
                         "debts": self.acc.debts(),
                         "break_even": self.acc.break_even()}
        if path == "/api/money":
            return 200, self.acc.money_state(int(num(one("months", "6"), 6)))
        if path == "/api/pnl":
            return 200, self.acc.pnl(int(num(one("months", "6"), 6)))
        if path == "/api/tax":
            return 200, self.acc.tax_report(int(num(one("year", "0"), 0)))
        if path == "/api/break-even":
            return 200, self.acc.break_even()
        if path == "/api/report":
            return 200, self.acc.report(one("period", "month"),
                                        int(num(one("offset", "0"), 0)))
        if path == "/api/report/sales":
            return 200, self.acc.sales_details(
                one("period", "month"), int(num(one("offset", "0"), 0)),
                int(num(one("limit", "500"), 500)))
        if path == "/api/debts":
            return 200, self.acc.debts()
        if path == "/api/accounts":
            return 200, {"accounts": self.repo.accounts(),
                         "state": self.acc.accounts_state()}
        if path == "/api/channels":
            return 200, {"channels": self.repo.channels()}
        if path == "/api/expense-categories":
            return 200, {"categories": self.repo.expense_categories()}
        if path == "/api/fixed-costs":
            return 200, {"fixed_costs": self.repo.fixed_costs(),
                         "monthly": self.acc.fixed_costs_monthly()}
        if path == "/api/payments":
            return 200, {"payments": self.repo.payments(one("order_id"))}
        if path == "/api/export/report":
            return 200, {"filename": f"printflow-{one('period', 'month')}.csv",
                         "csv": self.acc.report_csv(one("period", "month"),
                                                    int(num(one("offset", "0"), 0)))}
        if path == "/api/export/sales":
            return 200, {"filename": f"printflow-продажи-{one('period', 'month')}.csv",
                         "csv": self.acc.sales_details_csv(one("period", "month"),
                                                           int(num(one("offset", "0"), 0)))}
        if path == "/api/export/transactions":
            return 200, {"filename": "printflow-проводки.csv",
                         "csv": self.acc.transactions_csv(int(num(one("days", "365"), 365)))}
        if path == "/api/jobs":
            return 200, {"queue": self.manager.queue(),
                         "history": self.manager.history(int(num(one("limit", "100"), 100)))}
        if path == "/api/timeline":
            return 200, {"day": one("day", now_iso()[:10]),
                         "jobs": self.repo.timeline(one("day", now_iso()[:10]))}
        if path == "/api/shelf":
            return 200, {"items": self.shelf.items(), "summary": self.shelf.summary()}
        if path == "/api/shelf/item":
            item = self.shelf.item(one("id"))
            return (200, item) if item else (404, {"error": "Позиция не найдена"})
        if path == "/api/shelf/stock-available":
            # Товары учётных складов с остатком ≥ 1 шт — их можно
            # переместить на стеллаж (0 и «хвосты» меньше штуки не показываем).
            goods_only = str(one("goods", "")).lower() in ("1", "true", "yes")
            return 200, {"items": self.shelf.stock_available(goods_only=goods_only)}
        if path == "/api/shelf/moves":
            return 200, {"moves": self.shelf.moves(one("item_id"),
                                                   int(num(one("limit", "100"), 100)))}
        if path == "/api/shelf/qr-link":
            item = self.shelf.item(one("id"))
            if not item:
                return 404, {"error": "Позиция не найдена"}
            info = self.shelf.qr_link(
                one("id"), getattr(self, "last_host", ""),
                str(self.db.setting("public_url", "") or ""),
                int(getattr(self, "listen_port", 8080) or 8080))
            return 200, info
        if path == "/api/shelf/1c/lookup":
            try:
                item = self.shelf.cashier_lookup(one("barcode") or one("code"))
            except ValueError as exc:
                return 400, {"error": str(exc)}
            return (200, {"item": item}) if item else (
                404, {"error": "Код не привязан к позиции стеллажа"})
        if path == "/api/shelf/1c/export":
            items = self.shelf.items()
            return 200, {
                "filename": "printflow-1c-nomenclature.csv",
                "csv": self.shelf.one_c_export_csv(),
                "items": len(items),
                "linked": sum(1 for item in items if item.get("barcode")),
            }
        # ------------------------------------------------ учёт 3.0: номенклатура
        if path == "/api/nomenclature":
            items = self.nom.items(one("group_id"), one("kind"), one("search"),
                                   one("warehouse_id"),
                                   one("archived") == "1")
            # Без фильтров список полный — сводка считается по нему же и не
            # декорирует номенклатуру второй раз. С фильтрами сводка остаётся
            # глобальной (по всему складу) и считает свой проход, как раньше.
            filtered = bool(one("group_id") or one("kind") or one("search")
                            or one("archived") == "1")
            return 200, {
                "items": items,
                "summary": self.nom.summary(one("warehouse_id"),
                                            items=None if filtered else items),
                "groups": self.nom.groups(),
                "print_groups": self.nom.print_groups(),
                "warehouses": self.db.query(
                    "SELECT * FROM warehouses WHERE archived=0 ORDER BY position"),
                "price_types": self.db.query(
                    "SELECT * FROM price_types WHERE archived=0 ORDER BY position"),
            }
        if path == "/api/nomenclature/item":
            item = self.nom.item(one("id"))
            return (200, item) if item else (404, {"error": "Позиция не найдена"})
        if path == "/api/nomenclature/groups":
            return 200, {"groups": self.nom.groups()}
        if path == "/api/replenishment":
            return 200, {"rows": self.batches.plan_replenishment(one("warehouse_id"))}
        if path == "/api/nomenclature/frozen-capital":
            return 200, self.nom.frozen_capital(one("warehouse_id"))
        if path == "/api/nomenclature/filament-forecast":
            return 200, self.nom.filament_forecast(int(num(one("days", "30"), 30)))
        if path == "/api/plan/day":
            return 200, self.planner.day_plan()
        if path == "/api/insights":
            return 200, self.insights.all()
        if path == "/api/payback":
            return 200, self.insights.payback()
        if path == "/api/tax-compare":
            return 200, self.insights.tax_compare()
        if path == "/api/cash-daily":
            return 200, self.insights.cash_forecast_daily(int(num(one("days", "90"), 90)))
        if path == "/api/public/catalog":
            return 200, self.public_catalog()
        # --------------------------------------------------------- 5.0: сеть
        if path == "/api/network/diagnose":
            return 200, self.network_diagnose(one("host"))
        if path == "/api/network/ips":
            from .config import get_local_ips
            info = self.qr_target("/")
            return 200, {"ips": get_local_ips(), "base": info["base"],
                         "reachable": info["reachable"], "source": info["source"]}
        if path == "/api/labels":
            return 200, self.labels(one("kind", "all"))
        if path == "/api/ops/today":
            return 200, self.ops_today()
        if path == "/api/network/scan":
            from . import network
            ranges = [r for r in one("ranges").split(",") if r.strip()]
            return 200, {"found": network.scan_ranges(ranges)}
        if path == "/api/network/mdns":
            from . import network
            return 200, {"found": network.mdns_discover()}
        # ------------------------------------------------------- 5.0: конверты
        if path == "/api/envelopes":
            return 200, {"envelopes": self.envelopes.list(),
                         "total": self.envelopes.total(),
                         "auto": self.db.setting("envelope_auto", False)}
        # -------------------------------------------------------- 5.0: клиенты
        if path == "/api/clients/rfm":
            return 200, {"rows": self.clients.rfm(int(num(one("days", "90"), 90)))}
        if path == "/api/clients/duplicates":
            return 200, {"groups": self.clients.duplicates()}
        if path == "/api/data-check":
            return 200, self.repo.data_check()
        if path == "/api/order/history":
            return 200, {"history": self.repo.order_history(one("id"))}
        if path == "/api/track/order":
            return 200, self.track_order(one("number"), one("phone"), one("token"))
        # ------------------------------------------------------------- склады
        if path == "/api/warehouses":
            return 200, {"warehouses": self.stock.warehouse_totals(),
                         "reserves": self.stock.reserves(),
                         "reserved": round(sum(num(r.get("qty"))
                                               for r in self.stock.reserves()), 1)}
        if path == "/api/stock":
            return 200, {"balances": self.stock.balances(one("warehouse_id")),
                         "moves": self.stock.moves(one("nom_id"), one("warehouse_id"),
                                                   int(num(one("limit", "80"), 80)))}
        if path == "/api/stock/turnover":
            return 200, {"rows": self.stock.turnover(one("from"), one("to"),
                                                     one("warehouse_id"))}
        if path == "/api/reserves":
            return 200, {"reserves": self.stock.reserves()}
        # ---------------------------------------------------------- документы
        if path == "/api/documents":
            return 200, {"documents": self.docs.list(
                one("kind"), one("state"), one("warehouse_id"), one("search"),
                int(num(one("limit", "200"), 200)), one("order_id"))}
        if path == "/api/order/documents":
            order_id = one("id") or one("order_id")
            if not order_id:
                raise ValueError("Не указан заказ")
            return 200, self.docs.for_order(order_id)
        if path == "/api/document":
            doc = self.docs.get(one("id"))
            return (200, doc) if doc else (404, {"error": "Документ не найден"})
        # ------------------------------------------------------------- партии
        if path == "/api/batches":
            return 200, {"batches": self.batches.list(
                one("state"), int(num(one("limit", "100"), 100)))}
        if path == "/api/batch":
            batch = self.batches.get(one("id"))
            return (200, batch) if batch else (404, {"error": "Партия не найдена"})
        # --------------------------------------------------------------- цены
        if path == "/api/price-types":
            return 200, {"price_types": self.db.query(
                "SELECT * FROM price_types WHERE archived=0 ORDER BY position")}
        if path == "/api/audit":
            return 200, {"rows": self.db.query(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                (int(num(one("limit", "100"), 100)),))}
        if path == "/api/events":
            return 200, {"events": self.db.events(int(num(one("limit", "80"), 80)),
                                                  one("printer_id"), one("kind"))}
        if path == "/api/settings":
            return 200, {"settings": self.db.settings()}
        if path == "/api/staff":
            from .staff import ROLE_RIGHTS, ROLE_NAMES, Staff
            staff = Staff(self.db)
            return 200, {"staff": staff.all(), "invites": staff.invites(),
                         "roles": {r: {"name": ROLE_NAMES[r],
                                       "rights": sorted(rights)}
                                  for r, rights in ROLE_RIGHTS.items()},
                         "owner_chat": str(self.db.setting("telegram_chat_id",
                                                           "") or "")}
        if path == "/api/client-bot":
            bot = getattr(self.manager, "client_bot", None)
            settings = self.db.settings()
            data = {
                "enabled": bool(settings.get("client_bot_enabled")),
                "has_token": bool(settings.get("client_bot_token")),
                "welcome": str(settings.get("client_bot_welcome") or ""),
                "notify": bool(settings.get("client_bot_notify", True)),
                "catalog": bool(settings.get("client_bot_catalog", True)),
                "faq": str(settings.get("client_bot_faq") or ""),
                "review": bool(settings.get("client_bot_review", True)),
                "pickup_days": int(num(settings.get("client_bot_pickup_days"), 3)),
                "pay_info": str(settings.get("client_bot_pay_info") or ""),
                "pay_qr": str(settings.get("client_bot_pay_qr") or ""),
                "payment_purpose": str(settings.get("client_bot_payment_purpose") or ""),
                "quiet_hours_enabled": bool(settings.get("client_bot_quiet_hours_enabled", False)),
                "quiet_from": str(settings.get("client_bot_quiet_from") or "22:00"),
                "quiet_to": str(settings.get("client_bot_quiet_to") or "08:00"),
                "marketing_enabled": bool(settings.get("client_bot_marketing_enabled", False)),
                "track_url": str(settings.get("client_bot_track_url") or ""),
                "stats": bot.stats() if bot else {},
                "alive": bool(bot and bot.last_poll
                              and time.time() - bot.last_poll < 120),
                "chats": self.db.query(
                    "SELECT c.*, COUNT(l.order_id) orders FROM client_chats c"
                    " LEFT JOIN client_orders l ON l.chat_id=c.chat_id"
                    " GROUP BY c.chat_id ORDER BY datetime(c.last_seen) DESC"
                    " LIMIT 50"),
                "log": self.db.query(
                    "SELECT * FROM client_bot_log ORDER BY id DESC LIMIT 100"),
                "inbox": self.db.query(
                    "SELECT l.*, c.username, c.inbox_status, c.assigned_to"
                    " FROM client_bot_log l LEFT JOIN client_chats c ON c.chat_id=l.chat_id"
                    " WHERE l.direction='in' AND l.unread=1 ORDER BY l.id DESC LIMIT 60"),
                "payments": self.db.query(
                    "SELECT p.*,o.number,o.product,c.name FROM client_payment_intents p"
                    " LEFT JOIN orders o ON o.id=p.order_id LEFT JOIN client_chats c ON c.chat_id=p.chat_id"
                    " ORDER BY datetime(p.created_at) DESC LIMIT 60"),
                "analytics": (bot.analytics(30) if bot else {}),
                "templates": (bot.templates() if bot else []),
                "reviews": self.db.query(
                    "SELECT r.order_id, r.chat_id, r.rating, r.comment, r.state,"
                    " r.asked_at, r.created_at, r.resolved_at, r.operator_note,"
                    " o.number, o.product"
                    " FROM client_reviews r"
                    " LEFT JOIN orders o ON o.id=r.order_id"
                    " ORDER BY datetime(COALESCE(r.created_at,r.asked_at)) DESC LIMIT 30"),
                "orders": self.db.query(
                    "SELECT l.number, l.order_id, l.created_at linked_at,"
                    " l.source, o.product, o.status, o.price, c.name,"
                    " (SELECT COUNT(*) FROM order_photos p"
                    " WHERE p.order_id=l.order_id) photos"
                    " FROM client_orders l"
                    " JOIN orders o ON o.id=l.order_id"
                    " LEFT JOIN client_chats c ON c.chat_id=l.chat_id"
                    " ORDER BY datetime(l.created_at) DESC LIMIT 50"),
            }
            return 200, data
        if path == "/api/client-bot/inbox":
            return 200, {"items": self.db.query(
                "SELECT l.*,c.username,c.inbox_status,c.assigned_to FROM client_bot_log l"
                " LEFT JOIN client_chats c ON c.chat_id=l.chat_id"
                " WHERE l.direction='in' AND l.unread=1 ORDER BY l.id DESC LIMIT ?",
                (max(1, min(200, int(num(one("limit", "60"), 60)))),))}
        if path == "/api/client-bot/analytics":
            bot = getattr(self.manager, "client_bot", None)
            return 200, bot.analytics(int(num(one("days", "30"), 30))) if bot else {}
        if path == "/api/client-bot/payments":
            return 200, {"payments": self.db.query(
                "SELECT p.*,o.number,o.product,c.name FROM client_payment_intents p"
                " LEFT JOIN orders o ON o.id=p.order_id LEFT JOIN client_chats c ON c.chat_id=p.chat_id"
                " ORDER BY datetime(p.created_at) DESC LIMIT ?",
                (max(1, min(200, int(num(one("limit", "60"), 60)))),))}
        if path == "/api/rules":
            from .rules import ACTIONS, TRIGGERS
            return 200, {"rules": self.manager.rules.rules(),
                         "triggers": TRIGGERS, "actions": ACTIONS}
        if path == "/api/shopping":
            return 200, {"items": self.shopping.items(one("all") == "1"),
                         "summary": self.shopping.summary(),
                         "filament_stats": self.acc.filament_stats(int(num(one("days", "30"), 30)))}
        if path == "/api/purchase-hint":
            return 200, {"hint": self.acc.purchase_hint()}
        if path == "/api/update-check":
            return 200, self.updater.report()
        if path == "/api/abc":
            return 200, self.acc.abc_report(int(num(one("days", "30"), 30)))
        if path == "/api/calc/materials":
            return 200, self.acc.material_options()
        if path == "/api/materials":
            return 200, self.acc.material_options()
        if path == "/api/calc/real-stats":
            return 200, self.acc.real_stats(
                one("product"), one("material"),
                int(num(one("days", "60"), 60)))
        if path == "/api/calc/plate-layout":
            from .model_registry import ModelRegistry
            mr = ModelRegistry(self.db)
            return 200, mr.plate_layout(
                num(one("dim_x")), num(one("dim_y")),
                num(one("gap")), num(one("plate_w")), num(one("plate_h")))
        if path == "/api/models":
            from .model_registry import ModelRegistry
            mr = ModelRegistry(self.db)
            return 200, {"models": mr.list(one("search"), one("nom_id")),
                         "stats": mr.stats()}
        if path == "/api/model":
            from .model_registry import ModelRegistry
            mr = ModelRegistry(self.db)
            model = mr.get(one("id"))
            return (200, model) if model else (404, {"error": "Модель не найдена"})
        # ------------------------------------------------ 6.0: аналитика
        if path == "/api/analytics/oee":
            from .analytics import Analytics
            return 200, Analytics(self.db).oee(
                int(num(one("days", "30"), 30)), one("printer_id"))
        if path == "/api/analytics/correction":
            from .analytics import Analytics
            return 200, Analytics(self.db).correction_factors(
                int(num(one("days", "60"), 60)), one("material"))
        if path == "/api/analytics/pnl-products":
            from .analytics import Analytics
            return 200, Analytics(self.db).pnl_by_product(
                int(num(one("days", "30"), 30)))
        if path == "/api/analytics/anomalies":
            from .analytics import Analytics
            return 200, {"anomalies": Analytics(self.db).detect_anomalies(
                int(num(one("days", "30"), 30)))}
        if path == "/api/analytics/defects":
            from .analytics import Analytics
            return 200, Analytics(self.db).defect_analysis(
                int(num(one("days", "30"), 30)))
        if path == "/api/analytics/smart-queue":
            from .analytics import Analytics
            return 200, Analytics(self.db).smart_queue()
        if path == "/api/filament-stats":
            return 200, self.acc.filament_stats(int(num(one("days", "30"), 30)))
        if path == "/api/price-history":
            return 200, {"history": self.acc.price_history(one("product"),
                                                           int(num(one("limit", "30"), 30)))}
        if path == "/api/defects":
            return 200, {"defects": self.db.query(
                "SELECT d.*, j.name job_name FROM defects d"
                " LEFT JOIN print_jobs j ON j.id=d.job_id"
                " ORDER BY datetime(d.at) DESC LIMIT ?", (int(num(one("limit", "100"), 100)),))}
        if path == "/api/defect/recovery":
            return 200, self.defect_recovery.summary(
                one("id") or one("job_id"), num(one("grams")), one("reason")
            )
        if path == "/api/schedule":
            return 200, {"commands": self.db.query(
                "SELECT * FROM scheduled_commands ORDER BY done, datetime(at) LIMIT ?",
                (int(num(one("limit", "50"), 50)),))}
        if path == "/api/ams-profiles":
            return 200, {"profiles": self.db.query("SELECT * FROM ams_profiles ORDER BY name")}
        if path == "/api/templates":
            return 200, {"templates": self._templates()}
        if path == "/api/order/photos":
            return 200, {"photos": self.db.query(
                "SELECT * FROM order_photos WHERE order_id=? ORDER BY datetime(at) DESC",
                (one("order_id"),))}
        if path == "/api/search":
            return 200, {"results": self.repo.search(one("q"))}
        if path == "/api/backup":
            return 200, self.repo.export_all()
        # 8.0: Watch Folder
        if path == "/api/watch/pending":
            watch = getattr(self.manager, "watch", None)
            return 200, {"items": watch.list_pending(int(num(one("limit","20"),20))) if watch else []}
        if path == "/api/watch/status":
            watch = getattr(self.manager, "watch", None)
            return 200, {"enabled": bool(self.db.setting("watch_folder_enabled", False)), "path": str(self.db.setting("watch_folder_path","")), "pending": len(watch._pending) if watch else 0}
        if path == "/api/slicer/thumbnail":
            fid = one("fid")
            name = one("name")
            watch = getattr(self.manager, "watch", None)
            if watch and fid:
                info = watch.get_pending(fid) or {}
                thumbs = info.get("thumbnails_full", {}) or info.get("thumbnails", {})
                # name may be exact key or suffix
                for k,v in thumbs.items():
                    if k==name or k.endswith(name):
                        import base64
                        try:
                            base64.b64decode(v, validate=True)
                            return 200, {"ok": True, "b64": v}
                        except Exception:
                            pass
                return 404, {"error": "Превью не найдено"}
            return 404, {"error": "Нет данных"}
        if path == "/api/printer/health":
            printer = self.printer_or_fail(one("printer_id"))
            return 200, printer.health() if hasattr(printer, "health") else {"ok": False}
        if path == "/api/printer/preflight":
            printer = self.printer_or_fail(one("printer_id"))
            return 200, self.manager.preflight(printer.id, one("file"), int(num(one("plate"),1)), json.loads(one("mapping","[]") or "[]"))
        if path == "/api/printer/files/tree":
            printer = self.printer_or_fail(one("printer_id"))
            if not (printer.record.get("host") and printer.record.get("access_code")):
                self._ensure_lan_access(printer)
                printer = self.printer_or_fail(one("printer_id"))
            depth = int(num(one("depth"), 1))
            try:
                files = printer.files.list_tree(one("path","/"), depth)
            except Exception:
                files = printer.files.list_files(one("path","/"))
            return 200, {"path": one("path","/"), "files": files}
        if path == "/api/printer/files/usage":
            printer = self.printer_or_fail(one("printer_id"))
            return 200, printer.files.disk_usage(one("path","/"))
        if path == "/api/estimate":
            fname = one("file").strip()
            if not fname:
                return 400, {"error": "Не указано имя файла"}
            from .config import UPLOAD_DIR
            from .estimate import estimate_file, parse_3mf_complete
            safe_name = Path(fname).name
            local = safe_file(UPLOAD_DIR, safe_name) or (UPLOAD_DIR / safe_name)
            if not local.exists():
                try:
                    low = safe_name.lower()
                    for pat in ("*.3mf", "*.gcode", "*.gcode.3mf"):
                        for pp in UPLOAD_DIR.glob(pat):
                            nlow = pp.name.lower()
                            if nlow == low or low in nlow or nlow in low or pp.stem.lower() in low:
                                local = pp
                                break
                        if local.exists():
                            break
                except Exception:
                    pass
            if not local.exists():
                try:
                    watch_root = str(self.db.setting("watch_folder_path", "") or "").strip()
                    if watch_root:
                        wp = Path(watch_root).expanduser() / safe_name
                        if wp.exists():
                            local = wp
                        else:
                            for pp in Path(watch_root).expanduser().glob("*.3mf"):
                                if pp.name.lower() == safe_name.lower():
                                    local = pp
                                    break
                except Exception:
                    pass
            if local.exists():
                is_3mf = local.name.lower().endswith(".3mf")
                if is_3mf:
                    try:
                        est = estimate_file(local)
                        try:
                            detail = parse_3mf_complete(local)
                        except Exception:
                            detail = {}
                        if (not est.get("grams") and not est.get("total_grams")) and detail.get("plates"):
                            total_g = round(sum(float(p.get("grams") or 0) for p in detail["plates"]), 1)
                            total_m = round(sum(float(p.get("minutes") or 0) for p in detail["plates"]), 1)
                            if detail["plates"]:
                                est = dict(detail["plates"][0])
                                est["total_grams"] = total_g
                                est["total_minutes"] = total_m
                                est["plates"] = detail["plates"]
                                est["plate_count"] = len(detail["plates"])
                        return 200, {"estimate": est, "detail": detail if 'detail' in locals() else {}}
                    except Exception:
                        return 200, {"estimate": estimate_file(local)}
                else:
                    return 200, {"estimate": estimate_file(local)}
            base = Path(fname).name.lower()
            base_variants = {base}
            if base.endswith(".gcode.3mf"):
                base_variants.add(base[:-10] + ".3mf")
                base_variants.add(base[:-10])
            if base.endswith(".3mf"):
                base_variants.add(base[:-4])
            known = None
            try:
                for job in self.manager.history(500) + self.manager.queue():
                    job_name = Path(str(job.get("file") or job.get("name") or "")).name.lower()
                    if job_name in base_variants or base in job_name or any(v in job_name for v in base_variants):
                        if num(job.get("grams")) or num(job.get("est_grams")):
                            known = job
                            break
                        if known is None:
                            known = job
                if known:
                    grams = num(known.get("grams")) or num(known.get("est_grams"))
                    minutes = num(known.get("duration_min")) or num(known.get("est_minutes"))
                    if grams or minutes:
                        return 200, {"estimate": {"grams": grams, "minutes": minutes,
                                                   "total_grams": grams, "total_minutes": minutes,
                                                   "source": "history"}}
            except Exception:
                pass
            try:
                for printer in self.manager.printers.values():
                    est = self.manager._slicer_estimate(printer, fname)
                    if num(est.get("grams")) or num(est.get("minutes")) or est.get("material") or est.get("color"):
                        if est.get("grams") and not est.get("total_grams"):
                            est["total_grams"] = est["grams"]
                        if est.get("minutes") and not est.get("total_minutes"):
                            est["total_minutes"] = est["minutes"]
                        return 200, {"estimate": est}
            except Exception:
                pass
            return 404, {"error": f"Файл не найден: {safe_name}"}

        if path == "/api/settings/profiles":
            return 200, {"profiles": self.db.setting("settings_profiles", [])}
        if path == "/api/slicer/materials":
            return 200, self.acc.material_options()
        # --- 8.5: Фаза 11 --------------------------------------------------
        if path == "/api/content/week":
            from .content import week_post
            try:
                days = max(1, min(int(one("days", "7") or 7), 92))
            except ValueError:
                days = 7
            return 200, week_post(self.db, days)
        if path == "/api/content/social":
            from .content import social_pack
            try:
                days = max(7, min(int(one("days", "30") or 30), 366))
            except ValueError:
                days = 30
            return 200, social_pack(self.db, days)
        if path == "/api/content/avito":
            from .content import avito_card
            try:
                return 200, avito_card(self.db, one("item_id"))
            except ValueError as exc:
                return 400, {"error": str(exc)}
        if path == "/api/content/holiday":
            from .content import holiday_cards
            return 200, holiday_cards()
        if path == "/api/content/season":
            from .content import seasonality
            return 200, seasonality(self.db)
        if path == "/api/content/report":
            from .content import workshop_report
            try:
                days = max(7, min(int(one("days", "30") or 30), 366))
            except ValueError:
                days = 30
            return 200, workshop_report(self.db, days)
        if path == "/api/shelf/forecast":
            try:
                days = max(1, min(int(one("days", "7") or 7), 30))
            except ValueError:
                days = 7
            return 200, {"days": days, "items": self.shelf.forecast(days)}
        if path == "/api/shelf/tags":
            return 200, self.shelf.live_tags()
        if path == "/api/achievements":
            from .achievements import achievements
            return 200, {"badges": achievements(self.db)}
        if path == "/api/system/heartbeat":
            return 200, self._heartbeat()
        if path == "/api/ams/suggestion":
            return 200, {"suggestion": self._ams_suggestion()}
        if path == "/api/job/keyframes":
            from .config import PHOTO_DIR
            job_id = one("id")
            d = (PHOTO_DIR / "keyframes" / str(job_id)) if job_id else None
            if not d or not d.is_dir():
                return 200, {"frames": []}
            return 200, {"frames": [f.name for f in sorted(d.iterdir())
                                     if f.suffix == ".jpg"]}
        if path == "/api/order/pack-data":
            return 200, self._pack_data(one("id"))
        if path == "/api/photos/similar":
            from .photos import similar
            try:
                return 200, similar(self.db, one("photo_id"), limit=12)
            except ValueError as exc:
                return 400, {"error": str(exc)}
        if path == "/api/public/my":
            return 200, self._my_nozza(one("code"))
        if path == "/api/wish/list":
            customer_id = str(one("customer_id") or "")
            rows = self.db.query(
                "SELECT * FROM wishes WHERE customer_id=? ORDER BY created_at DESC",
                (customer_id,)) if customer_id else []
            return 200, {"wishes": rows}
        if path == "/api/bed/reference":
            from .config import PHOTO_DIR
            return 200, {"has": (PHOTO_DIR / "bed_reference.jpg").is_file()}
        # ------------------------------------------------- 8.5: генераторы
        if path == "/api/content/shelf-header":
            from .content import shelf_header
            try:
                days = max(1, min(int(one("days", "7") or 7), 30))
            except ValueError:
                days = 7
            return 200, shelf_header(self.db, days)
        if path == "/api/content/promo":
            from .content import promo_pack
            return 200, promo_pack(self.db)
        if path == "/api/content/week-video":
            from .content import week_video
            try:
                days = max(1, min(int(one("days", "7") or 7), 30))
            except ValueError:
                days = 7
            return 200, week_video(self.db, days)
        if path == "/api/content/print-map":
            from .content import print_map
            return 200, print_map(self.db)
        if path == "/api/order/thread":
            from .content import order_thread
            try:
                return 200, order_thread(self.db, one("id"))
            except ValueError as exc:
                return 404, {"error": str(exc)}
        if path == "/api/content/report/print":
            from .content import workshop_report_html
            try:
                days = max(7, min(int(one("days", "30") or 30), 366))
            except ValueError:
                days = 30
            return 200, {"html": workshop_report_html(self.db, days)}
        if path == "/api/content/stickers":
            from .content import stickers
            return 200, {"html": stickers(one("kind", "all"))}
        if path == "/api/content/business-card":
            from .content import business_card_html
            return 200, {"html": business_card_html(self.db, one("customer_id"))}
        if path == "/api/tour/state":
            backup = str(self.db.setting("tour_backup_file", "") or "")
            return 200, {"active": bool(backup), "backup": backup}
        if path == "/api/labels/code128":
            from .barcode import svg, validate
            text = str(one("text") or "").strip()
            if not text:
                return 400, {"error": "Нет текста для штрихкода"}
            try:
                info = validate(text)
            except ValueError as exc:
                return 400, {"error": str(exc)}
            return 200, {"svg": svg(text), **info}
        return 404, {"error": "Неизвестный маршрут"}

    # ------------------------------------------------------------------ POST
    def post(self, path: str, body: dict, query: dict) -> tuple[int, object]:
        pid = body.get("printer_id") or (query.get("printer_id") or [""])[0]
        from .v9_api import dispatch_post
        v9 = dispatch_post(self, path, body, query)
        if v9 is not None:
            return v9

        # --- Bambu Cloud: вход, код, выход
        if path == "/api/cloud/login":
            from . import bambu_cloud
            email = str(body.get("email", "")).strip()
            region = str(body.get("region", "") or "global").strip()
            if email:
                self.db.set_settings({
                    "cloud_email": email,
                    "cloud_region": region,
                })
            result = bambu_cloud.login(
                email, body.get("password", ""),
                region, body.get("code", ""),
                body.get("tfa_code", ""))
            if result.get("status") == "ok":
                self.db.set_settings({
                    "cloud_email": email or str(self.db.setting("cloud_email", "")),
                    "cloud_region": region or str(self.db.setting("cloud_region", "global")),
                    "cloud_token": result.get("token", ""),
                    "cloud_uid": result.get("uid", ""),
                })
                self.manager.refresh_cloud()
                self.bus.publish("resync", {})
            return 200, result
        if path == "/api/cloud/code":
            from . import bambu_cloud
            email = str(body.get("email") or self.db.setting("cloud_email", "")).strip()
            region = str(body.get("region") or self.db.setting("cloud_region", "global")).strip()
            if not email and bambu_cloud._PENDING:
                email = next(iter(bambu_cloud._PENDING.keys()))
                region = bambu_cloud._PENDING[email].get("region", region)
            result = bambu_cloud.login(
                email, "",
                region,
                body.get("code", ""))
            if result.get("status") == "ok":
                self.db.set_settings({
                    "cloud_email": email,
                    "cloud_region": region,
                    "cloud_token": result.get("token", ""),
                    "cloud_uid": result.get("uid", ""),
                })
                self.manager.refresh_cloud()
                self.bus.publish("resync", {})
            return 200, result
        if path == "/api/cloud/logout":
            from .cloud_bridge import CloudBridge
            CloudBridge.shutdown_all()
            self.db.clear_settings(["cloud_token", "cloud_uid"])
            self.manager.refresh_cloud()
            self.bus.publish("resync", {})
            return 200, {"ok": True, "message": "Выход выполнен. Принтеры в облачном режиме отключены."}

        # --- принтеры и команды
        if path == "/api/printer/save":
            body = self._enrich_cloud_device(dict(body))
            row = self.repo.save_printer(body)
            self.manager.reload()
            return 200, {"ok": True, "printer": {**row, "access_code": "",
                                                 "has_access_code": bool(row.get("access_code"))}}
        if path == "/api/printer/delete":
            self.repo.delete_printer(body.get("id", ""))
            self.manager.reload()
            return 200, {"ok": True}
        if path == "/api/printer/connect":
            self.manager.reload()
            printer = self.printer_or_fail(pid)
            printer.reconnect()
            return 200, {"ok": True}
        if path == "/api/printer/command":
            printer = self.printer_or_fail(pid)
            cmd = str(body.get("command") or "").strip()
            if cmd in DANGEROUS_AUTOMATION_COMMANDS and body.get("confirmed") is not True:
                raise ValueError("Подтвердите физическую команду оператора")
            # Для pause marker ставится ДО MQTT-команды, чтобы асинхронный
            # report PAUSE не успел запустить recovery. При ошибке команды
            # оставляем безопасную ручную блокировку до явного решения оператора.
            if cmd == "pause":
                self.manager.mark_user_paused(printer.id)
            result = printer.command(cmd, body.get("value"))
            if cmd == "resume":
                self.manager.clear_user_paused(printer.id)
            return 200, result
        if path == "/api/printer/convert-to-order":
            printer_id = pid or body.get("printer_id", "")
            return 200, self.manager.convert_active_to_order(printer_id, body)
        if path == "/api/jobs/convert-to-order":
            job_id = body.get("job_id") or body.get("id", "")
            if not job_id and (pid or body.get("printer_id")):
                return 200, self.manager.convert_active_to_order(pid or body.get("printer_id", ""), body)
            return 200, self.manager.convert_job_to_order(job_id, body)
        if path == "/api/printer/link-to-order":
            # Привязать текущую печать к уже существующему заказу — нужно
            # после сбоя питания или ручной распечатки, когда файл не
            # сопоставился автоматически.
            printer_id = pid or body.get("printer_id", "")
            order_id = str(body.get("order_id") or "").strip()
            if not order_id:
                raise ValueError("Нужен order_id")
            return 200, self.manager.link_active_to_order(printer_id, order_id)
        if path == "/api/jobs/link-to-order":
            job_id = str(body.get("job_id") or body.get("id") or "").strip()
            order_id = str(body.get("order_id") or "").strip()
            if not job_id or not order_id:
                raise ValueError("Нужны job_id и order_id")
            return 200, self.manager.link_job_to_order(job_id, order_id)
        if path == "/api/printer/reprint":
            # Подготовка повтора — явное подтверждение; физический старт всё
            # равно выполняется отдельно. Для failed нужна записанная причина.
            confirmed = body.get("reprint_confirmed") is True
            request_id = str(body.get("request_id") or "")
            order_number = str(body.get("order_number") or "").strip()
            if body.get("id"):
                row = self.manager.reprint_job(
                    str(body["id"]), str(body.get("printer_id") or ""),
                    confirmed=confirmed, request_id=request_id,
                    defect_id=str(body.get("defect_id") or ""),
                )
            else:
                row = self.manager.reprint_last_failed(
                    order_number, confirmed=confirmed, request_id=request_id,
                )
            return 200, {"ok": True, "job": row}
        if path == "/api/printer/ams/sync":
            # Ручной запуск автосбора: катушки AMS и данные принтера → база
            printer = self.printer_or_fail(pid)
            snap = printer.snapshot()
            from .ams_sync import sync_ams_spools, sync_printer_info
            info_ok = sync_printer_info(self.db, printer.id, snap)
            counts = sync_ams_spools(self.db, printer.id, snap)
            return 200, {"ok": True, "printer_info": info_ok, **counts,
                         "spools": self.repo.spools()}
        if path == "/api/printer/print":
            if body.get("confirmed") is not True:
                raise ValueError("Подтвердите физический запуск печати")
            printer = self.printer_or_fail(pid)
            name = str(body.get("name") or body.get("file") or "").strip()
            if not body.get("file"):
                raise ValueError("Не указан файл для печати")
            from .sd_browser import can_print
            if not can_print(str(body.get("file") or "")):
                raise ValueError("Нельзя печатать логи, таймлапс и ipcam")
            check = self.manager.preflight(
                printer.id, str(body.get("file") or ""), int(num(body.get("plate"), 1) or 1),
                body.get("ams_mapping") or body.get("mapping"))
            if check.get("blocks"):
                raise ValueError("Preflight блокирует старт: " + "; ".join(
                    str(x.get("title") or x.get("detail") or "") for x in check["blocks"]))
            if check.get("warns") and body.get("preflight_acknowledged") is not True:
                raise ValueError("Подтвердите предупреждения Preflight перед стартом")
            # Сначала создаём starting-задание, затем посылаем команду. Это
            # устраняет гонку: быстрый MQTT START больше не превращается в
            # отдельное безымянное задание до записи строки в базе.
            job = self.manager.enqueue({**body, "printer_id": printer.id,
                                        "source": "manual", "name": name,
                                        "allow_auto_start": False})
            try:
                started = self.manager.start_job(job["id"], printer.id)
            except Exception:
                # start_job возвращает запись в очередь после неудачного старта;
                # ошибка всё равно уходит клиенту, а повтор можно сделать вручную.
                raise
            self.db.add_event("print_start", "Запущена печать", name,
                              printer.id, {"job_id": job["id"]})
            return 200, {"ok": True, "job_id": job["id"], "job": started}
        if path == "/api/printer/sync-history":
            return 200, self.manager.sync_cloud_history(pid)
        if path == "/api/estimate/pull":
            return 200, self.manager.pull_print_file(
                pid or body.get("printer_id", ""),
                str(body.get("file") or body.get("name") or ""),
            )
        if path == "/api/printer/file/delete":
            printer = self.printer_or_fail(pid)
            return 200, printer.files.delete(body.get("path", ""))
        if path == "/api/printer/ftps-test":
            printer = self.printer_or_fail(pid)
            return 200, printer.files.test()
        if path == "/api/printer/snapshot":
            printer = self.printer_or_fail(pid)
            return 200, {"ok": True, "shot": printer.camera.snapshot(
                body.get("note", "Снимок вручную"), body.get("job_id", ""))}
        if path == "/api/printer/maintenance/done":
            return 200, {"ok": True,
                         "task": self.manager.guard.complete_maintenance(body.get("id", ""))}
        if path == "/api/printer/maintenance/save":
            body.setdefault("id", uid("maint"))
            body.setdefault("printer_id", pid)
            return 200, {"ok": True, "task": self.db.upsert("maintenance", body)}
        if path == "/api/printer/alerts/clear":
            self.manager.guard.clear(pid)
            return 200, {"ok": True}
        if path == "/api/printer/part-removed":
            return 200, self.manager.part_removed(pid)

        # --- очередь
        if path == "/api/jobs/enqueue":
            from .sd_browser import can_print
            file_value = str(body.get("file") or "")
            if file_value and not can_print(file_value):
                raise ValueError("Нельзя печатать логи, таймлапс и ipcam")
            return 200, {"ok": True, "job": self.manager.enqueue(body)}
        # Совместимый маршрут «добавить в очередь» из карточки заказа.
        # Раньше фронтенд вызывал /api/queue/add, которого не было на сервере.
        if path == "/api/queue/add":
            from .sd_browser import can_print
            file_value = str(body.get("file") or "")
            if file_value and not can_print(file_value):
                raise ValueError("Нельзя печатать логи, таймлапс и ipcam")
            payload = dict(body)
            # Кнопка «В очередь» из карточки заказа использует человеческие
            # единицы (grams/hours), а очередь хранит минуты/граммы как смету.
            if payload.get("title") and not payload.get("name"):
                payload["name"] = payload.get("title")
            if not num(payload.get("est_grams")) and num(payload.get("grams")):
                payload["est_grams"] = num(payload.get("grams"))
            if not num(payload.get("est_minutes")) and num(payload.get("hours")):
                payload["est_minutes"] = round(num(payload.get("hours")) * 60.0, 2)
            payload.pop("title", None)
            payload.pop("grams", None)
            payload.pop("hours", None)
            payload.setdefault("source", "order-quick")
            payload.setdefault("allow_auto_start", False)
            return 200, {"ok": True, "job": self.manager.enqueue(payload)}
        if path == "/api/jobs/start":
            if body.get("confirmed") is not True:
                raise ValueError("Подтвердите физический запуск печати")
            job = self.db.one("SELECT * FROM print_jobs WHERE id=?", (body.get("id", ""),))
            if not job:
                raise ValueError("Задание не найдено")
            from .sd_browser import can_print
            file_value = str(job.get("file") or "")
            if file_value and not can_print(file_value):
                raise ValueError("Нельзя печатать логи, таймлапс и ipcam")
            printer_id = pid or job.get("printer_id") or ""
            check = self.manager.preflight(
                printer_id, str(job.get("file") or job.get("name") or ""),
                int(num(job.get("plate"), 1) or 1),
                json.loads(job.get("ams_mapping") or "[]") if job.get("ams_mapping") else [],
            )
            if check.get("blocks"):
                raise ValueError("Preflight блокирует старт: " + "; ".join(
                    str(x.get("title") or x.get("detail") or "") for x in check["blocks"]))
            if check.get("warns") and body.get("preflight_acknowledged") is not True:
                raise ValueError("Подтвердите предупреждения Preflight перед стартом")
            return 200, {"ok": True, "job": self.manager.start_job(
                job["id"], printer_id,
                start_request_id=str(body.get("start_request_id") or ""),
            )}
        if path == "/api/jobs/cancel":
            return 200, {"ok": True, "job": self.manager.cancel_job(body.get("id", ""))}
        if path == "/api/jobs/save":
            body.setdefault("id", uid("job"))
            body.setdefault("created_at", now_iso())
            return 200, {"ok": True, "job": self.db.upsert("print_jobs", body)}
        if path == "/api/jobs/delete":
            self.db.delete("print_jobs", body.get("id", ""))
            return 200, {"ok": True}

        # --- заказы и справочники
        if path == "/api/materials/save":
            return 200, {"ok": True, "material": self.repo.save_material(body)}
        if path == "/api/materials/delete":
            self.repo.delete_material(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/materials/reset":
            self.repo.reset_material(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/order/intake/preview":
            return 200, self.order_intake.preview(
                str(body.get("text") or ""), str(body.get("channel") or ""))
        if path == "/api/order/prepare":
            return 200, self.production.prepare(
                str(body.get("id") or ""), str(body.get("printer_id") or ""),
                str(body.get("spool_id") or ""))
        if path == "/api/order/accept":
            return 200, self.completion.accept(
                str(body.get("id") or ""),
                quality_confirmed=body.get("quality_confirmed") is True,
            )
        if path == "/api/order/save":
            return 200, self.save_order(body)
        if path == "/api/order/status":
            order = self.repo.set_order_status(body.get("id", ""), body.get("status", ""))
            client = getattr(self.manager, "client_bot", None)
            if client:
                try:
                    client._maybe_push_statuses()
                except Exception:
                    pass
            self._audit("order", body.get("id", ""), "status", "Статус заказа изменён",
                        str(body.get("status") or ""), actor=str(body.get("actor") or "panel"))
            return 200, {"ok": True, "order": order}
        if path == "/api/order/delete":
            self.stock.release(order_id=body.get("id", ""))
            self.repo.delete_order(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/order/fulfill":
            return 200, self.fulfill_order(
                str(body.get("id") or ""),
                str(body.get("account_id") or ""),
                handoff_confirmed=body.get("handoff_confirmed") is True,
                payment_action=str(body.get("payment_action") or ""),
                payment_method=str(body.get("payment_method") or ""),
            )
        if path == "/api/order/duplicate":
            return 200, {"ok": True, "order": self.repo.duplicate_order(body.get("id", ""))}
        if path == "/api/order/stock-to-warehouse":
            return 200, self.stocker.stock_to_warehouse(
                str(body.get("id") or ""),
                warehouse_id=str(body.get("warehouse_id") or ""),
                note=str(body.get("note") or ""),
            )
        if path == "/api/order/waybill":
            order_id = str(body.get("id") or body.get("order_id") or "").strip()
            already = bool(self.db.one(
                "SELECT id FROM documents WHERE order_id=? AND kind='sale' LIMIT 1",
                (order_id,)))
            return 200, {"ok": True, "existing": already,
                         "document": self.docs.waybill_from_order(
                             order_id,
                             warehouse_id=str(body.get("warehouse_id") or ""),
                             post=body.get("post") is True,
                         )}
        if path == "/api/aftercare/request/confirm":
            return 200, self.aftercare.confirm_request(
                str(body.get("id") or ""),
                sent_confirmed=body.get("sent_confirmed") is True,
                force=body.get("force") is True,
                request_id=str(body.get("request_id") or ""),
            )
        if path == "/api/aftercare/response":
            return 200, self.aftercare.record_response(
                str(body.get("id") or ""),
                response_received=body.get("response_received") is True,
                rating=body.get("rating") or 0,
                text=str(body.get("text") or ""),
                publish_permission=str(body.get("publish_permission") or "not_asked"),
                repeat_interest=str(body.get("repeat_interest") or "not_asked"),
                request_id=str(body.get("request_id") or ""),
            )
        if path == "/api/aftercare/repeat":
            return 200, self.aftercare.prepare_repeat(
                str(body.get("id") or ""),
                repeat_confirmed=body.get("repeat_confirmed") is True,
                request_id=str(body.get("request_id") or ""),
            )
        if path == "/api/public/order":
            return 200, self.public_order(body)
        # --------------------------------------------------------- 5.0: конверты
        if path == "/api/envelope/save":
            return 200, {"ok": True, "envelope": self.envelopes.save(body)}
        if path == "/api/envelope/delete":
            self.envelopes.delete(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/envelope/withdraw":
            return 200, {"ok": True, "move": self.envelopes.withdraw(
                body.get("id", ""), num(body.get("amount")), body.get("note", ""))}
        if path == "/api/envelope/auto":
            self.db.set_settings({"envelope_auto": bool(body.get("enabled", True))})
            return 200, {"ok": True, "auto": self.db.setting("envelope_auto", False)}
        # -------------------------------------------------------- 5.0: клиенты
        if path == "/api/clients/merge":
            return 200, self.clients.merge(body.get("keep_id", ""),
                                           body.get("drop_ids") or [])
        if path == "/api/customer/save":
            return 200, {"ok": True, "customer": self.repo.save_customer(body)}
        if path == "/api/customer/delete":
            if not body.get("id"):
                raise ValueError("Не указан клиент")
            self.db.delete("customers", body["id"])
            return 200, {"ok": True}
        if path == "/api/status/save":
            return 200, {"ok": True, "status": self.repo.save_status(body)}
        if path == "/api/status/delete":
            self.repo.delete_status(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/niche/save":
            return 200, {"ok": True, "niche": self.repo.save_niche(body)}
        if path == "/api/niche/delete":
            self.repo.delete_niche(body.get("id", ""))
            return 200, {"ok": True}

        # --- склад, каталог, деньги
        if path == "/api/spool/save":
            slot = str(body.get("ams_slot") or "").strip()
            printer_id = str(body.get("printer_id") or "").strip()
            spool_id = str(body.get("id") or "").strip()
            if slot and printer_id:
                other = self.db.one(
                    "SELECT * FROM spools WHERE id<>? AND printer_id=? AND ams_slot=?"
                    " AND remaining_grams>0",
                    (spool_id or "__new__", printer_id, slot),
                )
                if other and body.get("force") is not True:
                    raise ValueError(
                        f"Слот {slot} уже занят катушкой "
                        f"{other.get('material') or ''} {other.get('color_name') or other['id']}. "
                        "Снимите её или подтвердите force."
                    )
                if other and body.get("force") is True:
                    self.db.execute(
                        "UPDATE spools SET ams_slot='', tray_uuid='', updated_at=? WHERE id=?",
                        (now_iso(), other["id"]),
                    )
            return 200, {"ok": True, "spool": self.repo.save_spool(body)}
        if path == "/api/spool/delete":
            self.repo.delete_spool(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/spool/consume":
            # id и spool_id — синонимы: UI шлёт id выбранной катушки
            return 200, self.acc.consume_filament(
                num(body.get("grams")), body.get("spool_id") or body.get("id", ""),
                note=body.get("note", ""), order_id=body.get("order_id", ""),
                material=body.get("material", ""), auto=False)
        if path == "/api/spool/restock":
            return 200, {"ok": True, "spool": self.acc.restock_spool(
                body.get("id", ""), num(body.get("grams")), num(body.get("price")))}
        if path == "/api/catalog/save":
            return 200, {"ok": True, "item": self.repo.save_catalog_item(body)}
        if path == "/api/catalog/delete":
            self.repo.delete_catalog_item(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/transaction/save":
            if body.get("id"):
                return 200, {"ok": True,
                             "transaction": self.repo.save_transaction_fields(body)}
            return 200, {"ok": True, "transaction": self.acc.add_transaction(
                body.get("kind", "income"), body.get("category", "other"),
                num(body.get("amount")), body.get("title", ""), body.get("note", ""),
                body.get("order_id", ""), body.get("job_id", ""), auto=False,
                account_id=body.get("account_id", ""), channel=body.get("channel", ""),
                payer=body.get("payer", ""), fee=num(body.get("fee")),
                taxable=body.get("taxable", True) not in (False, 0, "0"),
                deductible=body.get("deductible", True) not in (False, 0, "0"),
                customer_id=body.get("customer_id", ""), at=body.get("at", ""))}
        if path == "/api/transaction/delete":
            self.repo.delete_transaction(body.get("id", ""))
            return 200, {"ok": True}

        # --- кассы, каналы, статьи, постоянные расходы, платежи
        if path == "/api/account/save":
            return 200, {"ok": True, "account": self.repo.save_account(body)}
        if path == "/api/account/delete":
            self.repo.delete_account(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/channel/save":
            return 200, {"ok": True, "channel": self.repo.save_channel(body)}
        if path == "/api/channel/delete":
            self.repo.delete_channel(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/expense-category/save":
            return 200, {"ok": True, "category": self.repo.save_expense_category(body)}
        if path == "/api/expense-category/delete":
            self.repo.delete_expense_category(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/fixed-cost/save":
            return 200, {"ok": True, "fixed_cost": self.repo.save_fixed_cost(body)}
        if path == "/api/fixed-cost/delete":
            self.repo.delete_fixed_cost(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/fixed-costs/run":
            created = self.acc.run_fixed_costs()
            return 200, {"ok": True, "created": created, "count": len(created)}
        if path == "/api/payment/save":
            payment = self.acc.add_payment(
                body.get("order_id", ""), num(body.get("amount")),
                body.get("kind", "payment"), body.get("account_id", ""),
                body.get("method", ""), body.get("note", ""),
                body.get("request_id", ""), body.get("expected_updated_at", ""))
            return 200, {"ok": True, "payment": payment,
                         "order": self.repo.order(body.get("order_id", ""))}
        if path == "/api/payment/delete":
            self.repo.delete_payment(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/tax/pay":
            if num(body.get("amount")) <= 0:
                raise ValueError("Сумма платежа должна быть больше нуля")
            tx = self.acc.add_transaction(
                "expense", body.get("category", "tax"), num(body.get("amount")),
                body.get("title") or "Уплата налога", body.get("note", ""),
                account_id=body.get("account_id", ""), deductible=False)
            return 200, {"ok": True, "transaction": tx, "tax": self.acc.tax_report()}

        if path == "/api/calc/cost":
            return 200, self.acc.cost_breakdown(
                num(body.get("grams")), num(body.get("hours")),
                num(body.get("spool_price")) or None, num(body.get("spool_weight")) or None,
                num(body.get("manual_minutes")), num(body.get("qty"), 1),
                num(body.get("design_minutes")), num(body.get("delivery")),
                num(body.get("color_swaps")),
                material=body.get("material", ""),
                quality=body.get("quality", "standard"),
                supports_pct=num(body.get("supports_pct")),
                plate_grams=num(body.get("plate_grams")),
                plate_hours=num(body.get("plate_hours")),
                fit_per_plate=num(body.get("fit_per_plate")),
                warmup_minutes=num(body.get("warmup_minutes")),
                remove_minutes=num(body.get("remove_minutes")),
                sand_minutes=num(body.get("sand_minutes")),
                paint_minutes=num(body.get("paint_minutes")),
                model_prep_minutes=num(body.get("model_prep_minutes")))
        if path == "/api/calc/price":
            cost = num(body.get("cost"))
            breakdown = None
            if cost <= 0 and (num(body.get("grams")) or num(body.get("hours"))):
                # цену можно спросить сразу по граммам и часам, без ручного расчёта
                breakdown = self.acc.cost_breakdown(
                    num(body.get("grams")), num(body.get("hours")),
                    num(body.get("spool_price")) or None, num(body.get("spool_weight")) or None,
                    num(body.get("manual_minutes")), num(body.get("qty"), 1),
                    num(body.get("design_minutes")), num(body.get("delivery")))
                cost = num(breakdown.get("per_unit")) or num(breakdown.get("total"))
            result = self.acc.suggest_price(
                cost, num(body.get("qty"), 1),
                body.get("channel", ""), bool(body.get("rush")))
            result["cost_per_unit"] = round(cost, 2)
            if breakdown is not None:
                result["breakdown"] = breakdown
            return 200, result
        if path == "/api/calc/order":
            # можно прислать либо весь заказ, либо только {"id": "..."} —
            # во втором случае берём актуальные данные из базы
            order = dict(body)
            if body.get("id"):
                saved = self.db.one("SELECT * FROM orders WHERE id=?", (body["id"],))
                if not saved:
                    raise ValueError("Заказ не найден")
                saved.update({k: v for k, v in body.items() if k != "id" and v not in ("", None)})
                order = saved
            return 200, self.acc.order_economics(order)
        if path == "/api/calc/materials":
            return 200, self.acc.material_options()
        if path == "/api/calc/scenarios":
            return 200, {"scenarios": self.acc.calc_scenarios(
                body.get("base", {}), body.get("variants", []))}
        if path == "/api/calc/payback":
            return 200, self.acc.payback_calc(
                num(body.get("model_cost")),
                num(body.get("design_hours")),
                num(body.get("profit_per_unit")),
                num(body.get("sales_per_week"), 1))
        if path == "/api/calc/real-stats":
            return 200, self.acc.real_stats(
                body.get("product", ""),
                body.get("material", ""),
                int(num(body.get("days"), 60)))
        if path == "/api/calc/min-batch":
            return 200, self.acc.min_profitable_batch(
                plate_grams=num(body.get("plate_grams")),
                plate_hours=num(body.get("plate_hours")),
                fit_per_plate=num(body.get("fit_per_plate"), 1),
                material=body.get("material", ""),
                quality=body.get("quality", "standard"),
                supports_pct=num(body.get("supports_pct")),
                target_per_hour=num(body.get("target_per_hour")),
                spool_price=num(body.get("spool_price")) or None,
                markup=num(body.get("markup")))
        # -------------------------------------------------------- 6.0: модели
        if path == "/api/model/save":
            from .model_registry import ModelRegistry
            mr = ModelRegistry(self.db)
            return 200, {"ok": True, "model": mr.save(body)}
        if path == "/api/model/delete":
            from .model_registry import ModelRegistry
            mr = ModelRegistry(self.db)
            mr.delete(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/model/prep-start":
            from .model_registry import ModelRegistry
            mr = ModelRegistry(self.db)
            return 200, mr.start_prep_session(
                body.get("model_id", ""), body.get("nom_id", ""),
                body.get("order_id", ""))
        if path == "/api/model/prep-finish":
            from .model_registry import ModelRegistry
            mr = ModelRegistry(self.db)
            return 200, mr.finish_prep_session(
                body.get("session_id", ""), body.get("stages"),
                body.get("note", ""))
        if path == "/api/model/repeat":
            from .model_registry import ModelRegistry
            mr = ModelRegistry(self.db)
            return 200, mr.repeat_from_model(
                body.get("model_id", ""), int(num(body.get("qty"), 1)),
                body.get("printer_id", ""))
        if path == "/api/model/clone-batch":
            from .model_registry import ModelRegistry
            mr = ModelRegistry(self.db)
            return 200, mr.clone_batch(
                body.get("batch_id", ""), int(num(body.get("qty"), 0)))
        if path == "/api/analytics/investment":
            from .analytics import Analytics
            return 200, Analytics(self.db).investment_calc(
                num(body.get("printer_cost")),
                num(body.get("extra_hours_month")),
                num(body.get("profit_per_hour")),
                num(body.get("extra_costs_month")))

        # --- настройки, бэкап, уведомления
        # --- стеллаж магазина
        if path == "/api/shelf/save":
            return 200, {"ok": True, "item": self.shelf.save_item(body)}
        if path == "/api/shelf/delete":
            self.shelf.delete_item(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/shelf/produce":
            return 200, self.shelf.produce(body.get("item_id", ""), num(body.get("qty")),
                                           body.get("job_id", ""), body.get("note", ""),
                                           num(body.get("cost_per_unit")))
        if path == "/api/shelf/transfer":
            # Перемещение готового товара со склада на стеллаж: только
            # целые штуки и только в пределах остатка (минимум 1).
            return 200, self.shelf.transfer_from_stock(
                body.get("nom_id", ""), body.get("warehouse_id", ""),
                num(body.get("qty")), body.get("item_id", ""),
                body.get("note", ""))
        if path == "/api/shelf/save-from-stock":
            # Новая позиция стеллажа сразу с готовым товаром со склада:
            # создание позиции и перенос штук — одной операцией.
            return 200, self.shelf.create_item_from_stock(
                body, body.get("nom_id", ""), body.get("warehouse_id", ""),
                num(body.get("qty")))
        if path == "/api/shelf/sale":
            return 200, self.shelf.sale(body.get("item_id", ""), num(body.get("qty")),
                                        num(body.get("price")), body.get("channel", "shelf"),
                                        body.get("note", ""))
        if path == "/api/shelf/1c/sale":
            return 200, self.shelf.sale_from_1c(
                body.get("barcode") or body.get("code") or "",
                num(body.get("qty")), body.get("external_id", ""),
                num(body.get("price")))
        if path == "/api/shelf/sales":
            return 200, {"ok": True, "results": self.shelf.sales_many(
                body.get("rows") or [], body.get("channel", "shelf"))}
        if path == "/api/shelf/writeoff":
            return 200, self.shelf.writeoff(body.get("item_id", ""), num(body.get("qty")),
                                            body.get("note", "Списание"))
        if path == "/api/shelf/inventory":
            return 200, self.shelf.inventory(body.get("item_id", ""), num(body.get("actual")),
                                             body.get("note", ""))
        if path == "/api/shelf/photo":
            if not body.get("id") or not body.get("data"):
                raise ValueError("Нужны id позиции и фото (data URL)")
            return 200, self.shelf_save_photo(body["id"], str(body["data"]))

        # --- брак, фото заказа, шаблоны, AMS-профили, отложенные команды
        # ------------------------------------------------ учёт 3.0: номенклатура
        if path == "/api/nomenclature/save":
            return 200, {"ok": True, "item": self.nom.save(body)}
        if path == "/api/nomenclature/delete":
            self.nom.delete(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/nomenclature/archive":
            return 200, {"ok": True, "item": self.nom.archive(
                body.get("id", ""), bool(body.get("archived", True)))}
        if path == "/api/nomenclature/price":
            return 200, {"ok": True, "price": self.nom.set_price(
                body.get("nom_id", ""), num(body.get("price")),
                body.get("price_type_id", ""), body.get("note", ""))}
        if path == "/api/nomenclature/recalc-price":
            nom_id = str(body.get("nom_id") or body.get("id") or "").strip()
            if not nom_id or nom_id.startswith("[object "):
                raise ValueError("Не указана позиция для пересчёта")
            return 200, self.nom.recalc_price(
                nom_id, body.get("price_type_id", ""))
        if path == "/api/nomenclature/recalc-prices":
            return 200, self.nom.recalc_prices(body.get("price_type_id", ""),
                                               body.get("group_id", ""))
        if path == "/api/nomenclature/update-cost":
            return 200, self.nom.update_cost_from_batch(body.get("nom_id", ""))
        if path == "/api/nomenclature/photo":
            item_id = body.get("id", "")
            from .config import PHOTO_DIR
            import base64
            data_url = body.get("data", "")
            if "," not in data_url:
                return 400, {"error": "Не похоже на data URL"}
            head, _, b64 = data_url.partition(",")
            ext = "png" if "png" in head else "jpg"
            raw = base64.b64decode(b64)
            if len(raw) > 8 * 1024 * 1024:
                return 400, {"error": "Фото больше 8 МБ"}
            PHOTO_DIR.mkdir(parents=True, exist_ok=True)
            name = f"nom_{item_id}.{ext}"
            (PHOTO_DIR / name).write_bytes(raw)
            self.db.execute("UPDATE nomenclature SET photo=?, updated_at=? WHERE id=?",
                            (name, now_iso(), item_id))
            return 200, {"ok": True, "photo": name}
        if path == "/api/nomenclature/group/save":
            return 200, {"ok": True, "group": self.nom.save_group(body)}
        if path == "/api/nomenclature/group/delete":
            self.nom.delete_group(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/nomenclature/variant/save":
            return 200, {"ok": True, "variant": self.nom.save_variant(body)}
        if path == "/api/nomenclature/variant/delete":
            self.nom.delete_variant(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/spec/save":
            return 200, {"ok": True, "spec": self.nom.save_spec(body)}
        if path == "/api/spec/delete":
            self.nom.delete_spec(body.get("id", ""))
            return 200, {"ok": True}
        # ------------------------------------------------------------- склады
        if path == "/api/warehouse/save":
            # Фронтенд шлёт пустой id для новой записи — setdefault его не заменит.
            if not body.get("id"):
                body["id"] = uid("wh")
                row = self.db.one(
                    "SELECT COALESCE(MAX(position),0) p FROM warehouses") or {}
                body.setdefault("position", int(num(row.get("p"))) + 1)
            if not (body.get("name") or "").strip():
                return 400, {"error": "Укажите название склада"}
            return 200, {"ok": True, "warehouse": self.db.upsert("warehouses", body)}
        if path == "/api/warehouse/delete":
            wid = body.get("id", "")
            left = self.db.one(
                "SELECT COALESCE(SUM(qty),0) v FROM stock_moves WHERE warehouse_id=?",
                (wid,)) or {}
            if abs(num(left.get("v"))) > 0.001:
                return 400, {"error": "На складе есть остатки — сначала переместите их"}
            self.db.execute("UPDATE warehouses SET archived=1 WHERE id=?", (wid,))
            return 200, {"ok": True}
        if path == "/api/price-type/save":
            if not body.get("id"):
                body["id"] = uid("pt")
                row = self.db.one(
                    "SELECT COALESCE(MAX(position),0) p FROM price_types") or {}
                body.setdefault("position", int(num(row.get("p"))) + 1)
            if not (body.get("name") or "").strip():
                return 400, {"error": "Укажите название типа цен"}
            if body.get("is_base"):
                self.db.execute("UPDATE price_types SET is_base=0")
            return 200, {"ok": True, "price_type": self.db.upsert("price_types", body)}
        if path == "/api/price-type/delete":
            self.db.execute("UPDATE price_types SET archived=1 WHERE id=?",
                            (body.get("id", ""),))
            return 200, {"ok": True}
        # ------------------------------------------------------------ резервы
        if path == "/api/reserve":
            return 200, {"ok": True, "reserve": self.stock.reserve(
                body.get("nom_id", ""), num(body.get("qty")), body.get("order_id", ""),
                body.get("warehouse_id", ""), body.get("note", ""),
                body.get("variant_id", ""))}
        if path == "/api/reserve/release":
            return 200, {"ok": True, "released": self.stock.release(
                body.get("id", ""), body.get("order_id", ""))}
        # ---------------------------------------------------------- документы
        if path == "/api/document/save":
            return 200, {"ok": True, "document": self.docs.save(body)}
        if path == "/api/document/post":
            return 200, {"ok": True, "document": self.docs.post(body.get("id", ""))}
        if path == "/api/document/unpost":
            return 200, {"ok": True, "document": self.docs.unpost(body.get("id", ""))}
        if path == "/api/document/delete":
            self.docs.delete(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/sale/quick":
            return 200, {"ok": True, "document": self.docs.quick_sale(
                body.get("rows") or [], body.get("warehouse_id", ""),
                body.get("channel", "shop"), body.get("account_id", ""),
                body.get("note", ""), num(body.get("discount")))}
        if path == "/api/receipt/quick":
            return 200, {"ok": True, "document": self.docs.quick_receipt(
                body.get("nom_id", ""), num(body.get("qty")), num(body.get("cost")),
                body.get("warehouse_id", ""), body.get("batch_id", ""),
                body.get("note", ""))}
        # ------------------------------------------------------------- партии
        if path == "/api/batch/plan":
            if isinstance(body.get("items"), list) and body.get("items"):
                return 200, self.batches.plan_multi(
                    body.get("items"), num(body.get("plates"), 1),
                    body.get("printer_id", ""), body.get("spool_id", ""),
                    body.get("file", ""))
            return 200, self.batches.plan(
                body.get("nom_id", ""), num(body.get("qty")), body.get("mode", "full"),
                int(num(body.get("plates"))), body.get("printer_id", ""),
                body.get("spool_id", ""), num(body.get("price")))
        if path == "/api/batch/create":
            if body.get("start_now") and body.get("confirmed") is not True:
                raise ValueError("Подтвердите физический запуск партии")
            return 200, {"ok": True, "batch": self.batches.create(body)}
        if path == "/api/batch/receive":
            return 200, {"ok": True, "batch": self.batches.receive(
                body.get("id", ""), num(body.get("qty")), num(body.get("scrap")),
                body.get("job_id", ""), num(body.get("cost")), body.get("note", ""),
                body.get("items"))}
        if path == "/api/batch/cancel":
            return 200, {"ok": True, "batch": self.batches.cancel(body.get("id", ""))}
        if path == "/api/batch/repeat":
            if body.get("start_now") and body.get("confirmed") is not True:
                raise ValueError("Подтвердите физический запуск повтора партии")
            return 200, {"ok": True, "batch": self.batches.repeat(
                body.get("id", ""), bool(body.get("start_now")))}
        if path == "/api/batch/from-plan":
            if body.get("start_now") and body.get("confirmed") is not True:
                raise ValueError("Подтвердите физический запуск партий из плана")
            return 200, {"ok": True, "batches": self.batches.create_from_plan(
                body.get("rows") or [], body.get("warehouse_id", ""),
                bool(body.get("start_now")))}
        if path == "/api/defect/recover":
            return 200, self.defect_recovery.recover(
                str(body.get("id") or body.get("job_id") or ""),
                defect_confirmed=body.get("defect_confirmed") is True,
                reason=str(body.get("reason") or ""),
                phase=str(body.get("phase") or "unknown"),
                code=str(body.get("code") or ""),
                note=str(body.get("note") or ""),
                lost_grams=num(body.get("grams")),
                reprint_confirmed=body.get("reprint_confirmed") is True,
                repeat_risk_confirmed=body.get("repeat_risk_confirmed") is True,
                printer_id=str(body.get("printer_id") or ""),
                request_id=str(body.get("request_id") or ""),
            )
        if path == "/api/defect/save":
            # Совместимый адрес: связанный с печатью брак всегда проходит
            # через расчёт по фактам и идемпотентное подтверждение.
            if body.get("job_id"):
                result = self.defect_recovery.recover(
                    str(body.get("job_id") or ""),
                    defect_confirmed=body.get("defect_confirmed") is True,
                    reason=str(body.get("reason") or ""),
                    phase=str(body.get("phase") or "unknown"),
                    code=str(body.get("code") or ""),
                    note=str(body.get("note") or ""),
                    lost_grams=num(body.get("grams")),
                    request_id=str(body.get("request_id") or ""),
                )
                return 200, {"ok": True, "defect": result["defect"]}
            if body.get("defect_confirmed") is not True:
                raise ValueError("Подтвердите причину и фактический брак")
            request_id = str(body.get("request_id") or "").strip()[:120]
            if not request_id:
                raise ValueError("Не указан ключ операции разбора брака")
            existing = self.db.one("SELECT * FROM defects WHERE request_id=?", (request_id,))
            if existing:
                return 200, {"ok": True, "defect": existing, "already_recorded": True}
            data = dict(body)
            data.setdefault("id", uid("df"))
            data.setdefault("at", now_iso())
            data["confirmed_at"] = now_iso()
            data["request_id"] = request_id
            row = self.db.upsert("defects", data)
            self.db.add_event(
                "defect", "Записан брак без задания",
                f"{row.get('reason') or ''} · {row.get('code') or ''}",
                row.get("printer_id") or "",
                {"defect_id": row["id"], "order_id": row.get("order_id")},
            )
            return 200, {"ok": True, "defect": row, "already_recorded": False}
        if path == "/api/defect/delete":
            defect = self.db.one("SELECT * FROM defects WHERE id=?", (body.get("id", ""),))
            if defect and defect.get("confirmed_at"):
                raise ValueError("Подтверждённый разбор брака нельзя удалить из аудита")
            self.db.delete("defects", body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/order/photo":
            if not body.get("order_id") or not body.get("data"):
                raise ValueError("Нужны order_id и фото (data URL)")
            return 200, self.order_save_photo(body["order_id"], str(body["data"]),
                                              body.get("note", ""), body.get("kind", "upload"))
        if path == "/api/order/photo/delete":
            self.db.delete("order_photos", body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/templates/save":
            templates = body.get("templates")
            if not isinstance(templates, list):
                raise ValueError("Ожидается список шаблонов")
            self.db.set_settings({"reply_templates": json.dumps(
                [{"id": t.get("id") or uid("tmpl"), "title": t.get("title", ""),
                  "text": t.get("text", "")} for t in templates if t.get("text")],
                ensure_ascii=False)})
            return 200, {"ok": True, "templates": self._templates()}
        if path == "/api/ams-profile/save":
            data = dict(body)
            if not data.get("name"):
                raise ValueError("Укажите название профиля")
            if not data.get("id"):
                data["id"] = uid("ap")
                data.setdefault("created_at", now_iso())
            if isinstance(data.get("slots"), list):
                data["slots"] = json.dumps(data["slots"], ensure_ascii=False)
            row = self.db.upsert("ams_profiles", data)
            return 200, {"ok": True, "profile": row}
        if path == "/api/ams-profile/delete":
            self.db.delete("ams_profiles", body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/ams-profile/apply":
            if body.get("confirmed") is not True:
                raise ValueError("Подтвердите отправку профиля в AMS")
            profile = self.db.one("SELECT * FROM ams_profiles WHERE id=?", (body.get("id", ""),))
            if not profile:
                raise ValueError("Профиль не найден")
            printer = self.printer_or_fail(body.get("printer_id") or "")
            try:
                slots = json.loads(profile.get("slots") or "[]")
            except json.JSONDecodeError:
                slots = []
            sent = 0
            for slot in slots:
                if not isinstance(slot, dict) or slot.get("type") not in (None, ""):
                    try:
                        printer.command("ams_filament", {"ams_id": 0, "tray_id": int(num(slot.get("tray"))),
                                                          "type": slot.get("type", "PLA"),
                                                          "color": slot.get("color", "FFFFFFFF")})
                        sent += 1
                    except Exception:
                        continue
            self.db.add_event("ams", "Профиль AMS применён", f"{profile['name']} · слотов: {sent}",
                              printer.id, {"profile_id": profile["id"], "sent": sent})
            return 200, {"ok": True, "sent": sent}
        if path == "/api/schedule/command":
            command = str(body.get("command") or "").strip()
            if not command or not body.get("at"):
                raise ValueError("Нужны команда и время")
            if (command in DANGEROUS_AUTOMATION_COMMANDS
                    and not self.db.setting("unattended_dangerous_actions", False)):
                raise ValueError(
                    "Команда заблокирована safety-gate: включите явное разрешение опасных автоматических действий"
                )
            self.db.upsert("scheduled_commands", {
                "id": uid("sch"), "at": body["at"], "printer_id": body.get("printer_id") or "",
                "command": body["command"], "value": json.dumps(body.get("value"), ensure_ascii=False),
                "note": body.get("note", ""), "done": 0, "created_at": now_iso()})
            self.db.add_event("command", "Отложенная команда", f"{body['command']} · {body.get('note') or ''}",
                              body.get("printer_id") or "", {})
            return 200, {"ok": True}
        if path == "/api/schedule/delete":
            self.db.delete("scheduled_commands", body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/spool/dry":
            spool = self.db.one("SELECT * FROM spools WHERE id=?", (body.get("id", ""),))
            if not spool:
                raise ValueError("Катушка не найдена")
            self.db.upsert("drying_sessions", {
                "id": uid("dry"), "at": now_iso(), "spool_id": spool["id"],
                "material": spool.get("material", ""), "color_name": spool.get("color_name", ""),
                "minutes": num(body.get("minutes")), "temp": num(body.get("temp")),
                "note": body.get("note", "")})
            self.db.add_event("dry", "Сушка пластика", f"{spool.get('material')} {spool.get('color_name')} · {round(num(body.get('minutes')))} мин",
                              spool.get("printer_id") or "", {"spool_id": spool["id"]})
            return 200, {"ok": True}
        if path == "/api/spool/bind":
            spool_id = str(body.get("id") or "").strip()
            if not spool_id or "ams_slot" not in body:
                raise ValueError("Нужны id катушки и слот AMS")
            spool = self.repo.spool(spool_id)
            if not spool:
                raise ValueError("Катушка не найдена")
            raw_slot = body.get("ams_slot")
            slot = "" if raw_slot in (None, "") else str(raw_slot).strip()
            printer_id = str(body.get("printer_id") or spool.get("printer_id") or "").strip()
            tray_uuid = str(body.get("tray_uuid") or "").strip()
            push_ams = body.get("push_ams") not in (False, 0, "0", "false")
            if not slot:
                self.db.execute(
                    "UPDATE spools SET printer_id=?, ams_slot='', tray_uuid='', updated_at=? WHERE id=?",
                    (printer_id or None, now_iso(), spool_id))
                self.db.add_event("spool", "Катушка отвязана от AMS",
                                  f"{spool.get('material')} {spool.get('color_name')}",
                                  printer_id, {"spool_id": spool_id})
                return 200, {"ok": True, "spool": self.repo.spool(spool_id) or {}}
            try:
                slot_n = int(float(slot))
            except (TypeError, ValueError) as exc:
                raise ValueError("Слот AMS: 0–15") from exc
            if not 0 <= slot_n <= 15:
                raise ValueError("Слот AMS: 0–15")
            slot = str(slot_n)
            self.db.execute(
                "UPDATE spools SET printer_id=?, ams_slot=?, tray_uuid=?, updated_at=? WHERE id=?",
                (printer_id or None, slot, tray_uuid, now_iso(), spool_id))
            pushed, push_error = False, ""
            manager = getattr(self, "manager", None)
            printer = manager.get(printer_id) if manager and printer_id else None
            if push_ams and printer and body.get("confirmed") is not True:
                raise ValueError("Подтвердите отправку материала в AMS")
            if push_ams and printer:
                try:
                    printer.command("ams_filament", {
                        "ams_id": slot_n // 4, "tray_id": slot_n % 4,
                        "type": spool.get("material") or "PLA",
                        "color": spool.get("color_hex") or "FFFFFFFF",
                    })
                    pushed = True
                except Exception as exc:
                    push_error = str(exc)
            self.db.add_event("spool", "Катушка привязана к слоту AMS",
                              f"{spool.get('material')} {spool.get('color_name')} → слот {slot}",
                              printer_id, {"spool_id": spool_id, "pushed": pushed})
            result = {"ok": True, "spool": self.repo.spool(spool_id) or {}, "pushed": pushed}
            if push_error:
                result["push_error"] = push_error
            return 200, result
        if path == "/api/update-check":
            return 200, self.updater.report(force=True)
        if path == "/api/update/apply":
            result = self.updater.apply(force=bool(body.get("force")))
            if result.get("restart_required") and body.get("restart", True):
                # Ответ уходит раньше перезапуска — интерфейс успеет его получить
                # и сам дождётся, пока коннектор снова поднимется.
                self.updater.restart(delay=1.5)
                result["restarting"] = True
            return 200, result
        if path == "/api/update/restart":
            self.updater.restart(delay=1.0)
            return 200, {"ok": True, "restarting": True}

        if path == "/api/settings":
            patch = dict(body)
            # bank_rules приходит из textarea строкой JSON — превращаем в список
            if "bank_rules" in patch and isinstance(patch["bank_rules"], str):
                try:
                    parsed = json.loads(patch["bank_rules"] or "[]")
                    patch["bank_rules"] = parsed if isinstance(parsed, list) else []
                except json.JSONDecodeError:
                    patch["bank_rules"] = []
            settings = self.db.set_settings(patch)
            if set(patch) & {"ftps_timeout", "ftps_retries", "ftps_block_kb",
                             "mqtt_keepalive", "mqtt_backoff"}:
                self.manager.reload()
            return 200, {"ok": True, "settings": settings}
        if path == "/api/shopping/add":
            return 200, {"ok": True, "item": self.shopping.add(body)}
        if path == "/api/shopping/toggle":
            return 200, {"ok": True, "item": self.shopping.toggle(
                body.get("id", ""), bool(body.get("done", True)))}
        if path == "/api/shopping/receive":
            return 200, self.shopping.receive(
                str(body.get("id") or ""),
                received_confirmed=body.get("received_confirmed") is True,
                payment_confirmed=body.get("payment_confirmed") is True,
                material=str(body.get("material") or ""),
                color_name=str(body.get("color_name") or ""),
                color_hex=str(body.get("color_hex") or ""),
                brand=str(body.get("brand") or ""),
                spool_count=num(body.get("spool_count"), 1),
                spool_grams=num(body.get("spool_grams"), 1000),
                total_amount=num(body.get("total_amount")),
                account_id=str(body.get("account_id") or ""),
                supplier=str(body.get("supplier") or ""),
                warehouse_id=str(body.get("warehouse_id") or ""),
                request_id=str(body.get("request_id") or ""),
            )
        if path == "/api/shopping/delete":
            self.shopping.delete(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/shopping/auto":
            return 200, self.shopping.auto_fill(bool(body.get("dry_run")))
        if path == "/api/shopping/clear-done":
            return 200, {"ok": True, "removed": self.shopping.clear_done()}
        if path == "/api/rules/save":
            return 200, {"ok": True, "rule": self.manager.rules.save_rule(body)}
        if path == "/api/rules/delete":
            self.manager.rules.delete_rule(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/rules/toggle":
            return 200, {"ok": True, "rule": self.manager.rules.toggle(
                body.get("id", ""), bool(body.get("enabled", True)))}
        if path == "/api/rules/run":
            # Ручной тест правила: выполнить его сейчас (для проверки шаблона).
            rule = self.db.one("SELECT * FROM automation_rules WHERE id=?",
                               (body.get("id", ""),))
            if not rule:
                raise ValueError("Правило не найдено")
            try:
                rule["config"] = json.loads(rule.get("config") or "{}")
            except json.JSONDecodeError:
                rule["config"] = {}
            fired = self.manager.rules.run(str(rule["event"]), {
                "name": "тест", "detail": "проверка правила", "printer": "PrintFlow",
                "order": "1001", "number": "1001", "product": "тест",
                "material": "PLA", "color": "чёрный", "grams": 50, "pct": 10,
                "status": (rule.get("config") or {}).get("status", ""),
                "total": 1000, "count": 1, "days": 14})
            return 200, {"ok": True, "fired": [r["id"] for r in fired]}
        if path == "/api/settings/reset":
            return 200, {"ok": True, "settings": self.repo.reset_settings(body.get("keys"))}
        if path == "/api/system/backup":
            from .db import make_backup
            result = make_backup()
            if result.get("ok"):
                self.db.add_event("backup", "Ручная копия базы", result["file"], "", {})
            return 200, result
        if path == "/api/system/restore":
            from .db import request_restore
            result = request_restore(str(body.get("file") or ""))
            self.db.add_event("backup", "Запрошен откат базы",
                              f"из копии {result['file']}; приложение перезапустится",
                              "", result)
            threading.Timer(1.5, self.restart_process).start()
            return 200, {**result, "restarting": True}
        if path == "/api/month-close/step":
            result = self.month_close.run(
                str(body.get("key") or ""), str(body.get("step") or ""),
                {"accounts": body.get("accounts") or []})
            self.db.add_event("finance", f"Закрытие месяца: {body.get('step')}",
                              str(result.get("message") or ""), "", result)
            self.bus.publish("resync", {})
            return 200, result
        if path == "/api/catalog/recalc-apply":
            result = self.acc.recalc_catalog(True)
            self.bus.publish("resync", {})
            return 200, result
        if path == "/api/orders/bulk-status":
            ids = [str(x) for x in (body.get("ids") or [])]
            status = str(body.get("status") or "")
            updated = 0
            for oid in ids:
                if not self.db.one("SELECT id FROM orders WHERE id=?", (oid,)):
                    continue
                self.repo.set_order_status(oid, status)
                updated += 1
            self.db.add_event("order", "Пакетная смена статуса",
                              f"{updated} заказов → {status}", "", {})
            self.bus.publish("resync", {})
            return 200, {"ok": True, "updated": updated}
        if path == "/api/debt/remind":
            # Предпросмотр ничего не отмечает отправленным: копирование текста
            # ещё не доказывает внешнюю отправку клиенту.
            result = self.receivables.summary(
                str(body.get("id") or body.get("order_id") or "")
            )
            return 200, {**result, "text": result.get("message") or ""}
        if path == "/api/debt/remind/confirm":
            return 200, self.receivables.mark_reminded(
                str(body.get("id") or body.get("order_id") or ""),
                sent_confirmed=body.get("sent_confirmed") is True,
                force=body.get("force") is True,
            )
        if path == "/api/debt/settle":
            return 200, self.receivables.settle(
                str(body.get("id") or body.get("order_id") or ""),
                payment_confirmed=body.get("payment_confirmed") is True,
                amount=num(body.get("amount")),
                account_id=str(body.get("account_id") or ""),
                payment_method=str(body.get("payment_method") or ""),
                request_id=str(body.get("request_id") or ""),
            )
        if path == "/api/bank/import-preview":
            from .bank_import import preview
            return 200, preview(self.db, str(body.get("text") or ""))
        if path == "/api/bank/import-apply":
            from .bank_import import apply_rows
            result = apply_rows(self.db, body.get("rows") or [])
            self.bus.publish("resync", {})
            return 200, result
        if path == "/api/telegram/test":
            self.db.set_settings({k: v for k, v in body.items() if k.startswith("telegram")})
            return 200, self.manager.send_telegram("PrintFlow: проверка уведомлений прошла успешно.")
        if path == "/api/staff/save":
            from .staff import Staff
            staff = Staff(self.db)
            member = staff.add(str(body.get("name") or ""),
                               str(body.get("role") or "employee"),
                               str(body.get("chat_id") or ""),
                               str(body.get("note") or ""))
            self.bus.publish("resync", {})
            return 200, {"ok": True, "member": member}
        if path == "/api/staff/delete":
            from .staff import Staff
            Staff(self.db).remove(str(body.get("id") or ""))
            self.bus.publish("resync", {})
            return 200, {"ok": True}
        if path == "/api/staff/restore":
            from .staff import Staff
            member = Staff(self.db).restore(str(body.get("id") or ""))
            self.bus.publish("resync", {})
            return 200, {"ok": True, "member": member}
        if path == "/api/staff/invite":
            from .staff import Staff
            invite = Staff(self.db).invite(str(body.get("role") or "employee"),
                                           str(body.get("name") or ""),
                                           "panel")
            return 200, {"ok": True, "invite": invite}
        if path == "/api/staff/invite/delete":
            from .staff import Staff
            Staff(self.db).invite_delete(str(body.get("code") or ""))
            return 200, {"ok": True}
        if path == "/api/client-bot/template/save":
            client = getattr(self.manager, "client_bot", None)
            if not client:
                raise ValueError("Клиентский бот не запущен")
            template = client.save_template(
                str(body.get("id") or ""), str(body.get("name") or ""),
                str(body.get("text") or ""))
            self._audit("client_template", template["id"], "save",
                        "Сохранён шаблон ответа", template["name"], actor=str(body.get("actor") or "panel"))
            return 200, {"ok": True, "template": template, "templates": client.templates()}
        if path == "/api/client-bot/template/delete":
            client = getattr(self.manager, "client_bot", None)
            if not client:
                raise ValueError("Клиентский бот не запущен")
            ident = str(body.get("id") or "")
            client.delete_template(ident)
            self._audit("client_template", ident, "delete", "Удалён шаблон ответа", actor=str(body.get("actor") or "panel"))
            return 200, {"ok": True, "templates": client.templates()}
        if path == "/api/client-bot/reply":
            client = getattr(self.manager, "client_bot", None)
            target = str(body.get("chat_id") or "").strip()
            message = str(body.get("text") or body.get("message") or "").strip()
            actor = str(body.get("actor") or body.get("operator") or "panel")[:120]
            if not client:
                raise ValueError("Клиентский бот не запущен")
            if not target or not target.lstrip("-").isdigit():
                raise ValueError("Укажите chat_id покупателя")
            if not message:
                raise ValueError("Введите текст ответа")
            if len(message) > 3800:
                raise ValueError("Ответ длиннее 3800 символов")
            if not self.db.one("SELECT chat_id FROM client_chats WHERE chat_id=?", (target,)):
                raise ValueError("Чат покупателя не найден")
            request_id = str(body.get("request_id") or uid("panel-reply"))[:160]
            cached = self.db.one("SELECT entity_id FROM idempotency_keys WHERE request_id=? AND kind='client_reply'",
                                 (request_id,))
            if cached:
                return 200, {"ok": True, "already_recorded": True, "chat_id": target}
            client._reply_keyed(target, message, client._menu(),
                                dedupe_key=f"panel-reply:{request_id}")
            client._log(target, (self.db.one("SELECT name FROM client_chats WHERE chat_id=?", (target,)) or {}).get("name") or "",
                        "← мастер", message, kind="answer", direction="out", unread=0,
                        operator=actor)
            self.db.execute("INSERT OR IGNORE INTO idempotency_keys(request_id,kind,entity_id,response,created_at) VALUES(?,?,?,?,?)",
                            (request_id, "client_reply", target, "{}", now_iso()))
            self._audit("client_chat", target, "reply", "Ответ покупателю",
                        message[:400], {"request_id": request_id}, actor)
            self.db.add_event("bot", "Ответ покупателю из панели", message[:200], "",
                              {"chat_id": target, "actor": actor})
            return 200, {"ok": True, "queued": True, "chat_id": target}
        if path in ("/api/client-bot/read", "/api/client-bot/chat/read"):
            target = str(body.get("chat_id") or "").strip()
            if not target:
                raise ValueError("Укажите chat_id")
            self.db.execute("UPDATE client_bot_log SET unread=0 WHERE chat_id=? AND direction='in'",
                            (target,))
            self.db.execute("UPDATE client_chats SET inbox_status='open' WHERE chat_id=?", (target,))
            self._audit("client_chat", target, "read", "Диалог отмечен прочитанным", actor=str(body.get("actor") or "panel"))
            return 200, {"ok": True, "chat_id": target}
        if path == "/api/client-bot/chat/status":
            target = str(body.get("chat_id") or "").strip()
            status = str(body.get("status") or "open").strip().lower()
            if status not in {"open", "pending", "closed"}:
                raise ValueError("Статус диалога: open, pending или closed")
            if not target:
                raise ValueError("Укажите chat_id")
            self.db.execute("UPDATE client_chats SET inbox_status=?,assigned_to=? WHERE chat_id=?",
                            (status, str(body.get("assigned_to") or "")[:120], target))
            self._audit("client_chat", target, "status", "Статус диалога изменён", status,
                        actor=str(body.get("actor") or "panel"))
            return 200, {"ok": True, "chat_id": target, "status": status}
        if path == "/api/client-bot/payment":
            intent_id = str(body.get("intent_id") or body.get("id") or "").strip()
            action = str(body.get("action") or body.get("status") or "confirm").strip().lower()
            actor = str(body.get("actor") or body.get("operator") or "panel")[:120]
            intent = self.db.one("SELECT * FROM client_payment_intents WHERE id=?", (intent_id,))
            if not intent:
                raise ValueError("Заявка оплаты не найдена")
            if action not in {"confirm", "confirmed", "reject", "rejected"}:
                raise ValueError("Действие: confirm или reject")
            if intent.get("status") in {"confirmed", "rejected"}:
                return 200, {"ok": True, "already_recorded": True, "intent": intent}
            client = getattr(self.manager, "client_bot", None)
            if action in {"reject", "rejected"}:
                reason = str(body.get("reason") or "Оплата не подтверждена").strip()[:500]
                self.db.execute("UPDATE client_payment_intents SET status='rejected',reject_reason=?,confirmed_at=?,confirmed_by=?,updated_at=? WHERE id=?",
                                (reason, now_iso(), actor, now_iso(), intent_id))
                if client:
                    client._reply_keyed(intent["chat_id"],
                                        f"По заказу №{(self.db.one('SELECT number FROM orders WHERE id=?',(intent['order_id'],)) or {}).get('number') or ''} оплату пока не удалось подтвердить. {reason}",
                                        client._menu(), dedupe_key=f"payment:{intent_id}:rejected")
                self._audit("payment_intent", intent_id, "reject", "Отклонено подтверждение оплаты", reason, actor=actor)
                return 200, {"ok": True, "intent": self.db.one("SELECT * FROM client_payment_intents WHERE id=?", (intent_id,))}
            payment = self.acc.add_payment(
                intent["order_id"], num(intent.get("amount")), "payment",
                str(body.get("account_id") or ""),
                str(body.get("method") or "СБП (ручная сверка)"),
                f"Подтверждено по заявке клиента {intent_id}",
                request_id=f"client-intent:{intent_id}")
            self.db.execute("UPDATE client_payment_intents SET status='confirmed',confirmed_at=?,confirmed_by=?,payment_id=?,updated_at=? WHERE id=?",
                            (now_iso(), actor, payment.get("id") or "", now_iso(), intent_id))
            if client:
                order = self.db.one("SELECT * FROM orders WHERE id=?", (intent["order_id"],)) or {}
                client._reply_keyed(intent["chat_id"],
                                    f"Оплата по заказу №{order.get('number') or ''} подтверждена мастером ✓",
                                    client._menu(), dedupe_key=f"payment:{intent_id}:confirmed")
                client._funnel(intent["chat_id"], "payment_confirmed", source="telegram",
                               order_id=intent["order_id"], data={"intent_id": intent_id})
            self._audit("payment_intent", intent_id, "confirm", "Оплата подтверждена вручную",
                        f"{num(intent.get('amount')):g} RUB", {"payment_id": payment.get("id") or ""}, actor)
            self.db.add_event("finance", "Оплата клиента подтверждена",
                              f"заявка {intent_id}", "", {"order_id": intent["order_id"], "actor": actor})
            return 200, {"ok": True, "intent": self.db.one("SELECT * FROM client_payment_intents WHERE id=?", (intent_id,)),
                         "payment": payment, "order": self.repo.order(intent["order_id"])}
        if path == "/api/client-bot/review/resolve":
            order_id = str(body.get("order_id") or "").strip()
            chat_id = str(body.get("chat_id") or "").strip()
            if not order_id or not chat_id:
                raise ValueError("Укажите order_id и chat_id")
            note = str(body.get("note") or "").strip()[:1000]
            self.db.execute("UPDATE client_reviews SET state='resolved',resolved_at=?,operator_note=? WHERE order_id=? AND chat_id=?",
                            (now_iso(), note, order_id, chat_id))
            self._audit("client_review", f"{order_id}:{chat_id}", "resolve", "Отзыв отмечен обработанным", note,
                        actor=str(body.get("actor") or "panel"))
            return 200, {"ok": True}
        if path == "/api/client-bot/broadcast":
            if not bool(self.db.setting("client_bot_marketing_enabled", False)):
                raise ValueError("Рассылки выключены в настройках")
            if body.get("confirmed") is not True:
                raise ValueError("Подтвердите отправку рассылки")
            text = str(body.get("text") or "").strip()
            if not text:
                raise ValueError("Введите текст рассылки")
            request_id = str(body.get("request_id") or uid("broadcast"))[:160]
            client = getattr(self.manager, "client_bot", None)
            if not client:
                raise ValueError("Клиентский бот не запущен")
            with self.db.transaction():
                cached = self.db.one(
                    "SELECT response FROM client_broadcasts WHERE request_id=?",
                    (request_id,))
                if cached:
                    try:
                        response = json.loads(cached.get("response") or "{}")
                    except (TypeError, ValueError):
                        response = {"ok": True, "request_id": request_id}
                    response["already_recorded"] = True
                    return 200, response
                stamp = now_iso()
                self.db.execute(
                    "INSERT INTO client_broadcasts(request_id,text,audience,created_at,updated_at)"
                    " VALUES(?,?,?,?,?)",
                    (request_id, text[:3800], "marketing_opt_in", stamp, stamp))
                sent = skipped = 0
                for chat in self.db.query("SELECT * FROM client_chats WHERE marketing_opt_in=1"):
                    if client._in_quiet_hours(chat):
                        skipped += 1
                        continue
                    client._reply_keyed(chat["chat_id"], text, client._menu(),
                                        dedupe_key=f"broadcast:{request_id}:{chat['chat_id']}")
                    sent += 1
                response = {"ok": True, "sent": sent, "skipped": skipped,
                            "request_id": request_id}
                self.db.execute(
                    "UPDATE client_broadcasts SET response=?,updated_at=? WHERE request_id=?",
                    (json.dumps(response, ensure_ascii=False), now_iso(), request_id))
            self._audit("client_broadcast", request_id, "send", "Рассылка клиентам",
                        text[:300], {"sent": sent, "skipped": skipped}, str(body.get("actor") or "panel"))
            return 200, response
        if path == "/api/client-bot/fulfill":
            order_id = str(body.get("order_id") or body.get("id") or "").strip()
            actor = str(body.get("actor") or "panel")[:120]
            result = self.fulfill_order(
                order_id, str(body.get("account_id") or ""),
                handoff_confirmed=body.get("handoff_confirmed") is True,
                payment_action=str(body.get("payment_action") or ""),
                payment_method=str(body.get("payment_method") or ""))
            client = getattr(self.manager, "client_bot", None)
            if client and not result.get("already_fulfilled"):
                link = self.db.one("SELECT * FROM client_orders WHERE order_id=? ORDER BY datetime(created_at) LIMIT 1", (order_id,))
                if link:
                    order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,)) or {}
                    self.db.execute("UPDATE orders SET client_delivered_at=? WHERE id=? AND COALESCE(client_delivered_at,'')=''",
                                    (now_iso(), order_id))
                    client._reply_keyed(link["chat_id"],
                                        f"Заказ №{order.get('number') or ''} выдан ✓ Спасибо, что выбрали NOZZA!",
                                        client._menu(), dedupe_key=f"delivered:{order_id}")
                    client._funnel(link["chat_id"], "delivered", source=link.get("source") or "telegram", order_id=order_id)
            self._audit("order", order_id, "fulfill", "Подтверждена выдача клиентского заказа", actor=actor)
            return 200, result
        if path == "/api/client-bot/save":
            patch = {k: v for k, v in body.items()
                     if k in ("client_bot_enabled", "client_bot_token",
                              "client_bot_welcome", "client_bot_notify",
                              "client_bot_catalog", "client_bot_faq",
                              "client_bot_review", "client_bot_pickup_days",
                              "client_bot_pay_info", "client_bot_pay_qr",
                              "client_bot_payment_purpose", "client_bot_quiet_hours_enabled",
                              "client_bot_quiet_from", "client_bot_quiet_to",
                              "client_bot_marketing_enabled", "client_bot_track_url")}
            settings = self.db.set_settings(patch)
            return 200, {"ok": True, "settings": {
                k: v for k, v in settings.items() if k.startswith("client_bot")}}
        if path == "/api/client-bot/test":
            # Проверка токена без отправки сообщения: getMe показывает имя бота.
            self.db.set_settings({k: v for k, v in body.items()
                                  if k.startswith("client_bot")})
            bot = getattr(self.manager, "client_bot", None)
            if not bot:
                return 200, {"ok": False,
                             "error": "Клиентский бот не запущен (перезапустите панель)"}
            return 200, bot.me()
        if path == "/api/import":
            return 200, {"ok": True, "imported": self.repo.import_backup(body)}
        if path == "/api/import/localstorage":
            return 200, {"ok": True, "imported": self.repo.import_local_storage(body)}
        # 8.0: Watch / slicer
        if path == "/api/slicer/push":
            # from Bambu Studio post-processing: {file, plates, estimate}
            watch = getattr(self.manager, "watch", None)
            # just log event and return ok; watch will pick up file itself
            self.db.add_event("slicer", "Bambu Studio push", str(body.get("file","")), "", body)
            if self.manager and hasattr(self.manager, "watch") and body.get("file"):
                # trigger immediate scan
                try:
                    from pathlib import Path as _P
                    p=_P(str(body["file"]))
                    if p.exists() and watch:
                        watch._handle_file(p)
                except Exception:
                    pass
            return 200, {"ok": True}
        if path == "/api/watch/dismiss":
            watch = getattr(self.manager, "watch", None)
            if watch:
                watch.dismiss(body.get("fid",""))
            return 200, {"ok": True}
        if path == "/api/watch/enqueue":
            watch = getattr(self.manager, "watch", None)
            fid = body.get("fid","")
            info = watch.get_pending(fid) if watch else None
            if not info:
                raise ValueError("Файл не найден в Watch Folder")
            self.manager.enqueue({"file": info.get("name") or Path(info.get("file","")).name, "order_id": info.get("order_id") or body.get("order_id",""), "plate": int(num(body.get("plate"),1)), "ams_mapping": body.get("ams_mapping", []), "printer_id": body.get("printer_id","")})
            if watch:
                watch.dismiss(fid)
            return 200, {"ok": True}
        if path == "/api/watch/create-order":
            watch = getattr(self.manager, "watch", None)
            fid = body.get("fid","")
            info = watch.get_pending(fid) if watch else None
            if not info:
                raise ValueError("Файл не найден")
            order = watch._create_order_from_info(info.get("name",""), info)
            return 200, {"ok": True, "order": order}
        if path == "/api/printer/preflight":
            return 200, self.manager.preflight(body.get("printer_id",""), body.get("file",""), int(num(body.get("plate"),1)), body.get("ams_mapping") or body.get("mapping"))
        if path == "/api/printer/files/batch-delete":
            printer = self.printer_or_fail(pid or body.get("printer_id",""))
            paths = body.get("paths") or body.get("files") or []
            return 200, printer.files.batch_delete(paths)
        if path == "/api/printer/ams/auto-map":
            # required: [{type, color}] ; printer_id
            printer = self.printer_or_fail(pid or body.get("printer_id",""))
            snap = printer.snapshot()
            trays = snap["ams"].get("trays", [])
            req = body.get("required") or body.get("filaments") or []
            from .estimate import auto_ams_map
            mapping = auto_ams_map(req, trays)
            return 200, {"mapping": mapping, "trays": trays, "required": req}
        if path == "/api/settings/profile/save":
            name = (body.get("name") or "").strip() or f"Снапшот {now_iso()[:16]}"
            profiles = self.db.setting("settings_profiles", []) or []
            profiles = [p for p in profiles if isinstance(p, dict)]
            snap = {"id": uid("prof"), "name": name, "at": now_iso(), "settings": self.db.settings()}
            profiles.append(snap)
            profiles = profiles[-10:]  # keep last 10
            self.db.set_settings({"settings_profiles": profiles})
            return 200, {"ok": True, "profile": snap}
        if path == "/api/settings/profile/restore":
            pid2 = body.get("id","")
            profiles = self.db.setting("settings_profiles", []) or []
            prof = next((p for p in profiles if p.get("id")==pid2), None)
            if not prof:
                raise ValueError("Снапшот не найден")
            self.db.set_settings(prof.get("settings", {}))
            return 200, {"ok": True, "settings": self.db.settings()}
        if path == "/api/settings/profile/delete":
            pid2 = body.get("id","")
            profiles = [p for p in (self.db.setting("settings_profiles", []) or []) if p.get("id")!=pid2]
            self.db.set_settings({"settings_profiles": profiles})
            return 200, {"ok": True}
        if path == "/api/slicer/material-sync":
            # {material, brand, price_per_kg}
            self.db.add_event("slicer", "Синхронизация материала", f"{body.get('material')} {body.get('price_per_kg')}", "", body)
            return 200, {"ok": True}
        # --- 8.5: Фаза 11 --------------------------------------------------
        if path == "/api/wish/save":
            from .accounting import uid
            customer_id = str(body.get("customer_id") or "")
            text = str(body.get("text") or "").strip()
            if not customer_id or not text:
                return 400, {"error": "Нужны customer_id и текст пожелания"}
            row = self.db.upsert("wishes", {
                "id": uid("wish"), "customer_id": customer_id,
                "order_id": str(body.get("order_id") or ""),
                "text": text[:500], "status": "pending",
                "created_at": now_iso(), "resolved_at": ""})
            self.db.add_event("crm", "Wish-list: добавлено", text[:80],
                              "", {"customer_id": customer_id})
            return 200, {"ok": True, "wish": row}
        if path == "/api/wish/resolve":
            wish = self.db.one("SELECT * FROM wishes WHERE id=?",
                               (str(body.get("id") or ""),))
            if not wish:
                return 404, {"error": "Пожелание не найдено"}
            status = "done" if body.get("status") == "done" else "declined"
            self.db.execute("UPDATE wishes SET status=?, resolved_at=? WHERE id=?",
                            (status, now_iso(), wish["id"]))
            if status == "done":
                # «Сделали — взять?» (идея 72): готовый текст в буфер.
                customer = self.db.one("SELECT * FROM customers WHERE id=?",
                                       (wish["customer_id"],)) or {}
                self.db.add_event("crm", "Wish-list: готово",
                                  f"«{wish['text'][:60]}» — клиенту {customer.get('name') or ''}",
                                  "", {"text": f"Готово! «{wish['text'][:80]}» — забирайте 🙂"})
            return 200, {"ok": True}
        if path == "/api/wish/delete":
            self.db.delete("wishes", str(body.get("id") or ""))
            return 200, {"ok": True}
        if path == "/api/portal/code":
            import hashlib
            customer_id = str(body.get("customer_id") or "")
            customer = self.db.one("SELECT * FROM customers WHERE id=?",
                                   (customer_id,))
            if not customer:
                return 404, {"error": "Клиент не найден"}
            code = str(customer.get("portal_code") or "")
            if not code:
                code = hashlib.sha1(("nozza:" + customer_id).encode()).hexdigest()[:8]
                self.db.execute("UPDATE customers SET portal_code=? WHERE id=?",
                                (code, customer_id))
            self.bus.publish("resync", {})
            return 200, {"ok": True, "code": code,
                         "customer": customer.get("name")}
        if path == "/api/tour/start":
            from . import tour
            result = tour.start(self.db)
            self.manager.reload()  # подхватить виртуальный принтер
            job = self.db.one(
                "SELECT * FROM print_jobs WHERE state='queued' AND printer_id='virtual'"
                " ORDER BY created_at DESC LIMIT 1")
            started = False
            if job:
                try:
                    self.manager.start_job(job["id"], "virtual")
                    started = True
                except Exception:
                    pass
            self.bus.publish("resync", {})
            return 200, {**result, "job_started": started}
        if path == "/api/tour/stop":
            from . import tour
            from .db import request_restore
            try:
                backup_file = tour.stop_backup_file(self.db)
            except ValueError as exc:
                return 400, {"error": str(exc)}
            tour.reset_settings(self.db)
            result = request_restore(backup_file)
            self.db.add_event("system", "NOZZA tour: завершён",
                              f"Откат из копии {backup_file}; приложение перезапустится",
                              "", result)
            threading.Timer(1.5, self.restart_process).start()
            return 200, {**result, "restarting": True}
        if path == "/api/bed/reference":
            try:
                return 200, self.manager.set_bed_reference(str(pid))
            except ValueError as exc:
                return 400, {"error": str(exc)}
        if path == "/api/order/brand-card":
            # Бренд-карточка в заказ (идея 42): design.py -> очередь печати.
            from .accounting import uid
            from .config import UPLOAD_DIR
            order_id = str(body.get("order_id") or "")
            order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
            if not order:
                return 404, {"error": "Заказ не найден"}
            from .design import brand_card as brand_card_stl
            text = str(self.db.setting("company_name", "NOZZA") or "NOZZA")[:10]
            name = f"brand-card-{uid('bc')}.stl"
            try:
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                (UPLOAD_DIR / name).write_bytes(brand_card_stl(text))
            except ValueError as exc:
                return 400, {"error": str(exc)}
            except OSError as exc:
                return 500, {"error": f"Не удалось сохранить STL: {exc}"}
            job = self.manager.enqueue({
                "file": name, "name": f"Бренд-карточка — заказ №{order.get('number')}",
                "order_id": None, "source": "brand-card",
                "est_minutes": 25.0, "est_grams": 12.0,
                "priority": int(num(body.get("priority"), 0)),
            })
            self.db.add_event("queue", "Бренд-карточка в очередь",
                              f"Заказ №{order.get('number')} → {job['name']}",
                              "", {"job_id": job.get("id"), "order_id": order_id})
            return 200, {"ok": True, "job": job, "file": name}
        return 404, {"error": "Неизвестный маршрут"}


    def _heartbeat(self) -> dict:
        """Сердцебиение системы (идея 36): живы ли каналы, от которых зависит
        удалённый пульт и учёт."""
        import shutil
        now = time.time()
        settings = self.db.settings()
        tg_enabled = bool(settings.get("telegram_enabled")
                          and settings.get("telegram_token")
                          and settings.get("telegram_chat_id"))
        bot = getattr(self.manager, "bot", None)
        if not tg_enabled:
            tg = {"enabled": False, "ok": True, "note": "Бот выключен"}
        elif bot is None:
            tg = {"enabled": True, "ok": False, "note": "Бот не запущен"}
        else:
            age = now - bot.last_poll if bot.last_poll else None
            ok = age is not None and age < 90
            tg = {"enabled": True, "ok": ok,
                  "age_sec": round(age, 0) if age is not None else None,
                  "note": "" if ok else "Опрос не был успешным >90 с"}
        printers = []
        for p in self.manager.printers.values():
            snap_conn = {}
            try:
                snap_conn = p.snapshot().get("connection") or {}
            except Exception:
                pass
            connected = bool(getattr(p, "connected", False)
                              or (snap_conn.get("connected")))
            printers.append({
                "id": p.id, "name": p.record.get("name") or p.id,
                "virtual": p.mode == "virtual",
                "connected": connected,
                "last_error": snap_conn.get("last_error") or "",
            })
        from .db import list_backups
        from .config import DB_FILE
        backups = list_backups()
        newest = None
        if backups:
            newest = max(b.get("at") or "" for b in backups)
        try:
            from datetime import datetime
            newest_dt = datetime.fromisoformat(str(newest).replace("Z", ""))
            backup_age_h = round((datetime.now() - newest_dt).total_seconds() / 3600, 1)
        except Exception:
            backup_age_h = None
        disk = None
        if DB_FILE.exists():
            usage = shutil.disk_usage(DB_FILE.parent)
            disk = {"free_gb": round(usage.free / 1024 ** 3, 1),
                    "used_pct": round(usage.used / usage.total * 100, 1)}
        from .workshop_v9 import heartbeat_channels
        channels = heartbeat_channels(self.manager, self.db)
        if channels.get("disk"):
            disk = {
                "free_gb": channels["disk"].get("free_gb"),
                "used_pct": channels["disk"].get("used_pct"),
                "ok": channels["disk"].get("ok", True),
                "error": channels["disk"].get("error") or "",
            }
        mqtt_ok = bool((channels.get("mqtt") or {}).get("ok", True))
        ftps_ok = bool((channels.get("ftps") or {}).get("ok", True))
        disk_ok = True if not disk else disk.get("ok", True)
        return {
            "at": now_iso(), "uptime_sec": round(now - self.started_at),
            "telegram": tg, "printers": printers,
            "backup_newest_at": newest, "backup_age_h": backup_age_h,
            "disk": disk,
            "db_exists": DB_FILE.exists(),
            "mqtt": channels.get("mqtt") or {"ok": True, "printers": []},
            "ftps": channels.get("ftps") or {"ok": True, "printers": []},
            "channels": channels,
            "ok": mqtt_ok and ftps_ok and disk_ok,
        }

    def _ams_suggestion(self) -> list[dict]:
        """Память AMS (идея 41): последняя раскладка катушек по слотам."""
        out = []
        for printer in self.manager.printers.values():
            rows = self.db.query(
                "SELECT * FROM spools WHERE printer_id=? AND ams_slot IS NOT NULL"
                " AND ams_slot<>'' AND remaining_grams>0"
                " ORDER BY updated_at DESC", (printer.id,))
            seen = {}
            for row in rows:
                slot = str(row.get("ams_slot"))
                if slot in seen:
                    continue
                seen[slot] = {
                    "slot": slot,
                    "material": row.get("material") or "",
                    "color": row.get("color_name") or "",
                    "color_hex": row.get("color_hex") or "",
                    "spool_id": row["id"],
                    "since": row.get("updated_at") or "",
                }
            if seen:
                out.append({"printer_id": printer.id,
                            "name": printer.record.get("name") or printer.id,
                            "slots": sorted(seen.values(),
                                            key=lambda x: int(x["slot"] or 0))})
        return out

    def _my_nozza(self, code: str) -> dict:
        """«Мой NOZZA» (идея 94): публичная страница клиента по коду."""
        code = str(code or "").strip().lower()
        if not code:
            return {"error": "Нет кода"}
        customer = self.db.one("SELECT * FROM customers WHERE lower(portal_code)=?",
                               (code,))
        if not customer:
            return {"error": "Код не найден"}
        finals = {r["id"] for r in self.db.query("SELECT id FROM statuses WHERE is_final=1")}
        orders = []
        for o in self.db.query(
                "SELECT * FROM orders WHERE customer_id=? ORDER BY created_at DESC LIMIT 20",
                (customer["id"],)):
            photos = [r["file"] for r in self.db.query(
                "SELECT file FROM order_photos WHERE order_id=? AND file<>'' ORDER BY at DESC LIMIT 3",
                (o["id"],))]
            orders.append({
                "number": o.get("number"), "product": o.get("product"),
                "status": o.get("status"), "final": o["status"] in finals,
                "created_at": o.get("created_at") or "",
                "photos": photos,
                "qty": o.get("qty") or 1,
            })
        debt = 0.0
        for o in self.db.query("SELECT price, paid, prepaid, status FROM orders"
                               " WHERE customer_id=?", (customer["id"],)):
            if o["status"] not in finals:
                debt += max(0.0, num(o.get("price")) - max(num(o.get("paid")),
                                                           num(o.get("prepaid"))))
        return {
            "name": customer.get("name") or "Клиент NOZZA",
            "orders": orders,
            "orders_count": len(orders),
            "debt": round(debt, 2),
        }

    def _pack_data(self, order_id: str) -> dict:
        """Карточка упаковки (идея 98): что положить в заказ."""
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        items = self.db.query("SELECT * FROM order_items WHERE order_id=?", (order_id,))
        return {
            "order": {
                "number": order.get("number"), "product": order.get("product"),
                "customer_name": order.get("customer_name"),
                "qty": order.get("qty") or 1, "notes": order.get("notes") or "",
            },
            "items": [dict(r) for r in items],
            "brand_card": bool(self.db.setting("brand_card_enabled", True)),
        }

class Handler(BaseHTTPRequestHandler):
    server_version = f"PrintFlow/{APP_VERSION}"
    api: Api = None  # назначается при запуске

    def log_message(self, fmt, *args):  # тише в консоли
        if "--verbose" in getattr(self.server, "flags", []):
            super().log_message(fmt, *args)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except CLIENT_DISCONNECT_ERRORS:
            self.close_connection = True

    # ---------------------------------------------------------------- ответы
    def send_json(self, code: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except CLIENT_DISCONNECT_ERRORS:
            # Пользователь закрыл вкладку или перешёл в другой раздел — не ошибка.
            self.close_connection = True

    def check_origin(self) -> bool:
        return request_origin_allowed(
            self.headers.get("Origin"), self.headers.get("Host"))

    # ------------------------------------------------------------------- GET
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        self.api.last_host = self.headers.get("Host", "")
        try:
            if path == "/api/printer/camera.jpg":
                return self.serve_camera_frame((query.get("printer_id") or [""])[0])
            if path == "/api/printer/camera.mjpeg":
                return self.serve_camera_stream((query.get("printer_id") or [""])[0])
            if path == "/api/printer/shot.jpg":
                return self.serve_shot((query.get("printer_id") or [""])[0],
                                       (query.get("id") or [""])[0])
            if path == "/api/shelf/photo.jpg":
                return self.serve_shelf_photo((query.get("id") or [""])[0])
            if path == "/api/nomenclature/photo.jpg":
                return self.serve_nom_photo((query.get("id") or [""])[0])
            if path == "/api/order/photo.jpg":
                return self.serve_order_photo((query.get("photo_id") or [""])[0])
            if path == "/api/job/keyframe.jpg":
                job_id = (query.get("id") or query.get("job_id") or [""])[0]
                return self.serve_keyframe(job_id, (query.get("name") or [""])[0])
            if path == "/api/order/pack":
                return self.serve_pack_sheet((query.get("id") or [""])[0])
            if path == "/api/design/stl":
                return self.serve_design_stl(query)
            if path == "/api/design/preview":
                return self.serve_design_preview(query)
            if path in ("/api/b2b/doc", "/api/b2b"):
                return self.serve_b2b_doc(query)
            if path == "/api/stream":
                return self.serve_sse()
            if path == "/api/uploads":
                return self.serve_upload((query.get("file") or query.get("name") or [""])[0])
            if path.startswith("/api/"):
                code, payload = self.api.get(path, query)
                return self.send_json(code, payload)
            return self.serve_static(path)
        except CLIENT_DISCONNECT_ERRORS:
            return
        except TimeoutError:
            return self.send_json(504, {"error": "Принтер не отвечает: проверьте IP и локальную сеть"})
        except sqlite3.DatabaseError as exc:
            try:
                from .logging_setup import log
                log().exception("Ошибка SQLite при GET %s", path)
            except Exception:
                pass
            return self.send_json(503, {"error": friendly_sqlite_error(exc)})
        except (OSError, ConnectionError) as exc:
            return self.send_json(503, {"error": str(exc)})
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            try:
                from .logging_setup import log
                log().exception("Ошибка GET %s", path)
            except Exception:
                pass
            return self.send_json(500, {"error": str(exc)})

    def serve_camera_frame(self, printer_id: str):
        printer = self.api.manager.get(printer_id)
        frame = printer.camera.frame if printer else None
        if not frame:
            return self.send_json(503, {"error": (printer.camera.error if printer else "")
                                        or "Кадр ещё не получен"})
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(frame)

    def serve_sse(self):
        """Server-Sent Events: сервер сам присылает изменения.

        Три вида сообщений:
          * ``telemetry`` — новое состояние парка (шлётся, только когда принтер
            действительно что-то прислал);
          * ``event`` — новая запись в журнале: печать началась, заказ закрыт,
            пластик списан;
          * ``resync`` — вкладка отстала (спящий телефон), нужно перечитать всё.

        Поллинг на стороне браузера остаётся страховкой на случай прокси,
        который режет длинные соединения.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # nginx не должен буферизовать
        self.end_headers()

        def send(kind: str, payload: object) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str)
            self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            self.wfile.write(b"retry: 3000\n\n")  # переподключение через 3 с
            send("telemetry", self.api.manager.snapshot())
            with self.api.bus.subscription() as subscriber:
                while True:
                    message = subscriber.get(timeout=20.0)
                    if message is None:
                        self.wfile.write(b": ping\n\n")  # держим соединение живым
                        self.wfile.flush()
                        continue
                    send(message[0], message[1])
        except CLIENT_DISCONNECT_ERRORS:
            pass
        except Exception:
            try:
                from .logging_setup import log

                log().debug("Поток событий закрыт", exc_info=True)
            except Exception:
                pass
        finally:
            self.close_connection = True

    def _send_bytes(self, data: bytes, ctype: str, download: str = "") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if download:
            from urllib.parse import quote
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(download)}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def serve_design_stl(self, query: dict):
        """Генерация STL конструктором изделий (5.0) — скачивание файла."""
        from .design import generate
        one = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731
        shape = one("shape", "number_plate")
        params = {"number": one("number", "1"), "text": one("text", ""),
                  "width": num(one("width", "40")),
                  "height": num(one("height", "24")), "thickness": num(one("thickness", "2")),
                  "font_h": num(one("font_h", "1.4")), "diameter": num(one("diameter", "30")),
                  "depth": num(one("depth", "40"))}
        try:
            data = generate(shape, params)
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        name = f"nozza-{shape}-{one('number', '1')}.stl"
        self._send_bytes(data, "model/stl", download=name)

    def serve_design_preview(self, query: dict):
        from .design import preview_svg
        one = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731
        shape = one("shape", "number_plate")
        params = {"number": one("number", "1"), "text": one("text", ""),
                  "width": num(one("width", "40")),
                  "height": num(one("height", "24")), "diameter": num(one("diameter", "30")),
                  "depth": num(one("depth", "40"))}
        self._send_bytes(preview_svg(shape, params).encode("utf-8"),
                         "image/svg+xml; charset=utf-8")

    def serve_b2b_doc(self, query: dict):
        """Документ B2B (счёт / КП / товарный чек) как печатная HTML-страница.

        group=0 отключает сворачивание мелких товаров в печатные группы —
        форма печатается построчно, как состав заказа."""
        one = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731
        html = self.api.b2b.document(one("id"), one("kind", "invoice"),
                                     group=one("group", "1") != "0")
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def serve_photo_file(self, name: str):
        from .config import PHOTO_DIR
        target = safe_file(PHOTO_DIR, name) if name else None
        if not target or not target.is_file():
            return self.send_json(404, {"error": "Фото не найдено"})
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def serve_shelf_photo(self, item_id: str):
        """Фото позиции стеллажа из каталога данных."""
        item = self.api.db.one("SELECT photo FROM shelf_items WHERE id=?", (item_id,))
        return self.serve_photo_file((item or {}).get("photo") or "")

    def serve_nom_photo(self, nom_id: str):
        """Фото карточки номенклатуры."""
        row = self.api.db.one("SELECT photo FROM nomenclature WHERE id=?", (nom_id,))
        return self.serve_photo_file((row or {}).get("photo") or "")

    # ------------------------------------------------ 8.5: вспомогательные
    def serve_keyframe(self, job_id: str, name: str):
        """Кейфрейм видео печати (идея 61)."""
        from .config import PHOTO_DIR
        d = (PHOTO_DIR / "keyframes" / str(job_id)) if job_id else None
        if not d or not d.is_dir() or "/" in name or "\\" in name or name.startswith("."):
            return self.send_json(400, {"error": "Недопустимый файл"})
        f = d / name
        if not f.is_file():
            return self.send_json(404, {"error": "Кейфрейм не найден"})
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(f.read_bytes())

    def serve_pack_sheet(self, order_id: str):
        """Печатная карточка упаковки (A4, 1:1)."""
        import html as _html
        data = self.api._pack_data(order_id)
        o = data["order"]
        h = lambda v: _html.escape("" if v is None else str(v))
        rows = ""
        for it in data["items"]:
            rows += (f"<tr><td>{h(it.get('name') or o['product'])}</td>"
                     f"<td>{h(it.get('qty') or '')}</td></tr>")
        if not rows:
            rows = f"<tr><td>{h(o['product'])}</td><td>{h(o['qty'])}</td></tr>"
        brand = ("<li>Бренд-карточка NOZZA (идея 42)</li>"
                 if data["brand_card"] else "")
        html = f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Карточка упаковки — заказ №{o['number']}</title>
<style>
  @page {{ size: A4; margin: 8mm; }}
  body {{ font-family: Arial, sans-serif; color: #131a2b; }}
  .sheet {{ width: 194mm; margin: 0 auto; }}
  h1 {{ font-size: 18pt; margin: 0 0 2mm; }}
  .meta {{ color: #6b7280; font-size: 10pt; margin-bottom: 4mm; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 5mm; }}
  td, th {{ border: 1px solid #d1d5db; padding: 2.5mm 3mm; font-size: 11pt; }}
  th {{ background: #f3f4f6; text-align: left; }}
  .check {{ list-style: none; padding: 0; margin: 0; }}
  .check li {{ font-size: 12pt; margin-bottom: 2.5mm; }}
  .check li:before {{ content: "☐ "; color: #4f46e5; font-weight: bold; }}
  .foot {{ margin-top: 6mm; font-size: 9pt; color: #6b7280; }}
  @media print {{ .noprint {{ display: none; }} }}
</style></head><body><div class="sheet">
  <button class="noprint" onclick="window.print()"
    style="font-size:11pt;padding:4px 14px;margin-bottom:4mm;cursor:pointer">⎙ Печать</button>
  <h1>Карточка упаковки · заказ №{o['number']}</h1>
  <div class="meta">Клиент: {o['customer_name'] or '—'} · NOZZA · PrintFlow</div>
  <table><tr><th>Изделие</th><th>Кол-во</th></tr>{rows}</table>
  <h3 style="font-size:12pt">Что положить</h3>
  <ul class="check">
    <li>Изделие (проверено по чек-листу качества)</li>
    {brand}
    <li>Бирка с названием и QR (если есть)</li>
    <li>Упаковка: плёнка/коробка, вложение — бумага, не воздух</li>
    <li>Если заказ подарок — без ценника</li>
  </ul>
  <div class="foot">Сформировано автоматически · {time.strftime('%d.%m.%Y %H:%M')}</div>
</div></body></html>"""
        self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

    def serve_order_photo(self, photo_id: str):
        """Фото заказа по id записи order_photos."""
        row = self.api.db.one("SELECT file FROM order_photos WHERE id=?", (photo_id,))
        return self.serve_photo_file((row or {}).get("file") or "")

    def serve_shot(self, printer_id: str, shot_id: str):
        """Отдать сохранённый кадр из архива камеры."""
        printer = self.api.manager.get(printer_id)
        frame = printer.camera.snapshot_frame(shot_id) if printer else None
        if not frame:
            return self.send_json(404, {"error": "Кадр не найден"})
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(frame)

    def serve_camera_stream(self, printer_id: str):
        printer = self.api.manager.get(printer_id)
        if not printer:
            return self.send_json(404, {"error": "Принтер не найден"})
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=pfframe")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        event = printer.camera.subscribe()
        try:
            deadline = time.time() + 600  # поток живёт не дольше 10 минут
            while time.time() < deadline:
                if not event.wait(10):
                    continue
                event.clear()
                frame = printer.camera.frame
                if not frame:
                    continue
                self.wfile.write(b"--pfframe\r\nContent-Type: image/jpeg\r\n"
                                 + f"Content-Length: {len(frame)}\r\n\r\n".encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (CLIENT_DISCONNECT_ERRORS, TimeoutError, OSError):
            pass
        finally:
            printer.camera.unsubscribe(event)

    def serve_static(self, path: str):
        rel = urllib.parse.unquote(path.lstrip("/")) or "index.html"
        target = safe_file(SITE, rel)
        if target is None:
            return self.send_error(403, "Forbidden")
        if target.is_dir():
            target = target / "index.html"
        if not target.exists() and not target.suffix:
            # Короткие адреса без расширения: /m, /order, /track, /shelf.
            # Их проще диктовать вслух и печатать на ценнике.
            alias = safe_file(SITE, rel + ".html")
            if alias is not None and alias.exists():
                target = alias
        if not target.exists():
            return self.send_error(404, "Not Found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".webmanifest":   # mimetypes про него ещё не знает
            ctype = "application/manifest+json"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json",
                                                  "application/manifest+json"):
            ctype += "; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if target.suffix in (".html", ".js", ".css") else "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if not self.check_origin():
            return self.send_json(403, {"error": "Запрос отклонён: посторонний источник"})
        try:
            if path == "/api/printer/upload":
                return self.handle_upload(query)
            if path == "/api/jobs/upload":
                return self.handle_job_upload()
            if path == "/api/estimate/upload":
                return self.handle_estimate_upload()
            length, too_large = request_length(self.headers.get("Content-Length"), MAX_JSON)
            if too_large:
                return self.send_json(413, {"error": "JSON-запрос слишком большой"})
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return self.send_json(400, {"error": "Некорректный JSON"})
            if not isinstance(body, dict):
                return self.send_json(400, {"error": "Ожидается объект JSON"})
            code, payload = self.api.post(path, body, query)
            return self.send_json(code, payload)
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        except CLIENT_DISCONNECT_ERRORS:
            return
        except TimeoutError:
            return self.send_json(504, {"error": "Принтер не отвечает: проверьте IP и локальную сеть"})
        except sqlite3.DatabaseError as exc:
            try:
                from .logging_setup import log
                log().exception("Ошибка SQLite при POST %s", path)
            except Exception:
                pass
            return self.send_json(503, {"error": friendly_sqlite_error(exc)})
        except (OSError, ConnectionError) as exc:
            return self.send_json(503, {"error": str(exc)})
        except Exception as exc:
            try:
                from .logging_setup import log
                log().exception("Ошибка POST %s", path)
            except Exception:
                pass
            return self.send_json(500, {"error": str(exc)})

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


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    flags: list[str] = []


def serve(host: str = "127.0.0.1", port: int = 8080, flags: list[str] | None = None) -> Server:
    Handler.api = Api()
    Handler.api.listen_host = host
    Handler.api.listen_port = port
    server = Server((host, port), Handler)
    server.flags = flags or []
    thread = threading.Thread(target=server.serve_forever, name="pf-http", daemon=True)
    thread.start()
    return server
