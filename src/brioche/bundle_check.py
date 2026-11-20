"""Deterministic packaged-runtime check; no desktop capture, login or input injection."""
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
from fractions import Fraction
from importlib.resources import files
from . import __version__


def run(output):
    result = {'version': __version__, 'system': platform.system(), 'pass': False,
              'scope': 'runtime loading and synthetic H.264; no real desktop or cross-network acceptance'}
    try:
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        app.processEvents()
        result['qt'] = 'initialized'
        import av
        encoder = av.CodecContext.create('libx264', 'w')
        encoder.width = encoder.height = 32
        encoder.pix_fmt = 'yuv420p'
        encoder.time_base = Fraction(1, 30)
        encoder.options = {'preset': 'ultrafast', 'tune': 'zerolatency'}
        encoder.open()
        frame = av.VideoFrame(32, 32, 'yuv420p')
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.pts = 0
        packets = list(encoder.encode(frame)) + list(encoder.encode(None))
        decoder = av.CodecContext.create('h264', 'r')
        decoded = [f for packet in packets for f in decoder.decode(av.Packet(bytes(packet)))]
        assert decoded and (decoded[0].width, decoded[0].height) == (32, 32)
        result['h264'] = 'synthetic encode/decode passed'
        from .security import identity
        with tempfile.TemporaryDirectory() as state:
            context, pin = identity(state)
            assert len(pin) == 64
        result['tls'] = 'identity and TLS context initialized'
        for name in ('index.html', 'app.js', 'video.js', 'app.css'):
            assert files('brioche').joinpath('web', name).read_bytes()
        result['browser_assets'] = 'present'
        from .embedded_network import helper_path
        helper = helper_path()
        assert helper.is_file()
        process = subprocess.run([str(helper), '--help'], capture_output=True, timeout=15)
        assert process.returncode == 0
        result['embedded_network'] = 'native executable loads; account login not attempted'
        if platform.system() == 'Windows':
            import pynput.keyboard
            import pynput.mouse
            result['input_modules'] = 'imported; input not injected'
        result['pass'] = True
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if result['pass'] else 1
