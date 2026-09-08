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
    let value = json!({"schema_version":1,"architecture":"linux-arm64","source_build":false,
        "minimum_free_bytes":0,"fabric_connectivity":"none","fabric_minimum_mbps":0,"mandatory_capabilities":[]});
    assert!(serde_json::from_value::<RuntimePreflightRequest>(value.clone()).is_ok());
    for invalid in [json!(true), json!(1.0), json!(2)] {
        let mut value = value.clone();
        value["schema_version"] = invalid;
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
fn scalar_union_preserves_tokens_and_rejects_integer_and_float_overflow() {
    use vonk_agent_protocol::generated::TelemetrySeries;
    let fixture = include_str!("../../../../agent_protocol/fixtures/typify-telemetry-series.json");
    let original: Value = serde_json::from_str(fixture).unwrap();
    let parsed: TelemetrySeries = serde_json::from_str(fixture).unwrap();
    assert_eq!(parsed.value, original["value"]);
    for token in [
        "9223372036854775808",
        "-9223372036854775809",
        "1e400",
        "-1e400",
    ] {
        let mut document = original.clone();
        document["value"] = serde_json::from_str(token).unwrap();
        assert!(
            serde_json::from_value::<TelemetrySeries>(document).is_err(),
            "{token}"
        );
    }
    for token in ["1.0", "9.223372036854776e18", "1e300", "-1e300"] {
        let mut document = original.clone();
        document["value"] = serde_json::from_str(token).unwrap();
        let parsed: TelemetrySeries = serde_json::from_value(document.clone()).unwrap();
        assert_eq!(parsed.value, document["value"]);
    }
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
