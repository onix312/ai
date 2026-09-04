"""Telegram-бот PrintFlow: принтер в кармане.

Работает на long polling — никаких белых IP и проброса портов не нужно.
Бот сам ходит к api.telegram.org и спрашивает: «есть новые сообщения?».
Отвечает только владельцу (chat_id из настроек), поэтому посторонний,
даже узнав имя бота, ничего не увидит и не нажмёт.

Команды намеренно на русском и без слэша тоже понимаются: «камера»,
«статус», «пауза» — с телефона так быстрее, чем искать латиницу.
"""
from __future__ import annotations

import difflib
import json
import re as _re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from . import APP_VERSION
from .accounting import num, uid
from .config import now_iso
from .db import SCHEMA_VERSION, list_backups
from .staff import ROLE_NAMES, Staff, gate, group_for_word

API = "https://api.telegram.org/bot{token}/{method}"

HELP = f"""PrintFlow {APP_VERSION} — цех в кармане.

Одно слово (без слэша, в любом регистре) — и я отвечу:

👀 Посмотреть
• статус · кадр · очередь · датчики · доктор · план
• таймлапс · живой — картинки с принтера

🛍 Полка (доступно сотруднику)
• стеллаж — остатки и дефицит одним взглядом
• продажа — меню «−1 шт» по цене ценника
• приход — меню «+1 шт» (или «приход Адресник 5»)
• движения · продажи стеллажа · касса
• «забрали 5000» — записать выемку из кассы магазина

💰 Деньги (руководитель+)
• деньги · сегодня · итоги недели · итоги месяца
• долги · брак · рейтинг · простой

📦 Заказы (руководитель+)
• «новый Адресник 2шт 900р Мария» — заказ из текста
• выдать 1001 · оплата 1500 по 1001 · статус 1001 печать
• следи 1001 — прогресс каждые 10% · фото — снимок к заказу

🖨 Печать (руководитель+)
• пауза · продолжить · свет · стоп · поток 90
• пропустить 2 · выше 1001 · ниже 1001 · повторить

🗂 Каталог (руководитель+)
• каталог · цена Адресник 500 · товар Название цена
• пересчёт все · архив Адресник · группы · описание

👥 Команда
• код — свой chat_id · команда — список команды
• пригласить сотрудник Имя · убрать 123456

Роли: сотрудник — обзоры и полка; руководитель — ещё деньги,
заказы, печать и каталог; владелец — всё."""


STATE_RU = {
    "RUNNING": "печатает", "IDLE": "свободен", "PAUSE": "на паузе",
    "PAUSED": "на паузе", "FINISH": "печать завершена", "PREPARE": "готовится",
    "FAILED": "ошибка", "OFFLINE": "не в сети", "UNKNOWN": "нет данных",
}


def _hm(minutes: float) -> str:
    """Минуты → «2 ч 15 мин»: так читается быстрее, чем «135 мин»."""
    total = int(max(0, num(minutes)))
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


def _money(value: float) -> str:
    return f"{round(num(value)):,}".replace(",", " ") + " ₽"


def _keyboard(*rows: list[tuple[str, str]]) -> dict:
    """Inline-клавиатура: [[(текст, callback_data)], ...]."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


# ------------------------------------------------------------- подсказки
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


class TelegramBot:
    """Фоновый слушатель команд. Запускается только когда включён в настройках."""

    def __init__(self, manager):
        self.manager = manager
        self.db = manager.db
        self._stop = threading.Event()
        try:
            self._offset = max(0, int(num(self.db.setting("telegram_bot_update_offset", 0))))
        except (TypeError, ValueError):
            self._offset = 0
        self._current_update_id = ""
        self.last_poll = 0.0  # время успешного опроса (сердцебиение, идея 36)
        self._pending_stop: dict[str, float] = {}
        self._live: dict[str, dict] = {}  # chat -> {message_id, text} живого дашборда
        self._printer_choice: dict[str, str] = {}  # chat -> printer_id
        self._watched: dict[str, dict] = {}  # chat -> {number, last_milestone}
        self._sell_page: dict[str, int] = {}  # chat -> страница меню продаж
        self._prod_page: dict[str, int] = {}  # chat -> страница меню прихода
        self._cat_page: dict[str, int] = {}   # chat -> страница меню каталога
        self._cat_filter: dict[str, str] = {}  # chat -> фильтр списка каталога
        self._cat_query: dict[str, str] = {}   # chat -> поисковый запрос каталога
        self._pending_del: dict[str, tuple] = {}  # chat -> (nom_id, время запроса)
        self._pending_recalc_all: dict[str, float] = {}  # chat -> время запроса
        # 14.0 (идеи 73, 74): транспорт, журнал update и очередь исходящих —
        # общие с клиентским ботом (модуль tg), а не своя копия в каждом боте.
        from .tg import Outbox, Transport, UpdateLedger
        self.transport = Transport(
            lambda: str(self._settings().get("telegram_token", "") or ""), "staff")
        self.ledger = UpdateLedger(self.db, "telegram_bot_updates")
        self.outbox = Outbox(
            self.db,
            sender=lambda method, payload, timeout=15: self._call(
                method, payload, timeout=timeout),
            table="telegram_outbox",
            token_provider=self.transport.token_provider)
        self._thread = threading.Thread(target=self._loop, name="pf-bot", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- транспорт
    def shutdown(self) -> None:
        self._stop.set()

    def _settings(self) -> dict:
        return self.db.settings(include_secrets=True)

    def _call(self, method: str, params: dict, timeout: int = 35) -> dict:
        """Вызов Bot API через общий транспорт (идея 73)."""
        return self.transport.call(method, params, timeout=timeout)

    def _claim_update(self, update: dict) -> bool | None:
        """Занять update рабочего бота в общем журнале (идея 73)."""
        return self.ledger.claim(update)

    def _finish_update(self, update_id: str, ok: bool = True, error: str = "") -> None:
        """Отметить update обработанным или ошибкой."""
        self.ledger.finish(str(update_id or ""), ok, error)

    def _reply(self, chat: str, text: str) -> None:
        """Ответ сотруднику через очередь исходящих (идея 74).

        Сообщение кладётся в `telegram_outbox` и сразу отправляется; при
        обрыве сети строка остаётся в очереди и уйдёт на следующем витке
        опроса — раньше ответ терялся молча.
        """
        payload = {"chat_id": chat, "text": str(text or "")[:3800],
                   "disable_web_page_preview": "true"}
        row = self.outbox.add(str(chat), "sendMessage", payload,
                              dedupe_key=f"reply:{chat}:{hash(payload['text'])}")
        self.outbox.send(row)

    def _reply_photo(self, chat: str, path, caption: str = "") -> None:
        """Фото сотруднику через ту же очередь."""
        payload = {"chat_id": chat, "caption": str(caption or "")[:1024]}
        row = self.outbox.add(str(chat), "sendPhoto", payload, file_path=str(path))
        self.outbox.send(row)

    def _inline_menu(self) -> dict:
        """Главное inline-меню сотрудника, общее для всех карточек."""
        return _keyboard(
            [("🖨 Принтеры", "cmd:printers"), ("≡ Очередь", "cmd:queue")],
            [("🛍 Стеллаж", "cmd:shelf"), ("📥 Inbox", "cmd:inbox")],
            [("🧵 AMS / пластик", "cmd:filament"), ("💰 Касса", "cmd:shelf-cash")],
            [("⚑ План", "cmd:plan"), ("₽ Деньги", "cmd:money")],
            [("🗂 Каталог", "cmd:cat"), ("📊 Итоги", "cmd:today")],
            [("🩺 Доктор", "cmd:doctor"), ("❔ Помощь", "cmd:help")],
        )

    def _reply_keyboard(self, chat: str, text: str) -> None:
        """Сообщение с постоянной inline-клавиатурой."""
        keyboard = self._inline_menu()
        self._call("sendMessage", {
            "chat_id": chat, "text": text[:3800], "disable_web_page_preview": "true",
            "reply_markup": json.dumps(keyboard, ensure_ascii=False),
        }, timeout=15)

    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = self._settings()
            if not (settings.get("telegram_enabled") and settings.get("telegram_bot")
                    and settings.get("telegram_token") and settings.get("telegram_chat_id")):
                self._stop.wait(20)          # бот выключен — просто ждём
                continue
            try:
                result = self._call("getUpdates", {
                    "offset": self._offset, "timeout": 25,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                })
                if result.get("ok"):
                    self.last_poll = time.time()
                    self.outbox.drain(batch=10)  # дозаправка недосланных (идея 74)
                for update in (result.get("result") or []):
                    update_id = str(update.get("update_id") or "")
                    claim = self._claim_update(update)
                    if claim is False:
                        # Не подтверждаем более новые update поверх живой
                        # обработки: порядок Telegram должен сохраниться.
                        break
                    try:
                        previous = self._current_update_id
                        self._current_update_id = update_id
                        if claim is not None:
                            self._handle(update, str(settings.get("telegram_chat_id")))
                            self._finish_update(update_id, True)
                        self._current_update_id = previous
                        try:
                            next_offset = int(num(update_id)) + 1
                        except (TypeError, ValueError):
                            next_offset = self._offset
                        self._offset = max(self._offset, next_offset)
                        self.db.set_settings({"telegram_bot_update_offset": self._offset})
                    except Exception as exc:
                        self._current_update_id = ""
                        self._finish_update(update_id, False, str(exc))
                        # Не подтверждаем offset: следующий polling повторит
                        # failed update после безопасного reclaim.
                        continue
                self._maybe_digest(settings)
                self._maybe_weekly(settings)
                self._maybe_live()
                self._maybe_watch()
            except Exception:
                self._stop.wait(10)

    # ------------------------------------------------- расписания рассылок
    def _maybe_digest(self, settings: dict) -> None:
        """Утренний дайджест в digest_time, раз в сутки."""
        digest_time = str(settings.get("digest_time") or "09:00")
        now = datetime.now()
        if now.strftime("%H:%M") != digest_time:
            return
        last = str(settings.get("digest_last") or "")
        today = now.strftime("%Y-%m-%d")
        if last == today:
            return
        self.db.set_settings({"digest_last": today})
        chat = str(settings.get("telegram_chat_id") or "")
        if chat:
            self.manager.notify_async(self.text_digest())

    def _maybe_weekly(self, settings: dict) -> None:
        """Еженедельный отчёт: день недели (1=пн) и время."""
        day = int(num(settings.get("weekly_report_day", 1), 1))
        at = str(settings.get("weekly_report_time") or "20:00")
        now = datetime.now()
        if now.isoweekday() != day or now.strftime("%H:%M") != at:
            return
        key = f"{now.isocalendar().year}-W{now.isocalendar().week}"
        if str(settings.get("weekly_last") or "") == key:
            return
        self.db.set_settings({"weekly_last": key})
        chat = str(settings.get("telegram_chat_id") or "")
        if chat:
            self.manager.notify_async(self.text_weekly())

    # -------------------------------------------------------------- разбор
    def _handle(self, update: dict, owner: str) -> None:
        # Кнопки управления: «пауза», «продолжить», «свет», «стоп», «кадр»
        callback = update.get("callback_query") or {}
        if callback:
            return self._handle_callback(callback, owner)
        message = update.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        if not chat:
            return
        text = (message.get("text") or "").strip()
        who = gate(self.db, chat)
        if not who["role"]:
            # Посторонний чат: единственное, что можно — показать свой chat_id
            # (чтобы владелец добавил в команду) или войти по коду приглашения.
            lowered = text.lower().lstrip("/").replace("ё", "е").strip()
            invite = _re.match(r"(?:старт|start|код|code)?\s*(pf-[a-z0-9]{4,12})$",
                               lowered)
            if invite:
                profile = message.get("from") or {}
                try:
                    member = Staff(self.db).use_invite(
                        invite.group(1), chat,
                        str(profile.get("first_name") or ""),
                        str(profile.get("id") or ""))
                    role = member.get("role_name") or "сотрудник"
                    self._reply(chat,
                                f"Добро пожаловать, {member.get('name')}! "
                                f"Вы в команде NOZZA как {role}.\n"
                                "Права: " + Staff(self.db).rights_text(member.get("role"))
                                + "\n\nНапишите «помощь» — покажу команды.")
                    self.db.add_event("bot", "Новый участник по приглашению",
                                      f"{member.get('name')} — {role}", "", {})
                    return
                except ValueError as exc:
                    return self._reply(chat, str(exc))
            if lowered in ("код", "code", "мой код", "id"):
                return self._reply(
                    chat, f"Ваш chat_id: {chat}\nПередайте его владельцу — "
                          "он добавит вас в команду в настройках панели или "
                          "пришлёт код приглашения.")
            self._reply(chat,
                        "Этот бот — рабочий инструмент команды NOZZA.\n"
                        f"Ваш chat_id: {chat} — попросите владельца добавить вас.\n"
                        "Если у вас есть код приглашения, напишите: старт КОД")
            self.db.add_event("bot", "Посторонний в Telegram-боте",
                              f"chat_id {chat}: {text[:80]}", "", {})
            return
        # Фото без подписи: прикрепляем к последнему активному заказу.
        photo = message.get("photo")
        text = (message.get("text") or "").strip()
        caption = (message.get("caption") or "").strip()
        if photo and not text:
            try:
                return self._attach_photo(chat, photo, caption)
            except Exception as exc:
                return self._reply(chat, f"Не получилось: {exc}")
        if not text:
            return
        try:
            self._dispatch(chat, text)
        except Exception as exc:
            self._reply(chat, f"Не получилось: {exc}")

    def _handle_callback(self, callback: dict, owner: str) -> None:
        """Нажатие inline-кнопки. Отвечаем команде, чужому — отказ."""
        message = callback.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id") or "")
        if not chat or not data:
            return
        who = gate(self.db, chat)
        if not who["role"]:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id,
                                               "text": "Этот бот приватный."})
            return
        command = data.replace("cmd:", "", 1)
        group = group_for_word("", command=command)
        if group not in who["allowed"]:
            self._call("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": f"Недоступно для роли «{ROLE_NAMES.get(who['role'])}»"})
            return
        # Меню стеллажа и продаж отправляют новое сообщение с актуальными
        # остатками и кнопками; исходное не превращаем в неактуальный чек.
        # Листание страниц («sell-menu:next» и т.п.) правит то же сообщение.
        if command == "shelf" or command.startswith("sell-menu") \
                or command.startswith("shelf-prod-menu"):
            self._call("answerCallbackQuery", {"callback_query_id": callback_id})
            message_id = str(message.get("message_id") or "")
            if command == "shelf":
                return self.shelf_keyboard(chat)
            if command.startswith("shelf-prod-menu"):
                page = self._page_from_command(
                    command, "shelf-prod-menu", self._prod_page, chat)
                return self.shelf_produce_keyboard(chat, page, message_id=message_id)
            page = self._page_from_command(command, "sell-menu", self._sell_page, chat)
            return self.sell_keyboard(chat, page, message_id=message_id)
        # Касса стеллажа и быстрые выемки: правим то же сообщение кнопками.
        if command in ("shelf-cash", "shelf-cash-menu") \
                or command.startswith("shelf-cash-w:"):
            self._call("answerCallbackQuery", {"callback_query_id": callback_id})
            message_id = str(message.get("message_id") or "")
            if command.startswith("shelf-cash-w:"):
                self.do_shelf_collect(command.split(":", 1)[1])
            return self.shelf_cash_keyboard(chat, message_id=message_id)
        # Подсказка-кнопка: выполнить исправленную команду как обычный текст.
        # Права проверяем по целевой команде так же, как в текстовом пути.
        if command.startswith("goto:"):
            target = command[5:].strip()
            if not target:
                self._call("answerCallbackQuery",
                           {"callback_query_id": callback_id, "text": "Команда пуста"})
                return
            tword = target.split()[0]
            tdigits = any(w.isdigit() for w in target.split()[1:])
            tgroup = group_for_word(tword, text_has_digits=tdigits)
            if tgroup and tgroup not in who["allowed"]:
                self._call("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": f"Недоступно для роли «{ROLE_NAMES.get(who['role'])}»"})
                return
            self._call("answerCallbackQuery", {"callback_query_id": callback_id})
            return self._dispatch(chat, target)
        # Меню каталога и карточки позиций правят то же сообщение, как стеллаж.
        if command == "cat" or command.startswith(("cati", "cat-hide", "cat-show",
                                                   "cat-archive", "cat-restore",
                                                   "cat-recalc", "cat-del",
                                                   "cat-grp", "cat-vitrine")):
            self._call("answerCallbackQuery", {"callback_query_id": callback_id})
            message_id = str(message.get("message_id") or "")
            return self._catalog_callback(command, chat, message_id)
        text = self._run_command(command, chat)
        self._call("answerCallbackQuery", {"callback_query_id": callback_id})
        # Управление печатью обновляем в том же сообщении; продажа и панель — новым.
        control = command in ("pause", "resume", "light", "frame", "stop")
        if control:
            try:
                self._call("editMessageText", {"chat_id": chat,
                                                "message_id": str(message.get("message_id")),
                                                "text": text[:3800],
                                                "reply_markup": json.dumps(_keyboard(
                                                    [("❙❙ Пауза", "cmd:pause"), ("▶ Продолжить", "cmd:resume")],
                                                    [("☀ Свет", "cmd:light"), ("◉ Кадр", "cmd:frame")],
                                                    [("■ Стоп", "cmd:stop")]))})
                return
            except Exception:
                pass
        self._edit_or_reply(chat, message, text, self._inline_menu())

    def _edit_or_reply(self, chat: str, message: dict, text: str,
                       buttons: dict | None = None) -> None:
        """Редактировать inline-карточку вместо нового сообщения.

        Новое сообщение используется только если Telegram уже не позволяет
        изменить старое или callback пришёл без message_id.
        """
        message_id = str(message.get("message_id") or "")
        if not message_id:
            return self._reply(chat, text, buttons)
        params = {"chat_id": chat, "message_id": message_id, "text": text[:3800]}
        if buttons:
            params["reply_markup"] = json.dumps(buttons, ensure_ascii=False)
        result = self._call("editMessageText", params)
        if not result.get("ok"):
            self._reply(chat, text, buttons)

    def _run_command(self, command: str, chat: str = "") -> str:
        """Выполнить команду (текстовую или inline-кнопки) и вернуть ответ."""
        if command in ("panel", "printers"):
            return self.text_panel() if command == "panel" else self._list_printers(chat)
        if command == "queue":
            return self.text_queue()
        if command == "sensors":
            return self.text_sensors()
        if command == "doctor":
            return self.text_doctor()
        if command == "inbox":
            return self._client_inbox()
        if command.startswith("cbot_tpl:"):
            return self._client_template_button(command)
        if command in ("plan", "filament", "money", "today", "weekly", "help"):
            return {"plan": self.text_plan, "filament": self.text_filament,
                    "money": self.text_money, "today": self.text_today,
                    "weekly": self.text_weekly,
                    "help": lambda: HELP}[command]()
        if command == "pause":
            return self.do_command("pause", "Печать поставлена на паузу", chat=chat)
        if command == "resume":
            return self.do_command("resume", "Печать продолжена", chat=chat)
        if command == "light":
            return self.do_command("light", "Подсветка переключена", chat=chat)
        if command == "stop":
            return self.do_stop(chat or "", "стоп" if chat else "стоп да")
        if command == "frame" or command == "кадр":
            if chat:
                self.send_frame(chat)
                return "Кадр отправлен."
            return "Кадр недоступен"
        if command == "next":
            return self._start_next(chat)
        if command == "removed":
            printer = self._pick_printer(chat)
            result = self.manager.part_removed(printer.id if printer else "")
            return f"✅ Деталь снята. Простой после печати {result.get('idle_min', 0)} мин."
        if command == "reprint":
            try:
                row = self.manager.reprint_last_failed(
                    confirmed=True, request_id=uid("tg-reprint")
                )
                return f"↻ Повтор «{row.get('name')}» подготовлен. Запуск подтвердите отдельно."
            except Exception as exc:
                return f"Не получилось подготовить повтор: {exc}"
        if command == "panel":
            return self.text_panel()
        if command == "plan":
            return self.text_plan()
        if command == "shelf:needs":
            return self.text_shelf(only_needs=True)
        if command.startswith("sell:"):
            return self.do_sell(command.split(":", 1)[1])
        if command.startswith("shelf-sell:"):
            return self.do_shelf_sell(command.split(":", 1)[1])
        if command.startswith("shelf-prod:"):
            return self.do_shelf_produce(command.split(":", 1)[1])
        if command == "shelf-cash":
            return self.text_shelf_cash()
        if command.startswith("shelf-cash-w:"):
            return self.do_shelf_collect(command.split(":", 1)[1])
        if command == "shelf-moves":
            return self.text_shelf_moves()
        if command == "shelf-sales7":
            return self.text_shelf_sales(7)
        if command == "shelf-sales30":
            return self.text_shelf_sales(30)
        return "Не понял команду."

    # -------------------------------------------------------------- команда
    def _staff_command(self, chat: str, action: str, text: str,
                       role: str = "") -> str:
        """Управление командой из чата: список, добавить, пригласить, убрать.

        Владелец делает всё; руководитель может смотреть список и приглашать
        сотрудников (но не руководителей и не убирать людей).
        """
        who = gate(self.db, chat)
        staff = Staff(self.db)
        if not who["role"]:
            return "Недоступно."
        is_owner = who["role"] == "owner"
        if action == "list":
            if not is_owner and who["role"] != "manager":
                return "Список команды — для владельца и руководителя."
            return staff.text_list()
        words = str(text).split()
        if action == "invite":
            if not (is_owner or who["role"] == "manager"):
                return "Приглашать может владелец или руководитель."
            role = "employee"
            name = ""
            for w in words[1:]:
                lowered = w.lower().replace("ё", "е")
                if lowered in ("сотрудник", "руководитель", "менеджер"):
                    role = "manager" if lowered == "руководитель" else "employee"
                else:
                    name += (" " if name else "") + w
            if role == "manager" and not is_owner:
                return "Руководителя может пригласить только владелец."
            code = staff.invite(role, name, created_by=str(chat))
            return (f"Код приглашения: {code.get('code')}\n"
                    f"Роль: {code.get('role_name')}"
                    + (f", имя: {name}" if name else "") +
                    "\nОтправьте код человеку — он пишет боту «старт "
                    f"{code.get('code')}» и попадает в команду. Код одноразовый.")
        if action == "add":
            if not is_owner:
                return "Добавлять участников может только владелец."
            digits = next((w for w in words if w.lstrip("-").isdigit()), "")
            name_words = [w for w in words[1:]
                          if not w.lstrip("-").isdigit()
                          and w.lower() != role]
            if not digits or not name_words:
                return ("Формат: «сотрудник Имя 123456» или "
                        "«руководитель Имя 123456».\n"
                        "chat_id человек узнает у бота командой «код».")
            try:
                member = staff.add(" ".join(name_words), role, digits)
            except ValueError as exc:
                return str(exc)
            return (f"✅ {member.get('name')} — {member.get('role_name')} "
                    f"(chat_id {member.get('chat_id')}).\n"
                    "Права: " + staff.rights_text(member.get("role")))
        if action == "remove":
            if not is_owner:
                return "Убирать участников может только владелец."
            ident = next((w for w in words[1:] if w), "")
            if not ident:
                return "Формат: «убрать 123456» (chat_id или имя)."
            try:
                row = staff.remove(ident)
            except ValueError as exc:
                return str(exc)
            return (f"✅ {row.get('name')} отключён от бота "
                    "(в панели можно вернуть).")
        return "Не понял команду команды 🙂 Напишите «команда»."

    def _start_next(self, chat: str) -> str:
        """Запустить следующее задание очереди на свободном принтере."""
        printer = self._pick_printer(chat)
        if not printer:
            return "Принтеры не добавлены."
        if not printer.connected:
            return f"{printer.record.get('name', 'Принтер')} не на связи."
        snap = printer.snapshot()
        if snap["printer"]["state"] not in ("IDLE", "FINISH"):
            return "Принтер занят — сначала завершите или остановите печать."
        job = self.manager.next_job(printer.id, snap)
        if not job:
            return "Очередь пуста — запускать нечего."
        try:
            self.manager.start_job(job["id"], printer.id)
            return f"▶ Запускаю «{job.get('name')}»."
        except Exception as exc:
            return f"Не удалось запустить: {exc}"

    def _reorder_queue(self, text: str, direction: str) -> str:
        """«выше 1001» / «ниже 1001» — передвинуть задание в очереди печати."""
        number = next((w for w in text.split()[1:] if w.isdigit()), "")
        if not number:
            return f"Формат: «{direction} 1001» — передвинуть задание заказа №1001 в очереди."
        jobs = self.db.query(
            "SELECT j.*, o.number AS order_number FROM print_jobs j"
            " LEFT JOIN orders o ON o.id=j.order_id"
            " WHERE j.state='queued'"
            " ORDER BY j.priority DESC, datetime(j.created_at)")
        if len(jobs) < 2:
            return "В очереди меньше двух заданий — двигать нечего."
        index = next((i for i, j in enumerate(jobs)
                      if str(j.get("order_number") or "") == number), None)
        if index is None:
            return f"Заказ №{number} не стоит в очереди."
        neighbor_index = index - 1 if direction == "выше" else index + 1
        if neighbor_index < 0:
            return "Задание уже первое в очереди."
        if neighbor_index >= len(jobs):
            return "Задание уже последнее в очереди."
        target, neighbor = jobs[index], jobs[neighbor_index]
        step = 1 if direction == "выше" else -1
        new_priority = int(num(neighbor.get("priority"))) + step
        self.db.execute("UPDATE print_jobs SET priority=? WHERE id=?",
                        (new_priority, target["id"]))
        self.db.add_event("queue", "Очередь: задание передвинуто",
                          f"{target.get('name') or ''} {direction}",
                          "", {"job_id": target["id"], "direction": direction})
        return (f"Задание заказа №{number} «{target.get('name') or ''}» "
                f"передвинуто {direction}.")

    def _watch_order(self, chat: str, number: str) -> str:
        """«следи 1001» — уведомления о прогрессе заказа каждые 10%."""
        if not number:
            return "Формат: «следи 1001» — пришлю прогресс каждые 10%."
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        self._watched[chat] = {"number": number, "last_milestone": 0}
        return f"👁 Слежу за заказом №{number}. Прогресс буду присылать каждые 10%."

    def _maybe_watch(self) -> None:
        """Разослать уведомления о прогрессе заказов, за которыми следят."""
        if not self._watched:
            return
        for chat, sub in list(self._watched.items()):
            number = sub.get("number", "")
            order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
            if not order:
                self._watched.pop(chat, None)
                self._reply(chat, f"Заказ №{number} не найден — слежка снята.")
                continue
            job = self.db.one(
                "SELECT * FROM print_jobs WHERE order_id=? AND state='running' LIMIT 1",
                (order["id"],))
            if not job:
                continue
            progress = int(num(job.get("progress") or 0))
            milestone = progress // 10 * 10
            if milestone <= sub.get("last_milestone", 0):
                continue
            sub["last_milestone"] = milestone
            # Прогресс уходит в тот же чат, из которого попросили следить, —
            # раньше уведомление шло через manager.notify_async в чат по умолчанию,
            # и слежка из другого чата молчала.
            self._reply(chat,
                        f"PrintFlow · заказ №{number}\n"
                        f"Прогресс {milestone}% — {order.get('product') or ''}")
            # По завершении снимаем слежку.
            if progress >= 100:
                self._watched.pop(chat, None)

    def _attach_photo(self, chat: str, photo: list, caption: str = "") -> None:
        """Прислал фото — прикрепляем к заказу (по подписи или последнему активному)."""
        order_id = ""
        caption = (caption or "").strip()
        number = next((w for w in caption.split() if w.isdigit()), "")
        if number:
            order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
            order_id = order["id"] if order else ""
        if not order_id:
            order = self.db.one(
                "SELECT * FROM orders WHERE status NOT IN"
                " (SELECT id FROM statuses WHERE is_final=1)"
                " ORDER BY datetime(updated_at) DESC LIMIT 1")
            if not order:
                return self._reply(chat, "Нет активного заказа — укажите номер: «фото 1001».")
            order_id = order["id"]
        # Скачиваем самый крупный из присланных размеров.
        file_id = str((photo[-1] or {}).get("file_id") or "")
        if not file_id:
            return self._reply(chat, "Не удалось получить фото.")
        raw = self._download_file(file_id)
        if not raw:
            return self._reply(chat, "Не удалось скачать фото.")
        from .config import PHOTO_DIR
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        name = f"order_{order_id}_{int(time.time())}.jpg"
        (PHOTO_DIR / name).write_bytes(raw)
        self.db.upsert("order_photos", {
            "id": f"ph{int(time.time() * 1000)}", "order_id": order_id, "at": now_iso(),
            "file": name, "note": "фото из Telegram", "kind": "upload"})
        self.db.add_event("order", "Фото к заказу (Telegram)",
                          f"Заказ {self._order_number(order_id)}", "",
                          {"order_id": order_id})
        self._reply(chat, f"📷 Фото прикреплено к заказу №{self._order_number(order_id)}.")

    def _order_number(self, order_id: str) -> str:
        order = self.db.one("SELECT number FROM orders WHERE id=?", (order_id,))
        return str((order or {}).get("number") or "")

    def _download_file(self, file_id: str) -> bytes | None:
        """Скачать файл из Telegram через общий транспорт (идея 73)."""
        return self.transport.download_file(file_id, call=self._call)

    def _dispatch(self, chat: str, raw: str) -> None:
        text = raw.lower().lstrip("/").replace("ё", "е")
        for emoji in "🖨📷≡₽⚑🛍🛒📦📊⚠❔▦▤·🗂":
            text = text.replace(emoji, " ")
        text = text.strip()
        word = text.split()[0] if text else ""

        # Команда: управление участниками (владелец; руководитель — наполовину).
        if word in ("команда", "сотрудники", "team"):
            return self._reply(chat, self._staff_command(chat, "list", text))
        if word in ("пригласить", "приглашение"):
            return self._reply(chat, self._staff_command(chat, "invite", raw))
        if word in ("сотрудник", "руководитель"):
            return self._reply(chat, self._staff_command(chat, "add", raw, role=word))
        if word in ("убрать", "уволить"):
            return self._reply(chat, self._staff_command(chat, "remove", raw))

        # Роль решает, что доступно: обзоры и полка — всем, деньги, заказы
        # и принтеры — руководителю и владельцу.
        who = gate(self.db, chat)
        effective = "стоп-живой" if (word == "стоп" and "живой" in text) else word
        group = group_for_word(
            effective, text_has_digits=any(w.isdigit() for w in text.split()[1:]))
        if who["role"] and group and group not in who["allowed"]:
            return self._reply(
                chat, f"🚫 «{word}» недоступен для роли "
                      f"«{ROLE_NAMES.get(who['role'])}».\n"
                      "Доступно сейчас: " + Staff(self.db).rights_text(who["role"]))

        if word in ("start", "help", "старт", "помощь", "меню", "?"):
            return self._reply_keyboard(chat, HELP)
        if text.startswith("закрыть месяц"):
            return self._reply(chat, self._month_close(text))
        if word in ("панель", "panel", "дашборд"):
            return self._reply_keyboard(chat, self.text_panel())
        if word in ("план", "plan", "печатать"):
            return self._reply(chat, self.text_plan())
        if word in ("стеллаж", "полка", "витрина", "shelf"):
            return self.shelf_keyboard(chat)
        # Отчёты стеллажа ловим до общей команды «продажа»:
        # «продажи стеллажа» — это сводка, а не меню быстрой продажи.
        if text.startswith("движения стеллаж") or text in ("движения", "стеллаж движения"):
            return self._reply(chat, self.text_shelf_moves(15))
        if text.startswith("продажи стеллаж") or text in ("продажи за неделю", "продажи полки"):
            return self._reply(chat, self.text_shelf_sales(7))
        if word in ("продажа", "продать", "продажи", "sell"):
            return self.sell_keyboard(chat)
        if word in ("приход", "положить", "пополнить"):
            # «приход» — меню быстрого прихода; «приход Адресник 5» — конкретной позиции.
            return self._shelf_produce_text(chat, text)
        # Каталог (номенклатура): просмотр — всем, правки — «catalog».
        if word in ("каталог", "catalog", "номенклатура"):
            return self._catalog_text(chat, text)
        if word == "цена":
            return self._reply(chat, self._catalog_price_cmd(text))
        if word in ("скрыть", "показать"):
            return self._reply(chat, self._catalog_publish_cmd(chat, text))
        if word == "описание":
            return self._reply(chat, self._catalog_desc_cmd(raw))
        if word == "товар":
            return self._reply(chat, self._catalog_new_cmd(raw))
        if word == "норматив":
            return self._reply(chat, self._catalog_norm_cmd(text))
        if word == "минималка":
            return self._reply(chat, self._catalog_minmax_cmd(text))
        if word == "архив":
            return self._reply(chat, self._catalog_archive_cmd(text, True))
        if word == "вернуть":
            return self._reply(chat, self._catalog_archive_cmd(text, False))
        if word == "удалить":
            return self._reply(chat, self._catalog_delete_cmd(chat, text))
        if word in ("пересчет", "пересчитать"):
            return self._reply(chat, self._catalog_recalc_cmd(chat, text))
        if word in ("группы", "группа"):
            return self._reply(chat, self._catalog_groups_text())
        if word in ("чаты", "диалоги", "inbox", "клиенты"):
            return self._reply(chat, self._client_inbox())
        if text.startswith("оплата подтвердить") or text.startswith("подтвердить оплату"):
            return self._reply(chat, self._client_payment_action(text, "confirm", chat))
        if text.startswith("оплата отклонить") or text.startswith("отклонить оплату"):
            return self._reply(chat, self._client_payment_action(text, "reject", chat))
        if word in ("оплата", "оплатить", "payment"):
            return self._reply(chat, self._pay(text))
        if word in ("кответ", "ответить", "creply"):
            return self._reply(chat, self._client_answer(raw))
        if word in ("клиент-бот", "кбот"):
            return self._reply(chat, self._client_bot_control(text))
        if word == "клиент" and len(text.split()) > 1 \
                and text.split()[1] in ("блок", "разблок", "бан", "разбан",
                                        "блокировка"):
            return self._reply(chat, self._client_ban(text))
        if text.startswith("отзыв ответ"):
            return self._reply(chat, self._review_answer(raw))
        if word in ("статус", "status", "принтер", "принтеры"):
            # «принтер 2» — выбрать принтер для команд; «статус 1001 печать» —
            # смена статуса заказа; иначе состояние принтеров.
            if word in ("принтер", "принтеры"):
                number = next((w for w in text.split()[1:] if w.isdigit()), "")
                if number:
                    return self._reply(chat, self._select_printer(chat, int(number)))
                if any(w.isdigit() for w in text.split()[1:]):
                    return self._reply(chat, self._set_status(text))
                return self._reply(chat, self._list_printers(chat))
            digits = any(w.isdigit() for w in text.split()[1:])
            return self._reply(chat, self._set_status(text) if digits else self.text_status())
        if word in ("выдать", "выдал", "выдан", "закрыть"):
            return self._reply(chat, self._fulfill(text))
        if word in ("новый", "заказ", "создать"):
            return self._reply(chat, self._new_order(text))
        if word in ("кадр", "камера", "фото", "photo", "cam"):
            return self.send_frame(chat)
        if word in ("таймлапс", "кадры", "гиф", "timelapse", "видео"):
            return self.send_timelapse(chat)
        if word in ("живой", "live", "дашборд"):
            return self.start_live(chat)
        if word in ("стоп-живой", "стопживой", "стоп живой"):
            return self.stop_live(chat)
        if word in ("очередь", "queue"):
            return self._reply(chat, self.text_queue())
        if word in ("датчики", "сенсоры", "ams", "амс"):
            return self._reply(chat, self.text_sensors())
        if word in ("доктор", "диагностика"):
            return self._reply(chat, self.text_doctor())
        if word in ("выше", "ниже"):
            return self._reply(chat, self._reorder_queue(text, word))
        if word in ("деньги", "финансы", "money", "прибыль"):
            return self._reply(chat, self.text_money())
        if word == "касса":
            return self.shelf_cash_keyboard(chat)
        if word in ("забрали", "выемка"):
            return self._reply(chat, self.do_collect_from_shop(text))
        if word in ("день", "сегодня", "итоги"):
            if "недел" in text:
                return self._reply(chat, self.text_weekly())
            if "месяц" in text:
                return self._reply(chat, self.text_month_report())
            return self._reply(chat, self.text_today())
        if word in ("долги", "должники", "debt", "долг"):
            return self._reply(chat, self.text_debts())
        if word in ("брак", "дефект", "defect", "дефекты"):
            return self._reply(chat, self.text_defects())
        if word in ("рейтинг", "топ", "abc", "изделия"):
            return self._reply(chat, self.text_rating())
        if word in ("хвосты", "хвост", "дыры", "проверка"):
            return self._reply(chat, self.text_loose_ends())
        if word in ("сколько", "что", "когда", "заработал", "заработано"):
            return self._reply(chat, self.text_ask(text))
        if word in ("филамент", "пластик", "катушки", "спул"):
            return self._reply(chat, self.text_filament())
        if word in ("закупить", "закупка", "закупки", "шоппинг", "покупки"):
            return self._reply(chat, self._shopping(text))
        if word in ("пауза", "pause"):
            return self._reply(chat, self.do_command("pause", "Печать поставлена на паузу", chat=chat))
        if word in ("продолжить", "resume", "старт-печати"):
            return self._reply(chat, self.do_command("resume", "Печать продолжена", chat=chat))
        if word in ("свет", "light"):
            return self._reply(chat, self.do_command("light", "Подсветка переключена", chat=chat))
        if word in ("пропустить", "скип", "исключить", "skip"):
            number = next((w for w in text.split()[1:] if w.isdigit()), "")
            if not number:
                return self._reply(chat, "Формат: «пропустить 2» — исключить объект N из печати.")
            return self._reply(chat, self.do_command("skip_objects", [int(number)],
                                                     f"Объект {number} исключён из печати", chat=chat))
        if word in ("поток", "flow"):
            m = _re.search(r"(\d+)", text)
            if not m:
                return self._reply(chat, "Формат: «поток 90» — процент подачи филамента (50–150%).")
            return self._reply(chat, self.do_command("flow", int(m.group(1)),
                                                     f"Поток {m.group(1)}%", chat=chat))
        if word in ("повторить", "перепечатать", "reprint", "повтор"):
            number = next((w for w in text.split()[1:] if w.isdigit()), "")
            try:
                row = self.manager.reprint_last_failed(
                    number, confirmed=True, request_id=uid("tg-reprint")
                )
                return self._reply(
                    chat,
                    f"↻ Повтор «{row.get('name')}» подготовлен. Запуск подтвердите отдельно.",
                )
            except Exception as exc:
                return self._reply(chat, f"Не получилось подготовить повтор: {exc}")
        if word in ("простой", "idle"):
            stats = self.manager.idle_stats()
            return self._reply(chat,
                f"⏳ Простой: {stats.get('idle_hours')} ч "
                f"({stats.get('idle_minutes')} мин)\n"
                f"Упущено ~{_money(stats.get('lost_profit'))} "
                f"по норме {_money(stats.get('rate_per_hour'))}/ч")
        if word in ("следи", "подпишись", "watch", "следить"):
            number = next((w for w in text.split()[1:] if w.isdigit()), "")
            return self._reply(chat, self._watch_order(chat, number))
        if word in ("снял", "снято", "забрал"):
            result = self.manager.part_removed()
            return self._reply(chat, f"✅ Деталь снята. Простой после печати {result.get('idle_min', 0)} мин.")
        if word in ("стоп", "stop"):
            return self._reply(chat, self.do_stop(chat, text))
        if word in ("готов", "ready"):
            return self._reply(chat, self.order_ready(text))
        self._unknown(chat, raw)

    def _unknown(self, chat: str, raw: str) -> None:
        """Непонятое сообщение: подсказка + кнопки, а не сухой отказ."""
        suggestion = suggest_command(raw)
        if suggestion:
            lines = [
                f"Не понял «{raw.strip()[:60]}».",
                f"Возможно, вы имели в виду «{suggestion}» — нажмите кнопку, "
                "и я выполню.",
                "",
                "Или нажмите «Помощь» — покажу, что умею.",
            ]
            buttons = [[{"text": suggestion, "callback_data": f"cmd:goto:{suggestion}"}],
                       [{"text": "❔ Помощь", "callback_data": "cmd:help"}]]
        else:
            lines = [
                "Не узнал такую команду.",
                "Напишите одно слово из подсказки ниже — я пойму.",
            ]
            buttons = [[{"text": "❔ Помощь", "callback_data": "cmd:help"}],
                       [{"text": "🖥 Что происходит", "callback_data": "cmd:panel"}]]
        self._call("sendMessage", {
            "chat_id": chat, "text": "\n".join(lines)[:3800],
            "reply_markup": json.dumps({"inline_keyboard": buttons}, ensure_ascii=False),
        }, timeout=15)

    # -------------------------------------------------------------- ответы
    def order_ready(self, raw: str) -> str:
        """«готов 1001» — подтвердить приёмку через общий сервис завершения."""
        words = raw.lower().replace("ё", "е").split()
        number = next((w for w in words[1:] if w.isdigit()), "")
        if not number:
            return ("Укажите номер заказа: «готов 1001».\n"
                    "Команда подтвердит визуальную приёмку и подготовит текст клиенту.")
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        from .completion import OrderCompletion
        try:
            result = OrderCompletion(self.db, self.manager.repo).accept(
                order["id"], quality_confirmed=True
            )
        except ValueError as exc:
            return f"Не получилось принять заказ №{number}: {exc}"
        actual = result.get("actual") or {}
        repeated = " (уже был принят)" if result.get("already_accepted") else ""
        return (
            f"Заказ №{number} готов ✓{repeated}\n"
            f"Факт: {round(num(actual.get('grams')), 1)} г · "
            f"{round(num(actual.get('hours')), 2)} ч · {_money(actual.get('cost'))}\n\n"
            f"Текст для клиента (не отправлен):\n{result.get('message') or ''}"
        )

    def text_status(self) -> str:
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        if not printers:
            return "Принтеры не добавлены."
        blocks = []
        for snap in printers:
            info = snap["printer"]
            head = f"{snap['name']} — {STATE_RU.get(info['state'], info.get('state_label') or info['state'])}"
            if not snap["connection"]["connected"]:
                blocks.append(head + "\nНет связи по локальной сети.")
                continue
            lines = [head]
            if info["state"] in ("RUNNING", "PAUSE", "PREPARE"):
                lines.append(f"{info.get('task') or 'задание'} · {round(num(info.get('progress')))}%")
                if info.get("layer"):
                    lines.append(f"Слой {info['layer']} из {info.get('total_layers') or '—'}")
                if num(info.get("remaining_min")):
                    lines.append(f"Осталось {_hm(info['remaining_min'])}"
                                 + (f", финиш в {str(info.get('eta'))[11:16]}" if info.get("eta") else ""))
                job = snap.get("job") or {}
                order = job.get("order") or {}
                if order:
                    lines.append(f"Заказ №{order.get('number')} · {order.get('product') or ''}")
                if num(job.get("spent")):
                    lines.append(f"Потрачено {_money(job['spent'])}"
                                 + (f", всего будет ≈ {_money(job['cost_total'])}"
                                    if num(job.get("cost_total")) else ""))
                if job.get("profit") is not None and num(job.get("price")):
                    lines.append(f"Прибыль {_money(job['profit'])}"
                                 + (f" ({round(num(job.get('margin_pct')))}%)"
                                    if job.get("margin_pct") is not None else ""))
            lines.append(f"Сопло {round(num(snap['temperature'].get('nozzle')))}°, "
                         f"стол {round(num(snap['temperature'].get('bed')))}°")
            for alert in (snap.get("guard") or {}).get("alerts", [])[:3]:
                lines.append(f"⚠ {alert.get('title')}: {alert.get('reason', '')}")
            due = (snap.get("maintenance") or {}).get("due", 0)
            if due:
                lines.append(f"🔧 Просрочено работ по обслуживанию: {due}")
            blocks.append("\n".join(lines))
        farm = state.get("farm") or {}
        blocks.append(f"Парк: печатают {farm.get('printing', 0)} из {len(printers)}, "
                      f"в очереди {farm.get('queued', 0)}.")
        return "\n\n".join(blocks)

    def text_sensors(self) -> str:
        """«датчики» (A.1.4): телеметрия парка одной сводкой.

        Только чтение снимка: температуры с целями, вентиляторы, скорость,
        WiFi/прошивка, AMS (влажность, температура, слоты с остатками) и
        расшифрованные HMS-коды. Катушка в слоте показывается по имени со
        склада (сверка по tray_uuid), иначе — тип и цвет слота.
        """
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        if not printers:
            return "Принтеры не добавлены."
        spools = {str(sp.get("tray_uuid") or ""): sp for sp in
                  self.db.query("SELECT * FROM spools WHERE archived=0")
                  if str(sp.get("tray_uuid") or "")}
        blocks = []
        for snap in printers:
            info = snap["printer"]
            temp = snap.get("temperature") or {}
            fans = snap.get("fans") or {}
            ams = snap.get("ams") or {}
            lines = [f"🌡 {snap['name']} — {STATE_RU.get(info['state'], info.get('state_label') or info['state'])}"]
            if not snap["connection"]["connected"]:
                lines.append("Нет связи по локальной сети — датчики неактуальны.")

            def t(key: str, target_key: str, title: str) -> str:
                value = temp.get(key)
                target = temp.get(target_key)
                if not value and not target:
                    return ""
                out = f"{title} {round(num(value))}°"
                if num(target) and round(num(target)) != round(num(value)):
                    out += f" → {round(num(target))}°"
                return out
            heads = [t("nozzle", "nozzle_target", "Сопло"),
                     t("bed", "bed_target", "Стол"),
                     t("chamber", "", "Камера")]
            heads = [h for h in heads if h]
            if heads:
                lines.append(" · ".join(heads))
            fan_parts = [f"{title} {round(num(v))}%"
                         for title, v in (("Обдув", fans.get("part")),
                                          ("Вспом.", fans.get("aux")),
                                          ("Камерный", fans.get("chamber")))
                         if v is not None]
            if fan_parts:
                lines.append(" · ".join(fan_parts))
            speed = []
            if info.get("speed_label"):
                speed.append(info["speed_label"])
            if num(info.get("speed_percent")) and num(info.get("speed_percent")) != 100:
                speed.append(f"{round(num(info.get('speed_percent')))}%")
            if info.get("wifi"):
                speed.append(f"WiFi {info['wifi']}")
            if info.get("firmware"):
                speed.append(f"прошивка {info['firmware']}")
            if speed:
                lines.append(" · ".join(speed))
            env = []
            if ams.get("temperature") is not None:
                env.append(f"температура {ams['temperature']}°")
            if ams.get("humidity") is not None:
                env.append(f"влажность {ams['humidity']}")
            if env:
                lines.append(f"AMS ({ams.get('units', 0)} бл.): " + " · ".join(env))
            for tray in ams.get("trays") or []:
                spool = spools.get(str(tray.get("uuid") or ""))
                name = (f"{spool.get('material')} {spool.get('color_name')}".strip()
                        if spool else
                        f"{tray.get('type') or 'пластик'} {tray.get('color') or ''}".strip())
                remain = tray.get("remain")
                left = f"{round(num(remain))}%" if remain is not None else "—"
                mark = "▸ " if tray.get("active") else "  · "
                lines.append(f"{mark}{name} ({tray['label']}) — {left}")
            for problem in info.get("problems") or []:
                lines.append(f"⚠ {problem.get('severity_label')}: "
                             f"{problem.get('title')}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _doctor_problems(self) -> list[str]:
        """Короткий список проблем цеха — для «доктора» и утреннего дайджеста (#86).

        Проверяется только локальное: жив ли опрос бота, каналы принтеров,
        свежесть резервных копий и место на диске. Сеть наружу не ходим —
        «доктор» должен отвечать даже когда Интернета нет.
        """
        problems: list[str] = []
        if self.last_poll:
            age = time.time() - self.last_poll
            if age > 90:
                problems.append(f"🤖 Бот: последний успешный опрос {int(age)} с назад")
        else:
            problems.append("🤖 Бот: ещё не было успешного опроса Telegram")
        try:
            from .workshop_v9 import heartbeat_channels
            channels = heartbeat_channels(self.manager, self.db)
        except Exception:
            channels = {}
        for unit, label in (("mqtt", "MQTT"), ("ftps", "FTPS")):
            for pr in (channels.get(unit) or {}).get("printers", []):
                if not pr.get("ok"):
                    problems.append(f"🔌 {pr.get('name')}: {label} — "
                                    f"{pr.get('error') or 'нет связи'}")
        disk = channels.get("disk") or {}
        if not disk.get("ok", True):
            problems.append(f"💿 Диск: {disk.get('error') or 'мало места'}")
        try:
            backups = list_backups()
            newest = max((b.get("at") or "" for b in backups), default="")
            if not newest:
                problems.append("💾 Резервных копий базы ещё нет")
            else:
                try:
                    from datetime import datetime
                    age_h = (datetime.now()
                             - datetime.fromisoformat(str(newest).replace("Z", ""))).total_seconds() / 3600
                    if age_h > 48:
                        problems.append(
                            f"💾 Последняя копия базы {round(age_h)} ч назад")
                except Exception:
                    pass
        except Exception:
            pass
        return problems

    def text_doctor(self) -> str:
        """«доктор» (#80): здоровье цеха одним сообщением, без внешней сети."""
        problems = self._doctor_problems()
        lines = ["🩺 Доктор PrintFlow:"]
        if problems:
            lines.append(f"⚠ Проблем: {len(problems)}")
            lines.extend(f"  · {p}" for p in problems)
        else:
            lines.append("✅ Цех здоров: бот, связь, копии и диск — ок.")
        lines.append(f"Версия {APP_VERSION} · схема {SCHEMA_VERSION}. "
                     "Подробная диагностика: python pf.py doctor.")
        return "\n".join(lines)

    def text_queue(self) -> str:
        jobs = [j for j in self.manager.queue() if j.get("state") in ("queued", "running", "starting")]
        if not jobs:
            return "Очередь пуста."
        lines = ["Очередь печати:"]
        for index, job in enumerate(jobs[:12], 1):
            order = job.get("order") or {}
            title = order.get("number") and f"№{order['number']} {order.get('product') or ''}" or job.get("name") or "задание"
            mark = {"running": "▶", "starting": "▶"}.get(job.get("state"), f"{index}.")
            lines.append(f"{mark} {title}")
        if self.manager.quiet_now():
            lines.append("\nСейчас тихие часы — автозапуск отложен до утра.")
        return "\n".join(lines)

    def text_money(self) -> str:
        summary = self.manager.acc.summary(30)
        debts = self.manager.acc.debts()
        lines = [
            "Деньги за 30 дней:",
            f"Выручка {_money(summary.get('income'))}",
            f"Расходы {_money(summary.get('expense'))}",
            f"Прибыль {_money(summary.get('profit'))}",
        ]
        if summary.get("orders"):
            lines.append(f"Заказов {summary['orders']}, средний чек {_money(summary.get('avg_check'))}")
        if num(debts.get("total")) > 0:
            lines.append(f"\nЖдём оплату: {_money(debts['total'])} по {debts.get('count', 0)} заказам")
            if num(debts.get("overdue")) > 0:
                lines.append(f"Из них просрочено: {_money(debts['overdue'])}")
        return "\n".join(lines)

    def text_shop_cash(self) -> str:
        """Касса стеллажа (магазин): сколько продано, забрано и лежит в магазине."""
        from .shelf import Shelf
        c = Shelf(self.db).shop_cash()
        lines = [
            "🛍 Касса стеллажа (магазин):",
            f"Продано со стеллажа: {_money(c.get('shelf_income'))}",
            f"Забрали из магазина: {_money(c.get('collected_total'))}",
            f"Лежит в магазине: {_money(c.get('in_shop'))}",
        ]
        rows = c.get("collections") or []
        if rows:
            lines.append("\nПоследние выемки:")
            for r in rows[:5]:
                when = str(r.get("at") or "")[:16].replace("T", " ")
                note = str(r.get("note") or "").strip()
                lines.append(f"· {_money(r.get('amount'))} — {when}"
                             + (f" ({note[:40]})" if note else ""))
        lines.append("\nЗаписать выемку: «забрали 5000» · «забрали 2500 картой» ·"
                     " «забрали все».")
        return "\n".join(lines)

    def do_collect_from_shop(self, text: str) -> str:
        """«забрали 5000» или «забрали все» — выемка из кассы магазина."""
        from .shelf import Shelf
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
        return (f"✅ Забрали из магазина {_money(amount)}.\n"
                f"Осталось в магазине: {_money(c.get('in_shop'))}.")

    def text_today(self) -> str:
        today = now_iso()[:10]
        rows = self.db.query(
            "SELECT kind, SUM(amount) AS total FROM transactions WHERE substr(at,1,10)=? GROUP BY kind",
            (today,))
        money = {r["kind"]: num(r["total"]) for r in rows}
        jobs = self.db.query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(grams),0) AS g, COALESCE(SUM(duration_min),0) AS m"
            " FROM print_jobs WHERE substr(COALESCE(finished_at,queued_at),1,10)=? AND state='done'",
            (today,))
        job = jobs[0] if jobs else {}
        lines = [
            f"Итоги дня {today}:",
            f"Приход {_money(money.get('income'))}, расход {_money(money.get('expense'))}",
            f"Напечатано заданий: {int(num(job.get('n')))}, пластика {round(num(job.get('g')))} г, "
            f"время печати {_hm(num(job.get('m')))}",
        ]
        # Хвосты учёта: заказы без цены и т.п. — чтобы не копились до конца месяца.
        try:
            from .repo import Repo
            problems = Repo(self.db).data_check().get("problems") or []
        except Exception:
            problems = []
        if problems:
            lines.append(f"⚠ Хвосты учёта: {len(problems)} — «хвосты» покажет список")
        return "\n".join(lines)

    def text_loose_ends(self) -> str:
        """«хвосты» — незакрытые дыры в учёте: заказы без цены и т.п."""
        from .repo import Repo
        problems = Repo(self.db).data_check().get("problems") or []
        if not problems:
            return "✅ Хвостов нет — учёт чистый."
        lines = [f"⚠ Хвосты учёта: {len(problems)}"]
        for problem in problems[:12]:
            lines.append(f"· {problem.get('title')}"
                         + (f" — {problem.get('detail')}" if problem.get("detail") else ""))
        return "\n".join(lines)

    def text_filament(self) -> str:
        """Остатки филамента на складе и в AMS: граммы, %, прогноз окончания."""
        spools = self.db.query("SELECT * FROM spools WHERE archived=0 ORDER BY remaining_grams DESC")
        if not spools:
            return "Катушек на складе нет — добавьте через раздел «Склад»."
        threshold = num(self.db.setting("filament_low_threshold", 15.0), 15.0)
        lines = ["🧵 Филамент на складе:"]
        for s in spools[:12]:
            total = max(1.0, num(s.get("total_grams"), 1000))
            left = num(s.get("remaining_grams"))
            pct = left / total * 100
            mark = "⚠" if pct <= threshold else "·"
            slot = f" · AMS слот {s.get('ams_slot')}" if str(s.get("ams_slot") or "") != "" else ""
            lines.append(f"{mark} {s.get('material')} {s.get('color_name') or ''} — "
                         f"{round(left)} г ({round(pct)}%){slot}")
        # Прогноз закупки по темпу расхода за 30 дней
        usage = self.db.one(
            "SELECT UPPER(material) m, SUM(grams) g FROM filament_usage"
            " WHERE at>=? GROUP BY UPPER(material) ORDER BY g DESC LIMIT 3",
            ((datetime.now() - timedelta(days=30)).isoformat(),))
        hints = []
        for row in usage or []:
            rate = num(row.get("g")) / 30.0  # г/день
            stock = num(self.db.one(
                "SELECT COALESCE(SUM(remaining_grams),0) v FROM spools"
                " WHERE archived=0 AND UPPER(material)=?", (row.get("m"),))["v"])
            if rate > 0:
                days = stock / rate
                hints.append(f"{row.get('m')}: хватит на ~{int(days)} дн (темп {round(rate)} г/дн)")
        if hints:
            lines.append("\nПрогноз:")
            lines.extend("  " + h for h in hints)
        return "\n".join(lines)

    def _shopping(self, text: str) -> str:
        """Список закупок: показать, автозаполнить или добавить вручную."""
        from .shopping import ShoppingList
        shop = ShoppingList(self.db)
        words = text.lower().replace("ё", "е").split()
        # «закупка авто» — автозаполнение из низких катушек и темпа расхода
        if len(words) > 1 and words[1] in ("авто", "обновить", "заполнить"):
            result = shop.auto_fill()
            if result.get("count"):
                return f"🛒 Добавлено в закупку: {result['count']} позиций.\n\n" + shop.text()
            return "🛒 Добавлять нечего — катушки и запас в порядке.\n\n" + shop.text()
        # Одной команды недостаточно для честной приёмки: нужны фактические
        # количество/вес, сумма и касса. Не закрываем строку без этих данных.
        if len(words) > 1 and words[1] in ("купил", "получил", "принял"):
            return ("Оформите приём в панели: Склад пластика → Список закупок → «Принять». "
                    "Там PrintFlow создаст катушки и запишет подтверждённый расход без дублей.")
        # «закупка <материал> <qty>» — добавить вручную
        if len(words) > 1 and words[1] not in ("авто", "купил", "обновить", "заполнить"):
            material = words[1].upper()
            qty = next((w for w in words[2:] if w.isdigit()), "1")
            shop.add({"name": material, "material": material, "qty": float(qty),
                      "unit": "кг", "source": "manual"})
            return f"🛒 Добавлено: {material} {qty} кг.\n\n" + shop.text()
        return shop.text()

    def text_digest(self) -> str:
        """Утренний дайджест: дедлайны, очередь, остатки, кому написать."""
        today = now_iso()[:10]
        finals = {r["id"] for r in self.db.query("SELECT id FROM statuses WHERE is_final=1")}
        orders = self.db.query("SELECT * FROM orders")
        active = [o for o in orders if o["status"] not in finals]
        due_today = [o for o in active if o.get("due") == today]
        late = [o for o in active if o.get("due") and o["due"] < today]
        lines = ["☀ Доброе утро! Дайджест PrintFlow:"]
        if late:
            lines.append(f"⚠ Просрочено заказов: {len(late)}")
            for o in late[:3]:
                lines.append(f"  №{o.get('number')} {o.get('product') or ''} (срок {o.get('due')})")
        if due_today:
            lines.append(f"📌 Срок сегодня: {len(due_today)}")
            for o in due_today[:3]:
                lines.append(f"  №{o.get('number')} {o.get('product') or ''}")
        else:
            lines.append("📌 Заказов со сроком на сегодня нет.")
        queue = [j for j in self.manager.queue() if j.get("state") == "queued"]
        if queue:
            lines.append(f"🖨 В очереди {len(queue)} заданий:")
            for j in queue[:5]:
                order = j.get("order") or {}
                lines.append(f"  · {order.get('number') and ('№' + str(order['number']) + ' ') or ''}{j.get('name') or ''}")
        else:
            lines.append("🖨 Очередь печати пуста.")
        low = self.db.query(
            "SELECT material, color_name, remaining_grams FROM spools WHERE archived=0")
        low = [s for s in low
               if num(s["remaining_grams"]) / max(1.0, num(self.db.setting("default_spool_weight", 1000))) * 100
               <= num(self.db.setting("filament_low_threshold", 15.0))]
        if low:
            lines.append("🧵 Мало пластика:")
            for s in low[:5]:
                lines.append(f"  · {s['material']} {s['color_name']} — {round(num(s['remaining_grams']))} г")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            lines.append(f"💰 Ждут оплаты {_money(debts['total'])} по {debts.get('count', 0)} заказам")
        # #86 «Утренний авто-доктор»: дайджест сам говорит, здоров ли цех.
        problems = self._doctor_problems()
        if problems:
            lines.append(f"🩺 Цех требует внимания ({len(problems)}):")
            lines.extend(f"  · {p}" for p in problems[:4])
        else:
            lines.append("🩺 Цех здоров: бот, связь, копии и диск — ок.")
        return "\n".join(lines)

    def text_weekly(self) -> str:
        """Еженедельный отчёт: деньги, печать, брак, пластик."""
        summary = self.manager.acc.summary(7)
        jobs = self.db.query(
            "SELECT state, COUNT(*) n, COALESCE(SUM(grams),0) g, COALESCE(SUM(duration_min),0) m"
            " FROM print_jobs WHERE finished_at>=? GROUP BY state",
            ((datetime.now() - timedelta(days=7)).isoformat(),))
        by_state = {r["state"]: r for r in jobs}
        lines = [
            "📊 Недельный отчёт PrintFlow:",
            f"Выручка {_money(summary.get('income'))}, расход {_money(summary.get('expense'))}",
            f"Прибыль {_money(summary.get('profit'))} (маржа {round(num(summary.get('margin')))}%)",
            f"Печать: {int(num((by_state.get('done') or {}).get('n')))} заданий, "
            f"{round(num((by_state.get('done') or {}).get('g')))} г, "
            f"{_hm(num((by_state.get('done') or {}).get('m')))}",
        ]
        failed = int(num((by_state.get("failed") or {}).get("n")))
        if failed:
            lines.append(f"⚠ Брак: {failed} печатей")
        stock = self.db.one("SELECT COALESCE(SUM(remaining_grams),0) v FROM spools WHERE archived=0") or {}
        lines.append(f"🧵 Пластика на складе: {round(num(stock.get('v')))} г")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            lines.append(f"💰 Долги: {_money(debts['total'])}")
        return "\n".join(lines)

    # ------------------------------------------------- отчётность по запросу
    def _month_close(self, text: str) -> str:
        """«закрыть месяц» — состояние мастера; «закрыть месяц fixed» — шаг."""
        from .month_close import MonthClose, STEP_ORDER
        master = MonthClose(self.db)
        parts = text.split()
        if len(parts) > 2 and parts[2] in STEP_ORDER:
            step = parts[2]
            result = master.run("", step)
            if not result.get("ok") and result.get("done"):
                return f"Шаг «{step}» уже выполнен в этом месяце."
            if not result.get("ok"):
                return f"Шаг не выполнен: {result.get('error')}"
            return f"✅ {result.get('message') or 'готово'}"
        state = master.state()
        lines = [f"🧾 Закрыть месяц {state['key']}:"]
        for step in state["order"]:
            mark = "✅" if state["done"][step] else ("▸" if state["next"] == step else "·")
            lines.append(f"{mark} {state['titles'][step]}")
        if state["next"]:
            lines.append(f"\nШаг: «закрыть месяц {state['next']}»")
        return "\n".join(lines)

    def text_debts(self) -> str:
        """«долги» — кто и сколько должен, с просрочкой."""
        debts = self.manager.acc.debts()
        if not debts.get("rows"):
            return "💰 Долгов нет — всё оплачено."
        lines = [f"💰 Долги клиентов: {_money(debts['total'])} "
                 f"по {debts.get('count', 0)} заказам"]
        if num(debts.get("overdue")) > 0:
            lines.append(f"Просрочено: {_money(debts['overdue'])}")
        for row in debts["rows"][:10]:
            who = (row.get("customer") or "").strip() or "без имени"
            age = f" · {row['days']} дн" if row.get("days") else ""
            lines.append(f"· №{row.get('number')} {who} — {_money(row['debt'])}{age}")
        return "\n".join(lines)

    def text_defects(self, days: int = 30) -> str:
        """«брак» — сорванные печати и сколько денег они съели."""
        since = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat()
        jobs = self.db.query(
            "SELECT * FROM print_jobs WHERE state='failed' AND finished_at>=?"
            " ORDER BY finished_at DESC", (since,))
        facts = self.manager.acc.defects_cost(days)
        lines = [f"❌ Брак за {int(days)} дней: {len(jobs)} печатей"]
        lines.append(
            f"Потеряно: {round(num(facts.get('grams')))} г пластика, "
            f"{_hm(num(facts.get('minutes')))} времени"
        )
        lines.append(
            f"≈ {_money(num(facts.get('cost')))}; подтверждённые разборы взяты по факту"
        )
        for job in jobs[:6]:
            error = str(job.get("error") or "").strip()[:70]
            lines.append(f"· {job.get('name') or 'без названия'}"
                         + (f" — {error}" if error else ""))
        return "\n".join(lines)

    def text_rating(self) -> str:
        """«рейтинг» — ABC изделий: что приносит деньги, что висит балластом."""
        items = self.manager.acc.abc_report(30).get("items", [])
        if not items:
            return "За 30 дней продаж не было — рейтинг пуст."
        lines = ["🏆 Рейтинг изделий (30 дней):"]
        for item in items[:10]:
            lines.append(
                f"{item.get('cls', '')} · {item.get('name')} — {_money(item.get('revenue'))}"
                f" ({item.get('share')}%), прибыль {_money(item.get('profit'))}")
        return "\n".join(lines)

    def text_month_report(self) -> str:
        """«итоги месяца» — P&L месяца, печать, брак, долги."""
        key = now_iso()[:7]
        pnl = self.manager.acc.pnl_month(key)
        jobs = self.db.query(
            "SELECT state, COUNT(*) n, COALESCE(SUM(grams),0) g,"
            " COALESCE(SUM(duration_min),0) m FROM print_jobs"
            " WHERE finished_at>=? GROUP BY state",
            (f"{key}-01",))
        by_state = {row["state"]: row for row in jobs}
        done = by_state.get("done") or {}
        failed = by_state.get("failed") or {}
        lines = [
            f"📊 Итоги месяца {key}:",
            f"Выручка {_money(pnl.get('income'))}, расход {_money(pnl.get('expense'))}",
            f"Прибыль {_money(pnl.get('profit'))} (маржа {round(num(pnl.get('margin')))}%)",
            f"Печать: {int(num(done.get('n')))} заданий, {round(num(done.get('g')))} г,"
            f" {_hm(num(done.get('m')))}",
        ]
        if int(num(failed.get("n"))):
            lines.append(f"⚠ Брак: {int(num(failed.get('n')))} печатей")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            lines.append(f"💰 Долги: {_money(debts['total'])}")
        tax = self.month_tax_estimate(key)
        if num(tax) > 0:
            lines.append(f"🧾 Налог месяца (оценка): {_money(tax)}")
        return "\n".join(lines)

    def month_tax_estimate(self, key: str) -> float:
        """Оценка налога месяца — из мастера «Закрыть месяц»."""
        try:
            from .month_close import MonthClose
            return num(MonthClose(self.db).month_tax(key).get("tax"))
        except Exception:
            return 0.0

    def text_ask(self, text: str) -> str:
        """«Спроси принтер»: сколько осталось / что печатает / когда закончит /
        сколько заработал — разбор по ключевым словам, без ИИ."""
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        printer = next((p for p in printers
                        if p["printer"]["state"] in ("RUNNING", "PAUSE", "PREPARE")), None)
        if "заработал" in text or "заработано" in text or "прибыль" in text:
            summary = self.manager.acc.summary(1)
            return (f"Сегодня: приход {_money(summary.get('income'))}, "
                    f"прибыль {_money(summary.get('profit'))}, "
                    f"часов печати {round(num(summary.get('print_hours')), 1)}")
        if not printer:
            return "Сейчас ничего не печатается."
        info = printer["printer"]
        if "когда" in text or "закончит" in text or "во сколько" in text:
            if num(info.get("remaining_min")):
                eta = str(info.get("eta") or "")
                return (f"Готово через {_hm(info['remaining_min'])}"
                        + (f", примерно в {eta[11:16]}" if eta else ""))
            return "Прогноз времени пока не готов — печать только началась."
        if "осталось" in text or "сколько" in text or "прогресс" in text:
            return (f"{printer['name']}: {round(num(info.get('progress')))}% · "
                    f"слой {info.get('layer')} из {info.get('total_layers') or '—'}"
                    + (f" · осталось {_hm(info['remaining_min'])}"
                       if num(info.get("remaining_min")) else ""))
        if "печатает" in text or "задание" in text or "задача" in text:
            job = printer.get("job") or {}
            order = job.get("order") or {}
            extra = f" · заказ №{order.get('number')}" if order else ""
            return (f"{printer['name']} печатает «{info.get('task') or job.get('name') or '—'}»{extra}"
                    f" · {round(num(info.get('progress')))}%")
        # не распознали вопрос — покажем компактный статус
        return self.text_status()

    def send_frame(self, chat: str) -> None:
        printer = self._pick_printer(chat)
        if not printer:
            return self._reply(chat, "Принтеры не добавлены.")
        frame = printer.camera.frame
        if not frame:
            return self._reply(chat, "Камера сейчас не отдаёт кадр. "
                                     "Проверьте, что принтер включён и в сети.")
        snap = printer.snapshot()
        info = snap["printer"]
        caption = f"{snap['name']} — {STATE_RU.get(info['state'], info['state'])}"
        if info["state"] in ("RUNNING", "PAUSE"):
            caption += f", {round(num(info.get('progress')))}%"
            if num(info.get("remaining_min")):
                caption += f", осталось {_hm(info['remaining_min'])}"
        if snap.get("camera", {}).get("demo"):
            caption += " (демо-кадр: принтер не подключён)"
        token = self._settings().get("telegram_token", "")
        try:
            self.manager._send_photo(token, chat, caption, frame)
            printer.camera.snapshot(note="Запрос из Telegram")
        except Exception as exc:
            self._reply(chat, f"Не удалось отправить кадр: {exc}")

    def send_timelapse(self, chat: str) -> None:
        """Отправить последние кадры печати как медиа-группу (мини-таймлапс)."""
        printer = self._pick_printer(chat)
        if not printer:
            return self._reply(chat, "Принтеры не добавлены.")
        shots = printer.camera.snapshot_list()
        # Берём последние кадры в прямом хронологическом порядке.
        frames = [s for s in shots if s.get("at")][:10]
        if not frames:
            return self._reply(chat, "Снимков пока нет. Камера делает их по событиям "
                                     "печати и по запросу «кадр».")
        photos = []
        for shot in reversed(frames):
            frame = printer.camera.snapshot_frame(shot["id"])
            if frame:
                photos.append(frame)
        if not photos:
            return self._reply(chat, "Не удалось прочитать сохранённые кадры.")
        token = self._settings().get("telegram_token", "")
        if len(photos) == 1:
            self.manager._send_photo(token, chat, "Последний кадр печати", photos[0])
            return None
        self._send_media_group(token, chat, photos)

    def _send_media_group(self, token: str, chat: str, photos: list[bytes]) -> None:
        """Несколько фото одним сообщением (sendMediaGroup, multipart)."""
        boundary = "----printflow" + uid("b").replace("b_", "")
        parts: list[bytes] = []
        media = []
        for i in range(len(photos)):
            attach = f"attach://photo{i}"
            media.append({"type": "photo", "media": attach})
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo{i}\";"
                f" filename=\"frame{i}.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode())
            parts.append(photos[i])
            parts.append(b"\r\n")
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
                f"{chat}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"media\"\r\n\r\n"
                f"{json.dumps(media)}\r\n").encode()
        tail = f"--{boundary}--\r\n".encode()
        body = head + b"".join(parts) + tail
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMediaGroup", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()

    # ----------------------------------------------------- живой дашборд
    def start_live(self, chat: str) -> str:
        """Включить автообновляющийся дашборд-сообщение на время печати."""
        printer = self._pick_printer(chat)
        if not printer:
            return "Принтеры не добавлены."
        self._live[chat] = {"message_id": 0, "text": ""}
        text = self.text_status()
        try:
            sent = self._call("sendMessage", {"chat_id": chat, "text": text[:3800],
                                              "disable_web_page_preview": "true"}, timeout=15)
            message_id = sent.get("result", {}).get("message_id", 0)
            self._live[chat] = {"message_id": message_id, "text": text}
            return ("Дашборд включён — будет обновляться во время печати.\n"
                    "Выключить: «стоп живой».")
        except Exception:
            self._live.pop(chat, None)
            return "Не удалось запустить дашборд."

    def stop_live(self, chat: str) -> str:
        self._live.pop(chat, None)
        return "Дашборд выключен."

    def _maybe_live(self) -> None:
        """Обновить живые дашборды, если печать активна и текст изменился."""
        if not self._live:
            return
        for chat, state in list(self._live.items()):
            text = self.text_status()
            if text == state.get("text"):
                continue
            message_id = state.get("message_id", 0)
            state["text"] = text
            if message_id:
                try:
                    self._call("editMessageText", {"chat_id": chat,
                                                   "message_id": str(message_id),
                                                   "text": text[:3800]}, timeout=15)
                except Exception:
                    pass

    # -------------------------------------------------- 5.0: панель и продажи
    def text_panel(self) -> str:
        """Главная панель: печать, деньги, план, долги — одним сообщением."""
        blocks = []
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        farm = state.get("farm") or {}
        if printers:
            active = next((p for p in printers if p["printer"]["state"] in ("RUNNING", "PAUSE", "PREPARE")), None)
            if active:
                info = active["printer"]
                blocks.append(f"🖨 {active['name']} — {STATE_RU.get(info['state'], info['state'])} "
                              f"{round(num(info.get('progress')))}%"
                              + (f", осталось {_hm(info['remaining_min'])}" if num(info.get("remaining_min")) else ""))
            else:
                blocks.append(f"🖨 Парк свободен ({len(printers)} принтер(ов), в очереди {farm.get('queued', 0)})")
        summary = self.manager.acc.summary(30)
        blocks.append(f"₽ За 30 дней: доход {_money(summary.get('income'))}, "
                      f"прибыль {_money(summary.get('profit'))}")
        debts = self.manager.acc.debts()
        if num(debts.get("total")) > 0:
            blocks.append(f"💰 Ждут оплаты {_money(debts['total'])} по {debts.get('count', 0)} заказам")
        # что печатать следующим
        try:
            from .planner import Planner
            from .batches import Batches
            planner = Planner(self.db, Batches(self.db))
            plan = planner.day_plan()
            next_task = plan.get("suggested_next")
            if next_task:
                label = "заказ" if next_task.get("kind") == "order" else "полка"
                blocks.append(f"⚑ Следующее ({label}): {next_task.get('title')} · {_hm(next_task.get('hours', 0) * 60)}")
            elif plan.get("sequence"):
                blocks.append("⚑ Очередь пуста, но есть план — откройте «план».")
            else:
                blocks.append("⚑ Печатать нечего: очередь и полка в порядке.")
        except Exception:
            pass
        alerts = []
        for p in printers:
            for a in (p.get("guard") or {}).get("alerts", [])[:2]:
                alerts.append(f"⚠ {a.get('title')}")
        if alerts:
            blocks.append("\n".join(alerts[:3]))
        return "\n".join(blocks)

    def text_plan(self) -> str:
        """Что печатать сегодня — из мастер-плана производства."""
        try:
            from .planner import Planner
            from .batches import Batches
            planner = Planner(self.db, Batches(self.db))
            plan = planner.day_plan()
        except Exception:
            return "План сейчас недоступен."
        lines = [f"⚑ План на сегодня ({plan.get('verdict_text') or ''})",
                 f"Занято {plan.get('in_progress_hours')} ч, план {plan.get('planned_hours')} ч, "
                 f"загрузка {round(num(plan.get('load_pct')))}%"]
        for i, t in enumerate(plan.get("sequence")[:8], 1):
            kind = "▦" if t.get("kind") == "order" else "▤"
            issues = " ✕" if not t.get("ready") else ""
            lines.append(f"{kind} {t.get('title')} · {_hm(num(t.get('hours')) * 60)}{issues}")
        if not plan.get("sequence"):
            lines.append("Печатать нечего — полка и очередь в порядке.")
        return "\n".join(lines)

    def text_shelf(self, only_needs: bool = False) -> str:
        """Короткий честный срез стеллажа для телефона.

        Не подменяет инвентаризацию: показывает учётный остаток и помечает
        позиции, которые надо проверить или пополнить физически.
        """
        from .shelf import Shelf
        shelf = Shelf(self.db)
        items = shelf.items()
        if not items:
            return "🛍 Стеллаж пуст: активных позиций пока нет."
        needs = [i for i in items if i.get("status") in ("empty", "low", "dead") or num(i.get("plan_qty")) > 0]
        total_qty = sum(num(i.get("qty")) for i in items)
        total_value = sum(num(i.get("stock_value")) for i in items)
        if only_needs:
            if not needs:
                return "✅ Стеллаж в порядке: пустых, низких и залежавшихся позиций нет."
            title = f"⚠ Внимание к стеллажу: {len(needs)}"
            rows = needs
        else:
            title = (f"🛍 Стеллаж: {len(items)} поз. · {round(total_qty, 1)} шт · "
                     f"учётная стоимость {_money(total_value)}")
            rows = sorted(needs, key=lambda i: (i.get("status") == "empty", num(i.get("plan_qty"))), reverse=True) or items
        lines = [title]
        for item in rows[:8]:
            # В базовой аналитике «нет продаж» приоритетнее low. Для человека
            # на полке это две разные причины действия, поэтому не прячем
            # низкий остаток за статусом залежавшегося товара.
            marks = []
            if num(item.get("qty")) <= 0:
                marks.append("нет")
            elif item.get("low"):
                marks.append("мало")
            elif not item.get("dead"):
                marks.append("в норме")
            if item.get("dead"):
                marks.append("нет продаж")
            status = " · ".join(marks) or "проверить"
            extra = f" · печать +{int(num(item.get('plan_qty')))}" if num(item.get("plan_qty")) > 0 else ""
            lines.append(f"• {item.get('name') or 'Без названия'} — {round(num(item.get('qty')), 1)} шт · {status}{extra}")
        if len(rows) > 8:
            lines.append(f"… ещё {len(rows) - 8} поз. в полной панели.")
        today = shelf.today_sales()
        cash = shelf.shop_cash()
        answer = "\n".join(lines)
        if not only_needs:
            answer += ("\n\n📈 Сегодня: "
                       f"{round(today.get('qty', 0), 1)} шт · {_money(today.get('money', 0))}"
                       f" · в кассе магазина {_money(cash.get('in_shop', 0))}")
            online_money = num(today.get("online_money", 0))
            online_total = num(cash.get("online_income", 0))
            if online_money > 0 or online_total > 0:
                answer += (f"\n🌐 Онлайн (Авито/ТГ): сегодня {_money(online_money)} · "
                           f"всего {_money(online_total)} — на счёте, не в кассе")
        return answer

    def text_shelf_cash(self) -> str:
        """Касса стеллажа: сколько лежит в магазине и как записать выемку."""
        from .shelf import Shelf
        shelf = Shelf(self.db)
        cash = shelf.shop_cash()
        today = shelf.today_sales()
        lines = [
            "💰 Касса стеллажа",
            f"• Продано сегодня: {round(today.get('qty', 0), 1)} шт · "
            f"{_money(today.get('money', 0))}"
            f" (полка {_money(today.get('shop_money', 0))} · "
            f"онлайн {_money(today.get('online_money', 0))})",
            f"• Продано за все время: {_money(cash.get('shelf_income'))}",
            f"• Забрали из магазина: {_money(cash.get('collected_total'))}",
            f"• Лежит в магазине: {_money(cash.get('in_shop'))}",
            f"• Онлайн (Авито/ТГ): {_money(cash.get('online_income'))}"
            " — на счёте, в кассу магазина не входит",
            "",
            "Быстрая выемка — кнопками ниже. Точная сумма: «забрали 5000».",
        ]
        return "\n".join(lines)

    def shelf_cash_keyboard(self, chat: str, message_id: str = "") -> None:
        """Меню кассы стеллажа: кнопки быстрой выемки + назад к полке."""
        from .shelf import Shelf
        cash = Shelf(self.db).shop_cash()
        in_shop = num(cash.get("in_shop"))
        lines = [
            "💰 Касса стеллажа",
            f"• Лежит в магазине: {_money(in_shop)}",
            f"• Забрали за все время: {_money(cash.get('collected_total'))}",
            f"• Онлайн (Авито/ТГ): {_money(cash.get('online_income'))} — на счёте",
            "",
            "Кнопки — быстрая выемка. Точная сумма: «забрали 2500».",
        ]
        buttons = []
        if in_shop >= 0.005:
            row = []
            for amount in (1000, 5000):
                if in_shop + 0.004 < amount:
                    continue
                row.append({"text": f"Забрали {_money(amount)}",
                            "callback_data": f"cmd:shelf-cash-w:{amount}"})
            if row:
                buttons.append(row)
            buttons.append([{"text": f"Забрать всё · {_money(in_shop)}",
                             "callback_data": "cmd:shelf-cash-w:all"}])
        buttons.append([{"text": "← Назад к полке", "callback_data": "cmd:shelf"}])
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def do_shelf_collect(self, spec: str) -> str:
        """Быстрая выемка из кассы магазина: сумма или «all» (забрать всё)."""
        from .shelf import Shelf
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
            return (f"В магазине лежит только {_money(in_shop)} — "
                    f"забрать {_money(amount)} нельзя.")
        try:
            shelf.add_collection(round(amount, 2), note="выемка из Telegram")
        except ValueError as exc:
            return f"Не получилось: {exc}"
        left = shelf.shop_cash().get("in_shop")
        return f"✅ Забрали {_money(amount)}. В кассе магазина осталось {_money(left)}."

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
            [{"text": "🔄 Обновить", "callback_data": "cmd:shelf"}],
        ]
        self._call("sendMessage", {"chat_id": chat, "text": text[:3800],
                                   "reply_markup": json.dumps({"inline_keyboard": buttons})}, timeout=15)

    def _page_from_command(self, command: str, prefix: str, state: dict,
                           chat: str) -> int:
        """Страница пагинации из callback «prefix:next/prev/число»."""
        part = command[len(prefix):].lstrip(":")
        if part in ("next", "вперёд"):
            return state.get(chat, 0) + 1
        if part in ("prev", "назад"):
            return state.get(chat, 0) - 1
        try:
            return max(0, int(num(part)))
        except (TypeError, ValueError):
            return 0

    def _sell_rows(self) -> list[dict]:
        """Позиции стеллажа с остатком для быстрой продажи — все, без лимита."""
        from .shelf import Shelf
        items = Shelf(self.db).items()
        rows = [i for i in items if num(i.get("qty")) > 0]
        rows.sort(key=lambda i: -num(i["qty"]))
        return rows

    def _paginate(self, rows: list, page: int, per_page: int = 8) -> tuple:
        """Нарезать список на страницы; вернуть (срез, page, total_pages)."""
        per_page = max(1, int(per_page))
        total = len(rows)
        total_pages = max(1, -(-total // per_page))  # округление вверх
        page = max(0, min(int(page), total_pages - 1))
        start = page * per_page
        return rows[start:start + per_page], page, total_pages

    def _send_menu(self, chat: str, text: str, buttons: list, message_id: str = "") -> None:
        """Отправить inline-меню; если есть message_id — правим старое сообщение."""
        markup = json.dumps({"inline_keyboard": buttons}, ensure_ascii=False)
        if message_id:
            self._call("editMessageText", {
                "chat_id": chat, "message_id": message_id,
                "text": text[:3800], "reply_markup": markup}, timeout=15)
            return
        self._call("sendMessage", {"chat_id": chat, "text": text[:3800],
                                   "reply_markup": markup}, timeout=15)

    def sell_keyboard(self, chat: str, page: int = 0, message_id: str = "") -> None:
        rows = self._sell_rows()
        if not rows:
            self._reply(chat, "На стеллаже нет товара. Сделайте приход или перенесите со склада.")
            return
        page_rows, page, total_pages = self._paginate(rows, page)
        self._sell_page[chat] = page
        lines = ["🛍 Продажа со стеллажа — нажмите «−1» (деньги по цене ценника):"]
        buttons = []
        for i in page_rows:
            name = str(i.get('name') or 'позиция')
            lines.append(f"• {name} — {round(num(i['qty']),1)} шт · {_money(i.get('price'))}")
            label = f"−1 · {name[:22]} · {_money(i.get('price'))}"
            buttons.append([{"text": label[:60], "callback_data": f"cmd:shelf-sell:{i['id']}"}])
        if total_pages > 1:
            nav = [{"text": "◀", "callback_data": "cmd:sell-menu:prev"},
                   {"text": f"{page + 1}/{total_pages}", "callback_data": "cmd:sell-menu"},
                   {"text": "▶", "callback_data": "cmd:sell-menu:next"}]
            buttons.append(nav)
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def do_sell(self, nom_id: str) -> str:
        from .documents import Documents
        item = self.db.one("SELECT * FROM nomenclature WHERE id=?", (nom_id,))
        if not item:
            return "Позиция не найдена."
        warehouse = self.db.one(
            "SELECT id FROM warehouses WHERE archived=0 AND retail=1 ORDER BY position LIMIT 1") \
            or self.db.one("SELECT id FROM warehouses WHERE archived=0 ORDER BY position LIMIT 1")
        if not warehouse:
            return "Не настроен склад."
        docs = Documents(self.db)
        try:
            docs.quick_sale([{"nom_id": nom_id, "qty": 1}], warehouse["id"],
                            "shop", "", "продажа из Telegram")
            name = item.get("name") or "позиция"
            return f"Продано 1 шт «{name}» — проведено и учтено в кассе."
        except Exception as exc:
            return f"Не получилось продать: {exc}"

    # ----------------------------------------------------- стеллаж из ТГ
    def do_shelf_sell(self, item_id: str, qty: float = 1) -> str:
        """Списать штуки с физической позиции стеллажа и записать доход."""
        from .shelf import Shelf
        shelf = Shelf(self.db)
        item = self.db.one("SELECT * FROM shelf_items WHERE id=? AND active=1", (item_id,))
        if not item:
            return "Позиция стеллажа не найдена."
        qty = num(qty) or 1
        try:
            shelf.sale(item_id, qty, 0, channel="shelf", note="продажа из Telegram")
            left = num(item.get("qty")) - qty
            price = num(item.get("price"))
            money = f" · {_money(price * qty)}" if price else " (без цены)"
            return (f"✅ Продано {round(qty)} шт «{item.get('name')}»{money}. "
                    f"Осталось {round(max(0, left),1)} шт.")
        except Exception as exc:
            return f"Не получилось продать: {exc}"

    def shelf_produce_keyboard(self, chat: str, page: int = 0, message_id: str = "") -> None:
        """Быстрый приход +1 на позиции с планом пополнения или низким остатком."""
        from .shelf import Shelf
        items = Shelf(self.db).items()
        candidates = [i for i in items if num(i.get("plan_qty")) > 0 or i.get("low") or i.get("status") == "empty"]
        candidates = candidates or items
        if not candidates:
            self._reply(chat, "Стеллаж пуст — сначала добавьте позицию.")
            return
        page_rows, page, total_pages = self._paginate(candidates, page)
        self._prod_page[chat] = page
        lines = ["📥 Приход на стеллаж (+1 шт). Для другого количества — «приход Адресник 5»."]
        buttons = []
        for i in page_rows:
            name = i.get("name") or "позиция"
            lines.append(f"• {name} — {round(num(i.get('qty')),1)} шт"
                         + (f" · нужно +{int(num(i.get('plan_qty')))}" if num(i.get("plan_qty")) else ""))
            buttons.append([{"text": f"+1 · {name[:24]}", "callback_data": f"cmd:shelf-prod:{i['id']}"}])
        if total_pages > 1:
            nav = [{"text": "◀", "callback_data": "cmd:shelf-prod-menu:prev"},
                   {"text": f"{page + 1}/{total_pages}", "callback_data": "cmd:shelf-prod-menu"},
                   {"text": "▶", "callback_data": "cmd:shelf-prod-menu:next"}]
            buttons.append(nav)
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def do_shelf_produce(self, item_id: str, qty: float = 1) -> str:
        from .shelf import Shelf
        item = self.db.one("SELECT * FROM shelf_items WHERE id=? AND active=1", (item_id,))
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
        from .shelf import Shelf
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

    # ============================================================ каталог
    # Каталог — это номенклатура («Товары» в панели), а не стеллаж: карточки,
    # цены, публикация в витрине клиентского бота. Просмотр доступен всем
    # ролям, правки — под группой «catalog» (руководитель и владелец).

    def _nom(self):
        from .nomenclature import Nomenclature
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
            price = f"{_money(i.get('price'))} · " if num(i.get("price")) else "без цены · "
            lines.append(f"• {marks} {i.get('name') or 'Без названия'} — {price}"
                         f"{round(num(i.get('qty')), 1)} {i.get('unit') or 'шт'}{tail}")
        if len(rows) > 8:
            lines.append(f"… ещё {len(rows) - 8} поз. — листайте кнопками.")
        return "\n".join(lines)

    def catalog_menu(self, chat: str, filt: str = "all", page: int = 0,
                     message_id: str = "") -> None:
        """Меню каталога: список с пагинацией, фильтры и кнопки управления."""
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
            page_rows, page, total_pages = self._paginate(rows, page)
            self._cat_page[chat] = page
            for i in page_rows:
                mark = "🗄" if num(i.get("archived")) else \
                    ("👁" if self._is_published(i) else "🙈")
                price = _money(i.get("price")) if num(i.get("price")) else "без цены"
                label = f"{mark} {i.get('name') or 'позиция'} · {price}"[:60]
                buttons.append([{"text": label,
                                 "callback_data": f"cmd:cati:{i['id']}"}])
            if total_pages > 1:
                buttons.append([
                    {"text": "◀", "callback_data": f"cmd:cat:{self._cat_filter[chat]}:{page - 1}"},
                    {"text": f"{page + 1}/{total_pages}", "callback_data": f"cmd:cat:{self._cat_filter[chat]}:{page}"},
                    {"text": "▶", "callback_data": f"cmd:cat:{self._cat_filter[chat]}:{page + 1}"}])
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
        self._send_menu(chat, "\n".join(lines), buttons, message_id)

    def catalog_card(self, chat: str, nom_id: str, message_id: str = "") -> None:
        """Карточка позиции каталога с кнопками правок (по правам роли)."""
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
            lines.append(f"Цена: {_money(item.get('price'))}"
                         + (f" · с/с ~{_money(item.get('cost'))}" if num(item.get("cost")) else ""))
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
            lines.append(f"Маржа: {_money(margin)}"
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

    def _catalog_callback(self, command: str, chat: str, message_id: str = "") -> None:
        """Нажатие кнопок каталога: список, карточка и действия над позицией."""
        parts = command.split(":")
        head = parts[0]
        if head == "cat":
            filt = parts[1] if len(parts) > 1 and parts[1] in ("all", "pub", "hid", "arc") \
                else self._cat_filter.get(chat, "all")
            try:
                page = int(num(parts[2], 0)) if len(parts) > 2 else self._cat_page.get(chat, 0)
            except (TypeError, ValueError):
                page = 0
            return self.catalog_menu(chat, filt=filt, page=page, message_id=message_id)
        if head == "cati" and len(parts) > 1:
            return self.catalog_card(chat, parts[1], message_id)
        if head == "cat-grps":
            if len(parts) > 1 and parts[1] != "list":
                return self.catalog_groups(chat, parts[1], message_id)
            return self._send_menu(
                chat, self._catalog_groups_text(),
                [[{"text": "⬅ К списку", "callback_data": "cmd:cat:all:0"}]], message_id)
        if head == "cat-grp" and len(parts) > 2:
            reply = self.do_catalog_group(parts[1], parts[2])
            self._reply(chat, reply)
            return self.catalog_card(chat, parts[2], message_id)
        if head == "cat-del" and len(parts) > 1:
            item = self.db.one("SELECT name FROM nomenclature WHERE id=?", (parts[1],)) or {}
            return self._send_menu(
                chat, f"🗑 Удалить «{item.get('name') or 'позицию'}»?\n"
                      "Позиция с движениями уйдёт в архив вместо удаления.",
                [[{"text": "✅ Да, удалить", "callback_data": f"cmd:cat-delyes:{parts[1]}"}],
                 [{"text": "⬅ Отмена", "callback_data": f"cmd:cati:{parts[1]}"}]],
                message_id)
        nom_id = parts[1] if len(parts) > 1 else ""
        if head == "cat-hide":
            self._reply(chat, self.do_catalog_publish(nom_id, False))
        elif head == "cat-show":
            self._reply(chat, self.do_catalog_publish(nom_id, True))
        elif head == "cat-archive":
            self._reply(chat, self.do_catalog_archive(nom_id, True))
        elif head == "cat-restore":
            self._reply(chat, self.do_catalog_archive(nom_id, False))
        elif head == "cat-recalc":
            if nom_id == "all":
                return self._recalc_all_confirm(chat, message_id)
            if nom_id == "allgo":
                self._reply(chat, self.do_catalog_recalc_all())
                return self.catalog_menu(chat, message_id=message_id)
            self._reply(chat, self.do_catalog_recalc(nom_id))
            return self.catalog_card(chat, nom_id, message_id)
        elif head == "cat-vitrine":
            enabled = not bool(self.db.setting("client_bot_catalog", True))
            self._reply(chat, self.do_catalog_vitrine(enabled))
        elif head == "cat-delyes" and nom_id:
            self._reply(chat, self.do_catalog_delete(nom_id))
        if nom_id:
            return self.catalog_card(chat, nom_id, message_id)
        return self.catalog_menu(chat, message_id=message_id)

    def _recalc_all_confirm(self, chat: str, message_id: str = "") -> None:
        """Массовый пересчёт цен — только через явное подтверждение."""
        count = len(self._catalog_rows("all"))
        return self._send_menu(
            chat, f"↻ Пересчитать цены всего каталога ({count} поз.) "
                  "от себестоимости?\nИзменённые цены запишутся в историю цен.",
            [[{"text": "✅ Да, пересчитать", "callback_data": "cmd:cat-recalc:allgo"}],
             [{"text": "⬅ Отмена", "callback_data": "cmd:cat:all:0"}]],
            message_id)

    # ------------------------------------------- каталог: текстовые команды
    def _catalog_text(self, chat: str, text: str) -> None:
        """«каталог» — меню; «каталог <слово>» — поиск по каталогу."""
        parts = text.split(maxsplit=1)
        self._cat_query[chat] = parts[1].strip().lower() if len(parts) > 1 else ""
        self._cat_filter[chat] = "all"
        return self.catalog_menu(chat, page=0)

    def _catalog_price_cmd(self, text: str) -> str:
        """«цена <товар> <сумма>» — новая базовая цена с записью в историю."""
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
                + (f"{_money(old)} → {_money(price)}." if old else f"{_money(price)}."))

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
                + (f" · цена {_money(price)}" if price else " · без цены")
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
                + (f". Себестоимость ~{_money(cost)}" if cost > 0 else "") + ".")

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


    def text_shelf_moves(self, limit: int = 12) -> str:
        """Последние движения стеллажа одной лентой."""
        from .shelf import Shelf
        moves = Shelf(self.db).moves(limit=limit)
        if not moves:
            return "Движений стеллажа пока нет."
        labels = {"produce": "приход", "sale": "продажа", "online": "онлайн",
                  "writeoff": "списание", "inventory": "инв."}
        lines = ["🧾 Последние движения стеллажа:"]
        for m in moves:
            q = num(m.get("qty"))
            sign = "+" if q > 0 else ""
            name = m.get("item_name") or "позиция"
            day = str(m.get("at") or "")[:16].replace("T", " ")
            price = num(m.get("price"))
            tail = f" · {_money(price * abs(q))}" if price and q < 0 else ""
            lines.append(f"{day} · {labels.get(m.get('kind'), m.get('kind'))} · "
                         f"{name} {sign}{round(q,1)} шт{tail}")
        return "\n".join(lines)

    def text_shelf_sales(self, days: int = 7) -> str:
        """Что реально продалось со стеллажа за период, по позициям."""
        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self.db.query(
            "SELECT m.item_id, i.name, SUM(-m.qty) qty, SUM(-m.qty*m.price) money"
            " FROM shelf_moves m LEFT JOIN shelf_items i ON i.id=m.item_id"
            " WHERE m.kind IN ('sale','online') AND m.qty<0 AND m.at>=?"
            " GROUP BY m.item_id ORDER BY money DESC", (since,))
        if not rows:
            return f"За {days} дн продаж со стеллажа не было."
        total_qty = sum(num(r.get("qty")) for r in rows)
        total_money = sum(num(r.get("money")) for r in rows)
        lines = [f"📊 Продажи стеллажа за {days} дн: {round(total_qty,1)} шт · {_money(total_money)}"]
        for r in rows[:15]:
            lines.append(f"• {r.get('name') or 'позиция'} — {round(num(r.get('qty')),1)} шт · {_money(r.get('money'))}")
        if len(rows) > 15:
            lines.append(f"… ещё {len(rows) - 15} поз.")
        return "\n".join(lines)

    def _final_status_id(self) -> str | None:
        row = self.db.one("SELECT id FROM statuses WHERE is_final=1 ORDER BY position LIMIT 1")
        return (row or {}).get("id")

    def _fulfill(self, raw: str) -> str:
        """Подтверждаемая выдача: «выдать 1001 оплачен» или «… в долг»."""
        words = raw.lower().replace("ё", "е").split()
        number = next((word for word in words[1:] if word.isdigit()), "")
        if not number:
            return "Укажите номер: «выдать 1001 оплачен» или «выдать 1001 в долг»."
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        paid_confirmed = any(token in words for token in (
            "оплачен", "оплачено", "оплата", "получено", "получена",
        ))
        debt_confirmed = "долг" in words
        due = self.manager.acc.order_economics(order).get("debt", 0)
        if num(due) > 0 and not (paid_confirmed or debt_confirmed):
            return (f"По заказу №{number} осталось {_money(due)}.\n"
                    f"Подтвердите: «выдать {number} оплачен» или «выдать {number} в долг».")
        if num(due) <= 0:
            action = "none"
        else:
            action = "received" if paid_confirmed else "debt"
        method = ("cash" if any("налич" in word for word in words) else
                  "card" if any("карт" in word for word in words) else
                  "transfer" if any("перевод" in word for word in words) else "other")
        from .fulfillment import OrderFulfillment
        from .stock import Stock
        try:
            result = OrderFulfillment(
                self.db, self.manager.repo, Stock(self.db), self.manager.acc
            ).fulfill(
                order["id"],
                handoff_confirmed=True,
                payment_action=action,
                payment_method=method if action == "received" else "",
            )
        except ValueError as exc:
            return f"Не получилось выдать заказ №{number}: {exc}"
        if not result.get("already_fulfilled"):
            client = getattr(self.manager, "client_bot", None)
            link = self.db.one("SELECT * FROM client_orders WHERE order_id=? ORDER BY datetime(created_at) LIMIT 1",
                               (order["id"],))
            if client and link:
                try:
                    client._reply_keyed(link["chat_id"],
                                        f"Заказ №{number} выдан ✓ Спасибо, что выбрали NOZZA!",
                                        client._menu(), dedupe_key=f"delivered:{order['id']}")
                    client._funnel(link["chat_id"], "delivered", source=link.get("source") or "telegram",
                                   order_id=order["id"])
                except Exception:
                    pass
        repeated = " (уже был выдан)" if result.get("already_fulfilled") else ""
        money = (f"получено {_money(result.get('collected'))}"
                 if num(result.get("collected")) > 0 else
                 f"оставлен долг {_money(result.get('debt'))}"
                 if num(result.get("debt")) > 0 else "оплачен ранее")
        return (f"✅ Заказ №{number} выдан{repeated} · {money}.\n\n"
                f"Текст клиенту (не отправлен):\n{result.get('message') or ''}")

    def _client_inbox(self, limit: int = 12) -> str:
        """Короткий inbox для рабочего Telegram-бота."""
        rows = self.db.query(
            "SELECT l.chat_id,l.name,l.text,l.at,c.inbox_status FROM client_bot_log l"
            " LEFT JOIN client_chats c ON c.chat_id=l.chat_id"
            " WHERE l.direction='in' AND l.unread=1 ORDER BY l.id DESC LIMIT ?", (limit,))
        if not rows:
            return "💬 Непрочитанных диалогов нет."
        lines = [f"💬 Непрочитанные диалоги: {len(rows)}", ""]
        for row in rows:
            when = str(row.get("at") or "")[5:16].replace("T", " ")
            status = f" · {row.get('inbox_status')}" if row.get("inbox_status") else ""
            lines.append(f"• чат {row.get('chat_id')} · {row.get('name') or 'клиент'}{status} · {when}")
            lines.append(f"  «{str(row.get('text') or '')[:180]}»")
        lines.append("\nОтвет: «кответ <chat_id> <текст>». После ответа диалог отметьте прочитанным в панели.")
        return "\n".join(lines)

    def _client_payment_action(self, text: str, action: str, actor: str) -> str:
        parts = text.split()
        ident = next((w for w in parts[2:] if w), "")
        if not ident:
            return "Формат: «оплата подтвердить 1001» или укажите id заявки из панели."
        intent = self.db.one(
            "SELECT p.*,o.number FROM client_payment_intents p LEFT JOIN orders o ON o.id=p.order_id"
            " WHERE p.status='pending' AND (p.id=? OR o.number=?) ORDER BY datetime(p.created_at) DESC LIMIT 1",
            (ident, ident))
        if not intent:
            return f"Ожидающая заявка оплаты «{ident}» не найдена."
        if action == "reject":
            self.db.execute("UPDATE client_payment_intents SET status='rejected',reject_reason=?,confirmed_at=?,confirmed_by=?,updated_at=? WHERE id=?",
                            ("Отклонено сотрудником Telegram", now_iso(), actor, now_iso(), intent["id"]))
            client = getattr(self.manager, "client_bot", None)
            if client:
                client._reply_keyed(intent["chat_id"],
                                    f"По заказу №{intent.get('number') or ''} оплату пока не подтвердили. Напишите мастеру, если это ошибка.",
                                    client._menu(), dedupe_key=f"payment:{intent['id']}:rejected")
            result = "Заявка оплаты отклонена."
        else:
            try:
                payment = self.manager.acc.add_payment(
                    intent["order_id"], num(intent.get("amount")), "payment", "",
                    "СБП (ручная сверка)", f"Подтверждено из рабочего Telegram: {intent['id']}",
                    request_id=f"client-intent:{intent['id']}")
            except ValueError as exc:
                return f"Не получилось подтвердить: {exc}"
            self.db.execute("UPDATE client_payment_intents SET status='confirmed',confirmed_at=?,confirmed_by=?,payment_id=?,updated_at=? WHERE id=?",
                            (now_iso(), actor, payment.get("id") or "", now_iso(), intent["id"]))
            client = getattr(self.manager, "client_bot", None)
            if client:
                client._reply_keyed(intent["chat_id"],
                                    f"Оплата по заказу №{intent.get('number') or ''} подтверждена мастером ✓",
                                    client._menu(), dedupe_key=f"payment:{intent['id']}:confirmed")
            result = f"Оплата по заказу №{intent.get('number') or ''} подтверждена и проведена."
        try:
            self.db.execute("INSERT INTO audit_log(at,entity,entity_id,action,title,detail,data) VALUES(?,?,?,?,?,?,?)",
                            (now_iso(), "payment_intent", intent["id"], action,
                             "Заявка оплаты обработана из Telegram", result,
                             json.dumps({"actor": actor}, ensure_ascii=False)))
        except Exception:
            pass
        return result

    def _client_answer(self, raw: str) -> str:
        """«кответ <chat_id> <текст>» — ответить покупателю клиентского бота.

        Уведомления о вопросах приходят с chat_id; ответ уходит от имени
        клиентского бота и попадает в журнал диалогов вкладки «Клиент-бот».
        """
        parts = raw.strip().split(None, 2)
        if len(parts) < 3 or not parts[1].isdigit():
            return ("Формат: «кответ <chat_id> <текст>» — chat_id виден "
                    "в уведомлении о вопросе покупателя.")
        target, message = parts[1], parts[2].strip()
        client = getattr(self.manager, "client_bot", None)
        if not client:
            return "Клиентский бот не запущен — проверьте вкладку «Клиент-бот»."
        row = self.db.one("SELECT * FROM client_chats WHERE chat_id=?", (target,))
        if not row:
            return f"Чат {target} не найден среди покупателей."
        dedupe_key = (f"staff-reply:{self._current_update_id}:{target}"
                      if self._current_update_id else "")
        client._reply_keyed(target, message, client._menu(), dedupe_key=dedupe_key)
        client._log(target, row.get("name") or "", "← мастер", message,
                    kind="answer", direction="out", unread=0, operator=str(self.db.setting("telegram_chat_id", "") or ""))
        self.db.execute("UPDATE client_bot_log SET unread=0 WHERE chat_id=? AND direction='in'", (target,))
        try:
            self.db.execute("INSERT INTO audit_log(at,entity,entity_id,action,title,detail,data) VALUES(?,?,?,?,?,?,?)",
                            (now_iso(), "client_chat", target, "reply", "Ответ покупателю из рабочего Telegram",
                             message[:400], json.dumps({"actor": str(self.db.setting("telegram_chat_id", "") or "")}, ensure_ascii=False)))
        except Exception:
            pass
        self.db.add_event("bot", "Ответ покупателю из бота",
                          f"{row.get('name') or target}", "",
                          {"chat_id": target})
        return f"Отправлено ✓ {row.get('name') or target} · «{message[:60]}»"

    def _client_bot_control(self, text: str) -> str:
        """КБ5: «клиент-бот пауза/старт/статус» — управление витриной с телефона."""
        parts = text.split()
        action = parts[1] if len(parts) > 1 else "статус"
        settings = self.db.settings()
        enabled = bool(settings.get("client_bot_enabled"))
        has_token = bool(settings.get("client_bot_token"))
        if action in ("пауза", "стоп", "выключить", "pause", "stop"):
            self.db.set_settings({"client_bot_enabled": False})
            self.db.add_event("bot", "Клиентский бот выключен из Telegram",
                              "«клиент-бот пауза»", "", {})
            return ("Клиентский бот выключен — покупателям бот не отвечает. "
                    "Включить: «клиент-бот старт».")
        if action in ("старт", "включить", "start"):
            if not has_token:
                return ("Сначала задайте токен клиентского бота в панели: "
                        "Настройки → Клиент-бот.")
            self.db.set_settings({"client_bot_enabled": True})
            self.db.add_event("bot", "Клиентский бот включён из Telegram",
                              "«клиент-бот старт»", "", {})
            return "Клиентский бот включён ✓ Каталог и заявки снова работают."
        bot = getattr(self.manager, "client_bot", None)
        alive = bool(bot and bot.last_poll
                     and time.time() - bot.last_poll < 120)
        return ("Клиентский бот: "
                f"{'включён' if enabled else 'выключен'} · "
                f"опрос {'жив' if alive else 'молчит'}\n"
                "«клиент-бот пауза» — выключить · «клиент-бот старт» — включить.")

    def _client_ban(self, text: str) -> str:
        """КБ6: «клиент блок 555» / «клиент разблок 555» — спам-фильтр чатов."""
        parts = text.split()
        if len(parts) < 3 or not parts[2].isdigit():
            return "Формат: «клиент блок 555» или «клиент разблок 555»."
        action, target = parts[1], parts[2]
        row = self.db.one("SELECT * FROM client_chats WHERE chat_id=?", (target,))
        if not row:
            return f"Чат {target} не найден среди покупателей."
        if action in ("блок", "бан", "блокировка"):
            self.db.execute("UPDATE client_chats SET banned=1 WHERE chat_id=?",
                            (target,))
            self.db.add_event("bot", "Чат покупателя заблокирован",
                              str(row.get("name") or target), "",
                              {"chat_id": target})
            return (f"Чат {target} заблокирован: бот молча игнорирует "
                    "сообщения и не шлёт уведомления. Разблок: "
                    f"«клиент разблок {target}».")
        self.db.execute("UPDATE client_chats SET banned=0 WHERE chat_id=?",
                        (target,))
        self.db.add_event("bot", "Чат покупателя разблокирован",
                          str(row.get("name") or target), "",
                          {"chat_id": target})
        return f"Чат {target} разблокирован — бот снова отвечает."

    def _review_answer(self, raw: str) -> str:
        """КБ4: «отзыв ответ 555 <текст>» — ответ покупателю на плохой отзыв."""
        parts = raw.strip().split(None, 3)
        if len(parts) < 4 or not parts[2].isdigit():
            return ("Формат: «отзыв ответ 555 <текст>» — chat_id из "
                    "уведомления об отзыве.")
        target, message = parts[2], parts[3].strip()
        client = getattr(self.manager, "client_bot", None)
        if not client:
            return "Клиентский бот не запущен — ответьте в панели (Отзывы)."
        review = self.db.one(
            "SELECT * FROM client_reviews WHERE chat_id=?"
            " AND state IN ('needs_attention','rated')"
            " ORDER BY datetime(COALESCE(created_at, asked_at)) DESC LIMIT 1",
            (target,))
        if not review:
            return f"У чата {target} нет отзыва, ждущего ответа."
        order = self.db.one("SELECT number FROM orders WHERE id=?",
                            (review.get("order_id") or "",)) or {}
        dedupe = (f"reviewreply:{review['order_id']}:{self._current_update_id}"
                  if self._current_update_id else "")
        client._reply_keyed(target, message, client._menu(), dedupe_key=dedupe)
        row = self.db.one("SELECT name FROM client_chats WHERE chat_id=?",
                          (target,))
        client._log(target, (row or {}).get("name") or "", "← ответ на отзыв",
                    message, kind="answer", direction="out", unread=0,
                    operator=str(self.db.setting("telegram_chat_id", "") or ""))
        self.db.execute(
            "UPDATE client_reviews SET state='answered',operator_note=?,"
            "resolved_at=? WHERE order_id=? AND chat_id=?",
            (message[:500], now_iso(), review["order_id"], target))
        self.db.add_event("order", "Ответ на отзыв покупателю",
                          f"№{order.get('number') or ''}", message[:200],
                          {"chat_id": target})
        return (f"Отправлено ✓ Ответ на отзыв ушёл покупателю "
                f"{(row or {}).get('name') or target}.")

    def _client_template_button(self, command: str) -> str:
        """КБ2: кнопка-шаблон из уведомления — готовый ответ покупателю."""
        parts = command.split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            return "Кнопка устарела — напишите «кответ <chat> <текст>»."
        target, template_id = parts[1], parts[2]
        client = getattr(self.manager, "client_bot", None)
        if not client:
            return "Клиентский бот не запущен."
        own = [item for item in client.templates() if item.get("enabled", True)]
        template = next((item for item in own if item["id"] == template_id), None)
        if not template:
            template = next((item for item in client.default_templates()
                             if item["id"] == template_id), None)
        if not template:
            return f"Шаблон удалён — напишите «кответ {target} <текст>»."
        if bool(num((self.db.one(
                "SELECT banned n FROM client_chats WHERE chat_id=?",
                (target,)) or {}).get("n"))):
            return f"Чат {target} заблокирован — сначала «клиент разблок {target}»."
        message = str(template.get("text") or "")
        dedupe = (f"tpl:{self._current_update_id}:{target}:{template_id}"
                  if self._current_update_id else "")
        client._reply_keyed(target, message, client._menu(), dedupe_key=dedupe)
        row = self.db.one("SELECT name FROM client_chats WHERE chat_id=?",
                          (target,))
        client._log(target, (row or {}).get("name") or "",
                    f"← шаблон «{template['name']}»", message, kind="answer",
                    direction="out", unread=0,
                    operator=str(self.db.setting("telegram_chat_id", "") or ""))
        self.db.execute("UPDATE client_bot_log SET unread=0"
                        " WHERE chat_id=? AND direction='in'", (target,))
        return (f"Отправлено ✓ «{template['name']}» → "
                f"{(row or {}).get('name') or target}")

    def _pay(self, text: str) -> str:
        import re as _re
        amount_m = _re.search(r"(\d[\d\s.,]*)\s*(?:р|руб|₽)?\s*по\s*(\d+)", text.lower())
        if not amount_m:
            amount_m = _re.search(r"по\s*(\d+).*?(\d[\d\s.,]*)\s*(?:р|руб|₽)?", text.lower())
        if not amount_m:
            return "Формат: «оплата 1500 по 1001»."
        amount = num(amount_m.group(1).replace(" ", "").replace(",", "."))
        number = amount_m.group(2)
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        if amount <= 0:
            return "Сумма должна быть больше нуля."
        try:
            self.manager.acc.add_payment(
                order["id"], amount, "payment",
                order.get("account_id") or "", "other", "оплата из Telegram",
                request_id=(f"staff-tg-payment:{self._current_update_id}"
                            if self._current_update_id else ""),
            )
        except ValueError as exc:
            return f"Не получилось записать оплату: {exc}"
        left = max(0.0, num(order.get("price")) -
                   (max(num(order.get("paid")), num(order.get("prepaid"))) + amount))
        return f"💰 Принято {_money(amount)} по заказу №{number}." + \
            (f" Осталось {_money(left)}." if left > 0 else " Оплачен полностью.")

    def _set_status(self, text: str) -> str:
        parts = text.split()
        number = next((w for w in parts[1:] if w.isdigit()), "")
        if not number:
            return "Формат: «статус 1001 печать»."
        target = next((w for w in parts[1:] if w.isalpha() and not w.isdigit()), "")
        if not target:
            return "Укажите статус: «статус 1001 печать»."
        order = self.db.one("SELECT * FROM orders WHERE number=?", (number,))
        if not order:
            return f"Заказ №{number} не найден."
        status = self.db.one(
            "SELECT id FROM statuses WHERE pylower(name) LIKE ? LIMIT 1",
            (f"%{target}%",))
        if not status:
            return f"Статус «{target}» не найден."
        self.db.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?",
                        (status["id"], now_iso(), order["id"]))
        self.db.add_event("order", "Статус изменён (Telegram)",
                          f"№{number} → {status['id']}", data={"order_id": order["id"]})
        return f"✅ Заказ №{number} → «{status['id']}»."

    def _parse_new_order(self, text: str) -> dict:
        """Единый локальный парсер для Telegram и панели «Заказ из текста»."""
        from .order_intake import parse_order_text
        parsed = parse_order_text(text)
        return {**parsed, "client": parsed.get("client", "")}

    def _new_order(self, text: str) -> str:
        from .order_intake import OrderIntake
        preview = OrderIntake(self.db).preview(text, "telegram")
        parsed = preview["parsed"]
        draft = preview["draft"]
        if not draft["product"]:
            return "Формат: «новый адресник 2шт 900р Мария»."
        if self._current_update_id:
            draft["client_request_id"] = f"staff-tg:{self._current_update_id}"
        order = self.manager.repo.save_order(draft)
        return (f"📝 Создан заказ №{order.get('number')} «{draft['product']}»"
                + (f" · {int(parsed['qty'])} шт" if parsed["qty"] > 1 else "")
                + (f" · {_money(draft['price'])}" if draft["price"] else "")
                + (f" · {draft['customer_name']}" if draft["customer_name"] else ""))

    # -------------------------------------------------------------- команды
    def _pick_printer(self, chat: str = ""):
        """Выбранный принтер чата или первый подключённый (мультипринтер)."""
        printer_id = self._printer_choice.get(chat, "") if chat else ""
        if printer_id:
            printer = self.manager.get(printer_id)
            if printer and printer.connected:
                return printer
        return self.manager.get()

    def _list_printers(self, chat: str) -> str:
        """Список принтеров с номерами для выбора команды «принтер N»."""
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        if not printers:
            return "Принтеры не добавлены."
        chosen = self._printer_choice.get(chat, "")
        lines = ["🖨 Принтеры (выбор: «принтер N»):"]
        for i, snap in enumerate(printers, 1):
            info = snap["printer"]
            mark = "◉" if snap["id"] == chosen else "·"
            lines.append(f"{mark} {i}. {snap['name']} — "
                         f"{STATE_RU.get(info['state'], info.get('state_label') or info['state'])}"
                         + (" · нет связи" if not snap["connection"]["connected"] else ""))
        return "\n".join(lines)

    def _select_printer(self, chat: str, index: int) -> str:
        state = self.manager.snapshot()
        printers = state.get("printers") or []
        if not printers:
            return "Принтеры не добавлены."
        if index < 1 or index > len(printers):
            return f"Номер от 1 до {len(printers)}."
        snap = printers[index - 1]
        self._printer_choice[chat] = snap["id"]
        return f"◉ Выбран {snap['name']}. Команды (пауза/кадр/свет…) теперь идут на него."

    def do_command(self, command: str, ok_text: str, value=None, chat: str = "") -> str:
        printer = self._pick_printer(chat)
        if not printer:
            return "Принтеры не добавлены."
        if not printer.connected:
            return f"{printer.record.get('name', 'Принтер')} не на связи."
        if command == "pause":
            self.manager.mark_user_paused(printer.id)
        printer.command(command, value)
        if command == "resume":
            self.manager.clear_user_paused(printer.id)
        self.db.add_event("command", f"Telegram: {command}", ok_text, printer.id, {})
        return f"{ok_text} — {printer.record.get('name', 'принтер')}."

    def do_stop(self, chat: str, text: str) -> str:
        """Остановка печати — только с подтверждением: случайное «стоп» дорого стоит."""
        confirmed = any(word in text for word in ("да", "точно", "подтверждаю"))
        pending = self._pending_stop.get(chat, 0)
        if confirmed or time.time() - pending < 120:
            self._pending_stop.pop(chat, None)
            return self.do_command("stop", "Печать остановлена")
        self._pending_stop[chat] = time.time()
        return ("Остановить печать? Задание прервётся, деталь придётся печатать заново.\n"
                "Для подтверждения напишите: стоп да")
