"""Маршруты помощника (18.18): рантайм, действия, голос, агент, журнал, Авито, ТГ.

Восемьдесят четыре маршрута, и каждый отвечает за свою часть договорённости:

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
  * `avito/*` — слежка за Авито, поиск, проверка, варианты ответа (18.15, И181–И186),
    архив переписок, связка с заказом, расписание, дедуп по фото, уведомления (18.16, И191, И193, И195-И197);
  * `tg/*` — идеи и черновики постов в ТГ, публикация с подтверждением (18.15, И182, И184, И188),
    календарь, шаблоны, хештеги, поиск, экспорт, статистика конверсии (18.16, И198, И200-И205),
    система, окна, экран, голос, буфер, таймеры, предпочтения, белый список, макросы (18.17, И206-И221),
    автоустановка зависимостей и моделей (18.18, И222).

Деньги и печать эти маршруты не двигают: выполнение делает панель через обычные
маршруты системы с `confirmed`, взятым из каталога, а не из ответа модели.
Маршруты 18.14 тоже только читают, а 18.15-18.17 проксируют вызовы к агенту: панель
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

# --- 18.16: архив переписок, связка, расписание, дедуп -------------------

@router.get("/api/assistant/avito/threads", doc="Помощник: архив переписок Авито")
def assistant_avito_threads(api: Any, ctx: Ctx):
    from . import assistant as service
    return service.avito_threads(api.db, int(ctx.num("limit", 30)), str(ctx.one("status") or ""))


@router.post("/api/assistant/avito/thread/save", doc="Помощник: сохранить переписку Авито")
def assistant_avito_thread_save(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    thread = str(body.get("thread") or body.get("text") or "")
    if not thread:
        return 400, {"ok": False, "error": "Пустая переписка"}
    return service.avito_thread_save(api.db, thread,
                                     str(body.get("intent") or ""),
                                     str(body.get("city") or ""),
                                     str(body.get("status") or "new"))


@router.post("/api/assistant/avito/to-order", doc="Помощник: объявление Авито в заказ",
             audit="Помощник: Авито-объявление в заказ")
def assistant_avito_to_order(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False) or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True,
                "reason": "Создание заказа из Авито требует подтверждения",
                "text": f"Создать заказ из объявления {str(body.get('title') or body.get('url') or '')[:80]}"}
    return service.avito_to_order(api.db,
                                  int(body.get("listing_id") or ctx.num("listing_id", 0) or 0),
                                  str(body.get("url") or ""),
                                  str(body.get("title") or ""),
                                  str(body.get("price") or ""),
                                  str(body.get("city") or ""))


@router.post("/api/assistant/avito/schedule", doc="Помощник: расписание проверки Авито")
def assistant_avito_schedule(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    watch_id = int(body.get("watch_id") or ctx.num("watch_id", 0) or 0)
    if not watch_id:
        return 400, {"ok": False, "error": "Не указан watch_id"}
    interval = int(body.get("interval_hours") or ctx.num("interval_hours", 0) or 0)
    notify = bool(body.get("notify")) if "notify" in body else bool(ctx.one("notify"))
    if "notify" not in body and not ctx.one("notify"):
        # если notify не передали — оставляем как есть
        return service.avito_schedule(api.db, watch_id, interval, False)
    return service.avito_schedule(api.db, watch_id, interval, notify)


@router.post("/api/assistant/avito/notify", doc="Помощник: уведомления Авито")
def assistant_avito_notify(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    watch_id = int(body.get("watch_id") or ctx.num("watch_id", 0) or 0)
    if not watch_id:
        return 400, {"ok": False, "error": "Не указан watch_id"}
    enabled = bool(body.get("enabled")) if "enabled" in body else True
    return service.avito_notify(api.db, watch_id, enabled)


@router.get("/api/assistant/avito/dedup", doc="Помощник: дубли Авито по фото")
def assistant_avito_dedup(api: Any, ctx: Ctx):
    from . import assistant as service
    return service.avito_dedup(api.db, int(ctx.num("limit", 20)), str(ctx.one("image_hash") or ""))


# --- 18.16: ТГ календарь, шаблоны, хештеги, поиск, экспорт, статистика -----

@router.post("/api/assistant/tg/schedule", doc="Помощник: запланировать пост ТГ")
def assistant_tg_schedule(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    draft_id = int(body.get("draft_id") or ctx.num("draft_id", 0) or 0)
    planned_at = str(body.get("planned_at") or ctx.one("planned_at") or "")
    if not draft_id or not planned_at:
        return 400, {"ok": False, "error": "Нужны draft_id и planned_at"}
    return service.tg_schedule(api.db, draft_id, planned_at, str(body.get("chat") or ""))


@router.get("/api/assistant/tg/schedules", doc="Помощник: календарь ТГ-постов")
def assistant_tg_schedules(api: Any, ctx: Ctx):
    from . import assistant as service
    return service.tg_schedules(api.db, int(ctx.num("limit", 30)), str(ctx.one("status") or ""))


@router.post("/api/assistant/tg/template/save", doc="Помощник: сохранить шаблон ТГ")
def assistant_tg_template_save(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    name = str(body.get("name") or "").strip()
    tmpl = str(body.get("template") or body.get("template_text") or "").strip()
    if not name or not tmpl:
        return 400, {"ok": False, "error": "Нужны name и template"}
    return service.tg_template_save(api.db, name, str(body.get("tone") or ""), tmpl)


@router.get("/api/assistant/tg/templates", doc="Помощник: шаблоны ТГ-постов")
def assistant_tg_templates(api: Any, ctx: Ctx):
    from . import assistant as service
    return service.tg_templates(api.db, int(ctx.num("limit", 30)))


@router.post("/api/assistant/tg/template/apply", doc="Помощник: пост из шаблона ТГ")
def assistant_tg_template_apply(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    template_id = int(body.get("template_id") or ctx.num("template_id", 0) or 0)
    return service.tg_template_apply(api.db, template_id,
                                     str(body.get("facts") or ""),
                                     str(body.get("topic") or ""),
                                     str(body.get("tone") or ""))


@router.post("/api/assistant/tg/hashtags", doc="Помощник: хештеги для ТГ")
def assistant_tg_hashtags(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    txt = str(body.get("text") or body.get("topic") or "")
    if not txt:
        return 400, {"ok": False, "error": "Пустой текст"}
    return service.tg_hashtags(api.db, txt, int(body.get("limit") or ctx.num("limit", 6) or 6))


@router.get("/api/assistant/tg/search", doc="Помощник: поиск по черновикам ТГ")
def assistant_tg_search(api: Any, ctx: Ctx):
    from . import assistant as service
    q = str(ctx.one("q") or ctx.one("query") or "")
    if not q:
        body = ctx.body if isinstance(ctx.body, dict) else {}
        q = str(body.get("query") or body.get("q") or "")
    if not q:
        return 400, {"ok": False, "error": "Пустой запрос"}
    return service.tg_search(api.db, q, int(ctx.num("limit", 20)))


@router.get("/api/assistant/tg/export", doc="Помощник: экспорт черновиков ТГ")
def assistant_tg_export(api: Any, ctx: Ctx):
    from . import assistant as service
    return service.tg_export(api.db, str(ctx.one("status") or ""),
                             int(ctx.num("limit", 100)), str(ctx.one("format") or "md"))


@router.get("/api/assistant/tg/stats", doc="Помощник: статистика ТГ")
def assistant_tg_stats(api: Any, ctx: Ctx):
    from . import assistant as service
    return service.tg_stats(api.db)


@router.post("/api/assistant/tg/idea/save", doc="Помощник: сохранить идею ТГ")
def assistant_tg_idea_save(api: Any, ctx: Ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    idea = str(body.get("idea") or body.get("idea_text") or "").strip()
    if not idea:
        return 400, {"ok": False, "error": "Пустая идея"}
    return service.tg_idea_save(api.db, str(body.get("context") or ""), idea)


@router.get("/api/assistant/tg/ideas/history", doc="Помощник: история идей ТГ")
def assistant_tg_ideas_history(api: Any, ctx: Ctx):
    from . import assistant as service
    return service.tg_ideas_history(api.db, int(ctx.num("limit", 30)), str(ctx.one("status") or ""))

# --- 18.17: система, окна, экран, голос, буфер, таймеры, предпочтения ----

@router.post("/api/assistant/system/autostart", doc="Помощник: автозагрузка")
def assistant_system_autostart(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    enabled = bool(body.get("enabled")) if "enabled" in body else True
    return service.system_autostart(api.db, enabled, str(body.get("app_name") or "PrintFlowAssistant"))

@router.get("/api/assistant/system/processes", doc="Помощник: список процессов")
def assistant_system_processes(api, ctx):
    from . import assistant as service
    return service.system_process_list(api.db, int(ctx.num("limit", 20)))

@router.get("/api/assistant/system/audio", doc="Помощник: аудио-устройства")
def assistant_system_audio(api, ctx):
    from . import assistant as service
    return service.system_audio_device(api.db, str(ctx.one("device_id") or ""))

@router.post("/api/assistant/system/audio", doc="Помощник: выбрать аудио-устройство")
def assistant_system_audio_set(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.system_audio_device(api.db, str(body.get("device_id") or ""))

@router.get("/api/assistant/system/volume", doc="Помощник: громкость")
def assistant_system_volume_get(api, ctx):
    from . import assistant as service
    return service.system_volume(api.db, 0)

@router.post("/api/assistant/system/volume", doc="Помощник: задать громкость", audit="Помощник: громкость")
def assistant_system_volume_set(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.system_volume(api.db, int(body.get("level") or ctx.num("level", 0) or 0))

@router.get("/api/assistant/system/display", doc="Помощник: дисплеи")
def assistant_system_display(api, ctx):
    from . import assistant as service
    return service.system_display(api.db)

@router.post("/api/assistant/system/focus", doc="Помощник: режим фокуса")
def assistant_system_focus(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.system_focus(api.db, int(body.get("minutes") or ctx.num("minutes", 30) or 30))

@router.post("/api/assistant/system/power", doc="Помощник: питание", audit="Помощник: питание")
def assistant_system_power(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False) or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "reason": "Действие питания требует подтверждения", "text": f"Выполнить {str(body.get('action') or 'lock')}"}
    return service.system_power(api.db, str(body.get("action") or "lock"))

@router.get("/api/assistant/system/health", doc="Помощник: здоровье ПК")
def assistant_system_health(api, ctx):
    from . import assistant as service
    return service.system_health(api.db)

@router.get("/api/assistant/window/active", doc="Помощник: активное окно")
def assistant_window_active(api, ctx):
    from . import assistant as service
    return service.window_active(api.db)

@router.get("/api/assistant/window/list", doc="Помощник: список окон")
def assistant_window_list(api, ctx):
    from . import assistant as service
    return service.window_list(api.db, int(ctx.num("limit", 20)))

@router.post("/api/assistant/window/focus", doc="Помощник: фокус на окно", audit="Помощник: фокус окна")
def assistant_window_focus(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.window_focus(api.db, str(body.get("title") or ctx.one("title") or ""))

@router.get("/api/assistant/window/text", doc="Помощник: текст окна")
def assistant_window_text(api, ctx):
    from . import assistant as service
    return service.window_text(api.db, str(ctx.one("title") or ""))

@router.get("/api/assistant/window/controls", doc="Помощник: элементы окна")
def assistant_window_controls(api, ctx):
    from . import assistant as service
    return service.window_controls(api.db, str(ctx.one("title") or ""))

@router.post("/api/assistant/window/click", doc="Помощник: клик", audit="Помощник: клик")
def assistant_window_click(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False) or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "reason": "Клик требует подтверждения", "text": f"Клик {body.get('x')},{body.get('y')}"}
    return service.window_click(api.db, int(body.get("x") or ctx.num("x", 0) or 0), int(body.get("y") or ctx.num("y", 0) or 0))

@router.post("/api/assistant/window/type", doc="Помощник: ввод текста", audit="Помощник: ввод текста")
def assistant_window_type(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False) or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "reason": "Ввод требует подтверждения", "text": f"Ввести «{str(body.get('text') or '')[:40]}»"}
    return service.window_type(api.db, str(body.get("text") or ""))

@router.post("/api/assistant/window/snap", doc="Помощник: разложить окна", audit="Помощник: разложить окна")
def assistant_window_snap(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False) or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "reason": "Разложить окна требует подтверждения", "text": f"Разложить {body.get('left_title')} и {body.get('right_title')}"}
    return service.window_snap(api.db, str(body.get("left_title") or ""), str(body.get("right_title") or ""))

@router.get("/api/assistant/screen/shot", doc="Помощник: снимок экрана")
def assistant_screen_shot(api, ctx):
    from . import assistant as service
    return service.screen_shot(api.db, int(ctx.num("max_side", 800) or 800))

@router.post("/api/assistant/screen/region", doc="Помощник: снимок области")
def assistant_screen_region(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.screen_region_shot(api.db, int(body.get("left") or 0), int(body.get("top") or 0), int(body.get("right") or 0), int(body.get("bottom") or 0))

@router.get("/api/assistant/screen/find", doc="Помощник: найти на экране")
def assistant_screen_find(api, ctx):
    from . import assistant as service
    q = str(ctx.one("q") or ctx.one("text") or "")
    return service.screen_find(api.db, q)

@router.post("/api/assistant/screen/find", doc="Помощник: найти на экране")
def assistant_screen_find_post(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.screen_find(api.db, str(body.get("text") or ""))

@router.post("/api/assistant/screen/find-click", doc="Помощник: найти и кликнуть", audit="Помощник: найти и кликнуть")
def assistant_screen_find_click(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False) or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "reason": "Клик по найденному требует подтверждения", "text": f"Найти и кликнуть «{body.get('text') or ''}»"}
    return service.screen_find_and_click(api.db, str(body.get("text") or ""))

@router.post("/api/assistant/screen/archive", doc="Помощник: архивировать экран")
def assistant_screen_archive(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.screen_archive(api.db, str(body.get("title") or ""))

@router.get("/api/assistant/screen/archive", doc="Помощник: архив экрана")
def assistant_screen_archive_list(api, ctx):
    from . import assistant as service
    return service.screen_archive_search(api.db, str(ctx.one("q") or ctx.one("query") or ""), int(ctx.num("limit", 20)))

@router.post("/api/assistant/screen/archive/erase", doc="Помощник: стереть архив экрана", audit="Помощник: стереть архив экрана")
def assistant_screen_archive_erase(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False) or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "reason": "Стирание архива необратимо", "text": "Стереть архив экрана"}
    return service.screen_archive_erase(api.db)

@router.post("/api/assistant/voice/listen", doc="Помощник: слушать микрофон")
def assistant_voice_listen(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.voice_listen(api.db, int(body.get("seconds") or ctx.num("seconds", 5) or 5))

@router.post("/api/assistant/voice/say", doc="Помощник: озвучить")
def assistant_voice_say(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.voice_say(api.db, str(body.get("text") or ""), str(body.get("tone") or ""))

@router.post("/api/assistant/voice/dictate", doc="Помощник: диктовка", audit="Помощник: диктовка")
def assistant_voice_dictate(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False) or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "reason": "Диктовка вводит текст в активное окно", "text": f"Диктовать {body.get('seconds') or 5} сек"}
    return service.voice_dictate(api.db, int(body.get("seconds") or 5))

@router.post("/api/assistant/voice/note", doc="Помощник: голосовая заметка")
def assistant_voice_note(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.voice_note(api.db, str(body.get("text") or ""), str(body.get("due") or ""))

@router.post("/api/assistant/voice/command", doc="Помощник: голосовая команда")
def assistant_voice_command(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.voice_command(api.db, str(body.get("text") or ""))

@router.post("/api/assistant/voice/profile", doc="Помощник: профиль голоса")
def assistant_voice_profile(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.voice_profile(api.db, str(body.get("speed") or ""), str(body.get("tone") or ""), int(body.get("volume") or 0))

@router.get("/api/assistant/clipboard/history", doc="Помощник: история буфера")
def assistant_clipboard_history(api, ctx):
    from . import assistant as service
    return service.clipboard_history(api.db, int(ctx.num("limit", 20)))

@router.post("/api/assistant/clipboard/read", doc="Помощник: читать буфер")
def assistant_clipboard_read(api, ctx):
    from . import assistant as service
    return service.clipboard_read(api.db)

@router.post("/api/assistant/clipboard/write", doc="Помощник: писать в буфер")
def assistant_clipboard_write(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.clipboard_write(api.db, str(body.get("text") or ""))

@router.post("/api/assistant/focus/timer", doc="Помощник: таймер фокуса")
def assistant_focus_timer(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.focus_timer(api.db, int(body.get("minutes") or ctx.num("minutes", 25) or 25), str(body.get("note") or ""))

@router.get("/api/assistant/focus/timers", doc="Помощник: таймеры фокуса")
def assistant_focus_timers(api, ctx):
    from . import assistant as service
    return service.focus_list(api.db, int(ctx.num("limit", 20)))

@router.post("/api/assistant/focus/stop", doc="Помощник: стоп таймер")
def assistant_focus_stop(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.focus_stop(api.db, int(body.get("timer_id") or ctx.num("timer_id", 0) or 0))

@router.post("/api/assistant/files/watch", doc="Помощник: следить за папкой")
def assistant_files_watch(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.files_watch(api.db, str(body.get("path") or ""), bool(body.get("enabled")) if "enabled" in body else True)

@router.get("/api/assistant/files/watches", doc="Помощник: слежки за папками")
def assistant_files_watches(api, ctx):
    from . import assistant as service
    return service.files_watches(api.db, int(ctx.num("limit", 20)))

@router.get("/api/assistant/files/quick-open", doc="Помощник: быстрый поиск файла")
def assistant_files_quick_open(api, ctx):
    from . import assistant as service
    return service.files_quick_open(api.db, str(ctx.one("q") or ctx.one("name") or ""), int(ctx.num("limit", 10)))

@router.get("/api/assistant/preferences", doc="Помощник: предпочтения")
def assistant_preferences(api, ctx):
    from . import assistant as service
    return service.knowledge_preferences(api.db, str(ctx.one("key") or ""))

@router.post("/api/assistant/preferences", doc="Помощник: сохранить предпочтение")
def assistant_preferences_save(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.knowledge_preference_save(api.db, str(body.get("key") or ""), str(body.get("value") or ""))

@router.get("/api/assistant/whitelist", doc="Помощник: белый список")
def assistant_whitelist(api, ctx):
    from . import assistant as service
    return service.safety_whitelist(api.db, int(ctx.num("limit", 100)))

@router.post("/api/assistant/whitelist", doc="Помощник: править белый список", audit="Помощник: белый список")
def assistant_whitelist_save(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False) or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "reason": "Правка белого списка требует подтверждения", "text": f"Белый список: {body.get('app_name')}"}
    return service.safety_whitelist_save(api.db, str(body.get("app_name") or ""), bool(body.get("allowed")) if "allowed" in body else True)

@router.post("/api/assistant/macro", doc="Помощник: сохранить макрос")
def assistant_macro_save(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return service.assistant_macro(api.db, str(body.get("name") or ""), body.get("steps") or [], str(body.get("description") or ""))

@router.get("/api/assistant/macros", doc="Помощник: макросы")
def assistant_macros_list(api, ctx):
    from . import assistant as service
    return service.assistant_macros(api.db, int(ctx.num("limit", 20)))

@router.post("/api/assistant/macro/run", doc="Помощник: запустить макрос", audit="Помощник: запуск макроса")
def assistant_macro_run(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False) or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "reason": "Запуск макроса требует подтверждения", "text": f"Макрос {body.get('name')}"}
    return service.assistant_macro_run(api.db, str(body.get("name") or ""))

@router.get("/api/assistant/system/check", doc="Помощник: проверка зависимостей")
def assistant_system_check(api, ctx):
    from . import assistant as service
    return service.system_check(api.db)

@router.post("/api/assistant/system/install", doc="Помощник: автоустановка", audit="Помощник: автоустановка")
def assistant_system_install(api, ctx):
    from . import assistant as service
    body = ctx.body if isinstance(ctx.body, dict) else {}
    confirmed = bool((body.get("confirmed") if isinstance(body, dict) else False) or ctx.arg("confirmed"))
    if not confirmed:
        return {"ok": False, "needs_confirmation": True, "reason": "Автоустановка меняет систему — нужно подтверждение", "text": f"Установить {body.get('what') or 'pip'}"}
    return service.system_install(api.db, str(body.get("what") or "pip"), str(body.get("confirm_text") or ""))

