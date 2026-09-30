"""Системный tray Люмы."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


def status_icon(state: str = "idle") -> QIcon:
    tones = {
        "idle": "#A779FF",
        "listening": "#4DDCFF",
        "thinking": "#9B80FF",
        "speaking": "#F067E8",
        "error": "#ef4444",
        "stopped": "#991b1b",
        "offline": "#475569",
    }
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    color = QColor(tones.get(state, tones["idle"]))
    halo = QRadialGradient(QPointF(32, 32), 31)
    halo.setColorAt(0, QColor(color.red(), color.green(), color.blue(), 165))
    halo.setColorAt(1, QColor(color.red(), color.green(), color.blue(), 0))
    painter.setPen(Qt.NoPen)
    painter.setBrush(halo)
    painter.drawEllipse(1, 1, 62, 62)
    painter.setPen(QPen(color.lighter(160), 3))
    painter.setBrush(QColor("#0A0D29"))
    painter.drawEllipse(10, 10, 44, 44)
    painter.setPen(QPen(color, 2))
    painter.drawArc(15, 15, 34, 34, 55 * 16, 245 * 16)
    core = QRadialGradient(QPointF(28, 25), 22)
    core.setColorAt(0, QColor("#F9F6FF"))
    core.setColorAt(0.18, color.lighter(170))
    core.setColorAt(0.58, color)
    core.setColorAt(1, QColor("#151047"))
    painter.setPen(Qt.NoPen)
    painter.setBrush(core)
    painter.drawEllipse(20, 20, 24, 24)
    ring = QPixmap(str(Path(__file__).resolve().parent / "assets" / "luma-energy-ring.png"))
    if not ring.isNull():
        painter.setOpacity(0.9)
        painter.drawPixmap(4, 4, 56, 56, ring)
    painter.end()
    return QIcon(pix)


class LumaTray(QSystemTrayIcon):
    def __init__(
        self,
        open_quick: Callable[[], None],
        open_control: Callable[[], None],
        toggle_mic: Callable[[], None],
        toggle_stop_all: Callable[[], None],
        restart_ui: Callable[[], None],
        quit_app: Callable[[], None],
    ) -> None:
        super().__init__(status_icon("offline"))
        self.setToolTip("Люма")
        menu = QMenu()

        self.status_action = QAction("● Люма подключается…", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        self.model_action = QAction("Модель: …", menu)
        self.model_action.setEnabled(False)
        menu.addAction(self.model_action)
        self.mic_action = QAction("🎤 Включить микрофон", menu)
        self.mic_action.triggered.connect(toggle_mic)
        menu.addAction(self.mic_action)
        self.stop_action = QAction("⛔ STOP ALL", menu)
        self.stop_action.triggered.connect(toggle_stop_all)
        menu.addAction(self.stop_action)

        menu.addSeparator()
        quick = QAction("⌨ Быстрая команда", menu)
        quick.triggered.connect(open_quick)
        menu.addAction(quick)
        center = QAction("🧠 Открыть Люму", menu)
        center.triggered.connect(open_control)
        menu.addAction(center)

        menu.addSeparator()
        restart = QAction("↻ Перезапустить интерфейс", menu)
        restart.triggered.connect(restart_ui)
        menu.addAction(restart)
        quit_action = QAction("⏻ Выход", menu)
        quit_action.triggered.connect(quit_app)
        menu.addAction(quit_action)
        self.setContextMenu(menu)
        self.activated.connect(
            lambda reason: open_quick() if reason == QSystemTrayIcon.Trigger else None
        )

    def apply_status(self, connected: bool, mic_enabled: bool, model_ok: bool,
                     assistant_state: str = "idle", error: str = "",
                     safety_stopped: bool = False) -> None:
        state = "stopped" if safety_stopped and connected else (assistant_state if connected else "offline")
        if error:
            state = "error"
        self.setIcon(status_icon(state))
        self.status_action.setText(
            "● Люма работает" if connected else "● Люма недоступна"
        )
        self.model_action.setText("Модель: готова" if model_ok else "Модель: недоступна")
        self.mic_action.setText("🎤 Выключить wake word" if mic_enabled else "🎤 Включить wake word")
        self.stop_action.setText("▶ Снять STOP ALL" if safety_stopped else "⛔ STOP ALL")
        tooltip = "Люма"
        if error:
            tooltip += f" · {error}"
        self.setToolTip(tooltip[:180])


# Backward-compatible import name for older launchers.
NozzaTray = LumaTray
