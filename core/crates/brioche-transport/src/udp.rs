//! Async encrypted UDP media sender/receiver with 4 MiB socket buffers,
//! RS-FEC, bounded NACK retransmission cache, PLI instant-IDR signaling,
//! and TWCC congestion feedback loop.

use std::collections::{BTreeMap, VecDeque};
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use brioche_proto::{
    PacketCipher, PacketHeader, PacketKind, ReplayWindow, MAX_DATAGRAM_LEN, MAX_SHARD_PAYLOAD,
};
use socket2::{Domain, Protocol, Socket, Type};
use tokio::net::UdpSocket;

use crate::congestion::{AbrLimits, CongestionController, QualityDecision};
use crate::fec::split_frame_into_shards;
use crate::reassembly::{AssembledFrame, AssemblerEvent, FrameAssembler};

const SOCKET_BUF_BYTES: usize = 4 * 1024 * 1024;
const NACK_CACHE_FRAMES: usize = 24;

#[inline]
pub fn now_unix_us() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_micros() as u64)
        .unwrap_or(0)
}

/// Bind a Tokio UDP socket with enlarged 4 MiB OS send/recv buffers for bursty 4K IDR frames.
pub fn bind_tuned_udp(addr: SocketAddr) -> std::io::Result<UdpSocket> {
    let domain = if addr.is_ipv6() {
        Domain::IPV6
    } else {
        Domain::IPV4
    };
    let sock = Socket::new(domain, Type::DGRAM, Some(Protocol::UDP))?;
    let _ = sock.set_recv_buffer_size(SOCKET_BUF_BYTES);
    let _ = sock.set_send_buffer_size(SOCKET_BUF_BYTES);
    sock.set_nonblocking(true)?;
    sock.bind(&addr.into())?;
    let std_sock: std::net::UdpSocket = sock.into();
    UdpSocket::from_std(std_sock)
}

struct NackCache {
    frames: BTreeMap<u32, BTreeMap<u16, Vec<u8>>>,
    order: VecDeque<u32>,
}

impl NackCache {
    fn new() -> Self {
        Self {
            frames: BTreeMap::new(),
            order: VecDeque::new(),
        }
    }

    fn insert(&mut self, frame_id: u32, shards: BTreeMap<u16, Vec<u8>>) {
        self.frames.insert(frame_id, shards);
        self.order.push_back(frame_id);
        while self.order.len() > NACK_CACHE_FRAMES {
            if let Some(old) = self.order.pop_front() {
                self.frames.remove(&old);
            }
        }
    }

    fn get_shard(&self, frame_id: u32, key: u16) -> Option<&[u8]> {
        self.frames
            .get(&frame_id)
            .and_then(|m| m.get(&key))
            .map(|b| b.as_slice())
    }
}

pub struct UdpMediaSender {
    socket: UdpSocket,
    peer: SocketAddr,
    tx_cipher: PacketCipher,
    rx_cipher: PacketCipher,
    rx_replay: ReplayWindow,
    seq: AtomicU64,
    stream_id: u16,
    frame_id: u32,
    nack_cache: NackCache,
    pub pli_requested: Arc<AtomicBool>,
    pub congestion: CongestionController,
    pub stats_nack_retransmits: u64,
    pub stats_pli_received: u64,
    feedback_buf: Vec<u8>,
}

impl UdpMediaSender {
    pub fn new(socket: UdpSocket, peer: SocketAddr, key: &[u8; 32], stream_id: u16) -> Self {
        Self {
            socket,
            peer,
            tx_cipher: PacketCipher::new(key, *b"H2C\0"),
            rx_cipher: PacketCipher::new(key, *b"C2H\0"),
            rx_replay: ReplayWindow::default(),
            seq: AtomicU64::new(1),
            stream_id,
            frame_id: 0,
            nack_cache: NackCache::new(),
            pli_requested: Arc::new(AtomicBool::new(false)),
            congestion: CongestionController::new(24_000, AbrLimits::default()),
            stats_nack_retransmits: 0,
            stats_pli_received: 0,
            feedback_buf: vec![0u8; MAX_DATAGRAM_LEN + 64],
        }
    }

    pub fn set_stream_id(&mut self, stream_id: u16) {
        self.stream_id = stream_id;
        self.frame_id = 0;
        self.nack_cache = NackCache::new();
    }

    pub fn take_pli(&self) -> bool {
        self.pli_requested.swap(false, Ordering::AcqRel)
    }

    pub fn current_quality(&self) -> QualityDecision {
        self.congestion.current()
    }

    /// Non-blocking drain of NACK / PLI / TWCC / Ping feedback datagrams from the viewer.
    pub async fn poll_feedback(&mut self) -> std::io::Result<()> {
        loop {
            match self.socket.try_recv_from(&mut self.feedback_buf) {
                Ok((n, from)) => {
                    let Ok((hdr, pt)) = self
                        .rx_cipher
                        .open(&self.feedback_buf[..n], Some(&mut self.rx_replay))
                    else {
                        continue;
                    };
                    // Learn authenticated peer's actual NAT/firewall mapped UDP port.
                    self.peer = from;
                    match hdr.kind {
                        PacketKind::Ping => {
                            let pong_hdr = PacketHeader {
                                kind: PacketKind::Pong,
                                stream_id: self.stream_id,
                                frame_id: 0,
                                stripe_idx: 0,
                                total_stripes: 0,
                                shard_idx: 0,
                                data_shards: 0,
                                fec_shards: 0,
                                flags: 0,
                                capture_ts_us: hdr.capture_ts_us,
                                seq_num: self.seq.fetch_add(1, Ordering::Relaxed),
                            };
                            if let Ok(dgram) = self.tx_cipher.seal(&pong_hdr, &pt) {
                                let _ = self.socket.send_to(&dgram, self.peer).await;
                            }
                        }
                        PacketKind::Nack => {
                            // Payload: list of u16 BE encoded (stripe_idx << 8) | shard_idx
                            for chunk in pt.chunks_exact(2) {
                                let key = u16::from_be_bytes([chunk[0], chunk[1]]);
                                if let Some(cached) = self.nack_cache.get_shard(hdr.frame_id, key) {
                                    let _ = self.socket.send_to(cached, self.peer).await;
                                    self.stats_nack_retransmits += 1;
                                }
                            }
                        }
                        PacketKind::Pli => {
                            self.pli_requested.store(true, Ordering::Release);
                            self.stats_pli_received += 1;
                        }
                        PacketKind::Twcc => {
                            if pt.len() >= 12 {
                                let loss_ppm =
                                    u32::from_be_bytes([pt[0], pt[1], pt[2], pt[3]]);
                                let grad_us = i64::from_be_bytes([
                                    pt[4], pt[5], pt[6], pt[7], pt[8], pt[9], pt[10], pt[11],
                                ]);
                                self.congestion.on_feedback(loss_ppm, grad_us);
                            }
                        }
                        _ => {}
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(e) => return Err(e),
            }
        }
        Ok(())
    }

    pub async fn send_video_frame(
        &mut self,
        payload: &[u8],
        flags: u8,
        fec_percent: u8,
        simulated_drop_every: Option<usize>,
    ) -> std::io::Result<(u32, usize, usize)> {
        self.frame_id = self.frame_id.wrapping_add(1);
        let fid = self.frame_id;
        let ts = now_unix_us();

        // Multi-stripe FEC: each stripe <= 64 data shards (~85 KB) -> fast O(K^2) RS & up to 22 MB IDRs!
        let max_stripe_bytes = 64 * (MAX_SHARD_PAYLOAD - 4);
        let stripes: Vec<&[u8]> = if payload.is_empty() {
            vec![&payload[..]]
        } else {
            payload.chunks(max_stripe_bytes).collect()
        };
        if stripes.len() > 255 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "frame exceeds 255 FEC stripes",
            ));
        }
        let total_stripes = stripes.len() as u8;
        let mut sent = 0usize;
        let mut dropped = 0usize;
        let mut sealed_cache = BTreeMap::new();

        for (stripe_idx, stripe_bytes) in stripes.into_iter().enumerate() {
            let (data_shards, parity_shards) =
                split_frame_into_shards(stripe_bytes, MAX_SHARD_PAYLOAD, fec_percent)
                    .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;

            let k = data_shards.len() as u8;
            let m = parity_shards.len() as u8;

            for (idx, shard) in data_shards
                .into_iter()
                .chain(parity_shards.into_iter())
                .enumerate()
            {
                let seq_num = self.seq.fetch_add(1, Ordering::Relaxed);
                let kind = if idx < k as usize {
                    PacketKind::VideoData
                } else {
                    PacketKind::VideoFec
                };
                let header = PacketHeader {
                    kind,
                    stream_id: self.stream_id,
                    frame_id: fid,
                    stripe_idx: stripe_idx as u8,
                    total_stripes,
                    shard_idx: idx as u8,
                    data_shards: k,
                    fec_shards: m,
                    flags,
                    capture_ts_us: ts,
                    seq_num,
                };
                let datagram = self
                    .tx_cipher
                    .seal(&header, &shard)
                    .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
                let cache_key = ((stripe_idx as u16) << 8) | (idx as u16);
                sealed_cache.insert(cache_key, datagram.clone());

                if let Some(every) = simulated_drop_every {
                    if every > 0 && (seq_num as usize) % every == 0 {
                        dropped += 1;
                        continue;
                    }
                }
                self.socket.send_to(&datagram, self.peer).await?;
                sent += 1;
            }
        }
        self.nack_cache.insert(fid, sealed_cache);
        Ok((fid, sent, dropped))
    }
}

pub struct UdpMediaReceiver {
    socket: UdpSocket,
    peer: Option<SocketAddr>,
    rx_cipher: PacketCipher,
    tx_cipher: PacketCipher,
    replay: ReplayWindow,
    feedback_seq: AtomicU64,
    assembler: FrameAssembler,
    stream_id: u16,
    buf: Vec<u8>,
}

impl UdpMediaReceiver {
    pub fn new(socket: UdpSocket, key: &[u8; 32], stream_id: u16) -> Self {
        Self {
            socket,
            peer: None,
            rx_cipher: PacketCipher::new(key, *b"H2C\0"),
            tx_cipher: PacketCipher::new(key, *b"C2H\0"),
            replay: ReplayWindow::default(),
            feedback_seq: AtomicU64::new(1),
            assembler: FrameAssembler::new(stream_id),
            stream_id,
            buf: vec![0u8; MAX_DATAGRAM_LEN + 64],
        }
    }

    pub fn set_peer(&mut self, peer: SocketAddr) {
        self.peer = Some(peer);
    }

    pub fn stats(&self) -> (u64, u64, u64) {
        (
            self.assembler.stats_frames_ok,
            self.assembler.stats_frames_fec_recovered,
            self.assembler.stats_frames_dropped,
        )
    }

    pub async fn send_nack(&self, frame_id: u32, missing_shards: &[u16]) -> std::io::Result<()> {
        let Some(peer) = self.peer else {
            return Ok(());
        };
        let mut payload = Vec::with_capacity(missing_shards.len() * 2);
        for &idx in missing_shards.iter().take(64) {
            payload.extend_from_slice(&idx.to_be_bytes());
        }
        let hdr = PacketHeader {
            kind: PacketKind::Nack,
            stream_id: self.stream_id,
            frame_id,
            stripe_idx: 0,
            total_stripes: 0,
            shard_idx: 0,
            data_shards: 0,
            fec_shards: 0,
            flags: 0,
            capture_ts_us: now_unix_us(),
            seq_num: self.feedback_seq.fetch_add(1, Ordering::Relaxed),
        };
        if let Ok(dgram) = self.tx_cipher.seal(&hdr, &payload) {
            self.socket.send_to(&dgram, peer).await?;
        }
        Ok(())
    }

    pub async fn send_pli(&self) -> std::io::Result<()> {
        let Some(peer) = self.peer else {
            return Ok(());
        };
        let hdr = PacketHeader {
            kind: PacketKind::Pli,
            stream_id: self.stream_id,
            frame_id: 0,
            stripe_idx: 0,
            total_stripes: 0,
            shard_idx: 0,
            data_shards: 0,
            fec_shards: 0,
            flags: 0,
            capture_ts_us: now_unix_us(),
            seq_num: self.feedback_seq.fetch_add(1, Ordering::Relaxed),
        };
        if let Ok(dgram) = self.tx_cipher.seal(&hdr, &[]) {
            self.socket.send_to(&dgram, peer).await?;
        }
        Ok(())
    }

    pub async fn send_twcc(&self, loss_ppm: u32, delay_gradient_us: i64) -> std::io::Result<()> {
        let Some(peer) = self.peer else {
            return Ok(());
        };
        let mut payload = [0u8; 12];
        payload[0..4].copy_from_slice(&loss_ppm.to_be_bytes());
        payload[4..12].copy_from_slice(&delay_gradient_us.to_be_bytes());
        let hdr = PacketHeader {
            kind: PacketKind::Twcc,
            stream_id: self.stream_id,
            frame_id: 0,
            stripe_idx: 0,
            total_stripes: 0,
            shard_idx: 0,
            data_shards: 0,
            fec_shards: 0,
            flags: 0,
            capture_ts_us: now_unix_us(),
            seq_num: self.feedback_seq.fetch_add(1, Ordering::Relaxed),
        };
        if let Ok(dgram) = self.tx_cipher.seal(&hdr, &payload) {
            self.socket.send_to(&dgram, peer).await?;
        }
        Ok(())
    }

    /// Send an authenticated UDP Ping punch/keepalive packet to open firewall & NAT pinholes.
    pub async fn send_ping(&self) -> std::io::Result<()> {
        let Some(peer) = self.peer else {
            return Ok(());
        };
        let hdr = PacketHeader {
            kind: PacketKind::Ping,
            stream_id: self.stream_id,
            frame_id: 0,
            stripe_idx: 0,
            total_stripes: 0,
            shard_idx: 0,
            data_shards: 0,
            fec_shards: 0,
            flags: 0,
            capture_ts_us: now_unix_us(),
            seq_num: self.feedback_seq.fetch_add(1, Ordering::Relaxed),
        };
        if let Ok(dgram) = self.tx_cipher.seal(&hdr, &[]) {
            self.socket.send_to(&dgram, peer).await?;
        }
        Ok(())
    }

    pub async fn recv_frame(&mut self) -> std::io::Result<AssembledFrame> {
        loop {
            let (n, addr) = self.socket.recv_from(&mut self.buf).await?;
            if self.peer.is_none() {
                self.peer = Some(addr);
            }
            let Ok((header, pt)) = self.rx_cipher.open(&self.buf[..n], Some(&mut self.replay))
            else {
                continue;
            };
            if matches!(header.kind, PacketKind::Pong | PacketKind::Ping) {
                continue;
            }
            let mut ready = None;
            let mut pli_needed = false;
            for ev in self.assembler.push_shard(header, pt, Instant::now()) {
                match ev {
                    AssemblerEvent::FrameReady(frame) => ready = Some(frame),
                    AssemblerEvent::RequestPli { .. } => pli_needed = true,
                    AssemblerEvent::RequestNack {
                        frame_id,
                        missing_shards,
                        ..
                    } => {
                        let _ = self.send_nack(frame_id, &missing_shards).await;
                    }
                }
            }
            if pli_needed {
                let _ = self.send_pli().await;
            }
            if let Some(frame) = ready {
                return Ok(frame);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use brioche_proto::FLAG_IDR;

    #[tokio::test]
    async fn loopback_encrypted_udp_recovers_frames_under_10pct_loss() {
        let key = [0xA5u8; 32];
        let rx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();
        let rx_addr = rx_sock.local_addr().unwrap();
        let tx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();

        let mut sender = UdpMediaSender::new(tx_sock, rx_addr, &key, 1);
        let mut receiver = UdpMediaReceiver::new(rx_sock, &key, 1);

        let frame_payload: Vec<u8> = (0..32_768).map(|i| (i % 251) as u8).collect();
        let expected = frame_payload.clone();

        let recv_task = tokio::spawn(async move {
            let mut frames = Vec::new();
            for _ in 0..20 {
                let f = tokio::time::timeout(
                    std::time::Duration::from_secs(3),
                    receiver.recv_frame(),
                )
                .await
                .expect("timed out waiting for UDP frame")
                .unwrap();
                frames.push(f);
            }
            (frames, receiver.stats())
        });

        for i in 0..20 {
            let flags = if i == 0 { FLAG_IDR } else { 0 };
            sender
                .send_video_frame(&frame_payload, flags, 20, Some(10))
                .await
                .unwrap();
            tokio::time::sleep(std::time::Duration::from_millis(2)).await;
        }

        let (frames, (ok, fec_rec, dropped)) = recv_task.await.unwrap();
        assert_eq!(frames.len(), 20);
        assert_eq!(ok, 20);
        assert!(fec_rec > 0, "expected RS-FEC recoveries under 10% loss");
        assert_eq!(dropped, 0);
        for f in frames {
            assert_eq!(f.payload, expected);
        }
    }

    #[tokio::test]
    async fn closed_loop_nack_pli_and_twcc_drive_sender() {
        let key = [0x33u8; 32];
        let rx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();
        let rx_addr = rx_sock.local_addr().unwrap();
        let tx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();
        let tx_addr = tx_sock.local_addr().unwrap();

        let mut sender = UdpMediaSender::new(tx_sock, rx_addr, &key, 1);
        let mut receiver = UdpMediaReceiver::new(rx_sock, &key, 1);
        receiver.set_peer(tx_addr);

        let payload = vec![0xCDu8; 4096];
        let (fid, _, _) = sender.send_video_frame(&payload, FLAG_IDR, 0, None).await.unwrap();

        receiver.send_nack(fid, &[0, 1]).await.unwrap();
        receiver.send_pli().await.unwrap();
        receiver.send_twcc(90_000, 10_000).await.unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;

        let before_bps = sender.current_quality().bitrate_kbps;
        sender.poll_feedback().await.unwrap();
        assert_eq!(sender.stats_nack_retransmits, 2);
        assert!(sender.take_pli());
        assert_eq!(sender.stats_pli_received, 1);
        assert!(sender.current_quality().bitrate_kbps < before_bps);
    }

    #[tokio::test]
    async fn symmetric_udp_ping_punch_learns_peer_and_keeps_session_alive() {
        let key = [0x77u8; 32];
        let rx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();
        let rx_addr = rx_sock.local_addr().unwrap();
        let tx_sock = bind_tuned_udp("127.0.0.1:0".parse().unwrap()).unwrap();
        let tx_addr = tx_sock.local_addr().unwrap();

        // Start sender with a stale/dummy peer port; receiver's authenticated Ping updates it!
        let stale_peer: SocketAddr = "127.0.0.1:9".parse().unwrap();
        let mut sender = UdpMediaSender::new(tx_sock, stale_peer, &key, 1);
        let mut receiver = UdpMediaReceiver::new(rx_sock, &key, 1);
        receiver.set_peer(tx_addr);

        receiver.send_ping().await.unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(15)).await;
        sender.poll_feedback().await.unwrap();
        assert_eq!(sender.peer, rx_addr);

        let payload = vec![0xABu8; 2048];
        sender.send_video_frame(&payload, FLAG_IDR, 0, None).await.unwrap();
        let frame = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            receiver.recv_frame(),
        )
        .await
        .expect("receiver should receive frame after UDP punch")
        .unwrap();
        assert_eq!(frame.payload, payload);
    }
}
