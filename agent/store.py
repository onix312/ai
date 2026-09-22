"""Своя база ассистента (18.14): память, индекс, журнал, заметки, навыки.

Почему у ассистента своя SQLite, а не таблицы в базе PrintFlow.

Ассистент компьютера — не раздел цеха. Он знает про файлы на диске, про свои
действия в чужих окнах и про предпочтения владельца; базе заказов эти сведения
не нужны, а их запись в неё сделала бы резервную копию цеха копией чужой жизни.
Поэтому:

  * своя база в `~/.printflow/assistant.db` (путь меняется переменной
    `PRINTFLOW_ASSISTANT_DB`), отдельный файл и отдельный бэкап;
  * база PrintFlow не открывается агентом вовсе: факты цеха он получает по
    loopback через `panel_client` (ADR-0004);
  * журнал действий на компьютере живёт здесь и не удаляется навыками — стереть
    след может только человек, удалив файл.

Поиск по индексу намеренно лексический (совпадение слов), а не семантический:
векторная база потребовала бы модель для каждого текста и второй источник
правды о том, что найдено. Отрывок с путём и номером строки проверяем глазами,
а «похожий по смыслу» текст — нет.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import threading
import time
from typing import Any

DEFAULT_PATH = pathlib.Path(os.environ.get("PRINTFLOW_ASSISTANT_DB", "")
                            or (pathlib.Path.home() / ".printflow" / "assistant.db"))
MAX_JOURNAL_LIMIT = 500
MAX_SEARCH_ROWS = 400

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS journal(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        skill TEXT NOT NULL,
        outcome TEXT NOT NULL,
        detail TEXT DEFAULT '',
        target TEXT DEFAULT '',
        params TEXT DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS documents(
        path TEXT PRIMARY KEY,
        kind TEXT DEFAULT '',
        title TEXT DEFAULT '',
        size INTEGER DEFAULT 0,
        mtime REAL DEFAULT 0,
        digest TEXT DEFAULT '',
        chunks INTEGER DEFAULT 0,
        indexed_at TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS chunks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        seq INTEGER NOT NULL,
        first_line INTEGER DEFAULT 1,
        text TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path)",
    """CREATE TABLE IF NOT EXISTS facts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        path TEXT NOT NULL,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        place TEXT DEFAULT '',
        used INTEGER DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS facts_path ON facts(path)",
    """CREATE TABLE IF NOT EXISTS notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        text TEXT NOT NULL,
        due TEXT DEFAULT '',
        done INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS skills(
        name TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        learned_at TEXT NOT NULL)""",
)


def now_iso() -> str:
    """Время в том же виде, что и в базе PrintFlow: сравнение двух журналов глазами."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def default_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PRINTFLOW_ASSISTANT_DB", "") or DEFAULT_PATH)


class Store:
    """Одно соединение под замком: сервер агента многопоточный."""

    def __init__(self, path: str | pathlib.Path | None = None) -> None:
        self.path = pathlib.Path(path if path is not None else default_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # `lower()` в SQLite знает только латиницу, а индекс русский: приведение
        # регистра делает та же функция, что и в базе PrintFlow (`pylower`).
        self._conn.create_function("pylower", 1, lambda value: str(value or "").casefold(),
                                   deterministic=True)
        with self._lock:
            for statement in SCHEMA:
                self._conn.execute(statement)
            self._conn.commit()

    # --- служебное --------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def _run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    # --- журнал (И178) ----------------------------------------------------
    def journal(self, skill: str, outcome: str, detail: str = "",
                target: str = "", params: dict | None = None) -> dict[str, Any]:
        """След действия на компьютере: навык, исход, параметры, куда смотрели.

        Пишется и успех, и отказ: причина отказа — то, что показывает
        `agent.why`, и без записи она была бы догадкой.
        """
        at = now_iso()
        row_params = json.dumps(params or {}, ensure_ascii=False, sort_keys=True)[:4000]
        cursor = self._run(
            "INSERT INTO journal(at,skill,outcome,detail,target,params) VALUES(?,?,?,?,?,?)",
            (at, str(skill or ""), str(outcome or ""), str(detail or "")[:1000],
             str(target or "")[:400], row_params))
        return {"id": cursor.lastrowid, "at": at, "skill": str(skill or ""),
                "outcome": str(outcome or ""), "detail": str(detail or "")[:1000],
                "target": str(target or "")[:400], "params": params or {}}

    def journal_recent(self, limit: int = 30) -> list[dict[str, Any]]:
        try:
            value = int(limit)
        except (TypeError, ValueError):
            value = 30
        value = max(1, min(MAX_JOURNAL_LIMIT, value))
        rows = self._rows("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (value,))
        for row in rows:
            try:
                row["params"] = json.loads(str(row.get("params") or "{}"))
            except json.JSONDecodeError:
                row["params"] = {}
        return rows

    def last_outcome(self, skill: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM journal WHERE skill=? ORDER BY id DESC LIMIT 1",
                          (str(skill or ""),))
        if not rows:
            return None
        row = rows[0]
        try:
            row["params"] = json.loads(str(row.get("params") or "{}"))
        except json.JSONDecodeError:
            row["params"] = {}
        return row

    # --- индекс документов (И142, И143, И148) -----------------------------
    def unchanged(self, path: str, size: int, mtime: float, digest: str) -> bool:
        """Нужно ли перечитывать файл: размер, время и хеш совпали — не нужно."""
        rows = self._rows("SELECT size,mtime,digest FROM documents WHERE path=?",
                          (str(path),))
        if not rows:
            return False
        row = rows[0]
        return (int(row["size"]) == int(size) and abs(float(row["mtime"]) - float(mtime)) < 1.0
                and str(row["digest"]) == str(digest))

    def put_document(self, path: str, kind: str, title: str, size: int, mtime: float,
                     digest: str, chunks: list[dict[str, Any]]) -> int:
        """Файл и его отрывки одной транзакцией: индекс не бывает наполовину старым."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO documents(path,kind,title,size,mtime,digest,chunks,indexed_at)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, title=excluded.title,"
                " size=excluded.size, mtime=excluded.mtime, digest=excluded.digest,"
                " chunks=excluded.chunks, indexed_at=excluded.indexed_at",
                (str(path), str(kind), str(title)[:300], int(size), float(mtime),
                 str(digest), len(chunks), now_iso()))
            self._conn.execute("DELETE FROM chunks WHERE path=?", (str(path),))
            for chunk in chunks:
                self._conn.execute(
                    "INSERT INTO chunks(path,seq,first_line,text) VALUES(?,?,?,?)",
                    (str(path), int(chunk.get("seq", 0)), int(chunk.get("first_line", 1)),
                     str(chunk.get("text") or "")[:8000]))
            self._conn.commit()
        return len(chunks)

    def forget_document(self, path: str) -> None:
        """Файл исчез с диска — из индекса он уходит тоже, иначе поиск врёт."""
        with self._lock:
            self._conn.execute("DELETE FROM documents WHERE path=?", (str(path),))
            self._conn.execute("DELETE FROM chunks WHERE path=?", (str(path),))
            self._conn.commit()

    def documents(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(5000, int(limit or 200)))
        return self._rows("SELECT * FROM documents ORDER BY indexed_at DESC LIMIT ?", (limit,))

    def known_paths(self) -> set[str]:
        return {str(row["path"]) for row in self._rows("SELECT path FROM documents")}

    def search(self, tokens: list[str], limit: int = 8) -> list[dict[str, Any]]:
        """Отрывки по совпадению слов. Считается оценка, а не «нашёл/не нашёл».

        Оценка прозрачная: сколько слов запроса встретилось в отрывке, плюс бонус
        за совпадение в заголовке документа. Её можно пересчитать глазами по
        тексту отрывка — поэтому найденное не требует доверия к модели.
        """
        tokens = [str(token).strip().casefold() for token in tokens if str(token).strip()]
        if not tokens:
            return []
        limit = max(1, min(50, int(limit or 8)))
        where = " OR ".join(["pylower(text) LIKE ?"] * len(tokens))
        rows = self._rows(
            f"SELECT path, seq, first_line, text FROM chunks WHERE {where}"
            f" ORDER BY id DESC LIMIT ?",
            tuple(f"%{token}%" for token in tokens) + (MAX_SEARCH_ROWS,))
        titles = {str(row["path"]): str(row["title"]) for row
                  in self._rows("SELECT path,title FROM documents")}
        hits: list[dict[str, Any]] = []
        for row in rows:
            text = str(row["text"])
            low = text.casefold()
            score = sum(low.count(token) for token in tokens)
            title = titles.get(str(row["path"]), "").casefold()
            score += sum(2 for token in tokens if token in title)
            if score <= 0:
                continue
            hits.append({"path": str(row["path"]), "seq": int(row["seq"]),
                         "first_line": int(row["first_line"]), "score": score,
                         "title": titles.get(str(row["path"]), ""),
                         "snippet": text[:600]})
        hits.sort(key=lambda hit: (-hit["score"], hit["path"], hit["seq"]))
        return hits[:limit]

    # --- факты из документов (И147) ---------------------------------------
    def put_facts(self, path: str, facts: list[dict[str, Any]]) -> int:
        """Факты файла целиком заменяются: документ изменился — старые не живут."""
        with self._lock:
            self._conn.execute("DELETE FROM facts WHERE path=?", (str(path),))
            for fact in facts:
                self._conn.execute(
                    "INSERT INTO facts(at,path,kind,value,place,used) VALUES(?,?,?,?,?,0)",
                    (now_iso(), str(path), str(fact.get("kind") or ""),
                     str(fact.get("value") or "")[:300], str(fact.get("place") or "")[:200]))
            self._conn.commit()
        return len(facts)

    def facts(self, limit: int = 100, path: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(1000, int(limit or 100)))
        if path:
            return self._rows("SELECT * FROM facts WHERE path=? ORDER BY id DESC LIMIT ?",
                              (str(path), limit))
        return self._rows("SELECT * FROM facts ORDER BY id DESC LIMIT ?", (limit,))

    def mark_fact_used(self, fact_id: int) -> None:
        """Факт уехал в панель — помечаем, чтобы не предложить его второй раз."""
        self._run("UPDATE facts SET used=1 WHERE id=?", (int(fact_id),))

    # --- заметки (И168, нужен журналу и брифингу) -------------------------
    def add_note(self, text: str, due: str = "") -> dict[str, Any]:
        at = now_iso()
        cursor = self._run("INSERT INTO notes(at,text,due,done) VALUES(?,?,?,0)",
                           (at, " ".join(str(text).split())[:1000], str(due or "")[:10]))
        return {"id": cursor.lastrowid, "at": at, "text": str(text)[:1000],
                "due": str(due or ""), "done": False}

    def open_notes(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM notes WHERE done=0 ORDER BY id DESC LIMIT ?",
                          (max(1, min(200, int(limit or 50))),))

    def close_note(self, note_id: int) -> bool:
        return self._run("UPDATE notes SET done=1 WHERE id=?", (int(note_id),)).rowcount > 0

    # --- выученные навыки (И180) ------------------------------------------
    def save_skill(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        at = now_iso()
        self._run("INSERT INTO skills(name,payload,learned_at) VALUES(?,?,?)"
                  " ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,"
                  " learned_at=excluded.learned_at",
                  (str(name), json.dumps(payload, ensure_ascii=False), at))
        return {"name": str(name), "learned_at": at, **payload}

    def learned_skills(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in self._rows("SELECT name,payload,learned_at FROM skills ORDER BY name"):
            try:
                data = json.loads(str(row["payload"]))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                out[str(row["name"])] = {**data, "learned_at": str(row["learned_at"])}
        return out

    def forget_skill(self, name: str) -> bool:
        return self._run("DELETE FROM skills WHERE name=?", (str(name),)).rowcount > 0

    # --- сводка -----------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """Что лежит в памяти ассистента — для окна и для диагностики."""
        def count(table: str) -> int:
            rows = self._rows(f"SELECT COUNT(*) AS n FROM {table}")
            return int(rows[0]["n"]) if rows else 0

        return {"path": str(self.path), "documents": count("documents"),
                "chunks": count("chunks"), "facts": count("facts"),
                "notes": count("notes"), "skills": count("skills"),
                "journal": count("journal")}
