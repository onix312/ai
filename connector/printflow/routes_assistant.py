"""Маршруты помощника (18.13): рантайм, действия, голос, агент, журнал.

Семь маршрутов, и каждый отвечает за свою часть договорённости:

  * `status` — жив ли рантайм модели, какая модель, каталог действий;
  * `suggest` — предложения по пустым полям черновика (ничего не сохраняет);
  * `intent` — фраза → действие из каталога; адрес маршрута берётся из каталога
    на сервере, поэтому модель не может его подменить;
  * `phrase` — фраза, услышанная стоп-словом внешнего агента: след в журнале;
  * `journal` (POST/GET) — запись и чтение следа действий;
  * `agent` — жив ли агент компьютера и какое окно сейчас активно.

Деньги и печать эти маршруты не двигают: выполнение делает панель через обычные
маршруты системы с `confirmed`, взятым из каталога, а не из ответа модели.
"""
from __future__ import annotations

from typing import Any

from .router import Ctx, router


@router.get("/api/assistant/status", doc="Помощник: доступен ли локальный рантайм")
def assistant_status(api: Any, ctx: Ctx):
    """Состояние помощника: включён ли, отвечает ли рантайм, какая модель.

    Панель зовёт его при открытии настроек и модалки «Заказ из сообщения»,
    `pf.py doctor` проверяет то же самое без сервера. Ответ всегда содержит
    `reason` — интерфейс показывает причину, а не гадает по пустому списку.
    """
    from . import assistant as service
    return service.status(api.db)


@router.post("/api/assistant/suggest", doc="Помощник: предложения для пустых полей черновика")
def assistant_suggest(api: Any, ctx: Ctx):
    """Дополнить черновик входящего заказа и вернуть черновик ответа клиенту.

    Тело: `{text, channel, draft}` — `draft` обязателен: помощник заполняет
    только пустые поля того черновика, который уже собрал детерминированный
    разбор. Без черновика модель начала бы выдумывать сумму и количество.
    """
    from . import assistant as service
    draft = ctx.arg("draft")
    if not isinstance(draft, dict):
        draft = {}
    return service.suggest(api.db, draft, str(ctx.arg("text") or ""),
                           str(ctx.arg("channel") or ""))


@router.post("/api/assistant/intent", doc="Помощник: действие из каталога по фразе владельца")
def assistant_intent(api: Any, ctx: Ctx):
    """Фраза → действие из каталога, параметры и объяснение.

    Маршрут ничего не выполняет: адрес и признак подтверждения панель берёт из
    каталога, а не из ответа модели, поэтому «полный доступ» не означает
    «модель дёргает любой URL».
    """
    from . import assistant as service
    return service.parse_intent(api.db, str(ctx.arg("text") or ""))


@router.post("/api/assistant/journal", doc="Помощник: запись действия в журнал",
             audit="Помощник: запись в журнал")
def assistant_journal(api: Any, ctx: Ctx):
    """След действия помощника: что сделано, чем кончилось, кто подтвердил.

    Вход в панель помощника не требуется, поэтому журнал — единственное место,
    где остаётся след. Пишет панель после выполнения, потому что только она
    знает итог.
    """
    from . import assistant as service
    return {"ok": True, "event": service.journal(
        api.db, str(ctx.arg("action") or ""), str(ctx.arg("title") or ""),
        str(ctx.arg("outcome") or ""), str(ctx.arg("detail") or ""),
        ctx.arg("data") if isinstance(ctx.arg("data"), dict) else None,
        str(ctx.arg("printer_id") or ""))}


@router.get("/api/assistant/journal", doc="Помощник: последние действия из журнала")
def assistant_journal_recent(api: Any, ctx: Ctx):
    """Последние действия помощника — для панели и для разбора инцидента."""
    from . import assistant as service
    return {"ok": True, "events": service.journal_recent(api.db, int(ctx.num("limit", 30)))}


@router.get("/api/assistant/agent", doc="Помощник: жив ли агент компьютера")
def assistant_agent(api: Any, ctx: Ctx):
    """Статус внешнего агента: стоп-слово и активное окно.

    PrintFlow только читает статус. Действия в чужих приложениях выполняет сам
    агент и показывает их человеку на своём экране — коннектор не хранит ни
    снимков, ни нажатий.
    """
    from . import assistant as service
    return service.agent_status(api.db)


@router.post("/api/assistant/phrase", doc="Помощник: фраза от агента компьютера")
def assistant_phrase(api: Any, ctx: Ctx):
    """Стоп-слово услышано внешним агентом — фраза попадает в журнал панели.

    Агент не выполняет команды: он только передаёт текст. Действие по этой фразе
    человек делает в панели помощника, где деньги и печать требуют «Подтвердить».
    Маршрут принимает любой loopback-запрос, поэтому пишет след и не доверяет
    источнику: `source` берётся из тела, но помечается как внешняя фраза.
    """
    from . import assistant as service

    body = ctx.body if isinstance(ctx.body, dict) else {}
    text = " ".join(str(body.get("text") or "").split())[:1000]
    if not text:
        return 400, {"ok": False, "error": "Пустая фраза"}
    source = str(body.get("source") or "agent")[:60]
    event = service.journal(api.db, "phrase", "Фраза голосом", "heard", text,
                            {"source": source})
    return {"ok": True, "text": text, "source": source, "event": event,
            "reason": "", "hint": "Действие по фразе делает человек в панели помощника"}
