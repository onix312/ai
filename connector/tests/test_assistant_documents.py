"""Чтение файлов и индекс знаний (18.14): идеи И142, И143, И147, И148.

Что здесь важно и почему проверяется именно так:

  * текст извлекается без новых зависимостей — `.docx` и `.xlsx` это zip с XML,
    и ассистент читает их `zipfile`, а не тянет библиотеку в окружение агента;
  * нечитаемое не притворяется прочитанным: PDF без библиотеки, фото и чужое
    расширение возвращают причину, а не пустую строку;
  * нарезка не теряет номер строки — иначе цитату нельзя проверить глазами;
  * факты из документа детерминированные: сумма, дата и телефон находятся
    регулярным выражением и хранят место в файле (модель деньги не считает);
  * индекс не перечитывает то, что не менялось, и забывает исчезнувший файл.
"""
from __future__ import annotations

import pathlib
import struct
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from agent import documents  # noqa: E402
from agent.store import Store  # noqa: E402


def make_docx(path: pathlib.Path, paragraphs: list[str]) -> None:
    """Минимальный .docx: только то, что нужно для извлечения текста."""
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = ('<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
           + body + "</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("[Content_Types].xml", "<Types/>")


def make_xlsx(path: pathlib.Path, strings: list[str]) -> None:
    shared = "".join(f"<si><t>{text}</t></si>" for text in strings)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml",
                         '<?xml version="1.0"?><sst>' + shared + "</sst>")
        archive.writestr("xl/worksheets/sheet1.xml",
                         '<?xml version="1.0"?><worksheet><sheetData>'
                         '<row><c t="inlineStr"><is><t>12500</t></is></c></row>'
                         "</sheetData></worksheet>")


def make_binary_stl(path: pathlib.Path, name: str, facets: int) -> None:
    header = name.encode("utf-8")[:80].ljust(80, b"\x00")
    with path.open("wb") as handle:
        handle.write(header + struct.pack("<I", facets))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def read(self, name: str, content: bytes | str) -> dict:
        path = self.root / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return documents.read_document(path)

    def test_plain_text_and_markdown(self):
        for name in ("a.txt", "b.md", "c.csv"):
            result = self.read(name, "Договор с Марией\nСумма 12 500 ₽\n")
            self.assertTrue(result["ok"], result["reason"])
            self.assertIn("Марией", result["text"])

    def test_cp1251_falls_back_and_says_so(self):
        result = self.read("win.txt", "Профиль печати".encode("cp1251"))
        self.assertTrue(result["ok"])
        self.assertIn("Профиль", result["text"])

    def test_docx_text_is_extracted_without_dependencies(self):
        path = self.root / "договор.docx"
        make_docx(path, ["Договор №12", "Сумма 12 500,00 ₽", "Срок 2026-10-05"])
        result = documents.read_document(path)
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual("docx", result["kind"])
        self.assertIn("12 500,00", result["text"])
        self.assertIn("2026-10-05", result["text"])

    def test_xlsx_strings_are_extracted(self):
        path = self.root / "прайс.xlsx"
        make_xlsx(path, ["Прайс поставщика", "PETG чёрный", "1 250 ₽ за кг"])
        result = documents.read_document(path)
        self.assertTrue(result["ok"], result["reason"])
        self.assertIn("PETG", result["text"])
        self.assertIn("12500", result["text"], "значения ячеек тоже попадают в индекс")

    def test_broken_office_file_gives_reason(self):
        result = self.read("broken.docx", "это не zip".encode("utf-8"))
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"])

    def test_gcode_keeps_settings_and_drops_coordinates(self):
        result = self.read("job.gcode",
                           "; filament type = PETG\n; nozzle temperature = 250C\n"
                           "G1 X10.5 Y20.3 E1.2345\nG1 X11.5 Y20.3 E1.2400\n"
                           "M104 S250\n")
        self.assertTrue(result["ok"], result["reason"])
        self.assertIn("filament type = PETG", result["text"])
        self.assertIn("M104 S250", result["text"])
        self.assertNotIn("G1 X10.5", result["text"],
                         "координаты не несут смысла, а объём в сто раз больше")

    def test_binary_stl_gives_metadata(self):
        path = self.root / "деталь.stl"
        make_binary_stl(path, "адресник Мария", 1234)
        result = documents.read_document(path)
        self.assertTrue(result["ok"], result["reason"])
        self.assertIn("адресник Мария", result["text"])
        self.assertIn("1234", result["text"])

    def test_ascii_stl_counts_facets(self):
        result = self.read("flat.stl", "solid деталь\nfacet normal 0 0 1\n"
                                       "facet normal 0 1 0\nendsolid деталь\n")
        self.assertTrue(result["ok"], result["reason"])
        self.assertIn("2 треугольников", result["text"])

    def test_pdf_without_library_is_honest(self):
        result = self.read("счёт.pdf", b"%PDF-1.4\n1 0 obj\n")
        try:
            import pypdf  # noqa: F401
            has_library = True
        except ImportError:
            has_library = False
        if not has_library:
            self.assertFalse(result["ok"])
            self.assertIn("pypdf", result["reason"])
        else:
            self.assertTrue(result["reason"] or result["ok"])

    def test_photo_is_not_pretending_to_be_text(self):
        result = self.read("фото.jpg", b"\xff\xd8\xff\xe0fakejpeg")
        self.assertFalse(result["ok"])
        self.assertIn("зрением", result["reason"])

    def test_foreign_suffix_is_refused(self):
        result = self.read("archive.zip", b"PK\x03\x04fake")
        self.assertFalse(result["ok"])
        self.assertIn("не входит", result["reason"])

    def test_empty_and_missing_files(self):
        self.assertFalse(documents.read_document(self.root / "нет-такого.md")["ok"])
        empty = self.root / "empty.md"
        empty.write_text("", encoding="utf-8")
        result = documents.read_document(empty)
        self.assertFalse(result["ok"])
        self.assertEqual("Файл пуст", result["reason"])

    def test_huge_file_is_refused_with_reason(self):
        path = self.root / "big.txt"
        with path.open("wb") as handle:
            handle.write(b"x" * (documents.MAX_FILE_BYTES + 1))
        result = documents.read_document(path)
        self.assertFalse(result["ok"])
        self.assertIn("МБ", result["reason"])


class ChunkAndTokenTests(unittest.TestCase):
    def test_chunks_keep_line_numbers(self):
        text = "\n".join(f"строка {index}" for index in range(1, 400))
        chunks = documents.chunks_of(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(1, chunks[0]["first_line"])
        self.assertGreater(chunks[1]["first_line"], 1)
        self.assertEqual(list(range(len(chunks))), [chunk["seq"] for chunk in chunks])

    def test_chunks_are_bounded(self):
        chunks = documents.chunks_of("слово " * 200000)
        self.assertLessEqual(len(chunks), documents.MAX_CHUNKS)

    def test_empty_text_gives_no_chunks(self):
        self.assertEqual([], documents.chunks_of("   \n\n  "))

    def test_tokens_drop_stopwords_and_short_words(self):
        tokens = documents.tokens_of("Что сколько стоит PETG чёрный у Марии?")
        self.assertNotIn("что", tokens)
        self.assertNotIn("сколько", tokens)
        self.assertIn("petg", tokens)
        self.assertIn("марии", tokens)
        self.assertEqual(len(tokens), len(set(tokens)))

    def test_tokens_of_empty_question(self):
        self.assertEqual([], documents.tokens_of(""))
        self.assertEqual([], documents.tokens_of("… — ?"))


class WalkAndIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "docs"
        self.root.mkdir()
        self.store = Store(pathlib.Path(self.tmp.name) / "assistant.sqlite3")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def write(self, name: str, text: str) -> pathlib.Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_walk_skips_service_folders(self):
        self.write("a.md", "видимый")
        self.write(".git/config", "[core]")
        self.write("node_modules/pkg/readme.md", "чужое")
        self.write(".hidden.md", "скрытый")
        paths = {path.name for path in documents.walk([self.root])}
        self.assertIn("a.md", paths)
        self.assertNotIn("config", paths)
        self.assertNotIn("readme.md", paths)
        self.assertNotIn(".hidden.md", paths)

    def test_walk_accepts_single_file(self):
        path = self.write("one.md", "один файл")
        self.assertEqual([path], documents.walk([path]))

    def test_walk_survives_missing_folder(self):
        self.assertEqual([], documents.walk([self.root / "нет-такой-папки"]))

    def test_index_counts_new_and_unchanged(self):
        self.write("a.md", "договор с Марией")
        self.write("b.md", "чек-лист печати")
        first = documents.index([self.root], self.store)
        self.assertTrue(first["ok"])
        self.assertEqual(2, first["new"])
        self.assertEqual(0, first["unchanged"])
        second = documents.index([self.root], self.store)
        self.assertEqual(0, second["new"])
        self.assertEqual(2, second["unchanged"])

    def test_index_updates_changed_file(self):
        path = self.write("a.md", "старый текст")
        documents.index([self.root], self.store)
        path.write_text("новый текст длиннее старого", encoding="utf-8")
        result = documents.index([self.root], self.store)
        self.assertEqual(1, result["updated"])
        self.assertTrue(self.store.search(["новый"]))

    def test_index_forgets_deleted_file(self):
        path = self.write("a.md", "временный текст")
        documents.index([self.root], self.store)
        path.unlink()
        documents.index([self.root], self.store)
        self.assertEqual([], self.store.search(["временный"]))

    def test_index_reports_unreadable_with_reason(self):
        self.write("good.md", "хороший текст")
        (self.root / "photo.png").write_bytes(b"\xff\xd8\xfffake")
        result = documents.index([self.root], self.store)
        self.assertEqual(1, result["unreadable"])
        self.assertTrue(result["samples"][0]["reason"])

    def test_knowledge_folder_is_separated_from_personal(self):
        self.assertTrue(documents.is_knowledge(self.root / "чек-лист-печати.md"))
        self.assertTrue(documents.is_knowledge("/home/user/ai/docs/ПОМОЩНИК.md"))
        self.assertFalse(documents.is_knowledge(self.root / "договор-мария.md"))


class FactsTests(unittest.TestCase):
    def test_money_date_phone_email_are_found_with_place(self):
        text = ("Договор №12 от 05.10.2026\n"
                "Сумма 12 500,00 ₽ к оплате\n"
                "Телефон +7 900 123-45-67, почта maria@example.com\n"
                "Количество 20 шт\n")
        facts = documents.facts_from_text(text, "/docs/договор.md")
        kinds = {fact["kind"] for fact in facts}
        self.assertIn("сумма", kinds)
        self.assertIn("дата", kinds)
        self.assertIn("телефон", kinds)
        self.assertIn("почта", kinds)
        self.assertIn("количество", kinds)
        money = next(fact for fact in facts if fact["kind"] == "сумма")
        self.assertEqual("12500.00 ₽", money["value"])
        self.assertEqual("строка 2", money["place"])
        phone = next(fact for fact in facts if fact["kind"] == "телефон")
        self.assertEqual("+79001234567", phone["value"])

    def test_facts_are_unique(self):
        facts = documents.facts_from_text("100 ₽ и снова 100 ₽\n100 ₽")
        self.assertEqual(1, len([fact for fact in facts if fact["kind"] == "сумма"]))

    def test_facts_are_bounded(self):
        facts = documents.facts_from_text("\n".join(f"{index} ₽" for index in range(500)))
        self.assertLessEqual(len(facts), documents.MAX_FACTS)

    def test_text_without_facts_gives_empty_list(self):
        self.assertEqual([], documents.facts_from_text("просто текст без цифр"))

    def test_facts_of_file_returns_reason_for_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "photo.png"
            path.write_bytes(b"\xff\xd8\xfffake")
            result = documents.facts_of_file(path)
            self.assertFalse(result["ok"])
            self.assertTrue(result["reason"])


if __name__ == "__main__":
    unittest.main()
