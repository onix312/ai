"""Резюме диалога покупателя (идея 61) — локально-экстрактивный разбор.

Никаких внешних AI: правила и регулярные выражения по журналу
client_bot_log. Задача — за секунду понять состояние переписки: кто
последним писал, какие вопросы покупателя остались без ответа, какие
суммы и сроки упоминались, что стоит перечитать.

Вход — строки журнала {at, direction, kind, text, answer, operator}
в хронологическом порядке; выход — структурированное резюме.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_AMOUNT_RE = re.compile(r"(\d[\d\s\u00a0]{1,9})\s*(₽|руб|р\.)", re.IGNORECASE)
_DEADLINE_RE = re.compile(
    r"(сегодня|завтра|послезавтра|до\s+\d{1,2}[:.]\d{2}"
    r"|\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?)", re.IGNORECASE)
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")
_FILLER_RE = re.compile(r"^(ок|окей|спасибо| thanks|👍|❤|😀|😊| WELL)[!., ]*$",
                        re.IGNORECASE)
_NOISE_WORDS = ("спасибо", "ок", "окей", "добрый день", "здравствуйте", "привет")


def _body(row: dict[str, Any]) -> str:
    """Текст сообщения: у входящих — text, у исходящих — answer или text."""
    if str(row.get("direction") or "in") == "in":
        return str(row.get("text") or "").strip()
    return str(row.get("answer") or row.get("text") or "").strip()


def _when(value: Any) -> str:
    return str(value or "")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Резюме переписки по строкам client_bot_log (хронология по возрастанию)."""
    msgs = [r for r in rows or [] if str(r.get("direction")) in ("in", "out")]
    incoming = [r for r in msgs if r.get("direction") == "in"]
    outgoing = [r for r in msgs if r.get("direction") == "out"]

    amounts: list[str] = []
    deadlines: list[str] = []
    phones: list[str] = []
    for r in msgs:
        body = _body(r)
        for m in _AMOUNT_RE.finditer(body):
            value = _clean(m.group(0))
            if value not in amounts:
                amounts.append(value)
        for m in _DEADLINE_RE.finditer(body):
            value = _clean(m.group(0))
            if value.lower() not in [d.lower() for d in deadlines]:
                deadlines.append(value)
        if r.get("direction") == "in":
            for m in _PHONE_RE.finditer(body):
                digits = re.sub(r"\D", "", m.group(0))
                if len(digits) in (10, 11) and len(digits) >= len(re.sub(r"\D", "", body)) - 2:
                    value = _clean(m.group(0))
                    if value not in phones:
                        phones.append(value)

    # Открытые вопросы: входящие с «?», после которых мастер ещё не отвечал.
    open_questions: list[dict[str, str]] = []
    saw_reply = False
    for r in reversed(msgs):                      # с конца: свежее — старше
        if r.get("direction") == "out":
            saw_reply = True                      # всё, что раньше этого, — отвечено
            continue
        body = _body(r)
        if "?" in body and not saw_reply:
            open_questions.append({"at": _when(r.get("at")), "text": _clean(body)[:200]})
            if len(open_questions) >= 3:
                break

    # Показательные сообщения: входящие по делу (не «ок», не «спасибо»).
    highlights: list[dict[str, str]] = []
    for r in reversed(incoming):
        body = _clean(_body(r))
        if not body or len(body) < 4:
            continue
        if _FILLER_RE.match(body):
            continue
        if body.lower() in _NOISE_WORDS:
            continue
        highlights.append({"at": _when(r.get("at")), "text": body[:200]})
        if len(highlights) >= 5:
            break
    highlights.reverse()

    first_at = _when(msgs[0].get("at")) if msgs else ""
    last_at = _when(msgs[-1].get("at")) if msgs else ""
    last_direction = str(msgs[-1].get("direction")) if msgs else ""
    if not msgs:
        verdict = "Диалог пуст"
    elif last_direction == "in":
        verdict = "Последним писал покупатель — ждёт ответа"
    elif open_questions:
        verdict = "Есть вопрос покупателя без ответа"
    else:
        verdict = "Последним отвечали вы — мяч на стороне покупателя"

    lines: list[str] = []
    if msgs:
        lines.append(f"Сообщений: {len(msgs)} (от покупателя {len(incoming)}, "
                     f"ваших {len(outgoing)}).")
    if open_questions:
        lines.append("Без ответа: " + "; ".join(
            f"«{q['text']}»" for q in reversed(open_questions[:2])) + ".")
    if amounts:
        lines.append("Суммы: " + ", ".join(amounts[:3]) + ".")
    if deadlines:
        lines.append("Сроки: " + ", ".join(deadlines[:3]) + ".")
    if phones:
        lines.append("Телефон в переписке: " + phones[0] + ".")
    if highlights:
        lines.append("Перечитать: «" + highlights[-1]["text"][:120] + "».")

    return {
        "verdict": verdict,
        "counts": {"total": len(msgs), "in": len(incoming), "out": len(outgoing)},
        "first_at": first_at,
        "last_at": last_at,
        "last_direction": last_direction,
        "open_questions": list(reversed(open_questions)),
        "highlights": highlights,
        "amounts": amounts[:5],
        "deadlines": deadlines[:5],
        "phones": phones[:2],
        "summary": "\n".join(lines),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

# ------------------------------------------------------------------ маршрут
def summary_for_order(db: Any, order_id: str) -> dict[str, Any]:
    """Резюме переписки по заказу: ссылка на чат + журнал client_bot_log."""
    link = db.one("SELECT chat_id FROM client_orders WHERE order_id=?"
                  " ORDER BY rowid DESC LIMIT 1", (order_id,))
    if not link or not str(link.get("chat_id") or ""):
        return {"empty": True, "verdict": "Чат не привязан к заказу"}
    rows = db.query(
        "SELECT at, direction, kind, text, answer, operator FROM client_bot_log"
        " WHERE chat_id=? ORDER BY id ASC LIMIT 300", (str(link["chat_id"]),))
    payload = summarize(rows or [])
    payload["empty"] = not payload.get("counts", {}).get("total")
    return payload
