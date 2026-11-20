import asyncio
import hashlib
import json
import os
from pathlib import Path
import ssl
import struct
import tempfile
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

import aiohttp
from brioche.protocol import StreamConfig
from brioche.security import access_uri
from brioche.session import Host, Viewer
from test_multiscreen import MONITORS, RegionCapture


class WebFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.host = Host(port=0, folder=self.folder.name, view_only=True, web_enabled=True, web_port=0)
        self.host.start()
        self.assertTrue(await asyncio.to_thread(self.host.ready.wait, 10))
        self.assertIsNone(self.host.error)
        parsed = urlsplit(self.host.browser_url)
        self.base = f"https://{parsed.netloc}"
        self.origin = self.base
        self.token = parsed.fragment
        cert = Path(self.folder.name) / "web" / hashlib.sha256(b"127.0.0.1").hexdigest()[:16] / "identity.crt"
        self.context = ssl.create_default_context(cafile=str(cert))
        self.client = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self.context, enable_cleanup_closed=True))
        self.transports = []

    async def asyncTearDown(self):
        await self.client.close()
        # Python 3.10's test-side aiohttp upgraded TLS transport can lose its
        # close flag after server-initiated shutdown. Own and close it explicitly.
        for transport in self.transports:
            transport.abort()
        self.host.stop()
        await asyncio.to_thread(self.host.join, 10)
        self.assertFalse(self.host._thread.is_alive())
        self.folder.cleanup()
        await asyncio.sleep(.25)

    async def connect(self, token=None, fps=30, codec="mjpeg"):
        ws = await self.client.ws_connect(self.base + "/ws", headers={"Origin": self.origin})
        self.transports.append(ws._writer.transport)
        await ws.send_json({"type": "auth", "token": self.token if token is None else token, "codec": codec,
                            "config": {"resolution": "720p", "fps": fps, "bitrate_mbps": 4}})
        return ws

    async def receive(self, ws):
        return await asyncio.wait_for(ws.receive(), 10)

    async def until(self, condition, timeout=5):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            if condition():
                return True
            await asyncio.sleep(.01)
        return False


class WebSecurityTests(WebFixture):
    async def test_https_san_and_static_assets_have_no_credentials(self):
        async with self.client.get(self.base + "/") as response:
            self.assertEqual(response.status, 200)
            html = await response.text()
            self.assertIn("浏览器远程", html)
            self.assertNotIn(self.token, html)
            self.assertNotIn(self.host.token, html)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        for path in ("/app.js", "/app.css", "/video.js"):
            async with self.client.get(self.base + path) as response:
                self.assertEqual(response.status, 200)

    async def test_foreign_host_and_origin_are_denied(self):
        async with self.client.get(self.base + "/", headers={"Host": "evil.example"}) as response:
            self.assertEqual(response.status, 403)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as failure:
            await self.client.ws_connect(self.base + "/ws", headers={"Origin": "https://evil.example"})
        self.assertEqual(failure.exception.status, 403)

    async def test_wrong_and_native_tokens_cannot_open_desktop(self):
        for token in ("wrong", self.host.token):
            ws = await self.connect(token)
            response = await self.receive(ws)
            self.assertEqual(response.json()["type"], "error")
            self.assertFalse(self.host.active)
            await ws.close()

    async def test_browser_high_fps_rejected(self):
        ws = await self.connect(fps=90)
        self.assertIn("30 FPS", (await self.receive(ws)).json()["message"])
        self.assertFalse(self.host.active)
        await ws.close()

    async def test_browser_port_conflict_closes_native_listener(self):
        other = Host(port=0, folder=Path(self.folder.name)/"other", web_enabled=True,
                     web_port=self.host.web_port, view_only=True)
        other.start()
        self.assertTrue(await asyncio.to_thread(other.ready.wait, 8))
        await asyncio.to_thread(other.join, 5)
        self.assertIsNotNone(other.error)
        self.assertFalse(other._thread.is_alive())
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", other.port)


@unittest.skipUnless(os.environ.get("BRIOCHE_DESKTOP_TESTS") == "1", "requires isolated desktop")
class WebDesktopTests(WebFixture):
    async def test_close_handshake_finishes_host_before_immediate_codec_reconnect(self):
        for codec in ("h264", "mjpeg", "h264"):
            ws = await self.connect(codec=codec)
            self.assertEqual((await self.receive(ws)).json()["type"], "welcome")
            await ws.send_json({"type":"bye"})
            await ws.close()
            self.assertFalse(self.host.active)

    async def test_h264_60_and_congestion_idr_recovery(self):
        ws = await self.connect(fps=60,codec="h264")
        info=(await self.receive(ws)).json()
        self.assertEqual((info["codec"],info["fps_limit"]),("h264",60))
        config=None
        packets=[]
        while len(packets)<4:
            response=await self.receive(ws)
            if response.type==aiohttp.WSMsgType.TEXT and response.json()["type"]=="video_config":
                config=response.json()
            elif response.type==aiohttp.WSMsgType.BINARY:
                packets.append(response.data)
        self.assertIsNotNone(config)
        self.assertTrue(config["codec"].startswith("avc1."))
        import av
        decoder=av.CodecContext.create("h264","r")
        frames=[]
        for packet in packets:
            frames.extend(decoder.decode(av.Packet(packet[21:])))
        self.assertTrue(frames)
        self.assertEqual((frames[0].width,frames[0].height),(info["width"],info["height"]))
        await asyncio.sleep(.15)
        first_id=struct.unpack("!IIIBQ",packets[0][:21])[1]
        await ws.send_json({"type":"frame_ack","frame":first_id})
        recovered=None
        while recovered is None:
            response=await self.receive(ws)
            if response.type==aiohttp.WSMsgType.TEXT and response.json()["type"]=="video_config":
                recovered=response.json()
        self.assertGreater(recovered["revision"],config["revision"])
        while True:
            response=await self.receive(ws)
            if response.type==aiohttp.WSMsgType.BINARY:
                break
        epoch,frame_id,revision,key,stamp=struct.unpack("!IIIBQ",response.data[:21])
        self.assertEqual((epoch,revision,key),(0,recovered["revision"],1))
        frame=av.CodecContext.create("h264","r").decode(av.Packet(response.data[21:]))
        self.assertTrue(frame,"Recovery packet was not independently decodable")
        await ws.send_json({"type":"frame_ack","frame":frame_id})
        await ws.close()

    async def jpeg(self, ws, acknowledge=True):
        for _ in range(50):
            response = await self.receive(ws)
            if response.type == aiohttp.WSMsgType.BINARY:
                epoch, frame_id = struct.unpack("!II", response.data[:8])
                self.assertEqual(response.data[8:10], b"\xff\xd8")
                import av
                decoder = av.CodecContext.create("mjpeg", "r")
                frames = decoder.decode(av.Packet(response.data[8:]))
                self.assertEqual(len(frames), 1)
                if acknowledge:
                    await ws.send_json({"type": "frame_ack", "frame": frame_id})
                return epoch, frame_id, frames[0]
            if response.type == aiohttp.WSMsgType.TEXT:
                self.assertNotEqual(response.json().get("type"), "error", response.data)
        self.fail("No JPEG frame received")

    async def test_jpeg_real_desktop_and_bounded_inflight_frames(self):
        ws = await self.connect()
        info = (await self.receive(ws)).json()
        self.assertEqual(info["codec"], "mjpeg")
        self.assertFalse(info["input"])
        epoch, frame_id, frame = await self.jpeg(ws, acknowledge=False)
        self.assertEqual((frame.width, frame.height), (info["width"], info["height"]))
        end = asyncio.get_running_loop().time() + .2
        while asyncio.get_running_loop().time() < end:
            try:
                response = await asyncio.wait_for(ws.receive(), .05)
            except asyncio.TimeoutError:
                continue
            self.assertNotEqual(response.type, aiohttp.WSMsgType.BINARY, "Unacknowledged frames accumulated")
        await ws.send_json({"type": "frame_ack", "frame": frame_id})
        _, next_id, _ = await self.jpeg(ws)
        self.assertGreater(next_id, frame_id)
        from Xlib.display import Display
        d = Display()
        try:
            before = d.screen().root.query_pointer()
            await ws.send_json({"type": "input", "event": "move", "x": 0., "y": 0., "stream_id": epoch})
            await self.jpeg(ws)
            after = d.screen().root.query_pointer()
            self.assertEqual((before.root_x, before.root_y), (after.root_x, after.root_y), "Read-only browser injected input")
        finally:
            d.close()
        await ws.close()
        self.assertTrue(await self.until(lambda: not self.host.active))

    async def test_browser_and_native_connections_are_exclusive(self):
        ws = await self.connect()
        self.assertEqual((await self.receive(ws)).json()["type"], "welcome")
        statuses = []
        viewer = Viewer(access_uri("127.0.0.1", self.host.port, self.host.pin, self.host.token),
                        StreamConfig("720p", 30, 4), lambda f: None, statuses.append)
        viewer.start()
        try:
            self.assertTrue(await asyncio.to_thread(viewer.finished.wait, 6))
            self.assertTrue(any("已有活动连接" in s for s in statuses), statuses)
        finally:
            viewer.stop()
            await asyncio.to_thread(viewer.join)
            await ws.close()

    async def test_browser_portrait_switch_and_failed_switch(self):
        with patch("brioche.capture.list_monitors", return_value=MONITORS), patch("brioche.media.DesktopCapture", RegionCapture):
            ws = await self.connect()
            self.assertEqual((await self.receive(ws)).json()["monitor"], 1)
            await self.jpeg(ws)
            await ws.send_json({"type": "switch_monitor", "monitor": 2, "request": 1})
            while True:
                response = await self.receive(ws)
                if response.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_json({"type": "frame_ack", "frame": struct.unpack("!II", response.data[:8])[1]})
                elif response.type == aiohttp.WSMsgType.TEXT and response.json()["type"] == "stream":
                    info = response.json()
                    break
            self.assertEqual((info["width"], info["height"]), (720, 1280))
            epoch, _, frame = await self.jpeg(ws)
            self.assertEqual(epoch, 1)
            self.assertEqual((frame.width, frame.height), (720, 1280))
            await asyncio.sleep(.35)
            await ws.send_json({"type": "switch_monitor", "monitor": 3, "request": 2})
            while True:
                response = await self.receive(ws)
                if response.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_json({"type": "frame_ack", "frame": struct.unpack("!II", response.data[:8])[1]})
                elif response.type == aiohttp.WSMsgType.TEXT and response.json()["type"] == "monitor_error":
                    break
            epoch, _, frame = await self.jpeg(ws)
            self.assertEqual((epoch, frame.width, frame.height), (1, 720, 1280))
            await ws.close()
            self.assertTrue(await self.until(lambda: not self.host.active))

    async def test_browser_keyboard_disconnect_releases_keys(self):
        from Xlib import XK
        from Xlib.display import Display
        self.host.view_only = False
        d = Display()
        code = d.keysym_to_keycode(XK.string_to_keysym("Shift_L"))
        ws = await self.connect()
        try:
            info = (await self.receive(ws)).json()
            self.assertTrue(info["input"])
            await ws.send_json({"type": "input", "event": "key", "key": "shift", "down": True, "stream_id": 0})
            self.assertTrue(await self.until(lambda: d.query_keymap()[code // 8] & (1 << (code % 8))))
            await ws.close()
            self.assertTrue(await self.until(lambda: not d.query_keymap()[code // 8] & (1 << (code % 8))))
        finally:
            await ws.close()
            d.close()

    async def test_stopping_share_closes_browser_session(self):
        ws = await self.connect()
        self.assertEqual((await self.receive(ws)).json()["type"], "welcome")
        await self.jpeg(ws)
        self.host.stop()
        await asyncio.to_thread(self.host.join, 8)
        self.assertFalse(self.host._thread.is_alive())
        for _ in range(10):
            response = await self.receive(ws)
            if response.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                break
        else:
            self.fail("Browser websocket survived stopped share")
        await ws.close()
        self.assertEqual(self.host.browser_url, "")
