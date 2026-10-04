"""Печатные формы цеха (обновление 18.0).

Всё, что уходит на бумагу, собирается здесь и только через каркас
:mod:`connector.printflow.printforms`: печатные токены бренда, миллиметры
вместо пикселей, линейка масштаба 100 мм и экранирование данных.

Что было не так до 18.0:

* каждый генератор рисовал свой ``<style>`` и свою палитру — 19 значений
  цвета вне токенов, три разных Arial вместо одного шрифта;
* QR визитки печатался текстовыми блоками «██» моноширинным шрифтом:
  метрика шрифта принтера решала, получится код квадратным или нет, а
  сканировался он как повезёт;
* «стикеры 50 × 25 мм» обещались в докстроке, а печатались 88 × 44 мм;
* имя клиента и название изделия подставлялись в HTML без экранирования,
  а пустой адрес витрины не мешал печати — на лист попадал QR заведомо
  нерабочей относительной ссылки;
* неизвестный шаблон стикера отдавал пустой лист с кодом 200.

Формы: :func:`stickers`, :func:`business_card_html`,
:func:`workshop_report_html`, :func:`warranty_html`. Данные для отчёта
считает :func:`workshop_report`, каталог форм для панели — реестр
:data:`FORMS` вместе с :func:`forms_catalog`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from . import printforms as pf
from .accounting import Accounting, num
from .config import ROOT, now_iso
from .db import Database


def workshop_report(db: Database, days: int = 30) -> dict[str, Any]:
    """Данные для брендированного PDF-отчёта «цеховой отчёт». Идея 21."""
    acc = Accounting(db)
    s = acc.summary(days)
    since = (datetime.now() - timedelta(days=days)).isoformat()
    top_rows = db.query(
        "SELECT COALESCE(product,'') p, COUNT(*) n, COALESCE(SUM(price),0) v"
        " FROM orders WHERE price>0 AND updated_at>=? AND product<>''"
        " GROUP BY p ORDER BY v DESC LIMIT 10", (since,))
    customers_new = db.one(
        "SELECT COUNT(*) n FROM customers WHERE created_at>=?", (since,)) or {}
    top = [{"product": r["p"], "qty": int(num(r["n"])), "revenue": round(num(r["v"]), 0)}
           for r in top_rows if r["p"]]
    return {
        "period_days": days,
        "company": db.setting("company_name", "NOZZA"),
        "generated_at": now_iso(),
        "income": s["income"], "expense": s["expense"], "profit": s["profit"],
        "margin": s["margin"], "print_hours": s["print_hours"],
        "profit_per_print_hour": s["profit_per_print_hour"],
        "grams": s["grams"], "energy_kwh": s["energy_kwh"],
        "jobs_done": s["jobs_done"], "jobs_failed": s["jobs_failed"],
        "failure_rate": s["failure_rate"], "defects_cost": s["defects_cost"],
        "active_orders": s["active_orders"],
        "customers_new": int(num(customers_new.get("n"))),
        "top": top,
        "top_total": round(sum(t["revenue"] for t in top), 0),
    }


# печатные токены (бренд), миллиметры вместо пикселей, линейка 100 мм,
# экранирование данных. До 18.0 каждая форма рисовала свой <style> и свою
# палитру, а QR визитки печатался текстовыми блоками «██» — метрика шрифта
# принтера решала, получится код квадратом или нет.
#: Шаблоны стикеров в упаковку (идея 113). ``accent`` — имя печатного токена
#: из printforms.ACCENTS, а не hex: цвета живут в одном месте.
STICKER_TEMPLATES: dict[str, dict[str, str]] = {
    "guarantee": {
        "title": "Гарантия цеха",
        "text": "Не подошло — покажите заказ: поменяем или перепечатаем.",
        "accent": "ok",
    },
    "pla": {
        "title": "PLA",
        "text": "Напечатано из PLA. Материал и цвет — в паспорте изделия.",
        "accent": "ok",
    },
    "workshop": {
        "title": "Цех NOZZA",
        "text": "Сделано локально. Статус заказа — по QR на упаковке.",
        "accent": "accent",
    },
    "fragile": {
        "title": "Хрупкое",
        "text": "Внутри 3D-печать: не сгибать и не ронять.",
        "accent": "warn",
    },
    "thanks": {
        "title": "Спасибо за заказ",
        "text": "Вопрос или замечание — напишите нам, разберёмся.",
        "accent": "accent-2",
    },
    "nozza": {
        "title": "Сделано в NOZZA",
        "text": "Локальное 3D-производство. Партия указана в паспорте.",
        "accent": "ink",
    },
}


def stickers(kind: str = "all", size: str = pf.DEFAULT_LABEL_SIZE,
             sheet: str = "A4", copies: int = 0) -> str:
    """Стикеры в упаковку (идея 113) на лист выбранного размера.

    Размер наклейки и раскладка считаются по физическим мм
    (:func:`printforms.layout_for`): 50 × 25 → 27 штук на A4 (3 × 9),
    88 × 44 → 10 штук (2 × 5). Лист заполняется целиком: выбранные шаблоны
    идут по кругу, пока не кончатся места (``copies`` — напечатать ровно
    столько). Неизвестный шаблон или размер — ошибка: раньше ``kind=nope``
    отдавал пустой лист с кодом 200, и вместо стикеров владелец получал
    чистую бумагу.
    """
    wanted = str(kind or "all").strip() or "all"
    if wanted in ("", "all"):
        kinds = list(STICKER_TEMPLATES)
    else:
        kinds = [part.strip() for part in wanted.split(",") if part.strip()]
    unknown = [k for k in kinds if k not in STICKER_TEMPLATES]
    if unknown:
        raise ValueError("Неизвестный шаблон стикера: " + ", ".join(unknown)
                         + ". Доступно: " + ", ".join(STICKER_TEMPLATES))
    if not kinds:
        raise ValueError("Не выбран ни один шаблон стикера")
    layout = pf.layout_for(size, sheet)
    sheet_info = pf.sheet(sheet)
    cell = layout["cell"]
    slots = int(copies) if int(copies or 0) > 0 else int(layout["total"])
    slots = min(slots, 200)
    small = float(cell["w"]) <= 60
    title_pt = 11 if small else 14
    text_pt = 7.5 if small else 9.5
    pad = "2.6mm 3.4mm" if small else "4mm 5mm"
    cells = ""
    for index in range(slots):
        t = STICKER_TEMPLATES[kinds[index % len(kinds)]]
        cells += (
            f'<div class="pf-cell st" style="border-top:1.6mm solid {pf.ACCENTS[t["accent"]]}">'
            f'<b>{pf.esc(t["title"])}</b><span>{pf.esc(t["text"])}</span></div>'
        )
    area_w = SHEET_AREAS[layout["sheet"]]["w"]
    css = pf.grid_css(layout["cols"], layout["rows"], float(cell["w"]),
                      float(cell["h"]), float(layout["gap"]), area_w)
    css += (
        f".st {{ border:.3mm dashed var(--pf-line-strong, #9ca3af);"
        f" border-radius:1.6mm; padding:{pad}; display:flex; flex-direction:column;"
        f" gap:1mm; }}"
        f".st b {{ font-size:{title_pt}pt; line-height:1.15; }}"
        f".st span {{ font-size:{text_pt}pt; line-height:1.25; color:var(--pf-muted); }}"
    )
    meta = (f"{layout['title']} · на листе {layout['total']} "
            f"({layout['cols']} × {layout['rows']})")
    body = (
        f'<div class="pf-head" style="margin-bottom:4mm">'
        f'{pf.brand_line("NOZZA", "стикеры в упаковку")}'
        f'<span class="pf-sub">{pf.esc(meta)}</span></div>'
        f'<div class="pf-grid">{cells}</div>'
        f'{pf.ruler()}'
        f'<div class="pf-note" style="margin-top:2mm">'
        f'Печать A4 в масштабе 100 %, без «вписать в страницу»: иначе размер '
        f'наклейки уедет и разрезка не совпадёт.</div>'
    )
    return pf.page("Стикеры NOZZA", body, css=css,
                   margin=f'{float(sheet_info["margin"])}mm')


#: Полезная площадь листа, мм (для сетки стикеров).
SHEET_AREAS: dict[str, dict[str, float]] = {
    name: {"w": float(s["w"]) - float(s["margin"]) * 2,
           "h": float(s["h"]) - float(s["margin"]) * 2}
    for name, s in pf.SHEETS.items()
}


# ---------------------------------------------------------------- цеховой отчёт
def workshop_report_html(db: Database, days: int = 30) -> str:
    """Цеховой отчёт A4 по 44 абсолютным блокам рецепта v2."""
    report = workshop_report(db, days)
    layout = _print_v2_document("workshop-report")
    if (layout.get("width_mm"), layout.get("height_mm")) != (210, 297):
        raise RuntimeError("Макет workshop-report должен быть A4 210×297 мм")

    money = lambda value: f"{num(value):,.0f}".replace(",", " ")
    values = {
        "document-title": "Цеховой отчёт",
        "metadata": (f"За {report['period_days']} дней · сформирован "
                     f"{str(report['generated_at'])[:10]}\n{report['company']}"),
        "top-title": "Топ изделий",
        "th-0": "Изделие", "th-1": "Шт.", "th-2": "Выручка",
        "kpi-label-0": "Выручка",
        "kpi-value-0": f"{money(report['income'])} ₽",
        "kpi-label-1": "Прибыль / маржа",
        "kpi-value-1": f"{money(report['profit'])} ₽ · {report['margin']:.0f}%",
        "kpi-label-2": "Время печати",
        "kpi-value-2": f"{report['print_hours']:.0f} ч",
        "kpi-label-3": "Пластик",
        "kpi-value-3": f"{money(report['grams'])} г",
        "kpi-label-4": "Заданий / брак",
        "kpi-value-4": f"{report['jobs_done']} / {report['failure_rate']:.0f}%",
        "kpi-label-5": "Новых клиентов",
        "kpi-value-5": str(report["customers_new"]),
        "footer": (f"Прибыль на час печати: {money(report['profit_per_print_hour'])} ₽/ч · "
                   f"энергия: {report['energy_kwh']:.1f} кВт·ч · "
                   f"себестоимость брака: {money(report['defects_cost'])} ₽"),
    }
    top = report["top"][:8]
    for row in range(8):
        item = top[row] if row < len(top) else {}
        values[f"cell-{row}-0"] = str(item.get("product") or ("—" if row == 0 and not top else ""))
        values[f"cell-{row}-1"] = str(item.get("qty", ""))
        values[f"cell-{row}-2"] = f"{money(item['revenue'])} ₽" if item else ""

    blocks = []
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
            content = pf.esc(values.get(block["id"], "")).replace("\n", "<br>")
        if block["kind"] == "cell":
            style += (f';border:{block.get("stroke_width_mm", 0.2)}mm solid '
                      f'{block.get("stroke_color") or "#E8E2E7"}')
        blocks.append(
            f'<div class="report-block report-{block["kind"]}" '
            f'data-block="{pf.esc(block["id"])}" style="{style}">{content}</div>')

    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<title>Цеховой отчёт — {pf.esc(report['company'])}</title>"
        "<style>@page{size:A4;margin:0}*{box-sizing:border-box}"
        "html,body{margin:0;min-height:100%;font-family:Arial,sans-serif;color:#31242E}"
        "body{background:#EEEAF0;padding:20px 0}.report-sheet{position:relative;"
        "width:210mm;height:297mm;margin:0 auto;background:#fff;overflow:hidden}"
        ".report-block{position:absolute;overflow:hidden;overflow-wrap:anywhere;white-space:normal}"
        ".report-logo img{object-fit:contain}.report-cell{border-collapse:collapse}"
        "@media print{body{background:#fff;padding:0;-webkit-print-color-adjust:exact;"
        "print-color-adjust:exact}.report-sheet{margin:0}}"
        "@media screen{.report-sheet{box-shadow:0 6px 30px #31242E20}}"
        ".no-print{position:fixed;z-index:2;right:18px;top:18px;border:0;border-radius:9px;"
        "padding:11px 18px;background:#6E2BC8;color:#fff;font:600 14px Arial,sans-serif;"
        "cursor:pointer}@media print{.no-print{display:none}}</style></head><body>"
        "<button class=\"no-print\" onclick=\"window.print()\">Печать / PDF</button>"
        f'<main class="report-sheet">{"".join(blocks)}</main></body></html>'
    )


def pack_sheet_html(data: dict[str, Any]) -> str:
    """Render the order packing checklist from its A4 v2 recipe."""
    layout = _print_v2_document("pack-sheet")
    if (layout.get("width_mm"), layout.get("height_mm")) != (210, 297):
        raise RuntimeError("Макет pack-sheet должен оставаться A4 210×297 мм")
    order = data["order"]
    items = data.get("items") or []
    if not items:
        items = [{"name": order.get("product"), "qty": order.get("qty") or 1}]
    chunks = [items[index:index + 4] for index in range(0, len(items), 4)] or [[]]
    checklist = [
        "Изделие проверено по чек-листу качества",
        ("Бренд-карточка NOZZA" if data.get("brand_card")
         else "Бренд-карточка не вкладывается"),
        "Бирка с названием и QR (если есть)",
        "Упаковка: плёнка / коробка, вложение — бумага",
        "Если заказ — подарок, убрать ценник",
    ]
    def qty_text(value: Any) -> str:
        qty = num(value)
        if abs(qty - round(qty)) < 0.0005:
            return str(int(round(qty)))
        return f"{qty:.3f}".rstrip("0").rstrip(".")

    sheets = []
    for page_index, rows in enumerate(chunks):
        values = {
            "document-title": f"Карточка упаковки · заказ №{order.get('number') or ''}",
            "metadata": (f"№ {order.get('number') or '—'} · Клиент: "
                         f"{order.get('customer_name') or '—'}"
                         + (f" · Страница {page_index + 1} из {len(chunks)}"
                            if len(chunks) > 1 else "")),
            "th-0": "Изделие", "th-1": "Кол-во",
            "check-title": "Что положить",
            "footer": f"Сформировано автоматически · {now_iso()[:16].replace('T', ' ')}",
        }
        rendered = []
        for block in layout["blocks"]:
            block_id = block["id"]
            value = values.get(block_id, "")
            if block_id.startswith("cell-"):
                row_index, col_index = (int(part) for part in block_id.split("-")[1:])
                item = rows[row_index] if row_index < len(rows) else {}
                value = (str(item.get("name") or order.get("product") or "")
                         if col_index == 0 else
                         qty_text(item.get("qty")) if item else "")
            elif block_id.startswith("check-label-"):
                value = checklist[int(block_id.rsplit("-", 1)[1])]

            style = (
                f'left:{block["x_mm"]}mm;top:{block["y_mm"]}mm;'
                f'width:{block["width_mm"]}mm;height:{block["height_mm"]}mm;'
                f'font-family:Arial,sans-serif;font-size:{block["font_pt"]}pt;'
                f'font-weight:{700 if "bold" in block["font"].lower() else 400};'
                f'line-height:{block["line_height"]};color:{block["color"]};'
                f'text-align:{block["align"]};padding:{block.get("text_padding_mm", 0)}mm;'
                f'background:{block.get("background") or "transparent"};'
                f'border-radius:{block.get("radius_mm", 0)}mm'
            )
            if block["kind"] == "logo":
                crop = "-".join(str(int(part)) for part in block["source_crop_px"])
                content = (f'<img src="/assets/brand/nozza-print-crop-{crop}.png" '
                           'alt="NOZZA" style="display:block;width:100%;height:100%;'
                           'object-fit:contain;object-position:left center">')
            elif block["kind"] == "checkbox":
                style += (f';border:{block.get("stroke_width_mm", 0.2)}mm solid '
                          f'{block.get("stroke_color") or "#D9C8CE"}')
                content = ""
            else:
                content = pf.esc(value).replace("\n", "<br>")
            if block["kind"] == "cell":
                style += (f';border:{block.get("stroke_width_mm", 0.2)}mm solid '
                          f'{block.get("stroke_color") or "#D9C8CE"}')
            rendered.append(
                f'<div class="pack-block pack-{block["kind"]}" '
                f'data-block="{pf.esc(block_id)}" style="{style}">{content}</div>')
        sheets.append(f'<main class="pack-sheet">{"".join(rendered)}</main>')

    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        f"<title>Карточка упаковки — заказ №{pf.esc(order.get('number') or '')}</title>"
        "<style>@page{size:A4;margin:0}*{box-sizing:border-box}"
        "html,body{margin:0;padding:0;font-family:Arial,sans-serif;color:#31242E}"
        "body{background:#EEEAF0;padding:20px 0}.pack-sheet{position:relative;"
        "width:210mm;height:297mm;margin:0 auto 16px;overflow:hidden;background:#fff;"
        "page-break-after:always;break-after:page}.pack-sheet:last-of-type{"
        "page-break-after:auto;break-after:auto}.pack-block{position:absolute;"
        "overflow:hidden;overflow-wrap:anywhere;white-space:normal}"
        "@media screen{.pack-sheet{box-shadow:0 6px 30px #31242E20}}"
        "@media print{body{background:#fff;padding:0;-webkit-print-color-adjust:exact;"
        "print-color-adjust:exact}.pack-sheet{margin:0}}"
        ".no-print{position:fixed;z-index:2;right:18px;top:18px;border:0;"
        "border-radius:9px;padding:11px 18px;background:#6E2BC8;color:#fff;"
        "font:600 14px Arial,sans-serif;cursor:pointer}"
        "@media print{.no-print{display:none}}</style></head><body>"
        "<button class=\"no-print\" onclick=\"window.print()\">Печать / PDF</button>"
        + "".join(sheets) + "</body></html>"
    )


# ---------------------------------------------------------------- визитка 2.0
def _print_base_url(db: Database) -> str:
    """Адрес витрины для QR: настройка public_url без хвостового слэша.

    Пустая настройка раньше не отменяла печать: QR всё равно рисовался и
    кодировал относительный «/track.html», то есть заведомо нерабочую ссылку.
    Теперь пустой адрес — это отсутствие QR и честная подпись на листе.
    """
    return str(db.setting("public_url", "") or "").strip().rstrip("/")


def _print_v2_document(document_id: str) -> dict[str, Any]:
    """Прочитать один физический рецепт из каталога печатных макетов v2."""
    spec_path = ROOT / "site" / "assets" / "print-layouts-v2.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    layout = next((item for item in spec.get("documents", [])
                   if item.get("id") == document_id), None)
    if not layout:
        raise RuntimeError(f"Макет {document_id} не найден в print-layouts-v2.json")
    return layout


def business_card_html(db: Database, customer_id: str = "") -> str:
    """Визитка 2.0 (идея 115): 4 карточки 85 × 55 мм на A4, векторный QR.

    Что изменилось против 8.5:

    * QR рисует ``qrgen.svg()`` (одной ``<path>``), а не текстовые «██» —
      код стал квадратным и не зависит от метрики шрифта принтера;
    * имя, компания и адрес экранируются и ограничиваются по длине;
    * при пустом ``public_url`` QR не печатается, а на карточке стоит
      подпись «адрес витрины не настроен»;
    * неизвестный клиент — ``ValueError`` (маршрут отдаёт 404), иначе на
      визитке молча печатался бы шаблон без имени.
    """
    from .qrgen import svg as qr_svg

    code = ""
    name = ""
    if customer_id:
        c = db.one("SELECT * FROM customers WHERE id=?", (customer_id,))
        if not c:
            raise ValueError(f"Клиент не найден: {customer_id}")
        code = str(c.get("portal_code") or "")
        name = str(c.get("name") or "")
    public = _print_base_url(db)
    qr_url = f"{public}/my.html?code={quote(code.upper(), safe='')}" if code else f"{public}/track.html"
    qr = ""
    if public and qr_url:
        try:
            qr = qr_svg(qr_url, level="M", border=2, compact=True)
        except Exception:
            qr = ""
    if qr:
        mark = qr
        qr_note = ""
    else:
        mark = "QR<br>место кода"
        qr_note = "QR не напечатан: адрес витрины не настроен (настройка «Адрес витрины»)."
    company = str(db.setting("company_name", "NOZZA") or "NOZZA")
    client = pf.short_name(name, 38)
    layout = _print_v2_document("business-personal")
    if not layout or layout.get("width_mm") != 85 or layout.get("height_mm") != 55:
        raise RuntimeError("Макет business-personal 85×55 мм не найден в print-layouts-v2.json")

    values = {
        "slogan": "3D-производство\n" + (pf.short_name(company, 36) if company.upper() != "NOZZA"
                                           else "3D-цех · напечатаем по вашим размерам"),
        "contact": "\n".join((
            f"Мой NOZZA: {code.upper()}" if code else "Мой NOZZA: уточните код заказа",
            f"Витрина: {public}" if public else "Витрина не настроена",
            f"Для: {client}" if client else "Для покупателя",
        )),
    }
    blocks = []
    for block in layout["blocks"]:
        font_weight = 700 if "bold" in block["font"].lower() else 400
        styles = (
            f'left:{block["x_mm"]}mm;top:{block["y_mm"]}mm;'
            f'width:{block["width_mm"]}mm;height:{block["height_mm"]}mm;'
            f'font-size:{block["font_pt"]}pt;font-weight:{font_weight};'
            f'line-height:{block["line_height"]};color:{block["color"]};'
            f'background:{block.get("background") or "transparent"};'
            f'text-align:{block["align"]};padding:{block.get("text_padding_mm", 0)}mm'
        )
        if block["kind"] == "logo":
            crop = "-".join(str(int(value)) for value in block["source_crop_px"])
            content = (f'<img src="/assets/brand/nozza-print-crop-{crop}.png" '
                       'alt="NOZZA" style="display:block;width:100%;height:100%;'
                       'object-fit:contain;object-position:left center">')
            kind = "logo"
        elif block["kind"] == "qr":
            content = mark
            kind = "qr"
        else:
            value = values.get(block["id"], block.get("text", ""))
            content = pf.esc(value).replace("\n", "<br>")
            kind = "text"
        if block["kind"] == "qr":
            styles += (f';border:{block.get("stroke_width_mm", 0)}mm solid '
                       f'{block.get("stroke_color") or "#D9C8CE"};display:grid;place-items:center')
        extra_class = " bc-noqr" if block["kind"] == "qr" and not qr else ""
        role = (f' role="img" aria-label="QR: {pf.esc(qr_url)}"'
                if block["kind"] == "qr" and qr else "")
        blocks.append(
            f'<div class="bc-block bc-{kind}{extra_class}"{role} '
            f'style="{styles}">{content}</div>')
    cards = ''.join(f'<div class="bc-card">{"".join(blocks)}</div>' for _ in range(4))
    css = """
  .pf-grid { display:grid; width:178mm; margin:0 auto;
             grid-template-columns:repeat(2,85mm); grid-auto-rows:55mm; gap:8mm; }
  .bc-card { position:relative; width:85mm; height:55mm; overflow:hidden;
             box-sizing:border-box; background:#fff; color:#31242e;
             font:10pt Arial,sans-serif; page-break-inside:avoid; break-inside:avoid; }
  .bc-block { position:absolute; box-sizing:border-box; overflow:hidden;
              white-space:pre-line; line-height:1.25; }
  .bc-block img { display:block; width:100%; height:100%;
                  object-fit:contain; object-position:left center; }
  .bc-qr { font-size:7pt; color:#66555f; text-align:center; }
  .bc-qr svg { display:block; width:100%; height:100%; }
  .bc-noqr { display:grid; place-content:center; }
"""
    body = (
        f'<div class="pf-grid">{cards}</div>'
        f'{pf.ruler()}'
        f'<div class="pf-note" style="margin-top:2mm">Резать по границе: 85 × 55 мм, бумага 250–300 г/м².'
        f'{" " + pf.esc(qr_note) if qr_note else ""}</div>'
    )
    return pf.page(f"Визитки {pf.short_name(company, 28)}", body, css=css, margin="10mm")


# ---------------------------------------------------------------- гарантийный талон
def warranty_html(db: Database, order_id: str, customer_id: str = "") -> str:
    """Гарантийный талон заказа (идея 123) — отдельный лист A5 по JSON.

    Срок гарантии печатается только если он задан в настройках
    (``warranty_months``); иначе на листе строка для заполнения от руки —
    система не выдумывает обещания, которых владелец не давал.
    """
    from .qrgen import svg as qr_svg

    order = db.one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not order:
        raise ValueError("Заказ не найден")
    cid = customer_id or str(order.get("customer_id") or "")
    code = ""
    if cid:
        c = db.one("SELECT portal_code FROM customers WHERE id=?", (cid,)) or {}
        code = str(c.get("portal_code") or "")
    public = _print_base_url(db)
    qr = ""
    qr_url = ""
    if public and code:
        try:
            qr_url = f"{public}/my.html?code={quote(code.upper(), safe='')}"
            qr = qr_svg(qr_url, level="M", border=2, compact=True)
        except Exception:
            qr = ""
            qr_url = ""
    months = num(db.setting("warranty_months", 0), 0)
    if months:
        term = f"Срок гарантии: {int(months)} мес. с даты выдачи"
    else:
        term = "Срок гарантии: ______ мес. с даты выдачи"
    issued = str(order.get("closed_at") or order.get("updated_at") or "")[:10]
    number = str(order.get("number") or order_id)
    product = pf.short_name(str(order.get("product") or "изделие"), 60)
    layout = _print_v2_document("warranty")
    if (layout.get("width_mm"), layout.get("height_mm")) != (148, 210):
        raise RuntimeError("Макет warranty должен оставаться форматом A5 148×210 мм")
    values = {
        "title": "Гарантийный талон",
        "field-0": f"Заказ: № {number}",
        "field-1": f"Изделие: {product}",
        "field-2": f"Дата выдачи: {issued or '—'}",
        "field-3": term,
        "terms": "Гарантия не покрывает механические повреждения, нагрузку выше "
                 "расчётной и нагрев выше рабочей температуры материала.",
        "qr": qr or "QR\nместо кода",
        "qr-note": "Заказ, фото и статус «Мой NOZZA»\n" + (
            public.removeprefix("https://").removeprefix("http://")
            if qr_url else "Адрес витрины не настроен" if not public
            else "Личный QR-код заказа не настроен"),
        "signatures": "Выдал ______________       Получил ______________",
    }
    blocks = []
    for block in layout["blocks"]:
        font_weight = 700 if "bold" in str(block.get("font", "")).lower() else 400
        styles = (
            f'left:{block["x_mm"]}mm;top:{block["y_mm"]}mm;'
            f'width:{block["width_mm"]}mm;height:{block["height_mm"]}mm;'
            f'font-family:Arial,sans-serif;font-size:{block["font_pt"]}pt;'
            f'font-weight:{font_weight};line-height:{block["line_height"]};'
            f'color:{block["color"]};background:{block.get("background") or "transparent"};'
            f'text-align:{block["align"]};padding:{block.get("text_padding_mm", 0)}mm;'
            f'border-radius:{block.get("radius_mm", 0)}mm'
        )
        if block["kind"] == "logo":
            crop = "-".join(str(int(value)) for value in block["source_crop_px"])
            content = (f'<img src="/assets/brand/nozza-print-crop-{crop}.png" '
                       'alt="NOZZA" style="display:block;width:100%;height:100%;'
                       'object-fit:contain;object-position:left center">')
        elif block["kind"] == "qr" and qr:
            content = qr
        elif block["kind"] == "qr":
            content = pf.esc(block.get("text") or values["qr"]).replace("\n", "<br>")
        else:
            content = pf.esc(values.get(block["id"], block.get("text", "")))
            content = content.replace("\n", "<br>")
        if block["kind"] == "qr":
            styles += (f';border:{block.get("stroke_width_mm", 0)}mm solid '
                       f'{block.get("stroke_color") or "#D9C8CE"};display:grid;place-items:center')
        role = (f' role="img" aria-label="QR: {pf.esc(qr_url)}"'
                if block["kind"] == "qr" and qr_url else "")
        blocks.append(
            f'<div class="wt-block wt-{block["kind"]}"{role} '
            f'data-block="{pf.esc(block["id"])}" '
            f'style="{styles}">{content}</div>')
    css = """
  .wt-sheet { position:relative; width:148mm; height:210mm; margin:0;
              overflow:hidden; page-break-after:always; break-after:page; }
  .wt-block { position:absolute; box-sizing:border-box; overflow:hidden;
              white-space:pre-line; }
  .wt-logo img { object-fit:contain; object-position:left center; }
  .wt-qr { display:grid; place-items:center; }
  .wt-qr svg { display:block; width:100%; height:100%; }
"""
    body = f'<div class="wt-sheet">{"".join(blocks)}</div>'
    return pf.page(f"Гарантийный талон — заказ №{number}", body, css=css,
                   size="A5", margin="0")


# ---------------------------------------------------------------- таблички цеха
#: Таблички для цеха и витрины. Раньше жили в `marketing.js` четырьмя
#: шаблонами с hex-цветами прямо в JS и собирались строкой в браузере:
#: на одной странице было два независимых печатных контура — серверный
#: (стикеры, визитки) и клиентский (таблички). Теперь контур один.
SIGNS: dict[str, dict[str, str]] = {
    "danger": {
        "title": "ОСТОРОЖНО",
        "text": "Рядом работает 3D-принтер. Не трогайте станок во время печати.",
        "accent": "bad",
    },
    "wash": {
        "title": "Мойте руки",
        "text": "После работы с пластиком и присыпкой. Пластик не для еды.",
        "accent": "accent",
    },
    "delivery": {
        "title": "Печатаем за 1–3 дня",
        "text": "Заказали — напечатали — заберите. Статус заказа — по QR на упаковке.",
        "accent": "ok",
    },
    "gift": {
        "title": "Подарки к празднику",
        "text": "Адресники, таблички и QR-стойки с вашим текстом. Закажите заранее.",
        "accent": "warn",
    },
    "pickup": {
        "title": "Заказ ждёт вас",
        "text": "Готовая печать лежит на полке выдачи. Назовите номер заказа.",
        "accent": "accent-2",
    },
    "care": {
        "title": "Уход за печатью",
        "text": "Протирайте мягкой тканью. PLA не любит жару: не оставляйте в машине летом.",
        "accent": "ink",
    },
}


def signs_html(kind: str = "delivery", note: str = "", copies: int = 0,
               sheet: str = "A4") -> str:
    """Таблички цеха: A4, 2 × 3 = 6 карточек 92 × 65 мм.

    ``note`` — дополнительная строка для конкретного места (например,
    «Стойка №2»), экранируется как и всё остальное с листа.
    """
    if kind not in SIGNS:
        raise ValueError("Неизвестная табличка: " + str(kind) + ". Доступно: "
                         + ", ".join(SIGNS))
    cell_w, cell_h, gap = 92.0, 65.0, 8.0
    sheet_info = pf.sheet(sheet)
    margin = float(sheet_info["margin"])
    cols, rows, total = pf.fit(float(sheet_info["w"]) - margin * 2,
                               float(sheet_info["h"]) - margin * 2, cell_w, cell_h, gap)
    slots = int(copies) if int(copies or 0) > 0 else total
    slots = max(1, min(slots, 24))
    sign = SIGNS[kind]
    note_html = f'<div class="sg-note">{pf.esc(note)}</div>' if note else ""
    cells = "".join(
        f'<div class="pf-cell sg" style="border-top:2mm solid {pf.ACCENTS[sign["accent"]]}">'
        f'<div class="sg-title">{pf.esc(sign["title"])}</div>'
        f'<div class="sg-text">{pf.esc(sign["text"])}</div>{note_html}'
        f'<div class="sg-foot">цех NOZZA · локальная 3D-печать</div></div>'
        for _ in range(slots))
    area_w = float(sheet_info["w"]) - margin * 2
    css = pf.grid_css(cols, rows, cell_w, cell_h, gap, area_w)
    css += """
  .sg { border: .6mm dashed var(--pf-line-strong); border-radius: 3mm;
        padding: 6mm 8mm; display: flex; flex-direction: column; gap: 3mm; }
  .sg-title { font-size: 19pt; font-weight: 800; }
  .sg-text { font-size: 11pt; line-height: 1.35; }
  .sg-note { font-size: 11pt; font-weight: 600; color: var(--pf-accent); }
  .sg-foot { margin-top: auto; font-size: 8pt; color: var(--pf-muted); }
"""
    body = (f'<div class="pf-head" style="margin-bottom:4mm">'
            f'{pf.brand_line("NOZZA", "таблички цеха")}'
            f'<span class="pf-sub">{pf.esc(sign["title"])} · на листе {total}</span></div>'
            f'<div class="pf-grid">{cells}</div>{pf.ruler()}')
    return pf.page("Таблички NOZZA", body, css=css, margin=f"{margin}mm")

# ---------------------------------------------------------------- каталог форм
#: Опись печатных форм цеха: что существует, на какой бумаге, откуда данные.
#: Хаб «Печать» рисуется из этого списка — новая форма добавляется строкой,
#: а не поиском по файлам. ``route`` — то, что открывает форму (GET),
#: ``page`` — LAN-страница, ``api`` — серверный генератор листа.
FORMS: list[dict[str, Any]] = [
    {"id": "price-tags", "group": "Полка",
     "title": "Ценники 67 × 32 и промостенды 67 × 57",
     "paper": "A4, 160–250 г/м²", "size": "67 × 32 мм / 67 × 57 мм",
     "source": "Стеллаж и номенклатура 1С", "kind": "page",
     "page": "/price-tags.html",
     "note": "27 ценников или 15 промостендов на лист, штрихкод 1С"},
    {"id": "signs", "group": "Цех",
     "title": "Таблички цеха и витрины",
     "paper": "A4, 160–250 г/м²", "size": "92 × 65 мм, 6 на лист",
     "source": "Шаблоны цеха + своя строка", "kind": "api",
     "api": "/api/print/signs",
     "note": "осторожно, мойте руки, сроки, подарки, выдача, уход"},
    {"id": "labels", "group": "Цех",
     "title": "QR-наклейки катушек и полки",
     "paper": "A4, самоклеящаяся плёнка", "size": "зависит от этикетки",
     "source": "Катушки и позиции стеллажа", "kind": "page",
     "page": "/labels.html",
     "note": "QR ведёт на LAN-адрес, не на localhost"},
    {"id": "stickers", "group": "Упаковка",
     "title": "Стикеры в упаковку",
     "paper": "A4, матовая самоклейка", "size": "50 × 25 / 88 × 44 мм (режется по сетке)",
     "source": "Шаблоны цеха", "kind": "api",
     "api": "/api/print/stickers",
     "note": "гарантия, материал, цех, хрупкое, спасибо, NOZZA; тираж и размер — параметрами"},
    {"id": "business-card", "group": "Упаковка",
     "title": "Визитки NOZZA",
     "paper": "A4, 250–300 г/м²", "size": "85 × 55 мм, 4 на лист",
     "source": "Настройки и клиент «Мой NOZZA»", "kind": "api",
     "api": "/api/print/business-card",
     "note": "векторный QR на личный кабинет покупателя"},
    {"id": "workshop-report", "group": "Цех",
     "title": "Цеховой отчёт за период",
     "paper": "A4, 80–120 г/м²", "size": "A4",
     "source": "Заказы, задания, деньги", "kind": "api",
     "api": "/api/print/report",
     "note": "внутренний итог или приложение к КП"},
    {"id": "warranty", "group": "Упаковка",
     "title": "Гарантийный талон",
     "paper": "A4, 160 г/м², резать по A5", "size": "148 × 210 мм",
     "source": "Заказ и срок гарантии из настроек", "kind": "api",
     "api": "/api/print/warranty",
     "note": "срок подставляется только если он задан в настройках; QR на «Мой NOZZA»"},
    {"id": "pack-sheet", "group": "Выдача",
     "title": "Карточка упаковки заказа",
     "paper": "A4, 80 г/м²", "size": "A4",
     "source": "Заказ и его состав", "kind": "api",
     "api": "/api/order/pack",
     "note": "что положить в коробку, чек-лист комплектации"},
    {"id": "spool-label", "group": "Склад",
     "title": "Наклейка катушки 58 × 40",
     "paper": "термоэтикетка 62 × 40 мм", "size": "58 × 40 мм",
     "source": "Катушка и её слот AMS", "kind": "api",
     "api": "/api/workshop/spool-label",
     "note": "QR, цвет, материал, сушка"},
    {"id": "pickup-receipt", "group": "Выдача",
     "title": "Чек выдачи 80 мм",
     "paper": "термолента 80 мм", "size": "72 мм ширина",
     "source": "Заказ, оплаты, QR трекинга", "kind": "api",
     "api": "/api/b2b/doc",
     "note": "печать на кассовом принтере или PDF"},
    {"id": "b2b-docs", "group": "Документы",
     "title": "Счёт, КП, товарный чек",
     "paper": "A4, 80 г/м²", "size": "A4",
     "source": "Заказ и реквизиты", "kind": "api",
     "api": "/api/b2b/doc",
     "note": "для юрлиц и самозанятых"},
]


def forms_for(group: str = "") -> list[dict[str, Any]]:
    """Реестр форм, при желании — только выбранная группа."""
    if not group:
        return [dict(item) for item in FORMS]
    return [dict(item) for item in FORMS if item.get("group") == group]


def form_by_id(form_id: str) -> dict[str, Any]:
    """Форма по идентификатору; неизвестная — ошибка с подсказкой."""
    for item in FORMS:
        if item["id"] == form_id:
            return dict(item)
    raise ValueError(f"Неизвестная печатная форма: {form_id}")


def groups() -> list[str]:
    """Группы реестра в порядке объявления."""
    seen: list[str] = []
    for item in FORMS:
        name = str(item.get("group") or "")
        if name and name not in seen:
            seen.append(name)
    return seen


def _choice(values: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    """Варианты выбора для каталога: значение + человеческая подпись."""
    return [{"value": key, "label": str(meta.get("title") or key)}
            for key, meta in values.items()]


def forms_catalog() -> list[dict[str, Any]]:
    """Реестр форм вместе с параметрами — из него панель рисует раздел.

    Параметры описаны данными, а не списком в JavaScript: новый шаблон
    стикера или размер наклейки появляется в панели сам, из этой таблицы.
    """
    options = {
        "stickers": {
            "kind": {"title": "Шаблон", "choices": [{"value": "all", "label": "Все шаблоны"}]
                     + _choice(STICKER_TEMPLATES)},
            "size": {"title": "Размер наклейки", "choices": _choice(pf.LABEL_SIZES)},
            "copies": {"title": "Штук (0 — весь лист)", "min": 0, "max": 200},
        },
        "signs": {
            "kind": {"title": "Табличка", "choices": _choice(SIGNS)},
            "note": {"title": "Своя строка", "text": True,
                     "placeholder": "Например: стойка №2"},
            "copies": {"title": "Штук (0 — весь лист)", "min": 0, "max": 24},
        },
        "business-card": {
            "customer_id": {"title": "Клиент", "source": "customers",
                            "empty": "Без персонализации"},
        },
        "workshop-report": {
            "days": {"title": "Период", "choices": [
                {"value": "7", "label": "7 дней"}, {"value": "30", "label": "30 дней"},
                {"value": "90", "label": "90 дней"}, {"value": "365", "label": "Год"}]},
        },
        "warranty": {
            "order_id": {"title": "Заказ", "source": "orders", "required": True},
        },
    }
    catalog: list[dict[str, Any]] = []
    for form in FORMS:
        item = dict(form)
        item["options"] = options.get(str(form["id"]), {})
        catalog.append(item)
    return catalog
