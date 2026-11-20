"""Local capture/encode/TLS/decode smoke benchmark. Not a high-refresh certification."""
import argparse
import json
import platform
import tempfile
import time
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from brioche.protocol import StreamConfig
from brioche.security import access_uri
from brioche.session import Host, Viewer

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=int, default=8)
parser.add_argument("--fps", type=int, default=90)
parser.add_argument("--output")
parser.add_argument("--encoder", choices=("auto", "libx264", "h264_nvenc"), default="libx264")
parser.add_argument("--binary", type=Path, help="Use an installed host executable instead of source Host")
args = parser.parse_args()
frames, stats, info, errors = [], [], [], []
with tempfile.TemporaryDirectory() as folder:
    host, process, viewer = None, None, None
    try:
        if args.binary:
            process = subprocess.Popen([str(args.binary), "host", "--port", "0", "--view-only",
                                        "--state-dir", folder, "--encoder", args.encoder], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            def read_uri():
                for line in process.stdout:
                    if line.startswith("brioche://"):
                        return line.strip()
                raise RuntimeError("Installed host exited before readiness")
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(read_uri)
                try:
                    uri = future.result(timeout=15)
                except Exception:
                    process.terminate()
                    raise
        else:
            host = Host(port=0, folder=folder, view_only=True, encoder=args.encoder)
            host.start()
            if not host.ready.wait(10) or host.error:
                raise RuntimeError(host.error or "Host startup timed out")
            uri = access_uri("127.0.0.1", host.port, host.pin, host.token)
        viewer = Viewer(uri, StreamConfig("1440p", args.fps, 20),
                        lambda frame: frames.append(time.monotonic()), errors.append, info.append, stats.append)
        viewer.start()
        time.sleep(args.seconds)
    finally:
        if viewer:
            viewer.stop()
            viewer.join()
        if host:
            host.stop()
            host.join()
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            stderr = process.stderr.read()
            if stderr or process.returncode:
                errors.append(f"Host exit {process.returncode}: {stderr[:1000]}")
report = {
    "platform": platform.platform(), "requested": f"1440p / {args.fps} FPS",
    "test": "same-machine loopback; actual screen capture; H.264; no GUI presentation",
    "requested_encoder": args.encoder,
    "host_binary": str(args.binary) if args.binary else "source",
    "duration_seconds": args.seconds,
    "decoded_frames": len(frames),
    "average_decoded_fps": round((len(frames)-1)/(frames[-1]-frames[0]), 2) if len(frames)>1 else 0,
    "stream_info": [i for i in info if i.get("type") == "welcome"],
    "samples": stats, "status": errors,
    "limitations": "Not a 30-minute high-refresh acceptance test or a cross-machine latency measurement.",
}
serialized = json.dumps(report, ensure_ascii=False, indent=2)
if args.output:
    from pathlib import Path
    Path(args.output).write_text(serialized + "\n", encoding="utf-8")
print(serialized)
raise SystemExit(0 if frames else 1)
