"""Маршруты автопилота AMS: доктор, журнал с откатом, правила, план (18.13).

Логика живёт в `ams_doctor.py` (что не так и что советуем), `ams_sync.py`
(привязки и остатки), `ams_defaults.py` (значения новых катушек) и
`ams_actions.py` (журнал и откат). Здесь — транспорт и проверки входа: маршрут
не должен уметь испортить склад, поэтому всё, что меняет данные, возвращает
снимок результата, а не «ок» без подробностей.
"""
from __future__ import annotations

import functools
from typing import Any

from .accounting import num
from .ams_actions import action_count, actions, undo_action
from .ams_defaults import forget_rule, rules, set_rule, update_material_default
from .config import now_iso
from .router import Ctx, router


def _reply_errors(handler):
    """Опечатка в параметре — это 400 с подсказкой, а не 500 в журнале.

    Проверки входа в маршрутах бросают ValueError (их же вызывают тесты
    логики); здесь они превращаются в понятный ответ для панели. Остальные
    сбои не глушим: это баги, их должно быть видно.
    """
    @functools.wraps(handler)
    def wrapper(api: Any, ctx: Ctx):
        try:
            return handler(api, ctx)
        except ValueError as exc:
            return 400, {"error": str(exc) or "Некорректные данные"}
    return wrapper


def _snapshots(api: Any, printer_id: str = "") -> dict[str, dict]:
    """Снимки парка для доктора: без них он покажет только то, что знает база."""
    try:
        state = api.manager.snapshot()
    except Exception:
        return {}
    out = {}
    for snap in state.get("printers") or []:
        pid = str(snap.get("id") or "")
        if printer_id and pid != str(printer_id):
            continue
        out[pid] = snap
    return out


@router.get("/api/ams/doctor",
            doc="AMS-доктор: проблемы слотов, действия автопилота и правила")
def ams_doctor(api: Any, ctx: Ctx):
    """Один ответ на вопрос «с AMS всё в порядке?».

    Отдаёт проблемы по каждому принтеру (с важностью), ленту действий
    автопилота за сутки, таблицу материалов и выученные правила. Панель,
    пульт и бот читают один и тот же ответ — расхождений между экранами нет.
    """
    from .ams_doctor import report

    printer_id = str(ctx.one("printer_id") or "").strip()
    limit = int(ctx.num("actions", 12) or 12)
    data = report(api.db, printer_id, _snapshots(api, printer_id),
                  actions_limit=max(1, min(100, limit)))
    return data


@router.get("/api/ams/actions", doc="Лента действий автопилота AMS с откатом")
def ams_actions(api: Any, ctx: Ctx):
    printer_id = str(ctx.one("printer_id") or "").strip()
    kind = str(ctx.one("kind") or "").strip()
    limit = int(ctx.num("limit", 40) or 40)
    only_undoable = str(ctx.one("undoable") or "") in ("1", "true", "yes")
    return {
        "actions": actions(api.db, printer_id, limit=limit, kind=kind,
                           only_undoable=only_undoable),
        "count_24h": action_count(api.db, printer_id, 24),
    }


@router.post("/api/ams/action/undo", audit="AMS: действие автопилота отменено",
             doc="Откатить действие автопилота AMS")
@_reply_errors
def ams_action_undo(api: Any, ctx: Ctx):
    """Откат одной строки журнала: значения катушки и память слота — назад.

    Для действия «записал настройки слота» откат снова отправляет в принтер
    прежние значения — поэтому нужен живой менеджер и принтер на связи.
    """
    action_id = str(ctx.arg("id") or "").strip()
    result = undo_action(api.db, action_id, manager=api.manager)
    api.db.add_event("ams", "Действие автопилота отменено",
                     str(result.get("detail") or ""), str(ctx.arg("printer_id") or ""),
                     {"action_id": action_id, "kind": result.get("kind")})
    return {"ok": True, **result}


@router.post("/api/ams/doctor/fix-all", audit="AMS: автопилот приведён в порядок",
             doc="Привести AMS в порядок сейчас, не дожидаясь цикла синка")
@_reply_errors
def ams_fix_all(api: Any, ctx: Ctx):
    """Кнопка «Привести в порядок»: синк по всем принтерам прямо сейчас.

    Делает то же, что фоновый цикл раз в пять минут, но по требованию: заводит
    и привязывает катушки, разбирает пустые и остатки, приводит настройки слота
    к складу. Возвращает счётчики — их показывает доктор.
    """
    printer_id = str(ctx.arg("printer_id") or "").strip()
    return api.manager.ams_fix_all(printer_id)


@router.get("/api/ams/plan", doc="План раскладки AMS под очередь заданий")
def ams_plan(api: Any, ctx: Ctx):
    """Совет «что в какой слот поставить»: ничего не меняет, только объясняет."""
    from .ams_doctor import plan

    printer_id = str(ctx.one("printer_id") or "").strip()
    limit = int(ctx.num("limit", 6) or 6)
    return plan(api.db, printer_id, _snapshots(api, printer_id), limit=limit)


@router.get("/api/ams/rules", doc="Правила AMS: что автопилот выучил и что задано")
def ams_rules(api: Any, ctx: Ctx):
    from .ams_defaults import material_defaults

    return {"rules": rules(api.db), "material_defaults": material_defaults(api.db),
            "rule_confirmations": 2}


@router.post("/api/ams/rule/save", audit="AMS: правило записано",
             doc="Записать правило AMS вручную (таблица материалов или имя цвета)")
@_reply_errors
def ams_rule_save(api: Any, ctx: Ctx):
    """Правило из панели действует сразу: `seen` не меньше порога подтверждения."""
    kind = str(ctx.arg("kind") or "").strip()
    key = str(ctx.arg("key") or "").strip()
    value = ctx.arg("value") or {}
    if isinstance(value, dict) is False:
        raise ValueError("Значение правила — словарь, например {\"price\": 1900}")
    rule = set_rule(api.db, kind, key, {str(k): v for k, v in value.items()})
    return {"ok": True, "rule": rule}


@router.post("/api/ams/rule/forget", audit="AMS: правило забыто",
             doc="Забыть правило AMS (одно или все)")
@_reply_errors
def ams_rule_forget(api: Any, ctx: Ctx):
    removed = forget_rule(api.db, str(ctx.arg("id") or ""))
    return {"ok": True, "removed": removed}


@router.post("/api/ams/material-default", audit="AMS: таблица материалов обновлена",
             doc="Таблица «материал → масса, цена, бренд» для новых катушек")
@_reply_errors
def ams_material_default(api: Any, ctx: Ctx):
    """Строка таблицы материалов: 0 и пустая строка убирают значение."""
    material = str(ctx.arg("material") or "").strip()
    if not material:
        raise ValueError("Укажите материал: PLA, PETG, ABS…")
    table = update_material_default(
        api.db, material,
        total_grams=ctx.arg("total_grams", 0), price=ctx.arg("price", 0),
        brand=ctx.arg("brand", ""))
    api.db.add_event("ams", "Таблица материалов AMS обновлена",
                     f"{material}: {table.get(material.upper()) or '—'}", "", {})
    return {"ok": True, "material_defaults": table}


@router.post("/api/ams/accept", audit="AMS: данные датчика приняты",
             doc="Принять остаток из AMS для катушки (склад подстраивается)")
@_reply_errors
def ams_accept(api: Any, ctx: Ctx):
    """Принять данные датчика: «склад расходится с AMS» → остаток по датчику.

    Зачем отдельным действием. Если расхождение большое, автомат молчит: он не
    знает, врёт датчик или в слоте другая катушка. Человек, посмотрев на слот,
    нажимает «принять» — и остаток склада становится таким, как показывает
    принтер. Откат возвращает прежнее значение (запись в журнале `ams_actions`).
    """
    from .ams_sync import accept_sensor, spool_by_id

    spool_id = str(ctx.arg("spool_id") or "").strip()
    spool = spool_by_id(api.db, spool_id)
    if not spool:
        raise ValueError("Катушка не найдена")
    printer_id = str(ctx.arg("printer_id") or spool.get("printer_id") or "").strip()
    if not printer_id:
        raise ValueError("У катушки не указан принтер — нечего принимать")
    printer = api.manager.get(printer_id) if api.manager else None
    if printer is None:
        raise ValueError("Принтер не найден в парке")
    slot = str(ctx.arg("slot") or spool.get("ams_slot") or "").strip()
    snap = printer.snapshot()
    tray = next((item for item in ((snap.get("ams") or {}).get("trays") or [])
                 if str(item.get("slot")) == slot), None)
    if tray is None:
        raise ValueError(f"Принтер ничего не знает про слот {slot or '—'}")
    result = accept_sensor(api.db, spool_id=spool_id, remain_pct=num(tray.get("remain"), -1),
                           slot=slot, printer_id=printer_id,
                           tray_label=str(tray.get("label") or ""))
    return {"ok": True, "spool": result["spool"],
            "remaining_grams": result["remaining_grams"],
            "action_id": result["action_id"]}


@router.get("/api/ams/summary", doc="Итог за сутки: действия автопилота и порядок в AMS")
def ams_summary(api: Any, ctx: Ctx):
    """Карточка для «Смены»: сколько автопилот сделал и что осталось руками.

    Ручная работа здесь измеряется числом катушек, которые ждут проверки
    (масса, цена, бренд), и числом нерешённых проблем — по ним видно, сходится
    ли автоматика к нулю или продолжает требовать человека.
    """
    from .ams_doctor import report, unverified_count

    printer_id = str(ctx.one("printer_id") or "").strip()
    data = report(api.db, printer_id, _snapshots(api, printer_id), actions_limit=6)
    return {
        "at": now_iso(),
        "actions_24h": action_count(api.db, printer_id, 24),
        "issues": data["issues_total"],
        "errors": data["errors"],
        "warns": data["warns"],
        "unverified": unverified_count(api.db, printer_id),
        "actions": data["actions"],
    }
