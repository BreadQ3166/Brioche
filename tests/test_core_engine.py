"""Integration tests for the Python <-> Rust `brioche-core` v2.0 engine bridge."""

import unittest

from brioche.core_engine import (
    CoreHostProcess,
    find_core_binary,
    probe_core_capabilities,
)


class CoreEngineBridgeTests(unittest.TestCase):
    def test_probe_and_host_handshake_when_core_binary_is_present(self):
        exe = find_core_binary()
        if exe is None:
            self.skipTest("brioche-core binary not built in this environment")
        caps = probe_core_capabilities(exe)
        self.assertTrue(caps.get("available"), caps)
        self.assertEqual(caps.get("protocol_version"), 3)
        self.assertEqual(caps.get("cipher"), "chacha20-poly1305")
        self.assertIn("brioche-udp-v3", caps.get("transports", []))
        self.assertIn("quic-pinned-tls13", caps.get("transports", []))

        host = CoreHostProcess(bind="127.0.0.1", quic_port=0, monitor=1, view_only=True, binary=exe)
        try:
            ep = host.start()
            self.assertGreater(ep.quic_port, 0)
            self.assertEqual(len(ep.pin), 64)
            self.assertEqual(len(ep.key), 64)
        finally:
            host.stop()


if __name__ == "__main__":
    unittest.main()
