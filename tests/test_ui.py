import os
import secrets
import unittest
import time
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from brioche.main_window import MainWindow
from brioche.security import access_uri


@unittest.skipUnless(os.environ.get("BRIOCHE_DESKTOP_TESTS") == "1", "requires isolated Qt desktop")
class SimpleFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(MainWindow, "refresh_environment"):
            self.window = MainWindow()
        self.window.apply_environment({
            "lan": [{"ip": "192.168.1.5", "name": "Ethernet"}],
            "tailscale": {"ready": True, "ips": ["100.70.1.2"], "message": "Tailscale 已连接"},
            "monitors": [{"id": 1, "width": 2560, "height": 1440}, {"id": 2, "width": 1440, "height": 2560}],
        })

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

    def test_lan_and_tailscale_bind_only_selected_address(self):
        self.assertEqual(self.window.address.currentData(), "192.168.1.5")
        self.window.share_mode.setCurrentIndex(1)
        self.assertEqual(self.window.address.currentData(), "100.70.1.2")
        with patch("brioche.main_window.Host") as host:
            self.window.toggle_host()
            self.assertEqual(host.call_args.args[0], "127.0.0.1")
            self.assertEqual(host.call_args.args[1], 0)
            self.window.host = None

    def test_unavailable_tailscale_cannot_fall_back_to_lan(self):
        self.window.environment["tailscale"] = {"ready": False, "ips": [], "message": "请先登录"}
        self.window.share_mode.setCurrentIndex(1)
        self.assertFalse(self.window.start_host.isEnabled())
        self.assertEqual(self.window.address.count(), 0)

    def test_pasted_code_selects_network_and_advanced_is_hidden(self):
        self.window.uri.setPlainText(access_uri("100.70.1.3", 47995, "a" * 64, secrets.token_urlsafe(32)))
        self.assertEqual(self.window.connection_mode.currentData(), "tailscale")
        self.assertTrue(self.window.connect_advanced.isHidden())
        self.assertEqual(self.window.monitor.count(), 2)

    def test_account_device_list_excludes_offline_and_preserves_selection(self):
        self.window.environment['tailscale']['peers'] = [
            {'ip':'100.70.1.3','name':'Laptop','online':True},
            {'ip':'100.70.1.4','name':'Offline','online':False}]
        self.window.apply_environment(self.window.environment)
        self.assertEqual(self.window.devices.count(), 2)
        self.window.devices.setCurrentIndex(1)
        self.window.apply_environment(self.window.environment)
        self.assertEqual(self.window.devices.currentData(), '100.70.1.3')

    def test_selected_device_connects_without_copying_code(self):
        self.window.environment['tailscale']['peers'] = [
            {'ip':'100.70.1.3','name':'Laptop','online':True}]
        self.window.apply_environment(self.window.environment)
        self.window.connection_mode.setCurrentIndex(1)
        self.window.devices.setCurrentIndex(1)
        uri = access_uri('127.0.0.1', 32123, 'a'*64, secrets.token_urlsafe(32))
        with patch.object(self.window.network, 'open_peer', return_value=uri) as open_peer, patch('brioche.ui.RemoteWindow') as remote:
            self.window.connect_remote()
            end=time.monotonic()+3
            while not remote.called and time.monotonic()<end:
                self.app.processEvents()
                time.sleep(.01)
            open_peer.assert_called_once_with('100.70.1.3')
            self.assertEqual(remote.call_args.args[0], uri)
            self.assertEqual(remote.call_args.kwargs['network_mode'], 'tailscale')
            self.window.remote = None
