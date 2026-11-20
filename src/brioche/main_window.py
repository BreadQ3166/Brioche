"""Device-first desktop: account devices, remembered LAN devices, and quiet settings."""

from concurrent.futures import ThreadPoolExecutor
import platform
import threading
import time
from PySide6.QtCore import QObject, Signal, QTimer, QUrl, Qt, QSize
from PySide6.QtGui import QDesktopServices, QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from . import __version__
from .capture import list_monitors
from .device_store import DeviceStore
from .embedded_network import EmbeddedNetwork, login_url
from .lan import discover, pair, target
from .network import lan_addresses
from .protocol import FRAME_RATES, StreamConfig
from .security import access_uri, parse_uri
from .session import Host, SessionGate
from .ui_components import DeviceList, RoundedComboBox, PageTransition
from .transfer_ui import TransferBoard, downloads
QComboBox = RoundedComboBox


def text(value, heading=False):
    label = QLabel(value)
    label.setWordWrap(True)
    label.setObjectName("heading" if heading else "muted")
    return label


def screen_label(monitor):
    return f"显示器 {monitor['id']} · {monitor['width']} × {monitor['height']}"


class EnvironmentBridge(QObject):
    ready = Signal(dict)
    result = Signal(dict)
    pair_request = Signal(object)
    pair_code = Signal(str)
    host_status = Signal(str, str)
    transfer_request = Signal(object)
    transfer_progress = Signal(dict)


class MainWindow(QMainWindow):
    def __init__(self, folder=None, network=None):
        super().__init__()
        self.setWindowTitle("Brioche")
        self.resize(1020, 730)
        self.setMinimumSize(830, 620)
        self.store = DeviceStore(folder)
        self.network = network or EmbeddedNetwork(self.store.folder / "network")
        self.gate = SessionGate()
        self.file_gate = SessionGate()
        self._transfer_dialogs = {}
        self.hosts = {"lan": None, "tailscale": None}
        self.remote = None
        self._alive, self._busy, self._lan_busy, self._connecting = (
            True,
            False,
            False,
            False,
        )
        self._published_host = None
        self._auth_url = ""
        self._pair_cancel = threading.Event()
        self._pair_dialog, self._incoming_pair_dialog = None, None
        self._misses = {}
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
        self.bridge.transfer_request.connect(self.confirm_transfer)
        self.bridge.transfer_progress.connect(self.transfer_progress)
        root = QWidget()
        row = QHBoxLayout(root)
        row.setContentsMargins(16, 16, 16, 16)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(210)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(18, 24, 18, 18)
        brand = QLabel("Brioche")
        brand.setObjectName("brand")
        nav.addWidget(brand)
        nav.addWidget(text("选择电脑，即刻连接。"))
        nav.addSpacing(30)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.internet_page())
        self.pages.addWidget(self.lan_page())
        self.pages.addWidget(self.settings_page())
        transfer_page, transfer_layout = self.page('文件传输', '连接设备后，在远程窗口点击“文件互传”。接收前确认保存位置。')
        self.transfer_board = TransferBoard();transfer_layout.addWidget(self.transfer_board,1)
        self.pages.addWidget(transfer_page)
        self.transitions = PageTransition(self.pages)
        self.nav_buttons = []
        for index, title in enumerate(("外网设备", "局域网设备", "设置", "文件传输")):
            button = QPushButton(title)
            button.setCheckable(True)
            button.setObjectName('navButton')
            button.clicked.connect(lambda checked=False, i=index: self.navigate(i))
            self.nav_buttons.append(button)
            nav.addWidget(button)
        nav.addStretch()
        nav.addWidget(text("远程分辨率"))
        nav.addWidget(self.resolution)
        nav.addWidget(self.match_display)
        nav.addWidget(text("源屏匹配：断开后恢复\n目前支持 Ubuntu Xorg"))
        nav.addSpacing(16)
        nav.addWidget(text(f"v{__version__}\n局域网 · 内置 Tailscale"))
        row.addWidget(sidebar)
        row.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.navigate(0)
        self.apply_motion()
        self.poll = QTimer(self)
        self.poll.setInterval(300)
        self.poll.timeout.connect(self.poll_hosts)
        self.poll.start()
        self.network_poll = QTimer(self)
        self.network_poll.setInterval(3000)
        self.network_poll.timeout.connect(self.refresh_environment)
        self.network_poll.start()
        self.lan_poll = QTimer(self)
        self.lan_poll.setInterval(4000)
        self.lan_poll.timeout.connect(self.refresh_lan)
        self.lan_poll.start()
        self.refresh_environment()
        self.refresh_lan()
        from .display_mode import recover

        self.work("display_recovery", lambda: recover(self.store.folder))

    def navigate(self, index):
        self.transitions.show(index, not self.store.setting('reduced_motion', False))
        for i, button in enumerate(self.nav_buttons):
            button.setChecked(i == index)

    @staticmethod
    def page(title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 16, 12)
        layout.addWidget(text(title, True))
        layout.addWidget(text(subtitle))
        layout.addSpacing(12)
        return page, layout

    def internet_page(self):
        page, layout = self.page(
            "外网设备", "同一账号的电脑会自动出现，在线且已允许共享时点击即可连接。"
        )
        account = QHBoxLayout()
        self.account_status = text("正在检查账号…")
        self.login_button = QPushButton("登录 Tailscale")
        self.login_button.setObjectName("primary")
        self.login_button.clicked.connect(self.account_action)
        account.addWidget(self.account_status, 1)
        account.addWidget(self.login_button)
        layout.addLayout(account)
        self.internet_list = DeviceList()
        self.internet_list.itemClicked.connect(self.internet_clicked)
        self.internet_list.itemActivated.connect(self.internet_clicked)
        layout.addWidget(self.internet_list, 1)
        self.internet_empty = text("登录后，设备状态会自动更新。")
        layout.addWidget(self.internet_empty)
        self.external_allowed = QCheckBox("允许同账号设备连接这台电脑（记住此选择）")
        self.external_allowed.setChecked(
            bool(self.store.setting("external_allowed", False))
        )
        self.external_allowed.toggled.connect(self.share_preference)
        layout.addWidget(self.external_allowed)
        self.external_note = text("未开启本机外网共享")
        layout.addWidget(self.external_note)
        return page

    def lan_page(self):
        page, layout = self.page(
            "局域网设备",
            "输入对方 IP 查找并添加。首次由对方确认配对，以后直接点击连接。",
        )
        search = QHBoxLayout()
        self.lan_ip = QLineEdit()
        self.lan_ip.setPlaceholderText("例如 192.168.1.8")
        self.lan_ip.returnPressed.connect(self.find_lan)
        self.find_button = QPushButton("查找并添加")
        self.find_button.setObjectName("primary")
        self.find_button.clicked.connect(self.find_lan)
        search.addWidget(self.lan_ip, 1)
        search.addWidget(self.find_button)
        layout.addLayout(search)
        self.lan_list = DeviceList()
        self.lan_list.itemClicked.connect(self.lan_clicked)
        self.lan_list.itemActivated.connect(self.lan_clicked)
        layout.addWidget(self.lan_list, 1)
        actions = QHBoxLayout()
        self.lan_note = text("目录会保存在本机，不进行网段扫描。")
        remove = QPushButton("移除选中设备")
        remove.clicked.connect(self.remove_lan)
        actions.addWidget(self.lan_note, 1)
        actions.addWidget(remove)
        layout.addLayout(actions)
        self.lan_allowed = QCheckBox("允许局域网设备连接这台电脑（首次配对需本机确认）")
        self.lan_allowed.setChecked(bool(self.store.setting("lan_allowed", False)))
        self.lan_allowed.toggled.connect(self.share_preference)
        layout.addWidget(self.lan_allowed)
        self.local_address = text("正在读取本机 IP…")
        layout.addWidget(self.local_address)
        return page

    def settings_page(self):
        content, layout = self.page(
            "设置", "常用操作留在设备页，画质与共享选项在这里调整。"
        )
        form = QFormLayout()
        self.profile = QComboBox()
        for label, data in (
            ("均衡 · 2K / 60 FPS", (60, 16)),
            ("高刷 · 2K / 90 FPS", (90, 20)),
            ("极致 · 2K / 144 FPS", (144, 35)),
            ("自定义", None),
        ):
            self.profile.addItem(label, data)
        self.monitor = QComboBox()
        self.mode = QComboBox()
        self.mode.addItem("允许鼠标键盘控制", False)
        self.mode.addItem("仅查看画面", True)
        self.mode.setCurrentIndex(1 if self.store.setting("view_only", False) else 0)
        form.addRow("连接画质", self.profile)
        form.addRow("默认共享屏幕", self.monitor)
        form.addRow("本机访问权限", self.mode)
        layout.addLayout(form)
        self.settings_note = text(
            "共享过程中修改屏幕、编码或权限设置，将在下次开启共享时生效。"
        )
        layout.addWidget(self.settings_note)
        self.web_enabled = QCheckBox("允许局域网浏览器访问")
        self.web_enabled.setChecked(bool(self.store.setting("web_enabled", False)))
        layout.addWidget(self.web_enabled)
        self.file_enabled = QCheckBox('允许文件互传（接收前确认）')
        self.file_enabled.setChecked(bool(self.store.setting('files_enabled', True)))
        self.file_enabled.toggled.connect(self.save_settings)
        layout.addWidget(self.file_enabled)
        self.reduced_motion = QCheckBox('减少动态效果')
        self.reduced_motion.setChecked(bool(self.store.setting('reduced_motion', False)))
        self.reduced_motion.toggled.connect(self.apply_motion)
        layout.addWidget(self.reduced_motion)
        if platform.system() == 'Darwin':
            permissions = QHBoxLayout()
            capture_permission=QPushButton('屏幕录制权限')
            capture_permission.clicked.connect(lambda:self.mac_permission('screen'))
            input_permission=QPushButton('辅助功能权限')
            input_permission.clicked.connect(lambda:self.mac_permission('input'))
            permissions.addWidget(capture_permission);permissions.addWidget(input_permission)
            layout.addLayout(permissions)
        browser = QPushButton("复制浏览器链接")
        browser.clicked.connect(self.copy_browser)
        layout.addWidget(browser)
        advanced = QWidget()
        advanced_form = QFormLayout(advanced)
        advanced_form.setContentsMargins(0, 0, 0, 0)
        self.address = QComboBox()
        advanced_form.addRow("局域网共享地址", self.address)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(self.store.setting("port", 47995)))
        self.web_port = QSpinBox()
        self.web_port.setRange(1024, 65535)
        self.web_port.setValue(int(self.store.setting("web_port", 47996)))
        self.encoder = QComboBox()
        self.encoder.addItem("自动 · 优先可用硬件编码", "auto")
        self.encoder.addItem("兼容 · 软件 H.264", "libx264")
        self.encoder.setCurrentIndex(
            max(0, self.encoder.findData(self.store.setting("encoder", "auto")))
        )
        self.resolution = QComboBox()
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
        self.match_display = QCheckBox("源屏也匹配")
        self.match_display.setChecked(bool(self.store.setting("match_display", False)))
        self.match_display.setEnabled(self.resolution.currentData() != "native")
        self.match_display.setToolTip(
            "临时调整被控电脑的屏幕分辨率，正常断开后恢复。需要 Ubuntu Xorg 和允许键鼠控制；不勾选时只缩放视频。"
        )
        self.match_display.toggled.connect(self.save_settings)
        self.fps = QComboBox()
        for fps in FRAME_RATES:
            self.fps.addItem(f"{fps} FPS", fps)
        self.fps.setCurrentIndex(
            max(0, self.fps.findData(self.store.setting("fps", 60)))
        )
        self.bitrate = QSpinBox()
        self.bitrate.setRange(2, 80)
        self.bitrate.setValue(int(self.store.setting("bitrate", 16)))
        self.bitrate.setSuffix(" Mbps")
        for title, widget in (
            ("共享端口", self.port),
            ("浏览器端口", self.web_port),
            ("编码策略", self.encoder),
            ("帧率上限", self.fps),
            ("目标码率", self.bitrate),
        ):
            advanced_form.addRow(title, widget)
        self.legacy_code = QLineEdit()
        self.legacy_code.setEchoMode(QLineEdit.EchoMode.Password)
        self.legacy_code.setPlaceholderText("兼容旧版连接码（可选）")
        legacy = QPushButton("使用连接码")
        legacy.clicked.connect(self.connect_legacy)
        advanced_form.addRow(self.legacy_code, legacy)
        clear = QPushButton("撤销本机所有局域网配对")
        clear.clicked.connect(self.clear_pairs)
        advanced_form.addRow(clear)
        self.advanced_toggle = QPushButton("高级设置 ▸")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.toggled.connect(advanced.setVisible)
        self.advanced_toggle.toggled.connect(
            lambda opened: self.advanced_toggle.setText(
                "收起高级设置 ▾" if opened else "高级设置 ▸"
            )
        )
        layout.addWidget(self.advanced_toggle)
        layout.addWidget(advanced)
        advanced.hide()
        self.connect_advanced = advanced
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
        self.profile.currentIndexChanged.connect(self.apply_profile)
        for control in (self.resolution, self.fps):
            control.currentIndexChanged.connect(self.custom_profile)
        self.bitrate.valueChanged.connect(self.custom_profile)
        for control in (self.mode, self.encoder, self.address, self.monitor):
            control.currentIndexChanged.connect(self.save_settings)
        for control in (self.port, self.web_port):
            control.valueChanged.connect(self.save_settings)
        self.web_enabled.toggled.connect(self.save_settings)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

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
        else:
            self.advanced_toggle.setChecked(True)
        self.save_settings()

    def custom_profile(self):
        self.profile.blockSignals(True)
        self.profile.setCurrentIndex(3)
        self.profile.blockSignals(False)
        self.match_display.setEnabled(self.resolution.currentData() != "native")
        self.save_settings()

    def stream_config(self):
        return StreamConfig(
            self.resolution.currentData(),
            self.fps.currentData(),
            self.bitrate.value(),
            self.match_display.isChecked()
            and self.resolution.currentData() != "native",
        )

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
            files_enabled=self.file_enabled.isChecked(),
        )
        for host in self.hosts.values():
            if host:
                host.files_enabled = self.file_enabled.isChecked() and not host.view_only
                if not host.files_enabled:host.cancel_transfers()

    def apply_motion(self):
        enabled = not self.reduced_motion.isChecked()
        self.store.configure(reduced_motion=not enabled)
        for widget in (self.internet_list,self.lan_list):widget.set_animations(enabled)

    def mac_permission(self, kind):
        from .mac_permissions import screen_allowed,input_allowed
        allowed=(screen_allowed if kind=='screen' else input_allowed)(request=True)
        self.settings_note.setText('权限已开启' if allowed else '请在系统偏好设置 → 安全性与隐私中授权；屏幕录制授权后需重新打开 Brioche。')

    def share_preference(self):
        self.store.configure(
            external_allowed=self.external_allowed.isChecked(),
            lan_allowed=self.lan_allowed.isChecked(),
        )
        self.ensure_hosts()

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
        self.environment = result
        status = result["tailscale"]
        self.account_status.setText(status["message"])
        if login_url(status.get("auth_url", "")):
            self._auth_url = status["auth_url"]
        if status["ready"]:
            self._auth_url = ""
        self.login_button.setText(
            "账号 / 退出"
            if status["ready"]
            else "继续登录" if self._auth_url else "登录 Tailscale"
        )
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
        records = []
        for peer in peers:
            ip = peer["ip"]
            alive = bool(peer.get("reachable", False))
            self._misses[ip] = 0 if alive else self._misses.get(ip, 0) + 1
            if alive:
                label = (
                    "使用中"
                    if peer.get("busy")
                    else (
                        "在线 · 点击连接"
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
            records.append(
                (
                    ip,
                    peer.get("name", ip),
                    label,
                    {
                        **peer,
                        "connectable": alive
                        and peer.get("sharing")
                        and not peer.get("busy"),
                    },
                )
            )
        self.render_list(self.internet_list, records)
        self.internet_empty.setText(
            "设备状态约每3秒更新；离线设备保留显示。"
            if records
            else (
                "暂无同账号设备。请在另一台电脑打开新版 Brioche 并登录同一账号。"
                if status["ready"]
                else "登录后显示同一账号的设备，局域网无需登录。"
            )
        )
        addresses = "、".join(a["ip"] for a in result["lan"]) or "未连接局域网"
        self.local_address.setText("本机 IP：" + addresses)
        self.ensure_hosts()

    @staticmethod
    def render_list(widget, records):
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
        for key, name, status, data in records:
            keep.add(key)
            item = existing.get(key)
            if item is None:
                item = QListWidgetItem(widget)
                size=widget.gridSize() if isinstance(widget,DeviceList) and widget.gridSize().width()>0 else QSize(260,220)
                item.setSizeHint(size)
                if isinstance(widget,DeviceList):widget.new_entry(key)
            item.setText(f'{name}\n{status}    ·    {data.get("ip", "")}')
            item.setData(Qt.ItemDataRole.UserRole, {**data, "key": key,'display_name':name,'status_text':status})
            item.setToolTip(status)
            item.setForeground(
                QColor(
                    "#292823"
                    if data.get("connectable")
                    or data.get("online")
                    or data.get("reachable")
                    else "#8b867c"
                )
            )
            if selected == key:
                widget.setCurrentItem(item)
        for key, item in existing.items():
            if key not in keep:
                widget.takeItem(widget.row(item))

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
        records = []
        for device in self.store.devices():
            key = (device["ip"], device["port"])
            state = self.lan_state.get(key, device)
            online = state.get("online", False)
            status = (
                (
                    "使用中"
                    if state.get("busy")
                    else "在线 · 点击连接" if device.get("token") else "在线 · 点击配对"
                )
                if online
                else state.get("error", "待检测")
            )
            records.append(
                (
                    f"{key[0]}:{key[1]}",
                    device.get("name", device["ip"]),
                    status,
                    {**state, "connectable": online and not state.get("busy")},
                )
            )
        self.render_list(self.lan_list, records)

    def find_lan(self):
        try:
            ip, port = target(self.lan_ip.text())
        except ValueError as exc:
            self.lan_note.setText(str(exc))
            return
        self.find_button.setEnabled(False)
        self.lan_note.setText("正在查找 Brioche…")
        existing = next(
            (d for d in self.store.devices() if (d["ip"], d["port"]) == (ip, port)), {}
        )
        self.work("find", lambda: discover(ip, port, existing.get("pin")))

    def remove_lan(self):
        item = self.lan_list.currentItem()
        if item:
            data = item.data(Qt.ItemDataRole.UserRole)
            self.store.remove(data["ip"], data["port"])
            self.update_lan_list(list(self.lan_state.values()))

    def internet_clicked(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data.get("connectable"):
            self.account_status.setText(
                "设备需在线并允许外网共享，使用中的设备请先结束原连接。"
            )
            return
        self.connect_device("tailscale", data)

    def lan_clicked(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data.get("connectable"):
            self.connect_device("lan", data)
        else:
            self.lan_note.setText(data.get("error", "对方尚未在线，或已有活动连接。"))

    def connect_device(self, mode, data):
        if self.remote:
            self.remote.raise_()
            return
        if self._connecting:
            return
        self._connecting = True
        self._pair_cancel.clear()
        config = self.stream_config()

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
            return {"uri": uri, "config": config, "mode": mode, "peer": data["ip"]}

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
        from .ui import RemoteWindow

        callback = (
            (lambda: self.network.status(result["peer"]))
            if result["mode"] == "tailscale"
            else None
        )
        self.remote = RemoteWindow(
            result["uri"],
            result["config"],
            network_mode=result["mode"],
            path_status=callback,
        )
        self.remote.closed.connect(self.remote_closed)
        self.remote.transfer_started.connect(self.transfer_board.add)
        self.remote.transfer_updated.connect(self.transfer_board.update_task)
        self.remote.show()

    def remote_closed(self):
        self.remote = None
        self.refresh_lan()

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
            lambda result: request.answer(result == QMessageBox.StandardButton.Yes)
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
                files_enabled=self.file_enabled.isChecked() and not self.mode.currentData(),
                on_transfer=self.bridge.transfer_request.emit,
                on_transfer_progress=self.bridge.transfer_progress.emit,
                file_gate=self.file_gate,
            )
            self.hosts[mode].start()

    def poll_hosts(self):
        for task_id,(request,dialog) in list(self._transfer_dialogs.items()):
            if request.done.is_set():
                dialog.reject();self._transfer_dialogs.pop(task_id,None)
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
        elif self.environment["lan"]:
            self.local_address.setText(
                "本机 IP："
                + "、".join(a["ip"] for a in self.environment["lan"])
                + " · "
                + message
            )

    def confirm_transfer(self, request):
        from PySide6.QtWidgets import QFileDialog
        if not self._alive:
            request.respond();return
        receiving=request.direction=='receive'
        count=len(request.items)
        names='、'.join(item['name'] for item in request.items[:3])[:140]
        title=f'接收 {names}（{count}个文件），选择保存目录' if receiving else '对方请求文件：选择要发送的文件（最多32个）'
        self.transfer_board.add(request.id,title,request.cancel)
        self.navigate(3)
        dialog=QFileDialog(self,title,downloads())
        dialog.setFileMode(QFileDialog.FileMode.Directory if receiving else QFileDialog.FileMode.ExistingFiles)
        if receiving:dialog.setOption(QFileDialog.Option.ShowDirsOnly,True)
        dialog.filesSelected.connect(lambda paths:request.respond(paths[0] if receiving and paths else paths))
        dialog.rejected.connect(lambda:request.respond())
        dialog.finished.connect(lambda result:self._transfer_dialogs.pop(request.id,None))
        self._transfer_dialogs[request.id]=(request,dialog)
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
            if self.remote and self.remote.network_mode == "tailscale":
                self.remote.close()
            host = self.hosts["tailscale"]
            if host:
                host.stop()
            self.work("logout", self.network.logout)
        else:
            self.login_button.setEnabled(False)
            self.work("login", self.network.login)

    def copy_browser(self):
        host = self.hosts["lan"]
        if host and host.browser_url:
            QApplication.clipboard().setText(host.browser_url)
            self.settings_note.setText(
                "浏览器链接已复制。首次访问请核对 HTTPS 证书 SHA256：\n"
                + host.browser_pin
            )
        else:
            self.settings_note.setText(
                "请开启局域网共享和浏览器访问；修改浏览器选项后需要重新开启局域网共享。"
            )

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

    def job_result(self, result):
        operation = result["op"]
        error = result.get("error")
        value = result.get("value")
        if operation == "environment":
            self._busy = False
            if error:
                self.account_status.setText(error)
            else:
                self.apply_environment(value)
        elif operation == "lan":
            self._lan_busy = False
            if not error:
                self.update_lan_list(value)
        elif operation == "find":
            self.find_button.setEnabled(True)
            if error:
                self.lan_note.setText(
                    "查找失败：" + error + "。请确认对方已开启局域网共享。"
                )
            else:
                self.store.upsert(
                    {
                        k: v
                        for k, v in value.items()
                        if k in ("ip", "port", "name", "platform", "observed_pin")
                    }
                )
                self.lan_note.setText("已加入目录，点击设备即可配对连接。")
                self.refresh_lan()
        elif operation == "connect":
            self._connecting = False
            if self._pair_dialog:
                self._pair_dialog.close()
                self._pair_dialog = None
            if error:
                self.lan_note.setText(error)
                self.account_status.setText(error)
                self.settings_note.setText(error)
            else:
                self.open_remote(value)
        elif operation in ("login", "logout"):
            self.login_button.setEnabled(True)
            if error:
                self.account_status.setText(error)
            elif operation == "login" and login_url(value.get("auth_url", "")):
                self._auth_url = value["auth_url"]
                QDesktopServices.openUrl(QUrl(self._auth_url))
            self.refresh_environment()
        elif operation == "publish" and error:
            self._published_host = None
            self.external_note.setText(error)
        elif operation == "display_recovery" and error:
            self.settings_note.setText("临时分辨率恢复：" + error)

    def closeEvent(self, event):
        self._alive = False
        self._pair_cancel.set()
        self.transfer_board.stop_all()
        for request,dialog in list(self._transfer_dialogs.values()):request.cancel();dialog.reject()
        self.poll.stop()
        self.network_poll.stop()
        self.lan_poll.stop()
        if self._incoming_pair_dialog:
            self._incoming_pair_dialog.close()
        if self.remote:
            self.remote.close()
        for host in self.hosts.values():
            if host:
                host.stop()
        for host in self.hosts.values():
            if host:
                host.join(5)
        self.network.close()
        super().closeEvent(event)
