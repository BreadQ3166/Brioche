"""Replaceable H.264 CPU-frame backend; owned by one dedicated worker thread."""
from fractions import Fraction
import os
import sys
import time
import av
from .capture import DesktopCapture
from .protocol import ENCODERS, RESOLUTIONS
from .h264 import annexb_access_unit


def _frame_signature(frame):
    """Sub-millisecond sampled pixel hash for idle frame pacing (0.2ms @ 4K)."""
    plane = frame.planes[0]
    buf = memoryview(plane)
    total = len(buf)
    if total == 0:
        return 0
    step = max(64, total // 192)
    sample = bytes(buf[::step])
    return hash((frame.width, frame.height, sample))


class DesktopEncoder:
    def __init__(self, config, encoder="libx264", monitor_index=1, capture_backend='auto', idle_pacing=True):
        if encoder not in ENCODERS + ("mjpeg",):
            raise ValueError("编码器不在允许列表中")
        self.capture = (DesktopCapture(monitor_index, config.fps) if capture_backend == 'auto'
                        else DesktopCapture(monitor_index, config.fps, backend=capture_backend))
        try:
            self.monitor = self.capture.monitor
            self.initial_image = self.capture.grab() if sys.platform == 'darwin' else None
            # Retina pixels differ from the logical coordinates used by input.
            self.capture_width = self.initial_image.width if self.initial_image is not None else self.monitor['width']
            self.capture_height = self.initial_image.height if self.initial_image is not None else self.monitor['height']
            max_w, max_h = RESOLUTIONS[config.resolution]
            if self.capture_height > self.capture_width:
                max_w, max_h = max_h, max_w
            scale = min(max_w / self.capture_width, max_h / self.capture_height, 1.0)
            self.width = max(2, int(self.capture_width * scale) // 2 * 2)
            self.height = max(2, int(self.capture_height * scale) // 2 * 2)
            self.encoder_name = encoder
            self.codec_name = "mjpeg" if encoder == "mjpeg" else "h264"
            self.codec = av.CodecContext.create(encoder, "w")
            self.codec.width, self.codec.height = self.width, self.height
            self.codec.pix_fmt = "yuvj420p" if encoder == "mjpeg" else "yuv420p"
            self.codec.time_base = Fraction(1, config.fps)
            self.codec.framerate = Fraction(config.fps, 1)
            self.codec.bit_rate = config.bitrate_mbps * 1_000_000
            self.codec.gop_size = max(30, config.fps * 2) if encoder == "h264_videotoolbox" else config.fps
            self.codec.max_b_frames = 0
            self.interpolation = "BICUBIC"
            if encoder == "libx264":
                self.codec.thread_count = min(8, os.cpu_count() or 1)
                self.codec.options = {"preset": "ultrafast", "tune": "zerolatency",
                                      "forced-idr": "1",
                                      "x264-params": "repeat-headers=1:scenecut=0"}
            elif encoder == "h264_nvenc":
                self.codec.options = {"preset": "p1", "tune": "ull", "zerolatency": "1"}
            elif encoder == "h264_videotoolbox":
                self.codec.options = {"realtime": "1", "allow_sw": "0"}
            elif encoder == "mjpeg":
                self.codec.thread_count = 1
                self.codec.options = {"qmin": "4", "qmax": "8"}
            self.codec.open()
            self.pts = 0
            self.idle_pacing = bool(idle_pacing) and encoder != "mjpeg"
            self._last_sig = None
            self._last_encoded_at = 0.0
        except Exception:
            self.capture.close()
            raise

    def frame(self, force_keyframe=False):
        primed = getattr(self, '_primed', None)
        if primed is not None:
            self._primed = None
            return primed
        start = time.perf_counter()
        image = self.initial_image
        self.initial_image = None
        if image is None:image = self.capture.grab()
        now = time.monotonic()
        if self.idle_pacing and not force_keyframe and self.pts >= 3:
            sig = _frame_signature(image)
            if sig == self._last_sig and (now - self._last_encoded_at) < 0.5:
                return [], (time.perf_counter() - start) * 1000
            self._last_sig = sig
        image = image.reformat(width=self.width, height=self.height, format=self.codec.pix_fmt,
                               interpolation=self.interpolation)
        image.pts = self.pts
        image.time_base = self.codec.time_base
        if force_keyframe and self.codec_name == "h264":
            image.pict_type = av.video.frame.PictureType.I
        self.pts += 1
        self._last_encoded_at = now
        packets = [annexb_access_unit(bytes(p), self.codec.extradata, p.is_keyframe)
                   if self.codec_name == "h264" else bytes(p) for p in self.codec.encode(image)]
        return packets, (time.perf_counter() - start) * 1000

    def close(self):
        self.capture.close()
        self.codec = None


class DesktopDecoder:
    """Multi-codec H.264/HEVC/AV1 decoder with HW acceleration probe and zero-reformat YUV output."""

    def __init__(self, codec="h264", output_format="bgra"):
        self.codec_name = codec if codec in ("h264", "hevc", "av1") else "h264"
        self.output_format = output_format  # "yuv" (zero-copy GPU shader) or "bgra" (QImage fallback)
        self.hw_accelerated = False
        self.decoder_name = self.codec_name
        self.codec = self._open_decoder(self.codec_name)
        self.decode_ms = self.convert_ms = 0.0

    def _open_decoder(self, codec_name):
        candidates = []
        if sys.platform == "darwin":
            if codec_name == "h264":
                candidates.append("h264_videotoolbox")
            elif codec_name == "hevc":
                candidates.append("hevc_videotoolbox")
        else:
            candidates.append(f"{codec_name}_cuvid")
            if codec_name == "av1":
                candidates.append("libdav1d")
        available = getattr(av, "codecs_available", set())
        for name in candidates:
            if name not in available:
                continue
            try:
                ctx = av.CodecContext.create(name, "r")
                ctx.open()
                self.decoder_name = name
                self.hw_accelerated = name != "libdav1d"
                return ctx
            except Exception:
                continue
        ctx = av.CodecContext.create(codec_name, "r")
        ctx.thread_count = min(8, os.cpu_count() or 1)
        ctx.thread_type = "SLICE"
        self.decoder_name = codec_name
        self.hw_accelerated = False
        return ctx

    def decode(self, payload):
        started = time.perf_counter()
        decoded = self.codec.decode(av.Packet(payload))
        converted = time.perf_counter()
        if self.output_format == "yuv":
            # GPU shader consumes native planar YUV420p/YUV444p directly — zero CPU BGRA conversion!
            frames = list(decoded)
            self.decode_ms = (converted - started) * 1000
            self.convert_ms = 0.0
            return frames
        # Legacy QImage path: Qt paints native 32-bit pixels; RGB24 requires another expansion.
        frames = [frame.reformat(format="bgra") for frame in decoded]
        finished = time.perf_counter()
        self.decode_ms = (converted - started) * 1000
        self.convert_ms = (finished - converted) * 1000
        return frames


def create_encoder(config, encoder="libx264", monitor_index=1):
    if encoder == 'stable':
        media = DesktopEncoder(config, 'libx264', monitor_index, capture_backend='mss')
        media.fallback_reason = '兼容采集：MSS + 软件 H.264，适合排查频闪，性能可能较低'
        return media
    if encoder == 'auto' and sys.platform == 'darwin':
        hardware = None
        try:
            hardware = DesktopEncoder(config,'h264_videotoolbox',monitor_index)
            first = hardware.frame()
            if not first[0]:
                raise RuntimeError('VideoToolbox 未及时输出首帧')
            hardware._primed = first
            return hardware
        except Exception:
            if hardware is not None:
                hardware.close()
            media=DesktopEncoder(config,'libx264',monitor_index)
            media.fallback_reason='VideoToolbox 不可用，使用软件 H.264'
            return media
    if encoder in ("auto", "h264_nvenc"):
        from .hardware_media import HardwareEncoder, executable, nvenc_available
        path=executable()
        if nvenc_available(path):
            try:
                return HardwareEncoder(config,monitor_index,path)
            except (RuntimeError,ValueError,OSError):
                if encoder=="h264_nvenc":raise
        elif encoder=="h264_nvenc":
            raise RuntimeError("当前媒体后端或驱动不支持 NVENC，请使用自动或软件编码")
        media=DesktopEncoder(config,"libx264",monitor_index)
        media.fallback_reason="硬件后端未就绪，使用软件 H.264"
        return media
    return DesktopEncoder(config,encoder,monitor_index)
