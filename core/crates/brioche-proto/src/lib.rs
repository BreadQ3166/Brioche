//! Brioche v3 low-latency media & control protocol.
//!
//! Wire layout for UDP datagrams:
//! ```text
//! [0..2]   magic ("BR")
//! [2]      version (3)
//! [3]      packet_kind (VideoData=1, VideoFec=2, AudioOpus=3, Nack=4, Pli=5, Twcc=6, Ping=7, Pong=8)
//! [4..6]   stream_id (u16 BE)
//! [6..10]  frame_id (u32 BE)
//! [10..12] shard_idx (u16 BE)
//! [12]     data_shards (u8)
//! [13]     fec_shards (u8)
//! [14..16] flags (u16 BE: bit0=IDR, bit1=HEVC, bit2=AV1, bit3=YUV444)
//! [16..24] capture_ts_us (u64 BE)
//! [24..32] seq_num (u64 BE, unique monotonic per-session AEAD nonce counter)
//! [32..]   AEAD ciphertext (payload + 16-byte Poly1305 tag), AAD = bytes[0..32]
//! ```

use chacha20poly1305::aead::{Aead, KeyInit, Payload};
use chacha20poly1305::{ChaCha20Poly1305, Nonce};
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const MAGIC: [u8; 2] = *b"BR";
pub const PROTO_VERSION: u8 = 3;
pub const HEADER_LEN: usize = 32;
pub const AEAD_TAG_LEN: usize = 16;
pub const MAX_DATAGRAM_LEN: usize = 1400;
pub const MAX_SHARD_PAYLOAD: usize = MAX_DATAGRAM_LEN - HEADER_LEN - AEAD_TAG_LEN;

pub const FLAG_IDR: u16 = 1 << 0;
pub const FLAG_HEVC: u16 = 1 << 1;
pub const FLAG_AV1: u16 = 1 << 2;
pub const FLAG_YUV444: u16 = 1 << 3;

#[derive(Debug, Error)]
pub enum ProtoError {
    #[error("datagram too short: {0} bytes")]
    TooShort(usize),
    #[error("invalid protocol magic or version")]
    InvalidHeader,
    #[error("unknown packet kind: {0}")]
    UnknownKind(u8),
    #[error("invalid shard metadata")]
    InvalidShardMeta,
    #[error("packet authentication failed")]
    AuthFailed,
    #[error("replayed or out-of-window sequence number: {0}")]
    ReplayDetected(u64),
    #[error("payload too large: {0} bytes")]
    PayloadTooLarge(usize),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum PacketKind {
    VideoData = 1,
    VideoFec = 2,
    AudioOpus = 3,
    Nack = 4,
    Pli = 5,
    Twcc = 6,
    Ping = 7,
    Pong = 8,
}

impl TryFrom<u8> for PacketKind {
    type Error = ProtoError;
    fn try_from(value: u8) -> Result<Self, Self::Error> {
        match value {
            1 => Ok(Self::VideoData),
            2 => Ok(Self::VideoFec),
            3 => Ok(Self::AudioOpus),
            4 => Ok(Self::Nack),
            5 => Ok(Self::Pli),
            6 => Ok(Self::Twcc),
            7 => Ok(Self::Ping),
            8 => Ok(Self::Pong),
            other => Err(ProtoError::UnknownKind(other)),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PacketHeader {
    pub kind: PacketKind,
    pub stream_id: u16,
    pub frame_id: u32,
    pub shard_idx: u16,
    pub data_shards: u8,
    pub fec_shards: u8,
    pub flags: u16,
    pub capture_ts_us: u64,
    pub seq_num: u64,
}

impl PacketHeader {
    pub fn encode(&self, out: &mut [u8; HEADER_LEN]) {
        out[0..2].copy_from_slice(&MAGIC);
        out[2] = PROTO_VERSION;
        out[3] = self.kind as u8;
        out[4..6].copy_from_slice(&self.stream_id.to_be_bytes());
        out[6..10].copy_from_slice(&self.frame_id.to_be_bytes());
        out[10..12].copy_from_slice(&self.shard_idx.to_be_bytes());
        out[12] = self.data_shards;
        out[13] = self.fec_shards;
        out[14..16].copy_from_slice(&self.flags.to_be_bytes());
        out[16..24].copy_from_slice(&self.capture_ts_us.to_be_bytes());
        out[24..32].copy_from_slice(&self.seq_num.to_be_bytes());
    }

    pub fn decode(buf: &[u8]) -> Result<Self, ProtoError> {
        if buf.len() < HEADER_LEN {
            return Err(ProtoError::TooShort(buf.len()));
        }
        if buf[0..2] != MAGIC || buf[2] != PROTO_VERSION {
            return Err(ProtoError::InvalidHeader);
        }
        let kind = PacketKind::try_from(buf[3])?;
        let stream_id = u16::from_be_bytes([buf[4], buf[5]]);
        let frame_id = u32::from_be_bytes([buf[6], buf[7], buf[8], buf[9]]);
        let shard_idx = u16::from_be_bytes([buf[10], buf[11]]);
        let data_shards = buf[12];
        let fec_shards = buf[13];
        let flags = u16::from_be_bytes([buf[14], buf[15]]);
        let capture_ts_us = u64::from_be_bytes(buf[16..24].try_into().unwrap());
        let seq_num = u64::from_be_bytes(buf[24..32].try_into().unwrap());
        if matches!(kind, PacketKind::VideoData | PacketKind::VideoFec) {
            let total = data_shards as u16 + fec_shards as u16;
            if data_shards == 0 || total == 0 || shard_idx >= total {
                return Err(ProtoError::InvalidShardMeta);
            }
        }
        Ok(Self {
            kind,
            stream_id,
            frame_id,
            shard_idx,
            data_shards,
            fec_shards,
            flags,
            capture_ts_us,
            seq_num,
        })
    }

    #[inline]
    pub fn is_idr(&self) -> bool {
        (self.flags & FLAG_IDR) != 0
    }
}

/// 128-bit sliding anti-replay window for UDP datagrams.
#[derive(Debug, Default)]
pub struct ReplayWindow {
    highest: u64,
    initialized: bool,
    bitmap: u128,
}

impl ReplayWindow {
    pub fn check_and_record(&mut self, seq: u64) -> bool {
        if !self.initialized {
            self.initialized = true;
            self.highest = seq;
            self.bitmap = 1;
            return true;
        }
        if seq > self.highest {
            let shift = seq - self.highest;
            self.bitmap = if shift >= 128 { 0 } else { self.bitmap << shift };
            self.bitmap |= 1;
            self.highest = seq;
            true
        } else {
            let diff = self.highest - seq;
            if diff >= 128 {
                return false;
            }
            let mask = 1u128 << diff;
            if (self.bitmap & mask) != 0 {
                return false;
            }
            self.bitmap |= mask;
            true
        }
    }
}

/// Authenticated AEAD packet sealer/opener bound to a 32-byte session key + direction salt.
pub struct PacketCipher {
    cipher: ChaCha20Poly1305,
    direction_salt: [u8; 4],
}

impl PacketCipher {
    pub fn new(key: &[u8; 32], direction_salt: [u8; 4]) -> Self {
        Self {
            cipher: ChaCha20Poly1305::new(key.into()),
            direction_salt,
        }
    }

    fn nonce(&self, seq: u64) -> Nonce {
        let mut raw = [0u8; 12];
        raw[0..4].copy_from_slice(&self.direction_salt);
        raw[4..12].copy_from_slice(&seq.to_be_bytes());
        *Nonce::from_slice(&raw)
    }

    pub fn seal(&self, header: &PacketHeader, plaintext: &[u8]) -> Result<Vec<u8>, ProtoError> {
        if plaintext.len() > MAX_SHARD_PAYLOAD {
            return Err(ProtoError::PayloadTooLarge(plaintext.len()));
        }
        let mut hdr_bytes = [0u8; HEADER_LEN];
        header.encode(&mut hdr_bytes);
        let nonce = self.nonce(header.seq_num);
        let ct = self
            .cipher
            .encrypt(
                &nonce,
                Payload {
                    msg: plaintext,
                    aad: &hdr_bytes,
                },
            )
            .map_err(|_| ProtoError::AuthFailed)?;
        let mut out = Vec::with_capacity(HEADER_LEN + ct.len());
        out.extend_from_slice(&hdr_bytes);
        out.extend_from_slice(&ct);
        Ok(out)
    }

    pub fn open(
        &self,
        datagram: &[u8],
        replay: Option<&mut ReplayWindow>,
    ) -> Result<(PacketHeader, Vec<u8>), ProtoError> {
        if datagram.len() < HEADER_LEN + AEAD_TAG_LEN {
            return Err(ProtoError::TooShort(datagram.len()));
        }
        let header = PacketHeader::decode(&datagram[..HEADER_LEN])?;
        let nonce = self.nonce(header.seq_num);
        let pt = self
            .cipher
            .decrypt(
                &nonce,
                Payload {
                    msg: &datagram[HEADER_LEN..],
                    aad: &datagram[..HEADER_LEN],
                },
            )
            .map_err(|_| ProtoError::AuthFailed)?;
        if let Some(window) = replay {
            if !window.check_and_record(header.seq_num) {
                return Err(ProtoError::ReplayDetected(header.seq_num));
            }
        }
        Ok((header, pt))
    }
}

/// Control plane messages carried over reliable QUIC Stream 0/1.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ControlMessage {
    StreamDescriptor {
        stream_id: u16,
        monitor_id: u32,
        width: u32,
        height: u32,
        fps_limit: u32,
        codec: String,
        chroma_444: bool,
    },
    SwitchMonitor {
        monitor_id: u32,
        request_id: u32,
    },
    CursorShape {
        hash: String,
        width: u16,
        height: u16,
        hotspot_x: u16,
        hotspot_y: u16,
        rgba_b64: String,
    },
    CursorPosition {
        x: f32,
        y: f32,
        visible: bool,
    },
    QualityTarget {
        bitrate_kbps: u32,
        fec_percent: u8,
        max_fps: u32,
    },
    TwccReport {
        loss_ppm: u32,
        delay_gradient_us: i64,
        rtt_us: u64,
        recv_bitrate_kbps: u32,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn seal_open_roundtrip_and_tamper_rejection() {
        let key = [0x42u8; 32];
        let cipher = PacketCipher::new(&key, *b"H2C\0");
        let mut window = ReplayWindow::default();
        let hdr = PacketHeader {
            kind: PacketKind::VideoData,
            stream_id: 2,
            frame_id: 99,
            shard_idx: 1,
            data_shards: 4,
            fec_shards: 2,
            flags: FLAG_IDR | FLAG_HEVC | FLAG_YUV444,
            capture_ts_us: 1_234_567_890,
            seq_num: 10,
        };
        let mut wire = cipher.seal(&hdr, b"nal-unit-payload").unwrap();
        let (decoded, pt) = cipher.open(&wire, Some(&mut window)).unwrap();
        assert_eq!(decoded, hdr);
        assert_eq!(pt, b"nal-unit-payload");

        // Replay must be rejected
        assert!(matches!(
            cipher.open(&wire, Some(&mut window)),
            Err(ProtoError::ReplayDetected(10))
        ));

        // Tampering with AAD header must fail AEAD authentication
        wire[6] ^= 0x01;
        assert!(matches!(
            cipher.open(&wire, Some(&mut window)),
            Err(ProtoError::AuthFailed)
        ));
    }
}
