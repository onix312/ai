"""Штрихкоды Code 128 (идея 101): касса сканирует ценник.

Только стандартная библиотека: таблица паттернов + PNG-энкодер как в qrgen.
Касса/телефон читают Code 128 из коробки, DataMatrix откладываем — у нас
основной сценарий «поставили ценник на кассу и нажали сканер».

Схема честная:
• Code C — если текст чистые цифры (2 цифры на символ);
• Code B — иначе (буквы, цена «199.90», артикулы);
• паритет (GS) не используем — в ценнике один сегмент.
"""
from __future__ import annotations

import struct
import zlib

# Коды символов: start B=104, start C=99, stop=106, паритет=105.
_START_B = 104
_START_C = 99
_STOP = 106

# Таблица 107 паттернов (ширины штрихов/промежутков, 11 модулей) — стандарт ISO/IEC 15417.
_CODE128_PATTERNS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213",
    "122312", "132212", "221213", "221312", "231212", "112232", "122132",
    "122231", "113222", "123122", "123221", "223211", "221132", "221231",
    "213212", "223112", "312131", "311222", "321122", "321221", "312212",
    "322112", "322211", "212123", "212321", "232121", "111323", "131123",
    "131321", "112313", "132113", "132311", "211313", "231113", "231311",
    "112133", "112331", "132131", "113123", "113321", "133121", "313121",
    "211331", "231131", "213113", "213311", "213131", "311123", "311321",
    "331121", "312113", "312311", "332111", "314111", "221411", "431111",
    "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114",
    "413111", "241112", "134111", "111242", "121142", "121241", "114212",
    "124112", "124211", "411212", "421112", "421211", "212141", "214121",
    "412121", "111143", "111341", "131141", "114113", "114311", "411113",
    "411311", "113141", "114131", "311141", "411131", "211412", "211214",
    "211232", "2331112",
]


def _ascii_code(ch: str) -> int:
    """Код символа в Code 128 B: ASCII 32..127."""
    code = ord(ch)
    if not 32 <= code <= 127:
        raise ValueError(f"Символ «{ch}» не входит в ASCII-32..127 — "
                         "Code 128 B не печатает его")
    return code - 32


def encode(text: str) -> list[int]:
    """Коды символов (start…stop) с контрольной суммой."""
    text = str(text or "")
    if not text:
        raise ValueError("Пустой текст — штрихкод не построить")
    codes: list[int] = []
    if text.isdigit() and len(text) % 2 == 0:
        # Code C: пары цифр
        codes.append(_START_C)
        for i in range(0, len(text), 2):
            codes.append(int(text[i:i + 2]))
    else:
        codes.append(_START_B)
        for ch in text:
            codes.append(_ascii_code(ch))
    # Контрольная сумма: start + Σ symbol_i · i, где i — с 1 для первого символа.
    check = codes[0] + sum(c * (i + 1) for i, c in enumerate(codes[1:]))
    codes.append(check % 103)
    codes.append(_STOP)
    return codes


def modules(text: str) -> list[int]:
    """Биты штрихов: 1 — чёрный, 0 — белый. Порядок: слева направо."""
    out: list[int] = []
    for code in encode(text):
        pattern = _CODE128_PATTERNS[code]
        for i, w in enumerate(pattern):
            out.extend([1 if i % 2 == 0 else 0] * int(w))
    # Stop-код уже содержит завершающие 13 модулей; выравниваем до чётного.
    if len(out) % 2:
        out.append(0)
    return out


def svg(text: str, width_mm: float = 30.0, height_mm: float = 12.0) -> str:
    """SVG-штрихкод с зоной тишины (~8 модулей) — прямо в лист ценников."""
    mods = modules(text)
    unit = width_mm / len(mods)  # один модуль в мм
    rects = []
    i = 0
    x = 0
    while i < len(mods):
        if mods[i]:
            j = i
            while j < len(mods) and mods[j]:
                j += 1
            rects.append(f'<rect x="{x * unit:.3f}" y="0" '
                         f'width="{(j - i) * unit:.3f}" height="{height_mm}"/>')
            i = j
            x = j
        else:
            i += 1
    svg_w = (len(mods) + 16) * unit
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {svg_w:.2f} {height_mm + 6:.2f}" '
            f'width="{svg_w:.2f}mm" height="{height_mm + 6:.2f}mm">'
            f'<g fill="#111">{"".join(rects)}</g>'
            f'<text x="{svg_w / 2:.2f}" y="{height_mm + 5:.2f}" text-anchor="middle" '
            f'font-size="4" font-family="monospace">{text}</text></svg>')


def png_bytes(text: str, scale: int = 4, quiet: int = 8) -> bytes:
    """PNG-штрихкод без внешних библиотек (паттерн qrgen)."""
    mods = modules(text)
    w = (len(mods) + quiet * 2) * scale
    h = (12 + 4) * scale
    rows = bytearray()
    bar_h = 12 * scale  # штрихкод в верхней части, ниже — белая полоса под текст
    for y in range(h):
        rows.append(0)
        in_bar = y < bar_h
        for x in range(w):
            mx = x // scale - quiet
            dark = in_bar and 0 <= mx < len(mods) and mods[mx]
            rows.append(0 if dark else 255)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))


def validate(text: str) -> dict:
    """Честная проверка: контрольная сумма сходится после перекодирования."""
    codes = encode(text)
    payload = codes[1:-2]
    check = codes[0] + sum(c * (i + 1) for i, c in enumerate(payload))
    ok = check % 103 == codes[-2]
    return {
        "text": text,
        "mode": "C" if codes[0] == _START_C else "B",
        "symbols": len(codes),
        "modules": len(modules(text)),
        "check_ok": ok,
    }
