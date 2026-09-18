"""Номенклатура PrintFlow 3.0 — единый справочник товаров.

Заменяет разрозненные `catalog` (модель) и `shelf_items` (полка): теперь это
одна карточка, у которой есть нормативы производства, остатки по складам,
цены по типам и экономика. Остаток берётся из регистра `stock_moves`,
а не хранится полем.
"""
from __future__ import annotations

from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso
from .db import Database
from .schema_v3 import NOM_KINDS
from .stock import Stock


# Виды, участвующие в производственном учёте (остатки, план пополнения,
# замороженный капитал). «Для магазина» (showcase) сознательно исключена:
# это витрина, а не товар для печати.
GOOD_KINDS = {"product", "kit", "semi"}

# Вариации: предел на один товар и предел осей. Ограничения нужны не ради
# экономии места, а чтобы ошибка ввода («случайно перемножили три оси по
# сорок значений») не превратилась в пять тысяч строк, которые потом не
# удалить из интерфейса.
MAX_VARIANTS_TOTAL = 2000
MAX_AXES = 6

# Галерея вариации (18.6): обложка + кадры. Шесть кадров хватает на товар
# с четырёх сторон, а телефон владельца не превращается в фотоархив.
VARIANT_GALLERY_MAX = 6

_SLUG_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_AXIS_KINDS = (
    ("color", ("цвет", "color", "колер", "расцветка")),
    ("size", ("размер", "size", "габарит", "объём", "объем")),
    ("material", ("пластик", "материал", "material", "филамент")),
)


def variant_slug(value: str) -> str:
    """Артикул из человеческого названия: «Чёрный матовый» → «chernyi-matovyi»."""
    out = []
    for ch in str(value or "").casefold():
        if ch in _SLUG_MAP:
            out.append(_SLUG_MAP[ch])
        elif ch.isalnum() and ch.isascii():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


def variant_label(variant: dict | None) -> str:
    """Подпись вариации для документов, стеллажа и кассы: «Красный · L».

    Собирается из признаков, а не из имени: владелец может переименовать
    вариацию, а цвет с размером остаются тем, чем один товар отличается от
    другого — и в складском документе, и на полке.
    """
    if not variant:
        return ""
    bits = [str(variant.get("color_name") or "").strip(),
            str(variant.get("size") or "").strip(),
            str(variant.get("material") or "").strip()]
    label = " · ".join(bit for bit in bits if bit)
    return label or str(variant.get("name") or "").strip()


def axis_kind(name: str) -> str:
    """Что за ось: цвет, размер, пластик или просто признак."""
    low = str(name or "").casefold()
    for kind, words in _AXIS_KINDS:
        if any(word in low for word in words):
            return kind
    return ""


def _axis_value(axes: list[dict], combo: list[dict], kind: str,
                field: str) -> str:
    """Значение оси нужного типа из конкретного сочетания."""
    for axis, part in zip(axes, combo):
        if axis_kind(axis.get("name")) == kind:
            return str(part.get(field) or "")
    return ""



class Nomenclature:
    """Справочник номенклатуры: карточки, группы, цены, спецификации."""

    def __init__(self, db: Database):
        self.db = db
        self.stock = Stock(db)
        self.acc = Accounting(db)

    # ------------------------------------------------------------ код позиции
    def next_code(self) -> str:
        row = self.db.one("SELECT COUNT(*) n FROM nomenclature") or {}
        return f"{int(num(row.get('n'))) + 1:06d}"

    # -------------------------------------------------------------- список
    def items(self, group_id: str = "", kind: str = "", search: str = "",
              warehouse_id: str = "", include_archived: bool = False) -> list[dict]:
        sql = "SELECT * FROM nomenclature WHERE 1=1"
        params: list[Any] = []
        if not include_archived:
            sql += " AND archived=0"
        if group_id:
            groups = self._group_tree(group_id)
            sql += f" AND group_id IN ({','.join('?' * len(groups))})"
            params += groups
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if search:
            like = f"%{search.lower()}%"
            sql += (" AND (pylower(name) LIKE ? OR pylower(sku) LIKE ?"
                    " OR pylower(code) LIKE ? OR pylower(barcode) LIKE ?)")
            params += [like, like, like, like]
        sql += " ORDER BY name"
        rows = self.db.query(sql, params)

        balances = self.stock.balances(warehouse_id)
        prices = self._all_prices()
        target = num(self.db.setting("target_profit_per_hour", 250), 250)
        # Один проход по регистру вместо запросов на каждую строку списка:
        # статистика продаж, резервы и базовый тип цен — массово.
        stats_map = self.stock.sales_stats_all()
        reserved_map = self.stock.reserved_all(warehouse_id)
        manual_map = self.stock.manual_stats(days=7)["per_nom"]  # идея 5
        base_type = self._base_type()
        out = []
        for row in rows:
            out.append(self._decorate(row, balances, prices, target, warehouse_id,
                                      stats_map=stats_map, reserved_map=reserved_map,
                                      base_type=base_type,
                                      manual_map=manual_map))
        return out

    def _decorate(self, row: dict, balances: dict, prices: dict,
                  target: float, warehouse_id: str = "",
                  stats_map: dict | None = None,
                  reserved_map: dict | None = None,
                  base_type: str = "",
                  manual_map: dict | None = None) -> dict:
        nom_id = row["id"]
        bal = balances.get(nom_id) or {"qty": 0.0, "value": 0.0, "cost": 0.0}
        qty = num(bal["qty"])
        stats = ((stats_map or {}).get(nom_id)
                 or {"sold_7": 0.0, "sold_30": 0.0, "rate_per_day": 0.0, "last_sale": "",
                     "sold_days": []})
        status, days_left, plan = self.stock.status_of(
            qty, num(row.get("min_qty")), stats, num(row.get("max_qty")))
        reserved = (num((reserved_map or {}).get(nom_id))
                    if reserved_map is not None
                    else self.stock.reserved(nom_id, warehouse_id))
        item_prices = prices.get(nom_id, {})
        price = num(item_prices.get(base_type or self._base_type()))
        # Остаток со склада → записанная с/с карточки → норматив по граммам/часам.
        display_only = str(row.get("kind") or "product") == "showcase"
        # Витринная позиция не участвует в тревогах запасов и плане пополнения:
        # это витрина, а не товар на производство.
        if display_only:
            status, days_left, plan = "none", None, 0
        cost = 0.0 if display_only else (num(bal["cost"]) or num(row.get("cost")) or self._norm_cost(row))
        margin = 0.0 if display_only else (price - cost if price else 0.0)
        hours = num(row.get("hours"))
        profit_per_hour = round(margin / hours, 2) if hours else 0.0
        return {
            **row,
            "kind_label": NOM_KINDS.get(row.get("kind") or "product", "Товар"),
            "qty": qty,
            "reserved": reserved,
            "free": round(qty - reserved, 3),
            "stock_value": num(bal["value"]),
            "cost": round(cost, 2),
            "price": price,
            "prices": item_prices,
            "margin": round(margin, 2),
            "margin_pct": round(margin / price * 100, 1) if price and not display_only else 0.0,
            "profit_per_hour": profit_per_hour,
            "profitable": None if display_only else (profit_per_hour >= target if hours else None),
            "display_only": display_only,
            "sold_7": stats["sold_7"],
            "sold_30": stats["sold_30"],
            "rate_per_day": stats["rate_per_day"],
            "last_sale": stats["last_sale"],
            "sold_days": stats.get("sold_days") or [],
            "days_left": days_left,
            "status": status,
            "plan_qty": plan,
            # ручные списания за 7 дней (идея 5): видно потери на плитке
            "manual_written": (manual_map or {}).get(nom_id, {}).get("qty", 0.0),
            "manual_written_value": (manual_map or {}).get(nom_id, {}).get("value", 0.0),
        }

    def _norm_cost(self, row: dict) -> float:
        """Нормативная себестоимость по граммам и часам, если факта ещё нет.

        Нормативы карточки — на штуку. Если заданы вес, время и вместимость
        плиты, считаем полную плиту и берём себестоимость штуки: так материал
        и часы печати делятся на fit, а цена катушки берётся из справочника.

        Категория «Для магазина» (showcase) — витрина без учёта: нормативная
        себестоимость не считается вообще.
        """
        if str(row.get("kind") or "product") == "showcase":
            return 0.0
        grams = num(row.get("grams"))
        hours = num(row.get("hours"))
        if not grams and not hours:
            return 0.0
        fit = max(1, int(num(row.get("fit_per_plate"), 1) or 1))
        kwargs = {
            "manual_minutes": num(row.get("post_minutes")),
            "material": str(row.get("material") or ""),
            "qty": 1.0,
            "fit_per_plate": fit,
        }
        if grams > 0 and hours > 0:
            kwargs["plate_grams"] = grams * fit
            kwargs["plate_hours"] = hours * fit
            kwargs["qty"] = float(fit)
            kwargs["warmup_minutes"] = 0.0
        br = self.acc.cost_breakdown(grams, hours, **kwargs)
        return round(num(br.get("per_unit") or br.get("total")), 2)

    def _base_type(self) -> str:
        row = self.db.one("SELECT id FROM price_types WHERE is_base=1 LIMIT 1")
        return (row or {}).get("id") or "retail"

    def _prices_of(self, nom_id: str) -> dict[str, float]:
        """Последние цены одной позиции по всем типам цен (точечный запрос)."""
        out: dict[str, float] = {}
        for row in self.db.query(
                "SELECT price_type_id, price FROM prices"
                " WHERE nom_id=? AND (variant_id IS NULL OR variant_id='')"
                " ORDER BY datetime(at) DESC, rowid DESC", (nom_id,)):
            if row["price_type_id"] not in out:
                out[row["price_type_id"]] = round(num(row["price"]), 2)
        return out

    def _all_prices(self) -> dict[str, dict[str, float]]:
        """Последние цены всех позиций по всем типам цен.

        Правило «последней цены» совпадает с Documents.price_of: сначала
        свежесть datetime(at), при равенстве — максимальный rowid. Раньше
        выбирался просто max(rowid), и запись, внесённая задним числом,
        перебивала актуальную цену."""
        rows = self.db.query(
            "SELECT nom_id, price_type_id, price FROM prices p"
            " WHERE (p.variant_id IS NULL OR p.variant_id='') AND p.rowid=("
            "   SELECT p2.rowid FROM prices p2"
            "   WHERE p2.nom_id=p.nom_id AND (p2.variant_id IS NULL OR p2.variant_id='')"
            "     AND p2.price_type_id=p.price_type_id"
            "   ORDER BY datetime(p2.at) DESC, p2.rowid DESC LIMIT 1)")
        out: dict[str, dict[str, float]] = {}
        for row in rows:
            out.setdefault(row["nom_id"], {})[row["price_type_id"]] = round(num(row["price"]), 2)
        return out

    def _group_tree(self, group_id: str) -> list[str]:
        """Группа со всеми вложенными подгруппами."""
        result = [group_id]
        queue = [group_id]
        while queue:
            current = queue.pop()
            for row in self.db.query("SELECT id FROM nom_groups WHERE parent_id=?", (current,)):
                if row["id"] not in result:
                    result.append(row["id"])
                    queue.append(row["id"])
        return result

    # -------------------------------------------------------------- карточка
    def resolve_id(self, nom_id: str) -> str:
        """Канонический id карточки: сам id, legacy_catalog_id или catalog.id."""
        key = str(nom_id or "").strip()
        if not key or key.startswith("[object "):
            return ""
        row = self.db.one("SELECT id FROM nomenclature WHERE id=?", (key,))
        if row:
            return row["id"]
        row = self.db.one(
            "SELECT id FROM nomenclature WHERE legacy_catalog_id=?", (key,))
        if row:
            return row["id"]
        row = self.db.one("SELECT nom_id FROM catalog WHERE id=?", (key,))
        if row and row.get("nom_id"):
            return str(row["nom_id"])
        return ""

    def item(self, nom_id: str) -> dict | None:
        resolved = self.resolve_id(nom_id)
        if not resolved:
            return None
        row = self.db.one("SELECT * FROM nomenclature WHERE id=?", (resolved,))
        if not row:
            return None
        # Точечные запросы одной карточки: не сворачиваем регистр и цены
        # по всему справочнику, чтобы открыть одну позицию.
        balances = self.stock.balances(nom_id=resolved)
        stats = {resolved: self.stock.sales_stats(resolved)}
        item = self._decorate(row, balances, {resolved: self._prices_of(resolved)},
                              num(self.db.setting("target_profit_per_hour", 250), 250),
                              stats_map=stats, base_type=self._base_type())
        item["warehouses"] = self.stock.by_warehouse(resolved)
        item["moves"] = self.stock.moves(resolved, limit=50)
        item["variants"] = self.db.query(
            "SELECT * FROM nom_variants WHERE nom_id=? AND archived=0 ORDER BY position, name",
            (resolved,))
        # М2: состав каждой вариации (несколько катушек × граммы). Пустой —
        # вариация однокатушечная, старое поведение полностью сохраняется.
        item["variant_structures"] = {
            v["id"]: self.variant_structures(v["id"]) for v in item["variants"]
        }
        item["price_history"] = self.db.query(
            "SELECT p.*, t.name type_name FROM prices p"
            " LEFT JOIN price_types t ON t.id=p.price_type_id"
            " WHERE p.nom_id=? ORDER BY datetime(p.at) DESC LIMIT 30", (resolved,))
        item["spec"] = self.spec_of(resolved)
        item["batches"] = self.db.query(
            "SELECT * FROM batches WHERE nom_id=? ORDER BY datetime(at) DESC LIMIT 20",
            (resolved,))
        item["fact"] = self._fact_stats(resolved)
        return item

    def _fact_stats(self, nom_id: str) -> dict:
        """Факт против норматива по завершённым партиям."""
        row = self.db.one(
            "SELECT COUNT(*) n, COALESCE(AVG(NULLIF(est_grams,0)),0) g,"
            " COALESCE(AVG(NULLIF(est_minutes,0)),0) m,"
            " COALESCE(SUM(qty_done),0) done, COALESCE(SUM(qty_scrap),0) scrap"
            " FROM batches WHERE nom_id=? AND state IN ('done','partial')",
            (nom_id,)) or {}
        done = num(row.get("done"))
        scrap = num(row.get("scrap"))
        return {
            "batches": int(num(row.get("n"))),
            "avg_grams": round(num(row.get("g")), 1),
            "avg_minutes": round(num(row.get("m")), 1),
            "produced": round(done, 1),
            "scrap": round(scrap, 1),
            "scrap_pct": round(scrap / (done + scrap) * 100, 1) if (done + scrap) else 0.0,
        }

    # --------------------------------------------------------------- запись
    def save(self, data: dict) -> dict:
        data = dict(data)
        prices = data.pop("prices", None)
        expected_updated_at = str(data.pop("expected_updated_at", "") or "").strip()
        new = not data.get("id")
        if not new and expected_updated_at:
            current = self.db.one("SELECT updated_at FROM nomenclature WHERE id=?", (data["id"],))
            if not current:
                raise ValueError("Позиция номенклатуры не найдена")
            if expected_updated_at != str(current.get("updated_at") or ""):
                raise ValueError("Позиция уже изменена — обновите карточку перед сохранением")
        if new:
            data["id"] = uid("nom")
            data["created_at"] = now_iso()
            data.setdefault("code", self.next_code())
        data["updated_at"] = now_iso()
        if not (data.get("name") or "").strip():
            raise ValueError("Укажите наименование")
        for key in ("grams", "hours", "post_minutes", "min_qty", "max_qty", "vat"):
            if key in data:
                data[key] = round(num(data[key]), 3)
        if "fit_per_plate" in data:
            data["fit_per_plate"] = max(1, int(num(data["fit_per_plate"], 1)))

        # Нормативная с/с, пока нет факта с партии. После приёмки партии
        # update_cost_from_batch пишет фактическую — её не затираем.
        prev = {} if new else (self.db.one(
            "SELECT * FROM nomenclature WHERE id=?", (data["id"],)) or {})
        if num(data.get("cost")) <= 0:
            has_fact = False
            if not new:
                fact = self._fact_stats(data["id"])
                has_fact = int(fact.get("batches") or 0) > 0 and num(prev.get("cost")) > 0
            if has_fact:
                data.pop("cost", None)
            else:
                data["cost"] = self._norm_cost({**prev, **data})

        with self.db.transaction():
            row = self.db.upsert("nomenclature", data)
            if isinstance(prices, dict):
                for type_id, value in prices.items():
                    if value in ("", None):
                        continue
                    self.set_price(row["id"], num(value), type_id)
        # Legacy catalog — зеркало, а не второй источник. При редактировании
        # canonical карточки поддерживаем старый экран синхронным.
        legacy = self.db.one("SELECT id, price FROM catalog WHERE nom_id=? OR id=("
                             "SELECT legacy_catalog_id FROM nomenclature WHERE id=?) LIMIT 1",
                             (row["id"], row["id"]))
        if legacy:
            self.db.execute(
                "UPDATE catalog SET name=?, niche_id=?, grams=?, hours=?, fit_per_plate=?,"
                " material=?, file=?, notes=?, nom_id=?, updated_at=? WHERE id=?",
                (row.get("name") or "", row.get("niche_id") or "", num(row.get("grams")),
                 num(row.get("hours")), max(1, int(num(row.get("fit_per_plate"), 1))),
                 row.get("material") or "", row.get("file") or "", row.get("note") or "",
                 row["id"], now_iso(), legacy["id"]))
        self.db.add_event("nom", "Номенклатура создана" if new else "Номенклатура изменена",
                          row.get("name") or "", data={"nom_id": row["id"]})
        return self.item(row["id"]) or row

    def archive(self, nom_id: str, archived: bool = True) -> dict:
        self.db.execute("UPDATE nomenclature SET archived=?, updated_at=? WHERE id=?",
                        (1 if archived else 0, now_iso(), nom_id))
        return self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,)) or {}

    def delete(self, nom_id: str) -> None:
        """Удалить можно только позицию без движений — иначе архивируем."""
        row = self.db.one("SELECT COUNT(*) n FROM stock_moves WHERE nom_id=?", (nom_id,)) or {}
        if int(num(row.get("n"))):
            self.archive(nom_id, True)
            return
        with self.db.transaction():
            self.db.execute("DELETE FROM prices WHERE nom_id=?", (nom_id,))
            self.db.execute("DELETE FROM nom_variants WHERE nom_id=?", (nom_id,))
            self.db.delete("nomenclature", nom_id)

    # ----------------------------------------------------------------- цены
    def set_price(self, nom_id: str, price: float, price_type_id: str = "",
                  note: str = "", variant_id: str = "") -> dict:
        price_type_id = price_type_id or self._base_type()
        if variant_id:
            current = self.db.one(
                "SELECT price FROM prices WHERE nom_id=? AND variant_id=? AND price_type_id=?"
                " ORDER BY datetime(at) DESC, rowid DESC LIMIT 1",
                (nom_id, variant_id, price_type_id))
        else:
            current = self.db.one(
                "SELECT price FROM prices WHERE nom_id=? AND (variant_id IS NULL OR variant_id='')"
                " AND price_type_id=? ORDER BY datetime(at) DESC, rowid DESC LIMIT 1",
                (nom_id, price_type_id))
        if current and abs(num(current["price"]) - num(price)) < 0.005:
            return dict(current)
        return self.db.upsert("prices", {
            "id": uid("prc"), "at": now_iso(), "nom_id": nom_id,
            "variant_id": variant_id or None, "price_type_id": price_type_id,
            "price": round(num(price), 2), "note": note})

    def _suggest_price(self, item: dict, ptype: dict, price_type_id: str,
                       base_type: str) -> float:
        """Рассчитать одну цену без записи в БД."""
        cost = num(item.get("cost"))
        if cost <= 0:
            return 0.0
        group = self.db.one("SELECT markup FROM nom_groups WHERE id=?",
                            (item.get("group_id"),)) if item.get("group_id") else None
        markup = num((group or {}).get("markup")) or num(ptype.get("markup"))
        raw = cost * (1 + markup / 100.0)
        # Целевую прибыль за час применяем только к основной розничной цене.
        hours = num(item.get("hours"))
        if hours and price_type_id == base_type:
            raw = max(raw, cost + num(self.db.setting("target_profit_per_hour", 250), 250) * hours)
        rounding = max(1.0, num(self.db.setting("price_rounding", 10), 10))
        return round(-(-raw // rounding) * rounding, 2)

    def recalc_price(self, nom_id: str, price_type_id: str = "") -> dict:
        """Пересчитать цену одной позиции и записать изменение в историю.

        Пустой ``price_type_id`` означает все активные типы цен этой позиции.
        Это отдельная операция от массового пересчёта: карточка товара не может
        случайно изменить остальные товары.
        """
        item = self.item(nom_id)
        if not item:
            raise ValueError("Позиция номенклатуры не найдена")
        if item.get("kind") in ("service", "showcase"):
            reason = "Услуга не пересчитывается по себестоимости" if item.get("kind") == "service" \
                else "Витринная позиция «Для магазина» не пересчитывается — цена задаётся вручную"
            return {"ok": True, "changed": 0, "prices": {}, "reason": reason}
        cost = num(item.get("cost"))
        if cost <= 0:
            return {"ok": True, "changed": 0, "prices": {},
                    "reason": "Нет себестоимости: укажите нормативы или проведите партию"}
        if price_type_id:
            types = [self.db.one("SELECT * FROM price_types WHERE id=? AND archived=0",
                                 (price_type_id,))]
            if not types[0]:
                raise ValueError("Тип цен не найден")
        else:
            types = self.db.query("SELECT * FROM price_types WHERE archived=0 ORDER BY position, id")
        base_type = self._base_type()
        prices: dict[str, dict[str, float]] = {}
        changed = 0
        for ptype in types:
            suggested = self._suggest_price(item, ptype, ptype["id"], base_type)
            if suggested <= 0:
                continue
            old = num(item.get("prices", {}).get(ptype["id"]))
            if abs(suggested - old) >= 0.01:
                self.set_price(item["id"], suggested, ptype["id"], "автопересчёт одной позиции")
                changed += 1
            prices[ptype["id"]] = {"old": old, "price": suggested,
                                    "changed": abs(suggested - old) >= 0.01}
        return {"ok": True, "changed": changed, "prices": prices,
                "nom_id": item["id"], "cost": cost}

    def recalc_prices(self, price_type_id: str = "", group_id: str = "") -> dict:
        """Пересчитать цены от себестоимости и нормы прибыли массово."""
        price_type_id = price_type_id or self._base_type()
        ptype = self.db.one("SELECT * FROM price_types WHERE id=? AND archived=0", (price_type_id,))
        if not ptype:
            raise ValueError("Тип цен не найден")
        base_type = self._base_type()
        changed = 0
        for item in self.items(group_id=group_id):
            if item.get("kind") in ("service", "showcase") or num(item.get("cost")) <= 0:
                continue
            price = self._suggest_price(item, ptype, price_type_id, base_type)
            if abs(price - num(item.get("prices", {}).get(price_type_id))) >= 0.01:
                self.set_price(item["id"], price, price_type_id, "автопересчёт")
                changed += 1
        return {"ok": True, "changed": changed, "price_type_id": price_type_id}

    # -------------------------------------------------------------- группы
    def groups(self) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM nom_groups WHERE archived=0 ORDER BY position, name")
        counts = {r["group_id"]: int(num(r["n"])) for r in self.db.query(
            "SELECT group_id, COUNT(*) n FROM nomenclature WHERE archived=0"
            " GROUP BY group_id") if r["group_id"]}
        for row in rows:
            row["items"] = counts.get(row["id"], 0)
        return rows

    def print_groups(self) -> list[str]:
        """Существующие печатные группы мелких товаров — для подсказок ввода."""
        return [r["print_group"] for r in self.db.query(
            "SELECT DISTINCT print_group FROM nomenclature"
            " WHERE print_group<>'' ORDER BY print_group") if r["print_group"]]

    def save_group(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("id"):
            data["id"] = uid("grp")
        if not (data.get("name") or "").strip():
            raise ValueError("Укажите название группы")
        if data.get("parent_id") == data.get("id"):
            raise ValueError("Группа не может быть родителем самой себе")
        return self.db.upsert("nom_groups", data)

    def delete_group(self, group_id: str) -> None:
        kids = self.db.one("SELECT COUNT(*) n FROM nom_groups WHERE parent_id=?",
                           (group_id,)) or {}
        if int(num(kids.get("n"))):
            raise ValueError("Сначала удалите или перенесите подгруппы")
        self.db.execute("UPDATE nomenclature SET group_id=NULL WHERE group_id=?", (group_id,))
        self.db.delete("nom_groups", group_id)

    # -------------------------------------------------------- спецификации
    def spec_of(self, nom_id: str) -> dict | None:
        spec = self.db.one(
            "SELECT * FROM specs WHERE nom_id=? AND active=1 ORDER BY rowid LIMIT 1",
            (nom_id,))
        if not spec:
            return None
        spec["items"] = self.db.query(
            "SELECT si.*, n.name nom_name, n.unit, n.kind,"
            " w.name AS warehouse_name, COALESCE(w.kind,'') AS warehouse_kind"
            " FROM spec_items si"
            " LEFT JOIN nomenclature n ON n.id=si.nom_id"
            " LEFT JOIN warehouses w ON w.id=si.warehouse_id"
            " WHERE si.spec_id=? ORDER BY si.line", (spec["id"],))
        for item in spec["items"]:
            # «Где лежит этот расходник» — панель показывает остатки по складам
            # прямо в строке состава, чтобы место выбирали по факту, а не вслепую.
            item["places"] = self.stock.by_warehouse(str(item.get("nom_id") or ""))
        return spec

    def save_spec(self, data: dict) -> dict:
        """Сохранить состав изделия (один активный состав на изделие).

        Без `id` состав не создаётся заново, а обновляется: панель шлёт только
        `nom_id` и строки, и раньше каждое сохранение добавляло ещё одну
        спецификацию. Производство берёт первую активную — то есть после правки
        состава оно продолжало списывать по старой, и «поменял место расходника,
        а ничего не изменилось» выглядело как баг списания. Заодно лишние
        активные копии (следы прежних сохранений) гасятся, чтобы данные не
        двоились.
        """
        data = dict(data)
        items = data.pop("items", [])
        if not data.get("nom_id"):
            raise ValueError("Не указано изделие")
        if not data.get("id"):
            existing = self.db.one(
                "SELECT id FROM specs WHERE nom_id=? AND active=1 ORDER BY rowid LIMIT 1",
                (data["nom_id"],)) or {}
            data["id"] = str(existing.get("id") or uid("spc"))
            data["created_at"] = (self.db.one(
                "SELECT created_at FROM specs WHERE id=?", (data["id"],)) or {}).get("created_at") \
                or now_iso()
        data.setdefault("active", 1)
        with self.db.transaction():
            spec = self.db.upsert("specs", data)
            # Следы прошлых сохранений (до 17.0.14 каждая правка состава плодила
            # копию) — не удаляем, а гасим: так видно историю и нет двойных списаний.
            self.db.execute(
                "UPDATE specs SET active=0 WHERE nom_id=? AND id<>? AND active=1",
                (data["nom_id"], spec["id"]))
            self.db.execute("DELETE FROM spec_items WHERE spec_id=?", (spec["id"],))
            for index, item in enumerate(items or []):
                if not isinstance(item, dict) or not item.get("nom_id"):
                    continue
                self.db.upsert("spec_items", {
                    "id": item.get("id") or uid("spi"), "spec_id": spec["id"],
                    "line": index + 1, "nom_id": item["nom_id"],
                    "variant_id": item.get("variant_id") or None,
                    "qty": round(num(item.get("qty"), 1), 3),
                    # место расходника: '' — выберем сами (см. consumption)
                    "warehouse_id": item.get("warehouse_id") or None,
                    "note": item.get("note", "")})
        return self.spec_of(data["nom_id"]) or {}

    def delete_spec(self, spec_id: str) -> None:
        with self.db.transaction():
            self.db.execute("DELETE FROM spec_items WHERE spec_id=?", (spec_id,))
            self.db.delete("specs", spec_id)

    # ---------------------------------------------------------- варианты
    def save_variant(self, data: dict) -> dict:
        data = dict(data)
        if not data.get("nom_id"):
            raise ValueError("Не указана номенклатура")
        if not data.get("id"):
            data["id"] = uid("var")
        data.setdefault("updated_at", now_iso())
        return self.db.upsert("nom_variants", data)

    def delete_variant(self, variant_id: str) -> None:
        self.db.delete("nom_variants", variant_id)

    # --------------------------------------------------- вариации массово
    def generate_variants(self, nom_id: str, axes: list | None = None,
                          sku_prefix: str = "", preview: bool = False) -> dict[str, Any]:
        """Создать вариации по осям: цвет × размер × пластик → все сочетания.

        Один товар легко живёт в сотнях вариаций (адресник: 12 цветов ×
        5 размеров × 3 пластика = 180). Вводить их руками невозможно, поэтому
        карточка собирает их декартовым произведением осей: каждая ось — это
        список значений, на выходе — все сочетания.

        Повторные вызовы не плодят дубли: сочетание, которое уже есть (по
        имени или артикулу), пропускается. Свои цены и привязанные катушки
        существующих вариаций не трогаются.

        ``preview=True`` — только подсчёт: сколько вариаций получится и
        сколько из них уже есть. Касса не должна узнавать о 500 строках
        постфактум.
        """
        nom = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not nom:
            raise ValueError("Товар не найден")
        clean: list[dict] = []
        for axis in (axes or []):
            if not isinstance(axis, dict):
                continue
            values: list[dict] = []
            for raw in (axis.get("values") or []):
                item = raw if isinstance(raw, dict) else {"name": str(raw)}
                name = str(item.get("name") or item.get("value") or "").strip()
                if name:
                    values.append({
                        "name": name,
                        "hex": str(item.get("hex") or item.get("color_hex") or ""),
                        "material": str(item.get("material") or ""),
                        "grams": num(item.get("grams")),
                        "hours": num(item.get("hours")),
                    })
            if values:
                clean.append({"name": str(axis.get("name") or "").strip(),
                              "values": values})
        if not clean:
            raise ValueError("Добавьте хотя бы одну ось со значениями")
        if len(clean) > MAX_AXES:
            raise ValueError(f"Осей больше {MAX_AXES} — такой товар проще "
                             "разделить на два")

        combos: list[list[dict]] = [[]]
        for axis in clean:
            combos = [combo + [value] for combo in combos for value in axis["values"]]
            if len(combos) > MAX_VARIANTS_TOTAL:
                raise ValueError(
                    f"Получается больше {MAX_VARIANTS_TOTAL} вариаций "
                    f"({len(combos)}) — разбейте товар на части или уберите "
                    "лишние значения")

        existing_names = {str(r["name"] or "").casefold()
                          for r in self.db.query(
                              "SELECT name FROM nom_variants WHERE nom_id=?",
                              (nom_id,))}
        existing_skus = {str(r["sku"] or "").casefold()
                         for r in self.db.query(
                             "SELECT sku FROM nom_variants WHERE nom_id=? AND "
                             "COALESCE(sku,'')<>''", (nom_id,))}
        base = str(sku_prefix or nom.get("sku") or nom.get("code") or "").strip()
        total_now = int((self.db.one(
            "SELECT COUNT(*) n FROM nom_variants WHERE nom_id=? AND archived=0",
            (nom_id,)) or {}).get("n") or 0)

        # Авто-привязка катушки: если цвет и/или пластик сочетания однозначно
        # указывают на живую катушку склада — вариация сразу печатается с неё,
        # а цена грамма посчитается из её цены. Ровно один кандидат —
        # привязываем; ноль или несколько — оставляем человеку выбор в
        # карточке: ошибочная привязка стоит дороже её отсутствия.
        spools = self.db.query(
            "SELECT id, material, color_name FROM spools WHERE archived=0")

        def _spool_match(material: str, color: str) -> str:
            material = material.strip().casefold()
            color = color.strip().casefold()
            if not material and not color:
                return ""
            found = set()
            for sp in spools:
                if material and (sp.get("material") or "").strip().casefold() != material:
                    continue
                if color and (sp.get("color_name") or "").strip().casefold() != color:
                    continue
                found.add(sp["id"])
            return next(iter(found)) if len(found) == 1 else ""

        created: list[dict] = []
        skipped = 0
        spools_bound = 0
        for combo in combos:
            name = " / ".join(part["name"] for part in combo)
            slug = "-".join(variant_slug(part["name"]) for part in combo)
            sku = f"{base}-{slug}" if base else slug
            row = {
                "name": name,
                "color_name": _axis_value(clean, combo, "color", "name"),
                "color_hex": _axis_value(clean, combo, "color", "hex"),
                "size": _axis_value(clean, combo, "size", "name"),
                "material": _axis_value(clean, combo, "material", "name"),
                "sku": sku,
            }
            if row["name"].casefold() in existing_names or \
                    (sku and sku.casefold() in existing_skus):
                skipped += 1
                continue
            existing_names.add(row["name"].casefold())
            if sku:
                existing_skus.add(sku.casefold())
            if len(created) + skipped + total_now >= MAX_VARIANTS_TOTAL:
                skipped += 1
                continue
            row["id"] = uid("var")
            row["nom_id"] = nom_id
            row["position"] = total_now + len(created)
            for part in combo:
                if part.get("grams") and not num(row.get("grams")):
                    row["grams"] = part["grams"]
                if part.get("hours") and not num(row.get("hours")):
                    row["hours"] = part["hours"]
            # Пластик для поиска катушки: своя ось «Пластик» важнее, но и
            # значение цвета-чипа может нести материал («PLA из чипа»).
            pick_material = str(row.get("material") or next(
                (str(p.get("material") or "") for p in combo if p.get("material")),
                "") or "")
            spool_id = _spool_match(pick_material, str(row.get("color_name") or ""))
            if spool_id:
                row["spool_id"] = spool_id
                spools_bound += 1
            if not preview:
                self.db.upsert("nom_variants", row)
            created.append(row)
        if not preview and created:
            # Карточка изменилась: у товара появились новые строки.
            self.db.upsert("nomenclature", {"id": nom_id, "updated_at": now_iso()})
        return {
            "nom_id": nom_id,
            "created": 0 if preview else len(created),
            "would_create": len(created),
            "skipped": skipped,
            "total": total_now + (0 if preview else len(created)),
            "axes": [{"name": a["name"], "values": len(a["values"])} for a in clean],
            "limit": MAX_VARIANTS_TOTAL,
            "spools_bound": spools_bound,
            "preview": bool(preview),
            "items": created[:50],
        }

    def variant_economics(self, variant_id: str) -> dict[str, Any]:
        """Себестоимость и цена одной вариации с ценой её катушки.

        Пластик — главная статья расхода, и цена грамма берётся из той
        катушки, которой вариацию реально печатают: у магазина PLA за 1600 и
        PETG за 3200 за килограмм, и «средняя по справочнику» здесь врёт.
        Если катушка не привязана, берём подходящую по пластику и цвету и
        честно говорим, откуда цифра.
        """
        row = self.db.one("SELECT * FROM nom_variants WHERE id=?", (variant_id,))
        if not row:
            raise ValueError("Вариация не найдена")
        nom = self.db.one("SELECT * FROM nomenclature WHERE id=?",
                          (row.get("nom_id"),)) or {}
        spool, spool_source = self._variant_spool(row, nom)
        grams = num(row.get("grams")) or num(nom.get("grams"))
        hours = num(row.get("hours")) or num(nom.get("hours"))
        material = (str(row.get("material") or "").strip()
                    or str(spool.get("material") or "").strip()
                    or str(nom.get("material") or "").strip())
        fit = max(1, int(num(nom.get("fit_per_plate"), 1) or 1))
        # М2: состав вариации — каждая часть на своей катушке со своей ценой
        # грамма. Общий вес — сумма частей, стоимость пластика — не «граммы ×
        # цена одной катушки», а сумма по катушкам состава.
        structures = self.variant_structures(row["id"])
        filament_cost: float | None = None
        if structures:
            grams = round(sum(num(r["grams"]) for r in structures), 2)
            filament_cost = round(sum(num(r["filament_cost"]) for r in structures), 2)
            # Привязанная к вариации катушка в расчёте пластика не участвует:
            # она замена хранится как «чем печатать, если состава нет».
            spool, spool_source = {}, "structures"
        kwargs = {
            "manual_minutes": num(row.get("post_minutes"))
                              or num(nom.get("post_minutes")),
            "material": material,
            "qty": float(fit),
            "fit_per_plate": fit,
        }
        if spool:
            kwargs["spool_price"] = num(spool.get("price"))
            kwargs["spool_weight"] = num(spool.get("total_grams"))
        if filament_cost is not None:
            # Изделий на плите fit штук: стоимость пластика на плиту
            kwargs["filament_cost"] = filament_cost * fit
        if grams > 0 and hours > 0:
            kwargs["plate_grams"] = grams * fit
            kwargs["plate_hours"] = hours * fit
            kwargs["warmup_minutes"] = 0.0
        breakdown = self.acc.cost_breakdown(grams, hours, **kwargs)
        cost = round(num(breakdown.get("per_unit") or breakdown.get("total")), 2)
        suggested = self.acc.suggest_price(cost)
        own_price = num(row.get("price"))
        return {
            "variant_id": variant_id,
            "name": str(row.get("name") or ""),
            "grams": round(grams, 2),
            "hours": round(hours, 2),
            "material": material,
            "cost": cost,
            "price": round(own_price, 2) if own_price > 0
                     else round(num(suggested.get("price")), 2),
            "auto_price": own_price <= 0,
            "markup": num(suggested.get("markup")),
            "spool": spool or {},
            "spool_source": spool_source,
            "structures": structures,
            "breakdown": breakdown,
        }

    def recalc_variant_prices(self, nom_id: str, only_auto: bool = True) -> dict[str, Any]:
        """Пересчитать себестоимость и цены всех вариаций товара.

        Цена переписывается только там, где её не задали руками: своя цена —
        решение владельца, машина его не отменяет. Себестоимость обновляется
        всегда: она расчётная.
        """
        rows = self.db.query(
            "SELECT * FROM nom_variants WHERE nom_id=? AND archived=0"
            " ORDER BY position, name", (nom_id,))
        updated: list[dict] = []
        skipped_price = 0
        for row in rows:
            try:
                eco = self.variant_economics(row["id"])
            except Exception:
                continue
            data = {"id": row["id"], "cost": eco["cost"],
                    "updated_at": now_iso()}
            if not (only_auto and not eco["auto_price"]):
                data["price"] = eco["price"]
            else:
                skipped_price += 1
            self.db.upsert("nom_variants", data)
            updated.append({"id": row["id"], "name": eco["name"],
                            "cost": eco["cost"], "price": eco["price"],
                            "auto_price": eco["auto_price"],
                            "spool_id": eco["spool"].get("id", ""),
                            "spool_source": eco["spool_source"]})
        return {"nom_id": nom_id, "updated": len(updated),
                "kept_manual_price": skipped_price, "items": updated}

    def _variant_spool(self, row: dict, nom: dict) -> tuple[dict, str]:
        """Катушка вариации: привязанная вручную или подходящая по складу."""
        spool_id = str(row.get("spool_id") or "").strip()
        if spool_id:
            spool = self.db.one("SELECT * FROM spools WHERE id=?", (spool_id,))
            if spool:
                return spool, "variant"
        material = (str(row.get("material") or "").strip()
                    or str(nom.get("material") or "").strip())
        color = str(row.get("color_name") or "").strip()
        if color:
            spool = self.db.one(
                "SELECT * FROM spools WHERE archived=0 AND material=? AND "
                "pylower(color_name)=? ORDER BY remaining_grams DESC LIMIT 1",
                (material, color.casefold())) if material else None
            if spool:
                return spool, "auto"
        if material:
            spool = self.db.one(
                "SELECT * FROM spools WHERE archived=0 AND material=? "
                "ORDER BY remaining_grams DESC LIMIT 1", (material,))
            if spool:
                return spool, "auto"
        return {}, "none"

    # ----------------------------------------------------------- М2: состав вариации
    MAX_VARIANT_STRUCTURES = 8

    def variant_structures(self, variant_id: str) -> list[dict]:
        """Состав вариации из нескольких катушек (18.5, М2).

        Каждая строка — «катушка × граммы». Цена грамма считается из своей
        катушки, а не «средней по складу». Без состава вариация идёт старым
        путём — одна катушка вариации или автоподбор.
        """
        rows = self.db.query(
            "SELECT * FROM nom_variant_structures WHERE variant_id=?"
            " ORDER BY position, id", (variant_id,))
        out: list[dict] = []
        for r in rows:
            spool = self.db.one("SELECT * FROM spools WHERE id=?", (r["spool_id"],))
            per_gram = 0.0
            if spool:
                weight = max(1.0, num(spool.get("total_grams"), 1000) or 1000.0)
                per_gram = num(spool.get("price")) / weight
            out.append({
                "id": r["id"],
                "variant_id": variant_id,
                "spool_id": r["spool_id"],
                "grams": round(num(r["grams"]), 1),
                "position": int(num(r.get("position"))),
                "material": (spool or {}).get("material") or "",
                "color_name": (spool or {}).get("color_name") or "",
                "color_hex": (spool or {}).get("color_hex") or "",
                "color_kind": (spool or {}).get("color_kind") or "",
                "colors_json": (spool or {}).get("colors_json") or "",
                "spool_missing": spool is None,
                "filament_cost": round(num(r["grams"]) * per_gram, 4),
            })
        return out

    def save_variant_structures(self, variant_id: str, rows: list[dict]) -> list[dict]:
        """Перезаписать состав вариации целиком (до 8 строк).

        Транзакционно: частично записанный состав не оставляем — себестоимость
        старого мгновенного пересчёта обязана совпасть с новым составом.
        """
        variant = self.db.one(
            "SELECT id, nom_id FROM nom_variants WHERE id=?", (variant_id,))
        if not variant:
            raise ValueError("Вариация не найдена")
        clean: list[dict] = []
        seen: set[str] = set()
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            spool_id = str(raw.get("spool_id") or "").strip()
            grams = num(raw.get("grams"))
            if not spool_id:
                continue
            if grams <= 0:
                raise ValueError("Укажите граммы для каждой катушки состава")
            if spool_id in seen:
                raise ValueError("Катушка в составе дважды — сложите граммы в одну строку")
            seen.add(spool_id)
            if not self.db.one(
                    "SELECT id FROM spools WHERE id=? AND archived=0", (spool_id,)):
                raise ValueError("Катушка из состава не найдена на складе")
            clean.append({"spool_id": spool_id, "grams": round(grams, 1)})
        if len(clean) > self.MAX_VARIANT_STRUCTURES:
            raise ValueError(
                f"Состав не длиннее {self.MAX_VARIANT_STRUCTURES} строк — "
                f"больше уже не набор, а новая технология печати")
        if len(clean) == 1:
            # Одна катушка — это не «состав», а обычная привязка вариации:
            # пишем в spool_id вариации, чтобы оба пути давали одну цену.
            with self.db.transaction():
                self.db.execute(
                    "DELETE FROM nom_variant_structures WHERE variant_id=?", (variant_id,))
                self.db.upsert("nom_variants", {
                    "id": variant_id, "spool_id": clean[0]["spool_id"],
                    "updated_at": now_iso()})
            self.recalc_variant_prices(variant["nom_id"])
            return self.variant_structures(variant_id)
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM nom_variant_structures WHERE variant_id=?", (variant_id,))
            for pos, row in enumerate(clean):
                self.db.upsert("nom_variant_structures", {
                    "id": uid("vstruct"),
                    "variant_id": variant_id,
                    "spool_id": row["spool_id"],
                    "grams": row["grams"],
                    "position": pos,
                    "updated_at": now_iso(),
                })
        self.recalc_variant_prices(variant["nom_id"])
        return self.variant_structures(variant_id)

    def delete_variant_structures(self, variant_id: str) -> None:
        variant = self.db.one(
            "SELECT id, nom_id FROM nom_variants WHERE id=?", (variant_id,))
        if not variant:
            raise ValueError("Вариация не найдена")
        self.db.execute(
            "DELETE FROM nom_variant_structures WHERE variant_id=?", (variant_id,))
        self.recalc_variant_prices(variant["nom_id"])

    def set_variant_photo(self, variant_id: str, data_url: str) -> str:
        """Фото вариации (18.5, М4): data URL → файл, ссылка — в строке вариации.

        Те же правила, что у общего фото товара: до 8 МБ. Общее фото живёт
        отдельно в nomenclature.photo и остаётся первым кадром карусели кассы.
        С 18.6 это обложка галереи: замена бьёт только файл обложки, кадры
        галереи не трогает.
        """
        from .config import PHOTO_DIR
        self._require_variant(variant_id)
        ext, raw = self._decode_variant_photo(data_url)
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        name = f"var_{variant_id}.{ext}"
        (PHOTO_DIR / name).write_bytes(raw)
        self.db.execute("UPDATE nom_variants SET photo=?, updated_at=? WHERE id=?",
                        (name, now_iso(), variant_id))
        return name

    def _require_variant(self, variant_id: str) -> dict:
        variant = self.db.one("SELECT * FROM nom_variants WHERE id=?", (variant_id,))
        if not variant:
            raise LookupError("Вариация не найдена")
        return variant

    @staticmethod
    def _decode_variant_photo(data_url: str) -> tuple[str, bytes]:
        """Data URL → (расширение, байты). Правила одни на обложку и галерею."""
        import base64
        data_url = str(data_url or "")
        if "," not in data_url:
            raise ValueError("Не похоже на data URL")
        head, _, b64 = data_url.partition(",")
        ext = "png" if "png" in head else "jpg"
        try:
            raw = base64.b64decode(b64)
        except Exception as exc:
            raise ValueError("Не похоже на data URL") from exc
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("Фото больше 8 МБ")
        return ext, raw

    def variant_gallery(self, variant_id: str) -> list[str]:
        """Кадры вариации обложкой вперёд: photo + photos_json без дублей."""
        row = self.db.one("SELECT photo, photos_json FROM nom_variants WHERE id=?",
                          (variant_id,))
        if not row:
            raise LookupError("Вариация не найдена")
        out: list[str] = []
        for name in [row.get("photo") or ""] + self._gallery_names(row.get("photos_json")):
            name = str(name or "").strip()
            if name and name not in out:
                out.append(name)
        return out

    @staticmethod
    def _gallery_names(raw: str) -> list[str]:
        import json
        try:
            parsed = json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []
        if not isinstance(parsed, list):
            return []
        return [str(x).strip() for x in parsed if str(x).strip()]

    def add_variant_photo(self, variant_id: str, data_url: str) -> dict[str, Any]:
        """Галерея вариации (18.6): новый кадр в конец, обложка не меняется.

        Обложки нет — первый кадр становится ею (старые витрины, знающие
        только photo, видят его сразу). Больше шести кадров не держим:
        телефон владельца и так распухнет от фото бобины.
        """
        import json
        from .config import PHOTO_DIR
        self._require_variant(variant_id)
        ext, raw = self._decode_variant_photo(data_url)
        gallery = self.variant_gallery(variant_id)
        if len(gallery) >= VARIANT_GALLERY_MAX:
            raise ValueError(f"Больше {VARIANT_GALLERY_MAX} фото не держим")
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        if not gallery:
            name = f"var_{variant_id}.{ext}"
            (PHOTO_DIR / name).write_bytes(raw)
            self.db.execute("UPDATE nom_variants SET photo=?, updated_at=? WHERE id=?",
                            (name, now_iso(), variant_id))
            return {"photo": name, "gallery": [name]}
        taken = set(gallery)
        slot = 1
        while f"var_{variant_id}_{slot}.{ext}" in taken:
            slot += 1
        name = f"var_{variant_id}_{slot}.{ext}"
        (PHOTO_DIR / name).write_bytes(raw)
        row = self.db.one("SELECT photos_json FROM nom_variants WHERE id=?", (variant_id,))
        names = self._gallery_names((row or {}).get("photos_json"))
        names.append(name)
        self.db.execute("UPDATE nom_variants SET photos_json=?, updated_at=? WHERE id=?",
                        (json.dumps(names, ensure_ascii=False), now_iso(), variant_id))
        return {"photo": name, "gallery": self.variant_gallery(variant_id)}

    def delete_variant_photo(self, variant_id: str, name: str) -> dict[str, Any]:
        """Убрать кадр из галереи. Удалённая обложка не оставляет дыру:
        обложкой становится следующий кадр, витрина не пустеет."""
        import json
        from .config import PHOTO_DIR
        row = self._require_variant(variant_id)
        name = str(name or "").strip()
        gallery = self.variant_gallery(variant_id)
        if name not in gallery:
            raise ValueError("Такого кадра у вариации нет")
        try:
            (PHOTO_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass
        cover = str(row.get("photo") or "")
        rest = [g for g in self._gallery_names(row.get("photos_json")) if g != name]
        if name == cover:
            cover = rest.pop(0) if rest else ""
        self.db.execute("UPDATE nom_variants SET photo=?, photos_json=?, updated_at=? WHERE id=?",
                        (cover, json.dumps(rest, ensure_ascii=False), now_iso(), variant_id))
        return {"photo": cover, "gallery": self.variant_gallery(variant_id)}

    def set_variant_cover(self, variant_id: str, name: str) -> dict[str, Any]:
        """Сделать кадр галереи обложкой (бывшая обложка уходит в галерею)."""
        import json
        row = self._require_variant(variant_id)
        name = str(name or "").strip()
        gallery = self.variant_gallery(variant_id)
        if name not in gallery:
            raise ValueError("Такого кадра у вариации нет")
        cover = str(row.get("photo") or "")
        rest = [g for g in gallery if g != name]
        if cover and cover != name:
            rest.insert(0, cover)
        self.db.execute("UPDATE nom_variants SET photo=?, photos_json=?, updated_at=? WHERE id=?",
                        (name, json.dumps(rest, ensure_ascii=False), now_iso(), variant_id))
        return {"photo": name, "gallery": self.variant_gallery(variant_id)}

    def variant_card(self, variant_id: str) -> dict[str, Any]:
        """Карточка вариации для мини-редактора (18.6): строка, галерея,
        экономика и живой слот AMS её пластика — привязка всегда через
        катушку/состав, прямых привязок вариации к слоту нет."""
        row = self._require_variant(variant_id)
        economics: dict[str, Any] = {}
        try:
            economics = self.variant_economics(variant_id)
        except ValueError:
            economics = {}
        return {
            "variant": row,
            "gallery": self.variant_gallery(variant_id),
            "economics": economics,
            "ams": self._variant_ams(row),
        }

    def _variant_ams(self, row: dict) -> dict[str, Any]:
        """Где физически лежит пластик вариации: слот AMS её катушки
        (и катушек состава). Пусто — вариация печатается «чем придётся»."""
        out: dict[str, Any] = {"spool": None, "structures": []}
        spool_id = str(row.get("spool_id") or "").strip()
        if spool_id:
            out["spool"] = self._spool_slot(spool_id)
        for struct in self.variant_structures(row.get("id") or ""):
            struct = dict(struct)
            struct["slot"] = self._spool_slot(str(struct.get("spool_id") or ""))
            out["structures"].append(struct)
        return out

    def _spool_slot(self, spool_id: str) -> dict[str, Any]:
        spool = self.db.one(
            "SELECT id, material, color_name, color_hex, remaining_grams,"
            " printer_id, ams_slot FROM spools WHERE id=?", (spool_id,)) or {}
        if not spool:
            return {"spool_id": spool_id, "missing": True}
        printer_id = str(spool.get("printer_id") or "")
        printer = self.db.one("SELECT id, name FROM printers WHERE id=?",
                              (printer_id,)) if printer_id else None
        slot = str(spool.get("ams_slot") or "")
        return {
            "spool_id": spool.get("id"), "material": spool.get("material") or "",
            "color_name": spool.get("color_name") or "",
            "color_hex": spool.get("color_hex") or "",
            "remaining_grams": num(spool.get("remaining_grams")),
            "printer_id": printer_id,
            "printer_name": str((printer or {}).get("name") or printer_id),
            "ams_slot": slot,
            "in_ams": bool(printer_id and slot not in ("", None)),
        }

    def spool_options(self) -> list[dict]:
        """Катушки склада для выбора в карточке: цена за грамм видна сразу."""
        rows = self.db.query(
            "SELECT id, material, brand, color_name, color_hex, color_kind,"
            " colors_json, price, total_grams, remaining_grams"
            " FROM spools WHERE archived=0"
            " ORDER BY material, color_name")
        out = []
        for r in rows:
            weight = max(1.0, num(r.get("total_grams"), 1000) or 1000.0)
            out.append({
                "id": r["id"],
                "material": r.get("material") or "",
                "brand": r.get("brand") or "",
                "color_name": r.get("color_name") or "",
                "color_hex": r.get("color_hex") or "#4b5563",
                "color_kind": r.get("color_kind") or "",
                "colors_json": r.get("colors_json") or "",
                "price": round(num(r.get("price")), 2),
                "per_gram": round(num(r.get("price")) / weight, 4),
                "remaining_grams": round(num(r.get("remaining_grams")), 1),
                "total_grams": round(weight, 1),
            })
        return out

    # ----------------------------------------------------------- сводка
    def summary(self, warehouse_id: str = "", items: list[dict] | None = None) -> dict[str, Any]:
        # В ответе /api/nomenclature список уже вычислен — используем его,
        # а не декорируем всю номенклатуру второй раз на один запрос.
        if items is None:
            items = self.items(warehouse_id=warehouse_id)
        goods = [i for i in items if i.get("kind") in GOOD_KINDS]
        qty = sum(num(i["qty"]) for i in goods)
        value = sum(num(i["stock_value"]) for i in goods)
        low = [i for i in goods if i["status"] == "low"]
        dead = [i for i in goods if i["status"] == "dead"]
        empty = [i for i in goods if i["status"] == "empty"]
        plan = sum(int(num(i["plan_qty"])) for i in goods)
        sold7 = sum(num(i["sold_7"]) for i in goods)
        sold7_money = sum(num(i["sold_7"]) * num(i["price"]) for i in goods)
        unprofitable = [i for i in goods if i.get("profitable") is False]
        return {
            "items": len(items),
            "goods": len(goods),
            "qty": round(qty, 1),
            "value": round(value, 2),
            "low": len(low),
            "dead": len(dead),
            "dead_value": round(sum(num(i["stock_value"]) for i in dead), 2),
            "empty": len(empty),
            "plan_qty": plan,
            "sold_7": round(sold7, 1),
            "sold_7_money": round(sold7_money, 2),
            "unprofitable": len(unprofitable),
            "reserved": round(sum(num(i["reserved"]) for i in goods), 1),
        }

    def replenishment(self, warehouse_id: str = "") -> list[dict]:
        """Что и сколько печатать: позиции с дефицитом, отсортированные по срочности."""
        out = []
        for item in self.items(warehouse_id=warehouse_id):
            if item.get("kind") not in GOOD_KINDS:
                continue
            if not num(item.get("plan_qty")):
                continue
            out.append({
                "nom_id": item["id"], "name": item["name"],
                "qty": item["qty"], "plan_qty": item["plan_qty"],
                "days_left": item["days_left"], "status": item["status"],
                "fit_per_plate": item.get("fit_per_plate") or 1,
                "grams": item.get("grams"), "hours": item.get("hours"),
                "file": item.get("file"), "material": item.get("material"),
                "price": item.get("price"), "cost": item.get("cost"),
            })
        out.sort(key=lambda x: (x["days_left"] if x["days_left"] is not None else 999))
        return out

    # ----------------------------------------------- замороженный капитал
    def frozen_capital(self, warehouse_id: str = "") -> dict[str, Any]:
        """Отчёт «Замороженный капитал»: сколько денег лежит в остатках.

        Группирует по группам номенклатуры и показывает:
        • общую стоимость остатков;
        • разбивку по группам;
        • позиции с наибольшим замороженным капиталом;
        • «мёртвый» сток (нет продаж 30+ дней).
        """
        items = self.items(warehouse_id=warehouse_id)
        goods = [i for i in items if i.get("kind") in GOOD_KINDS]
        groups: dict[str, dict[str, Any]] = {}
        by_item: list[dict[str, Any]] = []
        dead: list[dict[str, Any]] = []
        total_value = 0.0
        total_qty = 0.0

        for item in goods:
            qty = num(item["qty"])
            value = num(item["stock_value"])
            if qty <= 0:
                continue
            total_value += value
            total_qty += qty
            group_id = item.get("group_id") or "_none"
            group_name = (self.db.one("SELECT name FROM nom_groups WHERE id=?",
                                      (group_id,)) or {}).get("name") or "Без группы"
            if group_id not in groups:
                groups[group_id] = {"name": group_name, "value": 0.0, "qty": 0.0, "items": 0}
            groups[group_id]["value"] += value
            groups[group_id]["qty"] += qty
            groups[group_id]["items"] += 1
            by_item.append({
                "nom_id": item["id"], "name": item["name"],
                "qty": qty, "value": value, "days_left": item.get("days_left"),
                "status": item.get("status"),
            })
            if item.get("status") == "dead":
                dead.append({
                    "nom_id": item["id"], "name": item["name"],
                    "qty": qty, "value": value,
                    "last_sale": item.get("last_sale"),
                })

        by_item.sort(key=lambda x: -x["value"])
        dead.sort(key=lambda x: -x["value"])
        groups_list = sorted(groups.values(), key=lambda g: -g["value"])
        return {
            "total_value": round(total_value, 2),
            "total_qty": round(total_qty, 1),
            "items_count": len(goods),
            "by_group": groups_list,
            "top_items": by_item[:20],
            "dead_stock": dead,
            "dead_value": round(sum(d["value"] for d in dead), 2),
            "dead_qty": len(dead),
        }

    # ----------------------------------------------- прогноз расхода пластика
    def filament_forecast(self, days: int = 30) -> dict[str, Any]:
        """Прогноз расхода пластика на основе плана производства.

        Берёт позиции из replenishment (что нужно допечатать) и считает:
        • сколько граммов каждого материала понадобится;
        • хватит ли текущих остатков на складе;
        • что нужно закупить.
        """
        plan = self.replenishment()
        by_material: dict[str, dict[str, float]] = {}
        total_grams = 0.0
        for item in plan:
            grams = num(item.get("grams"))
            qty = num(item.get("plan_qty"))
            if not grams or not qty:
                continue
            material = (item.get("material") or "PLA").upper()
            need = grams * qty
            total_grams += need
            if material not in by_material:
                by_material[material] = {"need": 0.0, "stock": 0.0, "deficit": 0.0}
            by_material[material]["need"] += need

        # Остатки пластика на складе
        spools = self.db.query(
            "SELECT material, SUM(remaining_grams) stock FROM spools"
            " WHERE archived=0 GROUP BY material")
        for row in spools:
            mat = (row.get("material") or "PLA").upper()
            if mat in by_material:
                by_material[mat]["stock"] = num(row.get("stock"))

        # Дефицит
        for mat, data in by_material.items():
            data["deficit"] = max(0.0, data["need"] - data["stock"])
            data["need"] = round(data["need"], 1)
            data["stock"] = round(data["stock"], 1)
            data["deficit"] = round(data["deficit"], 1)

        return {
            "days": days,
            "total_need": round(total_grams, 1),
            "by_material": sorted(by_material.items(), key=lambda x: -x[1]["need"]),
            "positions": len(plan),
        }

    # ----------------------------------------------- обновление себестоимости
    def update_cost_from_batch(self, nom_id: str) -> dict[str, Any]:
        """Обновить себестоимость номенклатуры из последней завершённой партии.

        После приёмки партии фактическая себестоимость штуки записывается
        в карточку товара — это точнее нормативной из граммов и часов.

        Витринная позиция «Для магазина» не обновляется — у неё нет
        себестоимости по определению.
        """
        nom = self.db.one("SELECT kind FROM nomenclature WHERE id=?", (nom_id,))
        if (nom or {}).get("kind") == "showcase":
            return {"ok": False, "reason": "Витринная позиция не участвует в учёте"}
        batch = self.db.one(
            "SELECT * FROM batches WHERE nom_id=? AND state IN ('done','partial')"
            " ORDER BY datetime(at) DESC LIMIT 1", (nom_id,))
        if not batch:
            return {"ok": False, "reason": "Нет завершённых партий"}
        qty_done = num(batch.get("qty_done"))
        if qty_done <= 0:
            return {"ok": False, "reason": "В партии нет готовых изделий"}
        cost = num(batch.get("cost"))
        if cost <= 0:
            return {"ok": False, "reason": "Себестоимость партии неизвестна"}
        cost_per_unit = round(cost / qty_done, 2)
        self.db.execute(
            "UPDATE nomenclature SET cost=?, updated_at=? WHERE id=?",
            (cost_per_unit, now_iso(), nom_id))
        return {
            "ok": True,
            "cost_per_unit": cost_per_unit,
            "batch_id": batch["id"],
            "qty_done": qty_done,
        }
