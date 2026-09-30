"""Quick Panel в стиле Spotlight/PowerToys Run."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from . import theme
from .components import LumaPortrait


class RoundMicButton(QPushButton):
    """Circular microphone control with a quiet breathing glow."""

    def __init__(self) -> None:
        super().__init__("")
        self.setFixedSize(54, 54)
        self.setToolTip("Включить или выключить микрофон")
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        if self.isVisible():
            self._phase = (self._phase + 0.07) % math.tau
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pulse = 0.5 + 0.5 * math.sin(self._phase)
        center = QPointF(27, 27)
        glow = QRadialGradient(center, 27)
        glow.setColorAt(0.0, QColor(66, 116, 255, 130 + int(45 * pulse)))
        glow.setColorAt(0.72, QColor(67, 61, 221, 95))
        glow.setColorAt(1.0, QColor(71, 52, 222, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, 27, 27)
        painter.setBrush(QColor("#3158DD"))
        painter.setPen(QPen(QColor("#B9C8FF"), 2))
        painter.drawEllipse(center, 21, 21)
        painter.setPen(QPen(QColor("#FFFFFF"), 2.3, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(23, 17, 8, 17, 4, 4)
        painter.drawArc(19, 22, 16, 17, 180 * 16, 180 * 16)
        painter.drawLine(27, 39, 27, 43)
        painter.drawLine(23, 43, 31, 43)


class QuickActionButton(QPushButton):
    """Readable action tile with a vector icon on every Windows font setup."""

    def __init__(self, title: str, icon: str) -> None:
        super().__init__("")
        self.title = title
        self.icon = icon
        self.setMinimumHeight(74)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setBrush(QColor("#24214B" if self.underMouse() else "#181B35"))
        painter.setPen(QPen(QColor("#9A7CFF" if self.underMouse() else "#35395F"), 1))
        painter.drawRoundedRect(rect, 10, 10)
        x = self.width() / 2
        y = 21
        painter.setPen(QPen(QColor("#CFBAFF"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        if self.icon == "app":
            painter.drawRoundedRect(int(x - 10), 12, 20, 18, 3, 3)
            painter.drawLine(int(x), 15, int(x), 27)
            painter.drawLine(int(x - 6), 21, int(x + 6), 21)
        elif self.icon == "file":
            painter.drawLine(int(x - 11), 14, int(x - 2), 14)
            painter.drawLine(int(x - 2), 14, int(x + 1), 18)
            painter.drawLine(int(x + 1), 18, int(x + 11), 18)
            painter.drawLine(int(x - 11), 14, int(x - 11), 29)
            painter.drawLine(int(x - 11), 29, int(x + 11), 29)
            painter.drawLine(int(x + 11), 18, int(x + 11), 29)
        elif self.icon == "music":
            painter.drawLine(int(x - 1), 12, int(x - 1), 27)
            painter.drawLine(int(x - 1), 12, int(x + 9), 10)
            painter.drawLine(int(x + 9), 10, int(x + 9), 24)
            painter.drawEllipse(int(x - 8), 25, 7, 5)
            painter.drawEllipse(int(x + 2), 22, 7, 5)
        else:
            painter.drawRoundedRect(int(x - 8), 11, 16, 19, 2, 2)
            painter.drawLine(int(x - 4), 17, int(x + 4), 17)
            painter.drawLine(int(x - 4), 22, int(x + 2), 22)
        painter.setPen(QColor("#F1EDFF"))
        painter.setFont(QFont("Segoe UI", 10))
        painter.drawText(self.rect().adjusted(6, 39, -6, -4), Qt.AlignCenter, self.title)


class QuickPanel(QWidget):
    submitted = Signal(str)
    mic_toggle = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(690)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet(theme.stylesheet() + """
        QFrame#card {
            background:#0C1025;
            border:2px solid #8466F7;
            border-radius:22px;
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
        box.setContentsMargins(20, 16, 20, 18)
        box.setSpacing(8)
        header = QHBoxLayout()
        brand = QLabel("✦  LUMA")
        brand.setObjectName("quickBrand")
        header.addWidget(brand)
        header.addStretch(1)
        header.addWidget(QLabel("БЫСТРАЯ ПАНЕЛЬ  ·  Esc — закрыть"))
        box.addLayout(header)
        hero = QHBoxLayout()
        copy = QVBoxLayout()
        greeting = QLabel("Привет!\nЯ LUMA")
        greeting.setStyleSheet("color:#FFFFFF;font-size:27px;font-weight:800;background:transparent;")
        copy.addWidget(greeting)
        subtitle = QLabel("Слушаю тебя…\nВсегда рядом ♡")
        subtitle.setStyleSheet("color:#D2C6FF;font-size:14px;background:transparent;")
        copy.addWidget(subtitle)
        copy.addStretch(1)
        hero.addLayout(copy, 2)
        portrait = LumaPortrait()
        portrait.setFixedSize(240, 168)
        hero.addWidget(portrait, 3, Qt.AlignRight)
        box.addLayout(hero)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Спроси Люму или скажи, что сделать…")
        self.input.returnPressed.connect(self._submit)
        row.addWidget(self.input, 1)
        box.addLayout(row)
        self.answer = QLabel("")
        self.answer.setWordWrap(True)
        self.answer.setStyleSheet("color:#E7E3F3; font-size:14px; background:transparent;")
        self.answer.hide()
        box.addWidget(self.answer)
        mic_row = QHBoxLayout()
        mic_row.addStretch(1)
        mic = RoundMicButton()
        mic.clicked.connect(self.mic_toggle)
        mic_row.addWidget(mic)
        mic_row.addStretch(1)
        box.addLayout(mic_row)
        actions = QHBoxLayout()
        for icon, title, prompt in (
            ("app", "Открыть\nприложение", "Открой "),
            ("file", "Найти\nфайл", "Найди файл "),
            ("music", "Включить\nмузыку", "Включи музыку"),
            ("note", "Создать\nзаметку", "Создай заметку "),
        ):
            button = QuickActionButton(title, icon)
            button.clicked.connect(lambda _=False, value=prompt: self._pick(value))
            actions.addWidget(button, 1)
        box.addLayout(actions)
        root.addWidget(card)

    def _pick(self, text: str) -> None:
        self.input.setText(text)
        if text.endswith(" "):
            self.input.setFocus()
        else:
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
            self.answer.show()

    def show_answer(self, text: str) -> None:
        self.answer.setText(text)
        self.answer.setVisible(bool(text))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
