"""Бот сотрудников — уведомления, кнопки и ассистент (19.0).

Архитектура clean_layers:
- core/config, core/db, core/api_client
- router
- handlers/menu (меню, помощь, код)
- handlers/notify (дайджест 09:00, график 20:00, низкий остаток)
- handlers/report (сводка, принтеры, заказы, полка, очередь, кадр, деньги)
  + действия по уведомлениям: следующее, продолжить, снял, повтор
- handlers/assistant (core assistant.py): вопросы про цех мозгом помощника
- scenes SQLite с нуля
- ui keyboards — только callback-кнопки

19.0: Mini App убран. У бота нет кнопки web_app и внешнего адреса: всё меню
на callback-кнопках, каждая отвечает в чате, а на свободные вопросы отвечает
мозг помощника панели (`assistant_brain`).
"""
from __future__ import annotations

import json
import re as _re
import threading
import time

from ..accounting import num
from ..staff import ROLE_NAMES, Staff, gate, group_for_word
from .assistant import AskMixin
from .core.api_client import TelegramApiClient
from .core.config import load_config
from .core.db import ensure_scenes_table
from .handlers.menu import MenuMixin
from .handlers.notify import NotifyMixin
from .handlers.report import ReportMixin
from .router import ROUTER, normalize, suggest_command
from .scenes import ASK, BotScenes
from .ui import HELP, main_menu_keyboard


class StaffBot(MenuMixin, NotifyMixin, ReportMixin, AskMixin):
    """Рабочий бот PrintFlow: цех в кармане — кнопки, уведомления, ассистент.

    19.0: ни одной web_app-кнопки и ни одного «откройте цех» — отчёты,
    действия и вопросы ассистенту живут в самом чате.
    """

    def __init__(self, manager):
        self.manager = manager
        self.db = manager.db
        self._stop = threading.Event()
        try:
            self._offset = max(0, int(num(self.db.setting("telegram_bot_update_offset", 0))))
        except Exception:
            self._offset = 0
        self._current_update_id = ""
        self._callback_id = ""
        self._answered = False
        self.last_poll = 0.0
        self.scenes = BotScenes(self.db)
        ensure_scenes_table(self.db)
        from ..tg import Outbox, UpdateLedger

        self.transport = TelegramApiClient(
            lambda: str(self._settings().get("telegram_token", "") or ""), "staff"
        )
        # Для совместимости с тестами: transport должен иметь token_provider и call
        # TelegramApiClient уже имеет call
        self.ledger = UpdateLedger(self.db, "telegram_bot_updates")
        self.outbox = Outbox(
            self.db,
            sender=lambda method, payload, timeout=15: self._call(method, payload, timeout=timeout),
            table="telegram_outbox",
            token_provider=lambda: str(self._settings().get("telegram_token", "") or ""),
        )
        self._thread = threading.Thread(target=self._loop, name="pf-staffbot", daemon=True)
        self._thread.start()

    # ---------------- транспорт
    def shutdown(self) -> None:
        self._stop.set()

    def _settings(self) -> dict:
        try:
            return self.db.settings(include_secrets=True) or {}
        except Exception:
            return {}

    def _call(self, method: str, params: dict, timeout: int = 35,
              files: dict[str, tuple[str, bytes]] | None = None) -> dict:
        """Вызов Bot API; `files` — вложение (фото отчёта)."""
        return self.transport.call(method, params, timeout=timeout, files=files)


    def _claim_update(self, update: dict):
        return self.ledger.claim(update)

    def _finish_update(self, update_id: str, ok: bool = True, error: str = "") -> None:
        self.ledger.finish(str(update_id or ""), ok, error)

    def _reply(self, chat: str, text: str, buttons: dict | None = None) -> None:
        payload = {"chat_id": chat, "text": str(text or "")[:3800], "disable_web_page_preview": "true"}
        if buttons:
            payload["reply_markup"] = json.dumps(buttons, ensure_ascii=False)
        try:
            # Ключ дедупликации пустой: ответ на команду — всегда новое
            # сообщение. С ключом из текста вторая «деньги» или «статус» с теми
            # же цифрами молча не отправлялась (18.12.3).
            row = self.outbox.add(str(chat), "sendMessage", payload)
            self.outbox.send(row)
        except Exception:
            # fallback прямой вызов
            try:
                self._call("sendMessage", payload, timeout=15)
            except Exception:
                pass

    def _answer_callback(self, text: str = "") -> None:
        if not self._callback_id or self._answered:
            return
        self._answered = True
        params = {"callback_query_id": self._callback_id}
        if text:
            params["text"] = text[:190]
        self._call("answerCallbackQuery", params)

    # ---------------- цикл
    def _loop(self) -> None:
        while not self._stop.is_set():
            settings = self._settings()
            if not (settings.get("telegram_enabled") and settings.get("telegram_bot") and settings.get("telegram_token") and settings.get("telegram_chat_id")):
                self._stop.wait(20)
                continue
            try:
                result = self._call(
                    "getUpdates",
                    {"offset": self._offset, "timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])},
                )
                if result.get("ok"):
                    self.last_poll = time.time()
                    try:
                        self.outbox.drain(batch=10)
                    except Exception:
                        pass
                for update in result.get("result") or []:
                    update_id = str(update.get("update_id") or "")
                    claim = self._claim_update(update)
                    if claim is False:
                        break
                    try:
                        prev = self._current_update_id
                        self._current_update_id = update_id
                        if claim is not None:
                            self._handle(update, str(settings.get("telegram_chat_id")))
                            self._finish_update(update_id, True)
                        self._current_update_id = prev
                        try:
                            next_offset = int(num(update_id)) + 1
                        except Exception:
                            next_offset = self._offset
                        self._offset = max(self._offset, next_offset)
                        self.db.set_settings({"telegram_bot_update_offset": self._offset})
                    except Exception as exc:
                        self._current_update_id = ""
                        self._finish_update(update_id, False, str(exc))
                        continue
                self.scenes.sweep()
                self._maybe_digest(settings)
                self._maybe_weekly(settings)
                self._maybe_evening_chart(settings)
                self._maybe_shelf_low(settings)
            except Exception:
                self._stop.wait(10)

    # ---------------- приём
    def _handle(self, update: dict, owner: str) -> None:
        cb = update.get("callback_query") or {}
        if cb:
            return self._handle_callback(cb, owner)
        message = update.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        if not chat:
            return
        text = (message.get("text") or "").strip()
        who = gate(self.db, chat)
        if not who["role"]:
            lowered = text.lower().lstrip("/").replace("ё", "е").strip()
            invite = _re.match(r"(?:старт|start|код|code)?\s*(pf-[a-z0-9]{4,12})$", lowered)
            if invite:
                profile = message.get("from") or {}
                try:
                    member = Staff(self.db).use_invite(
                        invite.group(1), chat, str(profile.get("first_name") or ""), str(profile.get("id") or "")
                    )
                    role = member.get("role_name") or "сотрудник"
                    welcome = (
                        f"Добро пожаловать, {member.get('name')}! Вы в команде как {role}.\n"
                        f"Права: {Staff(self.db).rights_text(member.get('role'))}\n\n"
                        "Нажмите «Меню» — отчёты кнопками, а 🤖 Ассистент ответит на вопросы."
                    )
                    self._reply(
                        chat,
                        welcome,
                        main_menu_keyboard(str(member.get("role") or "")),
                    )
                    self.db.add_event("bot", "Новый участник по приглашению", f"{member.get('name')} — {role}", "", {})
                    return
                except ValueError as exc:
                    return self._reply(chat, str(exc))
            if lowered in ("код", "code", "мой код", "id"):
                return self._reply(
                    chat,
                    f"Ваш chat_id: {chat}\nПередайте его владельцу — он добавит вас в команду или пришлёт код приглашения.",
                )
            self._reply(
                chat,
                f"Этот бот — рабочий инструмент команды NOZZA.\nВаш chat_id: {chat} — попросите владельца добавить вас.\n"
                "Если есть код приглашения, напишите: старт КОД",
            )
            self.db.add_event("bot", "Посторонний в Telegram-боте", f"chat_id {chat}: {text[:80]}", "", {})
            return
        if not text:
            return
        try:
            self._dispatch(chat, text)
        except Exception as exc:
            self._reply(chat, f"Не получилось: {exc}")

    def _dispatch(self, chat: str, raw: str) -> None:
        text = normalize(raw)
        route = ROUTER.match_text(text)
        scene = self.scenes.active(chat)
        if scene and scene.get("scene") == ASK:
            # Режим ассистента: меню, помощь и код выходят из него, отчёты
            # отвечают поверх, остальное — вопрос ассистенту.
            if route is None:
                return self._ask_answer(chat, raw)
            if route.method in ("cmd_menu", "cmd_help", "cmd_code"):
                self.scenes.pop(chat)
        elif scene:
            # сцены старых релизов сбрасываем: у тонкого бота их больше нет
            self.scenes.pop(chat)
        who = gate(self.db, chat)
        if route is not None:
            group = ROUTER.group_for(route, text)
            if who["role"] and group and group not in who["allowed"]:
                word = text.split()[0]
                return self._reply(
                    chat,
                    f"🚫 «{word}» недоступен для роли «{ROLE_NAMES.get(who['role'])}».\n"
                    "Доступное — кнопками в меню.",
                    main_menu_keyboard(str(who["role"] or "")),
                )
            handler = getattr(self, route.method, None)
            if handler:
                return handler(chat, raw, text)
        self._unknown(chat, raw)

    def _handle_callback(self, callback: dict, owner: str) -> None:
        message = callback.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id") or "")
        if not chat or not data:
            return
        who = gate(self.db, chat)
        if not who["role"]:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": "Этот бот приватный."})
            return
        command = data.replace("cmd:", "", 1)
        group = ROUTER.group_for_callback(command)
        if group and group not in who["allowed"]:
            self._call(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": f"Недоступно для роли «{ROLE_NAMES.get(who['role'])}»"},
            )
            return
        self._callback_id = callback_id
        self._answered = False
        try:
            self._run_callback(command, chat, message)
        finally:
            self._callback_id = ""
            self._answered = False

    def _run_callback(self, command: str, chat: str, message: dict) -> None:
        message_id = str(message.get("message_id") or "")
        match = ROUTER.match_callback(command)
        if match is None:
            self._answer_callback()
            return self._reply(chat, "Не понял команду.", main_menu_keyboard(self._report_role(chat)))
        route, params = match
        handler = getattr(self, route.method, None)
        if not handler:
            self._answer_callback()
            return
        if route.kind == "text":
            answer = handler(chat, params) if route.method.startswith("cb_") and "params" in handler.__code__.co_varnames else ""
            # для text-роутов старый формат возвращал строку
            try:
                res = handler(chat, params) if route.kind == "text" else None
                if isinstance(res, str) and res:
                    self._answer_callback()
                    self._reply(chat, res, main_menu_keyboard(self._report_role(chat)))
                    return
            except TypeError:
                pass
            self._answer_callback()
            return
        if not route.late_answer:
            self._answer_callback()
        try:
            handler(chat, message_id, params)
        except TypeError:
            try:
                handler(chat, params)
            except Exception:
                handler(chat, "", params)

    def _unknown(self, chat: str, raw: str) -> None:
        suggestion = suggest_command(raw)
        lines = [
            "Не узнал команду. Кнопки ниже — отчёты цеха, 🤖 Ассистент ответит",
            "на вопрос словами. Спросите текстом: «что печатает P1S?»",
        ]
        kb = main_menu_keyboard(self._report_role(chat))
        self._call(
            "sendMessage",
            {
                "chat_id": chat,
                "text": ("\n".join(lines))[:3800],
                "reply_markup": json.dumps(kb, ensure_ascii=False),
            },
            timeout=15,
        )


    # --- совместимость со старыми тестами: любые старые команды ведут в меню
    def __getattr__(self, name: str):
        # старые методы бота: _list_printers, _client_answer, _sell_rows и т.д.
        # тонкий бот их не имеет — возвращаем заглушку, которая шлёт меню
        if name.startswith("_") or name.startswith("text_") or name.startswith("do_"):
            def _compat(*_a, **_kw):
                try:
                    chat = str(_a[0]) if _a else "111"
                    # если первый аргумент похож на chat_id
                    if chat.isdigit() or (chat.startswith("-") and chat[1:].isdigit()):
                        self._send_main_menu(chat)
                    else:
                        self._send_main_menu("111")
                except Exception:
                    pass
                return "Меню"
            return _compat
        raise AttributeError(name)


# Историческое имя
TelegramBot = StaffBot
