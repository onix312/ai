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

from datetime import datetime, timedelta
from typing import Any

from . import printforms as pf
from .accounting import Accounting, num
from .config import now_iso
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
    """Печатный цеховой отчёт (идея 21): A4, линейка масштаба, экранирование."""
    r = workshop_report(db, days)
    rows = "".join(
        f"<tr><td>{pf.esc(t['product'])}</td><td>{t['qty']}</td>"
        f"<td>{t['revenue']:,.0f} ₽</td></tr>".replace(",", " ")
        for t in r["top"][:8]) or "<tr><td colspan='3'>—</td></tr>"
    top_line = r["top"][0]["product"] if r["top"] else ""
    css = """
  h1 { font-size: 19pt; font-weight: 800; }
  h2 { font-size: 12pt; margin: 6mm 0 2mm; }
  table { border-collapse: collapse; width: 100%; }
  td, th { border: .25mm solid var(--pf-line); padding: 2mm 3mm; font-size: 10pt; }
  th { background: var(--pf-panel-2); text-align: left; }
  .kpi { display: flex; gap: 3mm; margin: 4mm 0; flex-wrap: wrap; }
  .kpi div { flex: 1 1 28mm; border: .25mm solid var(--pf-line);
             border-radius: 2mm; padding: 3mm; }
  .kpi b { display: block; font-size: 13pt; }
  .kpi span { color: var(--pf-muted); font-size: 8.5pt; }
"""
    kpi = "".join(
        f'<div><b>{value}</b><span>{pf.esc(label)}</span></div>'
        for value, label in (
            (f"{r['income']:,.0f} ₽".replace(",", " "), "выручка"),
            (f"{r['profit']:,.0f} ₽".replace(",", " "),
             f"прибыль (маржа {r['margin']:.0f}%)"),
            (f"{r['print_hours']:.0f} ч", "время печати"),
            (f"{r['grams']:,.0f} г".replace(",", " "), "пластик"),
            (f"{r['jobs_done']}", f"заданий, брак {r['failure_rate']:.0f}%"),
            (f"{r['customers_new']}", "новых клиентов"),
        ))
    foot = pf.join_meta([
        str(r["company"]),
        "локальное 3D-производство",
        f"прибыль на час печати: {r['profit_per_print_hour']:,.0f} ₽/ч".replace(",", " "),
        f"энергия: {r['energy_kwh']:.1f} кВт·ч",
        f"себестоимость брака: {r['defects_cost']:,.0f} ₽".replace(",", " "),
    ])
    body = (
        f'<div class="pf-head"><div><h1>{pf.esc(r["company"])} — цеховой отчёт</h1>'
        f'<div class="pf-sub">за {r["period_days"]} дней · сформирован '
        f'{pf.esc(str(r["generated_at"])[:10])}</div></div>'
        f'<span style="margin-left:auto">{pf.brand_line("NOZZA", "")}</span></div>'
        f'<div class="kpi">{kpi}</div>'
        f'<h2>Топ изделий{(" (" + pf.esc(top_line) + ")") if top_line else ""}</h2>'
        f'<table><tr><th>Изделие</th><th>Шт.</th><th>Выручка</th></tr>{rows}</table>'
        f'<div class="pf-foot"><span>{pf.esc(foot)}</span></div>'
        f'{pf.ruler()}'
    )
    return pf.page(f"Цеховой отчёт — {r['company']}", body, css=css, margin="12mm")


# ---------------------------------------------------------------- визитка 2.0
def _print_base_url(db: Database) -> str:
    """Адрес витрины для QR: настройка public_url без хвостового слэша.

    Пустая настройка раньше не отменяла печать: QR всё равно рисовался и
    кодировал относительный «/track.html», то есть заведомо нерабочую ссылку.
    Теперь пустой адрес — это отсутствие QR и честная подпись на листе.
    """
    return str(db.setting("public_url", "") or "").strip().rstrip("/")


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
    qr_url = ""
    if code:
        qr_url = f"{public}/my.html?code={code.upper()}"
        my_line = (f'Мой NOZZA: <b>{pf.esc(code.upper())}</b> — покажите код, '
                   f'покажем заказ')
    else:
        qr_url = f"{public}/track.html"
        my_line = "Спросите код «Мой NOZZA» — покажем ваш заказ онлайн"
    qr = ""
    if public and qr_url:
        try:
            qr = qr_svg(qr_url, level="M", border=2, compact=True)
        except Exception:
            qr = ""
    if qr:
        mark = f'<div class="bc-qr" role="img" aria-label="QR: {pf.esc(qr_url)}">{qr}</div>'
    else:
        mark = ('<div class="bc-noqr">QR не напечатан: адрес витрины не настроен '
                '(настройка «Адрес витрины»)</div>')
    company = str(db.setting("company_name", "NOZZA") or "NOZZA")
    short = pf.short_name(company, 28)
    client = pf.short_name(name, 38)
    cards = ""
    for _ in range(4):
        cards += (
            f'<div class="pf-cell bc">'
            f'<div class="bc-co">{pf.esc(short)}</div>'
            f'<div class="bc-ttl">3D-цех · напечатаем по вашим размерам</div>'
            f'<div class="bc-meta">Витрина: {pf.esc(public) if public else "не настроена"}</div>'
            f'<div class="bc-line">{my_line}</div>'
            f'{"<div class=bc-nm>Подготовлено для: " + pf.esc(client) + "</div>" if client else ""}'
            f'{mark}</div>'
        )
    css = """
  .pf-grid { display: grid; width: 178mm; margin: 0 auto;
             grid-template-columns: repeat(2, 85mm); grid-auto-rows: 55mm;
             gap: 8mm; }
  .bc { border: .25mm solid var(--pf-line); border-left: 2.6mm solid var(--pf-accent);
        border-radius: 2mm; padding: 4mm 5mm; display: flex; flex-direction: column;
        gap: 1mm; }
  .bc-co { font-size: 15pt; font-weight: 800; letter-spacing: -.01em; }
  .bc-ttl { font-size: 9pt; color: var(--pf-muted); }
  .bc-meta, .bc-line, .bc-nm { font-size: 8.5pt; color: var(--pf-muted); }
  .bc-nm { color: var(--pf-ink); font-weight: 600; }
  .bc-line b { color: var(--pf-accent); }
  .bc-qr { margin-top: auto; }
  .bc-qr svg { width: 20mm; height: 20mm; display: block; }
  .bc-noqr { margin-top: auto; max-width: 52mm; font-size: 7.5pt;
             color: var(--pf-warn); }
"""
    body = (
        f'<div class="pf-head" style="margin-bottom:4mm">'
        f'{pf.brand_line(company, "визитки · 85 × 55 мм, 4 на лист")}</div>'
        f'<div class="pf-grid">{cards}</div>'
        f'{pf.ruler()}'
        f'<div class="pf-note" style="margin-top:2mm">Резать по границе: 85 × 55 мм, '
        f'бумага 250–300 г/м².</div>'
    )
    return pf.page(f"Визитки {short}", body, css=css, margin="10mm")


# ---------------------------------------------------------------- гарантийный талон
def warranty_html(db: Database, order_id: str, customer_id: str = "") -> str:
    """Гарантийный талон заказа (идея 123) — A5 внутри A4.

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
    if public and code:
        try:
            qr = qr_svg(f"{public}/my.html?code={code.upper()}", level="M",
                        border=2, compact=True)
        except Exception:
            qr = ""
    months = num(db.setting("warranty_months", 0), 0)
    if months:
        term = f"Срок гарантии: {int(months)} мес. с даты выдачи"
    else:
        term = "Срок гарантии: ______ мес. с даты выдачи"
    issued = str(order.get("closed_at") or order.get("updated_at") or "")[:10]
    number = str(order.get("number") or order_id)
    product = pf.short_name(str(order.get("product") or "изделие"), 60)
    company = str(db.setting("company_name", "NOZZA") or "NOZZA")
    css = """
  .wt { width: 148mm; margin: 0 auto; border: .3mm solid var(--pf-line);
        border-radius: 2mm; padding: 6mm 8mm; }
  .wt h1 { font-size: 18pt; }
  .wt dl { display: grid; grid-template-columns: 42mm 1fr; gap: 2mm 4mm;
           margin: 5mm 0; font-size: 10.5pt; }
  .wt dt { color: var(--pf-muted); }
  .wt dd { margin: 0; font-weight: 600; }
  .wt-qr { display: flex; align-items: center; gap: 4mm; margin-top: 4mm; }
  .wt-qr svg { width: 24mm; height: 24mm; }
  .wt-sign { margin-top: 8mm; display: flex; gap: 8mm; font-size: 9pt;
             color: var(--pf-muted); }
  .wt-sign span { border-top: .25mm solid var(--pf-line); padding-top: 1.5mm;
                  min-width: 60mm; }
"""
    qr_block = (f'<div class="wt-qr">{qr}<span class="pf-note">QR: заказ, '
                f'фото и статус «Мой NOZZA»</span></div>') if qr else (
        '<div class="pf-note">QR не напечатан: в настройках не задан адрес витрины.</div>')
    body = (
        f'<div class="wt">'
        f'<div class="pf-head">{pf.brand_line(company, "гарантийный талон")}</div>'
        f'<h1 style="margin-top:4mm">Гарантийный талон</h1>'
        f'<dl><dt>Заказ</dt><dd>№{pf.esc(number)}</dd>'
        f'<dt>Изделие</dt><dd>{pf.esc(product)}</dd>'
        f'<dt>Дата выдачи</dt><dd>{pf.esc(issued) or "—"}</dd>'
        f'<dt>Условия</dt><dd>{pf.esc(term)}</dd></dl>'
        f'<div class="pf-note">Гарантия не покрывает механические повреждения, '
        f'нагрузку выше расчётной и нагрев выше рабочей температуры материала.</div>'
        f'{qr_block}'
        f'<div class="wt-sign"><span>Выдал</span><span>Получил</span></div>'
        f'</div>{pf.ruler()}'
    )
    return pf.page(f"Гарантийный талон — заказ №{number}", body, margin="14mm")


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
