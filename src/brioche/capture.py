"""Capture backend boundary: native FFmpeg X11 frames, portable MSS fallback."""
import os
import sys
import av
import mss


def list_monitors():
    with mss.mss() as capture:
        return [{"id": i, "label": f"显示器 {i}", **dict(monitor)}
                for i, monitor in enumerate(capture.monitors[1:], 1)][:32]


class DesktopCapture:
    def __init__(self, monitor_index, fps, backend='auto'):
        if sys.platform == 'darwin':
            from .mac_permissions import require_screen
            require_screen()
        if sys.platform == 'darwin':
            from .mac_capture import create
            self.mss = create()
        else:
            self.mss = mss.mss()
        self.container = None
        self.frames = None
        self.backend = "mss"
        try:
            if not 1 <= monitor_index < len(self.mss.monitors):
                raise ValueError("显示器不存在")
            self.monitor = dict(self.mss.monitors[monitor_index])
            if (backend != 'mss' and sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") != "wayland"
                    and "x11grab" in av.formats_available):
                m = self.monitor
                address = f"{os.environ.get('DISPLAY', ':0')}+{m['left']},{m['top']}"
                try:
                    self.container = av.open(address, format="x11grab", options={
                        "video_size": f"{m['width']}x{m['height']}",
                        "framerate": str(fps), "draw_mouse": "1", "probesize": "32",
                    })
                    self.frames = self.container.decode(video=0)
                    self.backend = "ffmpeg-x11grab"
                except Exception:
                    if self.container:
                        self.container.close()
                    self.container = None
        except Exception:
            self.close()
            raise

    def grab(self):
        if self.frames is not None:
            return next(self.frames)
        shot = self.mss.grab(self.monitor)
        image = av.VideoFrame(shot.width, shot.height, "bgra")
        pixels, plane = shot.bgra, image.planes[0]
        row_size = shot.width * 4
        if plane.line_size == row_size:
            plane.update(pixels)
        else:
            padded = bytearray(plane.buffer_size)
            for row in range(shot.height):
                padded[row * plane.line_size:row * plane.line_size + row_size] = pixels[row * row_size:(row + 1) * row_size]
            plane.update(padded)
        return image

    def close(self):
        if self.container:
            self.container.close()
        self.mss.close()
