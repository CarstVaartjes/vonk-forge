#![forbid(unsafe_code)]

use chrono::{DateTime, FixedOffset, Utc};
use serde_json::json;
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::state::{BeginDecision, StateStore};
use vonk_agent_protocol::{AgentClaim, canonical_json, hex_sha256};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

fn claim() -> AgentClaim {
    let payload = json!({"run_id": "run-1"});
    AgentClaim {
        attempt: 1,
        authority_revision: "b".repeat(64),
        deadline: DateTime::<FixedOffset>::parse_from_rfc3339("2099-01-01T00:00:00+00:00").unwrap(),
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
        node_id: NODE_ID.to_owned(),
        operation: "recipe.start".to_owned(),
        operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
        payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
        payload,
        schema_version: 1,
    }
}

#[test]
fn completed_result_is_redelivered_until_acknowledged() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let result = {
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        let claim = claim();
        assert_eq!(
            state.begin(&claim, Utc::now()).unwrap(),
            BeginDecision::Execute
        );
        state
            .finish(&claim, "succeeded", json!({"installed": true}))
            .unwrap()
    };

    let mut restarted = StateStore::open(&path, NODE_ID).unwrap();
    assert_eq!(restarted.pending_results().unwrap(), vec![result.clone()]);
    restarted.acknowledge(&result).unwrap();
    assert!(restarted.pending_results().unwrap().is_empty());
}

#[test]
fn interrupted_mutation_is_not_executed_twice_after_restart() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let claim = claim();
    {
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        assert_eq!(
            state.begin(&claim, Utc::now()).unwrap(),
            BeginDecision::Execute
        );
    }

    let mut restarted = StateStore::open(&path, NODE_ID).unwrap();
    restarted.recover_interrupted().unwrap();
    let decision = restarted.begin(&claim, Utc::now()).unwrap();
    assert!(
        matches!(decision, BeginDecision::Replay(ref result) if result.state == "waiting-for-operator")
    );
}
