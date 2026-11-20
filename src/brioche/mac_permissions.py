"""macOS 12 screen/input permission checks; never called by synthetic self-tests."""
import ctypes
import sys


def screen_allowed(request=False):
    if sys.platform != 'darwin':return True
    library=ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    function=library.CGRequestScreenCaptureAccess if request else library.CGPreflightScreenCaptureAccess
    function.argtypes=[];function.restype=ctypes.c_bool
    return bool(function())


def input_allowed(request=False):
    if sys.platform != 'darwin':return True
    import ApplicationServices
    if request:return bool(ApplicationServices.AXIsProcessTrustedWithOptions({ApplicationServices.kAXTrustedCheckOptionPrompt:True}))
    return bool(ApplicationServices.AXIsProcessTrusted())


def require_screen():
    if not screen_allowed():
        raise RuntimeError('请在系统偏好设置 → 安全性与隐私 → 屏幕录制中允许 Brioche，并重新打开软件')


def require_input():
    if not input_allowed():
        raise RuntimeError('请在系统偏好设置 → 安全性与隐私 → 辅助功能中允许 Brioche，或改为仅查看共享')
