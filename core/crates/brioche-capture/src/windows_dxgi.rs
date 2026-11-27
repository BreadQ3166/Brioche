//! Windows DXGI Desktop Duplication (`IDXGIOutputDuplication`) capture + Win32 hardware cursor.
//!
//! Replaces legacy GDI `gdigrab` with zero-tearing GPU desktop duplication supporting
//! high refresh rates (60/120/144 Hz), multi-monitor enumeration, and dirty frame detection.

use std::time::Instant;

use windows::core::Interface;
use windows::Win32::Foundation::{POINT, RECT};
use windows::Win32::Graphics::Direct3D::{D3D_DRIVER_TYPE_UNKNOWN, D3D_FEATURE_LEVEL_11_0};
use windows::Win32::Graphics::Direct3D11::{
    D3D11CreateDevice, ID3D11Device, ID3D11DeviceContext, ID3D11Texture2D,
    D3D11_CPU_ACCESS_READ, D3D11_CREATE_DEVICE_BGRA_SUPPORT, D3D11_MAP_READ,
    D3D11_SDK_VERSION, D3D11_TEXTURE2D_DESC, D3D11_USAGE_STAGING,
};
use windows::Win32::Graphics::Dxgi::{
    CreateDXGIFactory1, IDXGIFactory1, IDXGIOutput1, IDXGIOutputDuplication,
    DXGI_ERROR_ACCESS_LOST, DXGI_ERROR_WAIT_TIMEOUT, DXGI_OUTDUPL_FRAME_INFO,
};
use windows::Win32::UI::WindowsAndMessaging::{GetCursorInfo, CURSORINFO, CURSOR_SHOWING};

use crate::{
    CaptureError, CapturedFrame, CapturedFrameRef, CursorSnapshot, DirtyDetector, MonitorInfo,
};

pub fn list_monitors() -> Result<Vec<MonitorInfo>, CaptureError> {
    unsafe {
        let factory: IDXGIFactory1 = CreateDXGIFactory1()
            .map_err(|e| CaptureError::Unavailable(format!("DXGI factory failed: {e}")))?;
        let mut monitors = Vec::new();
        let mut adapter_idx = 0u32;
        while let Ok(adapter) = factory.EnumAdapters1(adapter_idx) {
            let mut out_idx = 0u32;
            while let Ok(output) = adapter.EnumOutputs(out_idx) {
                if let Ok(desc) = output.GetDesc() {
                    let r: RECT = desc.DesktopCoordinates;
                    let w = (r.right - r.left).max(0) as u32;
                    let h = (r.bottom - r.top).max(0) as u32;
                    if w > 0 && h > 0 {
                        let id = (monitors.len() + 1) as u32;
                        monitors.push(MonitorInfo {
                            id,
                            name: format!("显示器 {id}"),
                            left: r.left,
                            top: r.top,
                            width: w,
                            height: h,
                            primary: r.left == 0 && r.top == 0,
                        });
                    }
                }
                out_idx += 1;
            }
            adapter_idx += 1;
        }
        Ok(monitors)
    }
}

pub struct DxgiCapturer {
    monitor: MonitorInfo,
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    duplication: IDXGIOutputDuplication,
    staging: Option<ID3D11Texture2D>,
    last_frame: Vec<u8>,
    dirty: DirtyDetector,
}

unsafe impl Send for DxgiCapturer {}

impl DxgiCapturer {
    pub fn open(monitor_id: u32) -> Result<Self, CaptureError> {
        unsafe {
            let factory: IDXGIFactory1 = CreateDXGIFactory1()
                .map_err(|e| CaptureError::Unavailable(format!("DXGI factory failed: {e}")))?;
            let mut seen = 0u32;
            let mut adapter_idx = 0u32;
            while let Ok(adapter) = factory.EnumAdapters1(adapter_idx) {
                let mut out_idx = 0u32;
                while let Ok(output) = adapter.EnumOutputs(out_idx) {
                    if let Ok(desc) = output.GetDesc() {
                        let r: RECT = desc.DesktopCoordinates;
                        let w = (r.right - r.left).max(0) as u32;
                        let h = (r.bottom - r.top).max(0) as u32;
                        if w > 0 && h > 0 {
                            seen += 1;
                            if seen == monitor_id {
                                let mut device = None;
                                let mut context = None;
                                let levels = [D3D_FEATURE_LEVEL_11_0];
                                D3D11CreateDevice(
                                    &adapter,
                                    D3D_DRIVER_TYPE_UNKNOWN,
                                    None,
                                    D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                                    Some(&levels),
                                    D3D11_SDK_VERSION,
                                    Some(&mut device),
                                    None,
                                    Some(&mut context),
                                )
                                .map_err(|e| {
                                    CaptureError::Unavailable(format!("D3D11CreateDevice: {e}"))
                                })?;
                                let device = device.unwrap();
                                let context = context.unwrap();
                                let output1: IDXGIOutput1 = output.cast().map_err(|e| {
                                    CaptureError::Unavailable(format!("IDXGIOutput1 cast: {e}"))
                                })?;
                                let duplication =
                                    output1.DuplicateOutput(&device).map_err(|e| {
                                        CaptureError::Unavailable(format!("DuplicateOutput: {e}"))
                                    })?;
                                let monitor = MonitorInfo {
                                    id: monitor_id,
                                    name: format!("显示器 {monitor_id}"),
                                    left: r.left,
                                    top: r.top,
                                    width: w,
                                    height: h,
                                    primary: r.left == 0 && r.top == 0,
                                };
                                return Ok(Self {
                                    last_frame: vec![0u8; (w * h * 4) as usize],
                                    monitor,
                                    device,
                                    context,
                                    duplication,
                                    staging: None,
                                    dirty: DirtyDetector::default(),
                                });
                            }
                        }
                    }
                    out_idx += 1;
                }
                adapter_idx += 1;
            }
            Err(CaptureError::MonitorNotFound(monitor_id))
        }
    }

    pub fn monitor(&self) -> &MonitorInfo {
        &self.monitor
    }

    /// Zero-clone DXGI capture: skips GPU->CPU CopyResource completely when desktop is static.
    pub fn grab_frame_ref(&mut self) -> Result<CapturedFrameRef<'_>, CaptureError> {
        let started = Instant::now();
        unsafe {
            let mut info = DXGI_OUTDUPL_FRAME_INFO::default();
            let mut resource = None;
            match self.duplication.AcquireNextFrame(16, &mut info, &mut resource) {
                Ok(()) => {}
                Err(e) if e.code() == DXGI_ERROR_WAIT_TIMEOUT => {
                    return Ok(CapturedFrameRef {
                        width: self.monitor.width,
                        height: self.monitor.height,
                        stride: self.monitor.width * 4,
                        bgra: &self.last_frame,
                        dirty: false,
                        capture_us: started.elapsed().as_micros() as u64,
                    });
                }
                Err(e) if e.code() == DXGI_ERROR_ACCESS_LOST => {
                    return Err(CaptureError::System("DXGI access lost".into()));
                }
                Err(e) => return Err(CaptureError::System(e.to_string())),
            }

            // Cursor-only DXGI wakeups have AccumulatedFrames == 0: skip full GPU staging readback!
            if info.AccumulatedFrames == 0 {
                let _ = self.duplication.ReleaseFrame();
                return Ok(CapturedFrameRef {
                    width: self.monitor.width,
                    height: self.monitor.height,
                    stride: self.monitor.width * 4,
                    bgra: &self.last_frame,
                    dirty: false,
                    capture_us: started.elapsed().as_micros() as u64,
                });
            }

            let tex: ID3D11Texture2D = resource
                .unwrap()
                .cast()
                .map_err(|e| CaptureError::System(e.to_string()))?;
            let mut desc = D3D11_TEXTURE2D_DESC::default();
            tex.GetDesc(&mut desc);

            if self.staging.is_none() {
                let mut sdesc = desc;
                sdesc.Usage = D3D11_USAGE_STAGING;
                sdesc.BindFlags = 0;
                sdesc.CPUAccessFlags = D3D11_CPU_ACCESS_READ.0 as u32;
                sdesc.MiscFlags = 0;
                let mut staging = None;
                self.device
                    .CreateTexture2D(&sdesc, None, Some(&mut staging))
                    .map_err(|e| CaptureError::System(e.to_string()))?;
                self.staging = staging;
            }

            let staging = self.staging.as_ref().unwrap();
            self.context.CopyResource(staging, &tex);
            let mut mapped = Default::default();
            self.context
                .Map(staging, 0, D3D11_MAP_READ, 0, Some(&mut mapped))
                .map_err(|e| CaptureError::System(e.to_string()))?;

            let w = self.monitor.width as usize;
            let h = self.monitor.height as usize;
            let row_bytes = w * 4;
            let src_pitch = mapped.RowPitch as usize;
            let src_ptr = mapped.pData as *const u8;
            if src_pitch == row_bytes {
                let src = std::slice::from_raw_parts(src_ptr, row_bytes * h);
                self.last_frame.copy_from_slice(src);
            } else {
                for row in 0..h {
                    let dst = &mut self.last_frame[row * row_bytes..(row + 1) * row_bytes];
                    let src = std::slice::from_raw_parts(src_ptr.add(row * src_pitch), row_bytes);
                    dst.copy_from_slice(src);
                }
            }
            self.context.Unmap(staging, 0);
            let _ = self.duplication.ReleaseFrame();

            let dirty = self.dirty.check_bgra(
                &self.last_frame,
                self.monitor.width,
                self.monitor.height,
                self.monitor.width * 4,
            );
            Ok(CapturedFrameRef {
                width: self.monitor.width,
                height: self.monitor.height,
                stride: self.monitor.width * 4,
                bgra: &self.last_frame,
                dirty,
                capture_us: started.elapsed().as_micros() as u64,
            })
        }
    }

    pub fn grab_frame(&mut self) -> Result<CapturedFrame, CaptureError> {
        let r = self.grab_frame_ref()?;
        Ok(CapturedFrame {
            width: r.width,
            height: r.height,
            stride: r.stride,
            bgra: r.bgra.to_vec(),
            dirty: r.dirty,
            capture_us: r.capture_us,
        })
    }

    pub fn grab_cursor(&mut self) -> Result<(CursorSnapshot, bool), CaptureError> {
        unsafe {
            let mut ci = CURSORINFO {
                cbSize: std::mem::size_of::<CURSORINFO>() as u32,
                flags: Default::default(),
                hCursor: Default::default(),
                ptScreenPos: POINT::default(),
            };
            GetCursorInfo(&mut ci).map_err(|e| CaptureError::System(e.to_string()))?;
            let visible = (ci.flags.0 & CURSOR_SHOWING.0) != 0;
            Ok((
                CursorSnapshot {
                    x: if visible { ci.ptScreenPos.x - self.monitor.left } else { -1 },
                    y: if visible { ci.ptScreenPos.y - self.monitor.top } else { -1 },
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
