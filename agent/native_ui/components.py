"""Reusable visual components for Luma's native UI."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, Property, QPropertyAnimation, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


class AmbientCanvas(QWidget):
    """Cheap atmospheric background without blur effects."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ambientCanvas")
        self._glow = 0.58
        self._animation = QPropertyAnimation(self, b"glow", self)
        self._animation.setDuration(5200)
        self._animation.setStartValue(0.48)
        self._animation.setEndValue(0.68)
        self._animation.setEasingCurve(QEasingCurve.InOutSine)
        self._animation.setLoopCount(-1)
        self._animation.start()

    def get_glow(self) -> float:
        return float(self._glow)

    def set_glow(self, value: float) -> None:
        self._glow = max(0.0, min(1.0, float(value)))
        self.update()

    glow = Property(float, get_glow, set_glow)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#070816"))

        w, h = max(1, self.width()), max(1, self.height())
        violet = QRadialGradient(w * 0.74, h * 0.28, max(w, h) * 0.58)
        violet.setColorAt(0.0, QColor(100, 63, 210, int(52 * self._glow)))
        violet.setColorAt(0.46, QColor(58, 38, 142, int(28 * self._glow)))
        violet.setColorAt(1.0, QColor(7, 8, 22, 0))
        painter.fillRect(self.rect(), QBrush(violet))

        cyan = QRadialGradient(w * 0.58, h * 0.86, max(w, h) * 0.45)
        cyan.setColorAt(0.0, QColor(46, 186, 229, int(25 * self._glow)))
        cyan.setColorAt(1.0, QColor(7, 8, 22, 0))
        painter.fillRect(self.rect(), QBrush(cyan))


class GlassCard(QFrame):
    def __init__(self, accent: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("glassCard")
        if accent:
            self.setProperty("accent", accent)


class BrandCard(GlassCard):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.setObjectName("brandCard")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        identity = QVBoxLayout()
        identity.setSpacing(0)
        brand = QLabel("LUMA")
        brand.setObjectName("brand")
        subtitle = QLabel("LOCAL AI ASSISTANT")
        subtitle.setObjectName("brandSub")
        identity.addWidget(brand)
        identity.addWidget(subtitle)

        self.dot = QLabel()
        self.dot.setObjectName("onlineDot")
        self.dot.setFixedSize(12, 12)

        layout.addLayout(identity, 1)
        layout.addWidget(self.dot, 0, Qt.AlignVCenter)

    def set_online(self, online: bool) -> None:
        if online:
            self.dot.setStyleSheet("background:#58E6B1;border:2px solid #163B35;border-radius:6px;")
        else:
            self.dot.setStyleSheet("background:#F05266;border:2px solid #4A1826;border-radius:6px;")


class StatusHeader(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("topBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 14, 10)
        layout.setSpacing(10)

        text = QVBoxLayout()
        text.setSpacing(1)
        self.title = QLabel("Люма готова")
        self.title.setObjectName("statusTitle")
        self.subtitle = QLabel("Локальный режим · ожидаю команду")
        self.subtitle.setObjectName("statusSub")
        text.addWidget(self.title)
        text.addWidget(self.subtitle)

        self.state_pill = QLabel("IDLE")
        self.state_pill.setObjectName("statusPill")
        self.local_pill = QLabel("LOCAL")
        self.local_pill.setObjectName("localPill")

        layout.addLayout(text, 1)
        layout.addWidget(self.state_pill)
        layout.addWidget(self.local_pill)

    def set_status(self, *, connected: bool, armed: bool, model_ok: bool,
                   panel_ok: bool, safety_stopped: bool, error: str = "",
                   assistant_state: str = "idle") -> None:
        if not connected:
            self.title.setText("Люма недоступна")
            self.subtitle.setText(error or "Локальный backend не отвечает")
            self.state_pill.setText("OFFLINE")
            return

        if safety_stopped:
            self.title.setText("Все действия остановлены")
            self.subtitle.setText("STOP ALL активен")
            self.state_pill.setText("STOPPED")
            return

        state = str(assistant_state or "idle").casefold()
        titles = {
            "idle": "Люма готова",
            "listening": "Люма слушает",
            "thinking": "Люма думает",
            "speaking": "Люма отвечает",
            "executing": "Люма выполняет",
            "task": "Люма ведёт задачу",
            "waiting": "Люма ждёт",
            "error": "Люме нужна помощь",
        }
        self.title.setText(titles.get(state, "Люма работает"))
        pieces = [
            "wake word включён" if armed else "wake word выключен",
            "модель готова" if model_ok else "модель не готова",
            "PrintFlow подключён" if panel_ok else "PrintFlow не подключён",
        ]
        self.subtitle.setText(" · ".join(pieces))
        self.state_pill.setText(state.upper() if state else ("LISTENING" if armed else "IDLE"))
