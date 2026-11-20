"""CoreGraphics capture for macOS 12; each frame owns and releases its CF objects.

MSS 9's Darwin backend releases a borrowed provider and retains its CGImage.
Use MSS only for monitor enumeration and its screenshot value type.
"""
import ctypes


def grab(core, monitor, rectangle, image_type):
    image = core.CGWindowListCreateImage(rectangle, 1, 0, 0)
    if not image:
        raise RuntimeError('Mac 屏幕采集失败，请检查屏幕录制权限并退出重开 Brioche')
    data = None
    try:
        width, height = core.CGImageGetWidth(image), core.CGImageGetHeight(image)
        stride = core.CGImageGetBytesPerRow(image)
        if width < 1 or height < 1 or core.CGImageGetBitsPerPixel(image) != 32 or stride < width * 4:
            raise RuntimeError('Mac 屏幕像素格式不受支持，请关闭 HDR 后重试')
        # Get returns a borrowed provider. Only CopyData and CreateImage are owned.
        provider = core.CGImageGetDataProvider(image)
        if not provider:
            raise RuntimeError('Mac 未返回屏幕像素，请检查屏幕录制权限')
        data = core.CGDataProviderCopyData(provider)
        if not data or core.CFDataGetLength(data) < stride * height:
            raise RuntimeError('Mac 屏幕像素数据不完整')
        address = core.CFDataGetBytePtr(data)
        if not address:
            raise RuntimeError('Mac 未返回屏幕像素')
        raw = ctypes.string_at(address, stride * height)
        if stride != width * 4:
            raw = b''.join(raw[row * stride:row * stride + width * 4] for row in range(height))
        from mss.screenshot import Size
        return image_type(bytearray(raw), monitor, size=Size(width, height))
    finally:
        if data:
            core.CFRelease(data)
        core.CGImageRelease(image)


def create():
    from mss.darwin import MSS, CGRect

    class Capture(MSS):
        def __init__(self):
            super().__init__()
            self.core.CGImageRelease.argtypes = [ctypes.c_void_p]
            self.core.CGImageRelease.restype = None

        def _grab_impl(self, monitor):
            rectangle = CGRect((monitor['left'], monitor['top']), (monitor['width'], monitor['height']))
            return grab(self.core, monitor, rectangle, self.cls_image)

    return Capture()
