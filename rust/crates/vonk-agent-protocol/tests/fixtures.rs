#![forbid(unsafe_code)]

use std::{collections::BTreeMap, fs, path::PathBuf};

use serde_json::Value;
use vonk_agent_protocol::{
    AgentClaim, AgentResult, EnrollmentRequest, HostHelperOperation, canonical_generated_json,
    canonical_json, hex_sha256, parse_strict,
};

#[test]
fn sha256_digest_is_lowercase_hex() {
    assert_eq!(
        hex_sha256(b"abc"),
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
}

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../agent_protocol/fixtures")
}

#[test]
fn canonical_fixtures_match_python_manifest() {
    let root = fixtures();
    let manifest: BTreeMap<String, String> =
        serde_json::from_slice(&fs::read(root.join("manifest.json")).unwrap()).unwrap();
    for (name, expected) in manifest {
        let raw = fs::read(root.join(&name)).unwrap();
        let raw = raw.strip_suffix(b"\n").unwrap_or(&raw);
        let value: Value = parse_strict(raw).unwrap();
        assert_eq!(canonical_json(&value).unwrap(), raw, "{name}");
        assert_eq!(hex_sha256(raw), expected, "{name}");
    }
}

#[test]
fn strict_rust_types_parse_shared_messages() {
    let root = fixtures();
    let claim: AgentClaim =
        parse_strict(&fs::read(root.join("operation-poll.json")).unwrap()).unwrap();
    claim.validate().unwrap();
    let result: AgentResult =
        parse_strict(&fs::read(root.join("operation-result.json")).unwrap()).unwrap();
    result.validate().unwrap();
    let enrollment: EnrollmentRequest =
        parse_strict(&fs::read(root.join("enrollment-request.json")).unwrap()).unwrap();
    let _ = enrollment.evidence;
}

#[test]
fn signed_messages_reject_unknown_fields() {
    let mut value: Value =
        serde_json::from_slice(&fs::read(fixtures().join("operation-poll.json")).unwrap()).unwrap();
    value
        .as_object_mut()
        .unwrap()
        .insert("shell".into(), Value::String("no".into()));
    assert!(parse_strict::<AgentClaim>(&canonical_json(&value).unwrap()).is_err());
}

#[test]
fn peer_secret_vector_round_trips_and_is_a_start_only_field() {
    // The vector the Python tests read: a Start grant operation with and
    // without the per-job secret. Without it the bytes are what they always were.
    let vector: Value = serde_json::from_slice(
        &fs::read(fixtures().join("host-helper-peer-secret-grant.json")).unwrap(),
    )
    .unwrap();
    for key in ["without", "with"] {
        let raw = canonical_json(&vector[key]).unwrap();
        let operation: HostHelperOperation = parse_strict(&raw).unwrap();
        operation.validate().unwrap();
        assert_eq!(canonical_generated_json(&operation).unwrap(), raw, "{key}");
        let HostHelperOperation::ExecuteContainerRuntimeRequestOperation(parsed) = &operation
        else {
            panic!("the vector is a runtime request");
        };
        assert_eq!(parsed.peer_secret.is_some(), key == "with");
        // Debug is what a log line or an error would print.
        assert!(!format!("{operation:?}").contains("Gj3LimhJaTIm5nvdTza8cCMCE9Bclfa_3nOuDUgoGfM"));
    }
    for refused in [
        serde_json::json!({"peer_secret": "short"}),
        serde_json::json!({"action": "stop", "start_plan_sha256": null, "stop_plan_sha256": "d".repeat(64)}),
    ] {
        let mut changed = vector["with"].clone();
        for (name, value) in refused.as_object().unwrap() {
            if value.is_null() {
                changed.as_object_mut().unwrap().remove(name);
            } else {
                changed[name] = value.clone();
            }
        }
        let valid = parse_strict::<HostHelperOperation>(&canonical_json(&changed).unwrap())
            .is_ok_and(|operation| operation.validate().is_ok());
        assert!(!valid, "{changed}");
    }
}
