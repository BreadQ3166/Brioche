"""Embedded EasyTier userspace P2P mesh controller (zero root, zero TUN, global P2P + domestic relay)."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

from .security import access_uri, data_dir, parse_uri

DISCOVERY_PORT = 47984
STREAM_PORT = 47985
UDP_STREAM_PORT = 47987

DEFAULT_EASYTIER_PEERS = (
    "tcp://183.230.36.171:11010",
    "tcp://easytier.weiai.org.cn:11010",
    "tcp://et.sumnight.top:11010",
    "tcp://225284.xyz:11010",
)

PIN_RE = re.compile(r"^[a-f0-9]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
PEER_URL_RE = re.compile(r"^(?:tcp|udp|quic|ws|wss|wg|faketcp)://[^\s]+$", re.IGNORECASE)


def helper_path():
    name = "easytier-core.exe" if sys.platform == "win32" else "easytier-core"
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2] / "runtime"))
    return base / "network" / name


def cli_path():
    name = "easytier-cli.exe" if sys.platform == "win32" else "easytier-cli"
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2] / "runtime"))
    return base / "network" / name


def login_url(value):
    """Kept for backwards compatibility; EasyTier uses direct network name + secret login."""
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and url.hostname == "login.tailscale.com"
            and url.port in (None, 443)
            and not url.username
            and not url.password
        )
    except (TypeError, ValueError):
        return False


def parse_peer_urls(raw):
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        items = [p.strip() for p in re.split(r"[\s,;]+", str(raw or "")) if p.strip()]
    clean = []
    for item in items:
        if PEER_URL_RE.match(item) and item not in clean:
            clean.append(item)
    return clean or list(DEFAULT_EASYTIER_PEERS)


def derive_node_ips(machine_id):
    digest = hashlib.sha256(str(machine_id).encode("utf-8")).digest()
    a = (digest[0] % 250) + 1
    b = (digest[1] % 250) + 1
    return f"10.144.{a}.{b}", f"10.145.{a}.{b}"


def sanitize_hostname(raw):
    cleaned = re.sub(r"[^a-zA-Z0-9-]+", "-", str(raw or "").strip()).strip("-")[:32]
    return cleaned or "device"


def _free_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tune_tcp(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("SOCKS5 连接提前断开")
        buf.extend(chunk)
    return bytes(buf)


def socks5_connect(socks_port, dst_ip, dst_port, timeout=6.0):
    sock = socket.create_connection(("127.0.0.1", int(socks_port)), timeout=timeout)
    _tune_tcp(sock)
    sock.settimeout(timeout)
    try:
        sock.sendall(b"\x05\x01\x00")
        greeting = _recv_exact(sock, 2)
        if greeting != b"\x05\x00":
            raise ConnectionError("EasyTier SOCKS5 握手失败")
        ip_bytes = socket.inet_aton(dst_ip)
        req = b"\x05\x01\x00\x01" + ip_bytes + int(dst_port).to_bytes(2, "big")
        sock.sendall(req)
        head = _recv_exact(sock, 4)
        if head[1] != 0:
            raise ConnectionError(f"EasyTier 目标不可达 (socks5={head[1]})")
        atyp = head[3]
        if atyp == 1:
            _recv_exact(sock, 6)
        elif atyp == 3:
            ln = _recv_exact(sock, 1)[0]
            _recv_exact(sock, ln + 2)
        elif atyp == 4:
            _recv_exact(sock, 18)
        sock.settimeout(None)
        return sock
    except Exception:
        sock.close()
        raise


def socks5_http_get_json(socks_port, dst_ip, dst_port, path, timeout=2.5):
    sock = socks5_connect(socks_port, dst_ip, dst_port, timeout=timeout)
    try:
        sock.settimeout(timeout)
        req = f"GET {path} HTTP/1.1\r\nHost: {dst_ip}:{dst_port}\r\nConnection: close\r\n\r\n"
        sock.sendall(req.encode("ascii"))
        chunks = []
        total = 0
        while total < 16384:
            part = sock.recv(4096)
            if not part:
                break
            chunks.append(part)
            total += len(part)
        raw = b"".join(chunks)
        sep = raw.find(b"\r\n\r\n")
        if sep < 0:
            raise RuntimeError("无效 HTTP 响应")
        header = raw[:sep].decode("latin1", errors="replace")
        body = raw[sep + 4 :]
        status_line = header.splitlines()[0] if header else ""
        parts = status_line.split()
        code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        if code != 200:
            return code, {}
        return code, json.loads(body.decode("utf-8"))
    finally:
        sock.close()


class _TcpBridgeServer:
    """Loopback TCP listener forwarding each accepted socket via a connector callback."""

    def __init__(self, bind_port, connector):
        self._connector = connector
        self._stop = threading.Event()
        self._conns = set()
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", bind_port))
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name=f"brioche-et-tcp-{self.port}", daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, a):
        _tune_tcp(a)
        try:
            b = self._connector()
        except Exception:
            a.close()
            return
        _tune_tcp(b)
        with self._lock:
            if self._stop.is_set():
                a.close()
                b.close()
                return
            self._conns.add(a)
            self._conns.add(b)

        def pipe(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t1 = threading.Thread(target=pipe, args=(a, b), daemon=True)
        t2 = threading.Thread(target=pipe, args=(b, a), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass
        with self._lock:
            self._conns.discard(a)
            self._conns.discard(b)

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        with self._lock:
            for s in list(self._conns):
                try:
                    s.close()
                except OSError:
                    pass
            self._conns.clear()


class _UdpRelayServer:
    """Loopback UDP listener bridging EasyTier :47987 to local brioche-core UDP port."""

    def __init__(self, bind_port, target_port):
        self._target = ("127.0.0.1", int(target_port))
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", bind_port))
        self._sock.settimeout(0.5)
        self._peers = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="brioche-et-udp-host", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                up = self._peers.get(addr)
                if up is None:
                    up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    up.settimeout(30.0)
                    self._peers[addr] = up
                    threading.Thread(target=self._back, args=(up, addr), daemon=True).start()
            try:
                up.sendto(data, self._target)
            except OSError:
                pass

    def _back(self, up, client_addr):
        try:
            while not self._stop.is_set():
                data, _ = up.recvfrom(2048)
                self._sock.sendto(data, client_addr)
        except OSError:
            pass
        finally:
            with self._lock:
                self._peers.pop(client_addr, None)
            try:
                up.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        with self._lock:
            for up in list(self._peers.values()):
                try:
                    up.close()
                except OSError:
                    pass
            self._peers.clear()


class EmbeddedNetwork:
    """EasyTier userspace mesh manager exposing the unified Brioche WAN network API."""

    def __init__(self, folder=None, executable=None, cli_executable=None):
        self.folder = Path(folder) if folder else data_dir() / "network"
        self.executable = Path(executable) if executable else helper_path()
        self.cli_executable = Path(cli_executable) if cli_executable else (
            self.executable.parent / ("easytier-cli.exe" if sys.platform == "win32" else "easytier-cli")
        )
        self.process = None
        self.lock = threading.RLock()
        self.closed = False
        self._rpc_port = 0
        self._socks_port = 0
        self._virtual_ip = ""
        self._proxy_ip = ""
        self._shared = None
        self._http_server = None
        self._inbound_tcp = None
        self._inbound_udp = None
        self._outbound_bridges = []
        self._udp_rules = []

    @property
    def _config_file(self):
        return self.folder / "easytier.json"

    @property
    def enabled(self):
        return self._config_file.is_file()

    def load_config(self):
        if not self._config_file.is_file():
            return None
        try:
            data = json.loads(self._config_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("network_name") and data.get("network_secret"):
                return data
        except (OSError, ValueError):
            pass
        return None

    def _save_config(self, network_name, network_secret, peers=None, hostname=None):
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            self.folder.chmod(0o700)
        prev = self.load_config() or {}
        machine_id = prev.get("machine_id") or str(uuid.uuid4())
        clean_peers = parse_peer_urls(peers)
        payload = {
            "network_name": str(network_name).strip()[:64],
            "network_secret": str(network_secret).strip()[:64],
            "peers": clean_peers,
            "machine_id": machine_id,
            "hostname": sanitize_hostname(hostname or socket.gethostname()),
        }
        tmp = self.folder / "easytier.json.tmp"
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as out:
            json.dump(payload, out, ensure_ascii=False, indent=2)
        tmp.replace(self._config_file)
        return payload

    def _start(self):
        if self.closed:
            raise RuntimeError("网络模块已关闭")
        if self.process and self.process.poll() is None:
            return
        cfg = self.load_config()
        if not cfg:
            raise RuntimeError("尚未登录 EasyTier 网络")
        if not self.executable.is_file() or not self.cli_executable.is_file():
            raise RuntimeError("缺少内置 EasyTier 组件，请安装完整的 Brioche 发行包")
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._rpc_port = _free_tcp_port()
        self._socks_port = _free_tcp_port()
        self._virtual_ip, self._proxy_ip = derive_node_ips(cfg["machine_id"])
        cmd = [
            str(self.executable),
            "--no-tun",
            "--use-smoltcp",
            "-i",
            f"{self._virtual_ip}/16",
            "-n",
            f"127.0.0.1/32->{self._proxy_ip}/32",
            "--tcp-whitelist",
            str(DISCOVERY_PORT),
            str(STREAM_PORT),
            "--udp-whitelist",
            str(UDP_STREAM_PORT),
            "--hostname",
            f"brioche-{sanitize_hostname(cfg.get('hostname') or socket.gethostname())}",
            "--machine-id",
            cfg["machine_id"],
            "--network-name",
            cfg["network_name"],
            "--network-secret",
            cfg["network_secret"],
            "--latency-first",
            "--enable-kcp-proxy",
            "--socks5",
            str(self._socks_port),
            "--rpc-portal",
            f"127.0.0.1:{self._rpc_port}",
            "--disable-env-parsing",
        ]
        for peer_url in parse_peer_urls(cfg.get("peers")):
            cmd += ["-p", peer_url]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ET_", "TS_", "TSNET_", "TAILSCALE_"))}
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=creationflags,
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                code = self.process.returncode
                self._terminate()
                raise RuntimeError(f"内置 EasyTier 启动失败 (exit={code})")
            try:
                with socket.create_connection(("127.0.0.1", self._rpc_port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.08)

    def _cli_json(self, *cli_args, timeout=6.0):
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.run(
            [str(self.cli_executable), "-p", f"127.0.0.1:{self._rpc_port}", "-o", "json", *cli_args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "easytier-cli 查询失败").strip()[:160])
        return json.loads(proc.stdout)

    def _start_discovery_http(self):
        if self._http_server is not None:
            return
        net = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path not in ("/presence", "/info"):
                    self.send_response(404)
                    self.end_headers()
                    return
                with net.lock:
                    info = dict(net._shared) if net._shared else None
                if self.path == "/presence":
                    busy = False
                    if info and info.get("_host") is not None:
                        busy = bool(info["_host"].session_gate.active)
                    payload = {"product": "Brioche/1.4", "sharing": bool(info), "busy": busy}
                    body = json.dumps(payload).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not info:
                    self.send_response(409)
                    self.end_headers()
                    return
                busy = bool(info["_host"].session_gate.active) if info.get("_host") is not None else False
                payload = {
                    "product": "Brioche/1.4",
                    "port": STREAM_PORT,
                    "udp_port": UDP_STREAM_PORT if info.get("udp_port") else 0,
                    "pin": info["pin"],
                    "token": info["token"],
                    "busy": busy,
                }
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", DISCOVERY_PORT), Handler)
        server.daemon_threads = True
        self._http_server = server
        threading.Thread(target=server.serve_forever, name="brioche-et-discovery", daemon=True).start()

    def _stop_host_bridges(self):
        self._shared = None
        if self._inbound_tcp is not None:
            self._inbound_tcp.close()
            self._inbound_tcp = None
        if self._inbound_udp is not None:
            self._inbound_udp.close()
            self._inbound_udp = None

    def status(self, peer_ip=None, probe=False):
        cfg = self.load_config()
        if not cfg:
            return {
                "ready": False,
                "state": "NeedsLogin",
                "ips": [],
                "peers": [],
                "auth_url": "",
                "network_name": "",
                "peers_config": list(DEFAULT_EASYTIER_PEERS),
                "message": "点击“登录 EasyTier”，输入网络名与密钥即可全球直连",
                "path": "待检测",
            }
        try:
            with self.lock:
                self._start()
                socks_port = self._socks_port
                virtual_ip = self._virtual_ip
                proxy_ip = self._proxy_ip
            routes = self._cli_json("route", timeout=6.0)
            peer_rows = self._cli_json("peer", timeout=6.0)
            peer_by_ipv4 = {}
            for row in peer_rows if isinstance(peer_rows, list) else []:
                ip_key = str(row.get("ipv4", "")).split("/")[0]
                if ip_key:
                    peer_by_ipv4[ip_key] = row

            peers = []
            for r in routes if isinstance(routes, list) else []:
                hostname = str(r.get("hostname", ""))
                if r.get("next_hop_hostname") == "Local" or not hostname.startswith("brioche-"):
                    continue
                proxy_cidrs = str(r.get("proxy_cidrs", "")).strip()
                target_ip = ""
                for part in re.split(r"[\s,]+", proxy_cidrs):
                    cand = part.split("/")[0].strip()
                    if cand.startswith("10.145."):
                        target_ip = cand
                        break
                if not target_ip:
                    continue
                v4 = str(r.get("ipv4", "")).split("/")[0]
                prow = peer_by_ipv4.get(v4, {})
                cost = str(prow.get("cost", "")).lower()
                lat_raw = str(prow.get("lat_ms", "")).strip()
                proto = str(prow.get("tunnel_proto", "")).strip()
                if cost == "p2p" or str(r.get("next_hop_hostname", "")).upper() == "DIRECT":
                    lat_txt = f" · {lat_raw}ms" if lat_raw and lat_raw != "-" else ""
                    proto_txt = f" ({proto.upper()})" if proto and proto != "-" else ""
                    path_label = f"P2P 直连{lat_txt}{proto_txt}"
                else:
                    lat_txt = f" · {lat_raw}ms" if lat_raw and lat_raw != "-" else ""
                    path_label = f"EasyTier 中继{lat_txt}"
                peers.append(
                    {
                        "ip": target_ip,
                        "virtual_ip": v4,
                        "name": hostname[len("brioche-"):] or target_ip,
                        "online": True,
                        "reachable": False,
                        "sharing": False,
                        "busy": False,
                        "path": path_label,
                    }
                )

            if probe and peers:
                def probe_one(entry):
                    try:
                        code, data = socks5_http_get_json(socks_port, entry["ip"], DISCOVERY_PORT, "/presence", timeout=2.0)
                        if code == 200 and str(data.get("product", "")).startswith("Brioche/"):
                            entry["reachable"] = True
                            entry["sharing"] = bool(data.get("sharing", False))
                            entry["busy"] = bool(data.get("busy", False))
                    except Exception:
                        pass

                with ThreadPoolExecutor(max_workers=min(6, len(peers))) as pool:
                    list(pool.map(probe_one, peers[:32]))

            net_name = cfg["network_name"]
            return {
                "ready": True,
                "state": "Running",
                "ips": [virtual_ip, proxy_ip],
                "peers": peers,
                "auth_url": "",
                "network_name": net_name,
                "peers_config": list(cfg.get("peers") or DEFAULT_EASYTIER_PEERS),
                "message": f"EasyTier 已连接 · {net_name} ({virtual_ip})",
                "path": next((p["path"] for p in peers if p["ip"] == peer_ip), "待检测"),
            }
        except Exception as exc:
            return {
                "ready": False,
                "state": "Unavailable",
                "ips": [],
                "peers": [],
                "auth_url": "",
                "network_name": cfg.get("network_name", ""),
                "peers_config": list(cfg.get("peers") or DEFAULT_EASYTIER_PEERS),
                "message": f"EasyTier 暂不可用：{str(exc)[:120]}",
                "path": "待检测",
            }

    def login(self, network_name=None, network_secret=None, peers=None, hostname=None):
        with self.lock:
            if network_name and network_secret:
                self._terminate()
                self._save_config(network_name, network_secret, peers=peers, hostname=hostname)
            elif not self.load_config():
                raise RuntimeError("请填写 EasyTier 网络名称与密钥")
            self._start()
            return self.status(probe=True)

    def publish(self, host):
        if host.bind != "127.0.0.1":
            raise ValueError("内置网络仅代理本机回环共享")
        with self.lock:
            if host.stop_requested.is_set():
                return
            self._start()
            self._stop_host_bridges()
            self._start_discovery_http()
            host_port = int(host.port)
            self._inbound_tcp = _TcpBridgeServer(
                STREAM_PORT,
                lambda p=host_port: socket.create_connection(("127.0.0.1", p), timeout=4.0),
            )
            core_udp_port = int(getattr(host, "core_udp_port", 0) or 0)
            if core_udp_port:
                self._inbound_udp = _UdpRelayServer(UDP_STREAM_PORT, core_udp_port)
            self._shared = {
                "port": host_port,
                "udp_port": core_udp_port,
                "pin": host.pin,
                "token": host.token,
                "_host": host,
            }

    def clear(self, token=""):
        with self.lock:
            if not self._shared or not token or self._shared.get("token") == token:
                self._stop_host_bridges()

    def open_peer(self, ip):
        with self.lock:
            self._start()
            socks_port = self._socks_port
        try:
            code, info = socks5_http_get_json(socks_port, ip, DISCOVERY_PORT, "/info", timeout=6.0)
        except Exception as exc:
            raise RuntimeError("无法联系该 EasyTier 设备，请确认对方已打开 Brioche 并加入同一网络") from exc
        if code == 409:
            raise RuntimeError("对方尚未开启 Brioche 外网共享")
        if code != 200 or not str(info.get("product", "")).startswith("Brioche/"):
            raise RuntimeError("对方不是兼容的 Brioche 共享服务")
        pin = str(info.get("pin", ""))
        token = str(info.get("token", ""))
        remote_port = int(info.get("port", STREAM_PORT))
        if not PIN_RE.match(pin) or not TOKEN_RE.match(token):
            raise RuntimeError("对方返回的设备凭据无效")
        bridge = _TcpBridgeServer(
            0,
            lambda sp=socks_port, target_ip=ip, rp=remote_port: socks5_connect(sp, target_ip, rp, timeout=8.0),
        )
        with self.lock:
            self._outbound_bridges.append(bridge)
        uri = access_uri("127.0.0.1", bridge.port, pin, token)
        parse_uri(uri)
        return uri

    def proxy_uri(self, uri):
        host, port, pin, token = parse_uri(uri)
        with self.lock:
            self._start()
            socks_port = self._socks_port
        bridge = _TcpBridgeServer(
            0,
            lambda sp=socks_port, h=host, p=port: socks5_connect(sp, h, p, timeout=8.0),
        )
        with self.lock:
            self._outbound_bridges.append(bridge)
        return access_uri("127.0.0.1", bridge.port, pin, token)

    def open_udp_tunnel(self, ip, port=UDP_STREAM_PORT):
        """Open a local loopback UDP port forwarded over EasyTier mesh to peer :47987/udp."""
        with self.lock:
            self._start()
            rpc_port = self._rpc_port
        local_port = _free_udp_port()
        bind_addr = f"127.0.0.1:{local_port}"
        dst_addr = f"{ip}:{int(port)}"
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.run(
            [str(self.cli_executable), "-p", f"127.0.0.1:{rpc_port}", "port-forward", "add", "udp", bind_addr, dst_addr],
            capture_output=True,
            text=True,
            timeout=6.0,
            creationflags=creationflags,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"EasyTier UDP 隧道建立失败：{(proc.stderr or proc.stdout).strip()[:120]}")
        rule_id = f"udp:{bind_addr}"
        with self.lock:
            self._udp_rules.append((bind_addr, dst_addr))
        return local_port, rule_id

    def logout(self):
        with self.lock:
            self._stop_host_bridges()
            self._close_outbound()
            self._terminate()
            self._config_file.unlink(missing_ok=True)

    def _close_outbound(self):
        for bridge in self._outbound_bridges:
            bridge.close()
        self._outbound_bridges.clear()
        self._udp_rules.clear()

    def _terminate(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            self.process = None

    def close(self):
        with self.lock:
            self.closed = True
            self._stop_host_bridges()
            self._close_outbound()
            if self._http_server is not None:
                try:
                    self._http_server.shutdown()
                    self._http_server.server_close()
                except Exception:
                    pass
                self._http_server = None
            self._terminate()
