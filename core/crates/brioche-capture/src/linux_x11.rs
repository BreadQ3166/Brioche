//! Linux X11 MIT-SHM zero-socket-copy screen capture + XFixes separated hardware cursor + RandR monitors.

use std::ptr;
use std::time::Instant;

use x11rb::connection::Connection;
use x11rb::protocol::randr::ConnectionExt as RandrExt;
use x11rb::protocol::shm::ConnectionExt as ShmExt;
use x11rb::protocol::xfixes::ConnectionExt as XFixesExt;
use x11rb::protocol::xproto::ImageFormat;
use x11rb::rust_connection::RustConnection;

use crate::{hash_rgba, CaptureError, CapturedFrame, CursorSnapshot, DirtyDetector, MonitorInfo};

pub fn list_monitors() -> Result<Vec<MonitorInfo>, CaptureError> {
    let (conn, screen_num) = RustConnection::connect(None)
        .map_err(|e| CaptureError::Unavailable(format!("X11 connect failed: {e}")))?;
    let root = conn.setup().roots[screen_num].root;
    let reply = conn
        .randr_get_monitors(root, true)
        .map_err(|e| CaptureError::System(e.to_string()))?
        .reply()
        .map_err(|e| CaptureError::System(e.to_string()))?;

    let mut out = Vec::new();
    for (idx, m) in reply.monitors.iter().enumerate() {
        let name = format!("显示器 {}", idx + 1);
        out.push(MonitorInfo {
            id: (idx + 1) as u32,
            name,
            left: m.x as i32,
            top: m.y as i32,
            width: m.width as u32,
            height: m.height as u32,
            primary: m.primary,
        });
    }
    Ok(out)
}

pub struct X11ShmCapturer {
    conn: RustConnection,
    root: u32,
    monitor: MonitorInfo,
    shm_id: libc::c_int,
    shm_ptr: *mut u8,
    shm_size: usize,
    seg_id: u32,
    dirty: DirtyDetector,
    last_cursor_hash: String,
}

unsafe impl Send for X11ShmCapturer {}

impl X11ShmCapturer {
    pub fn open(monitor_id: u32) -> Result<Self, CaptureError> {
        let (conn, screen_num) = RustConnection::connect(None)
            .map_err(|e| CaptureError::Unavailable(format!("X11 connect failed: {e}")))?;
        let root = conn.setup().roots[screen_num].root;

        // Verify MIT-SHM & XFixes extensions
        conn.shm_query_version()
            .map_err(|e| CaptureError::Unavailable(format!("MIT-SHM unavailable: {e}")))?
            .reply()
            .map_err(|e| CaptureError::Unavailable(format!("MIT-SHM reply failed: {e}")))?;
        if let Ok(cookie) = conn.xfixes_query_version(4, 0) {
            let _ = cookie.reply();
        }

        let monitors = list_monitors()?;
        let monitor = monitors
            .into_iter()
            .find(|m| m.id == monitor_id)
            .ok_or(CaptureError::MonitorNotFound(monitor_id))?;

        let shm_size = (monitor.width as usize) * (monitor.height as usize) * 4;
        let shm_id = unsafe {
            libc::shmget(
                libc::IPC_PRIVATE,
                shm_size,
                libc::IPC_CREAT | 0o600,
            )
        };
        if shm_id < 0 {
            return Err(CaptureError::System("shmget failed".into()));
        }
        let shm_ptr = unsafe { libc::shmat(shm_id, ptr::null(), 0) } as *mut u8;
        if shm_ptr as isize == -1 {
            unsafe { libc::shmctl(shm_id, libc::IPC_RMID, ptr::null_mut()) };
            return Err(CaptureError::System("shmat failed".into()));
        }
        // Mark segment for automatic kernel cleanup once detached/process exits
        unsafe { libc::shmctl(shm_id, libc::IPC_RMID, ptr::null_mut()) };

        let seg_id = conn
            .generate_id()
            .map_err(|e| CaptureError::System(e.to_string()))?;
        conn.shm_attach(seg_id, shm_id as u32, false)
            .map_err(|e| CaptureError::System(e.to_string()))?;
        conn.flush()
            .map_err(|e| CaptureError::System(e.to_string()))?;

        Ok(Self {
            conn,
            root,
            monitor,
            shm_id,
            shm_ptr,
            shm_size,
            seg_id,
            dirty: DirtyDetector::default(),
            last_cursor_hash: String::new(),
        })
    }

    pub fn monitor(&self) -> &MonitorInfo {
        &self.monitor
    }

    /// Grab one monitor frame directly into POSIX shared memory (zero X11 socket copy).
    pub fn grab_frame(&mut self) -> Result<CapturedFrame, CaptureError> {
        let started = Instant::now();
        self.conn
            .shm_get_image(
                self.root,
                self.monitor.left as i16,
                self.monitor.top as i16,
                self.monitor.width as u16,
                self.monitor.height as u16,
                !0u32,
                ImageFormat::Z_PIXMAP.into(),
                self.seg_id,
                0,
            )
            .map_err(|e| CaptureError::System(e.to_string()))?
            .reply()
            .map_err(|e| CaptureError::System(e.to_string()))?;

        let slice = unsafe { std::slice::from_raw_parts(self.shm_ptr, self.shm_size) };
        let stride = self.monitor.width * 4;
        let dirty = self
            .dirty
            .check_bgra(slice, self.monitor.width, self.monitor.height, stride);
        let bgra = slice.to_vec();
        Ok(CapturedFrame {
            width: self.monitor.width,
            height: self.monitor.height,
            stride,
            bgra,
            dirty,
            capture_us: started.elapsed().as_micros() as u64,
        })
    }

    /// Capture separated hardware cursor via XFixes (returns RGBA bitmap only when shape changes).
    pub fn grab_cursor(&mut self) -> Result<(CursorSnapshot, bool), CaptureError> {
        let reply = self
            .conn
            .xfixes_get_cursor_image()
            .map_err(|e| CaptureError::System(e.to_string()))?
            .reply()
            .map_err(|e| CaptureError::System(e.to_string()))?;

        let mut rgba = Vec::with_capacity(reply.cursor_image.len() * 4);
        for &argb in &reply.cursor_image {
            let a = ((argb >> 24) & 0xFF) as u8;
            let r = ((argb >> 16) & 0xFF) as u8;
            let g = ((argb >> 8) & 0xFF) as u8;
            let b = (argb & 0xFF) as u8;
            rgba.extend_from_slice(&[r, g, b, a]);
        }
        let shape_hash = hash_rgba(&rgba);
        let shape_changed = shape_hash != self.last_cursor_hash;
        if shape_changed {
            self.last_cursor_hash = shape_hash.clone();
        }
        Ok((
            CursorSnapshot {
                x: reply.x as i32 - self.monitor.left,
                y: reply.y as i32 - self.monitor.top,
                width: reply.width,
                height: reply.height,
                hotspot_x: reply.xhot,
                hotspot_y: reply.yhot,
                shape_hash,
                rgba,
            },
            shape_changed,
        ))
    }
}

impl Drop for X11ShmCapturer {
    fn drop(&mut self) {
        let _ = self.conn.shm_detach(self.seg_id);
        let _ = self.conn.flush();
        if !self.shm_ptr.is_null() && self.shm_ptr as isize != -1 {
            unsafe { libc::shmdt(self.shm_ptr as *const libc::c_void) };
        }
        let _ = self.shm_id;
    }
}
