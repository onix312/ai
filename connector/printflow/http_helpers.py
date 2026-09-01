"""HTTP-утилиты PrintFlow (Н18).

`api.py` вырос до 4800 строк, потому что в нём смешались три разные вещи:
транспорт HTTP, раздача файлов и логика маршрутов. Здесь — только утилиты
транспорта, у которых нет ни состояния, ни доступа к базе:

  * `safe_file` — защита от выхода за каталог (`../../etc/passwd`);
  * `request_length` — проверка Content-Length до чтения тела;
  * `request_origin_allowed` — защита от управления принтером с чужой страницы;
  * `begin_request` — сквозной идентификатор запроса (идея 11);
  * `rate_bucket` — выбор корзины ограничения частоты (идея 34);
  * `parse_multipart`, `_upload_filename`, `save_upload`, `_form_bool` —
    приём загружаемых файлов.

Модуль намеренно не импортирует `api` — иначе разборка была бы фикцией:
зависимость идёт в одну сторону, и утилиты можно тестировать без сервера.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
from pathlib import Path

from . import config


def uploads_root() -> Path:
    """Каталог загрузок. Читается из config при каждом вызове.

    Раньше имя UPLOAD_DIR импортировалось в модуль один раз, и подмена
    каталога в тестах не доходила до функции: она продолжала писать в
    боевую папку. Чтение через модуль config убирает эту ловушку.
    """
    return config.UPLOAD_DIR

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


def begin_request(handler, method: str, path: str) -> str:
    """Задать контекст запроса (идея 11) и вернуть его идентификатор.

    Идентификатор берётся из заголовка клиента, если он есть (панель может
    прислать свой), иначе генерируется. Дальше он виден в каждой строке
    лога этого запроса — от HTTP-обработчика до SQL.
    """
    import uuid
    from .logging_setup import clear_request_context, set_request_context
    clear_request_context()
    incoming = ""
    try:
        incoming = str(handler.headers.get("X-Request-Id") or "").strip()[:64]
    except Exception:
        incoming = ""
    request_id = incoming or uuid.uuid4().hex[:16]
    set_request_context(request_id)
    try:
        handler.pf_request_id = request_id
    except Exception:
        pass
    from .logging_setup import log
    log().debug("%s %s", method, path)
    return request_id


def rate_bucket(path: str) -> str:
    """Корзина ограничения частоты для пути. Пусто — не ограничиваем."""
    if path.startswith("/api/public/order"):
        return "public_order"
    if path.startswith("/api/public/catalog"):
        return "public_catalog"
    if path.startswith("/api/track") or path.startswith("/api/public/my"):
        return "public_track"
    if path.startswith("/api/cloud/login") or path.startswith("/api/cloud/code"):
        return "login"
    if "upload" in path:
        return "upload"
    if path.startswith("/api/public/"):
        return "public_track"
    return ""


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
    root = uploads_root()
    root.mkdir(parents=True, exist_ok=True)
    requested = _upload_filename(name)
    suffix = Path(requested).suffix
    stem = requested[:-len(suffix)] if suffix else requested
    target = root / requested
    digest = hashlib.sha256(data).hexdigest()[:10]
    if target.exists():
        try:
            same = target.stat().st_size == len(data) and hashlib.sha256(
                target.read_bytes()).hexdigest()[:10] == digest
        except OSError:
            same = False
        if not same:
            target = root / f"{stem}-{digest}{suffix}"
            # Коллизия хеша крайне маловероятна, но не затираем файл и при ней.
            counter = 2
            while target.exists():
                target = root / f"{stem}-{digest}-{counter}{suffix}"
                counter += 1
    created = not target.exists()
    if created:
        target.write_bytes(data)
    return target.name, target, created


def _form_bool(value: object, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().casefold() in {"1", "true", "yes", "on", "да"}
