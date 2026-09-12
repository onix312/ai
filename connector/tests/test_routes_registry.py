"""Реестр маршрутов: справка `docs/МАРШРУТЫ.md` не должна расходиться с кодом.

Три контракта:

1. **Числа.** Сколько маршрутов и в каких файлах — записано в справке и
   сверяется с кодом. Новый маршрут без правки справки тест не пропустит:
   иначе реестр гниёт так же, как гнила рукописная документация API.
2. **Тени.** Путь, объявленный и декоратором, и if-цепочкой, во втором месте
   мёртв: `Api.get()`/`Api.post()` сначала спрашивают реестр (`api.py:988`,
   `api.py:2018`). Так молча умер `/api/search` в `api.py` — до него не
   доходила очередь, а фронт читал его старый плоский ответ.
3. **Фронт не зовёт пустоту.** Каждый `/api/…`, который набирают страницы и
   скрипты `site/`, обязан существовать в сервере.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow.api import router  # noqa: E402

REGISTRY_DOC = ROOT / "docs" / "МАРШРУТЫ.md"
CHAIN_FILES = ("api.py", "http_handler.py")
PATH_RE = re.compile(r'"(/api/[^"]+)"')


def chain_routes() -> dict[str, set[tuple[str, str]]]:
    """Пары (метод, путь) из if-цепочек: метод — из объемлющего def get/post."""
    per_file: dict[str, set[tuple[str, str]]] = {}
    for name in CHAIN_FILES:
        found: set[tuple[str, str]] = set()
        method = "GET"
        for line in (ROOT / "connector" / "printflow" / name).read_text(
                encoding="utf-8").splitlines():
            head = re.match(r"    def (get|post)\(", line)
            if head:
                method = head.group(1).upper()
            if re.search(r"if path (==|in )", line):
                for path in PATH_RE.findall(line):
                    found.add((method, path))
        per_file[name] = found
    return per_file


def decorator_routes() -> dict[str, set[str]]:
    """Пары (метод, путь) живого реестра, сгруппированные по модулю."""
    per_module: dict[str, set[str]] = {}
    for route in router.reference():
        module = pathlib.Path(str(route["module"]).split(".")[-1] + ".py").name
        per_module.setdefault(module, set()).add((route["method"], route["path"]))
    return per_module


def front_paths() -> dict[str, set[str]]:
    """Пути `/api/…`, которые набирает фронт."""
    files = list((ROOT / "site").glob("*.html")) \
        + list((ROOT / "site" / "assets").glob("*.js")) \
        + list((ROOT / "site" / "assets" / "dist").glob("*.js"))
    found: dict[str, set[str]] = {}
    pattern = re.compile(r"""['"`](/api/[A-Za-z0-9_\-/.]+)['"`]""")
    for file in files:
        for path in pattern.findall(file.read_text(encoding="utf-8", errors="ignore")):
            found.setdefault(path, set()).add(str(file.relative_to(ROOT)))
    return found


def _all_paths_in(dirs) -> set[str]:
    """Все `/api/…`, которые встречаются в перечисленных каталогах."""
    pattern = re.compile(r"/api/[A-Za-z0-9_\-/.]+")
    found: set[str] = set()
    for folder in dirs:
        for file in (ROOT / folder).rglob("*"):
            if file.suffix not in (".js", ".html", ".py", ".kt") or not file.is_file():
                continue
            for match in pattern.findall(file.read_text(encoding="utf-8", errors="ignore")):
                found.add(match.rstrip("."))
    return found


class RoutesInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chain = chain_routes()
        cls.decorators = decorator_routes()

    def test_doc_lists_every_route_file(self):
        text = REGISTRY_DOC.read_text(encoding="utf-8")
        rows = dict(re.findall(r"\| `(connector/printflow/[a-z_0-9]+\.py)` \|[^|]+\| (\d+) \|",
                               text))
        self.assertTrue(rows, "в docs/МАРШРУТЫ.md не найдена таблица счёта маршрутов")
        self.assertEqual(
            set(rows),
            {f"connector/printflow/{n}" for n in CHAIN_FILES}
            | {f"connector/printflow/{n}" for n in self.decorators},
            "список файлов в справке разошёлся с кодом")

    def test_doc_counts_match_code(self):
        text = REGISTRY_DOC.read_text(encoding="utf-8")
        rows = dict(re.findall(r"\| `(connector/printflow/[a-z_0-9]+\.py)` \|[^|]+\| (\d+) \|",
                               text))
        actual = {f"connector/printflow/{n}": len(v) for n, v in self.chain.items()}
        actual.update({f"connector/printflow/{n}": len(v)
                       for n, v in self.decorators.items()})
        self.assertEqual({k: int(v) for k, v in rows.items()}, actual,
                         "посчитайте маршруты заново и обновите docs/МАРШРУТЫ.md")
        total = re.search(r"\| \*\*Всего\*\* \| \| \*\*(\d+)\*\* \|", text)
        self.assertIsNotNone(total, "в справке нет строки «Всего»")
        self.assertEqual(int(total.group(1)), sum(actual.values()))

    def test_no_if_chain_is_shadowed_by_registry(self):
        registered = {(r["method"], r["path"]) for r in router.reference()}
        shadowed = sorted(
            f"{method} {path}"
            for found in self.chain.values()
            for method, path in found
            if (method, path) in registered)
        self.assertEqual([], shadowed,
                         "эти пути объявлены и декоратором, и if-цепочкой: ветка в "
                         "цепочке недостижима, перенесите её в реестр или удалите")

    def test_unrequested_route_counts_are_current(self):
        """Числа из приложения «непрошеные маршруты» сверяются с кодом."""
        registered = {(r["method"], r["path"]) for r in router.reference()}
        known = {path for _, path in registered}
        for found in self.chain.values():
            known |= {path for _, path in found}
        # Здесь — тот же широкий поиск, что в скрипте из приложения: путь
        # считается упомянутым, если встречается в файле хоть в кавычках,
        # хоть внутри шаблонной строки. Узкий front_paths() (только literals)
        # нужен для другого контракта и дал бы другие числа.
        front = _all_paths_in(("site",))
        callers = front | _all_paths_in(("connector/tests", "scripts", "android"))
        text = REGISTRY_DOC.read_text(encoding="utf-8")
        listed = {int(n) for n in re.findall(r"\*\*(\d+)\*\*", text)}
        for label, value in (("всего путей", len(known)),
                             ("не зовёт site/", len([p for p in known if p not in front])),
                             ("не упоминаются нигде", len([p for p in known if p not in callers]))):
            self.assertIn(value, listed,
                          f"в docs/МАРШРУТЫ.md нет числа {value} ({label}) — "
                          "пересчитайте приложение и обновите его")

    def test_frontend_only_calls_existing_routes(self):
        registered = {(r["method"], r["path"]) for r in router.reference()}
        known = {path for _, path in registered}
        for found in self.chain.values():
            known |= {path for _, path in found}
        missing = {path: sorted(files) for path, files in front_paths().items()
                   if path not in known}
        self.assertEqual({}, missing,
                         "фронт зовёт маршруты, которых нет в сервере")


class SearchRouteContractTests(unittest.TestCase):
    """/api/search и палитра команд обязаны говорить на одном языке.

    Живой маршрут объявлен декоратором и отдаёт группы; недосягаемая ветка в
    `api.py` отдавала плоский `results`. Палитра читала плоский — и удалённый
    поиск молча возвращал ноль строк.
    """

    def test_server_returns_grouped_shape(self):
        from connector.printflow.db import Database
        from connector.printflow.search import Search
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db = Database(pathlib.Path(folder.name) / "search.sqlite3")
        self.addCleanup(db.close)
        db.upsert("customers", {"id": "c1", "name": "Анна Коробка", "phone": "1"})
        result = Search(db).run("короб", 8)
        self.assertIn("groups", result)
        self.assertNotIn("results", result)
        self.assertTrue(any(g["kind"] == "customers" for g in result["groups"]),
                        f"клиент не найден: {result}")

    def test_palette_reads_grouped_shape(self):
        text = (ROOT / "site" / "assets" / "core.js").read_text(encoding="utf-8")
        block = text.split("const searchRemote", 1)[1].split("}, 220);", 1)[0]
        self.assertIn("data.groups", block)
        self.assertNotIn("data.results", block,
                         "палитра снова читает плоский ответ, которого нет")


if __name__ == "__main__":
    unittest.main()
