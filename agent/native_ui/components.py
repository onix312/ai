"""Reusable visual components for Luma's native UI."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPointF, Property, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QRadialGradient
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
    """Asset-free stylized female AI portrait used until a final character asset ships."""

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

        # Hair mass, deliberately long and soft to read as Luma's female persona.
        hair = QPainterPath()
        hair.moveTo(cx, h * 0.13)
        hair.cubicTo(w * 0.18, h * 0.15, w * 0.13, h * 0.44, w * 0.24, h * 0.77)
        hair.cubicTo(w * 0.30, h * 0.91, w * 0.39, h * 0.84, cx, h * 0.88)
        hair.cubicTo(w * 0.63, h * 0.85, w * 0.72, h * 0.92, w * 0.78, h * 0.77)
        hair.cubicTo(w * 0.88, h * 0.43, w * 0.82, h * 0.16, cx, h * 0.13)
        painter.setBrush(QColor("#ECEBFF"))
        painter.setPen(QPen(self._with_alpha(color, 105), max(1.0, w * 0.009)))
        painter.drawPath(hair)

        # Face.
        face = QPainterPath()
        face.moveTo(cx, h * 0.22)
        face.cubicTo(w * 0.35, h * 0.22, w * 0.31, h * 0.38, w * 0.34, h * 0.53)
        face.cubicTo(w * 0.38, h * 0.67, w * 0.44, h * 0.72, cx, h * 0.73)
        face.cubicTo(w * 0.56, h * 0.72, w * 0.63, h * 0.67, w * 0.66, h * 0.53)
        face.cubicTo(w * 0.69, h * 0.38, w * 0.65, h * 0.22, cx, h * 0.22)
        painter.setPen(QPen(QColor("#D8D5EC"), max(1.0, w * 0.007)))
        painter.setBrush(QColor("#F1E7E5"))
        painter.drawPath(face)

        # Side bangs.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#F8F7FF"))
        left_bang = QPainterPath()
        left_bang.moveTo(cx, h * 0.18)
        left_bang.cubicTo(w * 0.31, h * 0.20, w * 0.30, h * 0.36, w * 0.39, h * 0.48)
        left_bang.cubicTo(w * 0.42, h * 0.35, w * 0.45, h * 0.25, cx, h * 0.18)
        painter.drawPath(left_bang)
        right_bang = QPainterPath()
        right_bang.moveTo(cx, h * 0.18)
        right_bang.cubicTo(w * 0.69, h * 0.20, w * 0.70, h * 0.34, w * 0.61, h * 0.46)
        right_bang.cubicTo(w * 0.59, h * 0.34, w * 0.55, h * 0.24, cx, h * 0.18)
        painter.drawPath(right_bang)

        # Eyes with state glow.
        eye_y = h * 0.47
        eye_dx = w * 0.105
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._with_alpha(color, 65))
        painter.drawEllipse(QPointF(cx - eye_dx, eye_y), w * 0.055, h * 0.043)
        painter.drawEllipse(QPointF(cx + eye_dx, eye_y), w * 0.055, h * 0.043)
        painter.setBrush(color.lighter(150))
        painter.drawEllipse(QPointF(cx - eye_dx, eye_y), w * 0.026, h * 0.024)
        painter.drawEllipse(QPointF(cx + eye_dx, eye_y), w * 0.026, h * 0.024)

        # Minimal nose/mouth, softer while speaking.
        painter.setPen(QPen(QColor("#9B7180"), max(1.0, w * 0.008), Qt.SolidLine, Qt.RoundCap))
        mouth_y = h * 0.61
        mouth_half = w * (0.035 + (0.012 * pulse if self._state == "speaking" else 0.0))
        painter.drawLine(QPointF(cx - mouth_half, mouth_y), QPointF(cx + mouth_half, mouth_y))

        # Shoulders / luminous collar.
        shoulders = QPainterPath()
        shoulders.moveTo(w * 0.18, h * 0.98)
        shoulders.cubicTo(w * 0.24, h * 0.79, w * 0.38, h * 0.76, cx, h * 0.78)
        shoulders.cubicTo(w * 0.62, h * 0.76, w * 0.76, h * 0.79, w * 0.82, h * 0.98)
        painter.setBrush(QColor("#17152D"))
        painter.setPen(QPen(self._with_alpha(color, 110), max(1.0, w * 0.01)))
        painter.drawPath(shoulders)

        collar = QRectF(w * 0.38, h * 0.79, w * 0.24, h * 0.055)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._with_alpha(color, 120 + int(45 * pulse)))
        painter.drawRoundedRect(collar, collar.height() / 2, collar.height() / 2)


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
