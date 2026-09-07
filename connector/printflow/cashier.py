"""Мобильная касса в LAN (Касса 16.0, итерация 3 — единый каталог склада).

Кассир на телефоне/планшете в локальной сети магазина продаёт за наличные или
по СБП. Деньги не дублируются и не расходятся с учётом:

* наличные — продажа сразу через ``shelf.sale`` (канал in_shop, как в панели
  и боте — один учёт);
* СБП — платёж создаётся со статусом new/pending, склад НЕ списывается и
  выручка НЕ пишется до подтверждения; при подтверждении кассиром в одной
  транзакции списывается склад и записывается доход на счёт СБП;
* вход — общий код магазина (настройка ``cashier_code``), сессия в памяти;
* возвраты СБП кассиру недоступны — только руководитель/владелец в панели.

Каталог кассы — единый (WMS 4.0). Кассир видит не только позиции витрины
(``shelf_items``), но и готовую продукцию с учётных складов (номенклатура +
регистр ``stock_moves``): дублировать товар руками на полку больше не нужно.
Когда продают то, чего на полке не хватает, недостающие штуки переезжают со
склада на витрину автоматически — движением регистра (``transfer_from_stock``),
а не правкой остатка в обход журнала. Резерв под заказы касса не трогает:
доступным считается свободный остаток (остаток − активные резервы).
"""
from __future__ import annotations

import json
import time
from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso
from .sbp import Sbp, STATUS_PENDING
from .shelf import Shelf

SESSION_TTL = 12 * 3600  # смена 12 часов, затем код вводится заново
METHODS = ("cash", "sbp")
# Виртуальный идентификатор товара, который есть на складе, но ещё не заведён
# на витрине: ``stock:<nom_id>``. Позиция полки создаётся в момент продажи.
STOCK_PREFIX = "stock:"
PIECE_UNITS = ("шт", "шт.", "piece", "pcs")


def is_stock_id(item_id: Any) -> bool:
    """Ссылка на складской товар (ещё не заведён на витрине)?"""
    return str(item_id or "").startswith(STOCK_PREFIX)


def stock_nom_id(item_id: Any) -> str:
    """``stock:nom_1`` → ``nom_1``."""
    return str(item_id or "")[len(STOCK_PREFIX):].strip()


class Cashier:
    def __init__(self, db, acc: Accounting, shelf: Shelf | None = None,
                 sbp: Sbp | None = None):
        self.db = db
        self.acc = acc
        self.shelf = shelf or Shelf(db)
        self.sbp = sbp or Sbp(db, acc)
        self._sessions: dict[str, dict] = {}  # token -> {at, role}

    # ------------------------------------------------------------- сессии
    def _gc(self) -> None:
        now = time.time()
        stale = [t for t, s in self._sessions.items()
                 if now - float(s.get("ts", now)) > SESSION_TTL]
        for t in stale:
            self._sessions.pop(t, None)

    def login(self, code: str) -> dict:
        expected = str(self.db.setting("cashier_code", "") or "").strip()
        if not expected:
            raise ValueError("Код кассы не задан — настройте его в панели (Касса и СБП)")
        if str(code or "").strip() != expected:
            raise ValueError("Неверный код кассы")
        self._gc()
        token = uid("ck")
        self._sessions[token] = {"ts": time.time(), "role": "employee"}
        return {"token": token, "role": "employee", "expires_in": SESSION_TTL}

    def logout(self, token: str) -> dict:
        self._sessions.pop(str(token or "").strip(), None)
        return {"ok": True}

    def require(self, token: str) -> dict:
        self._gc()
        session = self._sessions.get(str(token or "").strip())
        if not session:
            raise ValueError("Сессия кассы истекла — введите код снова")
        return session

    # ------------------------------------------------------------- каталог
    def stock_offer(self) -> dict[str, dict]:
        """Готовая продукция учётных складов, доступная кассе.

        Возвращает ``{nom_id: {name, photo, unit, price, qty, sources[…]}}``,
        где ``qty`` — свободный остаток (остаток минус активные резервы под
        заказы), а ``sources`` — склады-источники, отсортированные по остатку
        (с крупного забираем в первую очередь). Витрина (склад kind='shelf')
        источником не является: это и есть полка.
        """
        try:
            rows = self.shelf.stock_available(goods_only=True)
        except Exception:
            # Старая база без регистра остатков: касса продолжает работать
            # по витрине — каталог просто не пополняется складом.
            return {}
        try:
            from .stock import Stock
            stock = Stock(self.db)
        except Exception:
            stock = None
        offer: dict[str, dict] = {}
        for row in rows:
            nom_id = str(row.get("nom_id") or "")
            if not nom_id:
                continue
            warehouse_id = str(row.get("warehouse_id") or "")
            free = num(row.get("qty"))
            if stock is not None:
                try:
                    free -= stock.reserved(nom_id, warehouse_id)
                except Exception:
                    pass
            unit = str(row.get("unit") or "шт")
            if unit in PIECE_UNITS:
                free = float(int(free + 1e-9))  # продаём только целые штуки
            free = round(free, 3)
            if free <= 0:
                continue
            entry = offer.setdefault(nom_id, {
                "nom_id": nom_id, "name": row.get("name") or "Без названия",
                "photo": row.get("photo") or "", "unit": unit,
                "price": num(row.get("price")), "qty": 0.0, "sources": [],
            })
            entry["qty"] = round(num(entry["qty"]) + free, 3)
            entry["price"] = entry["price"] or num(row.get("price"))
            entry["sources"].append({
                "warehouse_id": warehouse_id,
                "warehouse_name": row.get("warehouse_name") or "Склад",
                "qty": free,
            })
        for entry in offer.values():
            entry["sources"].sort(key=lambda s: num(s.get("qty")), reverse=True)
        return offer

    def catalog(self) -> dict[str, Any]:
        """Единый каталог кассы: витрина + свободные остатки складов.

        Позиция полки, связанная с номенклатурой, показывает суммарную
        доступность ``shelf_qty + stock_qty`` — кассир не упирается в «нет в
        наличии», когда товар лежит на складе в соседней комнате. Товары,
        которых на витрине нет вовсе, приходят виртуальными позициями
        ``stock:<nom_id>`` и материализуются на полке при продаже.
        """
        offer = self.stock_offer()
        items: list[dict] = []
        by_nom: dict[str, dict] = {}
        by_name: dict[str, dict] = {}
        by_id: dict[str, dict] = {}
        for it in self.shelf.items():
            if not it.get("active"):
                continue
            nom_id = str(it.get("nom_id") or "").strip()
            shelf_qty = round(num(it.get("qty")), 3)
            row = {
                "id": it["id"], "name": it.get("name") or "",
                "price": round(num(it.get("price")), 2),
                "qty": shelf_qty, "shelf_qty": shelf_qty, "stock_qty": 0.0,
                "status": str(it.get("status") or "ok"), "source": "shelf",
                "photo": bool(it.get("photo")), "barcode": it.get("barcode") or "",
                "sku": it.get("sku") or "", "nom_id": nom_id,
                "unit": str(it.get("unit") or "шт"), "warehouse_name": "",
            }
            items.append(row)
            by_id[row["id"]] = row
            if nom_id:
                by_nom.setdefault(nom_id, row)
            name_key = str(row["name"]).strip().lower()
            if name_key:
                by_name.setdefault(name_key, row)
        # Склад → витрина: остаток склада прибавляем к связанной позиции полки.
        # Связь ищем так же, как ``transfer_from_stock``: по nom_id, по
        # legacy_shelf_id номенклатуры и по имени — чтобы один товар не давал
        # два плитки в кассе.
        for nom_id, entry in offer.items():
            target = by_nom.get(nom_id)
            if target is None:
                nom = self.db.one(
                    "SELECT legacy_shelf_id FROM nomenclature WHERE id=?", (nom_id,)) or {}
                legacy_id = str(nom.get("legacy_shelf_id") or "").strip()
                if legacy_id:
                    target = by_id.get(legacy_id)
            if target is None:
                target = by_name.get(str(entry.get("name") or "").strip().lower())
            stock_qty = round(num(entry.get("qty")), 3)
            source_name = (entry["sources"][0]["warehouse_name"]
                           if entry.get("sources") else "Склад")
            if target is not None:
                target["nom_id"] = target["nom_id"] or nom_id
                target["stock_qty"] = round(num(target["stock_qty"]) + stock_qty, 3)
                target["qty"] = round(num(target["qty"]) + stock_qty, 3)
                target["price"] = target["price"] or round(num(entry.get("price")), 2)
                target["warehouse_name"] = target["warehouse_name"] or source_name
                by_nom.setdefault(nom_id, target)
                continue
            items.append({
                "id": f"{STOCK_PREFIX}{nom_id}", "name": entry.get("name") or "",
                "price": round(num(entry.get("price")), 2), "qty": stock_qty,
                "shelf_qty": 0.0, "stock_qty": stock_qty,
                "status": "ok", "source": "stock",
                "photo": bool(entry.get("photo")), "barcode": "", "sku": "",
                "nom_id": nom_id, "unit": str(entry.get("unit") or "шт"),
                "warehouse_name": source_name,
            })
        for row in items:
            # Полка пуста, но склад закрывает продажу — это не «нет в наличии»
            if num(row["qty"]) <= 0:
                row["status"] = "empty"
            elif row["status"] == "empty":
                row["status"] = "ok"
            row["price_missing"] = num(row["price"]) <= 0
        items.sort(key=lambda x: (str(x.get("name") or "").lower(), x["id"]))
        return {
            "items": items,
            "sbp_enabled": self.sbp.enabled(),
            # картинку в каталоге не гоняем — она нужна только на экране оплаты
            "sbp": self.payment_qr(with_svg=False),
            "shop_cash": self.shelf.shop_cash(),
        }

    # ------------------------------------------------------------- QR оплаты
    def payment_qr(self, payment: dict | None = None, amount: float = 0.0,
                   with_svg: bool = True) -> dict:
        """Что показать покупателю для оплаты по СБП.

        Динамический QR (со «вшитой» суммой) выпускает банк-эквайер и кладёт
        в ``sbp_payments.qr_payload``; статический QR магазина лежит в
        настройке ``sbp_shop_qr``. Если банк ничего не выдал, PrintFlow
        собирает код сам — по реквизитам счёта (ГОСТ Р 56042-2014) или из
        шаблона платёжной ссылки. Картинку тоже рисуем сами (``qrgen``).
        """
        from .payment_qr import build as build_qr
        purpose = str((payment or {}).get("purpose") or "").strip()
        try:
            qr = build_qr(self.db, amount=amount, purpose=purpose,
                          payment=payment, with_svg=with_svg)
        except Exception:
            qr = {"mode": "auto", "kind": "", "text": "", "svg": "",
                  "amount": round(num(amount), 2), "amount_in_qr": False,
                  "problems": [], "hint": "QR временно недоступен"}
        try:
            settings = self.sbp.settings()
        except Exception:
            settings = {}
        qr["enabled"] = bool(settings.get("enabled", self.sbp.enabled()))
        qr["bank_name"] = str(settings.get("bank_name") or qr.get("bank_name") or "")
        qr["purpose"] = purpose or str(qr.get("purpose") or "")
        return qr

    def _catalog_index(self) -> dict[str, dict]:
        """Каталог, разложенный по id — для валидации корзины."""
        return {str(it["id"]): it for it in self.catalog()["items"]}

    # -------------------------------------------- авто-пополнение витрины
    def _shelf_qty(self, item_id: str) -> float:
        row = self.db.one("SELECT qty FROM shelf_items WHERE id=?", (item_id,)) or {}
        return num(row.get("qty"))

    def _pull_from_stock(self, nom_id: str, need: float, offer: dict,
                         item_id: str = "", note: str = "") -> str:
        """Перевезти ``need`` штук со складов на витрину, вернуть id позиции.

        Идём по складам от большего остатка к меньшему и списываем регистром
        (``transfer_from_stock``) — прямых UPDATE остатка нет, каждая штука
        оставляет движение. ``offer`` мутируется: уже забранное не будет
        предложено второй позиции той же корзины.
        """
        entry = offer.get(nom_id) or {}
        left = round(num(need), 3)
        for source in list(entry.get("sources") or []):
            if left <= 1e-9:
                break
            have = num(source.get("qty"))
            if have <= 0:
                continue
            take = round(min(left, have), 3)
            moved = self.shelf.transfer_from_stock(
                nom_id, str(source.get("warehouse_id") or ""), take,
                item_id=item_id, note=note or "Касса: авто-пополнение полки")
            item_id = str((moved.get("item") or {}).get("id") or item_id)
            source["qty"] = round(have - take, 3)
            entry["qty"] = round(num(entry.get("qty")) - take, 3)
            left = round(left - take, 3)
        if left > 1e-9:
            raise ValueError("Товара на складе не хватает — обновите каталог")
        return item_id

    def _ensure_on_shelf(self, row: dict, offer: dict) -> str:
        """Подготовить позицию витрины к списанию и вернуть её реальный id.

        Складской товар (``stock:…``) переезжает на полку целиком; позиции
        полки добираются со склада ровно на недостающее количество.
        """
        item_id = str(row.get("item_id") or "")
        qty = num(row.get("qty"))
        if is_stock_id(item_id):
            nom_id = stock_nom_id(item_id)
            return self._pull_from_stock(
                nom_id, qty, offer,
                note=f"Касса: продажа со склада · {row.get('name') or nom_id}")
        shortage = round(qty - self._shelf_qty(item_id), 3)
        if shortage > 1e-9:
            nom_id = str(row.get("nom_id") or "")
            if not nom_id:
                item = self.db.one("SELECT nom_id FROM shelf_items WHERE id=?",
                                   (item_id,)) or {}
                nom_id = str(item.get("nom_id") or "")
            if not nom_id:
                raise ValueError(
                    f"«{row.get('name') or item_id}»: на витрине не хватает "
                    f"{shortage:g} шт, а со складом позиция не связана")
            self._pull_from_stock(
                nom_id, shortage, offer, item_id=item_id,
                note=f"Касса: пополнение витрины · {row.get('name') or item_id}")
        return item_id

    # ------------------------------------------------------------- продажа
    def sell(self, items: list, method: str, token: str, *,
             request_id: str = "", cashier_name: str = "") -> dict:
        """Продажа корзины. Наличные — сразу в журнал; СБП — платёж до сверки.

        Идемпотентно по ``request_id`` (двойное нажатие не создаёт две продажи).
        """
        self.require(token)
        method = str(method or "cash").strip().lower()
        if method not in METHODS:
            raise ValueError("Способ оплаты: наличные (cash) или СБП (sbp)")
        payload: list[dict] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            item_id = str(it.get("item_id") or "").strip()
            qty = num(it.get("qty"))
            if not item_id or qty <= 0:
                continue
            payload.append({"item_id": item_id, "qty": round(qty, 2)})
        if not payload:
            raise ValueError("Корзина пуста")
        cashier = str(cashier_name or "кассир")[:120]
        request_id = str(request_id or "").strip()[:120]

        with self.db.transaction():
            if request_id:
                existing = self.db.one(
                    "SELECT * FROM cashier_sales WHERE request_id=?", (request_id,))
                if existing:
                    return self._sale_result(existing, already_recorded=True)
            # валидация остатков и цены до любых изменений
            index = self._catalog_index()
            offer = self.stock_offer()
            rows = []
            total = 0.0
            for entry in payload:
                item = index.get(entry["item_id"])
                if not item:
                    raise ValueError("Позиция не найдена")
                left = num(item.get("qty"))
                if left < entry["qty"]:
                    raise ValueError(
                        f"«{item.get('name') or entry['item_id']}» осталось {left:g} — "
                        f"продать {entry['qty']:g} нельзя")
                price = num(item.get("price"))
                if price <= 0:
                    raise ValueError(
                        f"«{item.get('name') or entry['item_id']}»: цена не задана — "
                        "укажите её в номенклатуре или на ценнике")
                rows.append({"item_id": entry["item_id"], "qty": entry["qty"],
                             "price": round(price, 2), "name": str(item.get("name") or ""),
                             "nom_id": str(item.get("nom_id") or ""),
                             "source": str(item.get("source") or "shelf")})
                total += price * entry["qty"]
            total = round(total, 2)
            if total <= 0:
                raise ValueError("Сумма продажи должна быть больше нуля")
            sale_id = uid("cs")
            stamp = now_iso()
            if method == "cash":
                for row in rows:
                    # недостающее приезжает со склада движением регистра
                    row["item_id"] = self._ensure_on_shelf(row, offer)
                    row["source"] = "shelf"
                    self.shelf.sale(row["item_id"], row["qty"], row["price"],
                                    channel="shelf", note="Касса: наличные")
                self.db.execute(
                    "INSERT INTO cashier_sales"
                    "(id,payment_id,method,amount,items,cashier,request_id,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (sale_id, "", "cash", total, json.dumps(rows, ensure_ascii=False),
                     cashier, request_id, stamp))
                result = self.db.one("SELECT * FROM cashier_sales WHERE id=?", (sale_id,))
                payload_out = self._sale_result(result)
                payload_out["paid"] = True
            else:
                payment = self.sbp.create(
                    amount=total, order_id="", purpose=f"Продажа на кассе · {len(rows)} поз.",
                    note="Касса", request_id=f"cashier:{sale_id}" if not request_id else request_id,
                    actor=cashier, qr_kind="dynamic")
                self.db.execute(
                    "INSERT INTO cashier_sales"
                    "(id,payment_id,method,amount,items,cashier,request_id,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (sale_id, payment["id"], "sbp", total,
                     json.dumps(rows, ensure_ascii=False), cashier, request_id, stamp))
                result = self.db.one("SELECT * FROM cashier_sales WHERE id=?", (sale_id,))
                payload_out = self._sale_result(result)
                payload_out["paid"] = False
                payload_out["payment"] = payment
                # QR для покупателя: динамический от банка или статический
                # QR магазина — рисуется на экране кассы
                payload_out["qr"] = self.payment_qr(payment, total)
        self._audit(sale_id, "sell", "Продажа на кассе", f"{method} · {total:g} ₽", actor=cashier)
        self.db.add_event("shelf", "Продажа на кассе",
                          f"{method} · {total:g} ₽ · {len(rows)} поз.",
                          data={"sale_id": sale_id, "method": method, "cashier": cashier})
        return payload_out

    def _sale_result(self, sale: dict, already_recorded: bool = False) -> dict:
        try:
            items = json.loads(sale.get("items") or "[]")
        except json.JSONDecodeError:
            items = []
        return {
            "ok": True,
            "sale_id": sale["id"],
            "method": sale.get("method") or "cash",
            "amount": round(num(sale.get("amount")), 2),
            "items": items,
            "cashier": sale.get("cashier") or "",
            "payment_id": sale.get("payment_id") or "",
            "confirmed": bool(str(sale.get("confirmed_at") or "")),
            "already_recorded": already_recorded,
        }

    # ------------------------------------------------- подтверждение СБП
    def incoming(self) -> dict:
        """СБП-продажи кассы, ожидающие сверки (только продажи, не заказы)."""
        rows = self.db.query(
            "SELECT s.*, p.number, p.status, p.purpose FROM cashier_sales s"
            " JOIN sbp_payments p ON p.id=s.payment_id"
            " WHERE s.method='sbp' AND COALESCE(s.confirmed_at,'')=''"
            " ORDER BY datetime(s.created_at) DESC LIMIT 50")
        out = []
        for row in rows:
            try:
                row["items"] = json.loads(row.get("items") or "[]")
            except json.JSONDecodeError:
                row["items"] = []
            out.append(row)
        return {"payments": out, "sbp_enabled": self.sbp.enabled()}

    def confirm_sbp(self, payment_id: str, token: str, *, note: str = "") -> dict:
        """Подтвердить СБП-продажу: списать склад и записать выручку.

        Одна транзакция: остатки проверяются заново (за время сверки полку мог
        продать другой кассир), списание и деньги проходят вместе. Повторный
        вызов не списывает склад и не пишет проводку повторно.
        """
        self.require(token)
        sale = self.db.one("SELECT * FROM cashier_sales WHERE payment_id=?", (payment_id,))
        if not sale:
            raise ValueError("Продажа не найдена")
        if str(sale.get("method") or "") != "sbp":
            raise ValueError("Это не СБП-продажа")
        if str(sale.get("confirmed_at") or ""):
            return {**self._sale_result(sale), "already_recorded": True}
        try:
            rows = json.loads(sale.get("items") or "[]")
        except json.JSONDecodeError:
            rows = []
        cashier = str(sale.get("cashier") or "кассир")[:120]
        with self.db.transaction():
            sale = self.db.one("SELECT * FROM cashier_sales WHERE payment_id=?", (payment_id,))
            if str(sale.get("confirmed_at") or ""):
                return {**self._sale_result(sale), "already_recorded": True}
            # 1) остатки проверяем до денег: не хватает — ничего не трогаем.
            # Смотрим единый каталог: за время сверки товар могли продать с
            # витрины, но он мог и приехать на склад.
            index = self._catalog_index()
            offer = self.stock_offer()
            for row in rows:
                item = index.get(str(row.get("item_id") or ""))
                if not item:
                    raise ValueError(f"Позиция «{row.get('name') or row['item_id']}» не найдена")
                if num(item.get("qty")) < num(row["qty"]):
                    raise ValueError(
                        f"«{item.get('name') or row.get('name')}» осталось "
                        f"{num(item.get('qty')):g} — продать {num(row['qty']):g} нельзя")
            # 2) списываем склад без отдельной проводки (деньги — через СБП);
            # недостающее на витрине доезжает со склада регистром движений
            for row in rows:
                row["item_id"] = self._ensure_on_shelf(row, offer)
                row["source"] = "shelf"
                self.shelf.sale(row["item_id"], row["qty"], row["price"],
                                channel="shelf", note="Касса: СБП", record_income=False)
            # 3) деньги на счёт СБП + статус платежа
            payment = self.sbp.confirm(payment_id, actor=cashier, note=note or "")
            self.db.execute(
                "UPDATE cashier_sales SET items=?, confirmed_at=? WHERE id=?",
                (json.dumps(rows, ensure_ascii=False), now_iso(), sale["id"]))
        sale = self.db.one("SELECT * FROM cashier_sales WHERE payment_id=?", (payment_id,))
        result = self._sale_result(sale)
        result["payment"] = payment
        self._audit(sale["id"], "confirm_sbp", "СБП-продажа подтверждена",
                    f"{num(sale['amount']):g} ₽", actor=cashier)
        return result

    def reject_sbp(self, payment_id: str, token: str, *, reason: str = "") -> dict:
        """Отклонить СБП-продажу: деньги не трогаем, склад не списывали."""
        self.require(token)
        sale = self.db.one("SELECT * FROM cashier_sales WHERE payment_id=?", (payment_id,))
        if not sale:
            raise ValueError("Продажа не найдена")
        if str(sale.get("method") or "") != "sbp":
            raise ValueError("Это не СБП-продажа")
        cashier = str(sale.get("cashier") or "кассир")[:120]
        payment = self.sbp.reject(payment_id, reason=reason or "Оплата не поступила",
                                  actor=cashier)
        self._audit(sale["id"], "reject_sbp", "СБП-продажа отклонена",
                    f"{num(sale['amount']):g} ₽", actor=cashier)
        return {**self._sale_result(sale), "payment": payment}

    # ---------------------------------------------------------------- аудит
    def _audit(self, entity_id: str, action: str, title: str,
               detail: str = "", data: dict | None = None, actor: str = "panel") -> None:
        try:
            self.db.execute(
                "INSERT INTO audit_log(at,entity,entity_id,action,title,detail,data)"
                " VALUES(?,?,?,?,?,?,?)",
                (now_iso(), "cashier_sale", entity_id, action, title, detail,
                 json.dumps({"actor": actor or "panel", **(data or {})}, ensure_ascii=False)))
        except Exception:
            pass
