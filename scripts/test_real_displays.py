"""Read-only loopback switching over actual attached monitors. No input injection."""
import json
from pathlib import Path
import tempfile
import time
from brioche.protocol import StreamConfig
from brioche.security import access_uri
from brioche.session import Host, Viewer

root = Path(__file__).resolve().parents[1]
info, frames, status, result = [], [], [], []
def until(condition, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(.02)
    return False

with tempfile.TemporaryDirectory() as state:
    host = Host(port=0, folder=state, view_only=True)
    host.start()
    assert host.ready.wait(10) and not host.error, host.error
    viewer = Viewer(access_uri("127.0.0.1", host.port, host.pin, host.token),
                    StreamConfig("1440p", 60, 16),
                    lambda f: frames.append((viewer._stream_id, f.width, f.height)),
                    status.append, info.append)
    try:
        viewer.start()
        assert until(lambda: len(frames) >= 2), status
        monitors = info[0]["monitors"]
        order = [m["id"] for m in monitors if m["id"] != info[0]["monitor"]] + [info[0]["monitor"]]
        for monitor in order:
            if monitor != info[-1].get("monitor"):
                time.sleep(.35)
                old_epoch = viewer._stream_id
                assert viewer.switch_monitor(monitor)
                assert until(lambda: viewer._stream_id > old_epoch), (status, info[-1])
            epoch = viewer._stream_id
            assert until(lambda: sum(1 for f in frames if f[0] == epoch) >= 2), status
            last = next(i for i in reversed(info) if i.get("stream_id") == epoch)
            result.append({"monitor": monitor, "stream_id": epoch, "width": last["width"], "height": last["height"]})
    finally:
        viewer.stop()
        viewer.join()
        host.stop()
        host.join()
        assert not host._thread.is_alive()
report = {"test": "actual attached displays, read-only TLS loopback, software H.264",
          "monitors": monitors, "switches": result, "pass": True}
output = root / "artifacts/real-displays-02.json"
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(report, ensure_ascii=False, indent=2))
