#![forbid(unsafe_code)]

use chrono::{DateTime, FixedOffset, Utc};
use serde_json::json;
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::client::ControllerError;
use vonk_agent::state::{BeginDecision, StateError, StateStore};
use vonk_agent_protocol::generated::{
    AgentClaimPayload, AgentFailureKind, AgentOperation, AgentResultResult, RecipeStopPayload,
};
use vonk_agent_protocol::{AgentClaim, canonical_json, hex_sha256};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

fn claim() -> AgentClaim {
    let payload = RecipeStopPayload {
        cancel_pending_start: false,
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
fn acknowledged_result_is_not_replayed_after_restart() {
    // The original retention remedy was to delete reconciliation markers,
    // which would let an acknowledged outcome replay on the next start.  An
    // acknowledgement and its marker must survive a restart together, and only
    // the diagnostic-only reconciliation path may see the receipt once.
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let result = {
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        let claim = claim();
        assert_eq!(
            state.begin(&claim, Utc::now()).unwrap(),
            BeginDecision::Execute
        );
        let result = state
            .finish(&claim, "succeeded", json!({"stopped": true}))
            .unwrap();
        state.acknowledge(&result).unwrap();
        result
    };

    let mut restarted = StateStore::open(&path, NODE_ID).unwrap();
    assert!(restarted.pending_results().unwrap().is_empty());
    assert_eq!(
        restarted.unreconciled_results().unwrap(),
        vec![(AgentOperation::RecipeStop, result.clone())]
    );
    restarted.mark_reconciled(&result).unwrap();
    drop(restarted);

    let again = StateStore::open(&path, NODE_ID).unwrap();
    assert!(again.pending_results().unwrap().is_empty());
    assert!(again.unreconciled_results().unwrap().is_empty());
}

#[test]
fn rejected_result_and_its_refusal_survive_restart() {
    // A refused result is evidence the Controller never accepted, so a restart
    // must keep both the receipt and the bounded refusal: the receipt so a
    // corrected Controller rule can reconcile it, the refusal so the loop does
    // not hot-loop the same bytes in the meantime.
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let result = {
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        let claim = claim();
        assert_eq!(
            state.begin(&claim, Utc::now()).unwrap(),
            BeginDecision::Execute
        );
        let result = state
            .finish(&claim, "succeeded", json!({"stopped": true}))
            .unwrap();
        state
            .reject_result(&result, &ingress_refusal(), Utc::now())
            .unwrap();
        result
    };

    let restarted = StateStore::open(&path, NODE_ID).unwrap();
    assert_eq!(restarted.pending_results().unwrap().len(), 1);
    let rejection = restarted
        .result_rejection(&result, Utc::now())
        .unwrap()
        .expect("the refusal survives the restart");
    assert_eq!(rejection.http_status, 422);
    assert_eq!(rejection.code, "controller.invalid_request");
    assert_eq!(rejection.request_id.as_deref(), Some("req-422"));
    // Past the cool-down the same retained receipt is offered again.
    assert!(
        restarted
            .result_rejection(
                &result,
                rejection.retry_due_at + chrono::Duration::seconds(1)
            )
            .unwrap()
            .is_none()
    );
}

fn ingress_refusal() -> ControllerError {
    ControllerError {
        operation: "controller.request /agent/result".to_owned(),
        endpoint: "/agent/result".to_owned(),
        status: 422,
        code: "controller.invalid_request".to_owned(),
        request_id: Some("req-422".to_owned()),
        decision: "exit",
        retry_after_seconds: None,
    }
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
    let BeginDecision::Replay(result) = decision else {
        panic!("an interrupted attempt must not execute without fresh Controller authority");
    };
    let AgentResultResult::AgentFailureResult(failure) = result.result else {
        panic!("restart must report typed interrupted-effect evidence");
    };
    assert_eq!(
        failure.error_code.as_deref(),
        Some("agent_restart_interrupted")
    );
    assert_eq!(
        failure.failure_kind,
        Some(AgentFailureKind::UncertainEffect)
    );
    assert_eq!(failure.uncertain, Some(true));
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
