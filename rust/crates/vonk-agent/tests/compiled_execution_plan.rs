use std::path::Path;

use serde_json::{Value, json};
use vonk_agent::workloads::{
    CompiledExecutionPlan, MAX_COMPILED_EXECUTION_PLAN_MOUNTS, WorkloadError,
    materialized_model_path,
};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_workload_v2.json"
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
    assert_eq!(plan.artifacts.len(), 3);
    assert_eq!(plan.artifacts[0].path, "config.json");
    assert_eq!(plan.artifacts[1].path, "config.json");
    assert_ne!(plan.artifacts[0].model, plan.artifacts[1].model);
    assert_eq!(plan.artifacts[2].roles, ["entrypoint"]);
    assert!(
        plan.runtime
            .argv
            .contains(&"--served-model-name".to_owned())
    );
    assert_eq!(serde_json::to_value(plan).unwrap(), value);
}

#[test]
fn recipe_topology_vocabulary_and_engine_backends_survive_validation() {
    for mode in [
        "single",
        "distributed",
        "tensor_parallel",
        "pipeline_parallel",
        "data_parallel",
        "hybrid",
        "ray",
        "mpi",
    ] {
        for backend in ["tcp", "ucx", "future-engine-Δ"] {
            let mut value = fixture();
            value["topology"]["mode"] = json!(mode);
            value["topology"]["backend"] = json!(backend);
            let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
            plan.validate().unwrap();
            assert_eq!(plan.topology.mode, mode);
            assert_eq!(plan.topology.backend, backend);
        }
    }
    let mut value = fixture();
    value["topology"]["backend"] = json!("");
    assert!(
        serde_json::from_value::<CompiledExecutionPlan>(value)
            .unwrap()
            .validate()
            .is_err()
    );
}

#[test]
fn endpoint_and_job_are_required_one_of_wire_keys() {
    let mut value = fixture();
    value.as_object_mut().unwrap().remove("endpoint");
    assert!(serde_json::from_value::<CompiledExecutionPlan>(value).is_err());

    let mut value = fixture();
    value.as_object_mut().unwrap().remove("job");
    assert!(serde_json::from_value::<CompiledExecutionPlan>(value).is_err());

    let mut value = fixture();
    value["job"] = json!({
        "interface": "batch",
        "input": null,
        "output_path": "/outputs/result.json",
        "timeout_seconds": 30
    });
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(plan.validate().is_err());
}

#[test]
fn materialized_paths_remain_selection_scoped() {
    let plan: CompiledExecutionPlan = serde_json::from_value(fixture()).unwrap();
    let primary =
        materialized_model_path(Path::new("/run/vonk/models"), &plan.artifacts[0]).unwrap();
    let draft = materialized_model_path(Path::new("/run/vonk/models"), &plan.artifacts[1]).unwrap();
    assert_eq!(primary, Path::new("/run/vonk/models/primary/config.json"));
    assert_eq!(
        draft,
        Path::new("/run/vonk/models/dependency-qwen3-8-27b-dspark-b3c99101/config.json")
    );
    assert_ne!(primary, draft);
}

#[test]
fn canonical_unicode_space_and_max_length_model_identity_round_trip() {
    let mut value = fixture();
    let path = "模型 file_".repeat(64);
    assert_eq!(path.chars().count(), 512);
    let publisher = format!("发布者 {}", "_".repeat(124));
    assert_eq!(publisher.chars().count(), 128);
    value["artifacts"][0]["path"] = json!(path);
    value["artifacts"][0]["distribution_object"]["name"] = json!(path);
    value["artifacts"][0]["model"]["publisher"] = json!(publisher);

    let plan: CompiledExecutionPlan = serde_json::from_value(value.clone()).unwrap();
    plan.validate().unwrap();
    assert_eq!(plan.artifacts[0].path.chars().count(), 512);
    assert_eq!(plan.artifacts[0].model.publisher.chars().count(), 128);
    assert_eq!(serde_json::to_value(plan).unwrap(), value);
}

#[test]
fn unsafe_model_path_and_publisher_values_remain_rejected() {
    for path in [
        "/absolute",
        "../escape",
        "nested//file",
        "nested/./file",
        "nested/../file",
        "bad\\name",
        "bad\0name",
    ] {
        let mut value = fixture();
        value["artifacts"][0]["path"] = json!(path);
        value["artifacts"][0]["distribution_object"]["name"] = json!(path);
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        assert!(plan.validate().is_err(), "{path}");
    }

    let mut value = fixture();
    value["artifacts"][0]["model"]["publisher"] = json!("publisher\0");
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(plan.validate().is_err());

    let mut value = fixture();
    value["artifacts"][0]["model"]["publisher"] = json!("p".repeat(129));
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(plan.validate().is_err());
}

#[test]
fn duplicate_final_mount_target_is_rejected() {
    let mut value = fixture();
    let duplicate = value["artifacts"][0].clone();
    value["artifacts"] = serde_json::json!([duplicate.clone(), duplicate]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(matches!(
        plan.validate(),
        Err(WorkloadError::Invalid(
            "compiled model artifact mount target"
        ))
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

#[test]
fn canonical_multi_model_mount_projection_is_admitted() {
    let mut value = fixture();
    value["security"]["mounts"] = json!([
        {"source": "model", "target": "/models/target", "read_only": true},
        {"source": "model", "target": "/models/draft", "read_only": true},
        {"source": "model", "target": "/models/support", "read_only": true},
        {"source": "inputs", "target": "/inputs", "read_only": true},
        {"source": "outputs", "target": "/outputs", "read_only": false}
    ]);
    let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    plan.validate().unwrap();
}

#[test]
fn mount_source_and_target_policy_matches_python_matrix() {
    for (source, target, read_only) in [
        ("model", "/models", true),
        ("model", "/models/secondary", true),
        ("inputs", "/inputs", true),
        ("outputs", "/outputs", false),
    ] {
        let mut value = fixture();
        value["security"]["mounts"] = json!([{
            "source": source,
            "target": target,
            "read_only": read_only
        }]);
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        plan.validate().unwrap();
    }

    for (source, target, read_only) in [
        ("model", "/inputs", true),
        ("inputs", "/models", true),
        ("outputs", "/models", false),
    ] {
        let mut value = fixture();
        value["security"]["mounts"] = json!([{
            "source": source,
            "target": target,
            "read_only": read_only
        }]);
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        assert!(matches!(
            plan.validate(),
            Err(WorkloadError::Invalid("compiled security"))
        ));
    }
}

#[test]
fn mount_projection_rejects_over_duplicate_or_unsafe_targets() {
    let mut over = fixture();
    let mounts = over["security"]["mounts"].as_array_mut().unwrap();
    while mounts.len() <= MAX_COMPILED_EXECUTION_PLAN_MOUNTS {
        let index = mounts.len();
        mounts.push(json!({
            "source": "model",
            "target": format!("/models/extra-{index}"),
            "read_only": true
        }));
    }
    let plan: CompiledExecutionPlan = serde_json::from_value(over).unwrap();
    assert!(matches!(
        plan.validate(),
        Err(WorkloadError::Invalid("compiled security"))
    ));

    let mut duplicate = fixture();
    let first = duplicate["security"]["mounts"][0].clone();
    duplicate["security"]["mounts"]
        .as_array_mut()
        .unwrap()
        .push(first);
    let plan: CompiledExecutionPlan = serde_json::from_value(duplicate).unwrap();
    assert!(matches!(
        plan.validate(),
        Err(WorkloadError::Invalid("compiled security"))
    ));

    let mut unsafe_target = fixture();
    unsafe_target["security"]["mounts"][0]["target"] = json!("/models/../escape");
    let plan: CompiledExecutionPlan = serde_json::from_value(unsafe_target).unwrap();
    assert!(matches!(
        plan.validate(),
        Err(WorkloadError::Invalid("compiled security"))
    ));
}
