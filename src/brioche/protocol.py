"""Length-bounded versioned protocol; no pickle or executable payloads."""
import asyncio
import json
import struct
from dataclasses import dataclass

VERSION = 2
CONTROL, VIDEO, FILE = 1, 2, 3
MAX_FILE_CHUNK = 64 * 1024
HEADER = struct.Struct("!BI")
MAX_CONTROL = 16 * 1024
MAX_VIDEO = 8 * 1024 * 1024
RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440), "native": (3840, 2160)}
FRAME_RATES = (30, 60, 90, 120, 144)
ENCODERS = ("libx264", "h264_nvenc", "h264_qsv", "h264_videotoolbox")


class ProtocolError(Exception):
    pass


@dataclass(frozen=True)
class StreamConfig:
    resolution: str = "1440p"
    fps: int = 90
    bitrate_mbps: int = 20
    match_display: bool = False

    def __post_init__(self):
        if self.resolution not in RESOLUTIONS:
            raise ProtocolError("不支持的分辨率")
        if type(self.fps) is not int or self.fps not in FRAME_RATES:
            raise ProtocolError("不支持的帧率")
        if type(self.bitrate_mbps) is not int or not 2 <= self.bitrate_mbps <= 80:
            raise ProtocolError("码率必须在 2–80 Mbps")
        if type(self.match_display) is not bool:
            raise ProtocolError("无效源屏匹配选项")

    def to_wire(self):
        value = {"resolution": self.resolution, "fps": self.fps, "bitrate_mbps": self.bitrate_mbps}
        if self.match_display:
            value["match_display"] = True
        return value

    @classmethod
    def parse(cls, value):
        required = {"resolution", "fps", "bitrate_mbps"}
        if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"match_display"}:
            raise ProtocolError("无效串流配置")
        if not isinstance(value["resolution"], str):
            raise ProtocolError("无效分辨率")
        return cls(**value)


def pack_control(value):
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(payload) > MAX_CONTROL:
        raise ProtocolError("控制消息过长")
    return HEADER.pack(CONTROL, len(payload)) + payload


def pack_video(payload):
    if not 0 < len(payload) <= MAX_VIDEO:
        raise ProtocolError("视频包大小无效")
    return HEADER.pack(VIDEO, len(payload)) + payload


def pack_file(payload):
    if not 0 < len(payload) <= MAX_FILE_CHUNK:
        raise ProtocolError('无效文件分块')
    return HEADER.pack(FILE, len(payload)) + payload


async def read_message(reader):
    kind, size = HEADER.unpack(await reader.readexactly(HEADER.size))
    limit = {CONTROL: MAX_CONTROL, VIDEO: MAX_VIDEO, FILE: MAX_FILE_CHUNK}.get(kind, 0)
    if not 0 < size <= limit:
        raise ProtocolError("无效消息类型或长度")
    payload = await reader.readexactly(size)
    if kind in (VIDEO, FILE):
        return kind, payload
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ProtocolError("无效 JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ProtocolError("控制消息缺少类型")
    return kind, value


async def send(writer, message):
    writer.write(message)
    await asyncio.wait_for(writer.drain(), 4)
