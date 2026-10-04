"""Документы B2B PrintFlow 5.0: счёт, КП, товарный чек и накладная из заказа.

Генерирует готовый к печати HTML с реквизитами из настроек (legal_name, inn).
Формируется на сервере и открывается как отдельная страница — в один клик
из карточки заказа, без внешних сервисов.
"""
from __future__ import annotations

from .accounting import num
from .config import now_iso
from .db import Database
from urllib.parse import quote


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


def _v2_document(title: str, document_id: str, req: dict,
                 customer: str, product: str, number: str, date: str,
                 due: str, total: float, lines: list[dict],
                 fold: dict, kind: str) -> str:
    """Рендер A4 документа по абсолютной геометрии его рецепта v2."""
    from .printing import _print_v2_document

    layout = _print_v2_document(document_id)
    if (layout.get("width_mm"), layout.get("height_mm")) != (210, 297):
        raise RuntimeError(f"Макет {document_id} должен иметь формат A4 210×297 мм")

    inn = req["inn"]
    legal = req["legal_name"]
    buyer = customer or "частное лицо"
    metadata = f"№ {number}    от {date}\n{buyer}"
    if kind == "cp":
        requisites = f"{legal}" + (f" · ИНН {inn}" if inn else "")
        requisites += (f"\nДля: {buyer}. Изделие: {product}."
                       f"\nСрок: {due or 'по согласованию'}."
                       " Цена действует после утверждения образца.")
    elif kind == "waybill":
        sender = legal + (f" · ИНН {inn}" if inn else "")
        requisites = (f"Грузоотправитель: {sender}"
                      f"\nГрузополучатель: {buyer}"
                      f"\nОснование: заказ № {number}")
    else:
        sender = legal + (f" · ИНН {inn}" if inn else "")
        requisites = (f"{sender}\nПокупатель / грузополучатель: {buyer}")
        if kind == "receipt":
            requisites += "\nИзделие изготовлено по индивидуальному заказу."

    headings = ("Наименование", "Кол-во", "Цена", "Сумма")
    block_values = {
        "document-title": title,
        "metadata": metadata,
        "requisites": requisites,
        "total": f"Итого: {_fmt(total)} {req['currency']}",
        "fold-note": "",
        "signature-left": ("Отпустил" if kind == "waybill" else "Исполнитель")
                           + "\n________________",
        "signature-right": ("Получил" if kind == "waybill" else "Заказчик")
                            + "\n________________",
    }
    if fold.get("folded"):
        note = (f"Показано позиций {_fmt(fold['after'])} из {_fmt(fold['before'])}")
        if fold.get("groups"):
            note += " · мелкие товары группами: " + ", ".join(fold["groups"])
        block_values["fold-note"] = note + f" · полный состав — в заказе № {number}."
    foot = ("подтверждает передачу товара. Не является счётом-фактурой."
            if kind == "waybill" else "не является публичной офертой без подписи.")
    block_values["footer"] = f"{legal} · изготовлено локально · {foot}"

    # Recipe provides five rows. Additional rows repeat the same A4 form with
    # its header; totals and signing fields appear only on the final sheet.
    page_rows = 5
    pages = [lines[i:i + page_rows] for i in range(0, len(lines), page_rows)] or [[]]
    blocks_by_id = {block["id"]: block for block in layout["blocks"]}
    sheet_html = []
    for page_index, page_lines in enumerate(pages):
        values = dict(block_values)
        values["metadata"] += (f"\nСтраница {page_index + 1} из {len(pages)}"
                               if len(pages) > 1 else "")
        if page_index < len(pages) - 1:
            for hidden in ("total", "fold-note", "signature-left",
                           "signature-right", "footer"):
                values[hidden] = ""
        for col, label in enumerate(headings):
            values[f"th-{col}"] = label
        for row_index in range(page_rows):
            line = page_lines[row_index] if row_index < len(page_lines) else {}
            values[f"cell-{row_index}-0"] = str(line.get("name") or "")
            values[f"cell-{row_index}-1"] = _qty_text(line.get("qty")) if line else ""
            price_text = _fmt(line.get("price")) if line else ""
            values[f"cell-{row_index}-2"] = (
                ("ср. " if line.get("averaged") else "") + price_text)
            values[f"cell-{row_index}-3"] = _fmt(line.get("amount")) if line else ""

        rendered = []
        for block in layout["blocks"]:
            weight = 700 if "bold" in str(block.get("font", "")).lower() else 400
            style = (
                f'left:{block["x_mm"]}mm;top:{block["y_mm"]}mm;'
                f'width:{block["width_mm"]}mm;height:{block["height_mm"]}mm;'
                f'font-family:Arial,sans-serif;font-size:{block["font_pt"]}pt;'
                f'font-weight:{weight};line-height:{block.get("line_height", 1.25)};'
                f'color:{block["color"]};background:{block.get("background") or "transparent"};'
                f'text-align:{block["align"]};padding:{block.get("text_padding_mm", 0)}mm;'
                f'border-radius:{block.get("radius_mm", 0)}mm'
            )
            if block["kind"] == "logo":
                crop = "-".join(str(int(value)) for value in block["source_crop_px"])
                content = (f'<img src="/assets/brand/nozza-print-crop-{crop}.png" alt="NOZZA" '
                           'style="display:block;width:100%;height:100%;object-fit:contain;'
                           'object-position:left center">')
            else:
                content = _esc(values.get(block["id"], "")).replace("\n", "<br>")
            if block["kind"] == "cell":
                style += (f';border:{block.get("stroke_width_mm", 0.2)}mm solid '
                          f'{block.get("stroke_color") or "#E8E2E7"}')
            rendered.append(
                f'<div class="doc-block doc-{block["kind"]}" data-block="{_esc(block["id"])}" '
                f'style="{style}">{content}</div>')
        sheet_html.append(f'<main class="document-sheet">{"".join(rendered)}</main>')

    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<title>{_esc(title)} №{_esc(number)}</title>"
        "<style>@page{size:A4;margin:0}*{box-sizing:border-box}"
        "html,body{margin:0;min-height:100%;font-family:Arial,sans-serif;color:#31242E}"
        "body{background:#EEEAF0;padding:20px 0}.document-sheet{position:relative;"
        "width:210mm;height:297mm;margin:0 auto 16px;overflow:hidden;background:#fff;"
        "page-break-after:always;break-after:page}.document-sheet:last-of-type{"
        "page-break-after:auto;break-after:auto}.doc-block{position:absolute;overflow:hidden;"
        "overflow-wrap:anywhere;white-space:normal}.doc-logo img{object-fit:contain}"
        ".doc-cell{border-collapse:collapse}@media print{body{background:#fff;padding:0;"
        "-webkit-print-color-adjust:exact;print-color-adjust:exact}"
        ".document-sheet{margin:0;box-shadow:none}}"
        "@media screen{.document-sheet{box-shadow:0 6px 30px #31242E20}}"
        ".no-print{position:fixed;z-index:2;right:18px;top:18px;border:0;border-radius:9px;"
        "padding:11px 18px;background:#6E2BC8;color:#fff;font:600 14px Arial,sans-serif;"
        "cursor:pointer}@media print{.no-print{display:none}}</style></head><body>"
        "<button class=\"no-print\" onclick=\"window.print()\">Печать / PDF</button>"
        + "".join(sheet_html) + "</body></html>"
    )


def _pickup_receipt(order: dict, req: dict, number: str, customer: str,
                    cur: str, lines: list[dict], track_url: str) -> str:
    """Чек выдачи на термоленте 80 мм по печатному рецепту v2."""
    from .qrgen import svg as qr_svg
    from .printing import _print_v2_document

    paid = num(order.get("paid")) + num(order.get("prepaid"))
    price = num(order.get("price"))
    left = max(0.0, price - paid)
    closed = str(order.get("closed_at") or "")
    issued = (closed or now_iso()).replace("T", " ")[:16]
    status = "Выдан полностью" if closed else "Выдача"
    layout = _print_v2_document("pickup")
    extra_rows = max(0, len(lines or []) - 3)
    shift_after_rows = extra_rows * 10
    qr = ""
    if track_url:
        try:
            qr = qr_svg(track_url, level="M", scale=3, border=2)
        except Exception:
            qr = ""

    values = {
        "title": "ЧЕК ВЫДАЧИ ЗАКАЗА",
        "meta": (f"Заказ № {number}\nКлиент: {customer or 'частное лицо'}\n"
                 f"Статус: {status} · {issued}\n{req['legal_name']}"
                 + (f" / ИНН {req['inn']}" if req["inn"] else "")),
        "th-0": "Изделие", "th-1": "Сумма",
        "totals": (f"Итого: {_fmt(price)} {cur}\nОплачено: {_fmt(paid)} {cur}\n"
                   f"{'Долг' if left > 0.005 else 'Остаток'}: {_fmt(left)} {cur}"),
        "qr": qr or "QR\nссылка недоступна",
        "sign": "Заказ получил, претензий нет\n__________________",
    }
    rendered = []
    for block in layout["blocks"]:
        block_id = block["id"]
        value = values.get(block_id, "")
        if block_id.startswith("cell-"):
            _, row_text, col_text = block_id.split("-")
            row_index, col_index = int(row_text), int(col_text)
            item = (lines or [])[row_index] if row_index < len(lines or []) else {}
            if col_index == 0:
                value = str(item.get("name") or "")
            else:
                value = f"{_fmt(item.get('amount'))} {cur}" if item else ""
        top = block["y_mm"]
        if block_id in ("totals", "qr", "sign"):
            top += shift_after_rows
        if block["kind"] == "logo":
            crop = "-".join(str(int(part)) for part in block["source_crop_px"])
            content = (f'<img src="/assets/brand/nozza-print-crop-{crop}.png" alt="NOZZA" '
                       'style="display:block;width:100%;height:100%;object-fit:contain;'
                       'object-position:left center">')
        elif block_id == "qr" and qr:
            content = qr
        else:
            content = _esc(value).replace("\n", "<br>")
        height = block["height_mm"]
        if block_id == "qr" and qr:
            top = block["y_mm"] + shift_after_rows
            content = f'<div class="pickup-qr">{qr}</div>'
        style = (
            f'left:{block["x_mm"]}mm;top:{top}mm;width:{block["width_mm"]}mm;'
            f'height:{height}mm;font:{block["font_pt"]}pt Arial,sans-serif;'
            f'font-weight:{700 if "bold" in block["font"].lower() else 400};'
            f'line-height:{block["line_height"]};color:{block["color"]};'
            f'text-align:{block["align"]};padding:{block.get("text_padding_mm", 0)}mm;'
            f'border-radius:{block.get("radius_mm", 0)}mm'
        )
        if block["kind"] == "cell":
            style += (f';border:{block.get("stroke_width_mm", 0.2)}mm solid '
                      f'{block.get("stroke_color") or "#999999"}')
        if block_id == "qr":
            style += (f';border:{block.get("stroke_width_mm", 0.2)}mm solid '
                      f'{block.get("stroke_color") or "#999999"}')
        rendered.append(
            f'<div class="pickup-block" data-block="{_esc(block_id)}" '
            f'style="{style}">{content}</div>')

    for row_index, item in enumerate((lines or [])[3:], start=3):
        for col_index, (left_mm, width_mm, align) in enumerate(
                ((4, 46, "left"), (50, 26, "right"))):
            value = (str(item.get("name") or "") if col_index == 0
                     else f"{_fmt(item.get('amount'))} {cur}")
            style = (f'left:{left_mm}mm;top:{79 + row_index * 10}mm;'
                     f'width:{width_mm}mm;height:10mm;font:9pt Arial,sans-serif;'
                     f'font-weight:400;line-height:1.25;color:#000;text-align:{align};'
                     'padding:1mm;border:0.2mm solid #999')
            rendered.append(
                f'<div class="pickup-block" data-block="cell-{row_index}-{col_index}" '
                f'style="{style}">{_esc(value)}</div>')

    dynamic_height = max(layout["height_mm"], 190 + shift_after_rows)
    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<title>Чек выдачи №{_esc(number)}</title>"
        "<style>@page{size:80mm auto;margin:0}*{box-sizing:border-box}"
        "html,body{margin:0;padding:0;font-family:Arial,sans-serif;color:#000}"
        "body{width:80mm}.pickup-sheet{position:relative;width:80mm;"
        f"height:{dynamic_height}mm;min-height:190mm;background:#fff;overflow:hidden}}"
        ".pickup-block{position:absolute;overflow:hidden;overflow-wrap:anywhere;"
        "white-space:normal}.pickup-qr{width:26mm;height:26mm;margin:auto}"
        ".pickup-qr svg{display:block;width:100%;height:100%}"
        ".no-print{position:fixed;z-index:2;right:10px;top:10px;padding:8px 14px;"
        "background:#6E2BC8;color:#fff;border:0;border-radius:8px;cursor:pointer}"
        "@media print{.no-print{display:none}body{-webkit-print-color-adjust:exact;"
        "print-color-adjust:exact}}</style></head><body>"
        "<button class=\"no-print\" onclick=\"window.print()\">Печать чека</button>"
        f'<main class="pickup-sheet">{"".join(rendered)}</main></body></html>'
    )


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
            total = round(sum(num(ln["amount"]) for ln in lines), 2)
        else:
            # Цена заказа — итоговая сумма заказа, а не цена штуки (как в
            # экономике и в _order_lines складской накладной): делим на
            # количество, умножать нельзя — иначе итог документа завышался.
            total = round(price, 2)
            unit = round(price / qty, 2) if qty else price
            lines = [{"name": product, "qty": qty, "price": unit,
                      "amount": total, "averaged": False}]

        kind = str(kind or "invoice").strip().lower()
        if kind in ("накладная", "tn", "torg12", "rn"):
            kind = "waybill"

        # ------------------------------------------------------------------ В36
        # Квитанция-чек выдачи: узкая «термолента» вместо A4-документа.
        # Печатная форма подтверждения: состав, сумма, оплата, QR трекинга
        # и строка «получил, претензий нет». Возвращается отдельным HTML
        # со своей таблицей стилей — общий A4-каркас не используется.
        if kind in ("pickup", "выдача", "квитанция"):
            track_base = str(self.db.setting("client_bot_track_url") or "").strip().rstrip("/")
            track_link = (track_base + "/track.html?number="
                          + quote(str(number), safe="")) if (track_base and number) else ""
            receipt_lines = lines if items else [
                {"name": product, "amount": total}]
            return _pickup_receipt(order, req, number, customer, cur,
                                   receipt_lines, track_link)

        titles = {"invoice": "Счёт на оплату", "cp": "Коммерческое предложение",
                  "receipt": "Товарный чек", "waybill": "Товарная накладная"}
        if kind not in titles:
            kind = "invoice"
        return _v2_document(titles[kind], kind, req, customer, str(product),
                            number, date, due, total, lines, fold, kind)
