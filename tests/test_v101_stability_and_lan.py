"""Regression tests for v1.0.1 connection stability fixes & explicit LAN-only controls."""

import asyncio
import queue
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from brioche.device_model import _pick_channel, build_unified_devices
from brioche.device_store import DeviceStore
from brioche.embedded_network import EmbeddedNetwork
from brioche.hardware_media import HardwareEncoder
from brioche.protocol import ProtocolError, StreamConfig, pack_control, send
from brioche.security import access_uri
from brioche.session import Host, Viewer, is_retryable_disconnect, tune_socket


class LanOnlyChannelTests(unittest.TestCase):
    def test_lan_only_never_falls_back_to_tailscale_when_lan_offline(self):
        channels = {
            "lan": {"ip": "192.168.1.50", "port": 47995, "online": False, "connectable": False, "status_text": "离线"},
            "tailscale": {"ip": "100.64.0.9", "online": True, "reachable": True, "connectable": True, "sharing": True, "path": "DERP 中继"},
        }
        mode, ch = _pick_channel(channels, "lan_only")
        self.assertEqual(mode, "lan")
        self.assertEqual(ch["ip"], "192.168.1.50")

        with tempfile.TemporaryDirectory() as tmp:
            store = DeviceStore(Path(tmp))
            store.set_device_profile("ts:100.64.0.9", preferred_channel="lan_only")
            unified = build_unified_devices(
                [{"ip": "100.64.0.9", "name": "StudioPC", "reachable": True, "online": True, "connectable": True, "sharing": True, "path": "DERP 中继"}],
                [{"ip": "192.168.1.50", "port": 47995, "name": "StudioPC", "online": False, "connectable": False, "status_text": "连接超时"}],
                store,
            )
            self.assertEqual(len(unified), 1)
            dev = unified[0]
            self.assertEqual(dev["active_mode"], "lan")
            self.assertFalse(dev["connectable"])
            self.assertFalse(dev["online"])
            self.assertIn("禁用外网回退", dev["status_text"])

    def test_lan_only_unbound_never_routes_to_tailscale(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeviceStore(Path(tmp))
            store.set_device_profile("ts:100.64.0.10", preferred_channel="lan_only")
            unified = build_unified_devices(
                [{"ip": "100.64.0.10", "name": "WorkMac", "reachable": True, "online": True, "connectable": True, "sharing": True}],
                [],
                store,
            )
            dev = unified[0]
            self.assertIsNone(dev["active_mode"])
            self.assertFalse(dev["connectable"])
            self.assertEqual(dev["badge_text"], "未绑定 LAN")

    def test_lan_only_online_shows_locked_badge(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DeviceStore(Path(tmp))
            store.set_device_profile("ts:100.64.0.11", preferred_channel="lan_only")
            unified = build_unified_devices(
                [{"ip": "100.64.0.11", "name": "LabBox", "reachable": True, "online": True, "connectable": True, "sharing": True}],
                [{"ip": "10.27.56.20", "port": 47995, "name": "LabBox", "online": True, "connectable": True, "token": "t" * 43}],
                store,
            )
            dev = unified[0]
            self.assertEqual(dev["active_mode"], "lan")
            self.assertTrue(dev["connectable"])
            self.assertEqual(dev["badge_text"], "LAN 锁定")


class ConnectionStabilityTests(unittest.TestCase):
    def test_retryable_classification_allows_transient_stream_errors(self):
        self.assertTrue(is_retryable_disconnect(ProtocolError("硬件视频流中断")))
        self.assertTrue(is_retryable_disconnect(ProtocolError("硬件编码取帧超时")))
        self.assertTrue(is_retryable_disconnect(TimeoutError("read timed out")))
        self.assertFalse(is_retryable_disconnect(ProtocolError("被控端已有活动连接")))
        self.assertFalse(is_retryable_disconnect(ProtocolError("设备凭据或协议版本无效")))
        self.assertFalse(is_retryable_disconnect(ValueError("设备证书不匹配")))

    def test_tune_socket_sets_nodelay_and_keepalive(self):
        s1, s2 = socket.socketpair()
        try:
            # Verify TCP socket options on an actual AF_INET loopback pair
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            cli = socket.create_connection(srv.getsockname(), timeout=2)
            acc, _ = srv.accept()
            try:
                fake_writer = SimpleNamespace(get_extra_info=lambda k: cli if k == "socket" else None)
                tune_socket(fake_writer)
                self.assertEqual(cli.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY), 1)
                self.assertNotEqual(cli.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 0)
            finally:
                cli.close()
                acc.close()
                srv.close()
        finally:
            s1.close()
            s2.close()

    def test_send_with_lock_serializes_concurrent_writers(self):
        async def run():
            lock = asyncio.Lock()
            order = []

            class FakeWriter:
                def write(self, data):
                    order.append(("w", len(data)))

                async def drain(self):
                    await asyncio.sleep(0.01)
                    order.append(("d", 0))

            w = FakeWriter()
            msg = pack_control({"type": "pong", "stamp": 1.0})
            await asyncio.gather(
                send(w, msg, lock=lock),
                send(w, msg, lock=lock),
                send(w, msg, lock=lock),
            )
            # Strict write-drain alternation proves zero concurrent drain() calls
            self.assertEqual([op for op, _ in order], ["w", "d", "w", "d", "w", "d"])

        asyncio.run(run())

    def test_hardware_encoder_tolerates_transient_empty_frame_without_disconnect(self):
        enc = HardwareEncoder.__new__(HardwareEncoder)
        enc.closed = threading.Event()
        enc.packets = queue.Queue(maxsize=1)
        enc.error = None
        enc._last_packet_at = time.monotonic()
        enc.process = SimpleNamespace(poll=lambda: None)

        packets, elapsed = enc.frame()
        self.assertEqual(packets, [])
        self.assertGreaterEqual(elapsed, 0.0)

        # If process has exited, raise cleanly
        enc.process = SimpleNamespace(poll=lambda: 1)
        with self.assertRaises(RuntimeError):
            enc.frame()

    def test_embedded_network_poll_timeout_does_not_kill_active_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            net = EmbeddedNetwork(folder=Path(tmp))
            terminated = []
            net._terminate = lambda: terminated.append(True)

            class FakeStdin:
                def write(self, _):
                    pass

                def flush(self):
                    pass

            net.process = SimpleNamespace(poll=lambda: None, stdin=FakeStdin())
            net._start = lambda: None
            # Empty responses queue simulates a slow devices poll
            with patch.object(net.responses, "get", side_effect=queue.Empty()):
                with self.assertRaises(RuntimeError):
                    net.request("devices")
            self.assertEqual(terminated, [], "Background devices poll timeout must NOT kill brioche-network!")

    def test_same_peer_reconnect_reclaims_active_session_seamlessly(self):
        class FakeMedia:
            codec_name = "h264"
            encoder_name = "libx264"
            width = 160
            height = 90
            monitor = {"id": 1, "left": 0, "top": 0, "width": 160, "height": 90}
            capture = SimpleNamespace(backend="fake")
            codec = SimpleNamespace(framerate=30)
            paced = False

            def __init__(self):
                import av

                self._enc = av.CodecContext.create("libx264", "w")
                self._enc.width, self._enc.height = self.width, self.height
                self._enc.pix_fmt = "yuv420p"
                self._enc.options = {"preset": "ultrafast", "tune": "zerolatency", "x264-params": "repeat-headers=1"}
                self._enc.open()
                self._pts = 0

            def frame(self, force_keyframe=False):
                import av
                from brioche.h264 import annexb_access_unit

                vf = av.VideoFrame(self.width, self.height, "yuv420p")
                vf.pts = self._pts
                self._pts += 1
                pkts = [annexb_access_unit(bytes(p), self._enc.extradata, p.is_keyframe) for p in self._enc.encode(vf)]
                return pkts, 1.0

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(bind="127.0.0.1", port=0, folder=Path(tmp), view_only=True, audio_enabled=False)
            with patch("brioche.media.create_encoder", lambda *a, **kw: FakeMedia()), patch(
                "brioche.capture.list_monitors",
                lambda: [{"id": 1, "name": "Mock", "left": 0, "top": 0, "width": 160, "height": 90, "primary": True}],
            ):
                host.start()
                self.assertTrue(host.ready.wait(5))
                uri = access_uri("127.0.0.1", host.port, host.pin, host.token)
                cfg = StreamConfig("720p", 30, 4)

                v1_connected = threading.Event()
                v1 = Viewer(
                    uri,
                    cfg,
                    on_frame=lambda f: None,
                    on_info=lambda info: v1_connected.set() if info.get("type") == "welcome" else None,
                    audio_enabled=False,
                )
                v1.start()
                self.assertTrue(v1_connected.wait(6), "initial viewer should connect")

                # Reconnecting viewer from the SAME peer with reconnect=True must seamlessly take over!
                v2_connected = threading.Event()
                v2_statuses = []
                v2 = Viewer(
                    uri,
                    cfg,
                    on_frame=lambda f: None,
                    on_status=v2_statuses.append,
                    on_info=lambda info: v2_connected.set() if info.get("type") == "welcome" else None,
                    audio_enabled=False,
                    reconnect=True,
                )
                v2.start()
                self.assertTrue(v2_connected.wait(6), f"reconnecting viewer should reclaim session, got {v2_statuses}")

                v2.stop()
                v1.stop()
                v2.join(4)
                v1.join(4)
                host.stop()
                host.join(4)


class ExplicitLanUiTests(unittest.TestCase):
    def test_direct_lan_dialog_and_drawer_controls(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from brioche.device_drawer import DeviceInspectorDrawer
        from brioche.ui_components import AddLanDeviceDialog

        app = QApplication.instance() or QApplication([])
        _ = app

        dlg = AddLanDeviceDialog(
            local_ips="10.27.56.170",
            nearby_devices=[{"ip": "10.27.56.12", "port": 47995, "name": "MacMini", "platform": "Darwin"}],
            connect_primary=True,
            target_name="MacMini",
        )
        self.assertTrue(dlg.connect_immediately)
        self.assertIn("MacMini", dlg.windowTitle())
        dlg.add_only_btn.click()
        self.assertFalse(dlg.connect_immediately)

        drawer = DeviceInspectorDrawer()
        combo_values = [drawer.preferred_channel.itemData(i) for i in range(drawer.preferred_channel.count())]
        self.assertIn("lan_only", combo_values)
        drawer.show_device(
            {
                "key": "ts:100.64.0.5",
                "name": "MacMini",
                "display_name": "MacMini",
                "platform": "Darwin",
                "channels": {
                    "lan": {"ip": "10.27.56.12", "port": 47995, "online": True, "connectable": True, "token": "t" * 43},
                    "tailscale": {"ip": "100.64.0.5", "reachable": True, "path": "DERP 中继"},
                },
                "active_mode": "lan",
                "active_channel": {"ip": "10.27.56.12", "port": 47995, "online": True, "connectable": True, "token": "t" * 43},
                "online": True,
                "connectable": True,
                "badge_text": "LAN 锁定",
                "badge_tone": "online",
                "profile": {"preferred_channel": "lan_only"},
            }
        )
        self.assertEqual(drawer.preferred_channel.currentData(), "lan_only")
        self.assertTrue(drawer.lan_direct_btn.isEnabled())


if __name__ == "__main__":
    unittest.main()
