"""Общий каркас печатных форм PrintFlow (обновление 18.0).

Печать — единственный слой системы, который уходит на бумагу: миллиметры
вместо пикселей, абсолютные цвета вместо темы, обязательная проверка
масштаба. До 18.0 каждый генератор собирал свой ``<style>`` и свою палитру
(7 серверных сборщиков), поэтому формы расползались: визитка, стикеры и
отчёт рисовали QR и текст по-разному, а подписи о масштабе не было вовсе.

Здесь живут:

* **печатные токены** — цвета и шрифт для бумаги (бренд NOZZA);
* :func:`page` — единый каркас листа (``@page``, токены, сброс, линейка);
* :func:`ruler` — контрольная линейка 100 мм: видно, врёт ли принтер;
* :func:`fit` — раскладка «сколько влезает» на лист (столбцы × строки);
* :data:`LABEL_SIZES`, :data:`SHEETS` — физические размеры;
* :func:`fit`/`grid_css` — раскладка листа;
* :func:`short_name`/:func:`join_meta` — обрезка подписей.

Правила модуля (нарушение = форма уедет при печати):

1. Геометрия — только в мм. Пиксели на бумаге зависят от DPI принтера.
2. Цвета — только из :data:`PRINT_TOKENS`. Тёмная тема панели на бумагу
   не попадает: форма всегда светлая, даже если печатают из тёмного окна.
3. Никаких внешних шрифтов и CDN: системный стек + локальный Inter, если
   он уже загружен страницей.
4. Любое значение из данных (имя клиента, изделие, компания) проходит
   через :func:`esc` — иначе одинарная кавычка в имени ломает вёрстку, а
   ``<`` в названии изделия превращается в тег в окне печати.
"""
from __future__ import annotations

import html
from typing import Any, Iterable

# --------------------------------------------------------------- токены
# Цвета бренда (docs/БРЕНД-NOZZA.md): текст #131a2b, градиент #4f46e5 → #7c3aed.
# Это не тема интерфейса, а бумага: значения жёсткие и одинаковые для всех форм.
PRINT_TOKENS: dict[str, str] = {
    "--pf-ink": "#131a2b",
    "--pf-muted": "#5d6b85",
    "--pf-line": "#d6dcea",
    "--pf-line-strong": "#9ca3af",
    "--pf-line-soft": "#eef1f8",
    "--pf-panel": "#ffffff",
    "--pf-panel-2": "#f6f8fc",
    "--pf-accent": "#4f46e5",
    "--pf-accent-2": "#7c3aed",
    "--pf-ok": "#0f7b4f",
    "--pf-warn": "#9a5b0b",
    "--pf-bad": "#a92a20",
    "--pf-font": ('"Inter","Segoe UI","SF Pro Text",system-ui,-apple-system,'
                  '"Helvetica Neue",Arial,sans-serif'),
    "--pf-mono": '"JetBrains Mono","SF Mono","Cascadia Mono",Consolas,monospace',
}

#: Акценты шаблонов берутся отсюда, а не из hex-значений в коде формы.
ACCENTS: dict[str, str] = {
    "accent": "var(--pf-accent)",
    "accent-2": "var(--pf-accent-2)",
    "ok": "var(--pf-ok)",
    "warn": "var(--pf-warn)",
    "bad": "var(--pf-bad)",
    "ink": "var(--pf-ink)",
}


def esc(value: Any) -> str:
    """Безопасный текст для печатной формы.

    Печатные страницы открываются через ``document.write`` в своём окне,
    поэтому имя клиента или название изделия со ``<`` — это не косметика,
    а исполняемый код. Экранируем всё, что пришло из данных.
    """
    return html.escape("" if value is None else str(value), quote=True)


def fit(area_w: float, area_h: float, cell_w: float, cell_h: float,
        gap: float = 4.0) -> tuple[int, int, int]:
    """Раскладка «сколько влезает»: столбцы, строки, всего.

    Считает по физическим мм, а не «на глаз»: ширина ячейки и зазор —
    реальные размеры, поэтому ответ совпадает с тем, что выйдет на бумаге.
    """
    if min(cell_w, cell_h) <= 0:
        raise ValueError("Размер ячейки должен быть больше нуля")
    cols = int((area_w + gap) // (cell_w + gap)) if cell_w else 0
    rows = int((area_h + gap) // (cell_h + gap)) if cell_h else 0
    cols = max(0, cols)
    rows = max(0, rows)
    return cols, rows, cols * rows


# --------------------------------------------------------------- листы
SHEETS: dict[str, dict[str, Any]] = {
    "A4": {"title": "A4, 210 × 297 мм", "w": 210.0, "h": 297.0, "margin": 8.0},
    "A5": {"title": "A5, 148 × 210 мм", "w": 148.0, "h": 210.0, "margin": 6.0},
    "A3": {"title": "A3, 297 × 420 мм", "w": 297.0, "h": 420.0, "margin": 10.0},
}

#: Физические размеры наклеек и бирок. Ключи не меняем: на них ссылаются
#: шаблоны и сохранённые настройки печати.
LABEL_SIZES: dict[str, dict[str, Any]] = {
    "50x25": {"title": "Стикер 50 × 25 мм", "w": 50.0, "h": 25.0},
    "58x40": {"title": "Наклейка катушки 58 × 40 мм", "w": 58.0, "h": 40.0},
    "70x37": {"title": "Бирка 70 × 37 мм", "w": 70.0, "h": 37.0},
    "88x44": {"title": "Крупный стикер 88 × 44 мм", "w": 88.0, "h": 44.0},
    "105x74": {"title": "Карточка A7, 105 × 74 мм", "w": 105.0, "h": 74.0},
}

DEFAULT_LABEL_SIZE = "50x25"


def sheet(key: str) -> dict[str, Any]:
    """Лист по ключу; неизвестный — ошибка, а не молчаливый A4."""
    size = SHEETS.get(str(key or "").strip() or "A4")
    if not size:
        raise ValueError(f"Неизвестный лист: {key}. Доступно: " + ", ".join(SHEETS))
    return size


def label_size(key: str) -> dict[str, Any]:
    """Размер наклейки по ключу; неизвестный ключ — ошибка, а не пустой лист."""
    size = LABEL_SIZES.get(str(key or "").strip() or DEFAULT_LABEL_SIZE)
    if not size:
        raise ValueError(f"Неизвестный размер: {key}. Доступно: "
                         + ", ".join(LABEL_SIZES))
    return size


def layout_for(size_key: str, sheet_key: str = "A4", gap: float = 4.0) -> dict[str, Any]:
    """Сколько наклеек выбранного размера влезает на лист (по физическим мм)."""
    cell = label_size(size_key)
    sheet_info = sheet(sheet_key)
    margin = float(sheet_info["margin"])
    cols, rows, total = fit(float(sheet_info["w"]) - margin * 2,
                            float(sheet_info["h"]) - margin * 2,
                            float(cell["w"]), float(cell["h"]), gap)
    return {"size": size_key, "sheet": sheet_key, "cols": cols, "rows": rows,
            "total": total, "gap": gap, "cell": {"w": cell["w"], "h": cell["h"]},
            "title": cell["title"]}


# --------------------------------------------------------------- каркас
_BASE_CSS = """
*, *::before, *::after { box-sizing: border-box; }
:root { color-scheme: light; }
html, body { margin: 0; padding: 0; background: #fff; }
body {
  font-family: var(--pf-font);
  color: var(--pf-ink);
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
h1, h2, h3 { margin: 0; letter-spacing: -0.01em; }
.pf-sheet { margin: 0 auto; }
.pf-head { display: flex; align-items: baseline; gap: 4mm; flex-wrap: wrap; }
.pf-title { font-size: 17pt; font-weight: 800; }
.pf-sub { font-size: 9pt; color: var(--pf-muted); }
.pf-brand { display: flex; align-items: center; gap: 2mm; font-weight: 800;
  letter-spacing: .1em; text-transform: uppercase; font-size: 8.5pt; }
.pf-brand i { display: block; width: 3mm; height: 3mm; border-radius: 50%;
  background: linear-gradient(135deg, var(--pf-accent), var(--pf-accent-2)); }
.pf-foot { margin-top: 5mm; padding-top: 2.5mm; border-top: .25mm solid var(--pf-line);
  display: flex; gap: 4mm; flex-wrap: wrap; font-size: 7.5pt; color: var(--pf-muted); }
.pf-scale { display: flex; align-items: center; gap: 3mm; font-size: 7.5pt;
  color: var(--pf-muted); margin-top: 4mm; }
.pf-scale-bar { display: inline-block; width: 100mm; height: 2.4mm;
  border-bottom: .3mm solid var(--pf-ink);
  background: repeating-linear-gradient(90deg, var(--pf-ink) 0 .3mm,
    transparent .3mm 10mm); }
.pf-note { font-size: 8pt; color: var(--pf-muted); }
@media print { .pf-noprint { display: none !important; } }
"""


def ruler(text: str = "Масштаб 100 %: линейка должна быть ровно 100 мм") -> str:
    """Контрольная линейка 100 мм.

    Единственный способ увидеть, что принтер «вписал в страницу» и ужал
    макет: на бумаге линейка либо 100 мм, либо нет. Бренд-гайд требует
    печатать в масштабе 100 %, но проверить это было нечем.
    """
    return (f'<div class="pf-scale"><span class="pf-scale-bar"></span>'
            f'<span>{esc(text)}</span></div>')


def page(title: str, body: str, *, css: str = "", size: str = "A4",
         margin: str = "10mm", sheet_width: str = "", extra_head: str = "",
         print_button: bool = False) -> str:
    """Собрать печатный лист: ``@page``, печатные токены и общий сброс.

    ``css`` — только правила конкретной формы. Цвета берутся из
    :data:`PRINT_TOKENS`, поэтому форма не зависит от темы браузера.
    """
    tokens = "\n".join(f"  {key}: {value};" for key, value in PRINT_TOKENS.items())
    width = f"  .pf-sheet {{ width: {sheet_width}; }}\n" if sheet_width else ""
    button = ('<button class="pf-noprint" type="button" onclick="window.print()" '
              'style="margin:6mm 0;padding:3mm 8mm;font:inherit;cursor:pointer;'
              'border:.3mm solid var(--pf-line);border-radius:2mm">Печать / PDF</button>'
              if print_button else "")
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
@page {{ size: {size}; margin: {margin}; }}
:root {{
{tokens}
}}
{_BASE_CSS}{width}{css}
</style>{extra_head}</head><body>
{button}
<div class="pf-sheet">
{body}
</div>
</body></html>"""


def brand_line(name: str, note: str = "") -> str:
    """Фирменная строка формы: точка-градиент, имя, необязательная подпись."""
    tail = f'<span class="pf-sub">{esc(note)}</span>' if note else ""
    return f'<div class="pf-brand"><i></i>{esc(name)}</div>{tail}'


def grid_css(cols: int, rows: int, cell_w: float, cell_h: float, gap: float,
             area_w: float) -> str:
    """CSS сетки листа: фиксированные ячейки в мм, по центру листа."""
    return (f".pf-grid {{ display: grid; width: {area_w}mm; margin: 0 auto;"
            f" grid-template-columns: repeat({max(1, cols)}, {cell_w}mm);"
            f" grid-auto-rows: {cell_h}mm; gap: {gap}mm; }}"
            f" .pf-cell {{ overflow: hidden; }}")


def short_name(name: str, limit: int = 42) -> str:
    """Имя для узкого места (визитка, бирка): обрезаем по границе слова.

    Раньше имя клиента печаталось целиком, и длинное («ООО …») выезжало за
    карточку 85 × 55 мм: у неё фиксированная высота, а текст не ограничен.
    """
    text = " ".join(str(name or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit + 1].rsplit(" ", 1)[0].rstrip(",;:-")
    return (cut or text[:limit]).rstrip() + "…"


def join_meta(parts: Iterable[str]) -> str:
    """Пропустить пустые части и склеить через точку-разделитель."""
    return " · ".join(part for part in parts if part)
