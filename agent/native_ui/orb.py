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
        "working": "Выполняю",
        "waiting": "Жду",
        "error": "Ошибка",
        "stopped": "STOP ALL",
    }

    def __init__(self) -> None:
        super().__init__()
        self._state = "idle"
        self._partial = ""
        self._audio_level = 0
        self._pulse = 0
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
            Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(370, 72)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(68, 12, 14, 12)
        layout.setSpacing(3)
        self.label = QLabel("Люма · Готова")
        self.label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.label.setStyleSheet("color:#f8fafc; font-size:14px; font-weight:700;")
        layout.addWidget(self.label)
        self.context_label = QLabel("")
        self.context_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.context_label.setWordWrap(True)
        self.context_label.setStyleSheet("color:#b7c6db; font-size:12px;")
        self.context_label.hide()
        layout.addWidget(self.context_label)
        self.reply_label = QLabel("")
        self.reply_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.reply_label.setWordWrap(True)
        self.reply_label.setStyleSheet("color:#92a4be; font-size:11px;")
        self.reply_label.hide()
        layout.addWidget(self.reply_label)
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._tick)
        self._pulse_timer.start(90)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def set_state(self, state: str, auto_hide_ms: int = 0) -> None:
        self._state = state if state in self.LABELS else "idle"
        self.label.setText(f"Люма · {self.LABELS[self._state]}")
        self.update()
        self.show_near_bottom()
        if auto_hide_ms:
            self._hide_timer.start(auto_hide_ms)

    def set_activity(self, level: int = 0, partial: str = "") -> None:
        """Live mic activity without changing the state-machine."""
        self._audio_level = max(0, int(level or 0))
        self._partial = " ".join(str(partial or "").split())[:42]
        if self._state == "listening" and self._partial:
            self.label.setText(self._partial)
        else:
            self.label.setText(f"Люма · {self.LABELS[self._state]}")
        self.update()

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
            for row in rows:
                text = " ".join(str(row.get("reply") or row.get("heard") or "").split())[:54]
                if text:
                    snippets.append(text)
            self.reply_label.setText(" · ".join(snippets))
        self.context_label.setVisible(bool(self.context_label.text()))
        self.reply_label.setVisible(bool(self.reply_label.text()))
        target_height = 112 if self.context_label.text() or self.reply_label.text() else 72
        if self.height() != target_height:
            self.setFixedHeight(target_height)
            if self.isVisible():
                self.show_near_bottom()
        self.update()

    def _tick(self) -> None:
        if self._state in ("listening", "thinking", "speaking", "working", "waiting"):
            self._pulse = (self._pulse + 1) % 20
            self.update()

    def show_near_bottom(self) -> None:
        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2, geo.bottom() - self.height() - 24)
        self.show()
        self.raise_()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        tones = {
            "idle": QColor("#64748b"),
            "listening": QColor("#22b8cf"),
            "thinking": QColor("#9b7bff"),
            "speaking": QColor("#43c996"),
            "working": QColor("#f4b860"),
            "waiting": QColor("#94a3b8"),
            "error": QColor("#f87171"),
            "stopped": QColor("#ef4444"),
        }
        c = tones[self._state]
        center_y = self.height() // 2
        painter.setPen(QPen(QColor("#33435c"), 1))
        painter.setBrush(QColor("#111d30"))
        painter.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 20, 20)
        if self._state in ("listening", "thinking", "speaking", "working", "waiting"):
            pulse = abs(10 - self._pulse) / 10.0
            halo = QColor(c)
            halo.setAlpha(int(30 + 55 * pulse))
            painter.setPen(Qt.NoPen)
            painter.setBrush(halo)
            painter.drawEllipse(11, center_y - 24, 48, 48)
        painter.setPen(Qt.NoPen)
        painter.setBrush(c)
        painter.drawEllipse(20, center_y - 15, 30, 30)
        painter.setBrush(QColor("#111d30"))
        painter.drawEllipse(29, center_y - 6, 12, 12)

        if self._state == "listening" and self._audio_level:
            # Нормализованный индикатор громкости. Он декоративный и не влияет
            # на VAD threshold: решение «слышит/не слышит» остаётся в runtime.
            strength = min(1.0, self._audio_level / 8000.0)
            width = int((self.width() - 80) * strength)
            painter.setPen(Qt.NoPen)
            painter.setBrush(c)
            painter.drawRoundedRect(68, self.height() - 9, width, 3, 2, 2)
