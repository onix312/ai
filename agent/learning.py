"""Обучение помощника (18.22): научить, исправить, выучить самому, заметить привычки.

Это не дообучение весов модели (оно сознательно не входит в проект — см.
«Сознательно не вошло» в каталоге идей). Помощник учится так, как учится
человек-секретарь: запоминает, что владелец имеет в виду под своими словами.

Источники знания — от самого надёжного к наименее:

  * `correction` — поправка: «нет, я имел в виду …» после ошибки;
  * `taught`     — урок: «когда я говорю «рабочий режим» — открой Telegram и
                   громкость 30»; шагов может быть несколько;
  * `self`       — выучил сам: фразу поняла модель, навык выполнился — в
                   следующий раз та же фраза понимается без модели. Числа во
                   фразе, совпавшие с параметрами, становятся шаблоном:
                   «звук на 40» → «звук на {0}» понимает и «звук на 70».

Выученное можно отменить: 👎 и поправка снижают вес, самовыученное с
перевесом минусов выключается само, урок — после двух минусов подряд.
Выученная команда проходит те же проверки реестра и то же подтверждение,
что и сказанная впервые: обучение не открывает обходной путь.

Ещё два вида знания: **синонимы** владельца («телега» → «телеграм»)
подставляются во фразу до разбора, а **привычки** — что и в какой час
владелец обычно просит — становятся подсказками-кнопками, но никогда не
запускаются сами.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import re
from typing import Any

from .store import normalize, now_iso, stems

MAX_PHRASE = 160
MAX_STEPS = 8
SOURCES = ("correction", "taught", "self")
SOURCE_TITLES = {"correction": "исправили", "taught": "научили", "self": "выучил сам"}
FUZZY_FLOOR = 0.8

_LEAD_FILLERS = frozenset(("ну", "а", "и", "так", "слушай", "эй", "пожалуйста", "плиз", "давай", "ка",
                           "нозза", "ноза", "nozza", "noza", "помощник", "ассистент", "окей", "ok"))
_TAIL_FILLERS = frozenset(("пожалуйста", "плиз", "спасибо", "ка", "же"))
# Лишнее слово во фразе, меняющее смысл на противоположный: «выключи рабочий
# режим» не должно найти выученный «рабочий режим».
_OPPOSITE = ("выкл", "откл", "оста", "отме", "закр", "убер", "свер", "удал", "стоп", "не", "без", "хват")
# Навыки, привычку к которым считать бессмысленно: разговор о самом помощнике.
_NO_HABIT = ("agent.", "learn.", "memory.", "voice.command", "panel.actions", "me.")
_IDENTITY = ("target", "title", "app_name", "name", "list", "habit", "goal", "action", "url", "path", "query")
# Самообучение на таких навыках бессмысленно или опасно: абсолютное время
# напоминания устареет, «запомни …» повторять незачем.
_NO_SELF = ("agent.", "learn.", "memory.", "assistant.macro")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS learned(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, phrase TEXT NOT NULL, norm TEXT NOT NULL UNIQUE, stems TEXT NOT NULL, "
    "pattern TEXT DEFAULT '', kind TEXT NOT NULL DEFAULT 'command', steps_json TEXT DEFAULT '[]', "
    "answer TEXT DEFAULT '', meaning TEXT DEFAULT '', source TEXT NOT NULL, uses INTEGER DEFAULT 0, "
    "good INTEGER DEFAULT 0, bad INTEGER DEFAULT 0, active INTEGER DEFAULT 1, last_used TEXT DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS unknown_phrases(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
    "last_at TEXT NOT NULL, text TEXT NOT NULL, norm TEXT NOT NULL UNIQUE, count INTEGER DEFAULT 1, "
    "resolved INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS aliases(word TEXT PRIMARY KEY, stem TEXT NOT NULL, meaning TEXT NOT NULL, "
    "at TEXT NOT NULL, uses INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
    "turn_id INTEGER DEFAULT 0, rating INTEGER NOT NULL, phrase TEXT DEFAULT '', reply TEXT DEFAULT '', "
    "skill TEXT DEFAULT '', source TEXT DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, day TEXT NOT NULL, "
    "hour INTEGER NOT NULL, weekday INTEGER NOT NULL, skill TEXT NOT NULL, key TEXT NOT NULL, "
    "phrase TEXT DEFAULT '')",
    "CREATE INDEX IF NOT EXISTS usage_key ON usage(key, day)",
)


def norm_phrase(text: str) -> str:
    """Фраза для сравнения: регистр, ё, пунктуация, «ну/пожалуйста» по краям не различаются."""
    words = normalize(text).split()
    while words and words[0] in _LEAD_FILLERS:
        words.pop(0)
    while words and words[-1] in _TAIL_FILLERS:
        words.pop()
    return " ".join(words)[:MAX_PHRASE]


def _slot_regex(pattern: str) -> re.Pattern[str]:
    body = re.escape(pattern)
    for index in range(3):
        body = body.replace(re.escape("{" + str(index) + "}"), r"(\d+(?:[.,]\d+)?)")
    return re.compile("^" + body + "$")


def _fill(value: Any, groups: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        match = re.fullmatch(r"\{(\d)\}", value)
        if match and int(match.group(1)) < len(groups):
            raw = groups[int(match.group(1))].replace(",", ".")
            number = float(raw)
            return int(number) if number.is_integer() else number
        for index, group in enumerate(groups):
            value = value.replace("{" + str(index) + "}", group)
        return value
    if isinstance(value, dict):
        return {key: _fill(item, groups) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, groups) for item in value]
    return value


def generalize(norm: str, steps: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Числа фразы, совпавшие с числами параметров, → шаблон: «звук на 40» → «звук на {0}».

    Только для одношаговых планов и только однозначные совпадения: «открой 2
    файл из 2 папок» шаблоном не станет — неясно, какое «2» куда.
    """
    if len(steps) != 1 or not steps[0].get("skill"):
        return "", steps
    numbers = re.findall(r"\d+", norm)
    if not numbers:
        return "", steps
    params = dict(steps[0].get("params") or {})
    pattern, slot = norm, 0
    for key, value in list(params.items()):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        text = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
        if not text.isdigit() or numbers.count(text) != 1 or slot >= 3:
            continue
        pattern = re.sub(rf"\b{text}\b", "{" + str(slot) + "}", pattern, count=1)
        params[key] = "{" + str(slot) + "}"
        slot += 1
    if not slot:
        return "", steps
    return pattern, [{**steps[0], "params": params}]


def usage_key(skill: str, params: dict[str, Any]) -> str:
    for name in _IDENTITY:
        value = params.get(name) if isinstance(params, dict) else None
        if isinstance(value, str) and value.strip():
            return f"{skill}:{normalize(value)[:40]}"
    return skill


class Learning:
    """Всё, чему помощник научился, — в своей базе, видно во вкладке «Обучение»."""

    def __init__(self, store: Any) -> None:
        self.store = store
        for statement in _SCHEMA:
            self.store._run(statement)

    # ------------------------------------------------------------------
    # Выученные фразы
    # ------------------------------------------------------------------
    def teach(self, phrase: str, steps: list[dict[str, Any]] | None = None, *, meaning: str = "",
              answer: str = "", source: str = "taught") -> dict[str, Any]:
        """Запомнить: фраза → шаги (или готовый ответ). Возвращает {ok, entry, reason, replaced}."""
        source = source if source in SOURCES else "taught"
        norm = norm_phrase(phrase)
        if len(norm) < 2:
            return {"ok": False, "reason": "Фраза слишком короткая, чтобы её запомнить"}
        clean_steps = [step for step in (steps or []) if isinstance(step, dict)
                       and (step.get("skill") or step.get("say"))][:MAX_STEPS]
        answer = " ".join(str(answer or "").split())[:1000]
        if not clean_steps and not answer:
            return {"ok": False, "reason": "Не понял, что делать по этой фразе"}
        pattern = ""
        if clean_steps and source == "self":
            pattern, clean_steps = generalize(norm, clean_steps)
        existing = self.by_norm(norm)
        if existing and existing["active"] and SOURCES.index(existing["source"]) < SOURCES.index(source):
            # Самовыученное не перетирает урок и поправку владельца.
            return {"ok": False, "reason": "Эту фразу вы уже научили иначе", "entry": existing, "kept": True}
        stamp = now_iso()
        values = (stamp, " ".join(str(phrase).split())[:MAX_PHRASE], json.dumps(stems(norm), ensure_ascii=False),
                  pattern, "answer" if answer and not clean_steps else "command",
                  json.dumps(clean_steps, ensure_ascii=False, default=str)[:8000], answer,
                  " ".join(str(meaning or "").split())[:300], source)
        if existing:
            self.store._run(
                "UPDATE learned SET updated_at=?, phrase=?, stems=?, pattern=?, kind=?, steps_json=?, answer=?, "
                "meaning=?, source=?, active=1, bad=0 WHERE id=?", (*values, existing["id"]))
            entry_id = existing["id"]
        else:
            cursor = self.store._run(
                "INSERT INTO learned(updated_at, phrase, stems, pattern, kind, steps_json, answer, meaning, source, "
                "at, norm) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*values, stamp, norm))
            entry_id = int(cursor.lastrowid or 0)
        self.resolve_unknown(norm)
        return {"ok": True, "entry": self.entry(entry_id), "replaced": bool(existing), "reason": ""}

    def entry(self, entry_id: int) -> dict[str, Any] | None:
        rows = self.store._rows("SELECT * FROM learned WHERE id=?", (int(entry_id or 0),))
        return self._row(rows[0]) if rows else None

    def by_norm(self, norm: str) -> dict[str, Any] | None:
        rows = self.store._rows("SELECT * FROM learned WHERE norm=?", (norm,))
        return self._row(rows[0]) if rows else None

    @staticmethod
    def _row(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        try:
            out["steps"] = json.loads(out.pop("steps_json") or "[]")
        except json.JSONDecodeError:
            out["steps"] = []
        try:
            out["stems"] = json.loads(out.get("stems") or "[]")
        except json.JSONDecodeError:
            out["stems"] = []
        out["source_title"] = SOURCE_TITLES.get(out.get("source"), out.get("source"))
        return out

    def entries(self, limit: int = 100, active_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE active=1" if active_only else ""
        rows = self.store._rows(f"SELECT * FROM learned {where} ORDER BY active DESC, updated_at DESC LIMIT ?",
                                (max(1, min(500, int(limit or 100))),))
        return [self._row(row) for row in rows]

    def forget(self, target: Any) -> list[dict[str, Any]]:
        """Забыть по id или по фразе (точно или по основам). Синоним с этим словом — тоже."""
        if isinstance(target, int) or str(target).isdigit():
            found = [row for row in [self.entry(int(target))] if row]
        else:
            norm = norm_phrase(str(target))
            found = [row for row in [self.by_norm(norm)] if row]
            if not found:
                match = self.match(str(target), fuzzy_only=True)
                found = [match["entry"]] if match else []
        for row in found:
            self.store._run("DELETE FROM learned WHERE id=?", (row["id"],))
        return found

    def match(self, text: str, sources: tuple[str, ...] = SOURCES, fuzzy_only: bool = False) -> dict[str, Any] | None:
        """Выученное под эту фразу: точно → по шаблону с числами → по основам слов."""
        norm = norm_phrase(text)
        if not norm:
            return None
        marks = ",".join("?" for _ in sources)
        rows = [self._row(row) for row in self.store._rows(
            f"SELECT * FROM learned WHERE active=1 AND source IN ({marks})", tuple(sources))]
        if not rows:
            return None
        rank = {name: index for index, name in enumerate(SOURCES)}
        rows.sort(key=lambda row: (rank.get(row["source"], 9), -int(row.get("uses") or 0)))
        if not fuzzy_only:
            for row in rows:
                if row["norm"] == norm:
                    return {"entry": row, "steps": row["steps"], "how": "точно", "score": 1.0}
            for row in rows:
                if not row.get("pattern"):
                    continue
                found = _slot_regex(row["pattern"]).match(norm)
                if found:
                    return {"entry": row, "steps": _fill(row["steps"], found.groups()), "how": "по шаблону",
                            "score": 0.95}
        want = set(stems(norm))
        words = set(norm.split())
        best, best_score = None, 0.0
        for row in rows:
            have = set(row["stems"])
            if len(have) < 2 or not want:
                continue  # короткие фразы — только точно: «свет» не должен ловить «выключи свет»
            if ("не" in words) != ("не" in set(row["norm"].split())):
                continue
            score = len(want & have) / len(want | have)
            extra = want - have
            if have <= want and len(extra) <= 1 and not any(stem.startswith(_OPPOSITE) for stem in extra):
                score = max(score, FUZZY_FLOOR)
            if any(stem.startswith(_OPPOSITE) for stem in extra):
                continue
            if score > best_score:
                best, best_score = row, score
        if best is not None and best_score >= FUZZY_FLOOR:
            return {"entry": best, "steps": best["steps"], "how": "по смыслу слов", "score": round(best_score, 2)}
        return None

    def used(self, entry_id: int) -> None:
        self.store._run("UPDATE learned SET uses=uses+1, last_used=? WHERE id=?", (now_iso(), int(entry_id)))

    def good(self, entry_id: int) -> None:
        self.store._run("UPDATE learned SET good=good+1 WHERE id=?", (int(entry_id),))

    def bad(self, entry_id: int) -> dict[str, Any] | None:
        """Минус выученному. Самовыученное с перевесом минусов и урок после двух минусов выключаются."""
        row = self.entry(entry_id)
        if not row:
            return None
        bad = int(row["bad"] or 0) + 1
        good = int(row["good"] or 0)
        off = (row["source"] == "self" and bad > good) or (bad >= 2 and bad > good)
        self.store._run("UPDATE learned SET bad=?, active=? WHERE id=?", (bad, 0 if off else 1, row["id"]))
        return self.entry(entry_id)

    # ------------------------------------------------------------------
    # Непонятое
    # ------------------------------------------------------------------
    def note_unknown(self, text: str) -> None:
        norm = norm_phrase(text)
        if len(norm) < 3 or self.by_norm(norm):
            return
        stamp = now_iso()
        cursor = self.store._run("UPDATE unknown_phrases SET count=count+1, last_at=?, resolved=0 WHERE norm=?",
                                 (stamp, norm))
        if not cursor.rowcount:
            self.store._run("INSERT INTO unknown_phrases(at, last_at, text, norm, count, resolved) VALUES(?,?,?,?,1,0)",
                            (stamp, stamp, " ".join(str(text).split())[:MAX_PHRASE], norm))

    def unknowns(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.store._rows("SELECT * FROM unknown_phrases WHERE resolved=0 ORDER BY count DESC, last_at DESC "
                                "LIMIT ?", (max(1, min(200, int(limit or 30))),))

    def resolve_unknown(self, text_or_norm: str) -> None:
        norm = norm_phrase(text_or_norm)
        self.store._run("UPDATE unknown_phrases SET resolved=1 WHERE norm=?", (norm,))

    def dismiss_unknown(self, unknown_id: int) -> bool:
        return self.store._run("UPDATE unknown_phrases SET resolved=1 WHERE id=?", (int(unknown_id),)).rowcount > 0

    # ------------------------------------------------------------------
    # Синонимы владельца
    # ------------------------------------------------------------------
    def set_alias(self, word: str, meaning: str) -> dict[str, Any]:
        key = normalize(word)
        clean = " ".join(str(meaning or "").split())[:60]
        if not key or not clean or len(key) > 40 or len(key.split()) > 3 or normalize(clean) == key:
            return {"ok": False, "reason": "Синоним — одно-три слова и другое значение"}
        stem = " ".join(stems(key)) or key
        self.store._run("INSERT INTO aliases(word, stem, meaning, at, uses) VALUES(?,?,?,?,0) "
                        "ON CONFLICT(word) DO UPDATE SET stem=excluded.stem, meaning=excluded.meaning, at=excluded.at",
                        (key, stem, clean, now_iso()))
        return {"ok": True, "word": key, "meaning": clean}

    def aliases(self) -> list[dict[str, Any]]:
        return self.store._rows("SELECT * FROM aliases ORDER BY at DESC")

    def forget_alias(self, word: str) -> bool:
        return self.store._run("DELETE FROM aliases WHERE word=?", (normalize(word),)).rowcount > 0

    def apply_aliases(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """«Открой телегу» → «Открой телеграм»: слово владельца заменяется по основе."""
        rows = self.aliases()
        if not rows:
            return text, []
        applied: list[tuple[str, str]] = []
        out = text
        for row in sorted(rows, key=lambda item: -len(item["word"])):
            if " " in row["word"]:
                pattern = re.compile(r"\b" + r"\s+".join(re.escape(part) for part in row["word"].split()) + r"\b",
                                     re.IGNORECASE)
                if pattern.search(out.replace("ё", "е").replace("Ё", "Е")):
                    out = pattern.sub(row["meaning"], out.replace("ё", "е").replace("Ё", "Е"))
                    applied.append((row["word"], row["meaning"]))
                continue

            def swap(match: re.Match[str], row: dict[str, Any] = row) -> str:
                word = match.group(0)
                if normalize(word) == row["word"] or (stems(word) and stems(word)[0] == row["stem"]):
                    if (row["word"], row["meaning"]) not in applied:
                        applied.append((row["word"], row["meaning"]))
                    return row["meaning"]
                return word

            out = re.sub(r"[A-Za-zА-Яа-яЁё0-9-]+", swap, out)
        for word, _meaning in applied:
            self.store._run("UPDATE aliases SET uses=uses+1 WHERE word=?", (word,))
        return out, applied

    # ------------------------------------------------------------------
    # Оценки ответов
    # ------------------------------------------------------------------
    def feedback(self, rating: int, turn_id: int = 0, phrase: str = "", reply: str = "", skill: str = "",
                 source: str = "") -> dict[str, Any]:
        value = 1 if int(rating) > 0 else -1
        cursor = self.store._run("INSERT INTO feedback(at, turn_id, rating, phrase, reply, skill, source) "
                                 "VALUES(?,?,?,?,?,?,?)", (now_iso(), int(turn_id or 0), value, phrase[:300],
                                                           reply[:600], skill[:80], source[:20]))
        return {"id": int(cursor.lastrowid or 0), "rating": value}

    def feedback_stats(self) -> dict[str, int]:
        rows = self.store._rows("SELECT rating, COUNT(*) AS n FROM feedback GROUP BY rating")
        stats = {"good": 0, "bad": 0}
        for row in rows:
            stats["good" if row["rating"] > 0 else "bad"] = int(row["n"])
        return stats

    # ------------------------------------------------------------------
    # Привычки владельца: что и когда он обычно просит
    # ------------------------------------------------------------------
    def record_usage(self, skill: str, params: dict[str, Any], phrase: str, now: dt.datetime) -> None:
        if not skill or skill.startswith(_NO_HABIT):
            return
        self.store._run("INSERT INTO usage(at, day, hour, weekday, skill, key, phrase) VALUES(?,?,?,?,?,?,?)",
                        (now.strftime("%Y-%m-%d %H:%M:%S"), now.date().isoformat(), now.hour, now.weekday(), skill,
                         usage_key(skill, params or {}), " ".join(str(phrase or "").split())[:120]))
        # Держим последние 90 дней: привычки — это недавнее, а не архив.
        self.store._run("DELETE FROM usage WHERE day<?", ((now.date() - dt.timedelta(days=90)).isoformat(),))

    def insights(self, now: dt.datetime, days: int = 30, min_days: int = 3) -> list[dict[str, Any]]:
        """Устойчивые привычки: одно и то же около одного часа в разные дни."""
        since = (now.date() - dt.timedelta(days=days - 1)).isoformat()
        rows = self.store._rows("SELECT * FROM usage WHERE day>=? ORDER BY id", (since,))
        groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for row in rows:
            groups[row["key"]].append(row)
        out: list[dict[str, Any]] = []
        for key, items in groups.items():
            day_hours: dict[str, int] = {}
            for item in items:
                day_hours.setdefault(item["day"], item["hour"])
            if len(day_hours) < min_days:
                continue
            hours = collections.Counter(day_hours.values())
            peak, _count = hours.most_common(1)[0]
            near = [day for day, hour in day_hours.items() if abs(hour - peak) <= 1]
            phrase = collections.Counter(item["phrase"] for item in items if item["phrase"]).most_common(1)
            said = phrase[0][0] if phrase else items[-1]["skill"]
            timed = len(near) >= min_days
            text = (f"Около {peak:02d}:00 вы обычно просите «{said}» — {len(near)} "
                    f"{_days_word(len(near))} из последних {days}." if timed else
                    f"Часто: «{said}» — {len(day_hours)} {_days_word(len(day_hours))} из последних {days}.")
            out.append({"key": key, "skill": items[-1]["skill"], "phrase": said, "hour": peak if timed else None,
                        "days": len(day_hours), "near": len(near), "today": now.date().isoformat() in day_hours,
                        "text": text})
        out.sort(key=lambda item: (item["hour"] is None, -item["near"], -item["days"]))
        return out[:12]

    def suggestions(self, now: dt.datetime, limit: int = 2) -> list[str]:
        """Подсказки «в этот час вы обычно…» — кнопки, а не самовольный запуск."""
        out = []
        for item in self.insights(now):
            if item["hour"] is None or item["today"]:
                continue
            if abs(item["hour"] - now.hour) <= 1 and item["phrase"] not in out:
                out.append(item["phrase"][:60])
            if len(out) >= limit:
                break
        return out


def _days_word(count: int) -> str:
    count = abs(int(count))
    if count % 10 == 1 and count % 100 != 11:
        return "день"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "дня"
    return "дней"
