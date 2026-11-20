"""Real TLS, capture, H.264 and desktop-input tests. Requires an Xorg desktop.

BRIOCHE_DESKTOP_TESTS=1 python -m unittest discover -s tests -v
All host listeners are loopback-only and shut down in tearDown.
"""
import asyncio
from dataclasses import asdict
import os
import secrets
import tempfile
import threading
import time
import unittest

from brioche.protocol import VERSION, StreamConfig, pack_control, read_message, send, VIDEO
from brioche.security import access_uri, client_context
from brioche.session import Host, Viewer


@unittest.skipUnless(os.environ.get("BRIOCHE_DESKTOP_TESTS") == "1", "requires explicit desktop test opt-in")
class DesktopIntegration(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.host = Host(port=0, folder=self.folder.name, lan_discovery=True)
        self.host.start()
        self.assertTrue(self.host.ready.wait(10))
        self.assertIsNone(self.host.error)
        self.viewers = []

    def tearDown(self):
        for viewer in self.viewers:
            viewer.stop()
            viewer.join()
        self.host.stop()
        self.host.join(10)
        self.assertFalse(self.host._thread.is_alive(), "Host did not shut down")
        self.folder.cleanup()

    def uri(self, pin=None, token=None):
        return access_uri("127.0.0.1", self.host.port, pin or self.host.pin, token or self.host.token)

    def viewer(self, uri=None, config=None):
        ready, frames, statuses, info = threading.Event(), [], [], []
        def frame(value):
            frames.append((value.width, value.height))
            if len(frames) >= 3:
                ready.set()
        viewer = Viewer(uri or self.uri(), config or StreamConfig("720p", 30, 4), frame, statuses.append, info.append)
        self.viewers.append(viewer)
        viewer.start()
        return viewer, ready, frames, statuses, info

    def test_auto_encoder_reports_actual_backend_and_delivers_h264(self):
        self.host.encoder = "auto"
        viewer, ready, frames, statuses, info = self.viewer(config=StreamConfig("720p", 60, 8))
        self.assertTrue(ready.wait(15), statuses)
        self.assertTrue(frames)
        actual = info[0]["encoder"]
        if actual == "libx264":
            self.assertTrue(info[0].get("encoder_fallback"))
        else:
            self.assertIn("NVENC", actual)
            self.assertTrue(info[0]["capture"].startswith("ffmpeg-"))

    def test_file_channel_transfers_while_real_video_continues(self):
        from pathlib import Path
        from brioche.file_transfer import TransferJob
        self.host.files_enabled=True
        destination=Path(self.folder.name)/'received';destination.mkdir()
        source=Path(self.folder.name)/'payload.bin';source.write_bytes(secrets.token_bytes(350000))
        self.host.on_transfer=lambda request:request.respond(str(destination))
        viewer,ready,frames,statuses,info=self.viewer()
        self.assertTrue(ready.wait(15),statuses)
        self.assertTrue(info[0]['files'])
        count=len(frames)
        job=TransferJob(self.uri(),'upload',[str(source)])
        job.start()
        try:
            self.assertTrue(job.finished.wait(10))
            self.assertEqual((destination/source.name).read_bytes(),source.read_bytes())
            self.assertGreater(len(frames),count+2)
            self.assertFalse(viewer.finished.is_set())
        finally:job.stop()

    def test_matching_is_released_after_native_viewer_disconnect(self):
        from unittest.mock import patch
        with patch('brioche.display_mode.DisplayLease') as lease:
            viewer, ready, _, statuses, _ = self.viewer(config=StreamConfig('720p', 30, 4, True))
            self.assertTrue(ready.wait(15), statuses)
            lease.return_value.apply.assert_called_once()
            viewer.stop(); viewer.join()
            deadline = time.monotonic() + 5
            while self.host.active and time.monotonic() < deadline:time.sleep(.01)
            lease.return_value.close.assert_called_once()
            self.assertFalse(self.host.session_gate.active)

    def test_read_only_sharing_rejects_source_mode_changes(self):
        from unittest.mock import patch
        self.host.view_only = True
        with patch('brioche.display_mode.DisplayLease') as lease:
            viewer, ready, frames, statuses, _ = self.viewer(config=StreamConfig('720p', 30, 4, True))
            viewer.join(5)
            self.assertFalse(ready.is_set())
            self.assertFalse(frames)
            lease.assert_not_called()
            self.assertTrue(any('仅查看' in status for status in statuses), statuses)

    def test_paired_lan_device_streams_then_revocation_rejects_reconnect(self):
        from brioche.lan import pair
        self.host.on_pair = lambda request: request.answer(True)
        grant = pair('127.0.0.1', self.host.port, self.host.pin, secrets.token_hex(16), 'controller')
        uri = access_uri('127.0.0.1', self.host.port, self.host.pin, grant['token'])
        viewer, ready, frames, statuses, _ = self.viewer(uri)
        self.assertTrue(ready.wait(15), statuses)
        viewer.stop(); viewer.join()
        deadline = time.monotonic() + 3
        while self.host.active and time.monotonic() < deadline:
            time.sleep(.02)
        self.host.pair_store.clear()
        rejected, ready, frames, statuses, _ = self.viewer(uri)
        rejected.join(5)
        self.assertFalse(ready.is_set())
        self.assertFalse(frames)
        self.assertTrue(any('凭据' in status for status in statuses), statuses)

    def test_lan_and_external_hosts_share_one_active_session(self):
        from brioche.session import SessionGate
        gate = SessionGate()
        self.host.session_gate = gate
        other = Host(port=0, folder=self.folder.name + '/external', session_gate=gate)
        other.start()
        self.assertTrue(other.ready.wait(5))
        try:
            viewer, ready, _, statuses, _ = self.viewer()
            self.assertTrue(ready.wait(15), statuses)
            uri = access_uri('127.0.0.1', other.port, other.pin, other.token)
            rejected, second_ready, frames, statuses, _ = self.viewer(uri)
            rejected.join(5)
            self.assertFalse(second_ready.is_set())
            self.assertFalse(frames)
            self.assertTrue(any('连接' in status for status in statuses), statuses)
        finally:
            other.stop(); other.join(5)

    def test_real_capture_encode_tls_decode_and_mouse(self):
        viewer, ready, frames, statuses, info = self.viewer()
        self.assertTrue(ready.wait(15), statuses)
        self.assertTrue(info[0]["input"])
        self.assertLessEqual(frames[0][0], 1280)
        self.assertLessEqual(frames[0][1], 720)
        # Move the real pointer a small distance, verify through X11 and restore it.
        from Xlib.display import Display
        display = Display()
        try:
            old = display.screen().root.query_pointer()
            target_x, target_y = 0.31, 0.29
            viewer.input({"event": "move", "x": target_x, "y": target_y})
            deadline = time.monotonic() + 3
            welcome = info[0]
            expected_x = welcome["source_left"] + round(target_x * (welcome["source_width"] - 1))
            expected_y = welcome["source_top"] + round(target_y * (welcome["source_height"] - 1))
            try:
                while time.monotonic() < deadline:
                    point = display.screen().root.query_pointer()
                    if abs(point.root_x - expected_x) <= 1 and abs(point.root_y - expected_y) <= 1:
                        break
                    time.sleep(.02)
                self.assertLessEqual(abs(point.root_x - expected_x), 1)
                self.assertLessEqual(abs(point.root_y - expected_y), 1)
            finally:
                display.screen().root.warp_pointer(old.root_x, old.root_y)
                display.sync()
        finally:
            display.close()

    def test_1440p_144_request_negotiates_actual_size(self):
        viewer, ready, frames, statuses, info = self.viewer(config=StreamConfig("1440p", 144, 20))
        self.assertTrue(ready.wait(20), statuses)
        self.assertEqual(info[0]["fps_limit"], 144)
        self.assertLessEqual(frames[0][0], 2560)
        self.assertLessEqual(frames[0][1], 1440)

    def test_real_keyboard_and_disconnect_releases_pressed_key(self):
        from Xlib import X, XK
        from Xlib.display import Display
        viewer, ready, _, statuses, _ = self.viewer()
        self.assertTrue(ready.wait(15), statuses)
        display = Display()
        root = display.screen().root
        previous_focus = display.get_input_focus().focus
        window = root.create_window(0, 0, 100, 40, 0, display.screen().root_depth,
                                    X.InputOutput, X.CopyFromParent, override_redirect=True,
                                    event_mask=X.KeyPressMask | X.KeyReleaseMask)
        try:
            window.map()
            window.set_input_focus(X.RevertToParent, X.CurrentTime)
            display.sync()
            self.assertEqual(display.get_input_focus().focus.id, window.id)
            viewer.input({"event": "key", "key": "b", "down": True})
            code = display.keysym_to_keycode(XK.string_to_keysym("b"))
            deadline, received = time.monotonic() + 3, False
            while time.monotonic() < deadline:
                if display.pending_events():
                    event = display.next_event()
                    if event.type == X.KeyPress and event.detail == code:
                        received = True
                        break
                time.sleep(.01)
            self.assertTrue(received, "No injected key reached the disposable X11 window")
            viewer.stop()
            viewer.join()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                keys = display.query_keymap()
                if not (keys[code // 8] & (1 << (code % 8))):
                    break
                time.sleep(.01)
            self.assertFalse(keys[code // 8] & (1 << (code % 8)), "Disconnected key remained pressed")
        finally:
            # Extra cleanup even on assertion failure.
            from Xlib.ext import xtest
            xtest.fake_input(display, X.KeyRelease, display.keysym_to_keycode(XK.string_to_keysym("b")))
            display.set_input_focus(previous_focus, X.RevertToParent, X.CurrentTime)
            window.destroy()
            display.sync()
            display.close()

    def test_wrong_pin_and_token_rejected(self):
        for uri in (self.uri(pin="0" * 64), self.uri(token=secrets.token_urlsafe(32))):
            viewer, ready, frames, statuses, info = self.viewer(uri)
            self.assertTrue(viewer.finished.wait(8), statuses)
            self.assertEqual(frames, [])
            self.assertTrue(any("连接失败" in value for value in statuses))

    def test_active_session_exclusive_and_kick(self):
        viewer, ready, frames, statuses, info = self.viewer()
        self.assertTrue(ready.wait(15), statuses)
        other, _, other_frames, other_status, _ = self.viewer()
        self.assertTrue(other.finished.wait(8))
        self.assertEqual(other_frames, [])
        self.assertTrue(any("已有活动连接" in value for value in other_status), other_status)
        self.host.disconnect_viewer()
        self.assertTrue(viewer.finished.wait(8))
        replacement, ready2, _, status2, _ = self.viewer()
        self.assertTrue(ready2.wait(15), status2)

    def test_malformed_peer_does_not_kill_listener(self):
        async def malformed():
            reader, writer = await asyncio.open_connection("127.0.0.1", self.host.port,
                                                          ssl=client_context(), server_hostname="brioche")
            writer.write(b"\x01\xff\xff\xff\xff")
            await writer.drain()
            await asyncio.wait_for(reader.read(1024), 4)
            writer.close()
            await writer.wait_closed()
        asyncio.run(malformed())
        viewer, ready, _, statuses, _ = self.viewer()
        self.assertTrue(ready.wait(15), statuses)


if __name__ == "__main__":
    unittest.main()
