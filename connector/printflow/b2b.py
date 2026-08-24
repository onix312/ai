"""Документы B2B PrintFlow 5.0: счёт, КП, товарный чек и накладная из заказа.

Генерирует готовый к печати HTML с реквизитами из настроек (legal_name, inn).
Формируется на сервере и открывается как отдельная страница — в один клик
из карточки заказа, без внешних сервисов.
"""
from __future__ import annotations

from .accounting import num
from .config import now_iso
from .db import Database


def _esc(value) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(value) -> str:
    """Сумма с пробелом-разделителем тысяч: 1 200, 57 390."""
    return f"{int(round(num(value))):,}".replace(",", " ")


def _qty_text(value) -> str:
    """Количество без лишних нулей: 3, 2.5. Дроби больше не усекаются до целых."""
    qty = num(value)
    if abs(qty - round(qty)) < 0.0005:
        return str(int(round(qty)))
    return f"{qty:.3f}".rstrip("0").rstrip(".")


def fold_lines(lines: list[dict], collapse_groups: bool = True) -> tuple[list[dict], dict]:
    """Свёртка строк печатной формы без изменения итоговой суммы.

    Два правила уменьшают число позиций в накладной:

    * повторы одной позиции номенклатуры (или одного названия с той же
      ценой) сливаются в одну строку с суммарным количеством;
    * мелкие товары с одинаковой печатной группой (`nomenclature.print_group`)
      показываются одной строкой под именем группы — складские движения при
      этом по-прежнему идут построчно по конкретным товарам.

    Возвращает (rows, info): rows — список {name, qty, price, amount, averaged},
    info — {folded, before, after, groups}.
    """
    rows: list[dict] = []
    index: dict[tuple, int] = {}
    groups: list[str] = []
    before = 0
    for line in lines or []:
        qty = num(line.get("qty"))
        price = num(line.get("price"))
        if qty <= 0:
            continue  # нулевые строки в печатной форме — мусор
        before += 1
        name = str(line.get("name") or "Позиция")
        group = str(line.get("print_group") or "").strip()
        nom_id = str(line.get("nom_id") or "").strip()
        if collapse_groups and group:
            key: tuple = ("group", group.casefold())
        elif nom_id:
            key = ("nom", nom_id)
        else:
            key = ("custom", name.casefold(), round(price, 2))
        if key in index:
            row = rows[index[key]]
            row["qty"] = round(row["qty"] + qty, 3)
            row["amount"] = round(row["amount"] + price * qty, 2)
            if abs(row["price"] - price) > 0.004:
                row["averaged"] = True
            # Цена объединённой строки — средняя по сумме и количеству;
            # итог при этом всегда равен точной сумме по позициям.
            if row["qty"]:
                row["price"] = round(row["amount"] / row["qty"], 2)
            continue
        display = group if (collapse_groups and group) else name
        if collapse_groups and group and group not in groups:
            groups.append(group)
        index[key] = len(rows)
        rows.append({
            "name": display,
            "qty": round(qty, 3),
            "price": round(price, 2),
            "amount": round(price * qty, 2),
            "averaged": False,
        })
    return rows, {
        "folded": before > len(rows),
        "before": before,
        "after": len(rows),
        "groups": groups,
    }


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
        ".foldnote{margin-top:2mm;font-size:9pt;color:#5d6b85}"
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

    def document(self, order_id: str, kind: str = "invoice",
                 group: bool = True) -> str:
        order = self.db.one("SELECT * FROM orders WHERE id=?", (order_id,))
        if not order:
            return _doc_shell("Не найдено", "<p>Заказ не найден.</p>")
        req = _requisites(self.db)
        cur = req["currency"]
        number = str(order.get("number") or "")
        product = order.get("product") or "Изделие"
        qty = max(1.0, num(order.get("qty"), 1))
        price = num(order.get("price"))
        customer = order.get("customer_name") or ""
        due = (order.get("due") or "")[:10]
        date = (order.get("created_at") or now_iso())[:10]

        # Мультизаказ: строки документа — состав заказа, а не одна строка.
        # Цена заказа уже итоговая, умножать её на количество нельзя.
        # Печатная группа мелких товаров и повторы позиций сворачиваются
        # в одну строку (group=1), сумма документа не меняется.
        items = self.db.query(
            "SELECT oi.name, oi.qty, oi.price, oi.nom_id,"
            " COALESCE(n.print_group,'') print_group"
            " FROM order_items oi"
            " LEFT JOIN nomenclature n ON n.id=oi.nom_id"
            " WHERE oi.order_id=? ORDER BY oi.position",
            (order_id,))
        fold: dict = {"folded": False, "before": 0, "after": 0, "groups": []}
        if items:
            lines, fold = fold_lines(items, collapse_groups=group)
            rows = "".join(
                f"<tr><td>{_esc(ln['name'])}</td>"
                f"<td class=\"r\">{_qty_text(ln['qty'])}</td>"
                f"<td class=\"r\">{'ср. ' if ln['averaged'] else ''}{_fmt(ln['price'])}</td>"
                f"<td class=\"r\">{_fmt(ln['amount'])}</td></tr>"
                for ln in lines)
            total = round(sum(num(ln["amount"]) for ln in lines), 2)
        else:
            # Цена заказа — итоговая сумма заказа, а не цена штуки (как в
            # экономике и в _order_lines складской накладной): делим на
            # количество, умножать нельзя — иначе итог документа завышался.
            total = round(price, 2)
            unit = round(price / qty, 2) if qty else price
            rows = (f"<tr><td>{_esc(product)}</td>"
                    f"<td class=\"r\">{_qty_text(qty)}</td>"
                    f"<td class=\"r\">{_fmt(unit)}</td>"
                    f"<td class=\"r\">{_fmt(total)}</td></tr>")

        kind = str(kind or "invoice").strip().lower()
        if kind in ("накладная", "tn", "torg12", "rn"):
            kind = "waybill"

        if kind == "receipt":
            title = "Товарный чек"
            head = (f"<div class=\"head\"><div><div class=\"brand\">{_esc(req['legal_name'])}</div>"
                    f"<div class=\"doc\">{title}</div></div>"
                    f"<div class=\"meta\">Дата: {date}<br>№ {number}</div></div>")
            body = (f"<div class=\"meta\">Покупатель: {_esc(customer or 'частное лицо')}<br>"
                    f"Изделие изготовлено по индивидуальному заказу.</div>")
            left, right = "Исполнитель", "Заказчик"
            foot = "не является публичной офертой без подписи."
        elif kind == "cp":
            title = "Коммерческое предложение"
            head = (f"<div class=\"head\"><div><div class=\"brand\">{_esc(req['legal_name'])}</div>"
                    f"<div class=\"doc\">{title}</div></div>"
                    f"<div class=\"meta\">от {date}</div></div>")
            body = (f"<div class=\"meta\">Для: {_esc(customer or 'заказчик')}<br>"
                    f"Предлагаем изготовить «{_esc(product)}» — {int(qty)} шт. "
                    f"Срок готовности — по согласованию (ориентир: {due or 'уточняется'}). "
                    f"Цена действует после утверждения образца.</div>")
            left, right = "Исполнитель", "Заказчик"
            foot = "не является публичной офертой без подписи."
        elif kind == "waybill":
            title = "Товарная накладная"
            inn = req["inn"]
            head = (f"<div class=\"head\"><div><div class=\"brand\">{_esc(req['legal_name'])}</div>"
                    f"<div class=\"doc\">{title} № {_esc(number)}</div></div>"
                    f"<div class=\"meta\">ИНН: {_esc(inn)}<br>Дата: {date}</div></div>")
            body = (f"<div class=\"meta\">Грузоотправитель: {_esc(req['legal_name'])}"
                    f"{(' · ИНН ' + _esc(inn)) if inn else ''}<br>"
                    f"Грузополучатель: {_esc(customer or '—')}<br>"
                    f"Основание: заказ № {_esc(number)}</div>")
            left, right = "Отпустил", "Получил"
            foot = "подтверждает передачу товара. Не является счётом-фактурой."
        else:
            title = "Счёт на оплату"
            inn = req["inn"]
            head = (f"<div class=\"head\"><div><div class=\"brand\">{_esc(req['legal_name'])}</div>"
                    f"<div class=\"doc\">{title} № {_esc(number)}</div></div>"
                    f"<div class=\"meta\">ИНН: {_esc(inn)}<br>Дата: {date}</div></div>")
            body = f"<div class=\"meta\">Покупатель: {_esc(customer or '—')}</div>"
            left, right = "Исполнитель", "Заказчик"
            foot = "не является публичной офертой без подписи."

        # Сноска о свёртке: клиент видит, что часть строк показана группой,
        # а полный состав остаётся в заказе и складской накладной.
        fold_note = ""
        if fold.get("folded"):
            parts = [f"Показано позиций {_fmt(fold['after'])} из {_fmt(fold['before'])}"]
            if fold.get("groups"):
                parts.append("мелкие товары — группами: "
                             + ", ".join(_esc(g) for g in fold["groups"]))
            parts.append(f"полный состав — в заказе № {_esc(number)}")
            fold_note = f"<div class=\"foldnote\">{'; '.join(parts)}.</div>"

        return _doc_shell(f"{title} №{number}", (
            f"{head}{body}"
            "<table><thead><tr><th>Наименование</th><th class=\"r\">Кол-во</th>"
            "<th class=\"r\">Цена</th><th class=\"r\">Сумма</th></tr></thead><tbody>"
            f"{rows}</tbody></table>"
            f"<div class=\"total\">Итого: <b>{_fmt(total)} {cur}</b></div>"
            f"{fold_note}"
            f"<div class=\"sign\"><div>{left}</div><div>{right}</div></div>"
            f"<div class=\"foot\">{_esc(req['legal_name'])} · изготовлено локально · "
            f"{foot}</div>"
        ))
