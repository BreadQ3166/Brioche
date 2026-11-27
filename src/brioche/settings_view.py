"""Modern two-column categorized settings view."""

import platform
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from .protocol import FRAME_RATES
from .ui_components import RoundedComboBox


def _muted(text):
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setWordWrap(True)
    return lbl


def _card(title, subtitle=""):
    frame = QFrame()
    frame.setObjectName("surfaceCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(10)
    head = QLabel(title)
    head.setObjectName("subheading")
    layout.addWidget(head)
    if subtitle:
        layout.addWidget(_muted(subtitle))
    return frame, layout


class SettingsView(QWidget):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._combos = []

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 14)
        root.setSpacing(12)

        heading = QLabel("设置")
        heading.setObjectName("heading")
        root.addWidget(heading)
        self.settings_note = _muted(
            "外观与全局连接参数会实时保存；修改屏幕、编码或权限设置后，将在下次开启共享时生效。"
        )
        root.addWidget(self.settings_note)

        body = QHBoxLayout()
        body.setSpacing(16)

        # Left category menu
        nav_frame = QFrame()
        nav_frame.setObjectName("surfaceCard")
        nav_frame.setFixedWidth(168)
        nav_col = QVBoxLayout(nav_frame)
        nav_col.setContentsMargins(10, 12, 10, 12)
        nav_col.setSpacing(4)

        self.stack = QStackedWidget()
        self.cat_buttons = []
        categories = (
            ("常规与外观", self._general_page()),
            ("画质与性能", self._video_page()),
            ("共享与安全", self._sharing_page()),
            ("网络与浏览器", self._network_page()),
            ("高级设置", self._advanced_page()),
        )
        for idx, (label, widget) in enumerate(categories):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setObjectName("settingsNav")
            btn.clicked.connect(lambda checked=False, i=idx: self.select_category(i))
            self.cat_buttons.append(btn)
            nav_col.addWidget(btn)
            self.stack.addWidget(widget)
        nav_col.addStretch()

        body.addWidget(nav_frame)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        self.select_category(0)

    def _combo(self):
        c = RoundedComboBox()
        self._combos.append(c)
        return c

    def apply_theme(self, tokens):
        for combo in self._combos:
            combo.apply_theme(tokens)

    def select_category(self, index):
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self.cat_buttons):
            btn.setChecked(i == index)

    @staticmethod
    def _wrap_scroll(inner):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        return scroll

    def _general_page(self):
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(14)

        card, layout = _card("外观与主题", "保留 Brioche 温暖克制风格，支持浅色暖白与深烘深色模式。")
        form = QFormLayout()
        form.setSpacing(10)
        self.theme_combo = self._combo()
        for label, value in (
            ("跟随系统", "system"),
            ("暖白浅色（Warm Paper）", "light"),
            ("深烘深色（Dark Roast）", "dark"),
        ):
            self.theme_combo.addItem(label, value)
        self.theme_combo.setCurrentIndex(
            max(0, self.theme_combo.findData(self.store.setting("theme", "system")))
        )
        self.device_name = QLineEdit(self.store.name)
        self.device_name.setPlaceholderText("显示在局域网目录中的本机名称")
        form.addRow("界面主题", self.theme_combo)
        form.addRow("本机名称", self.device_name)
        layout.addLayout(form)

        self.reduced_motion = QCheckBox("减少动态效果（关闭卡片悬停及页面过渡动画）")
        self.reduced_motion.setChecked(bool(self.store.setting("reduced_motion", False)))
        layout.addWidget(self.reduced_motion)

        self.auto_reconnect = QCheckBox("网络波动断开时自动尝试重连（证书/权限错误不重试）")
        self.auto_reconnect.setChecked(bool(self.store.setting("auto_reconnect", True)))
        layout.addWidget(self.auto_reconnect)

        self.sync_audio = QCheckBox("连接远程电脑时默认同步远端声音（Opus 低延迟音频）")
        self.sync_audio.setChecked(bool(self.store.setting("sync_audio", True)))
        layout.addWidget(self.sync_audio)

        clip_form = QFormLayout()
        clip_form.setSpacing(10)
        self.clipboard_mode = self._combo()
        for label, value in (
            ("双向自动同步（推荐）", "auto"),
            ("仅手动推送", "manual"),
            ("禁用剪贴板同步", "off"),
        ):
            self.clipboard_mode.addItem(label, value)
        self.clipboard_mode.setCurrentIndex(
            max(0, self.clipboard_mode.findData(self.store.setting("clipboard_mode", "auto")))
        )
        clip_form.addRow("控制端剪贴板策略", self.clipboard_mode)
        layout.addLayout(clip_form)
        col.addWidget(card)

        exit_card, exit_layout = _card(
            "后台运行与退出",
            "关闭主窗口会收起到系统托盘（无托盘时最小化），本机共享与文件传输继续运行。",
        )
        self.exit_button = QPushButton("彻底退出 Brioche")
        self.exit_button.setObjectName("danger")
        exit_layout.addWidget(self.exit_button)
        col.addWidget(exit_card)
        col.addStretch()
        return self._wrap_scroll(page)

    def _video_page(self):
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(14)

        card, layout = _card(
            "全局默认画质",
            "应用于未单独配置的所有设备。选中单台设备后可在右侧抽屉覆盖该设备的专属画质。",
        )
        form = QFormLayout()
        form.setSpacing(10)
        self.profile = self._combo()
        for label, data in (
            ("均衡 · 2K / 60 FPS", (60, 16)),
            ("高刷 · 2K / 90 FPS", (90, 20)),
            ("极致 · 2K / 144 FPS", (144, 35)),
            ("自定义", None),
        ):
            self.profile.addItem(label, data)

        self.resolution = self._combo()
        for label, value in (
            ("2K 上限", "1440p"),
            ("1080p 上限", "1080p"),
            ("720p 上限", "720p"),
            ("原生 · 最高4K", "native"),
        ):
            self.resolution.addItem(label, value)
        self.resolution.setCurrentIndex(
            max(0, self.resolution.findData(self.store.setting("resolution", "1440p")))
        )

        self.fps = self._combo()
        for fps in FRAME_RATES:
            self.fps.addItem(f"{fps} FPS", fps)
        self.fps.setCurrentIndex(
            max(0, self.fps.findData(self.store.setting("fps", 60)))
        )

        self.bitrate = QSpinBox()
        self.bitrate.setRange(2, 80)
        self.bitrate.setValue(int(self.store.setting("bitrate", 16)))
        self.bitrate.setSuffix(" Mbps")

        self.encoder = self._combo()
        self.encoder.addItem("自动 · 优先可用硬件编码", "auto")
        self.encoder.addItem("兼容 · 软件 H.264", "libx264")
        if platform.system() == "Linux":
            self.encoder.addItem("防频闪对照 · 兼容采集＋软件 H.264", "stable")
        self.encoder.setCurrentIndex(
            max(0, self.encoder.findData(self.store.setting("encoder", "auto")))
        )

        form.addRow("画质预设", self.profile)
        form.addRow("默认分辨率", self.resolution)
        form.addRow("帧率上限", self.fps)
        form.addRow("目标码率", self.bitrate)
        form.addRow("被控编码策略", self.encoder)
        layout.addLayout(form)

        self.match_display = QCheckBox("源屏也匹配（连接期间临时匹配被控屏幕分辨率，断开恢复 · Ubuntu Xorg）")
        self.match_display.setChecked(bool(self.store.setting("match_display", False)))
        self.match_display.setEnabled(self.resolution.currentData() != "native")
        layout.addWidget(self.match_display)
        col.addWidget(card)

        v2_card, v2_layout = _card(
            "下一代串流内核（brioche-core v2.0 · Rust + UDP/QUIC + GPU）",
            "采用多条带 RS-FEC 加密 UDP + QUIC 钉扎控制面 + HEVC YUV 4:4:4 文字锐利 + wgpu 原生着色器渲染。",
        )
        v2_form = QFormLayout()
        v2_form.setSpacing(10)
        self.stream_engine = self._combo()
        self.stream_engine.addItem("智能 GPU 加速引擎（默认 · OpenGL YUV/CAS + 硬解 + 多标签）", "auto")
        self.stream_engine.addItem("v2.0 原生 Rust UDP/RS-FEC + wgpu 引擎（局域网 4:4:4 极速）", "v2_core")
        self.stream_engine.addItem("v1.x 兼容引擎（TLS/TCP + 软件渲染）", "v1_compat")
        self.stream_engine.setCurrentIndex(
            max(0, self.stream_engine.findData(self.store.setting("stream_engine", "auto")))
        )
        v2_form.addRow("串流引擎模式", self.stream_engine)
        v2_layout.addLayout(v2_form)

        self.chroma_444 = QCheckBox("开启 YUV 4:4:4 文字锐利模式（4K/2K 办公与代码无色度模糊，需硬件支持）")
        self.chroma_444.setChecked(bool(self.store.setting("chroma_444", True)))
        v2_layout.addWidget(self.chroma_444)

        self.core_engine_status = QLabel("正在检测 brioche-core 原生引擎与 GPU 能力…")
        self.core_engine_status.setObjectName("muted")
        self.core_engine_status.setWordWrap(True)
        v2_layout.addWidget(self.core_engine_status)
        col.addWidget(v2_card)

        col.addStretch()
        return self._wrap_scroll(page)

    def _sharing_page(self):
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(14)

        card, layout = _card(
            "本机被控权限",
            "控制其他设备如何连接这台电脑。局域网和外网共享可同时开启，共用单活动会话互斥。",
        )
        self.external_allowed = QCheckBox("允许同账号 Tailscale 设备连接这台电脑（记住此选择）")
        self.external_allowed.setChecked(bool(self.store.setting("external_allowed", False)))
        self.lan_allowed = QCheckBox("允许局域网设备连接这台电脑（首次配对需本机确认）")
        self.lan_allowed.setChecked(bool(self.store.setting("lan_allowed", False)))
        layout.addWidget(self.external_allowed)
        layout.addWidget(self.lan_allowed)

        form = QFormLayout()
        form.setSpacing(10)
        self.monitor = self._combo()
        self.mode = self._combo()
        self.mode.addItem("允许鼠标键盘控制", False)
        self.mode.addItem("仅查看画面", True)
        self.mode.setCurrentIndex(1 if self.store.setting("view_only", False) else 0)
        form.addRow("默认共享屏幕", self.monitor)
        form.addRow("本机访问权限", self.mode)
        layout.addLayout(form)

        self.file_enabled = QCheckBox("允许文件互传")
        self.file_enabled.setChecked(bool(self.store.setting("files_enabled", True)))
        layout.addWidget(self.file_enabled)

        self.file_browser_enabled = QCheckBox("允许已授权设备直接浏览本机文件夹并互传（无人值守）")
        self.file_browser_enabled.setChecked(bool(self.store.setting("file_browser_enabled", True)))
        layout.addWidget(self.file_browser_enabled)

        self.clipboard_enabled = QCheckBox("允许双向文本剪贴板同步（仅控制模式生效）")
        self.clipboard_enabled.setChecked(bool(self.store.setting("clipboard_enabled", True)))
        layout.addWidget(self.clipboard_enabled)

        self.audio_enabled = QCheckBox("允许共享本机系统音频（Opus）")
        self.audio_enabled.setChecked(bool(self.store.setting("audio_enabled", True)))
        layout.addWidget(self.audio_enabled)

        self.host_pill_enabled = QCheckBox("本机被远程连接时显示桌面悬浮控制条（可一键暂停输入或断开）")
        self.host_pill_enabled.setChecked(bool(self.store.setting("host_pill_enabled", True)))
        layout.addWidget(self.host_pill_enabled)

        if platform.system() == "Darwin":
            self.mac_permission_status = _muted("正在检查 Mac 共享权限…")
            layout.addWidget(self.mac_permission_status)
            perm_row = QHBoxLayout()
            self.mac_screen_btn = QPushButton("屏幕录制权限")
            self.mac_input_btn = QPushButton("辅助功能权限")
            perm_row.addWidget(self.mac_screen_btn)
            perm_row.addWidget(self.mac_input_btn)
            layout.addLayout(perm_row)

        col.addWidget(card)

        pair_card, pair_layout = _card(
            "已配对局域网设备",
            "可逐台撤销指定设备的局域网凭据，或一键撤销全部配对并断开当前局域网连接。",
        )
        self.paired_empty = _muted("当前没有已配对的局域网控制端。")
        pair_layout.addWidget(self.paired_empty)
        self.paired_rows = QVBoxLayout()
        self.paired_rows.setSpacing(6)
        pair_layout.addLayout(self.paired_rows)
        self.clear_pairs_btn = QPushButton("撤销本机所有局域网配对")
        self.clear_pairs_btn.setObjectName("danger")
        pair_layout.addWidget(self.clear_pairs_btn)
        col.addWidget(pair_card)
        col.addStretch()
        return self._wrap_scroll(page)

    def set_paired_devices(self, items, on_revoke):
        from datetime import datetime

        while self.paired_rows.count():
            child = self.paired_rows.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self.paired_empty.setVisible(not items)
        self.clear_pairs_btn.setEnabled(bool(items))
        for item in items:
            row_frame = QFrame()
            row_frame.setObjectName("miniCard")
            row = QHBoxLayout(row_frame)
            row.setContentsMargins(12, 8, 10, 8)
            ts = item.get("granted_at")
            when = ""
            if ts:
                try:
                    when = " · " + datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
                except (OSError, ValueError, TypeError):
                    when = ""
            short_id = str(item["client_id"])[:8]
            label = QLabel(f"{item['name']}  ({short_id}{when})")
            label.setWordWrap(True)
            btn = QPushButton("撤销")
            btn.setObjectName("danger")
            btn.clicked.connect(lambda checked=False, cid=item["client_id"]: on_revoke(cid))
            row.addWidget(label, 1)
            row.addWidget(btn)
            self.paired_rows.addWidget(row_frame)

    def _network_page(self):
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(14)

        ts_card, ts_layout = _card("内置 Tailscale 外网", "无需额外安装客户端，同账号设备自动出现在统一设备工作台。")
        ts_row = QHBoxLayout()
        self.account_status = _muted("正在检查账号…")
        self.login_button = QPushButton("登录 Tailscale")
        self.login_button.setObjectName("primary")
        ts_row.addWidget(self.account_status, 1)
        ts_row.addWidget(self.login_button)
        ts_layout.addLayout(ts_row)
        self.external_note = _muted("未开启本机外网共享")
        ts_layout.addWidget(self.external_note)
        col.addWidget(ts_card)

        lan_card, lan_layout = _card("局域网与浏览器访问", "在局域网内免装客户端，通过 HTTPS 浏览器远程控制本机。")
        form = QFormLayout()
        form.setSpacing(10)
        self.address = self._combo()
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(self.store.setting("port", 47995)))
        self.web_port = QSpinBox()
        self.web_port.setRange(1024, 65535)
        self.web_port.setValue(int(self.store.setting("web_port", 47996)))
        form.addRow("局域网共享网卡", self.address)
        form.addRow("共享端口", self.port)
        form.addRow("浏览器端口", self.web_port)
        lan_layout.addLayout(form)

        self.lan_beacon_enabled = QCheckBox("允许局域网自动发现广播（仅广播名称与端口，不含凭据）")
        self.lan_beacon_enabled.setChecked(bool(self.store.setting("lan_beacon_enabled", True)))
        lan_layout.addWidget(self.lan_beacon_enabled)

        self.web_enabled = QCheckBox("允许局域网浏览器访问")
        self.web_enabled.setChecked(bool(self.store.setting("web_enabled", False)))
        lan_layout.addWidget(self.web_enabled)
        self.copy_browser_btn = QPushButton("复制浏览器链接")
        lan_layout.addWidget(self.copy_browser_btn)
        col.addWidget(lan_card)
        col.addStretch()
        return self._wrap_scroll(page)

    def _advanced_page(self):
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(14)

        card, layout = _card("旧版连接码（兼容入口）", "用于手动粘贴 `brioche://` 凭据建立临时连接。")
        row = QHBoxLayout()
        self.legacy_code = QLineEdit()
        self.legacy_code.setEchoMode(QLineEdit.EchoMode.Password)
        self.legacy_code.setPlaceholderText("粘贴 brioche://... 连接码")
        self.legacy_btn = QPushButton("使用连接码")
        row.addWidget(self.legacy_code, 1)
        row.addWidget(self.legacy_btn)
        layout.addLayout(row)
        col.addWidget(card)
        col.addStretch()
        return self._wrap_scroll(page)
