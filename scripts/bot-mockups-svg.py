#!/usr/bin/env python3
"""Генератор SVG-макетов идей бота сотрудников (18.2).

Зачем: идеи улучшений обсуждаются с владельцем по картинкам, а не по тексту.
Рисуем макеты сами (SVG, как баннеры и схемы в docs/img): русский текст
получается точным, вёрстка — предсказуемой, внешних зависимостей нет.

Что делает:
  * собирает «карточки-чаты» в стиле Telegram (светлая тема):
    сообщения, inline-кнопки, нижняя reply-панель, фото-вложения
    (график выручки и товарная карточка рисуются тут же);
  * пишет по одному файлу на идею в docs/img/bot-idea-*.svg;
  * собирает постер docs/img/bot-ideas-182.svg (сетка 2×3) для просмотра
    одним взглядом и вставки в документацию.

Запуск из корня репозитория:: python3 scripts/bot-mockups-svg.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "img"

FONT = "Segoe UI, Roboto, 'Helvetica Neue', Arial, sans-serif"

# Палитра: чат — Telegram (светлый), рамки и подписи — бренд PrintFlow/NOZZA.
INK = "#17212B"          # текст сообщений
MUT = "#64748B"          # вторичный текст
FAINT = "#94A3B8"        # подписи-подсказки
CHAT_BG = "#E9F0F5"      # фон чата
HEADER_BG = "#FFFFFF"
BORDER = "#DCE4EB"
IN_BUBBLE = "#FFFFFF"    # входящее сообщение
OUT_BUBBLE = "#E2F8D2"   # исходящее (сотрудник)
TIME_C = "#93A1B0"
BTN_BG = "#FFFFFF"
BTN_BORDER = "#DAE3EA"
BTN_TEXT = "#1C93E3"
NAVY = "#0B1020"         # бренд: тёмный
INDIGO = "#4F46E5"       # бренд: индиго
VIOLET = "#7C3AED"       # бренд: фиолет


def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def rrect(x: float, y: float, w: float, h: float, r: float, fill: str,
          stroke: str = "none", sw: float = 1) -> str:
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"'
            f' rx="{r}" fill="{fill}" stroke="{stroke}"'
            + (f' stroke-width="{sw}"' if stroke != "none" else "") + "/>")


def text(x: float, y: float, s: str, size: float = 14, fill: str = INK,
         bold: bool = False, anchor: str = "start") -> str:
    weight = "700" if bold else "400"
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}"'
            f' font-size="{size}" font-weight="{weight}" fill="{fill}"'
            f' text-anchor="{anchor}">{esc(s)}</text>')


def char_w(size: float) -> float:
    """Средняя ширина символа: кириллица в Segoe/Roboto шире латиницы."""
    return size * 0.60


class Bubble:
    """Одно сообщение: строки текста (или фото), кнопки, время."""

    def __init__(self, who: str, lines: list, width: float,
                 buttons: list | None = None, photo: dict | None = None,
                 time: str = "12:04"):
        self.who = who            # "bot" — слева, "me" — справа
        self.lines = lines        # [(текст, bold, color), ...]
        self.width = width
        self.buttons = buttons or []  # [[(label, kind), ...], ...]
        self.photo = photo        # {"kind": "chart"|"product", "caption": [...]}
        self.time = time

    # ------------------------------------------------------------- размер
    def text_height(self) -> float:
        if not self.lines:
            return 0.0
        return len(self.lines) * 19 + 10

    def photo_height(self) -> float:
        return 172 + (len(self.photo["caption"]) * 18 + 6
                      if self.photo else 0)

    def buttons_height(self) -> float:
        return len(self.buttons) * (38 + 6) if self.buttons else 0

    def height(self) -> float:
        body = self.photo_height() if self.photo else self.text_height()
        return body + 14 + self.buttons_height()

    # ------------------------------------------------------------ отрисовка
    def render(self, x: float, y: float) -> tuple[str, float]:
        h = self.height()
        w = self.width
        out = []
        if self.who == "me":
            out.append(rrect(x, y, w, h, 14, OUT_BUBBLE))
        else:
            out.append(rrect(x, y, w, h, 14, IN_BUBBLE, stroke=BORDER, sw=0.8))
        cy = y + 10
        if self.photo:
            out.append(self._photo(x + 12, cy, w - 24))
            cy += 172 + 6
            for line in self.photo.get("caption", []):
                s, bold = (line if isinstance(line, tuple) else (line, False))
                out.append(text(x + 16, cy + 13, s, 13.5, INK, bold))
                cy += 18
            cy += 4
        else:
            for line in self.lines:
                s, bold, color = (line + (INK,))[:3] if isinstance(line, tuple) \
                    else (line, False, INK)
                out.append(text(x + 14, cy + 14, s, 13.5, color, bold))
                cy += 19
        # время — в правом нижнем углу пузыря
        if not self.photo:
            out.append(text(x + w - 10, y + h - 8, self.time, 10.5, TIME_C,
                            anchor="end"))
        # inline-клавиатура тем же блоком, что и сообщение
        by = y + h - self.buttons_height() + 2
        for row in self.buttons:
            weights = [max(len(label), 6) * char_w(13.5) + 18
                       for label, _ in row]
            total = sum(weights) + 6 * (len(row) - 1)
            scale = (w - 4) / total if total > w - 4 else 1.0
            bx = x + 2
            for (label, kind), weight in zip(row, weights):
                bw = weight * scale
                out.append(rrect(bx, by, bw, 36, 8, BTN_BG, stroke=BTN_BORDER,
                                 sw=0.9))
                label_text = label + ("  ↗" if kind == "url" else "")
                out.append(text(bx + bw / 2, by + 23, label_text, 13,
                                BTN_TEXT, bold=True, anchor="middle"))
                bx += bw + 6
            by += 44
        return "\n".join(out), y + h + 12

    # ---------------------------------------------------------------- фото
    def _photo(self, x: float, y: float, w: float) -> str:
        out = [rrect(x, y, w, 172, 10, "#F8FAFC", stroke=BORDER, sw=0.8)]
        if self.photo["kind"] == "chart":
            out.append(self._chart(x, y, w))
        else:
            out.append(self._product(x, y, w))
        return "\n".join(out)

    def _chart(self, x: float, y: float, w: float) -> str:
        """Выручка по дням: тот PNG, который бот приложит к «итогам недели»."""
        out = [text(x + 14, y + 24, "Выручка по дням · ₽", 12, NAVY, True),
               text(x + w - 14, y + 24, "неделя 15–21", 10.5, MUT,
                    anchor="end")]
        days = [("Пн", 6.2), ("Вт", 4.8), ("Ср", 7.1), ("Чт", 5.4),
                ("Пт", 8.9), ("Сб", 4.1), ("Вс", 6.6)]
        base = y + 146
        area_w = w - 28
        bar_w = min(34, area_w / len(days) - 8)
        step = area_w / len(days)
        top = max(v for _, v in days)
        out.append(f'<line x1="{x + 12}" y1="{base}" x2="{x + w - 12}"'
                   f' y2="{base}" stroke="#E2E8F0" stroke-width="1"/>')
        for i, (day, v) in enumerate(days):
            bh = v / top * 88
            bx = x + 14 + i * step + (step - bar_w) / 2
            color = VIOLET if v == top else INDIGO
            op = "" if v == top else ' opacity="0.82"'
            out.append(rrect(bx, base - bh, bar_w, bh, 4, color)[:-2]
                       + op + "/>")
            out.append(text(bx + bar_w / 2, base - bh - 6,
                            f"{str(v).replace('.', ',')}", 9.5, "#475569",
                            anchor="middle"))
            out.append(text(bx + bar_w / 2, base + 14, day, 10, MUT,
                            anchor="middle"))
        return "\n".join(out)

    def _product(self, x: float, y: float, w: float) -> str:
        """Фото позиции полки: адресник-бирка с колечком (бренд NOZZA)."""
        cx, cy = x + w / 2, y + 92
        out = [f'<ellipse cx="{cx}" cy="{cy + 52}" rx="96" ry="10" '
               f'fill="#0B1020" opacity="0.07"/>']
        # задняя бирка — глубина кадра
        out.append(f'<g transform="rotate(7 {cx} {cy})">'
                   + rrect(cx - 62, cy - 34, 124, 72, 10, INDIGO)
                   + f'<circle cx="{cx - 44}" cy="{cy - 14}" r="7" '
                     f'fill="#F8FAFC"/></g>')
        # передняя бирка
        out.append(f'<g transform="rotate(-6 {cx} {cy})">'
                   + rrect(cx - 70, cy - 40, 140, 82, 12, VIOLET)
                   + f'<circle cx="{cx - 50}" cy="{cy - 18}" r="8" '
                     f'fill="none" stroke="#F8FAFC" stroke-width="3"/>'
                   + text(cx - 30, cy - 6, "КОСТИЦА", 15, "#FFFFFF", True)
                   + text(cx - 30, cy + 14, "адресник · PLA", 10.5, "#EDE9FE")
                   + f'<rect x="{cx - 30}" y="{cy + 22}" width="52" height="3" '
                     f'rx="1.5" fill="#C4B5FD"/></g>')
        return "\n".join(out)


class Mockup:
    """Карточка-чат: ярлык идеи, шапка чата, сообщения, нижняя панель."""

    def __init__(self, code: str, slug: str, title: str, effect: str,
                 messages: list[Bubble], reply_rows: list[list[str]] | None = None):
        self.code = code
        self.slug = slug
        self.title = title
        self.effect = effect
        self.messages = messages
        self.reply_rows = reply_rows
        self.width = 560.0
        self.label_h = 66.0
        self.header_h = 56.0

    def chat_height(self) -> float:
        h = 14.0
        for m in self.messages:
            h += m.height() + 12
        if self.reply_rows:
            h += 8 + len(self.reply_rows) * 48
        return max(h, 330)

    def height(self) -> float:
        return self.label_h + self.header_h + self.chat_height() + 12

    def render(self) -> str:
        w, h = self.width, self.height()
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" '
               f'height="{h:.0f}" viewBox="0 0 {w:.0f} {h:.0f}">',
               rrect(0, 0, w, h, 18, HEADER_BG, stroke=BORDER, sw=1.2)]
        # ярлык идеи
        out.append(rrect(0, 0, w, self.label_h, 18, NAVY) +
                   rrect(0, self.label_h - 18, w, 18, 0, NAVY))
        out.append(rrect(0, 0, 6, self.label_h, 3, VIOLET))
        out.append(text(22, 29, f"{self.code} · {self.title}", 16, "#FFFFFF",
                        True))
        out.append(text(22, 50, self.effect, 12.5, FAINT))
        # шапка чата
        hy = self.label_h
        out.append(f'<line x1="0" y1="{hy}" x2="{w}" y2="{hy}" '
                   f'stroke="{BORDER}" stroke-width="1"/>')
        out.append(f'<line x1="0" y1="{hy + self.header_h}" x2="{w}" '
                   f'y2="{hy + self.header_h}" stroke="{BORDER}" '
                   f'stroke-width="1"/>')
        out.append(text(14, hy + 35, "‹", 22, MUT))
        out.append(f'<circle cx="44" cy="{hy + 28}" r="17" fill="{INDIGO}"/>')
        out.append(text(44, hy + 33, "PF", 12, "#FFFFFF", True, "middle"))
        out.append(text(70, hy + 24, "PrintFlow · цех", 14.5, INK, True))
        out.append(text(70, hy + 42, "бот", 12, MUT))
        # сообщения
        cy = hy + self.header_h + 10
        for m in self.messages:
            if m.who == "me":
                svg, cy = m.render(w - 14 - m.width, cy)
            else:
                svg, cy = m.render(14, cy)
            out.append(svg)
        # нижняя reply-панель
        if self.reply_rows:
            ry = h - 12 - len(self.reply_rows) * 48
            out.append(rrect(8, ry, w - 16, len(self.reply_rows) * 48 + 8, 10,
                             "#F2F6F9"))
            for i, row in enumerate(self.reply_rows):
                bx = 16
                bw = (w - 32 - 8 * (len(row) - 1)) / len(row)
                for label in row:
                    out.append(rrect(bx, ry + 6 + i * 48, bw, 40, 8,
                                     "#FFFFFF", stroke=BORDER, sw=0.9))
                    out.append(text(bx + bw / 2, ry + 32 + i * 48, label, 13,
                                    INK, anchor="middle"))
                    bx += bw + 8
        out.append("</svg>")
        return "\n".join(out)


def panel_lines() -> list:
    """Экран «меню = дашборд»: живое состояние цеха вместо справки."""
    return [
        ("🖨 P1S — печатает · 63%", True, INK),
        ("Осталось 1 ч 20 мин · финиш в 17:44", False, INK),
        ("", False, INK),
        ("₽ 30 дней: доход 84 500 ₽ · прибыль 31 200 ₽", True, INK),
        ("💰 Ждут оплаты: 4 300 ₽ по 2 заказам", False, INK),
        ("⚑ Следующее: Адресник «Костица» ×4 · 46 мин", False, INK),
    ]


MOCKUPS = [
    Mockup(
        "Б1", "menu", "Меню = дашборд",
        "«старт» отвечает состоянием цеха, а не текстом-справкой",
        [
            Bubble("me", [("меню", False, INK)], 96, time="17:41"),
            Bubble("bot", panel_lines(), 420,
                   buttons=[
                       [("🛒 Продать", "cb"), ("📦 Полка", "cb")],
                       [("💰 Касса", "cb"), ("📷 Кадр", "cb")],
                       [("📊 Итоги", "cb"), ("⚙ Ещё", "cb")],
                   ]),
        ],
        reply_rows=[["🛒 Продать", "📦 Полка", "💰 Касса"],
                    ["📷 Кадр", "📊 Итоги", "⚙ Ещё"]],
    ),
    Mockup(
        "Б2", "repeat", "Повтор продажи одним тапом",
        "частый чек — два касания вместо восьми",
        [
            Bubble("bot", [
                ("✅ Продано 2 шт «Адресник Костица» · 1 000 ₽", True, INK),
                ("в кассу магазина · осталось 8 шт", False, INK),
            ], 400, buttons=[
                [("↻ Ещё раз · 2 шт · 1 000 ₽", "cb")],
                [("🛒 Продать ещё", "cb"), ("💰 Забрали деньги", "cb")],
                [("🏠 В меню", "cb")],
            ]),
            Bubble("bot", [
                ("↻ Продано 2 шт · 1 000 ₽ — осталось 6 шт.", False, INK),
            ], 380),
        ],
    ),
    Mockup(
        "Б3", "sbp", "Заявка оплаты — кнопками",
        "СБП-сверка без набора «оплата подтвердить 1043»",
        [
            Bubble("bot", [
                ("💳 Заявка оплаты · заказ №1043", True, INK),
                ("Ольга · 2 500 ₽ · СБП", False, INK),
                ("«Перевела по QR, чек в чате»", False, MUT),
            ], 400, buttons=[
                [("✅ Подтвердить", "cb"), ("✖ Отклонить", "cb")],
                [("💬 Написать клиенту", "cb")],
            ]),
            Bubble("bot", [
                ("Оплата №1043 подтверждена и проведена ✓", False, INK),
                ("клиент получил уведомление", False, MUT),
            ], 400),
        ],
    ),
    Mockup(
        "Б4", "chart", "Отчёты картинкой",
        "график по дням — PNG без Pillow (техника qrgen.png_bytes)",
        [
            Bubble("bot", [], 424, photo={
                "kind": "chart",
                "caption": [("📊 Неделя 15–21 сентября", True),
                            ("выручка 38 400 ₽ · прибыль 14 900 ₽", False),
                            ("брак: 2 печати · часов печати: 41", False)],
            }, buttons=[
                [("📅 Сегодня", "cb"), ("📈 30 дней", "cb")],
                [("📄 Подробно текстом", "cb"), ("🏠 В меню", "cb")],
            ]),
        ],
    ),
    Mockup(
        "Б5", "photo", "Карточка полки с фото",
        "фото позиции уже лежит в базе — shelf_items.photo",
        [
            Bubble("bot", [], 424, photo={
                "kind": "product",
                "caption": [("🗂 Адресник «Костица» · 500 ₽", True),
                            ("на полке 10 шт · маржа 260 ₽ (52%)", False)],
            }, buttons=[
                [("🛒 Продать", "cb"), ("📥 Приход +1", "cb")],
                [("👁 Витрина: вкл", "cb"), ("🗄 В архив", "cb")],
                [("⬅ К списку", "cb")],
            ]),
        ],
    ),
    Mockup(
        "Б6", "pult", "Пульт цеха из чата",
        "из чата — в управление парком одним касанием",
        [
            Bubble("me", [("пульт", False, INK)], 96, time="17:52"),
            Bubble("bot", [
                ("Пульт цеха — в вашей Wi-Fi сети:", True, INK),
                ("• парк и очередь · камеры · AMS", False, INK),
                ("• файл «как в слайсере» со слотами", False, INK),
            ], 396, buttons=[
                [("📱 Открыть пульт цеха", "url")],
            ]),
        ],
    ),
    Mockup(
        "Б11", "oneline", "Продажа одной строкой",
        "«продал кост 2 500» = товар + количество + цена",
        [
            Bubble("me", [("продал кост 2 500", False, INK)], 190, time="17:58"),
            Bubble("bot", [
                ("Понял так:", True, INK),
                ("Адресник «Костица» × 2 по 500 ₽ = 1 000 ₽", False, INK),
                ("неточная фраза уточняется кнопкой", False, MUT),
            ], 400, buttons=[
                [("✓ Верно, записать", "cb"), ("✖ Нет", "cb")],
            ]),
            Bubble("bot", [
                ("✅ Продано · 1 000 ₽ в кассу · осталось 6", False, INK),
            ], 380, buttons=[
                [("↻ Ещё раз", "cb"), ("💰 Забрали деньги", "cb")],
                [("🏠 В меню", "cb")],
            ]),
        ],
    ),
    Mockup(
        "Б12", "change", "Сдача — в чате продажи",
        "кассир не считает в голове у ящика",
        [
            Bubble("bot", [
                ("✅ Продано на 1 760 ₽ — наличными", True, INK),
                ("чек №118 записан в кассу", False, MUT),
            ], 390, buttons=[
                [("💵 Считать сдачу", "cb")],
            ]),
            Bubble("me", [("2000", False, INK)], 96, time="18:02"),
            Bubble("bot", [
                ("💵 Сдача: 240 ₽", True, INK),
                ("1 760 + сдача 240 = 2 000 ✓", False, MUT),
            ], 360, buttons=[
                [("✓ Отдано", "cb"), ("🏠 В меню", "cb")],
            ]),
        ],
    ),
]


def poster() -> str:
    """Сетка 2×N: заголовок бренда, карточки, подвал."""
    gap = 26
    margin = 28
    cols = 2
    w = int(margin * 2 + MOCKUPS[0].width * cols + gap)
    head = 148
    row_h = []
    for r in range(0, len(MOCKUPS), cols):
        row_h.append(max(m.height() for m in MOCKUPS[r:r + cols]))
    h = int(head + sum(row_h) + gap * (len(row_h) - 1) + 66)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"'
           f' viewBox="0 0 {w} {h}">',
           f'<rect width="{w}" height="{h}" fill="#F8FAFC"/>',
           f'<rect width="{w}" height="{head - 24}" rx="0" fill="{NAVY}"/>',
           rrect(margin, 26, 84, 26, 13, VIOLET) ,
           text(margin + 42, 44, "18.2", 13, "#FFFFFF", True, "middle"),
           text(margin, 82, "Бот сотрудников: следующие шаги", 26, "#FFFFFF",
                True),
           text(margin, 108, "восемь макетов к docs/ИДЕИ-БОТ-18.2.md · "
                "обсуждаем и берём в работу по приоритету", 13.5, FAINT),
           text(w - margin, 44, "NOZZA · PrintFlow", 15, "#FFFFFF", True,
                "end"),
           text(w - margin, 66, "цех в кармане", 11.5, FAINT, anchor="end")]
    y = head
    for r in range(0, len(MOCKUPS), cols):
        for c, m in enumerate(MOCKUPS[r:r + cols]):
            body = m.render()
            body = body.split(">", 1)[1].rsplit("</svg>", 1)[0]
            x = margin + c * (m.width + gap)
            out.append(f'<g transform="translate({x} {y})">{body}</g>')
        y += row_h[r // cols] + gap
    out.append(text(margin, h - 24, "Макеты сгенерированы скриптом "
        "scripts/bot-ideas-svg.py — правятся одной командой, без ручной "
        "правки SVG.", 11.5, MUT))
    out.append("</svg>")
    return "\n".join(out)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for m in MOCKUPS:
        path = OUT / f"bot-idea-{m.slug}.svg"
        path.write_text(m.render(), encoding="utf-8")
        print("OK", path.relative_to(ROOT))
    path = OUT / "bot-ideas-182.svg"
    path.write_text(poster(), encoding="utf-8")
    print("OK", path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
