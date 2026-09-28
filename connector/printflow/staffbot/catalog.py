"""Каталог (номенклатура) в Telegram: витрина, цены, карточки.

Каталог — это «Товары» панели, а не стеллаж: карточки, цены, публикация
в витрине клиентского бота. Просмотр доступен всем ролям, правки (цены,
публикация, архив, удаление) — группе «catalog» (руководитель и владелец).
Опасные правки (удаление, массовый пересчёт) требуют подтверждения.
"""
from __future__ import annotations

import time

from ..accounting import num
from ..config import now_iso
from .ui import money, nav_row, paginate


class CatalogMixin:
    """Список каталога, карточки и правки позиций."""

    # ------------------------------------------------------------ доступ
    def _nom(self):
        from ..nomenclature import Nomenclature
        return Nomenclature(self.db)

    def _is_published(self, item: dict) -> bool:
        """Позиция показывается покупателям (флаг клиентского бота)."""
        return bool(num(item.get("client_bot_published"), 1))

    def _find_items(self, query: str, include_archived: bool = False) -> list[dict]:
        """Поиск позиций каталога по названию, артикулу или коду."""
        query = (query or "").strip().lower().replace("ё", "е")
        if not query:
            return []
        return [i for i in self._nom().items(include_archived=include_archived)
                if query in (i.get("name") or "").lower().replace("ё", "е")
                or query in (i.get("sku") or "").lower()
                or query in (i.get("code") or "").lower()]

    def _resolve_item(self, query: str, include_archived: bool = False):
        """Найти ровно одну позицию: (item, "") либо (None, подсказка)."""
        matches = self._find_items(query, include_archived=include_archived)
        if not matches:
            return None, f"Позиция «{query}» в каталоге не найдена. Поиск: «каталог {query}»."
        if len(matches) > 1:
            names = "; ".join((i.get("name") or "")[:30] for i in matches[:6])
            return None, f"Уточните позицию — совпало несколько: {names}."
        return matches[0], ""

    def _resolve_name_tail(self, words: list[str]):
        """Разделить «<Название> … хвост»: имя ищем самым длинным префиксом.

        Названия бывают из нескольких слов («Адресник для ключей»), поэтому
        одно слово после команды — ещё не имя целиком.
        """
        for k in range(len(words), 0, -1):
            item, _ = self._resolve_item(" ".join(words[:k]), include_archived=True)
            if item:
                return item, " ".join(words[k:]).strip()
        return None, ""

    def _catalog_rows(self, filt: str = "all", search: str = "") -> list[dict]:
        """Строки меню каталога с учётом фильтра и поиска."""
        items = self._nom().items(search=search, include_archived=(filt == "arc"))
        if filt == "pub":
            return [i for i in items if not num(i.get("archived")) and self._is_published(i)]
        if filt == "hid":
            return [i for i in items
                    if not num(i.get("archived")) and not self._is_published(i)]
        if filt == "arc":
            return [i for i in items if num(i.get("archived"))]
        return [i for i in items if not num(i.get("archived"))]

    # -------------------------------------------------------------- экраны
    def text_catalog(self, search: str = "") -> str:
        """Сводка каталога одной строкой на позицию."""
        rows = self._catalog_rows("all", search)
        published = sum(1 for i in rows if self._is_published(i))
        low = [i for i in rows if i.get("status") in ("low", "empty")]
        vitrine_on = bool(self.db.setting("client_bot_catalog", True))
        lines = []
        if search:
            lines.append(f"🔍 Поиск «{search}» — найдено позиций: {len(rows)}.")
        if not rows:
            lines.append("🗂 Каталог пуст" + (" — совпадений нет." if search else
                          ". Позиции создаются кнопкой «+» во вкладке «Товары» "
                          "или командой «товар <Название> <цена>»."))
            return "\n".join(lines)
        lines.append(f"🗂 Каталог: активных позиций {len(rows)} · "
                     f"в витрине 👁 {published} · дефицит {len(low)} · "
                     f"витрина бота: {'вкл' if vitrine_on else 'ВЫКЛ'}")
        for i in sorted(rows, key=lambda x: (x.get("status") not in ("empty", "low"),
                                             str(x.get("name") or "")))[:8]:
            marks = "👁" if self._is_published(i) else "🙈"
            tail = ""
            if num(i.get("archived")):
                marks, tail = "🗄", " · в архиве"
            price = f"{money(i.get('price'))} · " if num(i.get("price")) else "без цены · "
            lines.append(f"• {marks} {i.get('name') or 'Без названия'} — {price}"
                         f"{round(num(i.get('qty')), 1)} {i.get('unit') or 'шт'}{tail}")
        if len(rows) > 8:
            lines.append(f"… ещё {len(rows) - 8} поз. — листайте кнопками.")
        return "\n".join(lines)

    def catalog_menu(self, chat: str, filt: str = "all", page: int = 0,
                     message_id: str = "") -> None:
        """Меню каталога: список с пагинацией, фильтры и кнопки управления."""
        from ..staff import gate
        self._cat_filter[chat] = filt if filt in ("all", "pub", "hid", "arc") else "all"
        search = self._cat_query.get(chat, "")
        rows = self._catalog_rows(self._cat_filter[chat], search)
        filt_names = {"all": "Все", "pub": "Витрина", "hid": "Скрытые", "arc": "Архив"}
        head = f"🗂 Каталог · {filt_names[self._cat_filter[chat]]}"
        if search:
            head += f" · поиск «{search}»"
        lines = [head, self.text_catalog(search), ""]
        buttons: list[list[dict]] = []
        if rows:
            page_rows, page, total_pages = paginate(rows, page)
            self._cat_page[chat] = page
            for i in page_rows:
                mark = "🗄" if num(i.get("archived")) else \
                    ("👁" if self._is_published(i) else "🙈")
                price = money(i.get("price")) if num(i.get("price")) else "без цены"
                label = f"{mark} {i.get('name') or 'позиция'} · {price}"[:60]
                buttons.append([{"text": label,
                                 "callback_data": f"cmd:cati:{i['id']}"}])
            if total_pages > 1:
                buttons.append(nav_row(f"cat:{self._cat_filter[chat]}",
                                       page, total_pages))
        buttons.append([
            {"text": "📦 Все", "callback_data": "cmd:cat:all:0"},
            {"text": "👁 Витрина", "callback_data": "cmd:cat:pub:0"}])
        buttons.append([
            {"text": "🙈 Скрытые", "callback_data": "cmd:cat:hid:0"},
            {"text": "🗄 Архив", "callback_data": "cmd:cat:arc:0"}])
        buttons.append([{"text": "🗂 Группы", "callback_data": "cmd:cat-grps"}])
        if "catalog" in gate(self.db, chat)["allowed"]:
            vitrine_on = bool(self.db.setting("client_bot_catalog", True))
            lines.append("Правки: «цена <товар> <сумма>» · «скрыть <товар>» · "
                         "«товар <Название> <цена>».")
            buttons.append([
                {"text": "🏪 Витрина: " + ("вкл" if vitrine_on else "ВЫКЛ"),
                 "callback_data": "cmd:cat-vitrine"},
                {"text": "↻ Пересчёт цен", "callback_data": "cmd:cat-recalc:all"}])
        buttons.append([{"text": "🏠 В меню", "callback_data": "cmd:menu"}])
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def catalog_card(self, chat: str, nom_id: str, message_id: str = "") -> None:
        """Карточка позиции каталога с кнопками правок (по правам роли)."""
        from ..staff import gate
        item = self._nom().item(nom_id)
        if not item:
            return self._send_menu(chat, "Позиция не найдена — возможно, удалена.",
                                   [[{"text": "⬅ К списку", "callback_data": "cmd:cat"}]],
                                   message_id)
        group_name = ""
        if item.get("group_id"):
            row = self.db.one("SELECT name FROM nom_groups WHERE id=?",
                              (item.get("group_id"),))
            group_name = (row or {}).get("name") or ""
        lines = [f"🗂 {item.get('name') or 'Без названия'}",
                 f"{item.get('code') or ''}"
                 + (f" · {item.get('sku')}" if item.get("sku") else "")
                 + f" · {item.get('kind_label') or 'Товар'}"
                 + (f" · {group_name}" if group_name else "")]
        if num(item.get("price")):
            lines.append(f"Цена: {money(item.get('price'))}"
                         + (f" · с/с ~{money(item.get('cost'))}" if num(item.get("cost")) else ""))
        else:
            lines.append("Цена: не задана (в витрину не попадёт)")
        if str(item.get("kind") or "") != "showcase":
            lines.append(f"Остаток: {round(num(item.get('qty')), 1)} {item.get('unit') or 'шт'}"
                         + (f" · свободно {round(num(item.get('free')), 1)}" if num(item.get("reserved")) else ""))
        sold = num(item.get("sold_7"))
        lines.append(f"Продажи: {round(sold, 1)} шт за 7 дн"
                     + (f" · {round(num(item.get('sold_30')), 1)} за 30 дн" if num(item.get("sold_30")) else ""))
        who = gate(self.db, chat)
        if "finance" in who["allowed"]:
            margin = num(item.get("margin"))
            lines.append(f"Маржа: {money(margin)}"
                         + (f" ({round(num(item.get('margin_pct')), 1)}%)"
                            if num(item.get("price")) else "")
                         + (f" · {round(num(item.get('profit_per_hour')), 0)} ₽/ч"
                            if num(item.get("hours")) else ""))
        if num(item.get("grams")) or num(item.get("hours")):
            lines.append(f"Норматив: {round(num(item.get('grams')), 1)} г · "
                         f"{round(num(item.get('hours')), 2)} ч")
        vitrine_on = bool(self.db.setting("client_bot_catalog", True))
        if not vitrine_on:
            lines.append("🏪 Витрина бота выключена целиком (кнопка в списке каталога).")
        lines.append("Витрина: 👁 опубликован" if self._is_published(item)
                     else "Витрина: 🙈 скрыт от покупателей")
        if (item.get("client_bot_description") or "").strip():
            lines.append(f"Описание: {str(item.get('client_bot_description'))[:300]}")
        if num(item.get("archived")):
            lines.append("🗄 Позиция в архиве — покупателям не видна.")
        buttons: list[list[dict]] = []
        if "catalog" in who["allowed"]:
            if self._is_published(item):
                buttons.append([{"text": "🙈 Скрыть у покупателей",
                                 "callback_data": f"cmd:cat-hide:{item['id']}"}])
            else:
                buttons.append([{"text": "👁 Опубликовать в витрине",
                                 "callback_data": f"cmd:cat-show:{item['id']}"}])
            buttons.append([{"text": "↻ Пересчитать цену",
                             "callback_data": f"cmd:cat-recalc:{item['id']}"}])
            if num(item.get("archived")):
                buttons.append([{"text": "↩ Вернуть из архива",
                                 "callback_data": f"cmd:cat-restore:{item['id']}"}])
            else:
                buttons.append([{"text": "🗄 В архив",
                                 "callback_data": f"cmd:cat-archive:{item['id']}"}])
            buttons.append([
                {"text": "🗂 Группа", "callback_data": f"cmd:cat-grps:{item['id']}"},
                {"text": "🗑 Удалить", "callback_data": f"cmd:cat-del:{item['id']}"}])
        buttons.append([{"text": "⬅ К списку", "callback_data": "cmd:cat:all:0"}])
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def catalog_groups(self, chat: str, nom_id: str, message_id: str = "") -> None:
        """Выбор группы для позиции каталога кнопками."""
        groups = self._nom().groups()
        item = self.db.one("SELECT name FROM nomenclature WHERE id=?", (nom_id,)) or {}
        lines = [f"🗂 Группа для «{item.get('name') or 'позиции'}»:"]
        buttons = [[{"text": (g.get("name") or "без имени")[:48],
                     "callback_data": f"cmd:cat-grp:{g['id']}:{nom_id}"}]
                   for g in groups[:12]]
        buttons.append([{"text": "Без группы",
                         "callback_data": f"cmd:cat-grp:-:{nom_id}"}])
        buttons.append([{"text": "⬅ К карточке",
                         "callback_data": f"cmd:cati:{nom_id}"}])
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def _recalc_all_confirm(self, chat: str, message_id: str = "") -> None:
        """Массовый пересчёт цен — только через явное подтверждение."""
        count = len(self._catalog_rows("all"))
        return self._send_menu(
            chat, f"↻ Пересчитать цены всего каталога ({count} поз.) "
                  "от себестоимости?\nИзменённые цены запишутся в историю цен.",
            [[{"text": "✅ Да, пересчитать", "callback_data": "cmd:cat-recalc:allgo"}],
             [{"text": "⬅ Отмена", "callback_data": "cmd:cat:all:0"}]],
            message_id)

    def _catalog_groups_text(self) -> str:
        """Список групп каталога с количеством позиций."""
        groups = self._nom().groups()
        if not groups:
            return ("Групп каталога пока нет — они создаются в панели: "
                    "«Товары → Группы».")
        lines = ["🗂 Группы каталога:"]
        for g in groups:
            lines.append(f"• {g.get('name') or 'без имени'} — "
                         f"{int(num(g.get('items')))} поз.")
        return "\n".join(lines)

    # ------------------------------------------------------ кнопки каталога
    def cb_cat(self, chat: str, message_id: str, params: str) -> None:
        parts = params.split(":") if params else []
        filt = parts[0] if parts and parts[0] in ("all", "pub", "hid", "arc") \
            else self._cat_filter.get(chat, "all")
        try:
            page = int(num(parts[1], 0)) if len(parts) > 1 else self._cat_page.get(chat, 0)
        except (TypeError, ValueError):
            page = 0
        self.catalog_menu(chat, filt=filt, page=page, message_id=message_id)

    def cb_cat_item(self, chat: str, message_id: str, params: str) -> None:
        self.catalog_card(chat, params, message_id)

    def cb_cat_groups(self, chat: str, message_id: str, params: str) -> None:
        if params and params != "list":
            return self.catalog_groups(chat, params, message_id)
        self._send_menu(chat, self._catalog_groups_text(),
                        [[{"text": "⬅ К списку", "callback_data": "cmd:cat:all:0"}]],
                        message_id)

    def cb_cat_group(self, chat: str, message_id: str, params: str) -> None:
        group_id, _, nom_id = params.partition(":")
        self._reply(chat, self.do_catalog_group(group_id, nom_id))
        self.catalog_card(chat, nom_id, message_id)

    def cb_cat_delete(self, chat: str, message_id: str, params: str) -> None:
        """Экран подтверждения удаления (кнопкой, не только текстом)."""
        item = self.db.one("SELECT name FROM nomenclature WHERE id=?", (params,)) or {}
        self._send_menu(
            chat, f"🗑 Удалить «{item.get('name') or 'позицию'}»?\n"
                  "Позиция с движениями уйдёт в архив вместо удаления.",
            [[{"text": "✅ Да, удалить", "callback_data": f"cmd:cat-delyes:{params}"}],
             [{"text": "⬅ Отмена", "callback_data": f"cmd:cati:{params}"}]],
            message_id)

    def cb_cat_delete_yes(self, chat: str, message_id: str, params: str) -> None:
        self._reply(chat, self.do_catalog_delete(params))
        if params:
            return self.catalog_card(chat, params, message_id)
        self.catalog_menu(chat, message_id=message_id)

    def cb_cat_hide(self, chat: str, message_id: str, params: str) -> None:
        self._reply(chat, self.do_catalog_publish(params, False))
        if params:
            return self.catalog_card(chat, params, message_id)
        self.catalog_menu(chat, message_id=message_id)

    def cb_cat_show(self, chat: str, message_id: str, params: str) -> None:
        self._reply(chat, self.do_catalog_publish(params, True))
        if params:
            return self.catalog_card(chat, params, message_id)
        self.catalog_menu(chat, message_id=message_id)

    def cb_cat_archive(self, chat: str, message_id: str, params: str) -> None:
        self._reply(chat, self.do_catalog_archive(params, True))
        if params:
            return self.catalog_card(chat, params, message_id)
        self.catalog_menu(chat, message_id=message_id)

    def cb_cat_restore(self, chat: str, message_id: str, params: str) -> None:
        self._reply(chat, self.do_catalog_archive(params, False))
        if params:
            return self.catalog_card(chat, params, message_id)
        self.catalog_menu(chat, message_id=message_id)

    def cb_cat_recalc(self, chat: str, message_id: str, params: str) -> None:
        if params == "all":
            return self._recalc_all_confirm(chat, message_id)
        if params == "allgo":
            self._reply(chat, self.do_catalog_recalc_all())
            return self.catalog_menu(chat, message_id=message_id)
        self._reply(chat, self.do_catalog_recalc(params))
        if params:
            return self.catalog_card(chat, params, message_id)
        self.catalog_menu(chat, message_id=message_id)

    def cb_cat_vitrine(self, chat: str, message_id: str, params: str) -> None:
        enabled = not bool(self.db.setting("client_bot_catalog", True))
        self._reply(chat, self.do_catalog_vitrine(enabled))
        self.catalog_menu(chat, message_id=message_id)

    # ------------------------------------------------ текстовые команды
    def _catalog_text(self, chat: str, text: str) -> None:
        """«каталог» — меню; «каталог <слово>» — поиск по каталогу."""
        parts = text.split(maxsplit=1)
        self._cat_query[chat] = parts[1].strip().lower() if len(parts) > 1 else ""
        self._cat_filter[chat] = "all"
        return self.catalog_menu(chat, page=0)

    def _catalog_price_cmd(self, text: str) -> str:
        """«цена <товар> <сумма>» — новая базовая цена с записью в историю."""
        import re as _re
        words = text.split()
        if len(words) < 3:
            return "Формат: «цена <товар> <сумма>» — например «цена адресник 900»."
        m = _re.fullmatch(r"(\d+(?:[.,]\d+)?)(?:р|руб|руб\.|₽)?",
                          words[-1], _re.IGNORECASE)
        if not m:
            return "Не понял сумму. Формат: «цена <товар> <сумма>» — «цена адресник 900»."
        price = round(float(m.group(1).replace(",", ".")), 2)
        if price <= 0:
            return "Сумма должна быть больше нуля."
        item, err = self._resolve_name_tail(words[1:-1])
        if not item:
            return err or "Позиция не найдена."
        old = num(item.get("price"))
        self._nom().set_price(item["id"], price, note="цена из Telegram")
        self.db.add_event("nom", "Цена изменена из Telegram",
                          f"{item.get('name')}: {round(old)} → {round(price)}",
                          "", {"nom_id": item["id"]})
        return (f"💰 Цена «{item.get('name')}»: "
                + (f"{money(old)} → {money(price)}." if old else f"{money(price)}."))

    def _catalog_new_cmd(self, raw: str) -> str:
        """«товар <Название> <цена>» — создать позицию каталога.

        Название разбираем из исходного текста, а не из приведённого к нижнему
        регистру: витрина — лицо магазина, «адресник» там выглядит плохо.
        Новую позицию создаём скрытой от покупателей: витрину публикует
        человек, когда карточка готова, а не бот по умолчанию.
        """
        words = raw.strip().lstrip("/").strip().split()[1:]
        if not words:
            return ("Формат: «товар <Название> <цена>» — например "
                    "«товар Адресник для ключей 900р».")
        price = 0.0
        import re as _re
        m = _re.fullmatch(r"(\d+(?:[.,]\d+)?)(?:р|руб|руб\.|₽)?",
                          words[-1], _re.IGNORECASE)
        if m:
            price = round(float(m.group(1).replace(",", ".")), 2)
            words = words[:-1]
        name = " ".join(words).strip()
        if not name:
            return "Укажите название: «товар Адресник 900р»."
        if price < 0:
            return "Цена не может быть отрицательной."
        same = next((i for i in self._nom().items(include_archived=True)
                     if (i.get("name") or "").lower().replace("ё", "е")
                     == name.lower().replace("ё", "е")), None)
        if same:
            return (f"Позиция «{name}» уже есть в каталоге (код {same.get('code')}). "
                    f"Откройте её: «каталог {name.split()[0].lower()}».")
        item = self._nom().save({
            "name": name, "kind": "product", "client_bot_published": 0})
        if price > 0:
            self._nom().set_price(item["id"], price, note="создано в Telegram")
        self.db.add_event("nom", "Позиция создана из Telegram", name,
                          "", {"nom_id": item["id"]})
        return (f"✅ Создана позиция «{name}» (код {item.get('code')})"
                + (f" · цена {money(price)}" if price else " · без цены")
                + "\n🙈 Покупателям она пока не видна. Опубликуйте: "
                  f"«показать {name.split()[0].lower()}».")

    def _catalog_publish_cmd(self, chat: str, text: str) -> str:
        """«скрыть/показать <товар>» и «скрыть/показать витрину» (целиком)."""
        publishing = text.split()[0] == "показать"
        target = " ".join(text.split()[1:]).strip()
        if not target:
            return ("Формат: «скрыть <товар>» или «показать <товар>»; "
                    "для всей витрины: «скрыть витрину» / «показать витрину».")
        if target.startswith("витрин"):
            return self.do_catalog_vitrine(publishing)
        item, err = self._resolve_item(target, include_archived=True)
        if not item:
            return err
        return self.do_catalog_publish(item["id"], publishing)

    def do_catalog_publish(self, nom_id: str, published: bool) -> str:
        """Публикация/скрытие позиции в витрине клиентского бота."""
        # Нужна decorated-карточка: цена живёт в регистре prices, в сырой
        # строке nomenclature её нет — иначе проверка цены всегда проваливалась.
        item = self._nom().item(nom_id)
        if not item:
            return "Позиция не найдена."
        name = item.get("name") or "позиция"
        if published and num(item.get("price")) <= 0:
            return (f"У «{name}» нет цены — покупателям она всё равно не "
                    f"покажется. Сначала: «цена {name.split()[0].lower()} <сумма>».")
        if published and num(item.get("archived")):
            return (f"«{name}» в архиве — сначала верните: "
                    f"«вернуть {name.split()[0].lower()}».")
        self.db.execute(
            "UPDATE nomenclature SET client_bot_published=?, updated_at=? WHERE id=?",
            (1 if published else 0, now_iso(), nom_id))
        self.db.add_event("nom", "Позиция опубликована в витрине" if published
                          else "Позиция скрыта из витрины", name,
                          "", {"nom_id": nom_id})
        if published:
            return f"👁 «{name}» снова виден покупателям в клиентском боте."
        return f"🙈 «{name}» скрыт — покупатели его больше не видят."

    def do_catalog_vitrine(self, enabled: bool) -> str:
        """Витрина клиентского бота целиком: вкл/выкл (настройка панели)."""
        self.db.set_settings({"client_bot_catalog": bool(enabled)})
        published = self.db.one(
            "SELECT COUNT(*) n FROM nomenclature"
            " WHERE archived=0 AND kind='product' AND client_bot_published=1") or {}
        if enabled:
            return (f"🏪 Витрина клиентского бота включена. Опубликовано позиций: "
                    f"{int(num(published.get('n')))} (покупатели видят позиции с ценой).")
        return "🏪 Витрина клиентского бота выключена — каталог покупателям недоступен."

    def _catalog_desc_cmd(self, raw: str) -> str:
        """«описание <товар> <текст>» — текст в карточке покупателя.

        Без текста показывает текущее описание. Текст берём из исходного
        сообщения: покупатель читает его как написали, с заглавными буквами.
        """
        words = raw.strip().lstrip("/").strip().split()[1:]
        if not words:
            return ("Формат: «описание <товар> <текст>» — например "
                    "«описание адресник Держатель ключей на 6 крючков».")
        item, tail = self._resolve_name_tail(words)
        if not item:
            return (f"Позиция «{' '.join(words)}» в каталоге не найдена. "
                    f"Поиск: «каталог {words[0]}».")
        name = item.get("name") or "позиция"
        if not tail:
            current = (item.get("client_bot_description") or "").strip()
            return (f"Описание «{name}»: " + (current or "пока пустое.")
                    + "\nЗадать: «описание <товар> <текст>».")
        self.db.execute(
            "UPDATE nomenclature SET client_bot_description=?, updated_at=? WHERE id=?",
            (tail.strip()[:500], now_iso(), item["id"]))
        self.db.add_event("nom", "Описание витрины изменено из Telegram", name,
                          tail[:200], {"nom_id": item["id"]})
        return f"📝 Описание «{name}» сохранено — покупатель увидит его в карточке."

    def _catalog_norm_cmd(self, text: str) -> str:
        """«норматив <товар> <граммы> <часы>» — нормативы печати штуки."""
        words = text.split()
        if len(words) < 4:
            return "Формат: «норматив <товар> <граммы> <часы>» — «норматив адресник 25 1.5»."
        try:
            grams = float(words[-2].replace(",", "."))
            hours = float(words[-1].replace(",", "."))
        except ValueError:
            return "Не понял числа. Формат: «норматив <товар> <граммы> <часы>»."
        if grams < 0 or hours < 0:
            return "Нормативы не могут быть отрицательными."
        item, err = self._resolve_name_tail(words[1:-2])
        if not item:
            return err or "Позиция не найдена."
        self._nom().save({"id": item["id"], "name": item.get("name") or "",
                          "grams": grams, "hours": hours})
        fresh = self._nom().item(item["id"])
        cost = num((fresh or {}).get("cost"))
        return (f"📐 Норматив «{item.get('name')}»: {round(grams, 1)} г · "
                f"{round(hours, 2)} ч"
                + (f". Себестоимость ~{money(cost)}" if cost > 0 else "") + ".")

    def _catalog_minmax_cmd(self, text: str) -> str:
        """«минималка <товар> <мин> [макс]» — порог запаса и план пополнения."""
        words = text.split()
        if len(words) < 3:
            return ("Формат: «минималка <товар> <минимум> [максимум]» — "
                    "«минималка адресник 5 20».")
        numbers: list[float] = []
        while len(numbers) < 2 and len(words) > 1:
            try:
                numbers.insert(0, float(words[-1].replace(",", ".")))
                words = words[:-1]
            except ValueError:
                break
        if not numbers or not (0 < numbers[0]):
            return "Минимум должен быть больше нуля: «минималка <товар> 5»."
        item, err = self._resolve_name_tail(words[1:])
        if not item:
            return err or "Позиция не найдена."
        data = {"id": item["id"], "name": item.get("name") or "",
                "min_qty": round(numbers[0], 3)}
        tail = ""
        if len(numbers) > 1:
            data["max_qty"] = round(numbers[1], 3)
            tail = f", максимальный запас {round(numbers[1], 1)} шт"
        self._nom().save(data)
        return (f"⚖ «{item.get('name')}»: минимальный запас "
                f"{round(numbers[0], 1)} шт{tail}. Это влияет на план пополнения.")

    def _catalog_archive_cmd(self, text: str, archived: bool) -> str:
        """«архив <товар>» / «вернуть <товар>» — обратимое скрытие позиции."""
        target = " ".join(text.split()[1:]).strip()
        if not target:
            return ("Формат: «архив <товар>» спрячет позицию из работы, "
                    "«вернуть <товар>» — вернёт.")
        item, err = self._resolve_item(target, include_archived=True)
        if not item:
            return err
        return self.do_catalog_archive(item["id"], archived)

    def do_catalog_archive(self, nom_id: str, archived: bool) -> str:
        item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not item:
            return "Позиция не найдена."
        name = item.get("name") or "позиция"
        already = bool(num(item.get("archived")))
        if already == archived:
            return (f"«{name}» уже в архиве." if archived
                    else f"«{name}» уже активна.")
        self._nom().archive(nom_id, archived)
        self.db.add_event("nom", "Позиция убрана в архив (Telegram)" if archived
                          else "Позиция возвращена из архива (Telegram)", name,
                          "", {"nom_id": nom_id})
        if archived:
            return (f"🗄 «{name}» в архиве: позиция скрыта из каталога и витрины, "
                    "история продаж сохранена. Вернуть: «вернуть "
                    f"{name.split()[0].lower()}».")
        return f"↩ «{name}» снова активна в каталоге."

    def _catalog_delete_cmd(self, chat: str, text: str) -> str:
        """«удалить <товар>» — с подтверждением (случайное «удалить» дорого стоит)."""
        words = text.split()
        confirmed = len(words) > 2 and words[-1] == "да"
        query = " ".join(words[1:-1]) if confirmed else " ".join(words[1:])
        if not query:
            return ("Формат: «удалить <товар>» — спрошу подтверждение; "
                    "или сразу «удалить <товар> да».")
        item, err = self._resolve_item(query, include_archived=True)
        if not item:
            return err
        pending = self._pending_del.get(chat)
        if not confirmed and pending and pending[0] == item["id"] \
                and time.time() - pending[1] < 120:
            confirmed = True
        if not confirmed:
            self._pending_del[chat] = (item["id"], time.time())
            return (f"Точно удалить «{item.get('name')}»? Позиция с движениями "
                    "уйдёт в архив вместо удаления.\n"
                    f"Подтвердите: «удалить {query} да».")
        self._pending_del.pop(chat, None)
        return self.do_catalog_delete(item["id"])

    def do_catalog_delete(self, nom_id: str) -> str:
        item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not item:
            return "Позиция не найдена."
        name = item.get("name") or "позиция"
        self._nom().delete(nom_id)
        left = self.db.one("SELECT archived FROM nomenclature WHERE id=?", (nom_id,))
        if left is not None and num(left.get("archived")):
            self.db.add_event("nom", "Вместо удаления — архив (Telegram)", name,
                              "по позиции есть движения", {"nom_id": nom_id})
            return (f"🗄 У «{name}» есть движения по складу — удалять нельзя, "
                    "позиция переведена в архив.")
        self.db.add_event("nom", "Позиция удалена из Telegram", name,
                          "", {"nom_id": nom_id})
        return f"🗑 «{name}» удалена из каталога (движений по ней не было)."

    def _catalog_recalc_cmd(self, chat: str, text: str) -> str:
        """«пересчёт <товар|все>» — цены от себестоимости и нормы прибыли."""
        target = " ".join(text.split()[1:]).strip()
        if not target:
            return ("Формат: «пересчёт <товар>» — одна позиция, «пересчёт все» — "
                    "весь каталог (спрошу подтверждение).")
        if target in ("все", "всё", "all", "все да", "всё да"):
            pending = self._pending_recalc_all.get(chat, 0) if chat else 0
            if target.endswith("да") or time.time() - pending < 120:
                self._pending_recalc_all.pop(chat, None)
                return self.do_catalog_recalc_all()
            self._pending_recalc_all[chat] = time.time()
            count = len(self._catalog_rows("all"))
            return (f"Пересчитать цены всего каталога ({count} поз.) "
                    "от себестоимости? Напишите: «пересчёт все да».")
        item, err = self._resolve_item(target)
        if not item:
            return err
        return self.do_catalog_recalc(item["id"])

    def do_catalog_recalc(self, nom_id: str) -> str:
        try:
            result = self._nom().recalc_price(nom_id)
        except ValueError as exc:
            return f"Не получилось: {exc}"
        if not result.get("ok"):
            return "Пересчёт не выполнен."
        if result.get("reason"):
            return f"↻ Цены не менялись: {result['reason']}"
        changed = int(num(result.get("changed")))
        details = []
        for ptype, info in (result.get("prices") or {}).items():
            if info.get("changed"):
                details.append(f"{round(num(info.get('old')))} → "
                               f"{round(num(info.get('price')))}")
        if not changed:
            return "↻ Цены уже актуальны — изменений нет."
        return f"↻ Пересчитано типов цен: {changed} ({'; '.join(details[:4])})."

    def do_catalog_recalc_all(self) -> str:
        result = self._nom().recalc_prices()
        changed = int(num(result.get("changed")))
        if not changed:
            return "↻ Пересчёт завершён: цены уже актуальны."
        return f"↻ Пересчитано позиций: {changed}. Изменения — в истории цен."

    def do_catalog_group(self, group_id: str, nom_id: str) -> str:
        """Назначить позицию группу каталога (кнопками в карточке)."""
        item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not item:
            return "Позиция не найдена."
        if group_id == "-":
            self.db.execute(
                "UPDATE nomenclature SET group_id=NULL, updated_at=? WHERE id=?",
                (now_iso(), nom_id))
            return f"«{item.get('name')}» — без группы."
        group = self.db.one("SELECT * FROM nom_groups WHERE id=?", (group_id,))
        if not group:
            return "Группа не найдена."
        self.db.execute(
            "UPDATE nomenclature SET group_id=?, updated_at=? WHERE id=?",
            (group_id, now_iso(), nom_id))
        self.db.add_event("nom", "Группа изменена из Telegram",
                          f"{item.get('name')} → {group.get('name')}",
                          "", {"nom_id": nom_id})
        return f"«{item.get('name')}» → группа «{group.get('name')}»."

    # ---------------------------------------------------- текстовые команды
    def cmd_catalog(self, chat: str, raw: str, text: str) -> None:
        self._catalog_text(chat, text)

    def cmd_price(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._catalog_price_cmd(text))

    def cmd_publish(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._catalog_publish_cmd(chat, text))

    def cmd_desc(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._catalog_desc_cmd(raw))

    def cmd_item_new(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._catalog_new_cmd(raw))

    def cmd_norm(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._catalog_norm_cmd(text))

    def cmd_minmax(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._catalog_minmax_cmd(text))

    def cmd_archive(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._catalog_archive_cmd(text, True))

    def cmd_restore(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._catalog_archive_cmd(text, False))

    def cmd_delete(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._catalog_delete_cmd(chat, text))

    def cmd_recalc(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._catalog_recalc_cmd(chat, text))

    def cmd_groups(self, chat: str, raw: str, text: str) -> None:
        self._reply(chat, self._catalog_groups_text())
