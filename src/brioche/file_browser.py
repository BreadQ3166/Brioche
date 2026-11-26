"""Authenticated remote directory browser client & host handlers over pinned TLS."""

import asyncio
import os
from pathlib import Path
import platform
import queue
import stat
import sys
import threading

from .file_transfer import TransferError, safe_name
from .protocol import CONTROL, VERSION, ProtocolError, pack_control, read_message, send
from .security import client_context, parse_uri, verify_pin

MAX_LIST_ENTRIES = 2000


def local_roots():
    """Return quick-access root/shortcut folders for the current OS."""
    home = Path.home()
    roots = [{"name": "主目录", "path": str(home)}]
    for label, sub in (("桌面", "Desktop"), ("下载", "Downloads"), ("文档", "Documents")):
        candidate = home / sub
        if candidate.is_dir():
            roots.append({"name": label, "path": str(candidate)})
    if sys.platform == "win32":
        import string

        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                roots.append({"name": f"{letter}: 盘", "path": drive})
    else:
        roots.append({"name": "根目录 /", "path": "/"})
        if Path("/media").is_dir():
            roots.append({"name": "外置磁盘 /media", "path": "/media"})
        if Path("/Volumes").is_dir():
            roots.append({"name": "卷宗 /Volumes", "path": "/Volumes"})
    return roots


def _resolve_dir(raw_path):
    if not raw_path:
        target = Path.home()
    else:
        if not isinstance(raw_path, str) or len(raw_path) > 1024:
            raise TransferError("目录路径无效")
        target = Path(raw_path).expanduser()
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise TransferError(f"无法访问目录：{exc.strerror or exc}") from exc
    if not resolved.is_dir():
        raise TransferError("目标路径不是文件夹")
    return resolved


def list_directory(raw_path=""):
    directory = _resolve_dir(raw_path)
    entries = []
    try:
        with os.scandir(directory) as it:
            for idx, entry in enumerate(it):
                if idx >= MAX_LIST_ENTRIES:
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                is_dir = stat.S_ISDIR(info.st_mode)
                is_reg = stat.S_ISREG(info.st_mode)
                if not is_dir and not is_reg:
                    continue
                entries.append(
                    {
                        "name": entry.name,
                        "path": str(directory / entry.name),
                        "is_dir": is_dir,
                        "size": 0 if is_dir else int(info.st_size),
                        "mtime": int(info.st_mtime),
                    }
                )
    except OSError as exc:
        raise TransferError(f"无法读取目录内容：{exc.strerror or exc}") from exc

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    parent = str(directory.parent) if directory.parent != directory else ""
    return {
        "path": str(directory),
        "parent": parent,
        "entries": entries,
    }


def make_directory(parent_path, name):
    parent = _resolve_dir(parent_path)
    clean = safe_name(name)
    target = parent / clean
    if target.exists() or target.is_symlink():
        raise TransferError("同名文件或文件夹已存在")
    try:
        target.mkdir(exist_ok=False)
    except OSError as exc:
        raise TransferError(f"新建文件夹失败：{exc.strerror or exc}") from exc
    return str(target)


def rename_entry(raw_path, new_name):
    if not isinstance(raw_path, str) or not raw_path:
        raise TransferError("源路径无效")
    src = Path(raw_path).resolve(strict=True)
    if src == src.parent or src == Path.home().resolve():
        raise TransferError("不允许重命名根目录或主目录")
    clean = safe_name(new_name)
    dst = src.parent / clean
    if dst.exists() or dst.is_symlink():
        raise TransferError("目标名称已存在")
    try:
        src.rename(dst)
    except OSError as exc:
        raise TransferError(f"重命名失败：{exc.strerror or exc}") from exc
    return str(dst)


def delete_entry(raw_path):
    if not isinstance(raw_path, str) or not raw_path:
        raise TransferError("目标路径无效")
    target = Path(raw_path)
    if target.is_symlink():
        raise TransferError("拒绝删除符号链接")
    resolved = target.resolve(strict=True)
    if resolved == resolved.parent or resolved == Path.home().resolve():
        raise TransferError("禁止删除系统根目录或主目录")
    try:
        if resolved.is_dir():
            resolved.rmdir()
        else:
            resolved.unlink()
    except OSError as exc:
        if resolved.is_dir():
            raise TransferError("仅支持删除空文件夹，请先清空目录内容") from exc
        raise TransferError(f"删除失败：{exc.strerror or exc}") from exc
    return True


def handle_browser_command(message):
    kind = message.get("type")
    if kind == "fb_list":
        data = list_directory(message.get("path", ""))
        return {"type": "fb_entries", **data}
    if kind == "fb_mkdir":
        created = make_directory(message.get("parent", ""), message.get("name", ""))
        return {"type": "fb_ok", "path": created}
    if kind == "fb_rename":
        renamed = rename_entry(message.get("path", ""), message.get("new_name", ""))
        return {"type": "fb_ok", "path": renamed}
    if kind == "fb_delete":
        delete_entry(message.get("path", ""))
        return {"type": "fb_ok"}
    raise ProtocolError("未知文件浏览操作")


class RemoteFileBrowserClient:
    """Thread-safe persistent TLS client for browsing remote directories."""

    def __init__(self, uri):
        self.uri = uri
        self._requests = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._loop = None
        self._task = None
        self.info = None
        self.error = None

    def start(self, timeout=10):
        self._thread = threading.Thread(
            target=self._run, name="brioche-file-browser", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            self.close()
            raise TransferError("连接远端文件管理器超时")
        if self.error:
            self.close()
            raise TransferError(self.error)
        return dict(self.info)

    def _run(self):
        try:
            asyncio.run(self._session())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.error = str(exc)[:220]
            self._ready.set()
        finally:
            self._drain_errors("远端文件浏览连接已关闭")

    def _drain_errors(self, message):
        while True:
            try:
                _, fut = self._requests.get_nowait()
            except queue.Empty:
                break
            if fut and not fut["done"].is_set():
                fut["error"] = message
                fut["done"].set()

    async def _session(self):
        from .session import close_writer

        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        host, port, pin, token = parse_uri(self.uri)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                ssl=client_context(),
                server_hostname="brioche",
                ssl_handshake_timeout=5,
            ),
            8,
        )
        try:
            verify_pin(writer.get_extra_info("ssl_object"), pin)
            await send(
                writer,
                pack_control({"type": "file_browser", "version": VERSION, "token": token}),
            )
            kind, reply = await asyncio.wait_for(read_message(reader), 10)
            if kind != CONTROL or reply.get("type") != "fb_ready":
                msg = reply.get("message", "对方未开启直接文件浏览权限") if kind == CONTROL else "无效响应"
                raise TransferError(msg)
            self.info = reply
            self._ready.set()

            while not self._stop.is_set():
                try:
                    payload, fut = self._requests.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.015)
                    continue
                if payload is None:
                    break
                try:
                    await send(writer, pack_control(payload))
                    kind, resp = await asyncio.wait_for(read_message(reader), 15)
                    if kind != CONTROL:
                        raise TransferError("无效目录响应")
                    if resp.get("type") in ("fb_error", "error"):
                        fut["error"] = resp.get("message", "操作失败")
                    else:
                        fut["value"] = resp
                except Exception as exc:
                    fut["error"] = str(exc)[:220]
                    fut["done"].set()
                    raise
                else:
                    fut["done"].set()
            try:
                await send(writer, pack_control({"type": "fb_bye"}))
            except Exception:
                pass
        finally:
            await close_writer(writer)

    def _call(self, payload, timeout=15):
        if self._stop.is_set() or (self._thread and not self._thread.is_alive()):
            raise TransferError("远端文件浏览连接已断开")
        fut = {"done": threading.Event(), "value": None, "error": None}
        self._requests.put((payload, fut))
        if not fut["done"].wait(timeout):
            raise TransferError("远端目录操作超时")
        if fut["error"]:
            raise TransferError(fut["error"])
        return fut["value"]

    def list_dir(self, path=""):
        return self._call({"type": "fb_list", "path": path})

    def mkdir(self, parent, name):
        return self._call({"type": "fb_mkdir", "parent": parent, "name": name})

    def rename(self, path, new_name):
        return self._call({"type": "fb_rename", "path": path, "new_name": new_name})

    def delete(self, path):
        return self._call({"type": "fb_delete", "path": path})

    def close(self):
        self._stop.set()
        self._requests.put((None, None))
        if self._loop and self._task and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(2)
