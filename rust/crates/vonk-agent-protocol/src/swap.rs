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
mod tests {
    use super::*;
    use crate::generated::{ApplySwapOperation, HostOperationKind, SwapObservation};
    use crate::{HostHelperOperation, canonical_json, parse_strict};

    #[test]
    fn python_vectors_bind_identical_sizes_in_the_signed_helper_operation() {
        let vectors: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/swap-v1.json"
        ))
        .unwrap();
        for value in vectors["valid"].as_array().unwrap() {
            let desired: SwapIntent = parse_strict(&serde_json::to_vec(value).unwrap()).unwrap();
            assert!(valid_intent(&desired));
            let operation = HostHelperOperation::ApplySwapOperation(ApplySwapOperation {
                type_: HostOperationKind::ApplySwap.as_str().to_owned(),
                desired,
            });
            operation.validate().unwrap();
            let bytes = canonical_json(&operation).unwrap();
            let parsed: HostHelperOperation = parse_strict(&bytes).unwrap();
            parsed.validate().unwrap();
            assert_eq!(canonical_json(&parsed).unwrap(), bytes);
        }
        for value in vectors["invalid"].as_array().unwrap() {
            let parsed = parse_strict::<SwapIntent>(&serde_json::to_vec(value).unwrap());
            assert!(
                parsed.is_err() || !valid_intent(&parsed.unwrap()),
                "accepted {value}"
            );
        }
        let observation: SwapObservation =
            parse_strict(&serde_json::to_vec(&vectors["observation"]).unwrap()).unwrap();
        assert_eq!(observation.total_bytes, 64 * GIB);
    }

    #[test]
    fn usable_swap_matches_the_python_vectors() {
        let vectors: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/swap-v1.json"
        ))
        .unwrap();
        let usable = vectors["usable"].as_array().unwrap();
        assert!(!usable.is_empty());
        for pair in usable {
            assert_eq!(
                usable_swap_bytes(pair["total_bytes"].as_u64().unwrap()),
                pair["usable_bytes"].as_u64().unwrap(),
                "{pair}"
            );
        }
    }

    #[test]
    fn an_operation_outside_the_policy_is_refused_before_any_effect() {
        let policy = policy();
        for size_gib in [
            0,
            u32::from(policy.minimum_gib) - 1,
            u32::from(policy.maximum_gib) + 1,
        ] {
            let operation = HostHelperOperation::ApplySwapOperation(ApplySwapOperation {
                type_: HostOperationKind::ApplySwap.as_str().to_owned(),
                desired: SwapIntent {
                    generation: 1,
                    size_gib,
                },
            });
            assert!(operation.validate().is_err(), "accepted {size_gib} GiB");
        }
    }
}
