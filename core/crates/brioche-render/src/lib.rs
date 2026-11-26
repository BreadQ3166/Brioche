//! GPU-accelerated wgpu (Vulkan / Metal / D3D12) planar YUV420P & YUV444P renderer
//! with Contrast Adaptive Sharpening (CAS) and separated hardware cursor compositing.

pub mod window;

use bytemuck::{Pod, Zeroable};
use thiserror::Error;
use wgpu::util::DeviceExt;

#[derive(Debug, Error)]
pub enum RenderError {
    #[error("no suitable GPU adapter found")]
    NoAdapter,
    #[error("failed to request GPU device: {0}")]
    Device(String),
    #[error("invalid plane buffer size")]
    InvalidPlaneSize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChromaFormat {
    Yuv420p,
    Yuv444p,
}

#[repr(C)]
#[derive(Clone, Copy, Pod, Zeroable)]
struct Uniforms {
    video_size: [f32; 2],
    inv_video_size: [f32; 2],
    cursor_rect: [f32; 4],
    params: [f32; 4],
}

pub struct GpuVideoRenderer {
    pub adapter_name: String,
    pub backend_name: String,
    pub(crate) device: wgpu::Device,
    queue: wgpu::Queue,
    pipeline: wgpu::RenderPipeline,
    bind_group_layout: wgpu::BindGroupLayout,
    sampler: wgpu::Sampler,
    uniform_buf: wgpu::Buffer,
    width: u32,
    height: u32,
    chroma: ChromaFormat,
    tex_y: wgpu::Texture,
    tex_u: wgpu::Texture,
    tex_v: wgpu::Texture,
    tex_cursor: wgpu::Texture,
    cursor_w: u32,
    cursor_h: u32,
    bind_group: wgpu::BindGroup,
    sharpness: f32,
    full_range: bool,
    cursor_visible: bool,
    cursor_rect: [f32; 4],
}

impl GpuVideoRenderer {
    pub fn new(
        width: u32,
        height: u32,
        chroma: ChromaFormat,
        target_format: wgpu::TextureFormat,
    ) -> Result<Self, RenderError> {
        let instance = wgpu::Instance::new(wgpu::InstanceDescriptor {
            backends: wgpu::Backends::PRIMARY,
            ..Default::default()
        });
        let adapter = pollster::block_on(instance.request_adapter(&wgpu::RequestAdapterOptions {
            power_preference: wgpu::PowerPreference::HighPerformance,
            compatible_surface: None,
            force_fallback_adapter: false,
        }))
        .ok_or(RenderError::NoAdapter)?;

        let info = adapter.get_info();
        let adapter_name = info.name.clone();
        let backend_name = format!("{:?}", info.backend);

        let (device, queue) = pollster::block_on(adapter.request_device(
            &wgpu::DeviceDescriptor {
                label: Some("brioche-gpu"),
                required_features: wgpu::Features::empty(),
                required_limits: wgpu::Limits::downlevel_defaults(),
                memory_hints: wgpu::MemoryHints::Performance,
            },
            None,
        ))
        .map_err(|e| RenderError::Device(e.to_string()))?;

        Self::from_device(device, queue, adapter_name, backend_name, width, height, chroma, target_format)
    }

    pub fn from_device(
        device: wgpu::Device,
        queue: wgpu::Queue,
        adapter_name: String,
        backend_name: String,
        width: u32,
        height: u32,
        chroma: ChromaFormat,
        target_format: wgpu::TextureFormat,
    ) -> Result<Self, RenderError> {

        let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("brioche-yuv-cas-shader"),
            source: wgpu::ShaderSource::Wgsl(include_str!("video_shader.wgsl").into()),
        });

        let bind_group_layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("brioche-video-bgl"),
            entries: &[
                wgpu::BindGroupLayoutEntry {
                    binding: 0,
                    visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Buffer {
                        ty: wgpu::BufferBindingType::Uniform,
                        has_dynamic_offset: false,
                        min_binding_size: None,
                    },
                    count: None,
                },
                tex_entry(1),
                tex_entry(2),
                tex_entry(3),
                tex_entry(4),
                wgpu::BindGroupLayoutEntry {
                    binding: 5,
                    visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Sampler(wgpu::SamplerBindingType::Filtering),
                    count: None,
                },
            ],
        });

        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: Some("brioche-video-layout"),
            bind_group_layouts: &[&bind_group_layout],
            push_constant_ranges: &[],
        });

        let pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: Some("brioche-video-pipeline"),
            layout: Some(&pipeline_layout),
            vertex: wgpu::VertexState {
                module: &shader,
                entry_point: "vs_main",
                buffers: &[],
                compilation_options: Default::default(),
            },
            fragment: Some(wgpu::FragmentState {
                module: &shader,
                entry_point: "fs_main",
                targets: &[Some(wgpu::ColorTargetState {
                    format: target_format,
                    blend: Some(wgpu::BlendState::REPLACE),
                    write_mask: wgpu::ColorWrites::ALL,
                })],
                compilation_options: Default::default(),
            }),
            primitive: wgpu::PrimitiveState::default(),
            depth_stencil: None,
            multisample: wgpu::MultisampleState::default(),
            multiview: None,
            cache: None,
        });

        let sampler = device.create_sampler(&wgpu::SamplerDescriptor {
            label: Some("brioche-linear"),
            mag_filter: wgpu::FilterMode::Linear,
            min_filter: wgpu::FilterMode::Linear,
            ..Default::default()
        });

        let uniforms = Uniforms {
            video_size: [width as f32, height as f32],
            inv_video_size: [1.0 / width.max(1) as f32, 1.0 / height.max(1) as f32],
            cursor_rect: [0.0; 4],
            params: [0.35, 0.0, 1.0, 0.0],
        };
        let uniform_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("brioche-uniforms"),
            contents: bytemuck::bytes_of(&uniforms),
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
        });

        let (tex_y, tex_u, tex_v) = create_yuv_textures(&device, width, height, chroma);
        let tex_cursor = create_rgba_texture(&device, 1, 1);
        let bind_group = build_bind_group(
            &device,
            &bind_group_layout,
            &uniform_buf,
            &tex_y,
            &tex_u,
            &tex_v,
            &tex_cursor,
            &sampler,
        );

        Ok(Self {
            adapter_name,
            backend_name,
            device,
            queue,
            pipeline,
            bind_group_layout,
            sampler,
            uniform_buf,
            width,
            height,
            chroma,
            tex_y,
            tex_u,
            tex_v,
            tex_cursor,
            cursor_w: 1,
            cursor_h: 1,
            bind_group,
            sharpness: 0.35,
            full_range: true,
            cursor_visible: false,
            cursor_rect: [0.0; 4],
        })
    }

    pub fn set_sharpness(&mut self, sharpness: f32) {
        self.sharpness = sharpness.clamp(0.0, 1.0);
        self.write_uniforms();
    }

    pub fn set_full_range(&mut self, full_range: bool) {
        self.full_range = full_range;
        self.write_uniforms();
    }

    pub fn update_cursor_shape(&mut self, width: u32, height: u32, rgba: &[u8]) {
        if width == 0 || height == 0 || rgba.len() < (width * height * 4) as usize {
            return;
        }
        if width != self.cursor_w || height != self.cursor_h {
            self.tex_cursor = create_rgba_texture(&self.device, width, height);
            self.cursor_w = width;
            self.cursor_h = height;
            self.rebuild_bind_group();
        }
        self.queue.write_texture(
            wgpu::ImageCopyTexture {
                texture: &self.tex_cursor,
                mip_level: 0,
                origin: wgpu::Origin3d::ZERO,
                aspect: wgpu::TextureAspect::All,
            },
            rgba,
            wgpu::ImageDataLayout {
                offset: 0,
                bytes_per_row: Some(width * 4),
                rows_per_image: Some(height),
            },
            wgpu::Extent3d {
                width,
                height,
                depth_or_array_layers: 1,
            },
        );
    }

    pub fn update_cursor_position(&mut self, norm_x: f32, norm_y: f32, visible: bool) {
        self.cursor_visible = visible;
        self.cursor_rect = [
            norm_x,
            norm_y,
            self.cursor_w as f32 / self.width.max(1) as f32,
            self.cursor_h as f32 / self.height.max(1) as f32,
        ];
        self.write_uniforms();
    }

    /// Upload planar Y, U, V bytes directly to GPU textures (zero CPU RGB conversion).
    pub fn upload_yuv(
        &mut self,
        width: u32,
        height: u32,
        chroma: ChromaFormat,
        y: &[u8],
        u: &[u8],
        v: &[u8],
    ) -> Result<(), RenderError> {
        let (cw, ch) = chroma_dims(width, height, chroma);
        if y.len() < (width * height) as usize
            || u.len() < (cw * ch) as usize
            || v.len() < (cw * ch) as usize
        {
            return Err(RenderError::InvalidPlaneSize);
        }
        if width != self.width || height != self.height || chroma != self.chroma {
            self.width = width;
            self.height = height;
            self.chroma = chroma;
            let (ty, tu, tv) = create_yuv_textures(&self.device, width, height, chroma);
            self.tex_y = ty;
            self.tex_u = tu;
            self.tex_v = tv;
            self.rebuild_bind_group();
            self.write_uniforms();
        }
        write_r8_plane(&self.queue, &self.tex_y, width, height, y);
        write_r8_plane(&self.queue, &self.tex_u, cw, ch, u);
        write_r8_plane(&self.queue, &self.tex_v, cw, ch, v);
        Ok(())
    }

    /// Render the current GPU YUV frame + CAS sharpening + cursor onto `target_view`.
    pub fn render_to_view(&self, target_view: &wgpu::TextureView) {
        let mut encoder = self
            .device
            .create_command_encoder(&wgpu::CommandEncoderDescriptor {
                label: Some("brioche-render-enc"),
            });
        {
            let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: Some("brioche-video-pass"),
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view: target_view,
                    resolve_target: None,
                    ops: wgpu::Operations {
                        load: wgpu::LoadOp::Clear(wgpu::Color::BLACK),
                        store: wgpu::StoreOp::Store,
                    },
                })],
                depth_stencil_attachment: None,
                occlusion_query_set: None,
                timestamp_writes: None,
            });
            pass.set_pipeline(&self.pipeline);
            pass.set_bind_group(0, &self.bind_group, &[]);
            pass.draw(0..3, 0..1);
        }
        self.queue.submit(Some(encoder.finish()));
    }

    /// Offscreen render to RGBA8 readback (used for GPU shader verification & benchmarks).
    pub fn render_offscreen_rgba(&self, out_w: u32, out_h: u32) -> Vec<u8> {
        let tex = self.device.create_texture(&wgpu::TextureDescriptor {
            label: Some("brioche-offscreen"),
            size: wgpu::Extent3d {
                width: out_w,
                height: out_h,
                depth_or_array_layers: 1,
            },
            mip_level_count: 1,
            sample_count: 1,
            dimension: wgpu::TextureDimension::D2,
            format: wgpu::TextureFormat::Rgba8Unorm,
            usage: wgpu::TextureUsages::RENDER_ATTACHMENT | wgpu::TextureUsages::COPY_SRC,
            view_formats: &[],
        });
        let view = tex.create_view(&wgpu::TextureViewDescriptor::default());
        self.render_to_view(&view);

        let align = wgpu::COPY_BYTES_PER_ROW_ALIGNMENT;
        let unpadded = out_w * 4;
        let padded = ((unpadded + align - 1) / align) * align;
        let readback = self.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("brioche-readback"),
            size: (padded * out_h) as u64,
            usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
            mapped_at_creation: false,
        });

        let mut enc = self
            .device
            .create_command_encoder(&wgpu::CommandEncoderDescriptor::default());
        enc.copy_texture_to_buffer(
            wgpu::ImageCopyTexture {
                texture: &tex,
                mip_level: 0,
                origin: wgpu::Origin3d::ZERO,
                aspect: wgpu::TextureAspect::All,
            },
            wgpu::ImageCopyBuffer {
                buffer: &readback,
                layout: wgpu::ImageDataLayout {
                    offset: 0,
                    bytes_per_row: Some(padded),
                    rows_per_image: Some(out_h),
                },
            },
            wgpu::Extent3d {
                width: out_w,
                height: out_h,
                depth_or_array_layers: 1,
            },
        );
        self.queue.submit(Some(enc.finish()));

        let slice = readback.slice(..);
        let (tx, rx) = std::sync::mpsc::channel();
        slice.map_async(wgpu::MapMode::Read, move |r| {
            let _ = tx.send(r);
        });
        self.device.poll(wgpu::Maintain::Wait);
        rx.recv().unwrap().unwrap();
        let data = slice.get_mapped_range();
        let mut out = Vec::with_capacity((unpadded * out_h) as usize);
        for row in 0..out_h as usize {
            let start = row * padded as usize;
            out.extend_from_slice(&data[start..start + unpadded as usize]);
        }
        drop(data);
        readback.unmap();
        out
    }

    fn write_uniforms(&self) {
        let uniforms = Uniforms {
            video_size: [self.width as f32, self.height as f32],
            inv_video_size: [
                1.0 / self.width.max(1) as f32,
                1.0 / self.height.max(1) as f32,
            ],
            cursor_rect: self.cursor_rect,
            params: [
                self.sharpness,
                if self.cursor_visible { 1.0 } else { 0.0 },
                if self.full_range { 1.0 } else { 0.0 },
                0.0,
            ],
        };
        self.queue
            .write_buffer(&self.uniform_buf, 0, bytemuck::bytes_of(&uniforms));
    }

    fn rebuild_bind_group(&mut self) {
        self.bind_group = build_bind_group(
            &self.device,
            &self.bind_group_layout,
            &self.uniform_buf,
            &self.tex_y,
            &self.tex_u,
            &self.tex_v,
            &self.tex_cursor,
            &self.sampler,
        );
    }
}

fn tex_entry(binding: u32) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry {
        binding,
        visibility: wgpu::ShaderStages::FRAGMENT,
        ty: wgpu::BindingType::Texture {
            multisampled: false,
            view_dimension: wgpu::TextureViewDimension::D2,
            sample_type: wgpu::TextureSampleType::Float { filterable: true },
        },
        count: None,
    }
}

fn chroma_dims(width: u32, height: u32, chroma: ChromaFormat) -> (u32, u32) {
    match chroma {
        ChromaFormat::Yuv420p => ((width + 1) / 2, (height + 1) / 2),
        ChromaFormat::Yuv444p => (width, height),
    }
}

fn create_r8_texture(device: &wgpu::Device, width: u32, height: u32) -> wgpu::Texture {
    device.create_texture(&wgpu::TextureDescriptor {
        label: Some("brioche-r8"),
        size: wgpu::Extent3d {
            width: width.max(1),
            height: height.max(1),
            depth_or_array_layers: 1,
        },
        mip_level_count: 1,
        sample_count: 1,
        dimension: wgpu::TextureDimension::D2,
        format: wgpu::TextureFormat::R8Unorm,
        usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
        view_formats: &[],
    })
}

fn create_rgba_texture(device: &wgpu::Device, width: u32, height: u32) -> wgpu::Texture {
    device.create_texture(&wgpu::TextureDescriptor {
        label: Some("brioche-cursor"),
        size: wgpu::Extent3d {
            width: width.max(1),
            height: height.max(1),
            depth_or_array_layers: 1,
        },
        mip_level_count: 1,
        sample_count: 1,
        dimension: wgpu::TextureDimension::D2,
        format: wgpu::TextureFormat::Rgba8Unorm,
        usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
        view_formats: &[],
    })
}

fn create_yuv_textures(
    device: &wgpu::Device,
    width: u32,
    height: u32,
    chroma: ChromaFormat,
) -> (wgpu::Texture, wgpu::Texture, wgpu::Texture) {
    let (cw, ch) = chroma_dims(width, height, chroma);
    (
        create_r8_texture(device, width, height),
        create_r8_texture(device, cw, ch),
        create_r8_texture(device, cw, ch),
    )
}

fn write_r8_plane(queue: &wgpu::Queue, tex: &wgpu::Texture, width: u32, height: u32, data: &[u8]) {
    queue.write_texture(
        wgpu::ImageCopyTexture {
            texture: tex,
            mip_level: 0,
            origin: wgpu::Origin3d::ZERO,
            aspect: wgpu::TextureAspect::All,
        },
        data,
        wgpu::ImageDataLayout {
            offset: 0,
            bytes_per_row: Some(width),
            rows_per_image: Some(height),
        },
        wgpu::Extent3d {
            width,
            height,
            depth_or_array_layers: 1,
        },
    );
}

#[allow(clippy::too_many_arguments)]
fn build_bind_group(
    device: &wgpu::Device,
    layout: &wgpu::BindGroupLayout,
    uniform_buf: &wgpu::Buffer,
    tex_y: &wgpu::Texture,
    tex_u: &wgpu::Texture,
    tex_v: &wgpu::Texture,
    tex_cursor: &wgpu::Texture,
    sampler: &wgpu::Sampler,
) -> wgpu::BindGroup {
    let vy = tex_y.create_view(&wgpu::TextureViewDescriptor::default());
    let vu = tex_u.create_view(&wgpu::TextureViewDescriptor::default());
    let vv = tex_v.create_view(&wgpu::TextureViewDescriptor::default());
    let vc = tex_cursor.create_view(&wgpu::TextureViewDescriptor::default());
    device.create_bind_group(&wgpu::BindGroupDescriptor {
        label: Some("brioche-video-bg"),
        layout,
        entries: &[
            wgpu::BindGroupEntry {
                binding: 0,
                resource: uniform_buf.as_entire_binding(),
            },
            wgpu::BindGroupEntry {
                binding: 1,
                resource: wgpu::BindingResource::TextureView(&vy),
            },
            wgpu::BindGroupEntry {
                binding: 2,
                resource: wgpu::BindingResource::TextureView(&vu),
            },
            wgpu::BindGroupEntry {
                binding: 3,
                resource: wgpu::BindingResource::TextureView(&vv),
            },
            wgpu::BindGroupEntry {
                binding: 4,
                resource: wgpu::BindingResource::TextureView(&vc),
            },
            wgpu::BindGroupEntry {
                binding: 5,
                resource: wgpu::BindingResource::Sampler(sampler),
            },
        ],
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gpu_yuv444_shader_and_hardware_cursor_compositing() {
        let mut renderer =
            match GpuVideoRenderer::new(64, 64, ChromaFormat::Yuv444p, wgpu::TextureFormat::Rgba8Unorm) {
                Ok(r) => r,
                Err(RenderError::NoAdapter) => return, // Skip cleanly on headless CI without GPU
                Err(e) => panic!("unexpected GPU init error: {e}"),
            };
        renderer.set_full_range(true);
        renderer.set_sharpness(0.0);

        // Pure neutral gray in full-range YUV444: Y=128, U=128, V=128 -> RGB ~(128, 128, 128)
        let y = vec![128u8; 64 * 64];
        let u = vec![128u8; 64 * 64];
        let v = vec![128u8; 64 * 64];
        renderer.upload_yuv(64, 64, ChromaFormat::Yuv444p, &y, &u, &v).unwrap();

        // Red opaque 8x8 hardware cursor placed at top-left (0, 0)
        let mut cursor = vec![0u8; 8 * 8 * 4];
        for px in cursor.chunks_exact_mut(4) {
            px[0] = 255;
            px[1] = 0;
            px[2] = 0;
            px[3] = 255;
        }
        renderer.update_cursor_shape(8, 8, &cursor);
        renderer.update_cursor_position(0.0, 0.0, true);

        let rgba = renderer.render_offscreen_rgba(64, 64);
        // Pixel (2, 2) must be red hardware cursor
        let p_cur = (2 * 64 + 2) * 4;
        assert!(rgba[p_cur] > 230 && rgba[p_cur + 1] < 25 && rgba[p_cur + 2] < 25);

        // Pixel (40, 40) must be neutral gray video background
        let p_bg = (40 * 64 + 40) * 4;
        assert!((rgba[p_bg] as i32 - 128).abs() <= 4);
        assert!((rgba[p_bg + 1] as i32 - 128).abs() <= 4);
        assert!((rgba[p_bg + 2] as i32 - 128).abs() <= 4);
    }
}
