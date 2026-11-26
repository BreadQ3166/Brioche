"""End-to-end integration test for authenticated TLS v2_negotiate -> Rust brioche-core host."""

import tempfile
import unittest

from brioche.core_engine import find_core_binary, negotiate_v2_endpoint
from brioche.security import access_uri
from brioche.session import Host


class V2NegotiateIntegrationTests(unittest.TestCase):
    def test_authenticated_v2_negotiate_starts_core_host_and_rejects_bad_token(self):
        exe = find_core_binary()
        if exe is None:
            self.skipTest("brioche-core binary not built")
        with tempfile.TemporaryDirectory() as folder:
            host = Host(
                bind="127.0.0.1",
                port=0,
                folder=folder,
                view_only=True,
                v2_core_enabled=True,
            )
            host.start()
            try:
                self.assertTrue(host.ready.wait(10), host.error)
                uri = access_uri("127.0.0.1", host.port, host.pin, host.token)
                res = negotiate_v2_endpoint(uri, timeout=6.0)
                self.assertIsNotNone(res)
                ip, ep = res
                self.assertEqual(ip, "127.0.0.1")
                self.assertGreater(ep.quic_port, 0)
                self.assertEqual(len(ep.pin), 64)
                self.assertEqual(len(ep.key), 64)

                # Invalid 43-char token must be rejected by Host authentication
                bad_uri = access_uri("127.0.0.1", host.port, host.pin, "A" * 43)
                self.assertIsNone(negotiate_v2_endpoint(bad_uri, timeout=3.0))
            finally:
                host.stop()
                host.join(5)

    def test_v2_negotiate_returns_none_when_disabled_for_v1_compat(self):
        with tempfile.TemporaryDirectory() as folder:
            host = Host(
                bind="127.0.0.1",
                port=0,
                folder=folder,
                view_only=True,
                v2_core_enabled=False,
            )
            host.start()
            try:
                self.assertTrue(host.ready.wait(10), host.error)
                uri = access_uri("127.0.0.1", host.port, host.pin, host.token)
                self.assertIsNone(negotiate_v2_endpoint(uri, timeout=4.0))
            finally:
                host.stop()
                host.join(5)


if __name__ == "__main__":
    unittest.main()
