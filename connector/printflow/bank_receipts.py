"""Поступления из банка и авто-подтверждение СБП (раунд «авто-СБП»).

Касса 16.0. Деньги по-прежнему не ломаются:

* поступление из банка — это факт из выписки/API, а не выручка сама по себе.
  Выручка появляется только когда поступление подтверждает СБП-платёж через
  ядро ``sbp.confirm`` (проводка на счёт СБП + закрытие долга);
* авто-подтверждение строгое: точная сумма до копейки + ровно один ожидающий
  платёж в окне времени (``sbp_match_window_hours``). Всё сомнительное — в
  «на сверку» с причиной, никогда не подтверждаем молча;
* импорт идемпотентен по ``external_key`` — повторная загрузка выписки не
  создаёт дубли;
* каждое сопоставление и подтверждение — в аудите.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timedelta
from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso
from .sbp import Sbp

SOURCE_TBANK_CSV = "tbank_csv"
STATUS_NEW = "new"
STATUS_MATCHED = "matched"
STATUS_CONFIRMED = "confirmed"
STATUS_UNMATCHED = "unmatched"
STATUS_REVIEW = "review"
STATUSES = (STATUS_NEW, STATUS_MATCHED, STATUS_CONFIRMED, STATUS_UNMATCHED, STATUS_REVIEW)

# Колонки выписки → ключевые слова заголовка (строчные, без учёта регистра).
_HEADER_MAP: dict[str, tuple[str, ...]] = {
    "at": ("дата операции", "дата", "дата списания", "date", "время"),
    "amount": ("сумма операции", "сумма", "сумма в валюте", "amount", "сумма в рублях"),
    "purpose": ("назначение платежа", "назначение", "описание", "комментарий",
                "description", "purpose"),
    "counterparty": ("контрагент", "плательщик", "получатель", "отправитель",
                     "counterparty", "имя плательщика"),
    "status": ("статус", "status"),
}

_DATE_FORMATS = (
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y",
)


def _parse_amount(value: Any) -> float:
    s = str(value or "").strip()
    if not s:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = (s.replace("(", "").replace(")", "")
          .replace("₽", "").replace("\u00a0", " ")
          .replace("\u2009", " ").replace(" ", ""))
    # банковский минус может быть и в конце строки («123,45-»)
    neg = neg or s.startswith("-") or s.endswith("-")
    s = s.replace("-", "")
    if "," in s and "." in s:
        s = s.replace(",", "")          # запятая — разделитель тысяч
    elif "," in s:
        s = s.replace(",", ".")         # запятая — десятичный разделитель
    try:
        value = float(s)
    except ValueError:
        return 0.0
    return -value if neg else value


def _parse_date(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            continue
    # ISO от банка без разбора (например «2026-09-06T10:00:00Z»). Банк пишет
    # локальное время, поэтому смещение отбрасываем и берём «как есть».
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None).isoformat(timespec="seconds")
    except ValueError:
        return ""


def _ts(value: Any) -> float | None:
    """Парс временно́й метки в epoch-секунды для сравнения окна. None — не разобрать."""
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_tbank_csv(text: str) -> list[dict]:
    """Разобрать выписку Т-Банка (CSV с `;` или `,`) в записи поступлений.

    Толерантно к порядку и именам колонок; берём только доход (amount > 0).
    """
    text = (text or "").lstrip("\ufeff")
    if not text.strip():
        return []
    sample = text[:2048]
    delimiter = ";" if sample.count(";") >= sample.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    lines = list(reader)
    if not lines:
        return []
    # ищем строку заголовка по ключевым словам
    header_idx = None
    for i, row in enumerate(lines[:10]):
        joined = " ".join(str(c or "") for c in row).lower()
        if any(word in joined for word in
               ("дата", "сумма", "назначение", "описание", "date", "amount")):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Не нашли строку заголовка в выписке (нужны колонки «Дата» и «Сумма»)")
    header = [str(c or "").strip().lower() for c in lines[header_idx]]
    index: dict[str, int] = {}
    for field, words in _HEADER_MAP.items():
        for i, name in enumerate(header):
            if field in index:
                break
            if any(word in name for word in words):
                index[field] = i
    if "amount" not in index:
        raise ValueError("Не нашли колонку «Сумма» в выписке")
    rows: list[dict] = []
    for row in lines[header_idx + 1:]:
        if not row or not any(str(c or "").strip() for c in row):
            continue
        def cell(field: str) -> str:
            i = index.get(field)
            return str(row[i]).strip() if i is not None and i < len(row) else ""
        amount = _parse_amount(cell("amount"))
        if amount <= 0:
            continue  # расход/списание — не поступление
        at = _parse_date(cell("at")) if "at" in index else ""
        rows.append({
            "at": at,
            "amount": round(amount, 2),
            "currency": "RUB",
            "purpose": cell("purpose") if "purpose" in index else "",
            "counterparty": cell("counterparty") if "counterparty" in index else "",
        })
    return rows


class BankReceipts:
    """Сопоставление поступлений из банка со СБП-платежами."""

    def __init__(self, db, acc: Accounting, sbp: Sbp | None = None):
        self.db = db
        self.acc = acc
        self.sbp = sbp or Sbp(db, acc)

    # --------------------------------------------------------------- импорт
    @staticmethod
    def _external_key(source: str, row: dict) -> str:
        raw = "|".join([
            source, str(row.get("at") or ""), str(row.get("amount") or ""),
            str(row.get("purpose") or ""), str(row.get("counterparty") or ""),
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]

    def ingest(self, rows: list[dict], source: str = SOURCE_TBANK_CSV,
               actor: str = "panel") -> dict:
        """Занести поступления и сопоставить со СБП-платежами. Идемпотентно."""
        if not isinstance(rows, list):
            raise ValueError("Ожидается список поступлений")
        new, skipped, matched, unmatched, confirmed = 0, 0, 0, 0, 0
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            amount = round(num(raw.get("amount")), 2)
            if amount <= 0:
                continue
            key = self._external_key(source, raw)
            existing = self.db.one(
                "SELECT * FROM bank_receipts WHERE external_key=?", (key,))
            if existing:
                skipped += 1
                continue
            receipt_id = uid("br")
            with self.db.transaction():
                self.db.execute(
                    "INSERT INTO bank_receipts"
                    "(id,external_key,source,at,amount,currency,counterparty,purpose,"
                    " status,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,'new',?)",
                    (receipt_id, key, source, str(raw.get("at") or ""), amount,
                     str(raw.get("currency") or "RUB"),
                     str(raw.get("counterparty") or "")[:200],
                     str(raw.get("purpose") or "")[:500], now_iso()))
                result = self._match(receipt_id, actor=actor)
            if result["status"] == STATUS_CONFIRMED:
                confirmed += 1
            elif result["status"] in (STATUS_MATCHED, STATUS_REVIEW):
                matched += 1
            else:
                unmatched += 1
            new += 1
        return {"new": new, "skipped": skipped, "matched": matched,
                "unmatched": unmatched, "confirmed": confirmed}

    # ------------------------------------------------------------ сопоставл.
    def _within_window(self, receipt_at: str, payment_at: str, hours: float) -> bool:
        """Сумма совпала; время — второй фильтр. Нет времени — считаем внутри окна.

        Строгое правило: разница по времени ≤ окна. Если какую-то из дат не
        разобрать, по сумме всё равно можно сопоставить (банк иногда опускает
        время) — это не ослабляет требование точной суммы.
        """
        r = _ts(receipt_at)
        p = _ts(payment_at)
        if r is None or p is None:
            return True
        return abs(r - p) <= hours * 3600

    def _candidates(self, amount: float, receipt_at: str,
                    window_hours: float) -> list[dict]:
        hours = max(1, min(168, num(window_hours, 24) or 24))
        rows = self.db.query(
            "SELECT * FROM sbp_payments WHERE status IN ('new','pending')"
            " AND amount=? ORDER BY datetime(created_at)",
            (round(amount, 2),))
        return [r for r in rows
                if self._within_window(receipt_at, r.get("created_at") or "", hours)]

    def _match(self, receipt_id: str, actor: str = "panel") -> dict:
        receipt = self.db.one("SELECT * FROM bank_receipts WHERE id=?", (receipt_id,))
        if not receipt:
            return {"status": "", "note": "поступление не найдено"}
        window_hours = num(self.db.setting("sbp_match_window_hours", 24), 24)
        candidates = self._candidates(num(receipt["amount"]), receipt.get("at") or "",
                                      window_hours)
        stamp = now_iso()
        if len(candidates) == 1:
            payment = candidates[0]
            auto = bool(self.db.setting("sbp_auto_confirm", True))
            if auto:
                result = self.sbp.confirm(payment["id"], actor=actor,
                                          note="Авто-подтверждение по поступлению из банка")
                self.db.execute(
                    "UPDATE bank_receipts SET status=?,sbp_id=?,matched_at=?,matched_by=?,"
                    "note=? WHERE id=?",
                    (STATUS_CONFIRMED, payment["id"], stamp, actor or "panel",
                     "точное совпадение суммы и времени", receipt_id))
                self._audit(receipt_id, "auto_confirm", "Поступление подтвердило СБП-платёж",
                            f"{num(receipt['amount']):g} RUB → {payment['id']}", actor=actor)
                return self.get(receipt_id)
            self.db.execute(
                "UPDATE bank_receipts SET status=?,sbp_id=?,matched_at=?,matched_by=?,"
                "note=? WHERE id=?",
                (STATUS_MATCHED, payment["id"], stamp, actor or "panel",
                 "авто-подтверждение выключено", receipt_id))
            self._audit(receipt_id, "match", "Поступление связано с СБП-платежом",
                        f"{num(receipt['amount']):g} RUB → {payment['id']}", actor=actor)
            return self.get(receipt_id)
        if len(candidates) > 1:
            numbers = ", ".join(str(c.get("number") or c["id"]) for c in candidates)
            self.db.execute(
                "UPDATE bank_receipts SET status=?,note=? WHERE id=?",
                (STATUS_REVIEW, f"несколько кандидатов: {numbers}", receipt_id))
            self._audit(receipt_id, "review", "Поступление требует ручной сверки",
                        "несколько кандидатов", actor=actor)
            return self.get(receipt_id)
        # точных кандидатов нет — уточняем причину
        any_pending = self.db.one(
            "SELECT COUNT(*) n FROM sbp_payments WHERE status IN ('new','pending')") or {}
        reason = ("сумма не совпадает с ожидающими платежами"
                  if int(num(any_pending.get("n"))) else "нет ожидающего платежа")
        self.db.execute(
            "UPDATE bank_receipts SET status=?,note=? WHERE id=?",
            (STATUS_UNMATCHED, reason, receipt_id))
        return self.get(receipt_id)

    # ------------------------------------------------------- ручные действия
    def get(self, receipt_id: str) -> dict:
        row = self.db.one("SELECT * FROM bank_receipts WHERE id=?", (receipt_id,))
        if not row:
            raise ValueError("Поступление не найдено")
        if row.get("sbp_id"):
            p = self.db.one("SELECT number,status,purpose FROM sbp_payments WHERE id=?",
                            (row["sbp_id"],))
            row["payment_number"] = (p or {}).get("number") or ""
            row["payment_status"] = (p or {}).get("status") or ""
        return row

    def list(self, status: str = "", limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM bank_receipts WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY datetime(created_at) DESC LIMIT ?"
        params.append(int(limit))
        rows = self.db.query(sql, params)
        for row in rows:
            if row.get("sbp_id"):
                p = self.db.one("SELECT number,status FROM sbp_payments WHERE id=?",
                                (row["sbp_id"],))
                row["payment_number"] = (p or {}).get("number") or ""
                row["payment_status"] = (p or {}).get("status") or ""
        return rows

    def state(self) -> dict:
        counts = {s: 0 for s in STATUSES}
        for row in self.db.query("SELECT status, COUNT(*) n FROM bank_receipts GROUP BY status"):
            counts[str(row["status"])] = int(row["n"])
        return {
            "counts": counts,
            "pending_review": self.list(status="review", limit=50)
            + self.list(status="unmatched", limit=50),
            "recent": self.list(status="", limit=30),
            "auto_confirm": bool(self.db.setting("sbp_auto_confirm", True)),
            "window_hours": int(num(self.db.setting("sbp_match_window_hours", 24), 24)),
        }

    def link(self, receipt_id: str, payment_ident: str, actor: str = "panel",
             confirm: bool = False) -> dict:
        """Вручную связать поступление с СБП-платежом (по id или номеру)."""
        receipt = self.db.one("SELECT * FROM bank_receipts WHERE id=?", (receipt_id,))
        if not receipt:
            raise ValueError("Поступление не найдено")
        ident = str(payment_ident or "").strip()
        payment = self.db.one(
            "SELECT * FROM sbp_payments WHERE id=? OR number=? LIMIT 1",
            (ident, ident))
        if not payment:
            raise ValueError("СБП-платёж не найден (укажите id или номер)")
        stamp = now_iso()
        with self.db.transaction():
            self.db.execute(
                "UPDATE bank_receipts SET sbp_id=?,status=?,matched_at=?,matched_by=?,"
                "note=? WHERE id=?",
                (payment["id"], STATUS_MATCHED, stamp, actor or "panel",
                 "связано вручную", receipt_id))
            self._audit(receipt_id, "link", "Поступление связано с платежом вручную",
                        f"{num(receipt['amount']):g} RUB → {payment['id']}", actor=actor)
            if confirm:
                self.sbp.confirm(payment["id"], actor=actor,
                                 note="Подтверждено вручную по поступлению из банка")
                self.db.execute(
                    "UPDATE bank_receipts SET status=? WHERE id=?",
                    (STATUS_CONFIRMED, receipt_id))
        return self.get(receipt_id)

    def confirm(self, receipt_id: str, actor: str = "panel") -> dict:
        """Подтвердить СБП-платёж, к которому привязано поступление."""
        receipt = self.db.one("SELECT * FROM bank_receipts WHERE id=?", (receipt_id,))
        if not receipt:
            raise ValueError("Поступление не найдено")
        if not receipt.get("sbp_id"):
            raise ValueError("Сначала свяжите поступление с СБП-платежом")
        self.sbp.confirm(receipt["sbp_id"], actor=actor,
                         note="Подтверждено вручную по поступлению из банка")
        self.db.execute("UPDATE bank_receipts SET status=?,matched_at=?,matched_by=?,"
                        "note=? WHERE id=?",
                        (STATUS_CONFIRMED, now_iso(), actor or "panel",
                         "подтверждено вручную", receipt_id))
        self._audit(receipt_id, "confirm", "Поступление подтверждено вручную",
                    f"{num(receipt['amount']):g} RUB", actor=actor)
        return self.get(receipt_id)

    # ---------------------------------------------------------------- аудит
    def _audit(self, entity_id: str, action: str, title: str,
               detail: str = "", data: dict | None = None, actor: str = "panel") -> None:
        try:
            self.db.execute(
                "INSERT INTO audit_log(at,entity,entity_id,action,title,detail,data)"
                " VALUES(?,?,?,?,?,?,?)",
                (now_iso(), "bank_receipt", entity_id, action, title, detail,
                 json.dumps({"actor": actor or "panel", **(data or {})}, ensure_ascii=False)))
        except Exception:
            pass
