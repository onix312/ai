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
import sqlite3
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING

from . import config, static_serve
from .accounting import num
from .config import now_iso
from .db import friendly_sqlite_error
from .http_helpers import (CLIENT_DISCONNECT_ERRORS, MAX_JSON, MAX_UPLOAD,
                           STREAM_DISCONNECT_ERRORS, begin_request,
                           rate_bucket, request_length,
                           request_origin_allowed, safe_file)
from .idempotency import extract_key as extract_idempotency_key
from .rate_limit import client_key, limiter
from .router import router
from .uploads import UploadMixin

if TYPE_CHECKING:  # только для подсказок — на рантайм не влияет
    from .api import Api

from . import APP_VERSION


SSE_TICK_SECONDS = 5.0     # как часто просыпаемся, чтобы проверить пинг
SSE_PING_SECONDS = 20.0    # касса считает поток мёртвым после 45 с тишины


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

    # ------------------------------------------------------- ранние ответы
    def _drain_body(self, limit: int = MAX_UPLOAD) -> int:
        """Дочитать тело запроса перед ранним ответом (403/429/400).

        18.12.1: ответ, отправленный НЕ дочитав тело, рвёт соединение на
        середине загрузки — браузер ещё льёт байты, а сервер уже закрыл
        канал. fetch падает сетевой ошибкой, и панель показывает «Нет связи
        с коннектором PrintFlow» вместо настоящей причины (чужой Origin,
        лимит частоты, «Файл слишком большой»).

        Тело читается кусками по 64 КБ и выбрасывается: ответ уже готов,
        байты не нужны. Тело больше ``limit`` не глотаем — такой поток
        дочитывать бессмысленно, соединение закрывается честно.

        Соединение закрывается в любом случае: ранний ответ — не штатный
        ответ на запрос, а держать keep-alive после отклонённой загрузки
        значит рискнуть рассинхроном потока (клиент может продолжать лить
        байты, которые уже никто не читает).
        """
        self.close_connection = True
        try:
            length, too_large = request_length(self.headers.get("Content-Length"), limit)
        except ValueError:
            return 0
        if too_large:
            return 0
        drained = 0
        while drained < length:
            try:
                chunk = self.rfile.read(min(65536, length - drained))
            except (OSError, ValueError):
                break
            if not chunk:
                break
            drained += len(chunk)
        return drained

    # ------------------------------------------------------------------- GET
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        self.api.last_host = self.headers.get("Host", "")
        begin_request(self, "GET", path)
        try:
            # 14.0 (идея 34): публичные страницы и трекинг ограничены по
            # частоте; служебные потоки (камера, SSE, статика) — нет.
            # 18.12.1: ключ клиента — прокси-заголовки, иначе адрес сокета
            # (общий "unknown" блокировал всю панель разом), а перед 429
            # тело запроса дочитывается — иначе ответ рвёт загрузку.
            bucket = rate_bucket(path)
            if bucket:
                allowed, info = limiter.check(
                    bucket, client_key(self.headers, self.client_address[0]))
                if not allowed:
                    self._drain_body()
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
            if path == "/api/nomenclature/variant/photo.jpg":
                return self.serve_variant_photo((query.get("id") or [""])[0],
                                                (query.get("n") or [""])[0])
            if path == "/api/order/photo.jpg":
                return self.serve_order_photo((query.get("photo_id") or [""])[0])
            if path == "/api/job/keyframe.jpg":
                job_id = (query.get("id") or query.get("job_id") or [""])[0]
                return self.serve_keyframe(job_id, (query.get("name") or [""])[0])
            if path == "/api/order/pack":
                return self.serve_pack_sheet((query.get("id") or [""])[0])
            if path in ("/api/spools/bambu-export", "/api/spools/bambu-export.zip"):
                return self.serve_bambu_spools_export()
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

        Виды сообщений:
          * ``telemetry`` — новое состояние парка (шлётся, только когда принтер
            действительно что-то прислал);
          * ``event`` — новая запись в журнале: печать началась, заказ закрыт,
            пластик списан;
          * ``resync`` — вкладка отстала (спящий телефон), нужно перечитать всё;
          * ``ping`` — «поток жив», раз в ``SSE_PING_SECONDS``.

        Пинг нужен кассе: по нему она отличает «канал работает, просто платежей
        нет» от «поток оборвался, работаем на страховочном поллинге». Шлём его
        по своему таймеру, а не «когда шина молчит»: телеметрия принтеров идёт
        чаще пинга и заслоняла бы его — касса 45 минут считала бы связь
        потерянной, хотя поток жив (находка замера 17.0.13).

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
            last_ping = time.time()
            with self.api.bus.subscription() as subscriber:
                while True:
                    message = subscriber.get(timeout=SSE_TICK_SECONDS)
                    now = time.time()
                    if now - last_ping >= SSE_PING_SECONDS:
                        # Комментарий держит соединение у прокси, именованный
                        # кадр EventSource отдаёт странице — она видит «жив».
                        self.wfile.write(b": ping\n\n")
                        send("ping", {"at": now_iso(), "idle": round(now - last_ping, 1)})
                        last_ping = now
                    if message is None:
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
        target = safe_file(config.PHOTO_DIR, name) if name else None
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

    def serve_variant_photo(self, variant_id: str, frame: str = ""):
        """Фото вариации (18.5, М4; 18.6 — кадры галереи по ?n=).

        Без n отдаётся обложка — байт-в-байт как раньше: касса, бот и
        старые витрины ничего не заметили. n=1… — кадры галереи.
        """
        try:
            gallery = self.api.nom.variant_gallery(variant_id)
        except LookupError:
            return self.send_json(404, {"error": "Вариация не найдена"})
        name = ""
        if frame not in (None, ""):
            try:
                name = gallery[int(frame)]
            except (ValueError, IndexError):
                return self.send_json(404, {"error": "Кадр не найден"})
        else:
            name = gallery[0] if gallery else ""
        return self.serve_photo_file(name)

    # ------------------------------------------------ 8.5: вспомогательные
    def serve_keyframe(self, job_id: str, name: str):
        """Кейфрейм видео печати (идея 61)."""
        d = (config.PHOTO_DIR / "keyframes" / str(job_id)) if job_id else None
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
        target = config.PHOTO_DIR / "bed_reference.jpg"
        if not target.is_file():
            return self.send_json(404, {"error": "Эталон стола не снят — нажмите «Пустой стол» на вкладке принтера"})
        try:
            data = target.read_bytes()
        except OSError:
            return self.send_json(500, {"error": "Эталон стола не читается"})
        self._send_bytes(data, "image/jpeg")

    def serve_bambu_spools_export(self):
        """Экспорт катушек склада в ZIP-архив с JSON-пресетами для Bambu Studio / OrcaSlicer."""
        import io
        import zipfile
        from .materials import generate_bambu_studio_filament_preset

        spools = self.api.repo.spools(include_archived=False)
        buf = io.BytesIO()
        used_names: set[str] = set()

        # db — для уровня «справочник» в температурах: катушка → справочник → каталог
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            readme = (
                "# Пресеты филамента PrintFlow для Bambu Studio / OrcaSlicer\n\n"
                "Как импортировать в Bambu Studio:\n"
                "1. Откройте Bambu Studio.\n"
                "2. Перейдите в меню: Файл -> Импорт -> Импорт конфигураций (File -> Import -> Import Configs...).\n"
                "3. Выберите из этого архива нужные .json файлы филаментов (или импортируйте распакованную папку).\n"
                "4. Профили появятся в списке пользовательских нитей (User Presets) с сохранёнными цветами, температурами и ценой за 1 кг.\n\n"
                "Либо скопируйте файлы напрямую в каталог пользовательских профилей:\n"
                "- Windows: %APPDATA%\\BambuStudio\\user\\default\\filament\\\n"
                "- macOS: ~/Library/Application Support/BambuStudio/user/default/filament/\n"
                "- Linux: ~/.config/BambuStudio/user/default/filament/\n"
            )
            zf.writestr("README.txt", readme.encode("utf-8"))

            for sp in spools:
                preset = generate_bambu_studio_filament_preset(sp, db=self.api.db)
                raw_name = preset.get("name") or "Filament"
                # Очищаем имя от недопустимых символов файловой системы
                clean_name = re.sub(r'[\\/*?:"<>|]', "_", raw_name).strip()
                if not clean_name:
                    clean_name = "Filament"

                filename = f"{clean_name}.json"
                counter = 2
                while filename.lower() in used_names:
                    filename = f"{clean_name}_{counter}.json"
                    counter += 1
                used_names.add(filename.lower())

                json_bytes = json.dumps(preset, indent=4, ensure_ascii=False).encode("utf-8")
                zf.writestr(filename, json_bytes)

        data = buf.getvalue()
        self._send_bytes(data, "application/zip", download="bambu_filament_presets.zip")

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
        except STREAM_DISCONNECT_ERRORS:
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
            # 18.12.1: тело дочитывается до отказа — иначе браузер, который
            # ещё льёт модель, получает обрыв соединения и «Нет связи».
            self._drain_body()
            return self.send_json(403, {"error": "Запрос отклонён: посторонний источник"})
        # 14.0 (идеи 5, 11, 34): контекст запроса, ограничение частоты и
        # идемпотентность. Всё на входе в обработчик, а не внутри веток.
        begin_request(self, "POST", path)
        try:
            bucket = rate_bucket(path)
            if bucket:
                allowed, info = limiter.check(
                    bucket, client_key(self.headers, self.client_address[0]))
                if not allowed:
                    self._drain_body()
                    return self.send_json(429, info)
            if path == "/api/printer/upload":
                return self.handle_upload(query)
            if path == "/api/jobs/upload":
                return self.handle_job_upload()
            if path == "/api/estimate/upload":
                return self.handle_estimate_upload()
            if path == "/api/library/upload":
                # 18.8 (слайсер-карточка): модель STL с компьютера — в
                # библиотеку. Байтовый маршрут, поэтому в цепочке до реестра,
                # как и остальные загрузки: реестр принимает только JSON.
                return self.handle_library_upload()
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
            key = extract_idempotency_key(body, self.headers)
            # /api/jobs/start пока живёт в legacy-ветке api.post, но сам
            # менеджер уже умеет дедупликацию по start_request_id. Связываем
            # общий Idempotency-Key панели с этим полем, чтобы сетевой ретрай
            # одного и того же клика не запускал задание повторно.
            if path == "/api/jobs/start" and key and not str(body.get("start_request_id") or "").strip():
                body["start_request_id"] = key
            # Идемпотентность (идея 5): для маршрутов, помеченных в реестре,
            # повтор с тем же ключом возвращает прежний ответ, а не создаёт
            # вторую сущность.
            route = router.find("POST", path)
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
            # 18.12.1: «Файл слишком большой» и «нет boundary» прилетали из
            # multipart в момент, когда браузер ещё льёт тело. Ответ без
            # дочитывания рвал соединение, и панель показывала «Нет связи»
            # вместо причины. Дочитываем (в пределах лимита) и закрываем
            # соединение: тело может быть прочитано наполовину, и на
            # keep-alive следующий запрос разобрать уже нельзя.
            self._drain_body()
            self.send_json(400, {"error": str(exc)})
            self.close_connection = True
            return
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

