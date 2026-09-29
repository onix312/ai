"""Persistent Task Engine for multi-step assistant work.

A task is a durable goal plus ordered skill calls. Execution never bypasses the
existing registry or confirmation queue: every step goes through Agent.run_skill.
"""
from __future__ import annotations

import json
import hashlib
import threading
from typing import Any

from . import autonomy, planner, skills, verifier
from .store import now_iso

TERMINAL = frozenset(("done", "cancelled"))
STARTABLE = frozenset(("planned", "paused"))


class TaskEngine:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.store = agent.runner.store
        self._lock = threading.RLock()
        self._running: set[int] = set()
        self._ensure_schema()
        self._recover_interrupted()

    def _ensure_schema(self) -> None:
        for statement in (
            """CREATE TABLE IF NOT EXISTS assistant_tasks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                title TEXT NOT NULL,
                goal TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned',
                current_step INTEGER DEFAULT 0,
                error TEXT DEFAULT '',
                source TEXT DEFAULT 'manual')""",
            """CREATE TABLE IF NOT EXISTS assistant_task_steps(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                skill TEXT NOT NULL,
                params_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                pending_action TEXT DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT DEFAULT '',
                finished_at TEXT DEFAULT '',
                UNIQUE(task_id, seq))""",
            "CREATE INDEX IF NOT EXISTS assistant_tasks_status ON assistant_tasks(status)",
            "CREATE INDEX IF NOT EXISTS assistant_task_steps_task ON assistant_task_steps(task_id, seq)",
            "CREATE INDEX IF NOT EXISTS assistant_task_steps_pending ON assistant_task_steps(pending_action)",
            """CREATE TABLE IF NOT EXISTS assistant_task_replans(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                replace_from INTEGER NOT NULL,
                reason TEXT NOT NULL,
                old_tail_json TEXT NOT NULL,
                new_tail_json TEXT NOT NULL)""",
        ):
            self.store._run(statement)

    def _recover_interrupted(self) -> None:
        """После рестарта не повторять действия молча.

        Очередь подтверждений живёт в RAM, поэтому waiting action после нового
        процесса уже не существует. Выполнявшийся шаг тоже нельзя считать
        успешным: он переводится обратно в pending, а задача ставится на паузу.
        Возобновление всегда явное.
        """
        rows = self.store._rows(
            "SELECT id,status FROM assistant_tasks WHERE status IN ('running','waiting')")
        for row in rows:
            task_id = int(row["id"])
            steps = self._rows(task_id)
            interrupted = next(
                (step for step in steps if step["status"] in ("running", "waiting")), None)
            if interrupted is not None:
                self._set_step(
                    task_id, int(interrupted["seq"]), status="pending",
                    pending_action="", result={})
                current = int(interrupted["seq"])
            else:
                current = int(self.get(task_id).get("current_step") or 0)
            self._set_task(
                task_id, status="paused", current_step=current,
                error="Выполнение прервано перезапуском NOZZA. Проверьте текущий шаг и продолжите вручную.")

    def on_action_expired(self, action_id: str) -> dict[str, Any] | None:
        """Истёкшее подтверждение не оставляет задачу в вечном waiting."""
        rows = self.store._rows(
            "SELECT task_id,seq FROM assistant_task_steps WHERE pending_action=? LIMIT 1",
            (str(action_id),))
        if not rows:
            return None
        task_id, seq = int(rows[0]["task_id"]), int(rows[0]["seq"])
        self._set_step(task_id, seq, status="pending", pending_action="", result={})
        self._set_task(
            task_id, status="paused", current_step=seq,
            error="Подтверждение шага истекло. Продолжите задачу, когда будете готовы.")
        return self.get(task_id)

    def validate_steps(self, raw: Any) -> tuple[list[dict[str, Any]], str]:
        if not isinstance(raw, list) or not raw:
            return [], "Нужен хотя бы один шаг"
        if len(raw) > 20:
            return [], "В одной задаче допускается не больше 20 шагов"
        learned = self.agent.runner.learned()
        out: list[dict[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                return [], f"Шаг {index + 1}: нужен объект"
            name = str(item.get("skill") or "").strip().casefold()
            skill = skills.get(name, learned)
            if skill is None:
                return [], f"Шаг {index + 1}: навыка «{name}» нет"
            params, errors = skills.check_params(skill, item.get("params") or {})
            if errors:
                return [], f"Шаг {index + 1}: {'; '.join(errors)}"
            out.append({
                "skill": name,
                "params": params,
                "title": str(skill.get("title") or name),
                "confirm": skills.confirm_required(skill),
                "risk": skills.risk_of(skill),
            })
        return out, ""

    def create(self, title: str, steps: Any, goal: str = "", source: str = "manual",
               start: bool = False) -> dict[str, Any]:
        clean_title = " ".join(str(title or "").split())[:200]
        if not clean_title:
            return {"ok": False, "reason": "У задачи нет названия"}
        checked, reason = self.validate_steps(steps)
        if reason:
            return {"ok": False, "reason": reason}
        at = now_iso()
        cursor = self.store._run(
            "INSERT INTO assistant_tasks(created_at,updated_at,title,goal,status,current_step,error,source) "
            "VALUES(?,?,?,?, 'planned',0,'',?)",
            (at, at, clean_title, " ".join(str(goal or "").split())[:1000], str(source or "manual")[:40]),
        )
        task_id = int(cursor.lastrowid or 0)
        for seq, step in enumerate(checked):
            self.store._run(
                "INSERT INTO assistant_task_steps(task_id,seq,skill,params_json,status) VALUES(?,?,?,?, 'pending')",
                (task_id, seq, step["skill"],
                 json.dumps(step["params"], ensure_ascii=False, sort_keys=True)),
            )
        task = self.get(task_id)
        if start:
            started = self.start(task_id)
            task = self.get(task_id)
            if not started.get("ok"):
                return {"ok": False, "reason": str(started.get("reason") or "Задача не запущена"),
                        "task": task, "autonomy_blocked": bool(started.get("autonomy_blocked")),
                        "autonomy": started.get("autonomy")}
        return {"ok": True, "task": task}

    def _rows(self, task_id: int) -> list[dict[str, Any]]:
        rows = self.store._rows(
            "SELECT * FROM assistant_task_steps WHERE task_id=? ORDER BY seq", (int(task_id),))
        for row in rows:
            try:
                row["params"] = json.loads(row.pop("params_json") or "{}")
            except (ValueError, TypeError):
                row["params"] = {}
            try:
                row["result"] = json.loads(row.pop("result_json") or "{}")
            except (ValueError, TypeError):
                row["result"] = {}
            row["verification"] = (
                dict(row["result"].get("_verification") or {})
                if isinstance(row["result"], dict) else {}
            )
        return rows

    def replan_history(self, task_id: int) -> list[dict[str, Any]]:
        rows = self.store._rows(
            "SELECT * FROM assistant_task_replans WHERE task_id=? ORDER BY id DESC",
            (int(task_id),),
        )
        for row in rows:
            for field, target in (("old_tail_json", "old_tail"), ("new_tail_json", "new_tail")):
                try:
                    row[target] = json.loads(row.pop(field) or "[]")
                except (ValueError, TypeError):
                    row[target] = []
        return rows

    def get(self, task_id: int) -> dict[str, Any] | None:
        rows = self.store._rows("SELECT * FROM assistant_tasks WHERE id=?", (int(task_id),))
        if not rows:
            return None
        task = rows[0]
        task["steps"] = self._rows(task_id)
        task["replans"] = self.replan_history(task_id)
        task["progress"] = sum(1 for step in task["steps"] if step["status"] == "done")
        task["total_steps"] = len(task["steps"])
        return task

    @staticmethod
    def tail_fingerprint(task: dict[str, Any], replace_from: int) -> str:
        tail = [step for step in task["steps"] if int(step["seq"]) >= replace_from]
        data = [(step["id"], step["seq"], step["skill"], step["params"],
                 step["status"], step["pending_action"], step["result"]) for step in tail]
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                         default=str).encode("utf-8")).hexdigest()

    def replace_remaining(self, task_id: int, replace_from: int, new_steps: Any,
                          reason: str, expected_tail: str = "") -> dict[str, Any]:
        """Atomically replace the unfinished suffix; never run it here."""
        with self._lock:
            checked, problem = self.validate_steps(new_steps)
            if problem:
                return {"ok": False, "reason": problem}
            if len(checked) > planner.MAX_STEPS:
                return {"ok": False, "reason": "Предложено слишком много шагов"}
            for step in checked:
                if step["skill"] in planner._DENY:
                    return {"ok": False, "reason": "Перепланирование не допускает meta skills"}
                skill = skills.get(step["skill"], self.agent.runner.learned())
                assert skill is not None
                available, reason = skills.availability(skill, self.agent.runner.caps)
                if not available:
                    return {"ok": False, "reason": reason}
            with self.store._lock:
                conn = self.store._conn
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute("SELECT status FROM assistant_tasks WHERE id=?", (int(task_id),)).fetchone()
                    if row is None or row["status"] not in ("paused", "failed") or int(task_id) in self._running:
                        raise ValueError("Задача уже выполняется или не допускает изменения")
                    task = self.get(task_id)
                    assert task is not None
                    first = next((int(step["seq"]) for step in task["steps"] if step["status"] != "done"),
                                 len(task["steps"]))
                    if replace_from != first or any(step["status"] == "done" for step in task["steps"][first:]):
                        raise ValueError("Завершённые шаги и история задачи неизменяемы")
                    if not expected_tail or self.tail_fingerprint(task, first) != expected_tail:
                        raise ValueError("Задача изменилась после подготовки плана. Составьте новый вариант")
                    old_tail = [step for step in task["steps"] if int(step["seq"]) >= first]
                    if any(step["status"] in ("running", "waiting") or step["pending_action"]
                           for step in old_tail):
                        raise ValueError("Есть выполняемый шаг или ожидающее подтверждение")
                    if first + len(checked) > 20:
                        raise ValueError("В задаче допускается не больше 20 шагов")
                    conn.execute("DELETE FROM assistant_task_steps WHERE task_id=? AND seq>=?", (task_id, first))
                    for seq, step in enumerate(checked, start=first):
                        conn.execute(
                            "INSERT INTO assistant_task_steps(task_id,seq,skill,params_json,status) "
                            "VALUES(?,?,?,?, 'pending')",
                            (task_id, seq, step["skill"],
                             json.dumps(step["params"], ensure_ascii=False, sort_keys=True)))
                    conn.execute(
                        "INSERT INTO assistant_task_replans(task_id,created_at,replace_from,reason,old_tail_json,new_tail_json) "
                        "VALUES(?,?,?,?,?,?)",
                        (task_id, now_iso(), first, str(reason or "Изменён план")[:1000],
                         json.dumps(old_tail, ensure_ascii=False, default=str),
                         json.dumps(checked, ensure_ascii=False, default=str)))
                    conn.execute(
                        "UPDATE assistant_tasks SET status='paused',current_step=?,error=?,updated_at=? WHERE id=?",
                        (first, "План изменён. Продолжение только по вашей команде.", now_iso(), task_id))
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    return {"ok": False, "reason": str(exc)}
        return {"ok": True, "task": self.get(task_id)}

    def list(self, limit: int = 50, status: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit or 50)))
        if status:
            rows = self.store._rows(
                "SELECT id FROM assistant_tasks WHERE status=? ORDER BY id DESC LIMIT ?",
                (str(status), limit))
        else:
            rows = self.store._rows(
                "SELECT id FROM assistant_tasks ORDER BY id DESC LIMIT ?", (limit,))
        return [task for row in rows if (task := self.get(int(row["id"]))) is not None]

    def _autonomy_preflight(self, task: dict[str, Any], mode: str = "task") -> tuple[bool, str, dict[str, Any] | None]:
        policy = getattr(self.agent, "autonomy", None)
        if policy is None:
            return True, "", None
        learned = {}
        runner = getattr(self.agent, "runner", None)
        if runner is not None and callable(getattr(runner, "learned", None)):
            try:
                learned = runner.learned()
            except Exception:
                learned = {}
        for step in task.get("steps") or []:
            if step.get("status") == "done":
                continue
            skill = skills.get(str(step.get("skill") or ""), learned)
            if skill is None:
                continue
            ok, reason = policy.check_skill(skill, mode)
            if not ok:
                return False, reason, policy.payload()
        return True, "", policy.payload()

    def start(self, task_id: int) -> dict[str, Any]:
        stopped = getattr(self.agent, "execution_stopped", None)
        if callable(stopped) and stopped():
            return {"ok": False, "reason": "STOP ALL активен", "stopped": True,
                    "task": self.get(task_id)}
        allowed, why, policy = autonomy.check_agent(self.agent, "operator", "core")
        if not allowed:
            return {"ok": False, "reason": why, "autonomy_blocked": True,
                    "autonomy": policy}
        with self._lock:
            task = self.get(task_id)
            if task is None:
                return {"ok": False, "reason": "Задача не найдена"}
            if task["status"] not in STARTABLE:
                return {"ok": False, "reason": f"Задачу в статусе {task['status']} нельзя запустить",
                        "task": task}
            allowed_steps, why_steps, policy_steps = self._autonomy_preflight(task, "task")
            if not allowed_steps:
                return {"ok": False, "reason": why_steps, "task": task,
                        "autonomy_blocked": True, "autonomy": policy_steps}
            if int(task_id) in self._running:
                return {"ok": True, "started": False, "reason": "Задача уже выполняется", "task": task}
            self._running.add(int(task_id))
            self._set_task(task_id, status="running", error="")
        thread = threading.Thread(
            target=self._run_loop, args=(int(task_id),), daemon=True,
            name=f"nozza-task-{int(task_id)}")
        thread.start()
        return {"ok": True, "started": True, "task": self.get(task_id)}

    def run_sync(self, task_id: int) -> dict[str, Any]:
        stopped = getattr(self.agent, "execution_stopped", None)
        if callable(stopped) and stopped():
            return {"ok": False, "reason": "STOP ALL активен", "stopped": True,
                    "task": self.get(task_id)}
        allowed, why, policy = autonomy.check_agent(self.agent, "operator", "core")
        if not allowed:
            return {"ok": False, "reason": why, "autonomy_blocked": True,
                    "autonomy": policy}
        with self._lock:
            task = self.get(task_id)
            if task is None:
                return {"ok": False, "reason": "Задача не найдена"}
            if task["status"] not in STARTABLE:
                return {"ok": False, "reason": f"Задачу в статусе {task['status']} нельзя запустить",
                        "task": task}
            allowed_steps, why_steps, policy_steps = self._autonomy_preflight(task, "task")
            if not allowed_steps:
                return {"ok": False, "reason": why_steps, "task": task,
                        "autonomy_blocked": True, "autonomy": policy_steps}
            if int(task_id) in self._running:
                return {"ok": False, "reason": "Задача уже выполняется"}
            self._running.add(int(task_id))
            self._set_task(task_id, status="running", error="")
        self._run_loop(int(task_id))
        return {"ok": True, "task": self.get(task_id)}

    def _run_skill(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run one task step with autonomy context when the host supports it."""
        if getattr(self.agent, "autonomy", None) is None:
            return self.agent.run_skill(name, params, ask=False)
        return self.agent.run_skill(name, params, ask=False, autonomy_mode="task")

    def _run_loop(self, task_id: int) -> None:
        try:
            while True:
                task = self.get(task_id)
                if task is None or task["status"] in TERMINAL or task["status"] == "paused":
                    return
                steps = task["steps"]
                pending = next((step for step in steps if step["status"] != "done"), None)
                if pending is None:
                    self._set_task(task_id, status="done", current_step=len(steps), error="")
                    try:
                        live = getattr(self.agent, "set_activity", None)
                        if callable(live):
                            live("done", session=f"task:{task_id}", task_id=task_id,
                                 detail="Задача завершена", active=False)
                    except Exception:
                        pass
                    return
                seq = int(pending["seq"])
                if pending["status"] == "waiting":
                    self._set_task(task_id, status="waiting", current_step=seq)
                    return
                self._set_step(task_id, seq, status="running", started_at=now_iso(),
                               pending_action="", result={})
                try:
                    live = getattr(self.agent, "set_activity", None)
                    if callable(live):
                        live("task", session=f"task:{task_id}", skill=str(pending["skill"]),
                             task_id=task_id,
                             detail=f"Шаг {seq + 1}/{len(steps)}", active=True)
                except Exception:
                    pass
                result = self._run_skill(
                    str(pending["skill"]), pending.get("params") or {})
                if result.get("queued") and result.get("id"):
                    action_id = str(result["id"])
                    self._set_step(task_id, seq, status="waiting", pending_action=action_id,
                                   result=result)
                    self._set_task(task_id, status="waiting", current_step=seq, error="")
                    return
                if not result.get("ok"):
                    reason = str(result.get("reason") or "Шаг не выполнен")
                    if result.get("autonomy_blocked"):
                        self._set_step(task_id, seq, status="pending",
                                       pending_action="", result=result)
                        self._set_task(task_id, status="paused", current_step=seq,
                                       error=reason)
                        return
                    self._set_step(task_id, seq, status="failed", finished_at=now_iso(),
                                   result=result)
                    self._set_task(task_id, status="failed", current_step=seq, error=reason)
                    return
                result, check = self._verify_result(
                    str(pending["skill"]), pending.get("params") or {}, result)
                if check.get("status") == "failed":
                    reason = "Проверка результата: " + str(check.get("reason") or "состояние не совпало")
                    self._set_step(task_id, seq, status="failed", finished_at=now_iso(),
                                   result=result)
                    self._set_task(task_id, status="failed", current_step=seq, error=reason)
                    self._schedule_replan(task_id)
                    return
                self._set_step(task_id, seq, status="done", finished_at=now_iso(), result=result)
                latest = self.get(task_id)
                if latest is None:
                    return
                if latest["status"] == "cancelled":
                    return
                if latest["status"] == "paused":
                    self._set_task(task_id, current_step=seq + 1)
                    return
                self._set_task(task_id, status="running", current_step=seq + 1, error="")
        finally:
            with self._lock:
                self._running.discard(int(task_id))

    def on_action_result(self, action_id: str, result: dict[str, Any],
                         confirmed: bool) -> dict[str, Any] | None:
        rows = self.store._rows(
            "SELECT task_id,seq FROM assistant_task_steps WHERE pending_action=? LIMIT 1",
            (str(action_id),))
        if not rows:
            return None
        task_id, seq = int(rows[0]["task_id"]), int(rows[0]["seq"])
        if not confirmed:
            self._set_step(task_id, seq, status="pending", pending_action="", result={})
            self._set_task(task_id, status="paused", current_step=seq,
                           error="Шаг отменён человеком")
            return self.get(task_id)
        if result.get("autonomy_blocked"):
            reason = str(result.get("reason") or "Уровень автономности изменён")
            self._set_step(task_id, seq, status="pending", pending_action="", result=result)
            self._set_task(task_id, status="paused", current_step=seq, error=reason)
            return self.get(task_id)
        if result.get("ok"):
            task = self.get(task_id)
            step = next(
                (row for row in (task or {}).get("steps", []) if int(row.get("seq") or 0) == seq),
                None,
            )
            if step is None:
                self._set_task(task_id, status="failed", current_step=seq,
                               error="Шаг подтверждения не найден для проверки")
                return self.get(task_id)
            actual = result.get("result") if isinstance(result.get("result"), dict) else result
            checked_result, check = self._verify_result(
                str(step.get("skill") or ""), step.get("params") or {}, actual)
            stored = dict(result)
            stored["_verification"] = check
            if isinstance(result.get("result"), dict):
                stored["result"] = checked_result
            if check.get("status") == "failed":
                reason = "Проверка результата: " + str(check.get("reason") or "состояние не совпало")
                self._set_step(task_id, seq, status="failed", pending_action="",
                               finished_at=now_iso(), result=stored)
                self._set_task(task_id, status="failed", current_step=seq, error=reason)
                self._schedule_replan(task_id)
                return self.get(task_id)
            self._set_step(task_id, seq, status="done", pending_action="",
                           finished_at=now_iso(), result=stored)
            self._set_task(task_id, status="paused", current_step=seq + 1, error="")
            self.start(task_id)
        else:
            reason = str(result.get("reason") or "Подтверждённый шаг не выполнен")
            self._set_step(task_id, seq, status="failed", pending_action="",
                           finished_at=now_iso(), result=result)
            self._set_task(task_id, status="failed", current_step=seq, error=reason)
        return self.get(task_id)

    @staticmethod
    def _verify_result(skill_name: str, params: dict[str, Any],
                       result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        check = verifier.verify(skill_name, params, result)
        stored = dict(result)
        stored["_verification"] = check
        return stored, check

    def _schedule_replan(self, task_id: int) -> None:
        def create_preview() -> None:
            try:
                self.agent.replanner.preview(task_id, automatic=True)
            except Exception:
                pass  # The failed task and its evidence remain readable.
        threading.Thread(target=create_preview, daemon=True,
                         name=f"nozza-replan-{task_id}").start()

    def pause(self, task_id: int) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            return {"ok": False, "reason": "Задача не найдена"}
        for step in task["steps"]:
            action_id = str(step.get("pending_action") or "")
            if step["status"] == "waiting" and action_id:
                discard = getattr(self.agent, "discard_action", None)
                if callable(discard):
                    discard(action_id)
                self._set_step(task_id, int(step["seq"]), status="pending",
                               pending_action="", result={})
        self._set_task(task_id, status="paused")
        return {"ok": True, "task": self.get(task_id)}

    def resume(self, task_id: int) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            return {"ok": False, "reason": "Задача не найдена"}
        if task["status"] not in ("paused", "planned"):
            return {"ok": False, "reason": f"Задачу в статусе {task['status']} нельзя продолжить"}
        return self.start(task_id)

    def cancel(self, task_id: int) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            return {"ok": False, "reason": "Задача не найдена"}
        for step in task["steps"]:
            action_id = str(step.get("pending_action") or "")
            if action_id:
                discard = getattr(self.agent, "discard_action", None)
                if callable(discard):
                    discard(action_id)
            if step["status"] not in ("done", "failed"):
                self._set_step(task_id, int(step["seq"]), status="cancelled", pending_action="")
        self._set_task(task_id, status="cancelled", error="")
        return {"ok": True, "task": self.get(task_id)}

    def _set_task(self, task_id: int, **fields: Any) -> None:
        allowed = {"status", "current_step", "error"}
        clean = {key: value for key, value in fields.items() if key in allowed}
        if not clean:
            return
        clean["updated_at"] = now_iso()
        sql = ", ".join(f"{key}=?" for key in clean)
        self.store._run(
            f"UPDATE assistant_tasks SET {sql} WHERE id=?",
            tuple(clean.values()) + (int(task_id),))

    def _set_step(self, task_id: int, seq: int, status: str | None = None,
                  pending_action: str | None = None, result: dict[str, Any] | None = None,
                  started_at: str | None = None, finished_at: str | None = None) -> None:
        fields: dict[str, Any] = {}
        if status is not None:
            fields["status"] = status
        if pending_action is not None:
            fields["pending_action"] = pending_action
        if result is not None:
            fields["result_json"] = json.dumps(result, ensure_ascii=False, default=str)[:20000]
        if started_at is not None:
            fields["started_at"] = started_at
        if finished_at is not None:
            fields["finished_at"] = finished_at
        if not fields:
            return
        sql = ", ".join(f"{key}=?" for key in fields)
        self.store._run(
            f"UPDATE assistant_task_steps SET {sql} WHERE task_id=? AND seq=?",
            tuple(fields.values()) + (int(task_id), int(seq)))
