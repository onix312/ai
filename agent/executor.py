"""Исполнение навыков ассистента (18.14).

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
"""
from __future__ import annotations

import pathlib
from typing import Any, Callable

from . import capabilities, config, documents, fileops, model, skills
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


def _summary(result: dict[str, Any]) -> str:
    """Короткая подпись успеха для журнала: что именно получилось."""
    for key, label in (("count", "позиций"), ("moved", "перемещено"),
                       ("indexed", "проиндексировано"), ("saved", "сохранено"),
                       ("ready", "доступно")):
        if key in result:
            return f"{label}: {result[key]}"
    if result.get("answer"):
        return str(result["answer"])[:120]
    if result.get("actions"):
        return f"действий панели: {len(result['actions'])}"
    return "готово"
