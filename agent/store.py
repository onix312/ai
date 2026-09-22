"""Своя база ассистента (18.17): память, индекс, журнал, заметки, навыки.

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
    # --- 18.15: Авито и ТГ ------------------------------------------------
    """CREATE TABLE IF NOT EXISTS avito_watches(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        query TEXT NOT NULL,
        city TEXT DEFAULT '',
        category TEXT DEFAULT '',
        max_price INTEGER DEFAULT 0,
        min_price INTEGER DEFAULT 0,
        enabled INTEGER DEFAULT 1,
        last_checked TEXT DEFAULT '',
        last_count INTEGER DEFAULT 0,
        check_interval_hours INTEGER DEFAULT 0,
        notify_enabled INTEGER DEFAULT 0,
        last_auto_check TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS avito_listings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        watch_id INTEGER NOT NULL,
        external_id TEXT DEFAULT '',
        title TEXT NOT NULL,
        price TEXT DEFAULT '',
        url TEXT DEFAULT '',
        city TEXT DEFAULT '',
        snippet TEXT DEFAULT '',
        seen_at TEXT NOT NULL,
        is_new INTEGER DEFAULT 1,
        image_url TEXT DEFAULT '',
        image_hash TEXT DEFAULT '',
        price_int INTEGER DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS avito_listings_watch ON avito_listings(watch_id)",
    "CREATE INDEX IF NOT EXISTS avito_listings_external ON avito_listings(external_id)",
    "CREATE INDEX IF NOT EXISTS avito_listings_image_hash ON avito_listings(image_hash)",
    """CREATE TABLE IF NOT EXISTS tg_drafts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        topic TEXT NOT NULL,
        tone TEXT DEFAULT '',
        text TEXT NOT NULL,
        source TEXT DEFAULT '',
        status TEXT DEFAULT 'draft')""",
    # --- 18.16: архив переписок, шаблоны, календарь, идеи ----------------
    """CREATE TABLE IF NOT EXISTS avito_threads(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        thread_text TEXT NOT NULL,
        intent TEXT DEFAULT '',
        city TEXT DEFAULT '',
        replies_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'new',
        source TEXT DEFAULT 'manual')""",
    """CREATE TABLE IF NOT EXISTS tg_templates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        name TEXT NOT NULL,
        tone TEXT DEFAULT '',
        template_text TEXT NOT NULL,
        vars_json TEXT DEFAULT '[]')""",
    """CREATE TABLE IF NOT EXISTS tg_schedule(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        draft_id INTEGER NOT NULL,
        planned_at TEXT NOT NULL,
        status TEXT DEFAULT 'planned',
        chat TEXT DEFAULT '',
        result TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS tg_ideas(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        context TEXT DEFAULT '',
        idea_text TEXT NOT NULL,
        draft_id INTEGER DEFAULT 0,
        status TEXT DEFAULT 'new')""",
    "CREATE INDEX IF NOT EXISTS tg_ideas_status ON tg_ideas(status)",
    "CREATE INDEX IF NOT EXISTS tg_schedule_status ON tg_schedule(status)",

    # --- 18.17: полноценный ассистент ПК (И206-И221) ----------------------
    """CREATE TABLE IF NOT EXISTS clipboard_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        text TEXT NOT NULL,
        source_app TEXT DEFAULT '',
        hash TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS preferences(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS whitelist(
        app_name TEXT PRIMARY KEY,
        allowed INTEGER DEFAULT 1,
        at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS macros(
        name TEXT PRIMARY KEY,
        steps_json TEXT NOT NULL,
        at TEXT NOT NULL,
        description TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS focus_timers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        duration_min INTEGER NOT NULL,
        status TEXT DEFAULT 'running',
        end_at TEXT DEFAULT '',
        note TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS file_watches(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        path TEXT NOT NULL,
        enabled INTEGER DEFAULT 1,
        last_seen TEXT DEFAULT '',
        last_count INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS screen_archive(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        title TEXT DEFAULT '',
        path TEXT DEFAULT '',
        hash TEXT DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS clipboard_hash ON clipboard_history(hash)",
    "CREATE INDEX IF NOT EXISTS file_watches_path ON file_watches(path)",
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
            # --- миграции 18.16: добавить колонки к старым таблицам -----------
            self._migrate_1817()

    def _migrate_1817(self) -> None:
        """Добавить колонки, которых не было в 18.15 — без пересоздания таблиц."""
        def has_column(table: str, col: str) -> bool:
            try:
                rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                return any(r[1] == col for r in rows)
            except sqlite3.Error:
                return False

        def add_column(table: str, col_def: str) -> None:
            col_name = col_def.split()[0]
            if not has_column(table, col_name):
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
                except sqlite3.Error:
                    pass

        # avito_watches
        add_column("avito_watches", "check_interval_hours INTEGER DEFAULT 0")
        add_column("avito_watches", "notify_enabled INTEGER DEFAULT 0")
        add_column("avito_watches", "last_auto_check TEXT DEFAULT ''")
        # avito_listings
        add_column("avito_listings", "image_url TEXT DEFAULT ''")
        add_column("avito_listings", "image_hash TEXT DEFAULT ''")
        add_column("avito_listings", "price_int INTEGER DEFAULT 0")
        try:
            self._conn.commit()
        except sqlite3.Error:
            pass
        # индексы 18.16
        try:
            self._conn.execute("CREATE INDEX IF NOT EXISTS avito_listings_image_hash ON avito_listings(image_hash)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS tg_ideas_status ON tg_ideas(status)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS tg_schedule_status ON tg_schedule(status)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS clipboard_hash ON clipboard_history(hash)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS file_watches_path ON file_watches(path)")
            # create new tables if missing (for old DBs)
            for stmt in (
                "CREATE TABLE IF NOT EXISTS clipboard_history(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, text TEXT NOT NULL, source_app TEXT DEFAULT '', hash TEXT DEFAULT '')",
                "CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY, value TEXT NOT NULL, at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS whitelist(app_name TEXT PRIMARY KEY, allowed INTEGER DEFAULT 1, at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS macros(name TEXT PRIMARY KEY, steps_json TEXT NOT NULL, at TEXT NOT NULL, description TEXT DEFAULT '')",
                "CREATE TABLE IF NOT EXISTS focus_timers(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, duration_min INTEGER NOT NULL, status TEXT DEFAULT 'running', end_at TEXT DEFAULT '', note TEXT DEFAULT '')",
                "CREATE TABLE IF NOT EXISTS file_watches(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, path TEXT NOT NULL, enabled INTEGER DEFAULT 1, last_seen TEXT DEFAULT '', last_count INTEGER DEFAULT 0)",
                "CREATE TABLE IF NOT EXISTS screen_archive(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, title TEXT DEFAULT '', path TEXT DEFAULT '', hash TEXT DEFAULT '')",
            ):
                self._conn.execute(stmt)
            self._conn.commit()
        except sqlite3.Error:
            pass


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

    # --- Авито: слежка (И181) ---------------------------------------------
    def add_avito_watch(self, query: str, city: str = "", category: str = "",
                        max_price: int = 0, min_price: int = 0,
                        enabled: bool = True) -> dict[str, Any]:
        at = now_iso()
        cursor = self._run(
            "INSERT INTO avito_watches(at,query,city,category,max_price,min_price,enabled,last_checked,last_count)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (at, " ".join(str(query).split())[:300], str(city or "")[:120],
             str(category or "")[:120], int(max_price or 0), int(min_price or 0),
             1 if enabled else 0, "", 0))
        return {"id": cursor.lastrowid, "at": at, "query": str(query)[:300],
                "city": str(city or "")[:120], "category": str(category or "")[:120],
                "max_price": int(max_price or 0), "min_price": int(min_price or 0),
                "enabled": bool(enabled), "last_checked": "", "last_count": 0}

    def list_avito_watches(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit or 50)))
        return self._rows("SELECT * FROM avito_watches ORDER BY id DESC LIMIT ?", (limit,))

    def get_avito_watch(self, watch_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM avito_watches WHERE id=?", (int(watch_id),))
        return rows[0] if rows else None

    def update_avito_watch(self, watch_id: int, enabled: bool | None = None,
                           last_count: int | None = None) -> bool:
        fields: list[str] = []
        params: list[Any] = []
        if enabled is not None:
            fields.append("enabled=?")
            params.append(1 if enabled else 0)
        if last_count is not None:
            fields.append("last_count=?")
            params.append(int(last_count))
            fields.append("last_checked=?")
            params.append(now_iso())
        elif enabled is None:
            fields.append("last_checked=?")
            params.append(now_iso())
        if not fields:
            return False
        params.append(int(watch_id))
        sql = f"UPDATE avito_watches SET {', '.join(fields)} WHERE id=?"
        return self._run(sql, tuple(params)).rowcount > 0

    def delete_avito_watch(self, watch_id: int) -> bool:
        with self._lock:
            self._conn.execute("DELETE FROM avito_listings WHERE watch_id=?", (int(watch_id),))
            self._conn.execute("DELETE FROM avito_watches WHERE id=?", (int(watch_id),))
            self._conn.commit()
        return True

    def save_avito_listings(self, watch_id: int,
                            listings: list[dict[str, Any]]) -> tuple[int, int]:
        """Сохранить найденные объявления. Возвращает (всего, новых)."""
        if not listings:
            return 0, 0
        existing = {str(row["external_id"]) for row
                    in self._rows("SELECT external_id FROM avito_listings WHERE watch_id=?",
                                  (int(watch_id),)) if row.get("external_id")}
        new = 0
        at = now_iso()
        with self._lock:
            for item in listings:
                ext = str(item.get("external_id") or item.get("url") or "")[:300]
                if ext and ext in existing:
                    # обновляем цену/заголовок, но не считаем новым
                    self._conn.execute(
                        "UPDATE avito_listings SET title=?, price=?, snippet=?, city=?, url=? WHERE watch_id=? AND external_id=?",
                        (str(item.get("title") or "")[:300], str(item.get("price") or "")[:120],
                         str(item.get("snippet") or "")[:600], str(item.get("city") or "")[:120],
                         str(item.get("url") or "")[:600], int(watch_id), ext))
                    continue
                self._conn.execute(
                    "INSERT INTO avito_listings(watch_id,external_id,title,price,url,city,snippet,seen_at,is_new)"
                    " VALUES(?,?,?,?,?,?,?,?,1)",
                    (int(watch_id), ext, str(item.get("title") or "")[:300],
                     str(item.get("price") or "")[:120], str(item.get("url") or "")[:600],
                     str(item.get("city") or "")[:120], str(item.get("snippet") or "")[:600], at))
                new += 1
                if ext:
                    existing.add(ext)
            self._conn.commit()
        return len(listings), new

    def list_avito_listings(self, watch_id: int = 0, limit: int = 50,
                            only_new: bool = False) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit or 50)))
        if watch_id:
            if only_new:
                return self._rows(
                    "SELECT * FROM avito_listings WHERE watch_id=? AND is_new=1 ORDER BY id DESC LIMIT ?",
                    (int(watch_id), limit))
            return self._rows(
                "SELECT * FROM avito_listings WHERE watch_id=? ORDER BY id DESC LIMIT ?",
                (int(watch_id), limit))
        if only_new:
            return self._rows("SELECT * FROM avito_listings WHERE is_new=1 ORDER BY id DESC LIMIT ?",
                              (limit,))
        return self._rows("SELECT * FROM avito_listings ORDER BY id DESC LIMIT ?", (limit,))

    def mark_avito_seen(self, watch_id: int = 0) -> int:
        if watch_id:
            cur = self._run("UPDATE avito_listings SET is_new=0 WHERE watch_id=?",
                            (int(watch_id),))
        else:
            cur = self._run("UPDATE avito_listings SET is_new=0", ())
        return cur.rowcount

    # --- ТГ: черновики постов (И182) ---------------------------------------
    def add_tg_draft(self, topic: str, tone: str, text: str, source: str = "") -> dict[str, Any]:
        at = now_iso()
        cursor = self._run(
            "INSERT INTO tg_drafts(at,topic,tone,text,source,status) VALUES(?,?,?,?,?,?)",
            (at, " ".join(str(topic).split())[:300], str(tone or "")[:60],
             str(text or "")[:8000], str(source or "")[:600], "draft"))
        return {"id": cursor.lastrowid, "at": at, "topic": str(topic)[:300],
                "tone": str(tone or "")[:60], "text": str(text)[:8000],
                "source": str(source or "")[:600], "status": "draft"}

    def list_tg_drafts(self, limit: int = 30, status: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit or 30)))
        if status:
            return self._rows("SELECT * FROM tg_drafts WHERE status=? ORDER BY id DESC LIMIT ?",
                              (str(status), limit))
        return self._rows("SELECT * FROM tg_drafts ORDER BY id DESC LIMIT ?", (limit,))

    def get_tg_draft(self, draft_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM tg_drafts WHERE id=?", (int(draft_id),))
        return rows[0] if rows else None

    def update_tg_draft_status(self, draft_id: int, status: str) -> bool:
        status = str(status or "draft")[:20]
        return self._run("UPDATE tg_drafts SET status=? WHERE id=?",
                         (status, int(draft_id))).rowcount > 0

    # --- 18.16: архив переписок Авито (И191) ------------------------------
    def add_avito_thread(self, thread_text: str, intent: str = "", city: str = "",
                         replies: list[str] | None = None, status: str = "new",
                         source: str = "manual") -> dict[str, Any]:
        at = now_iso()
        cursor = self._run(
            "INSERT INTO avito_threads(at,thread_text,intent,city,replies_json,status,source) VALUES(?,?,?,?,?,?,?)",
            (at, str(thread_text)[:8000], str(intent or "")[:300], str(city or "")[:120],
             json.dumps(replies or [], ensure_ascii=False)[:8000],
             str(status or "new")[:20], str(source or "manual")[:60]))
        return {"id": cursor.lastrowid, "at": at, "thread_text": str(thread_text)[:8000],
                "intent": str(intent or "")[:300], "city": str(city or "")[:120],
                "replies": replies or [], "status": str(status or "new")[:20],
                "source": str(source or "manual")[:60]}

    def list_avito_threads(self, limit: int = 50, status: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit or 50)))
        if status:
            rows = self._rows("SELECT * FROM avito_threads WHERE status=? ORDER BY id DESC LIMIT ?",
                              (str(status), limit))
        else:
            rows = self._rows("SELECT * FROM avito_threads ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            try:
                r["replies"] = json.loads(r.get("replies_json") or "[]")
            except json.JSONDecodeError:
                r["replies"] = []
        return rows

    def get_avito_thread(self, thread_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM avito_threads WHERE id=?", (int(thread_id),))
        if not rows:
            return None
        r = rows[0]
        try:
            r["replies"] = json.loads(r.get("replies_json") or "[]")
        except json.JSONDecodeError:
            r["replies"] = []
        return r

    def update_avito_thread_status(self, thread_id: int, status: str) -> bool:
        return self._run("UPDATE avito_threads SET status=? WHERE id=?",
                         (str(status)[:20], int(thread_id))).rowcount > 0

    # --- 18.16: расписание проверки Авито (И195) + уведомления (И197) -----
    def set_avito_watch_schedule(self, watch_id: int, interval_hours: int = 0,
                                 notify: bool | None = None) -> bool:
        fields: list[str] = []
        params: list[Any] = []
        if interval_hours is not None:
            fields.append("check_interval_hours=?")
            params.append(max(0, min(168, int(interval_hours))))
        if notify is not None:
            fields.append("notify_enabled=?")
            params.append(1 if notify else 0)
        if not fields:
            return False
        params.append(int(watch_id))
        sql = f"UPDATE avito_watches SET {', '.join(fields)} WHERE id=?"
        return self._run(sql, tuple(params)).rowcount > 0

    def watches_due_for_check(self, now: str | None = None) -> list[dict[str, Any]]:
        # возвращает вотчи у которых interval>0 и last_auto_check старше интервала
        # логика в executor, здесь просто все активные с интервалом
        return self._rows(
            "SELECT * FROM avito_watches WHERE enabled=1 AND check_interval_hours>0 ORDER BY last_auto_check ASC")

    def update_avito_watch_auto(self, watch_id: int, last_count: int | None = None) -> bool:
        if last_count is not None:
            return self._run(
                "UPDATE avito_watches SET last_auto_check=?, last_checked=?, last_count=? WHERE id=?",
                (now_iso(), now_iso(), int(last_count), int(watch_id))).rowcount > 0
        return self._run(
            "UPDATE avito_watches SET last_auto_check=?, last_checked=? WHERE id=?",
            (now_iso(), now_iso(), int(watch_id))).rowcount > 0

    # --- 18.16: дедуп по фото (И196) --------------------------------------
    def save_avito_listing_image(self, listing_id: int, image_url: str = "", image_hash: str = "",
                                 price_int: int = 0) -> bool:
        fields: list[str] = []
        params: list[Any] = []
        if image_url:
            fields.append("image_url=?")
            params.append(str(image_url)[:600])
        if image_hash:
            fields.append("image_hash=?")
            params.append(str(image_hash)[:120])
        if price_int:
            fields.append("price_int=?")
            params.append(int(price_int))
        if not fields:
            return False
        params.append(int(listing_id))
        return self._run(f"UPDATE avito_listings SET {', '.join(fields)} WHERE id=?",
                         tuple(params)).rowcount > 0

    def find_duplicate_listings_by_image(self, image_hash: str, limit: int = 20) -> list[dict[str, Any]]:
        if not image_hash:
            return []
        return self._rows(
            "SELECT * FROM avito_listings WHERE image_hash=? ORDER BY id DESC LIMIT ?",
            (str(image_hash)[:120], max(1, min(100, int(limit or 20)))))

    def list_duplicate_image_groups(self, limit: int = 20) -> list[dict[str, Any]]:
        # группы где image_hash повторяется
        return self._rows(
            """SELECT image_hash, COUNT(*) as cnt FROM avito_listings
               WHERE image_hash!='' GROUP BY image_hash HAVING cnt>1 ORDER BY cnt DESC LIMIT ?""",
            (max(1, min(100, int(limit or 20))),))

    # --- 18.16: шаблоны ТГ (И200) ------------------------------------------
    def add_tg_template(self, name: str, tone: str, template_text: str,
                        vars_list: list[str] | None = None) -> dict[str, Any]:
        at = now_iso()
        cursor = self._run(
            "INSERT INTO tg_templates(at,name,tone,template_text,vars_json) VALUES(?,?,?,?,?)",
            (at, str(name)[:200], str(tone or "")[:60], str(template_text)[:8000],
             json.dumps(vars_list or [], ensure_ascii=False)[:1000]))
        return {"id": cursor.lastrowid, "at": at, "name": str(name)[:200],
                "tone": str(tone or "")[:60], "template_text": str(template_text)[:8000],
                "vars": vars_list or []}

    def list_tg_templates(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM tg_templates ORDER BY id DESC LIMIT ?",
                          (max(1, min(200, int(limit or 50))),))
        for r in rows:
            try:
                r["vars"] = json.loads(r.get("vars_json") or "[]")
            except json.JSONDecodeError:
                r["vars"] = []
        return rows

    def get_tg_template(self, template_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM tg_templates WHERE id=?", (int(template_id),))
        if not rows:
            return None
        r = rows[0]
        try:
            r["vars"] = json.loads(r.get("vars_json") or "[]")
        except json.JSONDecodeError:
            r["vars"] = []
        return r

    def delete_tg_template(self, template_id: int) -> bool:
        return self._run("DELETE FROM tg_templates WHERE id=?", (int(template_id),)).rowcount > 0

    # --- 18.16: календарь постов (И198) ------------------------------------
    def add_tg_schedule(self, draft_id: int, planned_at: str, chat: str = "") -> dict[str, Any]:
        at = now_iso()
        cursor = self._run(
            "INSERT INTO tg_schedule(at,draft_id,planned_at,status,chat,result) VALUES(?,?,?,?,?,?)",
            (at, int(draft_id), str(planned_at)[:30], "planned", str(chat or "")[:200], ""))
        return {"id": cursor.lastrowid, "at": at, "draft_id": int(draft_id),
                "planned_at": str(planned_at)[:30], "status": "planned",
                "chat": str(chat or "")[:200], "result": ""}

    def list_tg_schedules(self, limit: int = 50, status: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit or 50)))
        if status:
            return self._rows("SELECT * FROM tg_schedule WHERE status=? ORDER BY planned_at ASC LIMIT ?",
                              (str(status), limit))
        return self._rows("SELECT * FROM tg_schedule ORDER BY planned_at ASC LIMIT ?", (limit,))

    def update_tg_schedule_status(self, schedule_id: int, status: str, result: str = "") -> bool:
        return self._run("UPDATE tg_schedule SET status=?, result=? WHERE id=?",
                         (str(status)[:20], str(result or "")[:600], int(schedule_id))).rowcount > 0

    # --- 18.16: идеи ТГ + конверсия (И205) ---------------------------------
    def add_tg_idea(self, context: str, idea_text: str, status: str = "new") -> dict[str, Any]:
        at = now_iso()
        cursor = self._run(
            "INSERT INTO tg_ideas(at,context,idea_text,draft_id,status) VALUES(?,?,?,?,?)",
            (at, str(context or "")[:600], str(idea_text)[:2000], 0, str(status)[:20]))
        return {"id": cursor.lastrowid, "at": at, "context": str(context or "")[:600],
                "idea_text": str(idea_text)[:2000], "draft_id": 0, "status": str(status)[:20]}

    def list_tg_ideas(self, limit: int = 50, status: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit or 50)))
        if status:
            return self._rows("SELECT * FROM tg_ideas WHERE status=? ORDER BY id DESC LIMIT ?",
                              (str(status), limit))
        return self._rows("SELECT * FROM tg_ideas ORDER BY id DESC LIMIT ?", (limit,))

    def link_tg_idea_to_draft(self, idea_id: int, draft_id: int) -> bool:
        return self._run("UPDATE tg_ideas SET draft_id=?, status='used' WHERE id=?",
                         (int(draft_id), int(idea_id))).rowcount > 0

    def tg_ideas_stats(self) -> dict[str, Any]:
        def cnt(sql: str, params: tuple = ()) -> int:
            rows = self._rows(sql, params)
            return int(rows[0]["n"]) if rows else 0
        total = cnt("SELECT COUNT(*) as n FROM tg_ideas")
        used = cnt("SELECT COUNT(*) as n FROM tg_ideas WHERE status='used'")
        drafts = cnt("SELECT COUNT(*) as n FROM tg_drafts")
        posted = cnt("SELECT COUNT(*) as n FROM tg_drafts WHERE status='posted'")
        scheduled = cnt("SELECT COUNT(*) as n FROM tg_schedule")
        planned = cnt("SELECT COUNT(*) as n FROM tg_schedule WHERE status='planned'")
        return {"ideas_total": total, "ideas_used": used,
                "ideas_conversion": round(used / total * 100, 1) if total else 0,
                "drafts_total": drafts, "drafts_posted": posted,
                "drafts_conversion": round(posted / drafts * 100, 1) if drafts else 0,
                "schedules_total": scheduled, "schedules_planned": planned}

    # --- 18.16: поиск и экспорт ТГ (И203, И204) ----------------------------
    def search_tg_drafts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        q = f"%{str(query or '').strip().casefold()}%"
        if not str(query or '').strip():
            return []
        limit = max(1, min(100, int(limit or 20)))
        return self._rows(
            "SELECT * FROM tg_drafts WHERE pylower(topic) LIKE ? OR pylower(text) LIKE ? ORDER BY id DESC LIMIT ?",
            (q, q, limit))

    def export_tg_drafts(self, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit or 100)))
        if status:
            return self._rows("SELECT * FROM tg_drafts WHERE status=? ORDER BY id DESC LIMIT ?",
                              (str(status), limit))
        return self._rows("SELECT * FROM tg_drafts ORDER BY id DESC LIMIT ?", (limit,))

    # --- сводка -----------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """Что лежит в памяти ассистента — для окна и для диагностики."""
        def count(table: str) -> int:
            rows = self._rows(f"SELECT COUNT(*) AS n FROM {table}")
            return int(rows[0]["n"]) if rows else 0

        return {"path": str(self.path), "documents": count("documents"),
                "chunks": count("chunks"), "facts": count("facts"),
                "notes": count("notes"), "skills": count("skills"),
                "journal": count("journal"),
                "avito_watches": count("avito_watches"),
                "avito_listings": count("avito_listings"),
                "tg_drafts": count("tg_drafts"),
                "avito_threads": count("avito_threads"),
                "tg_templates": count("tg_templates"),
                "tg_schedule": count("tg_schedule"),
                "tg_ideas": count("tg_ideas")}

    # --- 18.17: полноценный ассистент ПК ----------------------------------
    # clipboard_history (И211)
    def add_clipboard(self, text: str, source_app: str = "") -> dict[str, Any]:
        import hashlib
        at = now_iso()
        h = hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]
        # дедуп по хешу: не пишем подряд одинаковое
        rows = self._rows("SELECT hash FROM clipboard_history ORDER BY id DESC LIMIT 1")
        if rows and rows[0].get("hash") == h:
            return {"id": 0, "at": at, "text": str(text)[:4000], "source_app": source_app, "hash": h, "dedup": True}
        cur = self._run("INSERT INTO clipboard_history(at,text,source_app,hash) VALUES(?,?,?,?)",
                        (at, str(text)[:8000], str(source_app or "")[:200], h))
        return {"id": cur.lastrowid, "at": at, "text": str(text)[:4000], "source_app": str(source_app or "")[:200], "hash": h}

    def list_clipboard(self, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit or 30)))
        return self._rows("SELECT * FROM clipboard_history ORDER BY id DESC LIMIT ?", (limit,))

    def clear_clipboard(self) -> int:
        cur = self._run("DELETE FROM clipboard_history", ())
        return cur.rowcount

    # preferences (И214, И219)
    def set_preference(self, key: str, value: str) -> dict[str, Any]:
        at = now_iso()
        self._run("INSERT INTO preferences(key,value,at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, at=excluded.at",
                  (str(key)[:200], str(value)[:4000], at))
        return {"key": str(key)[:200], "value": str(value)[:4000], "at": at}

    def get_preference(self, key: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM preferences WHERE key=?", (str(key)[:200],))
        return rows[0] if rows else None

    def list_preferences(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM preferences ORDER BY key ASC LIMIT ?", (max(1, min(500, int(limit or 100))),))

    def delete_preference(self, key: str) -> bool:
        return self._run("DELETE FROM preferences WHERE key=?", (str(key)[:200],)).rowcount > 0

    # whitelist (И220)
    def set_whitelist(self, app_name: str, allowed: bool = True) -> dict[str, Any]:
        at = now_iso()
        self._run("INSERT INTO whitelist(app_name,allowed,at) VALUES(?,?,?) ON CONFLICT(app_name) DO UPDATE SET allowed=excluded.allowed, at=excluded.at",
                  (str(app_name)[:300], 1 if allowed else 0, at))
        return {"app_name": str(app_name)[:300], "allowed": bool(allowed), "at": at}

    def list_whitelist(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM whitelist ORDER BY app_name ASC LIMIT ?", (max(1, min(500, int(limit or 100))),))

    def is_whitelisted(self, app_name: str) -> bool:
        rows = self._rows("SELECT allowed FROM whitelist WHERE app_name=?", (str(app_name)[:300],))
        if not rows:
            return False
        return bool(rows[0].get("allowed"))

    # macros (И221)
    def save_macro(self, name: str, steps: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
        import json as _json
        at = now_iso()
        self._run("INSERT INTO macros(name,steps_json,at,description) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET steps_json=excluded.steps_json, at=excluded.at, description=excluded.description",
                  (str(name)[:200], _json.dumps(steps, ensure_ascii=False)[:20000], at, str(description or "")[:600]))
        return {"name": str(name)[:200], "steps": steps, "at": at, "description": str(description or "")[:600]}

    def list_macros(self, limit: int = 50) -> list[dict[str, Any]]:
        import json as _json
        rows = self._rows("SELECT * FROM macros ORDER BY at DESC LIMIT ?", (max(1, min(200, int(limit or 50))),))
        for r in rows:
            try:
                r["steps"] = _json.loads(r.get("steps_json") or "[]")
            except Exception:
                r["steps"] = []
        return rows

    def get_macro(self, name: str) -> dict[str, Any] | None:
        import json as _json
        rows = self._rows("SELECT * FROM macros WHERE name=?", (str(name)[:200],))
        if not rows:
            return None
        r = rows[0]
        try:
            r["steps"] = _json.loads(r.get("steps_json") or "[]")
        except Exception:
            r["steps"] = []
        return r

    def delete_macro(self, name: str) -> bool:
        return self._run("DELETE FROM macros WHERE name=?", (str(name)[:200],)).rowcount > 0

    # focus_timers (И215)
    def add_focus_timer(self, duration_min: int, note: str = "") -> dict[str, Any]:
        at = now_iso()
        import datetime
        try:
            end = datetime.datetime.now() + datetime.timedelta(minutes=int(duration_min))
            end_at = end.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            end_at = ""
        cur = self._run("INSERT INTO focus_timers(at,duration_min,status,end_at,note) VALUES(?,?,?,?,?)",
                        (at, max(1, min(240, int(duration_min or 25))), "running", end_at, str(note or "")[:400]))
        return {"id": cur.lastrowid, "at": at, "duration_min": int(duration_min or 25), "status": "running", "end_at": end_at, "note": str(note or "")[:400]}

    def list_focus_timers(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM focus_timers ORDER BY id DESC LIMIT ?", (max(1, min(100, int(limit or 20))),))

    def stop_focus_timer(self, timer_id: int) -> bool:
        return self._run("UPDATE focus_timers SET status='stopped' WHERE id=?", (int(timer_id),)).rowcount > 0

    # file_watches (И217)
    def add_file_watch(self, path: str, enabled: bool = True) -> dict[str, Any]:
        at = now_iso()
        cur = self._run("INSERT INTO file_watches(at,path,enabled,last_seen,last_count) VALUES(?,?,?,?,?)",
                        (at, str(path)[:600], 1 if enabled else 0, "", 0))
        return {"id": cur.lastrowid, "at": at, "path": str(path)[:600], "enabled": bool(enabled), "last_seen": "", "last_count": 0}

    def list_file_watches(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM file_watches ORDER BY id DESC LIMIT ?", (max(1, min(200, int(limit or 50))),))

    def update_file_watch(self, watch_id: int, last_count: int = 0) -> bool:
        return self._run("UPDATE file_watches SET last_seen=?, last_count=? WHERE id=?",
                         (now_iso(), int(last_count), int(watch_id))).rowcount > 0

    # screen_archive
    def add_screen_archive(self, title: str = "", path: str = "", hash_val: str = "") -> dict[str, Any]:
        at = now_iso()
        cur = self._run("INSERT INTO screen_archive(at,title,path,hash) VALUES(?,?,?,?)",
                        (at, str(title or "")[:300], str(path or "")[:600], str(hash_val or "")[:120]))
        return {"id": cur.lastrowid, "at": at, "title": str(title or "")[:300], "path": str(path or "")[:600], "hash": str(hash_val or "")[:120]}

    def list_screen_archive(self, limit: int = 30) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM screen_archive ORDER BY id DESC LIMIT ?", (max(1, min(200, int(limit or 30))),))

    def clear_screen_archive(self) -> int:
        cur = self._run("DELETE FROM screen_archive", ())
        return cur.rowcount

    def stats_17(self) -> dict[str, Any]:
        def cnt(t: str) -> int:
            rows = self._rows(f"SELECT COUNT(*) as n FROM {t}")
            return int(rows[0]["n"]) if rows else 0
        base = self.stats()
        base.update(clipboard=cnt("clipboard_history"), preferences=cnt("preferences"),
                    whitelist=cnt("whitelist"), macros=cnt("macros"),
                    focus_timers=cnt("focus_timers"), file_watches=cnt("file_watches"),
                    screen_archive=cnt("screen_archive"))
        return base
