"""Личный помощник (18.22): напоминания, списки, цели, привычки, расходы, дневник.

До 18.22 помощник был руками компьютера и окном к панели цеха. Личного у
него было два навыка — заметка и таймер фокуса, — и оба молчали: таймер
только записывался, заметка «на завтра» никогда не всплывала. Здесь то, что
делает помощника своим:

  * **напоминания** со временем по-русски (`when.py`) и повтором; фоновый
    `tick` сам поднимает их в окне и голосом, «Готово» и «Отложить» — кнопками;
  * **списки** — покупки, дела, книги: «добавь молоко и хлеб в покупки»;
  * **цели** с темпом: «12 книг до конца года» → сколько нужно в неделю и
    отстаёте ли вы;
  * **привычки** с сериями дней: «я сделал зарядку» → «5 дней подряд»;
  * **расходы** словами: «потратил 450 на такси» → категория сама; поправка
    категории запоминается;
  * **дневник** и настроение.

Всё живёт в своей базе агента (те же таблицы SQLite, что память и журнал),
машину не покидает. Таблицы создаются при первом обращении: старая база
18.21 открывается без миграций руками.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from . import when
from .store import normalize, now_iso, stems

MAX_TEXT = 400
SNOOZE_MIN = 10
MAX_FIRE = 20  # за один шаг планировщика: после долгого простоя окно не заваливается карточками

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
    "text TEXT NOT NULL, due_at TEXT NOT NULL, repeat TEXT DEFAULT '', status TEXT DEFAULT 'active', "
    "fired_at TEXT DEFAULT '', fired_count INTEGER DEFAULT 0, source TEXT DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due_at)",
    "CREATE TABLE IF NOT EXISTS lists(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
    "list TEXT NOT NULL, item TEXT NOT NULL, norm TEXT NOT NULL, done INTEGER DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS lists_name ON lists(list, done)",
    "CREATE TABLE IF NOT EXISTS goals(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, title TEXT NOT NULL, norm TEXT NOT NULL, target REAL DEFAULT 0, "
    "unit TEXT DEFAULT '', progress REAL DEFAULT 0, deadline TEXT DEFAULT '', status TEXT DEFAULT 'active')",
    "CREATE TABLE IF NOT EXISTS goal_log(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
    "goal_id INTEGER NOT NULL, delta REAL NOT NULL, note TEXT DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS habits(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
    "title TEXT NOT NULL, norm TEXT NOT NULL, remind_at TEXT DEFAULT '', status TEXT DEFAULT 'active', "
    "last_nudge TEXT DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS habit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, habit_id INTEGER NOT NULL, "
    "day TEXT NOT NULL, at TEXT NOT NULL, UNIQUE(habit_id, day))",
    "CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
    "day TEXT NOT NULL, amount REAL NOT NULL, category TEXT DEFAULT 'прочее', note TEXT DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS expenses_day ON expenses(day)",
    "CREATE TABLE IF NOT EXISTS diary(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
    "day TEXT NOT NULL, text TEXT NOT NULL, mood INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
    "kind TEXT NOT NULL, title TEXT NOT NULL, text TEXT DEFAULT '', ref_id INTEGER DEFAULT 0, "
    "seen INTEGER DEFAULT 0)",
)

# Списки: как человек говорит → как список называется.
LIST_NAMES = {
    "покупок": "покупки", "покупки": "покупки", "продуктов": "покупки", "продукты": "покупки",
    "магазин": "покупки", "магазина": "покупки", "дел": "дела", "дела": "дела", "задач": "дела",
    "задачи": "дела", "книг": "книги", "книги": "книги", "почитать": "книги", "фильмов": "фильмы",
    "фильмы": "фильмы", "кино": "фильмы", "сериалов": "сериалы", "сериалы": "сериалы",
    "подарков": "подарки", "подарки": "подарки", "идей": "идеи", "идеи": "идеи", "желаний": "желания",
    "вещей": "вещи", "лекарств": "лекарства", "в дорогу": "в дорогу",
}

# Категории расходов по словам. Поправка владельца («кофе — это кафе»)
# запоминается и важнее этого списка.
CATEGORIES = (
    ("транспорт", ("такси", "метро", "автобус", "бензин", "заправ", "парков", "электрич", "каршер", "проезд",
                   "трамва", "троллейб", "маршрут", "самокат", "яндекс го", "uber")),
    ("продукты", ("продукт", "магазин", "пятероч", "перекрест", "магнит", "ашан", "лента", "вкусвил", "молок",
                  "хлеб", "мясо", "овощ", "фрукт", "супермаркет", "рынок", "еда домой")),
    ("кафе", ("кафе", "ресторан", "обед", "ужин", "завтрак", "кофе", "пицц", "суши", "шаверм", "бургер",
              "доставк", "столов", "бар")),
    ("дом и связь", ("коммунал", "квартплат", "жкх", "интернет", "свет", "электричеств", "газ", "аренд",
                     "телефон", "связь", "мобильн", "ремонт")),
    ("здоровье", ("аптек", "лекарств", "врач", "стоматолог", "анализ", "клиник", "витамин")),
    ("развлечения", ("кино", "игр", "подписк", "концерт", "театр", "музей", "книг", "steam", "netflix")),
    ("одежда", ("одежд", "обув", "куртк", "джинс", "футболк", "кроссов")),
    ("спорт", ("спортзал", "фитнес", "тренир", "бассейн", "абонемент")),
    ("мастерская", ("пластик", "филамент", "petg", "pla", "катушк", "сопл", "принтер", "смол", "запчаст")),
    ("подарки", ("подар", "цвет")),
)

_MOODS = (("отлично", 9), ("отличн", 9), ("прекрасн", 9), ("супер", 9), ("хорош", 7), ("бодр", 8),
          ("нормальн", 6), ("норм", 6), ("так себе", 4), ("устал", 4), ("грустн", 3), ("плох", 3),
          ("ужасн", 1), ("паршив", 2))


def _today(now: dt.datetime | None = None) -> dt.date:
    return (now or dt.datetime.now()).date()


def _iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso(value: Any) -> dt.datetime | None:
    text = str(value or "").strip().replace("T", " ")
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return dt.datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    return None


def money(value: float) -> str:
    """1200 → «1 200 ₽», 450.5 → «450,50 ₽»."""
    whole = abs(value - round(value)) < 0.005
    text = f"{value:,.0f}" if whole else f"{value:,.2f}"
    return text.replace(",", " ").replace(".", ",") + " ₽"


def number_text(value: float) -> str:
    return str(int(round(value))) if abs(value - round(value)) < 1e-9 else f"{value:.1f}".replace(".", ",")


def split_items(text: str) -> list[str]:
    """«молоко, хлеб и яйца» → три пункта; пустое и повторы — прочь."""
    parts = re.split(r"\s*(?:,|;|\n|\s+и\s+|\s+а\s+также\s+|\s+плюс\s+)\s*", str(text or ""))
    out: list[str] = []
    for part in parts:
        clean = part.strip(" .!?:—–-\"«»")
        if clean and normalize(clean) not in (normalize(item) for item in out):
            out.append(clean[:120])
    return out[:30]


def list_name(raw: str) -> str:
    text = normalize(raw)
    text = re.sub(r"^(?:список|списка|списке|списку)\s+", "", text)
    if text in LIST_NAMES:
        return LIST_NAMES[text]
    for key, value in LIST_NAMES.items():
        if text.startswith(key):
            return value
    return text[:40] or "дела"


def _overlap(query: str, text: str) -> float:
    want, have = set(stems(query)), set(stems(text))
    if not want or not have:
        return 0.0
    return len(want & have) / len(want)


def _best(rows: list[dict[str, Any]], query: str, field: str = "title", floor: float = 0.5) -> dict[str, Any] | None:
    """Строка, чьё `field` больше всего похоже на запрос (по основам слов)."""
    query_norm = normalize(query)
    best, score = None, 0.0
    for row in rows:
        value = str(row.get(field) or "")
        if normalize(value) == query_norm:
            return row
        current = max(_overlap(query, value), _overlap(value, query) * 0.9)
        if current > score:
            best, score = row, current
    return best if score >= floor else None


class Personal:
    """Личные данные владельца поверх своей базы агента."""

    def __init__(self, store: Any) -> None:
        self.store = store
        for statement in _SCHEMA:
            self.store._run(statement)

    # ------------------------------------------------------------------
    # Напоминания
    # ------------------------------------------------------------------
    def add_reminder(self, text: str, due: dt.datetime, repeat: str = "", source: str = "chat") -> dict[str, Any]:
        clean = " ".join(str(text or "").split())[:MAX_TEXT] or "Напоминание"
        cursor = self.store._run(
            "INSERT INTO reminders(at, text, due_at, repeat, status, source) VALUES(?,?,?,?,?,?)",
            (now_iso(), clean, _iso(due), str(repeat or "")[:40], "active", str(source or "")[:20]))
        return self.reminder(int(cursor.lastrowid or 0)) or {}

    def reminder(self, reminder_id: int) -> dict[str, Any] | None:
        rows = self.store._rows("SELECT * FROM reminders WHERE id=?", (int(reminder_id or 0),))
        return rows[0] if rows else None

    def reminders(self, statuses: tuple[str, ...] = ("active",), limit: int = 50) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        return self.store._rows(f"SELECT * FROM reminders WHERE status IN ({marks}) ORDER BY due_at LIMIT ?",
                                (*statuses, max(1, min(200, int(limit or 50)))))

    def find_reminder(self, query: str, statuses: tuple[str, ...] = ("fired", "active")) -> dict[str, Any] | None:
        rows = self.reminders(statuses, 200)
        if not str(query or "").strip():
            fired = [row for row in rows if row["status"] == "fired"]
            return (sorted(fired, key=lambda row: row["fired_at"])[-1] if fired else (rows[0] if rows else None))
        return _best(rows, query, "text")

    def due_reminders(self, now: dt.datetime) -> list[dict[str, Any]]:
        return self.store._rows("SELECT * FROM reminders WHERE status='active' AND due_at<=? ORDER BY due_at",
                                (_iso(now),))

    def fire_reminder(self, reminder_id: int, now: dt.datetime) -> dict[str, Any] | None:
        """Сработало: разовое ждёт «Готово», повторяющееся переезжает на следующий раз."""
        row = self.reminder(reminder_id)
        if not row:
            return None
        due = _parse_iso(row["due_at"]) or now
        following = when.advance(due, row.get("repeat") or "", now)
        if following:
            self.store._run("UPDATE reminders SET due_at=?, fired_at=?, fired_count=fired_count+1 WHERE id=?",
                            (_iso(following), _iso(now), row["id"]))
        else:
            self.store._run("UPDATE reminders SET status='fired', fired_at=?, fired_count=fired_count+1 WHERE id=?",
                            (_iso(now), row["id"]))
        return self.reminder(reminder_id)

    def complete_reminder(self, reminder_id: int) -> bool:
        row = self.reminder(reminder_id)
        if not row:
            return False
        if row.get("repeat") and row["status"] == "active":
            return True  # повторяющееся «готово» на сегодня — следующий раз уже назначен
        return self.store._run("UPDATE reminders SET status='done' WHERE id=?", (int(reminder_id),)).rowcount > 0

    def cancel_reminder(self, reminder_id: int) -> bool:
        return self.store._run("UPDATE reminders SET status='cancelled' WHERE id=? AND status IN ('active','fired')",
                               (int(reminder_id),)).rowcount > 0

    def snooze_reminder(self, reminder_id: int, minutes: int, now: dt.datetime) -> dict[str, Any] | None:
        row = self.reminder(reminder_id)
        if not row:
            return None
        due = now + dt.timedelta(minutes=max(1, min(24 * 60, int(minutes or SNOOZE_MIN))))
        if row.get("repeat"):
            # Повтор не сдвигается целиком: отложенный раз — отдельное разовое напоминание.
            return self.add_reminder(row["text"], due, "", source="snooze")
        self.store._run("UPDATE reminders SET status='active', due_at=? WHERE id=?", (_iso(due), row["id"]))
        return self.reminder(reminder_id)

    def reminder_line(self, row: dict[str, Any], now: dt.datetime) -> str:
        due = _parse_iso(row.get("due_at")) or now
        return f"{when.label(due, row.get('repeat') or '', now)} — {row['text']}"

    # ------------------------------------------------------------------
    # Списки
    # ------------------------------------------------------------------
    def add_items(self, name: str, items: list[str]) -> dict[str, Any]:
        title = list_name(name)
        have = {row["norm"] for row in self.list_items(title)}
        added, repeated = [], []
        for item in items:
            norm = normalize(item)
            if not norm:
                continue
            if norm in have:
                repeated.append(item)
                continue
            self.store._run("INSERT INTO lists(at, list, item, norm, done) VALUES(?,?,?,?,0)",
                            (now_iso(), title, item[:120], norm))
            have.add(norm)
            added.append(item)
        return {"list": title, "added": added, "repeated": repeated, "items": self.list_items(title)}

    def list_items(self, name: str) -> list[dict[str, Any]]:
        return self.store._rows("SELECT id, at, list, item, norm FROM lists WHERE list=? AND done=0 ORDER BY id",
                                (list_name(name),))

    def lists(self) -> list[dict[str, Any]]:
        return self.store._rows("SELECT list AS name, COUNT(*) AS count FROM lists WHERE done=0 "
                                "GROUP BY list ORDER BY MAX(id) DESC")

    def remove_item(self, item: str, name: str = "") -> dict[str, Any] | None:
        rows = (self.list_items(name) if name else
                self.store._rows("SELECT id, at, list, item, norm FROM lists WHERE done=0 ORDER BY id DESC"))
        row = _best(rows, item, "item", floor=0.6)
        if not row:
            return None
        self.store._run("UPDATE lists SET done=1 WHERE id=?", (row["id"],))
        return row

    def clear_list(self, name: str) -> int:
        return int(self.store._run("UPDATE lists SET done=1 WHERE list=? AND done=0",
                                   (list_name(name),)).rowcount or 0)

    # ------------------------------------------------------------------
    # Цели
    # ------------------------------------------------------------------
    def add_goal(self, title: str, target: float = 0.0, unit: str = "",
                 deadline: dt.date | None = None) -> dict[str, Any]:
        clean = " ".join(str(title or "").split())[:160]
        stamp = now_iso()
        cursor = self.store._run(
            "INSERT INTO goals(at, updated_at, title, norm, target, unit, progress, deadline, status) "
            "VALUES(?,?,?,?,?,?,0,?, 'active')",
            (stamp, stamp, clean, normalize(clean), float(target or 0), str(unit or "")[:30],
             deadline.isoformat() if deadline else ""))
        return self.goal(int(cursor.lastrowid or 0)) or {}

    def goal(self, goal_id: int) -> dict[str, Any] | None:
        rows = self.store._rows("SELECT * FROM goals WHERE id=?", (int(goal_id or 0),))
        return rows[0] if rows else None

    def goals(self, status: str = "active") -> list[dict[str, Any]]:
        return self.store._rows("SELECT * FROM goals WHERE status=? ORDER BY id", (status,))

    def find_goal(self, query: str) -> dict[str, Any] | None:
        rows = self.goals()
        if not str(query or "").strip():
            return rows[-1] if len(rows) == 1 else None
        found = _best(rows, query, "title", floor=0.34)
        if found:
            return found
        return _best(rows, query, "unit", floor=0.5)

    def goal_by_unit(self, unit_word: str) -> dict[str, Any] | None:
        """«прочитал 2 книги» → цель, у которой единица «книг»."""
        want = stems(unit_word)
        for row in self.goals():
            have = stems(f"{row.get('unit') or ''} {row.get('title') or ''}")
            if want and any(stem[:4] == other[:4] for stem in want for other in have):
                return row
        return None

    def progress_goal(self, goal_id: int, amount: float, absolute: bool = False, note: str = "") -> dict[str, Any] | None:
        row = self.goal(goal_id)
        if not row:
            return None
        value = float(amount) if absolute else float(row["progress"] or 0) + float(amount)
        value = max(0.0, value)
        status = "done" if row["target"] and value >= float(row["target"]) else "active"
        self.store._run("UPDATE goals SET progress=?, status=?, updated_at=? WHERE id=?",
                        (value, status, now_iso(), row["id"]))
        self.store._run("INSERT INTO goal_log(at, goal_id, delta, note) VALUES(?,?,?,?)",
                        (now_iso(), row["id"], value - float(row["progress"] or 0), str(note or "")[:200]))
        return self.goal(goal_id)

    def remove_goal(self, goal_id: int) -> bool:
        return self.store._run("UPDATE goals SET status='removed' WHERE id=?", (int(goal_id),)).rowcount > 0

    def goal_view(self, row: dict[str, Any], today: dt.date) -> dict[str, Any]:
        """Прогресс, темп и «сколько нужно в неделю» — числа для окна и фраза для ответа."""
        target = float(row.get("target") or 0)
        progress = float(row.get("progress") or 0)
        unit = str(row.get("unit") or "")
        percent = int(min(100, round(progress / target * 100))) if target else 0
        parts = [f"{row['title']}: {number_text(progress)}" + (f" из {number_text(target)}" if target else "")
                 + (f" {unit}" if unit else "") + (f" ({percent}%)" if target else "")]
        pace, behind = "", False
        deadline = None
        try:
            deadline = dt.date.fromisoformat(str(row.get("deadline") or "")) if row.get("deadline") else None
        except ValueError:
            deadline = None
        if row.get("status") == "done" or (target and progress >= target):
            pace = "цель достигнута!"
        elif deadline and target:
            start = (_parse_iso(row.get("at")) or dt.datetime.combine(today, dt.time())).date()
            total = max(1, (deadline - start).days)
            left_days = (deadline - today).days
            if left_days < 0:
                pace, behind = f"срок {when.day_label(deadline, today)} прошёл", True
            else:
                expected = target * min(1.0, max(0.0, (today - start).days / total))
                remaining = target - progress
                weeks = max(1.0, left_days / 7)
                per_week = remaining / weeks
                per_text = number_text(per_week) if per_week >= 1 else f"{per_week:.1f}".replace(".", ",")
                if progress + 1e-9 >= expected:
                    pace = f"идёте в графике: до {when.day_label(deadline, today)} — ~{per_text} {unit} в неделю"
                else:
                    behind = True
                    pace = (f"отстаёте на {number_text(expected - progress)} {unit}: нужно ~{per_text} "
                            f"{unit} в неделю до {when.day_label(deadline, today)}")
                pace = " ".join(pace.split())
        elif deadline:
            pace = f"срок — {when.day_label(deadline, today)}"
        text = parts[0] + (f" — {pace}" if pace else "") + "."
        return {"id": row["id"], "title": row["title"], "progress": progress, "target": target, "unit": unit,
                "percent": percent, "deadline": row.get("deadline") or "", "pace": pace, "behind": behind,
                "status": row.get("status"), "text": text}

    # ------------------------------------------------------------------
    # Привычки
    # ------------------------------------------------------------------
    def add_habit(self, title: str, remind_at: str = "") -> dict[str, Any]:
        clean = " ".join(str(title or "").split())[:120]
        existing = _best(self.habits_raw(), clean, "title", floor=0.99)
        if existing:
            return existing
        cursor = self.store._run("INSERT INTO habits(at, title, norm, remind_at, status) VALUES(?,?,?,?, 'active')",
                                 (now_iso(), clean, normalize(clean), str(remind_at or "")[:5]))
        return self.habit(int(cursor.lastrowid or 0)) or {}

    def habit(self, habit_id: int) -> dict[str, Any] | None:
        rows = self.store._rows("SELECT * FROM habits WHERE id=?", (int(habit_id or 0),))
        return rows[0] if rows else None

    def habits_raw(self) -> list[dict[str, Any]]:
        return self.store._rows("SELECT * FROM habits WHERE status='active' ORDER BY id")

    def find_habit(self, query: str) -> dict[str, Any] | None:
        return _best(self.habits_raw(), query, "title", floor=0.5)

    def check_habit(self, habit_id: int, day: dt.date) -> dict[str, Any]:
        cursor = self.store._run("INSERT OR IGNORE INTO habit_log(habit_id, day, at) VALUES(?,?,?)",
                                 (int(habit_id), day.isoformat(), now_iso()))
        return {"already": not cursor.rowcount, "streak": self.streak(habit_id, day)}

    def uncheck_habit(self, habit_id: int, day: dt.date) -> bool:
        return self.store._run("DELETE FROM habit_log WHERE habit_id=? AND day=?",
                               (int(habit_id), day.isoformat())).rowcount > 0

    def habit_days(self, habit_id: int, limit: int = 400) -> set[str]:
        rows = self.store._rows("SELECT day FROM habit_log WHERE habit_id=? ORDER BY day DESC LIMIT ?",
                                (int(habit_id), limit))
        return {row["day"] for row in rows}

    def streak(self, habit_id: int, today: dt.date) -> int:
        """Дней подряд: сегодняшний пропуск ещё не рвёт серию — день не кончился."""
        days = self.habit_days(habit_id)
        cursor = today if today.isoformat() in days else today - dt.timedelta(days=1)
        count = 0
        while cursor.isoformat() in days:
            count += 1
            cursor -= dt.timedelta(days=1)
        return count

    def remove_habit(self, habit_id: int) -> bool:
        return self.store._run("UPDATE habits SET status='removed' WHERE id=?", (int(habit_id),)).rowcount > 0

    def habits(self, today: dt.date) -> list[dict[str, Any]]:
        out = []
        for row in self.habits_raw():
            days = self.habit_days(row["id"])
            week = sum(1 for offset in range(7) if (today - dt.timedelta(days=offset)).isoformat() in days)
            out.append({**row, "streak": self.streak(row["id"], today), "done_today": today.isoformat() in days,
                        "week": week})
        return out

    def habit_nudges(self, now: dt.datetime) -> list[dict[str, Any]]:
        today = now.date().isoformat()
        out = []
        for row in self.habits_raw():
            remind = str(row.get("remind_at") or "")
            if not re.match(r"^\d{2}:\d{2}$", remind) or remind > now.strftime("%H:%M"):
                continue
            if row.get("last_nudge") == today or today in self.habit_days(row["id"], 3):
                continue
            created = str(row.get("at") or "")
            if created[:10] == today and created[11:16] > remind:
                continue  # привычку завели уже после её часа — в первый день не дёргаем
            out.append(row)
        return out

    def mark_nudged(self, habit_id: int, day: dt.date) -> None:
        self.store._run("UPDATE habits SET last_nudge=? WHERE id=?", (day.isoformat(), int(habit_id)))

    # ------------------------------------------------------------------
    # Расходы
    # ------------------------------------------------------------------
    def categorize(self, note: str) -> str:
        low = normalize(note)
        for stem in stems(note):
            learned = self.store.get_preference(f"expense.cat.{stem}")
            if learned and learned.get("value"):
                return str(learned["value"])
        for category, words in CATEGORIES:
            if any(word in low for word in words):
                return category
        return "прочее"

    def learn_category(self, note: str, category: str) -> None:
        for stem in stems(note)[:3]:
            self.store.set_preference(f"expense.cat.{stem}", " ".join(str(category).split())[:30])

    def add_expense(self, amount: float, note: str = "", category: str = "",
                    day: dt.date | None = None) -> dict[str, Any]:
        clean_note = " ".join(str(note or "").split())[:160]
        chosen = " ".join(str(category or "").split()).casefold()[:30]
        if chosen and clean_note:
            self.learn_category(clean_note, chosen)
        chosen = chosen or self.categorize(clean_note)
        cursor = self.store._run("INSERT INTO expenses(at, day, amount, category, note) VALUES(?,?,?,?,?)",
                                 (now_iso(), (day or _today()).isoformat(), round(float(amount), 2), chosen,
                                  clean_note))
        rows = self.store._rows("SELECT * FROM expenses WHERE id=?", (int(cursor.lastrowid or 0),))
        return rows[0] if rows else {}

    def expenses(self, start: dt.date, end: dt.date, category: str = "") -> dict[str, Any]:
        params: tuple[Any, ...] = (start.isoformat(), end.isoformat())
        where = "day>=? AND day<=?"
        if category:
            where += " AND category=?"
            params += (category.casefold(),)
        rows = self.store._rows(f"SELECT * FROM expenses WHERE {where} ORDER BY day DESC, id DESC", params)
        by_category: dict[str, float] = {}
        for row in rows:
            by_category[row["category"]] = by_category.get(row["category"], 0.0) + float(row["amount"])
        top = sorted(by_category.items(), key=lambda item: -item[1])
        return {"total": round(sum(by_category.values()), 2), "count": len(rows), "by_category": top,
                "rows": rows[:50], "start": start.isoformat(), "end": end.isoformat()}

    def undo_expense(self) -> dict[str, Any] | None:
        rows = self.store._rows("SELECT * FROM expenses ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        self.store._run("DELETE FROM expenses WHERE id=?", (rows[0]["id"],))
        return rows[0]

    # ------------------------------------------------------------------
    # Дневник
    # ------------------------------------------------------------------
    @staticmethod
    def mood_of(text: str) -> int:
        low = normalize(text)
        match = re.search(r"(?:настроени\w*|самочувстви\w*)\D{0,12}(\d{1,2})(?:\s*(?:из|/)\s*10)?", low)
        if match:
            return max(1, min(10, int(match.group(1))))
        for word, value in _MOODS:
            if word in low:
                return value
        return 0

    def add_diary(self, text: str, mood: int = 0, day: dt.date | None = None) -> dict[str, Any]:
        clean = " ".join(str(text or "").split())[:2000]
        cursor = self.store._run("INSERT INTO diary(at, day, text, mood) VALUES(?,?,?,?)",
                                 (now_iso(), (day or _today()).isoformat(), clean, int(mood or 0)))
        rows = self.store._rows("SELECT * FROM diary WHERE id=?", (int(cursor.lastrowid or 0),))
        return rows[0] if rows else {}

    def diary(self, day: dt.date | None = None, query: str = "", limit: int = 10) -> list[dict[str, Any]]:
        if day:
            return self.store._rows("SELECT * FROM diary WHERE day=? ORDER BY id", (day.isoformat(),))
        rows = self.store._rows("SELECT * FROM diary ORDER BY id DESC LIMIT 400")
        if query:
            rows = [row for row in rows if _overlap(query, row["text"]) >= 0.5]
        return rows[:max(1, min(50, int(limit or 10)))]

    def mood_average(self, today: dt.date, days: int = 7) -> float | None:
        rows = self.store._rows("SELECT mood FROM diary WHERE day>=? AND mood>0",
                                ((today - dt.timedelta(days=days - 1)).isoformat(),))
        return round(sum(row["mood"] for row in rows) / len(rows), 1) if rows else None

    # ------------------------------------------------------------------
    # Уведомления окна
    # ------------------------------------------------------------------
    def notify(self, kind: str, title: str, text: str = "", ref_id: int = 0) -> dict[str, Any]:
        cursor = self.store._run("INSERT INTO notifications(at, kind, title, text, ref_id, seen) VALUES(?,?,?,?,?,0)",
                                 (now_iso(), kind[:20], title[:120], str(text or "")[:600], int(ref_id or 0)))
        return {"id": int(cursor.lastrowid or 0), "kind": kind, "title": title, "text": text, "ref_id": ref_id,
                "at": now_iso()}

    def unseen(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store._rows("SELECT * FROM notifications WHERE seen=0 ORDER BY id LIMIT ?",
                                (max(1, min(100, int(limit or 20))),))

    def mark_seen(self, ids: list[int]) -> int:
        clean = [int(value) for value in ids if str(value).isdigit()][:100]
        if not clean:
            return 0
        marks = ",".join("?" for _ in clean)
        return int(self.store._run(f"UPDATE notifications SET seen=1 WHERE id IN ({marks})", tuple(clean)).rowcount or 0)

    # ------------------------------------------------------------------
    # Фоновый шаг: напоминания, таймеры фокуса, заметки со сроком, привычки
    # ------------------------------------------------------------------
    def tick(self, now: dt.datetime | None = None) -> list[dict[str, Any]]:
        """Всё, чему пора прозвучать. Каждое срабатывание — ровно одно уведомление."""
        now = (now or dt.datetime.now()).replace(microsecond=0)
        out: list[dict[str, Any]] = []
        for row in self.due_reminders(now)[:MAX_FIRE]:
            fired = self.fire_reminder(row["id"], now)
            if fired is None:
                continue
            due = _parse_iso(row["due_at"])
            # Агент был выключен: напоминание честно помечается пропущенным, а не
            # делает вид, что прозвучало вовремя.
            late = f"Пропущено (было на {due:%d.%m %H:%M}): " if due and now - due > dt.timedelta(hours=1) else ""
            more = ""
            if fired.get("status") == "active" and fired.get("repeat"):
                following = _parse_iso(fired["due_at"])
                more = f" Следующий раз — {when.label(following, '', now)}." if following else ""
            out.append(self.notify("reminder", "Напоминание", late + row["text"] + more, row["id"]))
        try:
            timers = self.store.list_focus_timers(50)
        except Exception:
            timers = []
        for timer in timers:
            end = _parse_iso(timer.get("end_at"))
            if timer.get("status") != "running" or end is None or end > now:
                continue
            done = self.store._run("UPDATE focus_timers SET status='done' WHERE id=? AND status='running'",
                                   (timer["id"],)).rowcount
            if done:
                minutes = int(timer.get("duration_min") or 0)
                note = str(timer.get("note") or "").strip()
                verb = when.plural(minutes, "Прошла", "Прошли", "Прошло")  # «прошла 1 минута», «прошло 25 минут»
                text = f"{verb} {minutes} {when.plural(minutes, 'минута', 'минуты', 'минут')}" + (f": {note}" if note else ".")
                out.append(self.notify("timer", "Таймер", text, timer["id"]))
        try:
            notes = self.store._rows("SELECT * FROM notes WHERE done=0 AND due<>'' ORDER BY id LIMIT 100")
        except Exception:
            notes = []
        for note in notes:
            due = _parse_iso(note.get("due"))
            if due is None or due > now:
                continue
            if self.store._run("UPDATE notes SET done=1 WHERE id=? AND done=0", (note["id"],)).rowcount:
                out.append(self.notify("note", "Заметка", str(note.get("text") or ""), note["id"]))
        for habit in self.habit_nudges(now):
            self.mark_nudged(habit["id"], now.date())
            streak = self.streak(habit["id"], now.date())
            tail = f" Серия — {streak} {when.plural(streak, 'день', 'дня', 'дней')}, не прерывайте её." if streak else ""
            out.append(self.notify("habit", "Привычка", f"«{habit['title']}» сегодня ещё не отмечена.{tail}", habit["id"]))
        # Прочитанные уведомления старше месяца не копятся вечно.
        self.store._run("DELETE FROM notifications WHERE seen=1 AND at<?",
                        (_iso(now - dt.timedelta(days=30)),))
        return out

    # ------------------------------------------------------------------
    # Сводка «мой день» и данные вкладки «Дела»
    # ------------------------------------------------------------------
    def overview(self, now: dt.datetime) -> dict[str, Any]:
        today = now.date()
        end_of_day = dt.datetime.combine(today, dt.time(23, 59, 59))
        active = self.reminders(("active",), 100)
        fired = self.reminders(("fired",), 20)
        reminders = []
        for row in active:
            due = _parse_iso(row["due_at"]) or now
            reminders.append({**row, "label": when.label(due, row.get("repeat") or "", now),
                              "today": due <= end_of_day})
        month_start = today.replace(day=1)
        spent = self.expenses(month_start, today)
        return {
            "reminders": reminders,
            "fired": [{**row, "label": "сработало " + (row.get("fired_at") or "")[11:16]} for row in fired],
            "goals": [self.goal_view(row, today) for row in self.goals()],
            "habits": self.habits(today),
            "lists": [{"name": row["name"], "count": row["count"], "items": self.list_items(row["name"])[:30]}
                      for row in self.lists()],
            "expenses": {"month": spent["total"], "month_text": money(spent["total"]),
                         "top": [[name, money(value)] for name, value in spent["by_category"][:4]],
                         "today": self.expenses(today, today)["total"]},
            "mood": self.mood_average(today),
        }

    def day_lines(self, now: dt.datetime) -> list[str]:
        """«Мой день» строками: напоминания на сегодня, привычки, цели, списки."""
        view = self.overview(now)
        lines: list[str] = []
        today_rows = [row for row in view["reminders"] if row["today"]]
        if today_rows:
            lines.append("Напоминания на сегодня: " + "; ".join(
                f"{str(row['due_at'])[11:16]} — {row['text']}" for row in today_rows[:6]) + ".")
        if view["fired"]:
            lines.append("Ждут отметки: " + "; ".join(row["text"] for row in view["fired"][:4]) + ".")
        habits = view["habits"]
        if habits:
            left = [row["title"] for row in habits if not row["done_today"]]
            done = len(habits) - len(left)
            lines.append(f"Привычки: отмечено {done} из {len(habits)}" + (f", осталось — {', '.join(left[:4])}." if left else ". Все сделаны!"))
        behind = [goal for goal in view["goals"] if goal["behind"]]
        if view["goals"]:
            lines.append("Цели: " + "; ".join(goal["text"].rstrip(".") for goal in (behind or view["goals"])[:3]) + ".")
        shopping = next((row for row in view["lists"] if row["name"] == "покупки"), None)
        if shopping and shopping["count"]:
            lines.append(f"В списке покупок {shopping['count']} {when.plural(shopping['count'], 'пункт', 'пункта', 'пунктов')}.")
        return lines


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
