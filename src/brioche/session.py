"""Async TLS transport, one active viewer, bounded messages and explicit shutdown."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import queue
import secrets
import platform
import socket as system_socket
import socket
import threading
import time

from .protocol import (CONTROL, VIDEO, VERSION, ProtocolError, StreamConfig,
                       pack_control, pack_video, read_message, send)
from .security import client_context, identity, parse_uri, verify_pin


async def close_writer(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 1)
    except (Exception, asyncio.CancelledError):
        # Python 3.10 TLS can otherwise retain pending writes past loop shutdown.
        writer.transport.abort()
    finally:
        writer.transport.abort()
        await asyncio.sleep(0)


def stream_info(media, monitors, selected, epoch, view_only, kind="welcome", request=0):
    return {
        "type": kind, "version": VERSION, "codec": media.codec_name,
        "width": media.width, "height": media.height,
        "source_width": media.monitor["width"], "source_height": media.monitor["height"],
        "source_left": media.monitor["left"], "source_top": media.monitor["top"],
        "fps_limit": int(media.codec.framerate), "encoder": media.encoder_name,
        "capture": media.capture.backend, "encoder_fallback": getattr(media,"fallback_reason",""), "input": not view_only,
        "monitors": monitors, "monitor": selected, "stream_id": epoch, "request": request,
    }


def validate_stream_info(info):
    w, h = info.get("width"), info.get("height")
    if (type(w) is not int or type(h) is not int or min(w, h) < 2
            or max(w, h) > 2560 or min(w, h) > 1440):
        raise ProtocolError("服务端画面尺寸超出限制")
    monitors = info.get("monitors")
    if not isinstance(monitors, list) or not 1 <= len(monitors) <= 32:
        raise ProtocolError("无效显示器列表")
    ids = set()
    for monitor in monitors:
        if not isinstance(monitor, dict) or type(monitor.get("id")) is not int or not 1 <= monitor["id"] <= 32:
            raise ProtocolError("无效显示器编号")
        if monitor["id"] in ids:
            raise ProtocolError("重复显示器编号")
        ids.add(monitor["id"])
        for name in ("width", "height"):
            if type(monitor.get(name)) is not int or not 1 <= monitor[name] <= 32768:
                raise ProtocolError("无效显示器尺寸")
    if (type(info.get("monitor")) is not int or info["monitor"] not in ids
            or type(info.get("stream_id")) is not int or info["stream_id"] < 0):
        raise ProtocolError("无效屏幕状态")
    return w, h


class SessionGate:
    """Share one active desktop session across LAN, browser and embedded-network hosts."""
    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None

    def acquire(self, owner):
        with self.lock:
            if self.owner is not None:
                return False
            self.owner = owner
            return True

    def release(self, owner):
        with self.lock:
            if self.owner is owner:
                self.owner = None

    @property
    def active(self):
        with self.lock:
            return self.owner is not None


class Host:
    def __init__(self, bind="127.0.0.1", port=47995, encoder="libx264", monitor=1,
                 status=lambda message: None, folder=None, view_only=False,
                 web_enabled=False, web_port=47996, lan_discovery=False,
                 on_pair=lambda request: request.answer(False), session_gate=None, name=None):
        self.bind, self.port, self.encoder, self.monitor = bind, port, encoder, monitor
        self.status, self.folder, self.view_only = status, folder, view_only
        self.token = secrets.token_urlsafe(32)
        self.pin = None
        self._thread = None
        self.loop = None
        self.stop_event = None
        self.ready = threading.Event()
        self.error = None
        self.active = False
        self._media = None
        self.session_gate = session_gate or SessionGate()
        self.lan_discovery = lan_discovery
        self.on_pair = on_pair
        self.name = (name or system_socket.gethostname())[:64]
        self._pair_pending = None
        if lan_discovery:
            from .lan import PairStore
            self.pair_store = PairStore(folder)
        else:
            self.pair_store = None
        self._failures = []
        self.peer_tasks = set()
        self.connection_tasks = {}
        self.session_tasks = set()
        self.stop_requested = threading.Event()
        self.web_enabled, self.web_port = web_enabled, web_port
        self.browser_url, self.browser_pin = "", ""

    def start(self):
        if self._thread:
            raise RuntimeError("服务已经启动")
        self._thread = threading.Thread(target=self._run, name="brioche-host", daemon=True)
        self._thread.start()

    def _run(self):
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self.error = str(exc)
            self.status(f"被控服务错误：{exc}")
        finally:
            self.ready.set()
            self.status("被控服务已停止")

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        context, self.pin = identity(self.folder)
        server = await asyncio.start_server(self._accept, self.bind, self.port, ssl=context,
                                            ssl_handshake_timeout=5, backlog=4)
        self.port = server.sockets[0].getsockname()[1]
        gateway = None
        try:
            if self.web_enabled:
                from .web_gateway import BrowserGateway
                gateway = BrowserGateway(self, self.web_port)
                await gateway.start()
                self.browser_url, self.browser_pin = gateway.url, gateway.pin
                self.web_port = gateway.port
            self.status(f"等待连接 · 端口 {self.port}")
            self.ready.set()
            if self.stop_requested.is_set():
                self.stop_event.set()
            async with server:
                await self.stop_event.wait()
        finally:
            self.browser_url = ""
            if gateway:
                await gateway.close()
            server.close()
            await server.wait_closed()
            for task in tuple(self.peer_tasks):
                task.cancel()
            if self.peer_tasks:
                await asyncio.gather(*tuple(self.peer_tasks), return_exceptions=True)
            await asyncio.sleep(0.25)

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.peer_tasks.add(task)
        peer = writer.get_extra_info('peername')
        self.connection_tasks[peer] = task
        acquired = False
        executor = None
        media = None
        sink = None
        receiver = None
        try:
            # Bound unauthenticated peers and brute-force attempts. Tokens are 256-bit.
            now = time.monotonic()
            self._failures = [t for t in self._failures if now - t < 60]
            if len(self.peer_tasks) > 4 or len(self._failures) >= 20:
                return
            kind, hello = await asyncio.wait_for(read_message(reader), 8)
            if kind == CONTROL and hello.get("type") in ("discover", "pair"):
                await self._lan_request(reader, writer, hello)
                return
            if kind == CONTROL and hello.get("type") == "hello" and hello.get("version") != VERSION:
                raise ProtocolError("两端 Brioche 版本不兼容，请都更新到 0.2 或更高版本")
            if (kind != CONTROL or hello.get("type") != "hello" or hello.get("version") != VERSION
                    or not isinstance(hello.get("token"), str)
                    or not (secrets.compare_digest(hello["token"], self.token)
                            or (self.pair_store and self.pair_store.accepts(hello["token"])))):
                self._failures.append(now)
                raise ProtocolError("设备凭据或协议版本无效")
            config = StreamConfig.parse(hello.get("config"))
            codec = hello.get("codec", "h264")
            if codec not in ("h264", "mjpeg") or (codec == "mjpeg" and config.fps > 30):
                raise ProtocolError("不支持的画面格式或浏览器帧率")
            if not self.session_gate.acquire(task):
                raise ProtocolError("被控端已有活动连接")
            self.active = acquired = True
            self.session_tasks.add(task)
            from .media import DesktopEncoder
            from .capture import list_monitors
            from .platform_input import InputSink
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brioche-capture")
            monitors = await self.loop.run_in_executor(executor, list_monitors)
            media = await self._open_media(executor, config, self.monitor, codec)
            self._media = media
            if not self.view_only:
                sink = InputSink(media.monitor)
            state = {"stream_id": 0, "monitor": self.monitor, "pending": None,
                     "switching": False, "last_switch": 0.0, "monitors": monitors}
            await send(writer, pack_control(stream_info(media, monitors, self.monitor, 0, self.view_only)))
            self.status(f"正在共享 · {media.width}×{media.height} · 上限 {config.fps} FPS")
            receiver = asyncio.create_task(self._receive_input(reader, writer, sink, state))
            interval = 1 / config.fps
            frames, process_ms, report_at = 0, 0.0, time.monotonic()
            while not receiver.done():
                started = time.monotonic()
                if state["pending"] is not None:
                    selected, request = state["pending"]
                    state["pending"] = None
                    state["switching"] = True
                    if sink:
                        sink.release_all()
                    try:
                        replacement = await self._open_media(executor, config, selected, codec)
                    except Exception:
                        await send(writer, pack_control({"type": "monitor_error", "request": request,
                                                        "message": "无法打开该显示器，已保留当前画面"}))
                    else:
                        previous, media = media, replacement
                        self._media = media
                        await self.loop.run_in_executor(executor, previous.close)
                        state["monitor"] = selected
                        state["stream_id"] += 1
                        if sink:
                            sink.monitor = media.monitor
                        await send(writer, pack_control(stream_info(
                            media, monitors, selected, state["stream_id"], self.view_only, "stream", request)))
                        self.status(f"正在共享显示器 {selected} · {media.width}×{media.height}")
                        frames, process_ms, report_at = 0, 0.0, time.monotonic()
                    finally:
                        state["switching"] = False
                packets, elapsed = await self.loop.run_in_executor(executor, media.frame, state.pop("force_keyframe", False))
                for packet in packets:
                    await send(writer, pack_video(packet))
                frames += bool(packets)
                process_ms += elapsed
                now = time.monotonic()
                if now - report_at >= 1:
                    await send(writer, pack_control({
                        "type": "stats", "encoded_fps": round(frames / (now - report_at), 1),
                        "capture_encode_ms": round(process_ms / max(frames, 1), 2),
                    }))
                    frames, process_ms, report_at = 0, 0.0, now
                await asyncio.sleep(max(0, interval - (time.monotonic() - started)))
            await receiver
        except asyncio.CancelledError:
            pass
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            pass
        except Exception as exc:
            # Do not echo authentication payloads or secret URIs into logs.
            try:
                await send(writer, pack_control({"type": "error", "message": str(exc)[:300]}))
            except Exception:
                pass
            if acquired:
                self.status(f"连接结束：{exc}")
        finally:
            if receiver:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            if sink:
                try:
                    sink.close()
                except Exception:
                    pass
            if media and executor:
                await self.loop.run_in_executor(executor, media.close)
                if self._media is media:self._media = None
            if executor:
                executor.shutdown(wait=True, cancel_futures=True)
            if acquired:
                self.session_gate.release(task)
                self.active = False
                self.session_tasks.discard(task)
                self.status("等待连接")
            await close_writer(writer)
            self.peer_tasks.discard(task)
            self.connection_tasks.pop(peer, None)

    async def _lan_request(self, reader, writer, message):
        from .lan import CLIENT_ID, PairRequest, pairing_code, target
        if not self.lan_discovery or message.get('version') != VERSION:
            raise ProtocolError('请在被控端开启局域网共享，并更新两端到0.6或更高版本')
        try:
            target(writer.get_extra_info('peername')[0])
        except ValueError as exc:
            raise ProtocolError('局域网入口只接受局域网设备') from exc
        if message['type'] == 'discover':
            await send(writer, pack_control({'type':'device', 'product':'Brioche', 'version':VERSION,
                'name':self.name, 'platform':platform.system(), 'busy':self.session_gate.active}))
            return
        client_id, nonce, name = message.get('client_id'), message.get('nonce'), message.get('name')
        if (not isinstance(client_id, str) or not CLIENT_ID.fullmatch(client_id)
                or not isinstance(nonce, str) or not CLIENT_ID.fullmatch(nonce)
                or not isinstance(name, str) or not 1 <= len(name) <= 64):
            raise ProtocolError('无效配对请求')
        if self._pair_pending:
            raise ProtocolError('已有设备正在等待配对，请稍后重试')
        name = ''.join(c for c in name if c.isprintable()) or '局域网设备'
        request = PairRequest(name, writer.get_extra_info('peername')[0], pairing_code(self.pin, nonce, client_id))
        self._pair_pending = request
        try:
            self.on_pair(request)
            end = time.monotonic() + 30
            while not request.done.is_set() and time.monotonic() < end and not reader.at_eof() and not self.stop_requested.is_set():
                await asyncio.sleep(.1)
            if not request.done.is_set() or not request.allowed or reader.at_eof() or self.stop_requested.is_set():
                raise ProtocolError('配对未被允许、已取消或确认超时')
            token = self.pair_store.grant(client_id, name)
            await send(writer, pack_control({'type':'paired', 'token':token}))
        finally:
            request.answer(False)
            self._pair_pending = None

    async def _open_media(self, executor, config, monitor, codec="h264"):
        from .media import create_encoder
        opening = self.loop.run_in_executor(executor, create_encoder, config,
                                            "mjpeg" if codec == "mjpeg" else self.encoder, monitor)
        try:
            return await asyncio.shield(opening)
        except asyncio.CancelledError:
            # Initialization must finish on its owning thread before cleanup.
            try:
                orphan = await opening
                await self.loop.run_in_executor(executor, orphan.close)
            except Exception:
                pass
            raise

    async def _receive_input(self, reader, writer, sink, state):
        window, count = time.monotonic(), 0
        while True:
            kind, message = await asyncio.wait_for(read_message(reader), 15)
            now = time.monotonic()
            if now - window >= 1:
                window, count = now, 0
            count += 1
            if count > 1000:
                raise ProtocolError("输入消息过于频繁")
            if kind != CONTROL:
                raise ProtocolError("客户端不允许发送视频")
            if message["type"] == "ping":
                stamp = message.get("stamp")
                if type(stamp) not in (float, int):
                    raise ProtocolError("无效 ping")
                await send(writer, pack_control({"type": "pong", "stamp": stamp}))
            elif message["type"] == "input":
                if sink and (message.get("event") == "release" or (
                        not state["switching"] and state["pending"] is None
                        and type(message.get("stream_id")) is int
                        and message["stream_id"] == state["stream_id"])):
                    sink.apply(message)
            elif message["type"] == "switch_monitor":
                monitor, request = message.get("monitor"), message.get("request")
                if type(request) is not int or not 1 <= request <= 1_000_000_000:
                    raise ProtocolError("无效切屏请求")
                if (type(monitor) is not int or monitor not in {m["id"] for m in state["monitors"]}
                        or now - state["last_switch"] < .3):
                    await send(writer, pack_control({"type": "monitor_error", "request": request,
                                                    "message": "显示器不可用或操作过快，请稍后重试"}))
                    continue
                state["last_switch"] = now
                state["pending"] = (monitor, request)
                if sink:
                    sink.release_all()
            elif message["type"] == "request_keyframe":
                if (type(message.get("stream_id")) is int and message["stream_id"] == state["stream_id"]
                        and now - state.get("last_keyframe_request", 0.) >= .25):
                    state["force_keyframe"] = True
                    state["last_keyframe_request"] = now
            elif message["type"] == "bye":
                return
            else:
                raise ProtocolError("未知客户端消息")

    def disconnect_viewer(self):
        if self.loop and self.loop.is_running():
            def cancel():
                for task in tuple(self.session_tasks):
                    task.cancel()
            self.loop.call_soon_threadsafe(cancel)

    def stop(self):
        self.stop_requested.set()
        interrupt = getattr(self._media, "interrupt", None)
        if interrupt:interrupt()
        if self.loop and self.stop_event and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def join(self, timeout=5):
        if self._thread:
            self._thread.join(timeout)


class Viewer:
    """Callbacks run on the network thread. UI must use signals/a latest-frame mailbox."""
    def __init__(self, uri, config, on_frame, on_status=lambda message: None,
                 on_info=lambda info: None, on_stats=lambda stats: None):
        self.address = parse_uri(uri)
        self.config = config
        self.on_frame, self.on_status = on_frame, on_status
        self.on_info, self.on_stats = on_info, on_stats
        self.commands = queue.Queue(maxsize=128)
        self._mouse = None
        self._mouse_lock = threading.Lock()
        self._release = threading.Event()
        self.stopped = threading.Event()
        self.finished = threading.Event()
        self.thread = None
        self.loop = None
        self.task = None
        self._stream_id = 0
        self._switching = threading.Event()
        self._monitor_request = None
        self._request_id = 0
        self._switch_started = 0.0
        self.monitors = []

    def start(self):
        if self.thread:
            raise RuntimeError("连接已经启动")
        self.thread = threading.Thread(target=self._run, name="brioche-viewer", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            asyncio.run(self._connect())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self.stopped.is_set():
                self.on_status(f"连接失败：{exc}")
        finally:
            self.stopped.set()
            self.finished.set()
            self.on_info({"disconnected": True})

    def input(self, event):
        if self.stopped.is_set() or (self._switching.is_set() and event["event"] != "release"):
            return
        message = {"type": "input", **event, "stream_id": self._stream_id}
        if event["event"] == "move":
            with self._mouse_lock:
                self._mouse = message
            return
        if event["event"] == "release":
            self._release.set()
            return
        try:
            self.commands.put_nowait(message)
        except queue.Full:
            # Never silently lose a key-up: discard stale input and release remotely.
            self._release.set()

    def switch_monitor(self, monitor):
        with self._mouse_lock:
            if (self.stopped.is_set() or self._switching.is_set() or type(monitor) is not int
                    or monitor not in {m["id"] for m in self.monitors}):
                return False
            self._switching.set()
            self._switch_started = time.monotonic()
            self._request_id += 1
            self._monitor_request = {"type": "switch_monitor", "monitor": monitor,
                                     "request": self._request_id}
            self._mouse = None
            self._release.set()
        return True

    async def _connect(self):
        from .media import DesktopDecoder
        if self.stopped.is_set():
            return
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        host, port, pin, token = self.address
        self.on_status("正在建立加密连接…")
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            host, port, ssl=client_context(), server_hostname="brioche",
            ssl_handshake_timeout=5), 10)
        sender = None
        try:
            verify_pin(writer.get_extra_info("ssl_object"), pin)
            sock = writer.get_extra_info("socket")
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            await send(writer, pack_control({"type": "hello", "version": VERSION,
                                            "token": token, "config": asdict(self.config)}))
            kind, info = await asyncio.wait_for(read_message(reader), 15)
            if kind != CONTROL or info["type"] != "welcome" or info.get("version") != VERSION:
                raise ProtocolError(info.get("message", "服务端握手失败") if kind == CONTROL else "无效握手")
            w, h = validate_stream_info(info)
            self.monitors = info["monitors"]
            self._stream_id = info["stream_id"]
            self.on_info(info)
            self.on_status("已连接 · 点击画面控制，Ctrl+Alt+Shift 释放输入" if info.get("input")
                           else "已连接 · 只读共享")
            decoder = DesktopDecoder()
            sender = asyncio.create_task(self._send_commands(writer))
            frames, byte_count, since = 0, 0, time.monotonic()
            while not self.stopped.is_set():
                kind, payload = await asyncio.wait_for(read_message(reader), 12)
                if self._switching.is_set() and time.monotonic() - self._switch_started > 10:
                    raise ProtocolError("切换显示器超时，请断开后重新连接")
                if kind == VIDEO:
                    byte_count += len(payload)
                    for frame in decoder.decode(payload):
                        if frame.width != w or frame.height != h:
                            raise ProtocolError("视频尺寸与协商结果不符")
                        if not self._switching.is_set():
                            self.on_frame(frame)
                        frames += 1
                    now = time.monotonic()
                    if now - since >= 1:
                        self.on_stats({"decoded_fps": round(frames / (now - since), 1),
                                       "mbps": round(byte_count * 8 / (now - since) / 1e6, 2)})
                        frames, byte_count, since = 0, 0, now
                elif payload["type"] == "pong":
                    self.on_stats({"rtt_ms": round((time.monotonic() - payload["stamp"]) * 1000, 1)})
                elif payload["type"] == "stats":
                    self.on_stats(payload)
                elif payload["type"] == "stream":
                    w, h = validate_stream_info(payload)
                    if payload["stream_id"] <= self._stream_id:
                        raise ProtocolError("屏幕流序号未递增")
                    decoder = DesktopDecoder()
                    self._stream_id = payload["stream_id"]
                    self.monitors = payload["monitors"]
                    self._switching.clear()
                    self.on_info(payload)
                    frames, byte_count, since = 0, 0, time.monotonic()
                elif payload["type"] == "monitor_error":
                    if payload.get("request") == self._request_id:
                        self._switching.clear()
                        self.on_info({"monitor_error": True, "message": payload.get("message", "切换失败")})
                elif payload["type"] == "error":
                    raise ProtocolError(payload.get("message", "服务端错误"))
                if sender.done():
                    await sender
        finally:
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            if self.stopped.is_set():
                try:
                    await send(writer, pack_control({"type": "input", "event": "release"}))
                    await send(writer, pack_control({"type": "bye"}))
                except Exception:
                    pass
            await close_writer(writer)
            # Let Python 3.10's SSL transport finish its scheduled shutdown callbacks.
            await asyncio.sleep(0.25)

    async def _send_commands(self, writer):
        last_ping = 0.0
        while not self.stopped.is_set():
            if self._release.is_set():
                while True:
                    try:
                        self.commands.get_nowait()
                    except queue.Empty:
                        break
                with self._mouse_lock:
                    self._mouse = None
                self._release.clear()
                await send(writer, pack_control({"type": "input", "event": "release"}))
            with self._mouse_lock:
                mouse, self._mouse = self._mouse, None
                monitor_request, self._monitor_request = self._monitor_request, None
            if monitor_request:
                await send(writer, pack_control(monitor_request))
            if mouse:
                await send(writer, pack_control(mouse))
            for _ in range(32):
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    break
                await send(writer, pack_control(command))
            now = time.monotonic()
            if now - last_ping >= 2:
                await send(writer, pack_control({"type": "ping", "stamp": now}))
                last_ping = now
            await asyncio.sleep(0.005)

    def stop(self):
        self.stopped.set()
        if self.loop and self.task and self.loop.is_running():
            def deadline():
                self.loop.call_later(1, lambda: self.task.cancel() if not self.task.done() else None)
            self.loop.call_soon_threadsafe(deadline)

    def join(self, timeout=5):
        if self.thread:
            self.thread.join(timeout)
