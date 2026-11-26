//! Native cross-platform low-latency input injection.
//!
//! - Linux X11: pure Rust `x11rb` XTest extension (zero Python/GIL latency)
//! - macOS / Windows: clean trait boundary for CGEventPost / SendInput

use brioche_proto::InputAction;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum InputError {
    #[error("input backend unavailable: {0}")]
    Unavailable(String),
    #[error("input injection error: {0}")]
    Inject(String),
}

#[derive(Debug, Clone, Copy)]
pub struct MonitorRect {
    pub left: i32,
    pub top: i32,
    pub width: u32,
    pub height: u32,
}

pub trait InputInjector: Send {
    fn set_monitor(&mut self, rect: MonitorRect);
    fn apply(&mut self, action: &InputAction) -> Result<(), InputError>;
    fn release_all(&mut self);
}

#[cfg(target_os = "linux")]
pub mod linux_xtest {
    use std::collections::HashSet;

    use brioche_proto::InputAction;
    use x11rb::connection::Connection;
    use x11rb::protocol::xproto::{self, ConnectionExt as XprotoExt};
    use x11rb::protocol::xtest::ConnectionExt as XtestExt;
    use x11rb::rust_connection::RustConnection;

    use crate::{InputError, InputInjector, MonitorRect};

    pub struct XTestInjector {
        conn: RustConnection,
        root: u32,
        rect: MonitorRect,
        pressed_buttons: HashSet<u8>,
        pressed_keycodes: HashSet<u8>,
        min_keycode: u8,
        keysyms_per_keycode: u8,
        keysyms: Vec<u32>,
    }

    impl XTestInjector {
        pub fn open(rect: MonitorRect) -> Result<Self, InputError> {
            let (conn, screen_num) = RustConnection::connect(None)
                .map_err(|e| InputError::Unavailable(format!("X11 connect failed: {e}")))?;
            let root = conn.setup().roots[screen_num].root;
            conn.xtest_get_version(2, 2)
                .map_err(|e| InputError::Unavailable(format!("XTest unavailable: {e}")))?
                .reply()
                .map_err(|e| InputError::Unavailable(format!("XTest reply failed: {e}")))?;

            let min_kc = conn.setup().min_keycode;
            let max_kc = conn.setup().max_keycode;
            let mapping = conn
                .get_keyboard_mapping(min_kc, max_kc - min_kc + 1)
                .map_err(|e| InputError::Unavailable(e.to_string()))?
                .reply()
                .map_err(|e| InputError::Unavailable(e.to_string()))?;

            Ok(Self {
                conn,
                root,
                rect,
                pressed_buttons: HashSet::new(),
                pressed_keycodes: HashSet::new(),
                min_keycode: min_kc,
                keysyms_per_keycode: mapping.keysyms_per_keycode,
                keysyms: mapping.keysyms,
            })
        }

        fn key_name_to_keysym(name: &str) -> Option<u32> {
            match name {
                "esc" | "escape" => Some(0xFF1B),
                "tab" => Some(0xFF09),
                "backspace" => Some(0xFF08),
                "enter" | "return" => Some(0xFF0D),
                "space" => Some(0x0020),
                "insert" => Some(0xFF63),
                "delete" => Some(0xFFFF),
                "home" => Some(0xFF50),
                "end" => Some(0xFF57),
                "left" => Some(0xFF51),
                "up" => Some(0xFF52),
                "right" => Some(0xFF53),
                "down" => Some(0xFF54),
                "page_up" => Some(0xFF55),
                "page_down" => Some(0xFF56),
                "shift" | "shift_l" => Some(0xFFE1),
                "shift_r" => Some(0xFFE2),
                "ctrl" | "ctrl_l" => Some(0xFFE3),
                "ctrl_r" => Some(0xFFE4),
                "alt" | "alt_l" => Some(0xFFE9),
                "alt_r" => Some(0xFFEA),
                "cmd" | "cmd_l" | "super" => Some(0xFFEB),
                "cmd_r" => Some(0xFFEC),
                "caps_lock" => Some(0xFFE5),
                "f1" => Some(0xFFBE),
                "f2" => Some(0xFFBF),
                "f3" => Some(0xFFC0),
                "f4" => Some(0xFFC1),
                "f5" => Some(0xFFC2),
                "f6" => Some(0xFFC3),
                "f7" => Some(0xFFC4),
                "f8" => Some(0xFFC5),
                "f9" => Some(0xFFC6),
                "f10" => Some(0xFFC7),
                "f11" => Some(0xFFC8),
                "f12" => Some(0xFFC9),
                s if s.chars().count() == 1 => {
                    let ch = s.chars().next().unwrap() as u32;
                    if (0x20..=0x7E).contains(&ch) {
                        Some(ch)
                    } else {
                        Some(0x0100_0000 | ch)
                    }
                }
                _ => None,
            }
        }

        fn keysym_to_keycode(&self, keysym: u32) -> Option<u8> {
            let stride = self.keysyms_per_keycode as usize;
            if stride == 0 {
                return None;
            }
            for (idx, chunk) in self.keysyms.chunks_exact(stride).enumerate() {
                if chunk.iter().take(2).any(|&ks| ks == keysym) {
                    return Some(self.min_keycode.wrapping_add(idx as u8));
                }
            }
            None
        }

        fn fake(&self, kind: u8, detail: u8) -> Result<(), InputError> {
            self.conn
                .xtest_fake_input(kind, detail, 0, self.root, 0, 0, 0)
                .map_err(|e| InputError::Inject(e.to_string()))?;
            self.conn
                .flush()
                .map_err(|e| InputError::Inject(e.to_string()))
        }
    }

    impl InputInjector for XTestInjector {
        fn set_monitor(&mut self, rect: MonitorRect) {
            self.release_all();
            self.rect = rect;
        }

        fn apply(&mut self, action: &InputAction) -> Result<(), InputError> {
            match action {
                InputAction::Move { x, y } => {
                    let nx = x.clamp(0.0, 1.0);
                    let ny = y.clamp(0.0, 1.0);
                    let px = self.rect.left + (nx * self.rect.width.saturating_sub(1) as f32).round() as i32;
                    let py = self.rect.top + (ny * self.rect.height.saturating_sub(1) as f32).round() as i32;
                    self.conn
                        .warp_pointer(x11rb::NONE, self.root, 0, 0, 0, 0, px as i16, py as i16)
                        .map_err(|e| InputError::Inject(e.to_string()))?;
                    self.conn
                        .flush()
                        .map_err(|e| InputError::Inject(e.to_string()))
                }
                InputAction::Button { button, down } => {
                    let code = match button.as_str() {
                        "left" => 1u8,
                        "middle" => 2u8,
                        "right" => 3u8,
                        _ => return Ok(()),
                    };
                    let ev = if *down {
                        xproto::BUTTON_PRESS_EVENT
                    } else {
                        xproto::BUTTON_RELEASE_EVENT
                    };
                    self.fake(ev, code)?;
                    if *down {
                        self.pressed_buttons.insert(code);
                    } else {
                        self.pressed_buttons.remove(&code);
                    }
                    Ok(())
                }
                InputAction::Scroll { dx, dy } => {
                    for _ in 0..dy.abs().min(10) {
                        let btn = if *dy > 0 { 4u8 } else { 5u8 };
                        self.fake(xproto::BUTTON_PRESS_EVENT, btn)?;
                        self.fake(xproto::BUTTON_RELEASE_EVENT, btn)?;
                    }
                    for _ in 0..dx.abs().min(10) {
                        let btn = if *dx < 0 { 6u8 } else { 7u8 };
                        self.fake(xproto::BUTTON_PRESS_EVENT, btn)?;
                        self.fake(xproto::BUTTON_RELEASE_EVENT, btn)?;
                    }
                    Ok(())
                }
                InputAction::Key { key, down } => {
                    let Some(ks) = Self::key_name_to_keysym(&key.to_lowercase()) else {
                        return Ok(());
                    };
                    let Some(kc) = self.keysym_to_keycode(ks) else {
                        return Ok(());
                    };
                    let ev = if *down {
                        xproto::KEY_PRESS_EVENT
                    } else {
                        xproto::KEY_RELEASE_EVENT
                    };
                    self.fake(ev, kc)?;
                    if *down {
                        self.pressed_keycodes.insert(kc);
                    } else {
                        self.pressed_keycodes.remove(&kc);
                    }
                    Ok(())
                }
                InputAction::ReleaseAll => {
                    self.release_all();
                    Ok(())
                }
            }
        }

        fn release_all(&mut self) {
            for kc in self.pressed_keycodes.drain().collect::<Vec<_>>() {
                let _ = self.fake(xproto::KEY_RELEASE_EVENT, kc);
            }
            for btn in self.pressed_buttons.drain().collect::<Vec<_>>() {
                let _ = self.fake(xproto::BUTTON_RELEASE_EVENT, btn);
            }
        }
    }

    impl Drop for XTestInjector {
        fn drop(&mut self) {
            self.release_all();
        }
    }
}

#[cfg(target_os = "macos")]
pub mod macos_cgevent;
#[cfg(target_os = "windows")]
pub mod windows_sendinput;

/// No-op fallback injector (for headless tests or read-only mode).
pub struct NoopInjector;

impl InputInjector for NoopInjector {
    fn set_monitor(&mut self, _rect: MonitorRect) {}
    fn apply(&mut self, _action: &InputAction) -> Result<(), InputError> {
        Ok(())
    }
    fn release_all(&mut self) {}
}

pub fn create_injector(rect: MonitorRect, view_only: bool) -> Box<dyn InputInjector> {
    if view_only {
        return Box::new(NoopInjector);
    }
    #[cfg(target_os = "linux")]
    {
        if let Ok(inj) = linux_xtest::XTestInjector::open(rect) {
            return Box::new(inj);
        }
    }
    #[cfg(target_os = "windows")]
    {
        return Box::new(windows_sendinput::WinSendInputInjector::open(rect));
    }
    #[cfg(target_os = "macos")]
    {
        return Box::new(macos_cgevent::MacCgEventInjector::open(rect));
    }
    #[allow(unreachable_code)]
    Box::new(NoopInjector)
}
