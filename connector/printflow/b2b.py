"""Документы B2B PrintFlow 5.0: счёт, КП и товарный чек из заказа.

Генерирует готовый к печати HTML с реквизитами из настроек (legal_name, inn).
Формируется на сервере и открывается как отдельная страница — в один клик
из карточки заказа, без внешних сервисов.
"""
from __future__ import annotations

from .accounting import num, rub
from .config import now_iso
from .db import Database


def _esc(value) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(value) -> str:
    """Сумма с пробелом-разделителем тысяч: 1 200, 57 390."""
    return f"{int(round(num(value))):,}".replace(",", " ")


def _doc_shell(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<title>{_esc(title)}</title>"
        "<style>"
        "@page{size:A4;margin:14mm}*{box-sizing:border-box;margin:0;padding:0}"
        "body{font-family:'Segoe UI',Arial,sans-serif;color:#12203c;font-size:11pt}"
        ".head{display:flex;justify-content:space-between;gap:10mm;margin-bottom:8mm}"
        ".brand{font-size:20pt;font-weight:900;letter-spacing:2px;color:#4f46e5}"
        ".brand small{display:block;font-size:9pt;color:#5d6b85;letter-spacing:1px;margin-top:1mm}"
        ".doc{font-size:15pt;font-weight:800}"
        ".meta{margin-bottom:6mm;font-size:10.5pt;color:#42506e;line-height:1.7}"
        "table{width:100%;border-collapse:collapse;margin:5mm 0}"
        "th{background:#4f46e5;color:#fff;padding:2.6mm 3mm;text-align:left;font-size:9.5pt}"
        "td{border:1px solid #dbe3f2;padding:2.6mm 3mm}"
        "td.r{text-align:right;white-space:nowrap}"
        ".total{font-size:13pt;font-weight:800;text-align:right;margin-top:4mm}"
        ".total b{color:#4f46e5}"
        ".sign{margin-top:14mm;display:flex;gap:20mm}"
        ".sign div{flex:1;border-top:1px solid #12203c;padding-top:2mm;font-size:9.5pt;color:#5d6b85}"
        ".foot{margin-top:8mm;font-size:9pt;color:#5d6b85;border-top:1px dashed #c9d3e8;padding-top:3mm}"
        "@media print{.no-print{display:none}}"
        ".no-print{position:fixed;top:10px;right:10px;padding:8px 14px;background:#4f46e5;color:#fff;"
        "border-radius:8px;font-size:12px;cursor:pointer;border:0}"
        "</style></head><body>"
        "<button class=\"no-print\" onclick=\"window.print()\">Печать / PDF</button>"
        f"{body}</body></html>"
    )


def _requisites(db: Database) -> dict:
    s = db.settings()
    return {
        "legal_name": s.get("legal_name") or s.get("company_name") or "NOZZA",
        "inn": s.get("inn") or "",
        "currency": s.get("currency") or "₽",
    }


class B2B:
    def __init__(self, db: Database):
        self.db = db

    def document(self, order_id: str, kind: str = "invoice") -> str:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            return _doc_shell("Не найдено", "<p>Заказ не найден.</p>")
        req = _requisites(self.db)
        cur = req["currency"]
        number = str(order.get("number") or "")
        product = order.get("product") or "Изделие"
        qty = max(1.0, num(order.get("qty"), 1))
        price = num(order.get("price"))
        total = round(price * qty, 2)
        customer = order.get("customer_name") or ""
        due = (order.get("due") or "")[:10]
        date = (order.get("created_at") or now_iso())[:10]

        rows = (f"<tr><td>{_esc(product)}</td>"
                f"<td class=\"r\">{int(qty)}</td>"
                f"<td class=\"r\">{_fmt(price)}</td>"
                f"<td class=\"r\">{_fmt(total)}</td></tr>")

        if kind == "receipt":
            title = "Товарный чек"
            head = (f"<div class=\"head\"><div><div class=\"brand\">{_esc(req['legal_name'])}</div>"
                    f"<div class=\"doc\">{title}</div></div>"
                    f"<div class=\"meta\">Дата: {date}<br>№ {number}</div></div>")
            body = (f"<div class=\"meta\">Покупатель: {_esc(customer or 'частное лицо')}<br>"
                    f"Изделие изготовлено по индивидуальному заказу.</div>")
        elif kind == "cp":
            title = "Коммерческое предложение"
            head = (f"<div class=\"head\"><div><div class=\"brand\">{_esc(req['legal_name'])}</div>"
                    f"<div class=\"doc\">{title}</div></div>"
                    f"<div class=\"meta\">от {date}</div></div>")
            body = (f"<div class=\"meta\">Для: {_esc(customer or 'заказчик')}<br>"
                    f"Предлагаем изготовить «{_esc(product)}» — {int(qty)} шт. "
                    f"Срок готовности — по согласованию (ориентир: {due or 'уточняется'}). "
                    f"Цена действует после утверждения образца.</div>")
        else:
            title = "Счёт на оплату"
            inn = req["inn"]
            head = (f"<div class=\"head\"><div><div class=\"brand\">{_esc(req['legal_name'])}</div>"
                    f"<div class=\"doc\">{title} № {_esc(number)}</div></div>"
                    f"<div class=\"meta\">ИНН: {_esc(inn)}<br>Дата: {date}</div></div>")
            body = f"<div class=\"meta\">Покупатель: {_esc(customer or '—')}</div>"

        return _doc_shell(f"{title} №{number}", (
            f"{head}{body}"
            "<table><thead><tr><th>Наименование</th><th class=\"r\">Кол-во</th>"
            "<th class=\"r\">Цена</th><th class=\"r\">Сумма</th></tr></thead><tbody>"
            f"{rows}</tbody></table>"
            f"<div class=\"total\">Итого: <b>{_fmt(total)} {cur}</b></div>"
            "<div class=\"sign\"><div>Исполнитель</div><div>Заказчик</div></div>"
            f"<div class=\"foot\">{_esc(req['legal_name'])} · изготовлено локально · "
            "не является публичной офертой без подписи.</div>"
        ))
