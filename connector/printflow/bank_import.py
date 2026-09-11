"""Импорт банковской выписки CSV (идея M1 из списка идей).

Выгрузка из банка → строки разносятся по статьям автоматически по правилам
(совпадение по ключевым словам в назначении платежа). Правила хранятся в
настройке ``bank_rules`` и редактируются в интерфейсе; ниже — стартовый
набор примеров, которые владелец подгоняет под свой банк.

Разбор CSV не привязан к формату конкретного банка: разделитель и имена
колонок определяются по первой строке (дата/сумма/назначение в разных
написаниях).
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any

from .accounting import num

# Стартовые правила: ключевые слова в назначении → вид и статья проводки.
DEFAULT_BANK_RULES: list[dict[str, Any]] = [
    {"match": "ozon", "kind": "income", "category": "sale",
     "title": "Продажа (Ozon)"},
    {"match": "wildberries|вб", "kind": "income", "category": "sale",
     "title": "Продажа (Wildberries)"},
    {"match": "авито", "kind": "income", "category": "sale",
     "title": "Продажа (Авито)"},
    {"match": "сбербанк онлайн перевод|перевод от", "kind": "income",
     "category": "sale", "title": "Перевод от клиента"},
    {"match": "эквайринг|терминал", "kind": "income", "category": "sale",
     "title": "Оплата картой"},
    {"match": "пластик|филамент|петг|petg|pla|abs|tpu", "kind": "expense",
     "category": "filament", "title": "Закупка пластика"},
    {"match": "электроэнергия|энергосбыт|тнс", "kind": "expense",
     "category": "energy", "title": "Электричество"},
    {"match": "комиссия|обслуживание счета|обслуживание счёта",
     "kind": "expense", "category": "fee", "title": "Комиссия банка"},
    {"match": "аренда", "kind": "expense", "category": "rent",
     "title": "Аренда"},
    {"match": "налог", "kind": "expense", "category": "tax",
     "title": "Налог"},
]

# Допустимые имена колонок (нижний регистр, без ё).
_DATE_COLS = {"дата", "date", "дата операции", "дата проводки", "датаоперации"}
_AMOUNT_COLS = {"сумма", "amount", "сумма операции", "суммаоперации",
                "сумма в валюте счета", "суммаввалютесчета", "приход",
                "расход"}
_DESC_COLS = {"назначение", "описание", "description", "назначение платежа",
              "назначениеплатежа", "наименование", "детали", "контрагент",
              "наименование контрагента"}


def _norm(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", str(value).lower().replace("ё", "е"))


def parse_csv(text: str) -> list[dict[str, Any]]:
    """Разобрать CSV-выписку в строки {date, amount, description}.

    Разделитель и колонки определяются автоматически. Сумма положительная —
    расход, отрицательная — приход (и наоборот для «Приход/Расход»-колонок).
    """
    sample = text[:4096]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        pass
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        return []
    fields = {_norm(name): name for name in reader.fieldnames}
    date_col = next((fields[key] for key in fields if key in _DATE_COLS), "")
    amount_col = next((fields[key] for key in fields if key in _AMOUNT_COLS), "")
    desc_col = next((fields[key] for key in fields if key in _DESC_COLS), "")
    if not date_col or not amount_col:
        return []
    rows: list[dict[str, Any]] = []
    for raw in reader:
        amount_raw = (raw.get(amount_col) or "").replace("\u00a0", "").strip()
        if not amount_raw:
            continue
        try:
            amount = float(amount_raw.replace(",", "."))
        except ValueError:
            continue
        # в выписках расход обычно отрицательный; у «Приход/Расход» — наоборот
        if amount_col in ("приход", "расход") and amount < 0:
            amount = -amount
        date = str(raw.get(date_col) or "").strip()[:10]
        description = " ".join(str(raw.get(desc_col) or "").split())
        if not date or not description:
            continue
        rows.append({"date": date, "amount": amount,
                     "description": description})
    return rows


def classify(description: str, rules: list[dict[str, Any]] | None = None) \
        -> dict[str, Any] | None:
    """Подобрать правило по назначению платежа. None — правило не найдено."""
    for rule in rules or DEFAULT_BANK_RULES:
        pattern = str(rule.get("match") or "")
        if pattern and re.search(pattern, description, re.IGNORECASE):
            return {"kind": rule.get("kind", "expense"),
                    "category": rule.get("category", "other"),
                    "title": rule.get("title", description[:60]),
                    "rule": pattern}
    return None



def sbp_taken(db, date: str, amount: float) -> dict | None:
    """Найден ли уже такой доход в журнале как подтверждённый СБП-платёж.

    Зачем. Выписка в один и тот же период можно занести двумя дорожками:
    «Поступления из банка» (`/api/bank/*`, money ведёт через ядро СБП) и
    «Импорт выписки» (`/api/bank/import-*`, разносит по статьям напрямую).
    Раньше вторая дорожка не знала о первой и писала второй доход на ту же
    кассу: выручка и налоговая база удваивались ровно на сумму СБП-чеков.
    Здесь импорт уступает: если такое поступление уже проведено как СБП —
    строка пропускается с явной причиной, а не заводится повторно.

    Сумма сверяется точно (копейка в копейку), дата — в окне
    ``sbp_match_window_hours`` (тот же регулятор, что у сопоставления банка).
    """
    try:
        days = max(1, int(num(db.setting("sbp_match_window_hours", 24), 24) / 24.0) + 1)
    except Exception:
        days = 2
    row = db.one(
        "SELECT p.id AS payment_id, p.number, p.status, COALESCE(t.at, p.confirmed_at) AS at"
        " FROM sbp_payments p"
        " LEFT JOIN transactions t ON t.id = p.tx_id"
        " WHERE p.status IN ('confirmed','refunded')"
        "   AND ROUND(COALESCE(t.amount, p.amount), 2) = ?"
        "   AND ABS(julianday(substr(COALESCE(t.at, p.confirmed_at), 1, 10))"
        "       - julianday(?)) <= ?"
        " LIMIT 1", (round(num(amount), 2), str(date or "")[:10], days))
    return dict(row) if row else None

def preview(db, text: str) -> dict[str, Any]:
    """Предпросмотр импорта: что распозналось, что останется без правила."""
    rules = db.setting("bank_rules", DEFAULT_BANK_RULES) or DEFAULT_BANK_RULES
    if not isinstance(rules, list):
        rules = DEFAULT_BANK_RULES
    rows: list[dict[str, Any]] = []
    matched = unmatched = duplicates = sbp_taken_rows = 0
    for row in parse_csv(text):
        rule = classify(row["description"], rules)
        entry = {
            "date": row["date"],
            "amount": round(row["amount"], 2),
            "description": row["description"][:120],
            "kind": "", "category": "", "title": "", "matched": False,
            "duplicate": False,
        }
        if rule:
            entry.update({"kind": rule["kind"], "category": rule["category"],
                          "title": rule["title"], "matched": True})
        duplicate = db.one(
            "SELECT id FROM transactions WHERE at>=? AND at<? AND amount=?"
            " AND title=? LIMIT 1",
            (f"{row['date']}T00:00:00", f"{row['date']}T23:59:59",
             abs(round(row["amount"], 2)),
             entry["title"] or row["description"][:60]))
        if duplicate:
            entry["duplicate"] = True
            duplicates += 1
        # СБП-выручка, уже проведённая через ядро СБП, — не второй доход
        entry["sbp_taken"] = {}
        if entry["matched"] and row["amount"] > 0:
            taken = sbp_taken(db, row["date"], row["amount"])
            if taken:
                entry["sbp_taken"] = taken
                entry["duplicate"] = True
                entry["note"] = f"уже проведено как СБП-платёж №{taken.get('number') or taken.get('payment_id')}"
                duplicates += 1
                sbp_taken_rows += 1
        if entry["matched"]:
            matched += 1
        else:
            unmatched += 1
        rows.append(entry)
    return {"rows": rows, "matched": matched, "unmatched": unmatched,
            "duplicates": duplicates, "sbp_taken": sbp_taken_rows,
            "rules": [dict(r) for r in rules]}


def apply_rows(db, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Провести распознанные строки как транзакции (дубли пропускаются).

    Дубликаты проверяются повторно по базе — на случай, если предпросмотр
    делался до предыдущего импорта. Отдельно проверяется СБП: доход, уже
    записанный ядром СБП при подтверждении платежа, здесь не повторяется —
    иначе одна и та же оплата попадает в выручку (и в налоговую базу) дважды.
    """
    imported = skipped = sbp_skipped = 0
    for row in rows:
        if not row.get("matched"):
            skipped += 1
            continue
        if row.get("sbp_taken") or (num(row.get("amount")) > 0 and sbp_taken(
                db, str(row.get("date") or "")[:10], num(row.get("amount")))):
            skipped += 1
            sbp_skipped += 1
            continue
        amount = abs(num(row.get("amount")))
        if amount <= 0:
            skipped += 1
            continue
        from .accounting import Accounting
        kind = "income" if row.get("kind") == "income" else "expense"
        date = str(row.get("date") or "")[:10]
        title = str(row.get("title") or "Импорт выписки")[:80]
        duplicate = db.one(
            "SELECT id FROM transactions WHERE at>=? AND at<? AND amount=?"
            " AND title=? LIMIT 1",
            (f"{date}T00:00:00", f"{date}T23:59:59", round(amount, 2), title))
        if duplicate:
            skipped += 1
            continue
        try:
            Accounting(db).add_transaction(
                kind, str(row.get("category") or "other"), amount, title,
                note=f"импорт выписки: {str(row.get('description') or '')[:120]}",
                at=f"{date}T12:00:00", auto=True)
            imported += 1
        except Exception:
            skipped += 1
    detail = f"проведено проводок: {imported}, пропущено: {skipped}"
    if sbp_skipped:
        detail += f" (из них уже проведено как СБП: {sbp_skipped})"
    db.add_event("finance", "Импорт банковской выписки", detail, "", {})
    return {"imported": imported, "skipped": skipped, "sbp_skipped": sbp_skipped}
