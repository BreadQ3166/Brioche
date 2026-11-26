import os
from pathlib import Path
import secrets
import tempfile
import time
import unittest

from PySide6.QtWidgets import QApplication

from brioche.file_browser import RemoteFileBrowserClient
from brioche.file_transfer import TransferError, TransferJob
from brioche.security import access_uri
from brioche.session import Host
from brioche.theme import build_stylesheet


def until(predicate, seconds=6):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.015)
    return False


class RemoteFileBrowserTLSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote_dir = self.root / "remote_space"
        self.local_dir = self.root / "local_space"
        self.remote_dir.mkdir()
        self.local_dir.mkdir()
        self.prompts = []
        self.host = Host(
            port=0,
            folder=self.root / "state",
            files_enabled=True,
            file_browser_enabled=True,
            on_transfer=lambda req: self.prompts.append(req),
        )
        self.host.start()
        self.assertTrue(self.host.ready.wait(5))
        self.uri = access_uri("127.0.0.1", self.host.port, self.host.pin, self.host.token)

    def tearDown(self):
        self.host.stop()
        self.host.join(5)
        self.temp.cleanup()

    def test_browse_mkdir_rename_delete_and_permission_guards(self):
        (self.remote_dir / "hello.txt").write_text("hi", encoding="utf-8")
        client = RemoteFileBrowserClient(self.uri)
        info = client.start()
        try:
            self.assertIn("roots", info)
            listed = client.list_dir(str(self.remote_dir))
            names = [e["name"] for e in listed["entries"]]
            self.assertEqual(names, ["hello.txt"])

            created = client.mkdir(str(self.remote_dir), "sub_dir")
            self.assertTrue(Path(created["path"]).is_dir())

            renamed = client.rename(created["path"], "renamed_dir")
            self.assertTrue(Path(renamed["path"]).is_dir())
            self.assertFalse(Path(created["path"]).exists())

            # Non-empty dir cannot be deleted blindly
            (Path(renamed["path"]) / "keep.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(TransferError):
                client.delete(renamed["path"])

            # Delete file then empty dir succeeds
            client.delete(str(Path(renamed["path"]) / "keep.txt"))
            client.delete(renamed["path"])
            self.assertFalse(Path(renamed["path"]).exists())
        finally:
            client.close()

        # Disabling file_browser_enabled rejects new browser clients
        self.host.file_browser_enabled = False
        denied = RemoteFileBrowserClient(self.uri)
        with self.assertRaises(TransferError):
            denied.start()

    def test_unattended_direct_upload_and_download_without_host_prompt(self):
        # 1. Direct upload of local folder into remote_dir without any host prompt
        src_folder = self.local_dir / "bundle"
        (src_folder / "nested").mkdir(parents=True)
        payload = secrets.token_bytes(180_000)
        (src_folder / "nested" / "data.bin").write_bytes(payload)

        up_job = TransferJob(
            self.uri,
            "upload",
            [str(src_folder)],
            rate=0,
            target_dir=str(self.remote_dir),
        )
        up_job.start()
        self.assertTrue(up_job.finished.wait(10))
        self.assertEqual(
            (self.remote_dir / "bundle" / "nested" / "data.bin").read_bytes(), payload
        )
        self.assertEqual(self.prompts, [])

        # 2. Direct download of remote folder into local_dir/dl without any host prompt
        dl_dir = self.local_dir / "dl"
        dl_dir.mkdir()
        down_job = TransferJob(
            self.uri,
            "download",
            str(dl_dir),
            rate=0,
            remote_paths=[str(self.remote_dir / "bundle")],
        )
        down_job.start()
        self.assertTrue(down_job.finished.wait(10))
        self.assertEqual(
            (dl_dir / "bundle" / "nested" / "data.bin").read_bytes(), payload
        )
        self.assertEqual(self.prompts, [])


@unittest.skipUnless(
    os.environ.get("BRIOCHE_DESKTOP_TESTS") == "1", "requires isolated Qt desktop"
)
class FileManagerQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dual_pane_file_manager_window_end_to_end_and_screenshot(self):
        from brioche.file_manager_ui import FileManagerWindow

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_dir = root / "local"
            remote_dir = root / "remote"
            local_dir.mkdir()
            remote_dir.mkdir()
            (local_dir / "设计方案.pdf").write_bytes(b"%PDF-1.7\n" + b"0" * 4096)
            (local_dir / "素材包").mkdir()
            (local_dir / "素材包" / "icon.svg").write_text("<svg/>", encoding="utf-8")
            (remote_dir / "已有资料.txt").write_text("remote-content", encoding="utf-8")

            host = Host(
                port=0,
                folder=root / "state",
                files_enabled=True,
                file_browser_enabled=True,
            )
            host.start()
            self.assertTrue(host.ready.wait(5))
            uri = access_uri("127.0.0.1", host.port, host.pin, host.token)

            self.app.setStyleSheet(build_stylesheet("dark"))
            win = FileManagerWindow(uri, remote_name="工作站-Ubuntu")
            try:
                win.show()
                self.assertTrue(
                    until(lambda: (self.app.processEvents() or True) and win._remote_connected, 6)
                )
                win._load_local(str(local_dir))
                win._load_remote(str(remote_dir))
                self.assertTrue(
                    until(
                        lambda: (self.app.processEvents() or True)
                        and win._remote_path == str(remote_dir.resolve()),
                        5,
                    )
                )
                self.assertEqual(win.local_tree.topLevelItemCount(), 2)
                self.assertEqual(win.remote_tree.topLevelItemCount(), 1)

                # Select local file and upload directly into remote pane
                win.local_tree.topLevelItem(1).setSelected(True)
                win.upload_selected()
                self.assertTrue(
                    until(
                        lambda: (self.app.processEvents() or True)
                        and (remote_dir / "设计方案.pdf").exists()
                        and win.remote_tree.topLevelItemCount() == 2,
                        8,
                    )
                )

                # Capture dark-mode screenshot for visual inspection
                artifacts = Path(__file__).resolve().parents[1] / "artifacts"
                artifacts.mkdir(exist_ok=True)
                self.app.processEvents()
                win.grab().save(str(artifacts / "brioche-09-file-manager-dark.png"))

                # Select remote file and download directly into local pane
                for i in range(win.remote_tree.topLevelItemCount()):
                    it = win.remote_tree.topLevelItem(i)
                    if "已有资料.txt" in it.text(0):
                        it.setSelected(True)
                win.download_selected()
                self.assertTrue(
                    until(
                        lambda: (self.app.processEvents() or True)
                        and (local_dir / "已有资料.txt").exists(),
                        8,
                    )
                )
                self.assertEqual((local_dir / "已有资料.txt").read_text(encoding="utf-8"), "remote-content")
            finally:
                win.close()
                self.app.processEvents()
                host.stop()
                host.join(5)
