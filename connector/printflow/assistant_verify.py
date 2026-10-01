"""Deterministic verification for PrintFlow v19 assistant actions.

Luma may request an action, but only PrintFlow can prove what happened.  This
module never performs mutations: it reads the authoritative state after an
action and returns verified / pending / failed evidence.
"""
from __future__ import annotations

from typing import Any

from .accounting import num
from .assistant import ACTIONS, action_contract


def _payload_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _action_result(value: Any) -> dict[str, Any]:
    """Action HTTP payload passed back by the loopback client."""
    return _payload_dict(value)


def _entity_id(params: dict[str, Any], result: dict[str, Any], key: str = "id") -> str:
    value = str(params.get(key) or "").strip()
    if value:
        return value
    nested = result.get("order") if key == "id" else None
    if isinstance(nested, dict) and nested.get("id"):
        return str(nested["id"])
    if result.get(key):
        return str(result[key])
    return ""


def _answer(action: str, verification: str, state: str, *,
            evidence: dict[str, Any] | None = None, reason: str = "") -> dict[str, Any]:
    return {
        "ok": state != "failed",
        "verified": state == "verified",
        "state": state,
        "action": action,
        "verification": verification,
        "evidence": evidence or {},
        "reason": str(reason or ""),
    }


def _printer_state(api: Any, action: str, params: dict[str, Any]) -> dict[str, Any]:
    pid = str(params.get("printer_id") or "").strip()
    if not pid:
        return _answer(action, "printer_state", "pending",
                       reason="Нет printer_id для проверки состояния")
    manager = getattr(api, "manager", None)
    printer = manager.get(pid) if manager and hasattr(manager, "get") else None
    if not printer:
        return _answer(action, "printer_state", "failed",
                       reason="Принтер не найден после команды")
    try:
        snap = printer.snapshot()
    except Exception as exc:
        return _answer(action, "printer_state", "pending",
                       reason=f"Команда отправлена, но снимок принтера пока недоступен: {exc}")
    info = snap.get("printer") if isinstance(snap, dict) and isinstance(snap.get("printer"), dict) else snap
    info = info if isinstance(info, dict) else {}
    state = str(info.get("state") or snap.get("state") or "").upper()
    command = str(params.get("command") or "").lower()
    expected = {
        "pause": {"PAUSED", "PAUSE"},
        "resume": {"RUNNING", "PRINTING"},
        "stop": {"IDLE", "FINISH", "FINISHED", "FAILED", "CANCELLED"},
    }.get(command)
    evidence = {"printer_id": pid, "command": command, "state": state}
    if not expected:
        return _answer(action, "printer_state", "verified", evidence=evidence,
                       reason="Команда принята; для неё нет отдельного конечного состояния")
    if state in expected:
        return _answer(action, "printer_state", "verified", evidence=evidence)
    return _answer(action, "printer_state", "pending", evidence=evidence,
                   reason="Принтер ещё не прислал ожидаемое состояние")


def _job_state(api: Any, action: str, params: dict[str, Any]) -> dict[str, Any]:
    job_id = str(params.get("id") or "").strip()
    if not job_id:
        return _answer(action, "job_state", "failed", reason="Нет id задания")
    row = api.db.one("SELECT id,state,printer_id FROM print_jobs WHERE id=?", (job_id,))
    if not row:
        return _answer(action, "job_state", "failed", reason="Задание не найдено после действия")
    state = str(row.get("state") or "").lower()
    evidence = {"job_id": job_id, "state": state, "printer_id": row.get("printer_id") or ""}
    if action == "job_cancel":
        if state == "cancelled":
            return _answer(action, "job_state", "verified", evidence=evidence)
        return _answer(action, "job_state", "pending", evidence=evidence,
                       reason="Отмена ещё не отражена в состоянии задания")
    if action == "job_start":
        if state in {"starting", "running", "done"}:
            return _answer(action, "job_state", "verified", evidence=evidence)
        return _answer(action, "job_state", "pending", evidence=evidence,
                       reason="Запуск принят, но задание ещё не перешло в печать")
    return _answer(action, "job_state", "verified", evidence=evidence)


def _order_readback(api: Any, action: str, params: dict[str, Any],
                    result: dict[str, Any]) -> dict[str, Any]:
    order_id = str(params.get("id") or "").strip()
    if not order_id and isinstance(params.get("draft"), dict):
        order_id = str(params["draft"].get("id") or "").strip()
    if not order_id:
        nested = result.get("order")
        if isinstance(nested, dict):
            order_id = str(nested.get("id") or "").strip()
    if not order_id:
        order_id = str(result.get("id") or "").strip()
    if not order_id:
        return _answer(action, "order_readback", "failed",
                       reason="Ответ не содержит id заказа")
    row = api.repo.order(order_id) if getattr(api, "repo", None) else None
    if not row:
        return _answer(action, "order_readback", "failed",
                       reason="Заказ не найден после действия")
    evidence = {
        "order_id": order_id,
        "number": row.get("number") or "",
        "status": row.get("status") or "",
    }
    if action == "order_status":
        expected = str(params.get("status") or "")
        if expected and str(row.get("status") or "") != expected:
            return _answer(action, "order_readback", "failed", evidence=evidence,
                           reason="Статус заказа не совпал с запрошенным")
    if action == "order_fulfill":
        meta = api.db.one("SELECT is_final FROM statuses WHERE id=?", (row.get("status"),)) or {}
        delivered = bool(str(row.get("client_delivered_at") or "").strip())
        evidence["delivered"] = delivered
        evidence["final"] = bool(num(meta.get("is_final")))
        if not evidence["final"] or not delivered:
            return _answer(action, "order_readback", "failed", evidence=evidence,
                           reason="Выдача не подтверждена финальным состоянием заказа")
    return _answer(action, "order_readback", "verified", evidence=evidence)


def _shelf_sync(api: Any, action: str, params: dict[str, Any],
                result: dict[str, Any], verification: str) -> dict[str, Any]:
    nested = result.get("item") if isinstance(result.get("item"), dict) else {}
    item_id = str(params.get("item_id") or nested.get("id") or "").strip()
    if not item_id:
        return _answer(action, verification, "failed",
                       reason="Нет позиции стеллажа для проверки")
    item = api.db.one("SELECT * FROM shelf_items WHERE id=?", (item_id,))
    if not item:
        return _answer(action, verification, "failed",
                       reason="Позиция стеллажа не найдена после действия")

    evidence: dict[str, Any] = {
        "item_id": item_id,
        "shelf_qty": round(num(item.get("qty")), 3),
    }
    if action == "shelf_inventory":
        actual = num(params.get("actual"))
        evidence["actual"] = round(actual, 3)
        if abs(num(item.get("qty")) - actual) > 0.001:
            return _answer(action, verification, "failed", evidence=evidence,
                           reason="Фактический остаток карточки не совпал с инвентаризацией")

    nom_id = str(item.get("nom_id") or "").strip()
    if nom_id:
        from .stock import Stock
        stock = Stock(api.db)
        zone = stock.shelf_warehouse()
        if zone:
            variant_id = str(item.get("variant_id") or "").strip()
            register_qty = stock.qty(nom_id, zone, variant_id)
            evidence.update({
                "nom_id": nom_id,
                "variant_id": variant_id,
                "register_qty": round(register_qty, 3),
                "register_synced": abs(register_qty - num(item.get("qty"))) <= 0.001,
            })
            if not evidence["register_synced"]:
                return _answer(action, verification, "failed", evidence=evidence,
                               reason="Стеллаж и складская retail-zone разошлись")

    move = result.get("move") if isinstance(result.get("move"), dict) else {}
    if move.get("id"):
        stored = api.db.one("SELECT id,kind,qty FROM shelf_moves WHERE id=?", (move["id"],))
        evidence["move_id"] = move["id"]
        if not stored:
            return _answer(action, verification, "failed", evidence=evidence,
                           reason="Движение стеллажа не найдено после операции")

    if action == "shelf_transfer_out":
        target = result.get("target_move") if isinstance(result.get("target_move"), dict) else {}
        if target.get("id"):
            stored = api.db.one(
                "SELECT id,warehouse_id,qty FROM stock_moves WHERE id=?", (target["id"],))
            evidence["target_move_id"] = target["id"]
            if not stored or str(stored.get("warehouse_id") or "") != str(params.get("warehouse_id") or ""):
                return _answer(action, verification, "failed", evidence=evidence,
                               reason="Приход на склад назначения не найден")

    return _answer(action, verification, "verified", evidence=evidence)


def _settings_readback(api: Any, action: str, params: dict[str, Any],
                       result: dict[str, Any]) -> dict[str, Any]:
    patch = params.get("patch") if isinstance(params.get("patch"), dict) else {}
    effective = result.get("settings") if isinstance(result.get("settings"), dict) else {}
    if not patch:
        return _answer(action, "settings_readback", "failed",
                       reason="Нет patch настроек для проверки")
    current = api.db.settings(include_secrets=True)
    checked: list[str] = []
    mismatched: list[str] = []
    for key in patch:
        if key not in effective:
            # Unknown keys are intentionally ignored by settings schema.
            continue
        checked.append(str(key))
        if current.get(key) != effective.get(key):
            mismatched.append(str(key))
    evidence = {"checked_keys": checked, "mismatched_keys": mismatched}
    if mismatched:
        return _answer(action, "settings_readback", "failed", evidence=evidence,
                       reason="Сохранённые настройки не совпали с ответом сервера")
    return _answer(action, "settings_readback", "verified", evidence=evidence)


def verify_action(api: Any, action_id: str, params: dict[str, Any] | None = None,
                  result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify one catalog action without performing any mutation."""
    action_id = str(action_id or "").strip()
    action = ACTIONS.get(action_id)
    if not action:
        return _answer(action_id, "", "failed", reason="Неизвестное действие PrintFlow")
    params = _payload_dict(params)
    result = _action_result(result)
    contract = action_contract(action_id, action)
    verification = str(contract.get("verification") or "audit")

    if str(action.get("method") or "GET").upper() == "GET":
        return _answer(action_id, verification, "verified",
                       reason="Чтение подтверждено успешным ответом API")
    if result.get("ok") is False:
        return _answer(action_id, verification, "failed",
                       reason=str(result.get("error") or result.get("reason") or "Действие вернуло ошибку"))
    if verification == "printer_state":
        return _printer_state(api, action_id, params)
    if verification == "job_state":
        return _job_state(api, action_id, params)
    if verification == "order_readback":
        return _order_readback(api, action_id, params, result)
    if verification in {"shelf_readback", "shelf_and_stock_readback"}:
        return _shelf_sync(api, action_id, params, result, verification)
    if verification == "settings_readback":
        return _settings_readback(api, action_id, params, result)

    # Some writes have no stronger deterministic invariant yet.  Do not invent
    # one: confirm only that the route returned successfully and leave evidence
    # expansion to the domain adapter when the action is promoted.
    return _answer(action_id, verification, "verified",
                   reason="Маршрут завершился успешно; отдельный readback не требуется")
