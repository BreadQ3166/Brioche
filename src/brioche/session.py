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

from .clipboard_sync import build_clipboard_message, validate_clipboard_payload
from .protocol import (AUDIO, CONTROL, VIDEO, VERSION, ProtocolError, StreamConfig,
                       pack_audio, pack_control, pack_video, read_message, send)
from .security import client_context, identity, parse_uri, verify_pin

NON_RETRYABLE_HINTS = (
    "证书",
    "凭据",
    "配对",
    "权限",
    "屏幕录制",
    "辅助功能",
    "版本不兼容",
    "已有活动连接",
    "仅查看",
    "不支持",
)


RETRYABLE_HINTS = (
    "超时",
    "硬件视频流中断",
    "硬件编码取帧超时",
    "硬件编码进程已结束",
    "连接意外中断",
)


def tune_socket(writer):
    """Enable TCP_NODELAY and OS TCP keepalive so idle/bursty LAN & Tailscale links stay healthy."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, AttributeError):
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except (OSError, AttributeError):
        return
    try:
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
        elif hasattr(socket, "TCP_KEEPALIVE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 15)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
        if hasattr(socket, "SIO_KEEPALIVE_VALS") and hasattr(sock, "ioctl"):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 15_000, 5_000))
    except (OSError, AttributeError, ValueError):
        pass


def is_retryable_disconnect(exc):
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError)):
        return True
    msg = str(exc)
    if any(hint in msg for hint in NON_RETRYABLE_HINTS):
        return False
    if any(hint in msg for hint in RETRYABLE_HINTS):
        return True
    if isinstance(exc, (ProtocolError, ValueError)):
        return False
    return isinstance(exc, OSError)


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


def stream_info(media, monitors, selected, epoch, view_only, kind="welcome", request=0, files=False, clipboard=False, audio=False):
    return {
        "type": kind, "version": VERSION, "codec": media.codec_name,
        "width": media.width, "height": media.height,
        "source_width": media.monitor["width"], "source_height": media.monitor["height"],
        'capture_width':getattr(media,'capture_width',media.monitor['width']),
        'capture_height':getattr(media,'capture_height',media.monitor['height']),
        "source_left": media.monitor["left"], "source_top": media.monitor["top"],
        "fps_limit": int(media.codec.framerate), "encoder": media.encoder_name,
        "capture": media.capture.backend, "encoder_fallback": getattr(media,"fallback_reason",""), "input": not view_only,
        "timing_scope": "packet_wait" if getattr(media, "paced", False) else "capture_encode",
        'files':bool(files and not view_only),
        "clipboard": bool(clipboard and not view_only),
        "audio": bool(audio),
        "audio_codec": "opus" if audio else "",
        "platform": platform.system(),
        "monitors": monitors, "monitor": selected, "stream_id": epoch, "request": request,
    }


def validate_stream_info(info):
    w, h = info.get("width"), info.get("height")
    if (type(w) is not int or type(h) is not int or min(w, h) < 2
            or max(w, h) > 3840 or min(w, h) > 2160):
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
                 on_pair=lambda request: request.answer(False), session_gate=None, name=None,
                 files_enabled=False, on_transfer=lambda request: request.respond(),
                 on_transfer_progress=lambda value: None, file_gate=None,
                 clipboard_enabled=True, on_clipboard=lambda text: None,
                 audio_enabled=True, on_session_state=lambda state: None,
                 file_browser_enabled=True, v2_core_enabled=False):
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
        self._sink = None
        self.v2_core_enabled = bool(v2_core_enabled)
        self._core_host = None
        self._core_lock = threading.Lock()
        self.session_gate = session_gate or SessionGate()
        self.file_gate = file_gate or SessionGate()
        self.files_enabled, self.on_transfer = files_enabled, on_transfer
        self.on_transfer_progress = on_transfer_progress
        self.file_browser_enabled = bool(file_browser_enabled)
        self.clipboard_enabled = clipboard_enabled and not view_only
        self.on_clipboard = on_clipboard
        self.audio_enabled = bool(audio_enabled)
        self.on_session_state = on_session_state
        self.input_paused = False
        self._pending_clipboard = None
        self._clipboard_lock = threading.Lock()
        self.transfer_requests = set()
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
        self._active_session = None
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
                                            ssl_handshake_timeout=8, backlog=16)
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
            self._stop_core_host()
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
        display_lease = None
        sink = None
        receiver = None
        audio_stream = None
        try:
            tune_socket(writer)
            write_lock = asyncio.Lock()
            # Bound unauthenticated peers and brute-force attempts. Tokens are 256-bit.
            now = time.monotonic()
            self._failures = [t for t in self._failures if now - t < 60]
            if len(self.peer_tasks) > 16 or len(self._failures) >= 20:
                return
            kind, hello = await asyncio.wait_for(read_message(reader), 12)
            if kind == CONTROL and hello.get("type") in ("discover", "pair"):
                await self._lan_request(reader, writer, hello)
                return
            if kind == CONTROL and hello.get('type') == 'file_transfer':
                if (hello.get('version') != VERSION or not isinstance(hello.get('token'), str)
                        or not hello['token'].isascii() or not (secrets.compare_digest(hello['token'], self.token)
                        or (self.pair_store and self.pair_store.accepts(hello['token'])))):
                    raise ProtocolError('文件连接认证失败')
                if self.view_only or not self.files_enabled:
                    raise ProtocolError('被控端未允许文件互传，或当前是仅查看共享')
                await self._file_session(reader, writer, hello)
                return
            if kind == CONTROL and hello.get('type') == 'file_browser':
                if (hello.get('version') != VERSION or not isinstance(hello.get('token'), str)
                        or not hello['token'].isascii() or not (secrets.compare_digest(hello['token'], self.token)
                        or (self.pair_store and self.pair_store.accepts(hello['token'])))):
                    raise ProtocolError('文件浏览认证失败')
                if self.view_only or not self.files_enabled or not self.file_browser_enabled:
                    raise ProtocolError('被控端未允许直接浏览目录，或当前是仅查看共享')
                await self._file_browser_session(reader, writer)
                return
            if kind == CONTROL and hello.get('type') == 'v2_negotiate':
                if (hello.get('version') != VERSION or not isinstance(hello.get('token'), str)
                        or not hello['token'].isascii() or not (secrets.compare_digest(hello['token'], self.token)
                        or (self.pair_store and self.pair_store.accepts(hello['token'])))):
                    raise ProtocolError('v2 引擎认证失败')
                ep = self._ensure_core_host()
                if ep is None:
                    await send(writer, pack_control({"type": "v2_unavailable", "reason": "v2_core_disabled_or_missing"}))
                else:
                    await send(writer, pack_control({
                        "type": "v2_ready",
                        "quic_port": ep.quic_port,
                        "pin": ep.pin,
                        "key": ep.key,
                        "monitor": ep.monitor,
                    }))
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
                # Allow seamless reclaim when the SAME authenticated peer auto-reconnects
                # (or when an old half-open session has gone silent > 12s).
                peer_ip = str(peer[0]) if peer else ""
                active_meta = self._active_session
                reclaim = False
                if active_meta is not None:
                    is_same_peer = bool(peer_ip) and active_meta.get("peer_ip") == peer_ip
                    is_stale = (time.monotonic() - active_meta.get("last_rx", 0.0)) > 12.0
                    if (is_same_peer and bool(hello.get("reconnect"))) or is_stale:
                        reclaim = True
                        old_task = active_meta.get("task")
                        if old_task is not None and not old_task.done():
                            old_task.cancel()
                if reclaim:
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        if self.session_gate.acquire(task):
                            break
                        await asyncio.sleep(0.05)
                    else:
                        raise ProtocolError("被控端已有活动连接")
                else:
                    raise ProtocolError("被控端已有活动连接")
            self.active = acquired = True
            self.session_tasks.add(task)
            self._active_session = {
                "task": task,
                "peer_ip": str(peer[0]) if peer else "",
                "last_rx": time.monotonic(),
            }
            if platform.system() == 'Darwin':
                from .mac_permissions import require_host
                require_host(self.view_only)
            from .media import DesktopEncoder
            from .capture import list_monitors
            from .platform_input import InputSink
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brioche-capture")
            if config.match_display and config.resolution != 'native':
                if self.view_only:
                    raise ProtocolError('仅查看共享不允许调整源屏，请取消源屏匹配')
                display_lease = await self._match_display(executor, config, self.monitor)
            monitors = await self.loop.run_in_executor(executor, list_monitors)
            media = await self._open_media(executor, config, self.monitor, codec)
            self._media = media
            if not self.view_only:
                sink = InputSink(media.monitor)
                self._sink = sink
            self.input_paused = False
            audio_ready = False
            if self.audio_enabled and bool(hello.get("audio", False)):
                from .audio import AudioCaptureStream
                candidate = AudioCaptureStream()
                if candidate.start():
                    audio_stream = candidate
                    audio_ready = True
            state = {"stream_id": 0, "monitor": self.monitor, "pending": None,
                     "switching": False, "last_switch": 0.0, "monitors": monitors}
            await send(writer, pack_control(stream_info(media, monitors, self.monitor, 0, self.view_only, files=self.files_enabled, clipboard=self.clipboard_enabled, audio=audio_ready)), lock=write_lock)
            self.on_session_state({
                "active": True,
                "peer": str(peer[0]) if peer else "",
                "view_only": self.view_only,
                "audio": audio_ready,
                "input_paused": self.input_paused,
            })
            self.status(f"正在共享 · {media.width}×{media.height} · 上限 {config.fps} FPS")
            receiver = asyncio.create_task(self._receive_input(reader, writer, sink, state, write_lock))
            interval = 1 / config.fps
            frames, process_ms, report_at = 0, 0.0, time.monotonic()
            send_ms = 0.0
            age_ms, age_frames = 0.0, 0
            while not receiver.done():
                started = time.monotonic()
                if state["pending"] is not None:
                    selected, request = state["pending"]
                    state["pending"] = None
                    state["switching"] = True
                    if sink:
                        sink.release_all()
                    try:
                        if display_lease:
                            await self._match_display(executor, config, selected, display_lease)
                            state['monitors'] = monitors = await self.loop.run_in_executor(executor, list_monitors)
                        replacement = await self._open_media(executor, config, selected, codec)
                    except Exception:
                        await send(writer, pack_control({"type": "monitor_error", "request": request,
                                                        "message": "无法打开该显示器，已保留当前画面"}), lock=write_lock)
                    else:
                        previous, media = media, replacement
                        self._media = media
                        await self.loop.run_in_executor(executor, previous.close)
                        state["monitor"] = selected
                        state["stream_id"] += 1
                        if sink:
                            sink.monitor = media.monitor
                        await send(writer, pack_control(stream_info(
                            media, monitors, selected, state["stream_id"], self.view_only, "stream", request, files=self.files_enabled, clipboard=self.clipboard_enabled, audio=audio_ready)), lock=write_lock)
                        self.status(f"正在共享显示器 {selected} · {media.width}×{media.height}")
                        frames, process_ms, report_at = 0, 0.0, time.monotonic()
                        send_ms = 0.0
                        age_ms, age_frames = 0.0, 0
                    finally:
                        state["switching"] = False
                pending_clip = self._take_pending_clipboard()
                if pending_clip is not None and self.clipboard_enabled and not self.view_only:
                    await send(writer, pack_control(pending_clip), lock=write_lock)
                if audio_stream is not None and self.audio_enabled:
                    for audio_pkt in audio_stream.drain_packets():
                        await send(writer, pack_audio(audio_pkt), lock=write_lock)
                packets, elapsed = await self.loop.run_in_executor(executor, media.frame, state.pop("force_keyframe", False))
                sending = time.perf_counter()
                age = getattr(media, 'capture_age_ms', None)
                if age is not None:
                    age_ms += age
                    age_frames += 1
                for packet in packets:
                    await send(writer, pack_video(packet), lock=write_lock)
                send_ms += (time.perf_counter() - sending) * 1000
                frames += bool(packets)
                process_ms += elapsed
                now = time.monotonic()
                if now - report_at >= 1:
                    await send(writer, pack_control({
                        "type": "stats", "encoded_fps": round(frames / (now - report_at), 1),
                        "capture_encode_ms": round(process_ms / max(frames, 1), 2),
                        "send_ms": round(send_ms / max(frames, 1), 2),
                        "capture_age_ms": round(age_ms / age_frames, 2) if age_frames else None,
                    }), lock=write_lock)
                    frames, process_ms, report_at = 0, 0.0, now
                    send_ms = 0.0
                    age_ms, age_frames = 0.0, 0
                if not getattr(media, "paced", False):
                    await asyncio.sleep(max(0, interval - (time.monotonic() - started)))
            await receiver
        except asyncio.CancelledError:
            pass
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            pass
        except Exception as exc:
            # Do not echo authentication payloads or secret URIs into logs.
            try:
                await send(writer, pack_control({"type": "error", "message": str(exc)[:300]}), lock=write_lock)
            except Exception:
                pass
            if acquired:
                self.status(f"连接结束：{exc}")
        finally:
            if receiver:
                receiver.cancel()
            if audio_stream:
                try:
                    audio_stream.close()
                except Exception:
                    pass
            if self._sink is sink:
                self._sink = None
            if sink:
                try:
                    sink.close()
                except Exception:
                    pass
            closing = None
            if executor and (media or display_lease):
                def cleanup():
                    try:
                        if media:media.close()
                    finally:
                        if display_lease:display_lease.close()
                closing = self.loop.run_in_executor(executor, cleanup)
            try:
                try:
                    if receiver:
                        await asyncio.gather(receiver, return_exceptions=True)
                    if closing:
                        await asyncio.shield(closing)
                except asyncio.CancelledError:
                    if closing:
                        await closing
            except Exception as exc:
                self.status(f'会话清理：{exc}')
            finally:
                if self._media is media:self._media = None
                if executor:
                    executor.shutdown(wait=True, cancel_futures=True)
                if acquired:
                    self.session_gate.release(task)
                    self.active = False
                    self.input_paused = False
                    self.session_tasks.discard(task)
                    if self._active_session and self._active_session.get("task") is task:
                        self._active_session = None
                    self.on_session_state({"active": False})
                    self.status("等待连接")
            await close_writer(writer)
            self.peer_tasks.discard(task)
            self.connection_tasks.pop(peer, None)

    async def _file_browser_session(self, reader, writer):
        from pathlib import Path
        from .file_browser import handle_browser_command, local_roots
        from .file_transfer import TransferError

        await send(
            writer,
            pack_control(
                {
                    "type": "fb_ready",
                    "platform": platform.system(),
                    "home": str(Path.home()),
                    "roots": local_roots(),
                }
            ),
        )
        while not self.stop_requested.is_set():
            try:
                kind, message = await asyncio.wait_for(read_message(reader), 120)
            except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
                return
            if kind != CONTROL:
                raise ProtocolError("无效文件浏览请求")
            if message.get("type") == "fb_bye":
                return
            if self.view_only or not self.files_enabled or not self.file_browser_enabled:
                await send(writer, pack_control({"type": "fb_error", "message": "被控端已关闭直接文件浏览权限"}))
                return
            try:
                reply = await asyncio.to_thread(handle_browser_command, message)
            except TransferError as exc:
                await send(writer, pack_control({"type": "fb_error", "message": str(exc)[:200]}))
            except Exception:
                await send(writer, pack_control({"type": "fb_error", "message": "远端目录操作失败"}))
            else:
                await send(writer, pack_control(reply))

    async def _file_session(self, reader, writer, hello):
        from .file_transfer import FileConsent, TransferError, validate_manifest, sources, send_files, receive_files
        from .file_browser import _resolve_dir
        task = asyncio.current_task()
        if not self.file_gate.acquire(task):
            raise ProtocolError('已有文件正在传输，请等待完成')
        request = None
        last_progress = 0.0
        task_id = secrets.token_hex(12)
        try:
            direction = hello.get('direction')
            if direction not in ('upload', 'download'):
                raise TransferError('无效文件传输方向')
            items = validate_manifest(hello.get('files')) if direction == 'upload' else []
            direct_target = hello.get('target_dir') if direction == 'upload' else None
            direct_sources = hello.get('remote_paths') if direction == 'download' else None
            unattended = bool(
                self.file_browser_enabled
                and not self.view_only
                and (direct_target is not None or direct_sources is not None)
            )
            if unattended:
                if direction == 'upload':
                    selection = str(await asyncio.to_thread(_resolve_dir, direct_target))
                else:
                    if not isinstance(direct_sources, list) or not direct_sources or not all(isinstance(p, str) for p in direct_sources):
                        raise TransferError('无效远端文件路径')
                    selection = direct_sources
            else:
                request = FileConsent('receive' if direction == 'upload' else 'send', writer.get_extra_info('peername')[0], items)
                task_id = request.id
                self.transfer_requests.add(request)
                self.on_transfer(request)
                selection = await request.wait(reader, self.stop_requested)
            def progress(completed, total, name):
                nonlocal last_progress
                now = time.monotonic()
                if now - last_progress >= .1 or name == '完成':
                    last_progress = now
                    self.on_transfer_progress({'id': task_id, 'completed': completed, 'total': total,
                                               'name': name, 'status': '传输中'})
            if direction == 'upload':
                await receive_files(reader, writer, selection, items, progress)
            else:
                paths, items = await asyncio.to_thread(sources, selection)
                await send(writer, pack_control({'type': 'file_offer', 'files': items}))
                await send_files(reader, writer, paths, items, progress)
            self.on_transfer_progress({'id': task_id, 'finished': True, 'status': '已完成 · SHA256 校验通过'})
        except asyncio.CancelledError:
            if request:self.on_transfer_progress({'id': task_id, 'finished': True, 'status': '已取消'})
            raise
        except Exception as exc:
            message = str(exc)[:200] if isinstance(exc, TransferError) else '文件传输中断，请检查连接、权限或磁盘空间'
            try:await send(writer, pack_control({'type': 'file_error', 'message': message}))
            except Exception:pass
            if request:self.on_transfer_progress({'id': task_id, 'finished': True, 'status': message})
        finally:
            if request:
                request.respond();self.transfer_requests.discard(request)
            self.file_gate.release(task)

    def set_input_paused(self, paused):
        self.input_paused = bool(paused)
        if self.input_paused and self._sink:
            try:
                self._sink.release_all()
            except Exception:
                pass
        if self.active:
            self.on_session_state({
                "active": True,
                "view_only": self.view_only,
                "input_paused": self.input_paused,
            })

    def push_clipboard(self, text):
        if not self.active or self.view_only or not self.clipboard_enabled:
            return False
        message = build_clipboard_message(text)
        if not message:
            return False
        with self._clipboard_lock:
            self._pending_clipboard = message
        return True

    def _take_pending_clipboard(self):
        with self._clipboard_lock:
            msg, self._pending_clipboard = self._pending_clipboard, None
        return msg

    def cancel_transfers(self):
        for request in tuple(self.transfer_requests):request.cancel()

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

    async def _match_display(self, executor, config, monitor, lease=None):
        from .display_mode import DisplayLease
        from .capture import list_monitors
        def prepare():
            active = lease or DisplayLease(self.folder)
            try:
                displays = list_monitors()
                if not 1 <= monitor <= len(displays):
                    raise ValueError('显示器不存在')
                active.apply(displays[monitor - 1], config.resolution, config.fps)
                return active
            except Exception:
                if lease is None:active.close()
                raise
        opening = self.loop.run_in_executor(executor, prepare)
        try:
            return await asyncio.shield(opening)
        except asyncio.CancelledError:
            try:
                orphan = await opening
                await self.loop.run_in_executor(executor, orphan.close)
            except Exception:
                pass
            raise

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

    async def _receive_input(self, reader, writer, sink, state, write_lock=None):
        window, count = time.monotonic(), 0
        while True:
            kind, message = await asyncio.wait_for(read_message(reader), 30)
            now = time.monotonic()
            if self._active_session is not None:
                self._active_session["last_rx"] = now
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
                await send(writer, pack_control({"type": "pong", "stamp": stamp}), lock=write_lock)
            elif message["type"] == "input":
                if sink and (message.get("event") == "release" or (
                        not self.input_paused
                        and not state["switching"] and state["pending"] is None
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
                                                    "message": "显示器不可用或操作过快，请稍后重试"}), lock=write_lock)
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
            elif message["type"] == "clipboard":
                if self.clipboard_enabled and not self.view_only:
                    text, _ = validate_clipboard_payload(message)
                    self.on_clipboard(text)
            elif message["type"] == "bye":
                return
            else:
                raise ProtocolError("未知客户端消息")

    def disconnect_viewer(self):
        self.cancel_transfers()
        if self.loop and self.loop.is_running():
            def cancel():
                for task in tuple(self.session_tasks):
                    task.cancel()
            self.loop.call_soon_threadsafe(cancel)

    def _ensure_core_host(self):
        if not self.v2_core_enabled:
            return None
        with self._core_lock:
            if self._core_host is not None and self._core_host.process is not None and self._core_host.process.poll() is None:
                return self._core_host.endpoint
            from .core_engine import CoreHostProcess, find_core_binary

            exe = find_core_binary()
            if exe is None:
                return None
            proc = CoreHostProcess(
                bind=self.bind,
                quic_port=0,
                monitor=self.monitor,
                view_only=self.view_only,
                binary=exe,
            )
            try:
                ep = proc.start()
                self._core_host = proc
                return ep
            except Exception:
                proc.stop()
                return None

    def _stop_core_host(self):
        with self._core_lock:
            proc, self._core_host = self._core_host, None
        if proc is not None:
            proc.stop()

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
                 on_info=lambda info: None, on_stats=lambda stats: None,
                 on_clipboard=lambda text: None, audio_enabled=True,
                 on_audio=lambda payload: None, reconnect=False):
        self.address = parse_uri(uri)
        self.config = config
        self.on_frame, self.on_status = on_frame, on_status
        self.on_info, self.on_stats = on_info, on_stats
        self.on_clipboard = on_clipboard
        self.audio_enabled = bool(audio_enabled)
        self.on_audio = on_audio
        self.reconnect = bool(reconnect)
        self.commands = queue.Queue(maxsize=128)
        self._mouse = None
        self._mouse_lock = threading.Lock()
        self._clipboard_out = None
        self._clipboard_lock = threading.Lock()
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
        self._connected_once = False

    def start(self):
        if self.thread:
            raise RuntimeError("连接已经启动")
        self.thread = threading.Thread(target=self._run, name="brioche-viewer", daemon=True)
        self.thread.start()

    def _run(self):
        retryable = False
        reason = ""
        try:
            asyncio.run(self._connect())
            if not self.stopped.is_set() and self._connected_once:
                retryable = True
                reason = "连接意外中断"
        except asyncio.CancelledError:
            pass
        except (TimeoutError, asyncio.TimeoutError) as exc:
            if not self.stopped.is_set():
                retryable = True
                reason = "等待被控端响应超时"
                self.on_status('连接失败：等待被控端画面超时；请检查对方屏幕录制权限、编码器和共享状态')
        except Exception as exc:
            if not self.stopped.is_set():
                retryable = is_retryable_disconnect(exc)
                reason = str(exc)[:200]
                self.on_status(f"连接失败：{exc}")
        finally:
            user_stopped = self.stopped.is_set()
            self.stopped.set()
            self.finished.set()
            self.on_info({
                "disconnected": True,
                "retryable": retryable and not user_stopped,
                "reason": reason,
            })

    def send_clipboard(self, text):
        if self.stopped.is_set():
            return False
        message = build_clipboard_message(text)
        if not message:
            return False
        with self._clipboard_lock:
            self._clipboard_out = message
        return True

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
            ssl_handshake_timeout=8), 15)
        sender = None
        try:
            verify_pin(writer.get_extra_info("ssl_object"), pin)
            tune_socket(writer)
            hello_msg = {
                "type": "hello",
                "version": VERSION,
                "token": token,
                "config": self.config.to_wire(),
                "audio": self.audio_enabled,
            }
            if self.reconnect:
                hello_msg["reconnect"] = True
            await send(writer, pack_control(hello_msg))
            kind, info = await asyncio.wait_for(read_message(reader), 25)
            if kind != CONTROL or info["type"] != "welcome" or info.get("version") != VERSION:
                raise ProtocolError(info.get("message", "服务端握手失败") if kind == CONTROL else "无效握手")
            w, h = validate_stream_info(info)
            self._connected_once = True
            self.monitors = info["monitors"]
            self._stream_id = info["stream_id"]
            self.on_info(info)
            self.on_status("已连接 · 点击画面控制，Ctrl+Alt+Shift 释放输入" if info.get("input")
                           else "已连接 · 只读共享")
            decoder = DesktopDecoder()
            sender = asyncio.create_task(self._send_commands(writer))
            frames, byte_count, since = 0, 0, time.monotonic()
            decode_ms = convert_ms = submit_ms = 0.0
            while not self.stopped.is_set():
                kind, payload = await asyncio.wait_for(read_message(reader), 25)
                if self._switching.is_set() and time.monotonic() - self._switch_started > 15:
                    raise ProtocolError("切换显示器超时，请断开后重新连接")
                if kind == VIDEO:
                    byte_count += len(payload)
                    decoded = decoder.decode(payload)
                    decode_ms += decoder.decode_ms
                    convert_ms += decoder.convert_ms
                    submitting = time.perf_counter()
                    for frame in decoded:
                        if frame.width != w or frame.height != h:
                            raise ProtocolError("视频尺寸与协商结果不符")
                        if not self._switching.is_set():
                            self.on_frame(frame)
                        frames += 1
                    submit_ms += (time.perf_counter() - submitting) * 1000
                    now = time.monotonic()
                    if now - since >= 1:
                        self.on_stats({"decoded_fps": round(frames / (now - since), 1),
                                       "mbps": round(byte_count * 8 / (now - since) / 1e6, 2),
                                       "decode_ms": round(decode_ms / max(1, frames), 2),
                                       "convert_ms": round(convert_ms / max(1, frames), 2),
                                       "submit_ms": round(submit_ms / max(1, frames), 2)})
                        frames, byte_count, since = 0, 0, now
                        decode_ms = convert_ms = submit_ms = 0.0
                elif kind == AUDIO:
                    if self.audio_enabled:
                        self.on_audio(payload)
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
                    decode_ms = convert_ms = submit_ms = 0.0
                elif payload["type"] == "monitor_error":
                    if payload.get("request") == self._request_id:
                        self._switching.clear()
                        self.on_info({"monitor_error": True, "message": payload.get("message", "切换失败")})
                elif payload["type"] == "clipboard":
                    text, _ = validate_clipboard_payload(payload)
                    self.on_clipboard(text)
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
            with self._clipboard_lock:
                clip_msg, self._clipboard_out = self._clipboard_out, None
            if clip_msg:
                await send(writer, pack_control(clip_msg))
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
