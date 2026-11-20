"""Opt-in Xorg source-mode leases, with guarded restore and crash recovery.

No desktop mode is changed for ordinary video scaling. Commands are structured
arguments, mode names come from RandR, and the framebuffer/layout is retained.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .device_store import private_json
from .protocol import RESOLUTIONS
from .security import data_dir


def supported():
    return (
        sys.platform.startswith("linux")
        and os.environ.get("XDG_SESSION_TYPE") != "wayland"
        and bool(shutil.which("xrandr"))
    )


def parse_randr(value):
    screen = re.search(r"current (\d+) x (\d+)", value)
    if not screen:
        raise RuntimeError("无法读取 Xorg 桌面尺寸")
    state = {"framebuffer": [int(screen[1]), int(screen[2])], "outputs": []}
    output = None
    for line in value.splitlines():
        header = re.match(
            r"^(\S+) connected(?: primary)? (\d+)x(\d+)([+-]\d+)([+-]\d+)(?: (normal|left|right|inverted))?",
            line,
        )
        if header:
            output = {
                "name": header[1],
                "width": int(header[2]),
                "height": int(header[3]),
                "left": int(header[4]),
                "top": int(header[5]),
                "rotation": header[6] or "normal",
                "modes": [],
            }
            state["outputs"].append(output)
        elif line and not line[0].isspace():
            output = None
        elif output:
            mode = re.match(r"^\s+(\d+x\d+[\w.-]*)\s+(.+)$", line)
            if not mode:
                continue
            size = re.match(r"(\d+)x(\d+)", mode[1])
            rates = []
            for token in mode[2].split():
                rate = re.fullmatch(r"(\d+(?:\.\d+)?)([+*]*)", token)
                if rate:
                    rates.append(float(rate[1]))
                    if "*" in rate[2]:
                        output.update(mode=mode[1], rate=float(rate[1]))
            if rates:
                output["modes"].append(
                    {
                        "name": mode[1],
                        "width": int(size[1]),
                        "height": int(size[2]),
                        "rates": rates,
                    }
                )
    return state


class RandR:
    def snapshot(self):
        value = subprocess.run(
            ["xrandr", "--query"], capture_output=True, text=True, timeout=5, check=True
        )
        return parse_randr(value.stdout)

    def apply(self, output, framebuffer):
        if (
            not re.fullmatch(r"[A-Za-z][\w.-]{0,63}", output["name"])
            or not re.fullmatch(r"\d+x\d+[\w.-]*", output["mode"])
            or output["rotation"] not in ("normal", "left", "right", "inverted")
            or not 0 < float(output["rate"]) <= 1000
        ):
            raise ValueError("无效显示模式")
        for dimension in framebuffer:
            if type(dimension) is not int or not 0 < dimension <= 32767:
                raise ValueError("无效桌面尺寸")
        subprocess.run(
            [
                "xrandr",
                "--fb",
                f"{framebuffer[0]}x{framebuffer[1]}",
                "--output",
                output["name"],
                "--mode",
                output["mode"],
                "--rate",
                str(output["rate"]),
                "--rotate",
                output["rotation"],
                "--pos",
                f"{int(output['left'])}x{int(output['top'])}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )


def choose_mode(output, resolution, fps):
    if resolution == "native":
        return None
    width, height = RESOLUTIONS[resolution]
    portrait = output["height"] > output["width"]
    if portrait:
        width, height = height, width
    if output["width"] <= width and output["height"] <= height:
        return None
    candidates = []
    aspect = output["width"] / output["height"]
    for mode in output["modes"]:
        w, h = mode["width"], mode["height"]
        if output["rotation"] in ("left", "right"):
            w, h = h, w
        if w <= width and h <= height and abs(w / h / aspect - 1) <= 0.02:
            candidates.append((w * h, mode, w, h))
    if not candidates:
        raise RuntimeError("该屏幕没有可用的目标分辨率，请选择仅缩放视频")
    _, mode, w, h = max(candidates, key=lambda item: item[0])
    ceiling = max(output["rate"], fps)
    rates = [rate for rate in mode["rates"] if rate <= ceiling + 0.5]
    chosen = deepcopy(output)
    chosen.update(
        mode=mode["name"],
        rate=max(rates) if rates else min(mode["rates"]),
        width=w,
        height=h,
    )
    return chosen


def same_mode(first, second):
    return (
        all(
            first.get(key) == second.get(key)
            for key in ("name", "mode", "rotation", "left", "top")
        )
        and abs(first.get("rate", 0) - second.get("rate", 0)) < 0.5
    )


class DisplayLease:
    def __init__(self, folder=None, backend=None):
        if backend is None and not supported():
            raise RuntimeError("源屏分辨率匹配当前支持 Ubuntu Xorg；请选择仅缩放视频")
        import fcntl

        self.folder = Path(folder) if folder else data_dir()
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.folder / "display-restore.json"
        self.backend = backend or RandR()
        self.lock = (self.folder / "display-mode.lock").open("a")
        os.chmod(self.folder / "display-mode.lock", 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("已有会话正在调整源屏分辨率")
        self.entries = []
        self.original_fb = None
        try:
            self._recover()
        except Exception:
            self.lock.close()
            raise

    def _recover(self):
        if not self.path.exists():
            return
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.entries, self.original_fb = saved["entries"], saved["framebuffer"]
        self.restore()

    def _save(self):
        if self.entries:
            private_json(
                self.path, {"entries": self.entries, "framebuffer": self.original_fb}
            )
        else:
            self.path.unlink(missing_ok=True)

    def apply(self, monitor, resolution, fps):
        snapshot = self.backend.snapshot()
        outputs = [
            o
            for o in snapshot["outputs"]
            if all(o[k] == monitor[k] for k in ("left", "top", "width", "height"))
        ]
        output = outputs[0] if len(outputs) == 1 else None
        if not output or "mode" not in output:
            raise RuntimeError("无法准确识别要调整的显示器，未改变本机分辨率")
        chosen = choose_mode(output, resolution, fps)
        if chosen is None:
            return
        if self.original_fb is None:
            self.original_fb = snapshot["framebuffer"]
        original = deepcopy(output)
        self.entries.append({"original": original, "applied": chosen})
        self._save()  # Recoverable even if the process terminates during modeset.
        try:
            self.backend.apply(chosen, self.original_fb)
            current = self.backend.snapshot()
            actual = next(
                (o for o in current["outputs"] if o["name"] == chosen["name"]), None
            )
            if not actual or not same_mode(actual, chosen):
                raise RuntimeError("源屏模式未成功应用")
        except Exception:
            try:
                self.backend.apply(original, self.original_fb)
            except Exception:
                pass
            else:
                self.entries.pop()
                self._save()
            raise RuntimeError("无法切换源屏分辨率，已尝试恢复；请使用仅缩放视频")

    def restore(self):
        remaining = []
        for entry in reversed(self.entries):
            try:
                snapshot = self.backend.snapshot()
                current = next(
                    (
                        o
                        for o in snapshot["outputs"]
                        if o["name"] == entry["applied"]["name"]
                    ),
                    None,
                )
                if current is None:
                    remaining.append(entry)
                elif same_mode(current, entry["applied"]):
                    # Keep any larger framebuffer required by newly attached displays.
                    fb = [
                        max(a, b)
                        for a, b in zip(self.original_fb, snapshot["framebuffer"])
                    ]
                    self.backend.apply(entry["original"], fb)
                # A manual mode/layout change wins over automatic restoration.
            except Exception:
                remaining.append(entry)
        self.entries = list(reversed(remaining))
        self._save()
        if remaining:
            raise RuntimeError("临时分辨率未完全恢复，下次启动 Brioche 时将重试")

    def close(self):
        if self.lock.closed:
            return
        try:
            self.restore()
        finally:
            self.lock.close()


def recover(folder):
    if not (Path(folder) / "display-restore.json").exists() or not supported():
        return
    lease = DisplayLease(folder)
    lease.close()
