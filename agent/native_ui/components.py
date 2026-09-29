"""Reusable visual components for Luma's native UI."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPointF, Property, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QRadialGradient
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


class LumaPortrait(QWidget):
    """Luma's portrait with a state-colored, softly animated halo."""

    STATE_COLORS = {
        "idle": QColor("#8B5CF6"),
        "listening": QColor("#43D7FF"),
        "thinking": QColor("#A78BFA"),
        "speaking": QColor("#58E6B1"),
        "working": QColor("#F5B84C"),
        "waiting": QColor("#9996B7"),
        "error": QColor("#F05266"),
        "stopped": QColor("#F05266"),
    }

    def __init__(self, parent: QWidget | None = None, *, compact: bool = False) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._phase = 0.0
        self._compact = bool(compact)
        self._portrait = QPixmap(str(Path(__file__).resolve().parent / "assets" / "luma-portrait.png"))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(108, 132)
        if compact:
            self.setFixedSize(82, 96)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_state(self, state: str) -> None:
        clean = str(state or "idle").casefold()
        if clean in ("executing", "task"):
            clean = "working"
        self._state = clean if clean in self.STATE_COLORS else "idle"
        self.update()

    def _tick(self) -> None:
        if self.isVisible() and self._state not in ("stopped",):
            self._phase = (self._phase + 0.045) % 6.283185307
            self.update()

    @staticmethod
    def _with_alpha(color: QColor, alpha: int) -> QColor:
        out = QColor(color)
        out.setAlpha(max(0, min(255, int(alpha))))
        return out

    def paintEvent(self, _event) -> None:  # noqa: N802
        import math

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        w, h = float(self.width()), float(self.height())
        cx = w * 0.5
        color = self.STATE_COLORS[self._state]
        pulse = 0.5 + 0.5 * math.sin(self._phase)

        # Halo behind the character.
        halo = QRadialGradient(QPointF(cx, h * 0.47), max(w, h) * 0.56)
        halo.setColorAt(0.0, self._with_alpha(color, 78 + int(24 * pulse)))
        halo.setColorAt(0.48, self._with_alpha(color, 25))
        halo.setColorAt(1.0, self._with_alpha(color, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(QPointF(cx, h * 0.48), w * 0.49, h * 0.48)

        if not self._portrait.isNull():
            # Crop a little of the wide artwork in compact placements so the face remains legible.
            inset = 0.1 if self._compact else 0.0
            source = QRectF(
                self._portrait.width() * inset,
                0,
                self._portrait.width() * (1 - 2 * inset),
                self._portrait.height(),
            )
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawPixmap(QRectF(0, 0, w, h), self._portrait, source)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(self._with_alpha(color, 120 + int(60 * pulse)), max(1.0, w * 0.012)))
        painter.drawEllipse(QRectF(w * 0.06, h * 0.12, w * 0.88, h * 0.76))


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
