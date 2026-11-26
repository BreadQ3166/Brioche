//! brioche-core: Next-gen Rust low-latency streaming engine CLI, hardware probe & E2E benchmark.

use std::env;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use brioche_codec::{
    bgra_to_yuv444p_bt709, encode_single_bgra_frame, probe_encoders, EncodeProfile, PixelChroma,
    VideoCodec,
};
use brioche_proto::{FLAG_HEVC, FLAG_IDR, FLAG_YUV444, PROTO_VERSION};
use brioche_render::{ChromaFormat, GpuVideoRenderer};
use brioche_transport::udp::{bind_tuned_udp, now_unix_us, UdpMediaReceiver, UdpMediaSender};
use serde::Serialize;

#[derive(Serialize)]
struct BenchResult {
    frames_sent: usize,
    frames_received: usize,
    frames_fec_recovered: u64,
    frames_dropped: u64,
    datagrams_sent: usize,
    datagrams_simulated_dropped: usize,
    fec_percent: u8,
    frame_kb: usize,
    throughput_mbps: f64,
    latency_p50_ms: f64,
    latency_p95_ms: f64,
    latency_p99_ms: f64,
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = env::args().collect();
    match args.get(1).map(|s| s.as_str()) {
        Some("bench-udp") => run_bench(&args[2..]).await,
        Some("bench-pipeline") => run_pipeline_bench().await,
        _ => run_info(),
    }
}

fn run_info() {
    let gpu = match GpuVideoRenderer::new(
        64,
        64,
        ChromaFormat::Yuv444p,
        wgpu::TextureFormat::Rgba8Unorm,
    ) {
        Ok(r) => serde_json::json!({
            "available": true,
            "adapter": r.adapter_name,
            "backend": r.backend_name,
            "shader": "wgsl-yuv420p-yuv444p-cas-cursor",
        }),
        Err(e) => serde_json::json!({
            "available": false,
            "reason": e.to_string(),
        }),
    };

    let report = serde_json::json!({
        "name": "brioche-core",
        "version": env!("CARGO_PKG_VERSION"),
        "protocol_version": PROTO_VERSION,
        "os": env::consts::OS,
        "arch": env::consts::ARCH,
        "transports": ["brioche-udp-v3", "quic-pinned-tls13", "fec-gf256-cauchy", "nack-pli-twcc-abr"],
        "cipher": "chacha20-poly1305",
        "capture": brioche_capture::probe_capabilities(),
        "encoders": probe_encoders(),
        "gpu_renderer": gpu,
    });
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
}

async fn run_pipeline_bench() {
    let caps = probe_encoders();
    let mut capture_ms = 0.0;
    let mut cursor_info = serde_json::json!(null);
    let (src_w, src_h, bgra) = {
        #[cfg(target_os = "linux")]
        {
            match brioche_capture::linux_x11::X11ShmCapturer::open(1) {
                Ok(mut cap) => {
                    let frame = cap.grab_frame().expect("MIT-SHM grab failed");
                    capture_ms = frame.capture_us as f64 / 1000.0;
                    if let Ok((cur, changed)) = cap.grab_cursor() {
                        cursor_info = serde_json::json!({
                            "x": cur.x,
                            "y": cur.y,
                            "size": [cur.width, cur.height],
                            "shape_changed": changed,
                            "hash_prefix": &cur.shape_hash[..12],
                        });
                    }
                    (frame.width, frame.height, frame.bgra)
                }
                Err(_) => (1920, 1080, vec![0x55u8; 1920 * 1080 * 4]),
            }
        }
        #[cfg(not(target_os = "linux"))]
        {
            (1920u32, 1080u32, vec![0x55u8; 1920 * 1080 * 4])
        }
    };

    // Hardware encode real captured frame
    let (encoded_bytes, encoder_used, encode_ms, chroma_444) = if let Some(ref c) = caps {
        let ffmpeg = PathBuf::from(&c.ffmpeg_path);
        let dst_w = src_w.min(2560) / 2 * 2;
        let dst_h = src_h.min(1440) / 2 * 2;
        let profile = EncodeProfile {
            src_width: src_w,
            src_height: src_h,
            dst_width: dst_w,
            dst_height: dst_h,
            fps: 60,
            bitrate_kbps: 20_000,
            codec: if c.hevc_nvenc {
                VideoCodec::Hevc
            } else {
                VideoCodec::H264
            },
            chroma: if c.h264_nvenc_444 {
                PixelChroma::Yuv444p
            } else {
                PixelChroma::Yuv420p
            },
        };
        let t0 = Instant::now();
        let (bytes, enc) = encode_single_bgra_frame(&ffmpeg, &bgra, &profile, c.h264_nvenc)
            .unwrap_or_else(|_| (vec![0xABu8; 48 * 1024], "synthetic"));
        (
            bytes,
            enc,
            t0.elapsed().as_secs_f64() * 1000.0,
            c.h264_nvenc_444,
        )
    } else {
        (vec![0xABu8; 48 * 1024], "synthetic", 0.0, false)
    };

    // Transmit encoded frame over encrypted Brioche-UDP + 15% RS-FEC with 5% loss
    let key = [0x99u8; 32];
    let rx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();
    let rx_addr = rx_sock.local_addr().unwrap();
    let tx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();
    let mut sender = UdpMediaSender::new(tx_sock, rx_addr, &key, 1);
    let mut receiver = UdpMediaReceiver::new(rx_sock, &key, 1);

    let t_net = Instant::now();
    let rx_task = tokio::spawn(async move { receiver.recv_frame().await.unwrap() });
    let flags = FLAG_IDR | if chroma_444 { FLAG_YUV444 } else { 0 };
    let (_fid, dgrams_sent, dgrams_dropped) = sender
        .send_video_frame(&encoded_bytes, flags, 15, Some(20))
        .await
        .unwrap();
    let rx_frame = rx_task.await.unwrap();
    let transport_ms = t_net.elapsed().as_secs_f64() * 1000.0;
    assert_eq!(rx_frame.payload, encoded_bytes);

    // Upload planar YUV444 frame to real wgpu Vulkan/Metal/D3D12 shader + render
    let gpu_w = 1280u32;
    let gpu_h = 720u32;
    let sample_bgra = vec![0x70u8; (gpu_w * gpu_h * 4) as usize];
    let (y, u, v) = bgra_to_yuv444p_bt709(&sample_bgra, gpu_w, gpu_h);
    let gpu_report = match GpuVideoRenderer::new(
        gpu_w,
        gpu_h,
        ChromaFormat::Yuv444p,
        wgpu::TextureFormat::Rgba8Unorm,
    ) {
        Ok(mut renderer) => {
            renderer.set_sharpness(0.35);
            let t_gpu = Instant::now();
            renderer
                .upload_yuv(gpu_w, gpu_h, ChromaFormat::Yuv444p, &y, &u, &v)
                .unwrap();
            let _rgba = renderer.render_offscreen_rgba(gpu_w, gpu_h);
            let gpu_ms = t_gpu.elapsed().as_secs_f64() * 1000.0;
            serde_json::json!({
                "adapter": renderer.adapter_name,
                "backend": renderer.backend_name,
                "upload_and_shader_render_ms": (gpu_ms * 1000.0).round() / 1000.0,
            })
        }
        Err(e) => serde_json::json!({ "error": e.to_string() }),
    };

    let summary = serde_json::json!({
        "source_resolution": [src_w, src_h],
        "capture_mit_shm_ms": (capture_ms * 1000.0).round() / 1000.0,
        "hardware_cursor_separated": cursor_info,
        "encoder": encoder_used,
        "chroma_444": chroma_444,
        "encoded_frame_bytes": encoded_bytes.len(),
        "single_shot_encode_ms": (encode_ms * 1000.0).round() / 1000.0,
        "udp_aead_fec_ms": (transport_ms * 1000.0).round() / 1000.0,
        "udp_datagrams_sent": dgrams_sent,
        "udp_datagrams_dropped_5pct": dgrams_dropped,
        "fec_recovered": rx_frame.recovered_via_fec,
        "wgpu_render": gpu_report,
    });
    println!("{}", serde_json::to_string_pretty(&summary).unwrap());
}

async fn run_bench(args: &[String]) {
    let mut frames: usize = 120;
    let mut frame_kb: usize = 48;
    let mut fec_percent: u8 = 15;
    let mut drop_every: Option<usize> = Some(20);

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--frames" if i + 1 < args.len() => {
                frames = args[i + 1].parse().unwrap_or(frames);
                i += 2;
            }
            "--frame-kb" if i + 1 < args.len() => {
                frame_kb = args[i + 1].parse().unwrap_or(frame_kb);
                i += 2;
            }
            "--fec" if i + 1 < args.len() => {
                fec_percent = args[i + 1].parse().unwrap_or(fec_percent);
                i += 2;
            }
            "--drop-every" if i + 1 < args.len() => {
                let v: usize = args[i + 1].parse().unwrap_or(0);
                drop_every = if v == 0 { None } else { Some(v) };
                i += 2;
            }
            _ => i += 1,
        }
    }

    let key = [0x7Bu8; 32];
    let rx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();
    let rx_addr = rx_sock.local_addr().unwrap();
    let tx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();

    let mut sender = UdpMediaSender::new(tx_sock, rx_addr, &key, 1);
    let mut receiver = UdpMediaReceiver::new(rx_sock, &key, 1);

    let payload: Vec<u8> = (0..frame_kb * 1024).map(|n| ((n * 31) & 0xFF) as u8).collect();
    let recv_handle = tokio::spawn(async move {
        let mut latencies_us = Vec::with_capacity(frames);
        for _ in 0..frames {
            match tokio::time::timeout(Duration::from_millis(500), receiver.recv_frame()).await {
                Ok(Ok(frame)) => {
                    let now = now_unix_us();
                    latencies_us.push(now.saturating_sub(frame.capture_ts_us));
                }
                _ => break,
            }
        }
        (latencies_us, receiver.stats())
    });

    let started = Instant::now();
    let mut datagrams_sent = 0usize;
    let mut datagrams_dropped = 0usize;
    for idx in 0..frames {
        let flags = if idx == 0 {
            FLAG_IDR | FLAG_HEVC | FLAG_YUV444
        } else {
            FLAG_HEVC | FLAG_YUV444
        };
        let (_fid, s, d) = sender
            .send_video_frame(&payload, flags, fec_percent, drop_every)
            .await
            .unwrap();
        datagrams_sent += s;
        datagrams_dropped += d;
        tokio::time::sleep(Duration::from_micros(500)).await;
    }

    let (mut latencies_us, (_ok, fec_rec, dropped)) = recv_handle.await.unwrap();
    let elapsed_s = started.elapsed().as_secs_f64().max(1e-6);
    latencies_us.sort_unstable();
    let pct = |p: f64| -> f64 {
        if latencies_us.is_empty() {
            return 0.0;
        }
        let idx = ((latencies_us.len() - 1) as f64 * p).round() as usize;
        latencies_us[idx] as f64 / 1000.0
    };
    let total_bits = (latencies_us.len() * frame_kb * 1024 * 8) as f64;

    let report = BenchResult {
        frames_sent: frames,
        frames_received: latencies_us.len(),
        frames_fec_recovered: fec_rec,
        frames_dropped: dropped,
        datagrams_sent,
        datagrams_simulated_dropped: datagrams_dropped,
        fec_percent,
        frame_kb,
        throughput_mbps: (total_bits / elapsed_s / 1e6 * 100.0).round() / 100.0,
        latency_p50_ms: (pct(0.50) * 1000.0).round() / 1000.0,
        latency_p95_ms: (pct(0.95) * 1000.0).round() / 1000.0,
        latency_p99_ms: (pct(0.99) * 1000.0).round() / 1000.0,
    };
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
}
