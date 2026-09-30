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


def stylesheet(accent: str = "#8B5CF6") -> str:
    css = """
    QMainWindow {
        background: #070816;
    }

    QWidget {
        color: #F4F2FF;
        font-size: 14px;
    }

    QWidget#shellRoot {
        background: transparent;
    }

    QWidget#settingsSurface {
        background: rgba(8, 11, 29, 178);
    }

    QTabBar#appearanceTabs {
        background: #0F142D;
        border: 1px solid #36355E;
        border-radius: 12px;
    }

    QTabBar#appearanceTabs::tab {
        background: transparent;
        color: #B8B2D2;
        min-height: 30px;
        padding: 5px 12px;
    }

    QTabBar#appearanceTabs::tab:selected {
        background: #302269;
        color: #FFFFFF;
        border: 1px solid #9A70FF;
        border-radius: 10px;
    }

    QPushButton#panelStyle {
        background: #141832;
        border: 1px solid #383D68;
        border-radius: 10px;
        color: #C7C1E0;
        min-height: 30px;
    }

    QPushButton#panelStyle:checked {
        background: #302269;
        border: 1px solid #A77EFF;
        color: #FFFFFF;
    }

    QWidget#sidebar {
        background: #090E24;
        border-right: 1px solid #59439A;
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
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #6846C8,stop:1 #252D68);
        color: #FFFFFF;
        border: 1px solid #9A79FF;
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

    QScrollArea#chatBubbleScroll, QScrollArea#chatBubbleScroll > QWidget > QWidget {
        background: #0D0F20;
        border: 0;
    }

    QFrame#chatBubbleUser {
        background: #3348B9;
        border: 1px solid #6684FF;
        border-radius: 14px;
    }

    QFrame#chatBubbleAssistant {
        background: #151B35;
        border: 1px solid #38406D;
        border-radius: 14px;
    }

    QLabel#chatBubbleText {
        background: transparent;
        color: #F5F3FF;
        font-size: 14px;
    }

    QLabel#chatBubbleMeta {
        background: transparent;
        color: #B9A7FF;
        font-size: 10px;
        font-weight: 700;
    }

    QLabel#homeClock {
        color: #D7A9FF;
        font-size: 29px;
        font-weight: 800;
    }

    QPushButton#heroAction {
        background: #1A1B43;
        border: 1px solid #6C59B4;
        border-radius: 11px;
        color: #F1EDFF;
        font-size: 13px;
        font-weight: 650;
        padding: 6px 10px;
        text-align: left;
    }

    QPushButton#heroAction:hover {
        background: #342A72;
        border-color: #BDA4FF;
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

    QLabel#systemMeterName, QLabel#systemMeterValue {
        background: transparent;
        color: #BBB4D9;
        font-size: 11px;
    }

    QProgressBar#systemMeter {
        min-height: 5px;
        max-height: 5px;
        border: 0;
        border-radius: 3px;
        background: #252951;
    }

    QProgressBar#systemMeter::chunk {
        border-radius: 3px;
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #43D7FF,stop:1 #B27AFF);
    }

    QLabel#opsMetric, QLabel#opsMetricReady, QLabel#opsMetricDanger {
        background: transparent;
        color: #F5F2FF;
        font-size: 22px;
        font-weight: 800;
    }

    QLabel#opsMetricReady {
        color: #78EDC6;
    }

    QLabel#opsMetricDanger {
        color: #F5B84C;
    }

    QLabel#memoryText {
        background: transparent;
        color: #F7F5FF;
        font-size: 15px;
        font-weight: 650;
    }

    QLabel#memoryConfidence {
        background: #10272B;
        color: #8BEACD;
        border: 1px solid #255A54;
        border-radius: 9px;
        padding: 3px 8px;
        font-size: 10px;
        font-weight: 750;
    }

    QLabel#memoryMeta {
        background: #0F1124;
        color: #777493;
        border-left: 2px solid #343861;
        border-radius: 7px;
        padding: 5px 8px;
        font-size: 10px;
    }

    QLabel#settingsState {
        background: transparent;
        color: #F5F2FF;
        font-size: 14px;
        font-weight: 650;
        padding: 3px 0;
    }

    QLabel#learningMeaning {
        background: #0F1124;
        color: #C9C4E3;
        border-left: 2px solid #5E4AA3;
        border-radius: 7px;
        padding: 7px 9px;
    }

    QLabel#learningAlias {
        background: transparent;
        color: #EDE9FF;
        font-size: 13px;
        font-weight: 650;
    }

    QLabel#learningInsight {
        background: transparent;
        color: #F5F2FF;
        font-size: 13px;
        font-weight: 650;
    }

    QLabel#todayListItem {
        background: transparent;
        color: #D8D4E8;
        font-size: 12px;
        padding: 3px 0;
    }

    QPushButton#memoryPin {
        background: #151C31;
        color: #9DE9D0;
        border: 1px solid #2B5B55;
    }

    QPushButton#memoryPin:hover {
        background: #18302F;
        border-color: #3B8176;
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

    QTextBrowser#activityTimeline {
        background: #0B0D1B;
        border: 1px solid #25294A;
        border-radius: 15px;
        padding: 8px;
        selection-background-color: #5E4AA3;
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

    QLabel#taskStepDone {
        background: #0F1E22;
        color: #9BE8CF;
        border-left: 3px solid #58E6B1;
        border-radius: 8px;
        padding: 7px 10px;
    }

    QLabel#taskStepWaiting {
        background: #211C12;
        color: #EACF94;
        border-left: 3px solid #F5B84C;
        border-radius: 8px;
        padding: 7px 10px;
    }

    QLabel#taskStepFailed {
        background: #2A131C;
        color: #FFB7C3;
        border-left: 3px solid #F05266;
        border-radius: 8px;
        padding: 7px 10px;
    }

    QLabel#taskStepCancelled {
        background: #141526;
        color: #777493;
        border-left: 2px solid #343861;
        border-radius: 8px;
        padding: 7px 10px;
    }

    QFrame#taskSection {
        background: transparent;
        border: 0;
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
        background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(21,27,59,232),stop:0.55 rgba(16,22,47,225),stop:1 rgba(11,16,37,232));
        border: 1px solid #4B4380;
        border-radius: 16px;
    }

    QFrame#glassCard[accent="violet"] {
        border: 1px solid #9A70FF;
    }

    QFrame#glassCard[accent="cyan"] {
        border: 1px solid #4FAEF9;
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
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #6C49DE,stop:1 #3D68DD);
        color: #FFFFFF;
        border: 1px solid #AB88FF;
    }

    QPushButton#primary:hover {
        background: #7554E5;
        border-color: #A78BFA;
    }

    QPushButton#suggestion {
        background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #24235A,stop:1 #111A3A);
        border: 1px solid #6C5AB0;
        border-radius: 14px;
        padding: 15px 18px;
        text-align: left;
    }

    QPushButton#suggestion:hover {
        background: #1A1E39;
        border-color: #6654B8;
    }

    QToolButton#capabilityTile {
        background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #17264A,stop:1 #10172E);
        border: 1px solid #405891;
        border-radius: 12px;
        color: #F5F2FF;
        font-size: 14px;
        font-weight: 700;
    }

    QToolButton#capabilityTile:hover, QToolButton#capabilityTile:checked {
        background: #28236A;
        border: 1px solid #AA80FF;
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

    /* Reference panel pass: denser navy glass, thin violet edges, compact controls. */
    QWidget#mainContent,
    QWidget#homePage, QWidget#chatPage, QWidget#voicePage, QWidget#todayPage,
    QWidget#tasksPage, QWidget#activityPage, QWidget#memoryPage,
    QWidget#learningPage, QWidget#skillsPage, QWidget#journalPage {
        background: transparent;
    }

    QWidget#sidebar {
        background: rgba(7, 12, 34, 242);
        border-right: 1px solid #6D54C6;
    }

    QFrame#brandCard {
        background: transparent;
        border: 0;
        border-radius: 10px;
    }

    QLabel#brand {
        color: #DAD8FF;
        font-size: 20px;
        font-weight: 900;
        letter-spacing: 1px;
    }

    QLabel#brandSub {
        color: #716D91;
        font-size: 9px;
    }

    QListWidget#nav {
        padding: 0;
    }

    QListWidget#nav::item {
        min-height: 26px;
        padding: 7px 10px;
        margin: 1px 0;
        border-radius: 7px;
        color: #BBB8D6;
        font-size: 12px;
        font-weight: 600;
    }

    QListWidget#nav::item:hover {
        background: #141B3D;
        color: #FFFFFF;
        border: 1px solid #3C4777;
    }

    QListWidget#nav::item:selected {
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #6750D8,stop:0.52 #4438A6,stop:1 #263078);
        color: #FFFFFF;
        border: 1px solid #8874FF;
    }

    QFrame#topBar {
        background: rgba(10, 16, 42, 224);
        border: 1px solid #3C4679;
        border-radius: 11px;
    }

    QLabel#statusTitle {
        font-size: 13px;
    }

    QLabel#statusSub {
        color: #8885A8;
        font-size: 10px;
    }

    QLabel#statusPill, QLabel#localPill {
        border-radius: 8px;
        padding: 4px 8px;
        font-size: 9px;
        font-weight: 750;
    }

    QLabel#pageTitle {
        color: #F3F1FF;
        font-size: 21px;
        font-weight: 800;
    }

    QLabel#heroKicker {
        color: #8F84BE;
        font-size: 9px;
        font-weight: 800;
        letter-spacing: 1px;
    }

    QLabel#heroTitle {
        color: #FFFFFF;
        font-size: 31px;
        font-weight: 900;
        letter-spacing: 0;
    }

    QLabel#heroDescription {
        color: #B3B0C9;
        font-size: 13px;
    }

    QLabel#sectionTitle {
        color: #F4F1FF;
        font-size: 13px;
        font-weight: 800;
    }

    QFrame#homeHero, QFrame#homeActions, QFrame#plannerRail,
    QFrame#glassCard {
        background: rgba(10, 16, 40, 230);
        border: 1px solid #43508B;
        border-radius: 11px;
    }

    QFrame#homeHero, QFrame#glassCard[accent="violet"] {
        border: 1px solid #8D6CFF;
    }

    QFrame#glassCard[accent="cyan"] {
        border: 1px solid #4C83E8;
    }

    QFrame#glassCard[accent="amber"] {
        border: 1px solid #806127;
    }

    QFrame#chatLiveCard {
        background: rgba(14, 18, 48, 238);
        border: 1px solid #604FC1;
        border-radius: 10px;
    }

    QLabel#homeClock {
        color: #E0B0FF;
        font-size: 28px;
        font-weight: 850;
    }

    QLabel#metricLabel {
        color: #77799C;
        font-size: 8px;
        font-weight: 800;
        letter-spacing: 1px;
    }

    QLabel#metricValue, QLabel#opsMetric, QLabel#opsMetricReady,
    QLabel#opsMetricDanger, QLabel#skillMetric, QLabel#skillMetricReady {
        color: #F7F4FF;
        font-weight: 850;
    }

    QLabel#metricValueSmall {
        color: #F4F1FF;
        font-size: 15px;
        font-weight: 800;
    }

    QLabel#systemMeterName, QLabel#systemMeterValue {
        color: #AAA7C1;
        font-size: 10px;
    }

    QPushButton {
        background: rgba(18, 25, 55, 238);
        color: #EDEBFA;
        border: 1px solid #3C4775;
        border-radius: 8px;
        padding: 7px 10px;
        font-size: 11px;
        font-weight: 650;
    }

    QPushButton:hover {
        background: #20295B;
        border-color: #7664D9;
    }

    QPushButton#primary {
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #6250DF,stop:1 #3D67E0);
        border: 1px solid #907CFF;
    }

    QPushButton#heroAction {
        background: rgba(21, 28, 65, 235);
        border: 1px solid #4E4F91;
        border-radius: 8px;
        color: #EDEBFF;
        min-height: 28px;
        padding: 5px 9px;
        font-size: 11px;
    }

    QPushButton#suggestion {
        background: rgba(15, 24, 54, 238);
        border: 1px solid #3B4C7E;
        border-radius: 8px;
        padding: 9px 11px;
        min-height: 26px;
        font-size: 10px;
        text-align: center;
    }

    QPushButton#panelStyle {
        background: #101735;
        border: 1px solid #394675;
        border-radius: 7px;
        min-height: 26px;
        font-size: 10px;
    }

    QPushButton#panelStyle:checked {
        background: #30246B;
        border: 1px solid #9C78FF;
    }

    QToolButton#capabilityTile {
        background: rgba(10, 20, 48, 238);
        border: 1px solid #3B538C;
        border-radius: 9px;
        color: #F4F1FF;
        font-size: 11px;
        font-weight: 750;
    }

    QToolButton#capabilityTile:hover, QToolButton#capabilityTile:checked {
        background: #24205B;
        border: 1px solid #9B78FF;
    }

    QTextBrowser, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
        background: rgba(8, 14, 34, 235);
        color: #F1EEFF;
        border: 1px solid #34416D;
        border-radius: 8px;
        padding: 7px 9px;
        font-size: 11px;
    }

    QTextBrowser:focus, QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus,
    QSpinBox:focus, QDoubleSpinBox:focus {
        border: 1px solid #8A6EF1;
        background: #0F1737;
    }

    QLineEdit#chatInput {
        background: transparent;
        padding: 8px 10px;
        font-size: 12px;
    }

    QFrame#chatBubbleUser {
        background: #324FC1;
        border: 1px solid #6080FF;
        border-radius: 10px;
    }

    QFrame#chatBubbleAssistant {
        background: #111A38;
        border: 1px solid #324574;
        border-radius: 10px;
    }

    QLabel#chatBubbleText {
        color: #F7F5FF;
        font-size: 12px;
    }

    QLabel#chatBubbleMeta {
        color: #AFA0FF;
        font-size: 9px;
    }

    QScrollArea#chatBubbleScroll, QScrollArea#chatBubbleScroll > QWidget > QWidget {
        background: rgba(6, 11, 28, 218);
    }

    QTabBar#appearanceTabs {
        background: #0A1230;
        border: 1px solid #36457B;
        border-radius: 8px;
    }

    QTabBar#appearanceTabs::tab {
        min-height: 25px;
        padding: 4px 9px;
        color: #A7A3BE;
        font-size: 10px;
    }

    QTabBar#appearanceTabs::tab:selected {
        background: #312369;
        color: #FFFFFF;
        border: 1px solid #9D78FF;
        border-radius: 7px;
    }

    QCheckBox {
        spacing: 8px;
        color: #D6D2E6;
        font-size: 11px;
    }

    QCheckBox::indicator {
        width: 30px;
        height: 15px;
        border-radius: 7px;
        border: 1px solid #46537F;
        background: #141B38;
    }

    QCheckBox::indicator:checked {
        background: #5F62F0;
        border: 1px solid #8B8FFF;
    }

    QProgressBar {
        background: #0B1331;
        border: 0;
        border-radius: 4px;
        min-height: 6px;
        max-height: 6px;
    }

    QProgressBar::chunk {
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #4AD7FF,stop:0.5 #6E62FF,stop:1 #F05BCA);
        border-radius: 4px;
    }

    QLabel#voiceChain {
        background: #0E183A;
        color: #B7B0E9;
        border: 1px solid #384A80;
        border-radius: 8px;
        padding: 7px 9px;
        font-size: 9px;
    }

    QLabel#footer {
        background: rgba(5, 9, 23, 246);
        color: #666887;
        border-top: 1px solid #252E52;
        padding: 4px 12px;
        font-size: 9px;
    }
    QLabel#brandMark {
        background: transparent;
        border: 0;
    }

    QLabel#sidebarStatus {
        background: rgba(13, 26, 52, 220);
        color: #73E7C4;
        border: 1px solid #2C5B59;
        border-radius: 8px;
        padding: 6px 7px;
        font-size: 8px;
        font-weight: 800;
        letter-spacing: 1px;
    }

    QLabel#themePreviewDark,
    QLabel#themePreviewLight,
    QLabel#themePreviewSystem {
        border-radius: 9px;
        padding: 8px;
        font-size: 10px;
        font-weight: 750;
    }

    QLabel#themePreviewDark {
        background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #241445,stop:1 #0A1535);
        color: #FFFFFF;
        border: 1px solid #A479FF;
    }

    QLabel#themePreviewLight {
        background: #D8D9E8;
        color: #36334A;
        border: 1px solid #8586A2;
    }

    QLabel#themePreviewSystem {
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #161C39,stop:0.5 #161C39,stop:0.51 #D9DBE7,stop:1 #D9DBE7);
        color: #AFA9FF;
        border: 1px solid #59618C;
    }

    QWidget#memoryPage QLineEdit,
    QWidget#skillsPage QLineEdit {
        min-height: 28px;
        border-radius: 9px;
    }

    QWidget#memoryPage QTabBar#appearanceTabs::tab {
        min-width: 76px;
    }

    QWidget#tasksPage QFrame#plannerRail {
        background: rgba(9, 15, 37, 238);
        border: 1px solid #5965AE;
    }

    QWidget#settingsSurface {
        background: rgba(7, 12, 31, 225);
        border: 1px solid #384579;
        border-radius: 11px;
    }

    QWidget#settingsSurface QLabel#pageTitle {
        font-size: 20px;
    }

    QWidget#settingsSurface QCheckBox::indicator {
        width: 34px;
        height: 17px;
        border-radius: 8px;
    }

    """
    if accent != "#8B5CF6":
        for source in ("#8B5CF6", "#9A70FF", "#AB88FF", "#7D63E6", "#6654B8"):
            css = css.replace(source, accent)
    return css
