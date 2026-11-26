//! GCC/TWCC-inspired Adaptive Bitrate (ABR) and dynamic FEC controller.
//!
//! Reacts immediately to delay-gradient queue buildup (even before packet loss occurs)
//! and supports a dedicated DERP-relay smooth pacing profile.

use brioche_proto::ControlMessage;

#[derive(Debug, Clone, Copy)]
pub struct AbrLimits {
    pub min_bitrate_kbps: u32,
    pub max_bitrate_kbps: u32,
    pub derp_relay: bool,
}

impl Default for AbrLimits {
    fn default() -> Self {
        Self {
            min_bitrate_kbps: 2_000,
            max_bitrate_kbps: 60_000,
            derp_relay: false,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct QualityDecision {
    pub bitrate_kbps: u32,
    pub fec_percent: u8,
}

pub struct CongestionController {
    limits: AbrLimits,
    bitrate_kbps: u32,
    fec_percent: u8,
    ewma_loss: f64,
    ewma_gradient_us: f64,
    stable_ticks: u32,
}

impl CongestionController {
    pub fn new(initial_bitrate_kbps: u32, limits: AbrLimits) -> Self {
        let clamped = initial_bitrate_kbps.clamp(limits.min_bitrate_kbps, limits.max_bitrate_kbps);
        Self {
            limits,
            bitrate_kbps: clamped,
            fec_percent: if limits.derp_relay { 15 } else { 10 },
            ewma_loss: 0.0,
            ewma_gradient_us: 0.0,
            stable_ticks: 0,
        }
    }

    pub fn set_derp_relay(&mut self, derp_relay: bool) {
        self.limits.derp_relay = derp_relay;
        if derp_relay {
            self.bitrate_kbps = self.bitrate_kbps.min(18_000);
            self.fec_percent = self.fec_percent.max(15);
        }
    }

    pub fn current(&self) -> QualityDecision {
        QualityDecision {
            bitrate_kbps: self.bitrate_kbps,
            fec_percent: self.fec_percent,
        }
    }

    /// Process a 200ms TWCC feedback report and return the updated target bitrate & FEC ratio.
    pub fn on_feedback(&mut self, loss_ppm: u32, delay_gradient_us: i64) -> QualityDecision {
        let loss = (loss_ppm as f64) / 1_000_000.0;
        self.ewma_loss = self.ewma_loss * 0.65 + loss * 0.35;
        self.ewma_gradient_us = self.ewma_gradient_us * 0.6 + (delay_gradient_us as f64) * 0.4;

        let cap = if self.limits.derp_relay {
            self.limits.max_bitrate_kbps.min(20_000)
        } else {
            self.limits.max_bitrate_kbps
        };

        // Overuse or significant loss -> immediate multiplicative decrease
        if self.ewma_loss > 0.05 || self.ewma_gradient_us > 6_000.0 {
            self.stable_ticks = 0;
            let next = ((self.bitrate_kbps as f64) * 0.78) as u32;
            self.bitrate_kbps = next.clamp(self.limits.min_bitrate_kbps, cap);
            self.fec_percent = if self.ewma_loss > 0.08 { 25 } else { 20 };
        } else if self.ewma_loss > 0.015 || self.ewma_gradient_us > 2_500.0 {
            self.stable_ticks = 0;
            let next = ((self.bitrate_kbps as f64) * 0.90) as u32;
            self.bitrate_kbps = next.clamp(self.limits.min_bitrate_kbps, cap);
            self.fec_percent = 15;
        } else {
            // Healthy link -> additive/gentle ramp up
            self.stable_ticks += 1;
            if self.stable_ticks >= 3 {
                let step = ((self.bitrate_kbps as f64) * 0.08).max(600.0) as u32;
                self.bitrate_kbps = (self.bitrate_kbps + step).clamp(self.limits.min_bitrate_kbps, cap);
            }
            self.fec_percent = if self.ewma_loss < 0.002 && !self.limits.derp_relay {
                5
            } else {
                10
            };
        }

        self.current()
    }

    pub fn to_control_message(&self, max_fps: u32) -> ControlMessage {
        ControlMessage::QualityTarget {
            bitrate_kbps: self.bitrate_kbps,
            fec_percent: self.fec_percent,
            max_fps,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backs_off_on_queue_delay_and_ramps_up_when_clean() {
        let mut cc = CongestionController::new(20_000, AbrLimits::default());
        let d1 = cc.on_feedback(0, 9_000);
        // EWMA gradient after 1 sample = 3600 > 2500 -> mild backoff
        assert!(d1.bitrate_kbps < 20_000);
        let d2 = cc.on_feedback(80_000, 12_000);
        assert!(d2.bitrate_kbps < d1.bitrate_kbps);
        assert!(d2.fec_percent >= 20);

        let low = d2.bitrate_kbps;
        for _ in 0..8 {
            cc.on_feedback(0, -100);
        }
        assert!(cc.current().bitrate_kbps > low);
        assert_eq!(cc.current().fec_percent, 5);
    }
}
