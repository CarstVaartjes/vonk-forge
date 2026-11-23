#![cfg(test)]
use crate::generated::FailureLogTail;

#[test]
fn union_shape_does_not_replace_named_authority_or_select_a_branch() {
    use crate::generated::{AgentClaimPayload, SparkApplyOperation};
    let valid = br#"{"installation_id":"11111111-1111-4111-8111-111111111111","plan_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#;
    assert!(matches!(
        crate::parse_strict::<AgentClaimPayload>(valid).unwrap(),
        AgentClaimPayload::RecipeReconcilePayload(_)
    ));
    // A missing required key cannot be supplied by an unrelated union
    // member's defaults. Forbidden extras cannot choose a tolerant member.
    let missing = br#"{"installation_id":"11111111-1111-4111-8111-111111111111"}"#;
    let extra = br#"{"installation_id":"11111111-1111-4111-8111-111111111111","plan_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","unexpected":true}"#;
    assert!(crate::parse_strict::<AgentClaimPayload>(missing).is_err());
    assert!(crate::parse_strict::<AgentClaimPayload>(extra).is_err());
    // Recover and Upgrade have identical root key shape. Full canonical
    // const validation, not a first surviving shape, owns the decision.
    let upgrade =
        crate::parse_strict::<SparkApplyOperation>(br#"{"operation":"upgrade"}"#).unwrap();
    assert_eq!(
        serde_json::to_value(upgrade).unwrap(),
        serde_json::json!({"operation":"upgrade"})
    );
    assert!(crate::parse_strict::<SparkApplyOperation>(br#"{"operation":"invented"}"#).is_err());
}

#[test]
fn generated_tail_rejects_private_number_objects_at_raw_json_boundary() {
    let raw = br#"{"text":"tail","truncated":false,"dropped_bytes":{"$serde_json::private::Number":"2"},"dropped_lines":null}"#;
    assert!(crate::parse_strict::<FailureLogTail>(raw).is_err());
}

#[test]
fn named_document_boundary_refuses_invalid_fields_before_typed_decoding() {
    // A generated named decoder must not gain an unchecked document from
    // its parser. This fails if the combined boundary skips its model.
    let invalid = br#"{"text":2,"truncated":false,"dropped_bytes":null,"dropped_lines":null}"#;
    let mut decoder = serde_json::Deserializer::from_slice(invalid);
    assert!(super::deserialize_wire_value(&mut decoder, Some("FailureLogTail")).is_err());
    // Directly constructed values use the same schema owner; serialization
    // alone does not establish a valid model.
    assert!(super::validate_constructed_document("FailureLogTail", &2_u64).is_err());
}

#[test]
fn scalar_integer_rejects_original_object_and_preserves_wide_number() {
    let spoof = br#"{"$serde_json::private::Number":"18446744073709551616"}"#;
    assert!(crate::parse_strict::<crate::integer::Integer>(spoof).is_err());
    let wide = "9".repeat(200);
    let value = crate::parse_strict::<crate::integer::Integer>(wide.as_bytes()).unwrap();
    assert_eq!(value.to_string(), wide);
    let zero = crate::parse_strict::<crate::integer::Integer>(b"-0").unwrap();
    assert_eq!(zero, crate::integer::Integer::from(0_u64));
    assert_eq!(zero.to_string(), "0");
    assert_eq!(serde_json::to_string(&zero).unwrap(), "0");
    for invalid in ["1.0", "1e0", "true", "null", "\"2\""] {
        assert!(crate::parse_strict::<crate::integer::Integer>(invalid.as_bytes()).is_err());
    }
}

#[test]
fn original_kind_is_preserved_for_nested_and_value_deserializers() {
    // The marker is a legitimate object key when an object is expected;
    // do not replace shape preservation with a global key blacklist.
    let raw = r#"{"nested":[{"$serde_json::private::Number":"18446744073709551616"}]}"#;
    let mut decoder = serde_json::Deserializer::from_str(raw);
    let value = super::deserialize_original_value(&mut decoder).unwrap().0;
    assert!(value["nested"][0].is_object());
    assert_eq!(
        value["nested"][0]["$serde_json::private::Number"].as_str(),
        Some("18446744073709551616")
    );
    let through_value = super::deserialize_original_value(value.clone()).unwrap().0;
    assert_eq!(through_value, value);

    let raw = r#"{"text":"tail","truncated":false,"dropped_bytes":18446744073709551615,"dropped_lines":null}"#;
    let tail = crate::parse_strict::<FailureLogTail>(raw.as_bytes()).unwrap();
    assert_eq!(tail.dropped_bytes, Some(u64::MAX));
    assert_eq!(
        crate::passthrough::revalidate::<FailureLogTail>(&tail).unwrap(),
        tail
    );
}
