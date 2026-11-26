"""Regression tests for v0.9.1 macOS monitor-switcher crash fix and NVENC quality tuning."""

import io
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from brioche.protocol import StreamConfig


class HardwareEncoderFlagTests(unittest.TestCase):
    def test_nvenc_uses_lanczos_vbr_p4_and_long_gop_without_single_frame_vbv(self):
        from brioche.hardware_media import HardwareEncoder

        captured_cmd = []

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                captured_cmd.extend(cmd)
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")

            def poll(self):
                return 0

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        def fake_read(self):
            self.ready.set()

        fake_monitors = [{"id": 1, "left": 0, "top": 0, "width": 3840, "height": 2160}]
        with (
            patch("brioche.hardware_media.list_monitors", return_value=fake_monitors),
            patch("brioche.hardware_media.subprocess.Popen", side_effect=FakeProc),
            patch.object(HardwareEncoder, "_read", fake_read),
            patch.object(sys, "platform", "linux"),
            patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":1"}),
        ):
            enc = HardwareEncoder(
                StreamConfig(resolution="1440p", fps=60, bitrate_mbps=16),
                monitor_index=1,
                path="/usr/bin/ffmpeg",
            )
            enc.close()

        self.assertIn("-vf", captured_cmd)
        vf_val = captured_cmd[captured_cmd.index("-vf") + 1]
        self.assertIn("lanczos", vf_val)
        self.assertNotIn("fast_bilinear", vf_val)
        self.assertEqual(captured_cmd[captured_cmd.index("-preset") + 1], "p4")
        self.assertEqual(captured_cmd[captured_cmd.index("-rc") + 1], "vbr")
        self.assertEqual(captured_cmd[captured_cmd.index("-g") + 1], "120")
        self.assertIn("-spatial-aq", captured_cmd)
        bufsize = int(captured_cmd[captured_cmd.index("-bufsize") + 1])
        self.assertGreaterEqual(bufsize, 2_000_000)

    def test_nvenc_skips_scale_when_native_resolution_matches(self):
        from brioche.hardware_media import HardwareEncoder

        captured_cmd = []

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                captured_cmd.extend(cmd)
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")

            def poll(self):
                return 0

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        def fake_read(self):
            self.ready.set()

        fake_monitors = [{"id": 1, "left": 0, "top": 0, "width": 2560, "height": 1440}]
        with (
            patch("brioche.hardware_media.list_monitors", return_value=fake_monitors),
            patch("brioche.hardware_media.subprocess.Popen", side_effect=FakeProc),
            patch.object(HardwareEncoder, "_read", fake_read),
            patch.object(sys, "platform", "linux"),
            patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":1"}),
        ):
            enc = HardwareEncoder(
                StreamConfig(resolution="1440p", fps=60, bitrate_mbps=16),
                monitor_index=1,
                path="/usr/bin/ffmpeg",
            )
            enc.close()

        vf_val = captured_cmd[captured_cmd.index("-vf") + 1]
        self.assertEqual(vf_val, "format=nv12")


@unittest.skipUnless(
    os.environ.get("BRIOCHE_RUN_QT_TESTS") == "1", "requires isolated Qt desktop"
)
class MonitorMenuButtonQtTests(unittest.TestCase):
    def test_monitor_menu_button_api_and_toolbar_usage(self):
        from PySide6.QtWidgets import QApplication, QComboBox
        from brioche.remote_overlays import FloatingPillToolbar, MonitorMenuButton

        app = QApplication.instance() or QApplication([])
        toolbar = FloatingPillToolbar("局域网")
        btn = toolbar.monitor_selector
        self.assertIsInstance(btn, MonitorMenuButton)
        self.assertNotIsInstance(btn, QComboBox)
        btn.clear()
        btn.addItem("显示器 1 · 3840×2160", 1)
        btn.addItem("显示器 2 · 2560×1440", 2)
        self.assertEqual(btn.count(), 2)
        self.assertEqual(btn.findData(2), 1)
        btn.setCurrentIndex(1)
        self.assertEqual(btn.currentIndex(), 1)
        self.assertEqual(btn.itemData(1), 2)
        self.assertFalse(btn.view().isVisible())
        toolbar.close()


if __name__ == "__main__":
    unittest.main()
