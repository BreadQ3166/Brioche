"""Low-latency Opus audio capture, encoding, decoding and playback.

Uses PyAV's bundled libopus (no extra Python packages) and platform-native
audio backends (PulseAudio/PipeWire on Linux, graceful fallback elsewhere).
"""

import array
from fractions import Fraction
import queue
import shutil
import subprocess
import sys
import threading

import av

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 960 samples per channel
PCM_FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2  # s16le stereo = 3840 bytes


def scale_pcm_s16le(pcm_bytes, volume):
    if volume <= 0.0:
        return b"\x00" * len(pcm_bytes)
    if volume >= 0.999:
        return pcm_bytes
    samples = array.array("h")
    samples.frombytes(pcm_bytes)
    if sys.byteorder != "little":
        samples.byteswap()
    factor = max(0.0, min(1.0, float(volume)))
    for i in range(len(samples)):
        val = int(samples[i] * factor)
        samples[i] = -32768 if val < -32768 else (32767 if val > 32767 else val)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class OpusEncoder:
    """Encodes 48kHz stereo s16le PCM chunks (20ms / 3840 bytes) into Opus packets."""

    def __init__(self, sample_rate=SAMPLE_RATE, channels=CHANNELS, bitrate=96000):
        self.sample_rate = sample_rate
        self.channels = channels
        self.layout = "stereo" if channels == 2 else "mono"
        self.codec = av.CodecContext.create("libopus", "w")
        self.codec.sample_rate = sample_rate
        self.codec.layout = self.layout
        self.codec.format = "s16"
        self.codec.bit_rate = bitrate
        self.codec.time_base = Fraction(1, sample_rate)
        try:
            self.codec.options = {"application": "lowdelay", "frame_duration": str(FRAME_MS)}
        except Exception:
            pass
        self.codec.open()
        self.resampler = av.AudioResampler(
            format=self.codec.format.name,
            layout=self.layout,
            rate=sample_rate,
        )
        self._pts = 0

    def encode_pcm(self, pcm_bytes):
        if len(pcm_bytes) != PCM_FRAME_BYTES:
            raise ValueError(f"Expected {PCM_FRAME_BYTES} PCM bytes, got {len(pcm_bytes)}")
        frame = av.AudioFrame(format="s16", layout=self.layout, samples=FRAME_SAMPLES)
        frame.sample_rate = self.sample_rate
        frame.planes[0].update(pcm_bytes)
        frame.pts = self._pts
        self._pts += FRAME_SAMPLES
        packets = []
        for resampled in self.resampler.resample(frame):
            for pkt in self.codec.encode(resampled):
                data = bytes(pkt)
                if data:
                    packets.append(data)
        return packets

    def close(self):
        self.codec = None


class OpusDecoder:
    """Decodes Opus packets into 48kHz stereo s16le PCM bytes."""

    def __init__(self, sample_rate=SAMPLE_RATE, channels=CHANNELS):
        self.sample_rate = sample_rate
        self.channels = channels
        self.layout = "stereo" if channels == 2 else "mono"
        self.codec = av.CodecContext.create("libopus", "r")
        self.codec.sample_rate = sample_rate
        self.codec.layout = self.layout
        self.codec.open()
        self.resampler = av.AudioResampler(
            format="s16",
            layout=self.layout,
            rate=sample_rate,
        )

    def decode_packet(self, payload):
        chunks = []
        for frame in self.codec.decode(av.Packet(payload)):
            for resampled in self.resampler.resample(frame):
                chunks.append(bytes(resampled.planes[0]))
        return b"".join(chunks)

    def close(self):
        self.codec = None


def _default_pulse_monitor():
    pactl = shutil.which("pactl")
    if not pactl:
        return None
    try:
        proc = subprocess.run(
            [pactl, "get-default-sink"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        sink = proc.stdout.strip()
        if proc.returncode == 0 and sink:
            return f"{sink}.monitor"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def capture_supported():
    if sys.platform.startswith("linux"):
        return bool(shutil.which("parec") and _default_pulse_monitor())
    return False


def playback_supported():
    if sys.platform.startswith("linux"):
        return bool(shutil.which("pacat"))
    return False


class AudioCaptureStream:
    """Background system audio capture -> Opus packets into a bounded drop-oldest queue."""

    def __init__(self, pcm_reader=None):
        self._pcm_reader = pcm_reader
        self._proc = None
        self._stop = threading.Event()
        self._packets = queue.Queue(maxsize=6)
        self._thread = None
        self.active = False
        self.backend = "none"

    def start(self):
        if self._pcm_reader is None:
            if not sys.platform.startswith("linux"):
                return False
            parec = shutil.which("parec")
            monitor = _default_pulse_monitor()
            if not parec or not monitor:
                return False
            try:
                self._proc = subprocess.Popen(
                    [
                        parec,
                        "--format=s16le",
                        f"--rate={SAMPLE_RATE}",
                        f"--channels={CHANNELS}",
                        f"--latency-msec={FRAME_MS}",
                        "-d",
                        monitor,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                return False
            self.backend = "pulse-monitor"
        else:
            self.backend = "custom"
        self.active = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="brioche-audio-capture", daemon=True
        )
        self._thread.start()
        return True

    def _read_exact(self, size):
        if self._pcm_reader is not None:
            return self._pcm_reader(size)
        stream = self._proc.stdout
        buf = bytearray()
        while len(buf) < size and not self._stop.is_set():
            chunk = stream.read(size - len(buf))
            if not chunk:
                return b""
            buf.extend(chunk)
        return bytes(buf)

    def _run(self):
        encoder = None
        try:
            encoder = OpusEncoder()
            while not self._stop.is_set():
                pcm = self._read_exact(PCM_FRAME_BYTES)
                if len(pcm) != PCM_FRAME_BYTES:
                    break
                for pkt in encoder.encode_pcm(pcm):
                    try:
                        self._packets.put_nowait(pkt)
                    except queue.Full:
                        try:
                            self._packets.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            self._packets.put_nowait(pkt)
                        except queue.Full:
                            pass
        except Exception:
            pass
        finally:
            self.active = False
            if encoder:
                encoder.close()

    def drain_packets(self, max_items=4):
        out = []
        for _ in range(max_items):
            try:
                out.append(self._packets.get_nowait())
            except queue.Empty:
                break
        return out

    def close(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=1.5)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(1.5)
        if self._proc and self._proc.stdout:
            try:
                self._proc.stdout.close()
            except OSError:
                pass
        self._proc = None
        self.active = False


class AudioPlayer:
    """Low-latency Opus decoder -> audio sink with live mute & volume scaling."""

    def __init__(self, pcm_writer=None):
        self._pcm_writer = pcm_writer
        self._proc = None
        self._stop = threading.Event()
        self._queue = queue.Queue(maxsize=8)
        self._thread = None
        self._muted = False
        self._volume = 1.0
        self.active = False

    def start(self):
        if self._pcm_writer is None:
            if not sys.platform.startswith("linux"):
                return False
            pacat = shutil.which("pacat")
            if not pacat:
                return False
            try:
                self._proc = subprocess.Popen(
                    [
                        pacat,
                        "--playback",
                        "--format=s16le",
                        f"--rate={SAMPLE_RATE}",
                        f"--channels={CHANNELS}",
                        "--latency-msec=25",
                        "--client-name=Brioche",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                return False
        self.active = True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="brioche-audio-play", daemon=True
        )
        self._thread.start()
        return True

    def set_muted(self, muted):
        self._muted = bool(muted)

    def set_volume(self, volume):
        self._volume = max(0.0, min(1.0, float(volume)))

    def push(self, opus_packet):
        if not self.active or self._stop.is_set() or not opus_packet:
            return
        try:
            self._queue.put_nowait(opus_packet)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(opus_packet)
            except queue.Full:
                pass

    def _write_pcm(self, pcm):
        if self._pcm_writer is not None:
            self._pcm_writer(pcm)
            return
        if self._proc and self._proc.stdin and self._proc.poll() is None:
            self._proc.stdin.write(pcm)
            self._proc.stdin.flush()

    def _run(self):
        decoder = None
        try:
            decoder = OpusDecoder()
            while not self._stop.is_set():
                try:
                    pkt = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if pkt is None:
                    break
                pcm = decoder.decode_packet(pkt)
                if not pcm or self._muted:
                    continue
                if self._volume < 0.999:
                    pcm = scale_pcm_s16le(pcm, self._volume)
                self._write_pcm(pcm)
        except Exception:
            pass
        finally:
            self.active = False
            if decoder:
                decoder.close()

    def close(self):
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._proc:
            if self._proc.stdin:
                try:
                    self._proc.stdin.close()
                except OSError:
                    pass
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=1.5)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(1.5)
        self._proc = None
        self.active = False
