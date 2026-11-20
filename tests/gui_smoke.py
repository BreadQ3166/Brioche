"""Run via scripts/run.sh equivalent library path. Captures only our own windows."""
from pathlib import Path
import tempfile
import time
from unittest.mock import patch
from test_multiscreen import MONITORS, RegionCapture
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from brioche.ui import MainWindow, RemoteWindow, STYLE
from brioche.protocol import StreamConfig
from brioche.security import access_uri
from brioche.session import Host

app = QApplication([])
app.setStyle("Fusion")
app.setStyleSheet(STYLE)
root = Path(__file__).resolve().parents[1]
output = root / "artifacts"
output.mkdir(exist_ok=True)
folder = tempfile.TemporaryDirectory()
topology = patch("brioche.capture.list_monitors", return_value=MONITORS[:2])
backend = patch("brioche.media.DesktopCapture", RegionCapture)
topology.start()
backend.start()
host = Host(port=0, folder=folder.name, view_only=True)
host.start()
assert host.ready.wait(10) and not host.error, host.error
main = MainWindow()
main.show()
remote = RemoteWindow(access_uri("127.0.0.1", host.port, host.pin, host.token), StreamConfig("1440p", 90, 20))
remote.show()
started = time.monotonic()
result = {"ok": False, "switched": False}

def inspect():
    if remote.canvas.image is None and time.monotonic() - started < 12:
        return
    if not result["switched"] and remote.canvas.image is not None:
        result["switched"] = True
        index = remote.monitor_selector.findData(2)
        remote.monitor_selector.setCurrentIndex(index)
        remote.switch_monitor(index)
        return
    if (remote.canvas.image is None or remote.canvas.image.height() != 1280) and time.monotonic() - started < 12:
        return
    result["ok"] = remote.canvas.image is not None and remote.canvas.image.height() == 1280
    main.grab().save(str(output / "brioche-running.png"))
    remote.grab().save(str(output / "brioche-live-session.png"))
    main.pages.setCurrentIndex(1)
    main.web_enabled.setChecked(True)
    app.processEvents()
    main.grab().save(str(output / "brioche-sharing.png"))
    remote.close()
    main.close()
    host.stop()
    host.join()
    timer.stop()
    app.quit()

timer = QTimer()
timer.setInterval(1500)
timer.timeout.connect(inspect)
timer.start()
app.exec()
folder.cleanup()
topology.stop()
backend.stop()
assert result["ok"], "No remote frame reached the Qt canvas"
print("GUI, TLS/H.264 canvas and toolbar portrait display switch passed")
