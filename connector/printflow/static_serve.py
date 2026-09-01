"""Раздача статических файлов панели (Н18).

Вынесено из `api.py`, где этот код не относился ни к API, ни к транспорту:
это обычный файловый сервер с кэшем. Отдельный модуль даёт три вещи:

  * `api.py` короче и читается как список маршрутов, а не как смесь;
  * политику кэша можно тестировать без HTTP-обработчика (`cache_policy`);
  * правила «что можно отдать браузеру» собраны в одном месте — раньше
    проверка `safe_file` была размазана по пяти методам.

`Handler.serve_static` остаётся тонкой обёрткой, чтобы существующие тесты
(`test_static_routes.py` вызывает метод напрямую) не менялись.
"""
from __future__ import annotations

import mimetypes
import re
import urllib.parse
from pathlib import Path

from .config import SITE
from .http_helpers import safe_file

# mimetypes про webmanifest ещё не знает, а без правильного типа браузер
# не зарегистрирует PWA и панель не поставится на домашний экран.
_EXTRA_MIME = {".webmanifest": "application/manifest+json"}

# HTML и service worker всегда перечитываем: это точки входа, и залипший
# кэш здесь ломает обновление у всех сразу.
_NO_STORE_NAMES = {"sw.js"}
_NO_STORE_SUFFIXES = {".html", ".htm"}

_VERSIONED = re.compile(r"[?&]v=")


def content_type(target: Path) -> str:
    """MIME-тип файла; для текста добавляем кодировку."""
    ctype = _EXTRA_MIME.get(target.suffix) or \
        mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in (
            "application/javascript", "application/json", "application/manifest+json"):
        ctype += "; charset=utf-8"
    return ctype


def cache_policy(target: Path, raw_path: str = "") -> str:
    """Политика кэша (идея 41/Б5).

    HTML — `no-store`: точка входа, залипший кэш ломает обновление у всех.
    Версионированные ассеты (`app.js?v=15.0.0`) меняются только вместе с
    релизом, поэтому их можно держать год — раньше даже они шли с no-store,
    и панель на телефоне/ТВ каждый раз перекачивала ~1,5 МБ.
    """
    if target.suffix in _NO_STORE_SUFFIXES or target.name in _NO_STORE_NAMES:
        return "no-store"
    if _VERSIONED.search(raw_path or ""):
        return "public, max-age=31536000, immutable"
    return "public, max-age=3600"


def resolve_target(path: str) -> Path | None:
    """Найти файл внутри `site/`, понимая короткие адреса и старый .htm.

    Короткие адреса без расширения (`/m`, `/order`, `/track`, `/shelf`) проще
    диктовать вслух и печатать на ценнике. Старые ссылки на `.htm` не должны
    превращаться в 404 — иначе внешние закладки ломаются после переименования.
    """
    rel = urllib.parse.unquote(path.lstrip("/")) or "index.html"
    target = safe_file(SITE, rel)
    if target is None:
        return None
    if target.is_dir():
        target = target / "index.html"
    if not target.exists() and not target.suffix:
        alias = safe_file(SITE, rel + ".html")
        if alias is not None and alias.exists():
            return alias
    if not target.exists() and target.suffix == ".htm":
        alias = safe_file(SITE, rel[:-4] + ".html")
        if alias is not None and alias.exists():
            return alias
    return target if target.exists() else None


def etag_for(target: Path) -> str:
    """ETag из времени изменения и размера — без чтения содержимого."""
    stat = target.stat()
    return f'"{int(stat.st_mtime_ns):x}-{stat.st_size:x}"'


def serve_static(handler, path: str) -> None:
    """Отдать файл панели, включая 304 по совпавшему ETag."""
    target = resolve_target(path)
    if target is None:
        rel = urllib.parse.unquote(path.lstrip("/")) or "index.html"
        # 403, когда путь пытался выйти за каталог, и 404 — когда файла нет.
        code = 403 if safe_file(SITE, rel) is None else 404
        return handler.send_error(code, "Forbidden" if code == 403 else "Not Found")
    ctype = content_type(target)
    # self.path может отсутствовать, когда метод вызывают напрямую
    # (так делает test_static_routes) — тогда считаем ассет неверсионированным.
    cache = cache_policy(target, getattr(handler, "path", "") or "")
    etag = etag_for(target)
    headers = getattr(handler, "headers", None)
    inm = headers.get("If-None-Match") if headers is not None else None
    if inm == etag and cache != "no-store":
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", cache)
        handler.end_headers()
        return
    data = target.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("ETag", etag)
    handler.send_header("Cache-Control", cache)
    handler.end_headers()
    handler.wfile.write(data)
