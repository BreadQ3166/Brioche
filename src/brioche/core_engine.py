"""Bridge between the Brioche desktop UI and the native Rust `brioche-core` v2.0 engine.

Handles discovery of the `brioche-core` binary, hardware capability probing
(Vulkan/Metal/DX12 GPU, HEVC/H.264 NVENC, YUV 4:4:4), supervised Host startup
with JSON handshake parsing, and launching the native `winit + wgpu` GPU Viewer.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys


@dataclass(frozen=True)
class CoreHostEndpoint:
    quic_port: int
    pin: str
    key: str
    monitor: int


def find_core_binary():
    name = "brioche-core.exe" if sys.platform == "win32" else "brioche-core"
    repo_root = Path(__file__).resolve().parents[2]
    meipass = Path(getattr(sys, "_MEIPASS", repo_root / "runtime"))
    candidates = [
        meipass / "core" / name,
        repo_root / "runtime" / "core" / name,
        repo_root / "core" / "target" / "release" / name,
        repo_root / "core" / "target" / "debug" / name,
    ]
    for path in candidates:
        if path.is_file():
            return path
    found = shutil.which(name)
    return Path(found) if found else None


def probe_core_capabilities(binary=None, timeout=6):
    exe = Path(binary) if binary else find_core_binary()
    if not exe or not exe.is_file():
        return {"available": False, "reason": "未检测到 brioche-core 原生引擎二进制"}
    try:
        proc = subprocess.run(
            [str(exe), "info"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        data = json.loads(proc.stdout)
        data["available"] = True
        data["binary"] = str(exe)
        return data
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"available": False, "binary": str(exe), "reason": str(exc)[:200]}


class CoreHostProcess:
    """Supervises a live `brioche-core host` child process (QUIC + Brioche-UDP/RS-FEC)."""

    def __init__(
        self,
        bind="127.0.0.1",
        quic_port=0,
        monitor=1,
        view_only=False,
        binary=None,
    ):
        self.binary = Path(binary) if binary else find_core_binary()
        self.bind = bind
        self.quic_port = int(quic_port)
        self.monitor = int(monitor)
        self.view_only = bool(view_only)
        self.process = None
        self.endpoint = None

    def start(self, timeout=8):
        if not self.binary or not self.binary.is_file():
            raise RuntimeError("未找到 brioche-core 原生引擎二进制")
        cmd = [
            str(self.binary),
            "host",
            "--bind",
            self.bind,
            "--quic-port",
            str(self.quic_port),
            "--monitor",
            str(self.monitor),
        ]
        if self.view_only:
            cmd.append("--view-only")
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        line = self.process.stdout.readline() if self.process.stdout else ""
        if not line:
            err = (self.process.stderr.read() if self.process.stderr else "").strip()
            self.stop()
            raise RuntimeError(f"brioche-core host 启动失败：{err or '无响应'}")
        info = json.loads(line)
        if not info.get("ready"):
            self.stop()
            raise RuntimeError("brioche-core host 未就绪")
        self.endpoint = CoreHostEndpoint(
            quic_port=int(info["quic_port"]),
            pin=str(info["pin"]),
            key=str(info["key"]),
            monitor=int(info.get("monitor", self.monitor)),
        )
        return self.endpoint

    def stop(self):
        proc, self.process = self.process, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass


def launch_core_viewer(
    host,
    quic_port,
    pin,
    key,
    title="Brioche v2.0 · GPU 极速会话",
    fps=60,
    bitrate_kbps=20000,
    chroma_444=True,
    binary=None,
):
    exe = Path(binary) if binary else find_core_binary()
    if not exe or not exe.is_file():
        raise RuntimeError("未找到 brioche-core 原生引擎二进制")
    cmd = [
        str(exe),
        "view",
        "--host",
        str(host),
        "--quic-port",
        str(int(quic_port)),
        "--pin",
        str(pin),
        "--key",
        str(key),
        "--title",
        str(title),
        "--fps",
        str(int(fps)),
        "--bitrate",
        str(int(bitrate_kbps)),
    ]
    if not chroma_444:
        cmd.append("--no-444")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def negotiate_v2_endpoint(uri, timeout=4.0):
    """Negotiate an authenticated v2.0 Rust QUIC+UDP endpoint over Brioche's pinned TLS control channel."""
    import asyncio
    from .protocol import CONTROL, VERSION, pack_control, read_message, send
    from .security import client_context, parse_uri, verify_pin

    try:
        host, port, pin, token = parse_uri(uri)
    except ValueError:
        return None

    async def _run():
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                ssl=client_context(),
                server_hostname="brioche",
                ssl_handshake_timeout=timeout,
            ),
            timeout,
        )
        try:
            verify_pin(writer.get_extra_info("ssl_object"), pin)
            await send(
                writer,
                pack_control({"type": "v2_negotiate", "version": VERSION, "token": token}),
            )
            kind, reply = await asyncio.wait_for(read_message(reader), timeout)
            if kind == CONTROL and reply.get("type") == "v2_ready":
                return (
                    host,
                    CoreHostEndpoint(
                        quic_port=int(reply["quic_port"]),
                        pin=str(reply["pin"]),
                        key=str(reply["key"]),
                        monitor=int(reply.get("monitor", 1)),
                    ),
                )
            return None
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 0.5)
            except Exception:
                pass

    try:
        return asyncio.run(_run())
    except Exception:
        return None
