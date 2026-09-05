use std::path::Path;

use serde_json::{Value, json};
use vonk_agent::workloads::{CompiledExecutionPlan, WorkloadError, materialized_model_path};

fn plan() -> Value {
    json!({
        "schema_version": 2,
        "recipe_revision_sha256": "a".repeat(64),
        "harness_sha256": "b".repeat(64),
        "execution_sha256": "c".repeat(64),
        "model_artifact_set_sha256": "d".repeat(64),
        "model_artifact_set_bytes": 7,
        "artifacts": [{
            "id": "config",
            "selection_id": "primary",
            "file_id": "config",
            "path": "text_encoder/config.json",
            "sha256": "e".repeat(64),
            "bytes": 7,
            "roles": ["entrypoint"],
            "mount": {
                "source": "/run/vonk/models/primary",
                "target": "/models",
                "read_only": true
            },
            "materialized_path": "/run/vonk/models/primary/text_encoder/config.json",
            "model": {
                "publisher": "vonk-forge",
                "slug": "synthetic-model",
                "content_sha256": "f".repeat(64)
            },
            "distribution_object": {
                "name": "text_encoder/config.json",
                "sha256": "e".repeat(64),
                "bytes": 7,
                "kind": "model"
            }
        }],
        "runtime_image": {
            "image_digest": format!("sha256:{}", "1".repeat(64)),
            "oci_layout_sha256": "2".repeat(64),
            "image_bytes": 4096,
            "architecture": "linux-arm64",
            "runtime_interface": "vonk.runtime.v1",
            "source": "published",
            "build_id": null,
            "distribution_object": {
                "name": "image.oci.tar",
                "sha256": "2".repeat(64),
                "bytes": 4096,
                "kind": "oci-archive"
            }
        }
    })
}

#[test]
fn python_receipt_shape_round_trips_and_validates() {
    let parsed: CompiledExecutionPlan = serde_json::from_value(plan()).unwrap();
    parsed.validate().unwrap();
    let round_trip = serde_json::to_value(parsed).unwrap();
    assert_eq!(
        round_trip["artifacts"][0]["materialized_path"],
        "/run/vonk/models/primary/text_encoder/config.json"
    );
    assert!(round_trip["artifacts"][0].get("repository").is_none());
}

#[test]
fn materialization_preserves_nested_model_path() {
    let parsed: CompiledExecutionPlan = serde_json::from_value(plan()).unwrap();
    let path =
        materialized_model_path(Path::new("/run/vonk/models"), &parsed.artifacts[0]).unwrap();
    assert_eq!(
        path,
        Path::new("/run/vonk/models/primary/text_encoder/config.json")
    );
    assert_eq!(parsed.artifacts[0].mount.source, "/run/vonk/models/primary");
}

#[test]
fn empty_support_file_is_valid_but_empty_weight_is_rejected() {
    let mut value = plan();
    value["model_artifact_set_bytes"] = json!(0);
    let artifact = &mut value["artifacts"][0];
    artifact["path"] = json!("tokenizer_config.json");
    artifact["file_id"] = json!("tokenizer-config");
    artifact["id"] = json!("tokenizer-config");
    artifact["sha256"] = json!(vonk_agent::workloads::EMPTY_SHA256);
    artifact["bytes"] = json!(0);
    artifact["materialized_path"] = json!("/run/vonk/models/primary/tokenizer_config.json");
    artifact["distribution_object"]["name"] = json!("tokenizer_config.json");
    artifact["distribution_object"]["sha256"] = json!(vonk_agent::workloads::EMPTY_SHA256);
    artifact["distribution_object"]["bytes"] = json!(0);
    let parsed: CompiledExecutionPlan = serde_json::from_value(value.clone()).unwrap();
    parsed.validate().unwrap();

    value["artifacts"][0]["roles"] = json!(["weights"]);
    let parsed: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    assert!(matches!(
        parsed.validate(),
        Err(WorkloadError::Invalid("compiled model artifact"))
    ));
}

#[test]
fn retired_authority_and_duplicate_materialized_paths_are_rejected() {
    let mut value = plan();
    value["artifacts"][0]["repository"] = json!("huggingface/private");
    assert!(serde_json::from_value::<CompiledExecutionPlan>(value).is_err());

    let mut value = plan();
    let duplicate_artifact = value["artifacts"][0].clone();
    value["artifacts"]
        .as_array_mut()
        .unwrap()
        .push(duplicate_artifact);
    assert!(serde_json::from_value::<CompiledExecutionPlan>(value).is_ok());
    let parsed: CompiledExecutionPlan = serde_json::from_value(plan()).unwrap();
    let mut duplicate = serde_json::to_value(parsed).unwrap();
    let duplicate_artifact = duplicate["artifacts"][0].clone();
    duplicate["artifacts"]
        .as_array_mut()
        .unwrap()
        .push(duplicate_artifact);
    let parsed: CompiledExecutionPlan = serde_json::from_value(duplicate).unwrap();
    assert!(parsed.validate().is_err());
}

#[test]
fn model_files_with_the_same_relative_name_remain_selection_scoped() {
    let mut value = plan();
    value["model_artifact_set_bytes"] = json!(14);
    value["artifacts"][0]["path"] = json!("config.json");
    value["artifacts"][0]["materialized_path"] = json!("/run/vonk/models/primary/config.json");
    value["artifacts"][0]["model"]["slug"] = json!("qwen3-8-27b");
    value["artifacts"][0]["distribution_object"]["name"] = json!("config.json");
    let mut second = value["artifacts"][0].clone();
    second["id"] = json!("secondary-config");
    second["selection_id"] = json!("secondary");
    second["file_id"] = json!("config");
    second["path"] = json!("config.json");
    second["sha256"] = json!("1".repeat(64));
    second["materialized_path"] = json!("/run/vonk/models/secondary/config.json");
    second["model"]["slug"] = json!("qwen3-8-flash-next");
    second["model"]["content_sha256"] = json!("2".repeat(64));
    second["distribution_object"]["name"] = json!("config.json");
    second["distribution_object"]["sha256"] = json!("1".repeat(64));
    value["artifacts"].as_array_mut().unwrap().push(second);
    let parsed: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    parsed.validate().unwrap();
    assert_ne!(
        parsed.artifacts[0].materialized_path,
        parsed.artifacts[1].materialized_path
    );
}

#[test]
fn controller_built_image_receipt_requires_its_build_identity() {
    let mut value = plan();
    value["runtime_image"]["source"] = json!("controller-build");
    value["runtime_image"]["build_id"] = json!("build-7");
    let parsed: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
    parsed.validate().unwrap();

    let mut invalid = serde_json::to_value(parsed).unwrap();
    invalid["runtime_image"]["build_id"] = Value::Null;
    let parsed: CompiledExecutionPlan = serde_json::from_value(invalid).unwrap();
    assert!(parsed.validate().is_err());
}
