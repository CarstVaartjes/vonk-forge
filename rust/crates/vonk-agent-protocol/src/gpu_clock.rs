//! Semantic checks and driver evidence shared by the agent and privileged helper.
use crate::generated::{GpuClockPolicy, GpuClockRange, GpuClockSupport};

pub const SUPPORT_QUERY: &str = "--query-supported-clocks=graphics";
pub const CSV_FORMAT: &str = "--format=csv,noheader,nounits";
pub const CHECKPOINT_FILE: &str = "gpu-clock.json";

include!(concat!(env!("OUT_DIR"), "/gpu_clock_policy.rs"));

pub fn policy() -> GpuClockPolicy {
    GPU_CLOCK_POLICY.clone()
}

pub fn default_range() -> GpuClockRange {
    let policy = policy();
    GpuClockRange {
        min_mhz: u32::try_from(policy.default_min_mhz).unwrap_or(u32::MAX),
        max_mhz: u32::try_from(policy.default_max_mhz).unwrap_or(u32::MAX),
    }
}

pub fn valid_range(range: &GpuClockRange) -> bool {
    range.min_mhz <= range.max_mhz && u64::from(range.max_mhz) <= policy().hard_max_mhz
}

pub fn supported_range(bytes: &[u8]) -> Option<GpuClockSupport> {
    let text = std::str::from_utf8(bytes).ok()?;
    if text.len() > 64 * 1024 {
        return None;
    }
    let mut levels = text
        .lines()
        .map(|line| line.trim().parse::<u32>().ok().filter(|n| *n > 0))
        .collect::<Option<Vec<_>>>()?;
    levels.sort_unstable();
    levels.dedup();
    Some(GpuClockSupport {
        min_mhz: *levels.first()?,
        max_mhz: *levels.last()?,
        levels_mhz: levels,
    })
}

#[cfg(test)]
mod tests;
