import os
from pathlib import Path
import secrets
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QApplication

from brioche.device_store import DeviceStore
from brioche.file_transfer import (
    TransferError,
    TransferJob,
    safe_relpath,
    sources,
    validate_manifest,
)
from brioche.lan import PairStore
from brioche.lan_discovery import LanBeaconListener, build_beacon, parse_beacon
from brioche.security import access_uri
from brioche.session import Host


def until(predicate, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class LanDiscoveryAndPairingTests(unittest.TestCase):
    def test_beacon_build_parse_and_security_filters(self):
        pin = "a" * 64
        raw = build_beacon("Workstation", 47995, pin, busy=False, system_name="Linux")
        parsed = parse_beacon(raw, "192.168.1.20")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["ip"], "192.168.1.20")
        self.assertEqual(parsed["name"], "Workstation")
        self.assertEqual(parsed["pin_prefix"], "a" * 16)

        # Public IPs and malformed payloads are rejected
        self.assertIsNone(parse_beacon(raw, "8.8.8.8"))
        self.assertIsNone(parse_beacon(b"{bad", "192.168.1.20"))
        self.assertIsNone(parse_beacon(b"x" * 2048, "192.168.1.20"))

        # Listener ignores own pin prefix and records remote peers
        listener = LanBeaconListener(local_pin_getter=lambda: pin)
        self.assertFalse(listener.ingest(raw, "192.168.1.20"))
        other = build_beacon("StudioMac", 47995, "b" * 64, busy=True, system_name="Darwin")
        self.assertTrue(listener.ingest(other, "192.168.1.21"))
        snap = listener.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["name"], "StudioMac")
        self.assertTrue(snap[0]["busy"])

    def test_dhcp_ip_change_preserves_pairing_and_profile_by_cert_pin(self):
        with tempfile.TemporaryDirectory() as folder:
            store = DeviceStore(folder)
            pin = "c" * 64
            tok = secrets.token_urlsafe(32)
            store.upsert(
                {"ip": "192.168.1.10", "port": 47995, "name": "DevBox", "pin": pin, "token": tok}
            )
            store.set_device_profile("lan:192.168.1.10:47995", alias="开发机", group="办公室", fps=120)

            # Device gets a new DHCP IP with the same verified certificate pin
            store.upsert(
                {"ip": "192.168.1.88", "port": 47995, "name": "DevBox", "observed_pin": pin}
            )
            devs = store.devices()
            self.assertEqual(len(devs), 1)
            self.assertEqual(devs[0]["ip"], "192.168.1.88")
            self.assertEqual(devs[0]["token"], tok)
            self.assertEqual(devs[0]["pin"], pin)
            self.assertEqual(store.device_profile("lan:192.168.1.88:47995")["alias"], "开发机")

    def test_pair_store_lists_without_tokens_and_revokes_individually(self):
        with tempfile.TemporaryDirectory() as folder:
            ps = PairStore(folder)
            cid1, cid2 = secrets.token_hex(16), secrets.token_hex(16)
            t1 = ps.grant(cid1, "MacBook")
            t2 = ps.grant(cid2, "ThinkPad")
            listed = ps.list_pairs()
            self.assertEqual(len(listed), 2)
            for item in listed:
                self.assertNotIn("token", item)
            self.assertTrue(ps.revoke(cid1))
            self.assertFalse(ps.accepts(t1))
            self.assertTrue(ps.accepts(t2))
            self.assertEqual(len(PairStore(folder).list_pairs()), 1)


class FolderTransferTests(unittest.TestCase):
    def test_safe_relpath_blocks_traversal_and_deep_nesting(self):
        self.assertEqual(safe_relpath("proj/sub/spec.pdf"), "proj/sub/spec.pdf")
        for bad in (
            "/etc/passwd",
            "../escape.txt",
            "a/../b.txt",
            "a\\b.txt",
            "dir/CON.txt",
            "/".join(["d"] * 20),
        ):
            with self.subTest(bad=bad), self.assertRaises(TransferError):
                safe_relpath(bad)
        with self.assertRaises(TransferError):
            validate_manifest([{"name": "a.txt", "relpath": "dir/b.txt", "size": 1}])

    def test_recursive_folder_tls_transfer_preserves_tree_and_reports_speed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "project"
            (src_dir / "docs").mkdir(parents=True)
            (src_dir / "assets" / "icons").mkdir(parents=True)
            f1 = src_dir / "docs" / "readme.md"
            f2 = src_dir / "assets" / "icons" / "logo.bin"
            p1 = b"# Brioche Folder Transfer\n" * 100
            p2 = secrets.token_bytes(350_000)
            f1.write_bytes(p1)
            f2.write_bytes(p2)

            resolved, manifest = sources([str(src_dir)])
            self.assertEqual(len(resolved), 2)
            self.assertEqual(
                {item["relpath"] for item in manifest},
                {"project/docs/readme.md", "project/assets/icons/logo.bin"},
            )

            out_dir = root / "received"
            out_dir.mkdir()
            updates = []
            host = Host(
                port=0,
                folder=root / "state",
                files_enabled=True,
                on_transfer=lambda req: req.respond(str(out_dir)),
            )
            host.start()
            self.assertTrue(host.ready.wait(5))
            uri = access_uri("127.0.0.1", host.port, host.pin, host.token)
            job = TransferJob(uri, "upload", [str(src_dir)], updates.append, rate=0)
            job.start()
            try:
                self.assertTrue(job.finished.wait(10))
                self.assertEqual((out_dir / "project" / "docs" / "readme.md").read_bytes(), p1)
                self.assertEqual((out_dir / "project" / "assets" / "icons" / "logo.bin").read_bytes(), p2)
                self.assertTrue(any(u.get("speed_bps", 0) > 0 for u in updates))
            finally:
                job.stop()
                host.stop()
                host.join(5)


@unittest.skipUnless(
    os.environ.get("BRIOCHE_DESKTOP_TESTS") == "1", "requires isolated Qt desktop"
)
class Step4QtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_add_lan_dialog_nearby_list_and_single_pair_revoke(self):
        from brioche.main_window import MainWindow
        from brioche.ui_components import AddLanDeviceDialog

        dlg = AddLanDeviceDialog(
            "192.168.1.5",
            nearby_devices=[
                {"ip": "192.168.1.33", "port": 47995, "name": "Studio", "platform": "Darwin", "busy": False}
            ],
        )
        self.assertEqual(dlg.nearby_list.count(), 1)
        dlg.nearby_list.itemClicked.emit(dlg.nearby_list.item(0))
        self.assertEqual(dlg.value(), "192.168.1.33")
        dlg.close()

        with tempfile.TemporaryDirectory() as folder:
            ps = PairStore(folder)
            cid = secrets.token_hex(16)
            ps.grant(cid, "RemoteLaptop")
            with (
                patch.object(MainWindow, "refresh_environment"),
                patch.object(MainWindow, "refresh_lan"),
            ):
                class FakeNet:
                    def status(self, *a, **k):
                        return {"ready": False, "ips": [], "peers": [], "message": "请登录", "state": "NeedsLogin"}
                    def close(self): pass
                    def clear(self, *a): pass
                win = MainWindow(folder=folder, network=FakeNet())
                try:
                    self.assertEqual(win.settings_view.paired_rows.count(), 1)
                    win.revoke_pair(cid)
                    self.assertEqual(win.settings_view.paired_rows.count(), 0)
                    self.assertEqual(PairStore(folder).list_pairs(), [])
                finally:
                    win.shutdown()
                    win.close()

    def test_extract_drop_paths(self):
        from brioche.remote_session import RemoteSessionPage

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("/tmp/a.txt"), QUrl("https://example.com")])
        self.assertEqual(RemoteSessionPage._extract_drop_paths(mime), ["/tmp/a.txt"])
