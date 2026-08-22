"""Фабрика контента PrintFlow 8.5: тексты и цифры из фактов цеха.

Идеи 20–22, 110–112, 107: авто-пост «дневник цеха», соцсетевой пакет с
реальными цифрами, фабрика карточек Авито, праздничные и сезонные карточки.
Всё считается из базовых таблиц (заказы, задания, стеллаж) — внешних
источников нет. Тексты — честные: если фактов мало, говорим про план,
а не выдумываем продажи.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .accounting import Accounting, num
from .config import now_iso
from .db import Database

RU_MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

# ---------------------------------------------------------------- календарь
# Фиксированные даты, которые реально двигают спрос в нишах NOZZA.
HOLIDAYS: list[dict[str, Any]] = [
    {"md": (1, 1), "name": "Новый год", "lead_days": 21,
     "hint": "подарки: QR-стойки, подарочные бирки, адресники",
     "title": "Новый год близко",
     "text": "Подарки, которые не надо искать: напечатаем адресник, бирку или QR-стойку с вашим текстом."},
    {"md": (2, 14), "name": "14 февраля", "lead_days": 10,
     "hint": "подарки: адресник питомцу, именная табличка",
     "title": "Подарок с характером",
     "text": "Именной адресник питомцу или табличка с вашим текстом — напечатаем к 14 февраля."},
    {"md": (3, 8), "name": "8 марта", "lead_days": 14,
     "hint": "подарки: органайзеры, именные таблички",
     "title": "К 8 марта",
     "text": "Органайзер, именная табличка или держатель с надписью — напечатаем к празднику."},
    {"md": (6, 1), "name": "1 июня", "lead_days": 14,
     "hint": "адресники и бирки на детскую одежду",
     "title": "К 1 июня",
     "text": "Адресники и бирки для детских вещей: имя, телефон, не стирать при 60°."},
    {"md": (9, 1), "name": "1 сентября", "lead_days": 21,
     "hint": "адресники, бирки для одежды, органайзеры для школы",
     "title": "Школьная пора",
     "text": "Адресник, бирки на одежду и органайзер — успеем к 1 сентября. Закажите заранее."},
    {"md": (10, 4), "name": "4 октября — День животных", "lead_days": 10,
     "hint": "подарки питомцам: адресники, держатели, таблички",
     "title": "День животных",
     "text": "Адресник, держатель поводка или табличка «здесь живёт …» — подарок питомцу."},
    {"md": (12, 1), "name": "Новогодний сезон", "lead_days": 14,
     "hint": "QR-стойки для офисов, подарочные бирки, упаковка",
     "title": "Новогодний сезон",
     "text": "QR-стойки для офиса, подарочные бирки и таблички — печатаем партиями на декабрь."},
]


def holiday_cards(now: date | None = None) -> dict[str, Any]:
    """Календарь праздников: ближайший, его карточка и список всех с датой."""
    now = now or date.today()
    out = []
    nearest = None
    nearest_in_days = None
    nearest_date: date | None = None
    for h in HOLIDAYS:
        m, d = h["md"]
        this_year = None
        try:
            this_year = date(now.year, m, d)
        except ValueError:
            continue
        if this_year < now:
            this_year = date(now.year + 1, m, d)
        days = (this_year - now).days
        out.append({**{k: v for k, v in h.items() if k != "md"},
                    "date": this_year.isoformat(), "days_left": days})
        if nearest_in_days is None or days < nearest_in_days:
            nearest, nearest_in_days, nearest_date = h, days, this_year
    return {"nearest": {**{k: v for k, v in nearest.items() if k != "md"},
                        "date": nearest_date.isoformat(),
                        "days_left": nearest_in_days} if nearest else None,
            "all": out}


# ---------------------------------------------------------------- недельный пост
def _top_product(db: Database, since: str) -> tuple[str, float] | None:
    rows = db.query(
        "SELECT COALESCE(product,'') p, COALESCE(SUM(price),0) v, COUNT(*) n"
        " FROM orders WHERE price>0 AND updated_at>=? GROUP BY p ORDER BY v DESC",
        (since,))
    for r in rows:
        if r["p"]:
            return r["p"], num(r["v"])
    return None


def week_post(db: Database, days: int = 7) -> dict[str, Any]:
    """«Дневник цеха»: пост из фактов за период. Идеи 22 и 112."""
    acc = Accounting(db)
    s = acc.summary(days)
    top = _top_product(db, (datetime.now() - timedelta(days=days)).isoformat())
    shelf = db.one("SELECT COALESCE(SUM(-qty),0) v FROM shelf_moves"
                   " WHERE kind IN ('sale','online') AND qty<0"
                   " AND at>=?", ((datetime.now() - timedelta(days=days)).isoformat(),))
    shelf_sold = num(shelf.get("v")) if shelf else 0.0
    lines = []
    if s["jobs_done"]:
        lines.append(f"напечатали {s['print_hours']:.0f} часов "
                     f"({s['jobs_done']} заданий, {s['grams']:.0f} г пластика)")
    if s["income"]:
        lines.append(f"заработали {s['income']:,.0f} ₽"
                     + (f", прибыль {s['profit']:,.0f} ₽" if s["profit"] else ""))
    if s["jobs_failed"]:
        lines.append(f"брак: {s['jobs_failed']} шт ({s['failure_rate']:.0f}%)")
    elif s["jobs_done"]:
        lines.append("без брака")
    if shelf_sold:
        lines.append(f"на полке продали {shelf_sold:.0f} шт")
    if top:
        lines.append(f"топ недели — «{top[0]}» ({top[1]:,.0f} ₽)")
    facts = ", ".join(lines) if lines else "пока тихо — готовимся к сезону"
    post = (f"Цех NOZZA за {days} дней: {facts}. "
            f"Дальше в очереди: {s['active_orders']} заказов. "
            f"Заказывайте — {db.setting('company_name', 'NOZZA')}.")
    return {"period_days": days, "text": post, "numbers": s,
            "top": top[0] if top else "", "shelf_sold": round(shelf_sold, 1)}


def social_pack(db: Database, days: int = 30) -> dict[str, Any]:
    """Цифры для графического пакета: шапка / квадрат / сторис. Идея 112."""
    post = week_post(db, days)
    s = post["numbers"]
    company = db.setting("company_name", "NOZZA")
    period = f"за {days} дней" if days <= 31 else "за квартал" if days <= 92 else "за год"
    return {
        "period_days": days, "period": period,
        "company": company,
        "header": f"{company} — там, где рождается форма",
        "square": (f"Цех {company}\n{s['jobs_done']} печатей · {s['grams']:.0f} г · "
                   f"{s['income']:,.0f} ₽\n{period}"),
        "story": (f"За {period} наш цех: {s['print_hours']:.0f} часов печати, "
                  f"{s['jobs_done']} заказов, брак {s['failure_rate']:.0f}%.\n"
                  f"{company} — локальное 3D-производство."),
        "numbers": s,
    }


# ---------------------------------------------------------------- Авито
def avito_card(db: Database, item_id: str) -> dict[str, Any]:
    """Тексты карточки Авито по позиции стеллажа. Идея 20."""
    shelf = db.one("SELECT * FROM shelf_items WHERE id=?", (item_id,))
    if not shelf:
        raise ValueError("Позиция не найдена")
    name = str(shelf.get("name") or "изделие из 3D-печати")
    price = num(shelf.get("price"))
    catalog = None
    if shelf.get("catalog_id"):
        catalog = db.one("SELECT * FROM catalog WHERE id=?", (shelf["catalog_id"],))
    material = (catalog or {}).get("material") or db.setting("default_material", "PLA")
    niche = (catalog or {}).get("niche_id") or ""
    niche_name = ""
    if niche:
        row = db.one("SELECT name FROM niches WHERE id=?", (niche,))
        niche_name = (row or {}).get("name") or ""
    desc_lines = [
        f"{name} — напечатано на 3D-принтере в цеху NOZZA.",
        f"Материал: {material}.",
        "Товар в наличии, самовывоз, отправка по городу и области.",
        "Можем напечатать в другом цвете или с вашим текстом — напишите в сообщения.",
    ]
    if num(shelf.get("qty")) <= 0:
        desc_lines.insert(1, "Сейчас закончилось — напечатаем под заказ за 2–3 дня.")
    keywords = " ".join(filter(None, [
        name, "3д печать", "3d печать", str(material).lower(),
        niche_name.lower(), "изделие на заказ", "NOZZA",
    ]))
    return {
        "item_id": item_id,
        "name": name,
        "title": (name + " — 3D печать, в наличии")[:70],
        "price": round(price, 0),
        "description": "\n".join(desc_lines),
        "keywords": keywords,
        "photo": shelf.get("photo") or "",
    }


# ---------------------------------------------------------------- сезонность
def seasonality(db: Database, months: int = 12) -> dict[str, Any]:
    """Индекс спроса по месяцам из истории заказов + якорные даты. Идея 11."""
    since = (datetime.now() - timedelta(days=365 * (months // 6 + 1))).isoformat()
    rows = db.query(
        "SELECT strftime('%Y-%m', updated_at) ym, COUNT(*) n, COALESCE(SUM(price),0) v"
        " FROM orders WHERE updated_at>=? AND price>0 GROUP BY ym ORDER BY ym",
        (since,))
    by_month: dict[int, dict[str, float]] = {}
    for r in rows:
        ym = str(r["ym"] or "")
        if len(ym) != 7:
            continue
        try:
            m = int(ym[5:7])
        except ValueError:
            continue
        slot = by_month.setdefault(m, {"orders": 0.0, "money": 0.0})
        slot["orders"] += num(r["n"])
        slot["money"] += num(r["v"])
    months_list = []
    total = sum(s["orders"] for s in by_month.values()) or 1.0
    for m in range(1, 13):
        slot = by_month.get(m, {"orders": 0.0, "money": 0.0})
        months_list.append({
            "month": m, "name": RU_MONTHS[m - 1],
            "orders": round(slot["orders"], 1),
            "money": round(slot["money"], 0),
            # индекс: 100 = средний месяц за историю
            "index": round(slot["orders"] / (total / 12) * 100) if total else 0,
        })
    top = max(months_list, key=lambda x: x["orders"]) if months_list else None
    return {
        "months": months_list,
        "peak": {"month": top["month"], "name": top["name"]} if top and top["orders"] else None,
        "holidays": holiday_cards()["all"][:8],
    }


# ---------------------------------------------------------------- отчёт
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


# ---------------------------------------------------------------- шапка полки
def shelf_header(db: Database, days: int = 7) -> dict[str, Any]:
    """Еженедельная шапка полки «Эта неделя: …» (идея 105)."""
    since = (datetime.now() - timedelta(days=max(1, int(days or 7)))).isoformat()
    rows = db.query(
        "SELECT i.name, COALESCE(SUM(-m.qty), 0) sold, COALESCE(SUM(m.price * -m.qty), 0) money"
        " FROM shelf_moves m JOIN shelf_items i ON i.id=m.item_id"
        " WHERE m.kind IN ('sale','online') AND m.at>=? GROUP BY i.id"
        " ORDER BY sold DESC", (since,))
    top = [{ "name": r["name"], "sold": int(num(r["sold"])) } for r in rows
           if num(r["sold"]) > 0][:3]
    sold_total = sum(t["sold"] for t in top)
    money = db.one("SELECT COALESCE(SUM(-qty * price),0) v FROM shelf_moves"
                   " WHERE kind IN ('sale','online') AND at>=?", (since,)) or {}
    new_items = db.query("SELECT name FROM shelf_items WHERE active=1"
                         " AND (created_at>=? OR updated_at>=?) ORDER BY updated_at DESC",
                         (since, since))
    if top:
        text = "Эта неделя: " + ", ".join(f"«{t['name']}» ×{t['sold']}" for t in top) + "."
        if sold_total:
            text += f" Всего с полки: {sold_total} шт."
    else:
        text = "Эта неделя: полка прогревается — загляните."
    return {
        "days": max(1, int(days or 7)),
        "text": text,
        "top": top,
        "sold_total": sold_total,
        "money": round(num(money.get("v")), 2),
        "new_items": [r["name"] for r in new_items][:5],
        "updated_at": now_iso(),
    }


# ---------------------------------------------------------------- промо-пак
def promo_pack(db: Database) -> dict[str, Any]:
    """Сезонный промо-пак (идея 107): что печатать сейчас под праздники и спрос.

    Один клик — набор карточек: ближайший праздник со «стартом продаж»,
    пиковые месяцы из истории и тексты. Всё из фактов, без домыслов.
    """
    today = date.today()
    holidays = holiday_cards(today)["all"]
    active = [h for h in holidays if h["days_left"] <= max(7, int(h["lead_days"]))]
    nearest = holiday_cards(today)["nearest"]
    season = seasonality(db, 12)
    out = {
        "generated_at": now_iso(),
        "company": db.setting("company_name", "NOZZA"),
        "nearest": nearest,
        "planning": active[:4],
        "peak_months": [m for m in season["months"] if m["orders"] > 0][:3],
        "cards": [],
    }
    for h in (active or [nearest] if nearest else [])[:3]:
        out["cards"].append({
            "name": h["name"], "date": h["date"], "days_left": h["days_left"],
            "title": h["title"], "text": h["text"], "hint": h["hint"],
        })
    if not out["cards"] and nearest:
        out["cards"].append({
            "name": nearest["name"], "date": nearest["date"],
            "days_left": nearest["days_left"], "title": nearest["title"],
            "text": nearest["text"], "hint": nearest["hint"],
        })
    return out


# ---------------------------------------------------------------- видео недели
def week_video(db: Database, days: int = 7) -> dict[str, Any]:
    """«Видео недели» (идея 87): монтаж из кейфреймов за период.

    Без внешних кодеков: отдаём последовательность кадров по заданиям —
    интерфейс проигрывает их как видео (кадр каждые N секунд).
    """
    from .config import PHOTO_DIR
    since = (datetime.now() - timedelta(days=max(1, int(days or 7)))).isoformat()
    jobs = db.query(
        "SELECT id, name, state, printer_id, started_at, finished_at FROM print_jobs"
        " WHERE COALESCE(finished_at, started_at, created_at) >=?"
        " ORDER BY COALESCE(finished_at, started_at, created_at) DESC", (since,))
    frames = []
    jobs_with_frames = 0
    for j in jobs:
        d = PHOTO_DIR / "keyframes" / str(j["id"])
        if not d.is_dir():
            continue
        files = sorted(f for f in d.iterdir() if f.suffix == ".jpg")
        if not files:
            continue
        jobs_with_frames += 1
        step = max(1, len(files) // 8)  # не больше 8 кадров на задание
        for f in files[::step]:
            frames.append({
                "job_id": j["id"], "job": j.get("name") or "",
                "printer": j.get("printer_id") or "",
                "file": f.name,
                "url": f"/api/job/keyframe.jpg?id={j['id']}&name={f.name}",
            })
    return {
        "days": max(1, int(days or 7)),
        "frames": frames,
        "jobs": len(jobs),
        "jobs_with_frames": jobs_with_frames,
        "generated_at": now_iso(),
    }


# ---------------------------------------------------------------- карта печатей
def print_map(db: Database) -> dict[str, Any]:
    """«Карта печатей» (идея 91): точка = завершённая печать, сетка года 52×7."""
    from datetime import date as _d, timedelta as _td
    today = _d.today()
    year = today.year
    # сетка GitHub-стиля: недели × день недели (пн=0 … вс=6)
    grid: dict[tuple[int, int], int] = {}
    total = 0
    # сетка GitHub-стиля, привязана к первому понедельнику года
    first_monday = _d(year, 1, 1) - _td(days=_d(year, 1, 1).weekday())
    rows = db.query(
        "SELECT finished_at FROM print_jobs WHERE state='done' AND finished_at<>''")
    for r in rows:
        stamp = str(r["finished_at"] or "")[:10]
        if not stamp:
            continue
        try:
            d = _d.fromisoformat(stamp)
        except ValueError:
            continue
        total += 1
        if d.year != year:
            continue
        monday = d - _td(days=d.weekday())
        week = (monday - first_monday).days // 7
        if 0 <= week < 53:
            cell = grid.get((week, d.weekday()), 0)
            grid[(week, d.weekday())] = cell + 1
    max_cell = max(grid.values()) if grid else 0
    cells = []
    for (week, wd), count in sorted(grid.items()):
        level = 0
        if count:
            level = 1 + int((count / max(max_cell, 1)) * 3)
        cells.append({"week": week, "weekday": wd, "count": count, "level": level})
    return {
        "year": year,
        "total": total,
        "max_day": max_cell,
        "cells": cells,
        "generated_at": now_iso(),
    }


# ---------------------------------------------------------------- цифровая нить
def order_thread(db: Database, order_id: str) -> dict[str, Any]:
    """Цифровая нить изделия (идея 12): заказ → печать → полка → продажа → отзыв."""
    order = db.one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not order:
        raise ValueError("Заказ не найден")
    jobs = db.query("SELECT * FROM print_jobs WHERE order_id=?"
                    " ORDER BY created_at", (order_id,))
    product = str(order.get("product") or "")
    shelf_item = None
    if product:
        shelf_item = db.one("SELECT * FROM shelf_items WHERE active=1"
                            " AND lower(name)=lower(?) LIMIT 1", (product,))
    shelf_sales = []
    if shelf_item:
        shelf_sales = db.query(
            "SELECT at, qty, price, kind FROM shelf_moves WHERE item_id=?"
            " AND kind IN ('sale','online') AND at>=? ORDER BY at DESC LIMIT 10",
            (shelf_item["id"], str(order.get("created_at") or "")[:10]))
    income = db.query("SELECT at, amount, category FROM transactions"
                      " WHERE kind='income' AND order_id=? ORDER BY at", (order_id,))
    feedback = db.one(
        "SELECT rating, feedback_text, feedback_received_at, publish_permission"
        " FROM customer_feedback WHERE order_id=? ORDER BY created_at DESC", (order_id,))
    return {
        "order": {
            "id": order["id"], "number": order.get("number"),
            "product": product, "customer_name": order.get("customer_name"),
            "status": order.get("status"), "price": num(order.get("price")),
            "created_at": order.get("created_at") or "",
            "gift": bool(order.get("gift")),
        },
        "print": [{
            "id": j["id"], "name": j.get("name") or "", "state": j.get("state"),
            "grams": num(j.get("grams")), "duration_min": num(j.get("duration_min")),
            "started_at": j.get("started_at") or "",
            "finished_at": j.get("finished_at") or "",
        } for j in jobs],
        "shelf": {
            "item_id": shelf_item["id"] if shelf_item else "",
            "name": shelf_item.get("name") if shelf_item else "",
            "qty": shelf_item.get("qty") if shelf_item else None,
            "recent_sales": [{"at": r["at"], "qty": -num(r["qty"]),
                              "price": num(r.get("price"))} for r in shelf_sales],
        },
        "income": [{"at": r["at"], "amount": num(r["amount"])} for r in income],
        "feedback": ({
            "rating": feedback.get("rating") or 0,
            "text": feedback.get("feedback_text") or "",
            "received_at": feedback.get("feedback_received_at") or "",
            "publish_permission": feedback.get("publish_permission") or "",
        } if feedback else None),
        "generated_at": now_iso(),
    }


# ---------------------------------------------------------------- цеховой отчёт
def workshop_report_html(db: Database, days: int = 30) -> str:
    """Брендированный печатный отчёт (идея 21): A4-HTML, печать в PDF из браузера."""
    r = workshop_report(db, days)
    rows = "".join(
        f"<tr><td>{t['product']}</td><td>{t['qty']}</td><td>{t['revenue']:,.0f} ₽</td></tr>"
        for t in r["top"][:8]) or "<tr><td colspan='3'>—</td></tr>"
    top_line = r["top"][0]["product"] if r["top"] else ""
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Цеховой отчёт — {r['company']}</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  body {{ font-family: Arial, sans-serif; color: #131a2b; }}
  .sheet {{ width: 186mm; margin: 0 auto; }}
  .brand {{ border-bottom: 3px solid #f97316; padding-bottom: 4mm; margin-bottom: 6mm; }}
  h1 {{ font-size: 20pt; margin: 0; }}
  .sub {{ color: #6b7280; font-size: 10pt; }}
  h2 {{ font-size: 12pt; margin: 6mm 0 2mm; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td, th {{ border: 1px solid #e5e7eb; padding: 2mm 3mm; font-size: 10pt; }}
  th {{ background: #f3f4f6; text-align: left; }}
  .kpi {{ display: flex; gap: 3mm; margin: 4mm 0; }}
  .kpi div {{ flex: 1; border: 1px solid #e5e7eb; border-radius: 2mm; padding: 3mm; }}
  .kpi b {{ display: block; font-size: 13pt; }}
  .kpi span {{ color: #6b7280; font-size: 8.5pt; }}
  .foot {{ margin-top: 8mm; color: #6b7280; font-size: 8.5pt;
           border-top: 1px solid #e5e7eb; padding-top: 3mm; }}
</style></head><body><div class="sheet">
<div class="brand"><h1>{r['company']} — цеховой отчёт</h1>
<div class="sub">за {r['period_days']} дней · сформирован {str(r['generated_at'])[:10]}</div></div>
<div class="kpi">
  <div><b>{r['income']:,.0f} ₽</b><span>выручка</span></div>
  <div><b>{r['profit']:,.0f} ₽</b><span>прибыль (маржа {r['margin']:.0f}%)</span></div>
  <div><b>{r['print_hours']:.0f} ч</b><span>время печати</span></div>
  <div><b>{r['grams']:,.0f} г</b><span>пластик</span></div>
  <div><b>{r['jobs_done']}</b><span>заданий, брак {r['failure_rate']:.0f}%</span></div>
  <div><b>{r['customers_new']}</b><span>новых клиентов</span></div>
</div>
<h2>Топ изделий {f"({top_line})" if top_line else ""}</h2>
<table><tr><th>Изделие</th><th>Шт.</th><th>Выручка</th></tr>{rows}</table>
<div class="foot">{r['company']} · локальное 3D-производство ·
прибыль на час печати: {r['profit_per_print_hour']:,.0f} ₽/ч ·
энергия: {r['energy_kwh']:.1f} кВт·ч · себестоимость брака: {r['defects_cost']:,.0f} ₽</div>
</div></body></html>"""


# ---------------------------------------------------------------- стикеры
STICKER_TEMPLATES = {
    "guarantee": {
        "title": "Гарантия цеха",
        "text": "Напечатали не то? Показывайте заказ — поменяем или перепечатаем.",
        "accent": "#0ea5e9",
    },
    "pla": {
        "title": "100% PLA",
        "text": "Печать на биоразлагаемом PLA-пластике. Сертификат — в паспорте изделия.",
        "accent": "#22c55e",
    },
    "workshop": {
        "title": "Цех NOZZA",
        "text": "Сделано локально, за 1–3 дня. QR на упаковке — статус вашего заказа.",
        "accent": "#f97316",
    },
}


def stickers(kind: str = "all") -> str:
    """Стикеры в упаковку (идея 113): печатный лист 4 стикера 50×25 мм."""
    kinds = list(STICKER_TEMPLATES) if (kind or "all") in ("", "all") else [kind]
    cells = ""
    for k in kinds:
        if k not in STICKER_TEMPLATES:
            continue
        t = STICKER_TEMPLATES[k]
        cells += f"""<div class="st" style="border-top: 2mm solid {t['accent']}">
  <b>{t['title']}</b><br><span>{t['text']}</span></div>"""
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Стикеры NOZZA</title>
<style>
  @page {{ size: A4; margin: 8mm; }}
  body {{ font-family: Arial, sans-serif; color: #131a2b; }}
  .grid {{ width: 194mm; margin: 0 auto; display: grid;
           grid-template-columns: 1fr 1fr; gap: 6mm; }}
  .st {{ width: 88mm; height: 44mm; border: 0.5mm dashed #9ca3af;
         border-radius: 2mm; padding: 4mm 5mm; box-sizing: border-box; }}
  .st b {{ font-size: 13pt; }}
  .st span {{ font-size: 9pt; color: #374151; }}
</style></head><body><div class="grid">{cells}</div></body></html>"""


# ---------------------------------------------------------------- визитка 2.0
def business_card_html(db: Database, customer_id: str = "") -> str:
    """Визитка 2.0 (идея 115): QR в «Мой NOZZA» / витрину, A4 — 4 шт. на лист."""
    from .qrgen import matrix
    code = ""
    name = ""
    if customer_id:
        c = db.one("SELECT * FROM customers WHERE id=?", (customer_id,)) or {}
        code = str(c.get("portal_code") or "")
        name = str(c.get("name") or "")
    public = str(db.setting("public_url", "") or "")
    if code:
        qr_url = f"{public}/my.html?code={code.upper()}"
        my_line = f'Мой NOZZA: <b>{code.upper()}</b> — покажите код, покажем заказ'
    else:
        qr_url = f"{public}/track.html"
        my_line = "Спросите код «Мой NOZZA» — покажем ваш заказ онлайн"
    # QR рисуем ASCII-сеткой из собственного генератора (qrgen)
    qr_lines: list[str] = []
    if qr_url:
        try:
            m = matrix(qr_url, "M")
            qr_lines = ["".join("██" if x else "  " for x in row) for row in m]
        except Exception:
            qr_lines = []
    qr = ("".join(f"<div>{line}</div>" for line in qr_lines)
          if qr_lines else "<div class='noqr'>QR появится, когда укажете public_url</div>")
    company = str(db.setting("company_name", "NOZZA") or "NOZZA")
    cards = ""
    for _ in range(4):
        cards += f"""<div class="card">
  <div class="co">{company}</div>
  <div class="ttl">3D-цех · напечатаем к завтрашнему дню</div>
  <div class="meta">Витрина: {public or "укажите public_url в настройках"}</div>
  <div class="pc">{my_line}</div>
  {'<div class="nm">Подготовлено для: ' + name + '</div>' if name else ''}
  <div class="qr">{qr}</div>
</div>"""
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Визитки {company}</title>
<style>
  @page {{ size: A4; margin: 10mm; }}
  body {{ font-family: Arial, sans-serif; color: #131a2b; }}
  .grid {{ width: 190mm; margin: 0 auto; display: grid;
           grid-template-columns: 1fr 1fr; gap: 8mm; }}
  .card {{ width: 85mm; height: 55mm; border: 0.5mm solid #d1d5db;
           border-radius: 2mm; padding: 5mm; box-sizing: border-box;
           border-left: 3mm solid #f97316; }}
  .co {{ font-size: 15pt; font-weight: bold; }}
  .ttl {{ font-size: 9pt; color: #374151; margin: 1mm 0 3mm; }}
  .meta, .pc, .nm {{ font-size: 8.5pt; color: #4b5563; }}
  .qr {{ font-size: 3px; line-height: 3px; letter-spacing: 0; margin-top: 2mm;
         font-family: monospace; }}
  .noqr {{ color: #9ca3af; font-size: 8pt; margin-top: 4mm; }}
</style></head><body><div class="grid">{cards}</div></body></html>"""
