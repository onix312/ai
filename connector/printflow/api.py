"""HTTP-сервер PrintFlow: JSON-API и раздача сайта.

По умолчанию слушает только 127.0.0.1. Все изменяющие запросы проверяют
заголовок Origin, чтобы посторонняя страница не смогла управлять принтером.
"""
from __future__ import annotations

import json
import mimetypes
import re
import socketserver
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
        self.started_at = time.time()

    # --------------------------------------------------------------- хелперы
    def printer_or_fail(self, printer_id: str = "") -> BambuPrinter:
        printer = self.manager.get(printer_id)
        if not printer:
            raise ValueError("Принтер не настроен. Добавьте его в разделе «Принтеры».")
        return printer

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
        if path == "/api/events":
            return 200, {"events": self.db.events(int(num(one("limit", "80"), 80)),
                                                  one("printer_id"), one("kind"))}
        if path == "/api/settings":
            return 200, {"settings": self.db.settings()}
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
        except (BrokenPipeError, ConnectionResetError):
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
        except (BrokenPipeError, ConnectionResetError):
            # Пользователь закрыл вкладку или перешёл в другой раздел — не ошибка.
            self.close_connection = True

    def check_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or bool(ALLOWED_ORIGIN.match(origin))

    # ------------------------------------------------------------------- GET
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/printer/camera.jpg":
                return self.serve_camera_frame((query.get("printer_id") or [""])[0])
            if path == "/api/printer/camera.mjpeg":
                return self.serve_camera_stream((query.get("printer_id") or [""])[0])
            if path.startswith("/api/"):
                code, payload = self.api.get(path, query)
                return self.send_json(code, payload)
            return self.serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except TimeoutError:
            return self.send_json(504, {"error": "Принтер не отвечает: проверьте IP и локальную сеть"})
        except (OSError, ConnectionError) as exc:
            return self.send_json(503, {"error": str(exc)})
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})
        except Exception as exc:
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
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            printer.camera.unsubscribe(event)

    def serve_static(self, path: str):
        rel = urllib.parse.unquote(path.lstrip("/")) or "index.html"
        target = (SITE / rel).resolve()
        if not str(target).startswith(str(SITE.resolve())):
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
        except (BrokenPipeError, ConnectionResetError):
            return
        except TimeoutError:
            return self.send_json(504, {"error": "Принтер не отвечает: проверьте IP и локальную сеть"})
        except (OSError, ConnectionError) as exc:
            return self.send_json(503, {"error": str(exc)})
        except Exception as exc:
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
        order_id = fields.get("order_id", "")
        if order_id:
            self.api.db.execute("UPDATE orders SET file=?, updated_at=? WHERE id=?",
                                (name, now_iso(), order_id))
        return self.send_json(200, {"ok": True, **result})


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
