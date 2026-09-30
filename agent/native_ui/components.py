"""Reusable visual components for Luma's native UI."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPointF, Property, QPropertyAnimation, QRectF, Qt, QTime, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget


class AmbientCanvas(QWidget):
    """Cheap atmospheric background without blur effects."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ambientCanvas")
        self._glow = 0.58
        self._background = QPixmap(str(Path(__file__).resolve().parent / "assets" / "luma-night-bg.png"))
        self._animation = QPropertyAnimation(self, b"glow", self)
        self._animation.setDuration(5200)
        self._animation.setStartValue(0.48)
        self._animation.setEndValue(0.68)
        self._animation.setEasingCurve(QEasingCurve.InOutSine)
        self._animation.setLoopCount(-1)
        self._animation.start()

    def set_reduced_motion(self, enabled: bool) -> None:
        if enabled:
            self._animation.stop()
            self._glow = 0.58
            self.update()
        elif self._animation.state() != QPropertyAnimation.Running:
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

        if not self._background.isNull():
            painter.setOpacity(0.40)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawPixmap(self.rect(), self._background)
            painter.setOpacity(1.0)

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

        painter.setPen(Qt.NoPen)
        for index in range(24):
            x = int(((index * 179 + 43) % 997) / 997 * w)
            y = int(((index * 311 + 71) % 991) / 991 * h)
            painter.setBrush(QColor(160, 132, 255, int((35 + index % 5 * 13) * self._glow)))
            painter.drawEllipse(QPointF(x, y), 1.1, 1.1)


class LumaPortrait(QWidget):
    """Luma's portrait with a state-colored, softly animated halo."""

    STATE_COLORS = {
        "idle": QColor("#8B5CF6"),
        "listening": QColor("#43D7FF"),
        "thinking": QColor("#A78BFA"),
        "speaking": QColor("#F067E8"),
        "working": QColor("#F5B84C"),
        "waiting": QColor("#9996B7"),
        "error": QColor("#F05266"),
        "stopped": QColor("#F05266"),
    }

    def __init__(self, parent: QWidget | None = None, *, compact: bool = False) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._audio_level = 0
        self._phase = 0.0
        self._compact = bool(compact)
        self._reduced_motion = False
        self._portrait = QPixmap(str(Path(__file__).resolve().parent / "assets" / "luma-portrait.png"))
        self._energy = QPixmap(str(Path(__file__).resolve().parent / "assets" / "luma-energy-ring.png"))
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

    def set_activity(self, level: int = 0) -> None:
        self._audio_level = max(0, int(level or 0))
        self.update()

    def set_reduced_motion(self, enabled: bool) -> None:
        self._reduced_motion = bool(enabled)
        if enabled:
            self._timer.stop()
        elif not self._timer.isActive():
            self._timer.start()

    def _tick(self) -> None:
        if self.isVisible() and self._state not in ("stopped",):
            speed = {"listening": 0.075, "thinking": 0.095, "speaking": 0.11,
                     "working": 0.065, "idle": 0.035}.get(self._state, 0.035)
            self._phase = (self._phase + speed) % 6.283185307
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

        orbit = QRectF(0, h * 0.03, w, h * 0.94)
        if not self._compact and not self._energy.isNull():
            painter.save()
            painter.translate(cx, h * 0.5)
            painter.rotate(4.0 * math.sin(self._phase * 0.45))
            painter.translate(-cx, -h * 0.5)
            painter.setOpacity(0.70 + 0.10 * pulse)
            painter.drawPixmap(orbit, self._energy, QRectF(self._energy.rect()))
            painter.restore()

        if not self._compact:
            painter.setPen(Qt.NoPen)
            for index in range(13):
                angle = self._phase * (0.45 + index % 3 * 0.13) + index * 2.39996
                x = cx + math.cos(angle) * w * (0.36 + index % 4 * 0.025)
                y = h * 0.51 + math.sin(angle) * h * (0.35 + index % 3 * 0.018)
                radius = 1.0 + (index % 3) * 0.65 + pulse * 0.4
                painter.setBrush(self._with_alpha(color.lighter(150), 95 + index % 4 * 30))
                painter.drawEllipse(QPointF(x, y), radius, radius)

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

        if not self._compact and self._state in ("listening", "speaking"):
            strength = min(1.0, self._audio_level / 7000.0)
            painter.setPen(QPen(self._with_alpha(color.lighter(140), 145), max(1.0, w * 0.006)))
            middle = h * 0.49
            for index in range(17):
                wave = abs(math.sin(self._phase * 2.2 + index * 0.83))
                amplitude = h * (0.025 + (0.05 + 0.09 * strength) * wave)
                x = w * (0.018 + index * 0.013)
                painter.drawLine(QPointF(x, middle - amplitude), QPointF(x, middle + amplitude))
                painter.drawLine(QPointF(w - x, middle - amplitude), QPointF(w - x, middle + amplitude))

        if not self._compact and not self._energy.isNull():
            painter.save()
            painter.setClipRect(QRectF(0, h * 0.62, w, h * 0.38))
            painter.translate(cx, h * 0.5)
            painter.rotate(-3.0 * math.sin(self._phase * 0.55))
            painter.translate(-cx, -h * 0.5)
            painter.setOpacity(0.45 + 0.10 * pulse)
            painter.drawPixmap(orbit, self._energy, QRectF(self._energy.rect()))
            painter.restore()

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(self._with_alpha(color, 120 + int(60 * pulse)), max(1.0, w * 0.012)))
        painter.drawEllipse(QRectF(w * 0.06, h * 0.12, w * 0.88, h * 0.76))


class LumaClock(QWidget):
    """Orbital clock for the task planner."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._energy = QPixmap(str(Path(__file__).resolve().parent / "assets" / "luma-energy-ring.png"))
        self.setFixedSize(210, 210)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(30000)

    def paintEvent(self, _event) -> None:  # noqa: N802
        import math

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if not self._energy.isNull():
            painter.setOpacity(0.82)
            painter.drawPixmap(QRectF(0, 0, 210, 210), self._energy, QRectF(self._energy.rect()))
            painter.setOpacity(1.0)

        center = QPointF(105, 96)
        painter.setBrush(QColor(8, 13, 35, 205))
        painter.setPen(QPen(QColor("#82B9FF"), 2))
        painter.drawEllipse(center, 57, 57)
        for mark in range(12):
            angle = math.tau * mark / 12 - math.pi / 2
            start = QPointF(center.x() + math.cos(angle) * 47, center.y() + math.sin(angle) * 47)
            end = QPointF(center.x() + math.cos(angle) * 53, center.y() + math.sin(angle) * 53)
            painter.setPen(QPen(QColor("#CAB4FF"), 2))
            painter.drawLine(start, end)

        now = QTime.currentTime()
        hour_angle = math.tau * ((now.hour() % 12) + now.minute() / 60) / 12 - math.pi / 2
        minute_angle = math.tau * now.minute() / 60 - math.pi / 2
        painter.setPen(QPen(QColor("#E7D9FF"), 4, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(center, QPointF(center.x() + math.cos(hour_angle) * 29,
                                         center.y() + math.sin(hour_angle) * 29))
        painter.setPen(QPen(QColor("#63DFFF"), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(center, QPointF(center.x() + math.cos(minute_angle) * 43,
                                         center.y() + math.sin(minute_angle) * 43))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#F9F6FF"))
        painter.drawEllipse(center, 4, 4)


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

        mark = QLabel()
        mark.setObjectName("brandMark")
        mark.setFixedSize(30, 30)
        ring = QPixmap(str(Path(__file__).resolve().parent / "assets" / "luma-energy-ring.png"))
        if not ring.isNull():
            mark.setPixmap(ring.scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        mark.setAlignment(Qt.AlignCenter)

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

        layout.addWidget(mark, 0, Qt.AlignVCenter)
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
