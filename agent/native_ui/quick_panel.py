"""Quick Panel в стиле Spotlight/PowerToys Run."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from . import theme


class QuickPanel(QWidget):
    submitted = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(680)
        self._busy = False
        self._busy_frame = 0
        self._show_animation = QPropertyAnimation(self, b"windowOpacity", self)
        self._show_animation.setDuration(150)
        self._show_animation.setStartValue(0.0)
        self._show_animation.setEndValue(1.0)
        self._show_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(320)
        self._busy_timer.timeout.connect(self._advance_busy)
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
        QLabel#quickRoute {
            background:#191733;
            color:#BFAEFF;
            border:1px solid #4E4083;
            border-radius:9px;
            padding:3px 8px;
            font-size:9px;
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
        self.route = QLabel("READY")
        self.route.setObjectName("quickRoute")
        header.addWidget(self.route)
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
        self._answer_opacity = QGraphicsOpacityEffect(self.answer)
        self._answer_opacity.setOpacity(1.0)
        self.answer.setGraphicsEffect(self._answer_opacity)
        self._answer_animation = QPropertyAnimation(self._answer_opacity, b"opacity", self)
        self._answer_animation.setDuration(170)
        self._answer_animation.setStartValue(0.15)
        self._answer_animation.setEndValue(1.0)
        self._answer_animation.setEasingCurve(QEasingCurve.OutCubic)
        box.addWidget(self.answer)
        self.hints = QHBoxLayout()
        for text in ("Что у меня сегодня?", "Как там PrintFlow?", "Открой загрузки"):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, t=text: self._pick(t))
            self.hints.addWidget(b)
        self.hints.addStretch(1)
        box.addLayout(self.hints)
        root.addWidget(card)

    @staticmethod
    def _route_label(source: str, repaired: bool = False) -> str:
        if repaired:
            return "SELF-CORRECTED"
        clean = str(source or "").casefold()
        return {
            "model": "MODEL",
            "rules": "RULES",
            "rule": "RULES",
            "panel": "PRINTFLOW",
            "panel-camera": "VISION",
            "task": "TASK",
            "planner": "PLANNER",
            "memory": "MEMORY",
            "agent-loop": "AGENT LOOP",
            "talk": "TALK",
            "clock": "LOCAL",
            "math": "LOCAL",
            "util": "LOCAL",
        }.get(clean, clean.upper()[:18] or "READY")

    def _set_route(self, source: str = "", skill: str = "", repaired: bool = False) -> None:
        route = self._route_label(source, repaired)
        skill_clean = str(skill or "").strip()
        self.route.setText(route + (f" · {skill_clean}" if skill_clean else ""))

    def _advance_busy(self) -> None:
        if not self._busy:
            return
        self._busy_frame = (self._busy_frame + 1) % 4
        self.answer.setText("Думаю" + "." * (self._busy_frame + 1))

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
        self._show_animation.stop()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self._show_animation.start()
        self.input.setFocus()
        self.input.selectAll()

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.input.setEnabled(not self._busy)
        if self._busy:
            self._busy_frame = 0
            self._set_route("model")
            self.route.setText("THINKING")
            self.answer.setText("Думаю.")
            self._busy_timer.start()
        else:
            self._busy_timer.stop()

    def show_answer(self, text: str, *, source: str = "", skill: str = "",
                    repaired: bool = False) -> None:
        self._set_route(source, skill, repaired)
        self.answer.setText(str(text or ""))
        self._answer_animation.stop()
        self._answer_opacity.setOpacity(0.15)
        self._answer_animation.start()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
