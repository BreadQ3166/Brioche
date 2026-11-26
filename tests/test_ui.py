import os
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from brioche.device_model import build_unified_devices, effective_stream_config, filter_devices
from brioche.device_store import DeviceStore
from brioche.main_window import MainWindow
from brioche.protocol import StreamConfig
from brioche.security import access_uri


class FakeNetwork:
    def status(self, *args, **kwargs):
        return {
            "ready": False,
            "ips": [],
            "peers": [],
            "message": "请登录",
            "state": "NeedsLogin",
        }

    def close(self):
        pass

    def clear(self, *args):
        pass


class DeviceModelTests(unittest.TestCase):
    def test_merges_same_device_across_lan_and_tailscale_and_prefers_lan(self):
        with tempfile.TemporaryDirectory() as folder:
            store = DeviceStore(folder)
            ts = [
                {
                    "ip": "100.70.1.3",
                    "name": "Workstation",
                    "reachable": True,
                    "online": True,
                    "sharing": True,
                    "connectable": True,
                    "path": "直连",
                    "status_text": "在线 · 双击连接",
                }
            ]
            lan = [
                {
                    "ip": "192.168.1.8",
                    "port": 47995,
                    "name": "workstation",
                    "platform": "Linux",
                    "online": True,
                    "connectable": True,
                    "token": secrets.token_urlsafe(32),
                    "status_text": "在线 · 双击连接",
                }
            ]
            unified = build_unified_devices(ts, lan, store)
            self.assertEqual(len(unified), 1)
            device = unified[0]
            self.assertIn("lan", device["channels"])
            self.assertIn("tailscale", device["channels"])
            self.assertEqual(device["active_mode"], "lan")
            self.assertIn("双通道", device["ip"])

            store.set_device_profile(device["key"], preferred_channel="tailscale")
            unified = build_unified_devices(ts, lan, store)
            self.assertEqual(unified[0]["active_mode"], "tailscale")

    def test_filtering_by_query_category_and_group(self):
        with tempfile.TemporaryDirectory() as folder:
            store = DeviceStore(folder)
            store.set_device_profile("ts:100.70.1.3", alias="主力机", group="办公室", favorite=True)
            ts = [
                {
                    "ip": "100.70.1.3",
                    "name": "Workstation",
                    "reachable": True,
                    "online": True,
                    "sharing": True,
                    "connectable": True,
                    "status_text": "在线",
                },
                {
                    "ip": "100.70.1.4",
                    "name": "HomePC",
                    "reachable": False,
                    "online": False,
                    "sharing": False,
                    "connectable": False,
                    "status_text": "离线",
                },
            ]
            devices = build_unified_devices(ts, [], store)
            self.assertEqual(len(filter_devices(devices, category="online")), 1)
            self.assertEqual(len(filter_devices(devices, category="favorite")), 1)
            self.assertEqual(len(filter_devices(devices, group="办公室")), 1)
            self.assertEqual(len(filter_devices(devices, query="主力")), 1)
            self.assertEqual(len(filter_devices(devices, query="100.70.1.4")), 1)

    def test_per_device_stream_config_overrides_global(self):
        base = StreamConfig("1440p", 60, 16, False)
        merged = effective_stream_config(
            base, {"resolution": "1080p", "fps": 120, "bitrate": 30, "match_display": True}
        )
        self.assertEqual(merged, StreamConfig("1080p", 120, 30, True))
        native = effective_stream_config(base, {"resolution": "native", "match_display": True})
        self.assertFalse(native.match_display)


@unittest.skipUnless(
    os.environ.get("BRIOCHE_DESKTOP_TESTS") == "1", "requires isolated Qt desktop"
)
class SimpleFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with (
            patch.object(MainWindow, "refresh_environment"),
            patch.object(MainWindow, "refresh_lan"),
        ):
            self.window = MainWindow(folder=self.temp.name, network=FakeNetwork())
        self.window.network_poll.stop()
        self.window.lan_poll.stop()
        self.environment = {
            "lan": [{"ip": "192.168.1.5", "name": "Ethernet"}],
            "tailscale": {
                "ready": True,
                "ips": ["100.70.1.2"],
                "peers": [],
                "message": "已连接",
            },
            "monitors": [
                {"id": 1, "width": 2560, "height": 1440},
                {"id": 2, "width": 1440, "height": 2560},
            ],
        }
        self.window.apply_environment(self.environment)

    def tearDown(self):
        self.window.shutdown()
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def test_home_has_unified_dashboard_and_default_settings(self):
        self.assertEqual(self.window.pages.currentIndex(), MainWindow.PAGE_DEVICES)
        self.assertEqual(self.window.device_list.count(), 0)
        self.assertTrue(self.window.drawer.isHidden())
        self.assertEqual(self.window.fps.currentData(), 60)
        self.assertFalse(self.window.external_allowed.isChecked())
        self.assertFalse(self.window.lan_allowed.isChecked())

    def test_resolution_and_source_matching_settings(self):
        self.window.navigate(MainWindow.PAGE_SETTINGS)
        self.window.settings_view.select_category(1)
        self.window.show()
        self.app.processEvents()
        self.assertTrue(self.window.resolution.isVisible())
        self.assertEqual(self.window.resolution.currentData(), "1440p")
        self.assertFalse(self.window.stream_config().match_display)
        self.window.resolution.setCurrentIndex(self.window.resolution.findData("1080p"))
        self.window.match_display.setChecked(True)
        self.assertEqual(
            self.window.stream_config(), StreamConfig("1080p", 60, 16, True)
        )
        self.window.resolution.setCurrentIndex(
            self.window.resolution.findData("native")
        )
        self.assertFalse(self.window.stream_config().match_display)
        self.assertFalse(self.window.match_display.isEnabled())

    def test_online_and_offline_devices_remain_in_directory_without_flicker(self):
        self.environment["tailscale"]["peers"] = [
            {
                "ip": "100.70.1.3",
                "name": "Laptop",
                "online": True,
                "reachable": True,
                "sharing": True,
            },
            {
                "ip": "100.70.1.4",
                "name": "Offline",
                "online": False,
                "reachable": False,
                "sharing": False,
            },
        ]
        self.window.apply_environment(self.environment)
        self.assertEqual(self.window.device_list.count(), 2)
        original = self.window.device_list.item(0)
        self.window.device_list.setCurrentItem(original)
        self.window.apply_environment(self.environment)
        self.assertIs(self.window.device_list.item(0), original)
        self.assertIs(self.window.device_list.currentItem(), original)
        self.assertIn("离线", self.window.device_list.item(1).text())

    def test_device_card_uses_live_probe_instead_of_cached_online_flag(self):
        for cached, actual in ((True, False), (False, True)):
            self.environment["tailscale"]["peers"] = [
                {
                    "ip": "100.70.1.3",
                    "name": "Laptop",
                    "online": cached,
                    "reachable": actual,
                    "sharing": True,
                }
            ]
            self.window.apply_environment(self.environment)
            data = self.window.device_list.item(0).data(Qt.ItemDataRole.UserRole)
            self.assertEqual(data["online"], actual)
            self.assertEqual(data["connectable"], actual)

    def test_click_opens_drawer_and_per_device_profile_applies_on_connect(self):
        self.environment["tailscale"]["peers"] = [
            {"ip": "100.70.1.3", "name": "Laptop", "reachable": True, "sharing": True}
        ]
        self.window.apply_environment(self.environment)
        item = self.window.device_list.item(0)
        self.window.device_list.itemClicked.emit(item)
        self.assertFalse(self.window.drawer.isHidden())

        # Override resolution and fps in right-side inspector drawer
        self.window.drawer.resolution.setCurrentIndex(
            self.window.drawer.resolution.findData("1080p")
        )
        self.window.drawer.fps.setCurrentIndex(self.window.drawer.fps.findData(90))
        self.window.drawer.alias_input.setText("主力本")
        self.window.drawer.group_input.setText("办公室")
        self.window.drawer._save_profile()
        self.app.processEvents()

        updated = self.window.device_list.item(0).data(Qt.ItemDataRole.UserRole)
        self.assertEqual(updated["display_name"], "主力本")
        self.assertEqual(updated["group"], "办公室")

        uri = access_uri("127.0.0.1", 32123, "a" * 64, secrets.token_urlsafe(32))
        with (
            patch.object(
                self.window.network, "open_peer", return_value=uri, create=True
            ) as opened,
            patch("brioche.ui.SessionWindow") as session_win_cls,
        ):
            session_win = session_win_cls.return_value
            session_win.find_session.return_value = (-1, None)
            self.window.device_list.itemDoubleClicked.emit(self.window.device_list.item(0))
            end = time.monotonic() + 3
            while not session_win.add_session.called and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(0.01)
            opened.assert_called_once_with("100.70.1.3")
            self.assertEqual(session_win.add_session.call_args.args[0], uri)
            self.assertEqual(
                session_win.add_session.call_args.args[1], StreamConfig("1080p", 90, 16, False)
            )
            self.assertEqual(session_win.add_session.call_args.kwargs["network_mode"], "tailscale")
            self.assertEqual(len(self.window.store.recent_connections()), 1)
            self.window.session_window = None

    def test_multi_tab_session_window_overlays_scale_and_combos(self):
        from PySide6.QtGui import QImage
        from brioche.remote_session import SessionWindow

        uri1 = access_uri("127.0.0.1", 40001, "a" * 64, secrets.token_urlsafe(32))
        uri2 = access_uri("127.0.0.1", 40002, "b" * 64, secrets.token_urlsafe(32))
        sent = []

        class FakeViewer:
            def __init__(self, uri, config, on_frame, on_status, on_info, on_stats, on_clipboard=None, **kwargs):
                self.address = ("127.0.0.1", 40001, "a" * 64, "tok")
                self.finished = type("E", (), {"is_set": lambda self: False})()

            def start(self):
                pass

            def stop(self):
                pass

            def join(self, timeout=2):
                pass

            def input(self, event):
                sent.append(event)

            def switch_monitor(self, monitor):
                return True

        with patch("brioche.remote_session.Viewer", FakeViewer):
            win = SessionWindow()
            try:
                win.show()
                p1 = win.add_session(uri1, StreamConfig("1440p", 60, 16), network_mode="lan", display_name="主机 A", session_key="lan:a")
                p2 = win.add_session(uri2, StreamConfig("1080p", 60, 16), network_mode="tailscale", display_name="主机 B", session_key="ts:b")
                self.assertEqual(win.session_count(), 2)
                self.assertEqual(win.tabs.count(), 2)

                # Focusing existing session key reuses tab without duplicating
                same = win.add_session(uri1, StreamConfig("1440p", 60, 16), network_mode="lan", display_name="主机 A", session_key="lan:a")
                self.assertIs(same, p1)
                self.assertEqual(win.session_count(), 2)

                # Scale modes & image rect math
                p1.canvas.image = QImage(1920, 1080, QImage.Format.Format_RGB32)
                p1.canvas.resize(960, 540)
                p1.canvas.set_scale_mode("fit")
                self.assertAlmostEqual(p1.canvas.image_rect().width(), 960.0)
                p1.canvas.set_scale_mode("actual")
                self.assertAlmostEqual(p1.canvas.image_rect().width(), 1920.0)
                p1.canvas.set_scale_mode("fill")
                self.assertGreaterEqual(p1.canvas.image_rect().width(), 960.0)

                # Shortcut combo injection presses and releases in reverse order
                p1.canvas.allowed = True
                sent.clear()
                p1.send_shortcut_combo(("ctrl", "alt", "delete"))
                self.assertEqual(
                    [(e["key"], e["down"]) for e in sent],
                    [
                        ("ctrl", True),
                        ("alt", True),
                        ("delete", True),
                        ("delete", False),
                        ("alt", False),
                        ("ctrl", False),
                    ],
                )

                # Floating pill auto-hide and pin toggle
                p1.toolbar.pin_btn.setChecked(False)
                p1._collapse_toolbar()
                self.assertTrue(p1.toolbar.isHidden())
                self.assertFalse(p1.handle.isHidden())
                p1.reveal_toolbar()
                self.assertFalse(p1.toolbar.isHidden())
                self.assertTrue(p1.handle.isHidden())

                # Performance HUD toggle
                p1.toolbar.metrics_btn.setChecked(True)
                self.assertFalse(p1.hud.isHidden())
                p1.hud._close()
                self.assertTrue(p1.hud.isHidden())
                self.assertFalse(p1.toolbar.metrics_btn.isChecked())

                # Detach tab into standalone SessionWindow
                detached = win.detach_page(p2)
                self.assertIsNotNone(detached)
                self.assertEqual(win.session_count(), 1)
                self.assertEqual(detached.session_count(), 1)
                detached.close()
            finally:
                win.close()
                self.app.processEvents()

    def test_theme_switching_persists_across_restart(self):
        self.window.apply_theme("dark")
        self.assertEqual(self.window.store.setting("theme"), "dark")
        self.assertIn("#161513", self.app.styleSheet())
        self.window.apply_theme("light")
        self.assertEqual(self.window.store.setting("theme"), "light")
        self.assertIn("#F6F3ED", self.app.styleSheet())

    def test_offline_and_unshared_devices_cannot_connect(self):
        self.environment["tailscale"]["peers"] = [
            {
                "ip": "100.70.1.3",
                "name": "Offline",
                "reachable": False,
                "sharing": True,
            },
            {
                "ip": "100.70.1.4",
                "name": "Unshared",
                "reachable": True,
                "sharing": False,
            },
        ]
        self.window.apply_environment(self.environment)
        with patch.object(self.window, "connect_device") as connect:
            for i in range(2):
                self.window._device_activated(self.window.device_list.item(i))
            connect.assert_not_called()

    def test_saved_lan_directory_preserves_offline_and_pairing(self):
        device = {
            "ip": "192.168.1.8",
            "port": 47995,
            "name": "Desktop",
            "pin": "a" * 64,
            "token": secrets.token_urlsafe(32),
        }
        self.window.store.upsert(device)
        self.window.update_lan_list([{**device, "online": False}])
        self.assertEqual(self.window.device_list.count(), 1)
        self.assertFalse(
            self.window.device_list.item(0).data(Qt.ItemDataRole.UserRole)["connectable"]
        )
        self.window.update_lan_list([{**device, "online": True}])
        self.assertTrue(
            self.window.device_list.item(0).data(Qt.ItemDataRole.UserRole)["connectable"]
        )
        with patch.object(self.window, "connect_device") as connect:
            self.window._device_activated(self.window.device_list.item(0))
            self.assertEqual(connect.call_args.args[0], "lan")

    def test_external_host_is_loopback_and_lan_host_binds_selected_interface(self):
        with patch("brioche.main_window.Host") as host:
            self.window.external_allowed.setChecked(True)
            self.assertEqual(host.call_args.args[0], "127.0.0.1")
            self.assertEqual(host.call_args.args[1], 0)
            self.assertFalse(host.call_args.kwargs["lan_discovery"])
            self.window.lan_allowed.setChecked(True)
            self.assertEqual(host.call_args.args[0], "192.168.1.5")
            self.assertTrue(host.call_args.kwargs["lan_discovery"])
            self.assertIs(host.call_args.kwargs["session_gate"], self.window.gate)
            self.window.hosts = {"lan": None, "tailscale": None}

    def test_missing_account_cannot_start_external_host(self):
        self.environment["tailscale"] = {
            "ready": False,
            "ips": [],
            "peers": [],
            "message": "请登录",
        }
        self.window.apply_environment(self.environment)
        with patch("brioche.main_window.Host") as host:
            self.window.external_allowed.setChecked(True)
            host.assert_not_called()

    def test_explicit_share_permission_restores_after_restart(self):
        self.window.store.configure(
            external_allowed=True, lan_allowed=True, encoder="libx264", theme="dark"
        )
        with (
            patch.object(MainWindow, "refresh_environment"),
            patch.object(MainWindow, "refresh_lan"),
            patch("brioche.main_window.Host") as host,
        ):
            reopened = MainWindow(folder=self.temp.name, network=FakeNetwork())
            reopened.network_poll.stop()
            reopened.lan_poll.stop()
            try:
                self.assertTrue(reopened.external_allowed.isChecked())
                self.assertTrue(reopened.lan_allowed.isChecked())
                self.assertEqual(reopened.encoder.currentData(), "libx264")
                self.assertEqual(reopened.theme_combo.currentData(), "dark")
                reopened.apply_environment(self.environment)
                self.assertEqual(host.call_count, 2)
            finally:
                reopened.hosts = {"lan": None, "tailscale": None}
                reopened.shutdown()
                reopened.close()

    def test_close_keeps_share_running_and_restore_opens_window(self):
        self.window.show()
        with patch(
            "brioche.main_window.QSystemTrayIcon.isSystemTrayAvailable",
            return_value=True,
        ):
            self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.window._alive)
        self.assertTrue(self.window.poll.isActive())
        self.window.restore_window()
        self.assertTrue(self.window.isVisible())

    def test_close_without_tray_minimizes_and_shutdown_is_explicit(self):
        self.window.show()
        with patch(
            "brioche.main_window.QSystemTrayIcon.isSystemTrayAvailable",
            return_value=False,
        ):
            self.window.close()
        self.assertTrue(self.window.isMinimized())
        self.assertTrue(self.window._alive)
        self.window.shutdown()
        self.assertFalse(self.window._alive)
        self.assertFalse(self.window.poll.isActive())
