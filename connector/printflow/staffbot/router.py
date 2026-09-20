"""Роутер бота — таблицы команд, без бизнеса.

Тонкий бот: все команды ведут в меню с web_app. Старые слова оставлены как
алиасы, чтобы не ломать UX, но метод один — cmd_menu / cmd_help / cmd_code.
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


# Все текстовые команды ведут в меню — бот notify_only
TEXT_COMMANDS: tuple[Command, ...] = (
    Command(("start", "help", "старт", "помощь", "меню", "?", "панель", "panel", "цех", "staff"), "view", "cmd_menu"),
    Command(("more", "еще", "ещё"), "view", "cmd_menu"),
    Command(("код", "code", "id", "мой код", "myid"), "view", "cmd_code"),
    Command(("план", "очередь", "queue", "статус", "status", "принтер", "принтеры", "датчики", "доктор", "кадр", "камера", "живой", "live", "стоп-живой"), "view", "cmd_menu"),
    Command(("стеллаж", "полка", "продажа", "продать", "приход", "касса", "забрали", "деньги", "сегодня", "итоги", "график", "долги", "брак", "рейтинг", "филамент", "пластик", "каталог", "цена", "группы", "заказ", "выдать", "оплата", "чаты", "кответ", "команда", "сотрудники", "пригласить"), "view", "cmd_menu"),
    Command(("закрыть месяц", "движения стеллаж", "продажи стеллаж", "оплата подтвердить", "отзыв ответ"), "view", "cmd_menu", phrase=True),
)

CALLBACKS: tuple[Route, ...] = (
    Route("menu", "view", "cb_menu"),
    Route("help", "view", "cb_help"),
    Route("code", "view", "cb_code"),
    Route("open", "view", "cb_menu"),
    Route("panel", "view", "cb_menu", kind="text"),
    Route("printers", "view", "cb_menu", kind="text"),
    Route("queue", "view", "cb_menu", kind="text"),
    Route("shelf", "view", "cb_menu", kind="text"),
    Route("money", "view", "cb_menu", kind="text"),
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
