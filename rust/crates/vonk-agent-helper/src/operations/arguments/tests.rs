#![cfg(test)]

use super::*;

#[test]
fn runtime_environment_values_are_bounded_but_not_charset_restricted() {
    // Break caught: a helper-only 4 KiB limit rejects values accepted by
    // the canonical 64 KiB UTF-8 environment contract.
    assert!(super::valid_environment(&format!(
        "PROMPT={}",
        "x".repeat(8192)
    )));
    assert!(super::valid_environment(&format!(
        "ANY_NAME={}",
        "x".repeat(MAX_ENVIRONMENT_VALUE_BYTES)
    )));
    assert!(!super::valid_environment(&format!(
        "ANY_NAME={}",
        "x".repeat(MAX_ENVIRONMENT_VALUE_BYTES + 1)
    )));
    assert!(!super::valid_environment("ANY_NAME=bad\0value"));
    // Values carry Controller-signed plan data: arbitrary content stays valid.
    assert!(super::valid_environment(
        "ANY_NAME=--flag; $(echo) with\nnewlines and spaces"
    ));
}

#[test]
fn runtime_environment_names_keep_engine_case_and_stay_shell_safe() {
    for accepted in ["NCCL_DEBUG=INFO", "RAY_memory_usage_threshold=0.99", "A1="] {
        assert!(super::valid_environment(accepted), "{accepted}");
    }
    for rejected in [
        "lowercase=1",
        "_LEADING=1",
        "HAS-DASH=1",
        "HAS SPACE=1",
        "=1",
        "NO_EQUALS",
    ] {
        assert!(!super::valid_environment(rejected), "{rejected}");
    }
}
