import asyncio
import hashlib
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest

from brioche.file_transfer import (
    IncomingFile,
    TransferError,
    TransferJob,
    safe_name,
    validate_manifest,
)
from brioche.protocol import VERSION, pack_control, pack_file, read_message, send
from brioche.security import access_uri, client_context
from brioche.session import Host


def until(predicate, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FileSafetyTests(unittest.TestCase):
    def test_reject_path_traversal_reserved_names_and_controls(self):
        for name in (
            "../x",
            "/tmp/x",
            "..",
            "C:\\foo",
            "NUL.txt",
            "con",
            "x:stream",
            "x\n.txt",
            "x\u202eexe.txt",
            "x.",
            "x ",
        ):
            with self.subTest(name=name), self.assertRaises(TransferError):
                safe_name(name)
        self.assertEqual(safe_name("中文 测试.txt"), "中文 测试.txt")
        for items in (
            [],
            [{"name": "x", "size": -1}],
            [{"name": "x", "size": True}],
            [{"name": "x", "size": 2**50}],
        ):
            with self.assertRaises(TransferError):
                validate_manifest(items)

    def test_checksum_failure_removes_partial_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            existing = folder / "document.txt"
            existing.write_bytes(b"original")
            file = IncomingFile(folder, {"name": "document.txt", "size": 3})
            try:
                file.write(b"new")
                with self.assertRaises(TransferError):
                    file.finish("0" * 64)
            finally:
                file.close()
            self.assertEqual(existing.read_bytes(), b"original")
            self.assertEqual(list(folder.iterdir()), [existing])

    def test_completed_file_does_not_follow_or_replace_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            existing = folder / "document.txt"
            existing.write_bytes(b"original")
            file = IncomingFile(folder, {"name": "document.txt", "size": 3})
            try:
                file.write(b"new")
                saved = file.finish(hashlib.sha256(b"new").hexdigest())
            finally:
                file.close()
            self.assertNotEqual(saved, "document.txt")
            self.assertEqual(existing.read_bytes(), b"original")
            self.assertEqual((folder / saved).read_bytes(), b"new")


class FileTLSIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.input = self.folder / "input"
        self.output = self.folder / "output"
        self.input.mkdir()
        self.output.mkdir()
        self.requests = []
        self.jobs = []
        self.status = []
        self.host = Host(
            port=0,
            folder=self.folder / "state",
            files_enabled=True,
            on_transfer=self.approve,
        )
        self.host.start()
        self.assertTrue(self.host.ready.wait(5))
        self.assertIsNone(self.host.error)

    def tearDown(self):
        for job in self.jobs:
            job.stop()
            job.finished.wait(5)
        self.host.stop()
        self.host.join(8)
        self.assertFalse(self.host._thread.is_alive())
        self.temp.cleanup()

    def approve(self, request):
        self.requests.append(request)
        request.respond(
            str(self.output)
            if request.direction == "receive"
            else [str(self.input / "from-host.txt")]
        )

    def uri(self, token=None):
        return access_uri(
            "127.0.0.1", self.host.port, self.host.pin, token or self.host.token
        )

    def job(self, direction, selection, uri=None):
        job = TransferJob(uri or self.uri(), direction, selection, self.status.append)
        self.jobs.append(job)
        job.start()
        return job

    def test_upload_unicode_and_empty_files_while_video_gate_is_occupied(self):
        payload = secrets.token_bytes(170000)
        first = self.input / "测试文件.bin"
        first.write_bytes(payload)
        empty = self.input / "empty.txt"
        empty.write_bytes(b"")
        owner = object()
        self.assertTrue(self.host.session_gate.acquire(owner))
        try:
            job = self.job("upload", [str(first), str(empty)])
            self.assertTrue(job.finished.wait(10))
            self.assertTrue(until(lambda: any(v.get("finished") for v in self.status)))
            self.assertEqual((self.output / first.name).read_bytes(), payload)
            self.assertEqual((self.output / empty.name).read_bytes(), b"")
            self.assertTrue(self.host.session_gate.active)
            self.assertEqual(len(self.requests), 1)
        finally:
            self.host.session_gate.release(owner)

    def test_request_remote_selection_and_download(self):
        payload = b"from host\n" * 10000
        (self.input / "from-host.txt").write_bytes(payload)
        job = self.job("download", str(self.output))
        self.assertTrue(job.finished.wait(10))
        self.assertEqual((self.output / "from-host.txt").read_bytes(), payload)
        self.assertEqual(self.requests[0].direction, "send")

    def test_read_only_and_disabled_file_permissions_do_not_prompt_or_write(self):
        file = self.input / "x"
        file.write_bytes(b"x")
        for readonly, enabled in ((True, True), (False, False)):
            self.host.view_only, self.host.files_enabled = readonly, enabled
            job = self.job("upload", [str(file)])
            self.assertTrue(job.finished.wait(5))
        self.assertFalse(self.requests)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_bad_credentials_do_not_prompt_or_write(self):
        file = self.input / "x"
        file.write_bytes(b"x")
        job = self.job("upload", [str(file)], self.uri(secrets.token_urlsafe(32)))
        self.assertTrue(job.finished.wait(5))
        self.assertFalse(self.requests)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_pending_request_can_be_cancelled_without_leaving_files_or_gate(self):
        self.host.on_transfer = self.requests.append
        file = self.input / "x"
        file.write_bytes(b"x")
        job = self.job("upload", [str(file)])
        self.assertTrue(until(lambda: len(self.requests) == 1))
        job.stop()
        self.assertTrue(job.finished.wait(5))
        self.assertTrue(until(lambda: not self.host.file_gate.active))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cancel_in_flight_removes_partial_file(self):
        file = self.input / "large.bin"
        file.write_bytes(secrets.token_bytes(2 * 1024 * 1024))
        job = self.job("upload", [str(file)])
        self.assertTrue(
            until(lambda: any(value.get("completed", 0) > 0 for value in self.status))
        )
        job.stop()
        self.assertTrue(job.finished.wait(5))
        self.assertTrue(until(lambda: not self.host.file_gate.active))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_malicious_offer_is_rejected_before_consent(self):
        async def attack():
            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                self.host.port,
                ssl=client_context(),
                server_hostname="brioche",
            )
            try:
                await send(
                    writer,
                    pack_control(
                        {
                            "type": "file_transfer",
                            "version": VERSION,
                            "token": self.host.token,
                            "direction": "upload",
                            "files": [{"name": "../escaped", "size": 1}],
                        }
                    ),
                )
                kind, result = await asyncio.wait_for(read_message(reader), 3)
                self.assertEqual(result["type"], "file_error")
            finally:
                writer.close()
                await writer.wait_closed()

        asyncio.run(attack())
        self.assertFalse(self.requests)
        self.assertFalse((self.folder / "escaped").exists())
