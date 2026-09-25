"""Ассистент в рабочем боте (19.0): вопросы про цех — словами, ответ — в чат.

Зачем так. Mini App убран: единственным «окном» в цех с телефона остался чат.
Кнопки закрывают отчёты (статус, принтеры, заказы, полка, очередь, кадр,
деньги), но вопросы вроде «кто нам должен?» или «что печатает Альфа?» кнопкой
не спросишь. Отвечает тот же мозг помощника, что и в панели
(`assistant_brain.chat`): правила, память, факты базы — и модель, если она
включена. Бот не тащит модель к себе: он передаёт фразу и пересказывает ответ.

Границы — те же, что у голосового канала панели:

* `delegate=False` — команды компьютеру из чата не исполняются: у агента свои
  подтверждения на экране компьютера, из телефона ими распоряжаться нельзя;
* действие с деньгами или печатью (`kind="action"`) не выполняется из чата —
  бот показывает, что предлагают, и просит подтвердить в панели;
* вопросы про деньги и клиентов видит владелец и руководитель — те же роли,
  которым в боте доступны «деньги». Сотруднику ассистент не показывается.

Разговор помнит контекст: сессия `tg:<chat_id>` в `assistant_dialog`, как у
панели — свои сессии. «А у второго?» после вопроса про P1S работает.
"""
from __future__ import annotations

from typing import Any

from ..staff import gate
from .scenes import ASK
from .ui import ask_keyboard, markup_or_none

# Сколько держим режим вопроса открытым: после этого бот честно начинает заново.
ASK_SCENE_TTL = 1800
# Подсказок под ответом — не больше трёх: чат не лента виджетов.
MAX_SUGGESTIONS = 3

WELCOME = (
    "🤖 Ассистент цеха на связи. Спросите что угодно — отвечу по фактам базы:\n"
    "«что печатает P1S» · «кто нам должен» · «сколько заказов в работе» · "
    "«деньги за месяц» · «что на полке заканчивается».\n\n"
    "Напишите вопрос следующим сообщением. «Меню» — выйти."
)


class AssistantApi:
    """Мост «бот → мозг помощника»: те же атрибуты, что у Api панели.

    Мозг читает `api.db`, `api.manager` (снимок парка), `api.acc`, `api.repo`.
    Чего у менеджера нет (`planner`, `shelf`, `insights`), у мозга честно
    пропадает соответствующий факт — сборщики фактов оборачивают каждый доступ
    в `_safe` и продолжают без него.
    """

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.db = getattr(manager, "db", None)
        self.acc = getattr(manager, "acc", None)
        self.repo = getattr(manager, "repo", None)


class AskMixin:
    """Режим вопроса: кнопка «🤖 Ассистент» и свободные фразы."""

    # ------------------------------------------------------------ роли
    def _gate(self, chat: str) -> dict:
        try:
            return gate(self.db, str(chat)) or {}
        except Exception:
            return {}

    def _ask_allowed(self, chat: str) -> bool:
        return self._gate(chat).get("role") in ("owner", "manager")

    # ------------------------------------------------------------ сцена
    def _ask_start(self, chat: str) -> None:
        self.scenes.set(chat, ASK, {}, ttl=ASK_SCENE_TTL)
        self._reply(chat, WELCOME, ask_keyboard())

    def _ask_stop(self, chat: str, note: str = "🏠 Меню цеха.") -> None:
        self.scenes.pop(chat)
        self._reply(chat, note, self._report_keyboard(chat))

    # ------------------------------------------------------------ ответ мозга
    def _ask_answer(self, chat: str, question: str) -> None:
        """Фраза → мозг помощника → ответ в чат текстом и кнопками."""
        clean = " ".join(str(question or "").split())[:1000]
        if not clean:
            self._ask_start(chat)
            return
        try:
            from .. import assistant_brain as brain

            answer = brain.chat(
                AssistantApi(self.manager), clean,
                session=f"tg:{chat}", source="telegram", delegate=False,
            )
        except Exception as exc:
            self.scenes.pop(chat)
            self._reply(
                chat,
                f"🤖 Не получилось спросить помощника: {exc}.\n"
                "Отчёты — кнопками ниже.",
                self._report_keyboard(chat),
            )
            return
        text = str(answer.get("reply") or answer.get("answer") or "").strip()
        action = answer.get("action") or {}
        if action:
            # Действие с печатью или деньгами из чата не выполняется:
            # подтверждение — только человек в панели.
            explain = str(answer.get("explain") or action.get("title") or "").strip()
            text = (text + ("\n\n" if text else "") +
                    "⚠ Это действие из чата не выполняю — подтвердите его в панели"
                    + (f": {explain}" if explain else "."))
        self._reply(chat, text or "Пустой ответ — попробуйте переспросить.",
                    self._report_keyboard(chat))
        tips = [str(s).strip() for s in (answer.get("suggestions") or []) if str(s).strip()]
        tips = tips[:MAX_SUGGESTIONS]
        if tips:
            # Вопросы подсказок лежат в сцене: callback остаётся коротким.
            self.scenes.set(chat, ASK, {"suggestions": tips}, ttl=ASK_SCENE_TTL)
            rows = [[{"text": f"💡 {tip}"[:60], "callback_data": f"cmd:sug:{i}"}]
                    for i, tip in enumerate(tips)]
            rows.append([{"text": "🏠 Меню", "callback_data": "cmd:menu"}])
            self._reply(chat, "Куда дальше?", markup_or_none({"inline_keyboard": rows}) or ask_keyboard())

    # --------------------------------------------------------- обработчики
    def cmd_ask(self, chat: str, raw: str = "", text: str = "") -> None:
        if not self._ask_allowed(chat):
            self._reply(
                chat,
                "🤖 Ассистент доступен владельцу и руководителю — он отвечает "
                "по деньгам, долгам и клиентам.",
                self._report_keyboard(chat),
            )
            return
        self._ask_start(chat)

    def cb_ask(self, chat: str, message_id: str = "", params: str = "") -> None:
        self.cmd_ask(chat)

    def cb_sug(self, chat: str, message_id: str = "", params: str = "") -> None:
        """Кнопка-подсказка: сам вопрос лежит в данных сцены."""
        scene = self.scenes.active(chat) or {}
        tips = (scene.get("data") or {}).get("suggestions") or []
        try:
            index = int(str(params or "0"))
        except (TypeError, ValueError):
            index = -1
        if not (0 <= index < len(tips)):
            self._reply(chat, "Подсказка устарела — спросите своими словами.",
                        self._report_keyboard(chat))
            return
        self._ask_answer(chat, str(tips[index]))
