"""Мобильная касса в LAN (Касса 16.0, итерация 3 — единый каталог склада).

Кассир на телефоне/планшете в локальной сети магазина продаёт за наличные или
по СБП. Деньги не дублируются и не расходятся с учётом:

* наличные — продажа сразу через ``shelf.sale`` (канал in_shop, как в панели
  и боте — один учёт);
* СБП (И2) — товар сразу откладывается на полку и встаёт в холд (резерв
  продажи): вторая продажа тех же штук не пройдёт. Выручка НЕ пишется до
  подтверждения; при подтверждении в одной транзакции холд снимается,
  склад списывается, доход идёт на счёт СБП. Отклонение и таймаут
  (``sbp_hold_hours``) снимают холд, товар остаётся на полке свободным;
* вход — личный PIN сотрудника (``staff.pin_hash``) или общий код магазина
  (настройка ``cashier_code``), сессия в памяти с ролью;
* смена — один открытый денежный ящик на всех; выемка и закрытие чужой
  смены — только роль «старший» (manager), проверяется на сервере;
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

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .accounting import Accounting, num, uid
from .config import now_iso
from .payment_purpose import build as build_purpose
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
        from .npd import Npd
        self.npd = Npd(db)   # одна строка про лимит режима на экране кассы
        self._sessions: dict[str, dict] = {}  # token -> {at, role}

    # ------------------------------------------------------------- сессии
    # Прочитанные из базы сессии вычищаем не чаще, чем раз в 5 минут: require()
    # вызывается на каждый запрос кассы, и писать в базу при каждом тапе —
    # лишняя работа на горячем пути продажи.
    _PRUNE_SECONDS = 300.0
    _pruned_at = 0.0

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()

    def _remember(self, token: str, session: dict) -> None:
        """Записать сессию в базу — чтобы рестарт сервера не сбрасывал кассу.

        Ошибку не поднимаем: память процесса держит сессию в любом случае, а
        касса не должна терять возможность продавать из-за сбоя реестра токенов.
        """
        try:
            life = datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL)
            self.db.execute(
                "INSERT OR REPLACE INTO cashier_tokens"
                "(token_hash,staff_id,cashier,role,legacy,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (self._token_hash(token), str(session.get("staff_id") or ""),
                 str(session.get("name") or ""), str(session.get("role") or "employee"),
                 1 if session.get("legacy") else 0, now_iso(),
                 life.astimezone().isoformat(timespec="seconds")))
        except Exception:
            pass

    def _forget(self, token: str) -> None:
        try:
            self.db.execute("DELETE FROM cashier_tokens WHERE token_hash=?",
                            (self._token_hash(token),))
        except Exception:
            pass

    def _recall(self, token: str) -> dict | None:
        """Найти сессию в базе: процесс перезапустился, а телефон помнит токен.

        Срок абсолютный (момент входа + 12 часов), как и в памяти процесса, —
        через базу токен «вечно живым» не становится.
        """
        row = self.db.one("SELECT * FROM cashier_tokens WHERE token_hash=?",
                          (self._token_hash(token),))
        if not row:
            return None
        expires = str(row.get("expires_at") or "")
        if expires:
            try:
                deadline = datetime.fromisoformat(expires)
                # значение без смещения (старая база/ручная правка) сравниваем
                # с локальным временем — иначе TypeError ронял вход на кассе
                now = (datetime.now().astimezone() if deadline.tzinfo else datetime.now())
                if now > deadline:
                    self._forget(token)
                    return None
            except ValueError:
                pass
        session = {"ts": time.time(), "role": str(row.get("role") or "employee"),
                   "staff_id": str(row.get("staff_id") or ""),
                   "name": str(row.get("cashier") or "")}
        if row.get("legacy"):
            session["legacy"] = True
        self._sessions[token] = session
        return session

    def _gc(self) -> None:
        now = time.time()
        stale = [t for t, s in self._sessions.items()
                 if now - float(s.get("ts", now)) > SESSION_TTL]
        for t in stale:
            self._sessions.pop(t, None)
        if now - Cashier._pruned_at < self._PRUNE_SECONDS:
            return
        Cashier._pruned_at = now
        try:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=1)).astimezone().isoformat(timespec="seconds")
            self.db.execute("DELETE FROM cashier_tokens WHERE expires_at<?", (cutoff,))
        except Exception:
            pass

    def login(self, code: str) -> dict:
        """Вход по личному PIN сотрудника или общему коду магазина.

        PIN ищется первым: сессия получает имя и роль из ``staff``. Общий
        код — запасной путь: пока не заведено ни одного PIN, он даёт роль
        «старший» (старые одиночные установки работают как раньше), после
        появления PIN — только «кассир» (выемка и чужие смены закрыты).
        """
        from .staff import Staff
        staff = Staff(self.db)
        code = str(code or "").strip()
        self._gc()
        if code:
            member = staff.find_by_pin(code)
            if member:
                role = str(member.get("role") or "employee")
                if role not in ("manager", "employee"):
                    role = "employee"
                token = uid("ck")
                self._sessions[token] = {
                    "ts": time.time(), "role": role,
                    "staff_id": member.get("id") or "",
                    "name": str(member.get("name") or ""),
                }
                self._remember(token, self._sessions[token])
                return {"token": token, "role": role,
                        "name": str(member.get("name") or ""),
                        "expires_in": SESSION_TTL}
        expected = str(self.db.setting("cashier_code", "") or "").strip()
        pins = staff.pins_count()
        if not expected and pins == 0:
            raise ValueError("Код кассы не задан — настройте его в панели (Касса и СБП)")
        if code and code == expected:
            role = "manager" if pins == 0 else "employee"
            token = uid("ck")
            self._sessions[token] = {"ts": time.time(), "role": role,
                                     "legacy": True}
            self._remember(token, self._sessions[token])
            return {"token": token, "role": role, "name": "",
                    "legacy": True, "expires_in": SESSION_TTL}
        if pins > 0:
            raise ValueError("Неверный PIN или код кассы")
        raise ValueError("Неверный код кассы")

    def logout(self, token: str) -> dict:
        token = str(token or "").strip()
        self._sessions.pop(token, None)
        self._forget(token)
        return {"ok": True}

    def require(self, token: str) -> dict:
        """Сессия кассира: память процесса, затем база.

        База нужна для надёжности, а не для удобства: коннектор обновляется,
        падает и перезагружается по watchdog — и касса не должна встать с
        «введите код снова» посреди смены (отчёты 16.1 обещали обратное).
        """
        self._gc()
        token = str(token or "").strip()
        session = self._sessions.get(token) or self._recall(token)
        if not session:
            raise ValueError("Сессия кассы истекла — введите код снова")
        return session

    def require_role(self, token: str, *roles: str) -> dict:
        """Сессия с одной из ролей — права проверяются на сервере."""
        session = self.require(token)
        if str(session.get("role") or "") not in roles:
            raise ValueError("Нужно право руководителя — позовите старшего")
        return session

    @staticmethod
    def _session_name(session: dict) -> str:
        return str(session.get("name") or "").strip()[:120]

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

    def _nom_map(self, nom_ids: list[str]) -> dict[str, dict]:
        """Карта nom_id → {group_id, group_name, niche_id, niche_name, photo, color}"""
        if not nom_ids:
            return {}
        # группы
        groups = {g["id"]: g for g in self.db.query("SELECT id,name,color FROM nom_groups WHERE archived=0")}
        niches = {n["id"]: n for n in self.db.query("SELECT id,name,color,icon FROM niches WHERE active=1")}
        out: dict[str, dict] = {}
        # batch query
        placeholders = ",".join("?" for _ in nom_ids)
        try:
            rows = self.db.query(f"SELECT id, group_id, niche_id, photo FROM nomenclature WHERE id IN ({placeholders})", nom_ids)
        except Exception:
            rows = []
        for r in rows:
            gid = str(r.get("group_id") or "")
            nid = str(r.get("niche_id") or "")
            g = groups.get(gid) or {}
            nch = niches.get(nid) or {}
            out[r["id"]] = {
                "group_id": gid,
                "group_name": g.get("name") or "",
                "group_color": g.get("color") or "",
                "niche_id": nid,
                "niche_name": nch.get("name") or "",
                "niche_color": nch.get("color") or "",
                "niche_icon": nch.get("icon") or "",
                "photo_file": r.get("photo") or "",
            }
        return out

    def _categories(self) -> list[dict]:
        """Список категорий для кассы: группы номенклатуры + ниши."""
        groups = self.db.query("SELECT id,name,color FROM nom_groups WHERE archived=0 ORDER BY position, name")
        niches = self.db.query("SELECT id,name,color,icon FROM niches WHERE active=1 ORDER BY position, name")
        cats: list[dict] = []
        # ниши как категории верхнего уровня
        for n in niches:
            cats.append({
                "id": f"niche:{n['id']}",
                "kind": "niche",
                "raw_id": n["id"],
                "name": n.get("name") or "Без названия",
                "color": n.get("color") or "#6366f1",
                "icon": n.get("icon") or "◆",
            })
        for g in groups:
            cats.append({
                "id": f"group:{g['id']}",
                "kind": "group",
                "raw_id": g["id"],
                "name": g.get("name") or "Без названия",
                "color": g.get("color") or "#6366f1",
                "icon": "",
            })
        return cats

    def catalog(self, *, _offer: dict[str, dict] | None = None) -> dict[str, Any]:
        """Единый каталог кассы: витрина + свободные остатки складов.

        Позиция полки, связанная с номенклатурой, показывает суммарную
        доступность ``shelf_qty + stock_qty`` — кассир не упирается в «нет в
        наличии», когда товар лежит на складе в соседней комнате. Товары,
        которых на витрине нет вовсе, приходят виртуальными позициями
        ``stock:<nom_id>`` и материализуются на полке при продаже.

        Расширенная версия: фото-URL, категории (группы/ниши), цвета.
        """
        # Продажа уже получает снимок доступного склада для последующего
        # перемещения на полку. Переиспользуем его вместо второго тяжёлого
        # запроса по регистру остатков и резервам.
        offer = _offer if _offer is not None else self.stock_offer()
        # Удержания витрины-зоны (резервы под заказы, холды СБП): связанные
        # позиции показывают доступность сверх физического остатка полки.
        _zone, zone_held = self._zone_held()
        items: list[dict] = []
        by_nom: dict[str, dict] = {}
        by_name: dict[str, dict] = {}
        by_id: dict[str, dict] = {}
        # собрать все nom_id для мапы категорий
        shelf_raw = [it for it in self.shelf.items() if it.get("active")]
        all_nom_ids = list({str(it.get("nom_id") or "").strip() for it in shelf_raw if str(it.get("nom_id") or "").strip()} | set(offer.keys()))
        nom_map = self._nom_map(all_nom_ids)

        for it in shelf_raw:
            nom_id = str(it.get("nom_id") or "").strip()
            shelf_qty = round(num(it.get("qty")), 3)
            held = round(num(zone_held.get(nom_id)), 3) if nom_id else 0.0
            free_qty = round(max(0.0, shelf_qty - held), 3)
            nm = nom_map.get(nom_id) or {}
            has_photo = bool(it.get("photo")) or bool(nm.get("photo_file"))
            # photo_url приоритет: shelf photo, затем nomenclature
            if it.get("photo"):
                photo_url = f"/api/shelf/photo.jpg?id={it['id']}"
            elif nm.get("photo_file"):
                photo_url = f"/api/nomenclature/photo.jpg?id={nom_id}"
            else:
                photo_url = ""
            row = {
                "id": it["id"], "name": it.get("name") or "",
                "price": round(num(it.get("price")), 2),
                "qty": free_qty, "shelf_qty": shelf_qty, "stock_qty": 0.0,
                "held": held,
                "status": str(it.get("status") or "ok"), "source": "shelf",
                "photo": has_photo,
                "photo_url": photo_url,
                "barcode": it.get("barcode") or "",
                "sku": it.get("sku") or "", "nom_id": nom_id,
                "unit": str(it.get("unit") or "шт"), "warehouse_name": "",
                "group_id": nm.get("group_id") or "",
                "group_name": nm.get("group_name") or "",
                "group_color": nm.get("group_color") or "",
                "niche_id": nm.get("niche_id") or "",
                "niche_name": nm.get("niche_name") or "",
                "niche_color": nm.get("niche_color") or "",
                "niche_icon": nm.get("niche_icon") or "",
            }
            items.append(row)
            by_id[row["id"]] = row
            if nom_id:
                by_nom.setdefault(nom_id, row)
            name_key = str(row["name"]).strip().lower()
            if name_key:
                by_name.setdefault(name_key, row)

        # Склад → витрина
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
                # дополнить фото если у shelf не было, а у nom есть
                if not target.get("photo_url") and (entry.get("photo") or nom_map.get(nom_id, {}).get("photo_file")):
                    target["photo_url"] = f"/api/nomenclature/photo.jpg?id={nom_id}"
                    target["photo"] = True
                by_nom.setdefault(nom_id, target)
                continue
            nm = nom_map.get(nom_id) or {}
            has_photo = bool(entry.get("photo")) or bool(nm.get("photo_file"))
            photo_url = f"/api/nomenclature/photo.jpg?id={nom_id}" if has_photo else ""
            items.append({
                "id": f"{STOCK_PREFIX}{nom_id}", "name": entry.get("name") or "",
                "price": round(num(entry.get("price")), 2), "qty": stock_qty,
                "shelf_qty": 0.0, "stock_qty": stock_qty,
                "status": "ok", "source": "stock",
                "photo": has_photo,
                "photo_url": photo_url,
                "barcode": "", "sku": "",
                "nom_id": nom_id, "unit": str(entry.get("unit") or "шт"),
                "warehouse_name": source_name,
                "group_id": nm.get("group_id") or "",
                "group_name": nm.get("group_name") or "",
                "group_color": nm.get("group_color") or "",
                "niche_id": nm.get("niche_id") or "",
                "niche_name": nm.get("niche_name") or "",
                "niche_color": nm.get("niche_color") or "",
                "niche_icon": nm.get("niche_icon") or "",
            })
        for row in items:
            if num(row["qty"]) <= 0:
                row["status"] = "empty"
            elif row["status"] == "empty":
                row["status"] = "ok"
            row["price_missing"] = num(row["price"]) <= 0
            # категория для фильтра: приоритет niche, затем group
            if row.get("niche_id"):
                row["category_id"] = f"niche:{row['niche_id']}"
                row["category_name"] = row.get("niche_name") or "Без категории"
            elif row.get("group_id"):
                row["category_id"] = f"group:{row['group_id']}"
                row["category_name"] = row.get("group_name") or "Без категории"
            else:
                row["category_id"] = ""
                row["category_name"] = "Без категории"
        items.sort(key=lambda x: (str(x.get("name") or "").lower(), x["id"]))
        return {
            "items": items,
            "categories": self._categories(),
            "sbp_enabled": self.sbp.enabled(),
            "sbp": self.payment_qr(with_svg=False),
            "shop_cash": self.shelf.shop_cash(),
            "shift_mode": self.shift_mode(),
            "npd": self.npd.cashier_note(),
            # «Звенеть о платежах» — серверное решение (можно выключить в
            # настройках для тихого зала), а на самом телефоне кассир глушит
            # сам: громкость устройства — не общее право.
            "ring": bool(self.db.setting("cashier_ring", True)),
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

    def _catalog_index(self, offer: dict[str, dict] | None = None) -> dict[str, dict]:
        """Каталог, разложенный по id — для валидации корзины."""
        return {str(it["id"]): it for it in self.catalog(_offer=offer)["items"]}

    # ------------------------------------------------------------- холды СБП
    def _hold_ttl(self) -> float:
        try:
            ttl = num(self.db.setting("sbp_hold_hours", 24), 24)
        except Exception:
            ttl = 24.0
        return ttl if ttl > 0 else 24.0

    def _release_expired_holds(self) -> int:
        """Ленивое снятие просроченных холдов — без фоновых задач."""
        try:
            from .stock import Stock
            return Stock(self.db).release_expired_holds(self._hold_ttl())
        except Exception:
            return 0

    def _release_sale_holds(self, sale_id: str) -> int:
        try:
            from .stock import Stock
            return Stock(self.db).release(doc_id=sale_id)
        except Exception:
            return 0

    def _hold_rows(self, rows: list[dict], sale_id: str) -> None:
        """Холды строк продажи на витрине-зоне.

        Без связки с номенклатурой или без склада-витрины холд невозможен —
        такие строки продаются по-старому (проверка в момент подтверждения).
        """
        from .stock import Stock
        stock = Stock(self.db)
        zone = stock.shelf_warehouse()
        if not zone:
            return
        for row in rows:
            nom_id = str(row.get("nom_id") or "").strip()
            if not nom_id:
                # Связка могла появиться усыновлением при перемещении.
                item = self.db.one("SELECT nom_id FROM shelf_items WHERE id=?",
                                   (str(row.get("item_id") or ""),)) or {}
                nom_id = str(item.get("nom_id") or "").strip()
                row["nom_id"] = nom_id
            if not nom_id:
                continue
            stock.hold(nom_id, zone, num(row["qty"]), sale_id)

    def _zone_held(self) -> tuple[str, dict[str, float]]:
        """Витрина-зона и карта удержаний на ней {nom_id: qty}."""
        try:
            from .stock import Stock
            stock = Stock(self.db)
            zone = stock.shelf_warehouse()
            if not zone:
                return "", {}
            return zone, stock.reserved_all(zone)
        except Exception:
            return "", {}

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
        nom_id = str(row.get("nom_id") or "")
        if not nom_id:
            item = self.db.one("SELECT nom_id FROM shelf_items WHERE id=?",
                               (item_id,)) or {}
            nom_id = str(item.get("nom_id") or "")
            row["nom_id"] = nom_id
        on_shelf = self._shelf_qty(item_id)
        if nom_id:
            # Захолдированное под другую продажу со склада не добираем —
            # недостающее везём со складов, чужой холд не трогаем.
            _zone, held_map = self._zone_held()
            on_shelf = round(max(0.0, on_shelf - num(held_map.get(nom_id))), 3)
        shortage = round(qty - on_shelf, 3)
        if shortage > 1e-9:
            if not nom_id:
                raise ValueError(
                    f"«{row.get('name') or item_id}»: на витрине не хватает "
                    f"{shortage:g} шт, а со складом позиция не связана")
            self._pull_from_stock(
                nom_id, shortage, offer, item_id=item_id,
                note=f"Касса: пополнение витрины · {row.get('name') or item_id}")
        return item_id

    # ------------------------------------------------------ офлайн-очередь
    def _offline_stamp(self, value: str) -> str:
        """Серверный штамп времени локальной продажи (или пустая строка).

        Касса в лесу без интернета живёт по своим часам. Доверять им целиком
        нельзя: «вчера» задним числом попало бы в закрытую смену и в сверку,
        а будущее переписало бы выручку «наперёд». Поэтому локальное время
        используется как факт, что продажа была раньше (для порядка строк и
        для смены), а в базу идёт серверный момент выгрузки.

        Часы вперёд больше чем на 2 минуты — отклоняем: такой «офлайн»
        обычно означает, что на телефоне сбито время, и порядок продаж в
        журнале перестанет иметь смысл.
        """
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Офлайн-штамп должен быть в формате ISO 8601") from None
        if when.tzinfo is None:
            when = when.astimezone()
        now = datetime.now(timezone.utc)
        if when > now + timedelta(seconds=120):
            raise ValueError("Время на кассе спешит больше чем на 2 минуты — "
                             "проверьте часы: журнал по нему не разложишь")
        if when < now - timedelta(days=3):
            raise ValueError("Продаже больше трёх суток — проведите её вручную "
                             "через панель, а не через очередь кассы")
        return now.replace(microsecond=0).isoformat()

    def _offline_fallback_item(self, row: dict) -> str:
        """Позиция витрины для товара, которого в каталоге уже нет.

        Пока касса была офлайн, позицию могли удалить или переименовать.
        Проданное уже у покупателя, деньги в ящике — значит, проводка обязана
        появиться: заводим позицию с ценой продажи и помечаем расхождение,
        чтобы владелец разобрался на «Полке». Тише, чем отказать в приёме денег.
        """
        price = num(row.get("price"))
        item = self.shelf.save_item({
            "name": str(row.get("name") or "Позиция из офлайн-очереди")[:200],
            "price": price if price > 0 else 0.0,
            "qty": 0.0,
            "unit": "шт",
            "nom_id": str(row.get("nom_id") or ""),
            "comment": "Создано выгрузкой офлайн-очереди кассы — проверьте остаток",
        })
        return str(item.get("id") or "")

    def _offline_flags(self, conflicts: list, allow_negative: bool, stamp: str,
                       claim: bool = False) -> str:
        """Пометка строки журнала: что разошлось при выгрузке из очереди.

        Пустая строка — чистая выгрузка, она не должна выглядеть подозрительной.
        Заявку СБП флагуем всегда: «это не оплата, а обещание оплаты» обязано
        читаться и через неделю, когда будут разбирать сверку с банком.
        """
        payload: dict = {}
        if conflicts:
            payload["negative_stock"] = conflicts
            payload["policy"] = "negative" if allow_negative else "block"
            payload["replayed_at"] = stamp[:19]
        if claim:
            payload["sbp_claim"] = {"at": stamp[:19],
                                    "how": "QR магазина · сумму ввёл покупатель"}
        if not payload:
            return ""
        return json.dumps(payload, ensure_ascii=False)[:4000]

    def _rows_to_shelf(self, rows: list, offer: dict, allow_negative: bool,
                       conflicts: list) -> None:
        """Довести строки до позиций витрины, готовых к списанию.

        Недостающее доезжает со склада движением регистра. Офлайн-выгрузка
        (``allow_negative``) не имеет права требовать идеала: покупатель уже ушёл
        с товаром, поэтому позицию оставляем как есть и пишем расхождение —
        иначе касса «теряет» деньги вместо того, чтобы показать дыру в учёте.
        """
        for row in rows:
            try:
                row["item_id"] = self._ensure_on_shelf(row, offer)
            except ValueError as exc:
                if not allow_negative:
                    raise
                # Позиция витрины на месте, но везти со склада нечего —
                # списываем в минус её, а не заводим дубль.
                keep = str(row.get("item_id") or "")
                if not keep or is_stock_id(keep) or not self.db.one(
                        "SELECT id FROM shelf_items WHERE id=?", (keep,)):
                    keep = self._offline_fallback_item(row)
                row["item_id"] = keep
                conflicts.append({"item_id": str(row["item_id"]),
                                  "name": str(row.get("name") or ""),
                                  "qty": num(row.get("qty")),
                                  "kind": "no-stock", "reason": str(exc)[:160]})
            row["source"] = "shelf"

    def _static_qr_ready(self) -> bool:
        """Дешёвая проверка «есть чем показать код» — без рисования картинки."""
        """Есть ли чем показать код оплаты без связи с банком.

        Офлайн-СБП держится на статическом QR магазина (ГОСТ-код по
        реквизитам или шаблон платёжной ссылки): его рисует PrintFlow, банк не
        спрашивается. Динамический QR без связи невозможен физически — врать про
        «оплату офлайн» и не давать код мы не имеем права.
        """
        from .payment_qr import build as build_qr
        try:
            qr = build_qr(self.db, amount=0.0, purpose="", payment=None, with_svg=False)
        except Exception:
            return False
        return bool(str(qr.get("text") or "").strip())

    def offline_qr(self) -> dict:
        """QR для кассы без связи: текст + векторная картинка + подсказка."""
        from .payment_qr import build as build_qr
        try:
            qr = build_qr(self.db, amount=0.0, purpose="", payment=None, with_svg=True)
        except Exception:
            return {}
        text = str(qr.get("text") or "").strip()
        if not text:
            return {}
        return {"text": text[:2000], "svg": str(qr.get("svg") or "")[:80000],
                "kind": str(qr.get("kind") or "static"),
                "amount_in_qr": bool(qr.get("amount_in_qr")),
                "hint": str(qr.get("hint") or "")[:300],
                "shop": str(self.db.setting("shop_name", "") or "")[:120]}

    # ------------------------------------------------------------- продажа
    def _discount_approval(self, session: dict, manager_pin: str) -> str:
        """Имя утвердившего скидку — PIN активного старшего.

        Пока PIN не заведены (режим одного владельца) подтверждения не надо:
        утверждает сам владелец — консистентно с выемкой из И3. Сам PIN
        в логи, аудит и события не попадает никогда — только имя.
        """
        from .staff import Staff
        staff = Staff(self.db)
        if staff.pins_count() == 0:
            return self._session_name(session) or "владелец"
        member = staff.find_by_pin(manager_pin)
        if not member or str(member.get("role") or "") != "manager":
            raise ValueError("Скидку подтверждает PIN старшего")
        return str(member.get("name") or "старший")

    def sell(self, items: list, method: str, token: str, *,
             request_id: str = "", cashier_name: str = "",
             discount_pct: float = 0.0, manager_pin: str = "",
             box_id: str = "", offline_at: str = "") -> dict:
        """Продажа корзины. Наличные — сразу в журнал; СБП — платёж до сверки.

        Идемпотентно по ``request_id`` (двойное нажатие не создаёт две продажи).
        Имя кассира берём из сессии (PIN), а не из запроса: клиенту не доверяем.

        ``offline_at`` — штамп локальной продажи (17.0.8): корзина, проданная
        во время обрыва, дожидалась связи в очереди кассы и приехала только что.
        Это не «послабление», а другой порядок: деньги те же, но остатки на
        момент выгрузки могли разойтись, и политикой ``cashier_offline_negative``
        владелец выбрал провести такую продажу с минус-остатком и флагом, а не
        блокировать прилавок.
        Скидка — процентом на чек, только с PIN старшего; сервер пересчитывает
        цены сам по каталогу. Учёт нетто: выручка — сумма со скидкой.
        """
        session = self.require(token)
        self._release_expired_holds()
        box_id = self._box(box_id)
        pct = round(num(discount_pct), 2)
        if pct < 0 or pct > 100:
            raise ValueError("Скидка — 0–100%")
        approved_by = ""
        if pct > 0:
            # Подтверждение — до транзакции: отказ не создаёт ни движений,
            # ни платежа, ни строки продажи.
            approved_by = self._discount_approval(session, manager_pin)
        method = str(method or "cash").strip().lower()
        if method not in METHODS:
            raise ValueError("Способ оплаты: наличные (cash) или СБП (sbp)")
        # Офлайн-режим (17.0.8). Клиентскому времени не верим по-взрослому:
        # «вчера» задним числом попало бы в чужую смену и в закрытую сверку.
        offline_at = str(offline_at or "").strip()[:32]
        moment = self._offline_stamp(offline_at) if offline_at else ""
        # Офлайн-СБП (17.0.9) — это не «оплата», а заявка об оплате: QR магазина
        # рисуется на кассе локально, покупатель платит со своего телефона по
        # мобильному интернету, а связь нужна только чтобы записать ожидание.
        # Дохода и списания склада до подтверждения банка не будет никогда.
        claim = bool(moment) and method == "sbp"
        if claim and not self._static_qr_ready():
            raise ValueError(
                "Офлайн-СБП невозможен: нет QR магазина. Со статическим QR оплата "
                "проходит без связи с сервером, с динамическим — не проходит")
        allow_negative = bool(moment) and bool(
            self.db.setting("cashier_offline_negative", True))
        conflicts: list[dict] = []
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
        # PIN-сессия знает имя — оно главнее присланного клиентом; общий код
        # имени не имеет — оставляем присланное (старые клиенты) или «кассир».
        cashier = (self._session_name(session)
                   or str(cashier_name or "").strip()[:120] or "кассир")
        request_id = str(request_id or "").strip()[:120]

        with self.db.transaction():
            if request_id:
                existing = self.db.one(
                    "SELECT * FROM cashier_sales WHERE request_id=?", (request_id,))
                if existing:
                    result = self._sale_result(existing, already_recorded=True)
                    if str(existing.get("method") or "") == "sbp":
                        payment = self.db.one(
                            "SELECT * FROM sbp_payments WHERE id=?",
                            (existing.get("payment_id") or "",)) or {}
                        result["payment"] = payment
                        result["qr"] = self.payment_qr(payment, num(existing.get("amount")))
                        result["paid"] = bool(str(existing.get("confirmed_at") or ""))
                    else:
                        result["paid"] = True
                    return result
            # Один снимок склада используется и для проверки корзины, и для
            # перемещения товара: не выполняем тяжёлый расчёт остатков дважды.
            offer = self.stock_offer()
            index = self._catalog_index(offer)
            rows = []
            total = 0.0
            list_total = 0.0
            for entry in payload:
                item = index.get(entry["item_id"])
                if not item:
                    raise ValueError("Позиция не найдена")
                left = num(item.get("qty"))
                if left < entry["qty"]:
                    if not allow_negative:
                        raise ValueError(
                            f"«{item.get('name') or entry['item_id']}» осталось {left:g} — "
                            f"продать {entry['qty']:g} нельзя")
                    # Продажу из очереди не отменить: покупатель уже ушёл с
                    # товаром, деньги легли в ящик. Проводим и флагуем.
                    conflicts.append({"item_id": str(entry["item_id"]),
                                      "name": str(item.get("name") or ""),
                                      "left": round(left, 3), "qty": num(entry["qty"]),
                                      "kind": "short"})
                price = num(item.get("price"))
                if price <= 0:
                    raise ValueError(
                        f"«{item.get('name') or entry['item_id']}»: цена не задана — "
                        "укажите её в номенклатуре или на ценнике")
                charged = round(price * (100.0 - pct) / 100.0, 2) if pct else round(price, 2)
                rows.append({"item_id": entry["item_id"], "qty": entry["qty"],
                             "price": charged, "list_price": round(price, 2),
                             "discount_pct": pct,
                             "name": str(item.get("name") or ""),
                             "nom_id": str(item.get("nom_id") or ""),
                             "source": str(item.get("source") or "shelf")})
                total += charged * entry["qty"]
                list_total += price * entry["qty"]
            total = round(total, 2)
            list_total = round(list_total, 2)
            discount_amount = round(list_total - total, 2)
            gift = method == "cash" and pct >= 100
            if total <= 0 and not gift:
                if method == "sbp":
                    raise ValueError("Нулевая сумма — только за наличные (дарение)")
                raise ValueError("Сумма продажи должна быть больше нуля")
            sale_id = uid("cs")
            stamp = now_iso()
            # offline_flags считается внутри ветки: часть расхождений
            # (no-stock) рождается только при выгрузке строк на полку.
            offline_flags = ""
            if method == "cash":
                self._ensure_auto_shift(box_id, cashier, stamp)
                # недостающее приезжает со склада движением регистра
                self._rows_to_shelf(rows, offer, allow_negative, conflicts)
                offline_flags = self._offline_flags(conflicts, allow_negative, stamp,
                                                    claim)
                for row in rows:
                    # Цены со скидкой — финальные: нулевую не возвращаем
                    # к каталожной (дарение), иначе подарили бы за деньги.
                    done = self.shelf.sale(row["item_id"], row["qty"], row["price"],
                                           channel="shelf", note="Касса: наличные",
                                           keep_zero_price=pct > 0,
                                           allow_negative=allow_negative)
                    # Связка для отмены: движение полки за строкой продажи.
                    row["move_id"] = str((done.get("move") or {}).get("id") or "")
                self.db.execute(
                    "INSERT INTO cashier_sales"
                    "(id,payment_id,method,amount,items,cashier,request_id,created_at,"
                    " discount_pct,discount_amount,box_id,offline_at,offline_flags)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sale_id, "", "cash", total, json.dumps(rows, ensure_ascii=False),
                     cashier, request_id, stamp, pct, discount_amount, box_id,
                     moment, offline_flags))
                result = self.db.one("SELECT * FROM cashier_sales WHERE id=?", (sale_id,))
                payload_out = self._sale_result(result)
                payload_out["paid"] = True
                if moment:
                    payload_out["offline_at"] = moment
                    if conflicts:
                        payload_out["negative_stock"] = conflicts
                # Годовой лимит режима — дело владельца, но узнаёт он об этом
                # от кассира: продажа, которая выбирает лимит, должна быть
                # видна в момент расчёта, а не в декабрьском отчёте.
                payload_out["npd"] = self.npd.cashier_note()
            else:
                # Товар откладываем на полку сразу (физически — кассиру в
                # руки) и ставим в холд: деньги придут позже, а штуки уже
                # заняты. Всё в одной транзакции: ошибка холда откатывает
                # и перемещение, и платёж.
                self._rows_to_shelf(rows, offer, allow_negative, conflicts)
                offline_flags = self._offline_flags(conflicts, allow_negative, stamp,
                                                    claim)
                # Назначение — из серверной корзины (названия каталога, не
                # клиента): «NOZZA: Адресник × 2». Состав — в платёж целиком.
                composition = [{"name": r["name"], "qty": r["qty"],
                                "price": r["price"]} for r in rows]
                payment = self.sbp.create(
                    amount=total, order_id="", items=composition,
                    note="Касса · офлайн-заявка" if claim else "Касса",
                    request_id=f"cashier:{sale_id}" if not request_id else request_id,
                    actor=cashier,
                    # Заявка из офлайна: сумма уже введена покупателем, банк
                    # просить динамический QR поздно и незачем — код магазина.
                    qr_kind="static" if claim else "dynamic")
                self._hold_rows(rows, sale_id)
                self.db.execute(
                    "INSERT INTO cashier_sales"
                    "(id,payment_id,method,amount,items,cashier,request_id,created_at,"
                    " discount_pct,discount_amount,box_id,offline_at,offline_flags)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sale_id, payment["id"], "sbp", total,
                     json.dumps(rows, ensure_ascii=False), cashier, request_id, stamp,
                     pct, discount_amount, box_id, moment, offline_flags))
                result = self.db.one("SELECT * FROM cashier_sales WHERE id=?", (sale_id,))
                payload_out = self._sale_result(result)
                payload_out["paid"] = False
                payload_out["payment"] = payment
                if moment:
                    payload_out["offline_at"] = moment
                    if conflicts:
                        payload_out["negative_stock"] = conflicts
                    if claim:
                        # Кассир должен видеть, что он записал не оплату, а
                        # обещание оплаты: пока банк не показал приход, деньги
                        # не существует.
                        payload_out["claim"] = True
                        payload_out["claim_note"] = (
                            "Заявка записана. Подтвердится, когда выписка банка "
                            "подтвердит приход — или руками во «Входящих»")
                # QR для покупателя: динамический от банка или статический
                # QR магазина — рисуется на экране кассы
                payload_out["qr"] = self.payment_qr(payment, total)
        label = str(payload_out.get("payment", {}).get("purpose") or "")
        if not label:
            # Наличные без банковского назначения — состав для журнала.
            label = build_purpose(rows, brand="")
        detail = f"{method} · {total:g} ₽"
        if pct > 0:
            detail += f" · скидка {pct:g}% ({approved_by})"
        if gift:
            detail += " · дарение"
        self._audit(sale_id, "sell", "Продажа на кассе", detail, actor=cashier)
        self.db.add_event("shelf", "Продажа на кассе",
                          f"{method} · {total:g} ₽ · {label}",
                          data={"sale_id": sale_id, "method": method,
                                "cashier": cashier, "discount_pct": pct,
                                "discount_approved_by": approved_by})
        if pct > 0:
            # Скидка видна владельцу отдельной строкой в ленте — при
            # нетто-выручке это единственный быстрый счёт щедрости.
            self.db.add_event(
                "shelf", "Скидка на кассе",
                f"{pct:g}% · −{discount_amount:g} ₽ · утвердил {approved_by}"
                + (" · дарение" if gift else ""),
                data={"sale_id": sale_id, "discount_pct": pct,
                      "discount_amount": discount_amount,
                      "approved_by": approved_by})
        return payload_out

    def _sale_result(self, sale: dict, already_recorded: bool = False) -> dict:
        try:
            items = json.loads(sale.get("items") or "[]")
        except json.JSONDecodeError:
            items = []
        amount = round(num(sale.get("amount")), 2)
        discount = round(num(sale.get("discount_amount")), 2)
        return {
            "ok": True,
            "sale_id": sale["id"],
            "method": sale.get("method") or "cash",
            "amount": amount,
            "items": items,
            "cashier": sale.get("cashier") or "",
            "payment_id": sale.get("payment_id") or "",
            "confirmed": bool(str(sale.get("confirmed_at") or "")),
            "cancelled": bool(str(sale.get("cancelled_at") or "")),
            "discount_pct": round(num(sale.get("discount_pct")), 2),
            "discount_amount": discount,
            "list_amount": round(amount + discount, 2),
            "box_id": str(sale.get("box_id") or ""),
            "refunded_amount": round(num(sale.get("refunded_amount")), 2),
            "refunded": bool(str(sale.get("refunded_at") or "")),
            "refund_left": round(max(0.0, amount - num(sale.get("refunded_amount"))), 2),
            "already_recorded": already_recorded,
            # Офлайн (17.0.8/17.0.9): «когда» спорное, расхождение — факт.
            "offline_at": str(sale.get("offline_at") or ""),
            "offline": self._offline_flags_dict(sale),
        }

    @staticmethod
    def _offline_flags_dict(sale: dict) -> dict | None:
        raw = str(sale.get("offline_flags") or "")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"unparsed": raw[:200]}
        return data if isinstance(data, dict) else None

    # ------------------------------------------------- подтверждение СБП
    def incoming(self) -> dict:
        """СБП-продажи кассы, ожидающие сверки (только продажи, не заказы)."""
        self._release_expired_holds()
        rows = self.db.query(
            "SELECT s.*, p.number, p.status, p.purpose FROM cashier_sales s"
            " JOIN sbp_payments p ON p.id=s.payment_id"
            " WHERE s.method='sbp' AND COALESCE(s.confirmed_at,'')=''"
            " AND p.status IN ('new','pending')"
            " ORDER BY datetime(s.created_at) DESC LIMIT 50")
        holds_map: dict[str, list] = {}
        sale_ids = [str(r.get("id") or "") for r in rows if r.get("id")]
        if sale_ids:
            try:
                marks = ",".join("?" for _ in sale_ids)
                for hold in self.db.query(
                        "SELECT r.doc_id, r.qty, r.at, n.name nom_name FROM reserves r"
                        " LEFT JOIN nomenclature n ON n.id=r.nom_id"
                        f" WHERE r.doc_id IN ({marks}) AND r.kind='hold'"
                        " AND r.state='active' ORDER BY datetime(r.at)",
                        sale_ids):
                    holds_map.setdefault(str(hold.get("doc_id") or ""),
                                         []).append(hold)
            except Exception:
                holds_map = {}
        out = []
        for row in rows:
            try:
                row["items"] = json.loads(row.get("items") or "[]")
            except json.JSONDecodeError:
                row["items"] = []
            row["holds"] = holds_map.get(str(row.get("id") or ""), [])
            # Офлайн-заявка должна быть видна во «Входящих» отдельным статусом:
            # это не «ждём деньги», а «ждём деньги, товар мог быть вынесен».
            row["offline"] = self._offline_flags_dict(row)
            row["claim"] = self._sale_claim(row)
            out.append(row)
        return {"payments": out, "sbp_enabled": self.sbp.enabled()}

    def confirm_sbp(self, payment_id: str, token: str, *, note: str = "") -> dict:
        """Подтвердить СБП-продажу: списать склад и записать выручку.

        Одна транзакция: остатки проверяются заново (за время сверки полку мог
        продать другой кассир), списание и деньги проходят вместе. Повторный
        вызов не списывает склад и не пишет проводку повторно. Подтвердить
        может любой кассир смены — авторство фиксируем по сессии.
        """
        session = self.require(token)
        self._release_expired_holds()
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
        cashier = (self._session_name(session)
                   or str(sale.get("cashier") or "кассир")[:120])
        # Офлайн-заявка: товар мог уйти с прилавка раньше, чем мы его увидели.
        # Отказать в подтверждении — значит оставить деньги в банке без
        # проводки, поэтому проводим в минус и оставляем флаг разбора.
        moment = str(sale.get("offline_at") or "")
        allow_negative = bool(moment) and bool(
            self.db.setting("cashier_offline_negative", True))
        offline_conflicts: list[dict] = []
        with self.db.transaction():
            sale = self.db.one("SELECT * FROM cashier_sales WHERE payment_id=?", (payment_id,))
            if str(sale.get("confirmed_at") or ""):
                return {**self._sale_result(sale), "already_recorded": True}
            # 0) свой холд снимаем до проверок: иначе свободный остаток не
            # увидит отложенный под эту же продажу товар и проверка упадёт.
            self._release_sale_holds(sale["id"])
            # 1) остатки проверяем до денег: не хватает — ничего не трогаем.
            # Смотрим единый каталог: за время сверки товар могли продать с
            # витрины, но он мог и приехать на склад. Один снимок регистра
            # используем и для проверки, и для последующего перемещения.
            offer = self.stock_offer()
            index = self._catalog_index(offer)
            for row in rows:
                item = index.get(str(row.get("item_id") or ""))
                if not item:
                    raise ValueError(f"Позиция «{row.get('name') or row['item_id']}» не найдена")
                if num(item.get("qty")) < num(row["qty"]):
                    if not allow_negative:
                        raise ValueError(
                            f"«{item.get('name') or row.get('name')}» осталось "
                            f"{num(item.get('qty')):g} — продать {num(row['qty']):g} нельзя")
                    offline_conflicts.append({"item_id": str(row.get("item_id")),
                                              "name": str(row.get("name") or ""),
                                              "left": round(num(item.get("qty")), 3),
                                              "qty": num(row["qty"]), "kind": "short"})
            # 2) списываем склад без отдельной проводки (деньги — через СБП);
            # недостающее на витрине доезжает со склада регистром движений
            self._rows_to_shelf(rows, offer, allow_negative, offline_conflicts)
            for row in rows:
                done = self.shelf.sale(row["item_id"], row["qty"], row["price"],
                                       channel="shelf", note="Касса: СБП",
                                       record_income=False,
                                       keep_zero_price=num(row.get("discount_pct")) > 0,
                                       allow_negative=allow_negative)
                row["move_id"] = str((done.get("move") or {}).get("id") or "")
            if offline_conflicts:
                # Расхождение записываем до денег: если что-то пойдёт не так,
                # транзакция откатится целиком, а не оставит «чистое»
                # подтверждение. Ключ заявки (sbp_claim) при этом сохраняем.
                merged = {}
                try:
                    merged = json.loads(str(sale.get("offline_flags") or "{}"))
                except json.JSONDecodeError:
                    merged = {}
                if not isinstance(merged, dict):
                    merged = {}
                fresh = json.loads(self._offline_flags(
                    offline_conflicts, allow_negative, now_iso()) or "{}")
                merged.update(fresh)
                self.db.execute(
                    "UPDATE cashier_sales SET items=?, offline_flags=? WHERE id=?",
                    (json.dumps(rows, ensure_ascii=False),
                     json.dumps(merged, ensure_ascii=False)[:4000], sale["id"]))
            # 3) деньги на счёт СБП + статус платежа
            # authorized=имя: роль уже проверена сессией кассы (login по PIN
            # или общему коду), поэтому повторно PIN в запросе не нужен.
            payment = self.sbp.confirm(payment_id, actor=cashier, note=note or "",
                                       authorized=cashier)
            self.db.execute(
                "UPDATE cashier_sales SET items=?, confirmed_at=? WHERE id=?",
                (json.dumps(rows, ensure_ascii=False), now_iso(), sale["id"]))
        sale = self.db.one("SELECT * FROM cashier_sales WHERE payment_id=?", (payment_id,))
        result = self._sale_result(sale)
        result["payment"] = payment
        self._audit(sale["id"], "confirm_sbp", "СБП-продажа подтверждена",
                    f"{num(sale['amount']):g} ₽", actor=cashier)
        return result

    def reject_sbp(self, payment_id: str, token: str, *, reason: str = "",
                   goods_taken: bool | None = None) -> dict:
        """Отклонить СБП-продажу: деньги не трогаем, склад не списывали.

        Доступно любому кассиру (рутинная сверка «не пришло»), но авторство
        и причина фиксируются в аудите — мошенничество видно руководителю.

        ``goods_taken`` — только для офлайн-заявок (17.0.9). Обычный случай
        «передумал, деньги не пришли»: товар никуда не уходил, холд сняли. В
        офлайне покупатель мог выйти из магазина с товаром, и тогда полку
        надо списывать в минус — иначе учёт врёт, что штука на месте. Спрашиваем
        явно и без ответа не отклоняем: угадывать за кассира мы не вправе.
        """
        session = self.require(token)
        sale = self.db.one("SELECT * FROM cashier_sales WHERE payment_id=?", (payment_id,))
        if not sale:
            raise ValueError("Продажа не найдена")
        if str(sale.get("method") or "") != "sbp":
            raise ValueError("Это не СБП-продажа")
        cashier = (self._session_name(session)
                   or str(sale.get("cashier") or "кассир")[:120])
        # Холд снимаем в одной транзакции с отклонением: товар остаётся на
        # полке и снова свободен для продажи.
        claim = self._sale_claim(sale)
        if claim and goods_taken is None:
            raise ValueError("Уточните, ушёл ли товар с покупателем: «ушёл» — "
                             "спишем в минус, «вернулся» — оставим на полке")
        taken_rows: list[dict] = []
        with self.db.transaction():
            self._release_sale_holds(sale["id"])
            payment = self.sbp.reject(payment_id, reason=reason or "Оплата не поступила",
                                      actor=cashier, authorized=cashier)
            if claim and goods_taken:
                taken_rows = self._writeoff_claim(sale, cashier)
        self._audit(sale["id"], "reject_sbp", "СБП-продажа отклонена",
                    f"{num(sale['amount']):g} ₽"
                    + (f" · товар вынесен, списано {len(taken_rows)} строк"
                       if taken_rows else ""), actor=cashier)
        return {**self._sale_result(sale), "payment": payment}

    def _sale_claim(self, sale: dict) -> bool:
        """Заявка ли это из офлайн-очереди (товар вынесен, денег ещё нет)."""
        if not str(sale.get("offline_at") or ""):
            return False
        try:
            flags = json.loads(str(sale.get("offline_flags") or "{}"))
        except json.JSONDecodeError:
            return False
        return bool(isinstance(flags, dict) and flags.get("sbp_claim"))

    def _writeoff_claim(self, sale: dict, cashier: str) -> list[dict]:
        """Списать товар офлайн-заявки, если покупатель ушёл с ним.

        Дохода нет — оплата не пришла, поэтому проводку в деньги не пишем
        вообще: списываем только склад (движение полки + регистр), чтобы
        остаток перестал врать. Расхождение видно событием и инвентаризацией.
        """
        try:
            rows = json.loads(sale.get("items") or "[]")
        except json.JSONDecodeError:
            rows = []
        done_rows = []
        for row in rows:
            item_id = str(row.get("item_id") or "")
            if not item_id:
                continue
            self.shelf.sale(item_id, num(row.get("qty")), num(row.get("price")),
                            channel="shelf",
                            note=f"Касса: заявка СБП отклонена · товар вынесен "
                                 f"({cashier})",
                            record_income=False, keep_zero_price=True,
                            allow_negative=True)
            done_rows.append({"item_id": item_id, "name": str(row.get("name") or ""),
                              "qty": num(row.get("qty"))})
        if done_rows:
            self.db.add_event(
                "shelf", "Заявка СБП отклонена: товар вынесен",
                f"{sale['id']}: {num(sale.get('amount')):g} ₽ не пришли, списано "
                f"{len(done_rows)} строк — проверьте остаток на полке",
                data={"sale_id": sale["id"], "rows": done_rows, "cashier": cashier})
        return done_rows

    # ------------------------------------------------------- отмена продажи
    def shift_sales(self, token: str, box_id: str = "") -> dict:
        """Продажи открытой смены — для экрана «Смена» и отмены."""
        self.require(token)
        box_id = self._box(box_id)
        shift = self._open_shift(box_id)
        if not shift:
            return {"open": False, "shift": None, "box_id": box_id,
                    "sales": []}
        rows = self.db.query(
            "SELECT s.*, p.status pay_status FROM cashier_sales s"
            " LEFT JOIN sbp_payments p ON p.id=s.payment_id"
            " WHERE COALESCE(s.box_id,'')=? AND s.created_at>=?"
            # datetime режет микросекунды: продажи в одну секунду
            # упорядочиваем по вставке (новые сверху).
            " ORDER BY datetime(s.created_at) DESC, s.rowid DESC LIMIT 100",
            (box_id, str(shift.get("opened_at") or "")))
        sales = []
        for row in rows:
            try:
                row["items"] = json.loads(row.get("items") or "[]")
            except json.JSONDecodeError:
                row["items"] = []
            row["cancelled"] = bool(str(row.get("cancelled_at") or ""))
            row["confirmed"] = bool(str(row.get("confirmed_at") or ""))
            # Возвраты видны прямо в списке: «продано 3, вернули 1» должно
            # читаться с телефона, а не высчитываться из разницы ящика.
            row["refunded_amount"] = round(num(row.get("refunded_amount")), 2)
            row["refunded"] = bool(str(row.get("refunded_at") or ""))
            row["refund_left"] = round(max(0.0, num(row.get("amount"))
                                           - num(row.get("refunded_amount"))), 2)
            # Офлайн-выгрузка: «когда» спорное, расхождение с остатком — факт.
            # Прячем в один ключ, чтобы экран смены не расползался по полю.
            row["offline_at"] = str(row.get("offline_at") or "")
            flags = str(row.get("offline_flags") or "")
            row["offline"] = json.loads(flags) if flags else None
            sales.append(row)
        return {"open": True, "shift": shift, "box_id": box_id, "sales": sales}

    def abandon_offline(self, request_ids: list, token: str, reason: str) -> dict:
        """Пометить записи журнала как «офлайн не был»: очередь чистят руками.

        Очередь кассы живёт в телефоне. Если продажи из неё так и не доехали
        (телефон утонул, браузер почищен), в ящике они уже учтены кассиром, а
        в базе их нет — и сверка «в кармане» никогда не сойдётся. Кассир может
        сказать вслух, что именно он выкинул из очереди, и это уходит в журнал
        событий и в аудит: не отмена продажи (её не было), а объяснение.
        """
        session = self.require(token)
        ids = [str(x or "").strip()[:120] for x in list(request_ids or [])]
        ids = [x for x in ids if x]
        reason = str(reason or "").strip()
        if not ids:
            raise ValueError("Укажите, какие записи очереди снимаются")
        if len(ids) > 50:
            raise ValueError("Списком больше 50 записей не работаем")
        if not reason:
            raise ValueError("Причина обязательна: «дубликат», «передумали»…")
        stamp = now_iso()
        who = self._session_name(session) or "кассир"
        found = []
        for rid in ids:
            row = self.db.one("SELECT id, amount, created_at FROM cashier_sales"
                              " WHERE request_id=?", (rid,))
            if row:
                found.append({"request_id": rid, "sale_id": str(row.get("id") or ""),
                              "amount": round(num(row.get("amount")), 2),
                              "created_at": str(row.get("created_at") or "")[:19]})
        self.db.add_event(
            "cashier", "Снята офлайн-очередь кассы",
            f"{who}: {len(ids)} записей, в журнале найдено {len(found)} · {reason[:300]}",
            data={"ids": ids[:50], "found": found, "reason": reason[:300],
                  "actor_role": str(session.get("role") or "cashier")})
        self._audit("offline_queue", "abandon", "Снята офлайн-очередь кассы",
                    f"{len(ids)} записей, найдено {len(found)} · {reason[:300]}",
                    data={"ids": ids[:50], "found": found}, actor=who)
        return {"ok": True, "removed": len(ids), "found": found}

    def cancel_sale(self, sale_id: str, token: str) -> dict:
        """Отменить наличную продажу текущей открытой смены.

        Любой кассир (решение И4), но только наличные и только в окне смены:
        отмена удаляет проводку, поэтому переписывать прошлое нельзя.
        СБП-продажи — из панели (там же возврат денег из банка).
        """
        session = self.require(token)
        sale = self.db.one("SELECT * FROM cashier_sales WHERE id=?",
                           (str(sale_id or "").strip(),))
        if not sale:
            raise ValueError("Продажа не найдена")
        if str(sale.get("method") or "") != "cash":
            raise ValueError("СБП-возврат — из панели")
        if str(sale.get("cancelled_at") or ""):
            return {**self._sale_result(sale), "already": True}
        if not self._shift_covering(str(sale.get("created_at") or "")):
            raise ValueError("Продажа не из текущей смены — отмена из панели")
        try:
            rows = json.loads(sale.get("items") or "[]")
        except json.JSONDecodeError:
            rows = []
        if not rows:
            raise ValueError("В продаже нет строк — отмена из панели")
        for row in rows:
            if not str(row.get("move_id") or ""):
                raise ValueError("Продажа до обновления — отмена из панели")
        author = self._session_name(session) or "кассир"
        with self.db.transaction():
            fresh = self.db.one("SELECT * FROM cashier_sales WHERE id=?",
                                (sale["id"],))
            if str(fresh.get("cancelled_at") or ""):
                return {**self._sale_result(fresh), "already": True}
            for row in rows:
                # undo_sale: штуки назад, проводка удаляется, сторно в зону.
                self.shelf.undo_sale(str(row.get("move_id") or ""))
            self.db.execute("UPDATE cashier_sales SET cancelled_at=? WHERE id=?",
                            (now_iso(), sale["id"]))
        done = self.db.one("SELECT * FROM cashier_sales WHERE id=?",
                           (sale["id"],))
        result = self._sale_result(done)
        self._audit(sale["id"], "cancel_sale", "Продажа на кассе отменена",
                    f"{num(sale.get('amount')):g} ₽", actor=author)
        self.db.add_event("shelf", "Отмена продажи в кассе",
                          f"{num(sale.get('amount')):g} ₽ · {author}",
                          data={"sale_id": sale["id"],
                                "amount": num(sale.get("amount")),
                                "cashier": author})
        return {**result, "already": False}

    @staticmethod
    def _refund_map(sale: dict) -> dict[str, float]:
        """Что уже возвращено по строкам продажи: {move_id: штуки}."""
        try:
            rows = json.loads(sale.get("refund_items") or "[]")
        except json.JSONDecodeError:
            rows = []
        out: dict[str, float] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            key = str(row.get("move_id") or "")
            if key:
                out[key] = round(out.get(key, 0.0) + num(row.get("qty")), 3)
        return out

    def return_sale(self, sale_id: str, token: str, *, note: str = "",
                    lines: list | None = None, request_id: str = "") -> dict:
        """Принять возврат: товар на полку, деньги из ящика, история цела.

        Это не «Отменить» (``cancel_sale``). Отмена — сторно ошибки внутри
        текущей смены, она убирает проводку, потому что операции как бы не
        было. Возврат — покупатель пришёл позже (иногда на следующий день):
        продажа остаётся в журнале и в своей смене, а деньги уходят сегодня
        проводкой ``expense/refund``. Переписывать прошлое задним числом
        нельзя: поменялись бы и выручка того дня, и налоговая база, и уже
        сданная сверка смены.

        Право — только старший (решение №9 ТЗ «Касса 16.0»: возвраты у
        руководителя). Только наличные: СБП возвращается из «Входящих» через
        ``sbp.refund``, потому что там решение принимает банк, а не касса.
        Возврат может быть частичным — по строкам и по штукам; сумма возврата
        не может превысить уплаченное по продаже.
        """
        session = self.require_role(token, "manager")
        sale = self.db.one("SELECT * FROM cashier_sales WHERE id=?",
                           (str(sale_id or "").strip(),))
        if not sale:
            raise ValueError("Продажа не найдена")
        if str(sale.get("method") or "") != "cash":
            raise ValueError("Возврат СБП — из «Входящих»: деньги возвращает банк")
        if str(sale.get("cancelled_at") or ""):
            raise ValueError("Продажа отменена — возвращать нечего")
        try:
            rows = json.loads(sale.get("items") or "[]")
        except json.JSONDecodeError:
            rows = []
        rows = [r for r in rows if isinstance(r, dict) and str(r.get("move_id") or "")]
        if not rows:
            raise ValueError("В продаже нет строк — возврат из панели")
        request_id = str(request_id or "").strip()[:120]
        # Повтор с тем же ключом — та же отметка, а не второй возврат: сеть на
        # телефоне в магазине рвётся регулярно, и деньги дважды не уходят.
        if request_id and str(sale.get("refund_request_id") or "") == request_id:
            out = self._sale_result(sale)
            out["already_recorded"] = True
            out["replayed"] = True
            # форма та же, что у свежего ответа: интерфейс не должен разбирать
            # «почему тут нет returned»
            out["returned"] = round(num(sale.get("refunded_amount")), 2)
            return out
        author = self._session_name(session) or "руководитель"
        done = self._refund_map(sale)
        want = {str(l.get("move_id") or ""): num(l.get("qty"))
                for l in (lines or []) if isinstance(l, dict)}
        if want:
            unknown = set(want) - {str(r.get("move_id") or "") for r in rows}
            if unknown:
                raise ValueError("В продаже нет таких строк: "
                                 + ", ".join(sorted(unknown))[:120])
        plan: list[tuple[dict, float, float]] = []
        for row in rows:
            move_id = str(row.get("move_id") or "")
            left = round(max(0.0, num(row.get("qty")) - done.get(move_id, 0.0)), 3)
            if not left:
                continue
            qty = round(num(want.get(move_id, left)) or left, 3) if want else left
            if qty > left + 1e-9:
                raise ValueError(f"«{row.get('name') or move_id}»: вернуть {round(qty)} "
                                 f"из {round(left)} нельзя")
            price = round(num(row.get("price")), 2)
            plan.append((row, qty, round(price * qty, 2)))
        if not plan:
            raise ValueError("По этой продаже всё уже возвращено")
        total = round(sum(item[2] for item in plan), 2)
        paid = round(num(sale.get("amount")), 2)
        already = round(num(sale.get("refunded_amount")), 2)
        if already + total > paid + 0.005:
            raise ValueError(f"Возврат больше уплаченного: вернули {already:g} ₽ "
                             f"из {paid:g} ₽")
        stamp = now_iso()
        with self.db.transaction():
            fresh = self.db.one("SELECT * FROM cashier_sales WHERE id=?", (sale["id"],))
            done = self._refund_map(fresh)
            note_text = str(note or "").strip()[:300] or "Возврат товара"
            lines_out: list[dict] = []
            tx_ids: list[str] = []
            for row, qty, amount in plan:
                move_id = str(row.get("move_id") or "")
                if done.get(move_id, 0.0) >= num(row.get("qty")) - 1e-9:
                    raise ValueError(f"«{row.get('name') or move_id}»: строка уже возвращена")
                res = self.shelf.return_stock(
                    move_id, qty, f"{note_text} · продажа {sale['id']}",
                    actor=author)
                tx = res.get("tx") or {}
                if tx.get("id"):
                    tx_ids.append(str(tx["id"]))
                lines_out.append({"move_id": move_id, "item_id": row.get("item_id"),
                                  "name": row.get("name"), "qty": qty,
                                  "amount": round(num(res.get("amount")), 2),
                                  "at": stamp, "by": author})
            totals = dict(done)
            for line in lines_out:
                key = str(line.get("move_id") or "")
                totals[key] = round(totals.get(key, 0.0) + num(line.get("qty")), 3)
            merged = json.dumps([{"move_id": k, "qty": v}
                                  for k, v in sorted(totals.items()) if v > 0],
                                 ensure_ascii=False)
            self.db.execute(
                "UPDATE cashier_sales SET refunded_at=?, refunded_by=?, refunded_amount=?,"
                " refund_items=?, refund_request_id=? WHERE id=?",
                (stamp, author, round(already + total, 2), merged, request_id, sale["id"]))
        out = self.db.one("SELECT * FROM cashier_sales WHERE id=?", (sale["id"],))
        result = self._sale_result(out)
        result["returned"] = total
        result["return_lines"] = lines_out
        result["tx_ids"] = tx_ids
        self._audit(sale["id"], "return_sale", "Возврат товара на кассе",
                    f"{total:g} ₽ · {len(plan)} строка(ок) · {note_text}",
                    {"amount": total, "tx_ids": tx_ids, "actor": author})
        # Тот же живой поток, что и приход денег: возврат — событие ящика, и
        # кассир обязан услышать, что из кассы кто-то что-то вытащил.
        self.db.add_event("finance", "Возврат денег из кассы",
                          f"{total:g} ₽ · {author} · {note_text}",
                          data={"sale_id": sale["id"], "amount": -abs(total),
                                "cashier": author, "signal": "money_out"})
        result["npd"] = self.npd.cashier_note()
        return result

    # ------------------------------------------------------- смены и выемка
    @staticmethod
    def _box(box_id: str = "") -> str:
        """Нормализованный id ящика ('' — основной)."""
        return str(box_id or "").strip()[:64]

    def _open_shift(self, box_id: str = "") -> dict | None:
        return self.db.one(
            "SELECT * FROM cashier_shifts WHERE COALESCE(closed_at,'')=''"
            " AND COALESCE(box_id,'')=? ORDER BY datetime(opened_at) LIMIT 1",
            (self._box(box_id),))

    def _shift_covering(self, created_at: str) -> dict | None:
        """Открытая смена, в окно которой попадает продажа (любой ящик).

        Продажа привязана к ящику, но отмена ищет покрывающую смену без
        привязки: деньги физически возвращаются из текущего ящика, а
        разводка наличных по ящикам — будущий раунд (см. box_id).
        """
        return self.db.one(
            "SELECT * FROM cashier_shifts WHERE COALESCE(closed_at,'')=''"
            " AND opened_at<=? ORDER BY datetime(opened_at) DESC LIMIT 1",
            (str(created_at or ""),))

    def _shift_totals(self, opened_at: str, end: str) -> dict[str, float]:
        """Расчёт смены за окно [opened_at, end): наличные и выемки.

        Наличными считаем доходы канала ``shelf`` — туда падают наличные
        продажи кассы и панели (один физический ящик). СБП и «онлайн» в ящик
        не попадают. Деньги фискальных продаж 1С показываем отдельной строкой
        ``income_1c``: по ленте неизвестно, нал это или карта, — в расчёт
        ожидаемого остатка их не включаем, владелец сверяет глазами.
        """
        opened_at = str(opened_at or "")
        end = str(end or "")
        # Наличные смены = доходы канала ``shelf`` МИНУС возвраты по нему же.
        # Без вычета каждый возврат выглядел бы как недостача в ящике: деньги
        # покупателю кассир выдаёт из этой же кассы, и сервер обязан ждать
        # ровно столько, сколько физически лежит в ящике.
        income = self.db.one(
            "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount END),0) s,"
            " COALESCE(SUM(CASE WHEN kind='expense' AND category='refund'"
            " THEN amount END),0) r FROM transactions"
            " WHERE channel='shelf' AND at>=? AND at<?",
            (opened_at, end)) or {}
        refunds = round(num(income.get("r")), 2)
        collected = self.db.one(
            "SELECT COALESCE(SUM(amount),0) s FROM shelf_collections"
            " WHERE at>=? AND at<?", (opened_at, end)) or {}
        one_c = self.db.one(
            "SELECT COALESCE(SUM(-qty*price),0) s FROM shelf_moves"
            " WHERE kind='sale' AND source='1c' AND qty<0"
            " AND COALESCE(undone,0)=0 AND at>=? AND at<?",
            (opened_at, end)) or {}
        return {"income_cash": round(num(income.get("s")) - refunds, 2),
                "collected": round(num(collected.get("s")), 2),
                "income_1c": round(num(one_c.get("s")), 2),
                "refunds": refunds}

    def shift_mode(self) -> str:
        """auto — смен не видно (решение №6 «без смен»), manual — кассир сам их открывает."""
        mode = str(self.db.setting("cashier_shift_mode", "auto") or "auto").strip().lower()
        return mode if mode in ("auto", "manual") else "auto"

    def _ensure_auto_shift(self, box_id: str, cashier: str, at: str = "") -> None:
        """Открытая смена «в фоне» — чтобы отмена продажи и выемка не осиротели.

        Обе операции привязаны к смене: отмена удаляет проводку и потому обязана
        происходить в окне смены, выемка — в пределах ящика смены. Полное же
        «без смен» лишает кассира этих двух вещей, поэтому смена живёт, но
        молча: открывается сама на первой наличной продаже, в журнал и события
        не пишет (шума нет), закрытие — только по сверке ящика (reconcile).

        Открытой сменой считается окно ``opened_at <= created_at продажи``,
        поэтому смена получает ровно штамп продажи: свой ``now_iso()`` здесь
        оказался бы на микросекунду позже и продажа «выпадала» бы из смены —
        отмена отвечала «не из текущей смены».
        """
        if self.shift_mode() != "auto":
            return
        if self._open_shift(box_id):
            return
        self.db.execute(
            "INSERT INTO cashier_shifts(id,staff_id,cashier,opened_at,open_cash,box_id)"
            " VALUES(?,?,?,?,?,?)",
            (uid("shf"), "", str(cashier or "кассир")[:120], at or now_iso(), 0.0, box_id))

    def current_shift(self, token: str, box_id: str = "") -> dict:
        """Открытая смена с живым расчётом — для экрана «Смена»/«Касса»."""
        self.require(token)
        box_id = self._box(box_id)
        shift = self._open_shift(box_id)
        if not shift:
            return {"open": False, "mode": self.shift_mode(), "shift": None,
                    "box_id": box_id,
                    "live": {"income_cash": 0.0, "collected": 0.0,
                             "income_1c": 0.0, "refunds": 0.0, "expected": 0.0}}
        totals = self._shift_totals(str(shift.get("opened_at") or ""),
                                    now_iso())
        expected = round(num(shift.get("open_cash")) + totals["income_cash"]
                         - totals["collected"], 2)
        return {"open": True, "mode": self.shift_mode(), "shift": shift,
                "live": {**totals, "expected": expected}}

    def reconcile(self, token: str, counted_cash: float, note: str = "",
                   box_id: str = "") -> dict:
        """Пересчёт ящика — «сверка наличных» без кнопок «открыть/закрыть смену».

        Кассир пересчитал ящик и ввёл факт. Один вызов закрывает открытую смену
        с этим фактом (расхождение считается как обычно и уходит в аудит) и
        сразу открывает следующую с этим же остатком — отсчёт наличных
        продолжается от последней сверки. Открытой смены нет: просто открываем
        с названной суммой (первый день, установка, сверка после простоя).

        Чужую смену пересчитывает только старший — то же правило, что у
        закрытия смены: пересчёт переписывает итог дня.
        """
        session = self.require(token)
        box_id = self._box(box_id)
        counted = round(num(counted_cash), 2)
        if counted < 0:
            raise ValueError("В ящике не может быть меньше нуля")
        shift = self._open_shift(box_id)
        holder = str((shift or {}).get("staff_id") or "")
        me = str(session.get("staff_id") or "")
        if shift and holder and me != holder \
                and str(session.get("role") or "") != "manager":
            raise ValueError("Это чужая смена — пересчитать может только старший")
        note = str(note or "").strip()[:400]
        who = self._session_name(session) or "кассир"
        expected = diff = 0.0
        with self.db.transaction():
            if shift:
                totals = self._shift_totals(str(shift.get("opened_at") or ""), now_iso())
                expected = round(num(shift.get("open_cash")) + totals["income_cash"]
                                 - totals["collected"], 2)
                diff = round(counted - expected, 2)
                self.db.execute(
                    "UPDATE cashier_shifts SET closed_at=?, close_cash=?, income_cash=?,"
                    " collected=?, diff=?, note=? WHERE id=?",
                    (now_iso(), counted, totals["income_cash"], totals["collected"],
                     diff,
                     ("пересчёт ящика · " + who + (f" · {note}" if note else ""))[:500],
                     shift["id"]))
            self.db.execute(
                "INSERT INTO cashier_shifts(id,staff_id,cashier,opened_at,open_cash,box_id)"
                " VALUES(?,?,?,?,?,?)",
                (uid("shf"), str(session.get("staff_id") or ""), who, now_iso(),
                 counted, box_id))
        self._audit(str((shift or {}).get("id") or "now"), "reconcile",
                    "Пересчёт ящика",
                    f"факт {counted:g} ₽ · расчёт {expected:g} ₽ · расхождение {diff:+g} ₽"
                    + (f" · {note}" if note else "")
                    + (f" · ящик {box_id}" if box_id else ""),
                    entity="cashier_shift", actor=who)
        self.db.add_event(
            "money", "Касса: пересчёт ящика",
            f"{who}: в ящике {counted:g} ₽, по расчёту {expected:g} ₽ "
            f"(расхождение {diff:+g} ₽)",
            data={"counted": counted, "expected": expected, "diff": diff,
                  "box_id": box_id, "cashier": who})
        out = self.current_shift(token, box_id)
        out.update({"ok": True, "counted": counted, "expected": expected, "diff": diff,
                    "closed_shift": str((shift or {}).get("id") or "")})
        return out

    def open_shift(self, token: str, open_cash: float = 0.0,
                   box_id: str = "") -> dict:
        """Открыть смену: пересчитать ящик и зафиксировать старт."""
        session = self.require(token)
        box_id = self._box(box_id)
        busy = self._open_shift(box_id)
        if busy:
            raise ValueError(
                f"Смена уже открыта ({busy.get('cashier') or 'кассир'}, "
                f"с {str(busy.get('opened_at') or '')[:16]})"
                + (f" · ящик {box_id}" if box_id else "")
                + " — сначала закройте её")
        open_cash = round(num(open_cash), 2)
        if open_cash < 0:
            raise ValueError("В ящике не может быть меньше нуля")
        cashier = self._session_name(session) or "кассир"
        shift_id = uid("shf")
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO cashier_shifts(id,staff_id,cashier,opened_at,open_cash,box_id)"
            " VALUES(?,?,?,?,?,?)",
            (shift_id, str(session.get("staff_id") or ""), cashier,
             stamp, open_cash, box_id))
        self._audit(shift_id, "open_shift", "Смена открыта",
                    f"{open_cash:g} ₽ в ящике"
                    + (f" · ящик {box_id}" if box_id else ""),
                    entity="cashier_shift", actor=cashier)
        return self.current_shift(token, box_id)

    def close_shift(self, token: str, close_cash: float,
                    note: str = "", box_id: str = "") -> dict:
        """Закрыть смену: пересчёт ящика, расчёт сервера, расхождение.

        Чужую смену закрывает только старший. Расхождение не блокирует
        закрытие — оно фиксируется в аудите и событии (недостача/излишек).
        """
        session = self.require(token)
        box_id = self._box(box_id)
        shift = self._open_shift(box_id)
        if not shift:
            raise ValueError("Открытой смены нет — нечего закрывать")
        # Именная смена — только владелец или старший; безымянную (открыта
        # по общему коду) закрывает любой у ящика — анонимные сессии
        # неразличимы, это тот же уровень доверия, что и общий код.
        holder = str(shift.get("staff_id") or "")
        me = str(session.get("staff_id") or "")
        if holder and me != holder \
                and str(session.get("role") or "") != "manager":
            raise ValueError("Это чужая смена — закрыть может только старший")
        close_cash = round(num(close_cash), 2)
        if close_cash < 0:
            raise ValueError("В ящике не может быть меньше нуля")
        stamp = now_iso()
        totals = self._shift_totals(str(shift.get("opened_at") or ""), stamp)
        expected = round(num(shift.get("open_cash")) + totals["income_cash"]
                         - totals["collected"], 2)
        diff = round(close_cash - expected, 2)
        note = str(note or "").strip()[:500]
        cashier = self._session_name(session) or "кассир"
        with self.db.transaction():
            self.db.execute(
                "UPDATE cashier_shifts SET closed_at=?, close_cash=?,"
                " income_cash=?, collected=?, diff=?, note=? WHERE id=?",
                (stamp, close_cash, totals["income_cash"], totals["collected"],
                 diff, note, shift["id"]))
        self._audit(shift["id"], "close_shift", "Смена закрыта",
                    f"факт {close_cash:g} ₽ · расчёт {expected:g} ₽ · "
                    f"расхождение {diff:+g} ₽" + (f" · {note}" if note else "")
                    + (f" · ящик {box_id}" if box_id else ""),
                    entity="cashier_shift", actor=cashier)
        self.db.add_event(
            "money", "Смена закрыта",
            f"{cashier}: факт {close_cash:g} ₽, расчёт {expected:g} ₽ "
            f"(расхождение {diff:+g} ₽)"
            + (f" · ящик {box_id}" if box_id else ""),
            data={"shift_id": shift["id"], "diff": diff, "box_id": box_id})
        out = self.db.one("SELECT * FROM cashier_shifts WHERE id=?",
                          (shift["id"],)) or {}
        out["expected"] = expected
        out["income_1c"] = totals["income_1c"]
        return {"ok": True, "shift": out}

    def collect(self, token: str, amount: float, note: str = "",
                box_id: str = "") -> dict:
        """Выемка из ящика — только старший, только в пределах остатка.

        Привязывается к открытой смене (если есть) — смена видит выемку
        в своём расчёте. Лимит остатка проверяет ``shelf.add_collection``.
        """
        session = self.require_role(token, "manager")
        box_id = self._box(box_id)
        amount = round(num(amount), 2)
        if amount <= 0:
            raise ValueError("Сумма выемки должна быть больше нуля")
        cashier = self._session_name(session) or "старший"
        note = str(note or "").strip()[:500]
        with self.db.transaction():
            collection = self.shelf.add_collection(amount, note)
            shift = self._open_shift(box_id)
            shift_id = str((shift or {}).get("id") or "")
            if shift_id:
                self.db.execute("UPDATE shelf_collections SET shift_id=?"
                                " WHERE id=?", (shift_id, collection["id"]))
        self._audit(collection["id"], "collect", "Выемка из ящика",
                    f"{amount:g} ₽" + (f" · {note}" if note else "")
                    + (f" · смена {shift_id}" if shift_id else " · вне смены"),
                    entity="cashier_shift", actor=cashier)
        return {"ok": True, "collection": collection, "shift_id": shift_id}

    # ---------------------------------------------------------------- аудит
    def _audit(self, entity_id: str, action: str, title: str,
               detail: str = "", data: dict | None = None, actor: str = "panel",
               entity: str = "cashier_sale") -> None:
        try:
            self.db.execute(
                "INSERT INTO audit_log(at,entity,entity_id,action,title,detail,data)"
                " VALUES(?,?,?,?,?,?,?)",
                (now_iso(), entity, entity_id, action, title, detail,
                 json.dumps({"actor": actor or "panel", **(data or {})}, ensure_ascii=False)))
        except Exception:
            pass
