"""Full Control Center NOZZA: разговор, дела, память, обучение и настройки."""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QPushButton, QScrollArea, QStackedWidget, QTextBrowser,
    QVBoxLayout, QWidget,
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
        self.feed = QTextBrowser()
        layout.addWidget(self.feed, 1)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Спроси NOZZA или скажи, что сделать…")
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
            who = "Вы" if role == "user" else "NOZZA"
            text = str(turn.get("text") or "")
            chunks.append(f"<p><b>{who}</b><br>{text}</p>")
        self.feed.setHtml("".join(chunks))
        bar = self.feed.verticalScrollBar()
        bar.setValue(bar.maximum())

    def append_local(self, who: str, text: str) -> None:
        self.feed.append(f"<p><b>{who}</b><br>{text}</p>")


class TasksPage(QWidget):
    decision = Signal(str, bool)
    command = Signal(int, str)
    plan_preview = Signal(str)
    plan_command = Signal(str, str)
    replan_preview = Signal(int)
    replan_command = Signal(str, str)
    persona_save = Signal(object)
    persona_reset = Signal()
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
    task_decision = Signal(str, bool)
    task_command = Signal(int, str)
    plan_preview = Signal(str)
    plan_command = Signal(str, str)
    replan_preview = Signal(int)
    replan_command = Signal(str, str)
    persona_save = Signal(object)
    persona_reset = Signal()

    NAV = [
        ("chat", "💬  Разговор"),
        ("today", "☀  Сегодня"),
        ("tasks", "✓  Задачи"),
        ("memory", "🧠  Память"),
        ("learning", "🎓  Обучение"),
        ("skills", "⚡  Навыки"),
        ("journal", "≡  Журнал"),
        ("settings", "⚙  Настройки"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("NOZZA Assistant")
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

        defs = {
            "memory": ("Память", "Факты, предпочтения и то, что вы просили запомнить."),
            "learning": ("Обучение", "Чему NOZZA научилась и что пока не понимает."),
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
        self.settings_hotkey = QLabel("Быстрая команда: Ctrl + Shift + Space")
        sl.addWidget(self.settings_hotkey)
        self.mic_button = QPushButton("🎤 Включить wake word")
        self.mic_button.clicked.connect(self.mic_toggle)
        sl.addWidget(self.mic_button)
        persona_title = QLabel("Persona NOZZA")
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
        self.pages["settings"] = settings
        self.stack.addWidget(settings)

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
        """)

    def _change(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        key = self.nav.item(row).data(Qt.UserRole)
        if key != "chat":
            self.refresh_page.emit(str(key))

    def update_status(self, connected: bool, armed: bool, model_ok: bool,
                      panel_ok: bool, error: str = "") -> None:
        if connected:
            bits = ["агент ✓", "модель ✓" if model_ok else "модель –",
                    "PrintFlow ✓" if panel_ok else "PrintFlow –",
                    "wake ✓" if armed else "wake –"]
            self.footer.setText("   ".join(bits))
            self.settings_status.setText("Backend: подключён · 127.0.0.1:8799")
        else:
            self.footer.setText("Агент недоступен" + (f": {error}" if error else ""))
            self.settings_status.setText("Backend: недоступен" + (f" · {error}" if error else ""))
        self.mic_button.setText("🎤 Выключить wake word" if armed else "🎤 Включить wake word")

    def _emit_persona_save(self) -> None:
        profile = {
            key: str(combo.currentData() or "")
            for key, combo in self.persona_boxes.items()
        }
        self.persona_save.emit(profile)

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

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
