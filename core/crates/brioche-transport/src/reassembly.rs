//! Zero-head-of-line-blocking multi-stripe video frame assembler with automatic RS-FEC
//! reconstruction (supports arbitrary 4K/8K YUV 4:4:4 IDR frames up to 22 MB),
//! selective NACK suggestions, and PLI escalation on unrecoverable loss.

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use brioche_proto::{PacketHeader, PacketKind};

use crate::fec::{join_shards_into_frame, reconstruct_data};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AssembledFrame {
    pub stream_id: u16,
    pub frame_id: u32,
    pub flags: u8,
    pub capture_ts_us: u64,
    pub recovered_via_fec: bool,
    pub payload: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AssemblerEvent {
    FrameReady(AssembledFrame),
    RequestNack {
        stream_id: u16,
        frame_id: u32,
        /// Encoded as (stripe_idx << 8) | shard_idx
        missing_shards: Vec<u16>,
    },
    RequestPli {
        stream_id: u16,
        dropped_frame_id: u32,
    },
}

struct PendingStripe {
    data_shards: usize,
    fec_shards: usize,
    shards: Vec<Option<Vec<u8>>>,
    received_count: usize,
    completed: Option<(Vec<u8>, bool)>,
}

struct PendingFrame {
    stream_id: u16,
    flags: u8,
    capture_ts_us: u64,
    total_stripes: usize,
    stripes: Vec<Option<PendingStripe>>,
    completed_stripes: usize,
    first_seen: Instant,
    nack_sent: bool,
}

pub struct FrameAssembler {
    stream_id: u16,
    last_emitted_frame: Option<u32>,
    max_pending_frames: usize,
    max_frame_wait: Duration,
    pending: BTreeMap<u32, PendingFrame>,
    pub stats_frames_ok: u64,
    pub stats_frames_fec_recovered: u64,
    pub stats_frames_dropped: u64,
}

impl FrameAssembler {
    pub fn new(stream_id: u16) -> Self {
        Self {
            stream_id,
            last_emitted_frame: None,
            max_pending_frames: 6,
            max_frame_wait: Duration::from_millis(45),
            pending: BTreeMap::new(),
            stats_frames_ok: 0,
            stats_frames_fec_recovered: 0,
            stats_frames_dropped: 0,
        }
    }

    pub fn reset_stream(&mut self, stream_id: u16) {
        self.stream_id = stream_id;
        self.last_emitted_frame = None;
        self.pending.clear();
    }

    pub fn push_shard(
        &mut self,
        header: PacketHeader,
        payload: Vec<u8>,
        now: Instant,
    ) -> Vec<AssemblerEvent> {
        let mut events = Vec::new();
        if !matches!(header.kind, PacketKind::VideoData | PacketKind::VideoFec) {
            return events;
        }
        if header.stream_id != self.stream_id {
            if header.stream_id > self.stream_id {
                self.reset_stream(header.stream_id);
            } else {
                return events;
            }
        }
        if let Some(last) = self.last_emitted_frame {
            if header.frame_id <= last {
                return events;
            }
        }

        let total_stripes = header.total_stripes as usize;
        let stripe_idx = header.stripe_idx as usize;
        let k = header.data_shards as usize;
        let m = header.fec_shards as usize;
        let total = k + m;
        let shard_idx = header.shard_idx as usize;
        if total_stripes == 0 || stripe_idx >= total_stripes || k == 0 || shard_idx >= total {
            return events;
        }

        let frame = self.pending.entry(header.frame_id).or_insert_with(|| PendingFrame {
            stream_id: header.stream_id,
            flags: header.flags,
            capture_ts_us: header.capture_ts_us,
            total_stripes,
            stripes: (0..total_stripes).map(|_| None).collect(),
            completed_stripes: 0,
            first_seen: now,
            nack_sent: false,
        });

        if frame.total_stripes != total_stripes {
            return events;
        }

        let stripe = frame.stripes[stripe_idx].get_or_insert_with(|| PendingStripe {
            data_shards: k,
            fec_shards: m,
            shards: vec![None; total],
            received_count: 0,
            completed: None,
        });

        if stripe.completed.is_none()
            && stripe.shards.len() == total
            && stripe.shards[shard_idx].is_none()
        {
            stripe.shards[shard_idx] = Some(payload);
            stripe.received_count += 1;

            if stripe.received_count >= stripe.data_shards {
                let had_missing_data = stripe.shards[..stripe.data_shards]
                    .iter()
                    .any(|s| s.is_none());
                if let Ok(data_shards) =
                    reconstruct_data(&stripe.shards, stripe.data_shards, stripe.fec_shards)
                {
                    if let Ok(stripe_bytes) = join_shards_into_frame(&data_shards) {
                        stripe.completed = Some((stripe_bytes, had_missing_data));
                        frame.completed_stripes += 1;
                    }
                }
            }
        }

        if frame.completed_stripes == frame.total_stripes {
            let done = self.pending.remove(&header.frame_id).unwrap();
            let mut full_payload = Vec::new();
            let mut any_fec = false;
            for st in done.stripes.into_iter().flatten() {
                if let Some((bytes, used_fec)) = st.completed {
                    full_payload.extend_from_slice(&bytes);
                    any_fec |= used_fec;
                }
            }

            let stale: Vec<u32> = self
                .pending
                .keys()
                .copied()
                .filter(|&fid| fid < header.frame_id)
                .collect();
            for stale_id in stale {
                self.pending.remove(&stale_id);
                self.stats_frames_dropped += 1;
                if !header.is_idr() {
                    events.push(AssemblerEvent::RequestPli {
                        stream_id: self.stream_id,
                        dropped_frame_id: stale_id,
                    });
                }
            }

            self.last_emitted_frame = Some(header.frame_id);
            self.stats_frames_ok += 1;
            if any_fec {
                self.stats_frames_fec_recovered += 1;
            }
            events.push(AssemblerEvent::FrameReady(AssembledFrame {
                stream_id: done.stream_id,
                frame_id: header.frame_id,
                flags: done.flags,
                capture_ts_us: done.capture_ts_us,
                recovered_via_fec: any_fec,
                payload: full_payload,
            }));
        }

        while self.pending.len() > self.max_pending_frames {
            if let Some((&oldest_id, _)) = self.pending.iter().next() {
                self.pending.remove(&oldest_id);
                self.stats_frames_dropped += 1;
                events.push(AssemblerEvent::RequestPli {
                    stream_id: self.stream_id,
                    dropped_frame_id: oldest_id,
                });
            }
        }

        events
    }

    pub fn poll_maintenance(&mut self, now: Instant, rtt_ms: f64) -> Vec<AssemblerEvent> {
        let mut events = Vec::new();
        let mut expired = Vec::new();

        for (&frame_id, pf) in self.pending.iter_mut() {
            let age = now.saturating_duration_since(pf.first_seen);
            if age >= self.max_frame_wait {
                expired.push(frame_id);
            } else if !pf.nack_sent && rtt_ms < 25.0 && age >= Duration::from_millis(6) {
                let mut missing = Vec::new();
                for (s_idx, st_opt) in pf.stripes.iter().enumerate() {
                    if let Some(st) = st_opt {
                        if st.completed.is_none() {
                            for (i, s) in st.shards[..st.data_shards].iter().enumerate() {
                                if s.is_none() {
                                    missing.push(((s_idx as u16) << 8) | (i as u16));
                                }
                            }
                        }
                    }
                }
                if !missing.is_empty() {
                    pf.nack_sent = true;
                    events.push(AssemblerEvent::RequestNack {
                        stream_id: self.stream_id,
                        frame_id,
                        missing_shards: missing,
                    });
                }
            }
        }

        for frame_id in expired {
            self.pending.remove(&frame_id);
            self.stats_frames_dropped += 1;
            events.push(AssemblerEvent::RequestPli {
                stream_id: self.stream_id,
                dropped_frame_id: frame_id,
            });
        }
        events
    }
}
