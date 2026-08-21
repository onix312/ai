"""Конверты-накопления PrintFlow 5.0.

С каждого дохода автоматически откладывается процент в «конверты»: на налог,
на пластик, на второй принтер. Это видимая копилка: сколько денег реально
свободно, а не просто лежит в кассе.

Конверты хранятся в двух таблицах: `envelopes` (настройка) и
`envelope_moves` (история движений). Баланс считается по движениям.
"""
from __future__ import annotations


from .accounting import num, uid
from .config import now_iso
from .db import Database


class Envelopes:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- список
    def list(self) -> list[dict]:
        rows = self.db.query("SELECT * FROM envelopes WHERE archived=0 ORDER BY position, name")
        out = []
        for row in rows:
            agg = self.db.one(
                "SELECT COALESCE(SUM(amount),0) v FROM envelope_moves WHERE envelope_id=?",
                (row["id"],)) or {}
            balance = round(num(agg.get("v")), 2)
            goal = num(row.get("goal"))
            out.append({
                **row,
                "balance": balance,
                "goal_progress": round(balance / goal * 100, 1) if goal else None,
            })
        return out

    def total(self) -> float:
        return round(sum(num(e["balance"]) for e in self.list()), 2)

    # ------------------------------------------------------------- запись
    def save(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("id"):
            data["id"] = uid("env")
            row = self.db.one("SELECT COALESCE(MAX(position),0) p FROM envelopes") or {}
            data.setdefault("position", int(num(row.get("p"))) + 1)
        if not (data.get("name") or "").strip():
            raise ValueError("Укажите название конверта")
        data["pct"] = max(0.0, min(100.0, num(data.get("pct"))))
        return self.db.upsert("envelopes", data)

    def delete(self, env_id: str) -> None:
        self.db.execute("UPDATE envelopes SET archived=1 WHERE id=?", (env_id,))

    # ----------------------------------------------------------- движения
    def add_move(self, envelope_id: str, amount: float, note: str = "",
                 tx_id: str = "", order_id: str = "") -> dict:
        """Положить (положительное) или взять (отрицательное) из конверта."""
        if not self.db.one("SELECT id FROM envelopes WHERE id=?", (envelope_id,)):
            raise ValueError("Конверт не найден")
        amount = round(num(amount), 2)
        if amount == 0:
            raise ValueError("Сумма должна быть не нулевой")
        return self.db.upsert("envelope_moves", {
            "id": uid("evm"), "at": now_iso(), "envelope_id": envelope_id,
            "amount": amount, "note": note, "tx_id": tx_id or None,
            "order_id": order_id or None})

    def withdraw(self, envelope_id: str, amount: float, note: str = "") -> dict:
        """Забрать деньги из конверта (например, уплатить налог)."""
        amount = abs(num(amount))
        return self.add_move(envelope_id, -amount, note or "изъятие из конверта")


def auto_allocate(db: Database, tx: dict) -> None:
    """Отложить проценты с дохода по всем конвертам. Вызывается после проводки."""
    if not db.setting("envelope_auto", False):
        return
    if (tx.get("kind") or "") != "income":
        return
    amount = num(tx.get("amount"))
    if amount <= 0:
        return
    envelopes = Envelopes(db)
    for env in db.query("SELECT * FROM envelopes WHERE archived=0 AND pct>0"):
        cut = round(amount * num(env.get("pct")) / 100.0, 2)
        if cut <= 0:
            continue
        envelopes.add_move(env["id"], cut, note=f"авто: {cut} ₽ с дохода",
                           tx_id=tx.get("id"), order_id=tx.get("order_id"))
