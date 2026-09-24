"""Исполнение навыков ассистента (18.19).

Порядок один для всех навыков, и именно он держит договорённость с владельцем:

  1. **навык существует** — нет в реестре, значит нет и исполнения: модель не
     может выдумать действие (идея И178, «честность как функция»);
  2. **навык доступен** — нет способности (панель, модель, файлы, Windows),
     значит ответ содержит причину, а не пустой результат;
  3. **параметры разобраны** — необъявленное имя отброшено, неразобранное
     значение останавливает навык;
  4. **подтверждение** — признак берётся из реестра по риску; вызывающая
     сторона не может его понизить;
  5. **журнал** — исход каждого вызова, включая отказы, пишется в свою базу.

Отказ здесь — нормальный ответ, а не исключение: у каждого отказа есть
`reason`, который можно показать человеку и который объясняет `agent.why`.

18.21: мозг помощника — навыки ПК стали настоящими (громкость Core Audio,
питание, окна по имени программы, элементы окна, озвучка, описание экрана
видящей моделью), добавлены программы, медиаклавиши, сочетания клавиш,
свернуть/закрыть окно и память (`agent/pc.py`, `agent/brain.py`).
18.18: автоустановка систем
# 18.17: полноценный ассистент ПК (голос+система+зрение+окна+И206-И221)
# 18.16: архив переписок, связка, расписание, дедуп, календарь ТГ, шаблоны, хештеги, поиск, экспорт, статистика
# 18.17: голос+система+зрение+окна+И206-И221
# 18.15: добавлены Авито-слежка (И181, И183, И185, И186) и ТГ-посты (И182, И184).
"""

from __future__ import annotations

import pathlib
from typing import Any, Callable

from . import avito as avito_mod
from . import capabilities, config, documents, fileops, model, pc, skills, tg as tg_mod, winapi
from .panel_client import Client
from .store import Store

MAX_PASSAGES = 6
DEFAULT_LIMIT = 8


def describe(skill: dict[str, Any], params: dict[str, Any]) -> str:
    """Что будет сделано — одной фразой для окна подтверждения."""
    name = str(skill.get("name") or "")
    title = str(skill.get("title") or name)
    if skill.get("steps"):
        chain = " → ".join(str(step.get("skill")) for step in skill["steps"])
        return f"{title}: {chain}"
    if name == "panel.do":
        return f"{title}: {params.get('action') or 'действие не указано'}"
    if name == "files.to_order":
        return f"{title}: {pathlib.Path(str(params.get('path') or '')).name} → заказ «{params.get('order') or '—'}»"
    if name == "files.tidy_apply":
        folder = params.get("folder") or "папка загрузок"
        return f"{title}: переместить файлы в подпапки «{folder}» по плану"
    if name == "agent.learn":
        draft = params.get("skill") if isinstance(params.get("skill"), dict) else {}
        skill_name = draft.get("name") if isinstance(draft, dict) else ""
        return f"{title}: сохранить навык «{skill_name or 'без имени'}»"
    if name == "files.index":
        return f"{title}: перечитать папки {params.get('folders') or 'из настроек'}"
    if name == "avito.watch":
        return f"{title}: «{params.get('query') or '—'}» в {params.get('city') or 'везде'} до {params.get('max_price') or '∞'} ₽"
    if name == "avito.check":
        wid = params.get("watch_id")
        return f"{title}: проверить {'слежку ' + str(wid) if wid else 'все слежки'}"
    if name == "tg.draft":
        return f"{title}: «{params.get('topic') or '—'}» тон {params.get('tone') or 'дружелюбный'}"
    if name == "tg.post":
        did = params.get("draft_id")
        return f"{title}: черновик {did}" if did else f"{title}: «{str(params.get('text') or '')[:60]}»"
    if name == "avito.reply":
        return f"{title}: ответ на «{str(params.get('thread') or '')[:60]}» (только для копирования)"
    if name == "avito.thread_save":
        return f"{title}: «{str(params.get('thread') or '')[:60]}» → архив"
    if name == "avito.to_order":
        lid = params.get("listing_id") or params.get("url") or params.get("title") or "—"
        return f"{title}: объявление {lid} → черновик заказа"
    if name == "avito.schedule":
        return f"{title}: вотч {params.get('watch_id')} интервал {params.get('interval_hours')}ч"
    if name == "avito.dedup":
        return f"{title}: дубли по фото {params.get('image_hash') or 'все группы'}"
    if name == "tg.schedule":
        return f"{title}: черновик {params.get('draft_id')} на {params.get('planned_at')}"
    if name == "tg.template_save":
        return f"{title}: шаблон «{params.get('name') or '—'}»"
    if name == "tg.template_apply":
        return f"{title}: шаблон {params.get('template_id')} → черновик"
    if name == "tg.hashtags":
        return f"{title}: «{str(params.get('text') or '')[:50]}»"
    if name == "tg.search":
        return f"{title}: поиск «{params.get('query') or ''}»"
    if name == "tg.export":
        return f"{title}: экспорт {params.get('status') or 'все'} {params.get('format') or 'md'}"

    if name == "system.autostart":
        return f"{title}: {'включить' if params.get('enabled') else 'выключить'} автозагрузку"
    if name == "system.process_list":
        return f"{title}: топ {params.get('limit') or 20} процессов"
    if name == "system.audio_device":
        return f"{title}: {params.get('device_id') or 'список устройств'}"
    if name == "system.volume":
        if params.get("mute"):
            return f"{title}: звук {'выключить' if params['mute'] == 'on' else 'включить' if params['mute'] == 'off' else 'переключить'}"
        if params.get("level") is not None:
            return f"{title}: громкость {params.get('level')}%"
        if params.get("delta"):
            return f"{title}: {'громче' if int(params['delta']) > 0 else 'тише'} на {abs(int(params['delta']))}%"
        return f"{title}: узнать текущую"
    if name == "system.power":
        return f"{title}: {pc.POWER_ACTIONS.get(str(params.get('action') or ''), 'действие не указано')}"
    if name == "app.open":
        return f"{title}: «{params.get('target') or '—'}»"
    if name == "system.media":
        return f"{title}: {pc.MEDIA_ACTIONS.get(str(params.get('action') or ''), ('', 'действие не указано'))[1]}"
    if name == "system.hotkey":
        return f"{title}: нажать {params.get('keys') or '—'} в активном окне"
    if name == "window.arrange":
        verb = {"minimize": "свернуть", "maximize": "развернуть", "restore": "восстановить"}.get(
            str(params.get("action") or ""), "действие не указано")
        return f"{title}: {verb} «{params.get('title') or 'текущее окно'}»"
    if name == "window.close":
        return f"{title}: закрыть «{params.get('title') or '—'}» (программа спросит про несохранённое)"
    if name == "memory.remember":
        return f"{title}: «{str(params.get('text') or '')[:60]}»"
    if name == "memory.forget":
        return f"{title}: «{str(params.get('what') or '')[:60]}»"
    if name == "voice.listen":
        return f"{title}: открыть микрофон на {params.get('seconds') or '?'} с"
    if name == "window.focus":
        return f"{title}: фокус на «{params.get('title') or '—'}»"
    if name == "window.click":
        return f"{title}: клик {params.get('x')},{params.get('y')}"
    if name == "window.type":
        return f"{title}: ввод «{str(params.get('text') or '')[:40]}»"
    if name == "screen.region_shot":
        return f"{title}: область {params.get('left')},{params.get('top')}-{params.get('right')},{params.get('bottom')}"
    if name == "screen.find_and_click":
        return f"{title}: найти «{params.get('text') or ''}» и кликнуть"
    if name == "clipboard.write":
        return f"{title}: «{str(params.get('text') or '')[:40]}» в буфер"
    if name == "scheduler.focus_timer":
        return f"{title}: {params.get('minutes') or 25} мин — {params.get('note') or ''}"
    if name == "files.watch":
        return f"{title}: следить за {params.get('path') or 'папкой'}"
    if name == "knowledge.preference_save":
        return f"{title}: {params.get('key')} = {str(params.get('value') or '')[:40]}"
    if name == "safety.whitelist_save":
        return f"{title}: {params.get('app_name')} → {'разрешить' if params.get('allowed') else 'запретить'}"
    if name == "system.check":
        return f"{title}: проверка зависимостей"
    if name == "system.install":
        return f"{title}: установить {params.get('what') or 'недостающее'}"
    if name == "system.watchdog":
        return f"{title}: надзор — {params.get('mode') or 'status'}"
    if name == "system.watchdog_arm":
        return f"{title}: надзор {'включить' if str(params.get('enabled') or '') == 'on' else 'выключить'}"
    if name == "system.watchdog_once":
        return f"{title}: поднять упавшие роли ({(params.get('roles') or 'все')})"
    if name == "assistant.macro":
        return f"{title}: макрос «{params.get('name') or ''}» из {len(params.get('steps') or []) if isinstance(params.get('steps'), list) else 0} шагов"
    shown = ", ".join(f"{key}=«{str(value)[:60]}»" for key, value in list(params.items())[:3])
    return f"{title}" + (f": {shown}" if shown else "")


class Runner:
    """Один исполнитель на все навыки: реестр, способности, база, панель."""

    def __init__(self, store: Store | None = None, panel: Client | None = None) -> None:
        self.store = store if store is not None else Store()
        self.panel = panel if panel is not None else Client(config.PRINTFLOW_URL)
        self._caps: dict[str, Any] = {}
        self.refresh_capabilities()

    # --- способности ------------------------------------------------------
    def refresh_capabilities(self) -> dict[str, Any]:
        """Статические способности плюс живые: панель и рантайм модели.

        Живые проверяются коротким пингом и только здесь: `capabilities.detect()`
        остаётся без сети, чтобы агент стартовал даже с выключенной панелью.
        """
        caps = dict(capabilities.detect())
        caps.update(capabilities.dynamic(self.panel.url, config.MODEL_URL))
        caps["sqlite"] = True
        caps["sqlite_reason"] = ""
        try:
            self.store.stats()
        except Exception as exc:  # база могла оказаться на сетевом диске без прав
            caps["sqlite"] = False
            caps["sqlite_reason"] = f"Своя база ассистента не открывается: {exc.__class__.__name__}"
        self._caps = caps
        return caps

    @property
    def caps(self) -> dict[str, Any]:
        return dict(self._caps)

    def learned(self) -> dict[str, dict[str, Any]]:
        try:
            return self.store.learned_skills()
        except Exception:
            return {}

    def catalog(self) -> list[dict[str, Any]]:
        return skills.catalog(self.caps, self.learned())

    # --- главный вход -----------------------------------------------------
    def run(self, name: str, params: Any = None, confirmed: bool = False,
            ask: Callable[[str], bool] | None = None) -> dict[str, Any]:
        """Выполнить навык. `ask` — окно подтверждения человека (см. `server.Agent`)."""
        key = str(name or "").strip().casefold()
        learned = self.learned()
        skill = skills.get(key, learned)
        if skill is None:
            known = ", ".join(skills.names(learned)[:24])
            return self._refuse(key, {}, f"Навыка «{name}» нет в реестре. Есть: {known}")

        clean, errors = skills.check_params(skill, params)
        if errors:
            return self._refuse(key, clean, "; ".join(errors))

        # Выученный навык с шагами проверяет доступность по шагам, а не целиком:
        # иначе сценарий «утро = брифинг + файлы» был бы недоступен без панели,
        # хотя файлы доступны. Отказ конкретного шага покажет `agent.why` шага.
        if not skill.get("steps"):
            available, why = skills.availability(skill, self.caps)
            if not available:
                return self._refuse(key, clean, why, unavailable=True)

        if skills.confirm_required(skill):
            text = describe(skill, clean)
            human = bool(confirmed)
            if not human and ask is not None:
                human = bool(ask(text))
            if not human:
                outcome = self.store.journal(key, "needs_confirmation",
                                             detail="ждёт подтверждения человека",
                                             target=text, params=clean)
                return {"ok": False, "skill": key, "title": skill["title"],
                        "needs_confirmation": True, "reason": "Нужно подтверждение человека",
                        "text": text, "params": clean, "journal": outcome["id"]}

        try:
            result = self._dispatch(skill, clean)
        except Exception as exc:  # навык не имеет права уронить агента
            result = {"ok": False, "reason": f"Навык упал: {exc.__class__.__name__}: {exc}"}
        result.setdefault("skill", key)
        result.setdefault("title", str(skill.get("title") or key))
        result["params"] = clean
        # Чтение в журнал не пишется, если оно успешно: иначе лента утонет
        # (правило панели 18.13). Отказ чтения и любое изменение — пишутся.
        should_journal = not (skills.risk_of(skill) == "read" and result.get("ok"))
        if should_journal:
            detail = str(result.get("reason") or result.get("hint")
                         or _summary(result))[:300]
            outcome = "done" if result.get("ok") else "failed"
            entry = self.store.journal(key, outcome, detail=detail,
                                       target=str(result.get("target") or "")[:400],
                                       params=clean)
            result["journal"] = entry["id"]
        return result

    def _refuse(self, name: str, params: dict[str, Any], reason: str,
                unavailable: bool = False) -> dict[str, Any]:
        """Отказ с записью в журнал: причина потом видна через `agent.why`."""
        entry = self.store.journal(name, "unavailable" if unavailable else "refused",
                                   detail=reason[:300], params=params)
        return {"ok": False, "skill": name, "title": name, "reason": reason,
                "unavailable": unavailable, "params": params, "journal": entry["id"]}

    # --- разбор по навыкам ------------------------------------------------
    def _dispatch(self, skill: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        if skill.get("steps"):
            return self._run_steps(skill)
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "panel.actions": self._panel_actions,
            "panel.do": self._panel_do,
            "panel.ask": self._panel_ask,
            "files.index": self._files_index,
            "files.search": self._files_search,
            "files.recent": self._files_recent,
            "files.ask": self._files_ask,
            "files.facts": self._files_facts,
            "files.to_order": self._files_to_order,
            "files.tidy_plan": self._files_tidy_plan,
            "files.tidy_apply": self._files_tidy_apply,
            "knowledge.shop": self._knowledge_shop,
            "day.briefing": lambda p: self._day("briefing", p),
            "day.summary": lambda p: self._day("summary", p),
            "agent.skills": self._agent_skills,
            "agent.why": self._agent_why,
            "agent.journal": self._agent_journal,
            "agent.learn": self._agent_learn,
            # 18.15: Авито и ТГ
            "avito.watch": self._avito_watch,
            "avito.watches": self._avito_watches,
            "avito.search": self._avito_search,
            "avito.check": self._avito_check,
            "avito.reply": self._avito_reply,
            "avito.listings": self._avito_listings,
            "tg.draft": self._tg_draft,
            "tg.drafts": self._tg_drafts,
            "tg.ideas": self._tg_ideas,
            "tg.post": self._tg_post,
            # 18.16: архив, связка, расписание, дедуп
            "avito.threads": self._avito_threads,
            "avito.thread_save": self._avito_thread_save,
            "avito.to_order": self._avito_to_order,
            "avito.schedule": self._avito_schedule,
            "avito.notify": self._avito_notify,
            "avito.dedup": self._avito_dedup,
            "tg.schedule": self._tg_schedule,
            "tg.schedules": self._tg_schedules,
            "tg.template_save": self._tg_template_save,
            "tg.templates": self._tg_templates,
            "tg.template_apply": self._tg_template_apply,
            "tg.hashtags": self._tg_hashtags,
            "tg.search": self._tg_search,
            "tg.export": self._tg_export,
            "tg.stats": self._tg_stats,
            "tg.idea_save": self._tg_idea_save,
            "tg.ideas_history": self._tg_ideas_history,
            # 18.17: система, окна, зрение, голос, буфер, таймеры, предпочтения
            "system.autostart": self._system_autostart,
            "system.process_list": self._system_process_list,
            "system.audio_device": self._system_audio_device,
            "system.volume": self._system_volume,
            "system.display": self._system_display,
            "system.focus": self._system_focus,
            "system.power": self._system_power,
            "system.health": self._system_health,
            "system.check": self._system_check,
            "system.install": self._system_install,
            "system.watchdog": self._system_watchdog,
            "system.watchdog_arm": self._system_watchdog_arm,
            "system.watchdog_once": self._system_watchdog_once,
            "window.active": self._window_active,
            "window.list": self._window_list,
            "window.focus": self._window_focus,
            "window.text": self._window_text,
            "window.controls": self._window_controls,
            "window.click": self._window_click,
            "window.type": self._window_type,
            "window.snap": self._window_snap,
            "screen.shot": self._screen_shot,
            "screen.region_shot": self._screen_region_shot,
            "screen.describe": self._screen_describe,
            "screen.find": self._screen_find,
            "screen.find_and_click": self._screen_find_and_click,
            "screen.archive": self._screen_archive,
            "screen.archive_search": self._screen_archive_search,
            "screen.archive_erase": self._screen_archive_erase,
            "voice.listen": self._voice_listen,
            "voice.say": self._voice_say,
            "voice.dictate": self._voice_dictate,
            "voice.note": self._voice_note,
            "voice.command": self._voice_command,
            "voice.profile": self._voice_profile,
            "clipboard.history": self._clipboard_history,
            "clipboard.read": self._clipboard_read,
            "clipboard.write": self._clipboard_write,
            "scheduler.focus_timer": self._focus_timer,
            "scheduler.focus_list": self._focus_list,
            "scheduler.focus_stop": self._focus_stop,
            "files.watch": self._files_watch,
            "files.watches": self._files_watches,
            "files.quick_open": self._files_quick_open,
            "knowledge.preferences": self._knowledge_preferences,
            "knowledge.preference_save": self._knowledge_preference_save,
            "safety.whitelist": self._safety_whitelist,
            "safety.whitelist_save": self._safety_whitelist_save,
            "assistant.macro": self._assistant_macro,
            "assistant.macros": self._assistant_macros,
            "assistant.macro_run": self._assistant_macro_run,
            # 18.21
            "app.open": self._app_open,
            "system.media": self._system_media,
            "system.hotkey": self._system_hotkey,
            "window.arrange": self._window_arrange,
            "window.close": self._window_close,
            "memory.remember": self._memory_remember,
            "memory.recall": self._memory_recall,
            "memory.forget": self._memory_forget,
        }
        handler = handlers.get(str(skill.get("name") or ""))
        if handler is None:
            return {"ok": False,
                    "reason": f"Навык «{skill.get('name')}» объявлен, но не исполняется: "
                              "в диспетчере нет его обработчика"}
        return handler(params)

    def _run_steps(self, skill: dict[str, Any]) -> dict[str, Any]:
        """Выученный навык: шаги подряд, остановка на первом отказе.

        Подтверждение спрашивается один раз — на весь сценарий (`confirm` у
        выученного навыка всегда True), поэтому шаги идут с `confirmed=True`.
        """
        done: list[dict[str, Any]] = []
        for index, step in enumerate(skill.get("steps") or [], start=1):
            result = self.run(str(step.get("skill") or ""), step.get("params") or {},
                              confirmed=True)
            done.append({"step": index, "skill": str(step.get("skill") or ""),
                         "ok": bool(result.get("ok")),
                         "reason": str(result.get("reason") or "")})
            if not result.get("ok"):
                return {"ok": False, "steps": done,
                        "reason": f"Шаг {index} ({step.get('skill')}) не выполнен: "
                                  f"{result.get('reason')}"}
        return {"ok": True, "steps": done, "reason": "",
                "hint": f"Выполнено шагов: {len(done)}"}

    # --- панель (И137, И1, И174) ------------------------------------------
    def _panel_actions(self, _params: dict[str, Any]) -> dict[str, Any]:
        return self.panel.actions()

    def _panel_do(self, params: dict[str, Any]) -> dict[str, Any]:
        action, why = self.panel.find_action(str(params.get("action") or ""))
        if action is None:
            return {"ok": False, "reason": why}
        inner = params.get("params")
        values = inner if isinstance(inner, dict) else {}
        # Подтверждение действия панели берётся из её каталога: ассистент не
        # решает сам, какие действия двигают деньги и печать.
        confirmed = bool(action.get("confirm"))
        result = self.panel.run_action(action, values, confirmed=confirmed)
        result["title_action"] = str(action.get("title") or action.get("id") or "")
        result["panel_confirm"] = confirmed
        if not confirmed:
            result["hint"] = "Действие чтения: выполнено без подтверждения"
        return result

    def _panel_ask(self, params: dict[str, Any]) -> dict[str, Any]:
        question = str(params.get("question") or "").strip()
        if not question:
            return {"ok": False, "reason": "Пустой вопрос"}
        return self.panel.ask(question)

    def _day(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        days = int(params.get("days") or 1)
        return self.panel.day(kind, days=max(1, min(90, days)))

    # --- файлы и знания (И142, И143, И144, И147, И148) --------------------
    def _folders(self, raw: Any, default: tuple[str, ...]) -> list[str]:
        if isinstance(raw, (list, tuple)):
            parts = [str(item) for item in raw]
        else:
            parts = [part.strip() for part in
                     str(raw or "").replace("|", ";").replace(",", ";").split(";")]
        folders = [part for part in parts if part]
        return folders or list(default)

    def _files_index(self, params: dict[str, Any]) -> dict[str, Any]:
        folders = self._folders(params.get("folders"), config.file_folders())
        if not folders:
            return {"ok": False,
                    "reason": "Папки не заданы: укажите PRINTFLOW_ASSISTANT_FOLDERS "
                              "или параметр folders"}
        for folder in folders:
            allowed, why, _resolved = fileops.inside_allowed(folder)
            if not allowed:
                return {"ok": False, "reason": why, "folders": folders}
        return documents.index(folders, self.store)

    def _files_search(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        tokens = documents.tokens_of(query)
        if not tokens:
            return {"ok": False, "reason": "Пустой запрос: нужно хотя бы одно слово"}
        limit = int(params.get("limit") or DEFAULT_LIMIT)
        hits = self.store.search(tokens, limit=limit)
        if not hits:
            stats = self.store.stats()
            reason = ("В индексе ничего не нашлось"
                      + ("" if stats["chunks"] else " — индекс пуст, сначала выполните files.index"))
            return {"ok": True, "hits": [], "count": 0, "reason": reason,
                    "query": query, "stats": stats}
        return {"ok": True, "hits": hits, "count": len(hits), "reason": "",
                "query": query, "tokens": tokens,
                "hint": "Показаны отрывки с путями: источник проверяется глазами"}

    def _files_recent(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(200, int(params.get("limit") or 20)))
        rows = self.store.documents(limit=limit)
        rows.sort(key=lambda row: (-float(row.get("mtime") or 0), str(row.get("path"))))
        for row in rows:
            row.pop("digest", None)
        if not rows:
            return {"ok": True, "files": [], "reason": "Индекс пуст: сначала files.index"}
        return {"ok": True, "files": rows, "count": len(rows), "reason": "",
                "hint": "Время — последнее изменение файла, которое видел индекс"}

    def _retrieve(self, question: str, limit: int = MAX_PASSAGES,
                  knowledge_only: bool = False) -> tuple[list[dict[str, Any]], str]:
        """Отрывки по вопросу и текст-источник для модели."""
        tokens = documents.tokens_of(question)
        if not tokens:
            return [], ""
        hits = self.store.search(tokens, limit=limit * (3 if knowledge_only else 1))
        if knowledge_only:
            hits = [hit for hit in hits
                    if documents.is_knowledge(hit["path"])][:limit]
        else:
            hits = hits[:limit]
        source = "\n\n".join(
            f"[{index}] {hit['path']} (строка {hit['first_line']}): {hit['snippet']}"
            for index, hit in enumerate(hits, start=1))
        return hits, source

    def _answer_by_documents(self, question: str, knowledge_only: bool,
                             scope: str) -> dict[str, Any]:
        question = str(question or "").strip()
        if not question:
            return {"ok": False, "reason": "Пустой вопрос"}
        hits, source = self._retrieve(question, knowledge_only=knowledge_only)
        if not hits:
            stats = self.store.stats()
            return {"ok": False, "hits": [], "reason": (
                f"В {scope} ничего не нашлось по словам вопроса"
                + ("" if stats["chunks"] else f" — индекс {scope} пуст, сначала files.index")),
                "stats": stats}
        state = model.status()
        prompt = (
            "Ты ассистент владельца 3D-печатного цеха. Отвечай только по найденным "
            f"отрывкам из его {scope}.\n"
            f"Вопрос:\n\"\"\"\n{question[:1000]}\n\"\"\"\n\n"
            f"Найденное:\n{source[:8000]}\n\n"
            "Правила:\n"
            f"1. Если ответа в отрывках нет — скажи прямо: «в {scope} этого нет».\n"
            "2. Не придумывай суммы, сроки, телефоны и имена: они есть в отрывках "
            "или их нет вовсе.\n"
            "3. В конце перечисли файлы, из которых взят ответ.\n"
            "4. Два-три предложения по-русски, без списков ради списков.")
        answer = model.complete(prompt, url=state["url"], name=state["model"])
        citations = [{"path": hit["path"], "first_line": hit["first_line"],
                      "snippet": hit["snippet"][:300]} for hit in hits]
        if not answer["ok"]:
            return {"ok": False, "answer": "", "answered": False, "hits": citations,
                    "reason": f"Модель недоступна ({answer['reason']}) — вот найденные места",
                    "model": ""}
        check = model.numbers_checked(answer["text"], source)
        return {"ok": True, "answer": answer["text"], "answered": True,
                "hits": citations, "model": answer["model"], "reason": "",
                "numbers_checked": check,
                "warnings": [check["warning"]] if check["warning"] else []}

    def _files_ask(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._answer_by_documents(params.get("question"), False, "документах")

    def _knowledge_shop(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._answer_by_documents(params.get("question"), True, "знаниях цеха")

    def _files_facts(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = str(params.get("path") or "").strip()
        if not raw:
            return {"ok": False, "reason": "Не указан файл"}
        allowed, why, resolved = fileops.inside_allowed(raw)
        if not allowed:
            return {"ok": False, "reason": why}
        found = documents.facts_of_file(resolved)
        if not found["ok"]:
            return found
        saved = 0
        if params.get("save"):
            saved = self.store.put_facts(str(resolved), found["facts"])
        return {"ok": True, "path": str(resolved), "kind": found.get("kind", ""),
                "facts": found["facts"], "count": len(found["facts"]),
                "saved": saved, "reason": "",
                "target": str(resolved),
                "hint": ("Факты сохранены в память ассистента" if saved
                         else "Факты не сохранены: добавьте save=да")}

    def _files_to_order(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = str(params.get("path") or "").strip()
        if not raw:
            return {"ok": False, "reason": "Не указан файл"}
        allowed, why, resolved = fileops.inside_allowed(raw)
        if not allowed:
            return {"ok": False, "reason": why}
        result = self.panel.upload_to_order(resolved, str(params.get("order") or ""),
                                            str(params.get("kind") or "document"))
        result["target"] = str(resolved)
        return result

    def _files_tidy_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        """План раскладки: риск `read`, поэтому никакого подтверждения не просит."""
        folder = str(params.get("folder") or config.DOWNLOADS_FOLDER)
        plan = fileops.plan(folder)
        if not plan["ok"]:
            return plan
        return {"ok": True, "folder": plan["folder"], "plan": plan["moves"],
                "skipped": plan["skipped"], "count": plan["count"],
                "total_bytes": plan["total_bytes"], "moved": 0, "reason": "",
                "target": plan["folder"], "hint": plan["hint"],
                "next": "files.tidy_apply" if plan["moves"] else ""}

    def _files_tidy_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        """Исполнить план. План пересчитывается здесь же: человек подтверждает то,
        что будет сделано сейчас, а не тот список, который могли изменить."""
        folder = str(params.get("folder") or config.DOWNLOADS_FOLDER)
        plan = fileops.plan(folder)
        if not plan["ok"]:
            return plan
        if not plan["moves"]:
            return {"ok": True, "folder": plan["folder"], "plan": [], "moved": 0,
                    "skipped": plan["skipped"], "reason": "",
                    "target": plan["folder"], "hint": "Раскладывать нечего"}
        result = fileops.execute(plan["moves"])
        result.update(folder=plan["folder"], plan=plan["moves"],
                      skipped=plan["skipped"], target=plan["folder"])
        return result

    # --- Авито (И181, И183, И185, И186) ------------------------------------
    def _avito_watch(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        if not query:
            return {"ok": False, "reason": "Пустой запрос: что искать на Авито?"}
        city = str(params.get("city") or "").strip()[:120]
        category = str(params.get("category") or "").strip()[:120]
        max_price = int(params.get("max_price") or 0)
        min_price = int(params.get("min_price") or 0)
        enabled = bool(params.get("enabled")) if "enabled" in params else True
        if max_price and min_price and max_price < min_price:
            return {"ok": False, "reason": "max_price меньше min_price"}
        watch = self.store.add_avito_watch(query, city, category, max_price, min_price, enabled)
        return {"ok": True, "watch": watch, "reason": "",
                "hint": "Слежка сохранена. Проверьте её через avito.check"}

    def _avito_watches(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("limit") or 20)
        watches = self.store.list_avito_watches(limit)
        return {"ok": True, "watches": watches, "count": len(watches), "reason": "",
                "hint": "Список слежек из своей базы"}

    def _avito_search(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        if not query:
            return {"ok": False, "reason": "Пустой запрос"}
        city = str(params.get("city") or "").strip()
        category = str(params.get("category") or "").strip()
        max_price = int(params.get("max_price") or 0)
        min_price = int(params.get("min_price") or 0)
        limit = int(params.get("limit") or 20)
        result = avito_mod.search(query, city, category, max_price, min_price, limit)
        # не сохраняем в базу — это разовый поиск
        return result

    def _avito_check(self, params: dict[str, Any]) -> dict[str, Any]:
        watch_id = int(params.get("watch_id") or 0)
        only_new = bool(params.get("only_new")) if "only_new" in params else True

        watches: list[dict[str, Any]]
        if watch_id:
            w = self.store.get_avito_watch(watch_id)
            if not w:
                return {"ok": False, "reason": f"Слежки {watch_id} нет"}
            watches = [w]
        else:
            watches = [w for w in self.store.list_avito_watches(100) if w.get("enabled")]

        if not watches:
            return {"ok": True, "checked": 0, "new": 0, "listings": [], "reason": "Нет активных слежек"}

        all_new: list[dict[str, Any]] = []
        total_checked = 0
        total_new = 0
        for w in watches:
            res = avito_mod.search(str(w.get("query") or ""), str(w.get("city") or ""),
                                   str(w.get("category") or ""),
                                   int(w.get("max_price") or 0),
                                   int(w.get("min_price") or 0), 30)
            total_checked += 1
            if not res.get("ok"):
                continue
            listings = res.get("listings") or []
            _total, new_cnt = self.store.save_avito_listings(int(w["id"]), listings)
            self.store.update_avito_watch(int(w["id"]), last_count=new_cnt)
            total_new += new_cnt
            if only_new:
                # только новые для ответа
                saved = self.store.list_avito_listings(int(w["id"]), 50, only_new=True)
                all_new.extend(saved)
            else:
                all_new.extend(listings)

        # если only_new и ничего нового — отдаём последние
        if only_new and not all_new:
            return {"ok": True, "checked": total_checked, "new": 0,
                    "listings": [], "reason": "Нового нет — все объявления уже видели"}

        return {"ok": True, "checked": total_checked, "new": total_new,
                "listings": all_new[:100], "count": len(all_new[:100]), "reason": "",
                "hint": f"Проверено {total_checked}, нового {total_new}"}

    def _avito_reply(self, params: dict[str, Any]) -> dict[str, Any]:
        thread = str(params.get("thread") or "").strip()
        if not thread:
            return {"ok": False, "reason": "Пустая переписка: что отвечаем?"}
        intent = str(params.get("intent") or "").strip()[:300]
        city = str(params.get("city") or "").strip()[:120]
        state = model.status()
        result = avito_mod.suggest_replies(thread, intent, city,
                                           model_status=state if state.get("ok") else None)
        return result

    def _avito_listings(self, params: dict[str, Any]) -> dict[str, Any]:
        watch_id = int(params.get("watch_id") or 0)
        limit = int(params.get("limit") or 30)
        only_new = bool(params.get("only_new")) if "only_new" in params else False
        rows = self.store.list_avito_listings(watch_id, limit, only_new)
        return {"ok": True, "listings": rows, "count": len(rows), "reason": "",
                "watch_id": watch_id, "only_new": only_new}

    # --- ТГ (И182, И184) ---------------------------------------------------
    def _tg_draft(self, params: dict[str, Any]) -> dict[str, Any]:
        topic = str(params.get("topic") or "").strip()
        if not topic:
            return {"ok": False, "reason": "Пустая тема поста"}
        tone = str(params.get("tone") or "дружелюбный").strip().lower()
        facts = str(params.get("facts") or "").strip()[:1000]
        source = str(params.get("source") or "").strip()[:600]

        # контекст из базы: последние файлы и знания
        if not facts:
            try:
                recent = self.store.documents(5)
                facts = "Недавно: " + ", ".join(r.get("title") or "" for r in recent[:3])
            except Exception:
                facts = ""

        state = model.status()
        res = tg_mod.draft_post(topic, tone, facts, source,
                                model_status=state if state.get("ok") else None)
        if not res.get("ok"):
            return res
        saved = self.store.add_tg_draft(res.get("topic") or topic,
                                        res.get("tone") or tone,
                                        res.get("text") or "", source)
        res["draft"] = saved
        res["draft_id"] = saved["id"]
        return res

    def _tg_drafts(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("limit") or 20)
        status = str(params.get("status") or "").strip()[:20]
        rows = self.store.list_tg_drafts(limit, status)
        return {"ok": True, "drafts": rows, "count": len(rows), "reason": "",
                "hint": "Черновики из своей базы — публикуете руками в ТГ"}

    def _tg_ideas(self, params: dict[str, Any]) -> dict[str, Any]:
        context = str(params.get("context") or "").strip()[:600]
        limit = int(params.get("limit") or 8)
        if not context:
            try:
                docs = self.store.documents(5)
                context = "Печатали: " + ", ".join(d.get("title") or "" for d in docs[:3])
            except Exception:
                context = ""
        state = model.status()
        res = tg_mod.generate_ideas(context, limit,
                                    model_status=state if state.get("ok") else None)
        return res

    def _tg_post(self, params: dict[str, Any]) -> dict[str, Any]:
        draft_id = int(params.get("draft_id") or 0)
        text = str(params.get("text") or "").strip()
        chat = str(params.get("chat") or "").strip()

        if draft_id:
            draft = self.store.get_tg_draft(draft_id)
            if not draft:
                return {"ok": False, "reason": f"Черновика {draft_id} нет"}
            if not text:
                text = str(draft.get("text") or "")
        if not text:
            return {"ok": False, "reason": "Пустой текст поста"}
        # постим
        res = tg_mod.post_to_telegram(text, chat_id=chat)
        if not res.get("ok"):
            return {"ok": False, "reason": res.get("reason") or "Не удалось отправить в ТГ"}
        if draft_id:
            self.store.update_tg_draft_status(draft_id, "posted")
        return {"ok": True, "reason": "", "message_id": res.get("message_id"),
                "chat": chat or "из настроек", "text": text[:500],
                "hint": "Пост отправлен в ТГ-канал"}

    # --- 18.18: автоустановка систем
# 18.17: полноценный ассистент ПК (голос+система+зрение+окна+И206-И221)
# 18.16: архив переписок, связка, расписание, дедуп (И191, И193, И195-И197) ---
    def _avito_threads(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("limit") or 30)
        status = str(params.get("status") or "").strip()
        rows = self.store.list_avito_threads(limit, status)
        return {"ok": True, "threads": rows, "count": len(rows), "reason": "",
                "hint": "Архив переписок — только текст для копирования, без авто-отправки"}

    def _avito_thread_save(self, params: dict[str, Any]) -> dict[str, Any]:
        thread = str(params.get("thread") or "").strip()
        if not thread:
            return {"ok": False, "reason": "Пустая переписка"}
        intent = str(params.get("intent") or "").strip()[:300]
        city = str(params.get("city") or "").strip()[:120]
        status = str(params.get("status") or "new").strip()[:20]
        # генерим варианты ответа сразу для копирования
        state = model.status()
        rep = avito_mod.suggest_replies(thread, intent, city,
                                        model_status=state if state.get("ok") else None)
        replies = rep.get("replies") or []
        saved = self.store.add_avito_thread(thread, intent, city, replies, status=status)
        return {"ok": True, "thread": saved, "replies": replies, "reason": "",
                "hint": "Переписка сохранена. Варианты — только для копирования"}

    def _avito_to_order(self, params: dict[str, Any]) -> dict[str, Any]:
        # берём данные объявления из базы или из параметров
        listing_id = int(params.get("listing_id") or 0)
        url = str(params.get("url") or "").strip()
        title = str(params.get("title") or "").strip()
        price = str(params.get("price") or "").strip()
        city = str(params.get("city") or "").strip()

        listing = None
        if listing_id:
            rows = self.store.list_avito_listings(limit=1)
            # ищем по id
            all_rows = self.store._rows("SELECT * FROM avito_listings WHERE id=?", (listing_id,))
            listing = all_rows[0] if all_rows else None
        if listing:
            title = title or str(listing.get("title") or "")
            url = url or str(listing.get("url") or "")
            price = price or str(listing.get("price") or "")
            city = city or str(listing.get("city") or "")

        if not title and not url:
            return {"ok": False, "reason": "Не указано объявление: нужен listing_id или url/title"}

        # формируем черновик заказа для панели
        draft = {
            "product": title[:200] or "Заказ с Авито",
            "notes": f"Авито: {title} {url} {price} {city}".strip()[:1000],
            "source": "avito",
        }
        # через панель: order/save требует confirmed, но навык сам с confirm
        action, why = self.panel.find_action("order_save")
        if action is None:
            # если панель недоступна — возвращаем черновик для ручного сохранения
            return {"ok": True, "draft": draft, "reason": "",
                    "hint": f"Панель недоступна ({why}) — черновик для копирования",
                    "panel_available": False}

        res = self.panel.run_action(action, {"draft": draft}, confirmed=True)
        if not res.get("ok"):
            return {"ok": False, "reason": res.get("reason") or "Панель не создала заказ",
                    "draft": draft}
        return {"ok": True, "draft": draft, "result": res.get("result"), "reason": "",
                "hint": "Черновик заказа создан в PrintFlow, проверьте и сохраните"}

    def _avito_schedule(self, params: dict[str, Any]) -> dict[str, Any]:
        watch_id = int(params.get("watch_id") or 0)
        if not watch_id:
            return {"ok": False, "reason": "Не указан watch_id"}
        w = self.store.get_avito_watch(watch_id)
        if not w:
            return {"ok": False, "reason": f"Слежки {watch_id} нет"}
        interval = int(params.get("interval_hours") or 0)
        notify = params.get("notify")
        if "notify" in params:
            notify = bool(notify)
        else:
            notify = None
        ok = self.store.set_avito_watch_schedule(watch_id, interval, notify)
        return {"ok": bool(ok), "watch_id": watch_id, "interval_hours": interval,
                "notify": notify, "reason": "" if ok else "Не удалось обновить расписание"}

    def _avito_notify(self, params: dict[str, Any]) -> dict[str, Any]:
        watch_id = int(params.get("watch_id") or 0)
        if not watch_id:
            return {"ok": False, "reason": "Не указан watch_id"}
        enabled = bool(params.get("enabled")) if "enabled" in params else True
        ok = self.store.set_avito_watch_schedule(watch_id, interval_hours=self.store.get_avito_watch(watch_id).get("check_interval_hours", 0) if self.store.get_avito_watch(watch_id) else 0,
                                                 notify=enabled)
        # второй вызов для notify отдельно если interval не трогаем
        if not ok:
            ok = self.store.set_avito_watch_schedule(watch_id, interval_hours=0, notify=enabled)
        return {"ok": bool(ok), "watch_id": watch_id, "enabled": enabled, "reason": ""}

    def _avito_dedup(self, params: dict[str, Any]) -> dict[str, Any]:
        image_hash = str(params.get("image_hash") or "").strip()
        limit = int(params.get("limit") or 20)
        if image_hash:
            rows = self.store.find_duplicate_listings_by_image(image_hash, limit)
            return {"ok": True, "duplicates": rows, "count": len(rows),
                    "image_hash": image_hash, "reason": ""}
        groups = self.store.list_duplicate_image_groups(limit)
        # для каждой группы подтянем примеры
        detailed = []
        for g in groups:
            h = g.get("image_hash") or ""
            items = self.store.find_duplicate_listings_by_image(h, 5)
            detailed.append({"image_hash": h, "count": int(g.get("cnt") or len(items)), "items": items})
        return {"ok": True, "groups": detailed, "count": len(detailed), "reason": "",
                "hint": "Дубли по хешу первых 32КБ фото"}

    # --- 18.16: ТГ календарь, шаблоны, хештеги, поиск, экспорт, статистика ---
    def _tg_schedule(self, params: dict[str, Any]) -> dict[str, Any]:
        draft_id = int(params.get("draft_id") or 0)
        planned_at = str(params.get("planned_at") or "").strip()
        chat = str(params.get("chat") or "").strip()
        chk = tg_mod.schedule_post(draft_id, planned_at, chat)
        if not chk.get("ok"):
            return chk
        saved = self.store.add_tg_schedule(draft_id, chk["planned_at"], chk["chat"])
        return {"ok": True, "schedule": saved, "reason": "", "hint": "Пост запланирован"}

    def _tg_schedules(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("limit") or 30)
        status = str(params.get("status") or "").strip()
        rows = self.store.list_tg_schedules(limit, status)
        return {"ok": True, "schedules": rows, "count": len(rows), "reason": ""}

    def _tg_template_save(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        tone = str(params.get("tone") or "").strip()
        template = str(params.get("template") or "").strip()
        chk = tg_mod.save_template(name, tone, template)
        if not chk.get("ok"):
            return chk
        saved = self.store.add_tg_template(chk["name"], chk["tone"], chk["template_text"], chk["vars"])
        return {"ok": True, "template": saved, "reason": ""}

    def _tg_templates(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("limit") or 50)
        rows = self.store.list_tg_templates(limit)
        # добавим встроенные
        builtin = [{"id": 0, "name": b["name"], "tone": b["tone"], "template_text": b["template"],
                    "vars": b["vars"], "builtin": True} for b in tg_mod.BUILTIN_TEMPLATES]
        all_rows = builtin + rows
        return {"ok": True, "templates": all_rows[:limit], "count": len(all_rows[:limit]), "reason": ""}

    def _tg_template_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        template_id = int(params.get("template_id") or 0)
        facts = str(params.get("facts") or "").strip()[:1000]
        topic = str(params.get("topic") or "").strip()[:300]
        tone = str(params.get("tone") or "").strip()

        tmpl_text = ""
        tmpl_tone = tone
        if template_id:
            if template_id < 0:
                return {"ok": False, "reason": "Неверный template_id"}
            # 0 — встроенные не в базе, берём по индексу? для простоты требуем id из базы
            t = self.store.get_tg_template(template_id)
            if not t:
                # проверим встроенные по порядку
                if 1 <= template_id <= len(tg_mod.BUILTIN_TEMPLATES):
                    b = tg_mod.BUILTIN_TEMPLATES[template_id-1]
                    tmpl_text = b["template"]
                    tmpl_tone = b["tone"]
                else:
                    return {"ok": False, "reason": f"Шаблона {template_id} нет"}
            else:
                tmpl_text = str(t.get("template_text") or "")
                tmpl_tone = t.get("tone") or tone

        if not tmpl_text:
            # если без id — берём первый встроенный
            tmpl_text = tg_mod.BUILTIN_TEMPLATES[0]["template"]
            tmpl_tone = tg_mod.BUILTIN_TEMPLATES[0]["tone"]

        # подставляем переменные
        vars_dict = {"topic": topic or "новинка", "fact": facts or "факты цеха",
                     "detail": "Фото в карусели", "material": "PETG", "time": "4ч",
                     "price": "от 300 ₽", "source": "запрос клиента", "action": "напечатали партию"}
        applied = tg_mod.apply_template(tmpl_text, vars_dict)
        if not applied.get("ok"):
            return applied
        text = applied["text"]
        # создаём черновик
        draft = self.store.add_tg_draft(topic or "Из шаблона", tmpl_tone, text, source="template")
        return {"ok": True, "draft": draft, "draft_id": draft["id"], "text": text,
                "missing": applied.get("missing") or [], "reason": ""}

    def _tg_hashtags(self, params: dict[str, Any]) -> dict[str, Any]:
        txt = str(params.get("text") or "").strip()
        limit = int(params.get("limit") or 6)
        return tg_mod.generate_hashtags(txt, limit)

    def _tg_search(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query") or "").strip()
        limit = int(params.get("limit") or 20)
        rows = self.store.search_tg_drafts(query, limit)
        return {"ok": True, "results": rows, "count": len(rows), "query": query, "reason": ""}

    def _tg_export(self, params: dict[str, Any]) -> dict[str, Any]:
        status = str(params.get("status") or "").strip()
        limit = int(params.get("limit") or 100)
        fmt = str(params.get("format") or "md").strip()
        rows = self.store.export_tg_drafts(status, limit)
        res = tg_mod.export_drafts_text(rows, fmt)
        res["drafts"] = rows
        return res

    def _tg_stats(self, _params: dict[str, Any]) -> dict[str, Any]:
        stats = self.store.tg_ideas_stats()
        return {"ok": True, "stats": stats, "reason": "",
                "hint": "Конверсия идей→черновики→посты"}

    def _tg_idea_save(self, params: dict[str, Any]) -> dict[str, Any]:
        context = str(params.get("context") or "").strip()[:600]
        idea = str(params.get("idea") or params.get("idea_text") or "").strip()
        if not idea:
            return {"ok": False, "reason": "Пустая идея"}
        saved = self.store.add_tg_idea(context, idea)
        return {"ok": True, "idea": saved, "reason": ""}

    def _tg_ideas_history(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = int(params.get("limit") or 30)
        status = str(params.get("status") or "").strip()
        rows = self.store.list_tg_ideas(limit, status)
        stats = self.store.tg_ideas_stats()
        return {"ok": True, "ideas": rows, "count": len(rows), "stats": stats, "reason": ""}


    # --- 18.17: система (И206, И208, И209) + голос+окна+зрение+буфер ---------
    def _system_autostart(self, params: dict) -> dict:
        if "enabled" not in params:
            # Без явного «включить/выключить» реестр Windows не трогаем: вызов
            # навыка пустым (тест диспетчера, ошибка модели) не должен ничего менять.
            try:
                from . import system as sys_mod
                on, reason = sys_mod.autostart_status(str(params.get("app_name") or "PrintFlowAssistant"))
                return {"ok": False, "enabled": on, "reason": reason or "Скажите явно: включить или выключить автозагрузку (enabled)"}
            except Exception as exc:
                return {"ok": False, "reason": f"Автозагрузка не прочитана: {exc}"}
        enabled = bool(params.get("enabled"))
        app_name = str(params.get("app_name") or "PrintFlowAssistant").strip()[:120]
        try:
            from . import system as sys_mod
            if enabled:
                ok, reason = sys_mod.autostart_enable(app_name)
            else:
                ok, reason = sys_mod.autostart_disable(app_name)
            # сохраняем флаг в preferences
            try:
                self.store.set_preference(f"autostart.{app_name}", "1" if enabled and ok else "0")
            except Exception:
                pass
            return {"ok": ok, "enabled": enabled, "app_name": app_name, "reason": reason,
                    "hint": "Автозагрузка включена" if enabled and ok else ("Выключена" if ok else reason)}
        except Exception as exc:
            return {"ok": False, "reason": f"Автозагрузка не настроена: {exc}"}

    def _system_process_list(self, params: dict) -> dict:
        limit = int(params.get("limit") or 20)
        try:
            from . import system as sys_mod
            procs, reason = sys_mod.process_list(limit)
            if reason and not procs:
                return {"ok": False, "reason": reason}
            return {"ok": True, "processes": procs, "count": len(procs), "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Список процессов не получен: {exc}"}

    def _system_audio_device(self, params: dict) -> dict:
        device_id = str(params.get("device_id") or "").strip()
        try:
            from . import system as sys_mod
            devs, reason = sys_mod.audio_devices()
            if device_id:
                # переключение — заглушка, пишем в preferences
                try:
                    self.store.set_preference("audio.device", device_id)
                except Exception:
                    pass
                return {"ok": True, "device_id": device_id, "devices": devs, "reason": "",
                        "hint": f"Устройство {device_id} выбрано (требует перезапуска звука)"}
            return {"ok": True, "devices": devs, "count": len(devs), "reason": reason,
                    "hint": "Список устройств вывода"}
        except Exception as exc:
            return {"ok": False, "reason": f"Аудио-устройства не получены: {exc}"}

    def _system_volume(self, params: dict) -> dict:
        """Общая громкость: уровень, «громче/тише» на дельту, выключить/включить звук."""
        mute = str(params.get("mute") or "").strip()
        if mute:
            state, reason = pc.mute_set(None if mute == "toggle" else mute == "on")
            if reason:
                return {"ok": False, "reason": reason}
            return {"ok": True, **state, "reason": "",
                    "hint": "Звук выключен" if state.get("muted") else "Звук включён"}
        level = params.get("level")
        delta = params.get("delta")
        if level is None and delta:
            current, reason = pc.volume_get()
            if current.get("level") is not None:
                level = int(current["level"]) + int(delta)
            else:
                title, reason = pc.media("volume_up" if int(delta) > 0 else "volume_down",
                                         max(1, abs(int(delta)) // 2))
                if reason:
                    return {"ok": False, "reason": reason}
                return {"ok": True, "level": None, "approximate": True, "reason": "", "hint": title}
        if level is None:
            state, reason = pc.volume_get()
            if reason:
                return {"ok": False, "reason": reason}
            return {"ok": True, **state, "reason": "", "hint": f"Громкость {state.get('level')}%"}
        state, reason = pc.volume_set(max(0, min(100, int(level))))
        if reason and not state:
            return {"ok": False, "reason": reason}
        return {"ok": True, **state, "reason": "", "hint": f"Громкость {state.get('level')}%"}

    def _system_display(self, _params: dict) -> dict:
        try:
            import ctypes
            if sys_mod_is_win := __import__("sys").platform.startswith("win"):
                user32 = ctypes.windll.user32
                w = user32.GetSystemMetrics(0)
                h = user32.GetSystemMetrics(1)
                cnt = user32.GetSystemMetrics(80) if hasattr(user32, "GetSystemMetrics") else 1
                return {"ok": True, "width": w, "height": h, "monitors": cnt, "reason": ""}
            return {"ok": True, "width": 0, "height": 0, "monitors": 0, "reason": "Только Windows"}
        except Exception as exc:
            return {"ok": False, "reason": f"Дисплей не прочитан: {exc}"}

    def _system_focus(self, params: dict) -> dict:
        minutes = max(1, min(240, int(params.get("minutes") or 30)))
        try:
            self.store.set_preference("focus.enabled", "1")
            self.store.set_preference("focus.minutes", str(minutes))
            # также создаём таймер фокуса
            timer = self.store.add_focus_timer(minutes, note="Не беспокоить")
            return {"ok": True, "minutes": minutes, "timer": timer, "reason": "",
                    "hint": f"Фокус на {minutes} мин"}
        except Exception as exc:
            return {"ok": False, "reason": f"Фокус не включён: {exc}"}

    def _system_power(self, params: dict) -> dict:
        """Питание — только названное действие и только после подтверждения (см. `run`)."""
        action = str(params.get("action") or "").strip().lower()
        if not action:
            return {"ok": False, "reason": "Скажите, что сделать: " + ", ".join(pc.POWER_ACTIONS)}
        done, reason = pc.power(action)
        if reason:
            return {"ok": False, "action": action, "reason": reason}
        hint = done
        if action in ("restart", "shutdown"):
            hint += ". Передумали — скажите «отмени выключение» в течение минуты"
        return {"ok": True, "action": action, "done": done, "reason": "", "hint": hint}

    def _system_health(self, _params: dict) -> dict:
        """Снимок здоровья: процессор, память, диски, время работы, батарея."""
        state, reason = pc.health()
        if reason:
            return {"ok": False, "reason": reason}
        warnings = pc.health_warnings(state)
        return {"ok": True, **state, "warnings": warnings, "reason": "",
                # Поля 18.17 оставлены для панели и старых клиентов.
                "disk_total_gb": int((state.get("disks") or [{}])[0].get("total_gb") or 0),
                "disk_free_gb": int((state.get("disks") or [{}])[0].get("free_gb") or 0),
                "hint": "Всё спокойно" if not warnings else "Внимание: " + "; ".join(warnings)}

    def _window_active(self, _params: dict) -> dict:
        try:
            from .winapi import active_window
            title, reason = active_window()
            if reason:
                return {"ok": False, "reason": reason}
            return {"ok": True, "title": title, "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Активное окно не прочитано: {exc}"}

    def _window_list(self, params: dict) -> dict:
        limit = int(params.get("limit") or 20)
        try:
            from .winapi import list_windows
            titles, reason = list_windows(limit)
            if reason:
                return {"ok": False, "reason": reason}
            return {"ok": True, "windows": titles, "count": len(titles), "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Список окон не получен: {exc}"}

    def _window_focus(self, params: dict) -> dict:
        title = str(params.get("title") or "").strip()
        if not title:
            return {"ok": False, "reason": "Не указано окно: скажите название программы или часть заголовка"}
        row, reason = pc.focus(title)
        if row is None:
            return {"ok": False, "reason": reason}
        return {"ok": not reason, "title": row["title"], "process": row.get("process", ""),
                "reason": reason, "hint": f"На переднем плане: {row['title']}" if not reason else reason}

    def _window_text(self, params: dict) -> dict:
        title = str(params.get("title") or "").strip()
        state, reason = pc.window_text(title)
        if reason and not state:
            return {"ok": False, "reason": reason}
        return {"ok": True, **state, "reason": "",
                "hint": "Текст элементов окна" if state.get("text") else "Окно не отдаёт текст элементов"}

    def _window_controls(self, params: dict) -> dict:
        title = str(params.get("title") or "").strip()
        rows, reason = pc.controls(title)
        if reason and not rows:
            return {"ok": False, "reason": reason}
        return {"ok": True, "controls": rows, "count": len(rows), "reason": "",
                "hint": (f"Элементов: {len(rows)}" if rows
                         else "Программа рисует интерфейс сама — стандартных элементов нет")}

    def _window_click(self, params: dict) -> dict:
        if params.get("x") is None or params.get("y") is None:
            # Раньше пустой вызов кликал в угол экрана (0, 0) — туда, где у
            # Windows меню «Пуск» и горячий угол. Координаты обязательны.
            return {"ok": False, "reason": "Нужны координаты x и y: куда кликать, помощник не угадывает"}
        x = int(params.get("x"))
        y = int(params.get("y"))
        # проверка whitelist
        try:
            from .winapi import active_window
            title, _ = active_window()
            if title:
                # если whitelist не пуст — требуем разрешения
                wl = self.store.list_whitelist(100)
                if wl:
                    allowed = any(w.get("app_name","").lower() in title.lower() and w.get("allowed") for w in wl)
                    if not allowed:
                        return {"ok": False, "reason": f"Окно «{title}» не в белом списке — добавьте через safety.whitelist_save"}
        except Exception:
            pass
        try:
            from .winapi import click
            ok, reason = click(x, y)
            return {"ok": ok, "x": x, "y": y, "reason": reason, "hint": "Клик выполнен" if ok else reason}
        except Exception as exc:
            return {"ok": False, "reason": f"Клик не выполнен: {exc}"}

    def _window_type(self, params: dict) -> dict:
        txt = str(params.get("text") or "")
        if not txt:
            return {"ok": False, "reason": "Пустой текст для ввода"}
        try:
            from .winapi import type_text
            ok, reason = type_text(txt)
            return {"ok": ok, "text": txt[:200], "reason": reason, "hint": "Ввод выполнен" if ok else reason}
        except Exception as exc:
            return {"ok": False, "reason": f"Ввод не выполнен: {exc}"}

    def _window_snap(self, params: dict) -> dict:
        left_title = str(params.get("left_title") or "").strip()
        right_title = str(params.get("right_title") or "").strip()
        if not left_title or not right_title:
            return {"ok": False, "reason": "Нужны два окна: left_title и right_title"}
        state, reason = pc.snap(left_title, right_title)
        if reason:
            return {"ok": False, "reason": reason}
        return {"ok": True, "left": state["left"], "right": state["right"], "reason": "",
                "hint": "Окна разложены 50/50"}

    def _screen_shot(self, params: dict) -> dict:
        max_side = int(params.get("max_side") or 800)
        try:
            from .winapi import grab_screen
            data, reason = grab_screen(max_side)
            if reason:
                return {"ok": False, "reason": reason}
            # не сохраняем картинку, только метаданные в архив
            return {"ok": True, "size": len(data), "reason": "", "hint": "Снимок получен, на диск не сохранён"}
        except Exception as exc:
            return {"ok": False, "reason": f"Снимок не получился: {exc}"}

    def _screen_region_shot(self, params: dict) -> dict:
        left = int(params.get("left") or 0)
        top = int(params.get("top") or 0)
        right = int(params.get("right") or 0)
        bottom = int(params.get("bottom") or 0)
        try:
            from .screen import region_shot
            data, reason = region_shot(left, top, right, bottom)
            if reason:
                return {"ok": False, "reason": reason}
            return {"ok": True, "size": len(data), "rect": {"left": left, "top": top, "right": right, "bottom": bottom}, "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Снимок области не получился: {exc}"}

    def _screen_describe(self, params: dict) -> dict:
        """Описание экрана только видящей моделью: текстовая раньше «описывала» наугад."""
        max_side = max(320, min(1600, int(params.get("max_side") or 1024)))
        seeing, why = model.vision_ok()
        if not seeing:
            window, _reason = winapi.active_window()
            return {"ok": False, "reason": why, "window": window,
                    "hint": f"Активное окно: {window}" if window else ""}
        try:
            data, reason = winapi.grab_screen(max_side)
        except Exception as exc:
            return {"ok": False, "reason": f"Снимок не получился: {exc}"}
        if reason:
            return {"ok": False, "reason": reason}
        question = str(params.get("question") or "").strip()[:300]
        reply = model.chat(
            [{"role": "user", "content": (question or "Что на этом снимке экрана? Назови программу, "
                                          "главное содержимое и то, что требует внимания.")}],
            system=("Ты описываешь снимок экрана владельца мастерской. Отвечай по-русски, "
                    "два-четыре предложения, только то, что видно на снимке. Не выдумывай."),
            images=[data], temperature=0.1)
        return {"ok": bool(reply.get("ok")), "description": reply.get("text") or "", "size": len(data),
                "model": reply.get("model") or "", "reason": reply.get("reason") or ""}

    def _screen_find(self, params: dict) -> dict:
        txt = str(params.get("text") or "").strip()
        if not txt:
            return {"ok": False, "reason": "Пустой запрос"}
        try:
            from .screen import find_text_on_screen
            found, reason = find_text_on_screen(txt)
            if reason and not found:
                return {"ok": False, "reason": reason}
            method = "ocr" if self.caps.get("ocr") else "titles"
            return {"ok": True, "found": found, "method": method, "reason": "",
                    "hint": "" if method == "ocr" else "Без распознавания текста ищу только в заголовках окон"}
        except Exception as exc:
            return {"ok": False, "reason": f"Поиск не удался: {exc}"}

    def _screen_find_and_click(self, params: dict) -> dict:
        txt = str(params.get("text") or "").strip()
        if not txt:
            return {"ok": False, "reason": "Пустой запрос"}
        # сначала найдём
        find_res = self._screen_find(params)
        if not find_res.get("ok"):
            return find_res
        # проверка whitelist
        try:
            from .winapi import active_window
            title, _ = active_window()
            if title:
                wl = self.store.list_whitelist(100)
                if wl:
                    allowed = any(w.get("app_name","").lower() in title.lower() and w.get("allowed") for w in wl)
                    if not allowed:
                        return {"ok": False, "reason": f"Окно «{title}» не в белом списке"}
        except Exception:
            pass
        # клик — заглушка, так как координаты неизвестны без OCR
        return {"ok": False, "reason": "Клик по найденному тексту требует OCR — пока только поиск в заголовках окон", "found": find_res.get("found")}

    def _screen_archive(self, params: dict) -> dict:
        title = str(params.get("title") or "").strip()[:300]
        try:
            entry = self.store.add_screen_archive(title=title or "скрин", path="", hash_val="")
            return {"ok": True, "archive": entry, "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Архив не сохранён: {exc}"}

    def _screen_archive_search(self, params: dict) -> dict:
        query = str(params.get("query") or "").strip()
        limit = int(params.get("limit") or 20)
        try:
            rows = self.store.list_screen_archive(limit*2)
            if query:
                q = query.casefold()
                rows = [r for r in rows if q in str(r.get("title") or "").casefold()]
            return {"ok": True, "archives": rows[:limit], "count": len(rows[:limit]), "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Поиск по архиву не удался: {exc}"}

    def _screen_archive_erase(self, _params: dict) -> dict:
        try:
            cnt = self.store.clear_screen_archive()
            return {"ok": True, "deleted": cnt, "reason": "", "hint": "Архив стёрт"}
        except Exception as exc:
            return {"ok": False, "reason": f"Стереть не удалось: {exc}"}

    def _voice_listen(self, params: dict) -> dict:
        """Послушать микрофон названное число секунд и распознать речь локально."""
        if params.get("seconds") is None:
            return {"ok": False, "reason": "Скажите, сколько секунд слушать (seconds): микрофон не открывается «на всякий случай»"}
        seconds = max(1, min(30, int(params.get("seconds"))))
        try:
            from . import speech as speech_mod
            recognizer = speech_mod.Recognizer()
            text, reason = speech_mod.record_and_transcribe(recognizer, seconds)
        except Exception as exc:
            return {"ok": False, "reason": f"Слушать не удалось: {exc}", "seconds": seconds}
        if reason:
            return {"ok": False, "reason": reason, "seconds": seconds}
        return {"ok": True, "text": text, "seconds": seconds, "reason": "",
                "hint": f"Услышал: «{text}»" if text else "Речь не распознана"}

    def _voice_say(self, params: dict) -> dict:
        txt = str(params.get("text") or "").strip()
        tone = str(params.get("tone") or "").strip()
        if not txt:
            return {"ok": False, "reason": "Пустой текст для озвучки"}
        try:
            if tone:
                self.store.set_preference("voice.tone", tone)
            prefs = {row["key"]: row["value"] for row in self.store.list_preferences(100)}
        except Exception:
            prefs = {}
        rate = {"быстро": 3, "fast": 3, "медленно": -3, "slow": -3}.get(str(prefs.get("voice.speed") or "").casefold(), 0)
        if tone.casefold() in ("тревога", "alarm", "срочно"):
            rate += 1
        try:
            volume = int(prefs.get("voice.volume") or 100)
        except (TypeError, ValueError):
            volume = 100
        state, reason = pc.speak(txt, rate=rate, volume=volume)
        if reason:
            return {"ok": False, "text": txt[:500], "spoken": False, "reason": reason}
        return {"ok": True, "text": txt[:500], "tone": tone, "spoken": True, **state, "reason": "",
                "hint": "Говорю вслух"}

    def _voice_dictate(self, params: dict) -> dict:
        """Диктовка: послушать и ввести в активное окно (подтверждение уже получено в `run`)."""
        heard = self._voice_listen(params)
        if not heard.get("ok"):
            return heard
        text = str(heard.get("text") or "").strip()
        if not text:
            return {"ok": False, "reason": "Речь не распознана — вводить нечего"}
        ok, reason = winapi.type_text(text)
        return {"ok": ok, "text": text, "reason": reason, "hint": f"Введено: «{text[:80]}»" if ok else reason}

    def _voice_note(self, params: dict) -> dict:
        txt = str(params.get("text") or "").strip()
        due = str(params.get("due") or "").strip()
        if not txt:
            return {"ok": False, "reason": "Пустая заметка"}
        try:
            note = self.store.add_note(txt, due)
            return {"ok": True, "note": note, "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Заметка не сохранена: {exc}"}

    def _voice_command(self, params: dict) -> dict:
        """Фраза → понятый навык и параметры. Ничего не выполняет — только разбор."""
        txt = str(params.get("text") or "").strip()
        if not txt:
            return {"ok": False, "reason": "Пустая команда"}
        from . import brain
        plan = brain.understand(txt)
        if plan is None:
            return {"ok": True, "command": txt, "plan": None, "reason": "",
                    "hint": "Правила фразу не поняли — окно помощника спросит модель"}
        skill = skills.get(plan["skill"]) or {}
        # Понятое кладётся в `plan`, а не в `skill`/`params`: эти поля ответа
        # принадлежат самому навыку `voice.command` (их заполняет `Runner.run`).
        return {"ok": True, "command": txt,
                "plan": {"skill": plan["skill"], "params": plan["params"],
                         "title": skill.get("title", plan["skill"]),
                         "confirm": skills.confirm_required(skill) if skill else False},
                "reason": "", "hint": f"Понял как «{skill.get('title', plan['skill'])}»"}

    def _voice_profile(self, params: dict) -> dict:
        speed = str(params.get("speed") or "").strip()
        tone = str(params.get("tone") or "").strip()
        volume = params.get("volume")
        try:
            if speed:
                self.store.set_preference("voice.speed", speed)
            if tone:
                self.store.set_preference("voice.tone", tone)
            if volume is not None:
                self.store.set_preference("voice.volume", str(int(volume)))
            prefs = self.store.list_preferences(100)
            voice_prefs = {p["key"]: p["value"] for p in prefs if str(p["key"]).startswith("voice.")}
            return {"ok": True, "profile": voice_prefs, "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Профиль не сохранён: {exc}"}

    def _clipboard_history(self, params: dict) -> dict:
        limit = int(params.get("limit") or 20)
        try:
            rows = self.store.list_clipboard(limit)
            return {"ok": True, "history": rows, "count": len(rows), "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"История буфера не прочитана: {exc}"}

    def _clipboard_read(self, _params: dict) -> dict:
        try:
            from . import clipboard as cb_mod
            txt, reason = cb_mod.get_text()
            if reason:
                return {"ok": False, "reason": reason}
            entry = self.store.add_clipboard(txt, source_app="")
            return {"ok": True, "text": txt[:4000], "entry": entry, "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Буфер не прочитан: {exc}"}

    def _clipboard_write(self, params: dict) -> dict:
        txt = str(params.get("text") or "")
        if not txt:
            return {"ok": False, "reason": "Пустой текст"}
        try:
            from . import clipboard as cb_mod
            ok, reason = cb_mod.set_text(txt)
            if not ok:
                return {"ok": False, "reason": reason}
            entry = self.store.add_clipboard(txt, source_app="assistant")
            return {"ok": True, "text": txt[:4000], "entry": entry, "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Буфер не записан: {exc}"}

    def _focus_timer(self, params: dict) -> dict:
        minutes = max(1, min(240, int(params.get("minutes") or 25)))
        note = str(params.get("note") or "").strip()[:400]
        try:
            timer = self.store.add_focus_timer(minutes, note)
            return {"ok": True, "timer": timer, "reason": "", "hint": f"Фокус на {minutes} мин"}
        except Exception as exc:
            return {"ok": False, "reason": f"Таймер не запущен: {exc}"}

    def _focus_list(self, params: dict) -> dict:
        limit = int(params.get("limit") or 20)
        try:
            rows = self.store.list_focus_timers(limit)
            return {"ok": True, "timers": rows, "count": len(rows), "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Список таймеров не получен: {exc}"}

    def _focus_stop(self, params: dict) -> dict:
        timer_id = int(params.get("timer_id") or 0)
        if not timer_id:
            running = [row for row in self.store.list_focus_timers(20) if row.get("status") == "running"]
            if not running:
                return {"ok": False, "reason": "Запущенных таймеров нет"}
            timer_id = int(running[0]["id"])
        try:
            ok = self.store.stop_focus_timer(timer_id)
            return {"ok": ok, "timer_id": timer_id, "reason": "" if ok else "Таймер не найден"}
        except Exception as exc:
            return {"ok": False, "reason": f"Таймер не остановлен: {exc}"}

    def _files_watch(self, params: dict) -> dict:
        path = str(params.get("path") or "").strip()
        if not path:
            return {"ok": False, "reason": "Не указан путь"}
        enabled = bool(params.get("enabled")) if "enabled" in params else True
        try:
            from . import fileops as fo
            allowed, why, resolved = fo.inside_allowed(path)
            if not allowed:
                return {"ok": False, "reason": why}
            entry = self.store.add_file_watch(str(resolved), enabled)
            return {"ok": True, "watch": entry, "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Слежка не добавлена: {exc}"}

    def _files_watches(self, params: dict) -> dict:
        limit = int(params.get("limit") or 20)
        try:
            rows = self.store.list_file_watches(limit)
            return {"ok": True, "watches": rows, "count": len(rows), "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Список слежек не получен: {exc}"}

    def _files_quick_open(self, params: dict) -> dict:
        name = str(params.get("name") or "").strip()
        if not name:
            return {"ok": False, "reason": "Не указано имя файла"}
        limit = max(1, min(50, int(params.get("limit") or 10)))
        try:
            from . import config as cfg
            from pathlib import Path
            needle = name.casefold()
            found = []
            for folder in cfg.file_folders():
                try:
                    base = Path(folder)
                    if not base.exists():
                        continue
                    for p in base.rglob("*"):
                        if not p.is_file():
                            continue
                        if needle in p.name.casefold():
                            found.append({"path": str(p), "name": p.name, "size": p.stat().st_size})
                            if len(found) >= limit:
                                break
                    if len(found) >= limit:
                        break
                except Exception:
                    continue
            return {"ok": True, "files": found, "count": len(found), "query": name, "reason": "" if found else "Ничего не нашлось"}
        except Exception as exc:
            return {"ok": False, "reason": f"Поиск не удался: {exc}"}

    def _knowledge_preferences(self, params: dict) -> dict:
        key = str(params.get("key") or "").strip()
        try:
            if key:
                row = self.store.get_preference(key)
                if not row:
                    return {"ok": True, "preference": None, "reason": f"Ключа {key} нет"}
                return {"ok": True, "preference": row, "reason": ""}
            rows = self.store.list_preferences(200)
            return {"ok": True, "preferences": rows, "count": len(rows), "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Предпочтения не прочитаны: {exc}"}

    def _knowledge_preference_save(self, params: dict) -> dict:
        key = str(params.get("key") or "").strip()
        value = str(params.get("value") or "").strip()
        if not key:
            return {"ok": False, "reason": "Не указан ключ"}
        if not value:
            return {"ok": False, "reason": "Пустое значение"}
        try:
            saved = self.store.set_preference(key, value)
            return {"ok": True, "preference": saved, "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Предпочтение не сохранено: {exc}"}

    def _safety_whitelist(self, params: dict) -> dict:
        limit = int(params.get("limit") or 100)
        try:
            rows = self.store.list_whitelist(limit)
            return {"ok": True, "whitelist": rows, "count": len(rows), "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Белый список не прочитан: {exc}"}

    def _safety_whitelist_save(self, params: dict) -> dict:
        app_name = str(params.get("app_name") or "").strip()
        if not app_name:
            return {"ok": False, "reason": "Не указано имя приложения"}
        allowed = bool(params.get("allowed")) if "allowed" in params else True
        try:
            saved = self.store.set_whitelist(app_name, allowed)
            return {"ok": True, "entry": saved, "reason": "", "hint": "Белый список обновлён"}
        except Exception as exc:
            return {"ok": False, "reason": f"Белый список не сохранён: {exc}"}

    def _assistant_macro(self, params: dict) -> dict:
        name = str(params.get("name") or "").strip()
        if not name:
            return {"ok": False, "reason": "Не указано имя макроса"}
        steps = params.get("steps")
        if not isinstance(steps, list) or not steps:
            return {"ok": False, "reason": "Нужен непустой список шагов"}
        if len(steps) > 8:
            return {"ok": False, "reason": "Больше 8 шагов — это программа, а не макрос"}
        desc = str(params.get("description") or "").strip()[:600]
        try:
            # валидируем шаги через skills.learn логику
            for idx, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    return {"ok": False, "reason": f"Шаг {idx}: не структура"}
                sname = str(step.get("skill") or "").strip().casefold()
                if not sname:
                    return {"ok": False, "reason": f"Шаг {idx}: нет skill"}
                # проверим что навык существует
                from . import skills as sk
                if sname not in sk.SKILLS:
                    # проверим выученные
                    learned = self.store.learned_skills()
                    if sname not in learned:
                        return {"ok": False, "reason": f"Шаг {idx}: навыка {sname} нет"}
            saved = self.store.save_macro(name, steps, desc)
            return {"ok": True, "macro": saved, "reason": "", "hint": f"Макрос {name} сохранён"}
        except Exception as exc:
            return {"ok": False, "reason": f"Макрос не сохранён: {exc}"}

    def _assistant_macros(self, params: dict) -> dict:
        limit = int(params.get("limit") or 20)
        try:
            rows = self.store.list_macros(limit)
            return {"ok": True, "macros": rows, "count": len(rows), "reason": ""}
        except Exception as exc:
            return {"ok": False, "reason": f"Макросы не прочитаны: {exc}"}

    def _assistant_macro_run(self, params: dict) -> dict:
        name = str(params.get("name") or "").strip()
        if not name:
            return {"ok": False, "reason": "Не указано имя макроса"}
        try:
            macro = self.store.get_macro(name)
            if not macro:
                return {"ok": False, "reason": f"Макроса {name} нет"}
            steps = macro.get("steps") or []
            done = []
            for idx, step in enumerate(steps, start=1):
                res = self.run(str(step.get("skill") or ""), step.get("params") or {}, confirmed=True)
                done.append({"step": idx, "skill": step.get("skill"), "ok": res.get("ok"), "reason": res.get("reason") or ""})
                if not res.get("ok"):
                    return {"ok": False, "steps": done, "reason": f"Шаг {idx} не выполнен: {res.get('reason')}"}
            return {"ok": True, "steps": done, "reason": "", "hint": f"Макрос {name}: выполнено {len(done)} шагов"}
        except Exception as exc:
            return {"ok": False, "reason": f"Макрос не выполнен: {exc}"}


    def _system_check(self, _params: dict) -> dict:
        """Что установлено для полноценного ассистента (И222) — только чтение."""
        try:
            from . import install as install_module
            from . import capabilities as capabilities_module
            state = install_module.check()
            caps = capabilities_module.detect()
            missing = capabilities_module.missing(caps)
            return {"ok": True, "check": state, "capabilities": caps, "missing": missing,
                    "reason": "", "hint": "Не хватает — видно в missing; ставит только недостающее system.install"}
        except Exception as exc:
            return {"ok": False, "reason": f"Проверка зависимостей не удалась: {exc}"}

    def _system_install(self, params: dict) -> dict:
        """Автоустановка (И222): pip ставит только то, чего нет; модели — по слову."""
        what = str(params.get("what") or "").strip().lower()
        if not what:
            # Пустой вызов раньше означал «pip»: тест диспетчера на машине
            # разработчика ставил пакеты по-настоящему. Что ставить — называется явно.
            return {"ok": False, "reason": "Скажите, что ставить: pip, requirements, models или full (what)"}
        if what not in ("pip", "requirements", "models", "full"):
            return {"ok": False, "reason": f"what должен быть pip|requirements|models|full, а не {what}"}
        confirm_text = str(params.get("confirm_text") or "").strip().casefold()
        if what in ("models", "full") and confirm_text != "установить":
            return {"ok": False, "needs_text": True, "needs_confirmation": True,
                    "reason": "Скачивание моделей требует слова «установить» в confirm_text"}
        try:
            from . import install as install_module
            if what == "pip":
                result = install_module.install()
            elif what == "requirements":
                result = install_module.install_requirements()
            elif what == "models":
                result = install_module.download_vosk_model()
            else:
                result = install_module.full_setup()
            try:
                self.store.set_preference("install.last", f"{what}:{'ok' if result.get('ok') else 'fail'}")
            except Exception:
                pass
            return {"ok": bool(result.get("ok")), "what": what, "result": result,
                    "reason": "" if result.get("ok") else str(result.get("reason") or "не получилось"),
                    "hint": "Ставится только недостающее; модели речи скачиваются в ~/.printflow/models"}
        except Exception as exc:
            return {"ok": False, "reason": f"Автоустановка не удалась: {exc}"}

    def _system_watchdog(self, params: dict) -> dict:
        """Надзор: кто жив, что в журнале, верить ли самому надзирателю (И263)."""
        mode = str(params.get("mode") or "status").strip().lower()
        try:
            from . import watchdog as wd
        except Exception as exc:
            return {"ok": False, "reason": f"Модуль надзора недоступен: {exc}"}
        try:
            if mode == "log":
                events = wd.tail_log(40)
                self.store.add_watchdog_event("watchdog", "log_read", f"прочитано {len(events)}")
                return {"ok": True, "mode": mode, "events": events, "config": wd.load_config(),
                        "reason": "", "hint": "Журнал надзора: свежие события сверху"}
            if mode in ("self_check", "self-check", "check"):
                report = wd.self_check()
                return {"ok": bool(report.get("ok")), "mode": "self_check", "check": report,
                        "reason": report.get("reason") or "", "hint": report.get("hint") or ""}
            state = wd.status()
            saved = self.store.list_watchdog_events(10)
            return {"ok": True, "mode": "status", "status": state, "saved": saved,
                    "reason": state.get("reason") or "",
                    "hint": "Пульс протух — надзиратель поднимет роль; причина записана в журнал"}
        except Exception as exc:
            return {"ok": False, "reason": f"Надзор не ответил: {exc}"}

    def _system_watchdog_arm(self, params: dict) -> dict:
        """Вооружить или снять надзор. Сам надзиратель — отдельный процесс (И263)."""
        enabled = str(params.get("enabled") or "on").strip().lower()
        if enabled not in ("on", "off", "вкл", "выкл", "true", "false", "да", "нет"):
            return {"ok": False, "reason": f"enabled должен быть on или off, а не {enabled}"}
        want_on = enabled in ("on", "вкл", "true", "да")
        confirm_text = str(params.get("confirm_text") or "").strip().casefold()
        if confirm_text != ("вооружить" if want_on else "снять"):
            word = "вооружить" if want_on else "снять"
            return {"ok": False, "needs_text": True, "needs_confirmation": True,
                    "reason": f"Подтвердите словом «{word}» в confirm_text"}
        try:
            from . import watchdog as wd
            config = wd.save_config(interval_sec=params.get("interval") or None,
                                    stale_sec=params.get("stale_sec") or None,
                                    max_restarts=params.get("max_restarts") or None,
                                    enabled=want_on)
            event = self.store.add_watchdog_event(
                "watchdog", "armed" if want_on else "disarmed",
                f"роли {','.join(config.get('roles') or [])}, интервал {config.get('interval_sec')} с")
            self.store.set_preference("watchdog.enabled", "1" if want_on else "0")
            return {"ok": True, "enabled": want_on, "config": config, "event": event, "reason": "",
                    "hint": "Надзор поднимает только упавшие роли; запуск в цикле — python -m agent.watchdog --watch"}
        except Exception as exc:
            return {"ok": False, "reason": f"Надзор не вооружился: {exc}"}

    def _system_watchdog_once(self, params: dict) -> dict:
        """Подъём упавших по кнопке владельца (И263) — тот же проход, что по таймеру."""
        confirm_text = str(params.get("confirm_text") or "").strip().casefold()
        if confirm_text != "поднять":
            return {"ok": False, "needs_text": True, "needs_confirmation": True,
                    "reason": "Подтвердите словом «поднять» в confirm_text"}
        roles = params.get("roles") or None
        if isinstance(roles, str) and roles.strip():
            roles = [r.strip() for r in roles.split(",") if r.strip()]
        dry = str(params.get("dry") or "off").strip().lower() in ("on", "вкл", "true", "да")
        try:
            from . import watchdog as wd
            report = wd.pass_once(roles=roles, restart=not dry)
            for action in report.get("actions") or []:
                self.store.add_watchdog_event(str(action.get("role") or ""), str(action.get("action") or ""),
                                              str(action.get("reason") or "")[:300])
            self.store.set_preference("watchdog.last_pass", str(report.get("checked_at") or ""))
            return {"ok": True, "dry": dry, "report": report, "reason": "",
                    "hint": "Каждый подъём и каждая причина записаны в журнал надзора"}
        except Exception as exc:
            return {"ok": False, "reason": f"Проход надзора не удался: {exc}"}

        # --- мета (И178, И180) -------------------------------------------------
    def _agent_skills(self, _params: dict[str, Any]) -> dict[str, Any]:
        rows = self.catalog()
        unavailable = [{"name": row["name"], "reason": row["reason"]}
                       for row in rows if not row["available"]]
        return {"ok": True, "skills": rows, "count": len(rows),
                "ready": sum(1 for row in rows if row["available"]),
                "unavailable": unavailable, "reason": "",
                "stats": self.store.stats(),
                "hint": "Недоступные навыки показаны с причиной, а не спрятаны"}

    def _agent_why(self, params: dict[str, Any]) -> dict[str, Any]:
        key = str(params.get("skill") or "").strip().casefold()
        if not key:
            return {"ok": False,
                    "reason": "Укажите навык: agent.why отвечает про конкретный навык"}
        skill = skills.get(key, self.learned())
        if skill is None:
            return {"ok": False, "reason": f"Навыка «{key}» в реестре нет"}
        available, why = skills.availability(skill, self.caps)
        last = self.store.last_outcome(key)
        reasons: list[str] = []
        if not available:
            reasons.append(f"Навык недоступен на этом компьютере: {why}")
        if last and last["outcome"] in ("failed", "refused", "unavailable",
                                        "needs_confirmation"):
            reasons.append(f"Последний вызов ({last['at']}) — {last['outcome']}: {last['detail']}")
        if skills.confirm_required(skill):
            reasons.append("Навык требует подтверждения человека на этом компьютере")
        return {"ok": True, "skill": key, "title": skill["title"],
                "available": available, "capability_reason": why,
                "requires": list(skill.get("requires") or ()),
                "confirm": skills.confirm_required(skill),
                "last": last, "reasons": reasons,
                "reason": "; ".join(reasons) or "Отказов не было: навык доступен и работал",
                "stats": self.store.stats()}

    def _agent_journal(self, params: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(200, int(params.get("limit") or 30)))
        rows = self.store.journal_recent(limit)
        return {"ok": True, "entries": rows, "count": len(rows), "reason": "",
                "stats": self.store.stats(),
                "hint": "Журнал не удаляется навыками: это след ассистента на компьютере"}

    def _agent_learn(self, params: dict[str, Any]) -> dict[str, Any]:
        raw = params.get("skill")
        if not isinstance(raw, dict):
            return {"ok": False, "reason": "Навык описывается структурой: имя, название, шаги"}
        skill, why = skills.learn(raw, self.learned())
        if skill is None:
            return {"ok": False, "reason": why}
        name = str(raw.get("name") or "").strip().casefold()
        saved = self.store.save_skill(name, skill)
        self.refresh_capabilities()
        available, unavailable_reason = skills.availability({"name": name, **skill}, self.caps)
        return {"ok": True, "name": name, "skill": skills.payload({"name": name, **skill},
                                                                   self.caps),
                "steps": skill["steps"], "risk": skill["risk"], "confirm": True,
                "available": available, "reason": "" if available else unavailable_reason,
                "learned_at": saved["learned_at"],
                "hint": "Выученный навык исполняет шаги подряд и всегда спрашивает подтверждение"}

    # --- 18.21: программы, медиа, клавиши, окна, память --------------------
    def _app_open(self, params: dict) -> dict:
        target = str(params.get("target") or "").strip()
        if not target:
            return {"ok": False, "reason": "Скажите, что открыть: программу, сайт или папку"}
        plan, reason = pc.open_target(target, self.panel.url, config.file_folders())
        if reason:
            return {"ok": False, "reason": reason}
        return {"ok": True, "title": plan.get("title"), "kind": plan.get("kind"),
                "target": plan.get("target") or target, "reason": "",
                "hint": f"Открыто: {plan.get('title')}"}

    def _system_media(self, params: dict) -> dict:
        action = str(params.get("action") or "").strip()
        if not action:
            return {"ok": False, "reason": "Скажите, что сделать: " + ", ".join(pc.MEDIA_ACTIONS)}
        done, reason = pc.media(action)
        if reason:
            return {"ok": False, "reason": reason}
        return {"ok": True, "action": action, "done": done, "reason": "", "hint": done}

    def _system_hotkey(self, params: dict) -> dict:
        keys = str(params.get("keys") or "").strip()
        if not keys:
            return {"ok": False, "reason": "Не сказано, какие клавиши нажать"}
        window, _why = winapi.active_window()
        if window and not self._window_allowed(window):
            return {"ok": False, "reason": f"Окно «{window}» не в белом списке — добавьте его через safety.whitelist_save"}
        pressed, reason = pc.press_combo(keys)
        if reason:
            return {"ok": False, "reason": reason}
        return {"ok": True, "keys": pressed, "window": window, "reason": "",
                "hint": f"Нажато {'+'.join(pressed)}" + (f" в «{window}»" if window else "")}

    def _window_arrange(self, params: dict) -> dict:
        action = str(params.get("action") or "").strip()
        if not action:
            return {"ok": False, "reason": "Скажите, что сделать с окном: свернуть, развернуть или восстановить"}
        row, reason = pc.arrange(str(params.get("title") or ""), action)
        if row is None:
            return {"ok": False, "reason": reason}
        return {"ok": True, "title": row["title"], "done": row.get("done"), "action": action, "reason": "",
                "hint": f"«{row['title']}» {row.get('done')}"}

    def _window_close(self, params: dict) -> dict:
        row, reason = pc.close_window(str(params.get("title") or ""))
        if row is None:
            return {"ok": False, "reason": reason}
        return {"ok": True, "title": row["title"], "reason": "",
                "hint": f"Окно «{row['title']}» попросили закрыться"}

    def _memory_remember(self, params: dict) -> dict:
        text = str(params.get("text") or "").strip()
        if not text:
            return {"ok": False, "reason": "Что запомнить? Текст пустой"}
        saved = self.store.remember(text, kind=str(params.get("kind") or "fact"),
                                    subject=str(params.get("subject") or ""), source="skill")
        if not saved.get("ok"):
            return {"ok": False, "reason": saved.get("reason") or "Не запомнил"}
        return {"ok": True, "memory": saved["memory"], "duplicate": bool(saved.get("duplicate")),
                "reason": "", "hint": "Уже помню" if saved.get("duplicate") else "Запомнил"}

    def _memory_recall(self, params: dict) -> dict:
        query = str(params.get("query") or "").strip()
        limit = max(1, min(50, int(params.get("limit") or 10)))
        rows = self.store.recall(query, limit) if query else self.store.memories(limit)
        return {"ok": True, "memories": rows, "count": len(rows), "reason": "",
                "hint": f"Записей: {len(rows)}" if rows else "В памяти ничего не нашлось"}

    def _memory_forget(self, params: dict) -> dict:
        what = str(params.get("what") or "").strip()
        if not what:
            return {"ok": False, "reason": "Что забыть? Назовите запись словами или номером"}
        rows = self.store.forget(what)
        if not rows:
            return {"ok": False, "reason": f"В памяти нет уверенного совпадения с «{what}»"}
        return {"ok": True, "forgotten": rows, "count": len(rows), "reason": "",
                "hint": "Забыто: " + "; ".join(str(row.get("text"))[:60] for row in rows)}

    def _window_allowed(self, title: str) -> bool:
        """Белый список окон (И220): пуст — разрешено всё, иначе только перечисленные."""
        try:
            rows = self.store.list_whitelist(200)
        except Exception:
            return True
        if not rows:
            return True
        low = str(title or "").casefold()
        return any(str(row.get("app_name") or "").casefold() in low and row.get("allowed") for row in rows)


def _summary(result: dict[str, Any]) -> str:
    """Короткая подпись успеха для журнала: что именно получилось."""
    for key, label in (("count", "позиций"), ("moved", "перемещено"),
                       ("indexed", "проиндексировано"), ("saved", "сохранено"),
                       ("ready", "доступно"), ("new", "нового"),
                       ("checked", "проверено")):
        if key in result:
            return f"{label}: {result[key]}"
    if result.get("answer"):
        return str(result["answer"])[:120]
    if result.get("text"):
        return str(result["text"])[:120]
    if result.get("actions"):
        return f"действий панели: {len(result['actions'])}"
    return "готово"
