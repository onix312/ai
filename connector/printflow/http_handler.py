"""HTTP-транспорт PrintFlow: приём запросов и отправка ответов (Н18).

Третий слой разборки `api.py`. Здесь нет ни бизнес-логики, ни знания о том,
какие бывают заказы: `Handler` только разбирает запрос, передаёт его в
`Api` (контекст) или в раздачу статики и пишет ответ обратно в сокет.

Слои после разборки:

  * `http_handler.py` — транспорт (этот файл);
  * `static_serve.py` — раздача файлов панели;
  * `uploads.py` — приём загружаемых файлов;
  * `http_helpers.py` — утилиты без состояния;
  * `api.py` — `Api`, контекст со всеми зависимостями, и маршруты.

`Api` импортируется только для подсказки типов и под `TYPE_CHECKING`,
иначе получился бы круг: `api` подключает `Handler`, а `Handler` — `Api`.
"""
from __future__ import annotations

import json
import mimetypes
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

from . import static_serve
from .config import PHOTO_DIR, SITE, ensure_dirs, now_iso
from .http_helpers import (CLIENT_DISCONNECT_ERRORS, MAX_JSON, begin_request,
                           rate_bucket, request_length,
                           request_origin_allowed, safe_file)
from .idempotency import extract_key as extract_idempotency_key
from .rate_limit import client_key, limiter
from .router import router
from .uploads import UploadMixin

if TYPE_CHECKING:  # только для подсказок — на рантайм не влияет
    from .api import Api

from . import APP_VERSION


class Handler(UploadMixin, BaseHTTPRequestHandler):
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
            # Н2: id запроса возвращается клиенту — по нему ошибка находится в логе.
            from .logging_setup import REQUEST_ID
            request_id = REQUEST_ID.get() or ""
            if request_id:
                self.send_header("X-Request-Id", str(request_id))
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
        begin_request(self, "GET", path)
        try:
            # 14.0 (идея 34): публичные страницы и трекинг ограничены по
            # частоте; служебные потоки (камера, SSE, статика) — нет.
            bucket = rate_bucket(path)
            if bucket:
                allowed, info = limiter.check(bucket, client_key(self.headers))
                if not allowed:
                    return self.send_json(429, info)
            if path == "/api/printer/camera.jpg":
                return self.serve_camera_frame((query.get("printer_id") or [""])[0])
            if path == "/api/printer/camera.mjpeg":
                return self.serve_camera_stream((query.get("printer_id") or [""])[0])
            if path == "/api/camera/bed-ref.jpg":
                return self.serve_bed_reference()
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

    def serve_bed_reference(self):
        """Эталон пустого стола — фон для калибровки проекции (119)."""
        from .config import PHOTO_DIR

        target = PHOTO_DIR / "bed_reference.jpg"
        if not target.is_file():
            return self.send_json(404, {"error": "Эталон стола не снят — нажмите «Пустой стол» на вкладке принтера"})
        try:
            data = target.read_bytes()
        except OSError:
            return self.send_json(500, {"error": "Эталон стола не читается"})
        self._send_bytes(data, "image/jpeg")

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
        """Раздача файлов панели. Логика живёт в static_serve (Н18)."""
        return static_serve.serve_static(self, path)

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if not self.check_origin():
            return self.send_json(403, {"error": "Запрос отклонён: посторонний источник"})
        # 14.0 (идеи 5, 11, 34): контекст запроса, ограничение частоты и
        # идемпотентность. Всё на входе в обработчик, а не внутри веток.
        request_id = begin_request(self, "POST", path)
        try:
            bucket = rate_bucket(path)
            if bucket:
                allowed, info = limiter.check(bucket, client_key(self.headers))
                if not allowed:
                    return self.send_json(429, info)
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
            # Идемпотентность (идея 5): для маршрутов, помеченных в реестре,
            # повтор с тем же ключом возвращает прежний ответ, а не создаёт
            # вторую сущность.
            route = router.find("POST", path)
            key = extract_idempotency_key(body, self.headers)
            if route is not None and route.idempotent and key:
                store = self.api.idempotency
                found, cached = store.get(key, path)
                if found:
                    # Н2: повтор помечается явно — панель скажет «уже
                    # обработано» вместо того, чтобы тихо показать тот же ответ.
                    replay = dict(cached) if isinstance(cached, dict) else {"result": cached}
                    replay["replayed"] = True
                    replay["idempotency_key"] = key
                    return self.send_json(200, replay)
                code, payload = self.api.post(path, body, query)
                if 200 <= code < 300:
                    store.put(key, path, payload)
                return self.send_json(code, payload)
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

