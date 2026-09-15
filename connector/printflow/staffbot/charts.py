"""Отчёты картинкой: PNG-график дня без внешних библиотек.

«Отчёт сегодня картинкой» (идея Б4 из ИДЕИ-БОТ-18.2, ускоренная по просьбе
владельца): выручка дня столбиками по часам, короткие подписи прямо в кадре,
подробности — в подписи к фото, где шрифт нормальный. Отчётность с мобильной
кассы входит в тот же кадр: часы с деньгами «Кассы» (нал/карта/СБП) получают
фиолетовую секцию сверху столбика, итоги кассы — строкой в подписи.

Техника та же, что в ``qrgen.png_bytes``: PNG собирается вручную через
zlib + struct, поэтому работает везде, где есть стандартная библиотека.
Цифры и заглавные буквы рисуются встроенным битмап-шрифтом 5×7 — только
алфавит подписей (СЕГОДНЯ, ВЫРУЧКА, ЧЕКОВ, КАССА…), не полный юникод.
"""
from __future__ import annotations

import struct
import zlib

from ..accounting import num
from ..config import now_iso

__all__ = ["daily_report", "day_chart_png"]

# Палитра бренда (docs/img/*.svg): график выглядит как продолжение панели.
NAVY = (11, 16, 32)
INDIGO = (79, 70, 229)
VIOLET = (124, 58, 237)
LIGHT = (248, 250, 252)
WHITE = (255, 255, 255)
MUTED = (100, 116, 139)
GRID = (226, 232, 240)
INK = (15, 23, 42)

# Часы смены на оси: с 8 утра до 22 вечера — вне окна продажи редки,
# а пустые часы только растягивают картинку.
HOURS = tuple(range(8, 23))

W, H = 720, 540

# --- Битмап-шрифт 5×7 (ширина может отличаться — см. Ы) --------------------
# Каждая буква — 7 строк по 5–7 пикселей. Только то, что встречается
# в подписях графика; остальной текст живёт в подписи Telegram.
_FONT: dict[str, tuple[str, ...]] = {
    " ": (".....",) * 7,
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "..##.", ".#...", "#....", "#####"),
    "3": (".###.", "#...#", "....#", "..##.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": (".###.", "#....", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "....#", ".###."),
    ":": (".....", "..#..", "..#..", ".....", "..#..", "..#..", "....."),
    ".": (".....",) * 6 + ("..#..",),
    ",": (".....",) * 5 + ("..#..", ".#..."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    "·": (".....", ".....", "..#..", "..#..", ".....", ".....", "....."),
    "₽": (".###.", "#...#", "#...#", ".###.", "#....", "#####", "#...."),
    "А": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "Б": ("#####", "#....", "#....", "####.", "#...#", "#...#", "####."),
    "В": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "Г": ("#####", "#....", "#....", "#....", "#....", "#....", "#...."),
    "Д": (".####", "#...#", "#...#", "#...#", "#...#", "#####", "#...#"),
    "Е": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "И": ("#...#", "##..#", "##..#", "#.#.#", "#..##", "#..##", "#...#"),
    "К": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "М": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "Н": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "О": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "П": ("#####", "#...#", "#...#", "#...#", "#...#", "#...#", "#...#"),
    "Р": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "С": (".####", "#....", "#....", "#....", "#....", "#....", ".####"),
    "Т": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "У": ("#...#", "#...#", ".###.", "..#..", "..#..", ".#...", "##..."),
    "Х": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Ч": ("#...#", "#...#", "#...#", ".####", "....#", "....#", "....#"),
    "Ы": ("#.#..#", "#.#.##", "#.#..#", "#.##.#", "#.#..#", "#.#..#", "#.#..#"),
    "Я": (".####", "#...#", "#...#", ".####", "..#.#", ".#..#", "#...#"),
}


def _glyph(ch: str) -> tuple[str, ...]:
    return _FONT.get(ch) or _FONT.get(ch.upper()) or _FONT[" "]


def text_width(s: str, scale: int = 1) -> int:
    return sum(len(_glyph(c)[0]) + 1 for c in s) * scale


class Canvas:
    """RGB-холст: пиксели считаются в памяти, затем сжимаются в PNG."""

    def __init__(self, width: int = W, height: int = H, bg: tuple = LIGHT):
        self.w, self.h = width, height
        self._px = [bytearray(bytes(bg) * width) for _ in range(height)]

    def rect(self, x: int, y: int, w: int, h: int, color: tuple) -> None:
        if w <= 0 or h <= 0:
            return
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        row_color = bytes(color) * (x1 - x0)
        for yy in range(y0, y1):
            row = self._px[yy]
            row[x0 * 3:x1 * 3] = row_color

    def hline(self, x0: int, x1: int, y: int, color: tuple) -> None:
        self.rect(x0, y, x1 - x0, 1, color)

    def text(self, x: int, y: int, s: str, color: tuple, scale: int = 1) -> None:
        cx = x
        for ch in s:
            glyph = _glyph(ch)
            for gy, line in enumerate(glyph):
                for gx, cell in enumerate(line):
                    if cell != "#":
                        continue
                    self.rect(cx + gx * scale, y + gy * scale, scale, scale, color)
            cx += (len(glyph[0]) + 1) * scale

    def png_bytes(self) -> bytes:
        """PNG без Pillow: та же схема, что qrgen.png_bytes, но цветной."""
        raw = bytearray()
        for row in self._px:
            raw.append(0)              # фильтр строки PNG: none
            raw.extend(row)
        return _png_chunks(self.w, self.h, bytes(raw))


def _png_chunks(w: int, h: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8 бит, truecolor
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _rub(value: float) -> str:
    return f"{round(num(value)):,}".replace(",", " ") + " ₽"


def _fmt_date(date: str) -> str:
    return f"{date[8:10]}.{date[5:7]}.{date[:4]}"


def _day_rows(db, date: str) -> dict:
    """Агрегаты дня: доходы по часам (всего/касса), расход, продажи кассы."""
    rows = db.query(
        "SELECT substr(at,12,2) AS h, amount, note FROM transactions"
        " WHERE kind='income' AND substr(at,1,10)=?", (date,))
    by_hour: dict[int, dict] = {h: {"total": 0.0, "kassa": 0.0} for h in HOURS}
    checks = 0
    for row in rows:
        hour = int(num("0" + str(row.get("h") or "0")[:2]) or 0)
        amount = num(row.get("amount"))
        if hour not in by_hour:
            continue
        by_hour[hour]["total"] += amount
        checks += 1
        if str(row.get("note") or "").startswith("Касса"):
            by_hour[hour]["kassa"] += amount
    expense = num((db.one(
        "SELECT SUM(amount) AS s FROM transactions"
        " WHERE kind='expense' AND substr(at,1,10)=?", (date,)) or {}).get("s"))
    # Касса (мобильная): нал/карта сразу, СБП — только подтверждённые.
    kassa = {"cash": 0, "card": 0, "sbp": 0,
             "cash_sum": 0.0, "card_sum": 0.0, "sbp_sum": 0.0}
    for row in db.query(
            "SELECT method, COUNT(*) AS n, SUM(amount) AS s FROM cashier_sales"
            " WHERE substr(created_at,1,10)=?"
            " AND (method<>'sbp' OR confirmed_at<>'') GROUP BY method", (date,)):
        method = str(row.get("method") or "cash")
        if method not in kassa:
            continue
        kassa[method] = int(num(row.get("n")))
        kassa[f"{method}_sum"] = num(row.get("s"))
    income = sum(v["total"] for v in by_hour.values())
    return {"by_hour": by_hour, "checks": checks, "expense": expense,
            "income": income, "kassa": kassa}


def day_chart_png(day: dict, date: str) -> bytes:
    """Кадр 720×540: шапка с выручкой, столбики по часам, строка итогов."""
    c = Canvas()
    c.rect(0, 0, W, 100, NAVY)
    c.text(24, 18, "ВЫРУЧКА", (165, 180, 252), 2)
    c.text(24, 42, _rub(day["income"]), WHITE, 4)
    date_label = f"СЕГОДНЯ · {_fmt_date(date)}"
    c.text(W - 24 - text_width(date_label, 2), 18, date_label, (148, 163, 184), 2)

    x0, x1, top, base = 56, W - 20, 132, 468
    peak = max((v["total"] for v in day["by_hour"].values()), default=0.0)
    scale_max = _nice_ceiling(peak)

    for step in range(5):
        y = base - (base - top) * step // 4
        c.hline(x0, x1, y, GRID)
        if step:
            label = f"{round(scale_max * step / 4):,}".replace(",", " ")
            c.text(x0 - 12 - text_width(label), y - 4, label, MUTED)

    slot = (x1 - x0) / len(HOURS)
    bar_w = max(10, int(slot * 0.56))
    for i, hour in enumerate(HOURS):
        cell = day["by_hour"][hour]
        cx = int(x0 + slot * i + (slot - bar_w) / 2)
        total_h = int((base - top) * cell["total"] / scale_max) if scale_max else 0
        if not total_h:
            c.rect(cx, base - 3, bar_w, 3, GRID)      # час был, продаж нет
        else:
            kassa_h = int((base - top) * cell["kassa"] / scale_max) if scale_max else 0
            plain_h = max(0, total_h - kassa_h)
            if plain_h:
                c.rect(cx, base - plain_h, bar_w, plain_h, INDIGO)
            if kassa_h:
                c.rect(cx, base - total_h, bar_w, kassa_h, VIOLET)
        label = str(hour)
        c.text(int(x0 + slot * i + (slot - text_width(label)) / 2),
               base + 10, label, MUTED)

    c.hline(0, W, 502, GRID)
    y = 516
    checks = f"ЧЕКОВ {day['checks']}"
    c.text(24, y, checks, INK, 2)
    avg = day["income"] / day["checks"] if day["checks"] else 0.0
    middle = f"СР. ЧЕК {_rub(avg)}"
    c.text((W - text_width(middle, 2)) // 2, y, middle, INK, 2)
    kassa_n = day["kassa"]["cash"] + day["kassa"]["card"] + day["kassa"]["sbp"]
    right = f"КАССА {kassa_n}"
    c.rect(W - 24 - text_width(right, 2) - 22, y + 1, 14, 14, VIOLET)
    c.text(W - 24 - text_width(right, 2), y, right, INK, 2)
    return c.png_bytes()


def _nice_ceiling(peak: float) -> float:
    """Максимум оси: круглое число не меньше пика (0 → 1, пустой день)."""
    if peak <= 0:
        return 1.0
    step = 10 ** max(0, len(str(int(peak))) - 1)
    top = (int(peak) // step + 1) * step
    while top < peak:
        top += step
    return float(top)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    d = n % 10
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def day_caption(day: dict, date: str) -> str:
    kassa = day["kassa"]
    kassa_n = kassa["cash"] + kassa["card"] + kassa["sbp"]
    kassa_sum = kassa["cash_sum"] + kassa["card_sum"] + kassa["sbp_sum"]
    lines = [f"📈 Сегодня, {_fmt_date(date)}: выручка {_rub(day['income'])}"
             f" · чеков {day['checks']}"
             + (f" · средний чек {_rub(day['income'] / day['checks'])}"
                if day["checks"] else ""),
             f"🟣 Касса (мобильная): {kassa_n} "
             + _plural(kassa_n, "продажа", "продажи", "продаж")
             + (f" на {_rub(kassa_sum)}" if kassa_n else "")
             + " — нал {} · СБП {}".format(kassa["cash"], kassa["sbp"])]
    if kassa["card"]:
        lines[-1] += f" · карта {kassa['card']}"
    lines.append(f"🧾 Расход дня {_rub(day['expense'])} · чистыми"
                 f" {_rub(day['income'] - day['expense'])}")
    lines.append("\n«график» — прислать ещё раз. Фиолетовые секции — деньги,"
                 " прошедшие через мобильную кассу.")
    return "\n".join(lines)


def daily_report(db, date: str | None = None) -> tuple[bytes, str]:
    """Отчёт дня для Telegram: (PNG-картинка, подпись к фото)."""
    date = date or now_iso()[:10]
    day = _day_rows(db, date)
    return day_chart_png(day, date), day_caption(day, date)
