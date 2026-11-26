//! Native GPU-accelerated viewer window powered by `winit` + `wgpu` (Vulkan / Metal / DX12).
//!
//! Zero 6ms polling timer: incoming decoded YUV frames wake the window via `EventLoopProxy`
//! and render directly on the GPU via `PresentMode::Mailbox` / VSync.

use std::sync::{Arc, Mutex};

use brioche_proto::{ControlMessage, InputAction};
use winit::application::ApplicationHandler;
use winit::dpi::LogicalSize;
use winit::event::{ElementState, MouseButton, MouseScrollDelta, WindowEvent};
use winit::event_loop::{ActiveEventLoop, EventLoop, EventLoopProxy};
use winit::keyboard::{Key, ModifiersState, NamedKey};
use winit::window::{Fullscreen, Window, WindowId};

use crate::{ChromaFormat, GpuVideoRenderer, RenderError};

#[derive(Debug, Clone)]
pub struct PendingYuvFrame {
    pub width: u32,
    pub height: u32,
    pub chroma: ChromaFormat,
    pub y: Vec<u8>,
    pub u: Vec<u8>,
    pub v: Vec<u8>,
}

#[derive(Debug, Clone)]
pub struct PendingCursorShape {
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
}

#[derive(Debug, Clone)]
pub enum ViewerWake {
    FrameReady,
    CursorShape(PendingCursorShape),
    CursorPos { x: f32, y: f32, visible: bool },
    Title(String),
    Close,
}

#[derive(Clone)]
pub struct ViewerHandle {
    mailbox: Arc<Mutex<Option<PendingYuvFrame>>>,
    proxy: EventLoopProxy<ViewerWake>,
}

impl ViewerHandle {
    pub fn publish_frame(&self, frame: PendingYuvFrame) {
        if let Ok(mut slot) = self.mailbox.lock() {
            *slot = Some(frame);
        }
        let _ = self.proxy.send_event(ViewerWake::FrameReady);
    }

    pub fn update_cursor_shape(&self, width: u32, height: u32, rgba: Vec<u8>) {
        let _ = self
            .proxy
            .send_event(ViewerWake::CursorShape(PendingCursorShape {
                width,
                height,
                rgba,
            }));
    }

    pub fn update_cursor_pos(&self, x: f32, y: f32, visible: bool) {
        let _ = self
            .proxy
            .send_event(ViewerWake::CursorPos { x, y, visible });
    }

    pub fn set_title(&self, title: String) {
        let _ = self.proxy.send_event(ViewerWake::Title(title));
    }

    pub fn close(&self) {
        let _ = self.proxy.send_event(ViewerWake::Close);
    }
}

struct GpuSurfaceState {
    window: Arc<Window>,
    surface: wgpu::Surface<'static>,
    config: wgpu::SurfaceConfiguration,
    renderer: GpuVideoRenderer,
}

struct ViewerApp {
    title: String,
    mailbox: Arc<Mutex<Option<PendingYuvFrame>>>,
    control_tx: std::sync::mpsc::Sender<ControlMessage>,
    gpu: Option<GpuSurfaceState>,
    modifiers: ModifiersState,
    input_captured: bool,
    sharpness: f32,
    req_seq: u32,
}

impl ViewerApp {
    fn send_input(&self, action: InputAction) {
        let _ = self
            .control_tx
            .send(ControlMessage::InputEvent { event: action });
    }

    fn init_gpu(&mut self, event_loop: &ActiveEventLoop) -> Result<(), RenderError> {
        let attrs = Window::default_attributes()
            .with_title(self.title.clone())
            .with_inner_size(LogicalSize::new(1280.0, 760.0));
        let window = Arc::new(
            event_loop
                .create_window(attrs)
                .map_err(|e| RenderError::Device(e.to_string()))?,
        );
        let size = window.inner_size();
        let instance = wgpu::Instance::new(wgpu::InstanceDescriptor {
            backends: wgpu::Backends::PRIMARY,
            ..Default::default()
        });
        let surface = instance
            .create_surface(window.clone())
            .map_err(|e| RenderError::Device(e.to_string()))?;
        let adapter = pollster::block_on(instance.request_adapter(&wgpu::RequestAdapterOptions {
            power_preference: wgpu::PowerPreference::HighPerformance,
            compatible_surface: Some(&surface),
            force_fallback_adapter: false,
        }))
        .ok_or(RenderError::NoAdapter)?;

        let info = adapter.get_info();
        let (device, queue) = pollster::block_on(adapter.request_device(
            &wgpu::DeviceDescriptor {
                label: Some("brioche-viewer-gpu"),
                required_features: wgpu::Features::empty(),
                required_limits: wgpu::Limits::downlevel_defaults(),
                memory_hints: wgpu::MemoryHints::Performance,
            },
            None,
        ))
        .map_err(|e| RenderError::Device(e.to_string()))?;

        let caps = surface.get_capabilities(&adapter);
        let format = caps
            .formats
            .iter()
            .copied()
            .find(|f| !f.is_srgb())
            .unwrap_or(caps.formats[0]);
        let present_mode = if caps.present_modes.contains(&wgpu::PresentMode::Mailbox) {
            wgpu::PresentMode::Mailbox
        } else if caps.present_modes.contains(&wgpu::PresentMode::Immediate) {
            wgpu::PresentMode::Immediate
        } else {
            wgpu::PresentMode::Fifo
        };

        let config = wgpu::SurfaceConfiguration {
            usage: wgpu::TextureUsages::RENDER_ATTACHMENT,
            format,
            width: size.width.max(1),
            height: size.height.max(1),
            present_mode,
            alpha_mode: caps.alpha_modes[0],
            view_formats: vec![],
            desired_maximum_frame_latency: 1,
        };
        surface.configure(&device, &config);

        let mut renderer = GpuVideoRenderer::from_device(
            device,
            queue,
            info.name,
            format!("{:?}", info.backend),
            64,
            64,
            ChromaFormat::Yuv420p,
            format,
        )?;
        renderer.set_sharpness(self.sharpness);
        renderer.set_full_range(false);

        self.gpu = Some(GpuSurfaceState {
            window,
            surface,
            config,
            renderer,
        });
        Ok(())
    }

    fn redraw(&mut self) {
        let Some(gpu) = self.gpu.as_mut() else {
            return;
        };
        if let Ok(mut slot) = self.mailbox.lock() {
            if let Some(frame) = slot.take() {
                let _ = gpu.renderer.upload_yuv(
                    frame.width,
                    frame.height,
                    frame.chroma,
                    &frame.y,
                    &frame.u,
                    &frame.v,
                );
            }
        }
        match gpu.surface.get_current_texture() {
            Ok(output) => {
                let view = output
                    .texture
                    .create_view(&wgpu::TextureViewDescriptor::default());
                gpu.renderer.render_to_view(&view);
                output.present();
            }
            Err(wgpu::SurfaceError::Lost | wgpu::SurfaceError::Outdated) => {
                gpu.surface.configure(&gpu.renderer.device, &gpu.config);
            }
            Err(_) => {}
        }
    }
}

fn map_winit_key(key: &Key) -> Option<String> {
    match key {
        Key::Named(n) => Some(
            match n {
                NamedKey::Escape => "esc",
                NamedKey::Tab => "tab",
                NamedKey::Backspace => "backspace",
                NamedKey::Enter => "enter",
                NamedKey::Space => "space",
                NamedKey::Insert => "insert",
                NamedKey::Delete => "delete",
                NamedKey::Home => "home",
                NamedKey::End => "end",
                NamedKey::ArrowLeft => "left",
                NamedKey::ArrowUp => "up",
                NamedKey::ArrowRight => "right",
                NamedKey::ArrowDown => "down",
                NamedKey::PageUp => "page_up",
                NamedKey::PageDown => "page_down",
                NamedKey::Shift => "shift",
                NamedKey::Control => "ctrl",
                NamedKey::Alt => "alt",
                NamedKey::Super => "cmd",
                NamedKey::CapsLock => "caps_lock",
                NamedKey::F1 => "f1",
                NamedKey::F2 => "f2",
                NamedKey::F3 => "f3",
                NamedKey::F4 => "f4",
                NamedKey::F5 => "f5",
                NamedKey::F6 => "f6",
                NamedKey::F7 => "f7",
                NamedKey::F8 => "f8",
                NamedKey::F9 => "f9",
                NamedKey::F10 => "f10",
                NamedKey::F11 => "f11",
                NamedKey::F12 => "f12",
                _ => return None,
            }
            .to_string(),
        ),
        Key::Character(s) if s.chars().count() == 1 => Some(s.to_string()),
        _ => None,
    }
}

impl ApplicationHandler<ViewerWake> for ViewerApp {
    fn resumed(&mut self, event_loop: &ActiveEventLoop) {
        if self.gpu.is_none() {
            if let Err(e) = self.init_gpu(event_loop) {
                eprintln!("brioche-render init error: {e}");
                event_loop.exit();
            }
        }
    }

    fn user_event(&mut self, event_loop: &ActiveEventLoop, event: ViewerWake) {
        match event {
            ViewerWake::FrameReady => {
                if let Some(gpu) = &self.gpu {
                    gpu.window.request_redraw();
                }
            }
            ViewerWake::CursorShape(shape) => {
                if let Some(gpu) = self.gpu.as_mut() {
                    gpu.renderer
                        .update_cursor_shape(shape.width, shape.height, &shape.rgba);
                    gpu.window.request_redraw();
                }
            }
            ViewerWake::CursorPos { x, y, visible } => {
                if let Some(gpu) = self.gpu.as_mut() {
                    gpu.renderer.update_cursor_position(x, y, visible);
                    gpu.window.request_redraw();
                }
            }
            ViewerWake::Title(t) => {
                self.title = t.clone();
                if let Some(gpu) = &self.gpu {
                    gpu.window.set_title(&t);
                }
            }
            ViewerWake::Close => event_loop.exit(),
        }
    }

    fn window_event(&mut self, event_loop: &ActiveEventLoop, _id: WindowId, event: WindowEvent) {
        match event {
            WindowEvent::CloseRequested => {
                self.send_input(InputAction::ReleaseAll);
                event_loop.exit();
            }
            WindowEvent::Resized(new_size) => {
                if let Some(gpu) = self.gpu.as_mut() {
                    gpu.config.width = new_size.width.max(1);
                    gpu.config.height = new_size.height.max(1);
                    gpu.surface.configure(&gpu.renderer.device, &gpu.config);
                    gpu.window.request_redraw();
                }
            }
            WindowEvent::RedrawRequested => self.redraw(),
            WindowEvent::ModifiersChanged(m) => self.modifiers = m.state(),
            WindowEvent::CursorMoved { position, .. } => {
                if !self.input_captured {
                    return;
                }
                if let Some(gpu) = &self.gpu {
                    let size = gpu.window.inner_size();
                    if size.width > 0 && size.height > 0 {
                        let x = (position.x as f32 / size.width as f32).clamp(0.0, 1.0);
                        let y = (position.y as f32 / size.height as f32).clamp(0.0, 1.0);
                        self.send_input(InputAction::Move { x, y });
                    }
                }
            }
            WindowEvent::MouseInput { state, button, .. } => {
                let down = state == ElementState::Pressed;
                if down && !self.input_captured {
                    self.input_captured = true;
                }
                if self.input_captured {
                    let name = match button {
                        MouseButton::Left => "left",
                        MouseButton::Right => "right",
                        MouseButton::Middle => "middle",
                        _ => return,
                    };
                    self.send_input(InputAction::Button {
                        button: name.to_string(),
                        down,
                    });
                }
            }
            WindowEvent::MouseWheel { delta, .. } => {
                if !self.input_captured {
                    return;
                }
                let (dx, dy) = match delta {
                    MouseScrollDelta::LineDelta(x, y) => (x.round() as i32, y.round() as i32),
                    MouseScrollDelta::PixelDelta(p) => {
                        ((p.x / 40.0).round() as i32, (p.y / 40.0).round() as i32)
                    }
                };
                if dx != 0 || dy != 0 {
                    self.send_input(InputAction::Scroll {
                        dx: dx.clamp(-10, 10),
                        dy: dy.clamp(-10, 10),
                    });
                }
            }
            WindowEvent::KeyboardInput { event, .. } => {
                let down = event.state == ElementState::Pressed;
                // F11 toggles borderless fullscreen
                if down && event.logical_key == Key::Named(NamedKey::F11) {
                    if let Some(gpu) = &self.gpu {
                        let next = if gpu.window.fullscreen().is_some() {
                            None
                        } else {
                            Some(Fullscreen::Borderless(None))
                        };
                        gpu.window.set_fullscreen(next);
                    }
                    return;
                }
                // Ctrl+Alt+Shift releases input capture
                if down
                    && self.modifiers.control_key()
                    && self.modifiers.alt_key()
                    && self.modifiers.shift_key()
                {
                    self.input_captured = false;
                    self.send_input(InputAction::ReleaseAll);
                    return;
                }
                // Ctrl+Alt+1..9 switches remote monitor atomically
                if down && self.modifiers.control_key() && self.modifiers.alt_key() {
                    if let Key::Character(ref ch) = event.logical_key {
                        if let Ok(mon) = ch.parse::<u32>() {
                            if (1..=9).contains(&mon) {
                                self.req_seq += 1;
                                let _ = self.control_tx.send(ControlMessage::SwitchMonitor {
                                    monitor_id: mon,
                                    request_id: self.req_seq,
                                });
                                return;
                            }
                        } else if ch.eq_ignore_ascii_case("s") {
                            self.sharpness = if self.sharpness > 0.1 { 0.0 } else { 0.45 };
                            if let Some(gpu) = self.gpu.as_mut() {
                                gpu.renderer.set_sharpness(self.sharpness);
                                gpu.window.request_redraw();
                            }
                            return;
                        }
                    }
                }
                if self.input_captured {
                    if let Some(key) = map_winit_key(&event.logical_key) {
                        self.send_input(InputAction::Key { key, down });
                    }
                }
            }
            WindowEvent::Focused(false) => {
                if self.input_captured {
                    self.input_captured = false;
                    self.send_input(InputAction::ReleaseAll);
                }
            }
            _ => {}
        }
    }
}

/// Launch the native GPU viewer window on the calling thread and invoke `on_ready(ViewerHandle)`.
pub fn run_viewer_window<F>(
    title: String,
    control_tx: std::sync::mpsc::Sender<ControlMessage>,
    on_ready: F,
) -> Result<(), RenderError>
where
    F: FnOnce(ViewerHandle),
{
    let event_loop = EventLoop::<ViewerWake>::with_user_event()
        .build()
        .map_err(|e| RenderError::Device(e.to_string()))?;
    let mailbox = Arc::new(Mutex::new(None));
    let handle = ViewerHandle {
        mailbox: mailbox.clone(),
        proxy: event_loop.create_proxy(),
    };
    on_ready(handle);

    let mut app = ViewerApp {
        title,
        mailbox,
        control_tx,
        gpu: None,
        modifiers: ModifiersState::empty(),
        input_captured: false,
        sharpness: 0.35,
        req_seq: 0,
    };
    event_loop
        .run_app(&mut app)
        .map_err(|e| RenderError::Device(e.to_string()))
}
