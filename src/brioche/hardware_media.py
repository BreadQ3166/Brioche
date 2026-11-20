"""Optional NVENC process backend: capture/scale/encode outside Python frame copies."""

from collections import deque
from fractions import Fraction
from functools import lru_cache
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import av
from .capture import list_monitors
from .h264 import annexb_access_unit
from .protocol import RESOLUTIONS


def executable():
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    root = Path(
        getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2] / "runtime")
    )
    bundled = root / "media" / name
    return str(bundled) if bundled.is_file() else shutil.which(name)


@lru_cache(maxsize=4)
def nvenc_available(path):
    if not path:
        return False
    try:
        result = subprocess.run(
            [
                path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=size=320x240:rate=30:duration=0.1",
                "-frames:v",
                "1",
                "-an",
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p1",
                "-tune",
                "ull",
                "-f",
                "null",
                "-",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class HardwareEncoder:
    # FFmpeg's input owns the capture clock. A second sender sleep builds lag.
    paced = True

    def __init__(self, config, monitor_index=1, path=None):
        self.path = path or executable()
        monitors = list_monitors()
        if not 1 <= monitor_index <= len(monitors):
            raise ValueError("显示器不存在")
        self.monitor = {
            k: v
            for k, v in monitors[monitor_index - 1].items()
            if k in ("left", "top", "width", "height")
        }
        max_w, max_h = RESOLUTIONS[config.resolution]
        if self.monitor["height"] > self.monitor["width"]:
            max_w, max_h = max_h, max_w
        scale = min(max_w / self.monitor["width"], max_h / self.monitor["height"], 1.0)
        self.width = max(2, int(self.monitor["width"] * scale) // 2 * 2)
        self.height = max(2, int(self.monitor["height"] * scale) // 2 * 2)
        self.encoder_name = "NVENC · FFmpeg"
        self.codec_name = "h264"
        self.codec = SimpleNamespace(framerate=Fraction(config.fps, 1))
        self.capture = SimpleNamespace(
            backend=(
                "ffmpeg-x11grab"
                if sys.platform.startswith("linux")
                else "ffmpeg-gdigrab"
            )
        )
        self.fallback_reason = ""
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.packets = queue.Queue(maxsize=2)
        self.error = None
        self.logs = deque(maxlen=8)
        self.container = None
        command = [
            self.path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-filter_threads",
            str(min(8, os.cpu_count() or 1)),
        ]
        m = self.monitor
        if sys.platform.startswith("linux"):
            if os.environ.get("XDG_SESSION_TYPE") == "wayland":
                raise RuntimeError("NVENC 采集后端当前需要 Xorg")
            command += [
                "-f",
                "x11grab",
                "-framerate",
                str(config.fps),
                "-video_size",
                f"{m['width']}x{m['height']}",
                "-draw_mouse",
                "1",
                "-i",
                f"{os.environ.get('DISPLAY',':0')}+{m['left']},{m['top']}",
            ]
        elif sys.platform == "win32":
            command += [
                "-f",
                "gdigrab",
                "-framerate",
                str(config.fps),
                "-offset_x",
                str(m["left"]),
                "-offset_y",
                str(m["top"]),
                "-video_size",
                f"{m['width']}x{m['height']}",
                "-draw_mouse",
                "1",
                "-i",
                "desktop",
            ]
        else:
            raise RuntimeError("此平台尚未实现 NVENC 采集后端")
        command += [
            "-an",
            "-vf",
            f"scale={self.width}:{self.height}:flags=fast_bilinear,format=nv12",
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p1",
            "-tune",
            "ull",
            "-zerolatency",
            "1",
            "-rc",
            "cbr",
            "-b:v",
            f"{config.bitrate_mbps}M",
            "-maxrate",
            f"{config.bitrate_mbps}M",
            "-bufsize",
            str(max(100000, config.bitrate_mbps * 1_000_000 // config.fps)),
            "-bf",
            "0",
            "-g",
            str(max(1, config.fps // 4)),
            "-forced-idr",
            "1",
            "-flush_packets",
            "1",
            "-f",
            "nut",
            "pipe:1",
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

        def errors():
            for line in self.process.stderr:
                self.logs.append(line.decode("utf-8", errors="replace").strip())

        threading.Thread(target=errors, name="brioche-nvenc-log", daemon=True).start()
        self.reader = threading.Thread(
            target=self._read, name="brioche-nvenc-stream", daemon=True
        )
        self.reader.start()
        if not self.ready.wait(5) or self.error:
            self.close()
            raise RuntimeError("NVENC 采集编码启动失败")

    def _read(self):
        try:
            self.container = av.open(
                self.process.stdout,
                format="nut",
                buffer_size=4096,
                options={"probesize": "4096", "analyzeduration": "0"},
            )
            stream = self.container.streams.video[0]
            for packet in self.container.demux(stream):
                if self.closed.is_set():
                    break
                if not packet.size:
                    continue
                data = annexb_access_unit(
                    bytes(packet), stream.codec_context.extradata, packet.is_keyframe
                )
                while not self.closed.is_set():
                    try:
                        self.packets.put(data, timeout=0.1)
                        self.ready.set()
                        break
                    except queue.Full:
                        pass
        except Exception:
            if not self.closed.is_set():
                self.error = "硬件视频流中断"
        finally:
            if not self.closed.is_set():
                self.error = self.error or "硬件编码进程已结束"
            self.ready.set()
            if self.container:
                self.container.close()

    def frame(self, force_keyframe=False):
        # The independent backend emits complete IDRs at most 250ms apart.
        # Do not discard encoded references; bounded pipe/queue backpressure keeps the chain intact.
        start = time.perf_counter()
        if self.closed.is_set():
            raise RuntimeError("硬件编码已停止")
        try:
            data = self.packets.get(timeout=4)
        except queue.Empty:
            raise RuntimeError(self.error or "硬件编码取帧超时")
        if data is None:
            raise RuntimeError("硬件编码已停止")
        return [data], (time.perf_counter() - start) * 1000

    def interrupt(self):
        self.closed.set()
        if hasattr(self, "process") and self.process.poll() is None:
            self.process.terminate()
        try:
            self.packets.put_nowait(None)
        except queue.Full:
            pass

    def close(self):
        self.interrupt()
        if hasattr(self, "process"):
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            if (
                hasattr(self, "reader")
                and self.reader is not threading.current_thread()
            ):
                self.reader.join(2)
            for stream in (self.process.stdout, self.process.stderr):
                if stream:
                    stream.close()
