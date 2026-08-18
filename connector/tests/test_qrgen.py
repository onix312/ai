"""Тесты генератора QR: проверяем не «нарисовалось что-то», а стандарт.

Три уровня проверки:
  1. Коды Рида-Соломона — синдромы валидного кодового слова равны нулю
     (независимая проверка через подстановку корней, а не повтор кода).
  2. Служебные биты формата и версии — сверка с таблицами ISO/IEC 18004.
  3. Полный обратный разбор: читаем матрицу как сканер (снимаем маску,
     собираем кодовые слова, разбираем блоки) и получаем исходный текст.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import qrgen  # noqa: E402


def _gf_pow(exponent: int) -> int:
    return qrgen._EXP[exponent % 255]


def _gf_mul(a: int, b: int) -> int:
    return qrgen._gf_mul(a, b)


def _syndromes(block: list[int], ecc_len: int) -> list[int]:
    """Синдромы кодового слова: у корректного кода Рида-Соломона все нули."""
    out = []
    for j in range(ecc_len):
        acc = 0
        for byte in block:
            acc = _gf_mul(acc, _gf_pow(j)) ^ byte
        out.append(acc)
    return out


class TestReedSolomon(unittest.TestCase):
    def test_syndromes_are_zero(self):
        data = bytes(range(1, 17))
        for ecc_len in (7, 10, 16, 26):
            block = list(data) + qrgen._rs_remainder(data, ecc_len)
            self.assertEqual(_syndromes(block, ecc_len), [0] * ecc_len,
                             f"коррекция длиной {ecc_len} не проходит проверку")

    def test_single_error_is_detected(self):
        data = bytes(range(1, 17))
        block = list(data) + qrgen._rs_remainder(data, 10)
        block[3] ^= 0x5A
        self.assertNotEqual(_syndromes(block, 10), [0] * 10)


class TestServiceBits(unittest.TestCase):
    # Таблица C.1 стандарта: 15 бит формата для уровня M, маски 0..7.
    FORMAT_M = (0x5412, 0x5125, 0x5E7C, 0x5B4B, 0x45F9, 0x40CE, 0x4F97, 0x4AA0)
    FORMAT_L = (0x77C4, 0x72F3, 0x7DAA, 0x789D, 0x662F, 0x6318, 0x6C41, 0x6976)
    # Таблица D.1: 18 бит версии для версий 7..10.
    VERSION_BITS = {7: 0x07C94, 8: 0x085BC, 9: 0x09A99, 10: 0x0A4D3}

    def _format_bits(self, level: str, mask: int) -> int:
        data = qrgen._ECC_BITS[level] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        return (data << 10 | rem) ^ 0x5412

    def test_format_bits_match_standard(self):
        for mask in range(8):
            self.assertEqual(self._format_bits("M", mask), self.FORMAT_M[mask])
            self.assertEqual(self._format_bits("L", mask), self.FORMAT_L[mask])

    def test_version_bits_match_standard(self):
        for version, expected in self.VERSION_BITS.items():
            rem = version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            self.assertEqual(version << 12 | rem, expected)

    def test_alignment_positions(self):
        self.assertEqual(qrgen._alignment_positions(1), [])
        self.assertEqual(qrgen._alignment_positions(2), [6, 18])
        self.assertEqual(qrgen._alignment_positions(7), [6, 22, 38])
        self.assertEqual(qrgen._alignment_positions(10), [6, 28, 50])

    def test_capacity_table_is_consistent(self):
        # Данные + коррекция должны занимать ровно все кодовые слова версии.
        for version in range(1, 11):
            raw = qrgen._raw_data_modules(version) // 8
            for level in ("L", "M", "Q", "H"):
                ecc = qrgen._ECC_PER_BLOCK[level][version - 1]
                blocks = qrgen._NUM_BLOCKS[level][version - 1]
                self.assertEqual(qrgen._data_codewords(version, level) + ecc * blocks, raw)


class TestStructure(unittest.TestCase):
    def test_size_and_version_growth(self):
        small = qrgen.matrix("ok")
        self.assertEqual(len(small), 21)          # версия 1
        self.assertEqual(len(small[0]), 21)
        big = qrgen.matrix("x" * 200, "M")
        self.assertEqual(len(big), 10 * 4 + 17)   # версия 10

    def test_finder_and_timing_patterns(self):
        mod = qrgen.matrix("http://192.168.1.50:8080/")
        size = len(mod)
        for cx, cy in ((0, 0), (size - 7, 0), (0, size - 7)):
            self.assertEqual([mod[cy][cx + i] for i in range(7)], [1, 1, 1, 1, 1, 1, 1])
            self.assertEqual(mod[cy + 1][cx + 1], 0)
            self.assertEqual(mod[cy + 3][cx + 3], 1)
        for i in range(8, size - 8):
            self.assertEqual(mod[6][i], 1 - i % 2)
            self.assertEqual(mod[i][6], 1 - i % 2)
        self.assertEqual(mod[size - 8][8], 1, "обязательный тёмный модуль потерян")

    def test_too_long_text_is_rejected(self):
        with self.assertRaises(qrgen.QrError):
            qrgen.matrix("x" * 300)


class TestRoundTrip(unittest.TestCase):
    """Читаем матрицу как сканер и убеждаемся, что вернулся исходный текст."""

    def decode(self, mod: list[list[int]]) -> str:
        size = len(mod)
        version = (size - 17) // 4

        # 1. Информация о формате: уровень и маска (перебор 32 вариантов).
        raw_format = 0
        for i in range(8):
            raw_format |= mod[8][size - 1 - i] << i
        for i in range(8, 15):
            raw_format |= mod[size - 15 + i][8] << i
        level, mask = None, None
        for candidate_level, bits in qrgen._ECC_BITS.items():
            for candidate_mask in range(8):
                data = bits << 3 | candidate_mask
                rem = data
                for _ in range(10):
                    rem = (rem << 1) ^ ((rem >> 9) * 0x537)
                if ((data << 10 | rem) ^ 0x5412) == raw_format:
                    level, mask = candidate_level, candidate_mask
        self.assertIsNotNone(level, "информация о формате не распознана")

        # 2. Карта служебных модулей и снятие маски.
        clean = [row[:] for row in mod]
        fun = qrgen._new_matrix(size)
        qrgen._draw_function_patterns(qrgen._new_matrix(size), fun, version)
        for y in range(size):
            for x in range(size):
                if not fun[y][x] and qrgen._mask_bit(mask, x, y):
                    clean[y][x] ^= 1

        # 3. Чтение кодовых слов «змейкой» справа налево.
        bits = []
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (size - 1 - vert) if upward else vert
                    if not fun[y][x]:
                        bits.append(clean[y][x])
            right -= 2
        stream = [int("".join(str(b) for b in bits[i:i + 8]), 2)
                  for i in range(0, len(bits) - 7, 8)]

        # 4. Обратная раскладка блоков и проверка коррекции.
        blocks_count = qrgen._NUM_BLOCKS[level][version - 1]
        ecc_len = qrgen._ECC_PER_BLOCK[level][version - 1]
        raw = qrgen._raw_data_modules(version) // 8
        short_blocks = blocks_count - raw % blocks_count
        short_len = raw // blocks_count - ecc_len

        blocks = [[] for _ in range(blocks_count)]
        pos = 0
        for i in range(short_len + 1):
            for index in range(blocks_count):
                if i == short_len and index < short_blocks:
                    continue
                blocks[index].append(stream[pos])
                pos += 1
        for i in range(ecc_len):
            for index in range(blocks_count):
                blocks[index].append(stream[pos])
                pos += 1
        for index, block in enumerate(blocks):
            self.assertEqual(_syndromes(block, ecc_len), [0] * ecc_len,
                             f"блок {index} не проходит проверку коррекции")

        # 5. Разбор полезных данных.
        payload = bytearray()
        for index, block in enumerate(blocks):
            payload += bytes(block[:short_len + (0 if index < short_blocks else 1)])
        data_bits = "".join(f"{byte:08b}" for byte in payload)
        self.assertEqual(data_bits[:4], "0100", "ожидался байтовый режим")
        count_bits = qrgen._char_count_bits(version)
        length = int(data_bits[4:4 + count_bits], 2)
        start = 4 + count_bits
        out = bytearray()
        for i in range(length):
            out.append(int(data_bits[start + i * 8:start + (i + 1) * 8], 2))
        return out.decode("utf-8")

    def test_round_trip_texts(self):
        samples = [
            "http://localhost:8080/",
            "http://192.168.1.50:8080/",
            "http://10.0.0.7:9000/m",
            "NOZZA",
            "http://192.168.100.200:8080/shelf.html?id=abc-123-def",
            "Симферополь",          # кириллица в UTF-8
            "x" * 120,              # многоблочная версия
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(self.decode(qrgen.matrix(text)), text)

    def test_round_trip_all_levels(self):
        text = "http://192.168.1.50:8080/"
        for level in ("L", "M", "Q", "H"):
            with self.subTest(level=level):
                self.assertEqual(self.decode(qrgen.matrix(text, level)), text)


class TestRender(unittest.TestCase):
    def test_terminal_render_shapes(self):
        art = qrgen.terminal("http://localhost:8080/", border=2)
        lines = art.splitlines()
        size = len(qrgen.matrix("http://localhost:8080/")) + 4
        self.assertEqual(len(lines), (size + 1) // 2)   # половинные блоки
        self.assertTrue(all(len(line) == size for line in lines))
        wide = qrgen.terminal("http://localhost:8080/", border=2, compact=False)
        self.assertEqual(len(wide.splitlines()), size)

    def test_png_is_valid(self):
        data = qrgen.png_bytes("http://localhost:8080/", scale=4, border=2)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(data.endswith(b"IEND\xaeB`\x82"))
        width = int.from_bytes(data[16:20], "big")
        expected = (len(qrgen.matrix("http://localhost:8080/")) + 4) * 4
        self.assertEqual(width, expected)


if __name__ == "__main__":
    unittest.main()
