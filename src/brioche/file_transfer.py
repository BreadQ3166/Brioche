"""Consent-based, bounded file & folder batches on a separate authenticated TLS channel."""

import asyncio
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
import threading
import time
import unicodedata

from .protocol import (
    CONTROL,
    FILE,
    VERSION,
    MAX_FILE_CHUNK,
    pack_control,
    pack_file,
    read_message,
    send,
)
from .security import client_context, parse_uri, verify_pin

MAX_FILES = 512
MAX_DEPTH = 16
MAX_BATCH_SIZE = 64 * 1024**3
RATE_LIMIT = 20 * 1024 * 1024  # 20 MiB/s default; user-configurable (0 = unlimited).
RATE_PRESETS = (
    ("极速 · 不限速", 0),
    ("高速 · 20 MiB/s（推荐）", 20 * 1024 * 1024),
    ("均衡 · 8 MiB/s", 8 * 1024 * 1024),
    ("低干扰 · 2 MiB/s", 2 * 1024 * 1024),
)


class TransferError(ValueError):
    pass


def safe_name(name):
    if (
        not isinstance(name, str)
        or not name
        or len(name.encode("utf-8")) > 240
        or name in (".", "..")
        or name.endswith((" ", "."))
        or any(c in name for c in '/\\:<>"|?*')
        or any(unicodedata.category(c).startswith("C") for c in name)
    ):
        raise TransferError("文件名不受支持，请重命名后发送")
    name = unicodedata.normalize("NFC", name)
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)
    }
    if name.split(".")[0].upper() in reserved:
        raise TransferError("文件名与系统保留名称冲突")
    return name


def safe_relpath(relpath):
    """Validate an optional forward-slash relative path for folder transfers."""
    if not isinstance(relpath, str) or not relpath:
        raise TransferError("无效相对路径")
    if relpath.startswith(("/", "\\")) or "\\" in relpath:
        raise TransferError("相对路径包含非法分隔符")
    parts = relpath.split("/")
    if len(parts) > MAX_DEPTH:
        raise TransferError("文件夹层级过深（最多 16 层）")
    clean_parts = [safe_name(part) for part in parts]
    joined = "/".join(clean_parts)
    if len(joined.encode("utf-8")) > 512:
        raise TransferError("相对路径过长")
    return joined


def validate_manifest(items):
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_FILES:
        raise TransferError(f"每次请选择 1 至 {MAX_FILES} 个文件")
    clean = []
    for item in items:
        if (
            not isinstance(item, dict)
            or type(item.get("size")) is not int
            or not 0 <= item["size"] <= MAX_BATCH_SIZE
        ):
            raise TransferError("无效文件大小")
        entry = {"name": safe_name(item.get("name")), "size": item["size"]}
        if "relpath" in item and item["relpath"] is not None:
            rel = safe_relpath(item["relpath"])
            if rel.split("/")[-1] != entry["name"]:
                raise TransferError("文件相对路径与文件名不一致")
            entry["relpath"] = rel
        clean.append(entry)
    if sum(item["size"] for item in clean) > MAX_BATCH_SIZE:
        raise TransferError("一次传输合计最多 64 GiB")
    return clean


def _collect_dir(root_dir, resolved, items):
    base_name = safe_name(root_dir.name)
    for current, dirnames, filenames in os.walk(root_dir, followlinks=False):
        current_path = Path(current)
        rel_dir = current_path.relative_to(root_dir)
        # Skip symlinked directories
        dirnames[:] = sorted(
            d for d in dirnames if not (current_path / d).is_symlink()
        )
        for fname in sorted(filenames):
            fpath = current_path / fname
            if fpath.is_symlink():
                continue
            info = fpath.stat()
            if not stat.S_ISREG(info.st_mode):
                continue
            rel_parts = [base_name] + list(rel_dir.parts) + [fname]
            rel_str = safe_relpath("/".join(rel_parts))
            resolved.append(fpath)
            items.append({"name": safe_name(fname), "relpath": rel_str, "size": info.st_size})
            if len(resolved) > MAX_FILES:
                raise TransferError(f"文件夹内文件过多，单批最多 {MAX_FILES} 个文件")


def sources(paths):
    if not isinstance(paths, (list, tuple)) or not paths:
        raise TransferError("请至少选择一个文件或文件夹")
    resolved, items = [], []
    for value in paths:
        path = Path(value).resolve(strict=True)
        info = path.stat()
        if stat.S_ISDIR(info.st_mode):
            _collect_dir(path, resolved, items)
        elif stat.S_ISREG(info.st_mode):
            resolved.append(path)
            items.append({"name": safe_name(path.name), "size": info.st_size})
        else:
            raise TransferError("仅支持普通文件与文件夹")
        if len(resolved) > MAX_FILES:
            raise TransferError(f"每次最多传输 {MAX_FILES} 个文件")
    if not resolved:
        raise TransferError("所选文件夹内没有可发送的文件")
    return resolved, validate_manifest(items)


def commit_without_overwrite(source, target):
    if os.name == "nt":
        os.rename(source, target)  # Windows rename rejects an existing destination.
        return
    try:
        os.link(source, target)
        source.unlink()
        return
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise
        if exc.errno not in (errno.EOPNOTSUPP, errno.ENOSYS, errno.EPERM, errno.EXDEV):
            raise
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = library.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    else:
        rename = library.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _safe_subdir(root_folder, relpath):
    """Resolve and create subdirectory inside root_folder without crossing symlinks."""
    rel_parts = safe_relpath(relpath).split("/")[:-1]
    current = root_folder
    for part in rel_parts:
        nxt = current / part
        if nxt.is_symlink():
            raise TransferError("目标路径包含符号链接，已拒绝写入")
        nxt.mkdir(exist_ok=True)
        resolved_nxt = nxt.resolve(strict=True)
        try:
            resolved_nxt.relative_to(root_folder)
        except ValueError as exc:
            raise TransferError("目标路径超出保存目录") from exc
        current = resolved_nxt
    return current


class IncomingFile:
    def __init__(self, folder, item):
        self.root = Path(folder).resolve(strict=True)
        self.name = safe_name(item["name"])
        self.size = item["size"]
        relpath = item.get("relpath")
        self.folder = _safe_subdir(self.root, relpath) if relpath else self.root
        self.path = self.folder / (".brioche-" + secrets.token_hex(16) + ".part")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        self.file = os.fdopen(os.open(self.path, flags, 0o600), "wb")
        self.hash, self.received = hashlib.sha256(), 0

    def write(self, data):
        if not data or self.received + len(data) > self.size:
            raise TransferError("文件数据超出声明大小")
        self.file.write(data)
        self.hash.update(data)
        self.received += len(data)

    def finish(self, digest):
        if (
            self.received != self.size
            or not isinstance(digest, str)
            or digest != self.hash.hexdigest()
        ):
            raise TransferError("文件校验失败，未保存不完整文件")
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        name = Path(self.name)
        for counter in range(1000):
            destination = self.folder / (
                self.name if counter == 0 else f"{name.stem} ({counter}){name.suffix}"
            )
            try:
                commit_without_overwrite(self.path, destination)
                return str(destination.relative_to(self.root))
            except FileExistsError:
                continue
        raise TransferError("同名文件过多，请选择其他保存目录")

    def close(self):
        try:
            self.file.close()
        finally:
            self.path.unlink(missing_ok=True)


class FileConsent:
    def __init__(self, direction, peer, items=None):
        self.id = secrets.token_hex(12)
        self.direction, self.peer, self.items = direction, peer, items or []
        self.done = threading.Event()
        self.value = None
        self.lock = threading.Lock()
        self.loop = self.task = None

    def respond(self, value=None):
        with self.lock:
            if not self.done.is_set():
                self.value = value
                self.done.set()

    def cancel(self):
        self.respond()
        if self.loop and self.task and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.task.cancel)

    async def wait(self, reader, stopped):
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        deadline = time.monotonic() + 120
        while not self.done.is_set():
            if stopped.is_set() or reader.at_eof() or time.monotonic() > deadline:
                self.respond()
                raise TransferError("文件请求已取消或超时")
            await asyncio.sleep(0.1)
        if not self.value:
            raise TransferError("对方取消了文件传输")
        return self.value


async def control(reader, expected):
    kind, value = await asyncio.wait_for(read_message(reader), 130)
    if kind != CONTROL:
        raise TransferError("无效文件传输响应")
    if value.get("type") in ("file_error", "error"):
        raise TransferError(str(value.get("message", "文件传输被拒绝"))[:200])
    if value.get("type") != expected:
        raise TransferError("两端文件传输协议不兼容，请都更新 Brioche")
    return value


async def send_files(reader, writer, paths, items, progress, rate=RATE_LIMIT):
    await control(reader, "file_ready")
    total = sum(item["size"] for item in items)
    transferred, started = 0, time.monotonic()
    for path, item in zip(paths, items):
        opening = asyncio.create_task(asyncio.to_thread(path.open, "rb"))
        try:
            file = await asyncio.shield(opening)
        except asyncio.CancelledError:
            orphan = await opening
            await asyncio.to_thread(orphan.close)
            raise
        try:
            before = os.fstat(file.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != item["size"]:
                raise TransferError("源文件已改变，请重新选择")
            digest, count = hashlib.sha256(), 0
            while count < item["size"]:
                data = await asyncio.to_thread(
                    file.read, min(MAX_FILE_CHUNK, item["size"] - count)
                )
                if not data:
                    raise TransferError("源文件在传输中被修改")
                digest.update(data)
                await send(writer, pack_file(data))
                count += len(data)
                transferred += len(data)
                progress(transferred, total, item.get("relpath") or item["name"])
                if rate and rate > 0:
                    await asyncio.sleep(
                        max(0, transferred / rate - (time.monotonic() - started))
                    )
            after = os.fstat(file.fileno())
            if (after.st_size, after.st_mtime_ns) != (
                before.st_size,
                before.st_mtime_ns,
            ):
                raise TransferError("源文件在传输中被修改")
            await send(
                writer, pack_control({"type": "file_end", "sha256": digest.hexdigest()})
            )
            await control(reader, "file_saved")
        finally:
            await asyncio.to_thread(file.close)
    await send(writer, pack_control({"type": "file_batch_done"}))
    await control(reader, "file_batch_ack")
    progress(total, total, "完成")


async def receive_files(reader, writer, folder, items, progress):
    folder = Path(folder).resolve(strict=True)
    total = sum(item["size"] for item in items)
    if not folder.is_dir() or shutil.disk_usage(folder).free < total + 8 * 1024**2:
        raise TransferError("目标磁盘空间不足或保存目录不可用")
    await send(writer, pack_control({"type": "file_ready"}))
    transferred, saved = 0, []
    for item in items:
        opening = asyncio.create_task(asyncio.to_thread(IncomingFile, folder, item))
        try:
            incoming = await asyncio.shield(opening)
        except asyncio.CancelledError:
            orphan = await opening
            await asyncio.to_thread(orphan.close)
            raise
        try:
            while incoming.received < item["size"]:
                kind, data = await asyncio.wait_for(read_message(reader), 30)
                if kind != FILE:
                    raise TransferError("文件提前结束或连接中断")
                await asyncio.to_thread(incoming.write, data)
                transferred += len(data)
                progress(transferred, total, item.get("relpath") or item["name"])
            ending = await control(reader, "file_end")
            finishing = asyncio.create_task(
                asyncio.to_thread(incoming.finish, ending.get("sha256"))
            )
            try:
                name = await asyncio.shield(finishing)
            except asyncio.CancelledError:
                await finishing
                raise
            saved.append(name)
            await send(writer, pack_control({"type": "file_saved", "name": name}))
        finally:
            await asyncio.to_thread(incoming.close)
    await control(reader, "file_batch_done")
    await send(writer, pack_control({"type": "file_batch_ack"}))
    progress(total, total, "完成")
    return saved


class TransferJob:
    def __init__(
        self,
        uri,
        direction,
        selection,
        update=lambda value: None,
        rate=RATE_LIMIT,
        target_dir=None,
        remote_paths=None,
    ):
        self.uri, self.direction, self.selection, self.update = (
            uri,
            direction,
            selection,
            update,
        )
        self.rate = rate
        self.target_dir = target_dir
        self.remote_paths = list(remote_paths) if remote_paths else None
        self.loop = self.task = None
        self.finished = threading.Event()
        self.stopped = threading.Event()
        self.id = secrets.token_hex(12)
        self._last_progress = 0.0
        self._started_at = 0.0

    def start(self):
        threading.Thread(target=self._run, name="brioche-files", daemon=True).start()

    def stop(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        if self.loop and self.task and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.task.cancel)

    def progress(self, completed, total, name):
        now = time.monotonic()
        if now - self._last_progress >= 0.1 or name == "完成":
            self._last_progress = now
            elapsed = max(0.05, now - self._started_at) if self._started_at else 1.0
            speed_bps = int(completed / elapsed) if completed else 0
            self.update(
                {
                    "id": self.id,
                    "completed": completed,
                    "total": total,
                    "name": name,
                    "speed_bps": speed_bps,
                    "status": "传输中",
                }
            )

    def _run(self):
        self._started_at = time.monotonic()
        initial_status = (
            "正在连接远端目录…"
            if (self.target_dir or self.remote_paths)
            else "等待对方确认…"
        )
        self.update({"id": self.id, "status": initial_status})
        try:
            asyncio.run(self._connect())
            status = "已完成 · SHA256 校验通过"
        except asyncio.CancelledError:
            status = "已取消，未完成文件已移除"
        except Exception as exc:
            status = (
                str(exc)[:200]
                if isinstance(exc, TransferError)
                else "传输中断，请检查连接、权限或磁盘空间"
            )
        try:
            self.update({"id": self.id, "status": status, "finished": True})
        finally:
            self.finished.set()

    async def _connect(self):
        from .session import close_writer

        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        if self.stopped.is_set():
            raise asyncio.CancelledError
        host, port, pin, token = parse_uri(self.uri)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                ssl=client_context(),
                server_hostname="brioche",
                ssl_handshake_timeout=5,
            ),
            10,
        )
        try:
            verify_pin(writer.get_extra_info("ssl_object"), pin)
            message = {
                "type": "file_transfer",
                "version": VERSION,
                "token": token,
                "direction": self.direction,
            }
            if self.direction == "upload":
                paths, items = await asyncio.to_thread(sources, self.selection)
                message["files"] = items
                if self.target_dir:
                    message["target_dir"] = self.target_dir
            elif self.remote_paths:
                message["remote_paths"] = self.remote_paths
            await send(writer, pack_control(message))
            if self.direction == "upload":
                await send_files(reader, writer, paths, items, self.progress, rate=self.rate)
            else:
                offer = await control(reader, "file_offer")
                items = validate_manifest(offer.get("files"))
                await receive_files(
                    reader, writer, self.selection, items, self.progress
                )
        finally:
            await close_writer(writer)
