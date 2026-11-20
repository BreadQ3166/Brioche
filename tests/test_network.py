import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from brioche.network import address_mode, parse_tailscale_status, tailscale_status


class NetworkTests(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(address_mode("192.168.1.5"), "lan")
        self.assertEqual(address_mode("100.100.3.5"), "tailscale")
        self.assertEqual(address_mode("127.0.0.1"), "loopback")

    def test_logged_out_and_userspace_are_not_ready(self):
        for data in ({"BackendState": "NeedsLogin"},
                     {"BackendState": "Running", "TUN": False, "TailscaleIPs": ["100.80.1.2"]}):
            self.assertFalse(parse_tailscale_status(data)["ready"])

    def test_status_paths_require_active_traffic(self):
        data = {"BackendState": "Running", "TailscaleIPs": ["100.80.1.2"],
                "Peer": {"p": {"TailscaleIPs": ["100.80.1.3"], "Relay": "hkg", "Active": False}}}
        self.assertEqual(parse_tailscale_status(data, "100.80.1.3")["path"], "待检测")
        peer = data["Peer"]["p"]
        peer["Active"] = True
        self.assertEqual(parse_tailscale_status(data, "100.80.1.3")["path"], "DERP 中继")
        peer["CurAddr"] = "1.2.3.4:41641"
        self.assertEqual(parse_tailscale_status(data, "100.80.1.3")["path"], "直连")
        peer["PeerRelay"] = "5.6.7.8:40000:1"
        self.assertEqual(parse_tailscale_status(data, "100.80.1.3")["path"], "节点中继")

    @patch("brioche.network.tailscale_executable", return_value=None)
    def test_missing_dependency(self, lookup):
        self.assertEqual(tailscale_status()["state"], "Missing")

    @patch("brioche.network.tailscale_executable", return_value="/test/tailscale")
    @patch("brioche.network.subprocess.run")
    def test_read_only_cli_and_malformed_output(self, run, lookup):
        run.return_value = SimpleNamespace(stdout="not json", returncode=1)
        self.assertFalse(tailscale_status()["ready"])
        run.return_value = SimpleNamespace(stdout=json.dumps({"BackendState": "Running", "TailscaleIPs": ["100.70.1.2"]}), returncode=0)
        self.assertEqual(tailscale_status()["ips"], ["100.70.1.2"])
        self.assertEqual(run.call_args.args[0], ["/test/tailscale", "status", "--json"])
