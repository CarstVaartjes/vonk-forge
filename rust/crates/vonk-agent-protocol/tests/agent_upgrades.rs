#![forbid(unsafe_code)]

use chrono::DateTime;
use serde_json::{Value, json};
use uuid::Uuid;
use vonk_agent_protocol::{AgentClaim, AgentUpgradeRequest, canonical_json, hex_sha256};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

fn claim(payload: Value) -> AgentClaim {
    let payload_digest = hex_sha256(&canonical_json(&payload).unwrap());
    AgentClaim {
        attempt: 1,
        authority_revision: "a".repeat(64),
        deadline: DateTime::parse_from_rfc3339("2026-08-27T12:00:00+00:00").unwrap(),
        fence: Uuid::new_v4(),
        job_id: Uuid::new_v4(),
        node_id: NODE_ID.to_owned(),
        operation: "agent.upgrade.v1".to_owned(),
        operation_id: Uuid::new_v4(),
        payload,
        payload_digest,
        schema_version: 1,
    }
}

fn package() -> Value {
    json!({
        "architecture": "linux-arm64",
        "package_bytes": 5_000_000,
        "package_sha256": "b".repeat(64),
        "package_signature": "c".repeat(128),
        "package_url": "https://install.vonkforge.ai/artifacts/dev/releases/example/spark/current/linux-arm64/vonk-forge-agent.deb",
        "package_version": "0.1.0~dev.330+g0123456789ab",
        "schema_version": 1,
        "target_binary_digest": "d".repeat(64),
        "target_build_digest": format!("sha256:{}", "e".repeat(64)),
    })
}

#[test]
fn exact_signed_arm64_upgrade_contract_is_accepted() {
    let claim = claim(package());

    claim.validate().unwrap();
    let request = AgentUpgradeRequest::parse(&claim).unwrap();

    assert_eq!(request.architecture, "linux-arm64");
    assert_eq!(request.package_bytes, 5_000_000);
}

#[test]
fn upgrade_contract_rejects_non_release_hosts_and_ambiguous_urls() {
    for url in [
        "https://example.com/vonk-forge-agent.deb",
        "https://install.vonkforge.ai/vonk-forge-agent.deb?other=1",
        "https://install.vonkforge.ai:443/vonk-forge-agent.deb",
        "http://install.vonkforge.ai/vonk-forge-agent.deb",
    ] {
        let mut package = package();
        package["package_url"] = Value::String(url.to_owned());
        assert!(
            AgentUpgradeRequest::parse(&claim(package)).is_err(),
            "{url}"
        );
    }
}

#[test]
fn upgrade_contract_rejects_noncanonical_versions_and_unknown_fields() {
    for version in [".0.1.0", "0.1.0 bad", ""] {
        let mut package = package();
        package["package_version"] = Value::String(version.to_owned());
        assert!(
            AgentUpgradeRequest::parse(&claim(package)).is_err(),
            "{version}"
        );
    }
    let mut package = package();
    package["command"] = Value::String("apt upgrade".to_owned());
    assert!(AgentUpgradeRequest::parse(&claim(package)).is_err());
}
