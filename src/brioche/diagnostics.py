import json
import os
import platform
import shutil
import subprocess


def doctor():
    import av
    from .protocol import ENCODERS
    result = {
        "system": platform.system(), "release": platform.release(),
        "python": platform.python_version(), "session": os.environ.get("XDG_SESSION_TYPE", "native"),
        "display": os.environ.get("DISPLAY", "native"),
        "encoders": {}, "notes": ["编码器已编译不代表此设备可以使用；请实际连接验证。"],
    }
    for name in ENCODERS:
        try:
            av.Codec(name, "w")
            result["encoders"][name] = "compiled"
        except Exception:
            result["encoders"][name] = "unavailable"
    try:
        import mss
        with mss.mss() as capture:
            result["monitors"] = capture.monitors[1:]
            shot = capture.grab(capture.monitors[1])
            result["capture"] = f"ok: {shot.width}x{shot.height}"
    except Exception as exc:
        result["capture"] = f"failed: {exc}"
    executable = shutil.which("tailscale")
    if executable:
        try:
            proc = subprocess.run([executable, "ip", "-4"], capture_output=True, text=True, timeout=4)
            result["tailscale_ip"] = proc.stdout.strip() if proc.returncode == 0 else "not connected"
        except (OSError, subprocess.TimeoutExpired):
            result["tailscale_ip"] = "unavailable"
    else:
        result["tailscale_ip"] = "not installed (optional)"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if str(result["capture"]).startswith("ok:") else 1
