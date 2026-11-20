"""Small input adapter. No global keyboard hooks; only explicit remote events."""
import math
import sys
import os
from types import SimpleNamespace

KEY_NAMES = {"alt", "alt_l", "alt_r", "backspace", "caps_lock", "cmd", "cmd_l", "cmd_r",
             "ctrl", "ctrl_l", "ctrl_r", "delete", "down", "end", "enter", "esc", "home",
             "insert", "left", "page_down", "page_up", "right", "shift", "shift_l", "shift_r",
             "space", "tab", "up"} | {f"f{i}" for i in range(1, 13)}


def validate_event(event):
    if not isinstance(event, dict):
        raise ValueError("无效输入")
    kind = event.get("event")
    if kind == "move":
        for name in ("x", "y"):
            value = event.get(name)
            if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("鼠标坐标超出范围")
    elif kind == "button":
        if event.get("button") not in ("left", "middle", "right") or type(event.get("down")) is not bool:
            raise ValueError("无效鼠标按键")
    elif kind == "key":
        key = event.get("key")
        if not isinstance(key, str) or not (key in KEY_NAMES or (len(key) == 1 and key.isprintable())):
            raise ValueError("无效键盘按键")
        if type(event.get("down")) is not bool:
            raise ValueError("无效按键状态")
    elif kind == "scroll":
        if type(event.get("dy")) is not int or not -10 <= event["dy"] <= 10:
            raise ValueError("无效滚动量")
    elif kind != "release":
        raise ValueError("未知输入事件")
    return event


class InputSink:
    def __init__(self, monitor):
        if sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") == "wayland":
            raise RuntimeError("此版本被控输入需要 Xorg 会话；Wayland 尚未实现")
        self.display = None
        if sys.platform.startswith("linux"):
            from Xlib.display import Display
            self.display = Display()
            self.keyboard_module = SimpleNamespace(Key=SimpleNamespace(**{k: k for k in KEY_NAMES}))
            self.mouse_module = SimpleNamespace(Button=SimpleNamespace(left=1, middle=2, right=3))
            self.keyboard = X11Keyboard(self.display)
            self.mouse = X11Mouse(self.display)
        else:
            from pynput import keyboard, mouse
            self.keyboard_module, self.mouse_module = keyboard, mouse
            self.keyboard, self.mouse = keyboard.Controller(), mouse.Controller()
        self.monitor = monitor
        self.keys, self.buttons = set(), set()

    def apply(self, event):
        validate_event(event)
        kind = event["event"]
        if kind == "move":
            m = self.monitor
            self.mouse.position = (m["left"] + round(event["x"] * (m["width"] - 1)),
                                   m["top"] + round(event["y"] * (m["height"] - 1)))
        elif kind == "key":
            name = event["key"]
            key = getattr(self.keyboard_module.Key, name) if name in KEY_NAMES else name
            if event["down"]:
                self.keyboard.press(key)
                self.keys.add(key)
            else:
                self.keyboard.release(key)
                self.keys.discard(key)
        elif kind == "button":
            button = getattr(self.mouse_module.Button, event["button"])
            if event["down"]:
                self.mouse.press(button)
                self.buttons.add(button)
            else:
                self.mouse.release(button)
                self.buttons.discard(button)
        elif kind == "scroll":
            self.mouse.scroll(0, event["dy"])
        else:
            self.release_all()

    def release_all(self):
        for key in tuple(self.keys):
            try:
                self.keyboard.release(key)
            except Exception:
                pass
        for button in tuple(self.buttons):
            try:
                self.mouse.release(button)
            except Exception:
                pass
        self.keys.clear()
        self.buttons.clear()

    def close(self):
        self.release_all()
        if self.display:
            self.display.close()


X11_KEYS = {"alt": "Alt_L", "alt_l": "Alt_L", "alt_r": "Alt_R", "ctrl": "Control_L",
            "ctrl_l": "Control_L", "ctrl_r": "Control_R", "shift": "Shift_L",
            "shift_l": "Shift_L", "shift_r": "Shift_R", "cmd": "Super_L", "cmd_l": "Super_L",
            "cmd_r": "Super_R", "caps_lock": "Caps_Lock", "backspace": "BackSpace",
            "enter": "Return", "esc": "Escape", "page_up": "Prior", "page_down": "Next"}


class X11Keyboard:
    def __init__(self, display):
        self.display = display

    def emit(self, key, down):
        from Xlib import X, XK
        from Xlib.ext import xtest
        name = X11_KEYS.get(key, key.capitalize() if key in KEY_NAMES else key)
        symbol = XK.string_to_keysym(name)
        if not symbol and len(name) == 1:
            symbol = ord(name) if ord(name) < 256 else 0x01000000 | ord(name)
        code = self.display.keysym_to_keycode(symbol)
        if not code:
            raise ValueError("此 Xorg 键盘映射不支持该字符；请使用远端输入法")
        xtest.fake_input(self.display, X.KeyPress if down else X.KeyRelease, code)
        self.display.sync()

    def press(self, key):
        self.emit(key, True)

    def release(self, key):
        self.emit(key, False)


class X11Mouse:
    def __init__(self, display):
        self.display = display

    @property
    def position(self):
        point = self.display.screen().root.query_pointer()
        return point.root_x, point.root_y

    @position.setter
    def position(self, xy):
        self.display.screen().root.warp_pointer(*xy)
        self.display.sync()

    def press(self, button):
        from Xlib import X
        from Xlib.ext import xtest
        xtest.fake_input(self.display, X.ButtonPress, button)
        self.display.sync()

    def release(self, button):
        from Xlib import X
        from Xlib.ext import xtest
        xtest.fake_input(self.display, X.ButtonRelease, button)
        self.display.sync()

    def scroll(self, dx, dy):
        for _ in range(abs(dy)):
            button = 4 if dy > 0 else 5
            self.press(button)
            self.release(button)
