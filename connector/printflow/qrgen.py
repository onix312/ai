"""Генератор QR-кодов на чистом Python — без внешних библиотек.

Нужен, чтобы лаунчер показывал ссылку на панель прямо в терминале и в окне:
навёл телефон — панель открылась, ничего вручную набирать не надо.

Поддержаны версии 1–10 (до 271 байта на уровне M) и байтовый режим —
этого с запасом хватает для ссылок вида ``http://192.168.1.50:8080/``.

Алгоритм стандартный (ISO/IEC 18004): кодирование данных → коды Рида-Соломона
→ раскладка блоков → размещение модулей → выбор маски по штрафу.

    >>> m = matrix("http://localhost:8080/")
    >>> len(m) == len(m[0])
    True

Рендер:
    * :func:`terminal` — блоки Unicode для консоли (половинки — вдвое ниже);
    * :func:`png_bytes` — PNG без Pillow (для окна лаунчера и печати);
    * :func:`svg` — вектор для наклейки катушки и HTML-мастера QR.
"""
from __future__ import annotations

import struct
import zlib

__all__ = ["matrix", "terminal", "png_bytes", "svg", "QrError"]


class QrError(ValueError):
    """Текст не помещается в поддерживаемые версии QR."""


# --- Таблицы уровней коррекции -------------------------------------------
# Индекс — версия 1..10. Значения из ISO/IEC 18004, таблицы 13–22.
_ECC_PER_BLOCK = {
    "L": (7, 10, 15, 20, 26, 18, 20, 24, 30, 18),
    "M": (10, 16, 26, 18, 24, 16, 18, 22, 22, 26),
    "Q": (13, 22, 18, 26, 24, 18, 18, 22, 20, 24),
    "H": (17, 28, 22, 16, 22, 28, 26, 26, 24, 28),
}
_NUM_BLOCKS = {
    "L": (1, 1, 1, 1, 1, 2, 2, 2, 2, 4),
    "M": (1, 1, 1, 2, 2, 4, 4, 4, 5, 5),
    "Q": (1, 1, 2, 2, 4, 4, 6, 6, 8, 8),
    "H": (1, 1, 2, 4, 4, 4, 5, 6, 8, 8),
}
_ECC_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}
_MAX_VERSION = 10


# --- Арифметика поля Галуа GF(256) ----------------------------------------
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D  # порождающий многочлен поля QR
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    """Порождающий многочлен кода Рида-Соломона: (x-α⁰)(x-α¹)…(x-α^(n-1)).

    Коэффициенты идут от старшей степени к младшей, старший всегда 1.
    """
    poly = [1]
    for i in range(degree):
        shifted = [0] * (len(poly) + 1)
        for j, coeff in enumerate(poly):
            shifted[j] ^= coeff                          # умножение на x
            shifted[j + 1] ^= _gf_mul(coeff, _EXP[i])    # и на корень α^i
        poly = shifted
    return poly


def _rs_remainder(data: bytes, degree: int) -> list[int]:
    """Проверочные байты: остаток от деления данных на порождающий многочлен."""
    gen = _rs_generator(degree)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result[0]
        del result[0]
        result.append(0)
        for i in range(degree):
            result[i] ^= _gf_mul(gen[i + 1], factor)
    return result


# --- Геометрия версии ------------------------------------------------------
def _raw_data_modules(version: int) -> int:
    """Сколько модулей версии отведено под данные (до вычета коррекции)."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        num_align = version // 7 + 2
        result -= (25 * num_align - 10) * num_align - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version: int, level: str) -> int:
    ecc = _ECC_PER_BLOCK[level][version - 1]
    blocks = _NUM_BLOCKS[level][version - 1]
    return _raw_data_modules(version) // 8 - ecc * blocks


def _alignment_positions(version: int) -> list[int]:
    if version == 1:
        return []
    num_align = version // 7 + 2
    size = version * 4 + 17
    step = (version * 4 + num_align * 2 + 1) // (num_align * 2 - 2) * 2
    result = [6] * num_align
    pos = size - 7
    for i in range(num_align - 1, 0, -1):
        result[i] = pos
        pos -= step
    return result


def _char_count_bits(version: int) -> int:
    """Байтовый режим: 8 бит счётчика до версии 9, дальше 16."""
    return 8 if version <= 9 else 16


def _pick_version(length: int, level: str, min_version: int) -> int:
    for version in range(max(1, min_version), _MAX_VERSION + 1):
        capacity = _data_codewords(version, level)
        need = (4 + _char_count_bits(version) + length * 8 + 7) // 8
        if need <= capacity:
            return version
    raise QrError(
        f"Текст в {length} байт не помещается в QR версии {_MAX_VERSION} "
        f"с уровнем коррекции {level}"
    )


# --- Данные ----------------------------------------------------------------
def _bitstream(data: bytes, version: int, level: str) -> bytes:
    capacity = _data_codewords(version, level) * 8
    bits: list[int] = []

    def push(value: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((value >> shift) & 1)

    push(0b0100, 4)                              # режим «байты»
    push(len(data), _char_count_bits(version))
    for byte in data:
        push(byte, 8)
    push(0, min(4, capacity - len(bits)))        # терминатор
    while len(bits) % 8:
        bits.append(0)
    pad = (0xEC, 0x11)
    index = 0
    while len(bits) < capacity:
        push(pad[index % 2], 8)
        index += 1

    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for bit in bits[i:i + 8]:
            byte = (byte << 1) | bit
        out.append(byte)
    return bytes(out)


def _interleave(data: bytes, version: int, level: str) -> list[int]:
    """Разбить данные на блоки, добавить коррекцию и перемешать по стандарту."""
    blocks_count = _NUM_BLOCKS[level][version - 1]
    ecc_len = _ECC_PER_BLOCK[level][version - 1]
    raw = _raw_data_modules(version) // 8
    short_blocks = blocks_count - raw % blocks_count
    short_len = raw // blocks_count - ecc_len

    blocks: list[list[int]] = []
    offset = 0
    for i in range(blocks_count):
        size = short_len + (0 if i < short_blocks else 1)
        chunk = bytes(data[offset:offset + size])
        offset += size
        # Все блоки выравниваем по длинному: у короткого на месте
        # недостающего байта данных остаётся дырка, которую пропускаем при
        # перемешивании. Так проверочные байты стоят в одних и тех же колонках.
        gap = [0] * (short_len + 1 - size)
        blocks.append(list(chunk) + gap + _rs_remainder(chunk, ecc_len))

    result: list[int] = []
    for i in range(short_len + 1 + ecc_len):
        for index, block in enumerate(blocks):
            if i == short_len and index < short_blocks:
                continue  # дырка короткого блока
            result.append(block[i])
    return result


# --- Раскладка модулей -----------------------------------------------------
def _new_matrix(size: int) -> list[list[int]]:
    return [[0] * size for _ in range(size)]


def _draw_function_patterns(mod: list[list[int]], fun: list[list[int]], version: int) -> None:
    size = len(mod)

    def set_module(x: int, y: int, value: int) -> None:
        mod[y][x] = value
        fun[y][x] = 1

    # Тайминг-линии
    for i in range(size):
        set_module(6, i, 1 - i % 2)
        set_module(i, 6, 1 - i % 2)

    # Три поисковых квадрата с разделителями
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    dist = max(abs(dx), abs(dy))
                    set_module(x, y, 1 if dist != 2 and dist != 4 else 0)

    # Выравнивающие квадраты
    positions = _alignment_positions(version)
    last = len(positions) - 1
    for i, ay in enumerate(positions):
        for j, ax in enumerate(positions):
            if (i, j) in ((0, 0), (0, last), (last, 0)):
                continue  # углы заняты поисковыми квадратами
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    set_module(ax + dx, ay + dy, 1 if max(abs(dx), abs(dy)) != 1 else 0)

    # Резерв под информацию о формате и версии. Модули тайминга (строка и
    # столбец 6) уже размечены — их значение не трогаем, иначе разорвём линию.
    def reserve(x: int, y: int) -> None:
        if not fun[y][x]:
            set_module(x, y, 0)

    for i in range(9):
        reserve(8, i)
        reserve(i, 8)
    for i in range(8):
        reserve(8, size - 1 - i)
        reserve(size - 1 - i, 8)
    if version >= 7:
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = version << 12 | rem
        for i in range(18):
            bit = (bits >> i) & 1
            a, b = size - 11 + i % 3, i // 3
            set_module(a, b, bit)
            set_module(b, a, bit)


def _draw_format(mod: list[list[int]], level: str, mask: int) -> None:
    size = len(mod)
    data = _ECC_BITS[level] << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = (data << 10 | rem) ^ 0x5412

    def bit(i: int) -> int:
        return (bits >> i) & 1

    for i in range(6):
        mod[i][8] = bit(i)
    mod[7][8] = bit(6)
    mod[8][8] = bit(7)
    mod[8][7] = bit(8)
    for i in range(9, 15):
        mod[8][14 - i] = bit(i)
    for i in range(8):
        mod[8][size - 1 - i] = bit(i)
    for i in range(8, 15):
        mod[size - 15 + i][8] = bit(i)
    mod[size - 8][8] = 1  # всегда тёмный модуль


def _draw_codewords(mod: list[list[int]], fun: list[list[int]], codewords: list[int]) -> None:
    size = len(mod)
    index = 0
    total = len(codewords) * 8
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = (size - 1 - vert) if upward else vert
                if not fun[y][x] and index < total:
                    mod[y][x] = (codewords[index >> 3] >> (7 - (index & 7))) & 1
                    index += 1
        right -= 2


def _mask_bit(mask: int, x: int, y: int) -> bool:
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (y // 2 + x // 3) % 2 == 0
    if mask == 5:
        return x * y % 2 + x * y % 3 == 0
    if mask == 6:
        return (x * y % 2 + x * y % 3) % 2 == 0
    return ((x + y) % 2 + x * y % 3) % 2 == 0


def _apply_mask(mod: list[list[int]], fun: list[list[int]], mask: int) -> None:
    size = len(mod)
    for y in range(size):
        for x in range(size):
            if not fun[y][x] and _mask_bit(mask, x, y):
                mod[y][x] ^= 1


def _penalty(mod: list[list[int]]) -> int:
    """Штраф маски по четырём правилам стандарта: чем меньше, тем читаемее."""
    size = len(mod)
    score = 0

    # Правило 1: серии одинаковых модулей длиной 5+
    for line in list(mod) + [[mod[y][x] for y in range(size)] for x in range(size)]:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
                if run == 5:
                    score += 3
                elif run > 5:
                    score += 1
            else:
                run = 1

    # Правило 2: квадраты 2×2 одного цвета
    for y in range(size - 1):
        for x in range(size - 1):
            block = mod[y][x] + mod[y][x + 1] + mod[y + 1][x] + mod[y + 1][x + 1]
            if block in (0, 4):
                score += 3

    # Правило 3: шаблон, похожий на поисковый квадрат
    pattern = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    rpattern = pattern[::-1]
    for y in range(size):
        for x in range(size - 10):
            row = [mod[y][x + i] for i in range(11)]
            if row == pattern or row == rpattern:
                score += 40
    for x in range(size):
        for y in range(size - 10):
            col = [mod[y + i][x] for i in range(11)]
            if col == pattern or col == rpattern:
                score += 40

    # Правило 4: перекос баланса чёрного и белого
    dark = sum(sum(row) for row in mod)
    total = size * size
    score += abs(dark * 20 - total * 10) // total * 10
    return score


def matrix(text: str, level: str = "M", min_version: int = 1) -> list[list[int]]:
    """Матрица QR-кода: список строк, 1 — тёмный модуль, 0 — светлый.

    ``level`` — уровень коррекции ошибок L/M/Q/H (по умолчанию M: терпит
    примерно 15% повреждений, что достаточно для экрана и наклейки).
    """
    level = level.upper()
    if level not in _ECC_PER_BLOCK:
        raise QrError(f"Неизвестный уровень коррекции: {level}")
    data = text.encode("utf-8")
    version = _pick_version(len(data), level, min_version)
    size = version * 4 + 17

    codewords = _interleave(_bitstream(data, version, level), version, level)
    mod = _new_matrix(size)
    fun = _new_matrix(size)
    _draw_function_patterns(mod, fun, version)
    _draw_codewords(mod, fun, codewords)

    best, best_score = 0, None
    for mask in range(8):
        _apply_mask(mod, fun, mask)
        _draw_format(mod, level, mask)
        score = _penalty(mod)
        if best_score is None or score < best_score:
            best, best_score = mask, score
        _apply_mask(mod, fun, mask)  # откатываем маску (XOR обратим)
    _apply_mask(mod, fun, best)
    _draw_format(mod, level, best)
    return mod


# --- Рендер ----------------------------------------------------------------
def terminal(text: str, level: str = "M", border: int = 2, compact: bool = True) -> str:
    """QR-код символами для вывода в консоль.

    ``compact`` — половинные блоки: код вдвое ниже и помещается в окно,
    но требует шрифта с ▀ (есть в Windows Terminal, cmd, macOS, Linux).
    """
    mod = matrix(text, level)
    size = len(mod)
    field = [[0] * (size + border * 2) for _ in range(size + border * 2)]
    for y in range(size):
        for x in range(size):
            field[y + border][x + border] = mod[y][x]
    height = len(field)

    if not compact:
        return "\n".join("".join("██" if cell else "  " for cell in row) for row in field)

    lines = []
    for y in range(0, height, 2):
        top = field[y]
        bottom = field[y + 1] if y + 1 < height else [0] * len(top)
        row = []
        for x in range(len(top)):
            dark_top, dark_bottom = top[x], bottom[x]
            if dark_top and dark_bottom:
                row.append("█")
            elif dark_top:
                row.append("▀")
            elif dark_bottom:
                row.append("▄")
            else:
                row.append(" ")
        lines.append("".join(row))
    return "\n".join(lines)


def png_bytes(text: str, level: str = "M", scale: int = 6, border: int = 2) -> bytes:
    """PNG с QR-кодом без внешних библиотек (Tk умеет показывать такой файл)."""
    mod = matrix(text, level)
    size = len(mod)
    side = (size + border * 2) * scale

    rows = bytearray()
    for y in range(side):
        rows.append(0)  # фильтр строки PNG
        my = y // scale - border
        for x in range(side):
            mx = x // scale - border
            dark = 0 <= my < size and 0 <= mx < size and mod[my][mx]
            rows.append(0 if dark else 255)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)  # 8 бит, оттенки серого
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))


def svg(text: str, level: str = "M", scale: int = 4, border: int = 2,
        dark: str = "#111111", light: str = "#ffffff") -> str:
    """Векторный QR: модули как квадраты, без внешних библиотек."""
    mod = matrix(text, level)
    size = len(mod)
    side = size + border * 2
    px = max(1, int(scale))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}"'
        f' width="{side * px}" height="{side * px}" shape-rendering="crispEdges">',
        f'<rect width="{side}" height="{side}" fill="{light}"/>',
    ]
    for y, row in enumerate(mod):
        for x, cell in enumerate(row):
            if cell:
                parts.append(
                    f'<rect x="{x + border}" y="{y + border}" width="1" height="1" fill="{dark}"/>'
                )
    parts.append("</svg>")
    return "".join(parts)
