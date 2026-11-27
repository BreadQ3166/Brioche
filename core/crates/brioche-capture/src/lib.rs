//! Native cross-platform screen capture with hardware cursor separation
//! and ultra-fast tile-grid dirty detection for idle frame pacing (0-1 FPS static -> 144 FPS active).

use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;

#[cfg(target_os = "linux")]
pub mod linux_x11;
#[cfg(target_os = "macos")]
pub mod macos_cg;
#[cfg(target_os = "windows")]
pub mod windows_dxgi;

#[derive(Debug, Error)]
pub enum CaptureError {
    #[error("capture backend unavailable: {0}")]
    Unavailable(String),
    #[error("monitor {0} not found")]
    MonitorNotFound(u32),
    #[error("capture io/system failure: {0}")]
    System(String),
}

#[derive(Debug, Clone, Serialize)]
pub struct MonitorInfo {
    pub id: u32,
    pub name: String,
    pub left: i32,
    pub top: i32,
    pub width: u32,
    pub height: u32,
    pub primary: bool,
}

#[derive(Debug, Clone)]
pub struct CursorSnapshot {
    pub x: i32,
    pub y: i32,
    pub width: u16,
    pub height: u16,
    pub hotspot_x: u16,
    pub hotspot_y: u16,
    pub shape_hash: String,
    pub rgba: Vec<u8>,
}

#[derive(Debug, Clone)]
pub struct CapturedFrame {
    pub width: u32,
    pub height: u32,
    pub stride: u32,
    pub bgra: Vec<u8>,
    pub dirty: bool,
    pub capture_us: u64,
}

/// Zero-copy borrowed view into SHM / DXGI staging / reusable capture buffer (saves 33MB/frame @ 4K).
pub struct CapturedFrameRef<'a> {
    pub width: u32,
    pub height: u32,
    pub stride: u32,
    pub bgra: &'a [u8],
    pub dirty: bool,
    pub capture_us: u64,
}

pub trait ScreenCapturer: Send {
    fn monitor(&self) -> &MonitorInfo;
    fn grab_frame(&mut self) -> Result<CapturedFrame, CaptureError>;
    fn grab_frame_ref(&mut self) -> Result<CapturedFrameRef<'_>, CaptureError>;
    fn grab_cursor(&mut self) -> Result<(CursorSnapshot, bool), CaptureError>;
}

#[cfg(target_os = "linux")]
impl ScreenCapturer for linux_x11::X11ShmCapturer {
    fn monitor(&self) -> &MonitorInfo {
        self.monitor()
    }
    fn grab_frame(&mut self) -> Result<CapturedFrame, CaptureError> {
        self.grab_frame()
    }
    fn grab_frame_ref(&mut self) -> Result<CapturedFrameRef<'_>, CaptureError> {
        self.grab_frame_ref()
    }
    fn grab_cursor(&mut self) -> Result<(CursorSnapshot, bool), CaptureError> {
        self.grab_cursor()
    }
}

#[cfg(target_os = "windows")]
impl ScreenCapturer for windows_dxgi::DxgiCapturer {
    fn monitor(&self) -> &MonitorInfo {
        self.monitor()
    }
    fn grab_frame(&mut self) -> Result<CapturedFrame, CaptureError> {
        self.grab_frame()
    }
    fn grab_frame_ref(&mut self) -> Result<CapturedFrameRef<'_>, CaptureError> {
        self.grab_frame_ref()
    }
    fn grab_cursor(&mut self) -> Result<(CursorSnapshot, bool), CaptureError> {
        self.grab_cursor()
    }
}

#[cfg(target_os = "macos")]
impl ScreenCapturer for macos_cg::MacCgCapturer {
    fn monitor(&self) -> &MonitorInfo {
        self.monitor()
    }
    fn grab_frame(&mut self) -> Result<CapturedFrame, CaptureError> {
        self.grab_frame()
    }
    fn grab_frame_ref(&mut self) -> Result<CapturedFrameRef<'_>, CaptureError> {
        self.grab_frame_ref()
    }
    fn grab_cursor(&mut self) -> Result<(CursorSnapshot, bool), CaptureError> {
        self.grab_cursor()
    }
}

pub fn list_monitors() -> Result<Vec<MonitorInfo>, CaptureError> {
    #[cfg(target_os = "linux")]
    {
        linux_x11::list_monitors()
    }
    #[cfg(target_os = "windows")]
    {
        windows_dxgi::list_monitors()
    }
    #[cfg(target_os = "macos")]
    {
        macos_cg::list_monitors()
    }
}

pub fn open_capturer(monitor_id: u32) -> Result<Box<dyn ScreenCapturer>, CaptureError> {
    #[cfg(target_os = "linux")]
    {
        Ok(Box::new(linux_x11::X11ShmCapturer::open(monitor_id)?))
    }
    #[cfg(target_os = "windows")]
    {
        Ok(Box::new(windows_dxgi::DxgiCapturer::open(monitor_id)?))
    }
    #[cfg(target_os = "macos")]
    {
        Ok(Box::new(macos_cg::MacCgCapturer::open(monitor_id)?))
    }
}

pub fn hash_rgba(rgba: &[u8]) -> String {
    let digest = Sha256::digest(rgba);
    let mut s = String::with_capacity(64);
    for b in digest {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// Fast sampled tile-grid hash detector for idle frame pacing.
#[derive(Default)]
pub struct DirtyDetector {
    last_hash: Option<u64>,
}

impl DirtyDetector {
    pub fn check_bgra(&mut self, bgra: &[u8], width: u32, height: u32, stride: u32) -> bool {
        let mut h: u64 = 0xcbf29ce484222325;
        let row_bytes = (width as usize * 4).min(stride as usize);
        let step_y = (height as usize / 64).max(1);
        let mut y = 0usize;
        while y < height as usize {
            let start = y * stride as usize;
            let end = (start + row_bytes).min(bgra.len());
            let row = &bgra[start..end];
            let mut x = 0usize;
            while x + 8 <= row.len() {
                let word = u64::from_ne_bytes(row[x..x + 8].try_into().unwrap());
                h ^= word;
                h = h.wrapping_mul(0x100000001b3);
                x += 64;
            }
            y += step_y;
        }
        let changed = self.last_hash != Some(h);
        self.last_hash = Some(h);
        changed
    }
}

pub fn probe_capabilities() -> serde_json::Value {
    let monitors = list_monitors().unwrap_or_default();
    #[cfg(target_os = "linux")]
    {
        let pipewire_lib = std::path::Path::new("/lib/x86_64-linux-gnu/libpipewire-0.3.so.0").exists()
            || std::path::Path::new("/usr/lib/x86_64-linux-gnu/libpipewire-0.3.so.0").exists();
        serde_json::json!({
            "active_backend": "x11-mit-shm+xfixes-cursor",
            "pipewire_dmabuf_ready": pipewire_lib,
            "monitors": monitors,
        })
    }
    #[cfg(target_os = "windows")]
    {
        serde_json::json!({
            "active_backend": "dxgi-desktop-duplication",
            "monitors": monitors,
        })
    }
    #[cfg(target_os = "macos")]
    {
        serde_json::json!({
            "active_backend": "macos-coregraphics-direct",
            "monitors": monitors,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dirty_detector_identifies_static_vs_changed_frames() {
        let mut det = DirtyDetector::default();
        let mut buf = vec![0x20u8; 640 * 360 * 4];
        assert!(det.check_bgra(&buf, 640, 360, 640 * 4));
        assert!(!det.check_bgra(&buf, 640, 360, 640 * 4));
        buf[0] = 0xFF;
        assert!(det.check_bgra(&buf, 640, 360, 640 * 4));
    }
}
