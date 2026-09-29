use serde_json::{Value, json};
use vonk_agent_protocol::generated::{
    FailureDiagnostics, FailureLogTail, OperationProgress, RuntimePreflightRequest,
};

#[test]
fn required_nullable_fields_reject_missing_and_accept_explicit_null() {
    let value = json!({"text":"", "truncated":false, "dropped_bytes":null, "dropped_lines":null});
    let parsed: FailureLogTail = serde_json::from_value(value.clone()).unwrap();
    assert_eq!(serde_json::to_value(parsed).unwrap(), value);
    for name in ["dropped_bytes", "dropped_lines"] {
        let mut missing = value.clone();
        missing.as_object_mut().unwrap().remove(name);
        assert!(serde_json::from_value::<FailureLogTail>(missing).is_err());
    }
}

#[test]
fn direct_deserialization_validates_bounds_and_unknown_fields() {
    let value = json!({"text":"", "truncated":false, "dropped_bytes":null, "dropped_lines":null});
    let mut unknown = value.clone();
    unknown["unrecognized"] = json!(true);
    assert!(serde_json::from_value::<FailureLogTail>(unknown).is_err());
    let mut oversized = value;
    oversized["text"] = json!("x".repeat(2049));
    assert!(serde_json::from_value::<FailureLogTail>(oversized).is_err());
}

#[test]
fn literal_integers_do_not_accept_boolean_or_float_equivalents() {
    let value = json!({"source_build":false,
        "minimum_free_bytes":0,"fabric_connectivity":"none","fabric_minimum_mbps":0});
    assert!(serde_json::from_value::<RuntimePreflightRequest>(value.clone()).is_ok());
    for invalid in [json!(true), json!(1.5), json!(-1)] {
        let mut value = value.clone();
        value["fabric_minimum_mbps"] = invalid;
        assert!(serde_json::from_value::<RuntimePreflightRequest>(value).is_err());
    }
}

#[test]
fn canonical_default_values_are_materialized_without_hiding_required_fields() {
    let value = json!({"phase":"transfer"});
    let progress: OperationProgress = serde_json::from_value(value).unwrap();
    assert_eq!(progress.completed_bytes, 0);
    assert!(!progress.total_bytes_known);
    let mut diagnostics: Value = serde_json::from_str(include_str!(
        "../../../../agent_protocol/src/vonk_agent_protocol/vectors/failure-diagnostics-v1.json"
    ))
    .unwrap();
    diagnostics
        .as_object_mut()
        .unwrap()
        .remove("schema_version");
    let parsed: FailureDiagnostics = serde_json::from_value(diagnostics).unwrap();
    assert_eq!(parsed.schema_version, 1);
    assert!(serde_json::from_value::<OperationProgress>(json!({})).is_err());
}

#[test]
fn metadata_named_fields_keep_their_canonical_type() {
    use vonk_agent_protocol::generated::CompiledJobInputSlot;
    let fixture = json!({"id":"image","label":"Image","description":"input image","media_types":["image/png"],"extensions":[".png"],"min_files":0,"max_files":1,"max_file_bytes":1,"max_total_bytes":1});
    assert!(serde_json::from_value::<CompiledJobInputSlot>(fixture.clone()).is_ok());
    // The authoritative required fields drive this fixture, including bounds.
    let mut value = fixture.clone();
    value["description"] = json!({"unexpected":"object"});
    assert!(serde_json::from_value::<CompiledJobInputSlot>(value).is_err());
}

#[test]
fn optional_nulls_normalize_without_erasing_required_nulls_or_empty_defaults() {
    use vonk_agent_protocol::{canonical_generated_json, generated::CompiledJobInput};
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    let mut missing = fixture["payload"]["compiled_execution_plan"]["job"]["input"].clone();
    missing.as_object_mut().unwrap().remove("slots");
    let mut explicit = missing.clone();
    explicit["slots"] = Value::Null;
    let left: CompiledJobInput = serde_json::from_value(missing).unwrap();
    let right: CompiledJobInput = serde_json::from_value(explicit).unwrap();
    assert_eq!(
        canonical_generated_json(&left).unwrap(),
        canonical_generated_json(&right).unwrap()
    );
    let progress: OperationProgress = serde_json::from_value(json!({"phase":"transfer"})).unwrap();
    let encoded: Value =
        serde_json::from_slice(&canonical_generated_json(&progress).unwrap()).unwrap();
    assert_eq!(encoded["completed_bytes"], json!(0));
    assert_eq!(encoded["total_bytes_known"], json!(false));
    assert_eq!(encoded["members"], json!([]));
    assert!(encoded.get("kind").is_none());
    let engine: Value = serde_json::from_str(include_str!(
        "../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
    ))
    .unwrap();
    let plan: vonk_agent_protocol::generated::CompiledExecutionPlan =
        serde_json::from_value(engine.clone()).unwrap();
    let output: Value = serde_json::from_slice(&canonical_generated_json(&plan).unwrap()).unwrap();
    let extension =
        json!({"engine_null":null,"engine_false":false,"engine_zero":0,"engine_empty":[]});
    assert_eq!(
        serde_json::from_slice::<Value>(&vonk_agent_protocol::canonical_json(&extension).unwrap())
            .unwrap(),
        extension
    );
    assert_eq!(
        output["runtime"]["executable"],
        engine["runtime"]["executable"]
    );
}

#[test]
fn helper_response_uses_required_nulls_and_strict_optional_diagnostics() {
    use vonk_agent_protocol::{canonical_generated_json, generated::HostHelperResponse};
    let value = json!({"schema_version":1,"request_id":null,"status":"rejected"});
    let response: HostHelperResponse = serde_json::from_value(value.clone()).unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&canonical_generated_json(&response).unwrap()).unwrap(),
        value
    );
    let mut missing = value.clone();
    missing.as_object_mut().unwrap().remove("request_id");
    assert!(serde_json::from_value::<HostHelperResponse>(missing).is_err());
    for invalid in [json!(true), json!(-1), json!(256), json!(1.0)] {
        let mut invalid_response = value.clone();
        invalid_response["exit_code"] = invalid;
        assert!(serde_json::from_value::<HostHelperResponse>(invalid_response).is_err());
    }
}
