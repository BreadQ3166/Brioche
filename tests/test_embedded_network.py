import os
from pathlib import Path
import secrets
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from brioche.embedded_network import (
    DEFAULT_EASYTIER_PEERS,
    EmbeddedNetwork,
    derive_node_ips,
    login_url,
    parse_peer_urls,
    sanitize_hostname,
)
from brioche.security import access_uri, parse_uri


class LoginURLTests(unittest.TestCase):
    def test_only_official_https_login_urls(self):
        self.assertTrue(login_url("https://login.tailscale.com/a/test"))
        for value in (
            "http://login.tailscale.com/a/test",
            "https://login.tailscale.com.evil.test/",
            "https://user:secret@login.tailscale.com/",
            "file:///etc/passwd",
            "https://login.tailscale.com:8080/a/test",
            None,
        ):
            self.assertFalse(login_url(value))


class EasyTierEmbeddedNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.network = EmbeddedNetwork(folder=self.folder / "node")

    def tearDown(self):
        self.network.close()
        self.temp.cleanup()

    def test_idle_does_not_start_network_until_login(self):
        status = self.network.status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["state"], "NeedsLogin")
        self.assertIsNone(self.network.process)

    def test_peer_url_parser_and_deterministic_ips(self):
        parsed = parse_peer_urls("tcp://1.2.3.4:11010, invalid://x udp://5.6.7.8:11010")
        self.assertEqual(parsed, ["tcp://1.2.3.4:11010", "udp://5.6.7.8:11010"])
        self.assertEqual(parse_peer_urls(""), list(DEFAULT_EASYTIER_PEERS))
        vip, pip = derive_node_ips("00000000-1111-2222-3333-444444444444")
        self.assertTrue(vip.startswith("10.144."))
        self.assertTrue(pip.startswith("10.145."))
        self.assertEqual(vip.split(".")[2:], pip.split(".")[2:])
        self.assertEqual(sanitize_hostname("My Ubuntu PC!"), "My-Ubuntu-PC")

    def test_save_and_logout_preserves_permissions(self):
        cfg = self.network._save_config("team-net", "supersecret", peers="tcp://183.230.36.171:11010")
        self.assertEqual(cfg["network_name"], "team-net")
        self.assertEqual(cfg["peers"], ["tcp://183.230.36.171:11010"])
        self.assertTrue(self.network.enabled)
        if os.name != "nt":
            self.assertEqual(self.network.folder.stat().st_mode & 0o777, 0o700)
        self.network.logout()
        self.assertFalse(self.network.enabled)
        self.assertFalse(self.network.status()["ready"])

    def test_proxy_preserves_certificate_and_session_token(self):
        token = secrets.token_urlsafe(32)
        uri = access_uri("10.145.10.20", 47985, "a" * 64, token)
        with patch.object(self.network, "_start", lambda: None):
            self.network._socks_port = 1080
            proxied = self.network.proxy_uri(uri)
        host, port, pin, out_token = parse_uri(proxied)
        self.assertEqual(host, "127.0.0.1")
        self.assertGreater(port, 0)
        self.assertEqual(pin, "a" * 64)
        self.assertEqual(out_token, token)

    def test_close_prevents_restart(self):
        self.network.close()
        with self.assertRaises(RuntimeError):
            self.network._start()

    def test_stopped_share_cannot_be_published_after_delayed_work(self):
        stopped = threading.Event()
        stopped.set()
        host = SimpleNamespace(
            bind="127.0.0.1",
            stop_requested=stopped,
            port=32123,
            pin="a" * 64,
            token="b" * 43,
        )
        self.network.publish(host)
        self.assertIsNone(self.network.process)
