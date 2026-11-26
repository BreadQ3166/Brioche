"""Modern unified device workspace: LAN + Tailscale aggregation, Light/Dark themes, and per-device inspector drawer."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import platform
import threading
from PySide6.QtCore import QObject, QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)
from . import __version__
from .capture import list_monitors
from .clipboard_sync import ClipboardGuard
from .device_drawer import DeviceInspectorDrawer
from .device_model import (
    build_unified_devices,
    effective_stream_config,
    filter_devices,
)
from .device_store import DeviceStore
from .embedded_network import EmbeddedNetwork, login_url
from .lan import discover, pair, target
from .lan_discovery import LanBeaconBroadcaster, LanBeaconListener
from .network import lan_addresses
from .protocol import StreamConfig
from .remote_overlays import HostSessionPill
from .security import access_uri, parse_uri
from .session import Host, SessionGate
from .settings_view import SettingsView
from .theme import build_stylesheet, resolve_mode, tokens_for
from .transfer_ui import TransferBoard, downloads
from .ui_components import (
    AddLanDeviceDialog,
    DeviceList,
    FilterPillBar,
    PageTransition,
)


def text(value, heading=False):
    label = QLabel(value)
    label.setWordWrap(True)
    label.setObjectName("heading" if heading else "muted")
    return label


def screen_label(monitor):
    return f"显示器 {monitor['id']} · {monitor['width']} × {monitor['height']}"


def _format_recent_time(ts):
    if not ts:
        return "最近连接"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except (OSError, ValueError, TypeError):
        return "最近连接"


class EnvironmentBridge(QObject):
    ready = Signal(dict)
    result = Signal(dict)
    pair_request = Signal(object)
    pair_code = Signal(str)
    host_status = Signal(str, str)
    host_clipboard = Signal(str)
    host_session_state = Signal(dict)
    nearby_lan = Signal(list)
    core_engine_text = Signal(str)
    transfer_request = Signal(object)
    transfer_progress = Signal(dict)


class MainWindow(QMainWindow):
    PAGE_DEVICES = 0
    PAGE_RECENTS = 1
    PAGE_TRANSFERS = 2
    PAGE_SETTINGS = 3

    def __init__(self, folder=None, network=None):
        super().__init__()
        self._quitting = False
        self._shutdown = False
        self.setWindowTitle("Brioche")
        self.resize(1180, 760)
        self.setMinimumSize(900, 620)
        self.store = DeviceStore(folder)
        self.network = network or EmbeddedNetwork(self.store.folder / "network")
        self.gate = SessionGate()
        self.file_gate = SessionGate()
        self._transfer_dialogs = {}
        self.hosts = {"lan": None, "tailscale": None}
        self.session_window = None
        self.detached_windows = []
        self._v2_viewer_procs = []
        self._file_windows = []
        self._alive = True
        self._busy = False
        self._lan_busy = False
        self._connecting = False
        self._published_host = None
        self._auth_url = ""
        self._pair_cancel = threading.Event()
        self._pair_dialog = None
        self._incoming_pair_dialog = None
        self._misses = {}
        self._tailscale_records = []
        self._lan_records = []
        self._unified_devices = []
        self._active_group = None
        self._group_buttons = []
        self._local_ips_text = "未连接局域网"
        self._lan_host_message = ""
        self._nearby_lan_devices = []
        self._dhcp_probing = set()
        self.environment = {
            "lan": [],
            "monitors": [],
            "tailscale": {
                "ready": False,
                "ips": [],
                "peers": [],
                "message": "正在检查账号状态…",
            },
        }
        self.lan_state = {}

        self.bridge = EnvironmentBridge(self)
        self.bridge.ready.connect(self.apply_environment)
        self.bridge.result.connect(self.job_result)
        self.bridge.pair_request.connect(self.confirm_pair)
        self.bridge.pair_code.connect(self.show_pair_code)
        self.bridge.host_status.connect(self.host_status)
        self.bridge.host_clipboard.connect(self._apply_host_clipboard)
        self.bridge.host_session_state.connect(self._on_host_session_state)
        self.bridge.nearby_lan.connect(self._on_nearby_lan)
        self.bridge.core_engine_text.connect(self._on_core_engine_text)

        self.host_pill = HostSessionPill()
        self.host_pill.pause_input_toggled.connect(self._set_hosts_input_paused)
        self.host_pill.disconnect_requested.connect(self._disconnect_active_hosts)
        self.bridge.transfer_request.connect(self.confirm_transfer)
        self.bridge.transfer_progress.connect(self.transfer_progress)

        self.host_clipboard_guard = ClipboardGuard()
        self._qt_clipboard = QApplication.clipboard()
        if self._qt_clipboard is not None:
            self._qt_clipboard.dataChanged.connect(self._on_host_clipboard_changed)

        # Build settings view first so all shared setting widgets exist
        self.settings_view = SettingsView(self.store, self)
        self._bind_settings_widgets()

        # Root layout: Sidebar + Right Column (TopBar + Pages)
        root = QWidget()
        shell = QHBoxLayout(root)
        shell.setContentsMargins(14, 14, 14, 14)
        shell.setSpacing(14)

        sidebar = self._build_sidebar()
        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(12)

        topbar = self._build_topbar()
        right_col.addWidget(topbar)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_dashboard_page())
        self.pages.addWidget(self._build_recents_page())
        self.pages.addWidget(self._build_transfers_page())
        self.pages.addWidget(self.settings_view)
        self.transitions = PageTransition(self.pages)
        right_col.addWidget(self.pages, 1)

        shell.addWidget(sidebar)
        shell.addLayout(right_col, 1)
        self.setCentralWidget(root)

        self._rebuild_group_sidebar()
        self.navigate(self.PAGE_DEVICES)
        self.apply_theme()
        self.apply_motion()
        self._update_share_pill()

        self.poll = QTimer(self)
        self.poll.setInterval(300)
        self.poll.timeout.connect(self.poll_hosts)
        self.poll.start()
        self.network_poll = QTimer(self)
        self.network_poll.setInterval(6000)
        self.network_poll.timeout.connect(self.refresh_environment)
        self.network_poll.start()
        self.lan_poll = QTimer(self)
        self.lan_poll.setInterval(6000)
        self.lan_poll.timeout.connect(self.refresh_lan)
        self.lan_poll.start()

        self.refresh_environment()
        self._probe_core_engine_async()
        self.refresh_lan()
        self.refresh_paired_devices()
        self.lan_listener = LanBeaconListener(
            local_pin_getter=self._local_host_pin,
            on_change=lambda items: self.bridge.nearby_lan.emit(items) if self._alive else None,
        )
        self.lan_broadcaster = LanBeaconBroadcaster(self._beacon_host_info)
        self.lan_listener.start()
        self._sync_lan_broadcaster()
        from .display_mode import recover

        self.work("display_recovery", lambda: recover(self.store.folder))

        self.tray = QSystemTrayIcon(QApplication.instance().windowIcon(), self)
        self.tray.setToolTip("Brioche · 后台共享")
        tray_menu = QMenu(self)
        tray_menu.addAction("打开 Brioche", self.restore_window)
        tray_menu.addSeparator()
        tray_menu.addAction("退出 Brioche", self.quit_application)
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()
        QApplication.instance().aboutToQuit.connect(self.shutdown)

    # ------------------------------------------------------------------
    # Layout builders
    # ------------------------------------------------------------------
    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(214)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(14, 20, 14, 14)
        nav.setSpacing(6)

        brand = QLabel("Brioche")
        brand.setObjectName("brand")
        nav.addWidget(brand)
        tagline = text("选择电脑，即刻连接。")
        nav.addWidget(tagline)
        nav.addSpacing(14)

        self.nav_buttons = []
        nav_items = (
            (self.PAGE_DEVICES, "我的设备"),
            (self.PAGE_RECENTS, "最近连接"),
            (self.PAGE_TRANSFERS, "文件传输"),
            (self.PAGE_SETTINGS, "设置"),
        )
        for index, title in nav_items:
            button = QPushButton(title)
            button.setCheckable(True)
            button.setObjectName("navButton")
            button.clicked.connect(lambda checked=False, i=index: self._nav_clicked(i))
            self.nav_buttons.append(button)
            nav.addWidget(button)

        nav.addSpacing(10)
        self.group_header = QLabel("分组")
        self.group_header.setObjectName("sectionLabel")
        nav.addWidget(self.group_header)
        self.groups_layout = QVBoxLayout()
        self.groups_layout.setContentsMargins(0, 0, 0, 0)
        self.groups_layout.setSpacing(3)
        nav.addLayout(self.groups_layout)

        nav.addStretch()

        # Mini Tailscale account status card
        account_card = QFrame()
        account_card.setObjectName("miniCard")
        ac_layout = QVBoxLayout(account_card)
        ac_layout.setContentsMargins(12, 10, 12, 10)
        ac_layout.setSpacing(6)
        self.sidebar_account_label = QLabel("正在检查外网账号…")
        self.sidebar_account_label.setObjectName("muted")
        self.sidebar_account_label.setWordWrap(True)
        self.sidebar_login_btn = QPushButton("登录 Tailscale")
        self.sidebar_login_btn.setObjectName("primary")
        self.sidebar_login_btn.clicked.connect(self.account_action)
        ac_layout.addWidget(self.sidebar_account_label)
        ac_layout.addWidget(self.sidebar_login_btn)
        nav.addWidget(account_card)

        # Quick theme toggle + version
        footer = QHBoxLayout()
        footer.setContentsMargins(2, 4, 2, 0)
        self.theme_toggle_btn = QPushButton("主题")
        self.theme_toggle_btn.setObjectName("ghost")
        self.theme_toggle_btn.clicked.connect(self.cycle_theme)
        ver = QLabel(f"v{__version__}")
        ver.setObjectName("muted")
        footer.addWidget(self.theme_toggle_btn)
        footer.addStretch()
        footer.addWidget(ver)
        nav.addLayout(footer)
        return sidebar

    def _build_topbar(self):
        bar = QFrame()
        bar.setObjectName("topbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setObjectName("searchInput")
        self.search_input.setPlaceholderText("搜索设备名称、备注、IP 或分组…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._on_search_changed)
        row.addWidget(self.search_input, 1)

        self.direct_lan_btn = QPushButton("⚡ 指定局域网连接")
        self.direct_lan_btn.setToolTip("输入局域网 IP 或点选附近设备，仅走局域网直连（不经过外网/中继）")
        self.direct_lan_btn.clicked.connect(lambda: self.open_direct_lan_dialog())
        row.addWidget(self.direct_lan_btn)

        self.add_lan_btn = QPushButton("+ 添加局域网设备")
        self.add_lan_btn.clicked.connect(self.open_add_lan_dialog)
        row.addWidget(self.add_lan_btn)

        self.share_pill = QPushButton("○ 本机未共享")
        self.share_pill.setObjectName("sharePillOffline")
        self.share_pill.clicked.connect(self._open_share_menu)
        row.addWidget(self.share_pill)
        return bar

    def _build_dashboard_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(4, 2, 0, 0)
        outer.setSpacing(10)

        # Filter pills + subtle live note
        header = QHBoxLayout()
        header.setSpacing(10)
        self.filter_bar = FilterPillBar()
        self.filter_bar.changed.connect(self._on_filter_changed)
        header.addWidget(self.filter_bar, 1)
        self.dashboard_note = QLabel("单击设备查看配置，双击立即连接。")
        self.dashboard_note.setObjectName("muted")
        header.addWidget(self.dashboard_note)
        outer.addLayout(header)

        # Main workspace split: Unified Device Grid + Collapsible Inspector Drawer
        body = QHBoxLayout()
        body.setSpacing(14)

        left_col = QVBoxLayout()
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(8)

        self.device_list = DeviceList()
        self.device_list.itemClicked.connect(self._device_selected)
        self.device_list.itemDoubleClicked.connect(self._device_activated)
        self.device_list.itemActivated.connect(self._device_activated)
        self.device_list.context_requested.connect(self._device_context_menu)
        left_col.addWidget(self.device_list, 1)

        self.empty_label = text("暂无设备。登录 Tailscale 显示同账号设备，或点击右上角“+ 添加局域网设备”。")
        left_col.addWidget(self.empty_label)

        self.drawer = DeviceInspectorDrawer(self)
        self.drawer.connect_requested.connect(self.connect_unified_device)
        self.drawer.lan_connect_requested.connect(self.connect_device_via_lan)
        self.drawer.bind_lan_requested.connect(self.open_direct_lan_dialog)
        self.drawer.files_requested.connect(self.open_device_file_manager)
        self.drawer.profile_changed.connect(self._on_device_profile_changed)
        self.drawer.remove_lan_requested.connect(self._remove_lan_by_endpoint)

        body.addLayout(left_col, 1)
        body.addWidget(self.drawer)
        outer.addLayout(body, 1)
        return page

    def _build_recents_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 6, 4, 4)
        layout.setSpacing(10)
        layout.addWidget(text("最近连接", True))
        layout.addWidget(text("按最近使用时间排列，双击在线设备可快速重连。"))
        self.recent_list = DeviceList()
        self.recent_list.itemDoubleClicked.connect(self._device_activated)
        self.recent_list.itemActivated.connect(self._device_activated)
        self.recent_list.itemClicked.connect(self._recent_selected)
        layout.addWidget(self.recent_list, 1)
        self.recent_empty = text("暂无连接记录，连接过任意设备后会显示在这里。")
        layout.addWidget(self.recent_empty)
        return page

    def _build_transfers_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 6, 4, 4)
        layout.setSpacing(10)
        layout.addWidget(text("文件传输", True))
        layout.addWidget(text("连接设备后，在远程窗口点击“文件互传”。接收前确认保存位置。"))
        self.transfer_board = TransferBoard()
        layout.addWidget(self.transfer_board, 1)
        return page

    def _bind_settings_widgets(self):
        sv = self.settings_view
        self.theme_combo = sv.theme_combo
        self.device_name = sv.device_name
        self.reduced_motion = sv.reduced_motion
        self.profile = sv.profile
        self.resolution = sv.resolution
        self.fps = sv.fps
        self.bitrate = sv.bitrate
        self.encoder = sv.encoder
        self.match_display = sv.match_display
        self.stream_engine = sv.stream_engine
        self.chroma_444 = sv.chroma_444
        self.core_engine_status = sv.core_engine_status
        self.external_allowed = sv.external_allowed
        self.lan_allowed = sv.lan_allowed
        self.monitor = sv.monitor
        self.mode = sv.mode
        self.file_enabled = sv.file_enabled
        self.file_browser_enabled = sv.file_browser_enabled
        self.clipboard_enabled = sv.clipboard_enabled
        self.audio_enabled = sv.audio_enabled
        self.host_pill_enabled = sv.host_pill_enabled
        self.clipboard_mode = sv.clipboard_mode
        self.auto_reconnect = sv.auto_reconnect
        self.sync_audio = sv.sync_audio
        self.account_status = sv.account_status
        self.login_button = sv.login_button
        self.external_note = sv.external_note
        self.address = sv.address
        self.port = sv.port
        self.web_port = sv.web_port
        self.web_enabled = sv.web_enabled
        self.lan_beacon_enabled = sv.lan_beacon_enabled
        self.legacy_code = sv.legacy_code
        self.settings_note = sv.settings_note
        if hasattr(sv, "mac_permission_status"):
            self.mac_permission_status = sv.mac_permission_status
            sv.mac_screen_btn.clicked.connect(lambda: self.mac_permission("screen"))
            sv.mac_input_btn.clicked.connect(lambda: self.mac_permission("input"))

        # Sync quality preset index
        preset = next(
            (
                i
                for i in range(3)
                if self.resolution.currentData() == "1440p"
                and self.profile.itemData(i)
                == (self.fps.currentData(), self.bitrate.value())
            ),
            3,
        )
        self.profile.setCurrentIndex(preset)

        # Wire signals
        self.theme_combo.currentIndexChanged.connect(self._on_theme_combo_changed)
        self.device_name.editingFinished.connect(
            lambda: self.store.set_name(self.device_name.text())
        )
        self.reduced_motion.toggled.connect(self.apply_motion)
        sv.exit_button.clicked.connect(self.quit_application)

        self.profile.currentIndexChanged.connect(self.apply_profile)
        for control in (self.resolution, self.fps):
            control.currentIndexChanged.connect(self.custom_profile)
        self.bitrate.valueChanged.connect(self.custom_profile)
        self.match_display.toggled.connect(self.save_settings)
        self.stream_engine.currentIndexChanged.connect(self.save_settings)
        self.chroma_444.toggled.connect(self.save_settings)
        for control in (self.mode, self.encoder, self.address, self.monitor):
            control.currentIndexChanged.connect(self.save_settings)
        for control in (self.port, self.web_port):
            control.valueChanged.connect(self.save_settings)
        self.web_enabled.toggled.connect(self.save_settings)
        self.lan_beacon_enabled.toggled.connect(self._on_beacon_toggled)
        self.file_enabled.toggled.connect(self.save_settings)
        self.file_browser_enabled.toggled.connect(self.save_settings)
        self.clipboard_enabled.toggled.connect(self.save_settings)
        self.audio_enabled.toggled.connect(self.save_settings)
        self.host_pill_enabled.toggled.connect(self.save_settings)
        self.clipboard_mode.currentIndexChanged.connect(self.save_settings)
        self.auto_reconnect.toggled.connect(self.save_settings)
        self.sync_audio.toggled.connect(self.save_settings)
        self.external_allowed.toggled.connect(self.share_preference)
        self.lan_allowed.toggled.connect(self.share_preference)
        self.login_button.clicked.connect(self.account_action)
        sv.copy_browser_btn.clicked.connect(self.copy_browser)
        sv.clear_pairs_btn.clicked.connect(self.clear_pairs)
        sv.legacy_btn.clicked.connect(self.connect_legacy)

    # ------------------------------------------------------------------
    # Navigation, Theme & Motion
    # ------------------------------------------------------------------
    def _nav_clicked(self, index):
        if index == self.PAGE_DEVICES:
            self._active_group = None
            self._sync_group_buttons()
            self._render_dashboard()
        self.navigate(index)

    def navigate(self, index):
        self.transitions.show(index, not self.store.setting("reduced_motion", False))
        for i, button in enumerate(self.nav_buttons):
            button.setChecked(i == index and (index != self.PAGE_DEVICES or self._active_group is None))
        if index == self.PAGE_RECENTS:
            self._render_recents()

    def apply_theme(self, preference=None):
        pref = preference if preference is not None else self.store.setting("theme", "system")
        if preference is not None:
            self.store.configure(theme=pref)
        resolved = resolve_mode(pref)
        tokens = tokens_for(pref)
        qss = build_stylesheet(pref)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)
        else:
            self.setStyleSheet(qss)
        self.device_list.apply_theme(tokens)
        self.recent_list.apply_theme(tokens)
        self.drawer.apply_theme(tokens)
        self.settings_view.apply_theme(tokens)
        label_map = {"light": "☀ 浅色", "dark": "🌙 深色", "system": f"◐ 自动({'深' if resolved == 'dark' else '浅'})"}
        self.theme_toggle_btn.setText(label_map.get(pref, "◐ 自动"))
        idx = self.theme_combo.findData(pref)
        if idx >= 0 and self.theme_combo.currentIndex() != idx:
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentIndex(idx)
            self.theme_combo.blockSignals(False)

    def _on_theme_combo_changed(self):
        self.apply_theme(self.theme_combo.currentData())

    def cycle_theme(self):
        order = ("light", "dark", "system")
        current = self.store.setting("theme", "system")
        nxt = order[(order.index(current) + 1) % len(order)] if current in order else "dark"
        self.apply_theme(nxt)

    def apply_motion(self):
        enabled = not self.reduced_motion.isChecked()
        self.store.configure(reduced_motion=not enabled)
        for widget in (self.device_list, self.recent_list):
            widget.set_animations(enabled)
        if not enabled and hasattr(self, "transitions"):
            self.transitions.show(self.pages.currentIndex(), False)

    # ------------------------------------------------------------------
    # Unified Device Aggregation, Filtering & Inspector
    # ------------------------------------------------------------------
    def _rebuild_unified(self):
        self._unified_devices = build_unified_devices(
            self._tailscale_records, self._lan_records, self.store
        )
        self._rebuild_group_sidebar()
        self._render_dashboard()
        if self.pages.currentIndex() == self.PAGE_RECENTS:
            self._render_recents()

    def _rebuild_group_sidebar(self):
        while self.groups_layout.count():
            item = self.groups_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._group_buttons = []
        groups = self.store.all_groups()
        self.group_header.setVisible(bool(groups))
        for name, count in groups:
            btn = QPushButton(f"• {name} ({count})")
            btn.setCheckable(True)
            btn.setObjectName("groupButton")
            btn.setProperty("group_name", name)
            btn.clicked.connect(lambda checked=False, g=name: self._select_group(g))
            self.groups_layout.addWidget(btn)
            self._group_buttons.append(btn)
        if self._active_group and not any(g == self._active_group for g, _ in groups):
            self._active_group = None
        self._sync_group_buttons()

    def _select_group(self, group_name):
        self._active_group = None if self._active_group == group_name else group_name
        self._sync_group_buttons()
        self.navigate(self.PAGE_DEVICES)
        self._render_dashboard()

    def _sync_group_buttons(self):
        for btn in self._group_buttons:
            btn.setChecked(btn.property("group_name") == self._active_group)
        if self.pages.currentIndex() == self.PAGE_DEVICES:
            self.nav_buttons[0].setChecked(self._active_group is None)

    def _on_search_changed(self, _text):
        if self.pages.currentIndex() != self.PAGE_DEVICES:
            self.navigate(self.PAGE_DEVICES)
        self._render_dashboard()

    def _on_filter_changed(self, _category):
        self._render_dashboard()

    def _render_dashboard(self):
        all_devs = self._unified_devices
        self.filter_bar.update_counts(
            {
                "all": len(all_devs),
                "online": sum(1 for d in all_devs if d["online"]),
                "favorite": sum(1 for d in all_devs if d["favorite"]),
                "lan": sum(1 for d in all_devs if "lan" in d["channels"]),
                "tailscale": sum(1 for d in all_devs if "tailscale" in d["channels"]),
            }
        )
        visible = filter_devices(
            all_devs,
            query=self.search_input.text(),
            category=self.filter_bar.current,
            group=self._active_group,
        )
        self._render_device_list(self.device_list, visible)

        if visible:
            self.empty_label.hide()
        else:
            self.empty_label.show()
            if all_devs:
                self.empty_label.setText("没有符合当前筛选条件的设备，尝试清空搜索或切换分类。")
            elif self.environment["tailscale"]["ready"]:
                self.empty_label.setText("暂无同账号设备。请在另一台电脑登录同一 Tailscale 账号，或点击“+ 添加局域网设备”。")
            else:
                self.empty_label.setText("登录后自动显示同账号外网设备；也可点击右上角“+ 添加局域网设备”免登录连接。")

        # Keep open inspector drawer fresh
        if self.drawer.isVisible() and self.drawer.current_key:
            updated = next((d for d in all_devs if d["key"] == self.drawer.current_key), None)
            if updated:
                self.drawer.show_device(updated)
            else:
                self.drawer._close()

    def _render_recents(self):
        by_key = {d["key"]: d for d in self._unified_devices}
        recents = []
        for entry in self.store.recent_connections():
            live = by_key.get(entry["key"])
            if live:
                recents.append(
                    {
                        **live,
                        "status_text": f"{live['status_text']}  ·  {_format_recent_time(entry.get('connected_at'))}",
                    }
                )
            else:
                recents.append(
                    {
                        "key": entry["key"],
                        "name": entry.get("name", "未知设备"),
                        "display_name": entry.get("name", "未知设备"),
                        "platform": "",
                        "channels": {},
                        "active_mode": entry.get("mode"),
                        "active_channel": None,
                        "online": False,
                        "connectable": False,
                        "busy": False,
                        "status_text": f"上次连接：{_format_recent_time(entry.get('connected_at'))}",
                        "badge_text": "历史",
                        "badge_tone": "offline",
                        "ip": entry.get("address", ""),
                        "favorite": False,
                        "group": "",
                        "profile": {},
                    }
                )
        self._render_device_list(self.recent_list, recents)
        self.recent_empty.setVisible(not recents)

    @staticmethod
    def _render_device_list(widget, devices):
        selected = (
            widget.currentItem().data(Qt.ItemDataRole.UserRole).get("key")
            if widget.currentItem()
            else None
        )
        existing = {
            widget.item(i).data(Qt.ItemDataRole.UserRole)["key"]: widget.item(i)
            for i in range(widget.count())
        }
        keep = set()
        for idx, device in enumerate(devices):
            key = device["key"]
            keep.add(key)
            item = existing.get(key)
            if item is None:
                item = QListWidgetItem()
                size = (
                    widget.gridSize()
                    if isinstance(widget, DeviceList) and widget.gridSize().width() > 0
                    else QSize(256, 214)
                )
                item.setSizeHint(size)
                widget.insertItem(idx, item)
                if isinstance(widget, DeviceList):
                    widget.new_entry(key)
            else:
                current_row = widget.row(item)
                if current_row != idx:
                    taken = widget.takeItem(current_row)
                    widget.insertItem(idx, taken)
                    item = taken
            item.setText(f"{device['display_name']}\n{device['status_text']}    ·    {device.get('ip', '')}")
            item.setData(Qt.ItemDataRole.UserRole, device)
            item.setToolTip(f"{device['display_name']} — {device['status_text']}")
            if selected == key:
                widget.setCurrentItem(item)
        for key, item in existing.items():
            if key not in keep:
                widget.takeItem(widget.row(item))

    def _device_selected(self, item):
        device = item.data(Qt.ItemDataRole.UserRole)
        if device:
            self.drawer.show_device(device)

    def _recent_selected(self, item):
        device = item.data(Qt.ItemDataRole.UserRole)
        if device and device.get("channels"):
            self.navigate(self.PAGE_DEVICES)
            self.drawer.show_device(device)

    def _device_activated(self, item):
        device = item.data(Qt.ItemDataRole.UserRole)
        if device:
            self.connect_unified_device(device)

    def _device_context_menu(self, item, global_pos):
        device = item.data(Qt.ItemDataRole.UserRole)
        if not device:
            return
        menu = QMenu(self)
        conn = menu.addAction("立即连接")
        conn.setEnabled(bool(device.get("connectable")))
        conn.triggered.connect(lambda: self.connect_unified_device(device))

        channels = device.get("channels") or {}
        lan_ch = channels.get("lan")
        ts_ch = channels.get("tailscale")

        if lan_ch:
            lan_act = menu.addAction(
                f"⚡ 仅通过局域网直连 ({lan_ch['ip']})"
            )
            lan_act.setEnabled(bool(lan_ch.get("connectable")))
            lan_act.triggered.connect(lambda: self.connect_device_via_lan(device))
        bind_lan_act = menu.addAction(
            "修改并指定局域网 IP 连接…" if lan_ch else "⚡ 指定局域网 IP 直连…"
        )
        bind_lan_act.triggered.connect(lambda: self.open_direct_lan_dialog(device))

        if ts_ch and lan_ch:
            ts_act = menu.addAction(f"仅通过外网 Tailscale 连接 ({ts_ch['ip']})")
            ts_act.setEnabled(bool(ts_ch.get("connectable")))
            ts_act.triggered.connect(lambda: self.connect_device_via_tailscale(device))

        lan_only_act = menu.addAction("锁定为仅局域网直连（禁用外网回退）")
        lan_only_act.setCheckable(True)
        is_lan_only = device.get("preferred_channel") == "lan_only"
        lan_only_act.setChecked(is_lan_only)
        lan_only_act.triggered.connect(
            lambda checked=False: self._set_device_lan_only(device, checked)
        )

        menu.addSeparator()
        files_act = menu.addAction("打开双栏文件管理器")
        files_act.setEnabled(bool(device.get("connectable")))
        files_act.triggered.connect(lambda: self.open_device_file_manager(device))

        fav_label = "取消收藏" if device.get("favorite") else "收藏置顶"
        fav = menu.addAction(fav_label)
        fav.triggered.connect(lambda: self._toggle_device_favorite(device))

        inspect = menu.addAction("设备详情与独立配置")
        inspect.triggered.connect(lambda: self.drawer.show_device(device))

        if lan_ch:
            menu.addSeparator()
            rem = menu.addAction("从局域网目录移除")
            rem.triggered.connect(
                lambda: self._remove_lan_by_endpoint(lan_ch["ip"], int(lan_ch.get("port", 47995)))
            )
        menu.exec(global_pos)

    def _set_device_lan_only(self, device, enabled):
        self.store.set_device_profile(
            device["key"],
            preferred_channel="lan_only" if enabled else None,
        )
        self._rebuild_unified()

    def _toggle_device_favorite(self, device):
        new_fav = not bool(device.get("favorite"))
        self.store.set_device_profile(device["key"], favorite=True if new_fav else None)
        self._rebuild_unified()

    def _on_device_profile_changed(self, device_key, payload):
        self.store.set_device_profile(device_key, **payload)
        self._rebuild_unified()

    def _remove_lan_by_endpoint(self, ip, port):
        self.store.remove(ip, port)
        self.lan_state.pop((ip, port), None)
        self.update_lan_list(list(self.lan_state.values()))

    # ------------------------------------------------------------------
    # Quick Share Popover & Add LAN Dialog
    # ------------------------------------------------------------------
    def _update_share_pill(self):
        lan_on = self.lan_allowed.isChecked()
        ext_on = self.external_allowed.isChecked()
        parts = []
        if lan_on:
            parts.append("局域网")
        if ext_on:
            parts.append("外网")
        if parts:
            self.share_pill.setText("● 本机共享中 · " + "+".join(parts))
            self.share_pill.setObjectName("sharePillOnline")
        else:
            self.share_pill.setText("○ 本机未共享")
            self.share_pill.setObjectName("sharePillOffline")
        self.share_pill.style().unpolish(self.share_pill)
        self.share_pill.style().polish(self.share_pill)

    def _open_share_menu(self):
        menu = QMenu(self)
        ext_act = menu.addAction("允许同账号外网连接本机")
        ext_act.setCheckable(True)
        ext_act.setChecked(self.external_allowed.isChecked())
        ext_act.toggled.connect(self.external_allowed.setChecked)

        lan_act = menu.addAction("允许局域网设备连接本机")
        lan_act.setCheckable(True)
        lan_act.setChecked(self.lan_allowed.isChecked())
        lan_act.toggled.connect(self.lan_allowed.setChecked)

        menu.addSeparator()
        ip_act = menu.addAction(f"复制本机 IP：{self._local_ips_text}")
        ip_act.setEnabled(self._local_ips_text != "未连接局域网")
        ip_act.triggered.connect(
            lambda: QApplication.clipboard().setText(self._local_ips_text.split("、")[0])
        )

        browser_act = menu.addAction("复制局域网浏览器链接")
        browser_act.triggered.connect(self.copy_browser)

        menu.addSeparator()
        more_act = menu.addAction("打开共享与安全设置…")
        more_act.triggered.connect(self._open_sharing_settings)
        menu.exec(self.share_pill.mapToGlobal(self.share_pill.rect().bottomLeft()))

    def _open_sharing_settings(self):
        self.navigate(self.PAGE_SETTINGS)
        self.settings_view.select_category(2)

    def open_add_lan_dialog(self):
        nearby = self.lan_listener.snapshot() if hasattr(self, "lan_listener") else self._nearby_lan_devices
        dialog = AddLanDeviceDialog(
            self._local_ips_text,
            nearby_devices=nearby,
            parent=self,
            connect_primary=False,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            raw = dialog.value()
            if raw:
                self.find_lan(raw, connect_now=dialog.connect_immediately)

    def open_direct_lan_dialog(self, target_device=None):
        nearby = self.lan_listener.snapshot() if hasattr(self, "lan_listener") else self._nearby_lan_devices
        target_name = ""
        initial_addr = ""
        if isinstance(target_device, dict):
            target_name = target_device.get("display_name") or target_device.get("name") or ""
            lan_ch = (target_device.get("channels") or {}).get("lan")
            if lan_ch and lan_ch.get("ip"):
                port = int(lan_ch.get("port", 47995))
                initial_addr = f"{lan_ch['ip']}:{port}" if port != 47995 else lan_ch["ip"]
        dialog = AddLanDeviceDialog(
            self._local_ips_text,
            nearby_devices=nearby,
            parent=self,
            connect_primary=True,
            target_name=target_name,
            initial_address=initial_addr,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            raw = dialog.value()
            if raw:
                self.find_lan(raw, connect_now=dialog.connect_immediately)

    def _local_host_pin(self):
        for host in self.hosts.values():
            if host and host.pin:
                return host.pin
        return ""

    def _beacon_host_info(self):
        host = self.hosts.get("lan")
        if not host or not host.ready.is_set() or host.stop_requested.is_set() or not host.pin:
            return None
        return {
            "name": host.name,
            "port": host.port,
            "pin": host.pin,
            "bind": host.bind,
            "busy": host.session_gate.active,
        }

    def _sync_lan_broadcaster(self):
        if not hasattr(self, "lan_broadcaster"):
            return
        if self.lan_allowed.isChecked() and self.lan_beacon_enabled.isChecked():
            self.lan_broadcaster.start()
        else:
            self.lan_broadcaster.stop()

    def _on_beacon_toggled(self):
        self.save_settings()
        self._sync_lan_broadcaster()

    def _on_nearby_lan(self, items):
        self._nearby_lan_devices = items
        # Auto-heal saved LAN devices whose DHCP IP changed, verified by full TLS cert pin.
        saved = self.store.devices()
        for beacon in items:
            prefix = beacon.get("pin_prefix")
            if not prefix:
                continue
            for dev in saved:
                pin = dev.get("pin") or dev.get("observed_pin") or ""
                if not pin.startswith(prefix):
                    continue
                new_ep = (beacon["ip"], int(beacon["port"]))
                if (dev["ip"], int(dev.get("port", 47995))) == new_ep:
                    continue
                if new_ep in self._dhcp_probing:
                    continue
                self._dhcp_probing.add(new_ep)
                self.work(
                    "dhcp_migrate",
                    lambda ip=new_ep[0], port=new_ep[1], expected=pin: discover(ip, port, expected),
                )

    # ------------------------------------------------------------------
    # Settings & StreamConfig
    # ------------------------------------------------------------------
    def apply_profile(self):
        value = self.profile.currentData()
        if value:
            for control in (self.resolution, self.fps, self.bitrate):
                control.blockSignals(True)
            self.resolution.setCurrentIndex(0)
            self.fps.setCurrentIndex(self.fps.findData(value[0]))
            self.bitrate.setValue(value[1])
            for control in (self.resolution, self.fps, self.bitrate):
                control.blockSignals(False)
            self.match_display.setEnabled(True)
        self.save_settings()

    def custom_profile(self):
        self.profile.blockSignals(True)
        self.profile.setCurrentIndex(3)
        self.profile.blockSignals(False)
        self.match_display.setEnabled(self.resolution.currentData() != "native")
        self.save_settings()

    def stream_config(self, profile=None):
        base = StreamConfig(
            self.resolution.currentData(),
            self.fps.currentData(),
            self.bitrate.value(),
            self.match_display.isChecked()
            and self.resolution.currentData() != "native",
        )
        return effective_stream_config(base, profile) if profile else base

    def save_settings(self):
        if not hasattr(self, "address"):
            return
        self.store.configure(
            view_only=self.mode.currentData(),
            web_enabled=self.web_enabled.isChecked(),
            port=self.port.value(),
            web_port=self.web_port.value(),
            encoder=self.encoder.currentData(),
            address=self.address.currentData(),
            monitor=self.monitor.currentData(),
            resolution=self.resolution.currentData(),
            fps=self.fps.currentData(),
            bitrate=self.bitrate.value(),
            match_display=self.match_display.isChecked(),
            stream_engine=self.stream_engine.currentData(),
            chroma_444=self.chroma_444.isChecked(),
            files_enabled=self.file_enabled.isChecked(),
            file_browser_enabled=self.file_browser_enabled.isChecked(),
            clipboard_enabled=self.clipboard_enabled.isChecked(),
            audio_enabled=self.audio_enabled.isChecked(),
            host_pill_enabled=self.host_pill_enabled.isChecked(),
            clipboard_mode=self.clipboard_mode.currentData(),
            auto_reconnect=self.auto_reconnect.isChecked(),
            sync_audio=self.sync_audio.isChecked(),
            lan_beacon_enabled=self.lan_beacon_enabled.isChecked(),
        )
        for host in self.hosts.values():
            if host:
                host.files_enabled = self.file_enabled.isChecked() and not host.view_only
                host.file_browser_enabled = self.file_browser_enabled.isChecked() and not host.view_only
                host.clipboard_enabled = self.clipboard_enabled.isChecked() and not host.view_only
                host.audio_enabled = self.audio_enabled.isChecked()
                if not host.files_enabled:
                    host.cancel_transfers()
        if not self.host_pill_enabled.isChecked():
            self.host_pill.hide_session()

    def _on_host_session_state(self, state):
        if not self._alive:
            return
        if state.get("active") and self.host_pill_enabled.isChecked():
            self.host_pill.show_session(
                view_only=bool(state.get("view_only")),
                input_paused=bool(state.get("input_paused")),
            )
        else:
            self.host_pill.hide_session()

    def _set_hosts_input_paused(self, paused):
        for host in self.hosts.values():
            if host and host.active:
                host.set_input_paused(paused)

    def _disconnect_active_hosts(self):
        for host in self.hosts.values():
            if host and host.active:
                host.disconnect_viewer()
        self.host_pill.hide_session()

    def _apply_host_clipboard(self, text):
        if (
            not self._alive
            or not self.clipboard_enabled.isChecked()
            or self._qt_clipboard is None
        ):
            return
        if self.host_clipboard_guard.accept_remote(text):
            self._qt_clipboard.setText(text)

    def _on_host_clipboard_changed(self):
        if (
            not self._alive
            or not self.clipboard_enabled.isChecked()
            or self._qt_clipboard is None
        ):
            return
        active_hosts = [
            h for h in self.hosts.values() if h and h.active and h.clipboard_enabled and not h.view_only
        ]
        if not active_hosts:
            return
        text = self._qt_clipboard.text()
        if self.host_clipboard_guard.should_send_local(text):
            for host in active_hosts:
                host.push_clipboard(text)

    def mac_permission(self, kind):
        from .mac_permissions import input_allowed, screen_allowed

        allowed = (screen_allowed if kind == "screen" else input_allowed)(request=True)
        self.settings_note.setText(
            "权限已开启"
            if allowed
            else "请在系统偏好设置 → 安全性与隐私中授权；屏幕录制授权后需重新打开 Brioche。"
        )
        self.refresh_mac_permissions()

    def refresh_mac_permissions(self):
        if not hasattr(self, "mac_permission_status"):
            return
        from .mac_permissions import input_allowed, screen_allowed

        try:
            screen, inputs = screen_allowed(), input_allowed()
            self.mac_permission_status.setText(
                f'屏幕录制：{"已允许" if screen else "未允许，无法共享画面"}　'
                f'辅助功能：{"已允许" if inputs else "未允许，无法远程操作"}'
            )
        except Exception:
            self.mac_permission_status.setText("无法检查 Mac 权限，请在系统偏好设置中确认")

    def share_preference(self):
        self.store.configure(
            external_allowed=self.external_allowed.isChecked(),
            lan_allowed=self.lan_allowed.isChecked(),
        )
        self._update_share_pill()
        self.ensure_hosts()
        self._sync_lan_broadcaster()

    # ------------------------------------------------------------------
    # Background workers & Polling
    # ------------------------------------------------------------------
    def work(self, operation, function):
        def run():
            result = {"op": operation}
            try:
                result["value"] = function()
            except Exception as exc:
                result["error"] = str(exc)[:240]
            if self._alive:
                try:
                    self.bridge.result.emit(result)
                except RuntimeError:
                    pass

        threading.Thread(target=run, name="brioche-" + operation, daemon=True).start()

    def _on_core_engine_text(self, text):
        if hasattr(self, "core_engine_status"):
            self.core_engine_status.setText(text)

    def _probe_core_engine_async(self):
        from .core_engine import probe_core_capabilities

        def work():
            caps = probe_core_capabilities()
            if caps.get("available"):
                gpu = caps.get("gpu_renderer") or {}
                enc = caps.get("encoders") or {}
                parts = []
                if gpu.get("available"):
                    parts.append(f"GPU：{gpu.get('adapter')} ({gpu.get('backend')})")
                if enc.get("hevc_nvenc"):
                    parts.append("HEVC NVENC")
                elif enc.get("h264_nvenc"):
                    parts.append("H.264 NVENC")
                if enc.get("hevc_nvenc_444") or enc.get("h264_nvenc_444"):
                    parts.append("YUV 4:4:4 就绪")
                parts.append("UDP+RS-FEC · QUIC TLS1.3")
                text = f"v{caps.get('version', '2.0')} 已就绪 · " + " · ".join(parts)
            else:
                text = f"未检测到原生引擎（将自动使用 v1.x 兼容内核）：{caps.get('reason', '')}"
            if self._alive:
                try:
                    self.bridge.core_engine_text.emit(text)
                except RuntimeError:
                    pass

        threading.Thread(target=work, name="brioche-core-probe", daemon=True).start()

    def refresh_environment(self):
        if self._busy or not self._alive:
            return
        self._busy = True
        host = self.hosts["tailscale"]

        def check():
            status = self.network.status(probe=True)
            if (
                host
                and host.pin
                and not host.stop_requested.is_set()
                and status["ready"]
            ):
                self.network.publish(host)
            try:
                addresses = lan_addresses()
            except Exception:
                addresses = []
            try:
                monitors = list_monitors()
            except Exception:
                monitors = []
            return {"lan": addresses, "monitors": monitors, "tailscale": status}

        self.work("environment", check)

    def apply_environment(self, result):
        self.refresh_mac_permissions()
        self.environment = result
        status = result["tailscale"]
        self.account_status.setText(status["message"])
        self.sidebar_account_label.setText(status["message"])
        if login_url(status.get("auth_url", "")):
            self._auth_url = status["auth_url"]
        if status["ready"]:
            self._auth_url = ""
        btn_label = (
            "账号 / 退出"
            if status["ready"]
            else "继续登录" if self._auth_url else "登录 Tailscale"
        )
        self.login_button.setText(btn_label)
        self.sidebar_login_btn.setText(btn_label)
        self.sidebar_login_btn.setObjectName("ghost" if status["ready"] else "primary")
        self.sidebar_login_btn.style().unpolish(self.sidebar_login_btn)
        self.sidebar_login_btn.style().polish(self.sidebar_login_btn)

        for widget, entries in (
            (
                self.address,
                [(f"{a['ip']} · {a['name']}", a["ip"]) for a in result["lan"]],
            ),
            (self.monitor, [(screen_label(m), m["id"]) for m in result["monitors"]]),
        ):
            selected = widget.currentData()
            widget.blockSignals(True)
            widget.clear()
            for label, value in entries:
                widget.addItem(label, value)
            preferred = (
                selected
                if selected is not None
                else self.store.setting(
                    "address" if widget is self.address else "monitor"
                )
            )
            index = widget.findData(preferred)
            if index >= 0:
                widget.setCurrentIndex(index)
            widget.blockSignals(False)

        peers = status.get("peers", []) if status["ready"] else []
        ts_records = []
        for peer in peers:
            ip = peer["ip"]
            alive = bool(peer.get("reachable", False))
            self._misses[ip] = 0 if alive else self._misses.get(ip, 0) + 1
            if alive:
                label = (
                    "使用中"
                    if peer.get("busy")
                    else (
                        "在线 · 双击连接"
                        if peer.get("sharing")
                        else "在线 · 对方未允许共享"
                    )
                )
            else:
                label = (
                    "状态确认中…"
                    if self._misses[ip] < 2 and peer.get("reported_online")
                    else "离线 / 未响应"
                )
            ts_records.append(
                {
                    **peer,
                    "name": peer.get("name", ip),
                    "online": alive,
                    "connectable": alive
                    and bool(peer.get("sharing"))
                    and not peer.get("busy"),
                    "status_text": label,
                }
            )
        self._tailscale_records = ts_records
        self._local_ips_text = "、".join(a["ip"] for a in result["lan"]) or "未连接局域网"
        self._rebuild_unified()
        self.ensure_hosts()

    def refresh_lan(self):
        if self._lan_busy or not self._alive:
            return
        self._lan_busy = True
        devices = self.store.devices()

        def check():
            def probe(device):
                try:
                    return {
                        **device,
                        **discover(device["ip"], device["port"], device.get("pin")),
                    }
                except Exception as exc:
                    return {**device, "online": False, "error": str(exc)[:180]}

            with ThreadPoolExecutor(max_workers=4) as executor:
                return list(executor.map(probe, devices))

        self.work("lan", check)

    def update_lan_list(self, results):
        self.lan_state = {(d["ip"], d["port"]): d for d in results}
        lan_records = []
        for device in self.store.devices():
            key = (device["ip"], device["port"])
            state = self.lan_state.get(key, device)
            online = bool(state.get("online", False))
            status = (
                (
                    "使用中"
                    if state.get("busy")
                    else "在线 · 双击连接" if device.get("token") else "在线 · 双击配对"
                )
                if online
                else state.get("error", "待检测")
            )
            lan_records.append(
                {
                    **device,
                    **state,
                    "name": device.get("name", device["ip"]),
                    "online": online,
                    "connectable": online and not state.get("busy"),
                    "status_text": status,
                }
            )
        self._lan_records = lan_records
        self._rebuild_unified()

    def find_lan(self, raw_text, connect_now=False):
        try:
            ip, port = target(raw_text)
        except ValueError as exc:
            self.dashboard_note.setText(str(exc))
            return
        self.add_lan_btn.setEnabled(False)
        self.direct_lan_btn.setEnabled(False)
        self.dashboard_note.setText(
            f"正在通过局域网直连 {ip}:{port}…" if connect_now else f"正在查找 {ip}:{port}…"
        )
        existing = next(
            (d for d in self.store.devices() if (d["ip"], d["port"]) == (ip, port)), {}
        )
        op = "find_and_connect" if connect_now else "find"
        self.work(op, lambda: discover(ip, port, existing.get("pin")))

    # ------------------------------------------------------------------
    # Connection & Remote Session
    # ------------------------------------------------------------------
    def connect_unified_device(self, device):
        if not device.get("connectable"):
            self.dashboard_note.setText(
                device.get("status_text") or "设备当前离线、未允许共享或正在使用中。"
            )
            return
        mode = device.get("active_mode")
        channel = device.get("active_channel")
        if not mode or not channel:
            return
        self.connect_device(
            mode,
            channel,
            profile=device.get("profile"),
            device_key=device.get("key"),
            display_name=device.get("display_name", device.get("name", "")),
        )

    def connect_device_via_lan(self, device):
        lan_ch = (device.get("channels") or {}).get("lan")
        if not lan_ch:
            self.open_direct_lan_dialog(device)
            return
        if not lan_ch.get("connectable"):
            self.dashboard_note.setText(
                f"局域网通道 {lan_ch.get('ip', '')} 当前离线或使用中；请确认对方已开启局域网共享，或点击“指定局域网 IP”更新地址。"
            )
            return
        self.connect_device(
            "lan",
            lan_ch,
            profile=device.get("profile"),
            device_key=device.get("key"),
            display_name=device.get("display_name", device.get("name", "")),
        )

    def connect_device_via_tailscale(self, device):
        ts_ch = (device.get("channels") or {}).get("tailscale")
        if not ts_ch or not ts_ch.get("connectable"):
            self.dashboard_note.setText("该设备外网 Tailscale 通道当前不可连接。")
            return
        self.connect_device(
            "tailscale",
            ts_ch,
            profile=device.get("profile"),
            device_key=device.get("key"),
            display_name=device.get("display_name", device.get("name", "")),
        )

    def open_device_file_manager(self, device):
        if not device.get("connectable"):
            self.dashboard_note.setText("设备当前离线或不可连接，无法打开文件管理器。")
            return
        mode = device.get("active_mode")
        data = device.get("active_channel")
        if not mode or not data:
            return
        display_name = device.get("display_name", device.get("name", ""))
        self.dashboard_note.setText(f"正在打开 {display_name} 的文件管理器…")

        def resolve():
            if mode == "tailscale":
                uri = self.network.open_peer(data["ip"])
            else:
                remembered = next(
                    (
                        d
                        for d in self.store.devices()
                        if (d["ip"], d["port"]) == (data["ip"], data["port"])
                    ),
                    data,
                )
                if not remembered.get("token"):
                    grant = pair(
                        data["ip"],
                        data["port"],
                        data["observed_pin"],
                        self.store.client_id,
                        self.store.name,
                        self.bridge.pair_code.emit,
                        self._pair_cancel,
                    )
                    self.store.upsert({**remembered, **grant})
                    remembered = {**remembered, **grant}
                uri = access_uri(
                    remembered["ip"],
                    remembered["port"],
                    remembered["pin"],
                    remembered["token"],
                )
            return {"uri": uri, "display_name": display_name}

        self.work("open_file_manager", resolve)

    @property
    def remote(self):
        return self.session_window

    @remote.setter
    def remote(self, value):
        self.session_window = value

    def _all_session_windows(self):
        windows = []
        if self.session_window is not None:
            windows.append(self.session_window)
        windows.extend(self.detached_windows)
        return windows

    def _focus_existing_session(self, session_key):
        if not session_key:
            return False
        for win in self._all_session_windows():
            idx, _ = win.find_session(session_key)
            if idx >= 0:
                win.tabs.setCurrentIndex(idx)
                win.showNormal()
                win.raise_()
                win.activateWindow()
                return True
        return False

    def connect_device(self, mode, data, profile=None, device_key=None, display_name=""):
        session_key = device_key or f"{mode}:{data['ip']}"
        if self._focus_existing_session(session_key):
            return
        if self._connecting:
            return
        self._connecting = True
        self._pair_cancel.clear()
        config = self.stream_config(profile)

        def connect():
            if mode == "tailscale":
                uri = self.network.open_peer(data["ip"])
            else:
                remembered = next(
                    (
                        d
                        for d in self.store.devices()
                        if (d["ip"], d["port"]) == (data["ip"], data["port"])
                    ),
                    data,
                )
                if not remembered.get("token"):
                    grant = pair(
                        data["ip"],
                        data["port"],
                        data["observed_pin"],
                        self.store.client_id,
                        self.store.name,
                        self.bridge.pair_code.emit,
                        self._pair_cancel,
                    )
                    self.store.upsert({**remembered, **grant})
                    remembered = {**remembered, **grant}
                uri = access_uri(
                    remembered["ip"],
                    remembered["port"],
                    remembered["pin"],
                    remembered["token"],
                )
            v2_endpoint = None
            engine_pref = self.store.setting("stream_engine", "auto")
            if mode == "lan" and engine_pref == "v2_core":
                from .core_engine import find_core_binary, negotiate_v2_endpoint

                if find_core_binary() is not None:
                    v2_endpoint = negotiate_v2_endpoint(uri, timeout=3.0)
            return {
                "uri": uri,
                "config": config,
                "mode": mode,
                "peer": data["ip"],
                "port": int(data.get("port", 47995)),
                "pin": remembered.get("pin", "") if mode == "lan" else "",
                "device_key": session_key,
                "display_name": display_name or data.get("name", data["ip"]),
                "v2_endpoint": v2_endpoint,
            }

        self.work("connect", connect)

    def connect_legacy(self):
        try:
            uri = self.legacy_code.text().strip()
            host, *_ = parse_uri(uri)
            from .network import address_mode

            mode = address_mode(host)
            if mode == "tailscale":
                if self._connecting:
                    return
                self._connecting = True
                config = self.stream_config()
                self.work(
                    "connect",
                    lambda: {
                        "uri": self.network.proxy_uri(uri),
                        "config": config,
                        "mode": mode,
                        "peer": host,
                    },
                )
            else:
                self.open_remote(
                    {
                        "uri": uri,
                        "config": self.stream_config(),
                        "mode": mode,
                    }
                )
        except ValueError as exc:
            self.settings_note.setText(str(exc))

    def open_remote(self, result):
        from .ui import SessionWindow

        if result.get("device_key"):
            self.store.record_recent(
                result["device_key"],
                result.get("display_name", result.get("peer", "")),
                result["mode"],
                result.get("peer", ""),
            )
        if result.get("v2_endpoint"):
            from .core_engine import launch_core_viewer

            host_ip, ep = result["v2_endpoint"]
            cfg = result["config"]
            proc = launch_core_viewer(
                host=host_ip,
                quic_port=ep.quic_port,
                pin=ep.pin,
                key=ep.key,
                title=f"Brioche v2.0 · {result.get('display_name') or host_ip}",
                fps=cfg.fps,
                bitrate_kbps=cfg.bitrate_mbps * 1000,
                chroma_444=bool(self.chroma_444.isChecked()),
            )
            self._v2_viewer_procs.append(proc)
            self.dashboard_note.setText(
                f"已通过 v2.0 原生 Rust UDP/GPU 引擎连接 {result.get('display_name') or host_ip}（F11 全屏 · Ctrl+Alt+1~9 切屏 · Ctrl+Alt+S 锐化）"
            )
            return
        callback = (
            (lambda: self.network.status(result["peer"]))
            if result["mode"] == "tailscale"
            else None
        )
        if self.session_window is None:
            win = SessionWindow()
            win.closed.connect(self.remote_closed)
            win.session_closed.connect(lambda _k: self.refresh_lan())
            win.detach_window_created.connect(self._register_detached_window)
            win.transfer_started.connect(self.transfer_board.add)
            win.transfer_updated.connect(self.transfer_board.update_task)
            self.session_window = win
        def _lan_reconnect_uri(pin=result.get("pin", ""), ip=result.get("peer", ""), port=result.get("port", 47995), fallback=result["uri"]):
            devices = self.store.devices()
            match = None
            if pin:
                match = next((d for d in devices if d.get("pin") == pin and d.get("token")), None)
            if match is None and ip:
                match = next((d for d in devices if d.get("ip") == ip and int(d.get("port", 47995)) == int(port) and d.get("token")), None)
            if match is not None:
                return access_uri(match["ip"], match["port"], match["pin"], match["token"])
            return fallback

        reconnect_cb = (
            (lambda peer=result["peer"]: self.network.open_peer(peer))
            if result["mode"] == "tailscale" and result.get("peer")
            else (_lan_reconnect_uri if result["mode"] == "lan" else None)
        )
        self.session_window.add_session(
            result["uri"],
            result["config"],
            network_mode=result["mode"],
            path_status=callback,
            display_name=result.get("display_name") or result.get("peer", "远程桌面"),
            session_key=result.get("device_key"),
            reconnect_uri=reconnect_cb,
            clipboard_mode=self.clipboard_mode.currentData() or "auto",
            auto_reconnect=self.auto_reconnect.isChecked(),
            audio_enabled=self.sync_audio.isChecked(),
        )
        self.session_window.show()
        self.session_window.raise_()
        self.session_window.activateWindow()

    def _register_detached_window(self, window):
        self.detached_windows.append(window)
        window.transfer_started.connect(self.transfer_board.add)
        window.transfer_updated.connect(self.transfer_board.update_task)
        window.session_closed.connect(lambda _k: self.refresh_lan())
        window.closed.connect(
            lambda w=window: self._detached_closed(w)
        )

    def _detached_closed(self, window):
        if window in self.detached_windows:
            self.detached_windows.remove(window)
        self.refresh_lan()

    def remote_closed(self):
        self.session_window = None
        self.refresh_lan()

    # ------------------------------------------------------------------
    # Pairing, Hosts & Transfers
    # ------------------------------------------------------------------
    def show_pair_code(self, code):
        if not self._connecting:
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("首次配对")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(
            f"配对码：{code}\n\n请在对方电脑上核对相同的配对码，并允许连接。配对后会保存在局域网目录。"
        )
        dialog.setStandardButtons(QMessageBox.StandardButton.Cancel)
        dialog.finished.connect(lambda: self._pair_cancel.set())
        self._pair_dialog = dialog
        dialog.open()

    def confirm_pair(self, request):
        if not self._alive:
            request.answer(False)
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("允许局域网设备配对？")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(
            f"{request.name}（{request.address}）请求连接这台电脑。\n\n配对码：{request.code}\n\n请核对两端显示相同的配对码。允许后，该设备可在共享开启期间连接，直到你撤销配对。"
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        dialog.finished.connect(
            lambda result: (
                request.answer(result == QMessageBox.StandardButton.Yes),
                QTimer.singleShot(200, self.refresh_paired_devices),
            )
        )
        self._incoming_pair_dialog = dialog
        dialog.open()

    def ensure_hosts(self):
        if not self._alive:
            return
        wanted = {
            "lan": self.lan_allowed.isChecked(),
            "tailscale": self.external_allowed.isChecked(),
        }
        for mode in ("lan", "tailscale"):
            host = self.hosts[mode]
            if not wanted[mode]:
                if host and not host.stop_requested.is_set():
                    host.stop()
                    if mode == "tailscale":
                        self.work(
                            "unpublish", lambda h=host: self.network.clear(h.token)
                        )
                continue
            if host:
                continue
            if not self.monitor.currentData():
                continue
            if mode == "tailscale" and not self.environment["tailscale"]["ready"]:
                self.external_note.setText("登录成功后会自动恢复本机外网共享")
                continue
            address = "127.0.0.1" if mode == "tailscale" else self.address.currentData()
            if not address:
                continue
            self.save_settings()
            self.hosts[mode] = Host(
                address,
                0 if mode == "tailscale" else self.port.value(),
                self.encoder.currentData(),
                self.monitor.currentData(),
                status=lambda message, m=mode: self.bridge.host_status.emit(m, message),
                folder=self.store.folder,
                view_only=self.mode.currentData(),
                web_enabled=mode == "lan" and self.web_enabled.isChecked(),
                web_port=self.web_port.value(),
                lan_discovery=mode == "lan",
                on_pair=self.bridge.pair_request.emit,
                session_gate=self.gate,
                name=self.store.name,
                files_enabled=self.file_enabled.isChecked()
                and not self.mode.currentData(),
                file_browser_enabled=self.file_browser_enabled.isChecked()
                and not self.mode.currentData(),
                on_transfer=self.bridge.transfer_request.emit,
                on_transfer_progress=self.bridge.transfer_progress.emit,
                file_gate=self.file_gate,
                clipboard_enabled=self.clipboard_enabled.isChecked()
                and not self.mode.currentData(),
                on_clipboard=self.bridge.host_clipboard.emit,
                audio_enabled=self.audio_enabled.isChecked(),
                on_session_state=self.bridge.host_session_state.emit,
                v2_core_enabled=(self.stream_engine.currentData() != "v1_compat"),
            )
            self.hosts[mode].start()

    def poll_hosts(self):
        for task_id, (request, dialog) in list(self._transfer_dialogs.items()):
            if request.done.is_set():
                dialog.reject()
                self._transfer_dialogs.pop(task_id, None)
        for mode, host in list(self.hosts.items()):
            if host is None:
                continue
            if not host._thread.is_alive():
                self.hosts[mode] = None
                if mode == "tailscale":
                    self._published_host = None
                    self.work("unpublish", lambda h=host: self.network.clear(h.token))
                if host.error:
                    self.host_status(mode, host.error)
                    check = self.lan_allowed if mode == "lan" else self.external_allowed
                    check.setChecked(False)
                else:
                    self.ensure_hosts()
            elif (
                mode == "tailscale"
                and host.ready.is_set()
                and host.pin
                and self._published_host is not host
                and not host.stop_requested.is_set()
            ):
                self._published_host = host
                self.work("publish", lambda h=host: self.network.publish(h))
        if self._incoming_pair_dialog:
            host = self.hosts.get("lan")
            if not host or not host._pair_pending:
                self._incoming_pair_dialog.close()
                self._incoming_pair_dialog = None

    def host_status(self, mode, message):
        if mode == "tailscale":
            self.external_note.setText("本机外网共享：" + message)
        else:
            self._lan_host_message = message
            self.dashboard_note.setText(f"局域网共享：{message}")

    def confirm_transfer(self, request):
        from PySide6.QtWidgets import QFileDialog

        if not self._alive:
            request.respond()
            return
        receiving = request.direction == "receive"
        count = len(request.items)
        names = "、".join(item["name"] for item in request.items[:3])[:140]
        title = (
            f"接收 {names}（{count}个文件），选择保存目录"
            if receiving
            else "对方请求文件：选择要发送的文件（最多32个）"
        )
        self.transfer_board.add(request.id, title, request.cancel)
        self.navigate(self.PAGE_TRANSFERS)
        dialog = QFileDialog(self, title, downloads())
        dialog.setFileMode(
            QFileDialog.FileMode.Directory
            if receiving
            else QFileDialog.FileMode.ExistingFiles
        )
        if receiving:
            dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.filesSelected.connect(
            lambda paths: request.respond(paths[0] if receiving and paths else paths)
        )
        dialog.rejected.connect(lambda: request.respond())
        dialog.finished.connect(
            lambda result: self._transfer_dialogs.pop(request.id, None)
        )
        self._transfer_dialogs[request.id] = (request, dialog)
        dialog.open()

    def transfer_progress(self, value):
        self.transfer_board.update_task(value)

    def account_action(self):
        if self.environment["tailscale"]["ready"]:
            answer = QMessageBox.question(
                self, "退出账号", "退出后会停止本机外网共享及外网连接。局域网不受影响。"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            for win in list(self._all_session_windows()):
                win.close_sessions_by_mode("tailscale")
            host = self.hosts["tailscale"]
            if host:
                host.stop()
            self.work("logout", self.network.logout)
        else:
            self.login_button.setEnabled(False)
            self.sidebar_login_btn.setEnabled(False)
            self.work("login", self.network.login)

    def copy_browser(self):
        host = self.hosts["lan"]
        if host and host.browser_url:
            QApplication.clipboard().setText(host.browser_url)
            msg = (
                "浏览器链接已复制。首次访问请核对 HTTPS 证书 SHA256：\n"
                + host.browser_pin
            )
            self.settings_note.setText(msg)
            self.dashboard_note.setText("已复制局域网浏览器链接")
        else:
            msg = "请开启局域网共享和浏览器访问；修改浏览器选项后需要重新开启局域网共享。"
            self.settings_note.setText(msg)
            self.dashboard_note.setText(msg)

    def refresh_paired_devices(self):
        host = self.hosts.get("lan")
        from .lan import PairStore

        store = host.pair_store if host and host.pair_store else PairStore(self.store.folder)
        self.settings_view.set_paired_devices(store.list_pairs(), self.revoke_pair)

    def revoke_pair(self, client_id):
        host = self.hosts.get("lan")
        from .lan import PairStore

        store = host.pair_store if host and host.pair_store else PairStore(self.store.folder)
        if store.revoke(client_id):
            if host:
                host.disconnect_viewer()
            self.settings_note.setText("已撤销指定局域网设备的配对凭据")
            self.refresh_paired_devices()

    def clear_pairs(self):
        host = self.hosts["lan"]
        from .lan import PairStore

        store = host.pair_store if host else PairStore(self.store.folder)
        if (
            QMessageBox.question(
                self, "撤销配对", "撤销所有局域网配对，并结束当前局域网连接？"
            )
            == QMessageBox.StandardButton.Yes
        ):
            store.clear()
            if host:
                host.disconnect_viewer()
            self.settings_note.setText("局域网配对已撤销")
            self.refresh_paired_devices()

    def job_result(self, result):
        operation = result["op"]
        error = result.get("error")
        value = result.get("value")
        if operation == "environment":
            self._busy = False
            if error:
                self.account_status.setText(error)
                self.sidebar_account_label.setText(error)
            else:
                self.apply_environment(value)
        elif operation == "lan":
            self._lan_busy = False
            if not error:
                self.update_lan_list(value)
        elif operation in ("find", "find_and_connect"):
            self.add_lan_btn.setEnabled(True)
            self.direct_lan_btn.setEnabled(True)
            if error:
                self.dashboard_note.setText(
                    "查找失败：" + error + "。请确认对方已开启局域网共享。"
                )
            else:
                saved_record = {
                    k: v
                    for k, v in value.items()
                    if k in ("ip", "port", "name", "platform", "observed_pin")
                }
                self.store.upsert(saved_record)
                self.refresh_lan()
                if operation == "find_and_connect":
                    if value.get("busy"):
                        self.dashboard_note.setText(
                            f"局域网设备 {value.get('name', value['ip'])} 当前正在使用中，已加入列表。"
                        )
                    else:
                        self.dashboard_note.setText(
                            f"已找到局域网设备 {value.get('name', value['ip'])}（{value['ip']}），正在直连…"
                        )
                        self.connect_device(
                            "lan",
                            value,
                            device_key=f"lan:{value['ip']}:{value['port']}",
                            display_name=value.get("name", value["ip"]),
                        )
                else:
                    self.dashboard_note.setText("已加入设备工作台，双击设备即可配对连接。")
        elif operation == "connect":
            self._connecting = False
            if self._pair_dialog:
                self._pair_dialog.close()
                self._pair_dialog = None
            if error:
                self.dashboard_note.setText(error)
                self.account_status.setText(error)
                self.settings_note.setText(error)
            else:
                self.open_remote(value)
        elif operation in ("login", "logout"):
            self.login_button.setEnabled(True)
            self.sidebar_login_btn.setEnabled(True)
            if error:
                self.account_status.setText(error)
                self.sidebar_account_label.setText(error)
            elif operation == "login" and login_url(value.get("auth_url", "")):
                self._auth_url = value["auth_url"]
                QDesktopServices.openUrl(QUrl(self._auth_url))
            self.refresh_environment()
        elif operation == "publish" and error:
            self._published_host = None
            self.external_note.setText(error)
        elif operation == "open_file_manager":
            if self._pair_dialog:
                self._pair_dialog.close()
                self._pair_dialog = None
            if error:
                self.dashboard_note.setText(error)
            else:
                from .file_manager_ui import FileManagerWindow

                win = FileManagerWindow(
                    value["uri"], remote_name=value.get("display_name", "远端电脑"), parent=None
                )
                win.started.connect(self.transfer_board.add)
                win.updated.connect(self.transfer_board.update_task)
                win.finished.connect(
                    lambda _r, w=win: self._file_windows.remove(w) if w in self._file_windows else None
                )
                self._file_windows.append(win)
                win.show()
                win.raise_()
                win.activateWindow()
        elif operation == "dhcp_migrate":
            if not error and isinstance(value, dict):
                self._dhcp_probing.discard((value["ip"], int(value["port"])))
                self.store.upsert(
                    {
                        k: v
                        for k, v in value.items()
                        if k in ("ip", "port", "name", "platform", "observed_pin")
                    }
                )
                self.dashboard_note.setText(f"已自动更新设备 {value.get('name', value['ip'])} 的最新局域网 IP：{value['ip']}")
                self.refresh_lan()
        elif operation == "display_recovery" and error:
            self.settings_note.setText("临时分辨率恢复：" + error)

    # ------------------------------------------------------------------
    # Window / Tray Lifecycle
    # ------------------------------------------------------------------
    def restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def tray_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.restore_window()

    def quit_application(self):
        self._quitting = True
        self.close()
        QApplication.instance().quit()

    def closeEvent(self, event):
        if not self._quitting:
            event.ignore()
            if QSystemTrayIcon.isSystemTrayAvailable():
                self.hide()
            else:
                self.showMinimized()
            return
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self):
        if self._shutdown:
            return
        self._quitting = self._shutdown = True
        for proc in list(self._v2_viewer_procs):
            try:
                if proc.poll() is None:
                    proc.terminate()
            except OSError:
                pass
        self._v2_viewer_procs.clear()
        if hasattr(self, "tray"):
            self.tray.hide()
        self._alive = False
        if self._qt_clipboard is not None:
            try:
                self._qt_clipboard.dataChanged.disconnect(self._on_host_clipboard_changed)
            except (RuntimeError, TypeError):
                pass
        self._pair_cancel.set()
        self.transfer_board.stop_all()
        for request, dialog in list(self._transfer_dialogs.values()):
            request.cancel()
            dialog.reject()
        self.poll.stop()
        self.network_poll.stop()
        self.lan_poll.stop()
        if hasattr(self, "lan_broadcaster"):
            self.lan_broadcaster.stop()
        if hasattr(self, "lan_listener"):
            self.lan_listener.stop()
        if self._incoming_pair_dialog:
            self._incoming_pair_dialog.close()
        if hasattr(self, "host_pill"):
            self.host_pill.hide_session()
            self.host_pill.close()
        for fm in list(self._file_windows):
            fm.close()
        for win in list(self._all_session_windows()):
            win.close()
        for host in self.hosts.values():
            if host:
                host.stop()
        for host in self.hosts.values():
            if host:
                host.join(5)
        self.network.close()
