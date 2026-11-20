"""Real capture/codec/TLS/input on two virtual regions of an isolated X11 screen."""
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from brioche.protocol import StreamConfig
from brioche.security import access_uri
from brioche.session import Host, Viewer

MONITORS = [
    {"id": 1, "label": "Landscape", "left": 0, "top": 0, "width": 1280, "height": 720},
    {"id": 2, "label": "Portrait", "left": 1280, "top": 0, "width": 720, "height": 1280},
    {"id": 3, "label": "Unavailable", "left": 0, "top": 0, "width": 640, "height": 480},
]


class RegionCapture:
    def __init__(self, index, fps):
        import mss
        if index == 3:
            raise RuntimeError("removed display")
        self.monitor = {k: v for k, v in MONITORS[index - 1].items() if k in ("left", "top", "width", "height")}
        self.backend = "test-x11-regions"
        self.capture = mss.mss()

    def grab(self):
        import av
        shot = self.capture.grab(self.monitor)
        frame = av.VideoFrame(shot.width, shot.height, "bgra")
        frame.planes[0].update(shot.bgra)
        return frame

    def close(self):
        self.capture.close()


def until(condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(.01)
    return False


@unittest.skipUnless(os.environ.get("BRIOCHE_DESKTOP_TESTS") == "1", "requires isolated desktop")
class MultiScreenTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.topology = patch("brioche.capture.list_monitors", return_value=MONITORS)
        self.backend = patch("brioche.media.DesktopCapture", RegionCapture)
        self.topology.start()
        self.backend.start()
        self.host = Host(port=0, folder=self.folder.name)
        self.host.start()
        self.assertTrue(self.host.ready.wait(8))
        self.info, self.frames, self.status = [], [], []
        self.viewer = Viewer(access_uri("127.0.0.1", self.host.port, self.host.pin, self.host.token),
                             StreamConfig("720p", 30, 4),
                             lambda f: self.frames.append((f.width, f.height)),
                             self.status.append, self.info.append)
        self.viewer.start()
        self.assertTrue(until(lambda: len(self.frames) >= 3), self.status)

    def tearDown(self):
        self.viewer.stop()
        self.viewer.join()
        self.host.stop()
        self.host.join()
        self.assertFalse(self.host._thread.is_alive())
        self.topology.stop()
        self.backend.stop()
        self.folder.cleanup()

    def test_switch_portrait_back_and_preserve_identity(self):
        self.assertEqual(self.info[0]["monitor"], 1)
        token, pin = self.host.token, self.host.pin
        for index, shape, epoch in ((2, (720, 1280), 1), (1, (1280, 720), 2)):
            time.sleep(.35)
            self.assertTrue(self.viewer.switch_monitor(index))
            self.assertTrue(until(lambda: any(i.get("stream_id") == epoch for i in self.info)), self.status)
            self.assertTrue(until(lambda: self.frames[-1] == shape), self.frames[-3:])
        self.assertEqual((self.host.token, self.host.pin), (token, pin))
        self.assertFalse(self.viewer.finished.is_set())

    def test_unavailable_display_recovers_current_video(self):
        count = len(self.frames)
        self.assertFalse(self.viewer.switch_monitor(99))
        self.assertTrue(self.viewer.switch_monitor(3))
        self.assertTrue(until(lambda: any(i.get("monitor_error") for i in self.info)), self.status)
        self.assertTrue(until(lambda: len(self.frames) > count + 2))
        self.assertFalse(self.viewer._switching.is_set())
        self.assertEqual(self.frames[-1], (1280, 720))

    def test_switch_releases_keys_and_rejects_stale_coordinates(self):
        from Xlib import XK
        from Xlib.display import Display
        d = Display()
        code = d.keysym_to_keycode(XK.string_to_keysym("Shift_L"))
        try:
            self.viewer.input({"event": "key", "key": "shift", "down": True})
            self.assertTrue(until(lambda: d.query_keymap()[code // 8] & (1 << (code % 8))))
            self.assertTrue(self.viewer.switch_monitor(2))
            self.assertTrue(until(lambda: self.viewer._stream_id == 1))
            self.assertTrue(until(lambda: not d.query_keymap()[code // 8] & (1 << (code % 8))))
            self.viewer.input({"event": "move", "x": .75, "y": .25})
            expected = (1280 + round(.75 * 719), round(.25 * 1279))
            def positioned():
                point = d.screen().root.query_pointer()
                return (point.root_x, point.root_y) == expected
            self.assertTrue(until(positioned))
            self.viewer.commands.put_nowait({"type": "input", "event": "move", "x": 0., "y": 0., "stream_id": 0})
            time.sleep(.15)
            self.assertTrue(positioned(), "Old-screen input was applied after switching")
        finally:
            self.viewer.input({"event": "release"})
            d.close()
