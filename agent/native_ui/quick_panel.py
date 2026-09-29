"""Quick Panel в стиле Spotlight/PowerToys Run."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from . import theme


class QuickPanel(QWidget):
    submitted = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(680)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet(theme.stylesheet() + """
        QFrame#card {
            background:#101225;
            border:1px solid #514487;
            border-radius:20px;
        }
        QLineEdit {
            background:#15172D;
            border:1px solid #3B3F69;
            border-radius:13px;
            color:#F8F7FF;
            font-size:18px;
            padding:14px 16px;
        }
        QLineEdit:focus {
            border:1px solid #8B6EF0;
            background:#181A33;
        }
        QLabel { background:transparent; color:#9996B7; }
        QLabel#quickBrand {
            color:#FFFFFF;
            font-size:17px;
            font-weight:800;
            letter-spacing:1px;
        }
        QPushButton {
            background:#181B35;
            color:#D8D4E8;
            border:1px solid #35395F;
            border-radius:10px;
            padding:8px 11px;
        }
        QPushButton:hover {
            background:#242044;
            border-color:#6654B8;
        }
        """)
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(12)
        header = QHBoxLayout()
        brand = QLabel("LUMA")
        brand.setObjectName("quickBrand")
        header.addWidget(brand)
        header.addStretch(1)
        header.addWidget(QLabel("Esc — закрыть"))
        box.addLayout(header)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Спроси Люму или скажи, что сделать…")
        self.input.returnPressed.connect(self._submit)
        row.addWidget(self.input, 1)
        box.addLayout(row)
        self.answer = QLabel("")
        self.answer.setWordWrap(True)
        self.answer.setStyleSheet("color:#E7E3F3; font-size:14px; background:transparent;")
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
