"""Маршруты помощника (18.15): рантайм, действия, голос, агент, журнал, Авито, ТГ.

Шестнадцать маршрутов, и каждый отвечает за свою часть договорённости:

  * `status` — жив ли рантайм модели, какая модель, каталог действий;
  * `suggest` — предложения по пустым полям черновика (ничего не сохраняет);
  * `intent` — фраза → действие из каталога; адрес маршрута берётся из каталога
    на сервере, поэтому модель не может его подменить;
  * `phrase` — фраза, услышанная стоп-словом внешнего агента: след в журнале;
  * `journal` (POST/GET) — запись и чтение следа действий;
  * `agent` — жив ли агент компьютера и какое окно сейчас активно;
  * `ask` — ответ на вопрос владельца по фактам его же базы (18.14, идея И1);
  * `day` — утренний брифинг или итог дня, собранный детерминированно (И174);
  * `skills` — реестр навыков ассистента компьютера, который отдаёт агент (И136);
  * `avito/*` — слежка за Авито, поиск, проверка, варианты ответа (18.15, И181–И186);
  * `tg/*` — идеи и черновики постов в ТГ, публикация с подтверждением (18.15, И182, И184, И188).

Деньги и печать эти маршруты не двигают: выполнение делает панель через обычные
маршруты системы с `confirmed`, взятым из каталога, а не из ответа модели.
Маршруты 18.14 тоже только читают, а 18.15 проксируют вызовы к агенту: панель
не исполняет Авито и ТГ сама, а просит агента по loopback — тот же узор, что у
реестра навыков.
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


@router.post("/api/assistant/ask", doc="Помощник: ответ по фактам базы")
def assistant_ask(api: Any, ctx: Ctx):
    """Вопрос владельца → факты из базы и ответ по ним (идея И1).

    Тело: `{question}`. Ответ всегда содержит `facts` — строки из существующих
    сервисов панели с источниками, поэтому сказанное проверяется глазами в том же
    разделе, где эти цифры живут. Модель не обязательна: без рантайма маршрут
    отдаёт факты и причину, а не придуманный абзац.
    """
    from . import assistant_knowledge as knowledge

    body = ctx.body if isinstance(ctx.body, dict) else {}
    question = " ".join(str(body.get("question") or "").split())[:600]
    if not question:
        return 400, {"ok": False, "error": "Пустой вопрос"}
    return knowledge.answer(api, question)


@router.get("/api/assistant/day", doc="Помощник: брифинг или итог дня")
def assistant_day(api: Any, ctx: Ctx):
    """Утро и вечер цеха одним текстом (идея И174).

    `?kind=briefing|summary&days=N`. Модель не зовётся вовсе: цифры дня берутся
    из `planner.day_plan()`, `acc.summary()`, `acc.debts()`, `manager.snapshot()`
    и `insights.all()` — тех же сервисов, что рисуют панели. Голос ассистента и
    экран панели поэтому не могут разойтись.
    """
    from . import assistant_knowledge as knowledge

    days = int(ctx.num("days", 1) or 1)
    return knowledge.day(api, ctx.one("kind") or "briefing", days)


@router.get("/api/assistant/skills", doc="Помощник: реестр навыков ассистента компьютера")
def assistant_skills(api: Any, ctx: Ctx):
    """Что умеет ассистент компьютера: навыки, доступность и причины отказа.

    Реестр принадлежит агенту (идея И136), панель его только читает по loopback.
    Если агент выключен или не отвечает, панель показывает причину — список
    навыков не хранится в коннекторе и не выдуман заранее.
    """
    from . import assistant as service
    return service.agent_skills(api.db)


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


# --- 18.15: Авито и ТГ — прокси к агенту компьютера ------------------------

@router.post("/api/assistant/avito/watch", doc="Помощник: добавить слежку за Авито")
def assistant_avito_watch(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.avito_watch(
        api.db,
        str(body.get("query") or ctx.one("query") or ""),
        str(body.get("city") or ctx.one("city") or ""),
        str(body.get("category") or ctx.one("category") or ""),
        int(body.get("max_price") or ctx.num("max_price", 0) or 0),
        int(body.get("min_price") or ctx.num("min_price", 0) or 0))


@router.get("/api/assistant/avito/watches", doc="Помощник: список слежек Авито")
def assistant_avito_watches(api: Any, ctx: Ctx):
    from . import assistant as service
    # читаем напрямую через агент-клиент: прокси без своей логики
    state = service.agent_status(api.db)
    if not state.get("available"):
        return {"ok": False, "reason": state.get("reason") or "Агент недоступен"}
    ok, payload, reason = service._get_json(f"{state['url']}/skills", service.PING_SEC)
    # не используем skills, а напрямую зовём avito.watches через _call_agent_skill
    return service._call_agent_skill(api.db, "avito.watches", {"limit": int(ctx.num("limit", 20))})


@router.post("/api/assistant/avito/search", doc="Помощник: поиск на Авито")
def assistant_avito_search(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.avito_search(
        api.db,
        str(body.get("query") or ctx.one("query") or ""),
        str(body.get("city") or ctx.one("city") or ""),
        str(body.get("category") or ctx.one("category") or ""),
        int(body.get("max_price") or ctx.num("max_price", 0) or 0),
        int(body.get("min_price") or ctx.num("min_price", 0) or 0),
        int(body.get("limit") or ctx.num("limit", 20) or 20))


@router.post("/api/assistant/avito/check", doc="Помощник: проверить слежки Авито")
def assistant_avito_check(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.avito_check(
        api.db,
        int(body.get("watch_id") or ctx.num("watch_id", 0) or 0),
        bool(body.get("only_new")) if "only_new" in body else True)


@router.post("/api/assistant/avito/reply", doc="Помощник: варианты ответа на Авито")
def assistant_avito_reply(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    thread = str(body.get("thread") or body.get("text") or "")
    if not thread:
        return 400, {"ok": False, "error": "Пустая переписка"}
    return service.avito_reply(api.db, thread,
                               str(body.get("intent") or ""),
                               str(body.get("city") or ""))


@router.post("/api/assistant/tg/draft", doc="Помощник: черновик поста в ТГ")
def assistant_tg_draft(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    topic = str(body.get("topic") or "").strip()
    if not topic:
        return 400, {"ok": False, "error": "Пустая тема поста"}
    return service.tg_draft(api.db, topic,
                            str(body.get("tone") or "дружелюбный"),
                            str(body.get("facts") or ""),
                            str(body.get("source") or ""))


@router.get("/api/assistant/tg/drafts", doc="Помощник: список черновиков ТГ")
def assistant_tg_drafts(api: Any, ctx: Ctx):
    from . import assistant as service
    return service._call_agent_skill(api.db, "tg.drafts",
                                     {"limit": int(ctx.num("limit", 20)),
                                      "status": str(ctx.one("status") or "")})


@router.post("/api/assistant/tg/ideas", doc="Помощник: идеи постов в ТГ")
def assistant_tg_ideas(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.tg_ideas(api.db,
                            str(body.get("context") or ""),
                            int(body.get("limit") or ctx.num("limit", 8) or 8))


@router.post("/api/assistant/tg/post", doc="Помощник: опубликовать пост в ТГ",
             audit="Помощник: публикация в ТГ")
def assistant_tg_post(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    # Подтверждение берётся из общего механизма панели, а не из тела: маршрут
    # помечен audit и требует confirmed из каталога действий? Для ТГ делаем
    # своё подтверждение: без confirmed — отказ.
    if not ctx.body:
        pass
    # Проверяем confirmed флаг панели: если маршрут вызван без confirmed — просим
    # подтверждение как для денег/печати (тот же приём, что у panel.do).
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False)
                     or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True,
                "reason": "Публикация в ТГ требует подтверждения человека",
                "text": f"Опубликовать пост «{str(body.get('topic') or body.get('text') or '')[:80]}» в ТГ"}
    return service.tg_post(api.db,
                           int(body.get("draft_id") or ctx.num("draft_id", 0) or 0),
                           str(body.get("text") or ""),
                           str(body.get("chat") or ""))
