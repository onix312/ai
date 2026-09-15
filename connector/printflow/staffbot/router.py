"""Маршруты бота сотрудников: таблицы команд вместо цепочек if/elif.

Было. `_dispatch` — 218 строк и 78 веток if/elif, где порядок решений
критичен (в коде были комментарии-извинения вида «ловим до общей команды
"продажа"»). `_handle_callback` — ещё 180 строк и 38 веток разбора
`callback_data` по префиксам. Добавить команду значило найти правильное
место в цепочке и не сломать соседние ветки.

Стало. Два плоских реестра — `TEXT_COMMANDS` и `CALLBACKS`. Каждая запись
декларативна: слова-синонимы, группа прав и имя метода-обработчика.
Права берутся из записи (единый источник правды), особые случаи задаются
функцией. Порядок не хрупкий: сначала фразы (от длинных к коротким),
потом первое слово — совпадение с «продажи стеллажа» больше не зависит от
того, где в цепочке стоит «продажа».

Словарь подсказок `suggest_command` остаётся здесь: непонятое сообщение
должно предлагать только те команды, что реально существуют в реестре.
"""
from __future__ import annotations

import difflib
import re as _re
from dataclasses import dataclass
from typing import Callable

from .ui import REPLY_ALIASES

# --------------------------------------------------------------- типы маршрутов

Group = str | Callable[[str, str], str]
# group(word, text) → имя группы прав (view/shelf/catalog/orders/finance/
# printers/staff/inbox) либо "" — команда без специальной группы.


@dataclass(frozen=True)
class Command:
    """Текстовая команда: слово/фраза → метод.

    ``phrase=True`` — маршрут срабатывает по началу сообщения
    («закрыть месяц …»), а не по первому слову. Такие маршруты
    проверяются раньше и от длинной фразы к короткой.
    """
    words: tuple[str, ...]
    group: Group
    method: str
    phrase: bool = False


@dataclass(frozen=True)
class Route:
    """Inline-кнопка: префикс callback-команды → метод.

    Префикс «sell-item» ловит и точное «sell-item», и «sell-item:<id>»;
    всё после двоеточия уходит обработчику параметром. ``kind``:
    * "handler" — обработчик сам решает, что показать; получает
                  (chat, message_id, params) — правит то же сообщение
                  или шлёт новое, как требует сценарий;
    * "text"    — обработчик возвращает строку; маршрутизатор отвечает ею
                  и обновляет исходное сообщение главным меню (команды
                  управления принтером — своей панелью управления).
    """
    prefix: str
    group: Group
    method: str
    kind: str = "handler"
    # late_answer: маршрутизатор не отвечает за нажатие заранее — обработчик
    # сам говорит谢谢 toast с текстом (отказ роли у «goto» обязан быть видим).
    late_answer: bool = False


def _digits_tail(text: str) -> bool:
    """Есть ли цифры после первого слова («статус 1001 печать»)."""
    return any(w.isdigit() for w in text.split()[1:])


def _status_group(word: str, text: str) -> str:
    """«статус» — обзор (view), «статус 1001 печать» — правка заказа (orders),
    «принтер» — управление парком (printers)."""
    if word in ("статус", "status"):
        return "orders" if _digits_tail(text) else "view"
    return "printers"


# --------------------------------------------------------------- подсказки

# Словарь «что человек мог иметь в виду» → команда, которая реально сработает.
# Используется, когда бот не узнал сообщение: вместо сухого отказа предлагаем
# ближайшую команду кнопкой (идея «человеческого» бота).
_SUGGEST_VOCAB: list[tuple[str, str]] = [
    ("панель", "панель"), ("главное меню", "панель"), ("меню", "панель"),
    ("статус", "статус"), ("состояние", "статус"),
    ("датчики", "датчики"), ("сенсоры", "датчики"), ("температура", "датчики"),
    ("доктор", "доктор"), ("диагностика", "доктор"), ("здоровье", "доктор"),
    ("план", "план"), ("печатать", "план"),
    ("очередь", "очередь"), ("задания", "очередь"), ("журнал", "очередь"),
    ("кадр", "кадр"), ("камера", "кадр"), ("снимок", "кадр"), ("фото", "кадр"),
    ("таймлапс", "таймлапс"), ("видео", "таймлапс"),
    ("живой", "живой"), ("live", "живой"),
    ("стеллаж", "стеллаж"), ("полка", "стеллаж"), ("склад", "стеллаж"),
    ("продажа", "продажа"), ("продать", "продажа"), ("selling", "продажа"),
    ("приход", "приход"), ("положить", "приход"), ("пополнить", "приход"),
    ("движения стеллажа", "движения стеллажа"), ("движения", "движения стеллажа"),
    ("движения полки", "движения стеллажа"), ("движение", "движения стеллажа"),
    ("продажи стеллажа", "продажи стеллажа"),
    ("продажи полки", "продажи стеллажа"), ("продажи полка", "продажи стеллажа"),
    ("касса", "касса"), ("выемка", "касса"), ("забрали", "забрали"),
    ("деньги", "деньги"), ("финансы", "деньги"), ("прибыль", "деньги"),
    ("сегодня", "сегодня"), ("итоги", "итоги недели"), ("неделя", "итоги недели"),
    ("месяц", "итоги месяца"), ("отчет", "итоги недели"), ("отчёт", "итоги недели"),
    ("долги", "долги"), ("должники", "долги"), ("брак", "брак"), ("дефект", "брак"),
    ("рейтинг", "рейтинг"), ("abc", "рейтинг"), ("топ", "рейтинг"),
    ("простой", "простой"), ("заработок", "деньги"), ("заработал", "деньги"),
    ("сколько", "сколько осталось"), ("сколько осталось", "сколько осталось"),
    ("филамент", "филамент"), ("пластик", "филамент"), ("катушки", "филамент"),
    ("закупка", "закупка"), ("покупки", "закупка"), ("шоппинг", "закупка"),
    ("каталог", "каталог"), ("номенклатура", "каталог"), ("товары", "каталог"),
    ("цена", "цена"), ("группы", "группы"),
    ("новый", "новый заказ"), ("заказ", "новый заказ"), ("создать", "новый заказ"),
    ("выдать", "выдать"), ("выдал", "выдать"), ("выдача", "выдать"),
    ("оплата", "оплата"), ("оплатить", "оплата"),
    ("чаты", "чаты"), ("диалоги", "чаты"), ("inbox", "чаты"),
    ("кответ", "кответ"), ("ответить", "кответ"),
    ("клиент-бот", "клиент-бот"), ("витрина", "клиент-бот"),
    ("пауза", "пауза"), ("продолжить", "продолжить"), ("свет", "свет"),
    ("стоп", "стоп"), ("поток", "поток"), ("пропустить", "пропустить"),
    ("повторить", "повторить"), ("перепечатать", "повторить"),
    ("следи", "следи"), ("подпишись", "следи"),
    ("помощь", "помощь"), ("help", "помощь"), ("что умеешь", "помощь"),
    ("код", "код"), ("команда", "команда"), ("сотрудники", "команда"),
    ("сотрудник", "сотрудник"), ("пригласить", "пригласить"),
    ("принтеры", "принтер"), ("принтер", "принтер"),
]


def suggest_command(raw: str) -> str:
    """Ближайшая известная команда для непонятого сообщения.

    Возвращает '' если совпадение слабое — тогда честно зовём «помощь»,
    а не угадываем наугад.
    """
    text = _re.sub(r"\s+", " ", str(raw or "").lower().replace("ё", "е")).strip()
    if len(text) < 3:
        return ""
    tokens = [w for w in text.split() if len(w) >= 3] or [text]
    best, best_score = "", 0.0
    for phrase, canonical in _SUGGEST_VOCAB:
        # Точное совпадение словами — самое сильное; «брак» не должно
        # находиться внутри «абракадабра», поэтому смотрим границы слова.
        pattern = _re.compile(rf"\b{_re.escape(phrase)}\b")
        if pattern.search(text):
            return canonical
        score = max(
            difflib.SequenceMatcher(None, text, phrase).ratio(),
            max((difflib.SequenceMatcher(None, token, phrase).ratio()
                 for token in tokens), default=0.0),
        )
        if score > best_score:
            best_score, best = score, canonical
    return best if best_score >= 0.62 else ""


# --------------------------------------------------------------- нормализация

# Эмодзи нижней reply-панели и разделители, которые не несут смысла.
_STRIP_EMOJI = "🖨📷≡₽⚑🛍🛒📦📊⚠❔▦▤·🗂"


def normalize(raw: str) -> str:
    """Сообщение → канонический текст для разбора.

    Нижний регистр, «ё»→«е», без слэша команд и эмодзи панели; кнопки
    нижней панели превращаются в свои команды. «стоп живой» склеивается
    в одно слово — это команда обзора (выключить дашборд), а не останов
    печати: раньше просить «стоп да» на неё было недоразумением.
    """
    text = str(raw or "").lower().lstrip("/").replace("ё", "е")
    text = REPLY_ALIASES.get(text.strip(), text)
    for emoji in _STRIP_EMOJI:
        text = text.replace(emoji, " ")
    text = _re.sub(r"\s+", " ", text).strip()
    if text.startswith("стоп") and "живой" in text:
        tail = _re.sub(r"\s*живой\s*", " ", text[4:])
        text = _re.sub(r"\s+", " ", f"стоп-живой {tail}").strip()
    return text


# --------------------------------------------------------------- реестры

# Текстовые команды. Порядок записей внутри не важен: фразы матчатся
# раньше слов (и от длинных к коротким), слова — точным первым словом.
TEXT_COMMANDS: tuple[Command, ...] = (
    # --- навигация и справка
    Command(("start", "help", "старт", "помощь", "меню", "?"), "view", "cmd_help"),
    Command(("more",), "view", "cmd_more"),
    Command(("панель", "panel", "дашборд"), "view", "cmd_panel"),
    # --- обзоры цеха
    Command(("план", "plan", "печатать"), "view", "cmd_plan"),
    Command(("очередь", "queue"), "view", "cmd_queue"),
    Command(("статус", "status", "принтер", "принтеры"), _status_group, "cmd_status"),
    Command(("датчики", "сенсоры", "ams", "амс"), "view", "cmd_sensors"),
    Command(("доктор", "диагностика"), "view", "cmd_doctor"),
    Command(("кадр", "камера", "фото", "photo", "cam"), "view", "cmd_frame"),
    Command(("таймлапс", "кадры", "гиф", "timelapse", "видео"), "view", "cmd_timelapse"),
    Command(("живой", "live"), "view", "cmd_live"),
    Command(("стоп-живой", "стопживой"), "view", "cmd_live_stop"),
    Command(("простой", "idle"), "view", "cmd_idle"),
    Command(("сколько", "что", "когда", "заработал", "заработано"), "view", "cmd_ask"),
    Command(("рейтинг", "топ", "abc", "изделия"), "view", "cmd_rating"),
    Command(("брак", "дефект", "defect", "дефекты"), "view", "cmd_defects"),
    Command(("хвосты", "хвост", "дыры", "проверка"), "view", "cmd_loose_ends"),
    # --- деньги
    Command(("деньги", "финансы", "money", "прибыль"), "finance", "cmd_money"),
    Command(("касса",), "finance", "cmd_cash"),
    Command(("забрали", "выемка"), "finance", "cmd_collect"),
    Command(("день", "сегодня", "итоги"), "finance", "cmd_today"),
    Command(("долги", "должники", "debt", "долг"), "finance", "cmd_debts"),
    # --- полка
    Command(("стеллаж", "полка", "витрина", "shelf"), "shelf", "cmd_shelf"),
    Command(("продажа", "продать", "sell"), "shelf", "cmd_sell"),
    Command(("приход", "положить", "пополнить"), "shelf", "cmd_produce"),
    Command(("движения",), "view", "cmd_shelf_moves"),
    # --- пластик
    Command(("филамент", "пластик", "катушки", "спул"), "view", "cmd_filament"),
    Command(("закупить", "закупка", "закупки", "шоппинг", "покупки"), "view", "cmd_shopping"),
    # --- каталог (номенклатура): просмотр — всем, правки — «catalog»
    Command(("каталог", "catalog", "номенклатура"), "view", "cmd_catalog"),
    Command(("цена",), "catalog", "cmd_price"),
    Command(("скрыть", "показать"), "catalog", "cmd_publish"),
    Command(("описание",), "catalog", "cmd_desc"),
    Command(("товар",), "catalog", "cmd_item_new"),
    Command(("норматив",), "catalog", "cmd_norm"),
    Command(("минималка",), "catalog", "cmd_minmax"),
    Command(("архив",), "catalog", "cmd_archive"),
    Command(("вернуть",), "catalog", "cmd_restore"),
    Command(("удалить",), "catalog", "cmd_delete"),
    Command(("пересчет", "пересчитать"), "catalog", "cmd_recalc"),
    Command(("группы", "группа"), "catalog", "cmd_groups"),
    # --- заказы и клиенты
    Command(("новый", "заказ", "создать"), "orders", "cmd_new_order"),
    Command(("выдать", "выдал", "выдан", "закрыть"), "orders", "cmd_fulfill"),
    Command(("готов", "ready"), "orders", "cmd_ready"),
    Command(("оплата", "оплатить", "payment"), "orders", "cmd_pay"),
    Command(("следи", "подпишись", "watch", "следить"), "view", "cmd_watch"),
    # --- inbox клиентского бота
    Command(("чаты", "диалоги", "inbox", "клиенты"), "inbox", "cmd_inbox"),
    Command(("кответ", "ответить", "creply"), "inbox", "cmd_client_answer"),
    Command(("клиент",), "inbox", "cmd_client"),
    Command(("клиент-бот", "кбот"), "staff", "cmd_client_bot"),
    # --- принтер
    Command(("пауза", "pause"), "printers", "cmd_pause"),
    Command(("продолжить", "resume", "старт-печати"), "printers", "cmd_resume"),
    Command(("свет", "light"), "printers", "cmd_light"),
    Command(("стоп", "stop"), "printers", "cmd_stop"),
    Command(("пропустить", "скип", "исключить", "skip"), "printers", "cmd_skip"),
    Command(("поток", "flow"), "printers", "cmd_flow"),
    Command(("повторить", "перепечатать", "reprint", "повтор"), "printers", "cmd_reprint"),
    Command(("выше", "ниже"), "printers", "cmd_reorder"),
    Command(("снял", "снято", "забрал"), "view", "cmd_removed"),
    # --- команда
    Command(("команда", "сотрудники", "team"), "staff", "cmd_team"),
    Command(("пригласить", "приглашение"), "staff", "cmd_invite"),
    Command(("сотрудник", "руководитель"), "staff", "cmd_add_member"),
    Command(("убрать", "уволить"), "staff", "cmd_remove_member"),
    # --- фразы: срабатывают по началу сообщения, проверяются раньше слов
    Command(("закрыть месяц",), "orders", "cmd_month_close", phrase=True),
    Command(("движения стеллаж",), "view", "cmd_shelf_moves", phrase=True),
    Command(("стеллаж движения",), "view", "cmd_shelf_moves", phrase=True),
    Command(("продажи стеллаж",), "shelf", "cmd_shelf_sales7", phrase=True),
    Command(("продажи за неделю",), "shelf", "cmd_shelf_sales7", phrase=True),
    Command(("продажи полки",), "shelf", "cmd_shelf_sales7", phrase=True),
    Command(("продажи полка",), "shelf", "cmd_shelf_sales7", phrase=True),
    Command(("оплата подтвердить",), "orders", "cmd_payment_confirm", phrase=True),
    Command(("подтвердить оплату",), "orders", "cmd_payment_confirm", phrase=True),
    Command(("оплата отклонить",), "orders", "cmd_payment_reject", phrase=True),
    Command(("отклонить оплату",), "orders", "cmd_payment_reject", phrase=True),
    Command(("отзыв ответ",), "inbox", "cmd_review_answer", phrase=True),
)

# Inline-кнопки (callback_data после «cmd:»). Префиксы уникальны, поиск —
# самое длинное совпадение, поэтому «shelf-cash-w» не путается с «shelf-cash».
CALLBACKS: tuple[Route, ...] = (
    # навигация
    Route("menu", "view", "cb_menu"),
    Route("more", "view", "cb_more"),
    Route("help", "view", "cb_help"),
    Route("goto", "view", "cb_goto", late_answer=True),
    Route("panel", "view", "cb_panel", kind="text"),
    Route("printers", "view", "cb_printers", kind="text"),
    Route("sensors", "view", "cb_sensors", kind="text"),
    Route("doctor", "view", "cb_doctor", kind="text"),
    Route("plan", "view", "cb_plan", kind="text"),
    Route("filament", "view", "cb_filament", kind="text"),
    Route("money", "finance", "cb_money", kind="text"),
    Route("today", "finance", "cb_today", kind="text"),
    Route("weekly", "finance", "cb_weekly", kind="text"),
    Route("inbox", "inbox", "cb_inbox", kind="text"),
    Route("team", "staff", "cb_team", kind="text"),
    Route("cbot_tpl", "inbox", "cb_client_template", kind="text"),
    # очередь и принтер
    Route("queue", "view", "cb_queue"),
    Route("pause", "printers", "cb_pause", kind="text"),
    Route("resume", "printers", "cb_resume", kind="text"),
    Route("light", "printers", "cb_light", kind="text"),
    Route("stop", "printers", "cb_stop", kind="text"),
    Route("frame", "view", "cb_frame", kind="text"),
    Route("next", "printers", "cb_next", kind="text"),
    Route("removed", "view", "cb_removed", kind="text"),
    Route("reprint", "printers", "cb_reprint", kind="text"),
    # полка и продажи
    Route("shelf-cash-menu", "shelf", "cb_shelf_cash"),
    Route("shelf-cash-reconcile", "shelf", "cb_cash_reconcile"),
    Route("shelf-cash-w", "shelf", "cb_shelf_collect"),
    Route("shelf-cash-undo", "shelf", "cb_cash_undo"),
    Route("shelf-cash", "shelf", "cb_shelf_cash"),
    Route("shelf-prod-menu", "shelf", "cb_shelf_prod_menu"),
    Route("shelf-prod", "shelf", "cb_shelf_produce", kind="text"),
    Route("shelf-sell", "shelf", "cb_shelf_sell", kind="text"),
    Route("sell", "shelf", "cb_sell_nom", kind="text"),
    Route("sell-home", "shelf", "cb_sell_home"),
    Route("sell-menu", "shelf", "cb_sell_menu"),
    Route("sell-item", "shelf", "cb_sell_item"),
    Route("sell-qty", "shelf", "cb_sell_qty"),
    Route("sell-price", "shelf", "cb_sell_price"),
    Route("sell-channel", "shelf", "cb_sell_channel"),
    Route("sell-confirm", "shelf", "cb_sell_confirm"),
    Route("sell-cancel", "shelf", "cb_sell_cancel"),
    Route("shelf-moves", "view", "cb_shelf_moves", kind="text"),
    Route("shelf-sales7", "view", "cb_shelf_sales7", kind="text"),
    Route("shelf-sales30", "view", "cb_shelf_sales30", kind="text"),
    Route("shelf", "view", "cb_shelf"),
    Route("shelf:needs", "view", "cb_shelf_needs", kind="text"),
    # заказы и клиенты
    Route("order-pay", "orders", "cb_order_pay"),
    Route("order-status", "orders", "cb_order_status"),
    Route("order-ready", "orders", "cb_order_ready"),
    Route("order-fulfill", "orders", "cb_order_fulfill"),
    Route("order", "orders", "cb_order"),
    Route("orders", "orders", "cb_orders"),
    Route("watch", "view", "cb_watch"),
    Route("client-reply", "inbox", "cb_client_reply"),
    Route("client", "inbox", "cb_client"),
    Route("clients", "inbox", "cb_clients"),
    # каталог
    Route("cat-delyes", "catalog", "cb_cat_delete_yes"),
    Route("cat-del", "catalog", "cb_cat_delete"),
    Route("cat-recalc", "catalog", "cb_cat_recalc"),
    Route("cat-vitrine", "catalog", "cb_cat_vitrine"),
    Route("cat-grps", "catalog", "cb_cat_groups"),
    Route("cat-grp", "catalog", "cb_cat_group"),
    Route("cat-hide", "catalog", "cb_cat_hide"),
    Route("cat-show", "catalog", "cb_cat_show"),
    Route("cat-archive", "catalog", "cb_cat_archive"),
    Route("cat-restore", "catalog", "cb_cat_restore"),
    Route("cati", "view", "cb_cat_item"),
    Route("cat", "view", "cb_cat"),
)


class Router:
    """Поиск маршрута по нормализованному тексту или callback-команде."""

    def __init__(self) -> None:
        self._by_word: dict[str, Command] = {}
        self._phrases: list[Command] = []
        for command in TEXT_COMMANDS:
            if command.phrase:
                self._phrases.append(command)
            else:
                for word in command.words:
                    self._by_word[word] = command
        # Длинные фразы проверяем первыми: «продажи стеллажа» важнее слова
        # «продажи» из другого маршрута.
        self._phrases.sort(key=lambda c: -len(c.words[0]))
        # Длинные префиксы поглощают короткие: «shelf-cash-w» перед «shelf-cash».
        self._callbacks: list[Route] = sorted(
            CALLBACKS, key=lambda r: -len(r.prefix))
        self._callback_groups = {r.prefix: r.group for r in CALLBACKS}

    # ---------------------------------------------------------------- текст
    def match_text(self, text: str) -> Command | None:
        """Маршрут для сообщения: фраза (от длинной к короткой) или слово.

        Фраза матчится по началу сообщения — «продажи стеллажа за месяц»
        это та же сводка продаж, что и «продажи стеллажа».
        """
        if not text:
            return None
        for command in self._phrases:
            if text.startswith(command.words[0]):
                return command
        word = text.split()[0]
        return self._by_word.get(word)

    def group_for(self, route: Command | Route, text: str) -> str:
        """Группа прав маршрута: константа или результат функции."""
        group = route.group
        if callable(group):
            return group(text.split()[0] if text else "", text)
        return group

    # ------------------------------------------------------------- кнопки
    def match_callback(self, command: str) -> tuple[Route, str] | None:
        """(маршрут, параметры) для callback-команды; None — неизвестная."""
        for route in self._callbacks:
            if command == route.prefix:
                return route, ""
            if command.startswith(route.prefix + ":"):
                return route, command[len(route.prefix) + 1:]
        return None

    def group_for_callback(self, command: str) -> str:
        """Группа прав для произвольной callback-команды (в т.ч. неизвестной).

        Неизвестные кнопки считаем обзорными: пусть обработчик честно
        ответит «не понял», а не молча проглотит нажатие.
        """
        for prefix in sorted(self._callback_groups, key=len, reverse=True):
            if command == prefix or command.startswith(prefix + ":"):
                group = self._callback_groups[prefix]
                return group(command.split(":", 1)[0], command) \
                    if callable(group) else group
        return "view"


ROUTER = Router()
