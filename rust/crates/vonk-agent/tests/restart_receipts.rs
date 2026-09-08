#![forbid(unsafe_code)]

use chrono::{DateTime, FixedOffset, Utc};
use serde_json::json;
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::state::{BeginDecision, StateError, StateStore};
use vonk_agent_protocol::generated::{AgentClaimPayload, AgentOperation, RecipeStopPayload};
use vonk_agent_protocol::{AgentClaim, canonical_json, hex_sha256};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

fn claim() -> AgentClaim {
    let payload = RecipeStopPayload {
        plan_digest: "a".repeat(64),
        run_id: Uuid::parse_str("00000000-0000-4000-8000-000000000003").unwrap(),
        schema_version: 1,
    };
    let payload_digest = hex_sha256(&canonical_json(&payload).unwrap());
    AgentClaim {
        attempt: 1,
        authority_revision: "b".repeat(64),
        deadline: DateTime::<FixedOffset>::parse_from_rfc3339("2099-01-01T00:00:00+00:00").unwrap(),
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
        node_id: NODE_ID.to_owned(),
        operation: AgentOperation::RecipeStop,
        operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
        payload_digest,
        payload: AgentClaimPayload::RecipeStopPayload(payload),
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
            .finish(&claim, "succeeded", json!({"stopped": true}))
            .unwrap()
    };

    let mut restarted = StateStore::open(&path, NODE_ID).unwrap();
    assert_eq!(
        restarted.pending_results().unwrap(),
        vec![(AgentOperation::RecipeStop, result.clone())]
    );
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

#[test]
fn mismatched_result_is_rejected_before_persistence() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let mut state = StateStore::open(&path, NODE_ID).unwrap();
    let claim = claim();
    assert_eq!(
        state.begin(&claim, Utc::now()).unwrap(),
        BeginDecision::Execute
    );

    assert!(matches!(
        state.finish(&claim, "succeeded", json!({"installed_bytes": 0})),
        Err(StateError::Protocol(_))
    ));
    assert!(state.pending_results().unwrap().is_empty());

    let result = state
        .finish(&claim, "succeeded", json!({"stopped": true}))
        .unwrap();
    assert_eq!(
        state.pending_results().unwrap(),
        vec![(AgentOperation::RecipeStop, result)]
    );
}

#[test]
fn mismatched_durable_result_is_rejected_before_submission_and_replay() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let claim = claim();
    {
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        assert_eq!(
            state.begin(&claim, Utc::now()).unwrap(),
            BeginDecision::Execute
        );
        state
            .finish(&claim, "succeeded", json!({"stopped": true}))
            .unwrap();
    }
    let connection = rusqlite::Connection::open(&path).unwrap();
    let raw: Vec<u8> = connection
        .query_row(
            "SELECT result_json FROM operations WHERE operation_id=?1",
            [claim.operation_id.to_string()],
            |row| row.get(0),
        )
        .unwrap();
    let mut document: serde_json::Value = serde_json::from_slice(&raw).unwrap();
    document["result"] = json!({"installed_bytes": 0});
    connection
        .execute(
            "UPDATE operations SET result_json=?2 WHERE operation_id=?1",
            rusqlite::params![
                claim.operation_id.to_string(),
                canonical_json(&document).unwrap()
            ],
        )
        .unwrap();
    drop(connection);

    let state = StateStore::open(&path, NODE_ID).unwrap();
    assert!(matches!(
        state.pending_results(),
        Err(StateError::Protocol(_))
    ));
    let mut state = state;
    assert!(matches!(
        state.begin(&claim, Utc::now()),
        Err(StateError::Protocol(_))
    ));
}

#[test]
fn incompatible_operation_journal_fails_closed_without_touching_credentials() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let credentials = directory.path().join("credentials");
    std::fs::create_dir(&credentials).unwrap();
    let sentinel = credentials.join("active.json");
    std::fs::write(&sentinel, b"credential sentinel").unwrap();
    let connection = rusqlite::Connection::open(&path).unwrap();
    connection
        .execute_batch(
            "CREATE TABLE metadata (
               key TEXT PRIMARY KEY NOT NULL,
               value TEXT NOT NULL
             ) STRICT;
             CREATE TABLE operations (
               operation_id TEXT PRIMARY KEY NOT NULL,
               job_id TEXT NOT NULL,
               node_id TEXT NOT NULL,
               attempt INTEGER NOT NULL,
               fence TEXT NOT NULL,
               deadline TEXT NOT NULL,
               state TEXT NOT NULL,
               result_json BLOB,
               result_acknowledged INTEGER NOT NULL DEFAULT 0
             ) STRICT;",
        )
        .unwrap();
    drop(connection);

    let error = match StateStore::open(&path, NODE_ID) {
        Ok(_) => panic!("old operation journal was accepted"),
        Err(error) => error,
    };
    assert!(matches!(error, StateError::IncompatibleSchema));
    assert!(error.to_string().contains("remove only state.sqlite"));
    assert_eq!(std::fs::read(sentinel).unwrap(), b"credential sentinel");
}
