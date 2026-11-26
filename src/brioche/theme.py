"""Warm Paper (Light) and Dark Roast (Dark) design tokens and QSS generator."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

LIGHT_TOKENS = {
    "mode": "light",
    "bg_canvas": "#F6F3ED",
    "bg_sidebar": "#ECE7DE",
    "bg_surface": "#FFFDF9",
    "bg_surface_alt": "#F5F1E9",
    "bg_surface_hover": "#F7F1E7",
    "bg_input": "#FFFDF8",
    "bg_button": "#EDE7DC",
    "bg_button_hover": "#E2D7C7",
    "bg_pill": "#E8E2D6",
    "bg_pill_active": "#BD713D",
    "border_subtle": "#E3DCCF",
    "border_strong": "#D2C8B7",
    "border_focus": "#BD713D",
    "brand_primary": "#BD713D",
    "brand_hover": "#A55D2E",
    "brand_text": "#9C5A30",
    "brand_tint": "#F2E4D6",
    "text_primary": "#272521",
    "text_secondary": "#6E6960",
    "text_muted": "#8C867B",
    "text_on_brand": "#FFFFFF",
    "status_online": "#2F7D5B",
    "status_online_bg": "#E4F2EB",
    "status_relay": "#B5721C",
    "status_relay_bg": "#F7ECDC",
    "status_error": "#B8453E",
    "status_error_bg": "#F8E5E3",
    "status_offline": "#96938A",
    "status_offline_bg": "#EAE7E0",
    "card_shadow": (72, 47, 22, 12),
    "card_shadow_hover": (72, 47, 22, 22),
    "screen_frame_live": "#363D42",
    "screen_frame_off": "#9A9C96",
    "screen_grad_a_live": "#E6C9A8",
    "screen_grad_b_live": "#98B2B3",
    "screen_grad_a_off": "#DAD8D0",
    "screen_grad_b_off": "#C8CAC4",
    "screen_window_bg": (255, 255, 255, 175),
    "screen_accent_live": "#C5946D",
    "screen_accent_off": "#B5B2A8",
    "screen_stand_live": "#515959",
    "screen_stand_off": "#A2A49E",
    "scrollbar": "#CFC5B5",
    "scrollbar_hover": "#B8AA96",
}

DARK_TOKENS = {
    "mode": "dark",
    "bg_canvas": "#161513",
    "bg_sidebar": "#1E1C19",
    "bg_surface": "#25231F",
    "bg_surface_alt": "#1F1D1A",
    "bg_surface_hover": "#2E2B26",
    "bg_input": "#1C1A17",
    "bg_button": "#2C2924",
    "bg_button_hover": "#38342D",
    "bg_pill": "#2A2722",
    "bg_pill_active": "#D48A55",
    "border_subtle": "#34312B",
    "border_strong": "#48433B",
    "border_focus": "#D48A55",
    "brand_primary": "#D48A55",
    "brand_hover": "#E09B68",
    "brand_text": "#E3A170",
    "brand_tint": "#3A2C21",
    "text_primary": "#F2EFE9",
    "text_secondary": "#A8A297",
    "text_muted": "#7D786F",
    "text_on_brand": "#1A1511",
    "status_online": "#47A87D",
    "status_online_bg": "#1D3329",
    "status_relay": "#DE993E",
    "status_relay_bg": "#362A19",
    "status_error": "#E0635B",
    "status_error_bg": "#3A2220",
    "status_offline": "#78746C",
    "status_offline_bg": "#2B2925",
    "card_shadow": (0, 0, 0, 48),
    "card_shadow_hover": (0, 0, 0, 82),
    "screen_frame_live": "#484E53",
    "screen_frame_off": "#3A3936",
    "screen_grad_a_live": "#9E7855",
    "screen_grad_b_live": "#4F6B6D",
    "screen_grad_a_off": "#32312E",
    "screen_grad_b_off": "#292825",
    "screen_window_bg": (28, 26, 23, 195),
    "screen_accent_live": "#D48A55",
    "screen_accent_off": "#59554E",
    "screen_stand_live": "#5A5F61",
    "screen_stand_off": "#45433E",
    "scrollbar": "#47433C",
    "scrollbar_hover": "#5E584E",
}


def resolve_mode(preference="system"):
    if preference in ("light", "dark"):
        return preference
    app = QApplication.instance()
    if app is not None:
        try:
            scheme = app.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
        except AttributeError:
            pass
    return "light"


def tokens_for(preference="system"):
    return dict(DARK_TOKENS if resolve_mode(preference) == "dark" else LIGHT_TOKENS)


def popup_stylesheet(tokens):
    return (
        "QFrame { background: transparent; border: none; }"
        f"QListView {{ background: {tokens['bg_surface']}; color: {tokens['text_primary']};"
        f" border: 1px solid {tokens['border_subtle']}; border-radius: 12px; padding: 6px; outline: none; }}"
        "QListView::item { padding: 8px 12px; border-radius: 7px; }"
        f"QListView::item:selected {{ background: {tokens['brand_tint']}; color: {tokens['brand_text']}; }}"
        f"QListView::item:hover {{ background: {tokens['bg_surface_hover']}; }}"
    )


def build_stylesheet(preference="system"):
    t = tokens_for(preference)
    return f"""
QWidget {{
    background: {t['bg_canvas']};
    color: {t['text_primary']};
    font-size: 13px;
}}
QMainWindow, QDialog {{
    background: {t['bg_canvas']};
}}
QLabel {{
    background: transparent;
}}
QLabel#brand {{
    font-size: 23px;
    font-weight: 700;
    color: {t['brand_text']};
}}
QLabel#heading {{
    font-size: 22px;
    font-weight: 700;
    color: {t['text_primary']};
}}
QLabel#subheading {{
    font-size: 15px;
    font-weight: 650;
    color: {t['text_primary']};
}}
QLabel#sectionLabel {{
    font-size: 11px;
    font-weight: 700;
    color: {t['text_muted']};
    padding: 6px 8px 2px 8px;
}}
QLabel#muted {{
    color: {t['text_secondary']};
}}
QLabel#badgeOnline {{
    background: {t['status_online_bg']};
    color: {t['status_online']};
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
}}
QLabel#badgeRelay {{
    background: {t['status_relay_bg']};
    color: {t['status_relay']};
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
}}
QLabel#badgeOffline {{
    background: {t['status_offline_bg']};
    color: {t['status_offline']};
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
}}
QFrame#sidebar {{
    background: {t['bg_sidebar']};
    border: 1px solid {t['border_subtle']};
    border-radius: 14px;
}}
QFrame#topbar {{
    background: {t['bg_surface']};
    border: 1px solid {t['border_subtle']};
    border-radius: 14px;
}}
QFrame#drawer {{
    background: {t['bg_surface']};
    border: 1px solid {t['border_subtle']};
    border-radius: 14px;
}}
QFrame#surfaceCard, QWidget#transferCard {{
    background: {t['bg_surface']};
    border: 1px solid {t['border_subtle']};
    border-radius: 14px;
}}
QFrame#miniCard {{
    background: {t['bg_surface']};
    border: 1px solid {t['border_subtle']};
    border-radius: 11px;
}}
QFrame#divider {{
    background: {t['border_subtle']};
    max-height: 1px;
    min-height: 1px;
}}
QLineEdit, QSpinBox, QComboBox, QTextEdit {{
    background: {t['bg_input']};
    color: {t['text_primary']};
    border: 1px solid {t['border_subtle']};
    border-radius: 9px;
    padding: 8px 11px;
    selection-background-color: {t['brand_primary']};
    selection-color: {t['text_on_brand']};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QTextEdit:focus {{
    border: 1px solid {t['border_focus']};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
    subcontrol-origin: padding;
    subcontrol-position: top right;
}}
QSpinBox::up-button, QSpinBox::down-button {{
    border: none;
    background: transparent;
    width: 18px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background: {t['bg_surface_hover']};
    border-radius: 4px;
}}
QLineEdit#searchInput {{
    background: {t['bg_canvas']};
    border: 1px solid {t['border_subtle']};
    border-radius: 10px;
    padding: 8px 12px;
}}
QLineEdit#searchInput:focus {{
    border: 1px solid {t['border_focus']};
    background: {t['bg_input']};
}}
QPushButton {{
    background: {t['bg_button']};
    color: {t['text_primary']};
    border: 1px solid {t['border_subtle']};
    border-radius: 9px;
    padding: 8px 14px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: {t['bg_button_hover']};
    border-color: {t['border_strong']};
}}
QPushButton:disabled {{
    color: {t['text_muted']};
    background: {t['bg_surface_alt']};
    border-color: {t['border_subtle']};
}}
QPushButton#primary {{
    color: {t['text_on_brand']};
    background: {t['brand_primary']};
    border: none;
    font-weight: 600;
}}
QPushButton#primary:hover {{
    background: {t['brand_hover']};
}}
QPushButton#primary:disabled {{
    background: {t['border_strong']};
    color: {t['text_muted']};
}}
QPushButton#danger {{
    color: {t['status_error']};
    background: {t['status_error_bg']};
    border: 1px solid transparent;
}}
QPushButton#danger:hover {{
    border-color: {t['status_error']};
}}
QPushButton#ghost {{
    background: transparent;
    border: 1px solid transparent;
    color: {t['text_secondary']};
    padding: 6px 10px;
}}
QPushButton#ghost:hover {{
    background: {t['bg_surface_hover']};
    color: {t['text_primary']};
}}
QPushButton#navButton {{
    text-align: left;
    padding: 9px 14px;
    border: none;
    border-radius: 10px;
    background: transparent;
    color: {t['text_secondary']};
    font-weight: 500;
}}
QPushButton#navButton:hover {{
    background: {t['bg_surface_hover']};
    color: {t['text_primary']};
}}
QPushButton#navButton:checked {{
    background: {t['bg_surface']};
    color: {t['brand_text']};
    font-weight: 650;
}}
QPushButton#groupButton {{
    text-align: left;
    padding: 6px 12px;
    border: none;
    border-radius: 8px;
    background: transparent;
    color: {t['text_secondary']};
    font-size: 12px;
}}
QPushButton#groupButton:hover {{
    background: {t['bg_surface_hover']};
    color: {t['text_primary']};
}}
QPushButton#groupButton:checked {{
    background: {t['brand_tint']};
    color: {t['brand_text']};
    font-weight: 600;
}}
QPushButton#filterPill {{
    background: {t['bg_pill']};
    color: {t['text_secondary']};
    border: none;
    border-radius: 15px;
    padding: 6px 13px;
    font-size: 12px;
    font-weight: 550;
}}
QPushButton#filterPill:hover {{
    background: {t['bg_button_hover']};
    color: {t['text_primary']};
}}
QPushButton#filterPill:checked {{
    background: {t['bg_pill_active']};
    color: {t['text_on_brand']};
    font-weight: 650;
}}
QPushButton#sharePillOnline {{
    background: {t['status_online_bg']};
    color: {t['status_online']};
    border: 1px solid transparent;
    border-radius: 16px;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 650;
}}
QPushButton#sharePillOnline:hover {{
    border-color: {t['status_online']};
}}
QPushButton#sharePillOffline {{
    background: {t['bg_pill']};
    color: {t['text_secondary']};
    border: 1px solid {t['border_subtle']};
    border-radius: 16px;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#sharePillOffline:hover {{
    color: {t['text_primary']};
    border-color: {t['border_strong']};
}}
QPushButton#settingsNav {{
    text-align: left;
    padding: 9px 12px;
    border: none;
    border-radius: 9px;
    background: transparent;
    color: {t['text_secondary']};
}}
QPushButton#settingsNav:hover {{
    background: {t['bg_surface_hover']};
    color: {t['text_primary']};
}}
QPushButton#settingsNav:checked {{
    background: {t['brand_tint']};
    color: {t['brand_text']};
    font-weight: 650;
}}
QPushButton:checked {{
    background: {t['brand_tint']};
    color: {t['brand_text']};
    border-color: {t['border_focus']};
}}
QListWidget#deviceList {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget#deviceList::item {{
    background: transparent;
    border: none;
    padding: 0;
}}
QTreeWidget {{
    background: {t['bg_input']};
    color: {t['text_primary']};
    border: 1px solid {t['border_subtle']};
    border-radius: 10px;
    outline: none;
    padding: 4px;
}}
QTreeWidget::item {{
    padding: 6px 8px;
    border-radius: 6px;
}}
QTreeWidget::item:hover {{
    background: {t['bg_surface_hover']};
}}
QTreeWidget::item:selected {{
    background: {t['brand_tint']};
    color: {t['brand_text']};
}}
QHeaderView::section {{
    background: {t['bg_surface_alt']};
    color: {t['text_secondary']};
    border: none;
    border-bottom: 1px solid {t['border_subtle']};
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 600;
}}
QSplitter::handle {{
    background: transparent;
    width: 10px;
}}
QLabel#transferTitle {{
    font-weight: 650;
    color: {t['text_primary']};
}}
QProgressBar {{
    background: {t['bg_pill']};
    border: none;
    border-radius: 4px;
    max-height: 7px;
    min-height: 7px;
}}
QProgressBar::chunk {{
    background: {t['brand_primary']};
    border-radius: 4px;
}}
QCheckBox {{
    spacing: 9px;
    padding: 5px 0;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border-radius: 5px;
    border: 1.5px solid {t['border_strong']};
    background: {t['bg_input']};
}}
QCheckBox::indicator:hover {{
    border-color: {t['brand_primary']};
}}
QCheckBox::indicator:checked {{
    background: {t['brand_primary']};
    border-color: {t['brand_primary']};
}}
QCheckBox::indicator:disabled {{
    background: {t['bg_surface_alt']};
    border-color: {t['border_subtle']};
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {t['scrollbar']};
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t['scrollbar_hover']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QMenu {{
    background: {t['bg_surface']};
    color: {t['text_primary']};
    border: 1px solid {t['border_subtle']};
    border-radius: 10px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 14px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background: {t['brand_tint']};
    color: {t['brand_text']};
}}
"""
