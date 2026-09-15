"""Продажи и полка: флоу продажи кнопками, приход, касса магазина.

Флоу продажи (15.4) — позиция → количество → цена → канал → ✅ — живёт
в сцене `sell` (SQLite): начатая продажа не теряется при перезапуске
коннектора, просроченное ожидание честно начинается заново. Продажа
происходит только после нажатия ✅ — количество и цена до того никуда
не пишутся.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from ..accounting import num
from .scenes import SELL
from .ui import home_row, money, nav_row, page_from_command, paginate


class SalesMixin:
    """Полка: продать, оприходовать, снять выручку, сверить кассу."""

    # ------------------------------------------------------------ источники
    def _sell_rows(self) -> list[dict]:
        """Позиции стеллажа с остатком для быстрой продажи — все, без лимита."""
        from ..shelf import Shelf
        items = Shelf(self.db).items()
        rows = [i for i in items if num(i.get("qty")) > 0]
        rows.sort(key=lambda i: -num(i["qty"]))
        return rows

    def _sell_top_rows(self, limit: int = 5) -> list[dict]:
        """Топ продаж полки за 7 дней (штуки, без отменённых) — «часто продаём»."""
        since = (datetime.now() - timedelta(days=7)).isoformat()
        rows = self.db.query(
            "SELECT item_id, COALESCE(SUM(-qty),0) sold, "
            " COALESCE(SUM(-qty*price),0) money FROM shelf_moves"
            " WHERE kind IN ('sale','online') AND COALESCE(undone,0)=0"
            " AND datetime(at)>=? GROUP BY item_id"
            " HAVING sold>0 ORDER BY sold DESC LIMIT ?", (since, int(limit)))
        out = []
        for row in rows:
            item = self.db.one("SELECT * FROM shelf_items WHERE id=? AND active=1",
                               (row["item_id"],))
            if not item:
                continue
            item["sold"] = num(row.get("sold"))
            item["sold_money"] = num(row.get("money"))
            out.append(item)
        return out

    def _shelf_item(self, item_id: str) -> dict | None:
        return self.db.one("SELECT * FROM shelf_items WHERE id=? AND active=1",
                           (item_id,))

    # ------------------------------------------------------ экран «Продать»
    def sell_home_keyboard(self, chat: str, message_id: str = "") -> None:
        """Главный экран продажи: «Часто продаём» + «Все товары»."""
        top = self._sell_top_rows()
        lines = ["🛒 Продажа — выберите товар:", ""]
        buttons = []
        if top:
            lines.append("🔥 Часто продаём (7 дней):")
            for item in top:
                lines.append(f"• {item.get('name')} — {round(num(item['sold']),1)} шт за неделю")
                buttons.append([{"text": f"🔥 {str(item.get('name'))[:30]} · "
                                         f"{money(item.get('price'))}",
                                 "callback_data": f"cmd:sell-item:{item['id']}"}])
            lines.append("")
        buttons.append([{"text": "📋 Все товары", "callback_data": "cmd:sell-menu"}])
        buttons.append(home_row())
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def sell_keyboard(self, chat: str, page: int = 0, message_id: str = "") -> None:
        """Список позиций стеллажа: кнопки «−1» по цене ценника + листание."""
        rows = self._sell_rows()
        if not rows:
            self._reply(chat, "На стеллаже нет товара. Сделайте приход или перенесите со склада.")
            return
        page_rows, page, total_pages = paginate(rows, page)
        self._sell_page[chat] = page
        lines = ["🛍 Продажа со стеллажа — нажмите «−1» (деньги по цене ценника):"]
        buttons = []
        for i in page_rows:
            name = str(i.get('name') or 'позиция')
            lines.append(f"• {name} — {round(num(i['qty']),1)} шт · {money(i.get('price'))}")
            label = f"−1 · {name[:22]} · {money(i.get('price'))}"
            buttons.append([{"text": label[:60], "callback_data": f"cmd:shelf-sell:{i['id']}"}])
        if total_pages > 1:
            buttons.append(nav_row("sell-menu", page, total_pages))
        buttons.append(home_row())
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    # ------------------------------------------------ флоу продажи (15.4+)
    def sell_flow_qty(self, chat: str, item_id: str, message_id: str = "") -> None:
        """Шаг 1: количество — кнопки 1/2/5/10 и «своё число»."""
        item = self._shelf_item(item_id)
        if not item:
            self._reply(chat, "Позиция не найдена.")
            return
        self.scenes.set(chat, SELL, {"item": item_id, "qty": 1.0,
                                     "price": num(item.get("price")), "channel": "",
                                     "await": "qty"})
        lines = [f"✅ {item.get('name')} — остаток {round(num(item['qty']),1)} шт.",
                 "Сколько продать?"]
        buttons = [[{"text": "1 шт", "callback_data": "cmd:sell-qty:1"},
                    {"text": "2 шт", "callback_data": "cmd:sell-qty:2"},
                    {"text": "5 шт", "callback_data": "cmd:sell-qty:5"}],
                   [{"text": "10 шт", "callback_data": "cmd:sell-qty:10"}],
                   [{"text": "⏹ Отмена", "callback_data": "cmd:sell-cancel"}]]
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def sell_flow_price(self, chat: str, qty: float, message_id: str = "") -> None:
        """Шаг 2: цена — по умолчанию или своя (скидка)."""
        flow = (self.scenes.active(chat) or {}).get("data") or {}
        item = self._shelf_item(flow.get("item") or "")
        if not item:
            return self._reply(chat, "Позиция не найдена.")
        default = num(item.get("price"))
        flow.update({"qty": num(qty) or 1, "await": "price"})
        self.scenes.set(chat, SELL, flow)
        total = default * num(flow.get("qty") or 1)
        lines = [f"{item.get('name')} × {round(num(flow.get('qty')),1)} шт.",
                 "Цена за штуку (по умолчанию — ценник):"]
        buttons = [[{"text": f"Ценник · {money(default)} · итого {money(total)}",
                     "callback_data": "cmd:sell-price:default"}],
                   [{"text": "✏️ Своя цена (напишите число)",
                     "callback_data": "cmd:sell-price:custom"}],
                   [{"text": "⏹ Отмена", "callback_data": "cmd:sell-cancel"}]]
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def sell_flow_channel(self, chat: str, price: float, message_id: str = "") -> None:
        """Шаг 3: канал — полка (в кассу) или онлайн (на счёт)."""
        flow = (self.scenes.active(chat) or {}).get("data") or {}
        item = self._shelf_item(flow.get("item") or "")
        if not item:
            return self._reply(chat, "Позиция не найдена.")
        flow["price"] = num(price) if price and price > 0 else num(item.get("price"))
        flow["await"] = "channel"
        self.scenes.set(chat, SELL, flow)
        qty = num(flow.get("qty") or 1)
        total = flow["price"] * qty
        lines = [f"{item.get('name')} × {round(qty,1)} шт · {money(flow['price'])}/шт",
                 f"Итого: {money(total)}",
                 "Куда деньги?"]
        buttons = [[{"text": "🏪 Полка — в кассу магазина",
                     "callback_data": "cmd:sell-channel:shelf"},
                    {"text": "🌐 Онлайн (Авито/ТГ) — на счёт",
                     "callback_data": "cmd:sell-channel:online"}],
                   [{"text": "⏹ Отмена", "callback_data": "cmd:sell-cancel"}]]
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def sell_flow_confirm(self, chat: str, message_id: str = "") -> None:
        """Шаг 4: подтверждение на кнопке — продажа не происходит без ✅."""
        flow = (self.scenes.active(chat) or {}).get("data") or {}
        item = self._shelf_item(flow.get("item") or "")
        if not item:
            return self._reply(chat, "Позиция не найдена.")
        qty = num(flow.get("qty") or 1)
        price = num(flow.get("price") or item.get("price"))
        channel = flow.get("channel") or "shelf"
        flow["await"] = "confirm"
        self.scenes.set(chat, SELL, flow)
        channel_label = "онлайн (на счёте)" if channel == "online" else "в кассу магазина"
        total = price * qty
        lines = ["✅ Подтвердите продажу:",
                 f"{item.get('name')} × {round(qty,1)} шт × {money(price)} = {money(total)}",
                 f"Деньги: {channel_label}",
                 f"Остаток после: {round(max(0, num(item['qty']) - qty),1)} шт"]
        buttons = [[{"text": f"✅ Продать · {money(total)}",
                     "callback_data": "cmd:sell-confirm"}],
                   [{"text": "⏹ Отмена", "callback_data": "cmd:sell-cancel"}]]
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def sell_flow_do(self, chat: str) -> str:
        """Выполнить подтверждённую продажу по сохранённому флоу."""
        scene = self.scenes.pop(chat) or {}
        flow = scene.get("data") or {}
        item_id = flow.get("item") or ""
        if not item_id:
            return "Начните продажу заново: «Продать»."
        qty = num(flow.get("qty") or 1)
        price = num(flow.get("price") or 0)
        channel = flow.get("channel") or "shelf"
        return self.do_shelf_sell(item_id, qty, price=price, channel=channel)

    def sell_flow_cancel(self, chat: str) -> str:
        self.scenes.pop(chat)
        return "Продажа отменена. Ничего не списано."

    def sell_flow_done_buttons(self, chat: str, result: str) -> None:
        """Итог продажи с кнопками «ещё» и «забрали деньги» (ТЗ 15.4 §4.1).

        Продажа уже проведена — новый экран не спрашивает ничего, только
        предлагает продолжить тем же путём или зафиксировать выемку.
        """
        buttons = [
            [{"text": "🛒 Продать ещё", "callback_data": "cmd:sell-home"},
             {"text": "💰 Забрали деньги", "callback_data": "cmd:shelf-cash"}],
            [home_row()[0], {"text": "📦 Полка", "callback_data": "cmd:shelf"}],
        ]
        self._send_menu(chat, result, buttons)

    # ----------------------------------------------------------- операции
    def do_shelf_sell(self, item_id: str, qty: float = 1,
                      price: float | None = None, channel: str = "shelf") -> str:
        """Списать штуки с физической позиции стеллажа и записать доход.

        Цена и канал — параметры флоу продажи; «−1»-продажа по-прежнему
        работает с ценой ценника и каналом «shelf».
        """
        from ..shelf import Shelf
        shelf = Shelf(self.db)
        item = self._shelf_item(item_id)
        if not item:
            return "Позиция стеллажа не найдена."
        qty = num(qty) or 1
        if qty > num(item.get("qty")) + 0.004:
            return (f"На стеллаже только {round(num(item.get('qty')),1)} шт — "
                    f"продать {round(qty,1)} нельзя.")
        if channel not in ("shelf", "online"):
            channel = "shelf"
        price = num(price) if price is not None else num(item.get("price"))
        try:
            shelf.sale(item_id, qty, price, channel=channel,
                       note="продажа из Telegram")
            left = num(item.get("qty")) - qty
            money_line = f" · {money(price * qty)}" if price else " (без цены)"
            where = "на счёт (Авито/ТГ)" if channel == "online" else "в кассу магазина"
            return (f"✅ Продано {round(qty)} шт «{item.get('name')}»{money_line} — {where}. "
                    f"Осталось {round(max(0, left),1)} шт.")
        except Exception as exc:
            return f"Не получилось продать: {exc}"

    def do_sell(self, nom_id: str) -> str:
        """Продажа штуки номенклатуры: с витрины полкой, иначе со склада."""
        from ..documents import Documents
        from ..shelf import Shelf
        item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not item:
            return "Позиция не найдена."
        name = item.get("name") or "позиция"
        # И2: штуку с витрины продаёт полка — полка, регистр и деньги
        # списываются разом. Без витринной позиции — продажа со склада.
        try:
            positions = Shelf(self.db).items_for_nom(nom_id)
        except Exception:
            positions = []
        if positions:
            try:
                price = num(positions[0].get("price")) or Documents(self.db).price_of(nom_id)
                Shelf(self.db).sale(positions[0]["id"], 1, price, channel="shelf",
                                    note="продажа из Telegram")
                return f"Продано 1 шт «{name}» — списано с витрины и учтено в кассе."
            except Exception as exc:
                return f"Не получилось продать: {exc}"
        warehouse = self.db.one(
            "SELECT id FROM warehouses WHERE archived=0 AND retail=1 AND kind<>'shelf'"
            " ORDER BY position LIMIT 1") \
            or self.db.one(
                "SELECT id FROM warehouses WHERE archived=0 AND kind<>'shelf'"
                " ORDER BY position LIMIT 1")
        if not warehouse:
            return "Не настроен склад."
        docs = Documents(self.db)
        try:
            docs.quick_sale([{"nom_id": nom_id, "qty": 1}], warehouse["id"],
                            "shop", "", "продажа из Telegram")
            return f"Продано 1 шт «{name}» — проведено и учтено в кассе."
        except Exception as exc:
            return f"Не получилось продать: {exc}"

    # ------------------------------------------------------------ приход
    def shelf_produce_keyboard(self, chat: str, page: int = 0, message_id: str = "") -> None:
        """Быстрый приход +1 на позиции с планом пополнения или низким остатком."""
        from ..shelf import Shelf
        items = Shelf(self.db).items()
        candidates = [i for i in items if num(i.get("plan_qty")) > 0 or i.get("low") or i.get("status") == "empty"]
        candidates = candidates or items
        if not candidates:
            self._reply(chat, "Стеллаж пуст — сначала добавьте позицию.")
            return
        page_rows, page, total_pages = paginate(candidates, page)
        self._prod_page[chat] = page
        lines = ["📥 Приход на стеллаж (+1 шт). Для другого количества — «приход Адресник 5»."]
        buttons = []
        for i in page_rows:
            name = i.get("name") or "позиция"
            lines.append(f"• {name} — {round(num(i.get('qty')),1)} шт"
                         + (f" · нужно +{int(num(i.get('plan_qty')))}" if num(i.get("plan_qty")) else ""))
            buttons.append([{"text": f"+1 · {name[:24]}", "callback_data": f"cmd:shelf-prod:{i['id']}"}])
        if total_pages > 1:
            buttons.append(nav_row("shelf-prod-menu", page, total_pages))
        buttons.append(home_row())
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def do_shelf_produce(self, item_id: str, qty: float = 1) -> str:
        from ..shelf import Shelf
        item = self._shelf_item(item_id)
        if not item:
            return "Позиция стеллажа не найдена."
        qty = num(qty) or 1
        try:
            Shelf(self.db).produce(item_id, qty, note="приход из Telegram")
            return f"📥 Приход +{round(qty)} шт «{item.get('name')}» записан."
        except Exception as exc:
            return f"Не получилось оприходовать: {exc}"

    def _shelf_produce_text(self, chat: str, text: str) -> None:
        """«приход Адресник 5» — найти позицию по подстроке и сделать приход."""
        from ..shelf import Shelf
        parts = text.split()
        # Последнее слово может быть количеством.
        qty = 1.0
        name_words = parts[1:]
        if name_words:
            try:
                qty = float(name_words[-1].replace(",", "."))
                name_words = name_words[:-1]
            except ValueError:
                qty = 1.0
        query = " ".join(name_words).strip().lower()
        if not query:
            return self.shelf_produce_keyboard(chat)
        items = Shelf(self.db).items()
        matches = [i for i in items if query in (i.get("name") or "").lower()]
        if not matches:
            self._reply(chat, f"Позиция, содержащая «{query}», не найдена на стеллаже.")
            return
        if len(matches) > 1:
            names = "; ".join((i.get("name") or "")[:30] for i in matches[:5])
            self._reply(chat, f"Уточните позицию: совпало несколько — {names}.")
            return
        self._reply(chat, self.do_shelf_produce(matches[0]["id"], qty))

    # -------------------------------------------------------- касса полки
    def shelf_keyboard(self, chat: str) -> None:
        """Панель стеллажа: обзор, дефицит, продажи, приход, касса, движения."""
        text = self.text_shelf()
        buttons = [
            [{"text": "⚠ Нужны на полку", "callback_data": "cmd:shelf:needs"},
             {"text": "🛒 Продать", "callback_data": "cmd:sell-menu"}],
            [{"text": "📥 Приход +1", "callback_data": "cmd:shelf-prod-menu"},
             {"text": "💰 Касса", "callback_data": "cmd:shelf-cash"}],
            [{"text": "🧾 Движения", "callback_data": "cmd:shelf-moves"},
             {"text": "📊 Продажи 7 дн", "callback_data": "cmd:shelf-sales7"}],
            [{"text": "🔄 Обновить", "callback_data": "cmd:shelf"},
             {"text": "🏠 В меню", "callback_data": "cmd:menu"}],
        ]
        self._call("sendMessage", {"chat_id": chat, "text": text[:3800],
                                   "reply_markup": json.dumps(
                                       {"inline_keyboard": buttons}, ensure_ascii=False)}, timeout=15)

    def shelf_cash_keyboard(self, chat: str, message_id: str = "") -> None:
        """Меню кассы стеллажа: кнопки быстрой выемки + назад к полке."""
        from ..shelf import Shelf
        cash = Shelf(self.db).shop_cash()
        in_shop = num(cash.get("in_shop"))
        lines = [
            "💰 Касса стеллажа",
            f"• Лежит в магазине: {money(in_shop)}",
            f"• Забрали за все время: {money(cash.get('collected_total'))}",
            f"• Онлайн (Авито/ТГ): {money(cash.get('online_income'))} — на счёте",
            "",
            "Кнопки — быстрая выемка. Точная сумма: «забрали 2500».",
        ]
        buttons = []
        if in_shop >= 0.005:
            row = []
            for amount in (1000, 5000):
                if in_shop + 0.004 < amount:
                    continue
                row.append({"text": f"Забрали {money(amount)}",
                            "callback_data": f"cmd:shelf-cash-w:{amount}"})
            if row:
                buttons.append(row)
            buttons.append([{"text": f"Забрать всё · {money(in_shop)}",
                             "callback_data": "cmd:shelf-cash-w:all"}])
        buttons.append([{"text": "🧾 Сверка кассы", "callback_data": "cmd:shelf-cash-reconcile"},
                        {"text": "📊 Итог дня", "callback_data": "cmd:today"}])
        last = Shelf(self.db).collections(1)
        if last:
            row = last[0]
            buttons.append([{"text": f"↩️ Отменить {money(row.get('amount'))} "
                                     f"(последняя выемка)",
                             "callback_data": f"cmd:shelf-cash-undo:{row['id']}"}])
        buttons.append([{"text": "← Назад к полке", "callback_data": "cmd:shelf"}])
        buttons.append(home_row())
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def cash_reconcile_start(self, chat: str, message_id: str = "") -> None:
        """Сверка: показать ожидаемый остаток и попросить факт числом."""
        from ..shelf import Shelf
        cash = Shelf(self.db).shop_cash()
        expected = num(cash.get("in_shop"))
        self.scenes.set(chat, "cash_reconcile", {})
        lines = ["🧾 Сверка кассы магазина",
                 f"По учёту в кассе: {money(expected)}",
                 "Напишите фактическую сумму числом (например 1450).",
                 "Разницу посчитаю сам."]
        self._send_menu(chat, "\n".join(lines), [], message_id)

    def cash_reconcile_cancel(self, chat: str) -> None:
        scene = (self.scenes.active(chat) or {}).get("scene")
        if scene == "cash_reconcile":
            self.scenes.pop(chat)

    def _cash_fact(self, chat: str, text: str) -> str:
        """Факт наличных при сверке: разница считается и пишется в журнал."""
        from ..shelf import Shelf
        scene = self.scenes.active(chat)
        if scene and scene.get("scene") == "cash_reconcile":
            self.scenes.pop(chat)
        shelf = Shelf(self.db)
        cash = shelf.shop_cash()
        expected = num(cash.get("in_shop"))
        fact = num(text.replace(" ", "").replace(",", "."))
        diff = round(fact - expected, 2)
        line = (f"✅ Сверка: в кассе {money(fact)} — как по учёту."
                if abs(diff) < 0.005 else
                f"⚠ Сверка: факт {money(fact)}, по учёту {money(expected)}. "
                f"Разница {money(abs(diff))} ({'недостача' if diff < 0 else 'излишек'}).")
        try:
            self.db.add_event("money", "Сверка кассы магазина", line, chat, data={
                "expected": expected, "fact": fact, "diff": diff})
        except Exception:
            pass
        return line + "\n\nПроверьте выемки или сделайте инвентаризацию полки."

    def do_shelf_collect(self, spec: str) -> str:
        """Быстрая выемка из кассы магазина: сумма или «all» (забрать всё)."""
        from ..shelf import Shelf
        shelf = Shelf(self.db)
        state = shelf.shop_cash()
        in_shop = num(state.get("in_shop"))
        if in_shop <= 0.005:
            return "В кассе магазина сейчас нет денег от стеллажа."
        spec = str(spec or "").strip().lower()
        amount = in_shop if spec in ("all", "все", "всё") else num(spec)
        if amount <= 0:
            return "Укажите сумму: «забрали 5000»."
        if amount > in_shop + 0.005:
            return (f"В магазине лежит только {money(in_shop)} — "
                    f"забрать {money(amount)} нельзя.")
        try:
            shelf.add_collection(round(amount, 2), note="выемка из Telegram")
        except ValueError as exc:
            return f"Не получилось: {exc}"
        left = shelf.shop_cash().get("in_shop")
        return f"✅ Забрали {money(amount)}. В кассе магазина осталось {money(left)}."

    def do_collect_from_shop(self, text: str) -> str:
        """«забрали 5000» или «забрали все» — выемка из кассы магазина."""
        import re as _re
        from ..shelf import Shelf
        shelf = Shelf(self.db)
        m = _re.search(r"(\d[\d\s.,]*)\s*(?:р|руб|₽)?", text)
        state = shelf.shop_cash()
        if not m:
            if any(w in text.replace("ё", "е") for w in ("все", "всё", "всего")):
                amount = num(state.get("in_shop"))
                if amount <= 0:
                    return "В кассе магазина сейчас нет денег от стеллажа."
            else:
                return self.text_shop_cash()
        else:
            amount = num(m.group(1).replace(" ", "").replace(",", "."))
        note = text[m.end():].strip() if m else ""
        try:
            shelf.add_collection(amount, str(note)[:120])
        except ValueError as exc:
            return f"Не получилось: {exc}"
        c = shelf.shop_cash()
        return (f"✅ Забрали из магазина {money(amount)}.\n"
                f"Осталось в магазине: {money(c.get('in_shop'))}.")

    # ---------------------------------------------------- текстовые команды
    def cmd_shelf(self, chat: str, raw: str, text: str) -> None:
        self.shelf_keyboard(chat)

    def cmd_sell(self, chat: str, raw: str, text: str) -> None:
        self.sell_home_keyboard(chat)

    def cmd_produce(self, chat: str, raw: str, text: str) -> None:
        # «приход» — меню быстрого прихода; «приход Адресник 5» — конкретной позиции.
        self._shelf_produce_text(chat, text)

    def cmd_cash(self, chat: str, raw: str, text: str) -> None:
        self.shelf_cash_keyboard(chat)

    def cmd_collect(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.do_collect_from_shop(text))

    def cmd_shelf_moves(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_shelf_moves(15))

    def cmd_shelf_sales7(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self.text_shelf_sales(7))

    # ------------------------------------------------------- кнопки (callback)
    def cb_shelf(self, chat: str, message_id: str, params: str) -> None:
        self.shelf_keyboard(chat)

    def cb_shelf_needs(self, chat: str, params: str) -> str:
        return self.text_shelf(only_needs=True)

    def cb_sell_home(self, chat: str, message_id: str, params: str) -> None:
        self.sell_home_keyboard(chat, message_id)

    def cb_sell_menu(self, chat: str, message_id: str, params: str) -> None:
        if params:
            page = page_from_command(f"sell-menu:{params}", "sell-menu",
                                     self._sell_page, chat)
            return self.sell_keyboard(chat, page, message_id=message_id)
        self.sell_keyboard(chat, message_id=message_id)

    def cb_sell_item(self, chat: str, message_id: str, params: str) -> None:
        self.sell_flow_qty(chat, params, message_id)

    def cb_sell_qty(self, chat: str, message_id: str, params: str) -> None:
        self.sell_flow_price(chat, num(params), message_id)

    def cb_sell_price(self, chat: str, message_id: str, params: str) -> None:
        if params == "custom":
            return self._reply(chat, "✏️ Напишите цену за штуку числом (например 450)")
        self.sell_flow_channel(chat, num(params) or 0, message_id)

    def cb_sell_channel(self, chat: str, message_id: str, params: str) -> None:
        flow = (self.scenes.active(chat) or {}).get("data") or {}
        flow["channel"] = params
        self.scenes.set(chat, SELL, flow)
        self.sell_flow_confirm(chat, message_id)

    def cb_sell_confirm(self, chat: str, message_id: str, params: str) -> None:
        self.sell_flow_done_buttons(chat, self.sell_flow_do(chat))

    def cb_sell_cancel(self, chat: str, message_id: str, params: str) -> None:
        self._reply(chat, self.sell_flow_cancel(chat))

    def cb_sell_nom(self, chat: str, params: str) -> str:
        return self.do_sell(params)

    def cb_shelf_sell(self, chat: str, params: str) -> str:
        return self.do_shelf_sell(params)

    def cb_shelf_prod_menu(self, chat: str, message_id: str, params: str) -> None:
        if params:
            page = page_from_command(f"shelf-prod-menu:{params}", "shelf-prod-menu",
                                     self._prod_page, chat)
            return self.shelf_produce_keyboard(chat, page, message_id=message_id)
        self.shelf_produce_keyboard(chat, message_id=message_id)

    def cb_shelf_produce(self, chat: str, params: str) -> str:
        return self.do_shelf_produce(params)

    def cb_shelf_cash(self, chat: str, message_id: str, params: str) -> None:
        self.shelf_cash_keyboard(chat, message_id)

    def cb_cash_reconcile(self, chat: str, message_id: str, params: str) -> None:
        self.cash_reconcile_start(chat, message_id)

    def cb_shelf_collect(self, chat: str, message_id: str, params: str) -> None:
        self._reply(chat, self.do_shelf_collect(params))
        self.shelf_cash_keyboard(chat, message_id)

    def cb_cash_undo(self, chat: str, message_id: str, params: str) -> None:
        try:
            from ..shelf import Shelf
            Shelf(self.db).delete_collection(params)
            text = "↩️ Последняя выемка отменена — деньги вернулись в кассу."
        except Exception as exc:
            text = f"Не получилось отменить: {exc}"
        self._reply(chat, text)
        self.shelf_cash_keyboard(chat, message_id)

    def cb_shelf_moves(self, chat: str, params: str) -> str:
        return self.text_shelf_moves()

    def cb_shelf_sales7(self, chat: str, params: str) -> str:
        return self.text_shelf_sales(7)

    def cb_shelf_sales30(self, chat: str, params: str) -> str:
        return self.text_shelf_sales(30)
