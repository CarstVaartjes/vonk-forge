use super::*;
use crate::generated::{ApplyGpuClockOperation, GpuClockIntent};
use crate::{HostHelperOperation, canonical_json, parse_strict};

#[test]
fn python_vectors_bind_identical_ranges_in_signed_helper_and_run_start_messages() {
    let vectors: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/gpu-clock-v1.json"
    ))
    .unwrap();
    for value in vectors["valid"].as_array().unwrap() {
        let range: GpuClockRange = parse_strict(&serde_json::to_vec(value).unwrap()).unwrap();
        assert!(valid_range(&range));
        let desired = GpuClockIntent {
            generation: 1,
            range,
        };
        let operation = HostHelperOperation::ApplyGpuClockOperation(ApplyGpuClockOperation {
            type_: crate::generated::HostOperationKind::ApplyGpuClock
                .as_str()
                .to_owned(),
            desired: desired.clone(),
        });
        operation.validate().unwrap();
        let bytes = canonical_json(&operation).unwrap();
        let parsed: HostHelperOperation = parse_strict(&bytes).unwrap();
        parsed.validate().unwrap();
        assert_eq!(canonical_json(&parsed).unwrap(), bytes);
        let mut start = crate::recipe_start_tests::valid_start_plan();
        start.gpu_clock = Some(desired);
        let encoded = canonical_json(&start).unwrap();
        let decoded: crate::RecipeStartRequest = parse_strict(&encoded).unwrap();
        assert_eq!(decoded.gpu_clock, start.gpu_clock);
    }
    for value in vectors["invalid"].as_array().unwrap() {
        let parsed = parse_strict::<GpuClockRange>(&serde_json::to_vec(value).unwrap());
        assert!(
            parsed.is_err() || !valid_range(&parsed.unwrap()),
            "accepted {value}"
        );
    }
}
