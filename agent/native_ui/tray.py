"""Системный tray Люмы."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


def status_icon(state: str = "idle") -> QIcon:
    tones = {
        "idle": "#64748b",
        "listening": "#06b6d4",
        "thinking": "#8b5cf6",
        "speaking": "#22c55e",
        "error": "#ef4444",
        "stopped": "#991b1b",
        "offline": "#475569",
    }
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(tones.get(state, tones["idle"])))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(8, 8, 48, 48)
    painter.setBrush(QColor("#ffffff"))
    painter.drawEllipse(26, 20, 12, 12)
    painter.drawRoundedRect(25, 33, 14, 15, 7, 7)
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
