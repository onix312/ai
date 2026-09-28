"""Full Control Center NOZZA: разговор, дела, память, обучение и настройки."""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
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
        self.host = QVBoxLayout()
        self.layout.addLayout(self.host)
        self.layout.addStretch(1)

    def _clear(self) -> None:
        while self.host.count():
            item = self.host.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def set_payload(self, payload: dict[str, Any]) -> None:
        self._clear()
        pending = list(payload.get("pending") or [])
        notifications = list(payload.get("notifications") or [])
        if not pending and not notifications:
            self.host.addWidget(QLabel("Ничего не ждёт."))
            return
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
        self.mic_button = QPushButton("🎤 Включить микрофон")
        self.mic_button.clicked.connect(self.mic_toggle)
        sl.addWidget(self.mic_button)
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
                    "микрофон ✓" if armed else "микрофон –"]
            self.footer.setText("   ".join(bits))
            self.settings_status.setText("Backend: подключён · 127.0.0.1:8799")
        else:
            self.footer.setText("Агент недоступен" + (f": {error}" if error else ""))
            self.settings_status.setText("Backend: недоступен" + (f" · {error}" if error else ""))
        self.mic_button.setText("🎤 Выключить микрофон" if armed else "🎤 Включить микрофон")

    def set_page_payload(self, key: str, payload: Any) -> None:
        page = self.pages.get(key)
        if isinstance(page, TextPage):
            page.set_payload(payload)
        elif isinstance(page, TasksPage) and isinstance(payload, dict):
            page.set_payload(payload)

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
