"""Immersive multi-tab remote desktop session window with floating pill toolbar & HUD."""

import platform
import sys
import threading
import time

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)
from .clipboard_sync import ClipboardGuard
from .network import address_mode, tailscale_status
from .remote_overlays import (
    DropUploadOverlay,
    FloatingPillHandle,
    FloatingPillToolbar,
    OVERLAY_STYLE,
    PerformanceHUD,
    ReconnectOverlay,
    SessionToast,
)
from .security import parse_uri
from .session import Viewer

RECONNECT_DELAYS = (1, 2, 4, 8, 12)

SESSION_WINDOW_STYLE = """
QMainWindow#sessionWindow {
    background: #12110F;
}
QFrame#sessionTabStrip {
    background: #1B1916;
    border-bottom: 1px solid #2E2B26;
}
QTabBar#sessionTabs {
    background: transparent;
    border: none;
}
QTabBar#sessionTabs::tab {
    background: #23201C;
    color: #A8A297;
    border: 1px solid #302D27;
    border-bottom: none;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    padding: 7px 14px;
    margin-right: 4px;
    margin-top: 4px;
    font-size: 12px;
    font-weight: 550;
}
QTabBar#sessionTabs::tab:hover {
    background: #2C2924;
    color: #F2EFE9;
}
QTabBar#sessionTabs::tab:selected {
    background: #12110F;
    color: #F2EFE9;
    border-color: #464038;
    font-weight: 650;
}
QTabBar#sessionTabs::close-button {
    subcontrol-position: right;
    margin-left: 6px;
}
"""


class Bridge(QObject):
    status = Signal(str)
    info = Signal(dict)
    stats = Signal(dict)
    network = Signal(dict)
    clipboard = Signal(str)
    reconnect_uri_ready = Signal(str, str)


class FrameMailbox:
    def __init__(self):
        self.lock = threading.Lock()
        self.image = None

    def publish(self, frame):
        if frame.format.name == "bgra" and sys.byteorder == "little":
            pixel_format = QImage.Format.Format_RGB32
        else:
            if frame.format.name != "rgb24":
                frame = frame.reformat(format="rgb24")
            pixel_format = QImage.Format.Format_RGB888
        plane = frame.planes[0]
        # A deep copy owns the pixels after PyAV releases the decoded frame.
        image = QImage(
            memoryview(plane),
            frame.width,
            frame.height,
            plane.line_size,
            pixel_format,
        ).copy()
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
    Qt.Key.Key_Escape: "esc",
    Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Backtab: "tab",
    Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Return: "enter",
    Qt.Key.Key_Enter: "enter",
    Qt.Key.Key_Insert: "insert",
    Qt.Key.Key_Delete: "delete",
    Qt.Key.Key_Home: "home",
    Qt.Key.Key_End: "end",
    Qt.Key.Key_Left: "left",
    Qt.Key.Key_Right: "right",
    Qt.Key.Key_Up: "up",
    Qt.Key.Key_Down: "down",
    Qt.Key.Key_PageUp: "page_up",
    Qt.Key.Key_PageDown: "page_down",
    Qt.Key.Key_Shift: "shift",
    Qt.Key.Key_Control: "ctrl",
    Qt.Key.Key_Alt: "alt",
    Qt.Key.Key_Meta: "cmd",
    Qt.Key.Key_CapsLock: "caps_lock",
    Qt.Key.Key_Space: "space",
}
SPECIAL_KEYS.update({int(Qt.Key.Key_F1) + i: f"f{i + 1}" for i in range(12)})
BUTTONS = {
    Qt.MouseButton.LeftButton: "left",
    Qt.MouseButton.RightButton: "right",
    Qt.MouseButton.MiddleButton: "middle",
}


class RemoteCanvas(QWidget):
    top_edge_hovered = Signal()

    def __init__(self, viewer):
        super().__init__()
        self.viewer = viewer
        self.image = None
        self.input_enabled = False
        self.allowed = False
        self.scale_mode = "fit"
        self.swap_cmd_ctrl = False
        self.pressed_keys = {}
        self.painted = 0
        self.paint_ms = 0.0
        self._painted_key = None
        self.message = "正在等待远程画面…"
        self.setMinimumSize(480, 270)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_scale_mode(self, mode):
        if mode in ("fit", "actual", "fill"):
            self.scale_mode = mode
            self.update()

    def image_rect(self):
        if self.image is None:
            return QRectF()
        iw, ih = self.image.width(), self.image.height()
        if self.scale_mode == "actual":
            w, h = float(iw), float(ih)
        elif self.scale_mode == "fill":
            scale = max(self.width() / iw, self.height() / ih)
            w, h = iw * scale, ih * scale
        else:
            scale = min(self.width() / iw, self.height() / ih)
            w, h = iw * scale, ih * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, event):
        started = time.perf_counter()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#12110F"))
        if self.image:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.scale_mode != "actual")
            painter.drawImage(self.image_rect(), self.image)
        else:
            painter.setPen(QColor("#D8D2C6"))
            painter.drawText(
                self.rect().adjusted(32, 32, -32, -32),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self.message,
            )
        painter.end()
        if self.image is not None and self.image.cacheKey() != self._painted_key:
            self._painted_key = self.image.cacheKey()
            self.painted += 1
            self.paint_ms += (time.perf_counter() - started) * 1000

    def release_input(self):
        self.viewer.input({"event": "release"})
        self.pressed_keys.clear()
        self.input_enabled = False
        self.unsetCursor()

    def send_combo(self, keys):
        if not self.allowed or not keys:
            return False
        for key in keys:
            self.viewer.input({"event": "key", "key": key, "down": True})
        for key in reversed(keys):
            self.viewer.input({"event": "key", "key": key, "down": False})
        return True

    def mouseMoveEvent(self, event):
        if event.position().y() <= 18:
            self.top_edge_hovered.emit()
        if not self.input_enabled:
            return
        rect = self.image_rect()
        if not rect.isEmpty():
            point = event.position()
            self.viewer.input(
                {
                    "event": "move",
                    "x": max(0.0, min(1.0, (point.x() - rect.x()) / rect.width())),
                    "y": max(0.0, min(1.0, (point.y() - rect.y()) / rect.height())),
                }
            )

    def mousePressEvent(self, event):
        if not self.image_rect().contains(event.position()) or not self.allowed:
            return
        self.setFocus()
        self.input_enabled = True
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.mouseMoveEvent(event)
        if event.button() in BUTTONS:
            self.viewer.input(
                {"event": "button", "button": BUTTONS[event.button()], "down": True}
            )

    def mouseReleaseEvent(self, event):
        if self.input_enabled and event.button() in BUTTONS:
            self.mouseMoveEvent(event)
            self.viewer.input(
                {"event": "button", "button": BUTTONS[event.button()], "down": False}
            )

    def wheelEvent(self, event):
        if self.input_enabled:
            delta = event.angleDelta()
            dx = max(-10, min(10, round(delta.x() / 120))) if delta.x() else 0
            dy = max(-10, min(10, round(delta.y() / 120))) if delta.y() else 0
            if dx or dy:
                self.viewer.input({"event": "scroll", "dx": dx, "dy": dy})
            event.accept()

    def _map_wire_key(self, qt_key, text):
        key = SPECIAL_KEYS.get(qt_key)
        if key is None and Qt.Key.Key_A <= qt_key <= Qt.Key.Key_Z:
            key = chr(qt_key).lower()
        if key is None and len(text) == 1 and text.isprintable():
            key = text
        if self.swap_cmd_ctrl:
            if key == "ctrl":
                key = "cmd"
            elif key == "cmd":
                key = "ctrl"
        return key

    def keyPressEvent(self, event):
        mods = event.modifiers()
        if (
            mods & Qt.KeyboardModifier.ControlModifier
            and mods & Qt.KeyboardModifier.AltModifier
            and mods & Qt.KeyboardModifier.ShiftModifier
        ):
            self.release_input()
            event.accept()
            return
        if not self.input_enabled:
            return
        key = self._map_wire_key(event.key(), event.text())
        if key:
            is_repeat = event.isAutoRepeat()
            self.pressed_keys[event.key()] = key
            payload = {"event": "key", "key": key, "down": True}
            if is_repeat:
                payload["repeat"] = True
            self.viewer.input(payload)
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


class RemoteSessionPage(QWidget):
    """Single immersive remote desktop session suitable for hosting inside a tab or window."""

    tab_meta_changed = Signal(dict)
    disconnect_requested = Signal()
    fullscreen_requested = Signal()
    detach_requested = Signal()
    transfer_started = Signal(str, str, object)
    transfer_updated = Signal(dict)

    def __init__(
        self,
        uri,
        config,
        network_mode=None,
        path_status=None,
        display_name="远程桌面",
        session_key=None,
        reconnect_uri=None,
        clipboard_mode="auto",
        auto_reconnect=True,
        audio_enabled=True,
        parent=None,
    ):
        super().__init__(parent)
        self.session_key = session_key or uri
        self.display_name = display_name or "远程桌面"
        self.bridge = Bridge(self)
        self._closed = False
        self._path_busy = False
        self._disconnected = False
        self._reconnecting = False
        self._reconnect_attempt = 0
        self._reconnect_countdown = 0
        self._reconnect_reason = ""
        self._reconnect_uri = reconnect_uri
        self._auto_reconnect = bool(auto_reconnect)
        self._audio_enabled = bool(audio_enabled)
        self._audio_player = None
        self._audio_muted = False
        self._audio_volume = 1.0
        self._config = config
        self._swap_initialized = False
        self.clipboard_mode = clipboard_mode if clipboard_mode in ("auto", "manual", "off") else "auto"
        self.clipboard_guard = ClipboardGuard()
        self._clipboard_supported = False
        self._viewer_gen = 0
        self.path_status = path_status
        self.network_mode = network_mode or address_mode(parse_uri(uri)[0])
        self.current_info = {}
        self._uri = uri
        self._transfer_dialog = None
        self.mailbox = FrameMailbox()
        self.viewer = self._create_viewer(uri)
        self.canvas = RemoteCanvas(self.viewer)
        self.stats_values = {}
        self.presented, self.since, self.presented_fps = 0, time.monotonic(), 0.0
        self._path_text = {
            "lan": "局域网",
            "loopback": "回环",
            "tailscale": "Tailscale",
        }[self.network_mode]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.canvas, 1)

        # Floating overlays layered directly over the 100% immersive canvas
        self.toolbar = FloatingPillToolbar(self._path_text, self)
        self.toolbar.set_clipboard_mode(self.clipboard_mode)
        self.handle = FloatingPillHandle(self)
        self.handle.hide()
        self.hud = PerformanceHUD(self)
        self.toast = SessionToast(self)
        self.reconnect_overlay = ReconnectOverlay(self)
        self.drop_overlay = DropUploadOverlay(self)
        self.setAcceptDrops(True)

        # Compatibility aliases
        self.monitor_selector = self.toolbar.monitor_selector
        self.file_button = self.toolbar.file_button

        # Auto-hide timer for floating pill when unpinned
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(3000)
        self._hide_timer.timeout.connect(self._collapse_toolbar)

        # Auto-reconnect countdown tick (1 Hz)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(1000)
        self._reconnect_timer.timeout.connect(self._tick_reconnect)

        # Wire overlay interactions
        self.canvas.top_edge_hovered.connect(self.reveal_toolbar)
        self.handle.hovered.connect(self.reveal_toolbar)
        self.handle.clicked.connect(self.reveal_toolbar)
        self.toolbar.monitor_selected.connect(self.switch_monitor)
        self.toolbar.scale_mode_changed.connect(self._change_scale_mode)
        self.toolbar.combo_requested.connect(self.send_shortcut_combo)
        self.toolbar.swap_cmd_ctrl_toggled.connect(self._on_swap_cmd_ctrl)
        self.toolbar.clipboard_mode_changed.connect(self._on_clipboard_mode_changed)
        self.toolbar.clipboard_push_requested.connect(self.push_local_clipboard)
        self.toolbar.audio_mute_toggled.connect(self._on_audio_mute_toggled)
        self.toolbar.audio_volume_changed.connect(self._on_audio_volume_changed)
        self.toolbar.release_requested.connect(self._release_input_with_toast)
        self.toolbar.files_requested.connect(self.open_files)
        self.toolbar.hud_toggled.connect(self._toggle_hud)
        self.hud.closed.connect(lambda: self.toolbar.metrics_btn.setChecked(False))
        self.toolbar.fullscreen_requested.connect(self.fullscreen_requested.emit)
        self.toolbar.detach_requested.connect(self.detach_requested.emit)
        self.toolbar.disconnect_requested.connect(self.disconnect_requested.emit)
        self.toolbar.pin_toggled.connect(self._on_pin_toggled)
        self.reconnect_overlay.retry_requested.connect(self.retry_now)
        self.reconnect_overlay.cancel_requested.connect(self.disconnect_requested.emit)

        # Wire viewer bridge
        self.bridge.status.connect(self.set_status)
        self.bridge.info.connect(self.info)
        self.bridge.stats.connect(self.stats_values.update)
        self.bridge.network.connect(self.path_result)
        self.bridge.clipboard.connect(self._apply_remote_clipboard)
        self.bridge.reconnect_uri_ready.connect(self._finish_reconnect)

        self._qt_clipboard = QApplication.clipboard()
        if self._qt_clipboard is not None:
            self._qt_clipboard.dataChanged.connect(self._on_local_clipboard_changed)

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(6)
        self.timer.timeout.connect(self.present)
        self.timer.start()

        self.path_timer = QTimer(self)
        self.path_timer.setInterval(10000)
        self.path_timer.timeout.connect(self.refresh_path)
        if self.network_mode == "tailscale":
            self.path_timer.start()
            QTimer.singleShot(1500, self.refresh_path)

        self.viewer.start()
        self._emit_tab_meta()

    def _create_viewer(self, uri):
        self._viewer_gen += 1
        gen = self._viewer_gen
        return Viewer(
            uri,
            self._config,
            self.mailbox.publish,
            lambda msg, g=gen: self.bridge.status.emit(msg) if g == self._viewer_gen else None,
            lambda info, g=gen: self.bridge.info.emit({**info, "_gen": g}),
            lambda stats, g=gen: self.bridge.stats.emit(stats) if g == self._viewer_gen else None,
            lambda text, g=gen: self.bridge.clipboard.emit(text) if g == self._viewer_gen else None,
            audio_enabled=self._audio_enabled,
            on_audio=lambda payload, g=gen: self._push_audio(payload) if g == self._viewer_gen else None,
        )

    def _push_audio(self, payload):
        player = self._audio_player
        if player is not None:
            player.push(payload)

    def _start_audio_player(self):
        if self._audio_player is not None or not self._audio_enabled:
            return bool(self._audio_player and self._audio_player.active)
        from .audio import AudioPlayer

        player = AudioPlayer()
        if player.start():
            player.set_muted(self._audio_muted)
            player.set_volume(self._audio_volume)
            self._audio_player = player
            return True
        return False

    def _stop_audio_player(self):
        player, self._audio_player = self._audio_player, None
        if player is not None:
            player.close()

    def _on_audio_mute_toggled(self, muted):
        self._audio_muted = bool(muted)
        if self._audio_player:
            self._audio_player.set_muted(self._audio_muted)
        self.toast.show_message("远端音频已静音" if self._audio_muted else "已取消静音")

    def _on_audio_volume_changed(self, volume):
        self._audio_volume = float(volume)
        if self._audio_player:
            self._audio_player.set_volume(self._audio_volume)
        self.toast.show_message(f"远端音量：{int(round(self._audio_volume * 100))}%")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_overlays()

    def _layout_overlays(self):
        self.toolbar.reposition()
        self.handle.move((self.width() - self.handle.width()) // 2, 4)
        self.hud.reposition()
        self.toast.reposition()
        self.reconnect_overlay.reposition()
        self.drop_overlay.reposition()

    @staticmethod
    def _extract_drop_paths(mime_data):
        if not mime_data or not mime_data.hasUrls():
            return []
        return [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile() and url.toLocalFile()]

    def dragEnterEvent(self, event):
        paths = self._extract_drop_paths(event.mimeData())
        if paths and self.current_info.get("files") and not self._disconnected:
            self.drop_overlay.show_count(len(paths))
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.drop_overlay.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self.drop_overlay.hide()
        paths = self._extract_drop_paths(event.mimeData())
        if paths and self.current_info.get("files") and not self._disconnected:
            event.acceptProposedAction()
            self.open_files_and_upload(paths)
        else:
            event.ignore()

    def reveal_toolbar(self):
        self.handle.hide()
        self.toolbar.show()
        self.toolbar.raise_()
        self.toolbar.reposition()
        if not self.toolbar.pin_btn.isChecked() and self.canvas.image is not None:
            self._hide_timer.start()

    def _collapse_toolbar(self):
        if (
            self.toolbar.pin_btn.isChecked()
            or self.toolbar.underMouse()
            or self.toolbar.monitor_selector.view().isVisible()
            or self.canvas.image is None
        ):
            return
        self.toolbar.hide()
        self.handle.show()
        self.handle.raise_()
        self.handle.move((self.width() - self.handle.width()) // 2, 4)

    def _on_pin_toggled(self, pinned):
        if pinned:
            self._hide_timer.stop()
            self.reveal_toolbar()
        elif self.canvas.image is not None:
            self._hide_timer.start()

    def _toggle_hud(self, visible):
        if visible:
            self.hud.show()
            self.hud.raise_()
            self.hud.reposition()
        else:
            self.hud.hide()

    def _change_scale_mode(self, mode):
        self.canvas.set_scale_mode(mode)
        names = {"fit": "适应窗口", "actual": "原始像素 1:1", "fill": "铺满画布"}
        self.toast.show_message(f"画面模式：{names.get(mode, mode)}")

    def send_shortcut_combo(self, keys):
        if self.canvas.send_combo(keys):
            pretty = " + ".join(k.upper() if len(k) == 1 else k.capitalize() for k in keys)
            self.toast.show_message(f"已发送组合键：{pretty}")
        else:
            self.toast.show_message("当前会话为只读模式，无法发送按键")

    def _on_swap_cmd_ctrl(self, enabled):
        self.canvas.release_input()
        self.canvas.swap_cmd_ctrl = bool(enabled)
        self.toast.show_message(
            "已开启 Mac ↔ Win 修饰键互换 (Cmd ↔ Ctrl)"
            if enabled
            else "已关闭 Mac ↔ Win 修饰键互换"
        )

    def _on_clipboard_mode_changed(self, mode):
        self.clipboard_mode = mode
        names = {"auto": "双向自动同步", "manual": "仅手动推送", "off": "已禁用"}
        self.toast.show_message(f"剪贴板同步：{names.get(mode, mode)}")

    def _on_local_clipboard_changed(self):
        if (
            self._closed
            or self._disconnected
            or not self._clipboard_supported
            or self.clipboard_mode != "auto"
            or self._qt_clipboard is None
        ):
            return
        text = self._qt_clipboard.text()
        if self.clipboard_guard.should_send_local(text):
            self.viewer.send_clipboard(text)

    def push_local_clipboard(self):
        if not self._clipboard_supported or self.clipboard_mode == "off" or self._qt_clipboard is None:
            self.toast.show_message("剪贴板同步未启用")
            return
        text = self._qt_clipboard.text()
        if not text:
            self.toast.show_message("本机剪贴板为空（当前支持文本同步）")
            return
        if self.clipboard_guard.should_send_local(text, force=True):
            if self.viewer.send_clipboard(text):
                self.toast.show_message("已发送本机剪贴板到远端")
            else:
                self.toast.show_message("剪贴板发送失败或文本超过 128 KiB")

    def _apply_remote_clipboard(self, text):
        if (
            self._closed
            or not self._clipboard_supported
            or self.clipboard_mode == "off"
            or self._qt_clipboard is None
        ):
            return
        if self.clipboard_guard.accept_remote(text):
            self._qt_clipboard.setText(text)
            if self.clipboard_mode == "auto":
                self.toast.show_message("已同步远端剪贴板", duration_ms=1800)

    def _schedule_reconnect(self, reason=""):
        self._reconnecting = True
        self._reconnect_reason = reason or "网络连接中断"
        if self._reconnect_attempt < len(RECONNECT_DELAYS):
            delay = RECONNECT_DELAYS[self._reconnect_attempt]
            self._reconnect_attempt += 1
            self._reconnect_countdown = delay
            self.reconnect_overlay.show_state(
                "连接已中断 · 自动重连中",
                f"{self._reconnect_reason}\n第 {self._reconnect_attempt}/{len(RECONNECT_DELAYS)} 次重连将在 {delay} 秒后开始…",
                retry_enabled=True,
            )
            self._reconnect_timer.start()
        else:
            self._reconnect_timer.stop()
            self.reconnect_overlay.show_state(
                "自动重连已暂停",
                f"{self._reconnect_reason}\n已连续尝试 {len(RECONNECT_DELAYS)} 次未成功，可点击“立即重试”。",
                retry_enabled=True,
            )
        self._emit_tab_meta()

    def _tick_reconnect(self):
        if self._closed or not self._reconnecting:
            self._reconnect_timer.stop()
            return
        self._reconnect_countdown -= 1
        if self._reconnect_countdown <= 0:
            self._reconnect_timer.stop()
            self._begin_reconnect()
        else:
            self.reconnect_overlay.show_state(
                "连接已中断 · 自动重连中",
                f"{self._reconnect_reason}\n第 {self._reconnect_attempt}/{len(RECONNECT_DELAYS)} 次重连将在 {self._reconnect_countdown} 秒后开始…",
                retry_enabled=True,
            )

    def retry_now(self):
        if self._closed:
            return
        self._reconnect_timer.stop()
        self._reconnecting = True
        if self._reconnect_attempt >= len(RECONNECT_DELAYS):
            self._reconnect_attempt = 1
        self._begin_reconnect()

    def _begin_reconnect(self):
        if self._closed:
            return
        self.reconnect_overlay.show_state(
            "正在重新连接…",
            f"正在尝试重新建立加密会话（第 {max(1, self._reconnect_attempt)} 次）…",
            retry_enabled=False,
        )
        if self._reconnect_uri is None:
            self._finish_reconnect(self._uri, "")
            return

        def resolve():
            try:
                uri = self._reconnect_uri()
                if not self._closed:
                    self.bridge.reconnect_uri_ready.emit(uri, "")
            except Exception as exc:
                if not self._closed:
                    self.bridge.reconnect_uri_ready.emit("", str(exc)[:180])

        threading.Thread(target=resolve, name="brioche-reconnect", daemon=True).start()

    def _finish_reconnect(self, uri, error):
        if self._closed:
            return
        if error or not uri:
            self._schedule_reconnect(error or "无法解析对方地址")
            return
        self._uri = uri
        self._disconnected = False
        self.mailbox.clear()
        old = self.viewer
        old.stop()
        self.viewer = self._create_viewer(uri)
        self.canvas.viewer = self.viewer
        self.timer.start()
        if self.network_mode == "tailscale":
            self.path_timer.start()
        self.viewer.start()

    def _release_input_with_toast(self):
        self.canvas.release_input()
        self.toast.show_message("已释放鼠标键盘输入")

    def set_status(self, message):
        self.canvas.message = message
        if self.canvas.image is None:
            self.canvas.update()
        else:
            self.toast.show_message(message)

    def _emit_tab_meta(self):
        rtt = self.stats_values.get("rtt_ms")
        state = (
            "reconnecting"
            if self._reconnecting
            else ("offline" if self._disconnected else ("online" if self.canvas.image is not None else "connecting"))
        )
        title = self.display_name
        if self._reconnecting:
            title = f"{self.display_name} · 重连中"
        elif rtt is not None and not self._disconnected:
            title = f"{self.display_name} · {rtt:.0f}ms"
        self.tab_meta_changed.emit(
            {
                "title": title,
                "state": state,
                "path": self._path_text,
                "monitor": self.current_info.get("monitor"),
                "width": self.current_info.get("width"),
                "height": self.current_info.get("height"),
            }
        )

    def info(self, info):
        gen = info.get("_gen")
        if gen is not None and gen != self._viewer_gen:
            return
        if info.get("disconnected"):
            self._disconnected = True
            self._clipboard_supported = False
            self._stop_audio_player()
            self.toolbar.set_audio_state(False)
            self.file_button.setEnabled(False)
            self.toolbar.clipboard_btn.setEnabled(False)
            self.canvas.allowed = False
            self.canvas.release_input()
            self.timer.stop()
            self.path_timer.stop()
            self._hide_timer.stop()
            self.reveal_toolbar()
            self.monitor_selector.setEnabled(False)
            if self._auto_reconnect and info.get("retryable") and not self._closed:
                self._schedule_reconnect(info.get("reason", ""))
                return
            if self.canvas.image is None:
                self.canvas.message = self.canvas.message or info.get("reason") or "连接已结束，请关闭此标签页后重试"
                self.canvas.update()
            self.reconnect_overlay.hide()
            self.toast.show_message(info.get("reason") or "连接已断开", duration_ms=0)
            self._emit_tab_meta()
        elif info.get("monitor_error"):
            self.monitor_selector.blockSignals(True)
            self.monitor_selector.setCurrentIndex(
                self.monitor_selector.findData(self.current_info.get("monitor"))
            )
            self.monitor_selector.blockSignals(False)
            self.monitor_selector.setEnabled(self.monitor_selector.count() > 1)
            self.canvas.allowed = self.current_info.get("input", False)
            self.toast.show_message(info["message"])
        else:
            was_reconnecting = self._reconnecting
            self._reconnecting = False
            self._reconnect_attempt = 0
            self._reconnect_timer.stop()
            self.reconnect_overlay.hide()
            self._disconnected = False
            self.current_info = info
            self.file_button.setEnabled(bool(info.get("files")))
            self.file_button.setToolTip(
                "通过独立连接发送或接收文件"
                if info.get("files")
                else "对方需允许文件互传，且不能是仅查看共享"
            )
            self._clipboard_supported = bool(info.get("clipboard"))
            self.toolbar.clipboard_btn.setEnabled(self._clipboard_supported)
            self.toolbar.clipboard_btn.setToolTip(
                "双向文本剪贴板同步"
                if self._clipboard_supported
                else "对方未允许剪贴板同步，或当前为只读模式"
            )
            if info.get("audio") and self._audio_enabled:
                playing = self._start_audio_player()
                self.toolbar.set_audio_state(playing, muted=self._audio_muted, volume=self._audio_volume)
            else:
                self._stop_audio_player()
                self.toolbar.set_audio_state(False)
            if not self._swap_initialized and info.get("platform"):
                self._swap_initialized = True
                cross_mac = (platform.system() == "Darwin") != (info.get("platform") == "Darwin")
                self.canvas.swap_cmd_ctrl = cross_mac
                self.toolbar.set_swap_cmd_ctrl(cross_mac)
            self.canvas.release_input()
            if info["type"] == "stream":
                self.mailbox.clear()
                self.canvas.image = None
                self.canvas.update()
                self.stats_values.clear()
                self.presented, self.since = 0, time.monotonic()
            self.canvas.allowed = info.get("input", False)
            self.monitor_selector.blockSignals(True)
            self.monitor_selector.clear()
            from .main_window import screen_label

            for monitor in info["monitors"]:
                self.monitor_selector.addItem(screen_label(monitor), monitor["id"])
            self.monitor_selector.setCurrentIndex(
                self.monitor_selector.findData(info["monitor"])
            )
            self.monitor_selector.blockSignals(False)
            self.monitor_selector.setEnabled(self.monitor_selector.count() > 1)
            self.toolbar.reposition()
            if was_reconnecting:
                self.toast.show_message("已重新连接远程桌面")
            elif not self.canvas.allowed:
                self.toast.show_message("只读共享 · 被控端未启用键鼠输入")
            else:
                self.toast.show_message("点击画面开始控制 · Ctrl+Alt+Shift 释放输入")
            self._emit_tab_meta()

    def switch_monitor(self, index):
        selected = self.monitor_selector.itemData(index)
        if selected == self.current_info.get("monitor"):
            return
        self.canvas.release_input()
        if self.viewer.switch_monitor(selected):
            self.canvas.allowed = False
            self.mailbox.clear()
            self.canvas.image = None
            self.canvas.message = f"正在切换到显示器 {selected}…"
            self.canvas.update()
            self.monitor_selector.setEnabled(False)
            self.toast.show_message(f"正在切换到显示器 {selected}…")

    def refresh_path(self):
        if self._closed or self._path_busy or self.viewer.finished.is_set():
            return
        self._path_busy = True

        def work():
            result = (
                self.path_status()
                if self.path_status
                else tailscale_status(self.viewer.address[0])
            )
            if not self._closed:
                try:
                    self.bridge.network.emit(result)
                except RuntimeError:
                    pass

        threading.Thread(target=work, name="brioche-path", daemon=True).start()

    def path_result(self, result):
        self._path_busy = False
        path = result["path"] if result["ready"] else "状态不可用"
        self._path_text = f"TS · {path}"
        self.toolbar.set_network_chip(self._path_text, relay="中继" in path)
        self.toolbar.network_chip.setToolTip(result.get("message", ""))
        self.toolbar.reposition()

    def present(self):
        image = self.mailbox.take()
        if image is not None:
            first_frame = self.canvas.image is None
            self.canvas.image = image
            self.canvas.update()
            self.presented += 1
            if first_frame:
                self._emit_tab_meta()
                if not self.toolbar.pin_btn.isChecked():
                    self._hide_timer.start()
        now = time.monotonic()
        if now - self.since >= 1:
            painted_fps = self.canvas.painted / (now - self.since)
            paint_ms = self.canvas.paint_ms / max(1, self.canvas.painted)
            self.canvas.painted = 0
            self.canvas.paint_ms = 0.0
            self.presented_fps = self.presented / (now - self.since)
            self.presented, self.since = 0, now
            self.hud.update_metrics(
                self.current_info, self.stats_values, painted_fps, paint_ms
            )
            self._emit_tab_meta()

    def open_files(self):
        from .file_manager_ui import FileManagerWindow

        if self._transfer_dialog is None:
            self._transfer_dialog = FileManagerWindow(
                self._uri, remote_name=self.display_name, parent=self
            )
            self._transfer_dialog.started.connect(self.transfer_started.emit)
            self._transfer_dialog.updated.connect(self.transfer_updated.emit)
        else:
            self._transfer_dialog.uri = self._uri
        self._transfer_dialog.show()
        self._transfer_dialog.raise_()
        return self._transfer_dialog

    def open_files_and_upload(self, paths):
        dialog = self.open_files()
        if dialog.upload_paths(paths):
            self.toast.show_message(f"已发起发送 {len(paths)} 个项目到远端文件夹")
        else:
            self.toast.show_message("已有文件正在传输，请等待当前任务完成")

    def shutdown_session(self):
        if self._closed:
            return
        self._closed = True
        self._reconnecting = False
        self._reconnect_timer.stop()
        self._stop_audio_player()
        if self._qt_clipboard is not None:
            try:
                self._qt_clipboard.dataChanged.disconnect(self._on_local_clipboard_changed)
            except (RuntimeError, TypeError):
                pass
        if self._transfer_dialog:
            self._transfer_dialog.stop_jobs()
            self._transfer_dialog.close()
        self._hide_timer.stop()
        self.path_timer.stop()
        self.timer.stop()
        self.canvas.release_input()
        self.viewer.stop()
        self.viewer.join(2)


class SessionWindow(QMainWindow):
    """Multi-tab remote desktop window supporting concurrent sessions and tab detachment."""

    closed = Signal()
    session_closed = Signal(str)
    detach_window_created = Signal(object)
    transfer_started = Signal(str, str, object)
    transfer_updated = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sessionWindow")
        self.setStyleSheet(SESSION_WINDOW_STYLE + OVERLAY_STYLE)
        self.setWindowTitle("Brioche · 远程桌面")
        self.resize(1200, 780)
        self._pages = []
        self._closing = False

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tab_strip = QFrame()
        self.tab_strip.setObjectName("sessionTabStrip")
        strip_row = QHBoxLayout(self.tab_strip)
        strip_row.setContentsMargins(10, 0, 10, 0)
        strip_row.setSpacing(6)

        self.tabs = QTabBar()
        self.tabs.setObjectName("sessionTabs")
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setExpanding(False)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.tabMoved.connect(self._on_tab_moved)
        self.tabs.customContextMenuRequested.connect(self._tab_context_menu)
        strip_row.addWidget(self.tabs, 1)

        self.stack = QStackedWidget()
        layout.addWidget(self.tab_strip)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)

        self.shortcut = QShortcut(QKeySequence("F11"), self)
        self.shortcut.activated.connect(self.toggle_fullscreen)

    def session_count(self):
        return len(self._pages)

    def current_session(self):
        idx = self.stack.currentIndex()
        return self._pages[idx] if 0 <= idx < len(self._pages) else None

    def find_session(self, session_key):
        for i, page in enumerate(self._pages):
            if page.session_key == session_key:
                return i, page
        return -1, None

    def add_session(
        self,
        uri,
        config,
        network_mode=None,
        path_status=None,
        display_name="远程桌面",
        session_key=None,
        reconnect_uri=None,
        clipboard_mode="auto",
        auto_reconnect=True,
        audio_enabled=True,
    ):
        existing_idx, existing = self.find_session(session_key) if session_key else (-1, None)
        if existing is not None:
            self.tabs.setCurrentIndex(existing_idx)
            self.raise_()
            self.activateWindow()
            return existing
        page = RemoteSessionPage(
            uri,
            config,
            network_mode=network_mode,
            path_status=path_status,
            display_name=display_name,
            session_key=session_key,
            reconnect_uri=reconnect_uri,
            clipboard_mode=clipboard_mode,
            auto_reconnect=auto_reconnect,
            audio_enabled=audio_enabled,
            parent=self.stack,
        )
        self.attach_page(page)
        return page

    def attach_page(self, page):
        page.setParent(self.stack)
        idx = self.stack.addWidget(page)
        self._pages.append(page)
        tab_idx = self.tabs.addTab(f"● {page.display_name}")
        page.tab_meta_changed.connect(lambda meta, p=page: self._update_tab_meta(p, meta))
        page.disconnect_requested.connect(lambda p=page: self._close_page(p))
        page.fullscreen_requested.connect(self.toggle_fullscreen)
        page.detach_requested.connect(lambda p=page: self.detach_page(p))
        page.transfer_started.connect(self.transfer_started.emit)
        page.transfer_updated.connect(self.transfer_updated.emit)
        self.tabs.setCurrentIndex(tab_idx)
        self.stack.setCurrentIndex(idx)
        self._sync_title()

    def _update_tab_meta(self, page, meta):
        if page not in self._pages:
            return
        idx = self._pages.index(page)
        dot = "●" if meta["state"] == "online" else ("○" if meta["state"] == "offline" else "◐")
        self.tabs.setTabText(idx, f"{dot} {meta['title']}")
        if idx == self.tabs.currentIndex():
            self._sync_title(meta)

    def _sync_title(self, meta=None):
        page = self.current_session()
        if not page:
            self.setWindowTitle("Brioche · 远程桌面")
            return
        info = page.current_info
        if info.get("monitor") and info.get("width"):
            self.setWindowTitle(
                f"Brioche · {page.display_name} · 显示器 {info['monitor']} · {info['width']}×{info['height']}"
            )
        else:
            self.setWindowTitle(f"Brioche · {page.display_name}")

    def _on_tab_changed(self, index):
        if 0 <= index < len(self._pages):
            for i, p in enumerate(self._pages):
                if i != index:
                    p.canvas.release_input()
            self.stack.setCurrentWidget(self._pages[index])
            self._pages[index]._layout_overlays()
            self._sync_title()

    def _on_tab_moved(self, from_idx, to_idx):
        page = self._pages.pop(from_idx)
        self._pages.insert(to_idx, page)

    def _tab_context_menu(self, pos):
        index = self.tabs.tabAt(pos)
        if index < 0 or index >= len(self._pages):
            return
        page = self._pages[index]
        menu = QMenu(self)
        detach = menu.addAction("拆分为独立窗口")
        detach.setEnabled(len(self._pages) > 1)
        detach.triggered.connect(lambda: self.detach_page(page))
        menu.addSeparator()
        close_act = menu.addAction("断开并关闭此标签")
        close_act.triggered.connect(lambda: self.close_tab(index))
        menu.exec(self.tabs.mapToGlobal(pos))

    def detach_page(self, page):
        if page not in self._pages or len(self._pages) <= 1:
            return None
        idx = self._pages.index(page)
        self._pages.pop(idx)
        self.tabs.removeTab(idx)
        self.stack.removeWidget(page)
        # Re-parent live session into a fresh independent SessionWindow without dropping the stream
        for sig in (
            page.tab_meta_changed,
            page.disconnect_requested,
            page.fullscreen_requested,
            page.detach_requested,
            page.transfer_started,
            page.transfer_updated,
        ):
            try:
                sig.disconnect()
            except RuntimeError:
                pass
        detached = SessionWindow()
        detached.attach_page(page)
        detached.show()
        self.detach_window_created.emit(detached)
        self._sync_title()
        return detached

    def _close_page(self, page):
        if page in self._pages:
            self.close_tab(self._pages.index(page))

    def close_tab(self, index):
        if not 0 <= index < len(self._pages):
            return
        page = self._pages.pop(index)
        key = page.session_key
        self.tabs.removeTab(index)
        self.stack.removeWidget(page)
        page.shutdown_session()
        page.deleteLater()
        self.session_closed.emit(key)
        if not self._pages and not self._closing:
            self.close()
        else:
            self._sync_title()

    def close_sessions_by_mode(self, network_mode):
        for page in list(self._pages):
            if page.network_mode == network_mode:
                self._close_page(page)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.tab_strip.show()
        else:
            self.tab_strip.hide()
            self.showFullScreen()

    def closeEvent(self, event):
        self._closing = True
        while self._pages:
            page = self._pages.pop(0)
            key = page.session_key
            self.stack.removeWidget(page)
            page.shutdown_session()
            page.deleteLater()
            self.session_closed.emit(key)
        self.closed.emit()
        super().closeEvent(event)


class RemoteWindow(SessionWindow):
    """Backward-compatible single-session constructor wrapping SessionWindow."""

    def __init__(self, uri, config, network_mode=None, path_status=None, display_name="远程桌面"):
        super().__init__()
        self.session = self.add_session(
            uri,
            config,
            network_mode=network_mode,
            path_status=path_status,
            display_name=display_name,
        )
        self.network_mode = self.session.network_mode
        self.viewer = self.session.viewer
        self.mailbox = self.session.mailbox
        self.canvas = self.session.canvas
        self.toolbar = self.session.toolbar
        self.hud = self.session.hud
        self.monitor_selector = self.session.monitor_selector
        self.file_button = self.session.file_button

    @property
    def current_info(self):
        return self.session.current_info

    @property
    def stats_values(self):
        return self.session.stats_values

    def switch_monitor(self, index):
        self.session.switch_monitor(index)

    def open_files(self):
        self.session.open_files()
