"""Quick Panel в стиле Spotlight/PowerToys Run."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from . import theme
from .components import LumaPortrait


class QuickPanel(QWidget):
    submitted = Signal(str)
    mic_toggle = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(690)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet(theme.stylesheet() + """
        QFrame#card {
            background:#0C1025;
            border:2px solid #8466F7;
            border-radius:22px;
        }
        QLineEdit {
            background:#15172D;
            border:1px solid #3B3F69;
            border-radius:13px;
            color:#F8F7FF;
            font-size:18px;
            padding:14px 16px;
        }
        QLineEdit:focus {
            border:1px solid #8B6EF0;
            background:#181A33;
        }
        QLabel { background:transparent; color:#9996B7; }
        QLabel#quickBrand {
            color:#FFFFFF;
            font-size:17px;
            font-weight:800;
            letter-spacing:1px;
        }
        QPushButton {
            background:#181B35;
            color:#D8D4E8;
            border:1px solid #35395F;
            border-radius:10px;
            padding:8px 11px;
        }
        QPushButton:hover {
            background:#242044;
            border-color:#6654B8;
        }
        QPushButton#quickMic {
            background:#365CDD;
            border:2px solid #8C9FFF;
            border-radius:26px;
            color:white;
            font-size:23px;
        }
        """)
        box = QVBoxLayout(card)
        box.setContentsMargins(20, 16, 20, 18)
        box.setSpacing(8)
        header = QHBoxLayout()
        brand = QLabel("✦  LUMA")
        brand.setObjectName("quickBrand")
        header.addWidget(brand)
        header.addStretch(1)
        header.addWidget(QLabel("БЫСТРАЯ ПАНЕЛЬ  ·  Esc — закрыть"))
        box.addLayout(header)
        hero = QHBoxLayout()
        copy = QVBoxLayout()
        greeting = QLabel("Привет!\nЯ LUMA")
        greeting.setStyleSheet("color:#FFFFFF;font-size:27px;font-weight:800;background:transparent;")
        copy.addWidget(greeting)
        subtitle = QLabel("Слушаю тебя…\nВсегда рядом ♡")
        subtitle.setStyleSheet("color:#D2C6FF;font-size:14px;background:transparent;")
        copy.addWidget(subtitle)
        copy.addStretch(1)
        hero.addLayout(copy, 2)
        portrait = LumaPortrait()
        portrait.setFixedSize(240, 168)
        hero.addWidget(portrait, 3, Qt.AlignRight)
        box.addLayout(hero)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Спроси Люму или скажи, что сделать…")
        self.input.returnPressed.connect(self._submit)
        row.addWidget(self.input, 1)
        box.addLayout(row)
        self.answer = QLabel("")
        self.answer.setWordWrap(True)
        self.answer.setStyleSheet("color:#E7E3F3; font-size:14px; background:transparent;")
        self.answer.hide()
        box.addWidget(self.answer)
        mic_row = QHBoxLayout()
        mic_row.addStretch(1)
        mic = QPushButton("🎙")
        mic.setObjectName("quickMic")
        mic.setFixedSize(46, 46)
        mic.setToolTip("Включить или выключить микрофон")
        mic.clicked.connect(self.mic_toggle)
        mic_row.addWidget(mic)
        mic_row.addStretch(1)
        box.addLayout(mic_row)
        actions = QHBoxLayout()
        for icon, title, prompt in (
            ("◫", "Открыть\nприложение", "Открой "),
            ("▣", "Найти\nфайл", "Найди файл "),
            ("♫", "Включить\nмузыку", "Включи музыку"),
            ("✎", "Создать\nзаметку", "Создай заметку "),
        ):
            button = QPushButton(f"{icon}\n{title}")
            button.setMinimumHeight(64)
            button.clicked.connect(lambda _=False, value=prompt: self._pick(value))
            actions.addWidget(button, 1)
        box.addLayout(actions)
        root.addWidget(card)

    def _pick(self, text: str) -> None:
        self.input.setText(text)
        if text.endswith(" "):
            self.input.setFocus()
        else:
            self._submit()

    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.submitted.emit(text)

    def show_centered(self) -> None:
        screen = self.screen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2, geo.top() + int(geo.height() * .22))
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()
        self.input.selectAll()

    def set_busy(self, busy: bool) -> None:
        self.input.setEnabled(not busy)
        if busy:
            self.answer.setText("Думаю…")
            self.answer.show()

    def show_answer(self, text: str) -> None:
        self.answer.setText(text)
        self.answer.setVisible(bool(text))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
