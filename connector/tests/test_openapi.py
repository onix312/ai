"""OpenAPI-спецификация из реестра маршрутов (Н3).

Документ собирается из `router.reference()`, а не пишется руками, поэтому
не может разойтись с фактическим поведением сервера. Тесты ниже фиксируют
именно это: состав операций, уникальность id, теги по модулям и устойчивость
сборки к пустому или повреждённому входу.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "connector"))

from connector.printflow import APP_VERSION, openapi  # noqa: E402
from connector.printflow.api import register_routes  # noqa: E402
from connector.printflow.router import router  # noqa: E402


class OpenApiShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        register_routes()
        cls.spec = openapi.build()

    def test_document_is_valid_openapi_3(self):
        self.assertEqual("3.0.3", self.spec["openapi"])
        self.assertTrue(self.spec["info"]["title"])
        self.assertEqual(APP_VERSION, self.spec["info"]["version"])

    def test_every_registered_route_is_documented(self):
        expected = {(r["method"].lower(), r["path"]) for r in router.reference()}
        documented = {(method, path) for path, methods in self.spec["paths"].items()
                      for method in methods}
        self.assertEqual(expected, documented)

    def test_counts_match_document(self):
        summary = openapi.counts(self.spec)
        self.assertEqual(len(router.reference()), summary["operations"])
        self.assertEqual(summary["operations"],
                         sum(v for k, v in summary.items() if k != "operations"))

    def test_operation_ids_are_unique(self):
        ids = [op["operationId"] for methods in self.spec["paths"].values()
               for op in methods.values()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_operation_ids_are_identifier_safe(self):
        """Id с точкой ломает кодогенераторы клиентов."""
        for methods in self.spec["paths"].values():
            for op in methods.values():
                self.assertRegex(op["operationId"], r"^[a-z][a-z0-9_]*$",
                                 op["operationId"])

    def test_every_operation_declares_error_responses(self):
        for path, methods in self.spec["paths"].items():
            for method, op in methods.items():
                for code in ("200", "400", "500"):
                    self.assertIn(code, op["responses"], f"{method} {path}")

    def test_summary_falls_back_to_path(self):
        spec = openapi.build(routes=[{"method": "GET", "path": "/api/x", "doc": ""}])
        self.assertEqual("/api/x", spec["paths"]["/api/x"]["get"]["summary"])

    def test_tags_follow_route_modules(self):
        modules = {r["module"].split(".")[-1] for r in router.reference()}
        tags = {tag["name"] for tag in self.spec["tags"]}
        self.assertEqual(modules, tags)
        for tag in self.spec["tags"]:
            self.assertTrue(tag["description"], f"{tag['name']}: нет описания")

    def test_idempotent_route_is_marked(self):
        """Повтор с тем же Idempotency-Key не должен создавать вторую запись."""
        route = next(r for r in router.reference() if r.get("idempotent"))
        spec = openapi.build(routes=[route])
        operation = spec["paths"][route["path"]][route["method"].lower()]
        self.assertIn("Idempotency-Key", operation["description"])

    def test_public_route_is_marked(self):
        """Публичный маршрут обязан быть помечен как доступный без входа."""
        spec = openapi.build(routes=[{"method": "GET", "path": "/api/track",
                                      "public": True, "doc": "Статус заказа"}])
        operation = spec["paths"]["/api/track"]["get"]
        self.assertIn("публичный", operation["description"])

    def test_audit_route_mentions_journal(self):
        route = next(r for r in router.reference() if r.get("audit"))
        spec = openapi.build(routes=[route])
        operation = spec["paths"][route["path"]][route["method"].lower()]
        self.assertIn(route["audit"], operation["description"])

    def test_post_route_declares_request_body(self):
        spec = openapi.build(routes=[{"method": "POST", "path": "/api/x"}])
        self.assertIn("requestBody", spec["paths"]["/api/x"]["post"])

    def test_same_path_with_two_methods_is_not_collapsed(self):
        """/api/workshop/scrap есть и в GET, и в POST — это две операции."""
        spec = openapi.build(routes=[
            {"method": "GET", "path": "/api/workshop/scrap", "doc": "Список"},
            {"method": "POST", "path": "/api/workshop/scrap", "doc": "Создать"},
        ])
        self.assertEqual({"get", "post"}, set(spec["paths"]["/api/workshop/scrap"]))
        self.assertEqual(2, openapi.counts(spec)["operations"])


class OpenApiRobustnessTests(unittest.TestCase):
    def test_empty_route_list_builds_empty_document(self):
        spec = openapi.build(routes=[])
        self.assertEqual({}, spec["paths"])
        self.assertEqual({"operations": 0}, openapi.counts(spec))

    def test_routes_without_leading_slash_are_skipped(self):
        spec = openapi.build(routes=[{"method": "GET", "path": "api/no-slash"}])
        self.assertEqual({}, spec["paths"])

    def test_missing_method_defaults_to_get(self):
        spec = openapi.build(routes=[{"path": "/api/x"}])
        self.assertIn("get", spec["paths"]["/api/x"])

    def test_counts_tolerates_missing_paths(self):
        self.assertEqual({"operations": 0}, openapi.counts({}))


if __name__ == "__main__":
    unittest.main()
