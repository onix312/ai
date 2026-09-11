"""Откуда списывать материалы и расходники при производстве.

Производство делает две вещи: приходует готовое изделие и списывает состав по
спецификации. До этого раунда **и то и другое шло с одного склада** — того, куда
кладётся изделие. Из-за этого расходник, лежащий на своём складе («Материалы»,
«Домашний склад»), для производства был невидим: проведение падало с «не
хватает… есть 0», хотя на складе материалов лежала сотня штук. Единственным
обходным путём было переложить расходник на склад изделия, чего никто делать не
должен.

Правило теперь простое и явное:

1. **Место указано** — в составе на строку расходника выбран склад
   (`spec_items.warehouse_id`). Списываем строго оттуда. Если там не хватает —
   это ошибка с планом, а не тихое списание из другого места (иначе деньги
   «найдутся» не там, где лежали).
2. **Места нет** — берём склад материалов по остатку: сначала склады видов
   `MATERIAL_KINDS` (материалы, домашний), при равенстве — тот, где есть
   свободный остаток; если такого нет — склад изделия (старое поведение).
   Автовыбор запоминается в составе на будущее.
3. **Витрина не участвует.** С витрины списывает полка, остатки зоны ведёт
   `shelf_items`; проведение документа по ней запрещено и раньше
   (`documents._no_shelf_zone`) — здесь это же правило распространяется на
   склад-источник расходника.

При нехватке модуль не «дорисовывает» остаток: он собирает план и отдаёт его
наверх. Панель показывает план человеку: либо списать из минуса осознанно
(`allow_shortage`), либо переложить расходник и повторить. При списании в минус
движение помечается пометкой «в минус», а себестоимость считается по последней
цене прихода — цифра в учёте не обнуляется.
"""
from __future__ import annotations

from typing import Any, Iterable

from .accounting import num

#: Виды складов, где расходники лежат по умолчанию, в порядке предпочтения.
MATERIAL_KINDS = ("material", "home")

#: Виды складов, с которых производство списывать не может: витрину ведёт
#: полка, транзит — перемещение (документы по ним и так запрещены).
FORBIDDEN_KINDS = ("shelf", "transit")

#: Пометка движения при осознанном списании в минус.
SHORT_NOTE = "в минус"


class MaterialShortage(ValueError):
    """Материала не хватает в том месте, где он по плану.

    Несёт готовый план (`plan`), чтобы панель показала человеку, чего и где не
    хватает, а не просто «ошибка 400».
    """

    def __init__(self, plan: dict, message: str = ""):
        super().__init__(message or plan.get("message") or "Материала не хватает")
        self.plan = plan


def _warehouse(db, warehouse_id: str) -> dict:
    return db.one("SELECT id, name, kind, archived FROM warehouses WHERE id=?",
                  (warehouse_id,)) or {}


def component_places(db, nom_ids: Iterable[str]) -> dict[str, str]:
    """Места из состава: `nom_id` расходника -> склад (`''`, если не указан).

    Место живёт на строке спецификации: у разных изделий один и тот же крепёж
    может лежать в разных местах, а «где лежит вообще» панель показывает как
    состав конкретного изделия.
    """
    ids = [str(x) for x in nom_ids if x]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.query(
        f"SELECT nom_id, warehouse_id FROM spec_items"
        f" WHERE nom_id IN ({marks}) AND COALESCE(warehouse_id,'')<>''"
        f" ORDER BY rowid", ids)
    out: dict[str, str] = {}
    for row in rows:
        out.setdefault(str(row["nom_id"]), str(row.get("warehouse_id") or ""))
    return out


def _candidates(db) -> list[dict]:
    """Склады-кандидаты для расходников: активные, не витрина и не транзит."""
    marks = ",".join("?" for _ in MATERIAL_KINDS)
    return db.query(
        f"SELECT id, name, kind, position FROM warehouses"
        f" WHERE archived=0 AND kind IN ({marks})"
        f" ORDER BY position, name", MATERIAL_KINDS)


def pick_place(db, stock, nom_id: str, explicit: str = "", primary: str = "") -> dict:
    """Где лежит расходник: явное место, иначе склад материалов, иначе склад изделия.

    Возвращает `{warehouse_id, name, kind, reason, free}`: `reason` объясняет
    выбор и попадает в план — человек должен видеть, почему списали именно тут.
    """
    if explicit:
        row = _warehouse(db, explicit)
        if not row or num(row.get("archived")):
            raise MaterialShortage(
                {"lines": [], "short": [], "ok": False},
                f"Склад расходника «{explicit}» не найден или удалён — "
                "укажите место в составе заново")
        kind = str(row.get("kind") or "")
        if kind in FORBIDDEN_KINDS:
            raise MaterialShortage(
                {"lines": [], "short": [], "ok": False},
                f"«{row.get('name') or explicit}» — витрина: остатки ведёт полка. "
                "Выберите для расходника склад материалов")
        return {"warehouse_id": explicit, "name": str(row.get("name") or explicit),
                "kind": kind, "reason": "место указано в составе",
                "free": stock.free(nom_id, explicit)}

    candidates = _candidates(db)
    if candidates:
        with_stock = []
        for row in candidates:
            free = stock.free(nom_id, str(row["id"]))
            if free > 1e-9:
                with_stock.append((row, free))
        if with_stock:
            # Свободный остаток важнее «правильного вида»: если расходник лежит
            # на домашнем складе, а «Материалы» пусты — списываем откуда есть.
            row, free = max(with_stock, key=lambda pair: (num(pair[1]), -num(pair[0].get("position"))))
            return {"warehouse_id": str(row["id"]), "name": str(row.get("name") or ""),
                    "kind": str(row.get("kind") or ""),
                    "reason": f"выбран склад с остатком · {round(free, 3)} свободно",
                    "free": free}
        row = candidates[0]
        return {"warehouse_id": str(row["id"]), "name": str(row.get("name") or ""),
                "kind": str(row.get("kind") or ""),
                "reason": "склад материалов пуст — списание уйдёт в минус",
                "free": stock.free(nom_id, str(row["id"]))}

    row = _warehouse(db, primary)
    return {"warehouse_id": primary, "name": str(row.get("name") or primary or "склад"),
            "kind": str(row.get("kind") or ""),
            "reason": "складов материалов нет — списываю со склада изделия",
            "free": stock.free(nom_id, primary) if primary else stock.free(nom_id, "")}


def aggregate(needs: Iterable[dict]) -> list[dict]:
    """Сложить потребность по одному расходнику: две строки состава — одно списание.

    Строки помним списком (`lines`), чтобы автовыбор места можно было записать
    обратно ровно в те строки состава, из которых он пришёл.
    """
    merged: dict[str, dict] = {}
    for raw in needs:
        nom_id = str(raw.get("nom_id") or "")
        qty = num(raw.get("qty"))
        if not nom_id or qty <= 0:
            continue
        row = merged.setdefault(nom_id, {
            "nom_id": nom_id, "need": 0.0, "name": str(raw.get("name") or ""),
            "unit": str(raw.get("unit") or ""), "lines": [], "chosen": ""})
        row["need"] = round(row["need"] + qty, 3)
        if raw.get("line_id"):
            row["lines"].append(str(raw["line_id"]))
        if not row["chosen"] and raw.get("chosen"):
            row["chosen"] = str(raw["chosen"])
        if not row["name"] and raw.get("name"):
            row["name"] = str(raw["name"])
    return list(merged.values())


def plan(db, stock, needs: Iterable[dict], primary: str = "", *,
         allow_shortage: bool = False) -> dict:
    """План списания: что, откуда, сколько и чего не хватает.

    `needs` — потребность производства: `{nom_id, qty, name?, unit?, line_id?,
    chosen?}`. Явное место (`chosen`) важнее автовыбора. При `allow_shortage`
    план разрешает списать доступное и увести остаток в минус — это осознанное
    решение человека, панель показывает его до подтверждения.
    """
    components = aggregate(needs)
    if not components:
        return {"ok": True, "lines": [], "short": [], "warehouses": [],
                "message": "", "allow_shortage": bool(allow_shortage)}

    lines: list[dict] = []
    short: list[dict] = []
    for comp in components:
        nom = db.one("SELECT name, unit FROM nomenclature WHERE id=?",
                     (comp["nom_id"],)) or {}
        place = pick_place(db, stock, comp["nom_id"], comp["chosen"], primary)
        need = round(comp["need"], 3)
        free = round(num(place["free"]), 3)
        take = min(need, max(0.0, free))
        missing = round(need - take, 3)
        line = {
            "nom_id": comp["nom_id"],
            "name": comp["name"] or str(nom.get("name") or comp["nom_id"]),
            "unit": comp["unit"] or str(nom.get("unit") or "шт"),
            "need": need, "take": take, "missing": missing,
            "warehouse_id": place["warehouse_id"], "warehouse_name": place["name"],
            "warehouse_kind": place["kind"], "reason": place["reason"],
            "free": free, "explicit": bool(comp["chosen"]),
            "lines": comp["lines"],
            "unit_cost": stock.avg_cost(comp["nom_id"], place["warehouse_id"])
                          or stock.avg_cost(comp["nom_id"], ""),
        }
        lines.append(line)
        if missing > 1e-9:
            short.append(line)

    message = ""
    if short:
        first = short[0]
        message = (f"Не хватает «{first['name']}»: нужно {first['need']} "
                   f"{first['unit']}, на складе «{first['warehouse_name']}» "
                   f"свободно {first['free']}")
        if len(short) > 1:
            message += f" (и ещё {len(short) - 1} позиц.)"
        if first["explicit"]:
            message += (". Расходник указан на этот склад в составе — переложите "
                        "его туда или поменяйте место")
        else:
            message += ". Переложите расходник на склад материалов или укажите место в составе"

    return {
        "ok": not short or bool(allow_shortage),
        "lines": lines,
        "short": short,
        "warehouses": sorted({line["warehouse_id"] for line in lines if line["warehouse_id"]}),
        "message": message,
        "allow_shortage": bool(allow_shortage),
    }


def remember_places(db, planned: dict, *, only_auto: bool = True) -> int:
    """Запомнить выбранные места в составе, чтобы в следующий раз было видно.

    Пишем только автовыбор (`only_auto`) и только когда место определилось не
    «аварийно»: склад изделия и пустой склад материалов в память не попадают,
    иначе один неудачный запуск закрепил бы неправильное место. Явно указанные
    человеком места и так уже в составе.
    """
    saved = 0
    for line in planned.get("lines") or []:
        place = str(line.get("warehouse_id") or "")
        kind = str(line.get("warehouse_kind") or "")
        if not place or line.get("explicit"):
            continue
        if only_auto and kind not in MATERIAL_KINDS:
            continue
        for line_id in line.get("lines") or []:
            db.execute("UPDATE spec_items SET warehouse_id=? WHERE id=? AND "
                       "COALESCE(warehouse_id,'')=''", (place, line_id))
            saved += 1
    return saved
