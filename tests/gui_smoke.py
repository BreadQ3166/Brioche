"""Run via scripts/run.sh or QT_QPA_PLATFORM=offscreen. Captures only our own windows."""
from pathlib import Path
import secrets
import tempfile
import time
from unittest.mock import patch
from test_multiscreen import MONITORS, RegionCapture
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from brioche.ui import MainWindow, RemoteWindow
from brioche.protocol import StreamConfig
from brioche.security import access_uri
from brioche.session import Host

app = QApplication([])
app.setStyle("Fusion")
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
with patch.object(MainWindow, "refresh_environment"), patch.object(MainWindow, "refresh_lan"):
    main = MainWindow(folder=Path(folder.name) / "ui")
main.show()
remote = RemoteWindow(
    access_uri("127.0.0.1", host.port, host.pin, host.token),
    StreamConfig("1440p", 90, 20),
    display_name="工作站-Ubuntu",
)
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
    main.network_poll.stop()
    main.lan_poll.stop()
    main.store.upsert(
        {
            "ip": "192.168.1.8",
            "port": 47995,
            "name": "Windows 工作电脑",
            "platform": "Windows",
            "pin": "a" * 64,
            "token": secrets.token_urlsafe(32),
        }
    )
    main.store.upsert(
        {
            "ip": "192.168.1.12",
            "port": 47995,
            "name": "办公室 Ubuntu",
            "platform": "Linux",
        }
    )
    main.store.set_device_profile("ts:100.70.1.3", group="办公室", favorite=True, fps=90)
    main.apply_environment(
        {
            "lan": [{"ip": "192.168.1.5", "name": "Ethernet"}],
            "monitors": MONITORS[:2],
            "tailscale": {
                "ready": True,
                "ips": ["100.70.1.2"],
                "message": "同一账号 · 已连接",
                "peers": [
                    {
                        "ip": "100.70.1.3",
                        "name": "Windows 工作电脑",
                        "platform": "Windows",
                        "reachable": True,
                        "sharing": True,
                        "path": "直连",
                    },
                    {
                        "ip": "100.70.1.4",
                        "name": "MacBook Pro",
                        "platform": "Darwin",
                        "reachable": True,
                        "sharing": True,
                        "busy": True,
                        "path": "DERP 中继",
                    },
                    {
                        "ip": "100.70.1.5",
                        "name": "家里的电脑",
                        "platform": "Windows",
                        "reachable": False,
                        "reported_online": False,
                    },
                ],
            },
        }
    )
    main.update_lan_list(
        [
            {"ip": "192.168.1.8", "port": 47995, "name": "Windows 工作电脑", "platform": "Windows", "online": True},
            {"ip": "192.168.1.12", "port": 47995, "name": "办公室 Ubuntu", "platform": "Linux", "online": True},
        ]
    )
    main.apply_theme("light")
    if main.device_list.count() > 0:
        main.device_list.setCurrentRow(0)
        main._device_selected(main.device_list.item(0))
    QTest.qWait(350)
    main.grab().save(str(output / "brioche-09-light.png"))

    main.apply_theme("dark")
    QTest.qWait(250)
    main.grab().save(str(output / "brioche-09-dark.png"))

    remote.session.reveal_toolbar()
    remote.session.toolbar.pin_btn.setChecked(True)
    remote.session.toolbar.metrics_btn.setChecked(True)
    remote.session.hud.update_metrics(
        remote.session.current_info,
        {"decoded_fps": 60.0, "encoded_fps": 60.0, "rtt_ms": 1.8, "mbps": 14.2,
         "capture_encode_ms": 4.2, "send_ms": 0.4, "decode_ms": 2.1, "convert_ms": 0.8, "capture_age_ms": 6.5},
        59.8,
        1.1,
    )
    remote.session.toast.show_message("点击画面开始控制 · Ctrl+Alt+Shift 释放输入", duration_ms=0)
    QTest.qWait(150)
    remote.grab().save(str(output / "brioche-live-session.png"))

    main.navigate(MainWindow.PAGE_SETTINGS)
    main.settings_view.select_category(1)
    QTest.qWait(220)
    main.grab().save(str(output / "brioche-09-settings-dark.png"))

    main.transfer_board.add("example", "发送：设计资料.pdf", lambda: None)
    main.transfer_board.update_task(
        {"id": "example", "completed": 3600000, "total": 8000000, "status": "传输中"}
    )
    main.navigate(MainWindow.PAGE_TRANSFERS)
    QTest.qWait(220)
    main.grab().save(str(output / "brioche-09-files-dark.png"))

    remote.close()
    main.shutdown()
    main.close()
    host.stop()
    host.join()
    timer.stop()
    app.quit()


timer = QTimer()
timer.setInterval(1200)
timer.timeout.connect(inspect)
timer.start()
app.exec()
folder.cleanup()
topology.stop()
backend.stop()
assert result["ok"], "No remote frame reached the Qt canvas"
print("GUI, unified dashboard, Light/Dark themes, drawer and TLS/H.264 canvas passed")
