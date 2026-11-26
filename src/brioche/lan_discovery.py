"""Zero-dependency LAN multicast discovery beacon and listener.

Beacons advertise only non-secret discovery hints (name, OS, port, pin prefix).
All trust decisions still require explicit TLS connection and SHA-256 cert pinning.
"""

import json
import platform
import socket
import struct
import threading
import time

from .lan import target

MULTICAST_GROUP = "239.255.47.95"
MULTICAST_PORT = 47993
BEACON_TTL_SECONDS = 12.0
BEACON_INTERVAL_SECONDS = 3.0


def build_beacon(name, port, pin, busy=False, system_name=None):
    return json.dumps(
        {
            "product": "Brioche",
            "version": 2,
            "name": str(name)[:64],
            "platform": str(system_name or platform.system())[:32],
            "port": int(port),
            "pin_prefix": str(pin or "")[:16],
            "busy": bool(busy),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def parse_beacon(raw, sender_ip):
    if not raw or len(raw) > 1024:
        return None
    try:
        ip, _ = target(sender_ip)
    except ValueError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("product") != "Brioche":
        return None
    port = data.get("port")
    name = data.get("name")
    plat = data.get("platform", "")
    prefix = data.get("pin_prefix", "")
    if (
        type(port) is not int
        or not 1024 <= port <= 65535
        or not isinstance(name, str)
        or not name.strip()
        or not isinstance(plat, str)
        or not isinstance(prefix, str)
    ):
        return None
    clean_name = "".join(c for c in name if c.isprintable())[:64]
    if not clean_name:
        return None
    return {
        "ip": ip,
        "port": port,
        "name": clean_name,
        "platform": plat[:32],
        "pin_prefix": prefix[:16],
        "busy": bool(data.get("busy")),
    }


class LanBeaconBroadcaster:
    """Periodically announces a local LAN host on multicast group when enabled."""

    def __init__(self, get_info, interval=BEACON_INTERVAL_SECONDS):
        self._get_info = get_info
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="brioche-lan-beacon", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)

    def _run(self):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            while not self._stop.is_set():
                info = self._get_info()
                if info and info.get("port") and info.get("pin"):
                    payload = build_beacon(
                        info["name"],
                        info["port"],
                        info["pin"],
                        info.get("busy", False),
                    )
                    bind_ip = info.get("bind")
                    if bind_ip and bind_ip not in ("0.0.0.0", "127.0.0.1"):
                        try:
                            sock.setsockopt(
                                socket.IPPROTO_IP,
                                socket.IP_MULTICAST_IF,
                                socket.inet_aton(bind_ip),
                            )
                        except OSError:
                            pass
                    try:
                        sock.sendto(payload, (MULTICAST_GROUP, MULTICAST_PORT))
                    except OSError:
                        pass
                self._stop.wait(self._interval)
        except OSError:
            pass
        finally:
            if sock:
                sock.close()


class LanBeaconListener:
    """Listens for nearby Brioche LAN multicast beacons."""

    def __init__(self, local_pin_getter=lambda: "", on_change=lambda items: None):
        self._local_pin = local_pin_getter
        self._on_change = on_change
        self._lock = threading.Lock()
        self._seen = {}
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="brioche-lan-listen", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)

    def snapshot(self):
        now = time.monotonic()
        with self._lock:
            self._seen = {
                k: v for k, v in self._seen.items() if now - v["seen_at"] <= BEACON_TTL_SECONDS
            }
            return [dict(v["info"]) for v in self._seen.values()]

    def ingest(self, raw, sender_ip):
        parsed = parse_beacon(raw, sender_ip)
        if not parsed:
            return False
        own_prefix = str(self._local_pin() or "")[:16]
        if own_prefix and parsed["pin_prefix"] == own_prefix:
            return False
        key = (parsed["ip"], parsed["port"])
        with self._lock:
            self._seen[key] = {"info": parsed, "seen_at": time.monotonic()}
        return True

    def _run(self):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", MULTICAST_PORT))
            mreq = struct.pack("4s4s", socket.inet_aton(MULTICAST_GROUP), socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    raw, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if self.ingest(raw, addr[0]):
                    try:
                        self._on_change(self.snapshot())
                    except Exception:
                        pass
        except OSError:
            pass
        finally:
            if sock:
                sock.close()
