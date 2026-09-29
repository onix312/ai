"""Quick Panel в стиле Spotlight/PowerToys Run."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget


class QuickPanel(QWidget):
    submitted = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(720)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet("""
        QFrame#card { background:#0f172a; border:1px solid #334155; border-radius:20px; }
        QLineEdit { background:transparent; border:0; color:#f8fafc; font-size:20px; padding:16px; }
        QLabel { color:#94a3b8; }
        QPushButton { background:#1e293b; color:#cbd5e1; border:0; border-radius:9px; padding:7px 10px; }
        QPushButton:hover { background:#334155; }
        """)
        box = QVBoxLayout(card)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Спроси Люму или скажи, что сделать…")
        self.input.returnPressed.connect(self._submit)
        row.addWidget(self.input, 1)
        box.addLayout(row)
        self.answer = QLabel("")
        self.answer.setWordWrap(True)
        self.answer.setStyleSheet("color:#e2e8f0; padding:0 16px 8px 16px; font-size:14px;")
        box.addWidget(self.answer)
        self.hints = QHBoxLayout()
        for text in ("Что у меня сегодня?", "Как там PrintFlow?", "Открой загрузки"):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, t=text: self._pick(t))
            self.hints.addWidget(b)
        self.hints.addStretch(1)
        box.addLayout(self.hints)
        root.addWidget(card)

    def _pick(self, text: str) -> None:
        self.input.setText(text)
        self._submit()

    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.submitted.emit(text)

    def show_centered(self) -> None:
        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2, geo.top() + int(geo.height() * .22))
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()
        self.input.selectAll()

    def set_busy(self, busy: bool) -> None:
        self.input.setEnabled(not busy)
        if busy:
            self.answer.setText("Думаю…")

    def show_answer(self, text: str) -> None:
        self.answer.setText(text)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
