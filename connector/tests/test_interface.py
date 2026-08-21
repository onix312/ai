"""Автотесты интерфейса без браузера (роадмап 10.11).

Проверяется то, что ломает панель незаметно для человека:

* каждая страница сайта отдаётся с кодом 200;
* в HTML нет повторяющихся id (роутер и скрипты ломаются молча);
* все локальные ассеты, на которые ссылаются страницы, существуют;
* синтаксис JavaScript валиден (node --check, если node установлен);
* каждый пункт меню (`data-view`) ведёт на существующий раздел index.html.
"""
from __future__ import annotations

import html.parser
import pathlib
import shutil
import subprocess
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.api import Handler  # noqa: E402

PAGES = ["/", "/index.html", "/order.html", "/shelf.html", "/spool.html",
         "/design.html", "/track.html", "/labels.html", "/m.html",
         "/manifest.webmanifest", "/sw.js"]

# Файлы, чей id-набор не обязан быть уникальным глобально (вставки/демо).
ID_DUP_OK = {"demo"}


class Recorder:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    def flush(self) -> None:
        pass

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)


def serve(path: str) -> tuple[int, dict, bytes]:
    handler = Handler.__new__(Handler)
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.server = SimpleNamespace(flags=[])
    handler.close_connection = False
    handler.wfile = Recorder()
    handler.api = SimpleNamespace(last_host="")
    state = {"code": 0, "headers": {}}
    handler.send_response = lambda code, *_: state.update(code=code)
    handler.send_header = lambda key, value: state["headers"].__setitem__(key, value)
    handler.end_headers = lambda: None
    handler.send_error = lambda code, *_a, **_kw: state.update(code=code)
    handler.serve_static(path)
    return state["code"], state["headers"], handler.wfile.body


class _PageParser(html.parser.HTMLParser):
    """Собирает id, ссылки на ассеты, цели data-view и формы с id-элементами."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.ids: list[str] = []
        self.assets: list[str] = []
        self.view_targets: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id") or ""
        classes = set((attributes.get("class") or "").split())
        if element_id:
            self.ids.append(element_id)
        if "view" in classes and element_id:
            self.view_targets.append(element_id)
        for key in ("href", "src"):
            value = attributes.get(key) or ""
            if not value or value.startswith(("#", "http:", "https:", "mailto:", "data:")):
                continue
            clean = value.split("?")[0].split("#")[0]
            if not clean:
                continue
            if clean.startswith("/") or clean.startswith("assets/") or not clean.startswith(".."):
                if clean.startswith("/"):
                    clean = clean[1:]
                self.assets.append(clean)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def parse_page(body: bytes) -> _PageParser:
    parser = _PageParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    return parser


class TestAllPagesServe(unittest.TestCase):
    def test_every_page_returns_200(self):
        for page in PAGES:
            with self.subTest(page=page):
                code, _, body = serve(page)
                self.assertEqual(code, 200, f"{page} должен открываться")
                self.assertGreater(len(body), 100, f"{page} не должен быть пустым")


class TestNoDuplicateIds(unittest.TestCase):
    def test_no_duplicate_ids_in_pages(self):
        for page in PAGES:
            if not page.endswith(".html") and page != "/":
                continue
            with self.subTest(page=page):
                _, _, body = serve(page)
                parser = parse_page(body)
                seen: dict[str, int] = {}
                for element_id in parser.ids:
                    if element_id in ID_DUP_OK:
                        continue
                    seen[element_id] = seen.get(element_id, 0) + 1
                duplicates = {k: v for k, v in seen.items() if v > 1}
                self.assertEqual(
                    {}, duplicates,
                    f"{page}: повторяющиеся id ломают скрипты и роутер")


class TestLocalAssetsExist(unittest.TestCase):
    def test_referenced_assets_exist(self):
        for page in PAGES:
            with self.subTest(page=page):
                _, _, body = serve(page)
                parser = parse_page(body)
                for asset in parser.assets:
                    target = SITE / asset
                    self.assertTrue(target.is_file(),
                                    f"{page}: ассет {asset} не найден")


class TestJavaScriptSyntax(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node не установлен — проверка пропущена")
    def test_all_js_files_are_valid(self):
        files = sorted((SITE / "assets").glob("*.js")) + [SITE / "sw.js"]
        self.assertTrue(files, "JS-файлов не найдено")
        for js in files:
            with self.subTest(js=js.name):
                result = subprocess.run(
                    ["node", "--check", str(js)],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(0, result.returncode,
                                 f"{js.name}: {result.stderr.strip()}")


class TestNavigationTargetsExist(unittest.TestCase):
    def test_menu_views_exist_in_index(self):
        _, _, body = serve("/")
        parser = parse_page(body)
        # data-view — цель для роутера: у каждого должен быть элемент с таким id
        for target in parser.view_targets:
            self.assertIn(
                target, parser.ids,
                f"data-view={target} не имеет раздела с таким id в index.html")


class TestOrderFileButtonsWired(unittest.TestCase):
    """Кнопки файла печати в карточке заказа.

    Однажды эти кнопки появились в HTML без обработчиков в JS и молча
    не работали. Тест следит за обоими концами проводки: элементы есть
    в разметке, и ops.js их навешивает.
    """

    BUTTONS = {
        "of_file_pick": "«Выбрать файл» — выбрать 3MF/G-code с компьютера",
        "of_file_pull": "«Скачать с принтера» — файл с SD-карты в uploads",
        "of_file_download": "«На диск» — копия из uploads на компьютер",
    }

    def test_buttons_exist_in_index(self):
        _, _, body = serve("/")
        parser = parse_page(body)
        for element_id in list(self.BUTTONS) + ["of_file_local", "pull_file_modal", "pull_file_list"]:
            self.assertIn(element_id, parser.ids,
                          f"index.html: нет элемента с id={element_id}")

    def test_ops_js_wires_every_button(self):
        js = (SITE / "assets" / "ops.js").read_text(encoding="utf-8")
        for element_id in self.BUTTONS:
            # ищем именно привязку обработчика, а не простое упоминание id
            self.assertRegex(
                js, rf"\$\('{element_id}'\)\s*&&\s*\$\('{element_id}'\)\.addEventListener"
                   rf"|\$\('{element_id}'\)\.addEventListener",
                f"ops.js: кнопка {element_id} не имеет обработчика")
        self.assertIn("$('of_file_local').addEventListener('change'", js.replace(" ", ""),
                      "ops.js: выбор файла с компьютера не обрабатывается")
        self.assertIn("pickOrderFile(file)", js.replace(" ", ""),
                      "ops.js: нет вызова pickOrderFile")
        self.assertIn("pullOrderFile()", js.replace(" ", ""),
                      "ops.js: нет вызова pullOrderFile")
        self.assertIn("downloadOrderFile()", js.replace(" ", ""),
                      "ops.js: нет вызова downloadOrderFile")
        self.assertIn("[data-pull-file]", js,
                      "ops.js: список файлов принтера не обрабатывает выбор")

    def test_order_modal_keeps_file_input(self):
        _, _, body = serve("/")
        html = body.decode("utf-8", errors="replace")
        self.assertIn('accept=".3mf,.gcode,.gcode.3mf"', html,
                      "index.html: input файла потерял допустимые расширения")


if __name__ == "__main__":
    unittest.main()
