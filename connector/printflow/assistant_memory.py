"""Память помощника панели (18.21): что владелец просил запомнить и о чём шёл разговор.

Зачем, если у агента своя память. Агент — программа компьютера, он может быть
выключен, а помощник панели работает всегда: с телефона смены, из окна
PrintFlow, голосом через агента. До 18.21 у него не было памяти вовсе: история
разговора жила в браузере (шесть реплик по 400 символов) и терялась при
перезагрузке страницы, а «запомни, что Мария берёт только PETG» было некуда
положить. Поэтому две таблицы в базе цеха:

  * `assistant_memory` — факты, правила и предпочтения владельца; помощник
    подмешивает найденное в рассуждение и отвечает «про Марию помню: …»;
  * `assistant_dialog` — реплики разговора с метками (о каком станке, заказе,
    клиенте шла речь, какое действие предложено). Метки — то, без чего «а у
    второго?» и «выдай его» невозможно понять.

Таблицы создаются лениво (`ensure_schema`), как у `spool_mapping_repo`: схема
базы цеха не меняет номер версии ради помощника, а старая база получает
таблицы при первом разговоре.

Поиск по памяти лексический, по основам слов («Марии» = «Мария»): предсказуемо,
проверяемо глазами и без модели. Модель получает найденное как факты, а не как
«похожее по смыслу».
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any

from .config import now_iso

KINDS = ("fact", "preference", "profile", "person", "rule")
MAX_TEXT = 500
MAX_TURNS = 400
MAX_TURN_CHARS = 4000

_READY_FLAG = "_assistant_memory_ready"
_READY_LOCK = threading.Lock()
_WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
# Окончания без «ия/ие/ию»: иначе «Мария» и «Марии» дают разные основы.
_ENDINGS = ("ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ях", "ах",
            "ов", "ев", "ей", "ой", "ый", "ий", "ая", "яя", "ое", "ее", "ую", "юю", "ом", "ем",
            "ам", "ям", "ы", "и", "а", "я", "о", "е", "у", "ю", "ь")
_STOP = frozenset((
    "что", "это", "как", "мне", "меня", "мой", "моя", "мои", "моё", "мое", "для", "про",
    "при", "так", "там", "тут", "его", "еще", "ещё", "уже", "или", "the", "and", "всегда",
    "запомни", "помни", "забудь", "знаешь", "помнишь", "обо", "все", "всё", "был", "была",
))

_REMEMBER_RE = re.compile(
    r"^(?:запомни|запиши в память|занеси в память|имей в виду|учти|помни|сохрани в память)"
    r"(?:\s*,\s*|\s*:\s*|\s+)(?:что\s+|то,?\s+что\s+)?(?P<text>.{2,})$", re.IGNORECASE)
_FORGET_RE = re.compile(
    r"^(?:забудь|удали из памяти|сотри из памяти|выкинь из памяти)"
    r"(?:\s*,\s*|\s*:\s*|\s+)(?:что\s+|про\s+|о\s+|об\s+)?(?P<text>.{2,})$", re.IGNORECASE)
_RECALL_RE = re.compile(
    r"^(?:что ты (?:помнишь|знаешь|запомнил)|что (?:у тебя )?в памяти|вспомни|покажи память|"
    r"что я (?:тебе )?(?:говорил|рассказывал))(?:\s+(?:обо|о|об|про)\s+(?P<text>.+))?$", re.IGNORECASE)
_NAME_RE = re.compile(r"^(?:меня зовут|мое имя|моё имя|зови меня|называй меня)\s+(?P<name>[A-Za-zА-Яа-яЁё-]{2,30})",
                      re.IGNORECASE)
_LEAD_RE = re.compile(r"^(?:(?:ноза|нозза|nozza|помощник)\s*,?\s+)?(?:(?:пожалуйста|будь добр)\s*,?\s+)?",
                      re.IGNORECASE)


def ensure_schema(db: Any) -> None:
    """Таблицы памяти и разговора — один раз на объект базы.

    Метка ставится на сам объект, а не в множество `id(db)`: после закрытия
    базы Python переиспользует адрес, и новая база считалась бы готовой.
    """
    if getattr(db, _READY_FLAG, False):
        return
    db.execute(
        "CREATE TABLE IF NOT EXISTS assistant_memory("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "kind TEXT NOT NULL DEFAULT 'fact', subject TEXT DEFAULT '', text TEXT NOT NULL, "
        "norm TEXT NOT NULL, source TEXT DEFAULT '', pinned INTEGER DEFAULT 0, "
        "uses INTEGER DEFAULT 0, last_used TEXT DEFAULT '')")
    db.execute("CREATE INDEX IF NOT EXISTS idx_assistant_memory_norm ON assistant_memory(norm)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS assistant_dialog("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, session TEXT NOT NULL, "
        "role TEXT NOT NULL, text TEXT NOT NULL, meta TEXT DEFAULT '{}')")
    db.execute("CREATE INDEX IF NOT EXISTS idx_assistant_dialog_session ON assistant_dialog(session, id)")
    with _READY_LOCK:
        try:
            setattr(db, _READY_FLAG, True)
        except (AttributeError, TypeError):
            pass  # объект без атрибутов: CREATE IF NOT EXISTS дешёв и при повторе


# ---------------------------------------------------------------------------
# Слова
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Текст для сравнения: регистр, ё, пунктуация и пробелы не различаются."""
    return " ".join(_WORD_RE.findall(str(text or "").casefold().replace("ё", "е")))


def stems(text: str) -> list[str]:
    """Основы слов: «Марии», «Марией», «Мария» → «мари»."""
    out: list[str] = []
    for word in normalize(text).split():
        if word in _STOP or (len(word) < 3 and not word.isdigit()):
            continue
        stem = word
        if len(stem) > 4 and not stem.isdigit():
            for ending in _ENDINGS:
                if stem.endswith(ending) and len(stem) - len(ending) >= 3:
                    stem = stem[:-len(ending)]
                    break
        stem = stem[:6]
        if stem not in out:
            out.append(stem)
    return out


def score(query_stems: list[str], row: dict[str, Any]) -> float:
    """Похожесть записи на вопрос: доля совпавших основ плюс закрепление и частота."""
    if not query_stems:
        return 0.0
    have = set(stems(f"{row.get('subject') or ''} {row.get('text') or ''}"))
    hits = sum(1 for stem in query_stems if stem in have)
    if not hits:
        return 0.0
    return round(hits / len(query_stems) + (0.25 if row.get("pinned") else 0.0)
                 + min(0.2, int(row.get("uses") or 0) * 0.02), 3)


def memory_command(text: str) -> tuple[str, str]:
    """(«remember» | «forget» | «recall» | «name» | «whoami» | «», содержимое)."""
    clean = _LEAD_RE.sub("", " ".join(str(text or "").split()), count=1).strip()
    clean = re.sub(r"[\s.!]+$", "", clean)
    low = clean.casefold()
    for kind, pattern in (("remember", _REMEMBER_RE), ("forget", _FORGET_RE)):
        match = pattern.match(clean)
        if match:
            return kind, match.group("text").strip(" .,!")
    match = _RECALL_RE.match(clean.rstrip("?"))
    if match:
        return "recall", (match.group("text") or "").strip(" .,!?")
    match = _NAME_RE.match(clean)
    if match:
        return "name", match.group("name").strip().capitalize()
    if re.search(r"как меня зовут|ты знаешь,? как меня зовут", low):
        return "whoami", ""
    return "", ""


# ---------------------------------------------------------------------------
# Память
# ---------------------------------------------------------------------------

def remember(db: Any, text: str, kind: str = "fact", subject: str = "", source: str = "chat",
             pinned: bool = False) -> dict[str, Any]:
    """Запомнить. Повтор не плодит дубль; новое имя владельца заменяет старое."""
    ensure_schema(db)
    clean = " ".join(str(text or "").split())[:MAX_TEXT]
    if not clean:
        return {"ok": False, "reason": "Пустая запись памяти"}
    kind = kind if kind in KINDS else "fact"
    subject = " ".join(str(subject or "").split())[:80]
    norm = normalize(clean)
    at = now_iso()
    same = db.one("SELECT * FROM assistant_memory WHERE norm=? LIMIT 1", (norm,))
    if same:
        db.execute("UPDATE assistant_memory SET updated_at=? WHERE id=?", (at, same["id"]))
        return {"ok": True, "memory": {**same, "updated_at": at}, "duplicate": True, "reason": ""}
    if subject and kind in ("profile", "preference"):
        old = db.one("SELECT * FROM assistant_memory WHERE kind=? AND subject=? LIMIT 1", (kind, subject))
        if old:
            db.execute("UPDATE assistant_memory SET text=?, norm=?, updated_at=?, source=? WHERE id=?",
                       (clean, norm, at, str(source)[:40], old["id"]))
            row = db.one("SELECT * FROM assistant_memory WHERE id=?", (old["id"],)) or {}
            return {"ok": True, "memory": row, "replaced": old["text"], "reason": ""}
    cursor = db.execute(
        "INSERT INTO assistant_memory(at, updated_at, kind, subject, text, norm, source, pinned) "
        "VALUES(?,?,?,?,?,?,?,?)", (at, at, kind, subject, clean, norm, str(source)[:40], 1 if pinned else 0))
    row = db.one("SELECT * FROM assistant_memory WHERE id=?", (int(cursor.lastrowid or 0),)) or {}
    return {"ok": True, "memory": row, "reason": ""}


def memories(db: Any, limit: int = 50, kind: str = "") -> list[dict[str, Any]]:
    """Записи памяти: закреплённые и свежие сверху."""
    ensure_schema(db)
    limit = max(1, min(500, int(limit or 50)))
    if kind:
        return db.query("SELECT * FROM assistant_memory WHERE kind=? "
                        "ORDER BY pinned DESC, updated_at DESC, id DESC LIMIT ?", (kind, limit))
    return db.query("SELECT * FROM assistant_memory ORDER BY pinned DESC, updated_at DESC, id DESC LIMIT ?",
                    (limit,))


def recall(db: Any, query: str, limit: int = 5, touch: bool = True) -> list[dict[str, Any]]:
    """Найти в памяти по основам слов. `touch` — посчитать использование."""
    ensure_schema(db)
    wanted = stems(query)
    if not wanted:
        return []
    scored = []
    for row in db.query("SELECT * FROM assistant_memory ORDER BY id DESC LIMIT 2000"):
        value = score(wanted, row)
        if value > 0:
            scored.append({**row, "score": value})
    scored.sort(key=lambda row: (-row["score"], -int(row["id"])))
    top = scored[:max(1, min(50, int(limit or 5)))]
    if touch and top:
        at = now_iso()
        for row in top:
            db.execute("UPDATE assistant_memory SET uses=uses+1, last_used=? WHERE id=?", (at, row["id"]))
    return top


def forget(db: Any, what: Any) -> list[dict[str, Any]]:
    """Стереть по номеру или по словам: только уверенное совпадение (≥ половины слов)."""
    ensure_schema(db)
    if isinstance(what, int) or str(what or "").strip().isdigit():
        rows = db.query("SELECT * FROM assistant_memory WHERE id=?", (int(what),))
    else:
        rows = [row for row in recall(db, str(what or ""), 3, touch=False) if row["score"] >= 0.5]
        if len(rows) > 1 and rows[0]["score"] > rows[1]["score"]:
            rows = rows[:1]
    for row in rows:
        db.execute("DELETE FROM assistant_memory WHERE id=?", (row["id"],))
    return rows


def pin(db: Any, memory_id: int, pinned: bool = True) -> bool:
    """Закрепить запись: она всегда видна модели и стоит сверху."""
    ensure_schema(db)
    cursor = db.execute("UPDATE assistant_memory SET pinned=? WHERE id=?", (1 if pinned else 0, int(memory_id)))
    return bool(cursor.rowcount)


def owner_name(db: Any) -> str:
    """Имя владельца, если он представился («меня зовут …»)."""
    ensure_schema(db)
    row = db.one("SELECT text FROM assistant_memory WHERE kind='profile' AND subject='имя' LIMIT 1")
    if not row:
        return ""
    return str(row["text"]).replace("Владельца зовут", "").strip()


# ---------------------------------------------------------------------------
# Разговор
# ---------------------------------------------------------------------------

def session_key(raw: Any) -> str:
    return re.sub(r"[^0-9A-Za-z_.:-]+", "", str(raw or "main"))[:40] or "main"


def add_turn(db: Any, session: str, role: str, text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Реплика в разговор. Хвост старше MAX_TURNS реплик сессии уходит."""
    ensure_schema(db)
    session = session_key(session)
    role = role if role in ("user", "assistant") else "assistant"
    clean = str(text or "").strip()[:MAX_TURN_CHARS]
    if not clean:
        return {}
    cursor = db.execute("INSERT INTO assistant_dialog(at, session, role, text, meta) VALUES(?,?,?,?,?)",
                        (now_iso(), session, role, clean,
                         json.dumps(meta or {}, ensure_ascii=False, default=str)[:6000]))
    db.execute("DELETE FROM assistant_dialog WHERE session=? AND id <= "
               "(SELECT id FROM assistant_dialog WHERE session=? ORDER BY id DESC LIMIT 1 OFFSET ?)",
               (session, session, MAX_TURNS))
    return {"id": int(cursor.lastrowid or 0), "session": session, "role": role}


def dialog(db: Any, session: str = "main", limit: int = 20) -> list[dict[str, Any]]:
    """Последние реплики сессии по порядку, метки разобраны."""
    ensure_schema(db)
    rows = db.query("SELECT * FROM assistant_dialog WHERE session=? ORDER BY id DESC LIMIT ?",
                    (session_key(session), max(1, min(200, int(limit or 20)))))
    out = []
    for row in reversed(rows):
        try:
            row["meta"] = json.loads(row.get("meta") or "{}")
        except json.JSONDecodeError:
            row["meta"] = {}
        out.append(row)
    return out


def sessions(db: Any, limit: int = 20) -> list[dict[str, Any]]:
    """Сессии разговора: где говорили и когда в последний раз."""
    ensure_schema(db)
    return db.query("SELECT session, COUNT(*) AS turns, MAX(at) AS last_at FROM assistant_dialog "
                    "GROUP BY session ORDER BY last_at DESC LIMIT ?", (max(1, min(100, int(limit or 20))),))


def clear_dialog(db: Any, session: str = "main") -> int:
    """Начать разговор заново: реплики сессии стираются, память остаётся."""
    ensure_schema(db)
    cursor = db.execute("DELETE FROM assistant_dialog WHERE session=?", (session_key(session),))
    return int(cursor.rowcount or 0)


def stats(db: Any) -> dict[str, int]:
    ensure_schema(db)
    memory_rows = db.one("SELECT COUNT(*) AS n FROM assistant_memory") or {"n": 0}
    dialog_rows = db.one("SELECT COUNT(*) AS n FROM assistant_dialog") or {"n": 0}
    return {"memories": int(memory_rows["n"]), "turns": int(dialog_rows["n"])}
