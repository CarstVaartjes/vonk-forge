#![forbid(unsafe_code)]

use chrono::{DateTime, FixedOffset, Utc};
use serde_json::json;
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::client::ControllerError;
use vonk_agent::outcome::ExecutionResult;
use vonk_agent::state::{BeginDecision, StateError, StateStore};
use vonk_agent::workloads::CompiledExecutionPlan;
use vonk_agent_protocol::generated::{
    AgentClaimPayload, AgentInstallResult, AgentOperation, AgentResultResult, RecipeStopPayload,
    RecipeStopResult, WaitReason,
};
use vonk_agent_protocol::{AgentClaim, canonical_json};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

fn claim() -> AgentClaim {
    let compiled_execution_plan: CompiledExecutionPlan = serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_workload_v2.json"
    ))
    .unwrap();
    let run_id = Uuid::parse_str("00000000-0000-4000-8000-000000000003").unwrap();
    let payload = RecipeStopPayload {
        cancel_pending_start: false,
        compiled_execution_plan: compiled_execution_plan.clone(),
        installation_id: Uuid::parse_str("00000000-0000-4000-8000-000000000004").unwrap(),
        mapping_id: Uuid::parse_str("00000000-0000-4000-8000-000000000005").unwrap(),
        plan_digest: "e".repeat(64),
        recipe_revision_id: Uuid::parse_str("00000000-0000-4000-8000-000000000006").unwrap(),
        run_generation: 1,
        run_id,
        target_runtime_id: run_id,
    };
    AgentClaim {
        deadline: DateTime::<FixedOffset>::parse_from_rfc3339("2099-01-01T00:00:00+00:00").unwrap(),
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        operation: AgentOperation::RecipeStop,
        payload: AgentClaimPayload::RecipeStopPayload(payload),
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
            .finish(&claim, ExecutionResult::done(RecipeStopResult::default()))
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
            .finish(&claim, ExecutionResult::done(RecipeStopResult::default()))
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
            .finish(&claim, ExecutionResult::done(RecipeStopResult::default()))
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
        summary: Some(
            "request is invalid: body.result.AgentFailureResult.failure_kind (is_instance_of)"
                .to_owned(),
        ),
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
    let AgentResultResult::OutcomeUnknown(unknown) = result.result else {
        panic!("restart must report a typed unknown outcome");
    };
    assert_eq!(unknown.wait_reason, WaitReason::AgentRestartInterrupted);
    assert_eq!(
        unknown.reason,
        "agent restarted with an operation in progress"
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
        state.finish(
            &claim,
            ExecutionResult::done(AgentInstallResult { installed_bytes: 0 }),
        ),
        Err(StateError::Protocol(_))
    ));
    assert!(state.pending_results().unwrap().is_empty());

    let result = state
        .finish(&claim, ExecutionResult::done(RecipeStopResult::default()))
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
            .finish(&claim, ExecutionResult::done(RecipeStopResult::default()))
            .unwrap();
    }
    let connection = rusqlite::Connection::open(&path).unwrap();
    let raw: Vec<u8> = connection
        .query_row(
            "SELECT result_json FROM operations WHERE fence=?1",
            [claim.fence.to_string()],
            |row| row.get(0),
        )
        .unwrap();
    let mut document: serde_json::Value = serde_json::from_slice(&raw).unwrap();
    document["result"] = json!({"installed_bytes": 0});
    connection
        .execute(
            "UPDATE operations SET result_json=?2 WHERE fence=?1",
            rusqlite::params![claim.fence.to_string(), canonical_json(&document).unwrap()],
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
fn older_operation_journal_is_replaced_without_touching_credentials() {
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

    // The Controller re-issues unfinished work, so another protocol's
    // receipts are dropped rather than blocking the agent from starting.
    let state = StateStore::open(&path, NODE_ID).unwrap();
    assert!(state.pending_results().unwrap().is_empty());
    assert_eq!(std::fs::read(sentinel).unwrap(), b"credential sentinel");
}

#[test]
fn damaged_bookkeeping_is_quarantined_without_blocking_new_claims() {
    // Previously each of these made the startup open loop retry forever.
    for damage in ["sqlite", "identity", "fence", "operation"] {
        let directory = tempdir().unwrap();
        let path = directory.path().join("state.sqlite");
        if damage == "sqlite" {
            std::fs::write(&path, b"not a sqlite database").unwrap();
        } else {
            let mut state = StateStore::open(&path, NODE_ID).unwrap();
            state.begin(&claim(), Utc::now()).unwrap();
            drop(state);
            let connection = rusqlite::Connection::open(&path).unwrap();
            let sql = match damage {
                "identity" => "UPDATE metadata SET value='foreign-node' WHERE key='node_id'",
                "fence" => "UPDATE operations SET fence='invalid-fence'",
                _ => "UPDATE operations SET operation='invalid-operation'",
            };
            connection.execute(sql, []).unwrap();
        }
        let mut recovered = StateStore::open_recovered(&path, NODE_ID).unwrap();
        assert_eq!(
            recovered.begin(&claim(), Utc::now()).unwrap(),
            BeginDecision::Execute
        );
        assert!(std::fs::read_dir(directory.path()).unwrap().any(|entry| {
            entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with("state.sqlite.corrupt-")
        }));
    }
}

#[test]
fn unsafe_state_path_is_not_replaced_by_recovery() {
    let directory = tempdir().unwrap();
    let target = directory.path().join("private");
    std::fs::write(&target, b"keep").unwrap();
    let path = directory.path().join("state.sqlite");
    std::os::unix::fs::symlink(&target, &path).unwrap();
    assert!(matches!(
        StateStore::open_recovered(&path, NODE_ID),
        Err(StateError::Io(_))
    ));
    assert_eq!(std::fs::read(target).unwrap(), b"keep");
}
