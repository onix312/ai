"""PrintFlow 19 domain action registry for Luma/Nozza.

This catalog is the contract between the PrintFlow domain and Luma.
It contains business actions, not arbitrary raw URLs. The agent receives
the catalog over loopback, selects an action id, and PrintFlow remains the
canonical executor. Legacy assistant.ACTIONS stays intact during migration.
"""
from __future__ import annotations

from typing import Any

RISK_LEVELS = ("read", "soft", "write", "physical", "financial", "irreversible")


def _a(title: str, domain: str, method: str, path: str, *,
       params: tuple[str, ...] = (), required: tuple[str, ...] = (),
       risk: str = "read", reversible: bool = True, confirm: bool = False,
       verify: str = "", doc: str = "") -> dict[str, Any]:
    if risk not in RISK_LEVELS:
        raise ValueError(f"unknown PrintFlow action risk: {risk}")
    return {
        "title": title,
        "domain": domain,
        "method": method.upper(),
        "path": path,
        "params": params,
        "required": required,
        "risk": risk,
        "reversible": bool(reversible),
        "confirm": bool(confirm),
        "verify": verify,
        "doc": doc,
    }


ACTIONS: dict[str, dict[str, Any]] = {
    # today / system
    "today.operations": _a("Что требует внимания сегодня", "today", "GET", "/api/ops/today",
                            doc="Операционные исключения и следующие действия."),
    "today.plan": _a("План производства на сегодня", "today", "GET", "/api/plan/day"),
    "today.insights": _a("Здоровье бизнеса", "today", "GET", "/api/insights"),
    "today.search": _a("Поиск по PrintFlow", "today", "GET", "/api/search",
                       params=("q",), required=("q",)),
    "system.health": _a("Состояние PrintFlow", "system", "GET", "/api/health"),
    "system.diagnostics": _a("Диагностика PrintFlow", "system", "GET",
                             "/api/diagnostics/report"),

    # printers
    "printers.list": _a("Список принтеров", "printers", "GET", "/api/printers"),
    "printers.telemetry": _a("Телеметрия принтера", "printers", "GET",
                             "/api/printer/telemetry", params=("printer_id",)),
    "printers.health": _a("Здоровье принтера", "printers", "GET",
                          "/api/printer/health", params=("printer_id",)),
    "printers.alerts": _a("Тревоги принтера", "printers", "GET",
                          "/api/printer/alerts", params=("printer_id",)),
    "printers.maintenance": _a("ТО принтера", "printers", "GET",
                               "/api/printer/maintenance", params=("printer_id",)),
    "printers.files": _a("Файлы принтера", "printers", "GET",
                         "/api/printer/files", params=("printer_id", "path")),
    "printers.command": _a(
        "Команда принтеру", "printers", "POST", "/api/printer/command",
        params=("printer_id", "command", "value"), required=("printer_id", "command"),
        risk="physical", reversible=False, confirm=True, verify="printer_state",
        doc="Pause/resume/stop/light/speed через штатный PrintFlow command gate."),
    "printers.maintenance_done": _a(
        "Отметить ТО выполненным", "printers", "POST", "/api/printer/maintenance/done",
        params=("id", "note"), required=("id",), risk="write",
        verify="maintenance_state"),

    # queue
    "queue.list": _a("Очередь печати", "queue", "GET", "/api/jobs"),
    "queue.timeline": _a("Таймлайн печати", "queue", "GET", "/api/timeline"),
    "queue.smart": _a("Умная очередь", "queue", "GET", "/api/analytics/smart-queue"),
    "queue.start": _a(
        "Запустить задание", "queue", "POST", "/api/jobs/start",
        params=("id", "printer_id"), required=("id",), risk="physical",
        reversible=False, confirm=True, verify="job_running"),
    "queue.cancel": _a(
        "Отменить задание", "queue", "POST", "/api/jobs/cancel",
        params=("id",), required=("id",), risk="physical",
        reversible=True, confirm=True, verify="job_cancelled"),

    # orders
    "orders.list": _a("Список заказов", "orders", "GET", "/api/orders"),
    "orders.get": _a("Карточка заказа", "orders", "GET", "/api/order",
                     params=("id",), required=("id",)),
    "orders.readiness": _a("Готовность заказа к производству", "orders", "GET",
                           "/api/order/readiness",
                           params=("id", "printer_id", "spool_id"), required=("id",)),
    "orders.completion": _a("Приёмка производства заказа", "orders", "GET",
                            "/api/order/completion", params=("id",), required=("id",)),
    "orders.fulfillment": _a("Готовность заказа к выдаче", "orders", "GET",
                             "/api/order/fulfillment", params=("id",), required=("id",)),
    "orders.stock": _a("Готовность заказа к складу", "orders", "GET",
                       "/api/order/stock", params=("id",), required=("id",)),
    "orders.history": _a("История заказа", "orders", "GET", "/api/order/history",
                         params=("id",), required=("id",)),
    "orders.prepare": _a(
        "Подготовить заказ к производству", "orders", "POST", "/api/order/prepare",
        params=("id", "printer_id", "spool_id"), required=("id",), risk="write",
        reversible=True, verify="order_readiness",
        doc="Создаёт или готовит очередь, но не запускает физическую печать."),
    "orders.status": _a(
        "Сменить статус заказа", "orders", "POST", "/api/order/status",
        params=("id", "status", "expected_updated_at"), required=("id", "status"),
        risk="write", reversible=True, verify="order_status"),
    "orders.to_stock": _a(
        "Принять готовый заказ на склад", "orders", "POST",
        "/api/order/stock-to-warehouse",
        params=("id", "warehouse_id", "note"), required=("id", "warehouse_id"),
        risk="write", reversible=False, confirm=True, verify="stock_balance"),
    "orders.fulfill": _a(
        "Выдать заказ клиенту", "orders", "POST", "/api/order/fulfill",
        params=("id", "handoff_confirmed", "payment_action", "account_id", "payment_method"),
        required=("id", "handoff_confirmed"), risk="financial",
        reversible=False, confirm=True, verify="order_fulfilled"),

    # customers
    "customers.list": _a("Клиенты", "customers", "GET", "/api/customers"),
    "customers.rfm": _a("RFM клиентов", "customers", "GET", "/api/clients/rfm"),
    "customers.duplicates": _a("Дубли клиентов", "customers", "GET",
                               "/api/clients/duplicates"),
    "customers.aftercare": _a("Очередь aftercare", "customers", "GET",
                              "/api/aftercare/queue"),

    # shelf
    "shelf.summary": _a("Стеллаж", "shelf", "GET", "/api/shelf"),
    "shelf.item": _a("Позиция стеллажа", "shelf", "GET", "/api/shelf/item",
                     params=("id",), required=("id",)),
    "shelf.forecast": _a("Прогноз стеллажа", "shelf", "GET",
                         "/api/shelf/forecast", params=("days",)),
    "shelf.stock_available": _a("Что можно принести со склада", "shelf", "GET",
                                "/api/shelf/stock-available", params=("goods",)),
    "shelf.transfer_in": _a(
        "Перенести товар на стеллаж", "shelf", "POST", "/api/shelf/transfer",
        params=("nom_id", "warehouse_id", "qty", "item_id", "variant_id", "note"),
        required=("nom_id", "warehouse_id", "qty"), risk="write",
        reversible=True, confirm=True, verify="shelf_stock_transfer"),
    "shelf.transfer_out": _a(
        "Вернуть товар со стеллажа на склад", "shelf", "POST", "/api/shelf/transfer-out",
        params=("item_id", "warehouse_id", "qty", "note"),
        required=("item_id", "warehouse_id", "qty"), risk="write",
        reversible=True, confirm=True, verify="shelf_stock_transfer"),
    "shelf.inventory": _a(
        "Инвентаризация стеллажа", "shelf", "POST", "/api/shelf/inventory",
        params=("item_id", "actual", "note"), required=("item_id", "actual"),
        risk="write", reversible=False, confirm=True, verify="shelf_balance"),
    "shelf.produce": _a(
        "Приход на стеллаж", "shelf", "POST", "/api/shelf/produce",
        params=("item_id", "qty", "job_id", "note", "cost_per_unit"),
        required=("item_id", "qty"), risk="write", reversible=True,
        confirm=True, verify="shelf_balance"),

    # products / stock
    "products.list": _a("Номенклатура", "products", "GET", "/api/nomenclature"),
    "products.replenishment": _a("План пополнения", "products", "GET", "/api/replenishment"),
    "products.frozen_capital": _a("Замороженный капитал", "products", "GET",
                                  "/api/nomenclature/frozen-capital"),
    "products.filament_forecast": _a("Прогноз пластика", "products", "GET",
                                     "/api/nomenclature/filament-forecast"),
    "stock.balances": _a("Остатки складов", "stock", "GET", "/api/stock"),
    "stock.turnover": _a("Оборачиваемость склада", "stock", "GET",
                         "/api/stock/turnover", params=("days",)),
    "stock.reserves": _a("Резервы склада", "stock", "GET", "/api/reserves"),
    "stock.adjust": _a(
        "Корректировка склада", "stock", "POST", "/api/stock/adjust",
        params=("nom_id", "warehouse_id", "delta", "reason", "note"),
        required=("nom_id", "warehouse_id", "delta"), risk="write",
        reversible=True, confirm=True, verify="stock_balance"),

    # materials
    "materials.spools": _a("Катушки", "materials", "GET", "/api/spools"),
    "materials.stats": _a("Расход пластика", "materials", "GET", "/api/filament-stats"),
    "materials.purchase_hint": _a("Что закупить", "materials", "GET", "/api/purchase-hint"),
    "materials.shopping": _a("Список закупок", "materials", "GET", "/api/shopping"),

    # finance
    "finance.summary": _a("Финансовая сводка", "finance", "GET", "/api/finance"),
    "finance.money": _a("Деньги и счета", "finance", "GET", "/api/money"),
    "finance.pnl": _a("P&L", "finance", "GET", "/api/pnl"),
    "finance.debts": _a("Долги клиентов", "finance", "GET", "/api/debts"),
    "finance.report": _a("Финансовый отчёт", "finance", "GET", "/api/report",
                         params=("period", "offset", "start", "end")),
    "finance.sales": _a("Реестр продаж", "finance", "GET", "/api/report/sales",
                        params=("period", "offset", "start", "end", "limit")),
    "finance.tax": _a("Налоги", "finance", "GET", "/api/tax"),
    "finance.payment": _a(
        "Записать оплату или возврат", "finance", "POST", "/api/payment/save",
        params=("order_id", "amount", "kind", "account_id", "method", "note",
                "request_id", "expected_updated_at"),
        required=("order_id", "amount", "kind"), risk="financial",
        reversible=False, confirm=True, verify="payment_ledger"),
    "finance.sbp_create": _a(
        "Создать СБП-платёж", "finance", "POST", "/api/sbp/create",
        params=("order_id", "amount", "note"), required=("amount",),
        risk="financial", reversible=True, confirm=True, verify="sbp_payment"),
    "finance.sbp_refund": _a(
        "Возврат СБП", "finance", "POST", "/api/sbp/refund",
        params=("id", "amount", "note"), required=("id", "amount"),
        risk="financial", reversible=False, confirm=True, verify="payment_ledger"),

    # analytics
    "analytics.oee": _a("OEE парка", "analytics", "GET", "/api/analytics/oee"),
    "analytics.products": _a("P&L по товарам", "analytics", "GET",
                             "/api/analytics/pnl-products"),
    "analytics.anomalies": _a("Аномалии", "analytics", "GET", "/api/analytics/anomalies"),
    "analytics.defects": _a("Аналитика брака", "analytics", "GET",
                            "/api/analytics/defects"),

    # inbox / communication
    "inbox.list": _a("Входящие обращения", "inbox", "GET", "/api/client-bot/inbox"),
    "inbox.conversations": _a("Диалоги клиентов", "inbox", "GET", "/api/conversations"),

    # FarmLoop
    "farmloop.metrics": _a("Метрики FarmLoop", "farmloop", "GET", "/api/farmloop/metrics"),
    "farmloop.digital_twin": _a("Digital Twin FarmLoop", "farmloop", "GET",
                                "/api/farmloop/digital-twin"),
}


def get(action_id: str) -> dict[str, Any] | None:
    key = str(action_id or "").strip()
    spec = ACTIONS.get(key)
    return {"id": key, **spec} if spec else None


def payload(domain: str = "") -> list[dict[str, Any]]:
    wanted = str(domain or "").strip().casefold()
    rows: list[dict[str, Any]] = []
    for action_id, spec in ACTIONS.items():
        if wanted and str(spec["domain"]).casefold() != wanted:
            continue
        rows.append({
            "id": action_id,
            "title": spec["title"],
            "domain": spec["domain"],
            "method": spec["method"],
            "path": spec["path"],
            "params": list(spec["params"]),
            "required": list(spec["required"]),
            "risk": spec["risk"],
            "reversible": spec["reversible"],
            "confirm": spec["confirm"],
            "verify": spec["verify"],
            "doc": spec["doc"],
        })
    return rows


def domains() -> list[dict[str, Any]]:
    names: dict[str, int] = {}
    for spec in ACTIONS.values():
        name = str(spec["domain"])
        names[name] = names.get(name, 0) + 1
    return [{"id": name, "actions": count} for name, count in sorted(names.items())]


def validate() -> list[str]:
    problems: list[str] = []
    for action_id, spec in ACTIONS.items():
        if not action_id or "." not in action_id:
            problems.append(f"bad action id: {action_id!r}")
        if spec["method"] not in ("GET", "POST"):
            problems.append(f"{action_id}: bad method")
        if not str(spec["path"]).startswith("/api/"):
            problems.append(f"{action_id}: bad path")
        if any(key not in spec["params"] for key in spec["required"]):
            problems.append(f"{action_id}: required param missing from params")
        if spec["method"] == "GET" and spec["confirm"]:
            problems.append(f"{action_id}: GET cannot require confirmation")
        if spec["risk"] in ("physical", "financial", "irreversible") and not spec["confirm"]:
            problems.append(f"{action_id}: dangerous action must confirm")
    return problems
