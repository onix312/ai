"""HTTP-сервер PrintFlow: JSON-API и раздача сайта.

По умолчанию слушает только 127.0.0.1. Все изменяющие запросы проверяют
заголовок Origin, чтобы посторонняя страница не смогла управлять принтером.
"""
from __future__ import annotations

import json
import mimetypes
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_VERSION
from .accounting import Accounting, num, uid
from .bambu import BambuPrinter
from .bus import EventBus, LiveBroadcaster
from .config import SITE, UPLOAD_DIR, ensure_dirs, now_iso
from .db import Database
from .manager import PrinterManager
from .repo import Repo

ALLOWED_ORIGIN = re.compile(
    r"^https?://"
    r"(localhost|127\.0\.0\.1|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}|"
    r"\[::1\]|::1"
    r")(:\d+)?$"
)
MAX_UPLOAD = 400 * 1024 * 1024  # 400 МБ — с запасом на крупные 3MF

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


class Api:
    """Логика маршрутов, отделённая от транспорта."""

    def __init__(self):
        ensure_dirs()
        self.db = Database()
        # Шина событий: сервер сам сообщает вкладкам, что изменилось,
        # вместо того чтобы каждая из них опрашивала его по таймеру.
        self.bus = EventBus()
        self.db.bus = self.bus
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
        from .b2b import B2B
        self.b2b = B2B(self.db)
        from .updater import UpdateChecker
        self.updater = UpdateChecker(APP_VERSION, self.db, self.manager)
        self.updater.start_auto()
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
            for item in self.shelf.items():
                info = self.shelf.qr_link(
                    item["id"], getattr(self, "last_host", ""),
                    str(self.db.setting("public_url", "") or ""),
                    int(getattr(self, "listen_port", 8080) or 8080))
                shelf.append({
                    "id": item["id"], "url": info["url"],
                    "name": item.get("name") or "",
                    "price": item.get("price"),
                    "qty": item.get("qty"),
                })
        return {"base": base["base"], "reachable": base["reachable"], "source": base["source"],
                "spools": spools, "shelf": shelf}

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
        from . import bambu_cloud
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
            if num(item.get("price")) <= 0:
                continue
            goods.append({
                "id": item["id"],
                "name": item["name"],
                "price": item["price"],
                "qty": item["qty"],
                "status": item["status"],
                "photo": item.get("photo") or "",
                "group": groups.get(item.get("group_id") or "", ""),
                "niche_id": item.get("niche_id") or "",
                "material": item.get("material") or "",
            })
        return {
            "company": str(self.db.setting("company_name", "NOZZA") or "NOZZA"),
            "currency": str(self.db.setting("currency", "₽") or "₽"),
            "items": goods,
            "niches": niches,
        }

    def public_order(self, body: dict) -> dict:
        """Заявка с витрины (QR-заказ): создаёт заказ в статусе «Новая заявка».

        Вход строго валидируется: минимум полей, никаких внутренних ссылок.
        Клиент создаётся автоматически по имени и контакту.
        """
        name = str(body.get("name") or "").strip()
        contact = str(body.get("phone") or body.get("messenger") or "").strip()
        product = str(body.get("product") or "").strip()
        if not name:
            raise ValueError("Укажите имя")
        if not contact:
            raise ValueError("Оставьте телефон или мессенджер")
        if not product:
            raise ValueError("Укажите, что заказать")
        qty = max(1, int(num(body.get("qty"), 1)))
        color = str(body.get("color") or "").strip()
        nom_id = str(body.get("nom_id") or "").strip()
        niche_id = ""
        if nom_id:
            item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
            if item:
                niche_id = item.get("niche_id") or ""
        order = self.repo.save_order({
            "product": product,
            "customer_name": name,
            "phone": str(body.get("phone") or "").strip(),
            "messenger": str(body.get("messenger") or "").strip(),
            "channel": str(body.get("channel") or "shop").strip() or "shop",
            "niche_id": niche_id or None,
            "status": "new",
            "qty": qty,
            "color": color,
            "notes": str(body.get("note") or "").strip() or "Заявка с витрины",
            "nom_id": nom_id or None,
        })
        self.db.add_event("lead", "Заявка с витрины",
                          f"{product} · {name}" + (f" · {color}" if color else ""),
                          data={"order_id": order.get("id"), "source": "storefront"})
        return {"ok": True, "order_number": order.get("number"), "order_id": order.get("id")}

    def fulfill_order(self, order_id: str, account_id: str = "") -> dict:
        """«Заказ выдан» одной кнопкой: остаток оплаты → финальный статус → текст.

        Принимает недополученные деньги в кассу (если автоучёт включён),
        закрывает заказ и возвращает готовое сообщение клиенту.
        """
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            raise ValueError("Заказ не найден")
        final = self.db.one(
            "SELECT id FROM statuses WHERE is_final=1 ORDER BY position LIMIT 1")
        if not final:
            raise ValueError("Не настроен финальный статус заказа")
        if order.get("status") == final["id"]:
            raise ValueError("Заказ уже выдан")
        collected = 0.0
        if self.db.setting("auto_income_on_done", True):
            rest = round(max(0.0, num(order.get("price")) -
                             max(num(order.get("paid")), num(order.get("prepaid")))), 2)
            if rest > 0:
                self.acc.add_payment(
                    order_id, rest, "payment", account_id or order.get("account_id") or "",
                    "выдача заказа", "Оплата при выдаче заказа")
                collected = rest
        saved = self.repo.save_order({"id": order_id, "status": final["id"]})
        number = str(saved.get("number") or "")
        message = (f"Здравствуйте! Ваш заказ №{number} готов, можно забирать. "
                   f"Спасибо, что выбрали NOZZA!")
        return {"ok": True, "order": saved, "collected": collected, "message": message}

    def network_diagnose(self, host: str) -> dict:
        from . import network
        host = host or ""
        if not host:
            printers = self.repo.printers()
            host = (printers[0] or {}).get("host") if printers else ""
        if not host:
            return {"error": "Укажите IP принтера", "host": ""}
        return network.diagnose(host)

    def track_order(self, number: str, phone: str) -> dict:
        """Статус заказа для клиента по номеру и контактному телефону."""
        number = (number or "").strip()
        phone = (phone or "").strip()
        if not number:
            return {"found": False, "error": "Укажите номер заказа"}
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return {"found": False, "error": "Заказ не найден"}
        if phone and phone not in (order.get("phone") or ""):
            return {"found": False, "error": "Телефон не совпадает"}
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
            return 200, {"path": one("path", "/"),
                         "files": printer.files.list_files(one("path", "/"))}
        if path == "/api/orders":
            return 200, {"orders": self.repo.orders(one("status"), one("q"), one("niche_id"))}
        if path == "/api/order":
            order = self.repo.order(one("id"))
            return (200, order) if order else (404, {"error": "Заказ не найден"})
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
        # ------------------------------------------------ учёт 3.0: номенклатура
        if path == "/api/nomenclature":
            return 200, {
                "items": self.nom.items(one("group_id"), one("kind"), one("search"),
                                        one("warehouse_id"),
                                        one("archived") == "1"),
                "summary": self.nom.summary(one("warehouse_id")),
                "groups": self.nom.groups(),
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
            return 200, self.track_order(one("number"), one("phone"))
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
                int(num(one("limit", "200"), 200)))}
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
                            raw = base64.b64decode(v)
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
            fname = one("file")
            from .config import UPLOAD_DIR
            from .estimate import estimate_file, parse_3mf_complete
            local = UPLOAD_DIR / fname
            if not local.exists():
                # also check watch folder
                wp = Path(str(self.db.setting("watch_folder_path",""))).expanduser() / fname if self.db.setting("watch_folder_path","") else None
                if wp and wp.exists():
                    local = wp
            if not local.exists():
                return 404, {"error": "Файл не найден"}
            if local.suffix.lower() == ".3mf":
                try:
                    detail = parse_3mf_complete(local)
                    est = {}
                    if detail.get("plates"):
                        total_g = round(sum(p.get("grams",0) for p in detail["plates"]),1)
                        total_m = round(sum(p.get("minutes",0) for p in detail["plates"]),1)
                        est = dict(detail["plates"][0]) if detail["plates"] else {}
                        est["total_grams"]=total_g; est["total_minutes"]=total_m
                        est["plates"]=detail["plates"]; est["plate_count"]=len(detail["plates"])
                    return 200, {"estimate": est, "detail": detail}
                except Exception as exc:
                    return 200, {"estimate": estimate_file(local)}
            return 200, {"estimate": estimate_file(local)}
        if path == "/api/settings/profiles":
            return 200, {"profiles": self.db.setting("settings_profiles", [])}
        if path == "/api/slicer/materials":
            return 200, self.acc.material_options()
        return 404, {"error": "Неизвестный маршрут"}

    # ------------------------------------------------------------------ POST
    def post(self, path: str, body: dict, query: dict) -> tuple[int, object]:
        pid = body.get("printer_id") or (query.get("printer_id") or [""])[0]

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
            cmd = body.get("command", "")
            if cmd == "pause":
                self.manager.mark_user_paused(printer.id)
            elif cmd == "resume":
                self.manager.clear_user_paused(printer.id)
            return 200, printer.command(cmd, body.get("value"))
        if path == "/api/printer/convert-to-order":
            printer_id = pid or body.get("printer_id", "")
            return 200, self.manager.convert_active_to_order(printer_id, body)
        if path == "/api/jobs/convert-to-order":
            job_id = body.get("job_id") or body.get("id", "")
            if not job_id and (pid or body.get("printer_id")):
                return 200, self.manager.convert_active_to_order(pid or body.get("printer_id", ""), body)
            return 200, self.manager.convert_job_to_order(job_id, body)
        if path == "/api/printer/reprint":
            # Повтор сорванного задания: по id задания или по номеру заказа.
            order_number = str(body.get("order_number") or "").strip()
            if body.get("id"):
                row = self.manager.reprint_job(str(body["id"]), str(body.get("printer_id") or ""))
            else:
                row = self.manager.reprint_last_failed(order_number)
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
            printer = self.printer_or_fail(pid)
            name = str(body.get("name") or body.get("file") or "")
            if printer.mode == "cloud" and not body.get("local_force"):
                # Облачный запуск: заливка + диспетчеризация /my/task —
                # принтер качает файл сам, LAN Only Mode не нужен.
                result = self.manager.start_print_cloud(
                    printer, body.get("file", ""),
                    int(num(body.get("plate"), 1) or 1),
                    bool(body.get("use_ams", True)), body.get("ams_mapping"),
                    bool(body.get("bed_level", True)), bool(body.get("flow_cali", False)),
                    bool(body.get("timelapse", False)),
                    progress=self._cloud_progress(name))
            else:
                result = printer.start_print(
                    body.get("file", ""), int(num(body.get("plate"), 1) or 1),
                    bool(body.get("use_ams", True)), body.get("ams_mapping"),
                    bool(body.get("bed_level", True)), bool(body.get("flow_cali", False)),
                    bool(body.get("timelapse", False)), name)
            job = self.manager.enqueue({**body, "printer_id": printer.id,
                                       "source": "manual", "name": name})
            self.db.execute("UPDATE print_jobs SET state='starting', started_at=? WHERE id=?",
                            (now_iso(), job["id"]))
            self.db.add_event("print_start", "Запущена печать",
                              f"{name} · облако: {result.get('cloud', False)}",
                              printer.id, {"task_id": result.get("task_id")})
            return 200, {**result, "job_id": job["id"]}
        if path == "/api/printer/sync-history":
            return 200, self.manager.sync_cloud_history(pid)
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
            return 200, {"ok": True, "job": self.manager.enqueue(body)}
        if path == "/api/jobs/start":
            return 200, {"ok": True, "job": self.manager.start_job(body.get("id", ""), pid)}
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
        if path == "/api/order/save":
            return 200, {"ok": True, "order": self.repo.save_order(body)}
        if path == "/api/order/status":
            return 200, {"ok": True, "order": self.repo.set_order_status(
                body.get("id", ""), body.get("status", ""))}
        if path == "/api/order/delete":
            self.repo.delete_order(body.get("id", ""))
            return 200, {"ok": True}
        if path == "/api/order/fulfill":
            return 200, self.fulfill_order(body.get("id", ""), body.get("account_id", ""))
        if path == "/api/order/duplicate":
            return 200, {"ok": True, "order": self.repo.duplicate_order(body.get("id", ""))}
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
                body.get("method", ""), body.get("note", ""))
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
        if path == "/api/shelf/sale":
            return 200, self.shelf.sale(body.get("item_id", ""), num(body.get("qty")),
                                        num(body.get("price")), body.get("channel", "shelf"),
                                        body.get("note", ""))
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
                body.get("warehouse_id", ""), body.get("note", ""))}
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
            return 200, self.batches.plan(
                body.get("nom_id", ""), num(body.get("qty")), body.get("mode", "full"),
                int(num(body.get("plates"))), body.get("printer_id", ""),
                body.get("spool_id", ""), num(body.get("price")))
        if path == "/api/batch/create":
            return 200, {"ok": True, "batch": self.batches.create(body)}
        if path == "/api/batch/receive":
            return 200, {"ok": True, "batch": self.batches.receive(
                body.get("id", ""), num(body.get("qty")), num(body.get("scrap")),
                body.get("job_id", ""), num(body.get("cost")), body.get("note", ""))}
        if path == "/api/batch/cancel":
            return 200, {"ok": True, "batch": self.batches.cancel(body.get("id", ""))}
        if path == "/api/batch/repeat":
            return 200, {"ok": True, "batch": self.batches.repeat(
                body.get("id", ""), bool(body.get("start_now")))}
        if path == "/api/batch/from-plan":
            return 200, {"ok": True, "batches": self.batches.create_from_plan(
                body.get("rows") or [], body.get("warehouse_id", ""),
                bool(body.get("start_now")))}
        if path == "/api/defect/save":
            data = dict(body)
            data.setdefault("id", uid("df"))
            data.setdefault("at", now_iso())
            if data.get("job_id"):
                job = self.db.one("SELECT order_id FROM print_jobs WHERE id=?", (data["job_id"],))
                if job and not data.get("order_id"):
                    data["order_id"] = job["order_id"]
            row = self.db.upsert("defects", data)
            self.db.add_event("defect", "Записан брак", f"{row.get('reason') or ''} · {row.get('code') or ''}",
                              row.get("printer_id") or "", {"defect_id": row["id"], "order_id": row.get("order_id")})
            return 200, {"ok": True, "defect": row}
        if path == "/api/defect/delete":
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
            if not body.get("command") or not body.get("at"):
                raise ValueError("Нужны команда и время")
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
            return 200, {"ok": True, "settings": self.db.set_settings(patch)}
        if path == "/api/shopping/add":
            return 200, {"ok": True, "item": self.shopping.add(body)}
        if path == "/api/shopping/toggle":
            return 200, {"ok": True, "item": self.shopping.toggle(
                body.get("id", ""), bool(body.get("done", True)))}
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
            order = self.db.one("SELECT * FROM orders WHERE id=?",
                                (body.get("id") or body.get("order_id") or "",))
            if not order:
                raise ValueError("Заказ не найден")
            debt = self.acc.order_economics(order)["debt"]
            name = (order.get("customer_name") or "").strip()
            hello = f", {name}," if name else ","
            text = (f"Здравствуйте{hello} напоминаем о заказе "
                    f"№{order.get('number')} «{order.get('product') or ''}»: "
                    f"остаток к оплате {round(debt)} ₽. Спасибо!")
            self.db.execute(
                "UPDATE orders SET reminded_at=?, updated_at=? WHERE id=?",
                (now_iso(), now_iso(), order["id"]))
            self.db.add_event("order", "Напомнили о долге",
                              f"№{order.get('number')}", "",
                              {"order_id": order["id"]})
            self.bus.publish("resync", {})
            return 200, {"ok": True, "text": text, "debt": round(debt, 2),
                         "number": order.get("number")}
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
        return 404, {"error": "Неизвестный маршрут"}


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
        origin = self.headers.get("Origin")
        if not origin or ALLOWED_ORIGIN.match(origin):
            return True
        # Доступ по локальному имени хоста (printflow.local, имя ПК и т. п.)
        # безопасен, когда Origin в точности совпадает с Host текущего запроса.
        parsed = urllib.parse.urlparse(origin)
        return parsed.scheme in ("http", "https") and parsed.netloc == self.headers.get("Host", "")

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
            if path == "/api/design/stl":
                return self.serve_design_stl(query)
            if path == "/api/design/preview":
                return self.serve_design_preview(query)
            if path == "/api/b2b/doc":
                return self.serve_b2b_doc(query)
            if path == "/api/stream":
                return self.serve_sse()
            if path.startswith("/api/"):
                code, payload = self.api.get(path, query)
                return self.send_json(code, payload)
            return self.serve_static(path)
        except CLIENT_DISCONNECT_ERRORS:
            return
        except TimeoutError:
            return self.send_json(504, {"error": "Принтер не отвечает: проверьте IP и локальную сеть"})
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
        params = {"number": one("number", "1"), "width": num(one("width", "40")),
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
        params = {"number": one("number", "1"), "width": num(one("width", "40")),
                  "height": num(one("height", "24")), "diameter": num(one("diameter", "30")),
                  "depth": num(one("depth", "40"))}
        self._send_bytes(preview_svg(shape, params).encode("utf-8"),
                         "image/svg+xml; charset=utf-8")

    def serve_b2b_doc(self, query: dict):
        """Документ B2B (счёт / КП / товарный чек) как печатная HTML-страница."""
        one = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731
        html = self.api.b2b.document(one("id"), one("kind", "invoice"))
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
        except CLIENT_DISCONNECT_ERRORS:
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
            length = int(self.headers.get("Content-Length") or 0)
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
        except (OSError, ConnectionError) as exc:
            return self.send_json(503, {"error": str(exc)})
        except Exception as exc:
            try:
                from .logging_setup import log
                log().exception("Ошибка POST %s", path)
            except Exception:
                pass
            return self.send_json(500, {"error": str(exc)})

    def handle_upload(self, query: dict):
        """Приём файла модели и отправка его на принтер по FTPS."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
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
        name = Path(upload[0]).name
        if not name.lower().endswith((".3mf", ".gcode")):
            return self.send_json(400, {"error": "Поддерживаются только 3MF и G-code"})
        ensure_dirs()
        local = UPLOAD_DIR / name
        local.write_bytes(upload[1])
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


def serve(host: str = "127.0.0.1", port: int = 8765, flags: list[str] | None = None) -> Server:
    Handler.api = Api()
    Handler.api.listen_host = host
    Handler.api.listen_port = port
    server = Server((host, port), Handler)
    server.flags = flags or []
    thread = threading.Thread(target=server.serve_forever, name="pf-http", daemon=True)
    thread.start()
    return server
