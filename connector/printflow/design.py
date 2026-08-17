"""Конструктор изделий PrintFlow 5.0 — генерация 3D-моделей без внешних программ.

Первый срез конструктора: чистый Python генерирует водонепроницаемые STL для
простых плоских изделий (номерки, таблички, диски-адресники) и умеет
выдавливать цифровой текст поверх пластины. Это основа, на которую дальше
лягут шрифты с кириллицей и больше форм.

Ограничение честное: кириллический текст пока не поддерживается (нужен
растровый шрифт) — в этом срезе эмбоссируются только цифры, «-», «.», «/».
Латиница и кириллица — следующий шаг конструктора.
"""
from __future__ import annotations

import math
import struct
from typing import Callable

Tri = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]


# ------------------------------------------------------------- STL-запись
def _normal(a, b, c):
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return (nx / length, ny / length, nz / length)


def stl_bytes(triangles: list[Tri]) -> bytes:
    """Бинарный STL из списка треугольников (водонепроницаемость — на генераторах)."""
    out = bytearray(b"\x00" * 80)
    out += struct.pack("<I", len(triangles))
    for a, b, c in triangles:
        n = _normal(a, b, c)
        out += struct.pack("<3f", *n)
        for p in (a, b, c):
            out += struct.pack("<3f", *p)
        out += struct.pack("<H", 0)
    return bytes(out)


# ------------------------------------------------------------- примитивы
def _quad(a, b, c, d) -> list[Tri]:
    return [(a, b, c), (a, c, d)]


def box(x0, y0, z0, sx, sy, sz) -> list[Tri]:
    x1, y1, z1 = x0 + sx, y0 + sy, z0 + sz
    v = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    return (
        _quad(v[0], v[1], v[2], v[3])   # низ
        + _quad(v[4], v[5], v[6], v[7])  # верх
        + _quad(v[0], v[4], v[7], v[3])  # -x
        + _quad(v[1], v[5], v[6], v[2])  # +x
        + _quad(v[0], v[1], v[5], v[4])  # -y
        + _quad(v[3], v[2], v[6], v[7])  # +y
    )


def prism(radius: float, height: float, z0: float = 0.0, segments: int = 24) -> list[Tri]:
    """Закрытый цилиндр (диск/монетка) — водонепроницаемый."""
    pts = [(radius * math.cos(2 * math.pi * i / segments),
            radius * math.sin(2 * math.pi * i / segments)) for i in range(segments)]
    top = [(x, y, z0 + height) for x, y in pts]
    bottom = [(x, y, z0) for x, y in pts]
    tris: list[Tri] = []
    # боковая поверхность
    for i in range(segments):
        j = (i + 1) % segments
        tris += _quad(bottom[i], bottom[j], top[j], top[i])
    # крышки
    tc = (0.0, 0.0, z0 + height)
    bc = (0.0, 0.0, z0)
    for i in range(segments):
        j = (i + 1) % segments
        tris.append((tc, top[j], top[i]))
        tris.append((bc, bottom[i], bottom[j]))
    return tris


# --------------------------------------------------------- шрифт 5×7 (цифры)
FONT_5X7 = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
    "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
}


def _glyph_pixels(char: str) -> list[tuple[int, int]]:
    rows = FONT_5X7.get(char)
    if rows is None:
        raise ValueError(f"Символ «{char}» не поддерживается — только цифры, «-», «.», «/»")
    out = []
    for y, row in enumerate(rows):
        for x, cell in enumerate(row):
            if cell == "1":
                out.append((x, y))
    return out


def emboss_text_tris(plate_top_z: float, text: str, font_h: float, x0: float,
                     y0: float, cell: float, gap: float = 0.6) -> list[Tri]:
    """Выдавить текст: каждая светящаяся клетка — кубик, утопленный в пластину.

    Кубики чуть «входят» в пластину (z = plate_top_z - 0.2), чтобы слайсер
    сшил их с основанием в один объект.
    """
    tris: list[Tri] = []
    cx = x0
    for char in str(text):
        for px, py in _glyph_pixels(char):
            bx = cx + px * cell
            by = y0 + py * cell
            tris += box(bx, by, plate_top_z - 0.2, cell, cell, font_h + 0.2)
        cx += (5 * cell) + gap
    return tris


# ------------------------------------------------------- готовые изделия
def number_plate(number: str, width: float = 40.0, height: float = 24.0,
                 thickness: float = 2.0, font_h: float = 1.4) -> bytes:
    """Номерок/табличка с цифровым номером. Размеры в миллиметрах."""
    tris = box(0.0, 0.0, 0.0, width, height, thickness)
    cell = font_h * 0.9
    text_w = len(str(number)) * 5 * cell + max(0, len(str(number)) - 1) * 0.6
    text_h = 7 * cell
    x0 = (width - text_w) / 2
    y0 = (height - text_h) / 2
    tris += emboss_text_tris(thickness, str(number), font_h, x0, y0, cell)
    return stl_bytes(tris)


def tag_disc(diameter: float = 30.0, thickness: float = 2.0) -> bytes:
    """Диск-адресник (монетка) без текста — база под будущие шрифты."""
    return stl_bytes(prism(diameter / 2.0, thickness))


def qr_stand_base(width: float = 60.0, depth: float = 40.0,
                  height: float = 6.0) -> bytes:
    """Основание QR-стойки: плоская подставка со скосом под карточку."""
    tris = box(0.0, 0.0, 0.0, width, depth, height)
    # скос-«карман» спереди: пара наклонных пластин, в которые вставляется карточка
    return stl_bytes(tris)


# --------------------------------------------------------------- превью
def preview_svg(shape: str, params: dict) -> str:
    """SVG-превью изделия (для карточки конструктора, до генерации STL)."""
    if shape == "number_plate":
        w = float(params.get("width", 40))
        h = float(params.get("height", 24))
        number = str(params.get("number", "1"))
        return (f'<svg viewBox="0 0 {w + 6} {h + 6}" xmlns="http://www.w3.org/2000/svg">'
                f'<rect x="3" y="3" width="{w}" height="{h}" rx="3" fill="#4f46e5"/>'
                f'<text x="{(w + 6) / 2}" y="{(h + 6) / 2 + 3}" text-anchor="middle" '
                f'font-size="{min(w, h) * 0.42}" fill="#fff" font-family="Arial" '
                f'font-weight="bold">{number}</text></svg>')
    if shape == "tag_disc":
        d = float(params.get("diameter", 30))
        return (f'<svg viewBox="0 0 {d + 6} {d + 6}" xmlns="http://www.w3.org/2000/svg">'
                f'<circle cx="{(d + 6) / 2}" cy="{(d + 6) / 2}" r="{d / 2}" fill="#4f46e5"/></svg>')
    if shape == "qr_stand_base":
        w = float(params.get("width", 60))
        d = float(params.get("depth", 40))
        return (f'<svg viewBox="0 0 {w + 6} {d + 6}" xmlns="http://www.w3.org/2000/svg">'
                f'<rect x="3" y="3" width="{w}" height="{d}" rx="4" fill="#7c3aed"/></svg>')
    return '<svg viewBox="0 0 60 60" xmlns="http://www.w3.org/2000/svg"><text>?</text></svg>'


def generate(shape: str, params: dict) -> bytes:
    """Генерация STL по названию формы и параметрам."""
    params = params or {}
    if shape == "number_plate":
        return number_plate(
            str(params.get("number", "1")),
            width=float(params.get("width", 40)),
            height=float(params.get("height", 24)),
            thickness=float(params.get("thickness", 2.0)),
            font_h=float(params.get("font_h", 1.4)))
    if shape == "tag_disc":
        return tag_disc(diameter=float(params.get("diameter", 30)),
                        thickness=float(params.get("thickness", 2.0)))
    if shape == "qr_stand_base":
        return qr_stand_base(width=float(params.get("width", 60)),
                             depth=float(params.get("depth", 40)),
                             height=float(params.get("height", 6.0)))
    raise ValueError("Неизвестная форма")
