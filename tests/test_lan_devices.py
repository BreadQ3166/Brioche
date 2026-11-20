import asyncio
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from brioche.device_store import DeviceStore
from brioche.lan import discover, pair, pairing_code, target
from brioche.protocol import StreamConfig
from brioche.security import access_uri, client_context
from brioche.session import Host, SessionGate


class DirectoryTests(unittest.TestCase):
    def test_private_directory_remembers_pairing_and_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            store = DeviceStore(folder)
            identity = store.client_id
            token = secrets.token_urlsafe(32)
            store.upsert(
                {
                    "ip": "192.168.1.8",
                    "port": 47995,
                    "name": "Desktop",
                    "pin": "a" * 64,
                    "token": token,
                }
            )
            store.configure(lan_allowed=True)
            loaded = DeviceStore(folder)
            self.assertEqual(loaded.client_id, identity)
            self.assertEqual(loaded.devices()[0]["token"], token)
            self.assertTrue(loaded.setting("lan_allowed"))
            loaded.remove("192.168.1.8", 47995)
            self.assertEqual(DeviceStore(folder).devices(), [])

    def test_targets_are_explicit_lan_addresses_only(self):
        self.assertEqual(target("192.168.1.8"), ("192.168.1.8", 47995))
        self.assertEqual(target("10.0.1.2:50000"), ("10.0.1.2", 50000))
        for value in (
            "https://192.168.1.8",
            "example.com",
            "1.1.1.1",
            "100.70.1.3",
            "192.168.1.8:0",
            "127.0.0.1:65536",
        ):
            with self.assertRaises(ValueError):
                target(value)

    def test_code_is_bound_to_identity_nonce_and_client(self):
        code = pairing_code("a" * 64, "b" * 32, "c" * 32)
        self.assertEqual(len(code), 6)
        self.assertNotEqual(code, pairing_code("d" * 64, "b" * 32, "c" * 32))
        self.assertNotEqual(code, pairing_code("a" * 64, "d" * 32, "c" * 32))


class LANPairTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.requests = []
        self.codes = []
        self.client_id = secrets.token_hex(16)

        def approve(request):
            self.requests.append(request)
            self.assertEqual(request.code, self.codes[-1])
            request.answer(True)

        self.host = Host(
            port=0,
            folder=self.folder.name,
            lan_discovery=True,
            on_pair=approve,
            view_only=True,
        )
        self.host.start()
        self.assertTrue(self.host.ready.wait(5))
        self.assertIsNone(self.host.error)

    def tearDown(self):
        self.host.stop()
        self.host.join(8)
        self.assertFalse(self.host._thread.is_alive())
        self.folder.cleanup()

    def test_ip_discovery_never_returns_credentials(self):
        device = discover("127.0.0.1", self.host.port)
        self.assertEqual(device["observed_pin"], self.host.pin)
        self.assertNotIn("token", device)
        self.assertTrue(device["online"])
        self.assertFalse(self.requests)

    def test_first_pair_requires_confirmation_and_persists(self):
        grant = pair(
            "127.0.0.1",
            self.host.port,
            self.host.pin,
            self.client_id,
            "test-controller",
            self.codes.append,
        )
        self.assertTrue(self.host.pair_store.accepts(grant["token"]))
        from brioche.lan import PairStore

        self.assertTrue(PairStore(self.folder.name).accepts(grant["token"]))
        self.assertEqual(len(self.requests), 1)
        self.host.pair_store.clear()
        self.assertFalse(self.host.pair_store.accepts(grant["token"]))

    def test_denied_pairing_never_grants_token(self):
        self.host.on_pair = lambda request: request.answer(False)
        with self.assertRaises(ValueError):
            pair("127.0.0.1", self.host.port, self.host.pin, self.client_id, "denied")
        self.assertEqual(self.host.pair_store.pairs, {})

    def test_identity_change_is_rejected_before_request(self):
        with self.assertRaises(ValueError):
            discover("127.0.0.1", self.host.port, "f" * 64)
        with self.assertRaises(ValueError):
            pair("127.0.0.1", self.host.port, "f" * 64, self.client_id, "changed")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.host.pair_store.pairs, {})

    def test_pairing_is_disabled_by_default(self):
        disabled = Host(port=0, folder=Path(self.folder.name) / "disabled")
        disabled.start()
        self.assertTrue(disabled.ready.wait(5))
        try:
            with self.assertRaises(ValueError):
                discover("127.0.0.1", disabled.port)
        finally:
            disabled.stop()
            disabled.join()

    def test_session_gate_has_single_owner_across_hosts(self):
        gate = SessionGate()
        first, second = object(), object()
        self.assertTrue(gate.acquire(first))
        self.assertFalse(gate.acquire(second))
        gate.release(second)
        self.assertTrue(gate.active)
        gate.release(first)
        self.assertTrue(gate.acquire(second))
