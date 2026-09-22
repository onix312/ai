"""Чтение файлов и индекс знаний ассистента (18.14).

Что здесь происходит и чего здесь сознательно нет.

Навык «ответ по своим документам» (идея И142) состоит из двух половинок:
прочитать файл и найти в прочитанном место. Обе половинки делаются без внешних
библиотек там, где это возможно:

  * `.txt`, `.md`, `.csv`, `.log`, `.json`, `.html`, исходники — как есть;
  * `.docx` и `.xlsx` — это zip с XML внутри, поэтому читаются `zipfile` и
    регулярным выражением: зависимость не нужна, а текст извлекается;
  * `.gcode` — только комментарии и настройки (что печатали, чем и сколько),
    координаты в индекс не попадают: пользы в них нет, а объём в сто раз больше
    полезного текста;
  * `.stl` — только метаданные (имя из заголовка и число треугольников):
    геометрия в текстовый индекс не превращается;
  * `.pdf` — честно требует библиотеку: без `pypdf` навык говорит «нет
    библиотеки PDF», а не делает вид, что прочитал пустоту.

Поиск лексический (см. `store.Store.search`): отрывок с путём и номером строки
проверяется глазами, поэтому доверие к ответу не зависит от модели.

Никакой файл не покидает компьютер: индекс — это своя SQLite ассистента
(`store.py`), а не отправка текста в рантайм. В модель уходят только те отрывки,
которые нашлись по вопросу, и только на loopback-адрес.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import zipfile
from typing import Any

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 400_000
MAX_GCODE_LINES = 400
CHUNK_CHARS = 1400
MAX_CHUNKS = 400
MAX_FACTS = 60
DIGEST_BYTES = 1024 * 1024

# Папки, в которые ассистент не заходит: служебное, чужое и огромное.
SKIP_FOLDERS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
                ".mypy_cache", ".pytest_cache", "dist", "build", ".cache", ".local",
                "appdata", "$recycle.bin", "system volume information", ".idea"}

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".html",
                 ".htm", ".xml", ".ini", ".cfg", ".conf", ".py", ".js", ".css", ".sql",
                 ".yaml", ".yml", ".toml", ".bat", ".ps1", ".sh"}
OFFICE_SUFFIXES = {".docx": "docx", ".xlsx": "xlsx"}
PRINT_SUFFIXES = {".gcode": "gcode", ".stl": "stl", ".3mf": "3mf"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
KNOWLEDGE_HINTS = ("инструк", "чек-лист", "чеклист", "профил", "регламент", "памятка",
                   "adr", "отчёт", "отчет", "README", "readme")

# --- факты: детерминированный разбор (идея И147) ---------------------------
# Каждый факт хранит номер строки: человек проверяет не «модель так сказала», а
# место в своём документе.
_MONEY_RE = re.compile(
    r"(\d[\d\s\u00a0]{0,14}(?:[.,]\d{1,2})?)\s*(₽|руб\.?|р\.|RUB|рублей|рубля)",
    re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{4}-\d{2}-\d{2})\b")
_PHONE_RE = re.compile(r"(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_QTY_RE = re.compile(r"\b(\d{1,5})\s*(шт\.?|штуки|штук|экз\.?|комплекта|комплектов|шт)",
                     re.IGNORECASE)
_ORDER_RE = re.compile(r"(?:заказ|счёт|счет|накладная|договор)\s*(?:№|N|#)?\s*([0-9A-Za-zА-Яа-я/-]{1,20})")


def digest(path: pathlib.Path) -> str:
    """Хеш начала файла: достаточно, чтобы понять, изменился ли документ."""
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            hasher.update(handle.read(DIGEST_BYTES))
    except OSError as exc:
        return f"error:{exc.__class__.__name__}"
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------

def _plain(path: pathlib.Path) -> tuple[str, str]:
    raw = path.read_bytes()[:MAX_FILE_BYTES]
    for encoding in ("utf-8", "cp1251"):
        try:
            return raw.decode(encoding), ""
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "часть файла не в UTF-8 и не в CP1251 — заменены неизвестные символы"


def _strip_tags(xml_text: str) -> str:
    text = re.sub(r"<w:p[ >][^>]*>|</w:p>|<w:br/>|</w:tc>|<w:tab/>", "\n", xml_text)
    text = re.sub(r"<[^>]+>", "", text)
    return (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&apos;", "'"))


def _docx(path: pathlib.Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        parts = [name for name in archive.namelist()
                 if name == "word/document.xml" or name.startswith("word/footnotes")]
        if not parts:
            return "", "в .docx нет word/document.xml — файл повреждён или это не документ"
        return "\n".join(_strip_tags(archive.read(part).decode("utf-8", "replace"))
                         for part in sorted(parts)), ""


def _xlsx(path: pathlib.Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared = [_strip_tags(item) for item in re.findall(
                r"<si>(.*?)</si>", archive.read("xl/sharedStrings.xml").decode("utf-8", "replace"),
                re.DOTALL)]
        cells: list[str] = []
        for name in sorted(n for n in names if n.startswith("xl/worksheets/sheet")):
            xml_text = archive.read(name).decode("utf-8", "replace")
            for value in re.findall(r"<v>(.*?)</v>", xml_text, re.DOTALL)[:5000]:
                cells.append(value)
            for inline in re.findall(r"<t[^>]*>(.*?)</t>", xml_text, re.DOTALL)[:5000]:
                cells.append(_strip_tags(inline))
        if not cells and not shared:
            return "", "в .xlsx не нашлось ни строк, ни таблицы строк"
        text = "\n".join([*shared, *cells])
        return text, ""


def _pdf(path: pathlib.Path) -> tuple[str, str]:
    """PDF без библиотеки не читается — и это говорится прямо."""
    for module in ("pypdf", "PyPDF2"):
        try:
            reader_module = __import__(module)
        except ImportError:
            continue
        try:
            reader = reader_module.PdfReader(str(path))
            pages = [str(page.extract_text() or "") for page in reader.pages[:200]]
            return "\n".join(pages), ""
        except Exception as exc:  # библиотека есть, а файл ей не по зубам
            return "", f"{module} не прочитал файл: {exc.__class__.__name__}"
    return "", ("Нет библиотеки PDF: поставьте pypdf в окружение агента "
                "(`agent/requirements.txt`), тогда .pdf попадёт в индекс")


def _gcode(path: pathlib.Path) -> tuple[str, str]:
    """Из нарезки — только смысл: что печатали, чем и как долго."""
    lines: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle):
                if number >= MAX_GCODE_LINES:
                    break
                stripped = line.strip()
                if stripped.startswith(";"):
                    lines.append(stripped)
                elif stripped.startswith(("M104", "M109", "M140", "M190", "M106")):
                    lines.append(stripped)
    except OSError as exc:
        return "", f"файл не читается: {exc.__class__.__name__}"
    if not lines:
        return "", "в нарезке нет комментариев: сказать о ней нечего"
    return "\n".join(lines), ""


def _stl(path: pathlib.Path) -> tuple[str, str]:
    """Метаданные модели: имя из заголовка и число треугольников."""
    try:
        with path.open("rb") as handle:
            head = handle.read(1024)
    except OSError as exc:
        return "", f"файл не читается: {exc.__class__.__name__}"
    if not head:
        return "", "файл пуст"
    # Текстовый STL начинается с `solid` и содержит `facet`: геометрию не
    # индексируем, считаем треугольники по строкам. Проверяем до проверки длины
    # бинарного заголовка — иначе короткий текстовый файл считается битым.
    low = head[:512].lower()
    if low.startswith(b"solid") and b"facet" in low:
        try:
            facets = path.read_text(encoding="utf-8", errors="replace").count("facet normal")
        except OSError:
            facets = 0
        return f"Модель STL (текстовая): {facets} треугольников", ""
    if len(head) < 84:
        return "", "файл короче заголовка STL"
    name = head[:80].split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    facets = int.from_bytes(head[80:84], "little")
    return f"Модель STL: {name or 'без имени'} · {facets} треугольников", ""


def _image(path: pathlib.Path) -> tuple[str, str]:
    """Фото в текстовый индекс не превращается: для него нужно зрение (И164)."""
    return "", ("Изображение не читается как текст: описание фото появится, "
                "когда заработает зрение — зрением экрана (идея И164), "
                "а не в индексе документов")


def read_document(path: pathlib.Path | str) -> dict[str, Any]:
    """Прочитать файл. Ответ всегда с причиной, если текста нет."""
    file = pathlib.Path(path)
    suffix = file.suffix.lower()
    out: dict[str, Any] = {"ok": False, "text": "", "kind": "", "reason": "",
                           "path": str(file), "title": file.name}
    try:
        if not file.is_file():
            out["reason"] = "Файл не найден"
            return out
        size = file.stat().st_size
    except OSError as exc:
        out["reason"] = f"Файл недоступен: {exc.__class__.__name__}"
        return out
    if size > MAX_FILE_BYTES:
        out["reason"] = f"Файл больше {MAX_FILE_BYTES // (1024 * 1024)} МБ — индекс его не читает"
        return out
    if not size:
        out["reason"] = "Файл пуст"
        return out

    try:
        if suffix in TEXT_SUFFIXES:
            text, note = _plain(file)
            kind = "text"
        elif suffix in OFFICE_SUFFIXES:
            reader = _docx if OFFICE_SUFFIXES[suffix] == "docx" else _xlsx
            text, note = reader(file)
            kind = OFFICE_SUFFIXES[suffix]
        elif suffix == ".pdf":
            text, note = _pdf(file)
            kind = "pdf"
        elif suffix in PRINT_SUFFIXES:
            if suffix == ".gcode":
                text, note = _gcode(file)
            elif suffix == ".stl":
                text, note = _stl(file)
            else:
                text, note = "", ("3MF не разбирается: это архив с моделью и настройками, "
                                  "читайте его в нарезчике")
            kind = suffix.lstrip(".")
        elif suffix in IMAGE_SUFFIXES:
            text, note = _image(file)
            kind = "image"
        else:
            text, note = "", f"Расширение «{suffix or 'без расширения'}» в индекс не входит"
            kind = ""
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        out["reason"] = f"Файл не прочитан: {exc.__class__.__name__}"
        return out

    out.update(kind=kind, reason=note if not text else "", text=text[:MAX_TEXT_CHARS])
    out["ok"] = bool(text.strip())
    if not out["ok"] and not out["reason"]:
        out["reason"] = "В файле нет текста"
    return out


def is_knowledge(path: pathlib.Path | str) -> bool:
    """Документ относится к знаниям цеха, а не к личным бумагам.

    Признак простой и объяснимый: папка знаний (задана владельцем) или слово в
    имени, по которому инструкцию отличить от договора. Отдельный признак нужен,
    чтобы навык `knowledge.shop` не отвечал по личным файлам.

    Папка `docs/` считается знаниями только когда это документация репозитория,
    а не любая папка с таким именем во временной директории тестов: иначе
    договор в `/tmp/docs/договор.md` считался бы инструкцией.
    """
    file = pathlib.Path(path)
    name = file.name.casefold()
    parts = {part.casefold() for part in file.parts}
    # «знания» и «knowledge» — явные папки знаний; `docs` — только если внутри
    # репозитория (рядом лежит `README.md` или `ПОМОЩНИК.md`), иначе тестовая
    # временная папка `docs` из `test_assistant_documents` считалась бы знаниями.
    if "знания" in parts or "knowledge" in parts:
        return True
    if "docs" in parts:
        # Эвристика для репозитория: `docs/` рядом с корнем содержит справку.
        try:
            # Если путь внутри репозитория, считаем знаниями; иначе — нет.
            repo_root = pathlib.Path(__file__).resolve().parents[1]
            file.resolve(strict=False).relative_to(repo_root)
            return True
        except (ValueError, OSError):
            pass
    return any(hint.casefold() in name for hint in KNOWLEDGE_HINTS)


# ---------------------------------------------------------------------------
# Нарезка и обход папок
# ---------------------------------------------------------------------------

def chunks_of(text: str) -> list[dict[str, Any]]:
    """Текст → отрывки по границам строк, с номером первой строки."""
    out: list[dict[str, Any]] = []
    buffer: list[str] = []
    size = 0
    first_line = 1
    line = 1
    for raw in str(text).splitlines():
        if size and size + len(raw) > CHUNK_CHARS:
            out.append({"seq": len(out), "first_line": first_line,
                        "text": "\n".join(buffer).strip()[:CHUNK_CHARS * 2]})
            buffer, size, first_line = [], 0, line
        buffer.append(raw)
        size += len(raw) + 1
        line += 1
        if len(out) >= MAX_CHUNKS:
            break
    if buffer and len(out) < MAX_CHUNKS:
        out.append({"seq": len(out), "first_line": first_line,
                    "text": "\n".join(buffer).strip()[:CHUNK_CHARS * 2]})
    return [chunk for chunk in out if chunk["text"]]


_STOPWORDS = frozenset((
    "что", "как", "где", "кто", "сколько", "почему", "зачем", "когда", "какой",
    "какая", "какие", "это", "этот", "эта", "эти", "мне", "меня", "нам", "нас",
    "сейчас", "сегодня", "вчера", "завтра", "было", "будет", "есть", "нет",
    "давай", "покажи", "расскажи", "про", "для", "при", "над", "под", "без",
    "все", "всё", "весь", "пришли", "the", "and", "for", "with",
))


def tokens_of(text: str) -> list[str]:
    """Слова запроса: без стоп-слов, короче двух символов не ищем, повторы убираем."""
    seen: list[str] = []
    for word in re.split(r"[^0-9A-Za-zА-Яа-яЁё+-]+", str(text or "").casefold()):
        word = word.strip("+-")
        if len(word) < 2 or word in _STOPWORDS or word in seen:
            continue
        seen.append(word)
    return seen


def walk(folders: list[str | pathlib.Path]) -> list[pathlib.Path]:
    """Файлы для индекса: без служебных папок, без скрытых, в пределах размера."""
    found: list[pathlib.Path] = []
    seen: set[str] = set()
    for folder in folders:
        root = pathlib.Path(str(folder or "")).expanduser()
        if not root.exists():
            continue
        if root.is_file():
            key = str(root)
            if key not in seen:
                seen.add(key)
                found.append(root)
            continue
        for path in sorted(root.rglob("*")):
            try:
                if not path.is_file():
                    continue
                if any(part.casefold() in SKIP_FOLDERS or part.startswith(".")
                       for part in path.relative_to(root).parts[:-1]):
                    continue
                if path.name.startswith("."):
                    continue
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                found.append(path)
            except OSError:
                continue
    return found


def index(folders: list[str | pathlib.Path], store: Any) -> dict[str, Any]:
    """Индексировать папки. Повторно читаются только изменённые файлы."""
    added = updated = same = failed = 0
    unreadable: list[dict[str, str]] = []
    known = store.known_paths()
    seen_paths: set[str] = set()
    for path in walk(folders):
        key = str(path)
        seen_paths.add(key)
        try:
            stat = path.stat()
        except OSError:
            failed += 1
            continue
        stamp = digest(path)
        if key in known and store.unchanged(key, stat.st_size, stat.st_mtime, stamp):
            same += 1
            continue
        read = read_document(path)
        if not read["ok"]:
            failed += 1
            if len(unreadable) < 10:
                unreadable.append({"path": key, "reason": read["reason"]})
            continue
        chunks = chunks_of(read["text"])
        store.put_document(key, read["kind"], read["title"], stat.st_size,
                           stat.st_mtime, stamp, chunks)
        if key in known:
            updated += 1
        else:
            added += 1
    for old in known - seen_paths:
        store.forget_document(old)
    return {"ok": True, "folders": [str(f) for f in folders], "indexed": added + updated,
            "new": added, "updated": updated, "unchanged": same, "unreadable": failed,
            "samples": unreadable, "stats": store.stats()}


# ---------------------------------------------------------------------------
# Факты (И147)
# ---------------------------------------------------------------------------

def facts_from_text(text: str, path: str = "") -> list[dict[str, Any]]:
    """Суммы, даты, телефоны, почты, количества и номера — с местом в файле.

    Разбор детерминированный: модель здесь не участвует, поэтому факт всегда
    можно найти глазами по номеру строки. Это же правило, по которому помощник
    в панели не считает деньги (`assistant.py`, пункт 4 контракта).
    """
    facts: list[dict[str, Any]] = []

    def add(kind: str, value: str, place: str) -> None:
        if len(facts) >= MAX_FACTS:
            return
        clean = " ".join(str(value).split())[:200]
        if clean and not any(f["kind"] == kind and f["value"] == clean for f in facts):
            facts.append({"kind": kind, "value": clean, "place": place, "path": str(path)})

    for number, line in enumerate(str(text or "").splitlines(), start=1):
        if not line.strip():
            continue
        place = f"строка {number}"
        for match in _MONEY_RE.finditer(line):
            amount = re.sub(r"[\s\u00a0]", "", match.group(1)).replace(",", ".")
            add("сумма", f"{amount} ₽", place)
        for match in _DATE_RE.finditer(line):
            add("дата", match.group(1), place)
        for match in _PHONE_RE.finditer(line):
            phone = re.sub(r"[^\d+]", "", match.group(0))
            add("телефон", phone, place)
        for match in _EMAIL_RE.finditer(line):
            add("почта", match.group(0), place)
        for match in _QTY_RE.finditer(line):
            add("количество", f"{match.group(1)} {match.group(2)}", place)
        for match in _ORDER_RE.finditer(line):
            add("номер", match.group(0).strip(), place)
    return facts


def facts_of_file(path: pathlib.Path | str) -> dict[str, Any]:
    """Факты одного файла: прочитали, разобрали, вернули с причиной отказа."""
    read = read_document(path)
    if not read["ok"]:
        return {"ok": False, "path": str(path), "facts": [], "reason": read["reason"]}
    return {"ok": True, "path": str(path), "kind": read["kind"],
            "facts": facts_from_text(read["text"], str(path)), "reason": ""}
