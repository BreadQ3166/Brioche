"""Collapsible right-side inspector drawer for per-device profile and channel details."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from .protocol import FRAME_RATES
from .ui_components import RoundedComboBox


class DeviceInspectorDrawer(QFrame):
    connect_requested = Signal(dict)
    lan_connect_requested = Signal(dict)
    bind_lan_requested = Signal(dict)
    files_requested = Signal(dict)
    profile_changed = Signal(str, dict)
    remove_lan_requested = Signal(str, int)
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("drawer")
        self.setFixedWidth(296)
        self._device = None
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # Header row: title + favorite + close
        header = QHBoxLayout()
        header.setSpacing(6)
        self.title_label = QLabel("未选择设备")
        self.title_label.setObjectName("subheading")
        self.title_label.setWordWrap(True)
        self.fav_btn = QPushButton("☆")
        self.fav_btn.setObjectName("ghost")
        self.fav_btn.setToolTip("收藏 / 置顶此设备")
        self.fav_btn.setFixedSize(32, 32)
        self.fav_btn.clicked.connect(self._toggle_favorite)
        close_btn = QPushButton("✕")
        close_btn.setObjectName("ghost")
        close_btn.setToolTip("收起详情面板")
        close_btn.setFixedSize(32, 32)
        close_btn.clicked.connect(self._close)
        header.addWidget(self.title_label, 1)
        header.addWidget(self.fav_btn)
        header.addWidget(close_btn)
        layout.addLayout(header)

        # Status & platform row
        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)
        self.status_badge = QLabel("离线")
        self.status_badge.setObjectName("badgeOffline")
        self.platform_label = QLabel("")
        self.platform_label.setObjectName("muted")
        meta_row.addWidget(self.status_badge)
        meta_row.addWidget(self.platform_label, 1)
        layout.addLayout(meta_row)

        self.status_detail = QLabel("")
        self.status_detail.setObjectName("muted")
        self.status_detail.setWordWrap(True)
        layout.addWidget(self.status_detail)

        # Primary Connect & Direct File Manager CTAs
        cta_row = QHBoxLayout()
        cta_row.setSpacing(8)
        self.connect_btn = QPushButton("立即连接")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setMinimumHeight(38)
        self.connect_btn.clicked.connect(self._emit_connect)
        self.files_btn = QPushButton("文件管理")
        self.files_btn.setMinimumHeight(38)
        self.files_btn.setToolTip("直接浏览远端文件夹并互传文件（无需开启远程桌面画面）")
        self.files_btn.clicked.connect(self._emit_files)
        cta_row.addWidget(self.connect_btn, 2)
        cta_row.addWidget(self.files_btn, 1)
        layout.addLayout(cta_row)

        layout.addWidget(self._divider())

        # Device metadata (alias & group)
        meta_title = QLabel("设备信息")
        meta_title.setObjectName("sectionLabel")
        layout.addWidget(meta_title)

        meta_form = QFormLayout()
        meta_form.setSpacing(8)
        self.alias_input = QLineEdit()
        self.alias_input.setPlaceholderText("自定义备注名")
        self.alias_input.editingFinished.connect(self._save_profile)
        self.group_input = QLineEdit()
        self.group_input.setPlaceholderText("例如 办公室 / 家里")
        self.group_input.editingFinished.connect(self._save_profile)
        meta_form.addRow("备注名", self.alias_input)
        meta_form.addRow("分组", self.group_input)
        layout.addLayout(meta_form)

        layout.addWidget(self._divider())

        # Per-device connection profile
        prof_header = QHBoxLayout()
        prof_title = QLabel("此设备独立配置")
        prof_title.setObjectName("sectionLabel")
        self.reset_btn = QPushButton("恢复全局")
        self.reset_btn.setObjectName("ghost")
        self.reset_btn.clicked.connect(self._reset_profile)
        prof_header.addWidget(prof_title, 1)
        prof_header.addWidget(self.reset_btn)
        layout.addLayout(prof_header)

        form = QFormLayout()
        form.setSpacing(8)
        self.resolution = RoundedComboBox()
        for label, value in (
            ("跟随全局默认", None),
            ("2K 上限", "1440p"),
            ("1080p 上限", "1080p"),
            ("720p 上限", "720p"),
            ("原生 · 最高4K", "native"),
        ):
            self.resolution.addItem(label, value)

        self.fps = RoundedComboBox()
        self.fps.addItem("跟随全局默认", None)
        for rate in FRAME_RATES:
            self.fps.addItem(f"{rate} FPS", rate)

        self.bitrate = QSpinBox()
        self.bitrate.setRange(0, 80)
        self.bitrate.setSpecialValueText("跟随全局")
        self.bitrate.setSuffix(" Mbps")

        self.match_display = RoundedComboBox()
        self.match_display.addItem("跟随全局默认", None)
        self.match_display.addItem("开启（Ubuntu Xorg）", True)
        self.match_display.addItem("仅缩放视频", False)

        self.preferred_channel = RoundedComboBox()
        self.preferred_channel.addItem("自动择优（LAN 优先）", "auto")
        self.preferred_channel.addItem("仅局域网直连（不走外网）", "lan_only")
        self.preferred_channel.addItem("优先局域网（离线回退外网）", "lan")
        self.preferred_channel.addItem("优先外网 Tailscale", "tailscale")

        for label, widget in (
            ("分辨率", self.resolution),
            ("帧率上限", self.fps),
            ("目标码率", self.bitrate),
            ("源屏匹配", self.match_display),
            ("优先通道", self.preferred_channel),
        ):
            form.addRow(label, widget)
        layout.addLayout(form)

        for combo in (self.resolution, self.fps, self.match_display, self.preferred_channel):
            combo.currentIndexChanged.connect(self._save_profile)
        self.bitrate.valueChanged.connect(self._save_profile)

        layout.addWidget(self._divider())

        # Channels summary
        ch_title = QLabel("可用通道")
        ch_title.setObjectName("sectionLabel")
        layout.addWidget(ch_title)

        self.channels_label = QLabel("—")
        self.channels_label.setObjectName("muted")
        self.channels_label.setWordWrap(True)
        layout.addWidget(self.channels_label)

        lan_row = QHBoxLayout()
        lan_row.setSpacing(8)
        self.lan_direct_btn = QPushButton("仅通过局域网连接")
        self.lan_direct_btn.setToolTip("强制通过局域网直连此设备，不经过 Tailscale 外网")
        self.lan_direct_btn.clicked.connect(self._emit_lan_connect)
        self.bind_lan_btn = QPushButton("指定局域网 IP")
        self.bind_lan_btn.setObjectName("ghost")
        self.bind_lan_btn.setToolTip("手动输入或选择该设备的局域网 IP 地址并直连")
        self.bind_lan_btn.clicked.connect(self._emit_bind_lan)
        lan_row.addWidget(self.lan_direct_btn, 1)
        lan_row.addWidget(self.bind_lan_btn)
        layout.addLayout(lan_row)

        self.remove_btn = QPushButton("从局域网目录移除")
        self.remove_btn.setObjectName("danger")
        self.remove_btn.clicked.connect(self._emit_remove)
        layout.addWidget(self.remove_btn)

        layout.addStretch()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self.hide()

    @staticmethod
    def _divider():
        line = QFrame()
        line.setObjectName("divider")
        return line

    def apply_theme(self, tokens):
        for combo in (self.resolution, self.fps, self.match_display, self.preferred_channel):
            combo.apply_theme(tokens)

    @property
    def current_key(self):
        return self._device["key"] if self._device else None

    def show_device(self, device):
        self._loading = True
        self._device = device
        try:
            self.title_label.setText(device["display_name"])
            self.fav_btn.setText("★" if device.get("favorite") else "☆")
            tone = device.get("badge_tone", "offline")
            self.status_badge.setObjectName(
                "badgeOnline"
                if tone == "online"
                else ("badgeRelay" if tone == "relay" else "badgeOffline")
            )
            self.status_badge.setText(device.get("badge_text", "离线"))
            self.status_badge.style().unpolish(self.status_badge)
            self.status_badge.style().polish(self.status_badge)
            self.platform_label.setText(device.get("platform") or "桌面设备")
            self.status_detail.setText(device.get("status_text") or "")

            connectable = bool(device.get("connectable"))
            active = device.get("active_channel") or {}
            needs_pair = device.get("active_mode") == "lan" and not active.get("token")
            self.connect_btn.setEnabled(connectable)
            self.files_btn.setEnabled(connectable)
            self.connect_btn.setText(
                "发起配对并连接" if (connectable and needs_pair) else ("立即连接" if connectable else "设备不可连接")
            )

            profile = device.get("profile") or {}
            self.alias_input.setText(profile.get("alias", ""))
            self.group_input.setText(profile.get("group", ""))
            self._set_combo(self.resolution, profile.get("resolution"))
            self._set_combo(self.fps, profile.get("fps"))
            self.bitrate.setValue(int(profile.get("bitrate", 0) or 0))
            self._set_combo(
                self.match_display,
                profile["match_display"] if "match_display" in profile else None,
            )
            self._set_combo(
                self.preferred_channel, profile.get("preferred_channel", "auto")
            )

            lines = []
            channels = device.get("channels") or {}
            if "lan" in channels:
                lan = channels["lan"]
                state = "在线" if lan.get("online") else "离线"
                paired = "已配对" if lan.get("token") else "未配对"
                lines.append(f"• 局域网：{lan['ip']}:{lan.get('port', 47995)}（{state} · {paired}）")
            if "tailscale" in channels:
                ts = channels["tailscale"]
                state = "在线" if ts.get("reachable") else "离线"
                path = ts.get("path") or "待检测"
                lines.append(f"• 外网：{ts['ip']}（{state} · {path}）")
            self.channels_label.setText("\n".join(lines) or "—")
            lan_ch = channels.get("lan")
            lan_ready = bool(lan_ch and lan_ch.get("connectable"))
            self.lan_direct_btn.setVisible(bool(lan_ch))
            self.lan_direct_btn.setEnabled(lan_ready)
            self.lan_direct_btn.setText(
                "仅局域网直连" if lan_ready else "局域网通道离线"
            )
            self.bind_lan_btn.setText("修改 LAN IP" if lan_ch else "指定局域网 IP 连接")
            self.remove_btn.setVisible("lan" in channels)
            self.show()
        finally:
            self._loading = False

    @staticmethod
    def _set_combo(combo, value):
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def _toggle_favorite(self):
        if not self._device:
            return
        new_fav = not bool(self._device.get("favorite"))
        self._device["favorite"] = new_fav
        self.fav_btn.setText("★" if new_fav else "☆")
        self._save_profile()

    def _reset_profile(self):
        if not self._device:
            return
        self._loading = True
        try:
            self.resolution.setCurrentIndex(0)
            self.fps.setCurrentIndex(0)
            self.bitrate.setValue(0)
            self.match_display.setCurrentIndex(0)
            self.preferred_channel.setCurrentIndex(0)
        finally:
            self._loading = False
        self._save_profile()

    def _save_profile(self):
        if self._loading or not self._device:
            return
        bitrate_val = self.bitrate.value()
        if bitrate_val == 1:
            bitrate_val = 2
            self.bitrate.blockSignals(True)
            self.bitrate.setValue(2)
            self.bitrate.blockSignals(False)
        res = self.resolution.currentData()
        match = self.match_display.currentData()
        if res == "native" and match is True:
            match = False
        payload = {
            "alias": self.alias_input.text().strip() or None,
            "group": self.group_input.text().strip() or None,
            "favorite": True if self._device.get("favorite") else None,
            "resolution": res,
            "fps": self.fps.currentData(),
            "bitrate": bitrate_val if bitrate_val >= 2 else None,
            "match_display": match,
            "preferred_channel": (
                self.preferred_channel.currentData()
                if self.preferred_channel.currentData() != "auto"
                else None
            ),
        }
        self.profile_changed.emit(self._device["key"], payload)

    def _emit_connect(self):
        if self._device:
            self.connect_requested.emit(self._device)

    def _emit_lan_connect(self):
        if self._device:
            self.lan_connect_requested.emit(self._device)

    def _emit_bind_lan(self):
        if self._device:
            self.bind_lan_requested.emit(self._device)

    def _emit_files(self):
        if self._device:
            self.files_requested.emit(self._device)

    def _emit_remove(self):
        if not self._device or "lan" not in self._device.get("channels", {}):
            return
        lan = self._device["channels"]["lan"]
        self.remove_lan_requested.emit(lan["ip"], int(lan.get("port", 47995)))

    def _close(self):
        self._device = None
        self.hide()
        self.closed.emit()
