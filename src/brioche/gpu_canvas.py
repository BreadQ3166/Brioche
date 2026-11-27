"""GPU-accelerated OpenGL YUV420p/YUV444p + CAS sharpening surface for PySide6.

Eliminates CPU YUV->BGRA color conversion (`frame.reformat("bgra")`), eliminates
33 MB/frame 4K QImage deep copies, and runs BT.709 YUV->RGB + Contrast-Adaptive
Sharpening directly on the host GPU (Vulkan/Metal/DX-backed OpenGL).
"""

from dataclasses import dataclass
import os
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QOpenGLFunctions
from PySide6.QtOpenGL import (
    QOpenGLPixelTransferOptions,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget


@dataclass
class YuvFrame:
    """Planar YUV frame owned by the mailbox (12 MB @ 4K 4:2:0 vs 33 MB BGRA)."""

    width: int
    height: int
    chroma: str  # "yuv420p" or "yuv444p"
    y: bytes
    u: bytes
    v: bytes
    y_stride: int
    uv_stride: int
    full_range: bool = False
    frame_id: int = 0

    @classmethod
    def from_av_frame(cls, frame, frame_id=0):
        fmt = frame.format.name
        full_range = fmt in ("yuvj420p", "yuvj444p")
        if fmt in ("yuv420p", "yuvj420p"):
            chroma = "yuv420p"
            src = frame
        elif fmt in ("yuv444p", "yuvj444p"):
            chroma = "yuv444p"
            src = frame
        else:
            chroma = "yuv420p"
            src = frame.reformat(format="yuv420p")
        p0, p1, p2 = src.planes[0], src.planes[1], src.planes[2]
        return cls(
            width=src.width,
            height=src.height,
            chroma=chroma,
            y=bytes(p0),
            u=bytes(p1),
            v=bytes(p2),
            y_stride=p0.line_size,
            uv_stride=p1.line_size,
            full_range=full_range,
            frame_id=frame_id,
        )


VERTEX_SHADER = """
#ifdef GL_ES
precision mediump float;
#endif
attribute vec2 a_pos;
attribute vec2 a_uv;
varying vec2 v_uv;
void main() {
    v_uv = a_uv;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"""

FRAGMENT_SHADER = """
#ifdef GL_ES
precision mediump float;
#endif
uniform sampler2D u_tex_y;
uniform sampler2D u_tex_u;
uniform sampler2D u_tex_v;
uniform vec2 u_texel;
uniform float u_sharpness;
uniform float u_full_range;
varying vec2 v_uv;

vec3 sample_yuv(vec2 uv) {
    float y = texture2D(u_tex_y, uv).r;
    float u = texture2D(u_tex_u, uv).r - 0.5;
    float v = texture2D(u_tex_v, uv).r - 0.5;
    if (u_full_range < 0.5) {
        y = (y - 0.0627451) * 1.16438356;
        u = u * 1.13839286;
        v = v * 1.13839286;
    }
    // BT.709 matrix
    float r = y + 1.5748 * v;
    float g = y - 0.1873 * u - 0.4681 * v;
    float b = y + 1.8556 * u;
    return clamp(vec3(r, g, b), 0.0, 1.0);
}

void main() {
    vec3 c = sample_yuv(v_uv);
    if (u_sharpness > 0.01) {
        vec3 n = sample_yuv(v_uv + vec2(0.0, -u_texel.y));
        vec3 s = sample_yuv(v_uv + vec2(0.0,  u_texel.y));
        vec3 w = sample_yuv(v_uv + vec2(-u_texel.x, 0.0));
        vec3 e = sample_yuv(v_uv + vec2( u_texel.x, 0.0));
        vec3 mn = min(c, min(min(n, s), min(w, e)));
        vec3 mx = max(c, max(max(n, s), max(w, e)));
        vec3 amp = clamp(min(mn, 1.0 - mx) / max(mx, vec3(0.0001)), 0.0, 1.0);
        vec3 weight = -u_sharpness * 0.22 * sqrt(amp);
        c = clamp((c + (n + s + w + e) * weight) / (1.0 + 4.0 * weight), 0.0, 1.0);
    }
    gl_FragColor = vec4(c, 1.0);
}
"""


def gpu_opengl_allowed():
    """Skip QOpenGLWidget in offscreen/minimal test backends where GL contexts are unavailable."""
    if os.environ.get("BRIOCHE_DISABLE_GL") == "1":
        return False
    platform_name = os.environ.get("QT_QPA_PLATFORM", "").lower()
    if platform_name in ("offscreen", "minimal"):
        return False
    return True


class GpuYuvSurface(QOpenGLWidget):
    """Hardware-accelerated planar YUV420p/YUV444p + CAS sharpening renderer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.gl_ready = False
        self.sharpness = 0.30
        self._program = None
        self._textures = [None, None, None]
        self._tex_dims = (0, 0, "")
        self._frame = None
        self._pending_upload = False
        self._viewport_rect = None  # (x, y, w, h) in logical widget coords
        self.painted = 0
        self.paint_ms = 0.0
        self._last_frame_id = -1

    def set_sharpness(self, value):
        self.sharpness = max(0.0, min(1.0, float(value)))
        self.update()

    def set_target_rect(self, rect):
        self._viewport_rect = rect
        self.update()

    def set_frame(self, yuv_frame):
        self._frame = yuv_frame
        self._pending_upload = True
        self.update()

    def clear_frame(self):
        self._frame = None
        self._pending_upload = False
        self.update()

    def _release_textures(self):
        for idx, tex in enumerate(self._textures):
            if tex is not None:
                try:
                    tex.destroy()
                except Exception:
                    pass
                self._textures[idx] = None
        self._tex_dims = (0, 0, "")

    def initializeGL(self):
        try:
            program = QOpenGLShaderProgram(self)
            if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, VERTEX_SHADER):
                return
            if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT_SHADER):
                return
            program.bindAttributeLocation("a_pos", 0)
            program.bindAttributeLocation("a_uv", 1)
            if not program.link():
                return
            self._program = program
            self.gl_ready = True
        except Exception:
            self.gl_ready = False

    def _ensure_textures(self, width, height, chroma):
        if self._tex_dims == (width, height, chroma) and all(t is not None for t in self._textures):
            return
        self._release_textures()
        uv_w = width if chroma == "yuv444p" else (width + 1) // 2
        uv_h = height if chroma == "yuv444p" else (height + 1) // 2
        sizes = ((width, height), (uv_w, uv_h), (uv_w, uv_h))
        for idx, (tw, th) in enumerate(sizes):
            tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            tex.setFormat(QOpenGLTexture.TextureFormat.R8_UNorm)
            tex.setSize(tw, th)
            tex.setMinMagFilters(
                QOpenGLTexture.Filter.Linear, QOpenGLTexture.Filter.Linear
            )
            tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            tex.allocateStorage()
            self._textures[idx] = tex
        self._tex_dims = (width, height, chroma)

    def _upload_frame(self, frame):
        self._ensure_textures(frame.width, frame.height, frame.chroma)
        opts_y = QOpenGLPixelTransferOptions()
        opts_y.setAlignment(1)
        opts_y.setRowLength(frame.y_stride)
        self._textures[0].setData(
            QOpenGLTexture.PixelFormat.Red,
            QOpenGLTexture.PixelType.UInt8,
            frame.y,
            opts_y,
        )
        opts_uv = QOpenGLPixelTransferOptions()
        opts_uv.setAlignment(1)
        opts_uv.setRowLength(frame.uv_stride)
        self._textures[1].setData(
            QOpenGLTexture.PixelFormat.Red,
            QOpenGLTexture.PixelType.UInt8,
            frame.u,
            opts_uv,
        )
        self._textures[2].setData(
            QOpenGLTexture.PixelFormat.Red,
            QOpenGLTexture.PixelType.UInt8,
            frame.v,
            opts_uv,
        )

    def paintGL(self):
        started = time.perf_counter()
        ctx = self.context()
        if not self.gl_ready or ctx is None or self._program is None:
            return
        fn = QOpenGLFunctions(ctx)
        fn.initializeOpenGLFunctions()
        dpr = max(1.0, float(self.devicePixelRatioF()))
        full_w = max(1, int(round(self.width() * dpr)))
        full_h = max(1, int(round(self.height() * dpr)))
        fn.glViewport(0, 0, full_w, full_h)
        fn.glClearColor(0.0706, 0.0667, 0.0588, 1.0)  # #12110F
        fn.glClear(0x00004000)  # GL_COLOR_BUFFER_BIT

        frame = self._frame
        if frame is None:
            return
        try:
            if self._pending_upload:
                self._upload_frame(frame)
                self._pending_upload = False
            if self._viewport_rect is not None and not self._viewport_rect.isEmpty():
                r = self._viewport_rect
                vx = int(round(r.x() * dpr))
                vw = max(1, int(round(r.width() * dpr)))
                vh = max(1, int(round(r.height() * dpr)))
                vy = max(0, full_h - int(round((r.y() + r.height()) * dpr)))
                fn.glViewport(vx, vy, vw, vh)

            prog = self._program
            prog.bind()
            self._textures[0].bind(0)
            self._textures[1].bind(1)
            self._textures[2].bind(2)
            prog.setUniformValue("u_tex_y", 0)
            prog.setUniformValue("u_tex_u", 1)
            prog.setUniformValue("u_tex_v", 2)
            prog.setUniformValue("u_texel", 1.0 / max(1, frame.width), 1.0 / max(1, frame.height))
            prog.setUniformValue("u_sharpness", float(self.sharpness))
            prog.setUniformValue("u_full_range", 1.0 if frame.full_range else 0.0)

            import array
            verts = array.array(
                "f",
                [
                    -1.0, -1.0, 0.0, 1.0,
                     1.0, -1.0, 1.0, 1.0,
                    -1.0,  1.0, 0.0, 0.0,
                     1.0,  1.0, 1.0, 0.0,
                ],
            ).tobytes()
            prog.enableAttributeArray(0)
            prog.enableAttributeArray(1)
            prog.setAttributeArray(0, 0x1406, verts, 2, 16)  # GL_FLOAT
            prog.setAttributeArray(1, 0x1406, verts[8:], 2, 16)
            fn.glDrawArrays(0x0005, 0, 4)  # GL_TRIANGLE_STRIP
            prog.disableAttributeArray(0)
            prog.disableAttributeArray(1)
            prog.release()

            if frame.frame_id != self._last_frame_id:
                self._last_frame_id = frame.frame_id
                self.painted += 1
                self.paint_ms += (time.perf_counter() - started) * 1000
        except Exception:
            self.gl_ready = False

    def cleanup(self):
        if self.context() is not None:
            self.makeCurrent()
            self._release_textures()
            self._program = None
            self.doneCurrent()
