"""Спецификация OpenAPI 3 из реестра маршрутов (Н3).

Документация API вёлась руками и расходилась с кодом: маршрут переименовали,
а в описании осталось старое. Здесь спецификация собирается из того же
реестра, по которому запросы реально ходят (`router.reference()`), поэтому
разойтись она не может — новый маршрут появляется в документе сам.

Что берём из реестра:
  * метод и путь → операция;
  * `doc` → summary, первая строка докстринга обработчика;
  * `module` → тег (workshop, system, …);
  * `public` → пометка в описании: доступен без входа;
  * `idempotent` → пометка, что повтор с тем же `Idempotency-Key` безопасен;
  * `audit` → пометка, что действие пишется в журнал событий.

Схемы тел не выдумываются: их нет в реестре, а придуманная схема хуже
отсутствующей. Вместо этого в описании операции указано, откуда брать
реальный пример — `/api/diagnostics` и сами обработчики.
"""
from __future__ import annotations

import re
from typing import Any

from . import APP_VERSION

OPENAPI_VERSION = "3.0.3"

# Человекочитаемые имена тегов: модуль маршрутов → заголовок раздела.
TAG_TITLES = {
    "routes_workshop": "Цех: приход, склад, документы",
    "routes_system": "Система: диагностика, настройки, поиск",
}


def _tag_of(module: str) -> str:
    name = str(module or "").rsplit(".", 1)[-1]
    return name or "прочее"


def _operation_id(method: str, path: str) -> str:
    """Стабильный id операции: `get_api_workshop_about`.

    Точка и прочие знаки вычищаются: `operationId` уходит в имена функций
    кодогенераторов клиентов, и `get_api_openapi.json` ломал бы сборку
    на стороне потребителя спецификации.
    """
    tail = re.sub(r"[^0-9a-zA-Z]+", "_", path.strip("/")).strip("_").lower()
    return f"{method.lower()}_{tail}"


def build(routes: list[dict] | None = None, *, title: str = "PrintFlow API",
          description: str = "") -> dict[str, Any]:
    """Собрать OpenAPI-документ из описаний маршрутов."""
    from .router import router
    rows = routes if routes is not None else router.reference()

    paths: dict[str, dict] = {}
    tags_seen: set[str] = set()
    for route in rows:
        method = str(route.get("method") or "GET").lower()
        path = str(route.get("path") or "")
        if not path.startswith("/"):
            continue
        tag = _tag_of(route.get("module"))
        tags_seen.add(tag)
        notes = []
        if route.get("public"):
            notes.append("публичный маршрут — доступен без входа в панель")
        if route.get("idempotent"):
            notes.append("идемпотентен: повтор с тем же `Idempotency-Key` "
                         "вернёт первый ответ и не создаст вторую запись")
        if route.get("audit"):
            notes.append(f"действие пишется в журнал событий ({route['audit']})")
        summary = str(route.get("doc") or "").strip() or path
        operation: dict[str, Any] = {
            "operationId": _operation_id(method, path),
            "summary": summary.splitlines()[0][:200],
            "tags": [tag],
            "responses": {
                "200": {"description": "Успешный ответ в формате JSON"},
                "400": {"description": "Некорректные параметры запроса"},
                "429": {"description": "Слишком много запросов; см. заголовок Retry-After"},
                "500": {"description": "Внутренняя ошибка; в теле есть request_id"},
            },
        }
        if notes:
            operation["description"] = ". ".join(notes) + "."
        if method == "post":
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": {"type": "object"}}},
            }
        else:
            operation["parameters"] = [{
                "name": "q", "in": "query", "required": False,
                "description": "Строковые параметры передаются как query-аргументы",
                "schema": {"type": "string"},
            }]
        paths.setdefault(path, {})[method] = operation

    spec: dict[str, Any] = {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": title,
            "version": APP_VERSION,
            "description": description or (
                "Спецификация собрана из реестра маршрутов PrintFlow, поэтому "
                "совпадает с фактическим поведением сервера. Схемы тел не "
                "описаны: источник правды — обработчики маршрутов."),
        },
        "servers": [{"url": "/", "description": "Локальный коннектор PrintFlow"}],
        "tags": [{"name": tag, "description": TAG_TITLES.get(tag, "")}
                 for tag in sorted(tags_seen)],
        "paths": dict(sorted(paths.items())),
    }
    return spec


def counts(spec: dict) -> dict[str, int]:
    """Сколько операций в документе — для тестов и диагностики."""
    total = 0
    by_method: dict[str, int] = {}
    for methods in (spec.get("paths") or {}).values():
        for method in methods:
            total += 1
            by_method[method] = by_method.get(method, 0) + 1
    return {"operations": total, **by_method}
