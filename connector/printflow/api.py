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
        from .updater import UpdateChecker
        self.updater = UpdateChecker(APP_VERSION, self.db, self.manager)
        self.updater.start_auto()
        self.last_host = ""
        self.started_at = time.time()

    # --------------------------------------------------------------- хелперы
    def printer_or_fail(self, printer_id: str = "") -> BambuPrinter:
        printer = self.manager.get(printer_id)
        if not printer:
            raise ValueError("Принтер не настроен. Добавьте его в разделе «Принтеры».")
        return printer

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
            return 200, {"found": BambuPrinter.discover()}
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
        if path == "/api/catalog":
            return 200, {"catalog": self.repo.catalog()}
        if path == "/api/transactions":
            return 200, {"transactions": self.repo.transactions(int(num(one("limit", "200"), 200)))}
        if path == "/api/finance":
            days = int(num(one("days", "30"), 30))
            self.acc.run_fixed_costs()
            return 200, {"summary": self.acc.summary(days),
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
            return 200, {"url": self.shelf.qr_link(one("id"), getattr(self, "last_host", ""))}
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
        if path == "/api/update-check":
            return 200, self.updater.report()
        if path == "/api/abc":
            return 200, self.acc.abc_report(int(num(one("days", "30"), 30)))
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
        return 404, {"error": "Неизвестный маршрут"}

    # ------------------------------------------------------------------ POST
    def post(self, path: str, body: dict, query: dict) -> tuple[int, object]:
        pid = body.get("printer_id") or (query.get("printer_id") or [""])[0]

        # --- принтеры и команды
        if path == "/api/printer/save":
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
            return 200, printer.command(body.get("command", ""), body.get("value"))
        if path == "/api/printer/print":
            printer = self.printer_or_fail(pid)
            result = printer.start_print(
                body.get("file", ""), int(num(body.get("plate"), 1) or 1),
                bool(body.get("use_ams", True)), body.get("ams_mapping"),
                bool(body.get("bed_level", True)), bool(body.get("flow_cali", False)),
                bool(body.get("timelapse", False)), body.get("name", ""))
            job = self.manager.enqueue({**body, "printer_id": printer.id,
                                       "source": "manual", "name": body.get("name") or body.get("file", "")})
            self.db.execute("UPDATE print_jobs SET state='starting', started_at=? WHERE id=?",
                            (now_iso(), job["id"]))
            return 200, {**result, "job_id": job["id"]}
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
                num(body.get("design_minutes")), num(body.get("delivery")))
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
            if not body.get("id") or body.get("ams_slot") is None:
                raise ValueError("Нужны id катушки и слот AMS")
            printer_id = body.get("printer_id") or ""
            self.db.execute(
                "UPDATE spools SET printer_id=?, ams_slot=?, updated_at=? WHERE id=?",
                (printer_id or None, str(body["ams_slot"]), now_iso(), body["id"]))
            spool = self.db.one("SELECT * FROM spools WHERE id=?", (body["id"],)) or {}
            self.db.add_event("spool", "Катушка привязана к слоту AMS",
                              f"{spool.get('material')} {spool.get('color_name')} → слот {body['ams_slot']}",
                              printer_id, {"spool_id": body["id"]})
            return 200, {"ok": True, "spool": spool}
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
            return 200, {"ok": True, "settings": self.db.set_settings(body)}
        if path == "/api/settings/reset":
            return 200, {"ok": True, "settings": self.repo.reset_settings(body.get("keys"))}
        if path == "/api/telegram/test":
            self.db.set_settings({k: v for k, v in body.items() if k.startswith("telegram")})
            return 200, self.manager.send_telegram("PrintFlow: проверка уведомлений прошла успешно.")
        if path == "/api/import":
            return 200, {"ok": True, "imported": self.repo.import_backup(body)}
        if path == "/api/import/localstorage":
            return 200, {"ok": True, "imported": self.repo.import_local_storage(body)}
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
        """Server-Sent Events: поток «refresh» при появлении новых событий.

        Фронтенд слушает его и обновляет данные сразу, а не по таймеру.
        Поллинг остаётся как запасной вариант (например, при проксировании).
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_id = 0
        try:
            row = self.api.db.one("SELECT MAX(id) m FROM events")
            last_id = int(num((row or {}).get("m") or 0))
            deadline = time.time() + 12 * 3600  # поток живёт не дольше 12 часов
            while time.time() < deadline:
                row = self.api.db.one("SELECT MAX(id) m FROM events")
                cur = int(num((row or {}).get("m") or 0))
                if cur > last_id:
                    last_id = cur
                    self.wfile.write(b"event: refresh\ndata: {}\n\n")
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(2.5)
        except CLIENT_DISCONNECT_ERRORS:
            pass
        finally:
            self.close_connection = True

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
        if not target.exists():
            return self.send_error(404, "Not Found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
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
        result = printer.files.upload(local, name)
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
            self.api.db.execute("UPDATE orders SET file=?, updated_at=? WHERE id=?",
                                (name, now_iso(), order_id))
        return self.send_json(200, {"ok": True, "estimate": estimate, **result})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    flags: list[str] = []


def serve(host: str = "127.0.0.1", port: int = 8765, flags: list[str] | None = None) -> Server:
    Handler.api = Api()
    server = Server((host, port), Handler)
    server.flags = flags or []
    thread = threading.Thread(target=server.serve_forever, name="pf-http", daemon=True)
    thread.start()
    return server
