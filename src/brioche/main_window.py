"""Simple two-mode connection flow, with read-only background network discovery."""
import threading

from PySide6.QtCore import QObject, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QComboBox, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
                               QSpinBox, QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QCheckBox)
from .capture import list_monitors
from .network import address_mode, lan_addresses
from .embedded_network import EmbeddedNetwork, login_url
from .protocol import FRAME_RATES, StreamConfig, ProtocolError
from .security import access_uri, parse_uri
from .session import Host


def text(value, heading=False):
    label = QLabel(value)
    label.setWordWrap(True)
    label.setObjectName("heading" if heading else "muted")
    return label


def screen_label(monitor):
    direction = " · 竖屏" if monitor["height"] > monitor["width"] else ""
    return f"显示器 {monitor['id']} · {monitor['width']} × {monitor['height']}{direction}"


def mode_box(loopback=False):
    box = QComboBox()
    box.addItem("局域网 · 同一路由器 / Wi-Fi", "lan")
    box.addItem("外网 · Tailscale", "tailscale")
    if loopback:
        box.addItem("本机回环测试", "loopback")
    return box


class EnvironmentBridge(QObject):
    ready = Signal(dict)
    host_status = Signal(str)
    account_result = Signal(dict)
    connection_ready = Signal(dict)
    publication = Signal(object, str)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Brioche")
        self.resize(1060, 780)
        self.setMinimumSize(900, 720)
        self.host, self.remote = None, None
        self.network = EmbeddedNetwork()
        self.login_buttons = []
        self._auth_url = ""
        self._published = False
        self._publish_busy = False
        self._connecting = False
        self._busy, self._alive = False, True
        self.environment = {"lan": [], "tailscale": {"ready": False, "ips": [], "message": "正在检查 Tailscale…"}, "monitors": []}
        self.bridge = EnvironmentBridge(self)
        self.bridge.ready.connect(self.apply_environment)
        self.bridge.host_status.connect(self.host_status)
        self.bridge.account_result.connect(self.account_result)
        self.bridge.connection_ready.connect(self.connection_ready)
        self.bridge.publication.connect(self.publication_result)
        root = QWidget()
        row = QHBoxLayout(root)
        row.setContentsMargins(18, 18, 18, 18)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(205)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(20, 25, 20, 22)
        brand = QLabel("Brioche")
        brand.setObjectName("brand")
        nav.addWidget(brand)
        nav.addWidget(text("你的电脑，随时在手边。"))
        nav.addSpacing(35)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.connect_page())
        self.pages.addWidget(self.host_page())
        self.pages.addWidget(self.help_page())
        for index, title in enumerate(("连接电脑", "共享这台电脑", "使用帮助")):
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, i=index: self.pages.setCurrentIndex(i))
            nav.addWidget(button)
        nav.addStretch()
        nav.addWidget(text("v0.5 · 内置外网连接\n登录后选择设备"))
        row.addWidget(sidebar)
        row.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self.poll = QTimer(self)
        self.poll.setInterval(300)
        self.poll.timeout.connect(self.poll_host)
        self.poll.start()
        self.refresh_environment()
        self.network_poll = QTimer(self)
        self.network_poll.setInterval(5000)
        self.network_poll.timeout.connect(self.refresh_environment)
        self.network_poll.start()

    def network_buttons(self):
        row = QHBoxLayout()
        refresh = QPushButton("刷新网络与屏幕")
        refresh.clicked.connect(self.refresh_environment)
        setup = QPushButton("登录 Tailscale")
        setup.clicked.connect(self.tailscale_help)
        self.login_buttons.append(setup)
        row.addWidget(refresh)
        row.addWidget(setup)
        row.addStretch()
        return row

    def connect_page(self):
        page, layout = self.page("连接电脑", "选择已登录的外网设备，或粘贴另一台电脑的连接码。")
        self.connection_mode = mode_box(True)
        layout.addWidget(self.connection_mode)
        self.connection_note = text("局域网无需 Tailscale。两台电脑需要能够互相访问。")
        layout.addWidget(self.connection_note)
        self.devices = QComboBox()
        self.devices.addItem("选择同一账号的在线设备", None)
        self.devices.setVisible(False)
        layout.addWidget(self.devices)
        self.uri = QTextEdit()
        self.uri.setAcceptRichText(False)
        self.uri.setMaximumHeight(85)
        self.uri.setPlaceholderText("在这里粘贴 Brioche 连接码")
        self.uri.textChanged.connect(self.detect_code_mode)
        layout.addWidget(self.uri)
        paste = QPushButton("粘贴连接码")
        paste.clicked.connect(lambda: self.uri.setPlainText(QApplication.clipboard().text().strip()))
        layout.addWidget(paste)
        self.profile = QComboBox()
        self.profile.addItem("均衡 · 2K / 60 FPS", (60, 16))
        self.profile.addItem("高刷 · 2K / 90 FPS", (90, 20))
        self.profile.addItem("极致 · 2K / 144 FPS", (144, 35))
        self.profile.addItem("自定义", None)
        self.profile.setCurrentIndex(1)
        layout.addWidget(self.profile)
        self.connect_advanced = QWidget()
        form = QFormLayout(self.connect_advanced)
        form.setContentsMargins(0, 0, 0, 0)
        self.resolution = QComboBox()
        for title, value in (("2K 上限（自动适配横竖屏）", "1440p"), ("1080p 上限", "1080p"), ("720p 上限", "720p")):
            self.resolution.addItem(title, value)
        self.fps = QComboBox()
        for fps in FRAME_RATES:
            self.fps.addItem(f"{fps} FPS", fps)
        self.fps.setCurrentIndex(2)
        self.bitrate = QSpinBox()
        self.bitrate.setRange(2, 80)
        self.bitrate.setValue(20)
        self.bitrate.setSuffix(" Mbps")
        form.addRow("分辨率", self.resolution)
        form.addRow("帧率上限", self.fps)
        form.addRow("目标码率", self.bitrate)
        self.advanced_toggle = self.disclosure(layout, self.connect_advanced)
        self.profile.currentIndexChanged.connect(self.apply_profile)
        for control in (self.resolution, self.fps):
            control.currentIndexChanged.connect(self.custom_profile)
        self.bitrate.valueChanged.connect(self.custom_profile)
        self.connect_button = QPushButton("连接远程桌面 →")
        self.connect_button.setObjectName("primary")
        self.connect_button.clicked.connect(self.connect_remote)
        layout.addWidget(self.connect_button)
        layout.addWidget(text("连接后在顶部选择显示器。帧率为上限，实际效果取决于两端设备与网络。"))
        layout.addStretch()
        layout.addLayout(self.network_buttons())
        self.connection_mode.currentIndexChanged.connect(self.update_connection_note)
        return page

    def host_page(self):
        page, layout = self.page("共享这台电脑", "局域网使用连接码；外网登录同一账号后按设备连接。")
        self.share_mode = mode_box(True)
        self.share_mode.currentIndexChanged.connect(self.update_share_options)
        layout.addWidget(self.share_mode)
        self.network_note = text("正在读取网络与显示器…")
        layout.addWidget(self.network_note)
        form = QFormLayout()
        self.address = QComboBox()
        self.monitor = QComboBox()
        self.mode = QComboBox()
        self.mode.addItem("允许鼠标键盘控制", False)
        self.mode.addItem("仅查看画面", True)
        form.addRow("本机地址", self.address)
        form.addRow("默认显示器", self.monitor)
        form.addRow("访问权限", self.mode)
        layout.addLayout(form)
        self.web_enabled = QCheckBox("允许局域网浏览器访问（无需安装控制端）")
        layout.addWidget(self.web_enabled)
        self.browser_panel = QWidget()
        browser_layout = QVBoxLayout(self.browser_panel)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        self.browser_note = text("开启共享后复制浏览器链接。首次访问需要确认本机 HTTPS 证书。")
        browser_layout.addWidget(self.browser_note)
        browser_buttons = QHBoxLayout()
        self.copy_browser = QPushButton("复制浏览器链接")
        self.copy_browser.setEnabled(False)
        self.copy_browser.clicked.connect(lambda: QApplication.clipboard().setText(self.host.browser_url) if self.host else None)
        browser_help = QPushButton("首次访问说明")
        browser_help.clicked.connect(self.browser_help)
        browser_buttons.addWidget(self.copy_browser)
        browser_buttons.addWidget(browser_help)
        browser_layout.addLayout(browser_buttons)
        layout.addWidget(self.browser_panel)
        self.browser_panel.hide()
        self.web_enabled.toggled.connect(self.browser_panel.setVisible)
        self.host_advanced = QWidget()
        advanced = QFormLayout(self.host_advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(47995)
        self.encoder = QComboBox()
        for title, value in (("兼容 H.264（软件）", "libx264"), ("NVIDIA NVENC（实验）", "h264_nvenc"),
                             ("Intel Quick Sync（实验）", "h264_qsv"), ("Apple VideoToolbox（实验）", "h264_videotoolbox")):
            self.encoder.addItem(title, value)
        advanced.addRow("端口", self.port)
        advanced.addRow("编码器", self.encoder)
        self.web_port = QSpinBox()
        self.web_port.setRange(1024, 65535)
        self.web_port.setValue(47996)
        advanced.addRow("浏览器 HTTPS 端口", self.web_port)
        self.disclosure(layout, self.host_advanced)
        self.host_label = text("未开启共享")
        layout.addWidget(self.host_label)
        self.host_uri = QLineEdit()
        self.host_uri.setReadOnly(True)
        self.host_uri.setEchoMode(QLineEdit.EchoMode.Password)
        self.host_uri.setPlaceholderText("开启共享后，点击下方复制连接码")
        layout.addWidget(self.host_uri)
        buttons = QHBoxLayout()
        self.start_host = QPushButton("开启共享")
        self.start_host.setObjectName("primary")
        self.start_host.setEnabled(False)
        self.start_host.clicked.connect(self.toggle_host)
        self.copy = QPushButton("复制连接码")
        self.copy.setEnabled(False)
        self.copy.clicked.connect(lambda: QApplication.clipboard().setText(self.host_uri.text()))
        self.kick = QPushButton("结束当前连接")
        self.kick.clicked.connect(lambda: self.host.disconnect_viewer() if self.host else None)
        for button in (self.start_host, self.copy, self.kick):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        layout.addWidget(text("连接码包含访问权限。停止共享会结束连接，再次开启会生成新码。"))
        layout.addStretch()
        layout.addLayout(self.network_buttons())
        return page

    @staticmethod
    def page(title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 18, 18, 18)
        layout.addWidget(text(title, True))
        layout.addWidget(text(subtitle))
        layout.addSpacing(8)
        return page, layout

    @staticmethod
    def disclosure(layout, panel):
        button = QPushButton("高级设置 ▸")
        button.setCheckable(True)
        button.toggled.connect(panel.setVisible)
        button.toggled.connect(lambda opened: button.setText("收起高级设置 ▾" if opened else "高级设置 ▸"))
        layout.addWidget(button)
        layout.addWidget(panel)
        panel.hide()
        return button

    def apply_profile(self):
        profile = self.profile.currentData()
        if profile is None:
            self.advanced_toggle.setChecked(True)
            return
        for control in (self.resolution, self.fps, self.bitrate):
            control.blockSignals(True)
        self.resolution.setCurrentIndex(0)
        self.fps.setCurrentIndex(self.fps.findData(profile[0]))
        self.bitrate.setValue(profile[1])
        for control in (self.resolution, self.fps, self.bitrate):
            control.blockSignals(False)

    def custom_profile(self):
        self.profile.blockSignals(True)
        self.profile.setCurrentIndex(3)
        self.profile.blockSignals(False)

    def detect_code_mode(self):
        try:
            host, *_ = parse_uri(self.uri.toPlainText().strip())
        except ValueError:
            return
        self.devices.setCurrentIndex(0)
        self.connection_mode.setCurrentIndex(self.connection_mode.findData(address_mode(host)))

    def update_connection_note(self):
        mode = self.connection_mode.currentData()
        note = (self.environment["tailscale"]["message"] + "。两端在 Brioche 中登录同一 Tailscale 账号，被控端开启外网共享。" if mode == "tailscale"
                else "本机测试：只连接这台电脑。" if mode == "loopback"
                else "局域网无需 Tailscale。两台电脑需要能够互相访问。")
        self.connection_note.setText(note)
        self.devices.setVisible(mode == "tailscale")

    def refresh_environment(self):
        if self._busy:
            return
        self._busy = True
        shared_host = self.host if self.share_mode.currentData() == "tailscale" else None
        def work():
            result = {"lan": [], "monitors": [], "tailscale": self.network.status()}
            if shared_host and shared_host.pin and result["tailscale"]["ready"] and not shared_host.stop_requested.is_set():
                try:
                    self.network.publish(shared_host)
                except (RuntimeError, ValueError):
                    pass
            try:
                result["lan"] = lan_addresses()
            except Exception:
                pass
            try:
                result["monitors"] = list_monitors()
            except Exception:
                result["screen_error"] = "无法读取显示器，请检查桌面会话和屏幕权限"
            if self._alive:
                try:
                    self.bridge.ready.emit(result)
                except RuntimeError:
                    pass
        threading.Thread(target=work, name="brioche-discovery", daemon=True).start()

    def apply_environment(self, result):
        self._busy = False
        self.environment = result
        if not self.host:
            selected = self.monitor.currentData()
            self.monitor.clear()
            for monitor in result["monitors"]:
                self.monitor.addItem(screen_label(monitor), monitor["id"])
            index = self.monitor.findData(selected)
            if index >= 0:
                self.monitor.setCurrentIndex(index)
            self.update_share_options()
        ts = result["tailscale"]
        selected = self.devices.currentData()
        self.devices.clear()
        self.devices.addItem("选择同一账号的在线设备，或在下方粘贴连接码", None)
        for peer in sorted(ts.get("peers", []), key=lambda p: p.get("name", "")):
            if peer.get("online"):
                self.devices.addItem(f"{peer['name']} · {peer['ip']}", peer["ip"])
        index = self.devices.findData(selected)
        if index >= 0:
            self.devices.setCurrentIndex(index)
        if login_url(ts.get("auth_url", "")):
            self._auth_url = ts["auth_url"]
        if ts["ready"]:
            self._auth_url = ""
        for button in self.login_buttons:
            button.setText("账号与登录" if ts["ready"] else "继续登录 Tailscale" if self._auth_url else "登录 Tailscale")
        self.update_connection_note()

    def update_share_options(self):
        if self.host:
            return
        selected = self.address.currentData()
        self.address.clear()
        mode = self.share_mode.currentData()
        self.web_enabled.setEnabled(mode != "tailscale")
        if mode == "tailscale":
            self.web_enabled.setChecked(False)
        if mode == "tailscale":
            ts = self.environment["tailscale"]
            if ts["ready"]:
                for address in ts["ips"]:
                    self.address.addItem(address, address)
            note = ts["message"] + "。开启后允许同一账号的设备连接，无需复制连接码。"
        elif mode == "loopback":
            self.address.addItem("127.0.0.1 · 仅本机", "127.0.0.1")
            note = "无需联网，仅用于这台电脑的回环测试。"
        else:
            for entry in self.environment["lan"]:
                self.address.addItem(f"{entry['ip']} · {entry['name']}", entry["ip"])
            note = "地址已自动读取。多张网卡时，选择另一台电脑能够访问的地址。"
            if not self.environment["lan"]:
                note = "没有找到局域网地址，请连接 Wi-Fi 或网线后刷新。"
        index = self.address.findData(selected)
        if index >= 0:
            self.address.setCurrentIndex(index)
        if self.environment.get("screen_error"):
            note += " " + self.environment["screen_error"]
        self.network_note.setText(note)
        self.start_host.setEnabled(self.address.count() > 0 and self.monitor.count() > 0)

    def host_status(self, message):
        self.host_label.setText(message)

    def toggle_host(self):
        if self.host:
            self.start_host.setEnabled(False)
            self.host.stop()
            if self.share_mode.currentData() == "tailscale":
                self.clear_network_share(self.host)
            self.host_uri.clear()
            self.copy.setEnabled(False)
            self.copy_browser.setEnabled(False)
            return
        address = self.address.currentData()
        if not address or not self.monitor.currentData():
            return
        external = self.share_mode.currentData() == "tailscale"
        self._published, self._publish_busy = False, False
        self.host = Host("127.0.0.1" if external else address, 0 if external else self.port.value(), self.encoder.currentData(), self.monitor.currentData(),
                         self.bridge.host_status.emit, view_only=self.mode.currentData(),
                         web_enabled=self.web_enabled.isChecked(), web_port=self.web_port.value())
        self.host.start()
        self.start_host.setText("停止共享")
        for control in (self.share_mode, self.address, self.monitor, self.mode, self.port, self.encoder, self.web_enabled, self.web_port):
            control.setEnabled(False)

    def poll_host(self):
        host = self.host
        if not host:
            return
        if (host.ready.is_set() and host.pin and host._thread.is_alive()
                and not host.stop_requested.is_set() and not self.host_uri.text()):
            if self.share_mode.currentData() == "tailscale":
                if not self._published:
                    if not self._publish_busy:
                        self._publish_busy = True
                        def publish():
                            error = ""
                            try:
                                if not host.stop_requested.is_set():
                                    self.network.publish(host)
                            except (RuntimeError, ValueError) as exc:
                                error = str(exc)
                            if self._alive:
                                self.bridge.publication.emit(host, error)
                        threading.Thread(target=publish, name="brioche-publish", daemon=True).start()
                    return
                self.host_uri.setText(access_uri(self.address.currentData(), 47995, host.pin, host.token))
            else:
                self.host_uri.setText(access_uri(host.bind, host.port, host.pin, host.token))
            self.copy.setEnabled(True)
            if host.browser_url:
                self.copy_browser.setEnabled(True)
                self.browser_note.setText(f"浏览器入口：https://{host.bind}:{host.web_port} · 首次访问需确认本机证书。")
        if not host._thread.is_alive():
            self.host_label.setText(host.error or "共享已停止")
            self.host = None
            if self.share_mode.currentData() == "tailscale":
                self.clear_network_share(host)
            self.host_uri.clear()
            self.copy.setEnabled(False)
            self.copy_browser.setEnabled(False)
            self.browser_note.setText("开启共享后复制浏览器链接。首次访问需要确认本机 HTTPS 证书。")
            self.start_host.setText("开启共享")
            for control in (self.share_mode, self.address, self.monitor, self.mode, self.port, self.encoder, self.web_enabled, self.web_port):
                control.setEnabled(True)
            self.update_share_options()

    def clear_network_share(self, host):
        def clear():
            try:
                self.network.clear(host.token)
            except RuntimeError:
                pass
        threading.Thread(target=clear, name="brioche-unpublish", daemon=True).start()

    def publication_result(self, host, error):
        if host is not self.host:
            return
        self._publish_busy = False
        if error:
            self.host_label.setText(error)
            host.stop()
        else:
            self._published = True

    def connect_remote(self):
        if self.remote:
            self.remote.raise_()
            return
        if self._connecting:
            return
        mode = self.connection_mode.currentData()
        peer = self.devices.currentData() if mode == "tailscale" else None
        try:
            uri = self.uri.toPlainText().strip()
            if not peer:
                host, *_ = parse_uri(uri)
                if address_mode(host) != mode:
                    raise ValueError("连接码与所选连接方式不一致，请重新粘贴连接码。")
            if mode == "tailscale" and not self.environment["tailscale"]["ready"]:
                raise ValueError(self.environment["tailscale"]["message"])
            config = StreamConfig(self.resolution.currentData(), self.fps.currentData(), self.bitrate.value())
        except (ValueError, ProtocolError) as exc:
            QMessageBox.warning(self, "暂时无法连接", str(exc))
            return
        if mode != "tailscale":
            self.connection_ready({"uri": uri, "config": config, "mode": mode})
            return
        self._connecting = True
        self.connect_button.setEnabled(False)
        self.connection_note.setText("正在联系对方 Brioche 并核验账号…")
        def work():
            result = {"config": config, "mode": mode, "peer": peer or host}
            try:
                result["uri"] = self.network.open_peer(peer) if peer else self.network.proxy_uri(uri)
            except (RuntimeError, ValueError) as exc:
                result["error"] = str(exc)
            if self._alive:
                self.bridge.connection_ready.emit(result)
        threading.Thread(target=work, name="brioche-connect", daemon=True).start()

    def connection_ready(self, result):
        from .ui import RemoteWindow
        self._connecting = False
        if result.get("error"):
            self.connect_button.setEnabled(True)
            self.connection_note.setText(result["error"])
            return
        path_status = (lambda: self.network.status(result["peer"])) if result["mode"] == "tailscale" else None
        self.remote = RemoteWindow(result["uri"], result["config"], network_mode=result["mode"], path_status=path_status)
        self.remote.closed.connect(self.remote_closed)
        self.remote.show()
        self.connect_button.setEnabled(False)

    def remote_closed(self):
        self.remote = None
        self.connect_button.setEnabled(True)

    def tailscale_help(self):
        if self.environment["tailscale"]["ready"]:
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Tailscale 账号")
            dialog.setText("已通过 Brioche 内置网络登录。\n同一账号设备可互相连接，被控端需要主动开启外网共享。\n\n退出登录会停止外网共享和连接，局域网模式仍可使用。")
            logout = dialog.addButton("退出登录", QMessageBox.ButtonRole.DestructiveRole)
            dialog.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
            dialog.exec()
            if dialog.clickedButton() != logout:
                return
            if self.remote and self.remote.network_mode == "tailscale":
                self.remote.close()
            if self.host and self.share_mode.currentData() == "tailscale":
                self.host.stop()
            action = "logout"
        elif self._auth_url:
            QDesktopServices.openUrl(QUrl(self._auth_url))
            return
        else:
            action = "login"
        for button in self.login_buttons:
            button.setEnabled(False)
        def work():
            result = {"action": action}
            try:
                if action == "login":
                    result["status"] = self.network.login()
                else:
                    self.network.logout()
            except RuntimeError as exc:
                result["error"] = str(exc)
            if self._alive:
                self.bridge.account_result.emit(result)
        threading.Thread(target=work, name="brioche-account", daemon=True).start()

    def account_result(self, result):
        for button in self.login_buttons:
            button.setEnabled(True)
        if result.get("error"):
            QMessageBox.warning(self, "外网连接", result["error"])
        else:
            status = result.get("status", {})
            url = status.get("auth_url", "")
            if login_url(url):
                self._auth_url = url
                QDesktopServices.openUrl(QUrl(url))
            elif result["action"] == "login" and not status.get("ready"):
                self.connection_note.setText("正在获取登录链接，请稍后点击继续登录或刷新。")
        self.refresh_environment()

    def browser_help(self):
        message = ("1. 选择局域网，勾选浏览器访问并开启共享。\n"
                   "2. 点击“复制浏览器链接”，在另一台设备的浏览器中打开。\n"
                   "3. 确认地址是这台电脑，首次访问需在浏览器的证书提示页继续。\n"
                   "4. 点击“开始远程”；需要操作时点击“启用控制”。\n\n"
                   "链接含临时访问码，请通过可信渠道传递。默认 HTTPS 端口为 47996。\n"
                   "网页优先使用 H.264，最高 60 FPS；不支持时回退兼容模式（30 FPS）。实际帧率取决于设备和网络。")
        if self.host and self.host.browser_pin:
            message += "\n\n本机浏览器证书 SHA256（可与浏览器证书详情核对）：\n" + self.host.browser_pin
        QMessageBox.information(self, "浏览器远程桌面", message)

    def help_page(self):
        page, layout = self.page("三步连接", "1. 被控端开启共享。\n2. 复制、传递连接码。\n3. 控制端粘贴并连接。")
        layout.addWidget(text("局域网：两台电脑在可互访的同一网络，无需 Tailscale。\n\n"
            "外网：两端在 Brioche 中登录同一 Tailscale 账号，被控端开启外网共享，控制端选择设备连接。"
            "Brioche 已内置 Tailscale，无需另外安装；点击登录完成浏览器授权。\n\n"
            "切换屏幕：在远程窗口顶部选择显示器；一次只传输选中的一个屏幕。"
            "显示器插拔后，请停止并重新开启共享以刷新列表。\n\n"
            "释放输入：Ctrl+Alt+Shift；全屏：F11。\n\n"
            "浏览器访问：局域网共享时勾选浏览器访问，复制链接到另一台设备打开。"
            "需要确认首次访问的本机证书；网页中点击启用控制后即可操作。\n\n"
            "Ubuntu 被控端需要 Xorg；macOS 需要屏幕录制和辅助功能权限。"
            "Windows 与 macOS 仍需真机验收。\n\n"
            "2K90/144 为性能目标，当前软件编码不保证达到目标帧率。"))
        layout.addStretch()
        return page

    def closeEvent(self, event):
        self._alive = False
        self.network_poll.stop()
        if self.remote:
            self.remote.close()
        if self.host:
            self.host.stop()
            self.host.join(5)
        self.network.close()
        super().closeEvent(event)
