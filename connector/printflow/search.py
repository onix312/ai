"""Единый поиск по цеху (идея 70).

Раньше поиск был свой в каждом разделе: заказы искали по одному набору
полей, товары — по другому, клиенты — по третьему, а «где у меня эта
деталь?» требовал четырёх переключений вкладок. Здесь — один сервис и один
маршрут `/api/search?q=`, который отдаёт сгруппированный результат:
заказы, товары, клиенты, катушки, задания печати, документы и полка.

Принципы:
  * один SQL-запрос на сущность, лимит на группу — поиск не должен
    вычитывать базу целиком;
  * совпадение и по номеру/SKU/штрихкоду, и по тексту (pylower —
    регистронезависимое сравнение, как в остальном проекте);
  * никаких секретов и настроек в выдаче;
  * результат — плоские строки с `kind`, `id`, `title`, `sub`, `route`:
    панель рисует их одинаково и умеет переходить по `route`.
"""
from __future__ import annotations

from typing import Any

from .accounting import num

LIMIT_DEFAULT = 8
GROUPS = ("orders", "products", "customers", "spools", "jobs", "documents", "shelf")


def _like(term: str) -> str:
    return f"%{term.lower()}%"


class Search:
    def __init__(self, db) -> None:
        self.db = db

    def run(self, term: str, limit: int = LIMIT_DEFAULT,
            groups: tuple[str, ...] = GROUPS) -> dict:
        term = (term or "").strip()
        limit = max(1, min(50, int(num(limit, LIMIT_DEFAULT))))
        result: dict[str, Any] = {"query": term, "groups": [], "total": 0}
        if len(term) < 2:
            result["hint"] = "Минимум два символа"
            return result
        like = _like(term)
        collectors = {
            "orders": self._orders,
            "products": self._products,
            "customers": self._customers,
            "spools": self._spools,
            "jobs": self._jobs,
            "documents": self._documents,
            "shelf": self._shelf,
        }
        for name in GROUPS:
            if name not in groups:
                continue
            try:
                rows = collectors[name](like, limit)
            except Exception as exc:  # таблица может отсутствовать в старой базе
                rows = [{"error": str(exc)}]
            if rows:
                result["groups"].append({"kind": name, "items": rows, "count": len(rows)})
                result["total"] += len(rows)
        return result

    # ------------------------------------------------------------ заказы
    def _orders(self, like: str, limit: int) -> list[dict]:
        rows = self.db.query(
            "SELECT o.id, o.number, o.product, o.status, o.price, o.created_at,"
            " c.name AS customer"
            " FROM orders o LEFT JOIN customers c ON c.id=o.customer_id"
            " WHERE pylower(COALESCE(o.number,'')) LIKE ?"
            "    OR pylower(COALESCE(o.product,'')) LIKE ?"
            "    OR pylower(COALESCE(c.name,'')) LIKE ?"
            "    OR pylower(COALESCE(o.phone,'')) LIKE ?"
            " ORDER BY o.created_at DESC LIMIT ?",
            (like, like, like, like, limit))
        return [{
            "kind": "orders", "id": row["id"],
            "title": f"№{row.get('number') or '—'} · {row.get('product') or 'без названия'}",
            "sub": f"{row.get('customer') or 'без клиента'} · {num(row.get('price')):,.0f}",
            "route": f"#orders/{row['id']}",
            "status": row.get("status") or "",
        } for row in rows]

    # ------------------------------------------------------------ товары
    def _products(self, like: str, limit: int) -> list[dict]:
        rows = self.db.query(
            "SELECT id, name, sku, code, barcode, kind"
            " FROM nomenclature WHERE archived=0 AND ("
            " pylower(COALESCE(name,'')) LIKE ? OR pylower(COALESCE(sku,'')) LIKE ?"
            " OR pylower(COALESCE(code,'')) LIKE ? OR pylower(COALESCE(barcode,'')) LIKE ?)"
            " ORDER BY name LIMIT ?", (like, like, like, like, limit))
        return [{
            "kind": "products", "id": row["id"],
            "title": row.get("name") or "Без названия",
            "sub": " · ".join(x for x in (row.get("sku"), row.get("code")) if x),
            "route": f"#products/{row['id']}",
        } for row in rows]

    # ------------------------------------------------------------ клиенты
    def _customers(self, like: str, limit: int) -> list[dict]:
        rows = self.db.query(
            "SELECT id, name, phone, messenger FROM customers"
            " WHERE pylower(COALESCE(name,'')) LIKE ? OR pylower(COALESCE(phone,'')) LIKE ?"
            " ORDER BY name LIMIT ?", (like, like, limit))
        return [{
            "kind": "customers", "id": row["id"],
            "title": row.get("name") or "Без имени",
            "sub": row.get("phone") or row.get("messenger") or "",
            "route": f"#customers/{row['id']}",
        } for row in rows]

    # ------------------------------------------------------------ катушки
    def _spools(self, like: str, limit: int) -> list[dict]:
        rows = self.db.query(
            "SELECT id, material, color_name, color_hex, remaining_grams,"
            " total_grams, location, tray_uuid"
            " FROM spools WHERE archived=0 AND ("
            " pylower(COALESCE(material,'')) LIKE ? OR pylower(COALESCE(color_name,'')) LIKE ?"
            " OR pylower(COALESCE(tray_uuid,'')) LIKE ? OR pylower(COALESCE(location,'')) LIKE ?)"
            " ORDER BY remaining_grams DESC LIMIT ?", (like, like, like, like, limit))
        return [{
            "kind": "spools", "id": row["id"],
            "title": f"{row.get('material') or 'Материал'} · {row.get('color_name') or 'цвет'}",
            "sub": f"{num(row.get('remaining_grams')):,.0f} г из"
                   f" {num(row.get('total_grams')):,.0f} · {row.get('location') or 'склад'}",
            "route": "#inventory",
            "color": row.get("color_hex") or "",
        } for row in rows]

    # -------------------------------------------------------- задания печати
    def _jobs(self, like: str, limit: int) -> list[dict]:
        rows = self.db.query(
            "SELECT id, name, state, printer_id, est_minutes, created_at FROM print_jobs"
            " WHERE pylower(COALESCE(name,'')) LIKE ?"
            " ORDER BY created_at DESC LIMIT ?", (like, limit))
        return [{
            "kind": "jobs", "id": row["id"],
            "title": row.get("name") or "Задание",
            "sub": f"{row.get('state') or '—'} · {row.get('printer_id') or 'парк'}",
            "route": "#queue",
        } for row in rows]

    # ---------------------------------------------------------- документы
    def _documents(self, like: str, limit: int) -> list[dict]:
        rows = self.db.query(
            "SELECT id, kind, number, at, total_amount, title, note FROM workshop_docs"
            " WHERE pylower(COALESCE(number,'')) LIKE ? OR pylower(COALESCE(note,'')) LIKE ?"
            "    OR pylower(COALESCE(title,'')) LIKE ? OR pylower(COALESCE(supplier,'')) LIKE ?"
            " ORDER BY at DESC LIMIT ?", (like, like, like, like, limit))
        return [{
            "kind": "documents", "id": row["id"],
            "title": (row.get("title")
                      or f"{row.get('kind') or 'Документ'} №{row.get('number') or '—'}"),
            "sub": f"{row.get('at') or ''} · {num(row.get('total_amount')):,.0f}",
            "route": "#documents",
        } for row in rows]

    # ------------------------------------------------------------ полка
    def _shelf(self, like: str, limit: int) -> list[dict]:
        rows = self.db.query(
            "SELECT s.id, s.name, s.sku, s.qty, s.price, s.cell, n.name AS nom_name"
            " FROM shelf_items s LEFT JOIN nomenclature n ON n.id=s.nom_id"
            " WHERE pylower(COALESCE(s.name,'')) LIKE ? OR pylower(COALESCE(n.name,'')) LIKE ?"
            "    OR pylower(COALESCE(s.sku,'')) LIKE ? OR pylower(COALESCE(s.barcode,'')) LIKE ?"
            " ORDER BY s.updated_at DESC LIMIT ?", (like, like, like, like, limit))
        return [{
            "kind": "shelf", "id": row["id"],
            "title": row.get("name") or row.get("nom_name") or "Позиция полки",
            "sub": f"{num(row.get('qty')):,.0f} шт · {num(row.get('price')):,.0f}"
                   + (f" · {row['cell']}" if row.get("cell") else ""),
            "route": "#shelf",
        } for row in rows]
