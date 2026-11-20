import os
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit
from brioche.main_window import MainWindow
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
        self.window.close()
        self.app.processEvents()
        self.temp.cleanup()

    def test_home_has_device_directory_without_connection_code_form(self):
        self.assertEqual(self.window.pages.currentIndex(), 0)
        self.assertEqual(self.window.internet_list.count(), 0)
        self.assertEqual(self.window.pages.widget(0).findChildren(QLineEdit), [])
        self.assertTrue(self.window.connect_advanced.isHidden())
        self.assertEqual(self.window.fps.currentData(), 60)
        self.assertFalse(self.window.external_allowed.isChecked())
        self.assertFalse(self.window.lan_allowed.isChecked())

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
        self.assertEqual(self.window.internet_list.count(), 2)
        original = self.window.internet_list.item(0)
        self.window.internet_list.setCurrentItem(original)
        self.window.apply_environment(self.environment)
        self.assertIs(self.window.internet_list.item(0), original)
        self.assertIs(self.window.internet_list.currentItem(), original)
        self.assertIn("离线", self.window.internet_list.item(1).text())

    def test_online_device_single_click_connects_without_code(self):
        self.environment["tailscale"]["peers"] = [
            {"ip": "100.70.1.3", "name": "Laptop", "reachable": True, "sharing": True}
        ]
        self.window.apply_environment(self.environment)
        uri = access_uri("127.0.0.1", 32123, "a" * 64, secrets.token_urlsafe(32))
        with (
            patch.object(
                self.window.network, "open_peer", return_value=uri, create=True
            ) as opened,
            patch("brioche.ui.RemoteWindow") as remote,
        ):
            self.window.internet_list.itemClicked.emit(
                self.window.internet_list.item(0)
            )
            end = time.monotonic() + 3
            while not remote.called and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(0.01)
            opened.assert_called_once_with("100.70.1.3")
            self.assertEqual(remote.call_args.args[0], uri)
            self.assertEqual(remote.call_args.kwargs["network_mode"], "tailscale")
            self.window.remote = None

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
                self.window.internet_clicked(self.window.internet_list.item(i))
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
        self.assertEqual(self.window.lan_list.count(), 1)
        self.assertFalse(
            self.window.lan_list.item(0).data(Qt.ItemDataRole.UserRole)["connectable"]
        )
        self.window.update_lan_list([{**device, "online": True}])
        self.assertTrue(
            self.window.lan_list.item(0).data(Qt.ItemDataRole.UserRole)["connectable"]
        )
        with patch.object(self.window, "connect_device") as connect:
            self.window.lan_clicked(self.window.lan_list.item(0))
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
            external_allowed=True, lan_allowed=True, encoder="libx264"
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
                reopened.apply_environment(self.environment)
                self.assertEqual(host.call_count, 2)
            finally:
                reopened.hosts = {"lan": None, "tailscale": None}
                reopened.close()
