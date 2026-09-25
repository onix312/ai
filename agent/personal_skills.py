"""Обработчики личных навыков и навыков обучения (18.22) для диспетчера исполнителя.

Каждый обработчик возвращает `say` — готовую фразу для человека (её берёт
`brain.summarize`), данные для окна и, при отказе, причину по-русски.
Время берётся из `runner.clock` — тем же часам, что у мозга: в тестах
«сейчас» одно на весь разговор.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable

from . import intents, when
from .personal import _parse_iso, list_name, money, number_text, split_items

_REPEAT_RE = re.compile(r"^(?:daily|weekdays|weekends|weekly:[0-6]|monthly:\d{1,2}|every:\d{1,4})$")
_LIVE = ("reminder.", "expense.add", "diary.add", "me.today")


def _now(runner: Any) -> dt.datetime:
    clock = getattr(runner, "clock", None)
    return (clock() if callable(clock) else dt.datetime.now()).replace(microsecond=0)


def _text(params: dict[str, Any], key: str) -> str:
    return " ".join(str(params.get(key) or "").split())


# ---------------------------------------------------------------------------
# Напоминания
# ---------------------------------------------------------------------------

def reminder_add(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    now = _now(runner)
    text = _text(params, "text")
    repeat = _text(params, "repeat")
    due = _parse_iso(params.get("due"))
    for source in (_text(params, "when"), text):
        if due or not source:
            continue
        parsed = when.parse(source, now)
        if parsed:
            due, repeat = parsed["at"], repeat or parsed["repeat"]
            if source == text:
                text = parsed["text"] or text
    if repeat and not _REPEAT_RE.match(repeat):
        return {"ok": False, "reason": f"Повтор «{repeat}» не понят: бывает каждый день, по будням, по выходным, "
                                        "раз в неделю, раз в месяц или каждые N минут"}
    if due is None:
        return {"ok": False, "reason": "Не понял, когда напомнить: скажите «через 20 минут», «завтра в 10» "
                                        "или «каждый день в 9»"}
    if due <= now and not repeat:
        return {"ok": False, "reason": f"Это время уже прошло ({due:%d.%m %H:%M})"}
    if due <= now:
        due = when.advance(due, repeat, now) or due
    text = text or "Напоминание"
    row = runner.personal.add_reminder(text, due, repeat, source=str(params.get("source") or "chat"))
    label = when.label(due, repeat, now)
    say = (f"Буду напоминать {label}: {text}." if repeat else f"Напомню {label}: {text}.")
    return {"ok": True, "say": say, "reminder": row, "label": label, "target": text}


def reminder_list(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    now = _now(runner)
    personal = runner.personal
    rows = personal.reminders(("active",), int(params.get("limit") or 20))
    fired = personal.reminders(("fired",), 10)
    if not rows and not fired:
        return {"ok": True, "reminders": [], "say": "Напоминаний нет. Скажите, например: «напомни завтра в 10 позвонить маме»."}
    lines = []
    if rows:
        lines.append("Впереди:")
        lines += [f"• {personal.reminder_line(row, now)}" for row in rows[:10]]
        if len(rows) > 10:
            lines.append(f"…и ещё {len(rows) - 10}")
    if fired:
        lines.append("Сработали и ждут отметки: " + "; ".join(f"«{row['text']}»" for row in fired[:5]) + ".")
    return {"ok": True, "reminders": rows, "fired": fired, "say": "\n".join(lines)}


def _pick_reminder(runner: Any, params: dict[str, Any], statuses: tuple[str, ...]) -> tuple[dict[str, Any] | None, str]:
    personal = runner.personal
    if params.get("id"):
        row = personal.reminder(int(params["id"]))
        return (row, "") if row and row["status"] in statuses else (None, "Напоминание с таким номером не найдено")
    text = _text(params, "text")
    row = personal.find_reminder(text, statuses)
    if row:
        return row, ""
    if text:
        return None, f"Напоминания про «{text}» нет. Скажите «мои напоминания», чтобы увидеть все."
    return None, "Какое напоминание? Назовите его словами или скажите «мои напоминания»."


def reminder_done(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    row, reason = _pick_reminder(runner, params, ("fired", "active"))
    if not row:
        return {"ok": False, "reason": reason}
    runner.personal.complete_reminder(row["id"])
    fresh = runner.personal.reminder(row["id"]) or row
    if fresh.get("repeat") and fresh["status"] == "active":
        due = _parse_iso(fresh["due_at"])
        tail = f" Следующий раз — {when.label(due, '', _now(runner))}." if due else ""
        return {"ok": True, "reminder": fresh, "say": f"Отметил «{row['text']}».{tail}"}
    return {"ok": True, "reminder": fresh, "say": f"Отметил: «{row['text']}» — сделано."}


def reminder_cancel(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    row, reason = _pick_reminder(runner, params, ("active", "fired"))
    if not row:
        return {"ok": False, "reason": reason}
    runner.personal.cancel_reminder(row["id"])
    return {"ok": True, "reminder": row, "say": f"Отменил напоминание «{row['text']}»."}


def reminder_snooze(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    now = _now(runner)
    row, reason = _pick_reminder(runner, params, ("fired", "active"))
    if not row:
        return {"ok": False, "reason": reason}
    minutes = max(1, min(24 * 60, int(params.get("minutes") or 10)))
    fresh = runner.personal.snooze_reminder(row["id"], minutes, now) or row
    due = _parse_iso(fresh.get("due_at")) or now + dt.timedelta(minutes=minutes)
    return {"ok": True, "reminder": fresh,
            "say": f"Отложил «{row['text']}» на {minutes} {when.plural(minutes, 'минуту', 'минуты', 'минут')} — "
                   f"напомню в {due:%H:%M}."}


# ---------------------------------------------------------------------------
# Списки
# ---------------------------------------------------------------------------

def list_add(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    items = split_items(_text(params, "items"))
    if not items:
        return {"ok": False, "reason": "Что добавить? Например: «добавь молоко и хлеб в покупки»"}
    result = runner.personal.add_items(_text(params, "list") or "дела", items)
    total = len(result["items"])
    parts = []
    if result["added"]:
        parts.append(f"Добавил в «{result['list']}»: {', '.join(result['added'])}.")
    if result["repeated"]:
        parts.append(f"Уже было: {', '.join(result['repeated'])}.")
    parts.append(f"Всего в списке: {total}.")
    return {"ok": True, **result, "say": " ".join(parts), "target": result["list"]}


def list_show(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    personal = runner.personal
    name = _text(params, "list")
    if not name:
        rows = personal.lists()
        if not rows:
            return {"ok": True, "lists": [], "say": "Списков пока нет. Скажите: «добавь молоко в покупки»."}
        return {"ok": True, "lists": rows,
                "say": "Списки: " + ", ".join(f"«{row['name']}» ({row['count']})" for row in rows) + "."}
    title = list_name(name)
    items = personal.list_items(title)
    if not items:
        return {"ok": True, "list": title, "items": [], "say": f"Список «{title}» пуст."}
    return {"ok": True, "list": title, "items": items,
            "say": f"В списке «{title}» ({len(items)}): " + ", ".join(row["item"] for row in items[:30]) + "."}


def list_remove(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    personal = runner.personal
    name = _text(params, "list")
    removed, missing = [], []
    for item in split_items(_text(params, "item")):
        row = personal.remove_item(item, name)
        (removed if row else missing).append(row["item"] if row else item)
    if not removed:
        return {"ok": False, "reason": f"В {'списке «' + list_name(name) + '»' if name else 'списках'} нет: "
                                        + ", ".join(missing)}
    left = len(personal.list_items(name)) if name else None
    say = f"Вычеркнул: {', '.join(removed)}." + (f" Не нашёл: {', '.join(missing)}." if missing else "")
    if left is not None:
        say += f" Осталось: {left}." if left else " Список пуст."
    return {"ok": True, "removed": removed, "missing": missing, "say": say}


def list_clear(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = list_name(_text(params, "list") or "дела")
    count = runner.personal.clear_list(name)
    return {"ok": True, "list": name, "cleared": count,
            "say": f"Список «{name}» очищен" + (f" ({count} {when.plural(count, 'пункт', 'пункта', 'пунктов')})." if count else " — он и так был пуст.")}


# ---------------------------------------------------------------------------
# Цели
# ---------------------------------------------------------------------------

def _deadline(raw: str, today: dt.date) -> dt.date | None:
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw[:10])
    except ValueError:
        found = when.target_date(raw if re.match(r"^(?:до|к)\s", raw) else "до " + raw, today, new_year_is_first=False)
        return found[0] if found else None


def goal_add(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    now = _now(runner)
    title = _text(params, "title")
    if len(title) < 3:
        return {"ok": False, "reason": "Какая цель? Например: «моя цель — прочитать 12 книг до конца года»"}
    target = float(params.get("target") or 0)
    unit = _text(params, "unit")
    if not target:
        parsed = intents.parse_goal(title, now.date())
        target, unit = parsed["target"], unit or parsed["unit"]
    deadline = _deadline(_text(params, "deadline"), now.date())
    row = runner.personal.add_goal(title, target, unit, deadline)
    view = runner.personal.goal_view(row, now.date())
    say = f"Цель записана: {title}" + (f" — к {when.day_label(deadline, now.date())}" if deadline else "") + "."
    if target and deadline:
        per_week = target / max(1.0, (deadline - now.date()).days / 7)
        per_text = number_text(per_week) if per_week >= 1 else f"{per_week:.1f}".replace(".", ",")
        say += f" Это ~{per_text} {unit} в неделю."
    if target:
        say += " Отмечайте прогресс: «+2 к цели» или словами, например «прочитал 2 книги»."
    return {"ok": True, "goal": view, "say": say, "target": title}


def goal_progress(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    now = _now(runner)
    personal = runner.personal
    goal = personal.find_goal(_text(params, "goal"))
    if goal is None:
        goals = personal.goals()
        if not goals:
            return {"ok": False, "reason": "Целей пока нет. Скажите: «моя цель — прочитать 12 книг до конца года»"}
        return {"ok": False, "reason": "К какой цели? Есть: " + ", ".join(f"«{row['title']}»" for row in goals[:6])}
    try:
        amount = float(params.get("amount"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "Сколько добавить к цели? Например: «+2 к цели книги»"}
    row = personal.progress_goal(goal["id"], amount, bool(params.get("absolute")))
    view = personal.goal_view(row or goal, now.date())
    say = view["text"]
    if (row or {}).get("status") == "done":
        say = f"Цель «{goal['title']}» достигнута! Поздравляю. " + say
    return {"ok": True, "goal": view, "say": say, "target": goal["title"]}


def goal_list(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    del params
    now = _now(runner)
    views = [runner.personal.goal_view(row, now.date()) for row in runner.personal.goals()]
    if not views:
        return {"ok": True, "goals": [], "say": "Целей пока нет. Скажите: «моя цель — прочитать 12 книг до конца года»."}
    return {"ok": True, "goals": views, "say": "Цели:\n" + "\n".join(f"• {view['text']}" for view in views)}


def goal_remove(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    goal = runner.personal.find_goal(_text(params, "goal"))
    if goal is None:
        return {"ok": False, "reason": f"Цели «{_text(params, 'goal')}» нет"}
    runner.personal.remove_goal(goal["id"])
    return {"ok": True, "goal": goal, "say": f"Цель «{goal['title']}» удалена."}


# ---------------------------------------------------------------------------
# Привычки
# ---------------------------------------------------------------------------

def habit_add(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    title = _text(params, "title")
    if len(title) < 2:
        return {"ok": False, "reason": "Какую привычку завести? Например: «новая привычка: зарядка в 8»"}
    remind = _text(params, "remind_at")
    if remind and not re.match(r"^\d{2}:\d{2}$", remind):
        parsed = when.parse(remind, _now(runner))
        remind = parsed["at"].strftime("%H:%M") if parsed else ""
    row = runner.personal.add_habit(title, remind)
    say = f"Привычка «{row['title']}» заведена"
    if row.get("remind_at"):
        say += f": в {row['remind_at']} напомню, если ещё не отмечена"
    return {"ok": True, "habit": row, "target": row["title"],
            "say": say + ". Когда сделаете — скажите «я сделал…» своими словами или нажмите «Сегодня» во вкладке «Дела»."}


def habit_check(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    now = _now(runner)
    habit = runner.personal.find_habit(_text(params, "habit"))
    if habit is None:
        return {"ok": False, "reason": f"Привычки «{_text(params, 'habit')}» нет. Завести: «новая привычка: {_text(params, 'habit')}»"}
    result = runner.personal.check_habit(habit["id"], now.date())
    streak = result["streak"]
    days = f"{streak} {when.plural(streak, 'день', 'дня', 'дней')} подряд"
    if result["already"]:
        say = f"Сегодня «{habit['title']}» уже отмечена — серия {days}."
    elif streak >= 2:
        say = f"Отметил «{habit['title']}» — серия {days}!" + (" Отличный темп." if streak >= 7 else "")
    else:
        say = f"Отметил «{habit['title']}». Первый день серии — продолжайте завтра."
    return {"ok": True, "habit": habit, "streak": streak, "say": say, "target": habit["title"]}


def habit_list(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    del params
    now = _now(runner)
    rows = runner.personal.habits(now.date())
    if not rows:
        return {"ok": True, "habits": [], "say": "Привычек пока нет. Скажите: «новая привычка: зарядка в 8»."}
    lines = [f"• {row['title']} — " + (f"серия {row['streak']} {when.plural(row['streak'], 'день', 'дня', 'дней')}"
                                        if row["streak"] else "серии пока нет")
             + (", сегодня отмечена" if row["done_today"] else ", сегодня ещё нет") + f"; за неделю {row['week']} из 7"
             for row in rows]
    return {"ok": True, "habits": rows, "say": "Привычки:\n" + "\n".join(lines)}


def habit_remove(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    habit = runner.personal.find_habit(_text(params, "habit"))
    if habit is None:
        return {"ok": False, "reason": f"Привычки «{_text(params, 'habit')}» нет"}
    runner.personal.remove_habit(habit["id"])
    return {"ok": True, "habit": habit, "say": f"Привычка «{habit['title']}» больше не отслеживается."}


# ---------------------------------------------------------------------------
# Расходы и дневник
# ---------------------------------------------------------------------------

def expense_add(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    try:
        amount = float(params.get("amount"))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "Сколько потрачено? Например: «потратил 450 на такси»"}
    if not 0 < amount < 100_000_000:
        return {"ok": False, "reason": "Сумма расхода должна быть больше нуля"}
    now = _now(runner)
    day = None
    if params.get("day"):
        try:
            day = dt.date.fromisoformat(str(params["day"])[:10])
        except ValueError:
            day = None
    row = runner.personal.add_expense(amount, _text(params, "note"), _text(params, "category"), day or now.date())
    spent = runner.personal.expenses(now.date(), now.date())["total"]
    note = f" — {row['note']}" if row.get("note") else ""
    say = f"Записал расход: {money(row['amount'])}{note} ({row['category']})."
    if not day or day == now.date():
        say += f" Сегодня всего: {money(spent)}."
    return {"ok": True, "expense": row, "say": say, "target": row.get("note") or row["category"]}


def expense_report(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    now = _now(runner)
    label, start, end = intents.period(_text(params, "period"), now.date())
    category = _text(params, "category").casefold()
    report = runner.personal.expenses(start, end, category)
    if not report["count"]:
        what = f" на «{category}»" if category else ""
        return {"ok": True, **report, "say": f"{label.capitalize()}{what} расходов не записано. "
                                            "Записывайте словами: «потратил 450 на такси»."}
    if category:
        say = f"{label.capitalize()} на «{category}» — {money(report['total'])} ({report['count']} {when.plural(report['count'], 'запись', 'записи', 'записей')})."
    else:
        top = ", ".join(f"{name} — {money(value)}" for name, value in report["by_category"][:5])
        say = f"{label.capitalize()} потрачено {money(report['total'])}: {top}."
    return {"ok": True, **report, "label": label, "say": say}


def diary_add(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    text = _text(params, "text")
    if len(text) < 2:
        return {"ok": False, "reason": "Что записать в дневник?"}
    mood = int(params.get("mood") or runner.personal.mood_of(text) or 0)
    row = runner.personal.add_diary(text, mood, _now(runner).date())
    return {"ok": True, "entry": row, "say": "Записал в дневник" + (f" (настроение {mood} из 10)" if mood else "") + "."}


def diary_read(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    now = _now(runner)
    today = now.date()
    raw = _text(params, "day").casefold()
    personal = runner.personal
    if re.search(r"недел|настроени", raw):
        rows = [row for row in personal.diary(limit=50) if row["day"] >= (today - dt.timedelta(days=6)).isoformat()]
        mood = personal.mood_average(today)
        head = f"Настроение за неделю в среднем {str(mood).replace('.', ',')} из 10." if mood else "Оценок настроения за неделю нет."
        lines = [f"• {when.day_label(dt.date.fromisoformat(row['day']), today)}: {row['text'][:160]}" for row in rows[:7]]
        return {"ok": True, "entries": rows, "mood": mood, "say": "\n".join([head] + lines)}
    day = today if not raw or raw == "сегодня" else (today - dt.timedelta(days=1) if raw == "вчера" else when.any_date(raw, today))
    rows = personal.diary(day=day) if day else personal.diary(query=_text(params, "query") or raw)
    if not rows:
        return {"ok": True, "entries": [], "say": f"В дневнике за {when.day_label(day, today) if day else 'этот запрос'} записей нет."}
    lines = [f"• {row['text'][:300]}" + (f" (настроение {row['mood']})" if row.get("mood") else "") for row in rows[:10]]
    return {"ok": True, "entries": rows, "say": (f"{when.day_label(day, today).capitalize()}:\n" if day else "") + "\n".join(lines)}


# ---------------------------------------------------------------------------
# Мой день
# ---------------------------------------------------------------------------

def me_today(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    del params
    now = _now(runner)
    lines = [f"Сегодня {when.WEEKDAYS[now.weekday()]}, {now.day} {when.MONTHS[now.month - 1]}."]
    personal_lines = runner.personal.day_lines(now)
    lines += personal_lines
    try:
        panel_ok = bool(runner.caps.get("panel"))
        day = runner.panel.day("briefing", days=1) if panel_ok else {}
    except Exception:  # noqa: BLE001 — сводка цеха не должна ронять личный день
        day = {}
    farm = [str(line) for line in (day.get("lines") or [])[:3]] if isinstance(day, dict) and day.get("ok") else []
    if farm:
        lines.append("Цех: " + " ".join(farm))
    try:
        habits = runner.learning.suggestions(now)
    except Exception:  # noqa: BLE001
        habits = []
    if habits:
        lines.append("В это время вы обычно просите: " + ", ".join(f"«{item}»" for item in habits) + ".")
    if not personal_lines and not farm:
        lines.append("Планов на сегодня нет. Скажите «напомни …», «моя цель — …» или «новая привычка: …».")
    return {"ok": True, "lines": lines, "suggestions": habits, "say": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Обучение
# ---------------------------------------------------------------------------

def describe_steps(steps: list[dict[str, Any]]) -> str:
    """Шаги выученного словами: «Громкость: 30; открой телеграм»."""
    from .executor import describe
    from .skills import get
    parts = []
    for step in steps:
        said = " ".join(str(step.get("say") or "").split())
        skill = get(str(step.get("skill") or "")) if step.get("skill") else None
        if said:
            parts.append(said)  # урок пересказывается словами владельца, а не внутренним описанием
        elif skill:
            parts.append(describe({"name": step["skill"], **skill}, step.get("params") or {}))
    return "; ".join(parts) or "—"


def learn_teach(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    phrase, meaning = _text(params, "phrase"), _text(params, "meaning")
    if len(phrase) < 2 or len(meaning) < 2:
        return {"ok": False, "reason": "Нужны фраза и что она значит: «рабочий режим» → «открой телеграм и громкость 30»"}
    from .brain import resolve_meaning
    steps = resolve_meaning(meaning, runner.personal, _now(runner))
    saved = runner.learning.teach(phrase, steps, meaning=meaning, source="taught")
    if not saved["ok"]:
        return {"ok": False, "reason": saved["reason"]}
    return {"ok": True, "entry": saved["entry"], "target": phrase,
            "say": f"Запомнил: «{phrase}» → {describe_steps(steps)}."}


def learn_list(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    rows = runner.learning.entries(int(params.get("limit") or 30))
    aliases = runner.learning.aliases()
    if not rows and not aliases:
        return {"ok": True, "learned": [], "say": ("Пока ничему не научился. Научите: «когда я говорю «рабочий режим» — "
                                                   "открой телеграм и громкость 30». Ошибусь — скажите «нет, я имел в виду …».")}
    lines = []
    for row in rows[:12]:
        meaning = row["answer"] and f"ответ «{row['answer'][:60]}»" or row.get("meaning") or describe_steps(row["steps"])
        state = "" if row["active"] else ", выключено"
        lines.append(f"• «{row['phrase']}» → {meaning} ({row['source_title']}, {row['uses']} раз{state})")
    for alias in aliases[:6]:
        lines.append(f"• «{alias['word']}» = «{alias['meaning']}» (синоним)")
    return {"ok": True, "learned": rows, "aliases": aliases, "say": "Чему я научился:\n" + "\n".join(lines)}


def learn_forget(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    target = params.get("id") or _text(params, "phrase")
    if not target:
        return {"ok": False, "reason": "Что забыть? Скажите «забудь команду …»"}
    rows = runner.learning.forget(target)
    alias = runner.learning.forget_alias(str(target)) if not str(target).isdigit() else False
    if not rows and not alias:
        return {"ok": False, "reason": f"Выученного «{target}» нет. Скажите «чему ты научился», чтобы увидеть всё"}
    names = [f"«{row['phrase']}»" for row in rows] + ([f"синоним «{target}»"] if alias else [])
    return {"ok": True, "forgotten": rows, "say": "Забыл: " + ", ".join(names) + "."}


def learn_unknown(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    rows = runner.learning.unknowns(int(params.get("limit") or 10))
    if not rows:
        return {"ok": True, "unknown": [], "say": "Непонятых фраз нет — всё, что вы говорили, я разобрал."}
    lines = [f"• «{row['text']}»" + (f" — {row['count']} раза" if row["count"] > 1 else "") for row in rows[:10]]
    return {"ok": True, "unknown": rows,
            "say": "Этого я не понял:\n" + "\n".join(lines)
                   + "\nНаучите: «когда я говорю «…» — делай …» или кнопкой «Научить» во вкладке «Обучение»."}


def learn_habits(runner: Any, params: dict[str, Any]) -> dict[str, Any]:
    del params
    items = runner.learning.insights(_now(runner))
    if not items:
        return {"ok": True, "insights": [], "say": ("Привычек пока не заметил: нужно хотя бы три дня, когда вы просите "
                                                    "одно и то же примерно в одно время.")}
    return {"ok": True, "insights": items, "say": "Что я заметил:\n" + "\n".join(f"• {item['text']}" for item in items[:8])}


_HANDLERS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "reminder.add": reminder_add, "reminder.list": reminder_list, "reminder.done": reminder_done,
    "reminder.cancel": reminder_cancel, "reminder.snooze": reminder_snooze,
    "list.add": list_add, "list.show": list_show, "list.remove": list_remove, "list.clear": list_clear,
    "goal.add": goal_add, "goal.progress": goal_progress, "goal.list": goal_list, "goal.remove": goal_remove,
    "habit.add": habit_add, "habit.check": habit_check, "habit.list": habit_list, "habit.remove": habit_remove,
    "expense.add": expense_add, "expense.report": expense_report,
    "diary.add": diary_add, "diary.read": diary_read, "me.today": me_today,
    "learn.teach": learn_teach, "learn.list": learn_list, "learn.forget": learn_forget,
    "learn.unknown": learn_unknown, "learn.habits": learn_habits,
}


def handlers(runner: Any) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    return {name: (lambda params, handler=handler: handler(runner, params)) for name, handler in _HANDLERS.items()}


def is_live(skill: str) -> bool:
    """Навык, чей смысл зависит от «сейчас»: в уроке хранится фраза, а не застывшие параметры."""
    return skill.startswith(_LIVE)
