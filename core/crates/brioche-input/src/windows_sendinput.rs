//! Windows native Win32 `SendInput` low-latency keyboard & mouse injection.

use std::collections::HashSet;

use brioche_proto::InputAction;
use windows::Win32::UI::Input::KeyboardAndMouse::{
    SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, INPUT_MOUSE, KEYBDINPUT, KEYEVENTF_KEYUP,
    MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_HWHEEL, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, MOUSEEVENTF_MOVE, MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_WHEEL, MOUSEINPUT, VIRTUAL_KEY,
};
use windows::Win32::UI::WindowsAndMessaging::{
    GetSystemMetrics, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN, SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN,
};

use crate::{InputError, InputInjector, MonitorRect};

pub struct WinSendInputInjector {
    rect: MonitorRect,
    pressed_vks: HashSet<u16>,
    pressed_buttons: HashSet<String>,
}

impl WinSendInputInjector {
    pub fn open(rect: MonitorRect) -> Self {
        Self {
            rect,
            pressed_vks: HashSet::new(),
            pressed_buttons: HashSet::new(),
        }
    }

    fn key_to_vk(name: &str) -> Option<u16> {
        match name {
            "esc" | "escape" => Some(0x1B),
            "tab" => Some(0x09),
            "backspace" => Some(0x08),
            "enter" | "return" => Some(0x0D),
            "space" => Some(0x20),
            "insert" => Some(0x2D),
            "delete" => Some(0x2E),
            "home" => Some(0x24),
            "end" => Some(0x23),
            "left" => Some(0x25),
            "up" => Some(0x26),
            "right" => Some(0x27),
            "down" => Some(0x28),
            "page_up" => Some(0x21),
            "page_down" => Some(0x22),
            "shift" | "shift_l" => Some(0xA0),
            "shift_r" => Some(0xA1),
            "ctrl" | "ctrl_l" => Some(0xA2),
            "ctrl_r" => Some(0xA3),
            "alt" | "alt_l" => Some(0xA4),
            "alt_r" => Some(0xA5),
            "cmd" | "cmd_l" | "super" => Some(0x5B),
            "cmd_r" => Some(0x5C),
            "caps_lock" => Some(0x14),
            "f1" => Some(0x70),
            "f2" => Some(0x71),
            "f3" => Some(0x72),
            "f4" => Some(0x73),
            "f5" => Some(0x74),
            "f6" => Some(0x75),
            "f7" => Some(0x76),
            "f8" => Some(0x77),
            "f9" => Some(0x78),
            "f10" => Some(0x79),
            "f11" => Some(0x7A),
            "f12" => Some(0x7B),
            s if s.len() == 1 => {
                let b = s.as_bytes()[0];
                if b.is_ascii_alphanumeric() {
                    Some(b.to_ascii_uppercase() as u16)
                } else {
                    None
                }
            }
            _ => None,
        }
    }

    fn send_one(input: INPUT) {
        unsafe {
            let _ = SendInput(&[input], std::mem::size_of::<INPUT>() as i32);
        }
    }
}

impl InputInjector for WinSendInputInjector {
    fn set_monitor(&mut self, rect: MonitorRect) {
        self.release_all();
        self.rect = rect;
    }

    fn apply(&mut self, action: &InputAction) -> Result<(), InputError> {
        match action {
            InputAction::Move { x, y } => unsafe {
                let px = self.rect.left
                    + (x.clamp(0.0, 1.0) * self.rect.width.saturating_sub(1) as f32).round() as i32;
                let py = self.rect.top
                    + (y.clamp(0.0, 1.0) * self.rect.height.saturating_sub(1) as f32).round() as i32;
                let vx = GetSystemMetrics(SM_XVIRTUALSCREEN);
                let vy = GetSystemMetrics(SM_YVIRTUALSCREEN);
                let vw = GetSystemMetrics(SM_CXVIRTUALSCREEN).max(1);
                let vh = GetSystemMetrics(SM_CYVIRTUALSCREEN).max(1);
                let dx = (((px - vx) as f64) * 65535.0 / (vw - 1).max(1) as f64).round() as i32;
                let dy = (((py - vy) as f64) * 65535.0 / (vh - 1).max(1) as f64).round() as i32;
                Self::send_one(INPUT {
                    r#type: INPUT_MOUSE,
                    Anonymous: INPUT_0 {
                        mi: MOUSEINPUT {
                            dx,
                            dy,
                            mouseData: 0,
                            dwFlags: MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                            time: 0,
                            dwExtraInfo: 0,
                        },
                    },
                });
            },
            InputAction::Button { button, down } => {
                let flag = match (button.as_str(), *down) {
                    ("left", true) => MOUSEEVENTF_LEFTDOWN,
                    ("left", false) => MOUSEEVENTF_LEFTUP,
                    ("right", true) => MOUSEEVENTF_RIGHTDOWN,
                    ("right", false) => MOUSEEVENTF_RIGHTUP,
                    ("middle", true) => MOUSEEVENTF_MIDDLEDOWN,
                    ("middle", false) => MOUSEEVENTF_MIDDLEUP,
                    _ => return Ok(()),
                };
                Self::send_one(INPUT {
                    r#type: INPUT_MOUSE,
                    Anonymous: INPUT_0 {
                        mi: MOUSEINPUT {
                            dx: 0,
                            dy: 0,
                            mouseData: 0,
                            dwFlags: flag,
                            time: 0,
                            dwExtraInfo: 0,
                        },
                    },
                });
                if *down {
                    self.pressed_buttons.insert(button.clone());
                } else {
                    self.pressed_buttons.remove(button);
                }
            }
            InputAction::Scroll { dx, dy } => {
                if *dy != 0 {
                    Self::send_one(INPUT {
                        r#type: INPUT_MOUSE,
                        Anonymous: INPUT_0 {
                            mi: MOUSEINPUT {
                                dx: 0,
                                dy: 0,
                                mouseData: (*dy * 120) as u32,
                                dwFlags: MOUSEEVENTF_WHEEL,
                                time: 0,
                                dwExtraInfo: 0,
                            },
                        },
                    });
                }
                if *dx != 0 {
                    Self::send_one(INPUT {
                        r#type: INPUT_MOUSE,
                        Anonymous: INPUT_0 {
                            mi: MOUSEINPUT {
                                dx: 0,
                                dy: 0,
                                mouseData: (*dx * 120) as u32,
                                dwFlags: MOUSEEVENTF_HWHEEL,
                                time: 0,
                                dwExtraInfo: 0,
                            },
                        },
                    });
                }
            }
            InputAction::Key { key, down } => {
                if let Some(vk) = Self::key_to_vk(&key.to_lowercase()) {
                    Self::send_one(INPUT {
                        r#type: INPUT_KEYBOARD,
                        Anonymous: INPUT_0 {
                            ki: KEYBDINPUT {
                                wVk: VIRTUAL_KEY(vk),
                                wScan: 0,
                                dwFlags: if *down { Default::default() } else { KEYEVENTF_KEYUP },
                                time: 0,
                                dwExtraInfo: 0,
                            },
                        },
                    });
                    if *down {
                        self.pressed_vks.insert(vk);
                    } else {
                        self.pressed_vks.remove(&vk);
                    }
                }
            }
            InputAction::ReleaseAll => self.release_all(),
        }
        Ok(())
    }

    fn release_all(&mut self) {
        for vk in self.pressed_vks.drain().collect::<Vec<_>>() {
            Self::send_one(INPUT {
                r#type: INPUT_KEYBOARD,
                Anonymous: INPUT_0 {
                    ki: KEYBDINPUT {
                        wVk: VIRTUAL_KEY(vk),
                        wScan: 0,
                        dwFlags: KEYEVENTF_KEYUP,
                        time: 0,
                        dwExtraInfo: 0,
                    },
                },
            });
        }
        for btn in self.pressed_buttons.drain().collect::<Vec<_>>() {
            let _ = self.apply(&InputAction::Button {
                button: btn,
                down: false,
            });
        }
    }
}

impl Drop for WinSendInputInjector {
    fn drop(&mut self) {
        self.release_all();
    }
}
