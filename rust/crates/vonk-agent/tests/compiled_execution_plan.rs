use std::path::Path;

use serde_json::{Value, json};
use vonk_agent::workloads::{CompiledExecutionPlan, WorkloadError, materialized_model_path};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap()
}

#[test]
fn generated_python_workload_fixture_round_trips_through_rust() {
    let value = fixture();
    let plan: CompiledExecutionPlan = serde_json::from_value(value.clone()).unwrap();
    plan.validate().unwrap();
    assert_eq!(plan.schema_version, 2);
    assert_eq!(plan.runtime.executable, "/opt/vonk/bin/vllm");
    assert_eq!(plan.artifacts.len(), 2);
    assert_eq!(plan.artifacts[0].path, "config.json");
    assert_eq!(plan.artifacts[1].path, "config.json");
    assert_ne!(plan.artifacts[0].model, plan.artifacts[1].model);
    assert_eq!(
        plan.runtime.argv[2],
        "value with spaces; {\"mode\": [\"$\", \"μ\"]}"
    );
    assert_eq!(serde_json::to_value(plan).unwrap(), value);
}

#[test]
fn materialized_paths_remain_selection_scoped() {
    let plan: CompiledExecutionPlan = serde_json::from_value(fixture()).unwrap();
    let primary =
        materialized_model_path(Path::new("/run/vonk/models"), &plan.artifacts[0]).unwrap();
    let draft = materialized_model_path(Path::new("/run/vonk/models"), &plan.artifacts[1]).unwrap();
    assert_eq!(primary, Path::new("/run/vonk/models/primary/config.json"));
    assert_eq!(draft, Path::new("/run/vonk/models/draft/config.json"));
    assert_ne!(primary, draft);
}

#[test]
fn duplicate_selection_file_or_materialized_path_is_rejected() {
    let mut value = fixture();
    let duplicate = value["artifacts"][0].clone();
    value["artifacts"] = serde_json::json!([duplicate.clone(), duplicate]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(matches!(
        plan.validate(),
        Err(WorkloadError::Invalid("compiled model artifact identity"))
    ));
}

#[test]
fn valid_empty_support_file_is_admitted_and_empty_weight_is_rejected() {
    let mut value = fixture();
    value["identity"]["model_artifact_bytes"] = serde_json::json!(0);
    let artifact = &mut value["artifacts"][0];
    artifact["file_id"] = serde_json::json!("tokenizer-config");
    artifact["path"] = serde_json::json!("tokenizer_config.json");
    artifact["sha256"] = serde_json::json!(vonk_agent::workloads::EMPTY_SHA256);
    artifact["size_bytes"] = serde_json::json!(0);
    artifact["roles"] = serde_json::json!(["tokenizer"]);
    artifact["distribution_object"] = serde_json::json!({
        "name": "tokenizer_config.json",
        "sha256": vonk_agent::workloads::EMPTY_SHA256,
        "bytes": 0,
        "kind": "model"
    });
    value["artifacts"] = serde_json::json!([artifact.clone()]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value.clone()).unwrap();
    plan.validate().unwrap();

    value["artifacts"][0]["roles"] = serde_json::json!(["weights"]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(matches!(
        plan.validate(),
        Err(WorkloadError::Invalid("compiled model artifact"))
    ));
}

#[test]
fn retired_upstream_authority_is_rejected_by_strict_serde() {
    let mut value = fixture();
    value["artifacts"][0]["repository"] = serde_json::json!("huggingface/private");
    assert!(serde_json::from_value::<CompiledExecutionPlan>(value).is_err());
}

#[test]
fn opaque_argv_preserves_large_json_and_unicode_byte_boundaries() {
    let mut value = fixture();
    let compact_json = format!("{{\"payload\":\"{}\"}}", "x".repeat(4_090));
    assert!(compact_json.len() > 4_096);
    assert!(compact_json.len() <= 65_536);
    let exact_unicode = "🙂".repeat(16_384);
    assert_eq!(exact_unicode.len(), 65_536);
    value["runtime"]["argv"] = json!(["serve", compact_json, exact_unicode]);

    let plan: CompiledExecutionPlan = serde_json::from_value(value.clone()).unwrap();
    plan.validate().unwrap();
    assert_eq!(serde_json::to_value(plan).unwrap(), value);
}

#[test]
fn opaque_argv_rejects_nul_and_token_or_total_overflow() {
    let mut value = fixture();
    value["runtime"]["argv"] = json!([format!("{}x", "🙂".repeat(16_384))]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value.clone()).unwrap();
    assert!(plan.validate().is_err());

    value["runtime"]["argv"] = json!(["value\u{0000}"]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value.clone()).unwrap();
    assert!(plan.validate().is_err());

    value["runtime"]["argv"] = json!(vec!["x".repeat(65_536); 17]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(plan.validate().is_err());
}
