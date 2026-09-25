"""Роутер бота — таблицы команд, без бизнеса.

19.0: Mini App убран. Каждое слово и каждая кнопка имеют своего обработчика
в чате: отчёты, действия по уведомлениям и ассистент. Витринные слова
(продажа, закрыть месяц) отвечают меню — их действия живут в панели.
"""
from __future__ import annotations

import re as _re
from dataclasses import dataclass
from typing import Callable

from .ui import REPLY_ALIASES

Group = str | Callable[[str, str], str]


@dataclass(frozen=True)
class Command:
    words: tuple[str, ...]
    group: Group
    method: str
    phrase: bool = False


@dataclass(frozen=True)
class Route:
    prefix: str
    group: Group
    method: str
    kind: str = "handler"  # handler | text
    late_answer: bool = False


def normalize(raw: str) -> str:
    text = str(raw or "").lower().lstrip("/").replace("ё", "е")
    text = REPLY_ALIASES.get(text.strip(), text)
    for em in "🖨📷≡₽⚑🛍🛒📦📊⚠❔▦▤·🗂🏭":
        text = text.replace(em, " ")
    text = _re.sub(r"\s+", " ", text).strip()
    if text.startswith("стоп") and "живой" in text:
        tail = _re.sub(r"\s*живой\s*", " ", text[4:])
        text = _re.sub(r"\s+", " ", f"стоп-живой {tail}").strip()
    return text


# Все команды отвечают в чате — кнопок web_app и внешних адресов больше нет
TEXT_COMMANDS: tuple[Command, ...] = (
    Command(("start", "help", "старт", "помощь", "меню", "?", "панель", "panel", "цех", "staff"), "view", "cmd_menu"),
    Command(("more", "еще", "ещё"), "view", "cmd_menu"),
    Command(("код", "code", "id", "мой код", "myid"), "view", "cmd_code"),
    # Текстовые отчёты: ответ приходит в чат словами и фото.
    Command(("статус", "status", "сводка", "доктор"), "view", "cmd_status"),
    Command(("принтер", "принтеры", "датчики", "сенсоры", "ams"), "view", "cmd_printers"),
    Command(("план", "очередь", "queue"), "view", "cmd_queue"),
    Command(("заказ", "заказы"), "view", "cmd_orders"),
    Command(("полка", "стеллаж", "остатки"), "view", "cmd_shelf"),
    Command(("деньги", "касса", "итоги", "сегодня", "продажи", "выручка"), "view", "cmd_money"),
    Command(("кадр", "камера", "фото", "живой", "live", "стоп-живой"), "view", "cmd_frame"),
    # Действия по уведомлениям (19.0): те же ответы, что у кнопок.
    Command(("следующее", "дальше", "next"), "view", "cmd_next"),
    Command(("повторить", "повтор", "reprint"), "view", "cmd_reprint"),
    Command(("продолжить", "resume"), "printers", "cmd_resume"),
    Command(("снял", "снято", "removed"), "view", "cmd_removed"),
    # Ассистент (19.0): вопросы про цех — деньгами видны только руководству,
    # поэтому группа finance, как у «денег».
    Command(("ассистент", "ask", "помощник", "спросить"), "finance", "cmd_ask"),
    Command(("как дела", "что там"), "view", "cmd_status", phrase=True),
    Command(("продажа", "продать", "приход", "забрали", "график", "долги", "брак", "рейтинг", "филамент", "пластик", "каталог", "цена", "группы", "выдать", "оплата", "чаты", "кответ", "команда", "сотрудники", "пригласить"), "view", "cmd_menu"),
    Command(("закрыть месяц", "движения стеллаж", "продажи стеллаж", "оплата подтвердить", "отзыв ответ"), "view", "cmd_menu", phrase=True),
)

CALLBACKS: tuple[Route, ...] = (
    Route("menu", "view", "cb_menu"),
    Route("help", "view", "cb_help"),
    Route("code", "view", "cb_code"),
    Route("open", "view", "cb_menu"),
    Route("panel", "view", "cb_menu", kind="text"),
    # Кнопки отчётов: те же ответы, что у слов.
    Route("status", "view", "cb_status"),
    Route("printers", "view", "cb_printers"),
    Route("queue", "view", "cb_queue"),
    Route("orders", "view", "cb_orders"),
    Route("shelf", "view", "cb_shelf"),
    Route("frame", "view", "cb_frame"),
    Route("money", "view", "cb_money"),
    # Действия из уведомлений (19.0).
    Route("next", "view", "cb_next"),
    Route("resume", "printers", "cb_resume"),
    Route("removed", "view", "cb_removed"),
    Route("reprint", "view", "cb_reprint"),
    # Ассистент (19.0).
    Route("ask", "finance", "cb_ask"),
    Route("sug", "finance", "cb_sug", late_answer=True),
    Route("today", "view", "cb_menu", kind="text"),
    Route("inbox", "view", "cb_menu", kind="text"),
    Route("team", "view", "cb_menu", kind="text"),
    Route("goto", "view", "cb_goto", late_answer=True),
)


class Router:
    def __init__(self) -> None:
        self._by_word: dict[str, Command] = {}
        self._phrases: list[Command] = []
        for cmd in TEXT_COMMANDS:
            if cmd.phrase:
                self._phrases.append(cmd)
            else:
                for w in cmd.words:
                    self._by_word[w] = cmd
        self._phrases.sort(key=lambda c: -len(c.words[0]))
        self._callbacks: list[Route] = sorted(CALLBACKS, key=lambda r: -len(r.prefix))
        self._callback_groups = {r.prefix: r.group for r in CALLBACKS}

    def match_text(self, text: str) -> Command | None:
        if not text:
            return None
        for cmd in self._phrases:
            if text.startswith(cmd.words[0]):
                return cmd
        word = text.split()[0]
        return self._by_word.get(word)

    def group_for(self, route: Command | Route, text: str) -> str:
        g = route.group
        if callable(g):
            return g(text.split()[0] if text else "", text)
        return g

    def match_callback(self, command: str) -> tuple[Route, str] | None:
        for route in self._callbacks:
            if command == route.prefix:
                return route, ""
            if command.startswith(route.prefix + ":"):
                return route, command[len(route.prefix) + 1 :]
        return None

    def group_for_callback(self, command: str) -> str:
        for prefix in sorted(self._callback_groups, key=len, reverse=True):
            if command == prefix or command.startswith(prefix + ":"):
                g = self._callback_groups[prefix]
                return g(command.split(":", 1)[0], command) if callable(g) else g
        return "view"


ROUTER = Router()


def suggest_command(raw: str) -> str:
    t = normalize(raw)
    if not t:
        return ""
    return "меню"
