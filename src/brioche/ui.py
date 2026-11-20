"""Thin Qt desktop shell. Network threads publish into a single-frame mailbox."""
import socket
import threading
import time
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QKeySequence, QShortcut, QIcon
from PySide6.QtWidgets import (QApplication, QComboBox, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
                               QSpinBox, QStackedWidget, QTextEdit, QVBoxLayout, QWidget)
from .protocol import StreamConfig, FRAME_RATES, ENCODERS
from .security import access_uri, parse_uri
from .session import Host, Viewer
from .network import address_mode, tailscale_status

STYLE = """
QWidget { background: #f7f5f0; color: #292823; font-size: 14px; }
QMainWindow { background: #f7f5f0; }
QLabel#brand { font-size: 27px; font-weight: 650; color: #9c5a30; }
QLabel#heading { font-size: 25px; font-weight: 650; }
QLabel#muted { color: #6e6a61; }
QFrame#sidebar { background: #eeeae2; border-radius: 12px; }
QFrame#sidebar QLabel { background: transparent; }
QLineEdit, QSpinBox, QComboBox, QTextEdit {
 background: #fffdf8; border: 1px solid #d6cfc1; border-radius: 7px; padding: 9px;
 selection-background-color: #bd713d;
}
QPushButton { background: #ece6dc; border: 1px solid #d6cfc1; border-radius: 7px;
 padding: 11px 18px; }
QPushButton:hover { background: #e3d7c8; }
QPushButton:disabled { color: #9d998f; background: #eeeae2; }
QPushButton#primary { color: white; background: #bd713d; border: none; }
QPushButton#primary:hover { background: #a65f31; }
QPushButton#danger { color: #a44035; }
"""


class Bridge(QObject):
    status = Signal(str)
    info = Signal(dict)
    stats = Signal(dict)
    network = Signal(dict)


class FrameMailbox:
    def __init__(self):
        self.lock = threading.Lock()
        self.image = None

    def publish(self, frame):
        plane = frame.planes[0]
        # A deep copy owns the pixels after PyAV releases the decoded frame.
        image = QImage(bytes(plane), frame.width, frame.height, plane.line_size,
                       QImage.Format.Format_RGB888).copy()
        with self.lock:
            self.image = image

    def take(self):
        with self.lock:
            image, self.image = self.image, None
        return image

    def clear(self):
        with self.lock:
            self.image = None


SPECIAL_KEYS = {
    Qt.Key.Key_Escape: "esc", Qt.Key.Key_Tab: "tab", Qt.Key.Key_Backtab: "tab",
    Qt.Key.Key_Backspace: "backspace", Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Insert: "insert", Qt.Key.Key_Delete: "delete", Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end", Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
    Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down", Qt.Key.Key_PageUp: "page_up",
    Qt.Key.Key_PageDown: "page_down", Qt.Key.Key_Shift: "shift", Qt.Key.Key_Control: "ctrl",
    Qt.Key.Key_Alt: "alt", Qt.Key.Key_Meta: "cmd", Qt.Key.Key_CapsLock: "caps_lock",
    Qt.Key.Key_Space: "space",
}
SPECIAL_KEYS.update({int(Qt.Key.Key_F1) + i: f"f{i + 1}" for i in range(12)})
BUTTONS = {Qt.MouseButton.LeftButton: "left", Qt.MouseButton.RightButton: "right",
           Qt.MouseButton.MiddleButton: "middle"}


class RemoteCanvas(QWidget):
    def __init__(self, viewer):
        super().__init__()
        self.viewer = viewer
        self.image = None
        self.input_enabled = False
        self.allowed = False
        self.pressed_keys = {}
        self.setMinimumSize(480, 270)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def image_rect(self):
        if self.image is None:
            return QRectF()
        scale = min(self.width() / self.image.width(), self.height() / self.image.height())
        w, h = self.image.width() * scale, self.image.height() * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#171714"))
        if self.image:
            painter.drawImage(self.image_rect(), self.image)
        else:
            painter.setPen(QColor("#d8d2c6"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "正在等待远程画面…")

    def release_input(self):
        self.viewer.input({"event": "release"})
        self.pressed_keys.clear()
        self.input_enabled = False
        self.unsetCursor()

    def mouseMoveEvent(self, event):
        if not self.input_enabled:
            return
        rect = self.image_rect()
        if not rect.isEmpty():
            point = event.position()
            self.viewer.input({"event": "move", "x": max(0., min(1., (point.x() - rect.x()) / rect.width())),
                               "y": max(0., min(1., (point.y() - rect.y()) / rect.height()))})

    def mousePressEvent(self, event):
        if not self.image_rect().contains(event.position()) or not self.allowed:
            return
        self.setFocus()
        self.input_enabled = True
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.mouseMoveEvent(event)
        if event.button() in BUTTONS:
            self.viewer.input({"event": "button", "button": BUTTONS[event.button()], "down": True})

    def mouseReleaseEvent(self, event):
        if self.input_enabled and event.button() in BUTTONS:
            self.mouseMoveEvent(event)
            self.viewer.input({"event": "button", "button": BUTTONS[event.button()], "down": False})

    def wheelEvent(self, event):
        if self.input_enabled:
            self.viewer.input({"event": "scroll", "dy": max(-10, min(10, round(event.angleDelta().y() / 120)))})
            event.accept()

    def keyPressEvent(self, event):
        mods = event.modifiers()
        if (mods & Qt.KeyboardModifier.ControlModifier and mods & Qt.KeyboardModifier.AltModifier
                and mods & Qt.KeyboardModifier.ShiftModifier):
            self.release_input()
            event.accept()
            return
        if not self.input_enabled or event.isAutoRepeat():
            return
        key = SPECIAL_KEYS.get(event.key())
        if key is None and Qt.Key.Key_A <= event.key() <= Qt.Key.Key_Z:
            key = chr(event.key()).lower()
        if key is None and len(event.text()) == 1 and event.text().isprintable():
            key = event.text()
        if key:
            self.pressed_keys[event.key()] = key
            self.viewer.input({"event": "key", "key": key, "down": True})
            event.accept()

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        key = self.pressed_keys.pop(event.key(), None)
        if key:
            self.viewer.input({"event": "key", "key": key, "down": False})
            event.accept()

    def focusOutEvent(self, event):
        self.release_input()
        super().focusOutEvent(event)


class RemoteWindow(QMainWindow):
    closed = Signal()

    def __init__(self, uri, config, network_mode=None, path_status=None):
        super().__init__()
        self.setWindowTitle("Brioche · 远程桌面")
        self.resize(1150, 760)
        self.bridge = Bridge(self)
        self._closed = False
        self._path_busy = False
        self.path_status = path_status
        self.network_mode = network_mode or address_mode(parse_uri(uri)[0])
        self.current_info = {}
        self.mailbox = FrameMailbox()
        self.viewer = Viewer(uri, config, self.mailbox.publish, self.bridge.status.emit,
                             self.bridge.info.emit, self.bridge.stats.emit)
        self.canvas = RemoteCanvas(self.viewer)
        self.status_text = QLabel("正在连接…")
        self.stats_text = QLabel("界面提交 — FPS · RTT — ms")
        self.stats_text.setWordWrap(True)
        self.stats_values = {}
        self.presented, self.since, self.presented_fps = 0, time.monotonic(), 0.0
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Brioche"))
        self.monitor_selector = QComboBox()
        self.monitor_selector.setMinimumWidth(225)
        self.monitor_selector.addItem("正在读取显示器…")
        self.monitor_selector.setEnabled(False)
        self.monitor_selector.activated.connect(self.switch_monitor)
        bar.addWidget(self.monitor_selector)
        self.network_label = QLabel({"lan": "局域网", "loopback": "本机回环", "tailscale": "Tailscale · 待检测"}[self.network_mode])
        bar.addWidget(self.network_label)
        bar.addStretch()
        release = QPushButton("释放输入")
        release.clicked.connect(self.canvas.release_input)
        full = QPushButton("全屏 / F11")
        full.clicked.connect(self.toggle_fullscreen)
        disconnect = QPushButton("断开")
        disconnect.clicked.connect(self.close)
        metrics = QPushButton("性能")
        metrics.setCheckable(True)
        metrics.toggled.connect(self.stats_text.setVisible)
        self.stats_text.hide()
        for button in (metrics, release, full, disconnect):
            bar.addWidget(button)
        layout.addLayout(bar)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.status_text)
        layout.addWidget(self.stats_text)
        self.setCentralWidget(root)
        self.bridge.status.connect(self.status_text.setText)
        self.bridge.info.connect(self.info)
        self.bridge.stats.connect(self.stats_values.update)
        self.bridge.network.connect(self.path_result)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(6)
        self.timer.timeout.connect(self.present)
        self.timer.start()
        self.shortcut = QShortcut(QKeySequence("F11"), self)
        self.shortcut.activated.connect(self.toggle_fullscreen)
        self.viewer.start()
        self.path_timer = QTimer(self)
        self.path_timer.setInterval(10000)
        self.path_timer.timeout.connect(self.refresh_path)
        if self.network_mode == "tailscale":
            self.path_timer.start()
            QTimer.singleShot(1500, self.refresh_path)

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def info(self, info):
        if info.get("disconnected"):
            self.canvas.allowed = False
            self.canvas.release_input()
            self.stats_text.setText("连接已结束；关闭此窗口后可重新连接。")
            self.timer.stop()
            self.path_timer.stop()
            self.monitor_selector.setEnabled(False)
        elif info.get("monitor_error"):
            self.monitor_selector.blockSignals(True)
            self.monitor_selector.setCurrentIndex(self.monitor_selector.findData(self.current_info.get("monitor")))
            self.monitor_selector.blockSignals(False)
            self.monitor_selector.setEnabled(self.monitor_selector.count() > 1)
            self.canvas.allowed = self.current_info.get("input", False)
            self.status_text.setText(info["message"])
        else:
            self.current_info = info
            self.canvas.release_input()
            if info["type"] == "stream":
                self.mailbox.clear()
                self.canvas.image = None
                self.canvas.update()
                self.stats_values.clear()
                self.presented, self.since = 0, time.monotonic()
            self.canvas.allowed = info.get("input", False)
            self.setWindowTitle(f"Brioche · 显示器 {info['monitor']} · {info['width']}×{info['height']}")
            self.monitor_selector.blockSignals(True)
            self.monitor_selector.clear()
            from .main_window import screen_label
            for monitor in info["monitors"]:
                self.monitor_selector.addItem(screen_label(monitor), monitor["id"])
            self.monitor_selector.setCurrentIndex(self.monitor_selector.findData(info["monitor"]))
            self.monitor_selector.blockSignals(False)
            self.monitor_selector.setEnabled(self.monitor_selector.count() > 1)
            if not self.canvas.allowed:
                self.status_text.setText("只读共享 · 被控端没有启用输入")
            else:
                self.status_text.setText("点击画面控制 · Ctrl+Alt+Shift 释放输入")

    def switch_monitor(self, index):
        selected = self.monitor_selector.itemData(index)
        if selected == self.current_info.get("monitor"):
            return
        self.canvas.release_input()
        if self.viewer.switch_monitor(selected):
            self.canvas.allowed = False
            self.mailbox.clear()
            self.canvas.image = None
            self.canvas.update()
            self.monitor_selector.setEnabled(False)
            self.status_text.setText(f"正在切换到显示器 {selected}…")

    def refresh_path(self):
        if self._closed or self._path_busy or self.viewer.finished.is_set():
            return
        self._path_busy = True
        def work():
            result = self.path_status() if self.path_status else tailscale_status(self.viewer.address[0])
            if not self._closed:
                try:
                    self.bridge.network.emit(result)
                except RuntimeError:
                    pass
        threading.Thread(target=work, name="brioche-path", daemon=True).start()

    def path_result(self, result):
        self._path_busy = False
        self.network_label.setText("Tailscale · " + (result["path"] if result["ready"] else "状态不可用"))
        self.network_label.setToolTip(result["message"])

    def present(self):
        image = self.mailbox.take()
        if image is not None:
            self.canvas.image = image
            self.canvas.update()
            self.presented += 1
        now = time.monotonic()
        if now - self.since >= 1:
            self.presented_fps = self.presented / (now - self.since)
            self.presented, self.since = 0, now
            s = self.stats_values
            self.stats_text.setText(
                f"界面提交 {self.presented_fps:.1f} FPS · 解码 {s.get('decoded_fps', '—')} FPS · "
                f"编码 {s.get('encoded_fps', '—')} FPS · RTT {s.get('rtt_ms', '—')} ms · "
                f"接收 {s.get('mbps', '—')} Mbps · 采集+编码 {s.get('capture_encode_ms', '—')} ms")

    def closeEvent(self, event):
        self._closed = True
        self.path_timer.stop()
        self.timer.stop()
        self.canvas.release_input()
        self.viewer.stop()
        self.viewer.join(2)
        self.closed.emit()
        super().closeEvent(event)


from .main_window import MainWindow


def run():
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Brioche")
    app.setOrganizationName("Brioche")
    assets_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    app.setWindowIcon(QIcon(str(assets_root / "assets/brioche.svg")))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    return app.exec()
