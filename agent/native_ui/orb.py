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
        self.setFixedSize(390, 150)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        self.label = QLabel("Люма · Готова")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("color: white; font-size: 14px; font-weight: 700;")
        layout.addWidget(self.label)
        self.context_label = QLabel("")
        self.context_label.setAlignment(Qt.AlignCenter)
        self.context_label.setWordWrap(True)
        self.context_label.setStyleSheet("color: rgba(255,255,255,210); font-size: 12px;")
        layout.addWidget(self.context_label)
        self.reply_label = QLabel("")
        self.reply_label.setAlignment(Qt.AlignCenter)
        self.reply_label.setWordWrap(True)
        self.reply_label.setStyleSheet("color: rgba(255,255,255,170); font-size: 11px;")
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
        self.update()

    def _tick(self) -> None:
        if self._state in ("listening", "thinking", "speaking"):
            self._pulse = (self._pulse + 1) % 20
            self.update()

    def show_near_bottom(self) -> None:
        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2, geo.bottom() - self.height() - 42)
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
            "stopped": QColor("#991b1b"),
        }
        c = tones[self._state]
        if self._state in ("listening", "thinking", "speaking"):
            pulse = abs(10 - self._pulse) / 10.0
            halo = QColor(c)
            halo.setAlpha(int(28 + 42 * pulse))
            painter.setPen(QPen(halo, 3))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(self.rect().adjusted(3, 3, -3, -3), 20, 20)
        painter.setPen(QPen(QColor(255, 255, 255, 36), 1))
        painter.setBrush(c)
        painter.drawRoundedRect(self.rect().adjusted(6, 6, -6, -6), 18, 18)

        if self._state == "listening" and self._audio_level:
            # Нормализованный индикатор громкости. Он декоративный и не влияет
            # на VAD threshold: решение «слышит/не слышит» остаётся в runtime.
            strength = min(1.0, self._audio_level / 8000.0)
            width = int((self.width() - 36) * strength)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 80))
            painter.drawRoundedRect(18, self.height() - 11, width, 4, 2, 2)
