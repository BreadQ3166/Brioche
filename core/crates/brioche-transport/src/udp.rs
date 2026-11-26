//! Async encrypted UDP media sender/receiver with RS-FEC and zero-HOL frame assembly.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use brioche_proto::{
    PacketCipher, PacketHeader, PacketKind, ReplayWindow, MAX_DATAGRAM_LEN, MAX_SHARD_PAYLOAD,
};
use tokio::net::UdpSocket;

use crate::fec::split_frame_into_shards;
use crate::reassembly::{AssembledFrame, AssemblerEvent, FrameAssembler};

#[inline]
pub fn now_unix_us() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_micros() as u64)
        .unwrap_or(0)
}

pub struct UdpMediaSender {
    socket: UdpSocket,
    peer: SocketAddr,
    cipher: PacketCipher,
    seq: AtomicU64,
    stream_id: u16,
    frame_id: u32,
}

impl UdpMediaSender {
    pub fn new(socket: UdpSocket, peer: SocketAddr, key: &[u8; 32], stream_id: u16) -> Self {
        Self {
            socket,
            peer,
            cipher: PacketCipher::new(key, *b"H2C\0"),
            seq: AtomicU64::new(1),
            stream_id,
            frame_id: 0,
        }
    }

    pub async fn send_video_frame(
        &mut self,
        payload: &[u8],
        flags: u16,
        fec_percent: u8,
        simulated_drop_every: Option<usize>,
    ) -> std::io::Result<(u32, usize, usize)> {
        self.frame_id = self.frame_id.wrapping_add(1);
        let fid = self.frame_id;
        let ts = now_unix_us();

        let (data_shards, parity_shards) =
            split_frame_into_shards(payload, MAX_SHARD_PAYLOAD, fec_percent)
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidInput, e))?;

        let k = data_shards.len() as u8;
        let m = parity_shards.len() as u8;
        let mut sent = 0usize;
        let mut dropped = 0usize;

        for (idx, shard) in data_shards
            .into_iter()
            .chain(parity_shards.into_iter())
            .enumerate()
        {
            let seq_num = self.seq.fetch_add(1, Ordering::Relaxed);
            if let Some(every) = simulated_drop_every {
                if every > 0 && (seq_num as usize) % every == 0 {
                    dropped += 1;
                    continue;
                }
            }
            let kind = if idx < k as usize {
                PacketKind::VideoData
            } else {
                PacketKind::VideoFec
            };
            let header = PacketHeader {
                kind,
                stream_id: self.stream_id,
                frame_id: fid,
                shard_idx: idx as u16,
                data_shards: k,
                fec_shards: m,
                flags,
                capture_ts_us: ts,
                seq_num,
            };
            let datagram = self
                .cipher
                .seal(&header, &shard)
                .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
            self.socket.send_to(&datagram, self.peer).await?;
            sent += 1;
        }
        Ok((fid, sent, dropped))
    }
}

pub struct UdpMediaReceiver {
    socket: UdpSocket,
    cipher: PacketCipher,
    replay: ReplayWindow,
    assembler: FrameAssembler,
    buf: Vec<u8>,
}

impl UdpMediaReceiver {
    pub fn new(socket: UdpSocket, key: &[u8; 32], stream_id: u16) -> Self {
        Self {
            socket,
            cipher: PacketCipher::new(key, *b"H2C\0"),
            replay: ReplayWindow::default(),
            assembler: FrameAssembler::new(stream_id),
            buf: vec![0u8; MAX_DATAGRAM_LEN + 64],
        }
    }

    pub fn stats(&self) -> (u64, u64, u64) {
        (
            self.assembler.stats_frames_ok,
            self.assembler.stats_frames_fec_recovered,
            self.assembler.stats_frames_dropped,
        )
    }

    pub async fn recv_frame(&mut self) -> std::io::Result<AssembledFrame> {
        loop {
            let (n, _addr) = self.socket.recv_from(&mut self.buf).await?;
            let Ok((header, pt)) = self.cipher.open(&self.buf[..n], Some(&mut self.replay)) else {
                continue;
            };
            for ev in self.assembler.push_shard(header, pt, Instant::now()) {
                if let AssemblerEvent::FrameReady(frame) = ev {
                    return Ok(frame);
                }
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
        let rx_sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let rx_addr = rx_sock.local_addr().unwrap();
        let tx_sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();

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
            // 10% deterministic packet loss (drop every 10th datagram), protected by 20% RS-FEC
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
}
