//! macOS 12+ CoreGraphics direct display capture & cursor position (compatible with macOS 12.0+ x86_64 & arm64).
//!
//! Uses pure C FFI against CoreGraphics.framework with explicit ownership/release of every CF/CG object.

use std::ffi::c_void;
use std::time::Instant;

use crate::{CaptureError, CapturedFrame, CursorSnapshot, DirtyDetector, MonitorInfo};

#[repr(C)]
#[derive(Clone, Copy)]
struct CGPoint {
    x: f64,
    y: f64,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct CGSize {
    width: f64,
    height: f64,
}

#[repr(C)]
#[derive(Clone, Copy)]
struct CGRect {
    origin: CGPoint,
    size: CGSize,
}

#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
    fn CGGetActiveDisplayList(max: u32, active: *mut u32, count: *mut u32) -> i32;
    fn CGMainDisplayID() -> u32;
    fn CGDisplayBounds(display: u32) -> CGRect;
    fn CGDisplayCreateImage(display: u32) -> *mut c_void;
    fn CGImageGetWidth(image: *mut c_void) -> usize;
    fn CGImageGetHeight(image: *mut c_void) -> usize;
    fn CGImageGetBytesPerRow(image: *mut c_void) -> usize;
    fn CGImageGetDataProvider(image: *mut c_void) -> *mut c_void;
    fn CGDataProviderCopyData(provider: *mut c_void) -> *mut c_void;
    fn CGImageRelease(image: *mut c_void);
    fn CGEventCreate(source: *mut c_void) -> *mut c_void;
    fn CGEventGetLocation(event: *mut c_void) -> CGPoint;
}

#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    fn CFDataGetLength(data: *mut c_void) -> isize;
    fn CFDataGetBytePtr(data: *mut c_void) -> *const u8;
    fn CFRelease(cf: *mut c_void);
}

fn active_displays() -> Vec<u32> {
    let mut ids = [0u32; 32];
    let mut count = 0u32;
    let rc = unsafe { CGGetActiveDisplayList(32, ids.as_mut_ptr(), &mut count) };
    if rc != 0 {
        return Vec::new();
    }
    ids[..count.min(32) as usize].to_vec()
}

pub fn list_monitors() -> Result<Vec<MonitorInfo>, CaptureError> {
    let main = unsafe { CGMainDisplayID() };
    let mut out = Vec::new();
    for (i, did) in active_displays().into_iter().enumerate() {
        let b = unsafe { CGDisplayBounds(did) };
        let id = (i + 1) as u32;
        out.push(MonitorInfo {
            id,
            name: format!("显示器 {id}"),
            left: b.origin.x.round() as i32,
            top: b.origin.y.round() as i32,
            width: b.size.width.max(1.0).round() as u32,
            height: b.size.height.max(1.0).round() as u32,
            primary: did == main,
        });
    }
    Ok(out)
}

pub struct MacCgCapturer {
    display_id: u32,
    monitor: MonitorInfo,
    dirty: DirtyDetector,
}

unsafe impl Send for MacCgCapturer {}

impl MacCgCapturer {
    pub fn open(monitor_id: u32) -> Result<Self, CaptureError> {
        let displays = active_displays();
        let monitors = list_monitors()?;
        let idx = monitor_id
            .checked_sub(1)
            .and_then(|i| displays.get(i as usize).copied())
            .ok_or(CaptureError::MonitorNotFound(monitor_id))?;
        let monitor = monitors
            .into_iter()
            .find(|m| m.id == monitor_id)
            .ok_or(CaptureError::MonitorNotFound(monitor_id))?;
        Ok(Self {
            display_id: idx,
            monitor,
            dirty: DirtyDetector::default(),
        })
    }

    pub fn monitor(&self) -> &MonitorInfo {
        &self.monitor
    }

    pub fn grab_frame(&mut self) -> Result<CapturedFrame, CaptureError> {
        let started = Instant::now();
        unsafe {
            let img = CGDisplayCreateImage(self.display_id);
            if img.is_null() {
                return Err(CaptureError::Unavailable(
                    "macOS 屏幕录制权限未授予或采集失败".into(),
                ));
            }
            let width = CGImageGetWidth(img) as u32;
            let height = CGImageGetHeight(img) as u32;
            let stride = CGImageGetBytesPerRow(img);
            let provider = CGImageGetDataProvider(img);
            if provider.is_null() {
                CGImageRelease(img);
                return Err(CaptureError::System("null CGDataProvider".into()));
            }
            let data = CGDataProviderCopyData(provider);
            if data.is_null() {
                CGImageRelease(img);
                return Err(CaptureError::System("null CFData".into()));
            }
            let len = CFDataGetLength(data) as usize;
            let ptr = CFDataGetBytePtr(data);
            let row_bytes = width as usize * 4;
            let mut bgra = Vec::with_capacity(row_bytes * height as usize);
            if !ptr.is_null() && len >= stride * height as usize {
                let raw = std::slice::from_raw_parts(ptr, len);
                for r in 0..height as usize {
                    bgra.extend_from_slice(&raw[r * stride..r * stride + row_bytes]);
                }
            }
            CFRelease(data);
            CGImageRelease(img);

            let dirty = self.dirty.check_bgra(&bgra, width, height, width * 4);
            Ok(CapturedFrame {
                width,
                height,
                stride: width * 4,
                bgra,
                dirty,
                capture_us: started.elapsed().as_micros() as u64,
            })
        }
    }

    pub fn grab_cursor(&mut self) -> Result<(CursorSnapshot, bool), CaptureError> {
        unsafe {
            let ev = CGEventCreate(std::ptr::null_mut());
            if ev.is_null() {
                return Err(CaptureError::System("CGEventCreate failed".into()));
            }
            let loc = CGEventGetLocation(ev);
            CFRelease(ev);
            Ok((
                CursorSnapshot {
                    x: loc.x.round() as i32 - self.monitor.left,
                    y: loc.y.round() as i32 - self.monitor.top,
                    width: 0,
                    height: 0,
                    hotspot_x: 0,
                    hotspot_y: 0,
                    shape_hash: String::new(),
                    rgba: Vec::new(),
                },
                false,
            ))
        }
    }
}
