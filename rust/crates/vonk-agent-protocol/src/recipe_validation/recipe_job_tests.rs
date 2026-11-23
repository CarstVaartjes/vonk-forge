#![cfg(test)]
use super::*;

fn request() -> RecipeJobRunRequest {
    let inputs = vec![RecipeJobInputFile {
        slot: "input".to_owned(),
        name: "input.mp4".to_owned(),
        media_type: "video/mp4".to_owned(),
        size_bytes: 123,
        sha256: "a".repeat(64),
    }];
    let manifest = serde_json::json!({
        "schema_version": 1,
        "total_bytes": 123,
        "files": inputs,
    });
    RecipeJobRunRequest {
            output_delivery_only: None,
            job_id: Uuid::new_v4(),
            run_id: Uuid::new_v4(),
            installation_id: Uuid::new_v4(),
            recipe_revision_id: Uuid::new_v4(),
            mapping_id: Uuid::new_v4(),
            run_generation: 1,
            plan_digest: "d".repeat(64),
            input_manifest_sha256: hex_sha256(&canonical_json(&manifest).unwrap()),
            input_total_bytes: 123,
            inputs,
            compiled_execution_plan: serde_json::from_value(serde_json::from_str::<Value>(include_str!("../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json")).unwrap()["payload"]["compiled_execution_plan"].clone()).unwrap(),
            output_mappings: vec![RecipeJobOutputMapping {
                slot: "video".to_owned(),
                media_type: "video/mp4".to_owned(),
                extensions: vec![".mp4".to_owned()],
            }],
            output_limits: RecipeJobOutputLimits {
                max_files: 32,
                max_file_bytes: 1024 * 1024,
                max_total_bytes: 2 * 1024 * 1024,
                allowed_media_types: vec!["video/mp4".to_owned()],
            },
        }
}

#[test]
fn job_contract_binds_canonical_inputs_and_requires_plan_object() {
    let valid = request();
    assert!(validate_recipe_job(&valid));

    let mut cross_manifest = valid.clone();
    cross_manifest.inputs[0].name = "other.mp4".to_owned();
    assert!(!validate_recipe_job(&cross_manifest));

    let mut traversal = valid.clone();
    traversal.inputs[0].name = "../input.mp4".to_owned();
    assert!(!validate_recipe_job(&traversal));

    let mut invalid_slot = valid.clone();
    invalid_slot.inputs[0].slot = "0input".to_owned();
    assert!(!validate_recipe_job(&invalid_slot));

    let mut reserved_manifest = valid.clone();
    reserved_manifest.inputs[0].name = "manifest.json".to_owned();
    let manifest = serde_json::json!({
        "schema_version": 1,
        "total_bytes": reserved_manifest.input_total_bytes,
        "files": reserved_manifest.inputs,
    });
    reserved_manifest.input_manifest_sha256 = hex_sha256(&canonical_json(&manifest).unwrap());
    assert!(!validate_recipe_job(&reserved_manifest));

    let mut command = serde_json::to_value(valid).unwrap();
    command["compiled_execution_plan"] = Value::Null;
    assert!(serde_json::from_value::<RecipeJobRunRequest>(command).is_err());
}

#[test]
fn output_mapping_contract_is_sorted_exact_and_collision_free() {
    let mut valid = request();
    valid.output_mappings = vec![
        RecipeJobOutputMapping {
            slot: "custom".to_owned(),
            media_type: "application/vnd.vonk.custom".to_owned(),
            extensions: vec![".vonk.bin".to_owned()],
        },
        RecipeJobOutputMapping {
            slot: "document".to_owned(),
            media_type: "application/pdf".to_owned(),
            extensions: vec![".pdf".to_owned()],
        },
        RecipeJobOutputMapping {
            slot: "fallback".to_owned(),
            media_type: "application/octet-stream".to_owned(),
            extensions: vec![".bin".to_owned()],
        },
        RecipeJobOutputMapping {
            slot: "image".to_owned(),
            media_type: "image/avif".to_owned(),
            extensions: vec![".avif".to_owned()],
        },
    ];
    valid.output_limits.allowed_media_types = vec![
        "application/octet-stream".to_owned(),
        "application/pdf".to_owned(),
        "application/vnd.vonk.custom".to_owned(),
        "image/avif".to_owned(),
    ];
    assert!(validate_recipe_job(&valid));

    let mut collision = valid.clone();
    collision.output_mappings[3].extensions = vec![".pdf".to_owned()];
    assert!(!validate_recipe_job(&collision));

    let mut undeclared_limit = valid.clone();
    undeclared_limit.output_limits.allowed_media_types = vec!["video/mp4".to_owned()];
    assert!(!validate_recipe_job(&undeclared_limit));

    let mut uppercase = valid;
    uppercase.output_mappings[1].extensions = vec![".PDF".to_owned()];
    assert!(!validate_recipe_job(&uppercase));
}

#[test]
fn python_job_claim_and_result_vectors_have_rust_parity() {
    let claim: AgentClaim = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    claim.validate().unwrap();
    let parsed = RecipeOperationRequest::parse(&claim).unwrap();
    let RecipeOperationRequest::JobRun(request) = parsed else {
        panic!("job vector parsed as the wrong operation");
    };
    assert_eq!(
        request.input_manifest_sha256,
        "a3fa3ff4a07e23b945e72cda963e6aaf24671bc52642d328180b0ea4cde1776d"
    );

    let result: AgentResult = serde_json::from_str(include_str!(
            "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
        ))
        .unwrap();
    result.validate().unwrap();
    let generated::AgentResultResult::RecipeJobRunResult(typed) = result.result else {
        panic!("expected typed job result")
    };
    typed.validate().unwrap();
    assert_eq!(
        typed.output_manifest.manifest_sha256,
        "9f7781fb8415bc1cb9e835fe4bcc9c8dd8f45f6a6b333f0e0550e812db1da9cd"
    );

    let mut unavailable_peak = typed;
    unavailable_peak.evidence.peak_memory_bytes = None;
    unavailable_peak.validate().unwrap();
    assert!(
        serde_json::to_value(unavailable_peak).unwrap()["evidence"]["peak_memory_bytes"].is_null()
    );
}

#[test]
fn cancelled_is_a_typed_terminal_agent_result_state() {
    let result = AgentResult {
        fence: Uuid::new_v4(),
        result: serde_json::from_value(
            serde_json::json!({"reason": "controller cancellation requested"}),
        )
        .unwrap(),
        state: "cancelled".parse().unwrap(),
    };

    result.validate().unwrap();
}
