//! macOS 12+ thread-safe Quartz `CGEventPost` input injection (zero HIToolbox/TSM main-thread crashes!).

use std::collections::HashSet;
use std::ffi::c_void;

use brioche_proto::InputAction;

use crate::{InputError, InputInjector, MonitorRect};

#[repr(C)]
#[derive(Clone, Copy)]
struct CGPoint {
    x: f64,
    y: f64,
}

const K_CG_HID_EVENT_TAP: u32 = 0;
const K_CG_EVENT_LEFT_MOUSE_DOWN: u32 = 1;
const K_CG_EVENT_LEFT_MOUSE_UP: u32 = 2;
const K_CG_EVENT_RIGHT_MOUSE_DOWN: u32 = 3;
const K_CG_EVENT_RIGHT_MOUSE_UP: u32 = 4;
const K_CG_EVENT_MOUSE_MOVED: u32 = 5;
const K_CG_EVENT_OTHER_MOUSE_DOWN: u32 = 25;
const K_CG_EVENT_OTHER_MOUSE_UP: u32 = 26;

#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
    fn CGEventCreateMouseEvent(
        source: *mut c_void,
        mouse_type: u32,
        mouse_cursor_position: CGPoint,
        mouse_button: u32,
    ) -> *mut c_void;
    fn CGEventCreateKeyboardEvent(
        source: *mut c_void,
        virtual_key: u16,
        key_down: bool,
    ) -> *mut c_void;
    fn CGEventCreateScrollWheelEvent(
        source: *mut c_void,
        units: u32,
        wheel_count: u32,
        wheel1: i32,
        wheel2: i32,
    ) -> *mut c_void;
    fn CGEventPost(tap: u32, event: *mut c_void);
}

#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    fn CFRelease(cf: *mut c_void);
}

pub struct MacCgEventInjector {
    rect: MonitorRect,
    last_pos: CGPoint,
    pressed_keys: HashSet<u16>,
    pressed_buttons: HashSet<String>,
}

unsafe impl Send for MacCgEventInjector {}

impl MacCgEventInjector {
    pub fn open(rect: MonitorRect) -> Self {
        Self {
            last_pos: CGPoint {
                x: rect.left as f64 + rect.width as f64 / 2.0,
                y: rect.top as f64 + rect.height as f64 / 2.0,
            },
            rect,
            pressed_keys: HashSet::new(),
            pressed_buttons: HashSet::new(),
        }
    }

    fn key_to_mac_keycode(name: &str) -> Option<u16> {
        match name {
            "a" => Some(0x00), "s" => Some(0x01), "d" => Some(0x02), "f" => Some(0x03),
            "h" => Some(0x04), "g" => Some(0x05), "z" => Some(0x06), "x" => Some(0x07),
            "c" => Some(0x08), "v" => Some(0x09), "b" => Some(0x0B), "q" => Some(0x0C),
            "w" => Some(0x0D), "e" => Some(0x0E), "r" => Some(0x0F), "y" => Some(0x10),
            "t" => Some(0x11), "1" => Some(0x12), "2" => Some(0x13), "3" => Some(0x14),
            "4" => Some(0x15), "6" => Some(0x16), "5" => Some(0x17), "9" => Some(0x19),
            "7" => Some(0x1A), "8" => Some(0x1C), "0" => Some(0x1D), "o" => Some(0x1F),
            "u" => Some(0x20), "i" => Some(0x22), "p" => Some(0x23), "l" => Some(0x25),
            "j" => Some(0x26), "k" => Some(0x28), "n" => Some(0x2D), "m" => Some(0x2E),
            "enter" | "return" => Some(0x24),
            "tab" => Some(0x30),
            "space" => Some(0x31),
            "backspace" => Some(0x33),
            "esc" | "escape" => Some(0x35),
            "cmd" | "cmd_l" | "super" => Some(0x37),
            "shift" | "shift_l" => Some(0x38),
            "caps_lock" => Some(0x39),
            "alt" | "alt_l" => Some(0x3A),
            "ctrl" | "ctrl_l" => Some(0x3B),
            "shift_r" => Some(0x3C),
            "alt_r" => Some(0x3D),
            "ctrl_r" => Some(0x3E),
            "delete" => Some(0x75),
            "home" => Some(0x73),
            "end" => Some(0x77),
            "page_up" => Some(0x74),
            "page_down" => Some(0x79),
            "left" => Some(0x7B),
            "right" => Some(0x7C),
            "down" => Some(0x7D),
            "up" => Some(0x7E),
            "f1" => Some(0x7A), "f2" => Some(0x78), "f3" => Some(0x63), "f4" => Some(0x76),
            "f5" => Some(0x60), "f6" => Some(0x61), "f7" => Some(0x62), "f8" => Some(0x64),
            "f9" => Some(0x65), "f10" => Some(0x6D), "f11" => Some(0x67), "f12" => Some(0x6F),
            _ => None,
        }
    }

    fn post_and_release(ev: *mut c_void) {
        if !ev.is_null() {
            unsafe {
                CGEventPost(K_CG_HID_EVENT_TAP, ev);
                CFRelease(ev);
            }
        }
    }
}

impl InputInjector for MacCgEventInjector {
    fn set_monitor(&mut self, rect: MonitorRect) {
        self.release_all();
        self.rect = rect;
    }

    fn apply(&mut self, action: &InputAction) -> Result<(), InputError> {
        match action {
            InputAction::Move { x, y } => {
                let pt = CGPoint {
                    x: self.rect.left as f64 + (x.clamp(0.0, 1.0) as f64) * (self.rect.width.saturating_sub(1) as f64),
                    y: self.rect.top as f64 + (y.clamp(0.0, 1.0) as f64) * (self.rect.height.saturating_sub(1) as f64),
                };
                self.last_pos = pt;
                let ev = unsafe {
                    CGEventCreateMouseEvent(std::ptr::null_mut(), K_CG_EVENT_MOUSE_MOVED, pt, 0)
                };
                Self::post_and_release(ev);
            }
            InputAction::Button { button, down } => {
                let (ev_type, btn_num) = match (button.as_str(), *down) {
                    ("left", true) => (K_CG_EVENT_LEFT_MOUSE_DOWN, 0u32),
                    ("left", false) => (K_CG_EVENT_LEFT_MOUSE_UP, 0u32),
                    ("right", true) => (K_CG_EVENT_RIGHT_MOUSE_DOWN, 1u32),
                    ("right", false) => (K_CG_EVENT_RIGHT_MOUSE_UP, 1u32),
                    ("middle", true) => (K_CG_EVENT_OTHER_MOUSE_DOWN, 2u32),
                    ("middle", false) => (K_CG_EVENT_OTHER_MOUSE_UP, 2u32),
                    _ => return Ok(()),
                };
                let ev = unsafe {
                    CGEventCreateMouseEvent(std::ptr::null_mut(), ev_type, self.last_pos, btn_num)
                };
                Self::post_and_release(ev);
                if *down {
                    self.pressed_buttons.insert(button.clone());
                } else {
                    self.pressed_buttons.remove(button);
                }
            }
            InputAction::Scroll { dx, dy } => {
                let ev = unsafe {
                    CGEventCreateScrollWheelEvent(std::ptr::null_mut(), 1, 2, *dy * 3, *dx * 3)
                };
                Self::post_and_release(ev);
            }
            InputAction::Key { key, down } => {
                if let Some(kc) = Self::key_to_mac_keycode(&key.to_lowercase()) {
                    let ev = unsafe { CGEventCreateKeyboardEvent(std::ptr::null_mut(), kc, *down) };
                    Self::post_and_release(ev);
                    if *down {
                        self.pressed_keys.insert(kc);
                    } else {
                        self.pressed_keys.remove(&kc);
                    }
                }
            }
            InputAction::ReleaseAll => self.release_all(),
        }
        Ok(())
    }

    fn release_all(&mut self) {
        for kc in self.pressed_keys.drain().collect::<Vec<_>>() {
            let ev = unsafe { CGEventCreateKeyboardEvent(std::ptr::null_mut(), kc, false) };
            Self::post_and_release(ev);
        }
        for btn in self.pressed_buttons.drain().collect::<Vec<_>>() {
            let _ = self.apply(&InputAction::Button {
                button: btn,
                down: false,
            });
        }
    }
}

impl Drop for MacCgEventInjector {
    fn drop(&mut self) {
        self.release_all();
    }
}
