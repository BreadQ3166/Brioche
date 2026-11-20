"""Replaceable H.264 CPU-frame backend; owned by one dedicated worker thread."""
from fractions import Fraction
import os
import time
import av
from .capture import DesktopCapture
from .protocol import ENCODERS, RESOLUTIONS
from .h264 import annexb_access_unit


class DesktopEncoder:
    def __init__(self, config, encoder="libx264", monitor_index=1):
        if encoder not in ENCODERS + ("mjpeg",):
            raise ValueError("编码器不在允许列表中")
        self.capture = DesktopCapture(monitor_index, config.fps)
        try:
            self.monitor = self.capture.monitor
            max_w, max_h = RESOLUTIONS[config.resolution]
            if self.monitor["height"] > self.monitor["width"]:
                max_w, max_h = max_h, max_w
            scale = min(max_w / self.monitor["width"], max_h / self.monitor["height"], 1.0)
            self.width = max(2, int(self.monitor["width"] * scale) // 2 * 2)
            self.height = max(2, int(self.monitor["height"] * scale) // 2 * 2)
            self.encoder_name = encoder
            self.codec_name = "mjpeg" if encoder == "mjpeg" else "h264"
            self.codec = av.CodecContext.create(encoder, "w")
            self.codec.width, self.codec.height = self.width, self.height
            self.codec.pix_fmt = "yuvj420p" if encoder == "mjpeg" else "yuv420p"
            self.codec.time_base = Fraction(1, config.fps)
            self.codec.framerate = Fraction(config.fps, 1)
            self.codec.bit_rate = config.bitrate_mbps * 1_000_000
            self.codec.gop_size = config.fps
            self.codec.max_b_frames = 0
            self.interpolation = "FAST_BILINEAR" if config.fps >= 60 else "BILINEAR"
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
        except Exception:
            self.capture.close()
            raise

    def frame(self, force_keyframe=False):
        start = time.perf_counter()
        image = self.capture.grab()
        image = image.reformat(width=self.width, height=self.height, format=self.codec.pix_fmt,
                               interpolation=self.interpolation)
        image.pts = self.pts
        image.time_base = self.codec.time_base
        if force_keyframe and self.codec_name == "h264":
            image.pict_type = av.video.frame.PictureType.I
        self.pts += 1
        packets = [annexb_access_unit(bytes(p), self.codec.extradata, p.is_keyframe)
                   if self.codec_name == "h264" else bytes(p) for p in self.codec.encode(image)]
        return packets, (time.perf_counter() - start) * 1000

    def close(self):
        self.capture.close()
        self.codec = None


class DesktopDecoder:
    def __init__(self):
        self.codec = av.CodecContext.create("h264", "r")
        self.codec.thread_count = min(4, os.cpu_count() or 1)
        self.codec.thread_type = "SLICE"

    def decode(self, payload):
        return [frame.reformat(format="rgb24") for frame in self.codec.decode(av.Packet(payload))]


def create_encoder(config, encoder="libx264", monitor_index=1):
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
