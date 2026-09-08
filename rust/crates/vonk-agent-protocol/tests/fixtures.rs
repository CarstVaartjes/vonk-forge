#![forbid(unsafe_code)]

use std::{collections::BTreeMap, fs, path::PathBuf};

use serde_json::Value;
use vonk_agent_protocol::{
    AgentClaim, AgentResult, EnrollmentRequest, canonical_json, hex_sha256, parse_strict,
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
    enrollment.evidence.validate().unwrap();
    assert_eq!(enrollment.evidence.node_id, claim.node_id);
}

#[test]
fn enrollment_evidence_rejects_invalid_observation_receipt_public_key() {
    let root = fixtures();
    let mut value: Value =
        serde_json::from_slice(&fs::read(root.join("enrollment-request.json")).unwrap()).unwrap();
    let mut missing = value.clone();
    missing["evidence"]
        .as_object_mut()
        .unwrap()
        .remove("observation_receipt_public_key");
    assert!(parse_strict::<EnrollmentRequest>(&canonical_json(&missing).unwrap()).is_err());
    value["evidence"]["observation_receipt_public_key"] = Value::String("A".repeat(64));
    assert!(parse_strict::<EnrollmentRequest>(&canonical_json(&value).unwrap()).is_err());
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
