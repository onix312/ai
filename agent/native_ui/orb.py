"""Voice Orb: небольшой always-on-top индикатор состояния."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class VoiceOrb(QWidget):
    LABELS = {
        "idle": "Готов",
        "listening": "Слушаю",
        "thinking": "Думаю",
        "speaking": "Говорю",
        "error": "Ошибка",
    }

    def __init__(self) -> None:
        super().__init__()
        self._state = "idle"
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
            Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(170, 68)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        self.label = QLabel("Готов")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("color: white; font-size: 14px; font-weight: 700;")
        layout.addWidget(self.label)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def set_state(self, state: str, auto_hide_ms: int = 0) -> None:
        self._state = state if state in self.LABELS else "idle"
        self.label.setText(self.LABELS[self._state])
        self.update()
        self.show_near_bottom()
        if auto_hide_ms:
            self._hide_timer.start(auto_hide_ms)

    def show_near_bottom(self) -> None:
        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2, geo.bottom() - self.height() - 48)
        self.show()
        self.raise_()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        tones = {
            "idle": QColor("#334155"),
            "listening": QColor("#0891b2"),
            "thinking": QColor("#7c3aed"),
            "speaking": QColor("#16a34a"),
            "error": QColor("#dc2626"),
        }
        c = tones[self._state]
        painter.setPen(QPen(QColor(255, 255, 255, 36), 1))
        painter.setBrush(c)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 18, 18)
