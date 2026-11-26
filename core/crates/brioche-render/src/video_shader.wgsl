// Brioche v2.0 GPU Video & Hardware Cursor Shader (WGSL -> Vulkan / Metal / D3D12)
//
// Features:
// 1. Direct GPU planar YUV420P / YUV444P -> BT.709 RGB conversion (zero CPU color conversion)
// 2. Contrast Adaptive Sharpening (CAS) for razor-sharp 4K/2K text and IDE UI
// 3. Hardware-separated RGBA cursor compositing at 0ms local latency

struct Uniforms {
    video_size: vec2<f32>,
    inv_video_size: vec2<f32>,
    cursor_rect: vec4<f32>,   // (x, y, w, h) in normalized [0..1] UV space
    params: vec4<f32>,        // x = sharpness (0..1), y = cursor_visible (0 or 1), z = full_range (0=limited, 1=full), w = reserved
};

@group(0) @binding(0) var<uniform> u: Uniforms;
@group(0) @binding(1) var tex_y: texture_2d<f32>;
@group(0) @binding(2) var tex_u: texture_2d<f32>;
@group(0) @binding(3) var tex_v: texture_2d<f32>;
@group(0) @binding(4) var tex_cursor: texture_2d<f32>;
@group(0) @binding(5) var samp_linear: sampler;

struct VsOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) uv: vec2<f32>,
};

@vertex
fn vs_main(@builtin(vertex_index) vid: u32) -> VsOut {
    // Fullscreen triangle (no vertex buffer needed)
    var positions = array<vec2<f32>, 3>(
        vec2<f32>(-1.0, -3.0),
        vec2<f32>(-1.0,  1.0),
        vec2<f32>( 3.0,  1.0)
    );
    var uvs = array<vec2<f32>, 3>(
        vec2<f32>(0.0, 2.0),
        vec2<f32>(0.0, 0.0),
        vec2<f32>(2.0, 0.0)
    );
    var out: VsOut;
    out.pos = vec4<f32>(positions[vid], 0.0, 1.0);
    out.uv = uvs[vid];
    return out;
}

fn sample_yuv_to_rgb(uv: vec2<f32>) -> vec3<f32> {
    let y_raw = textureSampleLevel(tex_y, samp_linear, uv, 0.0).r;
    let u_raw = textureSampleLevel(tex_u, samp_linear, uv, 0.0).r;
    let v_raw = textureSampleLevel(tex_v, samp_linear, uv, 0.0).r;

    var y = y_raw;
    var cb = u_raw - 0.5;
    var cr = v_raw - 0.5;
    if (u.params.z < 0.5) {
        // BT.709 limited range (16..235 / 16..240)
        y = (y_raw - 16.0 / 255.0) * (255.0 / 219.0);
        cb = (u_raw - 128.0 / 255.0) * (255.0 / 224.0);
        cr = (v_raw - 128.0 / 255.0) * (255.0 / 224.0);
    }

    let r = y + 1.5748 * cr;
    let g = y - 0.187324 * cb - 0.468124 * cr;
    let b = y + 1.8556 * cb;
    return clamp(vec3<f32>(r, g, b), vec3<f32>(0.0), vec3<f32>(1.0));
}

@fragment
fn fs_main(in: VsOut) -> @location(0) vec4<f32> {
    let c = sample_yuv_to_rgb(in.uv);
    var rgb = c;

    // Lightweight Contrast Adaptive Sharpening (CAS) for crisp text
    let sharp = clamp(u.params.x, 0.0, 1.0);
    if (sharp > 0.001) {
        let dx = vec2<f32>(u.inv_video_size.x, 0.0);
        let dy = vec2<f32>(0.0, u.inv_video_size.y);
        let n = sample_yuv_to_rgb(in.uv - dy);
        let s = sample_yuv_to_rgb(in.uv + dy);
        let w = sample_yuv_to_rgb(in.uv - dx);
        let e = sample_yuv_to_rgb(in.uv + dx);
        let mn = min(min(min(n, s), min(w, e)), c);
        let mx = max(max(max(n, s), max(w, e)), c);
        let amp = clamp(min(mn, vec3<f32>(1.0) - mx) / max(mx, vec3<f32>(0.001)), vec3<f32>(0.0), vec3<f32>(1.0));
        let weight = sqrt(amp) * (-0.125 * sharp);
        rgb = clamp((c + weight * (n + s + w + e)) / (vec3<f32>(1.0) + 4.0 * weight), vec3<f32>(0.0), vec3<f32>(1.0));
    }

    // Hardware-separated RGBA cursor compositing
    if (u.params.y > 0.5 && u.cursor_rect.z > 0.0 && u.cursor_rect.w > 0.0) {
        let cur_uv = (in.uv - u.cursor_rect.xy) / u.cursor_rect.zw;
        if (cur_uv.x >= 0.0 && cur_uv.x <= 1.0 && cur_uv.y >= 0.0 && cur_uv.y <= 1.0) {
            let cur = textureSampleLevel(tex_cursor, samp_linear, cur_uv, 0.0);
            rgb = mix(rgb, cur.rgb, cur.a);
        }
    }

    return vec4<f32>(rgb, 1.0);
}
