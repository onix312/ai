"""Full Control Center Люмы: разговор, дела, память, обучение и настройки."""
from __future__ import annotations

import html
import json
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)


def _pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


class TextPage(QWidget):
    refresh_requested = Signal()

    def __init__(self, title: str, subtitle: str = "") -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        text = QVBoxLayout()
        label = QLabel(title)
        label.setObjectName("pageTitle")
        text.addWidget(label)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("muted")
            text.addWidget(sub)
        header.addLayout(text, 1)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.refresh_requested)
        header.addWidget(refresh)
        layout.addLayout(header)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        layout.addWidget(self.browser, 1)

    def set_payload(self, value: Any) -> None:
        self.browser.setPlainText(_pretty(value))


class ChatPage(QWidget):
    submitted = Signal(str)
    clear_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        head = QHBoxLayout()
        title = QLabel("Разговор")
        title.setObjectName("pageTitle")
        head.addWidget(title)
        head.addStretch(1)
        clear = QPushButton("Очистить диалог")
        clear.clicked.connect(self.clear_requested)
        head.addWidget(clear)
        layout.addLayout(head)
        self.live = QLabel("Люма готова")
        self.live.setObjectName("muted")
        self.live.setWordWrap(True)
        layout.addWidget(self.live)
        self.feed = QTextBrowser()
        layout.addWidget(self.feed, 1)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Спроси Люму или скажи, что сделать…")
        self.input.returnPressed.connect(self._submit)
        row.addWidget(self.input, 1)
        send = QPushButton("Отправить")
        send.setObjectName("primary")
        send.clicked.connect(self._submit)
        row.addWidget(send)
        layout.addLayout(row)

    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.submitted.emit(text)

    def set_history(self, turns: list[dict[str, Any]]) -> None:
        chunks = []
        for turn in turns:
            role = str(turn.get("role") or "")
            who = "Вы" if role == "user" else "Люма"
            text = str(turn.get("text") or "")
            chunks.append(f"<p><b>{who}</b><br>{text}</p>")
        self.feed.setHtml("".join(chunks))
        bar = self.feed.verticalScrollBar()
        bar.setValue(bar.maximum())

    def append_local(self, who: str, text: str) -> None:
        self.feed.append(f"<p><b>{who}</b><br>{text}</p>")

    def set_live_activity(self, phase: str = "idle", heard: str = "", reply: str = "",
                          skill: str = "", detail: str = "", task_id: int = 0) -> None:
        labels = {
            "thinking": "🧠 Люма думает",
            "speaking": "🔊 Люма отвечает",
            "executing": "⚡ Люма выполняет",
            "task": "✓ Люма ведёт задачу",
            "waiting": "⏳ Люма ждёт",
            "error": "⚠ Люме нужна помощь",
            "done": "✓ Готово",
            "idle": "Люма готова",
        }
        parts = [labels.get(str(phase or "idle"), "Люма работает")]
        if skill:
            parts.append(str(skill))
        if task_id:
            parts.append(f"задача #{int(task_id)}")
        if detail:
            parts.append(" ".join(str(detail).split())[:120])
        if reply:
            parts.append("«" + " ".join(str(reply).split())[:180] + "»")
        elif heard:
            parts.append("услышала: «" + " ".join(str(heard).split())[:120] + "»")
        self.live.setText(" · ".join(parts))


class ActivityPage(QWidget):
    refresh_requested = Signal()

    PHASES = {
        "thinking": "🧠 думает",
        "speaking": "🔊 отвечает",
        "executing": "⚡ выполняет",
        "task": "✓ ведёт задачу",
        "waiting": "⏳ ждёт",
        "done": "✓ готово",
        "error": "⚠ ошибка",
        "idle": "○ готова",
    }

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        head = QHBoxLayout()
        title = QLabel("Активность")
        title.setObjectName("pageTitle")
        head.addWidget(title)
        head.addStretch(1)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.refresh_requested)
        head.addWidget(refresh)
        layout.addLayout(head)
        sub = QLabel("Что Люма услышала, решила, выполнила и как проверила результат.")
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        layout.addWidget(sub)
        self.browser = QTextBrowser()
        layout.addWidget(self.browser, 1)

    @staticmethod
    def _e(value: Any) -> str:
        return html.escape(" ".join(str(value or "").split()))

    def _activity_card(self, row: dict[str, Any], title: str) -> str:
        phase = str(row.get("phase") or "idle")
        bits = [f"<b>{self._e(title)}</b> · {self._e(self.PHASES.get(phase, phase))}"]
        if row.get("heard"):
            bits.append(f"<div>🎧 Вы: {self._e(row.get('heard'))}</div>")
        if row.get("reply"):
            bits.append(f"<div>💬 Люма: {self._e(row.get('reply'))}</div>")
        if row.get("skill"):
            task = f" · задача #{int(row.get('task_id') or 0)}" if row.get("task_id") else ""
            bits.append(f"<div>⚡ {self._e(row.get('skill'))}{self._e(task)}</div>")
        if row.get("detail"):
            bits.append(f"<div><small>{self._e(row.get('detail'))}</small></div>")
        return "<div style='margin:8px 0;padding:10px;border:1px solid #334155;border-radius:8px'>" + "".join(bits) + "</div>"

    def set_payload(self, payload: dict[str, Any]) -> None:
        activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
        current = activity.get("current") if isinstance(activity.get("current"), dict) else {}
        recent = list(activity.get("recent") or [])
        tasks = list(payload.get("tasks") or [])
        journal = list(payload.get("journal") or [])

        chunks = ["<h3>Сейчас</h3>"]
        if current and (current.get("active") or current.get("phase") != "idle"):
            chunks.append(self._activity_card(current, "Текущий маршрут"))
        else:
            chunks.append("<p>Люма свободна.</p>")

        if recent:
            chunks.append("<h3>Недавние маршруты</h3>")
            for row in recent[:4]:
                chunks.append(self._activity_card(row, "Завершено"))

        chunks.append("<h3>Задачи</h3>")
        shown = 0
        for task in tasks[:12]:
            steps = list(task.get("steps") or [])
            if not steps:
                continue
            shown += 1
            task_id = int(task.get("id") or 0)
            title = self._e(task.get("title") or f"Задача {task_id}")
            status = self._e(task.get("status") or "")
            chunks.append(
                f"<div style='margin:10px 0'><b>#{task_id} {title}</b> · {status}"
                f" · {int(task.get('progress') or 0)}/{int(task.get('total_steps') or len(steps))}<br>"
            )
            for step in steps:
                verification = step.get("verification") if isinstance(step.get("verification"), dict) else {}
                verify = str(verification.get("status") or "")
                reason = str(verification.get("reason") or "")
                stamp = str(step.get("finished_at") or step.get("started_at") or "")
                line = (
                    f"{int(step.get('seq') or 0) + 1}. {self._e(step.get('skill'))}"
                    f" · {self._e(step.get('status') or 'pending')}"
                )
                if verify:
                    line += f" · проверка: {self._e(verify)}"
                if reason:
                    line += f" ({self._e(reason)})"
                if stamp:
                    line += f" · {self._e(stamp)}"
                chunks.append(f"<div style='margin-left:14px'>{line}</div>")
            chunks.append("</div>")
        if not shown:
            chunks.append("<p>Долгих задач пока нет.</p>")

        chunks.append("<h3>Фактические действия</h3>")
        if journal:
            for row in journal[:40]:
                outcome = self._e(row.get("outcome") or "")
                detail = self._e(row.get("detail") or "")
                target = self._e(row.get("target") or "")
                stamp = self._e(row.get("at") or "")
                suffix = f" · {target}" if target else ""
                chunks.append(
                    f"<div><small>{stamp}</small> · <b>{self._e(row.get('skill'))}</b>"
                    f" · {outcome}{suffix}"
                    + (f"<br><span style='margin-left:14px'>{detail}</span>" if detail else "")
                    + "</div>"
                )
        else:
            chunks.append("<p>Журнал действий пуст.</p>")

        self.browser.setHtml("".join(chunks))


class TasksPage(QWidget):
    decision = Signal(str, bool)
    command = Signal(int, str)
    plan_preview = Signal(str)
    plan_command = Signal(str, str)
    replan_preview = Signal(int)
    replan_command = Signal(str, str)
    refresh_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.layout = QVBoxLayout(self)
        head = QHBoxLayout()
        title = QLabel("Задачи")
        title.setObjectName("pageTitle")
        head.addWidget(title)
        head.addStretch(1)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.refresh_requested)
        head.addWidget(refresh)
        self.layout.addLayout(head)
        planner_row = QHBoxLayout()
        self.goal_input = QLineEdit()
        self.goal_input.setPlaceholderText("Цель: например, подготовь компьютер к работе")
        planner_row.addWidget(self.goal_input, 1)
        preview = QPushButton("Составить план")
        preview.setObjectName("primary")
        preview.clicked.connect(self._preview_plan)
        planner_row.addWidget(preview)
        self.layout.addLayout(planner_row)
        self.planner_message = QLabel("")
        self.planner_message.setObjectName("muted")
        self.planner_message.setWordWrap(True)
        self.layout.addWidget(self.planner_message)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.host_widget = QWidget()
        self.host = QVBoxLayout(self.host_widget)
        self.host.setAlignment(Qt.AlignTop)
        scroll.setWidget(self.host_widget)
        self.layout.addWidget(scroll, 1)

    def _preview_plan(self) -> None:
        goal = self.goal_input.text().strip()
        if goal:
            self.plan_preview.emit(goal)

    def set_planner_message(self, text: str) -> None:
        self.planner_message.setText(str(text or ""))

    def _clear(self) -> None:
        while self.host.count():
            item = self.host.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def set_payload(self, payload: dict[str, Any]) -> None:
        self._clear()
        plans = list(payload.get("plans") or [])
        replans = list(payload.get("replans") or [])
        tasks = list(payload.get("tasks") or [])
        pending = list(payload.get("pending") or [])
        notifications = list(payload.get("notifications") or [])
        if not plans and not replans and not tasks and not pending and not notifications:
            self.host.addWidget(QLabel("Ничего не ждёт."))
            return
        for plan in plans:
            card = QFrame()
            card.setStyleSheet("QFrame { background:#172033; border:1px solid #3b82f6; border-radius:12px; }")
            box = QVBoxLayout(card)
            title = str(plan.get("title") or "План")
            summary = str(plan.get("summary") or "")
            box.addWidget(QLabel(f"План: {title}"))
            if summary:
                note = QLabel(summary)
                note.setWordWrap(True)
                note.setObjectName("muted")
                box.addWidget(note)
            for step in list(plan.get("steps") or []):
                risk = str(step.get("risk") or "read")
                confirm = " · подтверждение" if step.get("confirm") else ""
                why = str(step.get("why") or "")
                line = f"{int(step.get('seq') or 0) + 1}. {step.get('title') or step.get('skill')} · {risk}{confirm}"
                params = dict(step.get("params") or {})
                if params:
                    pairs = ", ".join(f"{key}={value}" for key, value in params.items())
                    line += f" · {pairs}"
                if why:
                    line += f" — {why}"
                label = QLabel(line)
                label.setWordWrap(True)
                box.addWidget(label)
            buttons = QHBoxLayout()
            plan_id = str(plan.get("id") or "")
            discard = QPushButton("Отменить план")
            discard.clicked.connect(lambda _=False, i=plan_id: self.plan_command.emit(i, "discard"))
            approve = QPushButton("Запустить план")
            approve.setObjectName("primary")
            approve.clicked.connect(lambda _=False, i=plan_id: self.plan_command.emit(i, "approve"))
            buttons.addStretch(1)
            buttons.addWidget(discard)
            buttons.addWidget(approve)
            box.addLayout(buttons)
            self.host.addWidget(card)

        for replan in replans:
            card = QFrame()
            card.setStyleSheet("QFrame { background:#172033; border:1px solid #f59e0b; border-radius:12px; }")
            box = QVBoxLayout(card)
            heading = QLabel(f"Новый маршрут для задачи #{replan.get('task_id')}")
            heading.setObjectName("pageTitle")
            box.addWidget(heading)
            for field in ("reason", "summary"):
                if replan.get(field):
                    label = QLabel(str(replan[field]))
                    label.setWordWrap(True)
                    box.addWidget(label)
            old = list(replan.get("old_tail") or [])
            new = list(replan.get("steps") or [])
            for title, rows in (("Текущие незавершённые шаги", old),
                                ("Предлагаемые шаги", new)):
                box.addWidget(QLabel(title))
                for step in rows:
                    risk = str(step.get("risk") or "")
                    confirm = " · требует подтверждения" if step.get("confirm") else ""
                    details = f"{int(step.get('seq') or 0) + 1}. {step.get('title') or step.get('skill')}"
                    if risk:
                        details += f" · риск: {risk}{confirm}"
                    if step.get("verification", {}).get("status"):
                        details += f" · проверка: {step['verification']['status']}"
                    line = QLabel(details)
                    line.setWordWrap(True)
                    box.addWidget(line)
            buttons = QHBoxLayout()
            replan_id = str(replan.get("id") or "")
            keep = QPushButton("Сохранить текущий маршрут")
            keep.clicked.connect(lambda _=False, i=replan_id: self.replan_command.emit(i, "discard"))
            approve = QPushButton("Принять новый маршрут")
            approve.setObjectName("primary")
            approve.clicked.connect(lambda _=False, i=replan_id: self.replan_command.emit(i, "approve"))
            buttons.addStretch(1)
            buttons.addWidget(keep)
            buttons.addWidget(approve)
            box.addLayout(buttons)
            self.host.addWidget(card)

        for task in tasks:
            card = QFrame()
            card.setStyleSheet("QFrame { background:#111827; border:1px solid #334155; border-radius:12px; }")
            box = QVBoxLayout(card)
            title = str(task.get("title") or f"Задача {task.get('id')}")
            status = str(task.get("status") or "planned")
            progress = int(task.get("progress") or 0)
            total = int(task.get("total_steps") or 0)
            box.addWidget(QLabel(f"{title}  ·  {status}  ·  {progress}/{total}"))
            steps = list(task.get("steps") or [])
            checks = [
                str((step.get("verification") or {}).get("status") or "")
                for step in steps
                if isinstance(step.get("verification"), dict) and step.get("verification")
            ]
            if checks:
                verified = sum(1 for value in checks if value == "verified")
                assumed = sum(1 for value in checks if value == "assumed")
                failed_checks = sum(1 for value in checks if value == "failed")
                parts = [f"verified {verified}", f"assumed {assumed}"]
                if failed_checks:
                    parts.append(f"failed {failed_checks}")
                check_label = QLabel("Проверка: " + " · ".join(parts))
                check_label.setObjectName("muted")
                box.addWidget(check_label)
            current_index = int(task.get("current_step") or 0)
            current = next((step for step in steps if int(step.get("seq") or 0) == current_index), None)
            if current and status not in ("done", "cancelled"):
                detail = QLabel(f"Сейчас: {current.get('skill') or 'шаг'}")
                detail.setObjectName("muted")
                box.addWidget(detail)
            if task.get("error"):
                error = QLabel(str(task.get("error")))
                error.setObjectName("muted")
                error.setWordWrap(True)
                box.addWidget(error)
            buttons = QHBoxLayout()
            task_id = int(task.get("id") or 0)
            if status in ("planned", "paused"):
                op = "resume" if status == "paused" else "run"
                run = QPushButton("Продолжить" if status == "paused" else "Запустить")
                run.clicked.connect(lambda _=False, i=task_id, action=op: self.command.emit(i, action))
                buttons.addWidget(run)
            if status in ("paused", "failed") and any(
                    step.get("status") != "done" for step in steps):
                replan = QPushButton("Предложить новый маршрут")
                replan.clicked.connect(lambda _=False, i=task_id: self.replan_preview.emit(i))
                buttons.addWidget(replan)
            if status in ("running", "waiting"):
                pause = QPushButton("Пауза")
                pause.clicked.connect(lambda _=False, i=task_id: self.command.emit(i, "pause"))
                buttons.addWidget(pause)
            if status not in ("done", "cancelled"):
                cancel = QPushButton("Отменить задачу")
                cancel.clicked.connect(lambda _=False, i=task_id: self.command.emit(i, "cancel"))
                buttons.addWidget(cancel)
            buttons.addStretch(1)
            box.addLayout(buttons)
            self.host.addWidget(card)

        for row in pending:
            card = QFrame()
            card.setStyleSheet("QFrame { background:#111827; border:1px solid #334155; border-radius:12px; }")
            box = QVBoxLayout(card)
            box.addWidget(QLabel(str(row.get("text") or row.get("skill") or "Действие требует решения")))
            buttons = QHBoxLayout()
            no = QPushButton("Отмена")
            yes = QPushButton("Разрешить")
            yes.setObjectName("primary")
            action_id = str(row.get("id") or "")
            no.clicked.connect(lambda _=False, i=action_id: self.decision.emit(i, False))
            yes.clicked.connect(lambda _=False, i=action_id: self.decision.emit(i, True))
            buttons.addStretch(1)
            buttons.addWidget(no)
            buttons.addWidget(yes)
            box.addLayout(buttons)
            self.host.addWidget(card)
        for row in notifications:
            text = f"{row.get('title') or 'Уведомление'}: {row.get('text') or ''}"
            label = QLabel(text)
            label.setWordWrap(True)
            self.host.addWidget(label)


class ControlCenter(QMainWindow):
    chat_submitted = Signal(str)
    refresh_page = Signal(str)
    clear_chat = Signal()
    mic_toggle = Signal()
    safety_toggle = Signal()
    task_decision = Signal(str, bool)
    task_command = Signal(int, str)
    plan_preview = Signal(str)
    plan_command = Signal(str, str)
    replan_preview = Signal(int)
    replan_command = Signal(str, str)
    persona_save = Signal(object)
    persona_reset = Signal()
    autonomy_save = Signal(str, object)
    autonomy_reset = Signal()
    proactivity_save = Signal(object)
    proactivity_reset = Signal()
    voice_tune = Signal(int, float, int, float)
    voice_diag_reset = Signal()

    NAV = [
        ("chat", "💬  Разговор"),
        ("today", "☀  Сегодня"),
        ("tasks", "✓  Задачи"),
        ("activity", "◉  Активность"),
        ("memory", "🧠  Память"),
        ("learning", "🎓  Обучение"),
        ("skills", "⚡  Навыки"),
        ("journal", "≡  Журнал"),
        ("settings", "⚙  Настройки"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Люма")
        self.setMinimumSize(980, 680)
        self.resize(1180, 780)
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(210)
        for key, label in self.NAV:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            self.nav.addItem(item)
        body.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self.chat = ChatPage()
        self.chat.submitted.connect(self.chat_submitted)
        self.chat.clear_requested.connect(self.clear_chat)
        self.pages["chat"] = self.chat
        self.stack.addWidget(self.chat)

        today = TextPage("Сегодня", "Личные дела и краткий контекст дня.")
        today.refresh_requested.connect(lambda: self.refresh_page.emit("today"))
        self.pages["today"] = today
        self.stack.addWidget(today)

        tasks = TasksPage()
        tasks.refresh_requested.connect(lambda: self.refresh_page.emit("tasks"))
        tasks.decision.connect(self.task_decision)
        tasks.command.connect(self.task_command)
        tasks.plan_preview.connect(self.plan_preview)
        tasks.plan_command.connect(self.plan_command)
        tasks.replan_preview.connect(self.replan_preview)
        tasks.replan_command.connect(self.replan_command)
        self.pages["tasks"] = tasks
        self.stack.addWidget(tasks)

        activity = ActivityPage()
        activity.refresh_requested.connect(lambda: self.refresh_page.emit("activity"))
        self.pages["activity"] = activity
        self.stack.addWidget(activity)

        defs = {
            "memory": ("Память", "Факты, предпочтения и то, что вы просили запомнить."),
            "learning": ("Обучение", "Чему Люма научилась и что пока не понимает."),
            "skills": ("Навыки", "Доступные способности и их состояние."),
            "journal": ("Журнал", "Фактические действия ассистента на компьютере."),
        }
        for key, (title, subtitle) in defs.items():
            page = TextPage(title, subtitle)
            page.refresh_requested.connect(lambda k=key: self.refresh_page.emit(k))
            self.pages[key] = page
            self.stack.addWidget(page)

        settings = QWidget()
        sl = QVBoxLayout(settings)
        title = QLabel("Настройки")
        title.setObjectName("pageTitle")
        sl.addWidget(title)
        self.settings_status = QLabel("Backend: …")
        self.settings_status.setWordWrap(True)
        sl.addWidget(self.settings_status)
        self.settings_hotkey = QLabel(
            "Быстрая команда: Ctrl + Shift + Space · STOP ALL: Ctrl + Alt + Shift + Space"
        )
        sl.addWidget(self.settings_hotkey)
        controls = QHBoxLayout()
        self.mic_button = QPushButton("🎤 Включить wake word")
        self.mic_button.clicked.connect(self.mic_toggle)
        controls.addWidget(self.mic_button)
        self.stop_all_button = QPushButton("⛔ STOP ALL")
        self.stop_all_button.setObjectName("danger")
        self.stop_all_button.clicked.connect(self.safety_toggle)
        controls.addWidget(self.stop_all_button)
        sl.addLayout(controls)

        voice_title = QLabel("Voice Diagnostics")
        voice_title.setStyleSheet("font-size:16px; font-weight:700; margin-top:12px;")
        sl.addWidget(voice_title)
        self.voice_diag_meta = QLabel("ASR: … · vocabulary: 0")
        self.voice_diag_meta.setObjectName("muted")
        sl.addWidget(self.voice_diag_meta)

        self.voice_level = QProgressBar()
        self.voice_level.setRange(0, 4000)
        self.voice_level.setFormat("Mic level: %v")
        sl.addWidget(self.voice_level)
        self.voice_threshold = QProgressBar()
        self.voice_threshold.setRange(0, 4000)
        self.voice_threshold.setFormat("Echo/VAD threshold: %v")
        sl.addWidget(self.voice_threshold)
        self.voice_floor = QProgressBar()
        self.voice_floor.setRange(0, 4000)
        self.voice_floor.setFormat("Echo floor: %v")
        sl.addWidget(self.voice_floor)

        diag_row = QHBoxLayout()
        self.voice_vad = QSpinBox()
        self.voice_vad.setRange(40, 12000)
        self.voice_vad.setSingleStep(20)
        self.voice_vad.setPrefix("VAD ")
        diag_row.addWidget(self.voice_vad)
        self.voice_multiplier = QDoubleSpinBox()
        self.voice_multiplier.setRange(1.0, 4.0)
        self.voice_multiplier.setSingleStep(0.05)
        self.voice_multiplier.setDecimals(2)
        self.voice_multiplier.setPrefix("Gate × ")
        diag_row.addWidget(self.voice_multiplier)
        self.voice_margin = QSpinBox()
        self.voice_margin.setRange(0, 4000)
        self.voice_margin.setSingleStep(20)
        self.voice_margin.setPrefix("Margin ")
        diag_row.addWidget(self.voice_margin)
        self.voice_alpha = QDoubleSpinBox()
        self.voice_alpha.setRange(0.05, 0.95)
        self.voice_alpha.setSingleStep(0.05)
        self.voice_alpha.setDecimals(2)
        self.voice_alpha.setPrefix("Adapt ")
        diag_row.addWidget(self.voice_alpha)
        sl.addLayout(diag_row)

        diag_buttons = QHBoxLayout()
        apply_voice = QPushButton("Применить калибровку")
        apply_voice.clicked.connect(
            lambda: self.voice_tune.emit(
                int(self.voice_vad.value()),
                float(self.voice_multiplier.value()),
                int(self.voice_margin.value()),
                float(self.voice_alpha.value()),
            )
        )
        diag_buttons.addWidget(apply_voice)
        reset_voice = QPushButton("Сбросить Voice Diagnostics")
        reset_voice.clicked.connect(self.voice_diag_reset)
        diag_buttons.addWidget(reset_voice)
        diag_buttons.addStretch(1)
        sl.addLayout(diag_buttons)

        self.voice_diag_status = QLabel("Калибровка хранится только в RAM.")
        self.voice_diag_status.setObjectName("muted")
        self.voice_diag_status.setWordWrap(True)
        sl.addWidget(self.voice_diag_status)

        persona_title = QLabel("Persona Люмы")
        persona_title.setStyleSheet("font-size:16px; font-weight:700; margin-top:12px;")
        sl.addWidget(persona_title)
        self.persona_boxes: dict[str, QComboBox] = {}
        persona_fields = (
            ("address", "Обращение"),
            ("verbosity", "Подробность"),
            ("humor", "Юмор"),
            ("initiative", "Инициативность"),
            ("relationship", "Тон отношений"),
        )
        for key, label_text in persona_fields:
            row = QHBoxLayout()
            label = QLabel(label_text)
            label.setMinimumWidth(130)
            combo = QComboBox()
            combo.setObjectName("persona_" + key)
            row.addWidget(label)
            row.addWidget(combo, 1)
            sl.addLayout(row)
            self.persona_boxes[key] = combo
        persona_buttons = QHBoxLayout()
        save_persona = QPushButton("Сохранить стиль")
        save_persona.setObjectName("persona_save")
        save_persona.clicked.connect(self._emit_persona_save)
        reset_persona = QPushButton("По умолчанию")
        reset_persona.setObjectName("persona_reset")
        reset_persona.clicked.connect(lambda: self.persona_reset.emit())
        persona_buttons.addWidget(save_persona)
        persona_buttons.addWidget(reset_persona)
        persona_buttons.addStretch(1)
        sl.addLayout(persona_buttons)
        self.persona_status = QLabel("Загрузка профиля…")
        self.persona_status.setObjectName("muted")
        sl.addWidget(self.persona_status)
        autonomy_title = QLabel("Autonomy")
        autonomy_title.setStyleSheet("font-size:16px; font-weight:700; margin-top:12px;")
        sl.addWidget(autonomy_title)

        level_row = QHBoxLayout()
        level_label = QLabel("Глобальный уровень")
        level_label.setMinimumWidth(130)
        self.autonomy_level = QComboBox()
        self.autonomy_level.setObjectName("autonomy_level")
        level_row.addWidget(level_label)
        level_row.addWidget(self.autonomy_level, 1)
        sl.addLayout(level_row)

        self.autonomy_provider_boxes: dict[str, QComboBox] = {}
        for provider_name, title_text in (
            ("browser", "Browser"),
            ("desktop", "Desktop"),
            ("printflow", "PrintFlow"),
            ("personal", "Personal"),
            ("core", "Core"),
        ):
            row = QHBoxLayout()
            label = QLabel(title_text)
            label.setMinimumWidth(130)
            combo = QComboBox()
            combo.setObjectName("autonomy_provider_" + provider_name)
            row.addWidget(label)
            row.addWidget(combo, 1)
            sl.addLayout(row)
            self.autonomy_provider_boxes[provider_name] = combo

        autonomy_buttons = QHBoxLayout()
        save_autonomy = QPushButton("Сохранить автономность")
        save_autonomy.setObjectName("autonomy_save")
        save_autonomy.clicked.connect(self._emit_autonomy_save)
        reset_autonomy = QPushButton("По умолчанию")
        reset_autonomy.setObjectName("autonomy_reset")
        reset_autonomy.clicked.connect(lambda: self.autonomy_reset.emit())
        autonomy_buttons.addWidget(save_autonomy)
        autonomy_buttons.addWidget(reset_autonomy)
        autonomy_buttons.addStretch(1)
        sl.addLayout(autonomy_buttons)

        self.autonomy_status = QLabel("Загрузка policy…")
        self.autonomy_status.setObjectName("muted")
        self.autonomy_status.setWordWrap(True)
        sl.addWidget(self.autonomy_status)

        proactivity_title = QLabel("Proactivity")
        proactivity_title.setStyleSheet("font-size:16px; font-weight:700; margin-top:12px;")
        sl.addWidget(proactivity_title)

        pro_mode_row = QHBoxLayout()
        pro_mode_label = QLabel("Режим")
        pro_mode_label.setMinimumWidth(130)
        self.proactivity_mode = QComboBox()
        self.proactivity_mode.setObjectName("proactivity_mode")
        pro_mode_row.addWidget(pro_mode_label)
        pro_mode_row.addWidget(self.proactivity_mode, 1)
        sl.addLayout(pro_mode_row)

        pro_budget_row = QHBoxLayout()
        pro_budget_label = QLabel("Несрочных / час")
        pro_budget_label.setMinimumWidth(130)
        self.proactivity_budget = QComboBox()
        self.proactivity_budget.setObjectName("proactivity_budget")
        for value in (0, 1, 2, 3, 5, 10):
            self.proactivity_budget.addItem(str(value), value)
        pro_budget_row.addWidget(pro_budget_label)
        pro_budget_row.addWidget(self.proactivity_budget, 1)
        sl.addLayout(pro_budget_row)

        pro_cooldown_row = QHBoxLayout()
        pro_cooldown_label = QLabel("Cooldown")
        pro_cooldown_label.setMinimumWidth(130)
        self.proactivity_cooldown = QComboBox()
        self.proactivity_cooldown.setObjectName("proactivity_cooldown")
        for value in (5, 10, 15, 30, 60, 120):
            self.proactivity_cooldown.addItem(f"{value} мин", value)
        pro_cooldown_row.addWidget(pro_cooldown_label)
        pro_cooldown_row.addWidget(self.proactivity_cooldown, 1)
        sl.addLayout(pro_cooldown_row)

        quiet_row = QHBoxLayout()
        quiet_label = QLabel("Тихие часы")
        quiet_label.setMinimumWidth(130)
        self.proactivity_quiet_start = QLineEdit()
        self.proactivity_quiet_start.setObjectName("proactivity_quiet_start")
        self.proactivity_quiet_start.setPlaceholderText("22:00")
        self.proactivity_quiet_start.setMaximumWidth(90)
        self.proactivity_quiet_end = QLineEdit()
        self.proactivity_quiet_end.setObjectName("proactivity_quiet_end")
        self.proactivity_quiet_end.setPlaceholderText("08:00")
        self.proactivity_quiet_end.setMaximumWidth(90)
        quiet_row.addWidget(quiet_label)
        quiet_row.addWidget(self.proactivity_quiet_start)
        quiet_row.addWidget(QLabel("→"))
        quiet_row.addWidget(self.proactivity_quiet_end)
        quiet_row.addStretch(1)
        sl.addLayout(quiet_row)

        pro_buttons = QHBoxLayout()
        save_proactivity = QPushButton("Сохранить инициативность")
        save_proactivity.setObjectName("proactivity_save")
        save_proactivity.clicked.connect(self._emit_proactivity_save)
        reset_proactivity = QPushButton("По умолчанию")
        reset_proactivity.setObjectName("proactivity_reset")
        reset_proactivity.clicked.connect(lambda: self.proactivity_reset.emit())
        pro_buttons.addWidget(save_proactivity)
        pro_buttons.addWidget(reset_proactivity)
        pro_buttons.addStretch(1)
        sl.addLayout(pro_buttons)

        self.proactivity_status = QLabel("Загрузка Event Engine…")
        self.proactivity_status.setObjectName("muted")
        self.proactivity_status.setWordWrap(True)
        sl.addWidget(self.proactivity_status)

        self.events_view = QTextBrowser()
        self.events_view.setMaximumHeight(150)
        self.events_view.setPlainText("Событий пока нет.")
        sl.addWidget(self.events_view)
        providers_title = QLabel("Providers")
        providers_title.setStyleSheet("font-size:16px; font-weight:700; margin-top:12px;")
        sl.addWidget(providers_title)
        self.providers_view = QTextBrowser()
        self.providers_view.setMaximumHeight(220)
        self.providers_view.setPlainText("Загрузка…")
        sl.addWidget(self.providers_view)
        note = QLabel(
            "Нативный интерфейс не содержит мозг ассистента. Он подключается "
            "к локальному agent core на 127.0.0.1; старый /ui остаётся fallback."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        sl.addWidget(note)
        sl.addStretch(1)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.NoFrame)
        settings_scroll.setWidget(settings)
        self.pages["settings"] = settings_scroll
        self.stack.addWidget(settings_scroll)

        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)

        self.footer = QLabel("● подключение…")
        self.footer.setObjectName("footer")
        outer.addWidget(self.footer)

        self.nav.currentRowChanged.connect(self._change)
        self.nav.setCurrentRow(0)
        self.setStyleSheet("""
        QMainWindow, QWidget { background:#0b1120; color:#e5e7eb; font-size:14px; }
        QListWidget#nav { background:#111827; border:0; padding:14px 8px; }
        QListWidget#nav::item { padding:12px 14px; border-radius:9px; margin:2px; }
        QListWidget#nav::item:selected { background:#1d4ed8; color:white; }
        QLabel#pageTitle { font-size:24px; font-weight:700; }
        QLabel#muted { color:#94a3b8; }
        QLabel#footer { background:#111827; color:#94a3b8; padding:8px 14px; }
        QTextBrowser, QLineEdit { background:#111827; color:#e5e7eb; border:1px solid #263244; border-radius:10px; padding:10px; }
        QPushButton { background:#1e293b; color:#e5e7eb; border:0; border-radius:9px; padding:9px 13px; }
        QPushButton:hover { background:#334155; }
        QPushButton#primary { background:#2563eb; }
        QPushButton#primary:hover { background:#1d4ed8; }
        QPushButton#danger { background:#991b1b; color:white; font-weight:700; }
        QPushButton#danger:hover { background:#b91c1c; }
        """)

    def _change(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        key = self.nav.item(row).data(Qt.UserRole)
        if key != "chat":
            self.refresh_page.emit(str(key))

    def set_voice_diagnostics(self, *, audio_level: int, echo_floor: int,
                              echo_threshold: int, echo_suppressed: int,
                              multiplier: float, margin: int, alpha: float,
                              vad_threshold: int, asr_engine: str,
                              vocabulary_count: int) -> None:
        top = max(4000, int(audio_level), int(echo_floor), int(echo_threshold), int(vad_threshold))
        for bar in (self.voice_level, self.voice_floor, self.voice_threshold):
            bar.setMaximum(top)
        self.voice_level.setValue(max(0, int(audio_level)))
        self.voice_floor.setValue(max(0, int(echo_floor)))
        self.voice_threshold.setValue(max(0, int(echo_threshold)))
        editing = any(
            widget.hasFocus()
            for widget in (self.voice_vad, self.voice_multiplier, self.voice_margin, self.voice_alpha)
        )
        if not editing:
            self.voice_vad.blockSignals(True)
            self.voice_multiplier.blockSignals(True)
            self.voice_margin.blockSignals(True)
            self.voice_alpha.blockSignals(True)
            self.voice_vad.setValue(int(vad_threshold))
            self.voice_multiplier.setValue(float(multiplier))
            self.voice_margin.setValue(int(margin))
            self.voice_alpha.setValue(float(alpha))
            self.voice_vad.blockSignals(False)
            self.voice_multiplier.blockSignals(False)
            self.voice_margin.blockSignals(False)
            self.voice_alpha.blockSignals(False)
        engine = str(asr_engine or "не загружен")
        self.voice_diag_meta.setText(
            f"ASR: {engine} · vocabulary: {int(vocabulary_count)} · "
            f"VAD: {int(vad_threshold)} · echo suppressed: {int(echo_suppressed)}"
        )

    def set_voice_diagnostics_message(self, text: str) -> None:
        self.voice_diag_status.setText(str(text or ""))

    def update_status(self, connected: bool, armed: bool, model_ok: bool,
                      panel_ok: bool, error: str = "", safety_stopped: bool = False) -> None:
        if connected:
            bits = ["Люма ✓", "модель ✓" if model_ok else "модель –",
                    "PrintFlow ✓" if panel_ok else "PrintFlow –",
                    "wake ✓" if armed else "wake –",
                    "STOP ALL" if safety_stopped else "готова"]
            self.footer.setText("   ".join(bits))
            self.settings_status.setText("Backend: подключён · 127.0.0.1:8799")
        else:
            self.footer.setText("Агент недоступен" + (f": {error}" if error else ""))
            self.settings_status.setText("Backend: недоступен" + (f" · {error}" if error else ""))
        self.mic_button.setText("🎤 Выключить wake word" if armed else "🎤 Включить wake word")
        self.stop_all_button.setText("▶ Снять STOP ALL" if safety_stopped else "⛔ STOP ALL")

    def _emit_persona_save(self) -> None:
        profile = {
            key: str(combo.currentData() or "")
            for key, combo in self.persona_boxes.items()
        }
        self.persona_save.emit(profile)

    def _emit_autonomy_save(self) -> None:
        level = str(self.autonomy_level.currentData() or self.autonomy_level.currentText())
        providers = {
            key: str(combo.currentData() or combo.currentText())
            for key, combo in self.autonomy_provider_boxes.items()
        }
        self.autonomy_save.emit(level, providers)

    def set_autonomy_payload(self, payload: dict[str, Any]) -> None:
        levels = [str(value) for value in (payload.get("levels") or [])]
        labels = dict(payload.get("labels") or {})
        current = str(payload.get("level") or "agent")
        self.autonomy_level.blockSignals(True)
        self.autonomy_level.clear()
        for value in levels:
            self.autonomy_level.addItem(str(labels.get(value) or value), value)
        index = self.autonomy_level.findData(current)
        if index >= 0:
            self.autonomy_level.setCurrentIndex(index)
        self.autonomy_level.blockSignals(False)

        provider_rows = dict(payload.get("providers") or {})
        for name, combo in self.autonomy_provider_boxes.items():
            row = dict(provider_rows.get(name) or {})
            hard = str(row.get("hard_max") or "agent")
            selected = str(row.get("level") or hard)
            combo.clear()
            hard_index = levels.index(hard) if hard in levels else len(levels) - 1
            for value in levels[:hard_index + 1]:
                combo.addItem(str(labels.get(value) or value), value)
            idx = combo.findData(selected)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        self.autonomy_status.setText(str(payload.get("invariant") or ""))

    def set_autonomy_message(self, text: str) -> None:
        self.autonomy_status.setText(str(text or ""))

    def _emit_proactivity_save(self) -> None:
        self.proactivity_save.emit({
            "mode": str(self.proactivity_mode.currentData() or self.proactivity_mode.currentText()),
            "max_nonurgent_per_hour": int(self.proactivity_budget.currentData() or 0),
            "cooldown_minutes": int(self.proactivity_cooldown.currentData() or 30),
            "quiet_start": self.proactivity_quiet_start.text().strip(),
            "quiet_end": self.proactivity_quiet_end.text().strip(),
        })

    def set_proactivity_payload(self, payload: dict[str, Any]) -> None:
        settings = dict(payload.get("settings") or {})
        modes = [str(value) for value in (payload.get("modes") or [])]
        labels = {
            "silent": "Silent · только явные напоминания",
            "important": "Important only",
            "balanced": "Balanced",
            "active": "Active",
        }
        self.proactivity_mode.clear()
        for value in modes:
            self.proactivity_mode.addItem(labels.get(value, value), value)
        idx = self.proactivity_mode.findData(str(settings.get("mode") or "balanced"))
        if idx >= 0:
            self.proactivity_mode.setCurrentIndex(idx)

        for combo, key, default in (
            (self.proactivity_budget, "max_nonurgent_per_hour", 2),
            (self.proactivity_cooldown, "cooldown_minutes", 30),
        ):
            raw = settings.get(key)
            value = default if raw is None or raw == "" else int(raw)
            idx = combo.findData(value)
            if idx < 0:
                combo.addItem(str(value) if key == "max_nonurgent_per_hour" else f"{value} мин", value)
                idx = combo.findData(value)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        self.proactivity_quiet_start.setText(str(settings.get("quiet_start") or "22:00"))
        self.proactivity_quiet_end.setText(str(settings.get("quiet_end") or "08:00"))

        autopilot = bool(payload.get("autopilot"))
        prefix = "Autopilot включён." if autopilot else "Autopilot выключен: инициативные события журналируются, но не прерывают пользователя."
        self.proactivity_status.setText(prefix + " " + str(payload.get("invariant") or ""))

        rows = list(payload.get("recent") or [])
        lines = []
        for row in rows[:12]:
            state = str(row.get("state") or "new")
            title = str(row.get("title") or row.get("kind") or "event")
            reason = str(row.get("reason") or "")
            line = f"[{state}] {title}"
            if reason:
                line += f"\n  {reason}"
            lines.append(line)
        self.events_view.setPlainText("\n\n".join(lines) if lines else "Событий пока нет.")

    def set_proactivity_message(self, text: str) -> None:
        self.proactivity_status.setText(str(text or ""))

    def set_persona_payload(self, payload: dict[str, Any]) -> None:
        profile = dict(payload.get("profile") or {})
        options = dict(payload.get("options") or {})
        labels = dict(payload.get("labels") or {})
        for key, combo in self.persona_boxes.items():
            current = str(profile.get(key) or "")
            combo.blockSignals(True)
            combo.clear()
            for value in options.get(key) or []:
                title = str((labels.get(key) or {}).get(value) or value)
                combo.addItem(title, value)
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.blockSignals(False)
        self.persona_status.setText("Стиль влияет только на форму ответа, не на права и подтверждения.")

    def set_persona_message(self, text: str) -> None:
        self.persona_status.setText(str(text or ""))

    def set_providers_payload(self, payload: dict[str, Any]) -> None:
        rows = list(payload.get("providers") or [])
        if not rows:
            self.providers_view.setPlainText("Providers пока не зарегистрированы.")
            return
        lines = []
        for row in rows:
            mark = "✓" if row.get("available") else "–"
            title = str(row.get("title") or row.get("name") or "provider")
            skill_names = [str(item.get("name") or "") for item in (row.get("skills") or [])]
            detail = ", ".join(skill_names) if skill_names else "skills не перенесены"
            line = f"{mark} {title}: {detail}"
            if not row.get("available") and row.get("reason"):
                line += f"\n  {row.get('reason')}"
            lines.append(line)
        self.providers_view.setPlainText("\n\n".join(lines))

    def set_planner_message(self, text: str) -> None:
        page = self.pages.get("tasks")
        if isinstance(page, TasksPage):
            page.set_planner_message(text)

    def set_page_payload(self, key: str, payload: Any) -> None:
        page = self.pages.get(key)
        if isinstance(page, TextPage):
            page.set_payload(payload)
        elif isinstance(page, TasksPage) and isinstance(payload, dict):
            page.set_payload(payload)
        elif isinstance(page, ActivityPage) and isinstance(payload, dict):
            page.set_payload(payload)

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
