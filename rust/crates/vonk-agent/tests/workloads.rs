#![forbid(unsafe_code)]

use std::path::Path;

use serde_json::{Value, json};
use vonk_agent::workloads::{CompiledExecutionPlan, WorkloadError, materialized_model_path};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap()
}

fn plan(value: Value) -> CompiledExecutionPlan {
    serde_json::from_value(value).unwrap()
}

#[test]
fn compiled_plan_round_trips_the_persisted_schema2_document() {
    let value = fixture();
    let parsed = plan(value.clone());
    parsed.validate().unwrap();
    assert_eq!(serde_json::to_value(parsed).unwrap(), value);
}

#[test]
fn retired_authorities_and_unknown_fields_are_rejected() {
    for field in [
        "model_version_sha256",
        "runtime_distribution_sha256",
        "patch_bundle_sha256",
    ] {
        let mut value = fixture();
        value["identity"][field] = json!("a".repeat(64));
        assert!(
            serde_json::from_value::<CompiledExecutionPlan>(value).is_err(),
            "{field}"
        );
    }
    let mut value = fixture();
    value["artifacts"][0]["repository"] = json!("upstream/authority");
    assert!(serde_json::from_value::<CompiledExecutionPlan>(value).is_err());
}

#[test]
fn repeated_identity_and_receipt_tampering_is_rejected() {
    let mut value = fixture();
    value["runtime"]["image_digest"] = json!(format!("sha256:{}", "9".repeat(64)));
    let parsed = plan(value);
    assert!(matches!(
        parsed.validate(),
        Err(WorkloadError::Invalid("compiled execution identity"))
    ));

    let mut value = fixture();
    value["artifacts"][0]["distribution_object"]["bytes"] = json!(8);
    let parsed = plan(value);
    assert!(matches!(
        parsed.validate(),
        Err(WorkloadError::Invalid("compiled model artifact"))
    ));
}

#[test]
fn security_and_rank_topology_are_bound_to_the_compiled_plan() {
    let mut value = fixture();
    value["security"]["privileged"] = json!(true);
    assert!(plan(value).validate().is_err());

    let mut value = fixture();
    value["topology"]["rank"] = json!(1);
    let parsed = plan(value);
    assert!(matches!(
        parsed.validate(),
        Err(WorkloadError::Invalid("compiled execution identity"))
    ));
}

#[test]
fn model_materialization_keeps_nested_paths_selection_scoped() {
    let parsed = plan(fixture());
    let primary =
        materialized_model_path(Path::new("/run/vonk/models"), &parsed.artifacts[0]).unwrap();
    let draft =
        materialized_model_path(Path::new("/run/vonk/models"), &parsed.artifacts[1]).unwrap();
    assert_eq!(primary, Path::new("/run/vonk/models/primary/config.json"));
    assert_eq!(
        draft,
        Path::new("/run/vonk/models/dependency-qwen3-8-27b-dspark-b3c99101/config.json")
    );
    assert_ne!(primary, draft);
}

#[test]
fn empty_support_file_is_valid_but_empty_weights_are_rejected() {
    let mut value = fixture();
    value["identity"]["model_artifact_bytes"] = json!(2448);
    let artifact = &mut value["artifacts"][0];
    artifact["file_id"] = json!("tokenizer-config");
    artifact["path"] = json!("tokenizer_config.json");
    artifact["sha256"] = json!(vonk_agent::workloads::EMPTY_SHA256);
    artifact["size_bytes"] = json!(0);
    artifact["roles"] = json!(["tokenizer"]);
    artifact["distribution_object"] = json!({
        "name": "tokenizer_config.json",
        "sha256": vonk_agent::workloads::EMPTY_SHA256,
        "bytes": 0,
        "kind": "model"
    });
    let parsed = plan(value.clone());
    parsed.validate().unwrap();
    value["artifacts"][0]["roles"] = json!(["weights"]);
    assert!(plan(value).validate().is_err());
}

#[test]
fn opaque_argv_values_are_preserved_without_flag_interpretation() {
    let mut value = fixture();
    let json_value = format!("{{\"payload\":\"{}\"}}", "x".repeat(4_090));
    let unicode = "🙂".repeat(16_384);
    value["runtime"]["argv"] = json!(["--network", json_value, "", unicode, "--device"]);
    let parsed = plan(value.clone());
    parsed.validate().unwrap();
    assert_eq!(
        serde_json::to_value(parsed).unwrap()["runtime"]["argv"],
        value["runtime"]["argv"]
    );
}
