#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn compiled_plan_parser_rejects_malformed_or_unsafe_mounts() {
    let mut value: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    assert!(parse_compiled_execution_plan(value.to_string().as_bytes()).is_ok());
    value["security"]["mounts"][0]["target"] = json!("/etc");
    assert!(parse_compiled_execution_plan(value.to_string().as_bytes()).is_err());
    value["security"]["mounts"][0]["target"] = json!("/models");
    value["runtime"].as_object_mut().unwrap().remove("argv");
    assert!(parse_compiled_execution_plan(value.to_string().as_bytes()).is_err());
}
