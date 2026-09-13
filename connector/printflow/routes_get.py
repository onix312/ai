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
from .config import now_iso
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
