import asyncio
import os
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtWidgets import QApplication

from brioche.clipboard_sync import (
    ClipboardGuard,
    build_clipboard_message,
    validate_clipboard_payload,
)
from brioche.platform_input import validate_event
from brioche.protocol import MAX_CLIPBOARD_BYTES, ProtocolError, StreamConfig
from brioche.security import access_uri
from brioche.session import Host, Viewer, is_retryable_disconnect


def until(predicate, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ClipboardAndInputUnitTests(unittest.TestCase):
    def test_clipboard_guard_prevents_echo_loop(self):
        guard = ClipboardGuard()
        self.assertTrue(guard.accept_remote("hello world"))
        # Remote echo should not bounce back to remote
        self.assertFalse(guard.should_send_local("hello world"))
        # Duplicate remote delivery is ignored
        self.assertFalse(guard.accept_remote("hello world"))
        # Explicit manual push overrides loop cache
        self.assertTrue(guard.should_send_local("hello world", force=True))
        # Genuine local change sends once
        self.assertTrue(guard.should_send_local("local edit"))
        self.assertFalse(guard.should_send_local("local edit"))

    def test_clipboard_payload_validation_and_size_bound(self):
        msg = build_clipboard_message("测试文本 123")
        self.assertIsNotNone(msg)
        text, _ = validate_clipboard_payload(msg)
        self.assertEqual(text, "测试文本 123")

        oversized = "a" * (MAX_CLIPBOARD_BYTES + 1)
        self.assertIsNone(build_clipboard_message(oversized))
        with self.assertRaises(ProtocolError):
            validate_clipboard_payload({"type": "clipboard", "text": oversized})
        with self.assertRaises(ProtocolError):
            validate_clipboard_payload({"type": "clipboard", "text": "abc", "sha256": "0" * 64})

    def test_input_validation_supports_horizontal_scroll_and_key_repeat(self):
        validate_event({"event": "scroll", "dx": -3, "dy": 0})
        validate_event({"event": "scroll", "dx": 2, "dy": -4})
        validate_event({"event": "scroll", "dy": 1})
        validate_event({"event": "key", "key": "backspace", "down": True, "repeat": True})

        for bad in (
            {"event": "scroll"},
            {"event": "scroll", "dx": 11, "dy": 0},
            {"event": "scroll", "dx": "1", "dy": 0},
            {"event": "key", "key": "a", "down": True, "repeat": "yes"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_event(bad)

    def test_retryable_disconnect_classification(self):
        self.assertTrue(is_retryable_disconnect(TimeoutError("timed out")))
        self.assertTrue(is_retryable_disconnect(ConnectionResetError("reset")))
        self.assertTrue(is_retryable_disconnect(asyncio.IncompleteReadError(b"", 5)))
        self.assertFalse(is_retryable_disconnect(ProtocolError("设备凭据或协议版本无效")))
        self.assertFalse(is_retryable_disconnect(ValueError("设备证书不匹配")))
        self.assertFalse(is_retryable_disconnect(RuntimeError("请在系统偏好设置允许屏幕录制权限")))


class ClipboardTLSIntegrationTests(unittest.TestCase):
    def test_bidirectional_clipboard_over_tls_and_readonly_block(self):
        try:
            from test_multiscreen import MONITORS, RegionCapture
        except ImportError:
            from tests.test_multiscreen import MONITORS, RegionCapture

        with tempfile.TemporaryDirectory() as folder:
            host_received = []
            viewer_received = []
            with (
                patch("brioche.session.platform.system", return_value="Linux"),
                patch("brioche.capture.list_monitors", return_value=MONITORS[:1]),
                patch("brioche.media.DesktopCapture", RegionCapture),
                patch("brioche.platform_input.InputSink") as sink_cls,
            ):
                sink_cls.return_value.monitor = MONITORS[0]
                host = Host(
                    port=0,
                    folder=folder,
                    view_only=False,
                    clipboard_enabled=True,
                    on_clipboard=host_received.append,
                )
                host.start()
                self.assertTrue(host.ready.wait(5))
                uri = access_uri("127.0.0.1", host.port, host.pin, host.token)
                infos = []
                viewer = Viewer(
                    uri,
                    StreamConfig("720p", 30, 6),
                    lambda _frame: None,
                    on_info=infos.append,
                    on_clipboard=viewer_received.append,
                )
                viewer.start()
                try:
                    self.assertTrue(until(lambda: any(i.get("type") == "welcome" for i in infos), 8))
                    welcome = next(i for i in infos if i.get("type") == "welcome")
                    self.assertTrue(welcome.get("clipboard"))
                    self.assertTrue(bool(welcome.get("platform")))

                    # Viewer -> Host
                    self.assertTrue(viewer.send_clipboard("from-viewer-中文"))
                    self.assertTrue(until(lambda: host_received == ["from-viewer-中文"], 5))

                    # Host -> Viewer
                    self.assertTrue(host.push_clipboard("from-host-text"))
                    self.assertTrue(until(lambda: viewer_received == ["from-host-text"], 5))

                    # Disabling clipboard blocks subsequent sync
                    host.clipboard_enabled = False
                    self.assertFalse(host.push_clipboard("blocked"))
                finally:
                    viewer.stop()
                    viewer.join(5)
                    host.stop()
                    host.join(5)


@unittest.skipUnless(
    os.environ.get("BRIOCHE_DESKTOP_TESTS") == "1", "requires isolated Qt desktop"
)
class Step3QtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_canvas_auto_repeat_swap_cmd_ctrl_and_horizontal_wheel(self):
        from brioche.remote_session import RemoteCanvas

        events = []

        class StubViewer:
            def input(self, event):
                events.append(event)

        canvas = RemoteCanvas(StubViewer())
        canvas.allowed = True
        canvas.input_enabled = True
        canvas.swap_cmd_ctrl = True

        # Pressing Ctrl emits cmd, auto-repeat emits repeat=True, release emits cmd down=False
        press = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Control, Qt.KeyboardModifier.NoModifier, "", False)
        repeat = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Control, Qt.KeyboardModifier.NoModifier, "", True)
        rel_repeat = QKeyEvent(QKeyEvent.Type.KeyRelease, Qt.Key.Key_Control, Qt.KeyboardModifier.NoModifier, "", True)
        rel = QKeyEvent(QKeyEvent.Type.KeyRelease, Qt.Key.Key_Control, Qt.KeyboardModifier.NoModifier, "", False)

        canvas.keyPressEvent(press)
        canvas.keyPressEvent(repeat)
        canvas.keyReleaseEvent(rel_repeat)
        canvas.keyReleaseEvent(rel)

        self.assertEqual(
            events,
            [
                {"event": "key", "key": "cmd", "down": True},
                {"event": "key", "key": "cmd", "down": True, "repeat": True},
                {"event": "key", "key": "cmd", "down": False},
            ],
        )

        # Horizontal + vertical wheel
        events.clear()
        wheel = QWheelEvent(
            QPointF(100, 100),
            QPointF(100, 100),
            QPoint(0, 0),
            QPoint(240, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        canvas.wheelEvent(wheel)
        self.assertEqual(events, [{"event": "scroll", "dx": 2, "dy": -1}])

    def test_reconnect_overlay_retries_retryable_and_stops_on_non_retryable(self):
        from brioche.remote_session import SessionWindow

        uri1 = access_uri("127.0.0.1", 40001, "a" * 64, secrets.token_urlsafe(32))
        uri2 = access_uri("127.0.0.1", 40002, "a" * 64, secrets.token_urlsafe(32))
        created_uris = []

        class FakeViewer:
            def __init__(self, uri, config, on_frame, on_status, on_info, on_stats, on_clipboard=None, **kwargs):
                created_uris.append(uri)
                self.address = ("127.0.0.1", 40001, "a" * 64, "tok")
                self.finished = type("E", (), {"is_set": lambda self: False})()

            def start(self):
                pass

            def stop(self):
                pass

            def join(self, timeout=2):
                pass

            def input(self, event):
                pass

        with patch("brioche.remote_session.Viewer", FakeViewer):
            win = SessionWindow()
            try:
                win.show()
                page = win.add_session(
                    uri1,
                    StreamConfig("1440p", 60, 16),
                    network_mode="tailscale",
                    display_name="主机 A",
                    session_key="ts:a",
                    reconnect_uri=lambda: uri2,
                    auto_reconnect=True,
                )
                # Retryable disconnect shows reconnect overlay and allows instant retry
                page.info({"disconnected": True, "retryable": True, "reason": "网络波动", "_gen": page._viewer_gen})
                self.assertFalse(page.reconnect_overlay.isHidden())
                self.assertTrue(page._reconnecting)

                page.retry_now()
                self.assertTrue(until(lambda: (self.app.processEvents() or True) and len(created_uris) == 2, 3))
                self.assertEqual(created_uris[-1], uri2)

                # Non-retryable disconnect (e.g. cert mismatch) must NOT schedule reconnect
                page.info({"disconnected": True, "retryable": False, "reason": "设备证书不匹配", "_gen": page._viewer_gen})
                self.assertTrue(page.reconnect_overlay.isHidden())
                self.assertFalse(page._reconnect_timer.isActive())
            finally:
                win.close()
                self.app.processEvents()
