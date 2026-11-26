"""Modern theme-aware vector-painted controls. Animations run only during interaction."""

import sys
import time
from PySide6.QtCore import Qt, QRectF, QSize, QVariantAnimation, QEasingCurve, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QLinearGradient
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)
from .theme import LIGHT_TOKENS, popup_stylesheet


def _platform_badge(platform_name):
    low = str(platform_name or "").lower()
    if "darwin" in low or "mac" in low:
        return "macOS"
    if "win" in low:
        return "Win"
    if "linux" in low or "ubuntu" in low:
        return "Linux"
    return "PC"


class RoundedComboBox(QComboBox):
    _instances = []

    def __init__(self, parent=None):
        super().__init__(parent)
        view = QListView()
        view.setObjectName("comboPopup")
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setView(view)
        view.setItemDelegate(ComboDelegate(view))
        view.setUniformItemSizes(True)
        popup = view.window()
        # Qt 6.7 libqcocoa has a use-after-free in NSWindowAccessibility / autorelease
        # when QComboBoxPrivateContainer is forced to Frameless + WA_TranslucentBackground.
        if sys.platform != "darwin":
            popup.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
            popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._popup = popup
        self.apply_theme(LIGHT_TOKENS)

    def apply_theme(self, tokens):
        self._popup.setStyleSheet(popup_stylesheet(tokens))


class ComboDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(super().sizeHint(option, index).width(), 34)


class DeviceDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.tokens = dict(LIGHT_TOKENS)

    def set_theme(self, tokens):
        self.tokens = dict(tokens)

    def paint(self, painter, option, index):
        t = self.tokens
        data = index.data(Qt.ItemDataRole.UserRole) or {}
        live = bool(data.get("online") or data.get("reachable"))
        connectable = bool(data.get("connectable"))
        busy = bool(data.get("busy"))
        favorite = bool(data.get("favorite"))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        key = data.get("key")
        hover = self.parent().hover if key == self.parent().hover_key else 0.0
        fresh = self.parent().entries.get(key)
        opacity = min(1.0, (time.monotonic() - fresh) / 0.25) if fresh else 1.0

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(opacity)

        rect = QRectF(option.rect).adjusted(4, 6 - hover * 3, -4, -6 - hover * 3)

        # Soft shadow
        sr, sg, sb, sa = t["card_shadow_hover"] if hover > 0.2 else t["card_shadow"]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(sr, sg, sb, sa))
        painter.drawRoundedRect(rect.translated(0, 3), 15, 15)

        # Card surface & border
        bg_color = t["bg_surface"] if live else t["bg_surface_alt"]
        if hover > 0.2 and live:
            bg_color = t["bg_surface_hover"]
        border_color = (
            t["border_focus"]
            if selected
            else (t["border_strong"] if hover > 0.2 else t["border_subtle"])
        )
        painter.setBrush(QColor(bg_color))
        painter.setPen(QPen(QColor(border_color), 1.6 if selected else 1.0))
        painter.drawRoundedRect(rect, 15, 15)

        # Monitor preview art
        screen = QRectF(rect.left() + 18, rect.top() + 18, rect.width() - 36, 92)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(t["screen_frame_live"] if live else t["screen_frame_off"]))
        painter.drawRoundedRect(screen, 8, 8)

        inside = screen.adjusted(4, 4, -4, -8)
        gradient = QLinearGradient(inside.topLeft(), inside.bottomRight())
        gradient.setColorAt(0, QColor(t["screen_grad_a_live"] if live else t["screen_grad_a_off"]))
        gradient.setColorAt(1, QColor(t["screen_grad_b_live"] if live else t["screen_grad_b_off"]))
        painter.setBrush(gradient)
        painter.drawRoundedRect(inside, 5, 5)

        # Mini window card inside desktop art
        wr, wg, wb, wa = t["screen_window_bg"]
        painter.setBrush(QColor(wr, wg, wb, wa))
        mini = inside.adjusted(18, 16, -18, -14)
        painter.drawRoundedRect(mini, 5, 5)
        painter.setBrush(QColor(t["screen_accent_live"] if live else t["screen_accent_off"]))
        painter.drawRoundedRect(QRectF(mini.left() + 7, mini.top() + 7, 26, 4), 2, 2)
        painter.setBrush(QColor(t["border_subtle"]))
        painter.drawRoundedRect(
            QRectF(mini.left() + 7, mini.top() + 16, max(12, mini.width() - 20), 3),
            1.5,
            1.5,
        )

        # Stand
        painter.setBrush(QColor(t["screen_stand_live"] if live else t["screen_stand_off"]))
        painter.drawRoundedRect(QRectF(screen.center().x() - 5, screen.bottom() - 1, 10, 8), 2, 2)
        painter.drawRoundedRect(QRectF(screen.center().x() - 22, screen.bottom() + 6, 44, 3.5), 1.8, 1.8)

        # OS pill (top-left inside screen)
        os_text = _platform_badge(data.get("platform", ""))
        badge_font = QFont(option.font)
        badge_font.setPointSize(8)
        badge_font.setBold(True)
        painter.setFont(badge_font)
        os_w = painter.fontMetrics().horizontalAdvance(os_text) + 14
        os_rect = QRectF(inside.left() + 6, inside.top() + 6, os_w, 18)
        painter.setBrush(QColor(22, 21, 19, 155))
        painter.drawRoundedRect(os_rect, 9, 9)
        painter.setPen(QColor("#F7F5F0"))
        painter.drawText(os_rect, Qt.AlignmentFlag.AlignCenter, os_text)

        # Path badge (top-right inside screen)
        badge_text = data.get("badge_text") or ("在线" if live else "离线")
        tone = data.get("badge_tone") or ("online" if connectable else ("relay" if busy else "offline"))
        tone_colors = {
            "online": (t["status_online_bg"], t["status_online"]),
            "relay": (t["status_relay_bg"], t["status_relay"]),
            "offline": (t["status_offline_bg"], t["status_offline"]),
        }
        b_bg, b_fg = tone_colors.get(tone, tone_colors["offline"])
        bw = painter.fontMetrics().horizontalAdvance(badge_text) + 14
        b_rect = QRectF(inside.right() - bw - 6, inside.top() + 6, bw, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(b_bg))
        painter.drawRoundedRect(b_rect, 9, 9)
        painter.setPen(QColor(b_fg))
        painter.drawText(b_rect, Qt.AlignmentFlag.AlignCenter, badge_text)

        # Favorite star indicator
        if favorite:
            star_font = QFont(option.font)
            star_font.setPointSize(11)
            painter.setFont(star_font)
            painter.setPen(QColor(t["brand_primary"]))
            painter.drawText(
                QRectF(rect.right() - 28, rect.top() + 126, 20, 22),
                Qt.AlignmentFlag.AlignCenter,
                "★",
            )

        # Status dot + Device Name
        dot_color = (
            t["status_online"]
            if connectable
            else (t["status_relay"] if busy else (t["status_online"] if live else t["status_offline"]))
        )
        name_top = rect.top() + 128
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(dot_color))
        painter.drawEllipse(QRectF(rect.left() + 18, name_top + 7, 8, 8))

        title_font = QFont(option.font)
        title_font.setPointSize(10)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(t["text_primary"] if live else t["text_secondary"]))
        label = data.get("display_name", data.get("name", "电脑"))
        title_w = rect.width() - (58 if favorite else 46)
        fm_title = painter.fontMetrics()
        painter.drawText(
            QRectF(rect.left() + 32, name_top, title_w, fm_title.height() + 4),
            Qt.AlignmentFlag.AlignVCenter,
            fm_title.elidedText(label, Qt.TextElideMode.ElideRight, int(title_w)),
        )

        # Status text line
        sub_font = QFont(option.font)
        sub_font.setPointSize(9)
        sub_font.setBold(False)
        painter.setFont(sub_font)
        fm_sub = painter.fontMetrics()
        status_top = name_top + fm_title.height() + 6
        painter.setPen(
            QColor(
                t["status_online"]
                if connectable
                else (t["status_relay"] if busy else t["text_secondary"])
            )
        )
        status = data.get("status_text", "")
        painter.drawText(
            QRectF(rect.left() + 18, status_top, rect.width() - 36, fm_sub.height() + 2),
            Qt.AlignmentFlag.AlignVCenter,
            fm_sub.elidedText(status, Qt.TextElideMode.ElideRight, int(rect.width() - 36)),
        )

        # Bottom line: IP / Group tag
        meta_top = status_top + fm_sub.height() + 4
        painter.setPen(QColor(t["text_muted"]))
        meta_text = str(data.get("ip", ""))
        group = data.get("group", "")
        if group:
            meta_text = f"{meta_text}  ·  #{group}" if meta_text else f"#{group}"
        painter.drawText(
            QRectF(rect.left() + 18, meta_top, rect.width() - 36, fm_sub.height() + 2),
            Qt.AlignmentFlag.AlignVCenter,
            fm_sub.elidedText(meta_text, Qt.TextElideMode.ElideRight, int(rect.width() - 36)),
        )

        painter.restore()

    def sizeHint(self, option, index):
        return QSize(256, 214)


class DeviceList(QListWidget):
    context_requested = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("deviceList")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMouseTracking(True)
        self.setSpacing(8)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self._delegate = DeviceDelegate(self)
        self.setItemDelegate(self._delegate)
        self.hover, self.hover_key, self.entries = 0.0, None, {}
        self.animations = True
        self.motion = QVariantAnimation(self)
        self.motion.setDuration(150)
        self.motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.motion.valueChanged.connect(self._motion)
        self.entered.connect(self._entered)
        self.fade_timer = QTimer(self)
        self.fade_timer.setInterval(16)
        self.fade_timer.timeout.connect(self._fade)

    def apply_theme(self, tokens):
        self._delegate.set_theme(tokens)
        self.viewport().update()

    def _context_menu(self, pos):
        item = self.itemAt(pos)
        if item:
            self.setCurrentItem(item)
            self.context_requested.emit(item, self.viewport().mapToGlobal(pos))

    def _motion(self, value):
        self.hover = float(value)
        self.viewport().update()

    def _entered(self, index):
        key = (index.data(Qt.ItemDataRole.UserRole) or {}).get("key")
        if key != self.hover_key:
            self.hover_key = key
            self.hover = 0.0
            self.motion.stop()
            if self.animations:
                self.motion.setStartValue(0.0)
                self.motion.setEndValue(1.0)
                self.motion.start()
            else:
                self.hover = 1.0
                self.viewport().update()

    def leaveEvent(self, event):
        self.motion.stop()
        if self.animations:
            self.motion.setStartValue(self.hover)
            self.motion.setEndValue(0.0)
            self.motion.start()
        else:
            self.hover = 0.0
            self.viewport().update()
        super().leaveEvent(event)

    def new_entry(self, key):
        if self.animations:
            self.entries[key] = time.monotonic()
            self.fade_timer.start()

    def _fade(self):
        now = time.monotonic()
        self.entries = {
            key: stamp for key, stamp in self.entries.items() if now - stamp < 0.3
        }
        self.viewport().update()
        if not self.entries:
            self.fade_timer.stop()

    def set_animations(self, enabled):
        self.animations = enabled
        if not enabled:
            self.motion.stop()
            self.fade_timer.stop()
            self.entries.clear()
            self.hover = 0.0
            self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = max(240, self.viewport().width() - 16)
        columns = max(1, min(4, width // 248))
        cell_w = max(230, (width - 12 * (columns - 1)) // columns)
        self.setGridSize(QSize(cell_w, 214))
        for index in range(self.count()):
            self.item(index).setSizeHint(self.gridSize() - QSize(4, 4))


class FilterPillBar(QWidget):
    changed = Signal(str)

    CATEGORIES = (
        ("all", "全部"),
        ("online", "在线"),
        ("favorite", "收藏"),
        ("lan", "局域网"),
        ("tailscale", "外网"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current = "all"
        self.buttons = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        for key, label in self.CATEGORIES:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setObjectName("filterPill")
            btn.clicked.connect(lambda checked=False, k=key: self.select(k))
            self.buttons[key] = (btn, label)
            row.addWidget(btn)
        row.addStretch()
        self.select("all", emit=False)

    def select(self, key, emit=True):
        self.current = key
        for k, (btn, _) in self.buttons.items():
            btn.setChecked(k == key)
        if emit:
            self.changed.emit(key)

    def update_counts(self, counts):
        for k, (btn, label) in self.buttons.items():
            n = counts.get(k, 0)
            btn.setText(f"{label} {n}" if n > 0 else label)


class AddLanDeviceDialog(QDialog):
    def __init__(self, local_ips="", nearby_devices=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加局域网设备")
        self.setFixedWidth(470)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        title = QLabel("添加局域网设备")
        title.setObjectName("subheading")
        layout.addWidget(title)

        hint = QLabel("输入同一局域网电脑的 IPv4 地址（默认端口 47995），或从下方附近发现列表直接点选。首次连接将核对六位配对码。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.ip_input = QLineEdit()
        self.ip_input.setPlaceholderText("例如 192.168.1.8 或 192.168.1.8:47995")
        self.ip_input.returnPressed.connect(self.accept)
        layout.addWidget(self.ip_input)

        nearby = list(nearby_devices or [])
        if nearby:
            sec = QLabel(f"附近发现的 Brioche 设备（{len(nearby)}）")
            sec.setObjectName("sectionLabel")
            layout.addWidget(sec)
            self.nearby_list = QListWidget()
            self.nearby_list.setMaximumHeight(140)
            for item in nearby:
                addr = (
                    f"{item['ip']}:{item['port']}"
                    if item.get("port", 47995) != 47995
                    else item["ip"]
                )
                badge = _platform_badge(item.get("platform", ""))
                entry = f"{item['name']}  ·  {badge}  ·  {addr}"
                if item.get("busy"):
                    entry += "  （使用中）"
                self.nearby_list.addItem(entry)
                self.nearby_list.item(self.nearby_list.count() - 1).setData(
                    Qt.ItemDataRole.UserRole, addr
                )
            self.nearby_list.itemClicked.connect(
                lambda it: self.ip_input.setText(it.data(Qt.ItemDataRole.UserRole))
            )
            self.nearby_list.itemDoubleClicked.connect(self._pick_and_accept)
            layout.addWidget(self.nearby_list)
        else:
            no_nearby = QLabel("暂未监听到附近开启自动发现的局域网设备，可直接输入 IP 查找。")
            no_nearby.setObjectName("muted")
            no_nearby.setWordWrap(True)
            layout.addWidget(no_nearby)

        if local_ips:
            local_box = QFrame()
            local_box.setObjectName("miniCard")
            lrow = QHBoxLayout(local_box)
            lrow.setContentsMargins(12, 8, 10, 8)
            lbl = QLabel(f"本机局域网 IP：{local_ips}")
            lbl.setObjectName("muted")
            lbl.setWordWrap(True)
            copy_btn = QPushButton("复制")
            copy_btn.setObjectName("ghost")
            copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(local_ips.split("、")[0]))
            lrow.addWidget(lbl, 1)
            lrow.addWidget(copy_btn)
            layout.addWidget(local_box)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton("查找并添加")
        confirm.setObjectName("primary")
        confirm.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)

    def _pick_and_accept(self, item):
        self.ip_input.setText(item.data(Qt.ItemDataRole.UserRole))
        self.accept()

    def value(self):
        return self.ip_input.text().strip()


class PageTransition:
    def __init__(self, stack):
        self.stack = stack
        self.animation = QVariantAnimation(stack)
        self.animation.setDuration(150)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.effect = None
        self.animation.valueChanged.connect(self._value)
        self.animation.finished.connect(self._finish)

    def show(self, index, animate=True):
        self.animation.stop()
        self._finish()
        if self.stack.currentIndex() == index:
            return
        self.stack.setCurrentIndex(index)
        if animate and self.stack.isVisible():
            self.effect = QGraphicsOpacityEffect(self.stack)
            self.stack.setGraphicsEffect(self.effect)
            self.animation.setStartValue(0.4)
            self.animation.setEndValue(1.0)
            self.animation.start()

    def _value(self, value):
        if self.effect:
            self.effect.setOpacity(float(value))

    def _finish(self):
        if self.effect:
            self.stack.setGraphicsEffect(None)
            self.effect = None
