"""State-driven neon voice orb for Luma.

One renderer is shared by the floating always-on-top indicator and the embedded
Control Center hero. Animation is deliberately lightweight: QPainter + one
timer, no blur effects or external assets.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .components import LumaPortrait


_STATE_COLORS = {
    "idle": QColor("#8B5CF6"),
    "listening": QColor("#43D7FF"),
    "thinking": QColor("#A78BFA"),
    "speaking": QColor("#58E6B1"),
    "working": QColor("#F5B84C"),
    "waiting": QColor("#9C99B8"),
    "error": QColor("#F05266"),
    "stopped": QColor("#F05266"),
}

_STATE_LABELS = {
    "idle": "Готова",
    "listening": "Слушаю",
    "thinking": "Думаю",
    "speaking": "Говорю",
    "working": "Выполняю",
    "waiting": "Жду",
    "error": "Ошибка",
    "stopped": "STOP ALL",
}


class LumaOrbCore(QWidget):
    """Reusable animated orb which visualizes Luma's runtime state."""

    def __init__(self, parent: QWidget | None = None, *, compact: bool = False) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._audio_level = 0
        self._phase = 0.0
        self._compact = bool(compact)
        self._reduced_motion = False
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        if compact:
            self.setFixedSize(62, 62)
        else:
            self.setMinimumSize(210, 210)

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(62, 62) if self._compact else QSize(260, 260)

    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        clean = str(state or "idle").casefold()
        if clean == "executing":
            clean = "working"
        self._state = clean if clean in _STATE_COLORS else "idle"
        self.update()

    def set_activity(self, audio_level: int = 0) -> None:
        self._audio_level = max(0, int(audio_level or 0))
        self.update()

    def set_reduced_motion(self, enabled: bool) -> None:
        self._reduced_motion = bool(enabled)
        self.update()

    def _tick(self) -> None:
        if self._reduced_motion:
            return
        speed = {
            "idle": 0.025,
            "listening": 0.055,
            "thinking": 0.075,
            "speaking": 0.085,
            "working": 0.05,
            "waiting": 0.022,
            "error": 0.018,
            "stopped": 0.0,
        }.get(self._state, 0.025)
        if speed:
            self._phase = (self._phase + speed) % math.tau
            self.update()

    @staticmethod
    def _alpha(color: QColor, alpha: int) -> QColor:
        out = QColor(color)
        out.setAlpha(max(0, min(255, int(alpha))))
        return out

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        side = float(min(self.width(), self.height()))
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        color = _STATE_COLORS[self._state]
        strength = min(1.0, self._audio_level / 7000.0)
        breath = 0.5 + 0.5 * math.sin(self._phase)
        if self._reduced_motion:
            breath = 0.45

        outer_r = side * (0.40 if self._compact else 0.42)
        if self._state == "listening":
            outer_r *= 1.0 + strength * 0.08
        elif self._state == "speaking":
            outer_r *= 1.0 + 0.035 * math.sin(self._phase * 2.2)

        halo = QRadialGradient(center, outer_r * 1.12)
        halo.setColorAt(0.0, self._alpha(color, 90 if not self._compact else 72))
        halo.setColorAt(0.38, self._alpha(color, 42 + int(25 * breath)))
        halo.setColorAt(0.72, self._alpha(color, 12))
        halo.setColorAt(1.0, self._alpha(color, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(center, outer_r * 1.12, outer_r * 1.12)

        ring_rect = QRectF(
            center.x() - outer_r * 0.86,
            center.y() - outer_r * 0.86,
            outer_r * 1.72,
            outer_r * 1.72,
        )
        painter.setBrush(Qt.NoBrush)
        ring_pen = QPen(self._alpha(color, 78 if self._state != "idle" else 45))
        ring_pen.setWidthF(max(1.0, side * 0.009))
        ring_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(ring_pen)

        angle = int((self._phase / math.tau) * 360.0 * 16)
        if self._state == "thinking":
            painter.drawArc(ring_rect, angle, 115 * 16)
            painter.drawArc(ring_rect, angle + 180 * 16, 70 * 16)
        elif self._state == "speaking":
            painter.drawArc(ring_rect, -25 * 16, int((220 + 35 * math.sin(self._phase * 2)) * 16))
        elif self._state == "listening":
            painter.drawArc(ring_rect, 18 * 16, int((285 * max(0.18, strength)) * 16))
        elif self._state in ("working", "waiting"):
            painter.drawArc(ring_rect, angle // 2, 155 * 16)
        else:
            painter.drawEllipse(ring_rect)

        if not self._compact and self._state in ("thinking", "working", "speaking"):
            painter.setPen(Qt.NoPen)
            for offset, radius_scale in ((0.0, 0.86), (2.15, 0.72), (4.25, 0.93)):
                a = self._phase + offset
                rr = outer_r * radius_scale
                pos = QPointF(center.x() + math.cos(a) * rr, center.y() + math.sin(a) * rr)
                painter.setBrush(self._alpha(color, 145))
                painter.drawEllipse(pos, side * 0.012, side * 0.012)

        core_r = outer_r * 0.58
        core = QRadialGradient(
            QPointF(center.x() - core_r * 0.22, center.y() - core_r * 0.28),
            core_r * 1.45,
        )
        core.setColorAt(0.0, QColor("#F4F1FF"))
        core.setColorAt(0.13, self._alpha(color.lighter(145), 245))
        core.setColorAt(0.52, self._alpha(color, 225))
        core.setColorAt(0.80, QColor("#241C4C"))
        core.setColorAt(1.0, QColor("#0A0B19"))
        painter.setPen(QPen(self._alpha(color.lighter(150), 120), max(1.0, side * 0.007)))
        painter.setBrush(QBrush(core))
        painter.drawEllipse(center, core_r, core_r)

        inner_r = core_r * (0.42 + (0.025 * breath if self._state != "stopped" else 0.0))
        inner = QRadialGradient(center, inner_r)
        inner.setColorAt(0.0, QColor(255, 255, 255, 235))
        inner.setColorAt(0.22, self._alpha(color.lighter(170), 220))
        inner.setColorAt(1.0, self._alpha(color, 5))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(inner))
        painter.drawEllipse(center, inner_r, inner_r)

        if not self._compact and self._state == "speaking":
            painter.setPen(QPen(self._alpha(color.lighter(130), 135), max(1.4, side * 0.008)))
            for index in range(18):
                a = math.tau * index / 18.0
                wave = 0.5 + 0.5 * math.sin(self._phase * 3.0 + index * 0.92)
                r1 = core_r * 1.13
                r2 = r1 + outer_r * (0.05 + 0.09 * wave)
                p1 = QPointF(center.x() + math.cos(a) * r1, center.y() + math.sin(a) * r1)
                p2 = QPointF(center.x() + math.cos(a) * r2, center.y() + math.sin(a) * r2)
                painter.drawLine(p1, p2)


class VoiceOrb(QWidget):
    """Always-on-top indicator with a larger persona view during conversation."""

    LABELS = _STATE_LABELS

    def __init__(self) -> None:
        super().__init__()
        self._state = "idle"
        self._partial = ""
        self._audio_level = 0
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
            Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(390)
        self.setFixedHeight(72)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(8, 5, 14, 5)
        row.setSpacing(10)
        self.core = LumaOrbCore(self, compact=True)
        row.addWidget(self.core, 0, Qt.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.label = QLabel("Люма · Готова")
        self.label.setStyleSheet("background:transparent;color:#F8F7FF;font-size:14px;font-weight:700;")
        self.context_label = QLabel("")
        self.context_label.setWordWrap(True)
        self.context_label.setStyleSheet("background:transparent;color:#B7B2CF;font-size:12px;")
        self.context_label.hide()
        self.reply_label = QLabel("")
        self.reply_label.setWordWrap(True)
        self.reply_label.setStyleSheet("background:transparent;color:#8F8BAE;font-size:11px;")
        self.reply_label.hide()
        text.addWidget(self.label)
        text.addWidget(self.context_label)
        text.addWidget(self.reply_label)
        row.addLayout(text, 1)
        root.addLayout(row)

        self.portrait = LumaPortrait(self)
        self.portrait.setFixedSize(265, 205)
        root.addWidget(self.portrait, 0, Qt.AlignCenter)
        self.portrait.hide()

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def set_state(self, state: str, auto_hide_ms: int = 0) -> None:
        clean = str(state or "idle").casefold()
        self._state = clean if clean in self.LABELS else "idle"
        self.core.set_state(self._state)
        self.portrait.set_state(self._state)
        self.portrait.setVisible(self._state in ("listening", "thinking", "speaking"))
        self._resize_for_state()
        self.label.setText(f"Люма · {self.LABELS[self._state]}")
        self.show_near_bottom()
        if auto_hide_ms:
            self._hide_timer.start(auto_hide_ms)

    def set_activity(self, level: int = 0, partial: str = "") -> None:
        self._audio_level = max(0, int(level or 0))
        self._partial = " ".join(str(partial or "").split())[:52]
        self.core.set_activity(self._audio_level)
        if self._state == "listening" and self._partial:
            self.label.setText(self._partial)
        else:
            self.label.setText(f"Люма · {self.LABELS[self._state]}")

    def set_live(self, *, heard: str = "", reply: str = "", skill: str = "",
                 detail: str = "", task_id: int = 0,
                 recent: list[dict] | None = None) -> None:
        heard = " ".join(str(heard or "").split())[:96]
        reply = " ".join(str(reply or "").split())[:150]
        skill = str(skill or "").strip()
        detail = " ".join(str(detail or "").split())[:100]
        if heard:
            self.context_label.setText(f"Вы: {heard}")
        elif skill:
            suffix = f" · задача #{int(task_id)}" if task_id else ""
            self.context_label.setText(f"⚡ {skill}{suffix}")
        else:
            self.context_label.setText(detail)

        if reply:
            self.reply_label.setText(f"Люма: {reply}")
        elif detail and (heard or skill):
            self.reply_label.setText(detail)
        else:
            rows = list(recent or [])[:2]
            snippets = []
            for item in rows:
                value = " ".join(str(item.get("reply") or item.get("heard") or "").split())[:54]
                if value:
                    snippets.append(value)
            self.reply_label.setText(" · ".join(snippets))

        self.context_label.setVisible(bool(self.context_label.text()))
        self.reply_label.setVisible(bool(self.reply_label.text()))
        self._resize_for_state()
        self.update()

    def _resize_for_state(self) -> None:
        target_height = (
            280 if self._state in ("listening", "thinking", "speaking")
            else 112 if self.context_label.text() or self.reply_label.text()
            else 72
        )
        if self.height() != target_height:
            self.setFixedHeight(target_height)
            if self.isVisible():
                self.show_near_bottom()

    def show_near_bottom(self) -> None:
        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2, geo.bottom() - self.height() - 24)
        self.show()
        self.raise_()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor("#454B83"), 1))
        painter.setBrush(QColor(12, 13, 30, 238))
        painter.drawRoundedRect(rect, 20, 20)
