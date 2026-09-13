"""GET-маршруты, перенесённые из if-цепочки `Api.get()` (17.0.21).

Перенос механический: тело обработчика — то же выражение, что было в ветке,
отличаются только обращения (`self.` → `api.`, `one(` → `ctx.one(`). Так
маршрут попадает в реестр и в спецификацию API, а поведение не меняется:
сверено прогоном всех перенесённых путей на живом стенде до и после.

Описания механические (`GET <путь>`): содержательные подписи появятся
вместе с разбором доменов — выдумывать их сейчас нельзя.
"""
from __future__ import annotations

import time
from typing import Any

from . import APP_VERSION
from .accounting import num
from .bambu import BambuPrinter
from .config import now_iso
from .http_helpers import safe_file
from .router import Ctx, router


@router.get("/api/health", doc="GET /api/health")
def get_health(api: Any, ctx: Ctx):
    return 200, {"ok": True, "version": APP_VERSION,
                 "uptime": round(time.time() - api.started_at)}


@router.get("/api/month-close", doc="GET /api/month-close")
def get_month_close(api: Any, ctx: Ctx):
    return 200, api.month_close.state(ctx.one("key"))


@router.get("/api/catalog/recalc", doc="GET /api/catalog/recalc")
def get_catalog_recalc(api: Any, ctx: Ctx):
    return 200, api.acc.recalc_catalog(False)


@router.get("/api/bootstrap", doc="GET /api/bootstrap")
def get_bootstrap(api: Any, ctx: Ctx):
    return 200, {
        "version": APP_VERSION,
        "settings": api.db.settings(),
        "printers": api.repo.printers(),
        "statuses": api.repo.statuses(),
        "niches": api.repo.niches(),
        "summary": api.acc.summary(30),
        "state": api.manager.snapshot(),
    }


@router.get("/api/state", doc="GET /api/state")
def get_state(api: Any, ctx: Ctx):
    return 200, api.manager.snapshot(ctx.one("printer_id"))


@router.get("/api/cloud/status", doc="GET /api/cloud/status")
def get_cloud_status(api: Any, ctx: Ctx):
    return 200, api.cloud_status()


@router.get("/api/printer/cloud-files", doc="GET /api/printer/cloud-files")
def get_printer_cloud_files(api: Any, ctx: Ctx):
    return 200, {"tasks": api.cloud_tasks(ctx.one("printer_id"))}


@router.get("/api/wall", doc="GET /api/wall")
def get_wall(api: Any, ctx: Ctx):
    return 200, api.manager.wall()


@router.get("/api/order/readiness", doc="GET /api/order/readiness")
def get_order_readiness(api: Any, ctx: Ctx):
    return 200, api.production.readiness(
        ctx.one("id"), ctx.one("printer_id"), ctx.one("spool_id"))


@router.get("/api/order/completion", doc="GET /api/order/completion")
def get_order_completion(api: Any, ctx: Ctx):
    return 200, api.completion.summary(ctx.one("id"))


@router.get("/api/order/fulfillment", doc="GET /api/order/fulfillment")
def get_order_fulfillment(api: Any, ctx: Ctx):
    return 200, api.fulfillment.summary(ctx.one("id"))


@router.get("/api/order/stock", doc="GET /api/order/stock")
def get_order_stock(api: Any, ctx: Ctx):
    return 200, api.stocker.summary(ctx.one("id"))


@router.get("/api/debt/summary", doc="GET /api/debt/summary")
def get_debt_summary(api: Any, ctx: Ctx):
    return 200, api.receivables.summary(ctx.one("id"))


@router.get("/api/aftercare/queue", doc="GET /api/aftercare/queue")
def get_aftercare_queue(api: Any, ctx: Ctx):
    return 200, api.aftercare.queue(int(num(ctx.one("limit", "80"), 80)))


@router.get("/api/aftercare/summary", doc="GET /api/aftercare/summary")
def get_aftercare_summary(api: Any, ctx: Ctx):
    return 200, api.aftercare.summary(ctx.one("id"))


@router.get("/api/customers", doc="GET /api/customers")
def get_customers(api: Any, ctx: Ctx):
    return 200, {"customers": api.repo.customers()}


@router.get("/api/statuses", doc="GET /api/statuses")
def get_statuses(api: Any, ctx: Ctx):
    return 200, {"statuses": api.repo.statuses()}


@router.get("/api/niches", doc="GET /api/niches")
def get_niches(api: Any, ctx: Ctx):
    return 200, {"niches": api.repo.niches()}


@router.get("/api/spools", doc="GET /api/spools")
def get_spools(api: Any, ctx: Ctx):
    return 200, {"spools": api.repo.spools(ctx.one("all") == "1")}


@router.get("/api/catalog", doc="GET /api/catalog")
def get_catalog(api: Any, ctx: Ctx):
    return 200, {"catalog": api.repo.catalog()}


@router.get("/api/transactions", doc="GET /api/transactions")
def get_transactions(api: Any, ctx: Ctx):
    return 200, {"transactions": api.repo.transactions(int(num(ctx.one("limit", "200"), 200)))}


@router.get("/api/money", doc="GET /api/money")
def get_money(api: Any, ctx: Ctx):
    return 200, api.acc.money_state(int(num(ctx.one("months", "6"), 6)))


@router.get("/api/pnl", doc="GET /api/pnl")
def get_pnl(api: Any, ctx: Ctx):
    return 200, api.acc.pnl(int(num(ctx.one("months", "6"), 6)))


@router.get("/api/tax", doc="GET /api/tax")
def get_tax(api: Any, ctx: Ctx):
    return 200, api.acc.tax_report(int(num(ctx.one("year", "0"), 0)))


@router.get("/api/break-even", doc="GET /api/break-even")
def get_break_even(api: Any, ctx: Ctx):
    return 200, api.acc.break_even()


@router.get("/api/report", doc="GET /api/report")
def get_report(api: Any, ctx: Ctx):
    return 200, api.acc.report(ctx.one("period", "month"),
                                int(num(ctx.one("offset", "0"), 0)))


@router.get("/api/report/sales", doc="GET /api/report/sales")
def get_report_sales(api: Any, ctx: Ctx):
    return 200, api.acc.sales_details(
        ctx.one("period", "month"), int(num(ctx.one("offset", "0"), 0)),
        int(num(ctx.one("limit", "500"), 500)))


@router.get("/api/debts", doc="GET /api/debts")
def get_debts(api: Any, ctx: Ctx):
    return 200, api.acc.debts()


@router.get("/api/accounts", doc="GET /api/accounts")
def get_accounts(api: Any, ctx: Ctx):
    return 200, {"accounts": api.repo.accounts(),
                 "state": api.acc.accounts_state()}


@router.get("/api/channels", doc="GET /api/channels")
def get_channels(api: Any, ctx: Ctx):
    return 200, {"channels": api.repo.channels()}


@router.get("/api/expense-categories", doc="GET /api/expense-categories")
def get_expense_categories(api: Any, ctx: Ctx):
    return 200, {"categories": api.repo.expense_categories()}


@router.get("/api/fixed-costs", doc="GET /api/fixed-costs")
def get_fixed_costs(api: Any, ctx: Ctx):
    return 200, {"fixed_costs": api.repo.fixed_costs(),
                 "monthly": api.acc.fixed_costs_monthly()}


@router.get("/api/payments", doc="GET /api/payments")
def get_payments(api: Any, ctx: Ctx):
    return 200, {"payments": api.repo.payments(ctx.one("order_id"))}


@router.get("/api/export/report", doc="GET /api/export/report")
def get_export_report(api: Any, ctx: Ctx):
    return 200, {"filename": f"printflow-{ctx.one('period', 'month')}.csv",
                 "csv": api.acc.report_csv(ctx.one("period", "month"),
                                            int(num(ctx.one("offset", "0"), 0)))}


@router.get("/api/export/sales", doc="GET /api/export/sales")
def get_export_sales(api: Any, ctx: Ctx):
    return 200, {"filename": f"printflow-продажи-{ctx.one('period', 'month')}.csv",
                 "csv": api.acc.sales_details_csv(ctx.one("period", "month"),
                                                   int(num(ctx.one("offset", "0"), 0)))}


@router.get("/api/export/transactions", doc="GET /api/export/transactions")
def get_export_transactions(api: Any, ctx: Ctx):
    return 200, {"filename": "printflow-проводки.csv",
                 "csv": api.acc.transactions_csv(int(num(ctx.one("days", "365"), 365)))}


@router.get("/api/jobs", doc="GET /api/jobs")
def get_jobs(api: Any, ctx: Ctx):
    return 200, {"queue": api.manager.queue(),
                 "history": api.manager.history(int(num(ctx.one("limit", "100"), 100)))}


@router.get("/api/timeline", doc="GET /api/timeline")
def get_timeline(api: Any, ctx: Ctx):
    return 200, {"day": ctx.one("day", now_iso()[:10]),
                 "jobs": api.repo.timeline(ctx.one("day", now_iso()[:10]))}


@router.get("/api/shelf", doc="GET /api/shelf")
def get_shelf(api: Any, ctx: Ctx):
    return 200, {"items": api.shelf.items(), "summary": api.shelf.summary()}


@router.get("/api/shelf/moves", doc="GET /api/shelf/moves")
def get_shelf_moves(api: Any, ctx: Ctx):
    return 200, {"moves": api.shelf.moves(ctx.one("item_id"),
                                           int(num(ctx.one("limit", "100"), 100)))}


@router.get("/api/nomenclature/groups", doc="GET /api/nomenclature/groups")
def get_nomenclature_groups(api: Any, ctx: Ctx):
    return 200, {"groups": api.nom.groups()}


@router.get("/api/replenishment", doc="GET /api/replenishment")
def get_replenishment(api: Any, ctx: Ctx):
    return 200, {"rows": api.batches.plan_replenishment(ctx.one("warehouse_id"))}


@router.get("/api/nomenclature/frozen-capital", doc="GET /api/nomenclature/frozen-capital")
def get_nomenclature_frozen_capital(api: Any, ctx: Ctx):
    return 200, api.nom.frozen_capital(ctx.one("warehouse_id"))


@router.get("/api/nomenclature/filament-forecast", doc="GET /api/nomenclature/filament-forecast")
def get_nomenclature_filament_forecast(api: Any, ctx: Ctx):
    return 200, api.nom.filament_forecast(int(num(ctx.one("days", "30"), 30)))


@router.get("/api/plan/day", doc="GET /api/plan/day")
def get_plan_day(api: Any, ctx: Ctx):
    return 200, api.planner.day_plan()


@router.get("/api/insights", doc="GET /api/insights")
def get_insights(api: Any, ctx: Ctx):
    return 200, api.insights.all()


@router.get("/api/payback", doc="GET /api/payback")
def get_payback(api: Any, ctx: Ctx):
    return 200, api.insights.payback()


@router.get("/api/tax-compare", doc="GET /api/tax-compare")
def get_tax_compare(api: Any, ctx: Ctx):
    return 200, api.insights.tax_compare()


@router.get("/api/cash-daily", doc="GET /api/cash-daily")
def get_cash_daily(api: Any, ctx: Ctx):
    return 200, api.insights.cash_forecast_daily(int(num(ctx.one("days", "90"), 90)))


@router.get("/api/public/catalog", doc="GET /api/public/catalog")
def get_public_catalog(api: Any, ctx: Ctx):
    return 200, api.public_catalog()


@router.get("/api/network/diagnose", doc="GET /api/network/diagnose")
def get_network_diagnose(api: Any, ctx: Ctx):
    return 200, api.network_diagnose(ctx.one("host"))


@router.get("/api/labels", doc="GET /api/labels")
def get_labels(api: Any, ctx: Ctx):
    return 200, api.labels(ctx.one("kind", "all"))


@router.get("/api/ops/today", doc="GET /api/ops/today")
def get_ops_today(api: Any, ctx: Ctx):
    return 200, api.ops_today()


@router.get("/api/envelopes", doc="GET /api/envelopes")
def get_envelopes(api: Any, ctx: Ctx):
    return 200, {"envelopes": api.envelopes.list(),
                 "total": api.envelopes.total(),
                 "auto": api.db.setting("envelope_auto", False)}


@router.get("/api/clients/rfm", doc="GET /api/clients/rfm")
def get_clients_rfm(api: Any, ctx: Ctx):
    return 200, {"rows": api.clients.rfm(int(num(ctx.one("days", "90"), 90)))}


@router.get("/api/clients/duplicates", doc="GET /api/clients/duplicates")
def get_clients_duplicates(api: Any, ctx: Ctx):
    return 200, {"groups": api.clients.duplicates()}


@router.get("/api/data-check", doc="GET /api/data-check")
def get_data_check(api: Any, ctx: Ctx):
    return 200, api.repo.data_check()


@router.get("/api/order/history", doc="GET /api/order/history")
def get_order_history(api: Any, ctx: Ctx):
    return 200, {"history": api.repo.order_history(ctx.one("id"))}


@router.get("/api/track/order", doc="GET /api/track/order")
def get_track_order(api: Any, ctx: Ctx):
    return 200, api.track_order(ctx.one("number"), ctx.one("phone"), ctx.one("token"))


@router.get("/api/stock", doc="GET /api/stock")
def get_stock(api: Any, ctx: Ctx):
    return 200, {"balances": api.stock.balances(ctx.one("warehouse_id")),
                 "moves": api.stock.moves(ctx.one("nom_id"), ctx.one("warehouse_id"),
                                           int(num(ctx.one("limit", "80"), 80)))}


@router.get("/api/stock/turnover", doc="GET /api/stock/turnover")
def get_stock_turnover(api: Any, ctx: Ctx):
    return 200, {"rows": api.stock.turnover(ctx.one("from"), ctx.one("to"),
                                             ctx.one("warehouse_id"))}


@router.get("/api/documents", doc="GET /api/documents")
def get_documents(api: Any, ctx: Ctx):
    return 200, {"documents": api.docs.list(
        ctx.one("kind"), ctx.one("state"), ctx.one("warehouse_id"), ctx.one("search"),
        int(num(ctx.one("limit", "200"), 200)), ctx.one("order_id"))}


@router.get("/api/batches", doc="GET /api/batches")
def get_batches(api: Any, ctx: Ctx):
    return 200, {"batches": api.batches.list(
        ctx.one("state"), int(num(ctx.one("limit", "100"), 100)))}


@router.get("/api/price-types", doc="GET /api/price-types")
def get_price_types(api: Any, ctx: Ctx):
    return 200, {"price_types": api.repo.price_types()}


@router.get("/api/audit", doc="GET /api/audit")
def get_audit(api: Any, ctx: Ctx):
    return 200, {"rows": api.repo.audit_rows(int(num(ctx.one("limit", "100"), 100)))}


@router.get("/api/events", doc="GET /api/events")
def get_events(api: Any, ctx: Ctx):
    return 200, {"events": api.db.events(int(num(ctx.one("limit", "80"), 80)),
                                          ctx.one("printer_id"), ctx.one("kind"))}


@router.get("/api/settings", doc="GET /api/settings")
def get_settings(api: Any, ctx: Ctx):
    return 200, {"settings": api.db.settings()}


@router.get("/api/client-bot/inbox", doc="GET /api/client-bot/inbox")
def get_client_bot_inbox(api: Any, ctx: Ctx):
    return 200, {"items": api.repo.client_inbox(int(num(ctx.one("limit", "60"), 60)))}


@router.get("/api/client-bot/payments", doc="GET /api/client-bot/payments")
def get_client_bot_payments(api: Any, ctx: Ctx):
    return 200, {"payments": api.repo.client_payments(
        int(num(ctx.one("limit", "60"), 60)))}


@router.get("/api/rules/runs", doc="GET /api/rules/runs")
def get_rules_runs(api: Any, ctx: Ctx):
    return 200, {"runs": api.manager.rules.recent_runs(
        max(1, min(200, int(num(ctx.one("limit", "50"), 50)))))}


@router.get("/api/shopping", doc="GET /api/shopping")
def get_shopping(api: Any, ctx: Ctx):
    return 200, {"items": api.shopping.items(ctx.one("all") == "1"),
                 "summary": api.shopping.summary(),
                 "filament_stats": api.acc.filament_stats(int(num(ctx.one("days", "30"), 30)))}


@router.get("/api/purchase-hint", doc="GET /api/purchase-hint")
def get_purchase_hint(api: Any, ctx: Ctx):
    return 200, {"hint": api.acc.purchase_hint()}


@router.get("/api/update-check", doc="GET /api/update-check")
def get_update_check(api: Any, ctx: Ctx):
    return 200, api.updater.report()


@router.get("/api/abc", doc="GET /api/abc")
def get_abc(api: Any, ctx: Ctx):
    return 200, api.acc.abc_report(int(num(ctx.one("days", "30"), 30)))


@router.get("/api/calc/materials", doc="GET /api/calc/materials")
def get_calc_materials(api: Any, ctx: Ctx):
    return 200, api.acc.material_options()


@router.get("/api/materials", doc="GET /api/materials")
def get_materials(api: Any, ctx: Ctx):
    return 200, api.acc.material_options()


@router.get("/api/calc/real-stats", doc="GET /api/calc/real-stats")
def get_calc_real_stats(api: Any, ctx: Ctx):
    return 200, api.acc.real_stats(
        ctx.one("product"), ctx.one("material"),
        int(num(ctx.one("days", "60"), 60)))


@router.get("/api/filament-stats", doc="GET /api/filament-stats")
def get_filament_stats(api: Any, ctx: Ctx):
    return 200, api.acc.filament_stats(int(num(ctx.one("days", "30"), 30)))


@router.get("/api/price-history", doc="GET /api/price-history")
def get_price_history(api: Any, ctx: Ctx):
    return 200, {"history": api.acc.price_history(ctx.one("product"),
                                                   int(num(ctx.one("limit", "30"), 30)))}


@router.get("/api/defects", doc="GET /api/defects")
def get_defects(api: Any, ctx: Ctx):
    return 200, {"defects": api.repo.defect_rows(
        int(num(ctx.one("limit", "100"), 100)))}


@router.get("/api/defect/recovery", doc="GET /api/defect/recovery")
def get_defect_recovery(api: Any, ctx: Ctx):
    return 200, api.defect_recovery.summary(
        ctx.one("id") or ctx.one("job_id"), num(ctx.one("grams")), ctx.one("reason")
    )


@router.get("/api/schedule", doc="GET /api/schedule")
def get_schedule(api: Any, ctx: Ctx):
    return 200, {"commands": api.repo.scheduled_commands(
        int(num(ctx.one("limit", "50"), 50)))}


@router.get("/api/ams-profiles", doc="GET /api/ams-profiles")
def get_ams_profiles(api: Any, ctx: Ctx):
    return 200, {"profiles": api.repo.ams_profiles()}


@router.get("/api/templates", doc="GET /api/templates")
def get_templates(api: Any, ctx: Ctx):
    return 200, {"templates": api._templates()}


@router.get("/api/order/photos", doc="GET /api/order/photos")
def get_order_photos(api: Any, ctx: Ctx):
    return 200, {"photos": api._order_photos(ctx.one("order_id"))}


@router.get("/api/backup", doc="GET /api/backup")
def get_backup(api: Any, ctx: Ctx):
    return 200, api.repo.export_all()


@router.get("/api/settings/profiles", doc="GET /api/settings/profiles")
def get_settings_profiles(api: Any, ctx: Ctx):
    return 200, {"profiles": api.db.setting("settings_profiles", [])}


@router.get("/api/slicer/materials", doc="GET /api/slicer/materials")
def get_slicer_materials(api: Any, ctx: Ctx):
    return 200, api.acc.material_options()


@router.get("/api/shelf/tags", doc="GET /api/shelf/tags")
def get_shelf_tags(api: Any, ctx: Ctx):
    return 200, api.shelf.live_tags()


@router.get("/api/system/heartbeat", doc="GET /api/system/heartbeat")
def get_system_heartbeat(api: Any, ctx: Ctx):
    return 200, api._heartbeat()


@router.get("/api/ams/suggestion", doc="GET /api/ams/suggestion")
def get_ams_suggestion(api: Any, ctx: Ctx):
    return 200, {"suggestion": api._ams_suggestion()}


@router.get("/api/order/pack-data", doc="GET /api/order/pack-data")
def get_order_pack_data(api: Any, ctx: Ctx):
    return 200, api._pack_data(ctx.one("id"))


@router.get("/api/public/my", doc="GET /api/public/my")
def get_public_my(api: Any, ctx: Ctx):
    return 200, api._my_nozza(ctx.one("code"))


# --- блоки с несколькими строками
# Те же ветки, что были в `Api.get()`, только с несколькими операторами:
# присваивания, try/except, ранние return. Тело перенесено дословно.


@router.get("/api/job/passport", doc="GET /api/job/passport")
def get_job_passport(api: Any, ctx: Ctx):
    from .passport import job_passport
    return 200, job_passport(api.db, ctx.one("id"))


@router.get("/api/camera/diagnose", doc="GET /api/camera/diagnose")
def get_camera_diagnose(api: Any, ctx: Ctx):
    from .camera import diagnose
    return 200, diagnose(api.printer_or_fail(ctx.one("printer_id")))


@router.get("/api/printer/rtsp-link", doc="GET /api/printer/rtsp-link")
def get_printer_rtsp_link(api: Any, ctx: Ctx):
    # Ссылка содержит Access Code — отдаём только по явному запросу.
    from .camera import rtsp_link
    printer = api.printer_or_fail(ctx.one("printer_id"))
    link = rtsp_link(printer)
    if not link:
        return 200, {"link": "", "error":
                     "Нужны IP и Access Code в карточке принтера"}
    api.db.add_event("printer", "Запрошена RTSP-ссылка",
                      printer.record.get("name") or "Принтер",
                      printer.id, {})
    return 200, {"link": link}


@router.get("/api/printer/discover", doc="GET /api/printer/discover")
def get_printer_discover(api: Any, ctx: Ctx):
    # SSDP в локальной сети + принтеры аккаунта Bambu Cloud.
    # Access Code облачных устройств в браузер не отдаётся: при
    # добавлении из облака сервер подставляет его сам.
    return 200, {"found": BambuPrinter.discover(),
                 "cloud": api.cloud_devices()}


@router.get("/api/printer/files", doc="GET /api/printer/files")
def get_printer_files(api: Any, ctx: Ctx):
    printer = api.printer_or_fail(ctx.one("printer_id"))
    # Файлы SD — это FTPS по локальной сети. У облачного принтера
    # IP/Access Code могли не заполниться при добавлении: пробуем
    # дозаполнить (облачный список устройств + SSDP) прямо сейчас.
    if not (printer.record.get("host") and printer.record.get("access_code")):
        if not api._ensure_lan_access(printer):
            return 200, {"path": ctx.one("path", "/"), "files": [],
                         "error": ("Файлы SD-карты доступны только по локальной "
                                   "сети: укажите IP принтера (экран → Настройки → "
                                   "WLAN) и Access Code в карточке принтера.")}
        printer = api.printer_or_fail(ctx.one("printer_id"))
    from .routes_workshop import _files_payload
    return 200, _files_payload(printer, ctx.one("path", "/"))


@router.get("/api/orders", doc="GET /api/orders")
def get_orders(api: Any, ctx: Ctx):
    # limit/offset (17.0.16): без них список заказов рос вместе с
    # историей. Не переданы — прежнее поведение, весь список.
    # archived=1 — только снятые с доски, archived=all — и те и другие.
    archived = ctx.one("archived")
    return 200, {"orders": api.repo.orders(
        ctx.one("status"), ctx.one("q"), ctx.one("niche_id"),
        int(num(ctx.one("limit", "0"), 0)), int(num(ctx.one("offset", "0"), 0)),
        include_archived=archived in ("1", "all"),
        only_archived=archived == "1")}


@router.get("/api/order", doc="GET /api/order")
def get_order(api: Any, ctx: Ctx):
    order = api.repo.order(ctx.one("id"))
    return (200, order) if order else (404, {"error": "Заказ не найден"})


@router.get("/api/spool", doc="GET /api/spool")
def get_spool(api: Any, ctx: Ctx):
    spool = api.repo.spool(ctx.one("id"))
    if not spool:
        return 404, {"error": "Катушка не найдена"}
    suggest = api.suggest_spool_slot(spool)
    return 200, {"spool": spool, "printers": api.repo.printers(),
                 "suggest": suggest}


@router.get("/api/spool/qr-link", doc="GET /api/spool/qr-link")
def get_spool_qr_link(api: Any, ctx: Ctx):
    spool = api.repo.spool(ctx.one("id"))
    if not spool:
        return 404, {"error": "Катушка не найдена"}
    from urllib.parse import quote
    info = api.qr_target("/spool.html", f"id={quote(spool['id'], safe='')}")
    return 200, {**info, "spool": {
        "id": spool["id"], "material": spool.get("material"),
        "color_name": spool.get("color_name"),
        "brand": spool.get("brand") or "",
    }}


@router.get("/api/finance", doc="GET /api/finance")
def get_finance(api: Any, ctx: Ctx):
    days = int(num(ctx.one("days", "30"), 30))
    api.acc.run_fixed_costs()
    return 200, {"summary": api.acc.summary(days),
                 "hour_cost": api.acc.actual_hour_cost(max(days, 30)),
                 "series": api.acc.daily_series(days),
                 "transactions": api.repo.transactions(50),
                 "niches": api.acc.niche_report(),
                 "accounts": api.acc.accounts_state(),
                 "debts": api.acc.debts(),
                 "break_even": api.acc.break_even()}


@router.get("/api/shelf/item", doc="GET /api/shelf/item")
def get_shelf_item(api: Any, ctx: Ctx):
    item = api.shelf.item(ctx.one("id"))
    return (200, item) if item else (404, {"error": "Позиция не найдена"})


@router.get("/api/shelf/cash", doc="GET /api/shelf/cash")
def get_shelf_cash(api: Any, ctx: Ctx):
    # Касса магазина: сколько от стеллажа лежит в магазине и сколько забрали.
    return 200, api.shelf.shop_cash()


@router.get("/api/shelf/stock-available", doc="GET /api/shelf/stock-available")
def get_shelf_stock_available(api: Any, ctx: Ctx):
    # Товары учётных складов с остатком ≥ 1 шт — их можно
    # переместить на стеллаж (0 и «хвосты» меньше штуки не показываем).
    goods_only = str(ctx.one("goods", "")).lower() in ("1", "true", "yes")
    return 200, {"items": api.shelf.stock_available(goods_only=goods_only)}


@router.get("/api/shelf/qr-link", doc="GET /api/shelf/qr-link")
def get_shelf_qr_link(api: Any, ctx: Ctx):
    item = api.shelf.item(ctx.one("id"))
    if not item:
        return 404, {"error": "Позиция не найдена"}
    info = api.shelf.qr_link(
        ctx.one("id"), getattr(api, "last_host", ""),
        str(api.db.setting("public_url", "") or ""),
        int(getattr(api, "listen_port", 8080) or 8080))
    return 200, info


@router.get("/api/shelf/1c/lookup", doc="GET /api/shelf/1c/lookup")
def get_shelf_1c_lookup(api: Any, ctx: Ctx):
    try:
        item = api.shelf.cashier_lookup(ctx.one("barcode") or ctx.one("code"))
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return (200, {"item": item}) if item else (
        404, {"error": "Код не привязан к позиции стеллажа"})


@router.get("/api/shelf/1c/export", doc="GET /api/shelf/1c/export")
def get_shelf_1c_export(api: Any, ctx: Ctx):
    items = api.shelf.items()
    return 200, {
        "filename": "printflow-1c-nomenclature.csv",
        "csv": api.shelf.one_c_export_csv(),
        "items": len(items),
        "linked": sum(1 for item in items if item.get("barcode")),
    }


@router.get("/api/nomenclature/item", doc="GET /api/nomenclature/item")
def get_nomenclature_item(api: Any, ctx: Ctx):
    item = api.nom.item(ctx.one("id"))
    return (200, item) if item else (404, {"error": "Позиция не найдена"})


@router.get("/api/network/ips", doc="GET /api/network/ips")
def get_network_ips(api: Any, ctx: Ctx):
    from .config import get_local_ips
    info = api.qr_target("/")
    return 200, {"ips": get_local_ips(), "base": info["base"],
                 "reachable": info["reachable"], "source": info["source"]}


@router.get("/api/network/scan", doc="GET /api/network/scan")
def get_network_scan(api: Any, ctx: Ctx):
    from . import network
    ranges = [r for r in ctx.one("ranges").split(",") if r.strip()]
    return 200, {"found": network.scan_ranges(ranges)}


@router.get("/api/network/mdns", doc="GET /api/network/mdns")
def get_network_mdns(api: Any, ctx: Ctx):
    from . import network
    return 200, {"found": network.mdns_discover()}


@router.get("/api/warehouses", doc="GET /api/warehouses")
def get_warehouses(api: Any, ctx: Ctx):
    # И3: просроченные холды снимаются лениво — список всегда свежий.
    try:
        api.stock.release_expired_holds(
            num(api.db.setting("sbp_hold_hours", 24), 24))
    except Exception:
        pass
    return 200, {"warehouses": api.stock.warehouse_totals(),
                 "reserves": api.stock.reserves(),
                 "reserved": round(sum(num(r.get("qty"))
                                       for r in api.stock.reserves()), 1)}


@router.get("/api/order/filament-fact", doc="GET /api/order/filament-fact")
def get_order_filament_fact(api: Any, ctx: Ctx):
    # План пластика заказа (катушки, граммы) против факта списаний
    # с принтера (идеи 60, 68).
    oid = ctx.one("id")
    if not oid:
        return 400, {"error": "Укажите заказ"}
    return 200, api.acc.filament_plan_vs_actual(oid)


@router.get("/api/reserves", doc="GET /api/reserves")
def get_reserves(api: Any, ctx: Ctx):
    try:
        api.stock.release_expired_holds(
            num(api.db.setting("sbp_hold_hours", 24), 24))
    except Exception:
        pass
    return 200, {"reserves": api.stock.reserves()}


@router.get("/api/stock/writeoffs", doc="GET /api/stock/writeoffs")
def get_stock_writeoffs(api: Any, ctx: Ctx):
    # Сводка ручных списаний для блока «куда девается» (идея 6)
    days = int(num(ctx.one("days", "30"), 30))
    stats = api.stock.manual_stats(days=days)
    recent = api.stock.manual_recent(days=days)
    return 200, {"stats": stats, "recent": recent}


@router.get("/api/order/documents", doc="GET /api/order/documents")
def get_order_documents(api: Any, ctx: Ctx):
    order_id = ctx.one("id") or ctx.one("order_id")
    if not order_id:
        raise ValueError("Не указан заказ")
    return 200, api.docs.for_order(order_id)


@router.get("/api/document", doc="GET /api/document")
def get_document(api: Any, ctx: Ctx):
    doc = api.docs.get(ctx.one("id"))
    return (200, doc) if doc else (404, {"error": "Документ не найден"})


@router.get("/api/batch", doc="GET /api/batch")
def get_batch(api: Any, ctx: Ctx):
    batch = api.batches.get(ctx.one("id"))
    return (200, batch) if batch else (404, {"error": "Партия не найдена"})


@router.get("/api/staff", doc="GET /api/staff")
def get_staff(api: Any, ctx: Ctx):
    from .staff import ROLE_RIGHTS, ROLE_NAMES, Staff
    staff = Staff(api.db)
    return 200, {"staff": staff.all(), "invites": staff.invites(),
                 "roles": {r: {"name": ROLE_NAMES[r],
                               "rights": sorted(rights)}
                          for r, rights in ROLE_RIGHTS.items()},
                 "owner_chat": str(api.db.setting("telegram_chat_id",
                                                   "") or "")}


@router.get("/api/staff/subscriptions", doc="GET /api/staff/subscriptions")
def get_staff_subscriptions(api: Any, ctx: Ctx):
    # Н54: реестр событий и выбор сотрудника.
    from . import subscriptions
    staff_id = ctx.one("staff_id") or ctx.one("id")
    return 200, {"ok": True, "events": subscriptions.catalog(),
                 "staff_id": staff_id,
                 "current": subscriptions.get(api.db, staff_id) if staff_id else {}}


@router.get("/api/conversations", doc="GET /api/conversations")
def get_conversations(api: Any, ctx: Ctx):
    # Н55: одна лента вместо трёх вкладок.
    from .conversations import Conversations
    service = getattr(api, "conversations", None)
    if service is None:
        service = api.conversations = Conversations(api.db)
    rows = service.threads(
        int(num(ctx.one("limit"), 50)),
        channel=ctx.one("channel"), q=ctx.one("q"),
        unread_only=ctx.one("unread") in ("1", "true", "yes"),
        needs_answer=ctx.one("needs_answer") in ("1", "true", "yes"))
    return 200, {"ok": True, "threads": rows, "summary": service.summary()}


@router.get("/api/conversations/thread", doc="GET /api/conversations/thread")
def get_conversations_thread(api: Any, ctx: Ctx):
    from .conversations import Conversations
    service = getattr(api, "conversations", None)
    if service is None:
        service = api.conversations = Conversations(api.db)
    key = ctx.one("id") or ctx.one("key")
    if not key:
        raise ValueError("Не указан диалог")
    return 200, {"ok": True, **service.thread(key, int(num(ctx.one("limit"), 100)))}


@router.get("/api/client-bot/analytics", doc="GET /api/client-bot/analytics")
def get_client_bot_analytics(api: Any, ctx: Ctx):
    bot = getattr(api.manager, "client_bot", None)
    return 200, bot.analytics(int(num(ctx.one("days", "30"), 30))) if bot else {}


@router.get("/api/rules", doc="GET /api/rules")
def get_rules(api: Any, ctx: Ctx):
    from .rules import ACTIONS, TRIGGERS
    return 200, {"rules": api.manager.rules.rules(),
                 "triggers": TRIGGERS, "actions": ACTIONS}


@router.get("/api/calc/plate-layout", doc="GET /api/calc/plate-layout")
def get_calc_plate_layout(api: Any, ctx: Ctx):
    from .model_registry import ModelRegistry
    mr = ModelRegistry(api.db)
    return 200, mr.plate_layout(
        num(ctx.one("dim_x")), num(ctx.one("dim_y")),
        num(ctx.one("gap")), num(ctx.one("plate_w")), num(ctx.one("plate_h")))


@router.get("/api/models", doc="GET /api/models")
def get_models(api: Any, ctx: Ctx):
    from .model_registry import ModelRegistry
    mr = ModelRegistry(api.db)
    return 200, {"models": mr.list(ctx.one("search"), ctx.one("nom_id")),
                 "stats": mr.stats()}


@router.get("/api/model", doc="GET /api/model")
def get_model(api: Any, ctx: Ctx):
    from .model_registry import ModelRegistry
    mr = ModelRegistry(api.db)
    model = mr.get(ctx.one("id"))
    return (200, model) if model else (404, {"error": "Модель не найдена"})


@router.get("/api/analytics/oee", doc="GET /api/analytics/oee")
def get_analytics_oee(api: Any, ctx: Ctx):
    from .analytics import Analytics
    return 200, Analytics(api.db).oee(
        int(num(ctx.one("days", "30"), 30)), ctx.one("printer_id"))


@router.get("/api/analytics/correction", doc="GET /api/analytics/correction")
def get_analytics_correction(api: Any, ctx: Ctx):
    from .analytics import Analytics
    return 200, Analytics(api.db).correction_factors(
        int(num(ctx.one("days", "60"), 60)), ctx.one("material"))


@router.get("/api/analytics/pnl-products", doc="GET /api/analytics/pnl-products")
def get_analytics_pnl_products(api: Any, ctx: Ctx):
    from .analytics import Analytics
    return 200, Analytics(api.db).pnl_by_product(
        int(num(ctx.one("days", "30"), 30)))


@router.get("/api/analytics/anomalies", doc="GET /api/analytics/anomalies")
def get_analytics_anomalies(api: Any, ctx: Ctx):
    from .analytics import Analytics
    return 200, {"anomalies": Analytics(api.db).detect_anomalies(
        int(num(ctx.one("days", "30"), 30)))}


@router.get("/api/analytics/defects", doc="GET /api/analytics/defects")
def get_analytics_defects(api: Any, ctx: Ctx):
    from .analytics import Analytics
    return 200, Analytics(api.db).defect_analysis(
        int(num(ctx.one("days", "30"), 30)))


@router.get("/api/analytics/smart-queue", doc="GET /api/analytics/smart-queue")
def get_analytics_smart_queue(api: Any, ctx: Ctx):
    from .analytics import Analytics
    return 200, Analytics(api.db).smart_queue()


@router.get("/api/watch/pending", doc="GET /api/watch/pending")
def get_watch_pending(api: Any, ctx: Ctx):
    watch = getattr(api.manager, "watch", None)
    return 200, {"items": watch.list_pending(int(num(ctx.one("limit","20"),20))) if watch else []}


@router.get("/api/watch/status", doc="GET /api/watch/status")
def get_watch_status(api: Any, ctx: Ctx):
    watch = getattr(api.manager, "watch", None)
    return 200, {"enabled": bool(api.db.setting("watch_folder_enabled", False)), "path": str(api.db.setting("watch_folder_path","")), "pending": len(watch._pending) if watch else 0}


@router.get("/api/studio/status", doc="GET /api/studio/status")
def get_studio_status(api: Any, ctx: Ctx):
    studio = getattr(api.manager, "studio", None) if api.manager else None
    if studio:
        payload = studio.status()
        payload.pop("access_code", None)
        return 200, payload
    return 200, {
        "enabled": bool(api.db.setting("studio_gateway_enabled", False)),
        "running": False,
        "has_access_code": bool(api.db.setting("studio_gateway_access_code", "")),
    }


@router.get("/api/library", doc="GET /api/library")
def get_library(api: Any, ctx: Ctx):
    from .library import FileLibrary
    limit = int(num(ctx.one("limit", "80"), 80) or 80)
    return 200, {"files": FileLibrary(api.db).list(
        kind=ctx.one("kind"), q=ctx.one("q"), limit=max(1, min(limit, 500)))}


@router.get("/api/slicer/status", doc="GET /api/slicer/status")
def get_slicer_status(api: Any, ctx: Ctx):
    from .slicer import status as slicer_status
    return 200, slicer_status(str(api.db.setting("slicer_bin", "") or ""))


@router.get("/api/slicer/thumbnail", doc="GET /api/slicer/thumbnail")
def get_slicer_thumbnail(api: Any, ctx: Ctx):
    fid = ctx.one("fid")
    name = ctx.one("name")
    watch = getattr(api.manager, "watch", None)
    if watch and fid:
        info = watch.get_pending(fid) or {}
        thumbs = info.get("thumbnails_full", {}) or info.get("thumbnails", {})
        # name may be exact key or suffix
        for k,v in thumbs.items():
            if k==name or k.endswith(name):
                import base64
                try:
                    base64.b64decode(v, validate=True)
                    return 200, {"ok": True, "b64": v}
                except Exception:
                    pass
        return 404, {"error": "Превью не найдено"}
    return 404, {"error": "Нет данных"}


@router.get("/api/printer/preflight", doc="GET /api/printer/preflight")
def get_printer_preflight(api: Any, ctx: Ctx):
    printer = api.printer_or_fail(ctx.one("printer_id"))
    return 200, api.manager.preflight(printer.id, ctx.one("file"), int(num(ctx.one("plate"),1)), json.loads(ctx.one("mapping","[]") or "[]"))


@router.get("/api/printer/files/tree", doc="GET /api/printer/files/tree")
def get_printer_files_tree(api: Any, ctx: Ctx):
    printer = api.printer_or_fail(ctx.one("printer_id"))
    if not (printer.record.get("host") and printer.record.get("access_code")):
        api._ensure_lan_access(printer)
        printer = api.printer_or_fail(ctx.one("printer_id"))
    depth = int(num(ctx.one("depth"), 1))
    try:
        files = printer.files.list_tree(ctx.one("path","/"), depth)
    except Exception as exc:
        # Канал FTPS помечаем по факту (17.0.19): дерево не получилось —
        # пробуем плоский список. Если и он упадёт, исключение уйдёт
        # наружу, как и раньше, но причина уже записана в состояние.
        api.mark_link(printer.id, "ftps", False,
                       str(exc) or type(exc).__name__)
        files = printer.files.list_files(ctx.one("path","/"))
    api.mark_link(printer.id, "ftps", True)
    return 200, {"path": ctx.one("path","/"), "files": files}


@router.get("/api/printer/files/usage", doc="GET /api/printer/files/usage")
def get_printer_files_usage(api: Any, ctx: Ctx):
    printer = api.printer_or_fail(ctx.one("printer_id"))
    return 200, printer.files.disk_usage(ctx.one("path","/"))


@router.get("/api/estimate", doc="GET /api/estimate")
def get_estimate(api: Any, ctx: Ctx):
    fname = ctx.one("file").strip()
    if not fname:
        return 400, {"error": "Не указано имя файла"}
    from .config import UPLOAD_DIR
    from .estimate import estimate_file, parse_3mf_complete
    safe_name = Path(fname).name
    local = safe_file(UPLOAD_DIR, safe_name) or (UPLOAD_DIR / safe_name)
    if not local.exists():
        try:
            low = safe_name.lower()
            for pat in ("*.3mf", "*.gcode", "*.gcode.3mf"):
                for pp in UPLOAD_DIR.glob(pat):
                    nlow = pp.name.lower()
                    if nlow == low or low in nlow or nlow in low or pp.stem.lower() in low:
                        local = pp
                        break
                if local.exists():
                    break
        except Exception:
            pass
    if not local.exists():
        try:
            watch_root = str(api.db.setting("watch_folder_path", "") or "").strip()
            if watch_root:
                wp = Path(watch_root).expanduser() / safe_name
                if wp.exists():
                    local = wp
                else:
                    for pp in Path(watch_root).expanduser().glob("*.3mf"):
                        if pp.name.lower() == safe_name.lower():
                            local = pp
                            break
        except Exception:
            pass
    if local.exists():
        is_3mf = local.name.lower().endswith(".3mf")
        if is_3mf:
            try:
                est = estimate_file(local)
                try:
                    detail = parse_3mf_complete(local)
                except Exception:
                    detail = {}
                if (not est.get("grams") and not est.get("total_grams")) and detail.get("plates"):
                    total_g = round(sum(float(p.get("grams") or 0) for p in detail["plates"]), 1)
                    total_m = round(sum(float(p.get("minutes") or 0) for p in detail["plates"]), 1)
                    if detail["plates"]:
                        est = dict(detail["plates"][0])
                        est["total_grams"] = total_g
                        est["total_minutes"] = total_m
                        est["plates"] = detail["plates"]
                        est["plate_count"] = len(detail["plates"])
                return 200, {"estimate": est, "detail": detail if 'detail' in locals() else {}}
            except Exception:
                return 200, {"estimate": estimate_file(local)}
        else:
            return 200, {"estimate": estimate_file(local)}
    base = Path(fname).name.lower()
    base_variants = {base}
    if base.endswith(".gcode.3mf"):
        base_variants.add(base[:-10] + ".3mf")
        base_variants.add(base[:-10])
    if base.endswith(".3mf"):
        base_variants.add(base[:-4])
    known = None
    try:
        for job in api.manager.history(500) + api.manager.queue():
            job_name = Path(str(job.get("file") or job.get("name") or "")).name.lower()
            if job_name in base_variants or base in job_name or any(v in job_name for v in base_variants):
                if num(job.get("grams")) or num(job.get("est_grams")):
                    known = job
                    break
                if known is None:
                    known = job
        if known:
            grams = num(known.get("grams")) or num(known.get("est_grams"))
            minutes = num(known.get("duration_min")) or num(known.get("est_minutes"))
            if grams or minutes:
                return 200, {"estimate": {"grams": grams, "minutes": minutes,
                                           "total_grams": grams, "total_minutes": minutes,
                                           "source": "history"}}
    except Exception:
        pass
    try:
        for printer in api.manager.printers.values():
            est = api.manager._slicer_estimate(printer, fname)
            if num(est.get("grams")) or num(est.get("minutes")) or est.get("material") or est.get("color"):
                if est.get("grams") and not est.get("total_grams"):
                    est["total_grams"] = est["grams"]
                if est.get("minutes") and not est.get("total_minutes"):
                    est["total_minutes"] = est["minutes"]
                return 200, {"estimate": est}
    except Exception:
        pass
    return 404, {"error": f"Файл не найден: {safe_name}"}


@router.get("/api/shelf/forecast", doc="GET /api/shelf/forecast")
def get_shelf_forecast(api: Any, ctx: Ctx):
    try:
        days = max(1, min(int(ctx.one("days", "7") or 7), 30))
    except ValueError:
        days = 7
    return 200, {"days": days, "items": api.shelf.forecast(days)}


@router.get("/api/achievements", doc="GET /api/achievements")
def get_achievements(api: Any, ctx: Ctx):
    from .achievements import achievements
    return 200, {"badges": achievements(api.db)}


@router.get("/api/job/keyframes", doc="GET /api/job/keyframes")
def get_job_keyframes(api: Any, ctx: Ctx):
    from .config import PHOTO_DIR
    job_id = ctx.one("id")
    d = (PHOTO_DIR / "keyframes" / str(job_id)) if job_id else None
    if not d or not d.is_dir():
        return 200, {"frames": []}
    return 200, {"frames": [f.name for f in sorted(d.iterdir())
                             if f.suffix == ".jpg"]}


@router.get("/api/photos/similar", doc="GET /api/photos/similar")
def get_photos_similar(api: Any, ctx: Ctx):
    from .photos import similar
    try:
        return 200, similar(api.db, ctx.one("photo_id"), limit=12)
    except ValueError as exc:
        return 400, {"error": str(exc)}


@router.get("/api/bed/reference", doc="GET /api/bed/reference")
def get_bed_reference(api: Any, ctx: Ctx):
    from .config import PHOTO_DIR
    return 200, {"has": (PHOTO_DIR / "bed_reference.jpg").is_file()}


@router.get("/api/shelf/header", doc="Шапка полки: что продано за неделю")
def get_content_shelf_header(api: Any, ctx: Ctx):
    from .shelf import shelf_header
    try:
        days = max(1, min(int(ctx.one("days", "7") or 7), 30))
    except ValueError:
        days = 7
    return 200, shelf_header(api.db, days)


@router.get("/api/order/thread", doc="GET /api/order/thread")
def get_order_thread(api: Any, ctx: Ctx):
    from .order_thread import order_thread
    try:
        return 200, order_thread(api.db, ctx.one("id"))
    except ValueError as exc:
        return 404, {"error": str(exc)}


@router.get("/api/tour/state", doc="GET /api/tour/state")
def get_tour_state(api: Any, ctx: Ctx):
    backup = str(api.db.setting("tour_backup_file", "") or "")
    return 200, {"active": bool(backup), "backup": backup}


@router.get("/api/labels/code128", doc="GET /api/labels/code128")
def get_labels_code128(api: Any, ctx: Ctx):
    from .barcode import svg, validate
    text = str(ctx.one("text") or "").strip()
    if not text:
        return 400, {"error": "Нет текста для штрихкода"}
    try:
        info = validate(text)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, {"svg": svg(text), **info}
