"""Full Control Center Люмы: разговор, дела, память, обучение и настройки."""
from __future__ import annotations

import html
import json
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from .components import AmbientCanvas, BrandCard, GlassCard, LumaPortrait, StatusHeader
from .orb import LumaOrbCore
from . import theme


def _pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


class TextPage(QWidget):
    refresh_requested = Signal()

    LABELS = {
        "memories": "Сохранено", "learned": "Освоено", "unknown": "Требует уточнения",
        "aliases": "Ваши названия", "insights": "Наблюдения", "feedback": "Обратная связь",
        "skills": "Возможности", "entries": "Последние действия", "reminders": "Напоминания",
        "todos": "Дела", "goals": "Цели", "habits": "Привычки", "expenses": "Расходы",
    }

    def __init__(self, title: str, subtitle: str = "") -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        text = QVBoxLayout()
        label = QLabel(title)
        label.setObjectName("pageTitle")
        text.addWidget(label)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("muted")
            text.addWidget(sub)
        header.addLayout(text, 1)
        self.raw_button = QPushButton("Показать данные")
        self.raw_button.clicked.connect(self._toggle_raw)
        header.addWidget(self.raw_button)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.refresh_requested)
        header.addWidget(refresh)
        layout.addLayout(header)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        layout.addWidget(self.browser, 1)
        self._payload: Any = None
        self._show_raw = False

    def set_payload(self, value: Any) -> None:
        self._payload = value
        self._render()

    def _toggle_raw(self) -> None:
        self._show_raw = not self._show_raw
        self.raw_button.setText("Красивый вид" if self._show_raw else "Показать данные")
        self._render()

    def _render(self) -> None:
        if self._show_raw:
            self.browser.setPlainText(_pretty(self._payload))
            return
        payload = self._payload if isinstance(self._payload, dict) else {}
        sections = []
        for key, value in payload.items():
            if key in {"ok", "count", "ready", "unavailable", "stats", "layers"} or not value:
                continue
            title = html.escape(self.LABELS.get(key, key.replace("_", " ").capitalize()))
            if isinstance(value, list):
                cards = [self._card(item) for item in value]
            elif isinstance(value, dict):
                cards = [self._card({"title": name, "value": item}) for name, item in value.items()]
            else:
                cards = [self._card(value)]
            if cards:
                sections.append(f"<h2>{title}</h2>" + "".join(cards))
        self.browser.setHtml(
            "<div style='margin:18px 24px; color:#e5e7eb;'>" +
            ("".join(sections) if sections else
             "<h2>Пока здесь пусто</h2><p style='color:#94a3b8;'>Данные появятся после первого действия.</p>") +
            "</div>"
        )

    @staticmethod
    def _card(item: Any) -> str:
        if not isinstance(item, dict):
            return f"<p style='margin:10px 0;'>{html.escape(str(item))}</p>"
        heading = next((str(item[key]) for key in ("title", "name", "text", "phrase", "message")
                        if item.get(key)), "Запись")
        details = []
        for key in ("meaning_text", "description", "value", "status", "state", "reason", "at"):
            value = item.get(key)
            if value not in (None, "", [], {}) and str(value) != heading:
                details.append(html.escape(str(value)))
        badge = "<span style='color:#fbbf24;'>Недоступно</span>" if item.get("available") is False else ""
        body = "<br>".join(details[:3])
        return ("<table width='100%' cellspacing='0' cellpadding='12' style='margin:9px 0; border:1px solid #334155;'>"
                "<tr><td bgcolor='#172337'><b>" + html.escape(heading) + "</b> " + badge +
                ("<br><span style='color:#aabbd2;'>" + body + "</span>" if body else "") +
                "</td></tr></table>")


class SkillsPage(QWidget):
    """Searchable capability map: what Luma can really use on this computer."""

    refresh_requested = Signal()

    GROUP_LABELS = {
        "system": "Компьютер", "window": "Окна", "desktop": "Рабочий стол",
        "screen": "Зрение", "browser": "Браузер", "app": "Программы",
        "files": "Файлы", "panel": "PrintFlow", "voice": "Голос",
        "memory": "Память", "reminder": "Напоминания", "scheduler": "Таймеры",
        "list": "Списки", "goal": "Цели", "habit": "Привычки",
        "expense": "Расходы", "diary": "Дневник", "clipboard": "Буфер",
        "assistant": "Сценарии", "agent": "Ядро", "day": "День",
        "knowledge": "Знания", "learn": "Обучение", "tg": "Telegram",
        "avito": "Авито", "printer": "Печать",
    }

    def __init__(self) -> None:
        super().__init__()
        self._payload: dict[str, Any] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(12)

        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Навыки")
        title.setObjectName("pageTitle")
        title_box.addWidget(title)
        subtitle = QLabel("Карта реальных функций Люмы на этом компьютере.")
        subtitle.setObjectName("muted")
        title_box.addWidget(subtitle)
        head.addLayout(title_box, 1)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.refresh_requested)
        head.addWidget(refresh)
        root.addLayout(head)

        metrics = QHBoxLayout()
        self.ready_metric = QLabel("0")
        self.ready_metric.setObjectName("skillMetricReady")
        self.total_metric = QLabel("0")
        self.total_metric.setObjectName("skillMetric")
        self.off_metric = QLabel("0")
        self.off_metric.setObjectName("skillMetric")
        for title_text, widget in (
            ("ГОТОВО", self.ready_metric),
            ("ВСЕГО", self.total_metric),
            ("НЕДОСТУПНО", self.off_metric),
        ):
            card = GlassCard("cyan" if title_text == "ГОТОВО" else "")
            box = QVBoxLayout(card)
            box.setContentsMargins(12, 9, 12, 9)
            kicker = QLabel(title_text)
            kicker.setObjectName("metricLabel")
            box.addWidget(kicker)
            box.addWidget(widget)
            metrics.addWidget(card)
        root.addLayout(metrics)

        filter_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти функцию: экран, Steam, файл, принтер, память…")
        self.search.textChanged.connect(self._render)
        filter_row.addWidget(self.search, 1)
        self.state_filter = QComboBox()
        self.state_filter.addItem("Все состояния", "all")
        self.state_filter.addItem("Только доступные", "ready")
        self.state_filter.addItem("Что не работает", "off")
        self.state_filter.currentIndexChanged.connect(self._render)
        filter_row.addWidget(self.state_filter)
        root.addLayout(filter_row)

        brain_note = QLabel(
            "LUMA TOOL ROUTER · мозг получает релевантные функции под каждую фразу, "
            "а перед выполнением реестр повторно проверяет доступность и параметры."
        )
        brain_note.setObjectName("voiceChain")
        brain_note.setWordWrap(True)
        root.addWidget(brain_note)

        self.browser = QTextBrowser()
        self.browser.setObjectName("skillsBrowser")
        root.addWidget(self.browser, 1)

    def set_payload(self, payload: dict[str, Any]) -> None:
        self._payload = dict(payload or {})
        rows = [row for row in list(self._payload.get("skills") or []) if isinstance(row, dict)]
        ready = sum(1 for row in rows if row.get("available"))
        self.ready_metric.setText(str(ready))
        self.total_metric.setText(str(len(rows)))
        self.off_metric.setText(str(len(rows) - ready))
        self._render()

    def _render(self) -> None:
        rows = [row for row in list(self._payload.get("skills") or []) if isinstance(row, dict)]
        needle = " ".join(self.search.text().casefold().split())
        mode = str(self.state_filter.currentData() or "all")
        filtered = []
        for row in rows:
            available = bool(row.get("available"))
            if mode == "ready" and not available:
                continue
            if mode == "off" and available:
                continue
            hay = " ".join(
                str(row.get(key) or "") for key in ("name", "title", "description", "reason", "provider")
            ).casefold()
            if needle and needle not in hay:
                continue
            filtered.append(row)

        groups: dict[str, list[dict[str, Any]]] = {}
        for row in filtered:
            group = str(row.get("name") or "other").split(".", 1)[0]
            groups.setdefault(group, []).append(row)

        sections = []
        for group in sorted(groups, key=lambda value: self.GROUP_LABELS.get(value, value).casefold()):
            items = []
            for row in sorted(groups[group], key=lambda value: str(value.get("title") or value.get("name") or "")):
                available = bool(row.get("available"))
                state = "READY" if available else "OFF"
                state_color = "#58E6B1" if available else "#F5B84C"
                border = "#315F59" if available else "#664A2A"
                name = html.escape(str(row.get("name") or ""))
                title = html.escape(str(row.get("title") or name))
                desc = html.escape(str(row.get("description") or ""))
                risk = html.escape(str(row.get("risk") or "read"))
                provider = html.escape(str(row.get("provider") or "local"))
                params = dict(row.get("params") or {})
                params_text = ", ".join(f"{key}: {value}" for key, value in params.items()) or "без параметров"
                reason = html.escape(str(row.get("reason") or ""))
                confirm = " · подтверждение" if row.get("confirm") else ""
                learned = " · обучено" if row.get("learned") else ""
                meta = html.escape(f"{risk}{confirm}{learned} · {provider} · {params_text}")
                unavailable = (
                    f"<br><span style='color:#F5B84C;'>Причина: {reason}</span>" if reason and not available else ""
                )
                items.append(
                    "<table width='100%' cellspacing='0' cellpadding='0' "
                    f"style='margin:7px 0;border:1px solid {border};background:#101225;'>"
                    "<tr><td style='padding:11px 13px;'>"
                    f"<span style='color:{state_color};font-size:10px;font-weight:700;'>{state}</span>"
                    f"&nbsp;&nbsp;<b style='color:#F7F5FF;'>{title}</b>"
                    f"<span style='color:#777493;'> · {name}</span><br>"
                    f"<span style='color:#B4B0C9;'>{desc}</span><br>"
                    f"<span style='color:#777493;font-size:11px;'>{meta}</span>{unavailable}"
                    "</td></tr></table>"
                )
            group_title = html.escape(self.GROUP_LABELS.get(group, group.capitalize()))
            sections.append(
                f"<div style='margin:16px 0 6px;color:#A78BFA;font-size:16px;font-weight:700;'>"
                f"{group_title} · {len(items)}</div>" + "".join(items)
            )
        body = "".join(sections)
        if not body:
            body = (
                "<div style='margin:28px;color:#9996B7;'>"
                "По этому фильтру функций нет. Попробуйте другое слово.</div>"
            )
        self.browser.setHtml("<div style='margin:8px 14px;'>" + body + "</div>")


class ChatPage(QWidget):
    submitted = Signal(str)
    clear_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._turns: list[dict[str, str]] = []
        self._has_history = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(12)

        head = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Разговор")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Живой диалог с Люма · текст, голос и действия в одном потоке.")
        subtitle.setObjectName("muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        head.addLayout(title_box, 1)
        clear = QPushButton("Очистить диалог")
        clear.clicked.connect(self.clear_requested)
        head.addWidget(clear)
        layout.addLayout(head)

        live_card = GlassCard("violet")
        live_row = QHBoxLayout(live_card)
        live_row.setContentsMargins(12, 9, 14, 9)
        live_row.setSpacing(10)
        self.live_portrait = LumaPortrait(compact=True)
        self.live_portrait.setFixedSize(42, 50)
        live_row.addWidget(self.live_portrait, 0, Qt.AlignVCenter)
        live_text = QVBoxLayout()
        live_text.setSpacing(1)
        live_label = QLabel("LUMA // LIVE")
        live_label.setObjectName("heroKicker")
        live_text.addWidget(live_label)
        self.live = QLabel("Люма готова")
        self.live.setObjectName("chatLive")
        self.live.setWordWrap(True)
        live_text.addWidget(self.live)
        live_row.addLayout(live_text, 1)
        self.live_orb = LumaOrbCore(compact=True)
        self.live_orb.setFixedSize(46, 46)
        live_row.addWidget(self.live_orb, 0, Qt.AlignVCenter)
        layout.addWidget(live_card)

        self.trace_card = GlassCard()
        trace_box = QVBoxLayout(self.trace_card)
        trace_box.setContentsMargins(12, 9, 12, 10)
        trace_box.setSpacing(5)
        trace_head = QHBoxLayout()
        trace_title = QLabel("ХОД ВЫПОЛНЕНИЯ")
        trace_title.setObjectName("heroKicker")
        trace_head.addWidget(trace_title)
        trace_head.addStretch(1)
        self.trace_badge = QLabel("LOCAL")
        self.trace_badge.setObjectName("tracePill")
        trace_head.addWidget(self.trace_badge)
        trace_box.addLayout(trace_head)
        self.trace_text = QLabel("")
        self.trace_text.setObjectName("actionTrace")
        self.trace_text.setWordWrap(True)
        trace_box.addWidget(self.trace_text)
        self.trace_card.hide()
        layout.addWidget(self.trace_card)

        self.camera_card = GlassCard("cyan")
        camera_box = QVBoxLayout(self.camera_card)
        camera_box.setContentsMargins(10, 9, 10, 10)
        camera_head = QHBoxLayout()
        self.camera_title = QLabel("КАМЕРА ПРИНТЕРА")
        self.camera_title.setObjectName("heroKicker")
        camera_head.addWidget(self.camera_title)
        camera_head.addStretch(1)
        close_camera = QPushButton("Скрыть")
        close_camera.clicked.connect(self.camera_card.hide)
        camera_head.addWidget(close_camera)
        camera_box.addLayout(camera_head)
        self.camera_frame = QLabel()
        self.camera_frame.setObjectName("cameraFrame")
        self.camera_frame.setAlignment(Qt.AlignCenter)
        self.camera_frame.setMinimumHeight(180)
        self.camera_frame.setMaximumHeight(360)
        camera_box.addWidget(self.camera_frame, 1)
        self.camera_card.hide()
        layout.addWidget(self.camera_card)

        self.content = QStackedWidget()

        welcome = GlassCard()
        intro = QHBoxLayout(welcome)
        intro.setContentsMargins(28, 22, 28, 22)
        intro.setSpacing(26)

        portrait_column = QVBoxLayout()
        portrait_column.addStretch(1)
        self.welcome_portrait = LumaPortrait()
        self.welcome_portrait.setFixedSize(170, 205)
        portrait_column.addWidget(self.welcome_portrait, 0, Qt.AlignCenter)
        persona = QLabel("LUMA · LOCAL PERSONA")
        persona.setObjectName("heroKicker")
        persona.setAlignment(Qt.AlignCenter)
        portrait_column.addWidget(persona)
        portrait_column.addStretch(1)
        intro.addLayout(portrait_column, 4)

        copy = QVBoxLayout()
        copy.addStretch(1)
        greeting = QLabel("Привет. Я Люма.")
        greeting.setObjectName("welcomeTitle")
        copy.addWidget(greeting)
        description = QLabel(
            "Можешь писать обычным языком: спросить, попросить выполнить действие "
            "или продолжить задачу. Голос и текст используют один контекст."
        )
        description.setObjectName("welcomeText")
        description.setWordWrap(True)
        copy.addWidget(description)
        copy.addSpacing(16)

        suggestions = (
            "Что у меня сегодня?",
            "Покажи активные задачи",
            "Открой загрузки",
        )
        for prompt in suggestions:
            button = QPushButton(prompt)
            button.setObjectName("suggestion")
            button.clicked.connect(lambda _=False, value=prompt: self._pick_prompt(value))
            copy.addWidget(button)
        copy.addStretch(1)
        intro.addLayout(copy, 7)

        self.content.addWidget(welcome)

        feed_card = GlassCard()
        feed_layout = QVBoxLayout(feed_card)
        feed_layout.setContentsMargins(4, 4, 4, 4)
        self.feed = QTextBrowser()
        self.feed.setObjectName("chatFeed")
        self.feed.setOpenExternalLinks(True)
        feed_layout.addWidget(self.feed)
        self.content.addWidget(feed_card)

        layout.addWidget(self.content, 1)

        composer = GlassCard("cyan")
        composer_row = QHBoxLayout(composer)
        composer_row.setContentsMargins(10, 9, 10, 9)
        composer_row.setSpacing(8)
        self.input = QLineEdit()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText("Спроси Люму или скажи, что сделать…")
        self.input.returnPressed.connect(self._submit)
        composer_row.addWidget(self.input, 1)
        send = QPushButton("Отправить")
        send.setObjectName("primary")
        send.clicked.connect(self._submit)
        composer_row.addWidget(send)
        layout.addWidget(composer)

    def _pick_prompt(self, text: str) -> None:
        self.input.setText(text)
        self.input.setFocus()

    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.submitted.emit(text)

    @staticmethod
    def _route_label(source: str) -> str:
        clean = str(source or "").casefold()
        return {
            "model": "MODEL",
            "rules": "RULES",
            "rule": "RULES",
            "panel": "PRINTFLOW",
            "panel-camera": "VISION",
            "task": "TASK",
            "planner": "PLANNER",
            "memory": "MEMORY",
            "agent-loop": "AGENT LOOP",
            "registry": "SKILLS",
            "talk": "TALK",
            "clock": "LOCAL",
            "math": "LOCAL",
            "util": "LOCAL",
        }.get(clean, clean.upper()[:18])

    @classmethod
    def _bubble(cls, role: str, text: str, source: str = "", skill: str = "") -> str:
        safe = html.escape(str(text or "")).replace("\n", "<br>")
        if role == "user":
            return (
                "<table width='100%' cellspacing='0' cellpadding='0' style='margin:8px 0;'>"
                "<tr><td width='19%'></td><td align='right'>"
                "<div style='background:#242044;border:1px solid #5E4AA3;"
                "border-radius:14px;padding:11px 14px;color:#F5F2FF;'>"
                "<span style='color:#BCAEFF;font-size:11px;font-weight:700;'>ВЫ</span><br>"
                f"{safe}</div></td></tr></table>"
            )
        route = html.escape(cls._route_label(source))
        skill_text = html.escape(str(skill or ""))
        badge = ""
        if route or skill_text:
            bits = [part for part in (route, skill_text) if part]
            badge = (
                "&nbsp;&nbsp;<span style='color:#A78BFA;font-size:10px;"
                "font-weight:700;'>"
                + " · ".join(bits) + "</span>"
            )
        return (
            "<table width='100%' cellspacing='0' cellpadding='0' style='margin:8px 0;'>"
            "<tr><td>"
            "<div style='background:#111328;border:1px solid #30345A;"
            "border-radius:14px;padding:11px 14px;color:#ECE9F8;'>"
            "<span style='color:#58E6B1;font-size:11px;font-weight:700;'>LUMA</span>"
            + badge + "<br>"
            + safe + "</div></td><td width='19%'></td></tr></table>"
        )

    def _render_turns(self) -> None:
        html_rows = [
            self._bubble(
                row.get("role", "assistant"),
                row.get("text", ""),
                row.get("source", ""),
                row.get("skill", ""),
            )
            for row in self._turns
        ]
        self.feed.setHtml(
            "<div style='margin:12px 16px;background:#0D0F20;'>"
            + "".join(html_rows)
            + "</div>"
        )
        self._has_history = bool(self._turns)
        self.content.setCurrentIndex(1 if self._has_history else 0)
        bar = self.feed.verticalScrollBar()
        bar.setValue(bar.maximum())

    def set_history(self, turns: list[dict[str, Any]]) -> None:
        self._turns = []
        for turn in turns:
            role = "user" if str(turn.get("role") or "") == "user" else "assistant"
            meta = turn.get("meta") if isinstance(turn.get("meta"), dict) else {}
            self._turns.append({
                "role": role,
                "text": str(turn.get("text") or ""),
                "source": str(meta.get("source") or "") if role == "assistant" else "",
                "skill": str(meta.get("skill") or "") if role == "assistant" else "",
            })
        if not self._turns:
            self.camera_card.hide()
            self.camera_frame.clear()
            self.clear_action_trace()
        self._render_turns()

    def append_local(self, who: str, text: str, source: str = "", skill: str = "") -> None:
        role = "user" if str(who or "").casefold() in {"вы", "user"} else "assistant"
        self._turns.append({
            "role": role,
            "text": str(text or ""),
            "source": str(source or "") if role == "assistant" else "",
            "skill": str(skill or "") if role == "assistant" else "",
        })
        self._render_turns()

    def clear_action_trace(self) -> None:
        self.trace_text.clear()
        self.trace_card.hide()

    def show_action_trace(self, steps: list[dict[str, Any]] | None) -> None:
        """Show explicit operational stages returned by the agent, never hidden model reasoning."""
        rows = [row for row in list(steps or []) if isinstance(row, dict)]
        if not rows:
            self.clear_action_trace()
            return
        icons = {
            "rule": "◆", "context": "◇", "model": "◉", "check": "✓",
            "skill": "→", "task": "▰", "panel": "⌁", "learned": "✦",
            "plan": "≋", "agent": "◎",
        }
        lines = []
        for row in rows[-6:]:
            kind = str(row.get("kind") or "").casefold()
            title = " ".join(str(row.get("title") or "").split())
            detail = " ".join(str(row.get("detail") or "").split())
            if not title:
                continue
            line = f"{icons.get(kind, '·')} {title}"
            if detail:
                line += f"  ·  {detail[:150]}"
            lines.append(line)
        if not lines:
            self.clear_action_trace()
            return
        self.trace_text.setText("\n".join(lines))
        self.trace_badge.setText("LOCAL · " + str(len(lines)) + " ШАГ.")
        self.trace_card.show()

    def show_camera_image(self, data: bytes, title: str = "") -> bool:
        pixmap = QPixmap()
        if not data or not pixmap.loadFromData(bytes(data), "JPEG"):
            self.camera_card.hide()
            return False
        scaled = pixmap.scaled(720, 360, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.camera_frame.setPixmap(scaled)
        self.camera_title.setText(
            "КАМЕРА ПРИНТЕРА" + (f" · {str(title)[:72]}" if str(title or "").strip() else "")
        )
        self.camera_card.show()
        return True

    def set_live_activity(self, phase: str = "idle", heard: str = "", reply: str = "",
                          skill: str = "", detail: str = "", task_id: int = 0,
                          audio_level: int = 0) -> None:
        clean_phase = str(phase or "idle").casefold()
        orb_state = {
            "executing": "working",
            "task": "working",
            "done": "idle",
        }.get(clean_phase, clean_phase)
        if orb_state not in {"idle", "listening", "thinking", "speaking", "working",
                             "waiting", "error", "stopped"}:
            orb_state = "idle"
        self.live_orb.set_state(orb_state)
        self.live_orb.set_activity(audio_level)
        self.live_portrait.set_state(orb_state)
        self.welcome_portrait.set_state(orb_state)

        labels = {
            "thinking": "Люма думает",
            "speaking": "Люма отвечает",
            "executing": "Люма выполняет",
            "task": "Люма ведёт задачу",
            "waiting": "Люма ждёт",
            "error": "Люме нужна помощь",
            "done": "Готово",
            "idle": "Люма готова",
            "listening": "Люма слушает",
        }
        parts = [labels.get(clean_phase, "Люма работает")]
        if skill:
            parts.append(str(skill))
        if task_id:
            parts.append(f"задача #{int(task_id)}")
        if detail:
            parts.append(" ".join(str(detail).split())[:120])
        if reply:
            parts.append("«" + " ".join(str(reply).split())[:180] + "»")
        elif heard:
            parts.append("услышала: «" + " ".join(str(heard).split())[:120] + "»")
        self.live.setText(" · ".join(parts))


class HomePage(QWidget):
    submitted = Signal(str)

    STATE_LABELS = {
        "idle": "Готова",
        "listening": "Слушаю",
        "thinking": "Думаю",
        "speaking": "Отвечаю",
        "executing": "Выполняю",
        "working": "Выполняю",
        "task": "Веду задачу",
        "waiting": "Жду",
        "error": "Нужна помощь",
        "stopped": "STOP ALL",
    }

    def __init__(self) -> None:
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(14)

        hero = GlassCard("violet")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(22, 20, 22, 20)
        hero_layout.setSpacing(20)

        persona = QVBoxLayout()
        persona.setSpacing(6)
        kicker = QLabel("LUMA // LOCAL CORE")
        kicker.setObjectName("heroKicker")
        persona.addWidget(kicker)

        title = QLabel("Люма")
        title.setObjectName("heroTitle")
        persona.addWidget(title)

        description = QLabel(
            "Персональный AI-ассистент на твоём компьютере. "
            "Слушает, думает и действует локально."
        )
        description.setObjectName("heroDescription")
        description.setWordWrap(True)
        description.setMaximumWidth(300)
        persona.addWidget(description)

        self.persona_badge = QLabel("●  PERSONA ONLINE")
        self.persona_badge.setObjectName("localPill")
        self.persona_badge.setMaximumWidth(150)
        persona.addSpacing(8)
        persona.addWidget(self.persona_badge, 0, Qt.AlignLeft)

        self.portrait = LumaPortrait()
        self.portrait.setFixedSize(132, 156)

        persona_row = QHBoxLayout()
        persona_row.addLayout(persona, 1)
        persona_row.addWidget(self.portrait, 0, Qt.AlignBottom)
        hero_layout.addLayout(persona_row, 7)

        orb_column = QVBoxLayout()
        orb_column.setSpacing(2)
        self.orb = LumaOrbCore()
        self.orb.setMinimumSize(250, 250)
        self.orb.setMaximumSize(310, 310)
        orb_column.addWidget(self.orb, 1, Qt.AlignCenter)
        self.state_title = QLabel("Готова")
        self.state_title.setObjectName("orbStateTitle")
        self.state_title.setAlignment(Qt.AlignCenter)
        orb_column.addWidget(self.state_title)
        self.state_detail = QLabel("Ожидаю команду")
        self.state_detail.setObjectName("muted")
        self.state_detail.setAlignment(Qt.AlignCenter)
        self.state_detail.setWordWrap(True)
        self.state_detail.setMaximumWidth(360)
        orb_column.addWidget(self.state_detail)
        hero_layout.addLayout(orb_column, 8)

        telemetry = QVBoxLayout()
        telemetry.setSpacing(10)

        voice_card = GlassCard("cyan")
        voice_box = QVBoxLayout(voice_card)
        voice_box.setContentsMargins(14, 12, 14, 12)
        voice_label = QLabel("ГОЛОС")
        voice_label.setObjectName("metricLabel")
        voice_box.addWidget(voice_label)
        self.voice_value = QLabel("Baya")
        self.voice_value.setObjectName("metricValue")
        voice_box.addWidget(self.voice_value)
        self.voice_meta = QLabel("Silero v5_5_ru · 48 kHz")
        self.voice_meta.setObjectName("muted")
        self.voice_meta.setWordWrap(True)
        voice_box.addWidget(self.voice_meta)
        telemetry.addWidget(voice_card)

        brain_card = GlassCard("violet")
        brain_box = QVBoxLayout(brain_card)
        brain_box.setContentsMargins(14, 12, 14, 12)
        brain_label = QLabel("BRAIN // INTELLIGENCE")
        brain_label.setObjectName("metricLabel")
        brain_box.addWidget(brain_label)
        self.brain_value = QLabel("READY")
        self.brain_value.setObjectName("metricValueSmall")
        brain_box.addWidget(self.brain_value)
        self.brain_meta = QLabel("Локальная модель · функции загружаются…")
        self.brain_meta.setObjectName("muted")
        self.brain_meta.setWordWrap(True)
        brain_box.addWidget(self.brain_meta)
        telemetry.addWidget(brain_card)

        activity_card = GlassCard()
        activity_box = QVBoxLayout(activity_card)
        activity_box.setContentsMargins(14, 12, 14, 12)
        activity_label = QLabel("СЕЙЧАС")
        activity_label.setObjectName("metricLabel")
        activity_box.addWidget(activity_label)
        self.activity_value = QLabel("Ожидаю")
        self.activity_value.setObjectName("metricValueSmall")
        self.activity_value.setWordWrap(True)
        activity_box.addWidget(self.activity_value)
        self.heard_value = QLabel("")
        self.heard_value.setObjectName("muted")
        self.heard_value.setWordWrap(True)
        self.heard_value.hide()
        activity_box.addWidget(self.heard_value)
        telemetry.addWidget(activity_card)

        self.local_badge = QLabel("LOCAL · OFFLINE READY")
        self.local_badge.setObjectName("localPill")
        self.local_badge.setAlignment(Qt.AlignCenter)
        telemetry.addWidget(self.local_badge)
        telemetry.addStretch(1)

        hero_layout.addLayout(telemetry, 5)
        root.addWidget(hero, 1)

        action_card = GlassCard()
        action_box = QVBoxLayout(action_card)
        action_box.setContentsMargins(16, 13, 16, 13)
        action_box.setSpacing(10)
        action_title = QLabel("Быстрые действия")
        action_title.setObjectName("sectionTitle")
        action_box.addWidget(action_title)
        actions = QHBoxLayout()
        for text in (
            "Что у меня сегодня?",
            "Открой загрузки",
            "Что ты умеешь?",
            "Покажи активные задачи",
        ):
            button = QPushButton(text)
            button.setObjectName("suggestion")
            button.clicked.connect(lambda _=False, value=text: self.submitted.emit(value))
            actions.addWidget(button)
        action_box.addLayout(actions)
        root.addWidget(action_card)

    def set_runtime(self, *, connected: bool, state: str, audio_level: int = 0,
                    heard: str = "", reply: str = "", skill: str = "",
                    detail: str = "", task_id: int = 0,
                    safety_stopped: bool = False) -> None:
        clean_state = "stopped" if safety_stopped else str(state or "idle").casefold()
        orb_state = {
            "executing": "working",
            "task": "working",
        }.get(clean_state, clean_state)
        if orb_state not in self.orb_state_names():
            orb_state = "idle"

        self.orb.set_state(orb_state)
        self.orb.set_activity(audio_level)
        self.portrait.set_state(orb_state)
        self.state_title.setText(self.STATE_LABELS.get(clean_state, "Работаю"))

        heard_clean = " ".join(str(heard or "").split())[:130]
        reply_clean = " ".join(str(reply or "").split())[:180]
        detail_clean = " ".join(str(detail or "").split())[:150]
        skill_clean = str(skill or "").strip()

        if not connected:
            self.state_title.setText("Offline")
            self.state_detail.setText("Локальный backend недоступен")
            self.activity_value.setText("Нет соединения")
            self.persona_badge.setText("●  PERSONA OFFLINE")
        elif safety_stopped:
            self.state_detail.setText("STOP ALL активен")
            self.activity_value.setText("Все действия остановлены")
            self.persona_badge.setText("●  PERSONA PAUSED")
        else:
            self.persona_badge.setText("●  PERSONA ONLINE")
            self.state_detail.setText(
                reply_clean or detail_clean or
                (f"Слышу: {heard_clean}" if heard_clean else "Ожидаю команду")
            )
            if skill_clean:
                suffix = f" · задача #{int(task_id)}" if task_id else ""
                self.activity_value.setText(f"{skill_clean}{suffix}")
            elif clean_state == "thinking":
                self.activity_value.setText("Формирую ответ")
            elif clean_state == "speaking":
                self.activity_value.setText("Отвечаю голосом")
            elif clean_state == "listening":
                self.activity_value.setText("Слушаю микрофон")
            else:
                self.activity_value.setText("Ожидаю")

        self.heard_value.setText(f"Вы: {heard_clean}" if heard_clean else "")
        self.heard_value.setVisible(bool(heard_clean))

    def set_brain(self, *, model_ok: bool, ready: int = 0, total: int = 0,
                  route: str = "ready", repaired: bool = False,
                  agent_iterations: int = 0) -> None:
        clean_route = str(route or "ready").casefold()
        labels = {
            "rules": "RULES",
            "rule": "RULES",
            "model": "MODEL",
            "panel": "PRINTFLOW",
            "panel-camera": "VISION",
            "task": "TASK",
            "planner": "PLANNER",
            "memory": "MEMORY",
            "agent-loop": "AGENT LOOP",
            "ready": "READY",
        }
        label = "SELF-CORRECTED" if repaired else labels.get(clean_route, clean_route.upper()[:24] or "READY")
        self.brain_value.setText(label)
        model_text = "model ready" if model_ok else "model offline"
        tools = f"{int(ready)}/{int(total)} функций" if total else "функции загружаются"
        if repaired:
            suffix = " · repair loop"
        elif clean_route == "agent-loop":
            count = max(0, int(agent_iterations or 0))
            suffix = f" · observe → decide → act · {count} итерац." if count else " · observe → decide → act"
        else:
            suffix = " · context + tool router"
        self.brain_meta.setText(f"{model_text} · {tools}{suffix}")

    @staticmethod
    def orb_state_names() -> set[str]:
        return {"idle", "listening", "thinking", "speaking", "working", "waiting", "error", "stopped"}

    def set_voice(self, *, engine: str, speaker: str, sample_rate: int,
                  model: str = "", ready: bool = False) -> None:
        engine_clean = str(engine or "system")
        speaker_clean = str(speaker or "системный")
        self.voice_value.setText(speaker_clean.capitalize() if speaker_clean else engine_clean)
        bits = [engine_clean]
        if model:
            bits.append(str(model))
        if sample_rate:
            bits.append(f"{int(sample_rate) // 1000} kHz")
        bits.append("готов" if ready else "fallback")
        self.voice_meta.setText(" · ".join(bits))


class VoicePage(QWidget):
    tts_save = Signal(str, str, str)
    tts_reset = Signal()
    tts_test = Signal()
    pronunciation_add = Signal(str, str)
    pronunciation_delete = Signal(str)
    voice_tune = Signal(int, float, int, float)
    voice_diag_reset = Signal()

    def __init__(self) -> None:
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Голос Люмы")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Локальный голос Baya, произношение и диагностика микрофона.")
        subtitle.setObjectName("muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        root.addLayout(header)

        hero = GlassCard("cyan")
        hero_box = QHBoxLayout(hero)
        hero_box.setContentsMargins(20, 16, 20, 16)
        hero_box.setSpacing(18)

        self.portrait = LumaPortrait(compact=True)
        self.portrait.set_state("speaking")
        hero_box.addWidget(self.portrait, 0, Qt.AlignVCenter)

        voice_identity = QVBoxLayout()
        voice_identity.setSpacing(4)
        label = QLabel("PRIMARY LOCAL VOICE")
        label.setObjectName("heroKicker")
        voice_identity.addWidget(label)
        self.voice_name = QLabel("Baya")
        self.voice_name.setObjectName("metricValue")
        voice_identity.addWidget(self.voice_name)
        self.voice_profile = QLabel("Silero v5_5_ru · 48 kHz · CPU local")
        self.voice_profile.setObjectName("muted")
        voice_profile_text = (
            "Мягкий женский профиль для Люмы. "
            "Piper и системный TTS остаются резервной цепочкой."
        )
        profile_note = QLabel(voice_profile_text)
        profile_note.setObjectName("muted")
        profile_note.setWordWrap(True)
        voice_identity.addWidget(self.voice_profile)
        voice_identity.addWidget(profile_note)
        hero_box.addLayout(voice_identity, 1)

        self.preview_orb = LumaOrbCore(compact=True)
        self.preview_orb.set_state("speaking")
        hero_box.addWidget(self.preview_orb, 0, Qt.AlignVCenter)

        preview = QPushButton("▶  Прослушать Baya")
        preview.setObjectName("primary")
        preview.clicked.connect(lambda: self.tts_test.emit())
        hero_box.addWidget(preview)
        root.addWidget(hero)

        content = QHBoxLayout()
        content.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(14)

        engine_card = GlassCard("violet")
        engine = QVBoxLayout(engine_card)
        engine.setContentsMargins(16, 14, 16, 14)
        engine.setSpacing(9)
        engine_title = QLabel("Движок и fallback")
        engine_title.setObjectName("sectionTitle")
        engine.addWidget(engine_title)

        self.tts_meta = QLabel("TTS: …")
        self.tts_meta.setObjectName("muted")
        self.tts_meta.setWordWrap(True)
        engine.addWidget(self.tts_meta)

        chain = QLabel("SILERO BAYA  →  PIPER  →  SYSTEM")
        chain.setObjectName("voiceChain")
        chain.setAlignment(Qt.AlignCenter)
        engine.addWidget(chain)

        self.tts_piper = QLineEdit()
        self.tts_piper.setPlaceholderText("Piper executable · fallback")
        engine.addWidget(self.tts_piper)
        self.tts_model = QLineEdit()
        self.tts_model.setPlaceholderText("Piper model .onnx · fallback")
        engine.addWidget(self.tts_model)
        self.tts_speaker = QLineEdit()
        self.tts_speaker.setPlaceholderText("Piper speaker id")
        engine.addWidget(self.tts_speaker)

        tts_buttons = QHBoxLayout()
        apply_tts = QPushButton("Сохранить fallback")
        apply_tts.clicked.connect(
            lambda: self.tts_save.emit(
                self.tts_piper.text().strip(),
                self.tts_model.text().strip(),
                self.tts_speaker.text().strip(),
            )
        )
        reset_tts = QPushButton("Сбросить")
        reset_tts.clicked.connect(lambda: self.tts_reset.emit())
        tts_buttons.addWidget(apply_tts)
        tts_buttons.addWidget(reset_tts)
        tts_buttons.addStretch(1)
        engine.addLayout(tts_buttons)

        self.tts_status = QLabel(
            "Основной профиль: Silero v5_5_ru · Baya · 48 kHz."
        )
        self.tts_status.setObjectName("muted")
        self.tts_status.setWordWrap(True)
        engine.addWidget(self.tts_status)
        left.addWidget(engine_card)

        pronunciation_card = GlassCard()
        pronunciation = QVBoxLayout(pronunciation_card)
        pronunciation.setContentsMargins(16, 14, 16, 14)
        pronunciation.setSpacing(9)
        pronunciation_title = QLabel("Произношение")
        pronunciation_title.setObjectName("sectionTitle")
        pronunciation.addWidget(pronunciation_title)
        hint = QLabel(
            "Правила применяются к подготовленному тексту перед локальным TTS. "
            "Например: Bambu → бэмбу."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        pronunciation.addWidget(hint)

        pronunciation_row = QHBoxLayout()
        self.pronunciation_source = QLineEdit()
        self.pronunciation_source.setPlaceholderText("Как написано")
        self.pronunciation_target = QLineEdit()
        self.pronunciation_target.setPlaceholderText("Как произносить")
        add_pronunciation = QPushButton("Добавить")
        add_pronunciation.clicked.connect(
            lambda: self.pronunciation_add.emit(
                self.pronunciation_source.text().strip(),
                self.pronunciation_target.text().strip(),
            )
        )
        pronunciation_row.addWidget(self.pronunciation_source, 1)
        pronunciation_row.addWidget(self.pronunciation_target, 1)
        pronunciation_row.addWidget(add_pronunciation)
        pronunciation.addLayout(pronunciation_row)

        self.pronunciation_list = QListWidget()
        self.pronunciation_list.setMaximumHeight(150)
        pronunciation.addWidget(self.pronunciation_list)
        pronunciation_buttons = QHBoxLayout()
        delete_pronunciation = QPushButton("Удалить выбранное")
        delete_pronunciation.clicked.connect(
            lambda: self.pronunciation_delete.emit(
                str((self.pronunciation_list.currentItem().data(Qt.UserRole)
                     if self.pronunciation_list.currentItem() else "") or "")
            )
        )
        pronunciation_buttons.addWidget(delete_pronunciation)
        pronunciation_buttons.addStretch(1)
        pronunciation.addLayout(pronunciation_buttons)

        self.pronunciation_status = QLabel("Пользовательских правил: 0")
        self.pronunciation_status.setObjectName("muted")
        self.pronunciation_status.setWordWrap(True)
        pronunciation.addWidget(self.pronunciation_status)
        left.addWidget(pronunciation_card)
        left.addStretch(1)

        diagnostics_card = GlassCard()
        diagnostics = QVBoxLayout(diagnostics_card)
        diagnostics.setContentsMargins(16, 14, 16, 14)
        diagnostics.setSpacing(10)
        diagnostics_title = QLabel("Voice Diagnostics")
        diagnostics_title.setObjectName("sectionTitle")
        diagnostics.addWidget(diagnostics_title)
        self.voice_diag_meta = QLabel("ASR: … · vocabulary: 0")
        self.voice_diag_meta.setObjectName("muted")
        self.voice_diag_meta.setWordWrap(True)
        diagnostics.addWidget(self.voice_diag_meta)

        self.voice_level = QProgressBar()
        self.voice_level.setRange(0, 4000)
        self.voice_level.setFormat("Mic level: %v")
        diagnostics.addWidget(self.voice_level)
        self.voice_threshold = QProgressBar()
        self.voice_threshold.setRange(0, 4000)
        self.voice_threshold.setFormat("Echo/VAD threshold: %v")
        diagnostics.addWidget(self.voice_threshold)
        self.voice_floor = QProgressBar()
        self.voice_floor.setRange(0, 4000)
        self.voice_floor.setFormat("Echo floor: %v")
        diagnostics.addWidget(self.voice_floor)

        self.voice_vad = QSpinBox()
        self.voice_vad.setRange(40, 12000)
        self.voice_vad.setSingleStep(20)
        self.voice_vad.setPrefix("VAD ")
        diagnostics.addWidget(self.voice_vad)

        self.voice_multiplier = QDoubleSpinBox()
        self.voice_multiplier.setRange(1.0, 4.0)
        self.voice_multiplier.setSingleStep(0.05)
        self.voice_multiplier.setDecimals(2)
        self.voice_multiplier.setPrefix("Echo gate × ")
        diagnostics.addWidget(self.voice_multiplier)

        self.voice_margin = QSpinBox()
        self.voice_margin.setRange(0, 4000)
        self.voice_margin.setSingleStep(20)
        self.voice_margin.setPrefix("Margin ")
        diagnostics.addWidget(self.voice_margin)

        self.voice_alpha = QDoubleSpinBox()
        self.voice_alpha.setRange(0.05, 0.95)
        self.voice_alpha.setSingleStep(0.05)
        self.voice_alpha.setDecimals(2)
        self.voice_alpha.setPrefix("Adapt ")
        diagnostics.addWidget(self.voice_alpha)

        diag_buttons = QHBoxLayout()
        apply_voice = QPushButton("Применить калибровку")
        apply_voice.setObjectName("primary")
        apply_voice.clicked.connect(
            lambda: self.voice_tune.emit(
                int(self.voice_vad.value()),
                float(self.voice_multiplier.value()),
                int(self.voice_margin.value()),
                float(self.voice_alpha.value()),
            )
        )
        reset_voice = QPushButton("Сбросить")
        reset_voice.clicked.connect(lambda: self.voice_diag_reset.emit())
        diag_buttons.addWidget(apply_voice)
        diag_buttons.addWidget(reset_voice)
        diagnostics.addLayout(diag_buttons)

        self.voice_diag_status = QLabel("Калибровка хранится только в RAM.")
        self.voice_diag_status.setObjectName("muted")
        self.voice_diag_status.setWordWrap(True)
        diagnostics.addWidget(self.voice_diag_status)
        diagnostics.addStretch(1)

        content.addLayout(left, 7)
        content.addWidget(diagnostics_card, 5)
        root.addLayout(content, 1)

    def set_runtime_state(self, state: str, audio_level: int = 0) -> None:
        clean = str(state or "idle").casefold()
        clean = {"executing": "working", "task": "working"}.get(clean, clean)
        if clean not in {"idle", "listening", "thinking", "speaking", "working",
                          "waiting", "error", "stopped"}:
            clean = "idle"
        self.portrait.set_state(clean)
        self.preview_orb.set_state(clean)
        self.preview_orb.set_activity(audio_level)

    def set_voice_profile(self, *, engine: str, speaker: str, sample_rate: int,
                          model: str, ready: bool) -> None:
        speaker_clean = str(speaker or "system")
        self.voice_name.setText(speaker_clean.capitalize())
        bits = [str(engine or "system")]
        if model:
            bits.append(str(model))
        if sample_rate:
            bits.append(f"{int(sample_rate) // 1000} kHz")
        bits.append("LOCAL READY" if ready else "FALLBACK")
        self.voice_profile.setText(" · ".join(bits))


class ActivityPage(QWidget):
    refresh_requested = Signal()

    PHASES = {
        "thinking": "🧠 думает",
        "speaking": "🔊 отвечает",
        "executing": "⚡ выполняет",
        "task": "✓ ведёт задачу",
        "waiting": "⏳ ждёт",
        "done": "✓ готово",
        "error": "⚠ ошибка",
        "idle": "○ готова",
    }

    def __init__(self) -> None:
        super().__init__()
        self._payload: dict[str, Any] = {}
        layout = QVBoxLayout(self)

        head = QHBoxLayout()
        title = QLabel("Активность")
        title.setObjectName("pageTitle")
        head.addWidget(title)
        head.addStretch(1)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.refresh_requested)
        head.addWidget(refresh)
        layout.addLayout(head)

        sub = QLabel("Фильтруемый маршрут: запрос → skill/task → verification → provider result.")
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        filters = QHBoxLayout()
        self.task_filter = QComboBox()
        self.task_filter.addItem("Все задачи", 0)
        self.task_filter.currentIndexChanged.connect(self._render)
        filters.addWidget(self.task_filter)

        self.status_filter = QComboBox()
        self.status_filter.addItem("Все статусы", "")
        for status in ("running", "waiting", "paused", "failed", "done", "cancelled", "planned"):
            self.status_filter.addItem(status, status)
        self.status_filter.currentIndexChanged.connect(self._render)
        filters.addWidget(self.status_filter)

        self.search_filter = QLineEdit()
        self.search_filter.setPlaceholderText("skill / текст / outcome")
        self.search_filter.textChanged.connect(self._render)
        filters.addWidget(self.search_filter, 1)
        layout.addLayout(filters)

        self.browser = QTextBrowser()
        layout.addWidget(self.browser, 1)

    @staticmethod
    def _e(value: Any) -> str:
        return html.escape(" ".join(str(value or "").split()))

    @staticmethod
    def _json_compact(value: Any, limit: int = 900) -> str:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            text = str(value or "")
        return text[:limit]

    def _activity_card(self, row: dict[str, Any], title: str) -> str:
        phase = str(row.get("phase") or "idle")
        bits = [f"<b>{self._e(title)}</b> · {self._e(self.PHASES.get(phase, phase))}"]
        if row.get("heard"):
            bits.append(f"<div>🎧 Вы: {self._e(row.get('heard'))}</div>")
        if row.get("reply"):
            bits.append(f"<div>💬 Люма: {self._e(row.get('reply'))}</div>")
        if row.get("skill"):
            task = f" · задача #{int(row.get('task_id') or 0)}" if row.get("task_id") else ""
            bits.append(f"<div>⚡ {self._e(row.get('skill'))}{self._e(task)}</div>")
        if row.get("detail"):
            bits.append(f"<div><small>{self._e(row.get('detail'))}</small></div>")
        return "<div style='margin:8px 0;padding:10px;border:1px solid #334155;border-radius:8px'>" + "".join(bits) + "</div>"

    def _matches(self, *values: Any) -> bool:
        needle = " ".join(self.search_filter.text().casefold().split())
        if not needle:
            return True
        hay = " ".join(str(value or "") for value in values).casefold()
        return needle in hay

    def _step_detail(self, task_id: int, step: dict[str, Any]) -> str:
        verification = step.get("verification") if isinstance(step.get("verification"), dict) else {}
        evidence = verification.get("evidence") if isinstance(verification.get("evidence"), dict) else {}
        result = step.get("result") if isinstance(step.get("result"), dict) else {}
        safe_result = {key: value for key, value in result.items() if key != "_verification"}
        bits = [
            "<div style='margin-left:14px;padding:7px 0'>",
            f"<b>{int(step.get('seq') or 0) + 1}. {self._e(step.get('skill'))}</b>",
            f" · {self._e(step.get('status') or 'pending')}",
        ]
        stamp = str(step.get("finished_at") or step.get("started_at") or "")
        if stamp:
            bits.append(f" · <small>{self._e(stamp)}</small>")
        if verification:
            bits.append(
                f"<div>🔎 verification: <b>{self._e(verification.get('status'))}</b>"
                f" · {self._e(verification.get('reason'))}</div>"
            )
        if evidence:
            bits.append(
                f"<div><small>evidence: {self._e(self._json_compact(evidence, 700))}</small></div>"
            )
        if safe_result:
            bits.append(
                f"<div><small>provider result: {self._e(self._json_compact(safe_result, 900))}</small></div>"
            )
        pending = str(step.get("pending_action") or "")
        if pending:
            bits.append(f"<div><small>pending confirmation: {self._e(pending)}</small></div>")
        bits.append("</div>")
        return "".join(bits)

    def _task_card(self, task: dict[str, Any], detailed: bool) -> str:
        task_id = int(task.get("id") or 0)
        steps = list(task.get("steps") or [])
        title = self._e(task.get("title") or f"Задача {task_id}")
        status = self._e(task.get("status") or "")
        chunks = [
            "<div style='margin:10px 0;padding:10px;border:1px solid #334155;border-radius:8px'>",
            f"<b>#{task_id} {title}</b> · {status}"
            f" · {int(task.get('progress') or 0)}/{int(task.get('total_steps') or len(steps))}",
        ]
        if task.get("error"):
            chunks.append(f"<div>⚠ {self._e(task.get('error'))}</div>")

        for step in steps:
            if not self._matches(step.get("skill"), step.get("status"),
                                 step.get("verification"), step.get("result")):
                continue
            if detailed:
                chunks.append(self._step_detail(task_id, step))
            else:
                verification = step.get("verification") if isinstance(step.get("verification"), dict) else {}
                verify = str(verification.get("status") or "")
                line = (
                    f"{int(step.get('seq') or 0) + 1}. {self._e(step.get('skill'))}"
                    f" · {self._e(step.get('status') or 'pending')}"
                )
                if verify:
                    line += f" · проверка: {self._e(verify)}"
                    reason = str(verification.get("reason") or "")
                    if reason:
                        line += f" ({self._e(reason)})"
                chunks.append(f"<div style='margin-left:14px'>{line}</div>")

        replans = list(task.get("replans") or [])
        if replans:
            chunks.append("<div style='margin-top:7px'><b>Перепланирование</b></div>")
            for row in replans[:8]:
                chunks.append(
                    f"<div style='margin-left:14px'><small>{self._e(row.get('created_at'))}</small>"
                    f" · с шага {int(row.get('replace_from') or 0) + 1}"
                    f" · {self._e(row.get('reason'))}</div>"
                )
                if detailed:
                    old_skills = " → ".join(str(x.get("skill") or "") for x in list(row.get("old_tail") or []))
                    new_skills = " → ".join(str(x.get("skill") or "") for x in list(row.get("new_tail") or []))
                    chunks.append(
                        f"<div style='margin-left:28px'><small>было: {self._e(old_skills) or '—'}"
                        f"<br>стало: {self._e(new_skills) or '—'}</small></div>"
                    )
        chunks.append("</div>")
        return "".join(chunks)

    def _render(self, *_args: Any) -> None:
        payload = self._payload
        activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
        current = activity.get("current") if isinstance(activity.get("current"), dict) else {}
        recent = list(activity.get("recent") or [])
        tasks = list(payload.get("tasks") or [])
        journal = list(payload.get("journal") or [])
        drafts = list(payload.get("replans") or [])

        selected_task = int(self.task_filter.currentData() or 0)
        selected_status = str(self.status_filter.currentData() or "")
        filtered_tasks = [
            task for task in tasks
            if (not selected_task or int(task.get("id") or 0) == selected_task)
            and (not selected_status or str(task.get("status") or "") == selected_status)
            and self._matches(task.get("title"), task.get("goal"), task.get("status"), task.get("steps"))
        ]

        chunks = ["<h3>Сейчас</h3>"]
        if current and (current.get("active") or current.get("phase") != "idle") and self._matches(current):
            chunks.append(self._activity_card(current, "Текущий маршрут"))
        else:
            chunks.append("<p>Нет активного маршрута по текущему фильтру.</p>")

        if recent and not selected_task:
            matching_recent = [row for row in recent[:4] if self._matches(row)]
            if matching_recent:
                chunks.append("<h3>Недавние маршруты</h3>")
                for row in matching_recent:
                    chunks.append(self._activity_card(row, "Завершено"))

        chunks.append("<h3>Задачи</h3>")
        if filtered_tasks:
            for task in filtered_tasks[:20]:
                chunks.append(self._task_card(task, detailed=bool(selected_task)))
        else:
            chunks.append("<p>Задач по фильтру нет.</p>")

        matching_drafts = [
            row for row in drafts
            if (not selected_task or int(row.get("task_id") or 0) == selected_task)
            and self._matches(row.get("reason"), row.get("summary"), row.get("steps"))
        ]
        if matching_drafts:
            chunks.append("<h3>Черновики replan</h3>")
            for row in matching_drafts[:10]:
                chunks.append(
                    f"<div><b>Задача #{int(row.get('task_id') or 0)}</b>"
                    f" · {self._e(row.get('summary') or row.get('reason'))}"
                    f" · с шага {int(row.get('replace_from') or 0) + 1}</div>"
                )

        chunks.append("<h3>Фактические действия</h3>")
        matching_journal = [
            row for row in journal
            if self._matches(row.get("skill"), row.get("outcome"), row.get("detail"),
                             row.get("target"), row.get("params"))
        ]
        if matching_journal:
            for row in matching_journal[:60]:
                outcome = self._e(row.get("outcome") or "")
                detail = self._e(row.get("detail") or "")
                target = self._e(row.get("target") or "")
                stamp = self._e(row.get("at") or "")
                suffix = f" · {target}" if target else ""
                chunks.append(
                    f"<div><small>{stamp}</small> · <b>{self._e(row.get('skill'))}</b>"
                    f" · {outcome}{suffix}"
                    + (f"<br><span style='margin-left:14px'>{detail}</span>" if detail else "")
                    + "</div>"
                )
        else:
            chunks.append("<p>Журнал по фильтру пуст.</p>")

        self.browser.setHtml("".join(chunks))

    def set_payload(self, payload: dict[str, Any]) -> None:
        self._payload = dict(payload or {})
        tasks = list(self._payload.get("tasks") or [])
        current_task = int(self.task_filter.currentData() or 0)
        self.task_filter.blockSignals(True)
        self.task_filter.clear()
        self.task_filter.addItem("Все задачи", 0)
        for task in tasks:
            task_id = int(task.get("id") or 0)
            self.task_filter.addItem(
                f"#{task_id} {str(task.get('title') or 'Задача')[:42]}",
                task_id,
            )
        index = self.task_filter.findData(current_task)
        self.task_filter.setCurrentIndex(index if index >= 0 else 0)
        self.task_filter.blockSignals(False)
        self._render()


class TasksPage(QWidget):
    decision = Signal(str, bool)
    command = Signal(int, str)
    plan_preview = Signal(str)
    plan_command = Signal(str, str)
    replan_preview = Signal(int)
    replan_command = Signal(str, str)
    refresh_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.layout = QVBoxLayout(self)
        head = QHBoxLayout()
        title = QLabel("Задачи")
        title.setObjectName("pageTitle")
        head.addWidget(title)
        head.addStretch(1)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.refresh_requested)
        head.addWidget(refresh)
        self.layout.addLayout(head)
        planner_row = QHBoxLayout()
        self.goal_input = QLineEdit()
        self.goal_input.setPlaceholderText("Цель: например, подготовь компьютер к работе")
        planner_row.addWidget(self.goal_input, 1)
        preview = QPushButton("Составить план")
        preview.setObjectName("primary")
        preview.clicked.connect(self._preview_plan)
        planner_row.addWidget(preview)
        self.layout.addLayout(planner_row)
        self.planner_message = QLabel("")
        self.planner_message.setObjectName("muted")
        self.planner_message.setWordWrap(True)
        self.layout.addWidget(self.planner_message)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.host_widget = QWidget()
        self.host = QVBoxLayout(self.host_widget)
        self.host.setAlignment(Qt.AlignTop)
        scroll.setWidget(self.host_widget)
        self.layout.addWidget(scroll, 1)

    def _preview_plan(self) -> None:
        goal = self.goal_input.text().strip()
        if goal:
            self.plan_preview.emit(goal)

    def set_planner_message(self, text: str) -> None:
        self.planner_message.setText(str(text or ""))

    def _clear(self) -> None:
        while self.host.count():
            item = self.host.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def set_payload(self, payload: dict[str, Any]) -> None:
        self._clear()
        plans = list(payload.get("plans") or [])
        replans = list(payload.get("replans") or [])
        tasks = list(payload.get("tasks") or [])
        pending = list(payload.get("pending") or [])
        notifications = list(payload.get("notifications") or [])
        if not plans and not replans and not tasks and not pending and not notifications:
            self.host.addWidget(QLabel("Ничего не ждёт."))
            return
        for plan in plans:
            card = QFrame()
            card.setObjectName("glassCard")
            card.setProperty("accent", "violet")
            box = QVBoxLayout(card)
            title = str(plan.get("title") or "План")
            summary = str(plan.get("summary") or "")
            box.addWidget(QLabel(f"План: {title}"))
            if summary:
                note = QLabel(summary)
                note.setWordWrap(True)
                note.setObjectName("muted")
                box.addWidget(note)
            for step in list(plan.get("steps") or []):
                risk = str(step.get("risk") or "read")
                confirm = " · подтверждение" if step.get("confirm") else ""
                why = str(step.get("why") or "")
                line = f"{int(step.get('seq') or 0) + 1}. {step.get('title') or step.get('skill')} · {risk}{confirm}"
                params = dict(step.get("params") or {})
                if params:
                    pairs = ", ".join(f"{key}={value}" for key, value in params.items())
                    line += f" · {pairs}"
                if why:
                    line += f" — {why}"
                label = QLabel(line)
                label.setWordWrap(True)
                box.addWidget(label)
            buttons = QHBoxLayout()
            plan_id = str(plan.get("id") or "")
            discard = QPushButton("Отменить план")
            discard.clicked.connect(lambda _=False, i=plan_id: self.plan_command.emit(i, "discard"))
            approve = QPushButton("Запустить план")
            approve.setObjectName("primary")
            approve.clicked.connect(lambda _=False, i=plan_id: self.plan_command.emit(i, "approve"))
            buttons.addStretch(1)
            buttons.addWidget(discard)
            buttons.addWidget(approve)
            box.addLayout(buttons)
            self.host.addWidget(card)

        for replan in replans:
            card = QFrame()
            card.setObjectName("glassCard")
            card.setProperty("accent", "amber")
            box = QVBoxLayout(card)
            heading = QLabel(f"Новый маршрут для задачи #{replan.get('task_id')}")
            heading.setObjectName("pageTitle")
            box.addWidget(heading)
            for field in ("reason", "summary"):
                if replan.get(field):
                    label = QLabel(str(replan[field]))
                    label.setWordWrap(True)
                    box.addWidget(label)
            old = list(replan.get("old_tail") or [])
            new = list(replan.get("steps") or [])
            for title, rows in (("Текущие незавершённые шаги", old),
                                ("Предлагаемые шаги", new)):
                box.addWidget(QLabel(title))
                for step in rows:
                    risk = str(step.get("risk") or "")
                    confirm = " · требует подтверждения" if step.get("confirm") else ""
                    details = f"{int(step.get('seq') or 0) + 1}. {step.get('title') or step.get('skill')}"
                    if risk:
                        details += f" · риск: {risk}{confirm}"
                    if step.get("verification", {}).get("status"):
                        details += f" · проверка: {step['verification']['status']}"
                    line = QLabel(details)
                    line.setWordWrap(True)
                    box.addWidget(line)
            buttons = QHBoxLayout()
            replan_id = str(replan.get("id") or "")
            keep = QPushButton("Сохранить текущий маршрут")
            keep.clicked.connect(lambda _=False, i=replan_id: self.replan_command.emit(i, "discard"))
            approve = QPushButton("Принять новый маршрут")
            approve.setObjectName("primary")
            approve.clicked.connect(lambda _=False, i=replan_id: self.replan_command.emit(i, "approve"))
            buttons.addStretch(1)
            buttons.addWidget(keep)
            buttons.addWidget(approve)
            box.addLayout(buttons)
            self.host.addWidget(card)

        for task in tasks:
            card = QFrame()
            card.setObjectName("glassCard")
            status = str(task.get("status") or "planned")
            card.setProperty("accent", "cyan" if status in ("running", "waiting") else "violet")
            box = QVBoxLayout(card)
            box.setSpacing(8)
            title = str(task.get("title") or f"Задача {task.get('id')}")
            progress = int(task.get("progress") or 0)
            total = int(task.get("total_steps") or 0)

            task_head = QHBoxLayout()
            title_label = QLabel(title)
            title_label.setObjectName("taskTitle")
            title_label.setWordWrap(True)
            task_head.addWidget(title_label, 1)
            status_label = QLabel(status.upper())
            status_label.setObjectName("taskStatus")
            task_head.addWidget(status_label, 0, Qt.AlignTop)
            box.addLayout(task_head)

            progress_bar = QProgressBar()
            progress_bar.setObjectName("taskProgress")
            progress_bar.setTextVisible(False)
            progress_bar.setRange(0, max(1, total))
            progress_bar.setValue(max(0, min(progress, max(1, total))))
            box.addWidget(progress_bar)
            progress_meta = QLabel(f"{progress}/{total} шагов")
            progress_meta.setObjectName("muted")
            box.addWidget(progress_meta)

            steps = list(task.get("steps") or [])
            checks = [
                str((step.get("verification") or {}).get("status") or "")
                for step in steps
                if isinstance(step.get("verification"), dict) and step.get("verification")
            ]
            if checks:
                verified = sum(1 for value in checks if value == "verified")
                assumed = sum(1 for value in checks if value == "assumed")
                failed_checks = sum(1 for value in checks if value == "failed")
                parts = [f"verified {verified}", f"assumed {assumed}"]
                if failed_checks:
                    parts.append(f"failed {failed_checks}")
                check_label = QLabel("Проверка: " + " · ".join(parts))
                check_label.setObjectName("muted")
                box.addWidget(check_label)

            current_raw = task.get("current_step")
            current_index = int(current_raw if current_raw is not None else progress)
            for step in steps:
                seq = int(step.get("seq") or 0)
                step_status = str(step.get("status") or "")
                if not step_status:
                    if seq < progress:
                        step_status = "done"
                    elif seq == current_index and status not in ("done", "cancelled"):
                        step_status = "running"
                    else:
                        step_status = "pending"
                verification = str((step.get("verification") or {}).get("status") or "")
                symbol = {
                    "done": "✓", "running": "●", "waiting": "◌",
                    "failed": "×", "cancelled": "−", "pending": "○",
                }.get(step_status, "○")
                text_line = f"{symbol}  {seq + 1:02d}  {step.get('title') or step.get('skill') or 'Шаг'}"
                text_line += f"  ·  {step_status}"
                if verification:
                    text_line += f"  ·  {verification}"
                params = dict(step.get("params") or {})
                if params:
                    compact = ", ".join(f"{key}={value}" for key, value in list(params.items())[:3])
                    text_line += f"\n      {compact}"
                step_label = QLabel(text_line)
                step_label.setWordWrap(True)
                step_label.setObjectName("taskStepCurrent" if seq == current_index and status not in ("done", "cancelled")
                                         else "taskStep")
                box.addWidget(step_label)

            current = next((step for step in steps if int(step.get("seq") or 0) == current_index), None)
            if current and status not in ("done", "cancelled"):
                detail = QLabel(f"Сейчас выполняю: {current.get('title') or current.get('skill') or 'шаг'}")
                detail.setObjectName("taskNow")
                detail.setWordWrap(True)
                box.addWidget(detail)
            if task.get("error"):
                error = QLabel(str(task.get("error")))
                error.setObjectName("taskError")
                error.setWordWrap(True)
                box.addWidget(error)
            buttons = QHBoxLayout()
            task_id = int(task.get("id") or 0)
            if status in ("planned", "paused"):
                op = "resume" if status == "paused" else "run"
                run = QPushButton("Продолжить" if status == "paused" else "Запустить")
                run.clicked.connect(lambda _=False, i=task_id, action=op: self.command.emit(i, action))
                buttons.addWidget(run)
            if status in ("paused", "failed") and any(
                    step.get("status") != "done" for step in steps):
                replan = QPushButton("Предложить новый маршрут")
                replan.clicked.connect(lambda _=False, i=task_id: self.replan_preview.emit(i))
                buttons.addWidget(replan)
            if status in ("running", "waiting"):
                pause = QPushButton("Пауза")
                pause.clicked.connect(lambda _=False, i=task_id: self.command.emit(i, "pause"))
                buttons.addWidget(pause)
            if status not in ("done", "cancelled"):
                cancel = QPushButton("Отменить задачу")
                cancel.clicked.connect(lambda _=False, i=task_id: self.command.emit(i, "cancel"))
                buttons.addWidget(cancel)
            buttons.addStretch(1)
            box.addLayout(buttons)
            self.host.addWidget(card)

        for row in pending:
            card = QFrame()
            card.setObjectName("glassCard")
            box = QVBoxLayout(card)
            box.addWidget(QLabel(str(row.get("text") or row.get("skill") or "Действие требует решения")))
            buttons = QHBoxLayout()
            no = QPushButton("Отмена")
            yes = QPushButton("Разрешить")
            yes.setObjectName("primary")
            action_id = str(row.get("id") or "")
            no.clicked.connect(lambda _=False, i=action_id: self.decision.emit(i, False))
            yes.clicked.connect(lambda _=False, i=action_id: self.decision.emit(i, True))
            buttons.addStretch(1)
            buttons.addWidget(no)
            buttons.addWidget(yes)
            box.addLayout(buttons)
            self.host.addWidget(card)
        for row in notifications:
            text = f"{row.get('title') or 'Уведомление'}: {row.get('text') or ''}"
            label = QLabel(text)
            label.setWordWrap(True)
            self.host.addWidget(label)


class ControlCenter(QMainWindow):
    chat_submitted = Signal(str)
    refresh_page = Signal(str)
    clear_chat = Signal()
    mic_toggle = Signal()
    safety_toggle = Signal()
    task_decision = Signal(str, bool)
    task_command = Signal(int, str)
    plan_preview = Signal(str)
    plan_command = Signal(str, str)
    replan_preview = Signal(int)
    replan_command = Signal(str, str)
    persona_save = Signal(object)
    persona_reset = Signal()
    autonomy_save = Signal(str, object)
    autonomy_reset = Signal()
    proactivity_save = Signal(object)
    proactivity_reset = Signal()
    voice_tune = Signal(int, float, int, float)
    voice_diag_reset = Signal()
    tts_save = Signal(str, str, str)
    tts_reset = Signal()
    tts_test = Signal()
    pronunciation_add = Signal(str, str)
    pronunciation_delete = Signal(str)

    NAV = [
        ("home", "✦  Главная"),
        ("chat", "◈  Разговор"),
        ("voice", "◉  Голос"),
        ("today", "◇  Сегодня"),
        ("tasks", "✓  Задачи"),
        ("activity", "⌁  Активность"),
        ("memory", "◌  Память"),
        ("learning", "✦  Обучение"),
        ("skills", "⬡  Навыки"),
        ("journal", "≋  Журнал"),
        ("settings", "⚙  Настройки"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Люма")
        self.setMinimumSize(980, 680)
        self.resize(1180, 780)
        root = AmbientCanvas()
        root.setObjectName("shellRoot")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(236)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 18, 14, 14)
        side.setSpacing(14)

        self.brand_card = BrandCard()
        side.addWidget(self.brand_card)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        for key, label in self.NAV:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            self.nav.addItem(item)
        side.addWidget(self.nav, 1)
        body.addWidget(sidebar)

        main = QWidget()
        main.setObjectName("mainContent")
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(18, 16, 18, 12)
        main_layout.setSpacing(14)

        self.status_header = StatusHeader()
        main_layout.addWidget(self.status_header)

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}

        self.home = HomePage()
        self.home.submitted.connect(self.chat_submitted)
        self.pages["home"] = self.home
        self.stack.addWidget(self.home)

        self.chat = ChatPage()
        self.chat.submitted.connect(self.chat_submitted)
        self.chat.clear_requested.connect(self.clear_chat)
        self.pages["chat"] = self.chat
        self.stack.addWidget(self.chat)

        self.voice_page = VoicePage()
        self.voice_page.tts_save.connect(self.tts_save.emit)
        self.voice_page.tts_reset.connect(self.tts_reset.emit)
        self.voice_page.tts_test.connect(self.tts_test.emit)
        self.voice_page.pronunciation_add.connect(self.pronunciation_add.emit)
        self.voice_page.pronunciation_delete.connect(self.pronunciation_delete.emit)
        self.voice_page.voice_tune.connect(self.voice_tune.emit)
        self.voice_page.voice_diag_reset.connect(self.voice_diag_reset.emit)
        self.pages["voice"] = self.voice_page
        self.stack.addWidget(self.voice_page)

        for name in (
            "tts_meta", "tts_piper", "tts_model", "tts_speaker", "tts_status",
            "pronunciation_source", "pronunciation_target", "pronunciation_list",
            "pronunciation_status", "voice_diag_meta", "voice_level",
            "voice_threshold", "voice_floor", "voice_vad", "voice_multiplier",
            "voice_margin", "voice_alpha", "voice_diag_status",
        ):
            setattr(self, name, getattr(self.voice_page, name))

        today = TextPage("Сегодня", "Личные дела и краткий контекст дня.")
        today.refresh_requested.connect(lambda: self.refresh_page.emit("today"))
        self.pages["today"] = today
        self.stack.addWidget(today)

        tasks = TasksPage()
        tasks.refresh_requested.connect(lambda: self.refresh_page.emit("tasks"))
        tasks.decision.connect(self.task_decision)
        tasks.command.connect(self.task_command)
        tasks.plan_preview.connect(self.plan_preview)
        tasks.plan_command.connect(self.plan_command)
        tasks.replan_preview.connect(self.replan_preview)
        tasks.replan_command.connect(self.replan_command)
        self.pages["tasks"] = tasks
        self.stack.addWidget(tasks)

        activity = ActivityPage()
        activity.refresh_requested.connect(lambda: self.refresh_page.emit("activity"))
        self.pages["activity"] = activity
        self.stack.addWidget(activity)

        defs = {
            "memory": ("Память", "Факты, предпочтения и то, что вы просили запомнить."),
            "learning": ("Обучение", "Чему Люма научилась и что пока не понимает."),
            "journal": ("Журнал", "Фактические действия ассистента на компьютере."),
        }
        for key, (title, subtitle) in defs.items():
            page = TextPage(title, subtitle)
            page.refresh_requested.connect(lambda k=key: self.refresh_page.emit(k))
            self.pages[key] = page
            self.stack.addWidget(page)

        skills_page = SkillsPage()
        skills_page.refresh_requested.connect(lambda: self.refresh_page.emit("skills"))
        self.pages["skills"] = skills_page
        self.stack.addWidget(skills_page)

        settings = QWidget()
        sl = QVBoxLayout(settings)
        title = QLabel("Настройки")
        title.setObjectName("pageTitle")
        sl.addWidget(title)
        self.settings_status = QLabel("Backend: …")
        self.settings_status.setWordWrap(True)
        sl.addWidget(self.settings_status)
        self.settings_hotkey = QLabel(
            "Быстрая команда: Ctrl + Shift + Space · STOP ALL: Ctrl + Alt + Shift + Space"
        )
        sl.addWidget(self.settings_hotkey)
        controls = QHBoxLayout()
        self.mic_button = QPushButton("🎤 Включить wake word")
        self.mic_button.clicked.connect(self.mic_toggle)
        controls.addWidget(self.mic_button)
        self.stop_all_button = QPushButton("⛔ STOP ALL")
        self.stop_all_button.setObjectName("danger")
        self.stop_all_button.clicked.connect(self.safety_toggle)
        controls.addWidget(self.stop_all_button)
        sl.addLayout(controls)

        voice_link = QLabel("Голос, Baya, произношение и диагностика вынесены в отдельный раздел «Голос».")
        voice_link.setObjectName("muted")
        voice_link.setWordWrap(True)
        sl.addWidget(voice_link)

        persona_title = QLabel("Persona Люмы")
        persona_title.setStyleSheet("font-size:16px; font-weight:700; margin-top:12px;")
        sl.addWidget(persona_title)
        self.persona_boxes: dict[str, QComboBox] = {}
        persona_fields = (
            ("address", "Обращение"),
            ("verbosity", "Подробность"),
            ("humor", "Юмор"),
            ("initiative", "Инициативность"),
            ("relationship", "Тон отношений"),
        )
        for key, label_text in persona_fields:
            row = QHBoxLayout()
            label = QLabel(label_text)
            label.setMinimumWidth(130)
            combo = QComboBox()
            combo.setObjectName("persona_" + key)
            row.addWidget(label)
            row.addWidget(combo, 1)
            sl.addLayout(row)
            self.persona_boxes[key] = combo
        persona_buttons = QHBoxLayout()
        save_persona = QPushButton("Сохранить стиль")
        save_persona.setObjectName("persona_save")
        save_persona.clicked.connect(self._emit_persona_save)
        reset_persona = QPushButton("По умолчанию")
        reset_persona.setObjectName("persona_reset")
        reset_persona.clicked.connect(lambda: self.persona_reset.emit())
        persona_buttons.addWidget(save_persona)
        persona_buttons.addWidget(reset_persona)
        persona_buttons.addStretch(1)
        sl.addLayout(persona_buttons)
        self.persona_status = QLabel("Загрузка профиля…")
        self.persona_status.setObjectName("muted")
        sl.addWidget(self.persona_status)
        autonomy_title = QLabel("Autonomy")
        autonomy_title.setStyleSheet("font-size:16px; font-weight:700; margin-top:12px;")
        sl.addWidget(autonomy_title)

        level_row = QHBoxLayout()
        level_label = QLabel("Глобальный уровень")
        level_label.setMinimumWidth(130)
        self.autonomy_level = QComboBox()
        self.autonomy_level.setObjectName("autonomy_level")
        level_row.addWidget(level_label)
        level_row.addWidget(self.autonomy_level, 1)
        sl.addLayout(level_row)

        self.autonomy_provider_boxes: dict[str, QComboBox] = {}
        for provider_name, title_text in (
            ("browser", "Browser"),
            ("desktop", "Desktop"),
            ("printflow", "PrintFlow"),
            ("personal", "Personal"),
            ("core", "Core"),
        ):
            row = QHBoxLayout()
            label = QLabel(title_text)
            label.setMinimumWidth(130)
            combo = QComboBox()
            combo.setObjectName("autonomy_provider_" + provider_name)
            row.addWidget(label)
            row.addWidget(combo, 1)
            sl.addLayout(row)
            self.autonomy_provider_boxes[provider_name] = combo

        autonomy_buttons = QHBoxLayout()
        save_autonomy = QPushButton("Сохранить автономность")
        save_autonomy.setObjectName("autonomy_save")
        save_autonomy.clicked.connect(self._emit_autonomy_save)
        reset_autonomy = QPushButton("По умолчанию")
        reset_autonomy.setObjectName("autonomy_reset")
        reset_autonomy.clicked.connect(lambda: self.autonomy_reset.emit())
        autonomy_buttons.addWidget(save_autonomy)
        autonomy_buttons.addWidget(reset_autonomy)
        autonomy_buttons.addStretch(1)
        sl.addLayout(autonomy_buttons)

        self.autonomy_status = QLabel("Загрузка policy…")
        self.autonomy_status.setObjectName("muted")
        self.autonomy_status.setWordWrap(True)
        sl.addWidget(self.autonomy_status)

        proactivity_title = QLabel("Proactivity")
        proactivity_title.setStyleSheet("font-size:16px; font-weight:700; margin-top:12px;")
        sl.addWidget(proactivity_title)

        pro_mode_row = QHBoxLayout()
        pro_mode_label = QLabel("Режим")
        pro_mode_label.setMinimumWidth(130)
        self.proactivity_mode = QComboBox()
        self.proactivity_mode.setObjectName("proactivity_mode")
        pro_mode_row.addWidget(pro_mode_label)
        pro_mode_row.addWidget(self.proactivity_mode, 1)
        sl.addLayout(pro_mode_row)

        pro_budget_row = QHBoxLayout()
        pro_budget_label = QLabel("Несрочных / час")
        pro_budget_label.setMinimumWidth(130)
        self.proactivity_budget = QComboBox()
        self.proactivity_budget.setObjectName("proactivity_budget")
        for value in (0, 1, 2, 3, 5, 10):
            self.proactivity_budget.addItem(str(value), value)
        pro_budget_row.addWidget(pro_budget_label)
        pro_budget_row.addWidget(self.proactivity_budget, 1)
        sl.addLayout(pro_budget_row)

        pro_cooldown_row = QHBoxLayout()
        pro_cooldown_label = QLabel("Cooldown")
        pro_cooldown_label.setMinimumWidth(130)
        self.proactivity_cooldown = QComboBox()
        self.proactivity_cooldown.setObjectName("proactivity_cooldown")
        for value in (5, 10, 15, 30, 60, 120):
            self.proactivity_cooldown.addItem(f"{value} мин", value)
        pro_cooldown_row.addWidget(pro_cooldown_label)
        pro_cooldown_row.addWidget(self.proactivity_cooldown, 1)
        sl.addLayout(pro_cooldown_row)

        quiet_row = QHBoxLayout()
        quiet_label = QLabel("Тихие часы")
        quiet_label.setMinimumWidth(130)
        self.proactivity_quiet_start = QLineEdit()
        self.proactivity_quiet_start.setObjectName("proactivity_quiet_start")
        self.proactivity_quiet_start.setPlaceholderText("22:00")
        self.proactivity_quiet_start.setMaximumWidth(90)
        self.proactivity_quiet_end = QLineEdit()
        self.proactivity_quiet_end.setObjectName("proactivity_quiet_end")
        self.proactivity_quiet_end.setPlaceholderText("08:00")
        self.proactivity_quiet_end.setMaximumWidth(90)
        quiet_row.addWidget(quiet_label)
        quiet_row.addWidget(self.proactivity_quiet_start)
        quiet_row.addWidget(QLabel("→"))
        quiet_row.addWidget(self.proactivity_quiet_end)
        quiet_row.addStretch(1)
        sl.addLayout(quiet_row)

        pro_buttons = QHBoxLayout()
        save_proactivity = QPushButton("Сохранить инициативность")
        save_proactivity.setObjectName("proactivity_save")
        save_proactivity.clicked.connect(self._emit_proactivity_save)
        reset_proactivity = QPushButton("По умолчанию")
        reset_proactivity.setObjectName("proactivity_reset")
        reset_proactivity.clicked.connect(lambda: self.proactivity_reset.emit())
        pro_buttons.addWidget(save_proactivity)
        pro_buttons.addWidget(reset_proactivity)
        pro_buttons.addStretch(1)
        sl.addLayout(pro_buttons)

        self.proactivity_status = QLabel("Загрузка Event Engine…")
        self.proactivity_status.setObjectName("muted")
        self.proactivity_status.setWordWrap(True)
        sl.addWidget(self.proactivity_status)

        self.events_view = QTextBrowser()
        self.events_view.setMaximumHeight(150)
        self.events_view.setPlainText("Событий пока нет.")
        sl.addWidget(self.events_view)
        providers_title = QLabel("Providers")
        providers_title.setStyleSheet("font-size:16px; font-weight:700; margin-top:12px;")
        sl.addWidget(providers_title)
        self.providers_view = QTextBrowser()
        self.providers_view.setMaximumHeight(220)
        self.providers_view.setPlainText("Загрузка…")
        sl.addWidget(self.providers_view)
        note = QLabel(
            "Нативный интерфейс не содержит мозг ассистента. Он подключается "
            "к локальному agent core на 127.0.0.1; старый /ui остаётся fallback."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        sl.addWidget(note)
        sl.addStretch(1)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.NoFrame)
        settings_scroll.setWidget(settings)
        self.pages["settings"] = settings_scroll
        self.stack.addWidget(settings_scroll)

        main_layout.addWidget(self.stack, 1)
        body.addWidget(main, 1)
        outer.addLayout(body, 1)

        self.footer = QLabel("● подключение…")
        self.footer.setObjectName("footer")
        outer.addWidget(self.footer)

        self.nav.currentRowChanged.connect(self._change)
        self.nav.setCurrentRow(0)
        self.setStyleSheet(theme.stylesheet())

    def _change(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        key = self.nav.item(row).data(Qt.UserRole)
        if key not in ("home", "chat", "voice"):
            self.refresh_page.emit(str(key))

    def set_pronunciation_payload(self, payload: dict[str, Any]) -> None:
        items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
        self.pronunciation_list.clear()
        for source, target in sorted(items.items(), key=lambda pair: str(pair[0]).casefold()):
            item = QListWidgetItem(f"{source}  →  {target}")
            item.setData(Qt.UserRole, str(source))
            self.pronunciation_list.addItem(item)
        custom = int(payload.get("custom_terms") or len(items))
        builtin = int(payload.get("builtin_terms") or 0)
        path = str(payload.get("dictionary_path") or "")
        suffix = f" · встроенных {builtin}" if builtin else ""
        if path:
            suffix += f" · {path}"
        self.pronunciation_status.setText(f"Пользовательских правил: {custom}{suffix}")

    def clear_pronunciation_inputs(self) -> None:
        self.pronunciation_source.clear()
        self.pronunciation_target.clear()

    def set_tts_payload(self, payload: dict[str, Any]) -> None:
        engine = str(payload.get("engine") or "не найден")
        hq = bool(payload.get("hq_local"))
        model = str(payload.get("model") or "")
        ready = bool(payload.get("model_ready"))
        synth_ms = int(payload.get("last_synth_ms") or 0)
        last_chars = int(payload.get("last_chars") or 0)
        latency = f" · synth {synth_ms} ms/{last_chars} chars" if synth_ms and last_chars else ""
        speaker = str(payload.get("speaker") or "")
        sample_rate = int(payload.get("sample_rate") or 0)
        voice = f" · {speaker}" if speaker else ""
        rate = f" · {sample_rate // 1000} kHz" if sample_rate else ""
        self.tts_meta.setText(
            f"TTS: {engine} · HQ {'✓' if hq else '–'} · "
            f"model {'✓' if ready else '–'}" + (f" · {model}" if model else "") + voice + rate + latency
        )
        editing = any(widget.hasFocus() for widget in (self.tts_piper, self.tts_model, self.tts_speaker))
        if not editing:
            self.tts_piper.setText(str(payload.get("piper_path") or ""))
            self.tts_model.setText(str(payload.get("piper_model_path") or payload.get("model_path") or ""))
            self.tts_speaker.setText(str(payload.get("piper_speaker") or ""))
        self.home.set_voice(
            engine=engine,
            speaker=speaker,
            sample_rate=sample_rate,
            model=model,
            ready=ready,
        )
        self.voice_page.set_voice_profile(
            engine=engine,
            speaker=speaker,
            sample_rate=sample_rate,
            model=model,
            ready=ready,
        )

    def set_tts_message(self, text: str) -> None:
        self.tts_status.setText(str(text or ""))

    def set_voice_diagnostics(self, *, audio_level: int, echo_floor: int,
                              echo_threshold: int, echo_suppressed: int,
                              multiplier: float, margin: int, alpha: float,
                              vad_threshold: int, asr_engine: str,
                              vocabulary_count: int) -> None:
        top = max(4000, int(audio_level), int(echo_floor), int(echo_threshold), int(vad_threshold))
        for bar in (self.voice_level, self.voice_floor, self.voice_threshold):
            bar.setMaximum(top)
        self.voice_level.setValue(max(0, int(audio_level)))
        self.voice_floor.setValue(max(0, int(echo_floor)))
        self.voice_threshold.setValue(max(0, int(echo_threshold)))
        editing = any(
            widget.hasFocus()
            for widget in (self.voice_vad, self.voice_multiplier, self.voice_margin, self.voice_alpha)
        )
        if not editing:
            self.voice_vad.blockSignals(True)
            self.voice_multiplier.blockSignals(True)
            self.voice_margin.blockSignals(True)
            self.voice_alpha.blockSignals(True)
            self.voice_vad.setValue(int(vad_threshold))
            self.voice_multiplier.setValue(float(multiplier))
            self.voice_margin.setValue(int(margin))
            self.voice_alpha.setValue(float(alpha))
            self.voice_vad.blockSignals(False)
            self.voice_multiplier.blockSignals(False)
            self.voice_margin.blockSignals(False)
            self.voice_alpha.blockSignals(False)
        engine = str(asr_engine or "не загружен")
        self.voice_diag_meta.setText(
            f"ASR: {engine} · vocabulary: {int(vocabulary_count)} · "
            f"VAD: {int(vad_threshold)} · echo suppressed: {int(echo_suppressed)}"
        )

    def set_voice_diagnostics_message(self, text: str) -> None:
        self.voice_diag_status.setText(str(text or ""))

    def set_home_runtime(self, *, connected: bool, state: str, audio_level: int = 0,
                         heard: str = "", reply: str = "", skill: str = "",
                         detail: str = "", task_id: int = 0,
                         safety_stopped: bool = False) -> None:
        self.home.set_runtime(
            connected=connected,
            state=state,
            audio_level=audio_level,
            heard=heard,
            reply=reply,
            skill=skill,
            detail=detail,
            task_id=task_id,
            safety_stopped=safety_stopped,
        )
        self.voice_page.set_runtime_state("stopped" if safety_stopped else state, audio_level)

    def update_status(self, connected: bool, armed: bool, model_ok: bool,
                      panel_ok: bool, error: str = "", safety_stopped: bool = False,
                      assistant_state: str = "idle") -> None:
        self.brand_card.set_online(connected)
        self.status_header.set_status(
            connected=connected,
            armed=armed,
            model_ok=model_ok,
            panel_ok=panel_ok,
            safety_stopped=safety_stopped,
            error=error,
            assistant_state=assistant_state,
        )
        if connected:
            bits = ["Люма ✓", "модель ✓" if model_ok else "модель –",
                    "PrintFlow ✓" if panel_ok else "PrintFlow –",
                    "wake ✓" if armed else "wake –",
                    "STOP ALL" if safety_stopped else "готова"]
            self.footer.setText("   ".join(bits))
            self.settings_status.setText("Backend: подключён · 127.0.0.1:8799")
        else:
            self.footer.setText("Агент недоступен" + (f": {error}" if error else ""))
            self.settings_status.setText("Backend: недоступен" + (f" · {error}" if error else ""))
        self.mic_button.setText("🎤 Выключить wake word" if armed else "🎤 Включить wake word")
        self.stop_all_button.setText("▶ Снять STOP ALL" if safety_stopped else "⛔ STOP ALL")

    def _emit_persona_save(self) -> None:
        profile = {
            key: str(combo.currentData() or "")
            for key, combo in self.persona_boxes.items()
        }
        self.persona_save.emit(profile)

    def _emit_autonomy_save(self) -> None:
        level = str(self.autonomy_level.currentData() or self.autonomy_level.currentText())
        providers = {
            key: str(combo.currentData() or combo.currentText())
            for key, combo in self.autonomy_provider_boxes.items()
        }
        self.autonomy_save.emit(level, providers)

    def set_autonomy_payload(self, payload: dict[str, Any]) -> None:
        levels = [str(value) for value in (payload.get("levels") or [])]
        labels = dict(payload.get("labels") or {})
        current = str(payload.get("level") or "agent")
        self.autonomy_level.blockSignals(True)
        self.autonomy_level.clear()
        for value in levels:
            self.autonomy_level.addItem(str(labels.get(value) or value), value)
        index = self.autonomy_level.findData(current)
        if index >= 0:
            self.autonomy_level.setCurrentIndex(index)
        self.autonomy_level.blockSignals(False)

        provider_rows = dict(payload.get("providers") or {})
        for name, combo in self.autonomy_provider_boxes.items():
            row = dict(provider_rows.get(name) or {})
            hard = str(row.get("hard_max") or "agent")
            selected = str(row.get("level") or hard)
            combo.clear()
            hard_index = levels.index(hard) if hard in levels else len(levels) - 1
            for value in levels[:hard_index + 1]:
                combo.addItem(str(labels.get(value) or value), value)
            idx = combo.findData(selected)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        self.autonomy_status.setText(str(payload.get("invariant") or ""))

    def set_autonomy_message(self, text: str) -> None:
        self.autonomy_status.setText(str(text or ""))

    def _emit_proactivity_save(self) -> None:
        self.proactivity_save.emit({
            "mode": str(self.proactivity_mode.currentData() or self.proactivity_mode.currentText()),
            "max_nonurgent_per_hour": int(self.proactivity_budget.currentData() or 0),
            "cooldown_minutes": int(self.proactivity_cooldown.currentData() or 30),
            "quiet_start": self.proactivity_quiet_start.text().strip(),
            "quiet_end": self.proactivity_quiet_end.text().strip(),
        })

    def set_proactivity_payload(self, payload: dict[str, Any]) -> None:
        settings = dict(payload.get("settings") or {})
        modes = [str(value) for value in (payload.get("modes") or [])]
        labels = {
            "silent": "Silent · только явные напоминания",
            "important": "Important only",
            "balanced": "Balanced",
            "active": "Active",
        }
        self.proactivity_mode.clear()
        for value in modes:
            self.proactivity_mode.addItem(labels.get(value, value), value)
        idx = self.proactivity_mode.findData(str(settings.get("mode") or "balanced"))
        if idx >= 0:
            self.proactivity_mode.setCurrentIndex(idx)

        for combo, key, default in (
            (self.proactivity_budget, "max_nonurgent_per_hour", 2),
            (self.proactivity_cooldown, "cooldown_minutes", 30),
        ):
            raw = settings.get(key)
            value = default if raw is None or raw == "" else int(raw)
            idx = combo.findData(value)
            if idx < 0:
                combo.addItem(str(value) if key == "max_nonurgent_per_hour" else f"{value} мин", value)
                idx = combo.findData(value)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        self.proactivity_quiet_start.setText(str(settings.get("quiet_start") or "22:00"))
        self.proactivity_quiet_end.setText(str(settings.get("quiet_end") or "08:00"))

        autopilot = bool(payload.get("autopilot"))
        prefix = "Autopilot включён." if autopilot else "Autopilot выключен: инициативные события журналируются, но не прерывают пользователя."
        self.proactivity_status.setText(prefix + " " + str(payload.get("invariant") or ""))

        rows = list(payload.get("recent") or [])
        lines = []
        for row in rows[:12]:
            state = str(row.get("state") or "new")
            title = str(row.get("title") or row.get("kind") or "event")
            reason = str(row.get("reason") or "")
            line = f"[{state}] {title}"
            if reason:
                line += f"\n  {reason}"
            lines.append(line)
        self.events_view.setPlainText("\n\n".join(lines) if lines else "Событий пока нет.")

    def set_proactivity_message(self, text: str) -> None:
        self.proactivity_status.setText(str(text or ""))

    def set_persona_payload(self, payload: dict[str, Any]) -> None:
        profile = dict(payload.get("profile") or {})
        options = dict(payload.get("options") or {})
        labels = dict(payload.get("labels") or {})
        for key, combo in self.persona_boxes.items():
            current = str(profile.get(key) or "")
            combo.blockSignals(True)
            combo.clear()
            for value in options.get(key) or []:
                title = str((labels.get(key) or {}).get(value) or value)
                combo.addItem(title, value)
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.blockSignals(False)
        self.persona_status.setText("Стиль влияет только на форму ответа, не на права и подтверждения.")

    def set_persona_message(self, text: str) -> None:
        self.persona_status.setText(str(text or ""))

    def set_providers_payload(self, payload: dict[str, Any]) -> None:
        rows = list(payload.get("providers") or [])
        if not rows:
            self.providers_view.setPlainText("Providers пока не зарегистрированы.")
            return
        lines = []
        for row in rows:
            mark = "✓" if row.get("available") else "–"
            title = str(row.get("title") or row.get("name") or "provider")
            skill_names = [str(item.get("name") or "") for item in (row.get("skills") or [])]
            detail = ", ".join(skill_names) if skill_names else "skills не перенесены"
            line = f"{mark} {title}: {detail}"
            if not row.get("available") and row.get("reason"):
                line += f"\n  {row.get('reason')}"
            lines.append(line)
        self.providers_view.setPlainText("\n\n".join(lines))

    def set_planner_message(self, text: str) -> None:
        page = self.pages.get("tasks")
        if isinstance(page, TasksPage):
            page.set_planner_message(text)

    def set_page_payload(self, key: str, payload: Any) -> None:
        page = self.pages.get(key)
        if isinstance(page, TextPage):
            page.set_payload(payload)
        elif isinstance(page, TasksPage) and isinstance(payload, dict):
            page.set_payload(payload)
        elif isinstance(page, ActivityPage) and isinstance(payload, dict):
            page.set_payload(payload)
        elif isinstance(page, SkillsPage) and isinstance(payload, dict):
            page.set_payload(payload)

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
