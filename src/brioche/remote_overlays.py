"""Immersive dark-glass overlays for the remote desktop canvas:
floating auto-hide pill toolbar, draggable performance HUD, and transient toast.
"""

from PySide6.QtCore import Qt, QPoint, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
)
from .theme import DARK_TOKENS
from .ui_components import RoundedComboBox

OVERLAY_STYLE = """
QFrame#floatingPill {
    background: rgba(24, 22, 19, 232);
    border: 1px solid rgba(242, 239, 233, 36);
    border-radius: 19px;
}
QFrame#floatingPill QLabel {
    background: transparent;
    color: #F2EFE9;
    font-size: 12px;
}
QLabel#pillGrip {
    color: #7D786F;
    font-size: 13px;
    padding: 0 2px;
}
QFrame#floatingPill QLabel#pillChip {
    background: rgba(212, 138, 85, 42);
    color: #E3A170;
    border-radius: 9px;
    padding: 3px 9px;
    font-size: 11px;
    font-weight: 600;
}
QFrame#floatingPill QLabel#pillChipOnline {
    background: rgba(71, 168, 125, 46);
    color: #6AD2A4;
    border-radius: 9px;
    padding: 3px 9px;
    font-size: 11px;
    font-weight: 600;
}
QFrame#floatingPill QComboBox {
    background: rgba(255, 255, 255, 16);
    color: #F2EFE9;
    border: 1px solid rgba(255, 255, 255, 24);
    border-radius: 10px;
    padding: 4px 10px;
    font-size: 12px;
}
QFrame#floatingPill QComboBox:hover {
    background: rgba(255, 255, 255, 28);
}
QFrame#floatingPill QComboBox::drop-down {
    border: none;
    width: 18px;
}
QFrame#floatingPill QPushButton {
    background: transparent;
    color: #E8E3DA;
    border: 1px solid transparent;
    border-radius: 11px;
    padding: 5px 10px;
    font-size: 12px;
    font-weight: 550;
}
QFrame#floatingPill QPushButton:hover {
    background: rgba(255, 255, 255, 24);
    color: #FFFFFF;
}
QFrame#floatingPill QPushButton:disabled {
    color: rgba(232, 227, 218, 90);
}
QFrame#floatingPill QPushButton:checked {
    background: rgba(212, 138, 85, 58);
    color: #F6C197;
    border-color: rgba(212, 138, 85, 130);
}
QFrame#floatingPill QPushButton#pillDanger {
    color: #F28B82;
    padding: 5px 10px;
}
QFrame#floatingPill QPushButton#pillDanger:hover {
    background: rgba(224, 99, 91, 56);
    color: #FFFFFF;
}
QPushButton#pillHandle {
    background: rgba(212, 138, 85, 165);
    border: 1px solid rgba(255, 245, 232, 65);
    border-radius: 4px;
    padding: 0;
}
QPushButton#pillHandle:hover {
    background: rgba(212, 138, 85, 235);
}
QFrame#perfHud {
    background: rgba(20, 19, 17, 218);
    border: 1px solid rgba(242, 239, 233, 30);
    border-radius: 13px;
}
QFrame#perfHud QLabel {
    background: transparent;
    color: #E8E3DA;
    font-size: 11px;
}
QLabel#hudTitle {
    color: #E3A170;
    font-size: 12px;
    font-weight: 700;
}
QLabel#hudKey {
    color: #9E988E;
    font-size: 11px;
}
QLabel#hudValue {
    color: #F5F2EC;
    font-size: 11px;
    font-weight: 600;
}
QFrame#perfHud QPushButton {
    background: transparent;
    color: #9E988E;
    border: none;
    border-radius: 6px;
    padding: 2px 6px;
    font-size: 11px;
}
QFrame#perfHud QPushButton:hover {
    background: rgba(255, 255, 255, 20);
    color: #FFFFFF;
}
QLabel#sessionToast {
    background: rgba(22, 20, 18, 224);
    color: #F2EFE9;
    border: 1px solid rgba(242, 239, 233, 30);
    border-radius: 14px;
    padding: 7px 16px;
    font-size: 12px;
    font-weight: 550;
}
QFrame#reconnectOverlay {
    background: rgba(22, 20, 18, 240);
    border: 1px solid rgba(212, 138, 85, 90);
    border-radius: 16px;
}
QFrame#reconnectOverlay QLabel {
    background: transparent;
    color: #F2EFE9;
}
QLabel#reconnectTitle {
    font-size: 16px;
    font-weight: 700;
    color: #F2EFE9;
}
QLabel#reconnectDetail {
    font-size: 12px;
    color: #A8A297;
}
QFrame#reconnectOverlay QPushButton#reconnectPrimary {
    background: #D48A55;
    color: #1A1511;
    border: none;
    border-radius: 9px;
    padding: 8px 16px;
    font-weight: 650;
}
QFrame#reconnectOverlay QPushButton#reconnectPrimary:hover {
    background: #E09B68;
}
QFrame#reconnectOverlay QPushButton#reconnectCancel {
    background: rgba(224, 99, 91, 34);
    color: #F28B82;
    border: 1px solid rgba(224, 99, 91, 90);
    border-radius: 9px;
    padding: 8px 14px;
    font-weight: 600;
}
QFrame#reconnectOverlay QPushButton#reconnectCancel:hover {
    background: rgba(224, 99, 91, 64);
    color: #FFFFFF;
}
QFrame#dropOverlay {
    background: rgba(24, 21, 17, 195);
    border: 2px dashed #D48A55;
    border-radius: 16px;
}
QLabel#dropOverlayText {
    background: transparent;
    color: #F6C197;
    font-size: 16px;
    font-weight: 700;
}
QFrame#hostPill {
    background: rgba(24, 22, 19, 238);
    border: 1px solid rgba(212, 138, 85, 110);
    border-radius: 18px;
}
QFrame#hostPill QLabel {
    background: transparent;
    color: #F2EFE9;
    font-size: 12px;
    font-weight: 600;
}
QFrame#hostPill QLabel#hostPillDot {
    color: #47A87D;
    font-size: 13px;
}
QFrame#hostPill QLabel#hostPillDotPaused {
    color: #DE993E;
    font-size: 13px;
}
QFrame#hostPill QPushButton {
    background: rgba(255, 255, 255, 16);
    color: #F2EFE9;
    border: 1px solid transparent;
    border-radius: 10px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 600;
}
QFrame#hostPill QPushButton:hover {
    background: rgba(255, 255, 255, 28);
}
QFrame#hostPill QPushButton:checked {
    background: rgba(222, 153, 62, 58);
    color: #F6C197;
    border-color: rgba(222, 153, 62, 140);
}
QFrame#hostPill QPushButton#pillDanger {
    background: rgba(224, 99, 91, 38);
    color: #F28B82;
}
QFrame#hostPill QPushButton#pillDanger:hover {
    background: rgba(224, 99, 91, 78);
    color: #FFFFFF;
}
"""

SHORTCUT_COMBOS = (
    ("发送 Ctrl + Alt + Del", ("ctrl", "alt", "delete")),
    ("发送 Alt + Tab（切换窗口）", ("alt", "tab")),
    ("发送 Win / Cmd 键", ("cmd",)),
    ("发送 Win + L（锁定屏幕）", ("cmd", "l")),
    ("发送 Ctrl + Shift + Esc（任务管理器）", ("ctrl", "shift", "esc")),
    ("发送 Alt + F4（关闭远端窗口）", ("alt", "f4")),
)

SCALE_MODES = (
    ("fit", "适应窗口（推荐）"),
    ("actual", "原始像素 (1:1)"),
    ("fill", "铺满画布"),
)


class FloatingPillHandle(QPushButton):
    hovered = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("pillHandle")
        self.setFixedSize(68, 7)
        self.setToolTip("展开远程控制工具栏")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event):
        self.hovered.emit()
        super().enterEvent(event)


class FloatingPillToolbar(QFrame):
    monitor_selected = Signal(int)
    scale_mode_changed = Signal(str)
    combo_requested = Signal(tuple)
    swap_cmd_ctrl_toggled = Signal(bool)
    clipboard_mode_changed = Signal(str)
    clipboard_push_requested = Signal()
    audio_mute_toggled = Signal(bool)
    audio_volume_changed = Signal(float)
    release_requested = Signal()
    files_requested = Signal()
    hud_toggled = Signal(bool)
    fullscreen_requested = Signal()
    detach_requested = Signal()
    disconnect_requested = Signal()
    pin_toggled = Signal(bool)

    def __init__(self, network_label_text, parent=None):
        super().__init__(parent)
        self.setObjectName("floatingPill")
        self.setStyleSheet(OVERLAY_STYLE)
        self._dragging = False
        self._drag_origin = QPoint()
        self._offset_x = 0
        self._scale_mode = "fit"
        self._swap_cmd_ctrl = False
        self._clipboard_mode = "auto"
        self._audio_muted = False
        self._audio_volume = 1.0

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 5, 10, 5)
        row.setSpacing(6)

        self.grip = QLabel("⋮⋮")
        self.grip.setObjectName("pillGrip")
        self.grip.setToolTip("按住左右拖动工具栏位置")
        self.grip.setCursor(Qt.CursorShape.SizeHorCursor)
        row.addWidget(self.grip)

        self.monitor_selector = RoundedComboBox()
        self.monitor_selector.apply_theme(DARK_TOKENS)
        self.monitor_selector.setMinimumWidth(150)
        self.monitor_selector.setMaximumWidth(215)
        self.monitor_selector.addItem("正在读取显示器…")
        self.monitor_selector.setEnabled(False)
        self.monitor_selector.activated.connect(self.monitor_selected.emit)
        row.addWidget(self.monitor_selector)

        self.network_chip = QLabel(network_label_text)
        self.network_chip.setObjectName("pillChipOnline")
        row.addWidget(self.network_chip)

        # Scale mode dropdown button
        self.scale_btn = QPushButton("适应 ▾")
        self.scale_btn.setToolTip("画面缩放模式")
        self.scale_btn.clicked.connect(self._open_scale_menu)
        row.addWidget(self.scale_btn)

        # Input & Shortcut combos menu
        self.input_btn = QPushButton("键鼠 ▾")
        self.input_btn.setToolTip("发送系统组合键、Mac↔Win 映射或释放输入")
        self.input_btn.clicked.connect(self._open_input_menu)
        row.addWidget(self.input_btn)

        # Clipboard menu
        self.clipboard_btn = QPushButton("剪贴板 ▾")
        self.clipboard_btn.setEnabled(False)
        self.clipboard_btn.setToolTip("双向文本剪贴板同步")
        self.clipboard_btn.clicked.connect(self._open_clipboard_menu)
        row.addWidget(self.clipboard_btn)

        # Audio menu
        self.audio_btn = QPushButton("声音 ▾")
        self.audio_btn.setEnabled(False)
        self.audio_btn.setToolTip("远端系统音频（Opus）静音与音量控制")
        self.audio_btn.clicked.connect(self._open_audio_menu)
        row.addWidget(self.audio_btn)

        self.file_button = QPushButton("文件")
        self.file_button.setEnabled(False)
        self.file_button.setToolTip("双向文件互传")
        self.file_button.clicked.connect(self.files_requested.emit)
        row.addWidget(self.file_button)

        self.metrics_btn = QPushButton("性能")
        self.metrics_btn.setCheckable(True)
        self.metrics_btn.setToolTip("显示/隐藏半透明性能监控 HUD")
        self.metrics_btn.toggled.connect(self.hud_toggled.emit)
        row.addWidget(self.metrics_btn)

        self.full_btn = QPushButton("全屏")
        self.full_btn.setToolTip("切换全屏 (F11)")
        self.full_btn.clicked.connect(self.fullscreen_requested.emit)
        row.addWidget(self.full_btn)

        self.pin_btn = QPushButton("📌")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setChecked(False)
        self.pin_btn.setToolTip("图钉固定工具栏（取消固定后闲置自动收起）")
        self.pin_btn.toggled.connect(self.pin_toggled.emit)
        row.addWidget(self.pin_btn)

        self.disconnect_btn = QPushButton("断开")
        self.disconnect_btn.setObjectName("pillDanger")
        self.disconnect_btn.setToolTip("断开当前远程会话")
        self.disconnect_btn.clicked.connect(self.disconnect_requested.emit)
        row.addWidget(self.disconnect_btn)

        self.adjustSize()

    @property
    def offset_x(self):
        return self._offset_x

    def set_network_chip(self, text, relay=False):
        self.network_chip.setText(text)
        self.network_chip.setObjectName("pillChip" if relay else "pillChipOnline")
        self.network_chip.style().unpolish(self.network_chip)
        self.network_chip.style().polish(self.network_chip)
        self.adjustSize()

    def set_scale_mode_label(self, mode):
        self._scale_mode = mode
        short = {"fit": "适应 ▾", "actual": "1:1 ▾", "fill": "铺满 ▾"}.get(mode, "适应 ▾")
        self.scale_btn.setText(short)
        self.adjustSize()

    def _open_scale_menu(self):
        menu = QMenu(self)
        for key, label in SCALE_MODES:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._scale_mode == key)
            act.triggered.connect(lambda checked=False, m=key: self._select_scale(m))
        menu.addSeparator()
        detach = menu.addAction("拆分为独立窗口")
        detach.triggered.connect(self.detach_requested.emit)
        menu.exec(self.scale_btn.mapToGlobal(self.scale_btn.rect().bottomLeft()))

    def _select_scale(self, mode):
        self.set_scale_mode_label(mode)
        self.scale_mode_changed.emit(mode)

    def set_swap_cmd_ctrl(self, enabled):
        self._swap_cmd_ctrl = bool(enabled)

    def set_clipboard_mode(self, mode):
        self._clipboard_mode = mode
        labels = {"auto": "剪贴板 ▾", "manual": "剪贴板·手动 ▾", "off": "剪贴板·关 ▾"}
        self.clipboard_btn.setText(labels.get(mode, "剪贴板 ▾"))
        self.adjustSize()

    def _open_input_menu(self):
        menu = QMenu(self)
        for label, keys in SHORTCUT_COMBOS:
            act = menu.addAction(label)
            act.triggered.connect(lambda checked=False, k=keys: self.combo_requested.emit(k))
        menu.addSeparator()
        swap = menu.addAction("Mac ↔ Win 修饰键互换 (Cmd ↔ Ctrl)")
        swap.setCheckable(True)
        swap.setChecked(self._swap_cmd_ctrl)
        swap.toggled.connect(self._on_swap_toggled)
        menu.addSeparator()
        rel = menu.addAction("释放键盘鼠标 (Ctrl+Alt+Shift)")
        rel.triggered.connect(self.release_requested.emit)
        menu.exec(self.input_btn.mapToGlobal(self.input_btn.rect().bottomLeft()))

    def _on_swap_toggled(self, checked):
        self._swap_cmd_ctrl = checked
        self.swap_cmd_ctrl_toggled.emit(checked)

    def _open_clipboard_menu(self):
        menu = QMenu(self)
        for mode, label in (
            ("auto", "双向自动同步"),
            ("manual", "仅手动推送"),
            ("off", "禁用剪贴板同步"),
        ):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._clipboard_mode == mode)
            act.triggered.connect(lambda checked=False, m=mode: self._select_clipboard_mode(m))
        menu.addSeparator()
        push = menu.addAction("立即发送本机剪贴板到远端")
        push.setEnabled(self._clipboard_mode != "off")
        push.triggered.connect(self.clipboard_push_requested.emit)
        menu.exec(self.clipboard_btn.mapToGlobal(self.clipboard_btn.rect().bottomLeft()))

    def _select_clipboard_mode(self, mode):
        self.set_clipboard_mode(mode)
        self.clipboard_mode_changed.emit(mode)

    def set_audio_state(self, available, muted=False, volume=1.0):
        self._audio_muted = bool(muted)
        self._audio_volume = max(0.0, min(1.0, float(volume)))
        self.audio_btn.setEnabled(bool(available))
        if not available:
            self.audio_btn.setText("声音 ▾")
            self.audio_btn.setToolTip("被控端未开启音频共享或当前平台暂不支持采集")
        elif self._audio_muted or self._audio_volume <= 0.0:
            self.audio_btn.setText("静音 ▾")
            self.audio_btn.setToolTip("远端音频已静音，点击调整音量")
        else:
            pct = int(round(self._audio_volume * 100))
            self.audio_btn.setText(f"声音 {pct}% ▾" if pct < 100 else "声音 ▾")
            self.audio_btn.setToolTip("远端 Opus 音频播放中，点击调整音量或静音")
        self.adjustSize()

    def _open_audio_menu(self):
        menu = QMenu(self)
        mute_act = menu.addAction("静音" if not self._audio_muted else "取消静音")
        mute_act.setCheckable(True)
        mute_act.setChecked(self._audio_muted)
        mute_act.triggered.connect(lambda checked=False: self._set_audio_muted(checked))
        menu.addSeparator()
        for label, vol in (("音量 100%", 1.0), ("音量 75%", 0.75), ("音量 50%", 0.5), ("音量 25%", 0.25)):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(not self._audio_muted and abs(self._audio_volume - vol) < 0.05)
            act.triggered.connect(lambda checked=False, v=vol: self._set_audio_volume(v))
        menu.exec(self.audio_btn.mapToGlobal(self.audio_btn.rect().bottomLeft()))

    def _set_audio_muted(self, muted):
        self.set_audio_state(self.audio_btn.isEnabled(), muted=muted, volume=self._audio_volume)
        self.audio_mute_toggled.emit(bool(muted))

    def _set_audio_volume(self, volume):
        self.set_audio_state(self.audio_btn.isEnabled(), muted=False, volume=volume)
        self.audio_mute_toggled.emit(False)
        self.audio_volume_changed.emit(float(volume))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.childAt(event.position().toPoint()) in (None, self.grip, self.network_chip):
            self._dragging = True
            self._drag_origin = event.globalPosition().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and self.parentWidget():
            delta = event.globalPosition().toPoint().x() - self._drag_origin.x()
            self._drag_origin = event.globalPosition().toPoint()
            max_offset = max(0, (self.parentWidget().width() - self.width()) // 2 - 12)
            self._offset_x = max(-max_offset, min(max_offset, self._offset_x + delta))
            self.reposition()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def reposition(self):
        parent = self.parentWidget()
        if not parent:
            return
        self.adjustSize()
        max_offset = max(0, (parent.width() - self.width()) // 2 - 12)
        self._offset_x = max(-max_offset, min(max_offset, self._offset_x))
        x = (parent.width() - self.width()) // 2 + self._offset_x
        self.move(max(8, x), 12)


class PerformanceHUD(QFrame):
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("perfHud")
        self.setStyleSheet(OVERLAY_STYLE)
        self.setFixedWidth(268)
        self._dragging = False
        self._drag_start = QPoint()
        self._custom_pos = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 12, 12)
        outer.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("● 实时性能 HUD")
        title.setObjectName("hudTitle")
        close_btn = QPushButton("✕")
        close_btn.clicked.connect(self._close)
        head.addWidget(title, 1)
        head.addWidget(close_btn)
        outer.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        self.fields = {}
        rows = (
            ("stream", "分辨率"),
            ("encoder", "编码 / 采集"),
            ("fps", "绘制 / 解 / 编"),
            ("network", "RTT / 码率"),
            ("host_ms", "采集编 / 发等"),
            ("client_ms", "解 / 转 / 绘"),
            ("age", "主机帧龄"),
        )
        for i, (key, label) in enumerate(rows):
            k_lbl = QLabel(label)
            k_lbl.setObjectName("hudKey")
            v_lbl = QLabel("—")
            v_lbl.setObjectName("hudValue")
            v_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(k_lbl, i, 0)
            grid.addWidget(v_lbl, i, 1)
            self.fields[key] = v_lbl
        outer.addLayout(grid)
        self.adjustSize()
        self.hide()

    def update_metrics(self, source, stats, painted_fps, paint_ms):
        src_w = source.get("capture_width", source.get("source_width", "—"))
        src_h = source.get("capture_height", source.get("source_height", "—"))
        dst_w = source.get("width", "—")
        dst_h = source.get("height", "—")
        self.fields["stream"].setText(f"{src_w}×{src_h} → {dst_w}×{dst_h}")
        enc = source.get("encoder", "H.264")
        cap = source.get("capture", "—")
        self.fields["encoder"].setText(f"{enc} · {cap}")
        self.fields["fps"].setText(
            f"{painted_fps:.1f} / {stats.get('decoded_fps', '—')} / {stats.get('encoded_fps', '—')} FPS"
        )
        self.fields["network"].setText(
            f"{stats.get('rtt_ms', '—')} ms · {stats.get('mbps', '—')} Mbps"
        )
        self.fields["host_ms"].setText(
            f"{stats.get('capture_encode_ms', '—')} / {stats.get('send_ms', '—')} ms"
        )
        self.fields["client_ms"].setText(
            f"{stats.get('decode_ms', '—')} / {stats.get('convert_ms', '—')} / {paint_ms:.1f} ms"
        )
        age = stats.get("capture_age_ms")
        self.fields["age"].setText(f"{age} ms" if age is not None else "—")

    def reposition(self):
        parent = self.parentWidget()
        if not parent:
            return
        self.adjustSize()
        if self._custom_pos is None:
            self.move(max(12, parent.width() - self.width() - 16), 62)
        else:
            x = max(8, min(parent.width() - self.width() - 8, self._custom_pos.x()))
            y = max(8, min(parent.height() - self.height() - 8, self._custom_pos.y()))
            self.move(x, y)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start = event.globalPosition().toPoint() - self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and self.parentWidget():
            target = event.globalPosition().toPoint() - self._drag_start
            self._custom_pos = target
            self.reposition()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _close(self):
        self.hide()
        self.closed.emit()


class SessionToast(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sessionToast")
        self.setStyleSheet(OVERLAY_STYLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, message, duration_ms=3200):
        self.setText(message)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        if duration_ms > 0:
            self._timer.start(duration_ms)
        else:
            self._timer.stop()

    def reposition(self):
        parent = self.parentWidget()
        if not parent:
            return
        self.adjustSize()
        x = (parent.width() - self.width()) // 2
        y = max(12, parent.height() - self.height() - 20)
        self.move(max(12, x), y)


class ReconnectOverlay(QFrame):
    retry_requested = Signal()
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("reconnectOverlay")
        self.setStyleSheet(OVERLAY_STYLE)
        self.setFixedWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        self.title = QLabel("连接已中断")
        self.title.setObjectName("reconnectTitle")
        layout.addWidget(self.title)

        self.detail = QLabel("正在准备重新连接…")
        self.detail.setObjectName("reconnectDetail")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.retry_btn = QPushButton("立即重试")
        self.retry_btn.setObjectName("reconnectPrimary")
        self.retry_btn.clicked.connect(self.retry_requested.emit)
        self.cancel_btn = QPushButton("断开关闭")
        self.cancel_btn.setObjectName("reconnectCancel")
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)
        row.addWidget(self.retry_btn, 1)
        row.addWidget(self.cancel_btn)
        layout.addLayout(row)
        self.adjustSize()
        self.hide()

    def show_state(self, title, detail, retry_enabled=True):
        self.title.setText(title)
        self.detail.setText(detail)
        self.retry_btn.setEnabled(retry_enabled)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()

    def reposition(self):
        parent = self.parentWidget()
        if not parent:
            return
        self.adjustSize()
        x = (parent.width() - self.width()) // 2
        y = (parent.height() - self.height()) // 2
        self.move(max(12, x), max(12, y))


class DropUploadOverlay(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dropOverlay")
        self.setStyleSheet(OVERLAY_STYLE)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        self.label = QLabel("松开以发送文件到远端电脑")
        self.label.setObjectName("dropOverlayText")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label)
        self.hide()

    def show_count(self, count):
        self.label.setText(f"松开以发送 {count} 个文件 / 文件夹到远端电脑")
        self.reposition()
        self.show()
        self.raise_()

    def reposition(self):
        parent = self.parentWidget()
        if not parent:
            return
        self.setGeometry(18, 18, max(120, parent.width() - 36), max(100, parent.height() - 36))


class HostSessionPill(QFrame):
    """Always-on-top compact desktop pill shown on the controlled host while a viewer is connected."""

    pause_input_toggled = Signal(bool)
    disconnect_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("hostPill")
        self.setStyleSheet(OVERLAY_STYLE)
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self._dragging = False
        self._drag_origin = QPoint()
        self._elapsed = 0
        self._view_only = False
        self._collapsed = False

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 10, 6)
        row.setSpacing(8)

        self.dot = QLabel("●")
        self.dot.setObjectName("hostPillDot")
        row.addWidget(self.dot)

        self.status_label = QLabel("正在被控制 · 00:00")
        row.addWidget(self.status_label)

        self.pause_btn = QPushButton("暂停输入")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip("临时冻结远端鼠标键盘，不中断画面")
        self.pause_btn.toggled.connect(self._on_pause_toggled)
        row.addWidget(self.pause_btn)

        self.disconnect_btn = QPushButton("断开")
        self.disconnect_btn.setObjectName("pillDanger")
        self.disconnect_btn.setToolTip("立即结束当前远程连接")
        self.disconnect_btn.clicked.connect(self.disconnect_requested.emit)
        row.addWidget(self.disconnect_btn)

        self.collapse_btn = QPushButton("⌄")
        self.collapse_btn.setToolTip("折叠 / 展开控制条")
        self.collapse_btn.clicked.connect(self.toggle_collapsed)
        row.addWidget(self.collapse_btn)

        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick)
        self.adjustSize()
        self.hide()

    def show_session(self, view_only=False, input_paused=False):
        self._view_only = bool(view_only)
        self._elapsed = 0
        self._collapsed = False
        self.pause_btn.blockSignals(True)
        self.pause_btn.setChecked(bool(input_paused))
        self.pause_btn.setVisible(not self._view_only)
        self.pause_btn.setText("恢复输入" if input_paused else "暂停输入")
        self.pause_btn.blockSignals(False)
        self.disconnect_btn.show()
        self.collapse_btn.setText("⌄")
        self._refresh_text(input_paused)
        self._clock.start()
        self.adjustSize()
        self._dock_top_right()
        self.show()
        self.raise_()

    def hide_session(self):
        self._clock.stop()
        self.hide()

    def _dock_top_right(self):
        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.right() - self.width() - 20, geo.top() + 18)

    def _refresh_text(self, paused=None):
        is_paused = self.pause_btn.isChecked() if paused is None else bool(paused)
        mm, ss = divmod(self._elapsed, 60)
        if self._collapsed:
            self.status_label.setText(f"{mm:02d}:{ss:02d}")
        elif self._view_only:
            self.status_label.setText(f"正在被查看 · {mm:02d}:{ss:02d}")
        elif is_paused:
            self.status_label.setText(f"输入已暂停 · {mm:02d}:{ss:02d}")
        else:
            self.status_label.setText(f"正在被控制 · {mm:02d}:{ss:02d}")
        self.dot.setObjectName("hostPillDotPaused" if is_paused else "hostPillDot")
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)

    def _tick(self):
        self._elapsed += 1
        self._refresh_text()

    def _on_pause_toggled(self, paused):
        self.pause_btn.setText("恢复输入" if paused else "暂停输入")
        self._refresh_text(paused)
        self.pause_input_toggled.emit(bool(paused))

    def toggle_collapsed(self):
        self._collapsed = not self._collapsed
        self.pause_btn.setVisible(not self._collapsed and not self._view_only)
        self.disconnect_btn.setVisible(not self._collapsed)
        self.collapse_btn.setText("⌃" if self._collapsed else "⌄")
        self._refresh_text()
        self.adjustSize()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)



