//! Systematic Cauchy Reed-Solomon erasure coding over GF(2^8) (polynomial 0x11D).
//!
//! Zero external native dependencies; recovers any `<= M` missing shards out of `K + M`
//! in sub-millisecond time so video frames survive UDP packet loss with 0 RTT stall.

use thiserror::Error;

const GF_POLY: u16 = 0x11D; // x^8 + x^4 + x^3 + x^2 + 1

#[derive(Debug, Error)]
pub enum FecError {
    #[error("invalid shard configuration: data={0}, parity={1}")]
    InvalidConfig(usize, usize),
    #[error("insufficient shards for recovery: have {have}, need {need}")]
    InsufficientShards { have: usize, need: usize },
    #[error("inconsistent shard length")]
    LengthMismatch,
}

struct GfTables {
    exp: [u8; 512],
    log: [u8; 256],
}

fn gf_tables() -> &'static GfTables {
    use std::sync::OnceLock;
    static TABLES: OnceLock<GfTables> = OnceLock::new();
    TABLES.get_or_init(|| {
        let mut exp = [0u8; 512];
        let mut log = [0u8; 256];
        let mut x: u16 = 1;
        for i in 0..255usize {
            exp[i] = x as u8;
            log[x as usize] = i as u8;
            x <<= 1;
            if x & 0x100 != 0 {
                x ^= GF_POLY;
            }
        }
        for i in 255..512usize {
            exp[i] = exp[i - 255];
        }
        GfTables { exp, log }
    })
}

#[inline]
fn gf_mul(a: u8, b: u8) -> u8 {
    if a == 0 || b == 0 {
        return 0;
    }
    let t = gf_tables();
    t.exp[t.log[a as usize] as usize + t.log[b as usize] as usize]
}

#[inline]
fn gf_inv(a: u8) -> u8 {
    debug_assert_ne!(a, 0);
    let t = gf_tables();
    t.exp[255 - t.log[a as usize] as usize]
}

#[inline]
fn mul_add_slice(dst: &mut [u8], src: &[u8], coeff: u8) {
    if coeff == 0 {
        return;
    }
    if coeff == 1 {
        for (d, s) in dst.iter_mut().zip(src.iter()) {
            *d ^= *s;
        }
        return;
    }
    let t = gf_tables();
    let log_c = t.log[coeff as usize] as usize;
    for (d, &s) in dst.iter_mut().zip(src.iter()) {
        if s != 0 {
            *d ^= t.exp[t.log[s as usize] as usize + log_c];
        }
    }
}

/// Cauchy parity coefficient: 1 / ((k + row) ^ col), guaranteed MDS invertible for k + m <= 255.
#[inline]
fn cauchy_coeff(k: usize, parity_row: usize, data_col: usize) -> u8 {
    let x = (k + parity_row) as u8;
    let y = data_col as u8;
    gf_inv(x ^ y)
}

/// Encode `K` equal-length data shards into `M` parity shards.
pub fn encode_parity(data_shards: &[Vec<u8>], parity_count: usize) -> Result<Vec<Vec<u8>>, FecError> {
    let k = data_shards.len();
    if k == 0 || k + parity_count > 255 {
        return Err(FecError::InvalidConfig(k, parity_count));
    }
    if parity_count == 0 {
        return Ok(Vec::new());
    }
    let shard_len = data_shards[0].len();
    if data_shards.iter().any(|s| s.len() != shard_len) {
        return Err(FecError::LengthMismatch);
    }
    let mut parity = vec![vec![0u8; shard_len]; parity_count];
    for (p_idx, p_shard) in parity.iter_mut().enumerate() {
        for (d_idx, d_shard) in data_shards.iter().enumerate() {
            let coeff = cauchy_coeff(k, p_idx, d_idx);
            mul_add_slice(p_shard, d_shard, coeff);
        }
    }
    Ok(parity)
}

/// Reconstruct all `K` data shards given `K + M` optional shards (at least `K` present).
pub fn reconstruct_data(
    shards: &[Option<Vec<u8>>],
    data_count: usize,
    parity_count: usize,
) -> Result<Vec<Vec<u8>>, FecError> {
    if data_count == 0 || data_count + parity_count > 255 || shards.len() != data_count + parity_count {
        return Err(FecError::InvalidConfig(data_count, parity_count));
    }
    // Fast path: all data shards already present!
    if shards[..data_count].iter().all(|s| s.is_some()) {
        return Ok(shards[..data_count]
            .iter()
            .map(|s| s.as_ref().unwrap().clone())
            .collect());
    }

    let present_indices: Vec<usize> = shards
        .iter()
        .enumerate()
        .filter_map(|(i, s)| s.as_ref().map(|_| i))
        .take(data_count)
        .collect();
    if present_indices.len() < data_count {
        return Err(FecError::InsufficientShards {
            have: present_indices.len(),
            need: data_count,
        });
    }
    let shard_len = shards[present_indices[0]].as_ref().unwrap().len();
    for &idx in &present_indices {
        if shards[idx].as_ref().unwrap().len() != shard_len {
            return Err(FecError::LengthMismatch);
        }
    }

    // Build K x K submatrix M from the encoding matrix rows corresponding to `present_indices`,
    // then solve M * X = Y via Gauss-Jordan elimination over GF(2^8).
    let k = data_count;
    let mut mat = vec![vec![0u8; k]; k];
    let mut rhs: Vec<Vec<u8>> = Vec::with_capacity(k);
    for (row_i, &shard_idx) in present_indices.iter().enumerate() {
        if shard_idx < k {
            mat[row_i][shard_idx] = 1;
        } else {
            let p_row = shard_idx - k;
            for col in 0..k {
                mat[row_i][col] = cauchy_coeff(k, p_row, col);
            }
        }
        rhs.push(shards[shard_idx].as_ref().unwrap().clone());
    }

    for col in 0..k {
        let pivot = (col..k)
            .find(|&r| mat[r][col] != 0)
            .ok_or(FecError::InvalidConfig(data_count, parity_count))?;
        if pivot != col {
            mat.swap(pivot, col);
            rhs.swap(pivot, col);
        }
        let inv = gf_inv(mat[col][col]);
        if inv != 1 {
            for j in col..k {
                mat[col][j] = gf_mul(mat[col][j], inv);
            }
            for byte in rhs[col].iter_mut() {
                *byte = gf_mul(*byte, inv);
            }
        }
        let pivot_row_mat = mat[col].clone();
        let pivot_row_rhs = rhs[col].clone();
        for r in 0..k {
            if r == col {
                continue;
            }
            let factor = mat[r][col];
            if factor != 0 {
                for j in col..k {
                    mat[r][j] ^= gf_mul(factor, pivot_row_mat[j]);
                }
                mul_add_slice(&mut rhs[r], &pivot_row_rhs, factor);
            }
        }
    }

    Ok(rhs)
}

/// Split an arbitrary video frame into equal-sized data shards (prefixing 4-byte exact length).
pub fn split_frame_into_shards(
    frame: &[u8],
    max_shard_payload: usize,
    fec_percent: u8,
) -> Result<(Vec<Vec<u8>>, Vec<Vec<u8>>), FecError> {
    let total_bytes = 4 + frame.len();
    let max_data_shards = 200usize;
    let mut data_count = (total_bytes + max_shard_payload - 1) / max_shard_payload;
    data_count = data_count.clamp(1, max_data_shards);
    let shard_len = (total_bytes + data_count - 1) / data_count;
    if shard_len > max_shard_payload {
        return Err(FecError::InvalidConfig(data_count, 0));
    }

    let mut framed = Vec::with_capacity(data_count * shard_len);
    framed.extend_from_slice(&(frame.len() as u32).to_be_bytes());
    framed.extend_from_slice(frame);
    framed.resize(data_count * shard_len, 0);

    let data_shards: Vec<Vec<u8>> = framed
        .chunks_exact(shard_len)
        .map(|c| c.to_vec())
        .collect();

    let parity_count = if fec_percent == 0 {
        0
    } else {
        (((data_count * fec_percent as usize) + 99) / 100)
            .max(1)
            .min(255 - data_count)
    };
    let parity_shards = encode_parity(&data_shards, parity_count)?;
    Ok((data_shards, parity_shards))
}

/// Recombine recovered data shards back into the exact original frame bytes.
pub fn join_shards_into_frame(data_shards: &[Vec<u8>]) -> Result<Vec<u8>, FecError> {
    let total: usize = data_shards.iter().map(|s| s.len()).sum();
    if total < 4 {
        return Err(FecError::LengthMismatch);
    }
    let mut merged = Vec::with_capacity(total);
    for shard in data_shards {
        merged.extend_from_slice(shard);
    }
    let orig_len = u32::from_be_bytes([merged[0], merged[1], merged[2], merged[3]]) as usize;
    if 4 + orig_len > merged.len() {
        return Err(FecError::LengthMismatch);
    }
    Ok(merged[4..4 + orig_len].to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recovers_multiple_lost_data_shards_with_zero_error() {
        let frame: Vec<u8> = (0..18_731).map(|i| ((i * 73 + 19) & 0xFF) as u8).collect();
        let (data, parity) = split_frame_into_shards(&frame, 1200, 25).unwrap();
        let k = data.len();
        let m = parity.len();
        assert!(m >= 4);

        let mut received: Vec<Option<Vec<u8>>> = data
            .into_iter()
            .chain(parity.into_iter())
            .map(Some)
            .collect();
        // Drop `m` arbitrary shards (mix of data and parity)
        received[0] = None;
        received[3] = None;
        received[k - 1] = None;
        received[k] = None;

        let recovered = reconstruct_data(&received, k, m).unwrap();
        let rebuilt = join_shards_into_frame(&recovered).unwrap();
        assert_eq!(rebuilt, frame);
    }
}
