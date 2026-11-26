#![cfg(test)]
use super::*;
use crate::generated::{ApplySwapOperation, HostOperationKind, SwapObservation};
use crate::{HostHelperOperation, canonical_json, parse_strict};

#[test]
fn python_vectors_bind_identical_sizes_in_the_signed_helper_operation() {
    let vectors: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/swap-v1.json"
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
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/swap-v1.json"
    ))
    .unwrap();
    let usable = vectors["usable"].as_array().unwrap();
    assert!(!usable.is_empty(), "{usable:?}");
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
