"""LAN discovery and explicit first-use pairing over certificate-bound TLS."""

import asyncio
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
import threading
from .device_store import private_json
from .protocol import CONTROL, VERSION, ProtocolError, pack_control, read_message, send
from .security import client_context, data_dir

CLIENT_ID = re.compile(r"^[a-f0-9]{32}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
PIN = re.compile(r"^[a-f0-9]{64}$")


def target(value):
    value = value.strip()
    if value.count(":") > 1:
        raise ValueError("请填写局域网 IPv4，例如 192.168.1.8，可附加 :端口")
    host, separator, port = value.partition(":")
    ip = ipaddress.ip_address(host)
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
    if ip.version != 4 or not any(ip in ipaddress.ip_network(n) for n in networks):
        raise ValueError("请填写局域网 IPv4 地址")
    port = int(port) if separator else 47995
    if not 1 <= port <= 65535:
        raise ValueError("端口无效")
    return str(ip), port


def pairing_code(pin, nonce, client_id):
    digest = hashlib.sha256(
        f"Brioche-LAN-pair-v1:{pin}:{nonce}:{client_id}".encode()
    ).digest()
    return f'{int.from_bytes(digest[:8], "big") % 1_000_000:06d}'


class PairRequest:
    def __init__(self, name, address, code):
        self.name, self.address, self.code = name, address, code
        self.done = threading.Event()
        self.allowed = False

    def answer(self, allowed):
        self.allowed = bool(allowed)
        self.done.set()


class PairStore:
    def __init__(self, folder=None):
        self.path = (Path(folder) if folder else data_dir()) / "lan-pairs.json"
        self.lock = threading.RLock()
        try:
            pairs = json.loads(self.path.read_text(encoding="utf-8"))
            self.pairs = pairs if isinstance(pairs, dict) else {}
        except (ValueError, OSError):
            self.pairs = {}

    def grant(self, client_id, name):
        with self.lock:
            if client_id not in self.pairs and len(self.pairs) >= 32:
                raise ProtocolError("本机最多配对32台设备，请先清除旧配对")
            token = secrets.token_urlsafe(32)
            self.pairs[client_id] = {"name": name, "token": token}
            private_json(self.path, self.pairs)
            return token

    def accepts(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            return False
        with self.lock:
            return any(
                isinstance(p, dict)
                and isinstance(p.get("token"), str)
                and secrets.compare_digest(p["token"], token)
                for p in self.pairs.values()
            )

    def clear(self):
        with self.lock:
            self.pairs = {}
            private_json(self.path, self.pairs)


async def _connect(ip, port, expected=None):
    from .session import close_writer

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            ip,
            port,
            ssl=client_context(),
            server_hostname="brioche",
            ssl_handshake_timeout=2,
        ),
        3,
    )
    pin = hashlib.sha256(
        writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    ).hexdigest()
    if expected and not secrets.compare_digest(pin, expected):
        await close_writer(writer)
        raise ValueError("设备身份已变化，请删除该设备并重新查找、核对配对码")
    return reader, writer, pin


async def _discover(ip, port, expected=None):
    from .session import close_writer

    reader, writer, pin = await _connect(ip, port, expected)
    try:
        await send(writer, pack_control({"type": "discover", "version": VERSION}))
        kind, info = await asyncio.wait_for(read_message(reader), 3)
        if (
            kind != CONTROL
            or info.get("type") != "device"
            or info.get("product") != "Brioche"
        ):
            raise ValueError(
                "对方不是支持 IP 查找的 Brioche，请更新两端到0.6或更高版本并开启局域网共享"
            )
        if not isinstance(info.get("name"), str) or not isinstance(
            info.get("platform"), str
        ):
            raise ValueError("设备信息无效")
        return {
            "ip": ip,
            "port": port,
            "name": "".join(c for c in info["name"] if c.isprintable())[:64],
            "platform": info["platform"][:32],
            "observed_pin": pin,
            "online": True,
            "busy": bool(info.get("busy")),
        }
    finally:
        await close_writer(writer)


def discover(ip, port=47995, expected=None):
    return asyncio.run(_discover(ip, port, expected))


async def _pair(ip, port, expected, client_id, name, show_code, cancelled):
    from .session import close_writer

    reader, writer, pin = await _connect(ip, port, expected)
    nonce = secrets.token_hex(16)
    try:
        show_code(pairing_code(pin, nonce, client_id))
        await send(
            writer,
            pack_control(
                {
                    "type": "pair",
                    "version": VERSION,
                    "client_id": client_id,
                    "nonce": nonce,
                    "name": name[:64],
                }
            ),
        )
        receive = asyncio.create_task(read_message(reader))
        try:
            for _ in range(70):
                if cancelled.is_set():
                    raise ValueError("配对已取消")
                done, _ = await asyncio.wait([receive], timeout=0.5)
                if done:
                    break
            else:
                raise ValueError("等待对方确认超时，请重试")
            kind, message = await receive
            token = message.get("token")
            if (
                kind != CONTROL
                or message.get("type") != "paired"
                or not isinstance(token, str)
                or not TOKEN.fullmatch(token)
            ):
                raise ValueError(message.get("message", "对方未允许配对"))
            return {"ip": ip, "port": port, "pin": pin, "token": token}
        finally:
            receive.cancel()
            await asyncio.gather(receive, return_exceptions=True)
    finally:
        await close_writer(writer)


def pair(
    ip, port, expected, client_id, name, show_code=lambda code: None, cancelled=None
):
    if not CLIENT_ID.fullmatch(client_id) or not PIN.fullmatch(expected):
        raise ValueError("配对参数无效，请重新查找设备")
    return asyncio.run(
        _pair(
            ip,
            port,
            expected,
            client_id,
            name,
            show_code,
            cancelled if cancelled is not None else threading.Event(),
        )
    )
