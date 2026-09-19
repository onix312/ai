"""GET-маршруты, переехавшие из if-цепочки в реестр: контракт на перенос.

Перенос механический, поэтому главный риск — не логика, а два тихих дефекта:
ветка if-цепочки, оставшаяся рядом с декоратором (она мертва: диспетчер
спрашивает реестр первым), и маршрут, который не попал в спецификацию API.
Здесь проверяется и то, и другое для каждого перенесённого пути.
"""
from __future__ import annotations

import pathlib
import re
import unittest

from connector.printflow import router as router_module

ROOT = pathlib.Path(__file__).resolve().parents[2]
API_SOURCE = (ROOT / "connector" / "printflow" / "api.py").read_text(encoding="utf-8")

# Сколько маршрутов перенесено. Число фиксируется намеренно: следующая порция
# должна изменить его явно, а не «само получилось».
PORTED_COUNT = 159


def ported_routes() -> list[dict]:
    router_module.register_all()
    return [r for r in router_module.router.reference()
            if r["module"].endswith("routes_get")]


def get_dispatcher_region() -> str:
    """Тело `Api.get()` — до `Api.post()`, где пути повторяются законно."""
    start = API_SOURCE.index("    def get(self, path: str, query: dict)")
    end = API_SOURCE.index("    def post(self, path: str, body: dict")
    return API_SOURCE[start:end]


class PortedGetRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = ported_routes()
        cls.region = get_dispatcher_region()

    def test_expected_number_of_ported_routes(self):
        self.assertEqual(PORTED_COUNT, len(self.routes),
                         "число перенесённых маршрутов изменилось — обновите"
                         " PORTED_COUNT и справку docs/МАРШРУТЫ.md")

    def test_no_dead_if_branch_left_behind(self):
        dead = [r["path"] for r in self.routes
                if f'if path == "{r["path"]}":' in self.region]
        self.assertEqual([], dead,
                         "ветка if-цепочки осталась и теперь мертва")

    def test_every_ported_route_is_in_the_spec(self):
        from connector.printflow import openapi as service
        paths = service.build().get("paths") or {}
        missing = [r["path"] for r in self.routes if r["path"] not in paths]
        self.assertEqual([], missing, "перенесённый маршрут не попал в спецификацию")

    def test_handlers_do_not_touch_self_or_legacy_one(self):
        """Тело должно читать `api.` и `ctx.one(` — иначе NameError в рантайме."""
        source = (ROOT / "connector" / "printflow" / "routes_get.py").read_text(encoding="utf-8")
        bodies = source.split("@router.get(", 1)[1]
        # Именно `\bself\b`, а не `self.`: `getattr(self, "…")` точкой не
        # выдаёт себя, и первая версия контракта такое пропустила — два
        # маршрута упали в 500 уже на живом стенде.
        self.assertNotRegex(bodies, r"\bself\b", "осталось обращение к self")
        self.assertNotRegex(bodies, r"(?<![.\w])one\(",
                            "остался legacy-замыкатель one() — нужен ctx.one()")

    def test_same_path_is_not_declared_twice(self):
        router_module.register_all()
        declared = [(r["method"], r["path"]) for r in router_module.router.reference()]
        duplicates = {key for key in declared if declared.count(key) > 1}
        self.assertEqual(set(), duplicates)


class DispatcherShrinksTests(unittest.TestCase):
    """Разрезание диспетчера должно быть видно в числах, а не на глаз."""

    def test_if_chains_in_api_are_below_the_documented_count(self):
        count = 0
        for line in API_SOURCE.splitlines():
            if re.search(r"if path (==|in )", line):
                count += len(re.findall(r'"(/api/[^"]+)"', line))
        # 18.4: +1 за очередь хотелок GET /api/wish/queue; 18.8: +1 за
        # POST /api/watch/ensure (создание папки Watch Folder, в справке 246).
        self.assertLessEqual(count, 246,
                             "if-цепочек в api.py больше, чем записано в справке")


if __name__ == "__main__":
    unittest.main()
