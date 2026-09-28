"""Planner 1.0: цель человека -> проверенный preview -> Task Engine.

Planner не выполняет действия и не доверяет шагам от клиента. Модель может
только предложить имена уже доступных skills и параметры. Каждый шаг заново
проверяется по реестру; запуск возможен только по opaque draft id.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from . import autonomy, config, model, skills

MAX_GOAL = 1200
MAX_STEPS = 8
DRAFT_TTL_SEC = 10 * 60

# Planner не должен рекурсивно создавать планировщики/макросы или переписывать
# собственные правила. Эти операции остаются явными командами человека.
_DENY = frozenset({
    "voice.command",
    "assistant.macro.create",
    "assistant.macro.run",
    "assistant.macro.delete",
    "learn.teach",
    "learn.forget",
    "agent.bootstrap",
    "agent.watchdog_config",
})

_RULES = """Ты — планировщик NOZZA. Построй короткий практический план достижения
цели пользователя ТОЛЬКО из перечисленных ниже навыков.

Верни один JSON:
{
  "title": "короткое название",
  "summary": "что будет сделано",
  "ask": "",
  "steps": [
    {"skill": "точное имя навыка", "params": {}, "why": "зачем этот шаг"}
  ]
}

Правила:
1. Используй только навыки из списка и точные имена.
2. Не придумывай параметры, которых пользователь не дал и которые нельзя
   безопасно вывести из самой цели.
3. Если для корректного плана не хватает критичной информации, steps=[] и
   задай один конкретный вопрос в ask.
4. Не добавляй лишние действия "на всякий случай".
5. Максимум 8 шагов.
6. Не утверждай, что что-либо уже выполнено.
7. Наличие рискованного навыка допустимо: реальный executor всё равно потребует
   подтверждение человека.
"""


class Planner:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self._lock = threading.RLock()
        self._drafts: dict[str, dict[str, Any]] = {}
        self._approving: set[str] = set()

    def _purge(self) -> None:
        now = time.time()
        with self._lock:
            for draft_id in [
                key for key, value in self._drafts.items()
                if float(value.get("_expires_at") or 0) <= now
            ]:
                self._drafts.pop(draft_id, None)

    def list(self) -> list[dict[str, Any]]:
        self._purge()
        with self._lock:
            return [self._public(value) for value in self._drafts.values()]

    def preview(self, goal: str) -> dict[str, Any]:
        clean = " ".join(str(goal or "").split())
        if len(clean) < 3:
            return {"ok": False, "reason": "Опишите цель чуть подробнее"}
        if len(clean) > MAX_GOAL:
            return {"ok": False, "reason": f"Цель слишком длинная: максимум {MAX_GOAL} символов"}

        state = model.status()
        if not state.get("ok"):
            return {"ok": False, "reason": str(state.get("reason") or "Локальная модель недоступна")}

        learned = self.agent.runner.learned()
        catalog = [
            row for row in self.agent.runner.catalog()
            if row.get("available") and str(row.get("name") or "") not in _DENY
        ]
        if not catalog:
            return {"ok": False, "reason": "На этом компьютере нет доступных навыков для планирования"}

        skill_text = "\n".join(
            f"- {row['name']}: {row['title']}; params={row.get('params') or {}}; "
            f"risk={row.get('risk')}; confirm={bool(row.get('confirm'))}; "
            f"{row.get('description') or ''}"
            for row in catalog
        )
        system = _RULES + "\n\nДоступные навыки:\n" + skill_text
        reply = model.chat(
            [{"role": "user", "content": clean}],
            system=system,
            fmt="json",
            temperature=0.05,
            timeout=min(25.0, float(config.MODEL_TIMEOUT_SEC)),
            max_chars=12000,
            state=state,
        )
        if not reply.get("ok"):
            return {"ok": False, "reason": str(reply.get("reason") or "Модель не построила план")}

        data = model.parse_json(str(reply.get("text") or ""))
        if not isinstance(data, dict):
            return {"ok": False, "reason": "Модель вернула непонятный план"}
        ask = " ".join(str(data.get("ask") or "").split())[:500]
        raw_steps = data.get("steps")
        if ask and (not isinstance(raw_steps, list) or not raw_steps):
            return {"ok": True, "needs_clarification": True, "ask": ask, "goal": clean}
        if not isinstance(raw_steps, list) or not raw_steps:
            return {"ok": False, "reason": ask or "План получился пустым"}
        if len(raw_steps) > MAX_STEPS:
            return {"ok": False, "reason": f"Модель предложила слишком много шагов: максимум {MAX_STEPS}"}

        checked: list[dict[str, Any]] = []
        for index, row in enumerate(raw_steps):
            if not isinstance(row, dict):
                return {"ok": False, "reason": f"Шаг {index + 1}: нужен объект"}
            name = str(row.get("skill") or "").strip().casefold()
            if name in _DENY:
                return {"ok": False, "reason": f"Шаг {index + 1}: навык «{name}» запрещён планировщику"}
            skill = skills.get(name, learned)
            if skill is None:
                return {"ok": False, "reason": f"Шаг {index + 1}: навыка «{name}» нет"}
            available, reason = skills.availability(skill, self.agent.runner.caps)
            if not available:
                return {"ok": False, "reason": f"Шаг {index + 1}: {reason}"}
            params, errors = skills.check_params(skill, row.get("params") or {})
            if errors:
                return {"ok": False, "reason": f"Шаг {index + 1}: {'; '.join(errors)}"}
            checked.append({
                "seq": index,
                "skill": name,
                "title": str(skill.get("title") or name),
                "params": params,
                "why": " ".join(str(row.get("why") or "").split())[:300],
                "risk": skills.risk_of(skill),
                "confirm": skills.confirm_required(skill),
            })

        draft_id = uuid.uuid4().hex[:16]
        now = time.time()
        draft = {
            "id": draft_id,
            "goal": clean,
            "title": " ".join(str(data.get("title") or clean).split())[:160],
            "summary": " ".join(str(data.get("summary") or "").split())[:600],
            "steps": checked,
            "model": str(reply.get("model") or state.get("model") or ""),
            "created_at": now,
            "expires_at": now + DRAFT_TTL_SEC,
            "_expires_at": now + DRAFT_TTL_SEC,
        }
        with self._lock:
            self._drafts[draft_id] = draft
        return {"ok": True, "plan": self._public(draft)}

    def approve(self, draft_id: str) -> dict[str, Any]:
        allowed, why, policy = autonomy.check_agent(self.agent, "agent", "core")
        if not allowed:
            return {"ok": False, "reason": why, "autonomy_blocked": True,
                    "autonomy": policy}
        self._purge()
        key = str(draft_id or "").strip()
        with self._lock:
            draft = self._drafts.get(key)
            if not draft:
                return {"ok": False, "reason": "План не найден или уже истёк"}
            if key in self._approving:
                return {"ok": False, "reason": "Этот план уже запускается"}
            self._approving.add(key)
        try:
            # Повторная валидация непосредственно перед созданием задачи.
            steps = [{"skill": row["skill"], "params": dict(row.get("params") or {})}
                     for row in draft["steps"]]
            checked, reason = self.agent.tasks.validate_steps(steps)
            if reason:
                return {"ok": False, "reason": f"План больше нельзя запустить: {reason}"}
            result = self.agent.tasks.create(
                str(draft["title"]),
                [{"skill": row["skill"], "params": row["params"]} for row in checked],
                goal=str(draft["goal"]),
                source="planner",
                start=True,
            )
            if result.get("ok"):
                with self._lock:
                    self._drafts.pop(key, None)
                return {"ok": True, "task": result.get("task"), "plan_id": key}
            return result
        finally:
            with self._lock:
                self._approving.discard(key)

    def discard(self, draft_id: str) -> dict[str, Any]:
        key = str(draft_id or "").strip()
        with self._lock:
            if key in self._approving:
                return {"ok": False, "discarded": False, "reason": "План уже запускается"}
            existed = self._drafts.pop(key, None) is not None
        return {"ok": existed, "discarded": existed,
                "reason": "" if existed else "План не найден"}

    @staticmethod
    def _public(draft: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in draft.items() if not key.startswith("_")}
