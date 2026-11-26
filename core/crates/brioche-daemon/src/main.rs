//! brioche-core: Next-gen Rust low-latency streaming engine CLI, host, GPU viewer & benchmarks.
//!
//! Subcommands:
//!   info           - Probe GPU (Vulkan/Metal/DX12), hardware encoders, capture & monitors
//!   host           - Start live Brioche v3 QUIC + encrypted UDP/RS-FEC host daemon
//!   view           - Connect to a Brioche v3 host with native winit + wgpu GPU viewer
//!   bench-udp      - Encrypted UDP + GF(256) RS-FEC throughput & latency benchmark
//!   bench-stream   - Persistent NVENC/HEVC-444 -> UDP+FEC (5% loss) -> Decoder -> Vulkan wgpu 60-frame E2E test

use std::env;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use brioche_codec::{
    probe_encoders, EncodeProfile, PixelChroma, StreamingDecoder, StreamingEncoder, VideoCodec,
};
use brioche_input::{create_injector, MonitorRect};
use brioche_proto::{
    ControlMessage, MonitorDesc, FLAG_HEVC, FLAG_IDR, FLAG_YUV444, PROTO_VERSION,
};
use brioche_render::window::{run_viewer_window, PendingYuvFrame};
use brioche_render::{ChromaFormat, GpuVideoRenderer};
use brioche_transport::quic::{
    bind_quic_server, connect_quic_client, generate_self_signed_identity, recv_control, send_control,
    QuicConnection,
};
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

fn main() {
    let args: Vec<String> = env::args().collect();
    match args.get(1).map(|s| s.as_str()) {
        Some("view") => run_view_cli(&args[2..]),
        Some("host") => {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .unwrap();
            rt.block_on(run_host_cli(&args[2..]));
        }
        Some("bench-udp") => {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .unwrap();
            rt.block_on(run_bench_udp(&args[2..]));
        }
        Some("bench-stream") => {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .unwrap();
            rt.block_on(run_bench_stream(&args[2..]));
        }
        _ => run_info(),
    }
}

fn arg_val<'a>(args: &'a [String], key: &str) -> Option<&'a str> {
    args.windows(2)
        .find(|w| w[0] == key)
        .map(|w| w[1].as_str())
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

/// Live Brioche v3 Host: QUIC pinned control + MIT-SHM/XFixes capture + persistent NVENC + Brioche-UDP/FEC.
async fn run_host_cli(args: &[String]) {
    let bind_ip = arg_val(args, "--bind").unwrap_or("127.0.0.1");
    let quic_port: u16 = arg_val(args, "--quic-port")
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let monitor_id: u32 = arg_val(args, "--monitor")
        .and_then(|s| s.parse().ok())
        .unwrap_or(1);
    let view_only = args.iter().any(|a| a == "--view-only");

    let key: [u8; 32] = if let Some(khex) = arg_val(args, "--key") {
        let raw = hex::decode(khex).expect("invalid --key hex");
        raw.try_into().expect("--key must be 32 bytes hex")
    } else {
        // Ephemeral 256-bit key derived from system time + pid + address entropy
        use std::collections::hash_map::DefaultHasher;
        use std::hash::{Hash, Hasher};
        let mut out = [0u8; 32];
        for (i, chunk) in out.chunks_exact_mut(8).enumerate() {
            let mut h = DefaultHasher::new();
            now_unix_us().hash(&mut h);
            std::process::id().hash(&mut h);
            i.hash(&mut h);
            chunk.copy_from_slice(&h.finish().to_be_bytes());
        }
        out
    };

    let identity = generate_self_signed_identity().expect("QUIC cert generation failed");
    let quic_addr: SocketAddr = format!("{bind_ip}:{quic_port}").parse().unwrap();
    let (quic_ep, bound_quic) = bind_quic_server(quic_addr, &identity).expect("bind QUIC failed");

    // Emit single-line machine-readable JSON for PySide6 supervisor
    let ready = serde_json::json!({
        "ready": true,
        "quic_port": bound_quic.port(),
        "pin": identity.sha256_pin,
        "key": hex::encode(key),
        "monitor": monitor_id,
    });
    println!("{}", ready);

    let mut active_stop: Option<Arc<AtomicBool>> = None;
    let mut active_task: Option<tokio::task::JoinHandle<()>> = None;

    while let Some(incoming) = quic_ep.accept().await {
        let Ok(conn) = incoming.await else {
            continue;
        };
        // Immediately release any previous stale session before starting the new one.
        if let Some(prev_stop) = active_stop.take() {
            prev_stop.store(true, Ordering::Release);
        }
        if let Some(prev_task) = active_task.take() {
            let _ = tokio::time::timeout(Duration::from_millis(800), prev_task).await;
        }
        let session_stop = Arc::new(AtomicBool::new(false));
        active_stop = Some(session_stop.clone());
        active_task = Some(tokio::spawn(async move {
            run_host_session(conn, key, monitor_id, view_only, session_stop).await;
        }));
    }
}

async fn run_host_session(
    conn: QuicConnection,
    key: [u8; 32],
    monitor_id: u32,
    view_only: bool,
    stop_flag: Arc<AtomicBool>,
) {
    let peer_ip = conn.remote_address().ip();
    let (mut ctrl_send, mut ctrl_recv) = match conn.accept_bi().await {
        Ok(pair) => pair,
        Err(_) => return,
    };
    let Ok(ControlMessage::SessionHello {
        udp_port,
        requested_codec,
        chroma_444,
        fps,
        bitrate_kbps,
    }) = recv_control(&mut ctrl_recv).await
    else {
        return;
    };

    let Some(caps) = probe_encoders() else {
        eprintln!("no ffmpeg encoder found");
        return;
    };
    let ffmpeg = PathBuf::from(&caps.ffmpeg_path);
    let use_nvenc = caps.hevc_nvenc || caps.h264_nvenc;
    let codec = if requested_codec == "hevc" && caps.hevc_nvenc {
        VideoCodec::Hevc
    } else {
        VideoCodec::H264
    };
    let chroma = if chroma_444 && (caps.hevc_nvenc_444 || caps.h264_nvenc_444) {
        PixelChroma::Yuv444p
    } else {
        PixelChroma::Yuv420p
    };

    let monitors = brioche_capture::list_monitors().unwrap_or_default();
    let monitor_descs: Vec<MonitorDesc> = monitors
        .iter()
        .map(|m| MonitorDesc {
            id: m.id,
            name: m.name.clone(),
            width: m.width,
            height: m.height,
            primary: m.primary,
        })
        .collect();

    let requested_mon = Arc::new(AtomicU32::new(monitor_id));
    let peer_udp = SocketAddr::new(peer_ip, udp_port);
    let Ok(udp_sock) = bind_tuned_udp("0.0.0.0:0".parse().unwrap()) else {
        eprintln!("udp bind failed");
        return;
    };
    let host_udp_port = udp_sock.local_addr().map(|a| a.port()).unwrap_or(0);
    let mut sender = UdpMediaSender::new(udp_sock, peer_udp, &key, 1);

    let mut cap = match brioche_capture::open_capturer(monitor_id) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("capture open failed: {e}");
            return;
        }
    };
    let mut current_mon_id = monitor_id;
    let mut stream_id: u16 = 1;
    let mut mon = cap.monitor().clone();

    let dst_w = mon.width.min(2560) / 2 * 2;
    let dst_h = mon.height.min(1440) / 2 * 2;
    if send_control(
        &mut ctrl_send,
        &ControlMessage::StreamDescriptor {
            stream_id,
            monitor_id: mon.id,
            width: dst_w,
            height: dst_h,
            fps_limit: fps.clamp(30, 144),
            codec: if matches!(codec, VideoCodec::Hevc) {
                "hevc".into()
            } else {
                "h264".into()
            },
            chroma_444: matches!(chroma, PixelChroma::Yuv444p),
            monitors: monitor_descs.clone(),
            host_udp_port,
        },
    )
    .await
    .is_err()
    {
        return;
    }

    let injector = Arc::new(std::sync::Mutex::new(create_injector(
        MonitorRect {
            left: mon.left,
            top: mon.top,
            width: mon.width,
            height: mon.height,
        },
        view_only,
    )));
    let injector_rx = injector.clone();
    let stop_rx = stop_flag.clone();
    let requested_mon_rx = requested_mon.clone();
    let input_task = tokio::spawn(async move {
        while let Ok(msg) = recv_control(&mut ctrl_recv).await {
            match msg {
                ControlMessage::InputEvent { event } => {
                    if let Ok(mut inj) = injector_rx.lock() {
                        let _ = inj.apply(&event);
                    }
                }
                ControlMessage::SwitchMonitor { monitor_id, .. } => {
                    requested_mon_rx.store(monitor_id, Ordering::Release);
                }
                _ => {}
            }
        }
        if let Ok(mut inj) = injector_rx.lock() {
            inj.release_all();
        }
        stop_rx.store(true, Ordering::Release);
    });

    let mut profile = EncodeProfile {
        src_width: mon.width,
        src_height: mon.height,
        dst_width: dst_w,
        dst_height: dst_h,
        fps: fps.clamp(30, 144),
        bitrate_kbps: bitrate_kbps.clamp(2_000, 80_000),
        codec,
        chroma,
    };
    let mut encoder = match StreamingEncoder::start(&ffmpeg, &profile, use_nvenc) {
        Ok(e) => e,
        Err(e) => {
            eprintln!("encoder start failed: {e}");
            input_task.abort();
            return;
        }
    };

    let frame_interval = Duration::from_secs_f64(1.0 / profile.fps as f64);
    let mut last_sent = Instant::now();
    let mut idle_streak = 0u32;
    let mut last_cursor_pos: Option<(i32, i32, bool)> = None;

    while !stop_flag.load(Ordering::Acquire) {
        let tick = Instant::now();
        let _ = sender.poll_feedback().await;

        // Atomic live monitor switch (zero window teardown / zero macOS crash)
        let wanted_mon = requested_mon.load(Ordering::Acquire);
        if wanted_mon != current_mon_id && wanted_mon >= 1 {
            if let Ok(new_cap) = brioche_capture::open_capturer(wanted_mon) {
                let new_mon = new_cap.monitor().clone();
                let new_dst_w = new_mon.width.min(2560) / 2 * 2;
                let new_dst_h = new_mon.height.min(1440) / 2 * 2;
                let new_profile = EncodeProfile {
                    src_width: new_mon.width,
                    src_height: new_mon.height,
                    dst_width: new_dst_w,
                    dst_height: new_dst_h,
                    ..profile.clone()
                };
                if let Ok(new_enc) = StreamingEncoder::start(&ffmpeg, &new_profile, use_nvenc) {
                    cap = new_cap;
                    mon = new_mon;
                    profile = new_profile;
                    encoder = new_enc;
                    current_mon_id = wanted_mon;
                    stream_id = stream_id.wrapping_add(1).max(1);
                    sender.set_stream_id(stream_id);
                    last_cursor_pos = None;
                    if let Ok(mut inj) = injector.lock() {
                        inj.set_monitor(MonitorRect {
                            left: mon.left,
                            top: mon.top,
                            width: mon.width,
                            height: mon.height,
                        });
                    }
                    let _ = send_control(
                        &mut ctrl_send,
                        &ControlMessage::StreamDescriptor {
                            stream_id,
                            monitor_id: mon.id,
                            width: new_dst_w,
                            height: new_dst_h,
                            fps_limit: profile.fps,
                            codec: if matches!(codec, VideoCodec::Hevc) {
                                "hevc".into()
                            } else {
                                "h264".into()
                            },
                            chroma_444: matches!(chroma, PixelChroma::Yuv444p),
                            monitors: monitor_descs.clone(),
                            host_udp_port,
                        },
                    )
                    .await;
                }
            } else {
                requested_mon.store(current_mon_id, Ordering::Release);
            }
        }

        let grabbed = cap.grab_frame();

        // Separated hardware cursor sync over QUIC (deduped so idle cursor never floods control stream)
        if let Ok((cur, shape_changed)) = cap.grab_cursor() {
            if shape_changed && cur.width > 0 && cur.height > 0 {
                let _ = send_control(
                    &mut ctrl_send,
                    &ControlMessage::CursorShape {
                        hash: cur.shape_hash,
                        width: cur.width,
                        height: cur.height,
                        hotspot_x: cur.hotspot_x,
                        hotspot_y: cur.hotspot_y,
                        rgba_hex: hex::encode(&cur.rgba),
                    },
                )
                .await;
            }
            let nx = cur.x as f32 / mon.width.max(1) as f32;
            let ny = cur.y as f32 / mon.height.max(1) as f32;
            let visible = (0.0..=1.0).contains(&nx) && (0.0..=1.0).contains(&ny);
            let quantized = (cur.x, cur.y, visible);
            if last_cursor_pos != Some(quantized) {
                last_cursor_pos = Some(quantized);
                let _ = send_control(
                    &mut ctrl_send,
                    &ControlMessage::CursorPosition {
                        x: nx,
                        y: ny,
                        visible,
                    },
                )
                .await;
            }
        }

        if let Ok(frame) = grabbed {
            // Idle frame pacing: static desktop drops to ~2 FPS heartbeat; wakes instantly on change
            let force_send = frame.dirty || last_sent.elapsed() >= Duration::from_millis(500);
            if force_send {
                idle_streak = 0;
                if encoder.push_bgra(&frame.bgra).is_err() {
                    break;
                }
                last_sent = Instant::now();
            } else {
                idle_streak = idle_streak.saturating_add(1);
            }
        }

        let quality = sender.current_quality();
        while let Some(au) = encoder.try_recv_au() {
            let mut flags = 0u8;
            if au.is_idr {
                flags |= FLAG_IDR;
            }
            if matches!(encoder.codec, VideoCodec::Hevc) {
                flags |= FLAG_HEVC;
            }
            if matches!(encoder.chroma, PixelChroma::Yuv444p) {
                flags |= FLAG_YUV444;
            }
            let _ = sender
                .send_video_frame(&au.data, flags, quality.fec_percent, None)
                .await;
        }

        let sleep_for = if idle_streak > 4 {
            Duration::from_millis(30)
        } else {
            frame_interval.saturating_sub(tick.elapsed())
        };
        if !sleep_for.is_zero() {
            tokio::time::sleep(sleep_for).await;
        }
    }
    input_task.abort();
}

/// Live Brioche v3 Viewer: native winit + wgpu Vulkan/Metal/DX12 window + QUIC + UDP/RS-FEC.
fn run_view_cli(args: &[String]) {
    let host = arg_val(args, "--host").unwrap_or("127.0.0.1").to_string();
    let quic_port: u16 = arg_val(args, "--quic-port")
        .and_then(|s| s.parse().ok())
        .expect("--quic-port required");
    let pin = arg_val(args, "--pin").expect("--pin required").to_string();
    let key_hex = arg_val(args, "--key").expect("--key required");
    let key: [u8; 32] = hex::decode(key_hex)
        .expect("invalid --key hex")
        .try_into()
        .expect("--key must be 32 bytes hex");
    let title = arg_val(args, "--title")
        .unwrap_or("Brioche v2.0 · GPU 极速会话")
        .to_string();
    let fps: u32 = arg_val(args, "--fps")
        .and_then(|s| s.parse().ok())
        .unwrap_or(60);
    let bitrate_kbps: u32 = arg_val(args, "--bitrate")
        .and_then(|s| s.parse().ok())
        .unwrap_or(20_000);
    let chroma_444 = !args.iter().any(|a| a == "--no-444");

    let (ctrl_tx, ctrl_rx) = std::sync::mpsc::channel::<ControlMessage>();
    let _ = run_viewer_window(title, ctrl_tx, move |viewer_handle| {
        std::thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .unwrap();
            rt.block_on(async move {
                let addr: SocketAddr = format!("{host}:{quic_port}").parse().unwrap();
                let (_ep, conn) = match connect_quic_client(addr, &pin).await {
                    Ok(v) => v,
                    Err(e) => {
                        eprintln!("QUIC connect failed: {e}");
                        viewer_handle.close();
                        return;
                    }
                };
                let (mut send, mut recv) = conn.open_bi().await.unwrap();
                let udp_sock = bind_tuned_udp("0.0.0.0:0".parse().unwrap()).unwrap();
                let local_udp_port = udp_sock.local_addr().unwrap().port();
                let _ = send_control(
                    &mut send,
                    &ControlMessage::SessionHello {
                        udp_port: local_udp_port,
                        requested_codec: "hevc".into(),
                        chroma_444,
                        fps,
                        bitrate_kbps,
                    },
                )
                .await;

                let Ok(ControlMessage::StreamDescriptor {
                    stream_id,
                    width,
                    height,
                    codec,
                    chroma_444: actual_444,
                    host_udp_port,
                    ..
                }) = recv_control(&mut recv).await
                else {
                    viewer_handle.close();
                    return;
                };

                viewer_handle.set_title(format!(
                    "Brioche v2.0 · {width}×{height} · {} {}",
                    codec.to_uppercase(),
                    if actual_444 { "4:4:4" } else { "4:2:0" }
                ));

                // Outgoing input/control forwarder
                tokio::spawn(async move {
                    while let Ok(msg) = ctrl_rx.recv() {
                        if send_control(&mut send, &msg).await.is_err() {
                            break;
                        }
                    }
                });

                // Incoming cursor/stream control listener
                let vh_ctrl = viewer_handle.clone();
                tokio::spawn(async move {
                    while let Ok(msg) = recv_control(&mut recv).await {
                        match msg {
                            ControlMessage::CursorShape {
                                width,
                                height,
                                rgba_hex,
                                ..
                            } => {
                                if let Ok(rgba) = hex::decode(rgba_hex) {
                                    vh_ctrl.update_cursor_shape(
                                        width as u32,
                                        height as u32,
                                        rgba,
                                    );
                                }
                            }
                            ControlMessage::CursorPosition { x, y, visible } => {
                                vh_ctrl.update_cursor_pos(x, y, visible);
                            }
                            _ => {}
                        }
                    }
                });

                let Some(ffmpeg) = brioche_codec::find_ffmpeg() else {
                    eprintln!("ffmpeg not found; closing v2 viewer");
                    viewer_handle.close();
                    return;
                };
                let vcodec = if codec == "hevc" {
                    VideoCodec::Hevc
                } else {
                    VideoCodec::H264
                };
                let pchroma = if actual_444 {
                    PixelChroma::Yuv444p
                } else {
                    PixelChroma::Yuv420p
                };
                let rchroma = if actual_444 {
                    ChromaFormat::Yuv444p
                } else {
                    ChromaFormat::Yuv420p
                };
                let mut decoder = match StreamingDecoder::start(&ffmpeg, vcodec, pchroma, width, height) {
                    Ok(d) => d,
                    Err(e) => {
                        eprintln!("decoder start failed: {e}");
                        viewer_handle.close();
                        return;
                    }
                };
                let mut receiver = UdpMediaReceiver::new(udp_sock, &key, stream_id);
                if host_udp_port > 0 {
                    receiver.set_peer(SocketAddr::new(addr.ip(), host_udp_port));
                    // Symmetric UDP punch opens local firewall & router NAT pinholes immediately.
                    for _ in 0..3 {
                        let _ = receiver.send_ping().await;
                    }
                }
                let mut last_ping = Instant::now();

                loop {
                    if last_ping.elapsed() >= Duration::from_secs(2) {
                        let _ = receiver.send_ping().await;
                        last_ping = Instant::now();
                    }
                    match tokio::time::timeout(Duration::from_secs(2), receiver.recv_frame()).await {
                        Ok(Ok(frame)) => {
                            if decoder.push_access_unit(&frame.payload).is_err() {
                                break;
                            }
                            if let Some(yuv) = decoder.recv_frame_timeout(Duration::from_millis(80)) {
                                viewer_handle.publish_frame(PendingYuvFrame {
                                    width: yuv.width,
                                    height: yuv.height,
                                    chroma: rchroma,
                                    y: yuv.y,
                                    u: yuv.u,
                                    v: yuv.v,
                                });
                            }
                        }
                        Ok(Err(_)) => break,
                        Err(_) => {
                            // Periodic wakeup to send UDP keepalive ping while screen is idle.
                            let _ = receiver.send_ping().await;
                            last_ping = Instant::now();
                        }
                    }
                }
                viewer_handle.close();
            });
        });
    });
}

/// Sustained 60-frame persistent streaming benchmark:
/// Persistent NVENC HEVC 4:4:4 -> Multi-Stripe Encrypted UDP+RS-FEC (5% loss) -> Persistent Decoder -> Vulkan wgpu Shader.
async fn run_bench_stream(args: &[String]) {
    let total_frames: usize = arg_val(args, "--frames")
        .and_then(|s| s.parse().ok())
        .unwrap_or(60);
    let width = 1920u32;
    let height = 1080u32;

    let caps = probe_encoders().expect("ffmpeg required for bench-stream");
    let ffmpeg = PathBuf::from(&caps.ffmpeg_path);
    let use_nvenc = caps.hevc_nvenc || caps.h264_nvenc;
    let codec = if caps.hevc_nvenc {
        VideoCodec::Hevc
    } else {
        VideoCodec::H264
    };
    let chroma = if caps.hevc_nvenc_444 || caps.h264_nvenc_444 {
        PixelChroma::Yuv444p
    } else {
        PixelChroma::Yuv420p
    };
    let rchroma = match chroma {
        PixelChroma::Yuv444p => ChromaFormat::Yuv444p,
        PixelChroma::Yuv420p => ChromaFormat::Yuv420p,
    };

    let profile = EncodeProfile {
        src_width: width,
        src_height: height,
        dst_width: width,
        dst_height: height,
        fps: 60,
        bitrate_kbps: 20_000,
        codec,
        chroma,
    };
    let mut encoder = StreamingEncoder::start(&ffmpeg, &profile, use_nvenc).unwrap();
    let mut decoder = StreamingDecoder::start(&ffmpeg, encoder.codec, chroma, width, height).unwrap();
    let mut renderer = GpuVideoRenderer::new(
        width,
        height,
        rchroma,
        wgpu::TextureFormat::Rgba8Unorm,
    )
    .unwrap();
    renderer.set_sharpness(0.35);

    let key = [0x5Au8; 32];
    let rx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();
    let rx_addr = rx_sock.local_addr().unwrap();
    let tx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();
    let mut sender = UdpMediaSender::new(tx_sock, rx_addr, &key, 1);
    let mut receiver = UdpMediaReceiver::new(rx_sock, &key, 1);

    let started = Instant::now();
    // True concurrent pipelining (no lockstep wait):
    // Thread 1 pushes frames into persistent NVENC -> Thread 2 streams AUs over encrypted UDP+FEC
    // -> Thread 3 receives & feeds persistent decoder -> Main uploads YUV444 to RTX 3090 Vulkan wgpu.
    let (au_tx, mut au_rx) = tokio::sync::mpsc::channel::<brioche_codec::EncodedAccessUnit>(16);
    let encoder_name = encoder.encoder_name;
    let enc_codec = encoder.codec;
    let push_handle = std::thread::spawn(move || {
        let mut frame_buf = vec![0x48u8; (width * height * 4) as usize];
        for i in 0..total_frames {
            let bar_y = (i * 12) % (height as usize - 16);
            let row_start = bar_y * width as usize * 4;
            for b in &mut frame_buf[row_start..row_start + width as usize * 4 * 8] {
                *b = ((i * 19 + 80) & 0xFF) as u8;
            }
            if encoder.push_bgra(&frame_buf).is_err() {
                break;
            }
            while let Some(au) = encoder.try_recv_au() {
                let _ = au_tx.blocking_send(au);
            }
        }
        encoder.finish_input();
        while let Some(au) = encoder.recv_au_timeout(Duration::from_millis(300)) {
            let _ = au_tx.blocking_send(au);
        }
    });

    let send_task = tokio::spawn(async move {
        let mut bytes_total = 0usize;
        let mut dgrams_dropped = 0usize;
        let mut sent_frames = 0usize;
        while let Some(au) = au_rx.recv().await {
            bytes_total += au.data.len();
            let flags = if au.is_idr { FLAG_IDR } else { 0 } | FLAG_HEVC | FLAG_YUV444;
            let (_fid, _s, dropped) = sender
                .send_video_frame(&au.data, flags, 15, Some(20))
                .await
                .unwrap();
            dgrams_dropped += dropped;
            sent_frames += 1;
        }
        (bytes_total, dgrams_dropped, sent_frames)
    });

    let (yuv_tx, mut yuv_rx) = tokio::sync::mpsc::channel::<brioche_codec::DecodedYuvFrame>(8);
    let recv_task = tokio::spawn(async move {
        while let Ok(Ok(rx_frame)) =
            tokio::time::timeout(Duration::from_millis(400), receiver.recv_frame()).await
        {
            if decoder.push_access_unit(&rx_frame.payload).is_err() {
                break;
            }
            while let Some(yuv) = decoder.recv_frame_timeout(Duration::from_millis(2)) {
                let _ = yuv_tx.send(yuv).await;
            }
        }
        decoder.finish_input();
        while let Some(yuv) = decoder.recv_frame_timeout(Duration::from_millis(300)) {
            let _ = yuv_tx.send(yuv).await;
        }
        receiver.stats()
    });

    let mut decoded_frames = 0usize;
    let mut gpu_upload_ms_total = 0.0;
    while let Some(yuv) = yuv_rx.recv().await {
        let t_gpu = Instant::now();
        renderer
            .upload_yuv(yuv.width, yuv.height, rchroma, &yuv.y, &yuv.u, &yuv.v)
            .unwrap();
        gpu_upload_ms_total += t_gpu.elapsed().as_secs_f64() * 1000.0;
        decoded_frames += 1;
    }

    let _ = push_handle.join();
    let (encoded_bytes_total, total_dgrams_dropped, _sent_frames) = send_task.await.unwrap();
    let (udp_ok, udp_fec_rec, udp_dropped) = recv_task.await.unwrap();
    let elapsed_s = started.elapsed().as_secs_f64();

    let report = serde_json::json!({
        "resolution": [width, height],
        "encoder": encoder_name,
        "codec": format!("{:?}", enc_codec),
        "chroma": format!("{:?}", chroma),
        "gpu_adapter": renderer.adapter_name,
        "gpu_backend": renderer.backend_name,
        "frames_pushed": total_frames,
        "frames_decoded_and_gpu_uploaded": decoded_frames,
        "sustained_e2e_fps": (decoded_frames as f64 / elapsed_s * 10.0).round() / 10.0,
        "avg_frame_ms": (elapsed_s * 1000.0 / decoded_frames.max(1) as f64 * 100.0).round() / 100.0,
        "avg_gpu_yuv_upload_ms": (gpu_upload_ms_total / decoded_frames.max(1) as f64 * 1000.0).round() / 1000.0,
        "avg_encoded_kb": (encoded_bytes_total as f64 / total_frames.max(1) as f64 / 1024.0 * 10.0).round() / 10.0,
        "udp_frames_ok": udp_ok,
        "udp_fec_recovered": udp_fec_rec,
        "udp_frames_dropped": udp_dropped,
        "simulated_5pct_datagrams_dropped": total_dgrams_dropped,
    });
    println!("{}", serde_json::to_string_pretty(&report).unwrap());
}

async fn run_bench_udp(args: &[String]) {
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
