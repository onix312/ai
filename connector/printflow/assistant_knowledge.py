"""Ответ по фактам базы и день владельца (18.14): идеи И1 и И174.

Почему это отдельный модуль, а не продолжение `assistant.py`.

`assistant.py` разбирает входящее сообщение клиента и выбирает действие из
каталога: у него один вход — текст, и один выход — черновик или идентификатор
действия. Здесь другая задача: на вопрос владельца ответить фактами из его же
базы. Поэтому здесь три слоя, и порядок слоёв важнее, чем их состав:

  1. **Поиск по фактам.** `retrieve()` собирает строки из существующих сервисов
     (`Search`, `Accounting`, `Planner`, `PrinterManager`, `Repo`, `Shelf`) —
     ни одного нового SQL-запроса и ни второй правды о выручке или долгах.
  2. **Ответ модели — только по найденному.** Если фактов нет, модель не зовётся
     вовсе: «в базе этого нет» честнее, чем уверенно составленный абзац.
  3. **Числа сверяются.** Каждая цифра ответа ищется в тексте фактов; не
     нашлась — предупреждение рядом с ответом. Модель по-прежнему не считает
     деньги (пункт 4 контракта `assistant.py`).

Без рантайма модели навык не ломается: он отдаёт факты списком и причину. То же
самое делает `day()` — брифинг и итог дня считаются детерминированно и модель не
зовут никогда: цифры дня не должны зависеть от того, ответила ли модель.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from . import assistant
from .db import Database
from .logging_setup import log
from .search import Search

MAX_FACTS = 40
MAX_FACT_CHARS = 220
MAX_KEYWORDS = 4
MAX_ANSWER_CHARS = 1400

# Слова, которые не несут смысла для поиска: по ним LIKE найдёт половину базы.
STOPWORDS = frozenset((
    "что", "как", "где", "кто", "сколько", "почему", "зачем", "когда", "какой",
    "какая", "какие", "это", "этот", "эта", "эти", "мне", "меня", "нам", "нас",
    "сейчас", "сегодня", "вчера", "завтра", "было", "будет", "есть", "нет",
    "давай", "покажи", "расскажи", "про", "для", "при", "над", "под", "без",
    "все", "всё", "весь", "пришли", "the", "and", "for", "with",
))

# Темы вопроса → какие сервисы читать. Тема определяется словами, а не моделью:
# владелец видит, почему ответ собран именно из этих фактов.
TOPICS: dict[str, tuple[str, ...]] = {
    "money": ("деньг", "выручк", "прибыл", "долг", "оплат", "касс", "расход",
              "доход", "налог", "маржа", "себестоимость", "руб", "₽"),
    "orders": ("заказ", "заявк", "сделк", "издели"),
    "clients": ("клиент", "покупател", "заказчик", "мария", "иванов"),
    "farm": ("станок", "принтер", "парк", "ams", "катуш", "пластик", "сопло",
             "печата", "hms", "филамент"),
    "plan": ("план", "загрузк", "срок", "успеем", "очеред", "задани", "смен"),
    "stock": ("склад", "остатк", "стеллаж", "полк", "товар", "позици", "вариаци",
              "пластик", "филамент", "катуш", "бобин"),
    "events": ("журнал", "событи", "что было", "истори", "ошибк", "тревог"),
}


def _num(value: Any) -> float:
    try:
        return float(str(value or 0).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return 0.0


def keywords_of(question: str) -> list[str]:
    """Слова вопроса, по которым стоит искать: без стоп-слов и коротких."""
    found: list[str] = []
    for raw in re.split(r"[^0-9A-Za-zА-Яа-яЁё-]+", str(question or "").casefold()):
        word = raw.strip("-")
        if len(word) < 3 or word in STOPWORDS or word in found:
            continue
        found.append(word)
        if len(found) >= MAX_KEYWORDS:
            break
    return found


def topics_of(question: str) -> list[str]:
    """Темы вопроса. Пустой список значит «ищи только по словам»."""
    text = str(question or "").casefold()
    return [name for name, hints in TOPICS.items() if any(hint in text for hint in hints)]


def _fact(kind: str, title: str, text: str, ref: str = "") -> dict[str, Any]:
    return {"kind": kind, "title": str(title)[:120],
            "text": " ".join(str(text).split())[:MAX_FACT_CHARS], "ref": str(ref)[:120]}


def _safe(label: str, call: Any) -> tuple[Any, str]:
    """Сервис мог упасть (старая база, нет таблицы): факт пропускается с пометкой."""
    try:
        return call(), ""
    except Exception as exc:
        log().warning("Факты ассистента: %s недоступен (%s)", label, exc)
        return None, f"{label}: не удалось прочитать ({exc.__class__.__name__})"


# ---------------------------------------------------------------------------
# 1. Факты
# ---------------------------------------------------------------------------

def retrieve(api: Any, question: str) -> dict[str, Any]:
    """Факты базы по вопросу: строки с источниками, а не пересказ.

    Каждый факт — это значение, которое уже показывает панель в своём разделе.
    Отсюда два следствия: ответ ассистента не может разойтись с панелью, и
    владелец может проверить любую строку глазами в знакомом месте.
    """
    db: Database = getattr(api, "db", None)
    facts: list[dict[str, Any]] = []
    problems: list[str] = []
    words = keywords_of(question)
    topics = topics_of(question) or (["orders", "money"] if not words else [])

    # --- прямой поиск по словам: заказы, клиенты, катушки, задания, полка
    if words:
        service = getattr(api, "search", None) or Search(db)
        for word in words:
            found, why = _safe(f"поиск «{word}»", lambda w=word: service.run(w, 8))
            if why:
                problems.append(why)
                continue
            for group in (found or {}).get("groups") or []:
                kind = str(group.get("kind") or "")
                for row in (group.get("items") or [])[:4]:
                    title = str(row.get("title") or "")
                    detail = " · ".join(
                        str(value) for key, value in row.items()
                        if key in ("subtitle", "meta", "status", "customer", "material",
                                   "color", "due", "price", "debt", "qty", "remaining")
                        and value not in (None, ""))
                    facts.append(_fact(kind, title, detail or title,
                                       str(row.get("ref") or row.get("id") or "")))

    # --- деньги
    if "money" in topics:
        summary, why = _safe("финансы", lambda: api.acc.summary(30))
        if why:
            problems.append(why)
        elif summary:
            facts.append(_fact(
                "деньги", "Финансы за 30 дней",
                f"доход {summary.get('income')} ₽, расход {summary.get('expense')} ₽, "
                f"прибыль {summary.get('profit')} ₽, маржа {summary.get('margin')}%, "
                f"часов печати {summary.get('print_hours')}",
                "/api/finance"))
        debts, why = _safe("долги", lambda: api.acc.debts())
        if why:
            problems.append(why)
        elif debts:
            facts.append(_fact(
                "долги", "Долги клиентов",
                f"всего {debts.get('total')} ₽ у {debts.get('count')} клиентов, "
                f"просрочено {debts.get('overdue')} ₽", "/api/finance"))
            for row in (debts.get("rows") or [])[:5]:
                facts.append(_fact(
                    "долг", f"Долг: {row.get('customer') or row.get('number') or 'клиент'}",
                    f"{row.get('debt')} ₽, заказ №{row.get('number') or '—'}, "
                    f"{row.get('days')} дней" + (", просрочен" if row.get("overdue") else ""),
                    f"order:{row.get('id')}"))

    # --- заказы
    if "orders" in topics:
        rows, why = _safe("заказы", lambda: api.repo.orders(limit=12))
        if why:
            problems.append(why)
        for row in rows or []:
            facts.append(_fact(
                "заказ", f"Заказ №{row.get('number') or '—'}",
                f"{row.get('product') or 'без названия'} · {row.get('status') or '—'} · "
                f"цена {row.get('price')} ₽ · клиент {row.get('customer_name') or '—'} · "
                f"срок {row.get('due') or '—'}", f"order:{row.get('id')}"))

    # --- клиенты
    if "clients" in topics:
        rows, why = _safe("клиенты", lambda: api.repo.customers())
        if why:
            problems.append(why)
        for row in (rows or [])[:10]:
            facts.append(_fact(
                "клиент", str(row.get("name") or "клиент"),
                f"телефон {row.get('phone') or '—'} · заказов {row.get('orders', '—')} · "
                f"выручка {row.get('revenue', '—')} ₽ · последний заказ "
                f"{str(row.get('last_order') or '—')[:10]}",
                f"customer:{row.get('id')}"))

    # --- парк и печать
    if "farm" in topics or "plan" in topics:
        state, why = _safe("парк", lambda: api.manager.snapshot())
        if why:
            problems.append(why)
        elif state:
            farm = state.get("farm") or {}
            facts.append(_fact(
                "парк", "Парк сейчас",
                f"станков {farm.get('total', 0)}, в сети {farm.get('online', 0)}, "
                f"печатают {farm.get('printing', 0)}, в очереди {farm.get('queued', 0)}, "
                f"загрузка {farm.get('utilization', 0)}%", "/api/state"))
            for row in (state.get("printers") or [])[:6]:
                printer = row.get("printer") or {}
                job = row.get("job") or {}
                alerts = ((row.get("guard") or {}).get("alerts") or [])
                facts.append(_fact(
                    "станок", str(printer.get("name") or printer.get("id") or "станок"),
                    f"состояние {printer.get('state') or '—'} · "
                    f"прогресс {printer.get('progress', '—')}% · "
                    f"задание {job.get('name') or job.get('title') or '—'}"
                    + (f" · тревог: {len(alerts)}" if alerts else ""),
                    f"printer:{printer.get('id')}"))
            for row in (state.get("queue") or [])[:5]:
                facts.append(_fact(
                    "очередь", str(row.get("name") or row.get("title") or "задание"),
                    f"{row.get('state') or '—'} · станок {row.get('printer_id') or '—'}",
                    f"job:{row.get('id')}"))

    # --- план и загрузка
    if "plan" in topics:
        plan, why = _safe("план", lambda: api.planner.day_plan())
        if why:
            problems.append(why)
        elif plan:
            facts.append(_fact(
                "план", "План на сегодня",
                f"{plan.get('verdict_text') or ''} · заказов к печати "
                f"{plan.get('orders_to_print', 0)} · пополнений "
                f"{plan.get('replenish_count', 0)} · часов в плане "
                f"{plan.get('total_hours', 0)}", "/api/plan/day"))
            suggested = plan.get("suggested_next") or {}
            if suggested:
                facts.append(_fact(
                    "план", "Следующее задание",
                    f"{suggested.get('title') or suggested.get('name') or '—'} · "
                    f"{suggested.get('hours', '—')} ч", "/api/plan/day"))

    # --- склад и полка
    if "stock" in topics:
        rows, why = _safe("стеллаж", lambda: api.shelf.items())
        if why:
            problems.append(why)
        for row in (rows or [])[:8]:
            facts.append(_fact(
                "стеллаж", str(row.get("name") or row.get("title") or "позиция"),
                f"остаток {row.get('qty', '—')} · цена {row.get('price', '—')} ₽",
                f"shelf:{row.get('id')}"))
        spools, why = _safe("катушки", lambda: api.repo.spools())
        if why:
            problems.append(why)
        for row in (spools or [])[:6]:
            facts.append(_fact(
                "катушка", f"{row.get('material') or '—'} {row.get('color_name') or ''}".strip(),
                f"остаток {row.get('remaining_grams', '—')} г · бренд "
                f"{row.get('brand') or '—'}", f"spool:{row.get('id')}"))

    # --- события и журнал
    if "events" in topics:
        rows, why = _safe("события", lambda: db.events(limit=12))
        if why:
            problems.append(why)
        for row in rows or []:
            facts.append(_fact(
                "событие", str(row.get("title") or "событие"),
                f"{row.get('at') or ''} · {row.get('kind') or ''} · "
                f"{row.get('detail') or ''}".strip(" ·"), f"event:{row.get('id')}"))

    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for fact in facts:
        key = (fact["title"], fact["text"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(fact)
        if len(unique) >= MAX_FACTS:
            break
    return {"ok": True, "facts": unique, "count": len(unique), "topics": topics,
            "keywords": words, "problems": problems}



def _grams_text(grams: float) -> str:
    """Граммы так, как их говорит человек у стеллажа с катушками."""
    grams = max(0.0, float(grams or 0))
    if grams >= 1000:
        kilos = grams / 1000.0
        shown = f"{kilos:.1f}".replace(".", ",")
        if shown.endswith(",0"):
            shown = shown[:-2]
        return f"{shown} кг"
    return f"{int(round(grams))} г"


def format_plastic(spools: list[dict[str, Any]] | None) -> str:
    """Ответ «сколько пластика»: остатки катушек, не карточки готовых изделий.

    Стеллаж хранит вазы и брелоки. Пластик цеха — remaining_grams катушек.
    Смешивать их в одном JSON владелец уже видел: вопрос про филамент
    возвращал полку с ценниками.
    """
    rows = [row for row in (spools or []) if isinstance(row, dict)]
    live = [row for row in rows if _num(row.get("remaining_grams")) > 0]
    if not live:
        return ("Катушек с остатком на складе нет. "
                "Пластик — это катушки склада, а не товары на стеллаже.")
    by_mat: dict[str, dict[str, float]] = {}
    totals: dict[str, float] = {}
    low: list[str] = []
    for row in live:
        material = str(row.get("material") or "без материала").strip() or "без материала"
        color = str(row.get("color_name") or row.get("color") or "без цвета").strip() or "без цвета"
        grams = _num(row.get("remaining_grams"))
        bucket = by_mat.setdefault(material, {})
        bucket[color] = bucket.get(color, 0.0) + grams
        totals[material] = totals.get(material, 0.0) + grams
        if grams < 80:
            low.append(f"{material} {color} — {_grams_text(grams)}")
    total = sum(totals.values())
    lines = [f"На складе {_grams_text(total)} пластика, катушек с остатком: {len(live)}."]
    for material, grams in sorted(totals.items(), key=lambda item: -item[1]):
        colors = " · ".join(
            f"{name} {_grams_text(value)}"
            for name, value in sorted(by_mat[material].items(), key=lambda item: -item[1]))
        lines.append(f"{material} — {_grams_text(grams)} ({colors})")
    if low:
        lines.append("Мало осталось: " + ", ".join(low[:4]) + ".")
    return "\n".join(lines)


def _plastic_question(question: str) -> bool:
    text = str(question or "").casefold()
    if not any(word in text for word in ("пластик", "филамент", "катуш", "бобин")):
        return False
    # «сколько пластика ушло на заказ» — не остаток склада.
    if any(word in text for word in ("заказ", "ушло", "ушёл", "ушел", "списал", "расход")):
        return False
    return True


def plastic_report(api: Any) -> dict[str, Any]:
    """Сводка катушек для разговора: текст + факты с источниками."""
    spools, why = _safe("катушки", lambda: api.repo.spools())
    problems = [why] if why else []
    rows = [row for row in (spools or []) if isinstance(row, dict)]
    facts = []
    for row in rows:
        if _num(row.get("remaining_grams")) <= 0:
            continue
        facts.append(_fact(
            "катушка",
            f"{row.get('material') or '—'} {row.get('color_name') or ''}".strip(),
            f"остаток {_grams_text(_num(row.get('remaining_grams')))} · бренд "
            f"{row.get('brand') or '—'}",
            f"spool:{row.get('id')}"))
        if len(facts) >= 12:
            break
    return {"text": format_plastic(rows), "facts": facts, "problems": problems}


def facts_text(facts: list[dict[str, Any]]) -> str:
    """Факты одним текстом: его видит модель и по нему сверяются числа."""
    return "\n".join(
        f"[{index}] {fact['kind']}: {fact['title']} — {fact['text']}"
        + (f" (источник: {fact['ref']})" if fact.get("ref") else "")
        for index, fact in enumerate(facts, start=1))


# ---------------------------------------------------------------------------
# 2. Ответ
# ---------------------------------------------------------------------------

def _numbers(answer: str, source: str) -> dict[str, Any]:
    """Цифры ответа против цифр фактов.

    Сверка грубая намеренно: задача не поймить модель на арифметике, а не дать
    ей назвать сумму, которой в базе нет. Цифры из ответа не вычёркиваются —
    вместо этого рядом появляется предупреждение и список фактов.
    """
    def digits(text: str) -> set[str]:
        out = set()
        for raw in re.findall(r"\d[\d\s\u00a0]*(?:[.,]\d+)?", str(text or "")):
            value = re.sub(r"\D", "", raw).lstrip("0")
            if value:
                out.add(value)
        return out

    extra = sorted(digits(answer) - digits(source))
    return {"ok": not extra, "unchecked": extra,
            "warning": ("Числа в ответе не найдены среди фактов базы: "
                        + ", ".join(extra[:6]) if extra else "")}


def _prompt(question: str, facts: str) -> str:
    return (
        "Ты помощник владельца 3D-печатного цеха. Отвечай только по фактам из его "
        "базы, которые приведены ниже.\n"
        f"Вопрос владельца:\n\"\"\"\n{str(question)[:600]}\n\"\"\"\n\n"
        f"Факты из базы:\n{facts[:8000]}\n\n"
        "Правила:\n"
        "1. Если ответа среди фактов нет — так и скажи: «в базе этого нет». Не дополняй.\n"
        "2. Не считай и не округляй суммы, сроки и граммы: бери их из фактов дословно.\n"
        "3. Называй источник: номер заказа, имя клиента, раздел панели.\n"
        "4. Два-четыре предложения по-русски, без заголовков и списков ради списков."
    )


def _digest_facts(facts: list[dict[str, Any]]) -> str:
    """Короткий ответ без модели: заголовки фактов, не сырой JSON маршрута."""
    lines = []
    for fact in facts[:6]:
        title = str(fact.get("title") or "").strip()
        body = str(fact.get("text") or "").strip()
        if title and body:
            lines.append(f"{title}: {body}")
        elif title or body:
            lines.append(title or body)
    return "\n".join(lines)


def _from_chat(reply: dict[str, Any]) -> dict[str, Any]:
    """Ответ модели в той же форме, что и ответ по фактам базы."""
    warnings = list(reply.get("warnings") or [])
    reason = "" if reply.get("ok") else str(reply.get("reason") or "")
    return {"ok": bool(reply.get("ok")),
            "answered": bool(reply.get("ok") and reply.get("answer")),
            "answer": str(reply.get("answer") or ""),
            "facts": [], "count": 0, "topics": [], "keywords": [],
            "sources": list(reply.get("sources") or []),
            "warnings": warnings, "model": reply.get("model") or "",
            "reason": reason, "source": reply.get("source") or "ollama",
            "web": bool(reply.get("web"))}


def answer(api: Any, question: str, *, fast: bool = False, chat: bool = False,
           history: list | None = None) -> dict[str, Any]:
    """Вопрос владельца → ответ по фактам базы (идея И1).

    Ответ всегда содержит `facts`: даже когда модель недоступна, владелец видит
    то, что база знает по его вопросу. Это не «деградация до списка», а граница
    честности: придумать связный абзац без модели можно, а проверить его — нет.

    `fast` — разговор в панели: не ждать модель десятки секунд ради фразы,
    которую уже можно собрать из катушек и фактов.

    `chat` — обычный вопрос не про цех. Его отвечает модель Ollama, а свежие
    факты она берёт из своего включённого веб-поиска. База клиентов в поиск
    не уходит: вопросы с темой цеха остаются на фактах.
    """
    clean = " ".join(str(question or "").split())
    if not clean:
        return {"ok": False, "answered": False, "answer": "", "facts": [],
                "warnings": [], "reason": "Пустой вопрос"}
    if _plastic_question(clean):
        report = plastic_report(api)
        return {"ok": True, "answered": True, "answer": report["text"],
                "facts": report["facts"], "count": len(report["facts"]),
                "topics": ["stock"], "keywords": keywords_of(clean),
                "warnings": report["problems"], "model": "", "reason": "",
                "source": "spools"}
    if chat and not topics_of(clean):
        return _from_chat(assistant.converse(getattr(api, "db", None), clean, history))
    found = retrieve(api, clean)
    facts = found["facts"]
    # Разговор не ждёт модель, если ответ уже лежит в базе цеха.
    if (fast or chat) and facts:
        return {"ok": True, "answered": True, "answer": _digest_facts(facts),
                "facts": facts, "count": len(facts), "topics": found["topics"],
                "keywords": found["keywords"], "warnings": list(found["problems"]),
                "model": "", "reason": "", "source": "facts"}
    if not facts:
        return {"ok": False, "answered": False, "answer": "", "facts": [],
                "topics": found["topics"], "keywords": found["keywords"],
                "problems": found["problems"],
                "reason": ("В базе не нашлось фактов по этому вопросу. "
                           "Спросите про заказ, клиента, деньги, парк, план или склад — "
                           "или уточните имя и номер")}
    db: Database = api.db
    reply = assistant.complete(db, _prompt(clean, facts_text(facts)))
    warnings = list(found["problems"])
    if not reply["ok"]:
        return {"ok": False, "answered": False, "answer": "", "facts": facts,
                "count": len(facts), "topics": found["topics"],
                "keywords": found["keywords"], "warnings": warnings, "model": "",
                "reason": f"Модель недоступна ({reply['reason']}) — вот факты из базы"}
    check = _numbers(reply["text"], facts_text(facts))
    if check["warning"]:
        warnings.append(check["warning"])
    return {"ok": True, "answered": True, "answer": reply["text"][:MAX_ANSWER_CHARS],
            "facts": facts, "count": len(facts), "topics": found["topics"],
            "keywords": found["keywords"], "numbers_checked": check,
            "warnings": warnings, "model": reply["model"], "reason": ""}


# ---------------------------------------------------------------------------
# 3. День владельца (И174)
# ---------------------------------------------------------------------------

def _briefing(api: Any) -> dict[str, Any]:
    lines: list[str] = []
    numbers: dict[str, Any] = {}
    problems: list[str] = []

    plan, why = _safe("план дня", lambda: api.planner.day_plan())
    if why:
        problems.append(why)
    elif plan:
        numbers.update(load_pct=plan.get("load_pct"), total_hours=plan.get("total_hours"),
                       orders_to_print=plan.get("orders_to_print"),
                       verdict=plan.get("verdict"))
        lines.append(f"План недели: {plan.get('verdict_text')}")
        lines.append(f"К печати сегодня {plan.get('orders_to_print')} заказов, "
                     f"пополнений полки {plan.get('replenish_count')}")
        for issue in (plan.get("issues") or [])[:2]:
            lines.append(str(issue))

    state, why = _safe("парк", lambda: api.manager.snapshot())
    if why:
        problems.append(why)
    elif state:
        farm = state.get("farm") or {}
        numbers.update(printers=farm.get("total"), online=farm.get("online"),
                       printing=farm.get("printing"), queued=farm.get("queued"),
                       today_hours=farm.get("today_hours"),
                       today_jobs=farm.get("today_jobs"))
        lines.append(f"Парк: {farm.get('printing', 0)} из {farm.get('total', 0)} печатают, "
                     f"{farm.get('online', 0)} в сети, в очереди {farm.get('queued', 0)}")
        alerts = 0
        for row in (state.get("printers") or []):
            printer = row.get("printer") or {}
            found = ((row.get("guard") or {}).get("alerts") or [])
            problems_list = printer.get("problems") or []
            if found or problems_list:
                alerts += len(found) + len(problems_list)
                lines.append(f"{printer.get('name') or printer.get('id')}: "
                             f"{len(found)} тревог, {len(problems_list)} ошибок")
        numbers["alerts"] = alerts

    debts, why = _safe("долги", lambda: api.acc.debts())
    if why:
        problems.append(why)
    elif debts:
        numbers.update(debts_total=debts.get("total"), debts_count=debts.get("count"),
                       debts_overdue=debts.get("overdue"))
        lines.append(f"Долги: {debts.get('total')} ₽ у {debts.get('count')} клиентов, "
                     f"просрочено {debts.get('overdue')} ₽")
        for row in (debts.get("rows") or [])[:3]:
            if row.get("overdue"):
                lines.append(f"Просрочен долг {row.get('debt')} ₽: "
                             f"{row.get('customer') or 'клиент'}, заказ "
                             f"№{row.get('number') or '—'}")

    insights, why = _safe("здоровье бизнеса", lambda: api.insights.all())
    if why:
        problems.append(why)
    elif insights:
        goal = insights.get("goal") or {}
        cash = insights.get("cash") or {}
        tax = insights.get("tax") or {}
        numbers.update(goal_pct=goal.get("pct"), goal_profit=goal.get("profit"),
                       goal_target=goal.get("goal"), cash_now=cash.get("now"),
                       cash_pipeline=cash.get("pipeline"),
                       cash_runway_days=cash.get("runway_days"),
                       tax_due=tax.get("tax_due"), tax_reserve=tax.get("reserve"))
        if goal.get("verdict_text"):
            lines.append(f"Цель месяца: {goal['verdict_text']}")
        if cash.get("verdict_text"):
            lines.append(f"Касса: {cash['verdict_text']}")
        for row in (tax.get("events") or [])[:1]:
            lines.append(f"Налоговый календарь: {row.get('title')} до "
                         f"{row.get('due') or '—'} на {row.get('amount')} ₽")
        if tax.get("tax_due"):
            lines.append(f"Налогов к уплате {tax.get('tax_due')} ₽, "
                         f"в резерве {tax.get('reserve')} ₽")

    rows, why = _safe("сроки", lambda: api.repo.orders(limit=200))
    if why:
        problems.append(why)
    else:
        today = date.today().isoformat()
        due = [row for row in rows or []
               if str(row.get("due") or "")[:10] == today
               and str(row.get("status") or "") not in ("done", "cancelled", "closed")]
        numbers["due_today"] = len(due)
        if due:
            lines.append(f"Срок сегодня у {len(due)} заказов: "
                         + ", ".join(f"№{row.get('number') or '—'}" for row in due[:5]))
    return {"lines": lines, "numbers": numbers, "problems": problems}


def _summary(api: Any, days: int) -> dict[str, Any]:
    lines: list[str] = []
    numbers: dict[str, Any] = {"days": days}
    problems: list[str] = []

    summary, why = _safe("финансы", lambda: api.acc.summary(days))
    if why:
        problems.append(why)
    elif summary:
        numbers.update(income=summary.get("income"), expense=summary.get("expense"),
                       profit=summary.get("profit"), margin=summary.get("margin"),
                       jobs_done=summary.get("jobs_done"),
                       jobs_failed=summary.get("jobs_failed"),
                       print_hours=summary.get("print_hours"),
                       grams=summary.get("grams"),
                       failure_rate=summary.get("failure_rate"),
                       pipeline=summary.get("pipeline"))
        lines.append(f"За {days} дн.: доход {summary.get('income')} ₽, "
                     f"расход {summary.get('expense')} ₽, прибыль {summary.get('profit')} ₽ "
                     f"(маржа {summary.get('margin')}%)")
        lines.append(f"Напечатано {summary.get('jobs_done')} заданий за "
                     f"{summary.get('print_hours')} ч, {summary.get('grams')} г пластика; "
                     f"неудач {summary.get('jobs_failed')} "
                     f"({summary.get('failure_rate')}%)")
        lines.append(f"В работе заказов {summary.get('active_orders')} на сумму "
                     f"{summary.get('pipeline')} ₽")

    debts, why = _safe("долги", lambda: api.acc.debts())
    if why:
        problems.append(why)
    elif debts:
        numbers.update(debts_total=debts.get("total"), debts_overdue=debts.get("overdue"))
        lines.append(f"Долги остались: {debts.get('total')} ₽, "
                     f"из них просрочено {debts.get('overdue')} ₽")

    state, why = _safe("парк", lambda: api.manager.snapshot())
    if why:
        problems.append(why)
    elif state:
        farm = state.get("farm") or {}
        numbers.update(today_hours=farm.get("today_hours"), today_jobs=farm.get("today_jobs"),
                       today_grams=farm.get("today_grams"),
                       utilization=farm.get("utilization"))
        lines.append(f"Сегодня: {farm.get('today_jobs', 0)} заданий, "
                     f"{farm.get('today_hours', 0)} ч печати, "
                     f"{farm.get('today_grams', 0)} г, загрузка {farm.get('utilization', 0)}%")
        for row in (state.get("printers") or []):
            printer = row.get("printer") or {}
            if str(printer.get("state") or "").upper() in ("FAILED", "ERROR", "OFFLINE"):
                lines.append(f"Встал станок {printer.get('name') or printer.get('id')}: "
                             f"{printer.get('state')}")
    return {"lines": lines, "numbers": numbers, "problems": problems}


def day(api: Any, kind: str = "briefing", days: int = 1) -> dict[str, Any]:
    """Брифинг или итог дня. Модель не участвует: цифры дня детерминированны.

    Текст собирается из тех же сервисов, что рисуют панели «План», «Финансы» и
    «Парк», поэтому вслух ассистент произносит ровно то, что владелец видит на
    экране. Расхождение между голосом и панелью было бы хуже молчания.
    """
    kind = str(kind or "briefing").strip().casefold()
    if kind not in ("briefing", "summary"):
        return {"ok": False, "kind": kind, "lines": [], "numbers": {},
                "reason": "Вид дня: briefing (утро) или summary (вечер)"}
    days = max(1, min(90, int(days or 1)))
    built = _briefing(api) if kind == "briefing" else _summary(api, days)
    lines = [str(line) for line in built["lines"] if str(line).strip()]
    for problem in built["problems"]:
        lines.append(f"Не прочитано: {problem}")
    if not lines:
        return {"ok": False, "kind": kind, "days": days, "lines": [],
                "numbers": built["numbers"], "problems": built["problems"],
                "reason": "База пуста: нечего рассказать про день",
                "text": ""}
    title = "Утро цеха" if kind == "briefing" else f"Итог за {days} дн."
    # Текст собирается предложениями: его озвучивает рантайм речи (идея И11), а
    # склеенные без точки фразы голос читает одним дыханием.
    sentences = " ".join(line if line.endswith((".", "!", "?")) else line + "."
                         for line in lines)
    return {"ok": True, "kind": kind, "days": days, "title": title, "lines": lines,
            "text": f"{title}. {sentences}", "numbers": built["numbers"],
            "problems": built["problems"], "reason": "",
            "hint": "Числа взяты из тех же сервисов, что рисуют панели плана и финансов"}
