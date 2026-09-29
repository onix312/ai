"""Visual tokens and QSS for Luma's native neon-glass interface."""
from __future__ import annotations

COLORS = {
    "bg": "#070816",
    "bg_alt": "#0B0D1D",
    "surface": "#111328",
    "surface_2": "#171A34",
    "surface_hover": "#1C2040",
    "border": "#292D52",
    "border_bright": "#454B83",
    "text": "#F4F2FF",
    "muted": "#9996B7",
    "violet": "#8B5CF6",
    "violet_bright": "#A78BFA",
    "cyan": "#43D7FF",
    "mint": "#58E6B1",
    "amber": "#F5B84C",
    "danger": "#F05266",
}


def stylesheet() -> str:
    return """
    QMainWindow, QWidget {
        background: #070816;
        color: #F4F2FF;
        font-size: 14px;
    }

    QWidget#shellRoot {
        background: transparent;
    }

    QWidget#sidebar {
        background: rgba(12, 13, 30, 235);
        border-right: 1px solid #292D52;
    }

    QFrame#brandCard {
        background: #111328;
        border: 1px solid #343861;
        border-radius: 16px;
    }

    QLabel#brand {
        background: transparent;
        color: #F8F7FF;
        font-size: 22px;
        font-weight: 800;
        letter-spacing: 2px;
    }

    QLabel#brandSub {
        background: transparent;
        color: #8F8BAE;
        font-size: 11px;
    }

    QLabel#onlineDot {
        background: #58E6B1;
        border: 2px solid #163B35;
        border-radius: 6px;
        min-width: 12px;
        max-width: 12px;
        min-height: 12px;
        max-height: 12px;
    }

    QListWidget#nav {
        background: transparent;
        border: 0;
        outline: 0;
        padding: 2px;
    }

    QListWidget#nav::item {
        padding: 12px 14px;
        border-radius: 12px;
        margin: 3px 0;
        color: #A9A6C4;
    }

    QListWidget#nav::item:hover {
        background: #171A34;
        color: #F4F2FF;
    }

    QListWidget#nav::item:selected {
        background: #242044;
        color: #FFFFFF;
        border: 1px solid #6654B8;
    }

    QFrame#topBar {
        background: #0F1124;
        border: 1px solid #292D52;
        border-radius: 16px;
    }

    QLabel#statusTitle {
        color: #F7F5FF;
        font-size: 15px;
        font-weight: 700;
    }

    QLabel#statusSub, QLabel#muted {
        color: #9996B7;
    }

    QLabel#statusPill {
        background: #1B1737;
        color: #C7B8FF;
        border: 1px solid #4A3B81;
        border-radius: 10px;
        padding: 5px 10px;
        font-weight: 600;
    }

    QLabel#localPill {
        background: #10282A;
        color: #89F2CE;
        border: 1px solid #265956;
        border-radius: 10px;
        padding: 5px 10px;
        font-weight: 600;
    }

    QLabel#pageTitle {
        color: #F8F7FF;
        font-size: 26px;
        font-weight: 750;
    }

    QLabel#heroKicker {
        color: #7E74A8;
        font-size: 10px;
        font-weight: 700;
        letter-spacing: 2px;
    }

    QLabel#heroTitle {
        color: #FFFFFF;
        font-size: 38px;
        font-weight: 850;
        letter-spacing: 1px;
    }

    QLabel#heroDescription {
        color: #AAA6C3;
        font-size: 14px;
        line-height: 1.35;
    }

    QFrame#portraitFrame {
        background: #15152F;
        border: 1px solid #4B4380;
        border-radius: 24px;
    }

    QLabel#portraitMonogram {
        background: transparent;
        color: #BFA9FF;
        font-size: 56px;
        font-weight: 800;
    }

    QLabel#portraitCaption {
        background: transparent;
        color: #726D95;
        font-size: 9px;
        font-weight: 700;
        letter-spacing: 2px;
    }

    QLabel#orbStateTitle {
        background: transparent;
        color: #FFFFFF;
        font-size: 18px;
        font-weight: 750;
    }

    QLabel#metricLabel {
        background: transparent;
        color: #777493;
        font-size: 9px;
        font-weight: 700;
        letter-spacing: 1px;
    }

    QLabel#metricValue {
        background: transparent;
        color: #FFFFFF;
        font-size: 21px;
        font-weight: 750;
    }

    QLabel#metricValueSmall {
        background: transparent;
        color: #ECE9F8;
        font-size: 14px;
        font-weight: 650;
    }

    QLabel#skillMetric, QLabel#skillMetricReady {
        background: transparent;
        color: #F5F2FF;
        font-size: 24px;
        font-weight: 800;
    }

    QLabel#skillMetricReady {
        color: #78EDC6;
    }

    QTextBrowser#skillsBrowser {
        background: #0D0F20;
        border: 1px solid #25294A;
        border-radius: 14px;
        padding: 4px;
    }

    QLabel#sectionTitle {
        background: transparent;
        color: #F5F2FF;
        font-size: 15px;
        font-weight: 700;
    }

    QLabel#chatLive {
        background: transparent;
        color: #ECE9F8;
        font-size: 13px;
        font-weight: 650;
    }

    QLabel#tracePill {
        background: #132A2C;
        color: #79E9C2;
        border: 1px solid #285D58;
        border-radius: 9px;
        padding: 3px 8px;
        font-size: 9px;
        font-weight: 750;
        letter-spacing: 1px;
    }

    QLabel#actionTrace {
        background: transparent;
        color: #C9C4E3;
        font-size: 12px;
        line-height: 1.35;
        padding: 2px 1px;
    }

    QLabel#taskTitle {
        background: transparent;
        color: #FFFFFF;
        font-size: 16px;
        font-weight: 750;
    }

    QLabel#taskStatus {
        background: #1B1737;
        color: #C7B8FF;
        border: 1px solid #4A3B81;
        border-radius: 9px;
        padding: 4px 8px;
        font-size: 9px;
        font-weight: 750;
        letter-spacing: 1px;
    }

    QLabel#taskStep {
        background: #0E1122;
        color: #AAA6C3;
        border-left: 2px solid #30345A;
        border-radius: 8px;
        padding: 7px 10px;
    }

    QLabel#taskStepCurrent {
        background: #171634;
        color: #F5F2FF;
        border-left: 3px solid #43D7FF;
        border-radius: 8px;
        padding: 8px 10px;
        font-weight: 650;
    }

    QLabel#taskNow {
        background: #10272B;
        color: #8BEACD;
        border: 1px solid #255A54;
        border-radius: 9px;
        padding: 7px 10px;
        font-weight: 650;
    }

    QLabel#taskError {
        background: #2A131C;
        color: #FFB7C3;
        border: 1px solid #6A2838;
        border-radius: 9px;
        padding: 7px 10px;
    }

    QLabel#voiceChain {
        background: #15162D;
        color: #B9AEF5;
        border: 1px solid #3B3E67;
        border-radius: 10px;
        padding: 9px 12px;
        font-size: 11px;
        font-weight: 750;
        letter-spacing: 1px;
    }

    QTextBrowser#chatFeed {
        background: #0D0F20;
        border: 0;
        border-radius: 12px;
        padding: 4px;
    }

    QLineEdit#chatInput {
        background: transparent;
        border: 0;
        padding: 11px 12px;
        font-size: 15px;
    }

    QLineEdit#chatInput:focus {
        background: transparent;
        border: 0;
    }

    QLabel#welcomeTitle {
        color: #FFFFFF;
        font-size: 34px;
        font-weight: 800;
    }

    QLabel#welcomeText {
        color: #A6A2C0;
        font-size: 15px;
    }

    QFrame#glassCard {
        background: #111328;
        border: 1px solid #30345A;
        border-radius: 16px;
    }

    QFrame#glassCard[accent="violet"] {
        border: 1px solid #5E4AA3;
    }

    QFrame#glassCard[accent="cyan"] {
        border: 1px solid #28758B;
    }

    QFrame#glassCard[accent="amber"] {
        border: 1px solid #7C5A27;
    }

    QTextBrowser, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
        background: #101225;
        color: #F0EEFA;
        border: 1px solid #30345A;
        border-radius: 11px;
        padding: 9px 11px;
        selection-background-color: #6E56CF;
    }

    QTextBrowser:focus, QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus,
    QSpinBox:focus, QDoubleSpinBox:focus {
        border: 1px solid #7D63E6;
        background: #13152B;
    }

    QScrollArea {
        border: 0;
        background: transparent;
    }

    QScrollBar:vertical {
        background: transparent;
        width: 8px;
        margin: 2px;
    }

    QScrollBar::handle:vertical {
        background: #34365A;
        border-radius: 4px;
        min-height: 28px;
    }

    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0;
    }

    QPushButton {
        background: #1A1D37;
        color: #EAE7F7;
        border: 1px solid #30345A;
        border-radius: 10px;
        padding: 9px 13px;
        font-weight: 600;
    }

    QPushButton:hover {
        background: #232746;
        border-color: #4B507E;
    }

    QPushButton:pressed {
        background: #15182E;
    }

    QPushButton#primary {
        background: #6847D8;
        color: #FFFFFF;
        border: 1px solid #8B6EF0;
    }

    QPushButton#primary:hover {
        background: #7554E5;
        border-color: #A78BFA;
    }

    QPushButton#suggestion {
        background: #12162B;
        border: 1px solid #343861;
        border-radius: 14px;
        padding: 15px 18px;
        text-align: left;
    }

    QPushButton#suggestion:hover {
        background: #1A1E39;
        border-color: #6654B8;
    }

    QPushButton#danger {
        background: #35141E;
        color: #FFBCC8;
        border: 1px solid #7A2A3D;
        font-weight: 700;
    }

    QPushButton#danger:hover {
        background: #4A1826;
        border-color: #D44961;
    }

    QProgressBar {
        background: #101225;
        border: 1px solid #2E3154;
        border-radius: 7px;
        min-height: 10px;
        max-height: 10px;
        text-align: center;
    }

    QProgressBar::chunk {
        background: #795AE2;
        border-radius: 6px;
    }

    QProgressBar#taskProgress {
        min-height: 7px;
        max-height: 7px;
        border-radius: 4px;
        background: #0D1020;
        border: 0;
    }

    QProgressBar#taskProgress::chunk {
        background: #6E56CF;
        border-radius: 4px;
    }

    QLabel#footer {
        background: #090A17;
        color: #777493;
        border-top: 1px solid #20233F;
        padding: 7px 16px;
        font-size: 11px;
    }
    """
