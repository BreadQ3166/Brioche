//! brioche-core: Next-gen Rust low-latency streaming engine CLI & benchmark harness.

use std::env;
use std::time::Duration;

use brioche_proto::{FLAG_HEVC, FLAG_IDR, FLAG_YUV444, PROTO_VERSION};
use brioche_transport::udp::{now_unix_us, UdpMediaReceiver, UdpMediaSender};
use serde::Serialize;
use tokio::net::UdpSocket;

#[derive(Serialize)]
struct CoreInfo {
    name: &'static str,
    version: &'static str,
    protocol_version: u8,
    os: &'static str,
    arch: &'static str,
    transports: &'static [&'static str],
    fec: &'static str,
    cipher: &'static str,
}

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
        _ => {
            let info = CoreInfo {
                name: "brioche-core",
                version: env!("CARGO_PKG_VERSION"),
                protocol_version: PROTO_VERSION,
                os: env::consts::OS,
                arch: env::consts::ARCH,
                transports: &["brioche-udp-v3", "fec-gf256-cauchy", "abr-twcc-gcc"],
                fec: "systematic-cauchy-rs-gf256",
                cipher: "chacha20-poly1305",
            };
            println!("{}", serde_json::to_string_pretty(&info).unwrap());
        }
    }
}

async fn run_bench(args: &[String]) {
    let mut frames: usize = 120;
    let mut frame_kb: usize = 48; // ~48 KB per frame ≈ 23 Mbps at 60 FPS
    let mut fec_percent: u8 = 15;
    let mut drop_every: Option<usize> = Some(20); // 5% simulated packet loss

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
    let rx_sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let rx_addr = rx_sock.local_addr().unwrap();
    let tx_sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();

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

    let started = std::time::Instant::now();
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
