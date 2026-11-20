"""HTTPS/WebSocket adapter. Desktop capture, permissions and input stay in Host.

The browser gets a separate temporary bearer token; the native token never leaves
this process. H.264 uses the native encoder with a bounded IDR recovery window.
"""
import asyncio
from dataclasses import asdict
import hashlib
from importlib.resources import files
import ipaddress
import json
from pathlib import Path
import secrets
import struct
import time

from aiohttp import web, WSMsgType
from .platform_input import validate_event
from .protocol import CONTROL, VIDEO, VERSION, StreamConfig, ProtocolError, pack_control, read_message, send
from .security import client_context, data_dir, identity, verify_pin
from .session import close_writer, validate_stream_info
from .web_video import BrowserVideoFlow, VideoStreamError

FRAME_HEADER = struct.Struct("!II")  # stream generation, acknowledgement ID
SECURITY_HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "connect-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'none'; object-src 'none'",
}


class BrowserGateway:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.token = secrets.token_urlsafe(32)
        self.pin = ""
        self.authority = ""
        self.url = ""
        self.runner = None
        self.peers, self.tasks = set(), set()
        self.occupied = False
        self.failures = []

    async def start(self):
        address = ipaddress.ip_address(self.host.bind)
        if address.is_unspecified or address.is_multicast:
            raise ValueError("浏览器访问需要选择明确的本机地址")
        folder = (Path(self.host.folder) if self.host.folder else data_dir()) / "web" / hashlib.sha256(str(address).encode()).hexdigest()[:16]
        context, self.pin = identity(folder, hosts=[str(address)])
        @web.middleware
        async def guard(request, handler):
            if request.headers.get("Host") != self.authority:
                raise web.HTTPForbidden(text="地址不匹配")
            response = await handler(request)
            return response
        app = web.Application(middlewares=[guard], client_max_size=4096)
        async def headers(request, response):
            response.headers.update(SECURITY_HEADERS)
            response.headers["Content-Security-Policy"] = SECURITY_HEADERS["Content-Security-Policy"].replace(
                "connect-src 'self';", f"connect-src 'self' wss://{self.authority};")
        app.on_response_prepare.append(headers)
        app.router.add_get("/", self.index)
        app.router.add_get("/app.js", self.asset)
        app.router.add_get("/video.js", self.asset)
        app.router.add_get("/app.css", self.asset)
        app.router.add_get("/ws", self.websocket)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        await self.runner.setup()
        site = web.TCPSite(self.runner, str(address), self.port, ssl_context=context, backlog=4)
        await site.start()
        self.port = self.runner.addresses[0][1]
        host_label = f"[{address}]" if address.version == 6 else str(address)
        self.authority = f"{host_label}:{self.port}"
        self.url = f"https://{self.authority}/#{self.token}"

    async def index(self, request):
        return web.Response(text=files("brioche").joinpath("web/index.html").read_text(encoding="utf-8"), content_type="text/html")

    async def asset(self, request):
        name = request.path[1:]
        content_type = "text/javascript" if name.endswith(".js") else "text/css"
        return web.Response(text=files("brioche").joinpath(f"web/{name}").read_text(encoding="utf-8"), content_type=content_type)

    async def websocket(self, request):
        if request.headers.get("Origin") != f"https://{self.authority}":
            raise web.HTTPForbidden(text="不允许来自其他网页的连接")
        now = time.monotonic()
        self.failures = [t for t in self.failures if now - t < 60]
        if len(self.peers) >= 4 or len(self.failures) >= 20:
            raise web.HTTPTooManyRequests(text="请稍后重试")
        # Finish the native session before acknowledging a browser close. This
        # lets automatic decoder fallback reconnect without racing the old peer.
        ws = web.WebSocketResponse(heartbeat=5, max_msg_size=4096, receive_timeout=15, autoclose=False)
        await ws.prepare(request)
        self.peers.add(ws)
        task = asyncio.current_task()
        self.tasks.add(task)
        writer = None
        children = []
        acquired = False
        try:
            first = await asyncio.wait_for(ws.receive(), 6)
            if first.type != WSMsgType.TEXT:
                raise ProtocolError("请提供访问码")
            auth = json.loads(first.data)
            token = auth.get("token") if isinstance(auth, dict) else None
            if (not isinstance(auth, dict) or auth.get("type") != "auth"
                    or not isinstance(token, str) or not token.isascii()
                    or not secrets.compare_digest(token, self.token)):
                self.failures.append(time.monotonic())
                raise ProtocolError("访问码无效，请重新复制浏览器链接")
            config = StreamConfig.parse(auth.get("config"))
            codec = auth.get("codec", "mjpeg")  # retain 0.3 browser protocol compatibility
            if codec not in ("h264", "mjpeg"):
                raise ProtocolError("不支持的浏览器视频格式")
            if codec == "mjpeg" and config.fps > 30:
                raise ProtocolError("浏览器兼容模式最高 30 FPS")
            if codec == "h264" and config.fps > 60:
                raise ProtocolError("H.264 浏览器模式最高 60 FPS")
            if self.occupied:
                raise ProtocolError("已有浏览器正在连接")
            self.occupied = acquired = True
            reader, writer = await asyncio.wait_for(asyncio.open_connection(
                self.host.bind, self.host.port, ssl=client_context(), server_hostname="brioche",
                ssl_handshake_timeout=5), 8)
            verify_pin(writer.get_extra_info("ssl_object"), self.host.pin)
            await send(writer, pack_control({"type": "hello", "version": VERSION, "token": self.host.token,
                                            "codec": codec, "config": asdict(config)}))
            kind, info = await asyncio.wait_for(read_message(reader), 15)
            if kind != CONTROL or info.get("type") != "welcome":
                raise ProtocolError(info.get("message", "被控端未就绪") if kind == CONTROL else "无效响应")
            validate_stream_info(info)
            if info.get("codec") != codec:
                raise ProtocolError("被控端未提供请求的视频格式")
            await ws.send_json(info)
            state = {"epoch": info["stream_id"], "input": info["input"], "video": BrowserVideoFlow(info), "switch_at": 0.0}
            children = [asyncio.create_task(self.to_browser(reader, ws, writer, state)),
                        asyncio.create_task(self.from_browser(ws, writer, state)),
                        asyncio.create_task(self.heartbeat(writer))]
            done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
            for completed in done:
                completed.result()
        except asyncio.CancelledError:
            pass
        except (asyncio.IncompleteReadError, ConnectionError):
            if not ws.closed:
                try:
                    await ws.send_json({"type": "error", "message": "共享已结束，请重新连接"})
                except Exception:
                    pass
        except Exception as exc:
            if not ws.closed:
                # Never serialize auth payloads, tokens, URLs or native messages wholesale.
                message = str(exc)[:200] if isinstance(exc, (ProtocolError, ValueError)) else "连接中断，请检查共享状态后重试"
                try:
                    await ws.send_json({"type": "error", "message": message,
                                        "code": "video_unavailable" if isinstance(exc, VideoStreamError) else "session_error"})
                except Exception:
                    pass
        finally:
            for child in children:
                child.cancel()
            if children:
                await asyncio.gather(*children, return_exceptions=True)
            if writer:
                try:
                    await send(writer, pack_control({"type": "input", "event": "release"}))
                    await send(writer, pack_control({"type": "bye"}))
                    # Closing the client TLS writer only starts host cleanup.
                    # Observe host EOF before completing the WebSocket close,
                    # so an immediate fallback/reconnect cannot see a busy host.
                    async def drain_native():
                        while await reader.read(65536):
                            pass
                    await asyncio.wait_for(drain_native(), 3)
                except Exception:
                    pass
                await close_writer(writer)
            if acquired:
                self.occupied = False
            try:
                await asyncio.wait_for(ws.close(), 1)
            except Exception:
                pass
            self.peers.discard(ws)
            self.tasks.discard(task)
        return ws

    async def to_browser(self, reader, ws, writer, state):
        while not ws.closed:
            kind, payload = await asyncio.wait_for(read_message(reader), 12)
            if state["switch_at"] and time.monotonic() - state["switch_at"] > 10:
                raise ProtocolError("切屏响应超时，请重新连接")
            if kind == VIDEO:
                config, packet, request_key = state["video"].packet(payload)
                if request_key:
                    await send(writer, pack_control({"type": "request_keyframe", "stream_id": state["epoch"]}))
                if config:
                    await asyncio.wait_for(ws.send_json(config), 4)
                if packet:
                    await asyncio.wait_for(ws.send_bytes(packet), 4)
            elif payload["type"] in ("stream", "monitor_error", "stats", "error"):
                if payload["type"] in ("stream", "monitor_error"):
                    state["switch_at"] = 0.0
                if payload["type"] == "stream":
                    validate_stream_info(payload)
                    state["epoch"] = payload["stream_id"]
                    state["video"].reset(payload)
                await asyncio.wait_for(ws.send_json(payload), 4)

    async def from_browser(self, ws, writer, state):
        window, count = time.monotonic(), 0
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                raise ProtocolError("无效浏览器消息")
            now = time.monotonic()
            if now - window >= 1:
                window, count = now, 0
            count += 1
            if count > 500:
                raise ProtocolError("操作过于频繁")
            value = json.loads(message.data)
            if not isinstance(value, dict):
                raise ProtocolError("无效浏览器消息")
            kind = value.get("type")
            if kind == "frame_ack":
                state["video"].acknowledge(value.get("frame"))
            elif kind == "input":
                validate_event(value)
                if state["input"] and (value.get("event") == "release" or (
                        type(value.get("stream_id")) is int and value["stream_id"] == state["epoch"])):
                    await send(writer, pack_control(value))
            elif kind == "switch_monitor":
                state["switch_at"] = time.monotonic()
                await send(writer, pack_control(value))
            elif kind == "bye":
                return
            else:
                raise ProtocolError("不支持的浏览器操作")

    async def heartbeat(self, writer):
        while True:
            await send(writer, pack_control({"type": "ping", "stamp": time.monotonic()}))
            await asyncio.sleep(2)

    async def close(self):
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
