#![forbid(unsafe_code)]

use chrono::{DateTime, FixedOffset, Utc};
use serde_json::json;
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::config::AgentConfig;
use vonk_agent::state::{BeginDecision, StateError, StateStore};
use vonk_agent_protocol::{AgentClaim, canonical_json, hex_sha256};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

#[test]
fn polling_configuration_requires_an_enrollment_origin() {
    let document = r#"
controller_url = "https://agents.vonk.test/"
ca_path = "/etc/vonk-forge-agent/controller-ca.pem"
ca_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
data_dir = "/var/lib/vonk-forge"
node_id = "spk_0123456789abcdef0123456789abcdef"
poll_min_seconds = 2
poll_max_seconds = 60
"#;

    assert!(AgentConfig::parse(document).is_err());
}

fn claim(attempt: u32, deadline: &str) -> AgentClaim {
    let payload = json!({"plan_digest": "a".repeat(64)});
    AgentClaim {
        attempt,
        base_commit: "b".repeat(40),
        deadline: DateTime::<FixedOffset>::parse_from_rfc3339(deadline).unwrap(),
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
        node_id: NODE_ID.to_owned(),
        operation: "recipe.install".to_owned(),
        operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
        payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
        payload,
        schema_version: 1,
    }
}

#[test]
fn claims_fail_closed_on_deadline_identity_and_stale_attempt() {
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let now = Utc::now();
    let expired = claim(1, "2026-01-01T00:00:00+00:00");
    assert!(matches!(
        state.begin(&expired, now),
        Err(StateError::Expired)
    ));

    let live = claim(2, "2099-01-01T00:00:00+00:00");
    assert_eq!(state.begin(&live, now).unwrap(), BeginDecision::Execute);
    let stale = claim(1, "2099-01-01T00:00:00+00:00");
    assert!(matches!(state.begin(&stale, now), Err(StateError::Stale)));

    let mut foreign = live;
    foreign.node_id = "spk_ffffffffffffffffffffffffffffffff".to_owned();
    assert!(matches!(
        state.begin(&foreign, now),
        Err(StateError::Identity)
    ));
}

#[test]
fn bounded_backoff_never_exceeds_configured_poll_window() {
    for attempt in 0..100 {
        for entropy in [0, 1, u64::MAX / 2, u64::MAX] {
            let delay = vonk_agent::state::backoff_delay(attempt, entropy, 2, 60);
            assert!((2..=60).contains(&delay.as_secs()));
        }
    }
}

#[test]
fn claim_response_parser_enforces_status_size_and_protocol() {
    assert!(
        vonk_agent::client::parse_claim_response(204, b"")
            .unwrap()
            .is_none()
    );
    assert!(vonk_agent::client::parse_claim_response(200, b"{}").is_err());
    assert!(vonk_agent::client::parse_claim_response(403, b"{}").is_err());

    let body = canonical_json(&claim(1, "2099-01-01T00:00:00+00:00")).unwrap();
    let parsed = vonk_agent::client::parse_claim_response(200, &body)
        .unwrap()
        .unwrap();
    assert_eq!(
        parsed.operation_id,
        claim(1, "2099-01-01T00:00:00+00:00").operation_id
    );
}
