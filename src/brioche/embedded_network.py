"""Private IPC to the bundled tsnet helper; no privileged daemon or public control API."""
import ipaddress
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from urllib.parse import urlsplit
from .security import access_uri, data_dir, parse_uri

ERRORS = {
    "different_account": "仅允许连接同一 Tailscale 账号的设备，且设备不能使用标签身份",
    "peer_unavailable": "无法联系这台设备，请确认另一端 Brioche 已启动并登录",
    "peer_not_sharing": "对方尚未开启 Brioche 外网共享，或账号无访问权限",
    "invalid_peer": "对方不是兼容的 Brioche 共享服务",
    "login_failed": "无法发起登录，请检查网络后重试",
    "status_unavailable": "内置网络暂不可用，请稍后刷新",
}


def helper_path():
    name = "brioche-network.exe" if sys.platform == "win32" else "brioche-network"
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2] / "runtime"))
    return base / "network" / name


def login_url(value):
    """Never open arbitrary helper/peer-provided URLs in the user's browser."""
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and url.hostname == "login.tailscale.com"
                and url.port in (None, 443) and not url.username and not url.password)
    except (TypeError, ValueError):
        return False


class EmbeddedNetwork:
    def __init__(self, folder=None, executable=None):
        self.folder = Path(folder) if folder else data_dir() / "network"
        self.executable = Path(executable) if executable else helper_path()
        self.process = None
        self.lock = threading.RLock()
        self.responses = queue.Queue()
        self.serial = 0
        self.closed = False

    @property
    def enabled(self):
        return (self.folder / "enabled").exists()

    def _enable(self):
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            self.folder.chmod(0o700)
        with os.fdopen(os.open(self.folder / "enabled", os.O_WRONLY | os.O_CREAT, 0o600), "w") as out:
            out.write("1\n")

    def _start(self):
        if self.closed:
            raise RuntimeError("网络模块已关闭")
        if self.process and self.process.poll() is None:
            return
        if not self.executable.is_file():
            raise RuntimeError("缺少内置网络模块，请安装完整的 Brioche 发行包")
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TS_", "TSNET_", "TAILSCALE_"))}
        self.responses = queue.Queue()
        self.process = subprocess.Popen([str(self.executable), "--state-dir", str(self.folder / "state")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        process, responses = self.process, self.responses
        def read():
            try:
                while True:
                    line = process.stdout.readline(1024 * 1024 + 1)
                    if not line or len(line) > 1024 * 1024:
                        break
                    responses.put(json.loads(line))
            except (ValueError, OSError):
                pass
            finally:
                responses.put(None)
        threading.Thread(target=read, name="brioche-network-ipc", daemon=True).start()

    def request(self, operation, **values):
        with self.lock:
            self._start()
            self.serial += 1
            try:
                self.process.stdin.write(json.dumps({"id": self.serial, "op": operation, **values}) + "\n")
                self.process.stdin.flush()
                response = self.responses.get(timeout=20)
                if not isinstance(response, dict) or response.get("id") != self.serial:
                    self._terminate()
                    raise RuntimeError("内置网络模块已退出，请检查是否已打开另一份 Brioche")
                if response.get("error"):
                    raise RuntimeError(ERRORS.get(response["error"], "网络操作失败，请刷新后重试"))
                return response["result"]
            except (OSError, queue.Empty, ValueError, KeyError) as exc:
                self._terminate()
                raise RuntimeError("内置网络响应超时或中断，请检查网络后重试") from exc

    def status(self, peer_ip=None):
        if not self.enabled:
            return {"ready": False, "state": "NeedsLogin", "ips": [], "peers": [],
                    "message": "点击“登录 Tailscale”，无需另外安装", "path": "待检测", "auth_url": ""}
        try:
            raw = self.request("status")
            state = raw.get("state")
            ips = [ip for ip in raw.get("ips", []) if ipaddress.ip_address(ip).version == 4]
            ready = state == "Running" and bool(ips)
            peers = raw.get("peers", [])
            return {"ready": ready, "state": state, "ips": ips, "peers": peers,
                "auth_url": raw.get("auth_url", ""),
                "message": "内置 Tailscale 已连接" if ready else {
                    "NeedsMachineAuth": "请在 Tailscale 管理页批准这台设备",
                    "NeedsLogin": "请点击登录，完成浏览器授权",
                    "Starting": "正在连接 Tailscale，请稍后刷新",
                }.get(state, "内置网络正在启动或暂不可用，请刷新"),
                "path": next((p.get("path", "待检测") for p in peers if p.get("ip") == peer_ip), "待检测")}
        except (RuntimeError, ValueError, TypeError):
            return {"ready": False, "state": "Unavailable", "ips": [], "peers": [],
                    "message": "内置网络不可用，请检查安装、网络及是否打开了另一份 Brioche", "path": "待检测", "auth_url": ""}

    def login(self):
        # An already enrolled device should not be forced through reauthentication.
        self._enable()
        status = self.status()
        if status["ready"]:
            return status
        self.request("login")
        return self.status()

    def publish(self, host):
        if host.bind != "127.0.0.1":
            raise ValueError("内置网络仅代理本机回环共享")
        with self.lock:
            if host.stop_requested.is_set():
                return
            self.request("share", share={"port": host.port, "pin": host.pin, "token": host.token})

    def clear(self, token=""):
        if self.process and self.process.poll() is None:
            self.request("clear", share={"token": token})

    def open_peer(self, ip):
        result = self.request("connect", ip=ip)
        uri = access_uri("127.0.0.1", result["port"], result["pin"], result["token"])
        parse_uri(uri)
        return uri

    def proxy_uri(self, uri):
        host, port, pin, token = parse_uri(uri)
        result = self.request("tunnel", ip=host, port=port)
        return access_uri("127.0.0.1", result["port"], pin, token)

    def logout(self):
        with self.lock:
            if self.enabled:
                self.request("logout")
                (self.folder / "enabled").unlink(missing_ok=True)

    def _terminate(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            for stream in (self.process.stdin, self.process.stdout):
                stream.close()
            self.process = None

    def close(self):
        with self.lock:
            try:
                if self.process and self.process.poll() is None:
                    self.request("shutdown")
                    self.process.wait(timeout=5)
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            finally:
                self.closed = True
                self._terminate()
