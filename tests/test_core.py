import asyncio
import hashlib
import secrets
import struct
import tempfile
import unittest

from brioche.protocol import (CONTROL, HEADER, MAX_CONTROL, ProtocolError, StreamConfig,
                              pack_control, read_message)
from brioche.security import access_uri, identity, parse_uri, verify_pin
from brioche.platform_input import validate_event


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_stream(self):
        reader = asyncio.StreamReader()
        packet = pack_control({"type": "ping", "stamp": 3})
        async def feed():
            for part in (packet[:2], packet[2:6], packet[6:]):
                reader.feed_data(part)
                await asyncio.sleep(0)
        feeder = asyncio.create_task(feed())
        self.assertEqual(await read_message(reader), (CONTROL, {"type": "ping", "stamp": 3}))
        await feeder

    async def test_oversized_rejected_before_body(self):
        for header in (HEADER.pack(CONTROL, MAX_CONTROL + 1), HEADER.pack(99, 1), HEADER.pack(CONTROL, 0)):
            reader = asyncio.StreamReader()
            reader.feed_data(header)
            with self.assertRaises(ProtocolError):
                await read_message(reader)

    async def test_malformed_controls(self):
        for value in (b"{", b"[]", b'{"type":1}', b'"str"', b"\xff"):
            reader = asyncio.StreamReader()
            reader.feed_data(HEADER.pack(CONTROL, len(value)) + value)
            with self.assertRaises(ProtocolError):
                await read_message(reader)

    def test_config_validation(self):
        for kwargs in ({"fps": True}, {"fps": 10000}, {"resolution": "8k"}, {"bitrate_mbps": -1}):
            with self.assertRaises(ProtocolError):
                StreamConfig(**kwargs)


class SecurityTests(unittest.TestCase):
    def test_uri_ipv4_ipv6_and_dns(self):
        pin, token = "a" * 64, secrets.token_urlsafe(32)
        for host in ("192.168.1.10", "100.88.1.2", "::1", "workstation"):
            self.assertEqual(parse_uri(access_uri(host, 47995, pin, token)), (host, 47995, pin, token))

    def test_invalid_uri_has_no_insecure_fallback(self):
        pin, token = "a" * 64, secrets.token_urlsafe(32)
        for value in ("http://localhost", "brioche://localhost:47995", "", "x" * 3000,
                      f"brioche://localhost:0#{pin}.{token}",
                      f"brioche://user@localhost:47995#{pin}.{token}"):
            with self.assertRaises(ValueError):
                parse_uri(value)

    def test_identity_persistence_and_pin(self):
        with tempfile.TemporaryDirectory() as folder:
            _, pin = identity(folder)
            self.assertEqual(identity(folder)[1], pin)
        class Peer:
            def getpeercert(self, binary_form):
                return b"certificate"
        verify_pin(Peer(), hashlib.sha256(b"certificate").hexdigest())
        with self.assertRaises(ValueError):
            verify_pin(Peer(), "f" * 64)


class InputTests(unittest.TestCase):
    def test_reject_unbounded_and_invalid_input(self):
        for event in ({"event": "move", "x": float("nan"), "y": 0},
                      {"event": "move", "x": -1, "y": 0},
                      {"event": "key", "key": "shell command", "down": True},
                      {"event": "scroll", "dy": 1000},
                      {"event": "button", "button": "left", "down": "yes"}):
            with self.assertRaises(ValueError):
                validate_event(event)


if __name__ == "__main__":
    unittest.main()
