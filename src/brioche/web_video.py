"""Bounded browser video window, with IDR recovery rather than broken AVC drops."""
import struct
import time
from .h264 import access_unit_info
from .protocol import ProtocolError

JPEG_HEADER = struct.Struct("!II")
H264_HEADER = struct.Struct("!IIIBQ")  # epoch, frame ID, decoder revision, IDR, timestamp (us)


class VideoStreamError(ProtocolError):
    pass


class BrowserVideoFlow:
    def __init__(self, info):
        self.pending = {}
        self.frame = 0
        self.revision = 0
        self.last_request = 0.
        self.started = time.monotonic()
        self.last_timestamp = -1
        self.reset(info)

    def reset(self, info):
        self.info = info
        self.codec = info["codec"]
        self.epoch = info["stream_id"]
        self.pending.clear()
        self.waiting_key = self.codec == "h264"
        self.waiting_since = time.monotonic()
        self.reset_required = self.waiting_key
        self.codec_string = None

    def acknowledge(self, frame):
        if type(frame) is int:
            self.pending.pop(frame, None)

    def packet(self, payload, now=None):
        """Return (decoder_config | None, binary | None, request_IDR).

        Once a reference frame is skipped, discard the rest of that dependency
        chain until a complete IDR arrives. Decoder reset and parameter sets are
        sent together; an isolated delta frame is never presented as a new chain.
        """
        now = time.monotonic() if now is None else now
        if self.pending and now - next(iter(self.pending.values())) > 10:
            raise VideoStreamError("浏览器画面响应超时，请重新连接")
        if self.waiting_key and now - self.waiting_since > 10:
            raise VideoStreamError("等待 H.264 关键帧超时，请使用兼容模式")
        limit = 4 if self.codec == "h264" else 1
        if len(self.pending) >= limit:
            if self.codec == "h264" and not self.waiting_key:
                self.waiting_key, self.reset_required = True, True
                self.waiting_since = now
            return None, None, self.request_keyframe(now)
        config = None
        key = False
        if self.codec == "h264":
            unit = access_unit_info(payload)
            key = unit["key"]
            if unit["codec"] and unit["codec"] != self.codec_string:
                self.waiting_key, self.reset_required = True, True
            if self.waiting_key:
                if not unit["complete_key"] or not unit["codec"]:
                    return None, None, self.request_keyframe(now)
                self.revision += 1
                self.codec_string = unit["codec"]
                self.pending.clear()
                self.waiting_key, self.reset_required = False, False
                config = {"type": "video_config", "stream_id": self.epoch, "revision": self.revision,
                          "codec": self.codec_string, "format": "annexb", "width": self.info["width"],
                          "height": self.info["height"], "fps_limit": self.info["fps_limit"]}
        self.frame = (self.frame + 1) & 0xffffffff
        self.pending[self.frame] = now
        if self.codec == "h264":
            stamp = max(self.last_timestamp + 1, int((now - self.started) * 1_000_000))
            self.last_timestamp = stamp
            header = H264_HEADER.pack(self.epoch, self.frame, self.revision, int(key), stamp)
        else:
            header = JPEG_HEADER.pack(self.epoch, self.frame)
        return config, header + payload, False

    def request_keyframe(self, now):
        if self.codec == "h264" and self.waiting_key and now - self.last_request >= .25:
            self.last_request = now
            return True
        return False
