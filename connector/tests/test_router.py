"""Реестр маршрутов: объявление, диспетчеризация, контракт «нет SQL в роутах».

Идея 1 перенесла часть обработчиков из `api.py` в модули `routes_*`, а идея 2
запретила им содержать SQL: маршрут — это транспорт (путь, публичность,
аудит), запросы к базе живут в сервисах. Этот тест держит обе границы.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import routes_system, routes_workshop  # noqa: E402,F401
from connector.printflow.api import register_routes  # noqa: E402
from connector.printflow.router import Ctx, Router, router  # noqa: E402

PACKAGE = ROOT / "connector" / "printflow"
ROUTE_MODULES = sorted(PACKAGE.glob("routes_*.py"))


class RouterRegistryTests(unittest.TestCase):
    def setUp(self):
        register_routes()

    def test_routes_are_registered(self):
        """Реестр заполнен импортом модулей маршрутов."""
        self.assertGreaterEqual(router.count(), 28)
        for module in ("routes_workshop", "routes_system"):
            self.assertTrue(any(r.module.endswith(module) for r in router.routes()),
                            f"{module} не зарегистрировал ни одного маршрута")

    def test_every_route_has_method_path_and_handler(self):
        for route in router.routes():
            self.assertIn(route.method, ("GET", "POST"), route.path)
            self.assertTrue(route.path.startswith("/api/"), route.path)
            self.assertTrue(callable(route.handler), route.path)

    def test_find_and_dispatch(self):
        """Диспетчер возвращает (код, тело) и не трогает чужие методы."""
        local = Router()

        @local.get("/api/probe")
        def probe(api, ctx):
            """Проба."""
            return {"echo": ctx.one("q"), "flag": ctx.arg("flag")}

        self.assertIsNone(local.dispatch(None, "POST", "/api/probe"))
        status, body = local.dispatch(None, "GET", "/api/probe", query={"q": "1"})
        self.assertEqual((status, body), (200, {"echo": "1", "flag": ""}))

    def test_handler_may_return_status_pair(self):
        local = Router()

        @local.post("/api/probe2")
        def probe2(api, ctx):
            return 201, {"created": True}

        self.assertEqual(local.dispatch(None, "POST", "/api/probe2"),
                         (201, {"created": True}))

    def test_duplicate_route_is_rejected(self):
        """Два обработчика на один путь — ошибка на старте, а не тихая замена."""
        local = Router()
        local.add("GET", "/api/once", lambda api, ctx: {})
        with self.assertRaises(ValueError):
            local.add("GET", "/api/once", lambda api, ctx: {})

    def test_reference_describes_contract(self):
        rows = router.reference()
        self.assertEqual(len(rows), router.count())
        for row in rows:
            self.assertEqual({"method", "path", "public", "roles", "idempotent",
                              "audit", "doc", "module"}, set(row))

    def test_ctx_helpers_coerce_values(self):
        """query приходит списками (parse_qs), тело — готовыми значениями."""
        ctx = Ctx(body={"n": "12.5", "s": 7}, query={"q": ["текст"]})
        self.assertEqual(ctx.one("q"), "текст")
        self.assertEqual(ctx.one("missing", "запас"), "запас")
        self.assertEqual(ctx.num("n"), 12.5)
        self.assertEqual(ctx.num("missing", 3), 3)
        self.assertEqual(ctx.arg("s"), 7)
        self.assertEqual(ctx.arg("q"), "текст")   # тело пусто → берём query


class RoutesHaveNoSqlTests(unittest.TestCase):
    """Идея 2: модули маршрутов не содержат SQL — только вызовы сервисов."""

    SQL_KEYWORDS = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE",
                    "PRAGMA ", "DROP TABLE")

    def test_route_modules_are_sql_free(self):
        offenders: list[str] = []
        for path in ROUTE_MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                text = " ".join(node.value.split()).upper()
                if any(text.startswith(kw) or f" {kw}" in text for kw in self.SQL_KEYWORDS):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual([], offenders,
                         "SQL в модуле маршрутов — вынесите запрос в сервис: "
                         + ", ".join(offenders))

    def test_route_modules_do_not_import_db_internals(self):
        """Маршрут не должен строить Database/Repo сам — их даёт api."""
        for path in ROUTE_MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "db":
                    self.fail(f"{path.name} импортирует db напрямую")


if __name__ == "__main__":
    unittest.main()
