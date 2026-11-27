"""Regression & verification tests for v1.1.0 3-phase high-efficiency refactor."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import av

from brioche.gpu_canvas import YuvFrame, gpu_opengl_allowed
from brioche.media import DesktopDecoder, DesktopEncoder, _frame_signature
from brioche.protocol import StreamConfig
from brioche.remote_session import FrameMailbox
from brioche.embedded_network import EmbeddedNetwork


class PhaseAGpuAndIdlePacingTests(unittest.TestCase):
    def test_yuv_frame_from_av_preserves_planar_yuv_without_bgra_reformat(self):
        vf = av.VideoFrame(16, 16, "yuv420p")
        yuv = YuvFrame.from_av_frame(vf, frame_id=7)
        self.assertEqual((yuv.width, yuv.height), (16, 16))
        self.assertEqual(yuv.chroma, "yuv420p")
        self.assertEqual(yuv.frame_id, 7)
        self.assertGreaterEqual(len(yuv.y), 16 * 16)
        self.assertGreaterEqual(len(yuv.u), 8 * 8)
        self.assertGreaterEqual(len(yuv.v), 8 * 8)

    def test_frame_mailbox_yuv_mode_and_event_driven_wake(self):
        wakes = []
        mailbox = FrameMailbox(prefer_yuv=True, on_ready=lambda: wakes.append(1))
        vf1 = av.VideoFrame(32, 16, "yuv420p")
        vf2 = av.VideoFrame(32, 16, "yuv444p")
        mailbox.publish(vf1)
        mailbox.publish(vf2)
        # Coalesced wake: only 1 signal queued until take() drains it
        self.assertEqual(len(wakes), 1)
        item = mailbox.take()
        self.assertIsInstance(item, YuvFrame)
        self.assertEqual(item.chroma, "yuv444p")
        # Next frame wakes again cleanly
        mailbox.publish(vf1)
        self.assertEqual(len(wakes), 2)

    def test_desktop_decoder_yuv_mode_skips_cpu_bgra_conversion(self):
        enc = av.CodecContext.create("libx264", "w")
        enc.width, enc.height = 64, 64
        enc.pix_fmt = "yuv420p"
        enc.options = {"preset": "ultrafast", "tune": "zerolatency", "x264-params": "repeat-headers=1"}
        enc.open()
        vf = av.VideoFrame(64, 64, "yuv420p")
        vf.pts = 0
        pkts = [bytes(p) for p in enc.encode(vf)]
        pkts += [bytes(p) for p in enc.encode(None)]
        self.assertTrue(pkts)

        dec = DesktopDecoder(codec="h264", output_format="yuv")
        out = []
        for pkt in pkts:
            out.extend(dec.decode(pkt))
        self.assertTrue(out)
        self.assertEqual(dec.convert_ms, 0.0)
        self.assertIn(out[0].format.name, ("yuv420p", "yuvj420p", "nv12"))

    def test_idle_frame_signature_detects_static_vs_dirty(self):
        f1 = av.VideoFrame(128, 64, "yuv420p")
        f2 = av.VideoFrame(128, 64, "yuv420p")
        f1.planes[0].update(bytes([32] * f1.planes[0].buffer_size))
        f2.planes[0].update(bytes([32] * f2.planes[0].buffer_size))
        self.assertEqual(_frame_signature(f1), _frame_signature(f2))
        f2.planes[0].update(bytes([200] * f2.planes[0].buffer_size))
        self.assertNotEqual(_frame_signature(f1), _frame_signature(f2))

    def test_gpu_opengl_disabled_in_offscreen_mode(self):
        old = os.environ.get("QT_QPA_PLATFORM")
        try:
            os.environ["QT_QPA_PLATFORM"] = "offscreen"
            self.assertFalse(gpu_opengl_allowed())
        finally:
            if old is None:
                os.environ.pop("QT_QPA_PLATFORM", None)
            else:
                os.environ["QT_QPA_PLATFORM"] = old


class PhaseCV2AndWanUdpTests(unittest.TestCase):
    def test_embedded_network_publish_includes_core_udp_port_and_open_udp_tunnel(self):
        with tempfile.TemporaryDirectory() as tmp:
            net = EmbeddedNetwork(folder=Path(tmp))
            calls = []

            def fake_request(op, **kwargs):
                calls.append((op, kwargs))
                if op == "udp_tunnel":
                    return {"port": 51234, "tunnel": "udp-1"}
                return {"ok": True}

            net.request = fake_request
            fake_host = SimpleNamespace(
                bind="127.0.0.1",
                port=47995,
                pin="a" * 64,
                token="t" * 43,
                session_gate=SimpleNamespace(active=False),
                stop_requested=SimpleNamespace(is_set=lambda: False),
                core_udp_port=50123,
            )
            net.publish(fake_host)
            self.assertEqual(calls[0][0], "share")
            self.assertEqual(calls[0][1]["share"]["udp_port"], 50123)

            local_port, tid = net.open_udp_tunnel("100.64.0.9")
            self.assertEqual(local_port, 51234)
            self.assertEqual(tid, "udp-1")


if __name__ == "__main__":
    unittest.main()
