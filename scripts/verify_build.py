"""Exercise the bundled host using a real TLS client; never print access secrets."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import argparse
import asyncio
import hashlib
import ssl
import struct
from urllib.parse import urlsplit
from brioche.protocol import StreamConfig
from brioche.session import Viewer

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--binary", type=Path, default=root / "dist/Brioche/Brioche")
parser.add_argument("--input-tests", action="store_true", help="Inject input; run on an isolated X server")
parser.add_argument("--web-tests", action="store_true")
args = parser.parse_args()


async def browser_check(url, folder):
    import aiohttp
    import av
    parsed = urlsplit(url)
    base = f"https://{parsed.netloc}"
    cert = Path(folder)/"web"/hashlib.sha256(b"127.0.0.1").hexdigest()[:16]/"identity.crt"
    context = ssl.create_default_context(cafile=str(cert))
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context, enable_cleanup_closed=True)) as client:
        for path in ("/", "/app.js", "/app.css", "/video.js"):
            async with client.get(base+path) as response:
                assert response.status == 200
                assert parsed.fragment not in await response.text()
        for codec in ("h264", "mjpeg"):
            ws = await client.ws_connect(base+"/ws", headers={"Origin":base})
            await ws.send_json({"type":"auth", "token":parsed.fragment, "codec":codec,
                                "config":{"resolution":"720p", "fps":60 if codec=="h264" else 30, "bitrate_mbps":4}})
            info = (await asyncio.wait_for(ws.receive(), 12)).json()
            assert info.get("type")=="welcome" and info.get("codec")==codec,info
            decoder = av.CodecContext.create(codec,"r")
            configured = False
            for _ in range(30):
                message = await asyncio.wait_for(ws.receive(), 10)
                if message.type == aiohttp.WSMsgType.TEXT:
                    configured |= message.json().get("type")=="video_config"
                elif message.type == aiohttp.WSMsgType.BINARY:
                    if codec=="h264":
                        from brioche.web_video import H264_HEADER
                        _,frame_id,_,key,_=H264_HEADER.unpack(message.data[:H264_HEADER.size])
                        assert configured and key
                        payload=message.data[H264_HEADER.size:]
                    else:
                        _,frame_id=struct.unpack("!II",message.data[:8])
                        payload=message.data[8:]
                    frames=decoder.decode(av.Packet(payload))
                    assert frames and (frames[0].width,frames[0].height)==(info["width"],info["height"])
                    await ws.send_json({"type":"frame_ack","frame":frame_id})
                    print(f"Bundled browser gateway: HTTPS, assets and {codec} {info['width']}x{info['height']} passed")
                    break
            else:
                raise AssertionError("Bundled gateway produced no browser frame")
            await ws.send_json({"type":"bye"})
            await ws.close()
    await asyncio.sleep(.25)
# The shipped helper must work without a system tailscale executable or daemon.
from brioche.embedded_network import EmbeddedNetwork
with tempfile.TemporaryDirectory() as node_state:
    network = EmbeddedNetwork(folder=node_state, executable=args.binary.parent / "_internal/network/brioche-network")
    try:
        result = network.request("status")
        assert result.get("state") in ("NoState", "Starting", "NeedsLogin", "NeedsMachineAuth", "Running", "Stopped")
    finally:
        network.close()
    print("Bundled tsnet helper: private IPC startup/shutdown passed without system Tailscale")

with tempfile.TemporaryDirectory() as state:
    process = subprocess.Popen([str(args.binary), "host", "--port", "0",
                                *([] if args.input_tests else ["--view-only"]),
                                *(["--web", "--web-port", "0"] if args.web_tests else []), "--state-dir", state],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    viewer = None
    try:
        def read_uri():
            native, browser = None, None
            for line in process.stdout:
                if line.startswith("brioche://"):
                    native = line.strip()
                elif line.startswith("Browser URL:"):
                    browser = line.split(":",1)[1].strip()
                if native and (not args.web_tests or browser):
                    return native, browser
            raise RuntimeError("Bundled host exited before announcing readiness")
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(read_uri)
            try:
                uri, browser_url = future.result(timeout=15)
            except Exception:
                process.terminate()
                raise
        ready, frames, status, infos = threading.Event(), [], [], []
        def frame(value):
            frames.append((value.width, value.height))
            if len(frames) >= 3:
                ready.set()
        viewer = Viewer(uri, StreamConfig("1440p", 90, 20), frame, status.append, infos.append)
        viewer.start()
        assert ready.wait(15), status
        print(f"Bundled host: TLS + H.264 decoded {len(frames)} frames at {frames[0]}")
        # Rebuild the selected display stream inside the same authenticated connection.
        welcome = infos[0]
        time.sleep(.35)
        epoch = viewer._stream_id
        assert viewer.switch_monitor(welcome["monitor"])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and viewer._stream_id <= epoch:
            time.sleep(.01)
        assert viewer._stream_id == epoch + 1, status
        count = len(frames)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and len(frames) <= count:
            time.sleep(.01)
        assert len(frames) > count, status
        print("Bundled stream switch: new H.264 generation decoded without reconnect")
        if args.input_tests:
            from Xlib import X, XK
            from Xlib.display import Display
            from Xlib.ext import xtest
            d = Display()
            previous_focus = d.get_input_focus().focus
            pointer = d.screen().root.query_pointer()
            test_window = d.screen().root.create_window(
                0, 0, 100, 40, 0, d.screen().root_depth, X.InputOutput, X.CopyFromParent,
                override_redirect=True, event_mask=X.KeyPressMask | X.KeyReleaseMask)
            code = d.keysym_to_keycode(XK.string_to_keysym("b"))
            try:
                test_window.map()
                test_window.set_input_focus(X.RevertToParent, X.CurrentTime)
                d.sync()
                assert d.get_input_focus().focus.id == test_window.id
                welcome = infos[0]
                assert welcome["input"]
                expected_x = welcome["source_left"] + round(.25 * (welcome["source_width"] - 1))
                expected_y = welcome["source_top"] + round(.3 * (welcome["source_height"] - 1))
                viewer.input({"event": "move", "x": .25, "y": .3})
                viewer.input({"event": "key", "key": "b", "down": True})
                deadline, got_key, got_mouse = time.monotonic() + 4, False, False
                while time.monotonic() < deadline and not (got_key and got_mouse):
                    position = d.screen().root.query_pointer()
                    got_mouse |= abs(position.root_x - expected_x) <= 1 and abs(position.root_y - expected_y) <= 1
                    while d.pending_events():
                        event = d.next_event()
                        got_key |= event.type == X.KeyPress and event.detail == code
                    time.sleep(.01)
                assert got_mouse and got_key, f"Input failed: mouse={got_mouse}, key={got_key}"
                viewer.stop()
                viewer.join()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    keys = d.query_keymap()
                    if not keys[code // 8] & (1 << (code % 8)):
                        break
                    time.sleep(.01)
                assert not keys[code // 8] & (1 << (code % 8)), "Disconnected key still pressed"
                print("Installed host input: mouse, real keyboard and disconnect release passed")
            finally:
                xtest.fake_input(d, X.KeyRelease, code)
                d.set_input_focus(previous_focus, X.RevertToParent, X.CurrentTime)
                d.screen().root.warp_pointer(pointer.root_x, pointer.root_y)
                test_window.destroy()
                d.sync()
                d.close()
        if args.web_tests:
            viewer.stop()
            viewer.join()
            asyncio.run(browser_check(browser_url, state))
    finally:
        if viewer:
            viewer.stop()
            viewer.join()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        errors = process.stderr.read()
        if errors:
            print(errors)
        assert process.returncode == 0, f"Bundled host exit {process.returncode}"

# Also boot the bundled Qt/X11 UI and request a normal window-manager close.
from Xlib import X
from Xlib.display import Display
from Xlib.protocol.event import ClientMessage
process = subprocess.Popen([str(args.binary)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
display = Display()
window = None
try:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and process.poll() is None:
        for candidate in display.screen().root.query_tree().children:
            try:
                if (candidate.get_wm_name() == "Brioche"
                        and candidate.get_attributes().map_state == X.IsViewable):
                    window = candidate
                    break
            except Exception:
                pass
        if window:
            break
        time.sleep(.05)
    assert window is not None, "Bundled Qt window did not appear"
    window.send_event(ClientMessage(window=window, client_type=display.intern_atom("WM_PROTOCOLS"),
                                    data=(32, [display.intern_atom("WM_DELETE_WINDOW"),
                                               X.CurrentTime, 0, 0, 0])))
    display.flush()
    process.wait(timeout=5)
    assert process.returncode == 0, process.stderr.read()
    print("Bundled Qt/X11 UI: opened and closed normally")
finally:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)
    display.close()
