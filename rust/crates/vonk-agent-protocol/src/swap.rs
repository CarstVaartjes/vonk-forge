//! The swap policy shared by the agent and the privileged helper.
use crate::generated::{SwapIntent, SwapPolicy};

pub const GIB: u64 = 1024 * 1024 * 1024;
/// The helper's swap checkpoint, published read-only for the agent.
pub const OBSERVATION_FILE: &str = "swap-observation.json";
/// The agent's wait for one helper swap pass: create (fallocate, mkswap,
/// swapon at 30 s each) and release (swapoff at 20 s) in the worst case.
pub const SWAP_PASS_SECONDS: u64 = 120;

include!(concat!(env!("OUT_DIR"), "/swap_policy.rs"));

pub fn policy() -> SwapPolicy {
    SWAP_POLICY.clone()
}

pub fn default_intent() -> SwapIntent {
    SwapIntent {
        generation: 0,
        size_gib: u32::from(SWAP_POLICY.default_gib),
    }
}

pub fn valid_intent(intent: &SwapIntent) -> bool {
    (u32::from(SWAP_POLICY.minimum_gib)..=u32::from(SWAP_POLICY.maximum_gib))
        .contains(&intent.size_gib)
}

pub fn desired_bytes(intent: &SwapIntent) -> u64 {
    u64::from(intent.size_gib) * GIB
}

/// A shortfall below this is only swap-area header pages.
pub fn tolerance_bytes() -> u64 {
    SWAP_POLICY.tolerance_mib * 1024 * 1024
}

/// The swap a load may fill before the host memory guard's R1 or R2 applies;
/// the same arithmetic as `vonk_agent_protocol.swap.usable_swap_bytes`.
pub fn usable_swap_bytes(total: u64) -> u64 {
    let kept_free = (u64::from(SWAP_POLICY.exhausted_free_floor_gib) * GIB)
        .max(total / 100 * u64::from(SWAP_POLICY.exhausted_free_percent));
    total
        .saturating_sub(kept_free)
        .min(total / 100 * u64::from(SWAP_POLICY.filling_used_percent))
}

#[cfg(test)]
mod tests;
