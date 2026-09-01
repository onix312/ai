"""Реестр HTTP-маршрутов PrintFlow 14.0 (идеи 1, 2, 9, З3).

Зачем. Весь API жил одной if-цепочкой: `Api.get` — 918 строк и 175 веток,
`Api.post` — 1824 строки и 221 ветка, плюс отдельный диспетчер `v9_api.py`
из релиза 9.0, который вызывался первым и возвращал `None`, если путь «не
его». Найти маршрут, понять, публичный ли он, и добавить аудит можно было
только чтением тысяч строк.

Как теперь. Маршрут объявляется там, где живёт его логика::

    from .router import router

    @router.get("/api/workshop/about")
    def workshop_about(api, ctx):
        return workshop(api).about()

    @router.post("/api/workshop/shift", audit="Смена: отметка чек-листа")
    def workshop_shift(api, ctx):
        return workshop(api).shift_set(ctx.body)

Диспетчер вызывается из `Api.get`/`Api.post` ПЕРЕД legacy-цепочкой: если
маршрут есть в реестре — работает он, если нет — код идёт дальше, как
раньше. Это позволяет переносить маршруты порциями, не останавливая
систему, и держать `scripts/check.py` зелёным на каждом шаге.

Что даёт реестр, кроме порядка:
  * `public=True` — маршрут доступен без доступа к цеху (витрина, заказ,
    трекинг). Всё остальное по умолчанию приватное;
  * `roles=(...)` — минимальная роль для вызова (идея 61);
  * `audit="…"` — действие пишется в журнал одним аргументом;
  * `idempotent=True` — повтор с тем же ключом не плодит сущности (идея 5);
  * `router.reference()` — машинное описание API для справки и тестов.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Ctx:
    """Контекст одного запроса: тело, query и удобные доступы к ним."""

    body: dict = field(default_factory=dict)
    query: dict = field(default_factory=dict)
    request_id: str = ""
    started_at: float = 0.0

    def one(self, key: str, default: str = "") -> str:
        """Первое значение query-параметра (как `one` в legacy-диспетчере)."""
        values = self.query.get(key) or [default]
        return values[0] if values else default

    def arg(self, key: str, default: Any = "") -> Any:
        """Значение из тела запроса (POST) с запасным вариантом из query."""
        if isinstance(self.body, dict) and key in self.body:
            return self.body[key]
        values = self.query.get(key) or []
        return values[0] if values else default

    def num(self, key: str, default: float = 0.0) -> float:
        from .accounting import num
        return num(self.arg(key, self.one(key, "")), default)


@dataclass
class Route:
    method: str
    path: str
    handler: Callable[[Any, Ctx], Any]
    public: bool = False
    roles: tuple[str, ...] = ()
    audit: str = ""
    idempotent: bool = False
    doc: str = ""
    module: str = ""

    def describe(self) -> dict:
        return {
            "method": self.method, "path": self.path, "public": self.public,
            "roles": list(self.roles), "audit": self.audit,
            "idempotent": self.idempotent, "doc": self.doc,
            "module": self.module,
        }


class Router:
    """Реестр маршрутов. Один на процесс, заполняется импортом модулей."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Route] = {}

    def add(self, method: str, path: str, handler: Callable[[Any, Ctx], Any],
            *, public: bool = False, roles: tuple[str, ...] | list[str] = (),
            audit: str = "", idempotent: bool = False, doc: str = "") -> Route:
        route = Route(
            method=method.upper(), path=path, handler=handler, public=public,
            roles=tuple(roles), audit=audit, idempotent=idempotent,
            doc=doc or (handler.__doc__ or "").strip().splitlines()[0] if handler.__doc__ else "",
            module=getattr(handler, "__module__", ""),
        )
        key = (route.method, route.path)
        if key in self._routes:
            raise ValueError(f"Маршрут уже объявлен: {route.method} {route.path}")
        self._routes[key] = route
        return route

    def get(self, path: str, **options):
        def decorator(handler):
            self.add("GET", path, handler, **options)
            return handler
        return decorator

    def post(self, path: str, **options):
        def decorator(handler):
            self.add("POST", path, handler, **options)
            return handler
        return decorator

    def find(self, method: str, path: str) -> Route | None:
        return self._routes.get((method.upper(), path))

    def dispatch(self, api: Any, method: str, path: str, body: dict | None = None,
                 query: dict | None = None, request_id: str = "") -> tuple[int, Any] | None:
        """Выполнить маршрут. `None` — маршрута нет, вызывающий идёт дальше."""
        route = self.find(method, path)
        if route is None:
            return None
        ctx = Ctx(body=body if isinstance(body, dict) else {},
                  query=query or {}, request_id=request_id, started_at=time.time())
        result = route.handler(api, ctx)
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], int):
            return result
        return 200, result

    def routes(self) -> list[Route]:
        return sorted(self._routes.values(), key=lambda r: (r.path, r.method))

    def reference(self) -> list[dict]:
        """Машинное описание API (идея 103/12): справка и контрактные тесты."""
        return [route.describe() for route in self.routes()]

    def paths(self) -> set[str]:
        return {route.path for route in self._routes.values()}

    def count(self) -> int:
        return len(self._routes)


router = Router()


def register_module(module_name: str) -> None:
    """Импортировать модуль маршрутов, чтобы его декораторы отработали."""
    import importlib
    name = module_name if module_name.startswith(".") else "." + module_name
    importlib.import_module(name, package=__package__)


def register_all() -> int:
    """Подключить все модули маршрутов. Вызывается один раз при старте API."""
    for name in ("routes_workshop", "routes_system"):
        try:
            register_module(name)
        except Exception as exc:  # pragma: no cover - защита от частичного релиза
            from .logging_setup import log
            log().warning("Маршруты %s не зарегистрированы: %s", name, exc)
    return router.count()
