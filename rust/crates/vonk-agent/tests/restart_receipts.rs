#![forbid(unsafe_code)]

use chrono::{DateTime, FixedOffset, Utc};
use serde_json::json;
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::client::ControllerError;
use vonk_agent::outcome::ExecutionResult;
use vonk_agent::state::{BeginDecision, StateStore};
use vonk_agent::workloads::CompiledExecutionPlan;
use vonk_agent_protocol::generated::{
    AgentClaimPayload, AgentInstallResult, AgentOperation, AgentResultResult, RecipeStopPayload,
    RecipeStopResult,
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
        rank: compiled_execution_plan.runtime.placement.rank.clone(),
        role: compiled_execution_plan.runtime.placement.role.clone(),
        recipe_content_sha256: compiled_execution_plan
            .identity
            .recipe_revision_sha256
            .clone(),
        stop_timeout_seconds: compiled_execution_plan.lifecycle.stop_timeout_seconds,
        installation_id: Uuid::parse_str("00000000-0000-4000-8000-000000000004").unwrap(),
        mapping_id: Uuid::parse_str("00000000-0000-4000-8000-000000000005").unwrap(),
        plan_digest: "e".repeat(64),
        recipe_revision_id: Uuid::parse_str("00000000-0000-4000-8000-000000000006").unwrap(),
        run_generation: 1,
        run_id,
        target_runtime_id: run_id,
    };
    AgentClaim {
        observation_budget_seconds: 3600,
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
    assert!(rejection.retry_due_at > rejection.observed_at);
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

    let BeginDecision::Replay(result) = decision else {
        panic!("an interrupted attempt must not execute without fresh Controller authority");
    };
    let AgentResultResult::OutcomeUnknown(_) = result.result else {
        panic!("restart must report a typed unknown outcome");
    };
    let mut fresh = claim.clone();
    fresh.fence = Uuid::new_v4();
    assert_eq!(
        restarted.begin(&fresh, Utc::now()).unwrap(),
        BeginDecision::Execute
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

    assert!(
        state
            .finish(
                &claim,
                ExecutionResult::done(AgentInstallResult { installed_bytes: 0 }),
            )
            .is_err()
    );
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
    assert!(state.pending_results().unwrap().is_empty());
    let mut state = state;
    let BeginDecision::Replay(result) = state.begin(&claim, Utc::now()).unwrap() else {
        panic!("damaged custody authorized a replayed effect");
    };
    assert!(matches!(
        result.result,
        AgentResultResult::OutcomeUnknown(_)
    ));
    let mut fresh = claim.clone();
    fresh.fence = Uuid::new_v4();
    assert_eq!(
        state.begin(&fresh, Utc::now()).unwrap(),
        BeginDecision::Execute
    );
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
        let decision = recovered.begin(&claim(), Utc::now()).unwrap();
        if damage == "operation" {
            let BeginDecision::Replay(result) = decision else {
                panic!("damaged same-fence bookkeeping must not repeat its effect");
            };
            assert!(matches!(
                result.result,
                AgentResultResult::OutcomeUnknown(_)
            ));
        } else {
            assert_eq!(decision, BeginDecision::Execute);
        }
        let mut fresh = claim();
        fresh.fence = Uuid::new_v4();
        assert_eq!(
            recovered.begin(&fresh, Utc::now()).unwrap(),
            BeginDecision::Execute
        );
        let quarantined = std::fs::read_dir(directory.path()).unwrap().any(|entry| {
            entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with("state.sqlite.corrupt-")
        });
        assert_eq!(quarantined, matches!(damage, "sqlite" | "identity"));
    }
}

#[test]
fn unsafe_state_path_is_not_replaced_by_recovery() {
    let directory = tempdir().unwrap();
    let target = directory.path().join("private");
    std::fs::write(&target, b"keep").unwrap();
    let path = directory.path().join("state.sqlite");
    std::os::unix::fs::symlink(&target, &path).unwrap();
    assert!(StateStore::open_recovered(&path, NODE_ID).is_err());
    assert_eq!(std::fs::read(target).unwrap(), b"keep");
}

#[test]
fn interrupted_quarantine_is_completed_before_the_journal_is_reopened() {
    for moved in [0, 1, 2, 3] {
        let directory = tempdir().unwrap();
        let path = directory.path().join("state.sqlite");
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        state.begin(&claim(), Utc::now()).unwrap();
        drop(state);
        // Stand in for process death after each durable rename. These opaque
        // companion bytes must be retained, never read by the new database.
        std::fs::write(directory.path().join("state.sqlite-wal"), b"old-wal").unwrap();
        std::fs::write(directory.path().join("state.sqlite-shm"), b"old-shm").unwrap();
        let id = Uuid::new_v4();
        std::fs::write(
            directory.path().join("state.sqlite.repair-pending"),
            id.to_string(),
        )
        .unwrap();
        for suffix in ["-wal", "-shm", ""].into_iter().take(moved) {
            std::fs::rename(
                directory.path().join(format!("state.sqlite{suffix}")),
                directory
                    .path()
                    .join(format!("state.sqlite.corrupt-{id}{suffix}")),
            )
            .unwrap();
        }
        let mut reopened = StateStore::open_recovered(&path, NODE_ID).unwrap();
        assert_eq!(
            reopened.begin(&claim(), Utc::now()).unwrap(),
            BeginDecision::Execute
        );
        assert!(
            !directory
                .path()
                .join("state.sqlite.repair-pending")
                .exists()
        );
        // Interrupted and repaired files may have separate diagnostic IDs.
        // The bytes must survive; the old WAL must never reach the new journal.
        assert!(std::fs::read_dir(directory.path()).unwrap().any(|entry| {
            let entry = entry.unwrap();
            entry
                .file_name()
                .to_string_lossy()
                .starts_with("state.sqlite.corrupt-")
                && std::fs::read(entry.path()).is_ok_and(|bytes| bytes == b"old-wal")
        }));
    }
}

#[test]
fn state_diagnostic_retention_is_bounded_without_touching_other_files() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let old = directory
        .path()
        .join(format!("state.sqlite.corrupt-{}", Uuid::new_v4()));
    std::fs::File::create(&old)
        .unwrap()
        .set_len(vonk_agent::inventory::STATE_DATABASE_DISK_RESERVE_BYTES + 1)
        .unwrap();
    let unrelated = directory.path().join("state.sqlite.corrupt-not-owned");
    std::fs::write(&unrelated, b"keep").unwrap();
    StateStore::open_recovered(&path, NODE_ID).unwrap();
    assert!(!old.exists());
    assert_eq!(std::fs::read(&unrelated).unwrap(), b"keep");
}

#[test]
fn damaged_repair_intent_never_reopens_a_wal_stripped_journal() {
    for kind in [0, 1, 2, 3] {
        let directory = tempdir().unwrap();
        let path = directory.path().join("state.sqlite");
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        state.begin(&claim(), Utc::now()).unwrap();
        drop(state);
        let marker = directory.path().join("state.sqlite.repair-pending");
        match kind {
            0 => std::fs::write(&marker, b"damaged").unwrap(),
            1 => std::fs::write(&marker, [b'x'; 256]).unwrap(),
            2 => std::fs::create_dir(&marker).unwrap(),
            _ => std::os::unix::fs::symlink(directory.path().join("absent"), &marker).unwrap(),
        }
        let mut recovered = StateStore::open_recovered(&path, NODE_ID).unwrap();
        assert!(recovered.pending_results().unwrap().is_empty());
        assert_eq!(
            recovered.begin(&claim(), Utc::now()).unwrap(),
            BeginDecision::Execute
        );
    }
}

#[test]
fn an_unreadable_refusal_record_is_due_never_deleted_and_repaired_by_the_next_refusal() {
    // P6: a parse error, an out-of-range count or clock skew on a refusal
    // record never deletes the evidence. The result is offered again, the
    // record stays, and the Controller's next refusal rewrites it whole.
    for damage in [
        "UPDATE result_rejections SET retry_due_at='unreadable'",
        "UPDATE result_rejections SET observed_at='unreadable'",
        "UPDATE result_rejections SET rejections=1099511627776",
        "UPDATE result_rejections SET observed_at='2999-01-01T00:00:00+00:00', retry_due_at='2999-01-01T00:15:00+00:00'",
    ] {
        let directory = tempdir().unwrap();
        let path = directory.path().join("state.sqlite");
        let mut state = StateStore::open(&path, NODE_ID).unwrap();
        let original = claim();
        state.begin(&original, Utc::now()).unwrap();
        let result = state
            .finish(
                &original,
                ExecutionResult::done(RecipeStopResult::default()),
            )
            .unwrap();
        state
            .reject_result(&result, &ingress_refusal(), Utc::now())
            .unwrap();
        let connection = rusqlite::Connection::open(&path).unwrap();
        connection.execute(damage, []).unwrap();
        let records = || -> i64 {
            connection
                .query_row("SELECT count(*) FROM result_rejections", [], |row| {
                    row.get(0)
                })
                .unwrap()
        };

        assert_eq!(
            state.result_rejection(&result, Utc::now()).unwrap(),
            None,
            "{damage}"
        );
        assert_eq!(records(), 1, "{damage}");
        assert_eq!(
            state.pending_results().unwrap(),
            vec![(AgentOperation::RecipeStop, result.clone())]
        );
        let mut fresh = claim();
        fresh.fence = Uuid::new_v4();
        assert_eq!(
            state.begin(&fresh, Utc::now()).unwrap(),
            BeginDecision::Execute
        );

        let repaired = state
            .reject_result(&result, &ingress_refusal(), Utc::now())
            .unwrap();
        assert_eq!(records(), 1, "{damage}");
        assert_eq!(
            state.retained_rejection(&result).unwrap(),
            Some(repaired.clone()),
            "{damage}"
        );
        assert!(
            state
                .result_rejection(&result, Utc::now())
                .unwrap()
                .is_some(),
            "{damage}"
        );
    }
}

#[test]
fn a_refused_result_stays_in_the_spool_with_its_refusal_and_is_offered_again_after_900_s() {
    // P6: a 422 refusal is evidence the Controller never confirmed. It is
    // kept with the result and offered again on its schedule without end;
    // before, the result was never re-sent once 900 s had passed.
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let now = Utc::now();
    let mut state = StateStore::open(&path, NODE_ID).unwrap();
    let original = claim();
    state.begin(&original, now).unwrap();
    let result = state
        .finish(
            &original,
            ExecutionResult::done(RecipeStopResult::default()),
        )
        .unwrap();
    state
        .reject_result(&result, &ingress_refusal(), now)
        .unwrap();
    let latest = state
        .reject_result(
            &result,
            &ingress_refusal(),
            now + chrono::Duration::seconds(899),
        )
        .unwrap();
    assert_eq!(latest.observed_at, now, "the first refusal time is kept");
    assert_eq!(latest.rejections, 2);
    drop(state);
    let mut state = StateStore::open(&path, NODE_ID).unwrap();
    for seconds in [1800, 86400] {
        let at = now + chrono::Duration::seconds(seconds);
        assert_eq!(state.result_rejection(&result, at).unwrap(), None);
        assert_eq!(
            state.retained_rejection(&result).unwrap(),
            Some(latest.clone())
        );
    }
    assert_eq!(state.pending_results().unwrap()[0].1, result);
    // Each further refusal cools down for at most the schedule's maximum.
    let later = now + chrono::Duration::seconds(86400);
    let again = state
        .reject_result(&result, &ingress_refusal(), later)
        .unwrap();
    assert!(again.retry_due_at > later);
    assert!(again.retry_due_at <= later + chrono::Duration::seconds(900));
    let mut fresh = original;
    fresh.fence = Uuid::new_v4();
    assert_eq!(state.begin(&fresh, now).unwrap(), BeginDecision::Execute);
    state
        .finish(&fresh, ExecutionResult::done(RecipeStopResult::default()))
        .unwrap();
}

#[test]
fn adding_claim_custody_preserves_existing_journal_receipts() {
    // Wrong implementation: bumping the journal version resets retained
    // receipts instead of adding claim custody to the existing table.
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let accepted = claim();
    let mut store = StateStore::open(&path, NODE_ID).unwrap();
    store.begin(&accepted, Utc::now()).unwrap();
    let receipt = store
        .finish(
            &accepted,
            ExecutionResult::done(RecipeStopResult::default()),
        )
        .unwrap();
    drop(store);
    let old = rusqlite::Connection::open(&path).unwrap();
    old.execute("ALTER TABLE operations DROP COLUMN claim_json", [])
        .unwrap();
    drop(old);
    let reopened = StateStore::open_recovered(&path, NODE_ID).unwrap();
    assert_eq!(
        reopened.pending_results().unwrap(),
        vec![(accepted.operation, receipt)]
    );
}

#[test]
fn a_later_exact_completion_replaces_unknown_without_repeated_unknown_redelivery() {
    // Wrong implementation: a delivered unknown becomes permanent, or every
    // observation resets its acknowledgement and masquerades as new progress.
    use vonk_agent::outcome::UnknownEvidence;
    use vonk_agent_protocol::generated::{FailureStage, WaitReason};
    let directory = tempdir().unwrap();
    let path = directory.path().join("state.sqlite");
    let accepted = claim();
    let mut store = StateStore::open(&path, NODE_ID).unwrap();
    store.begin(&accepted, Utc::now()).unwrap();
    let unknown = || {
        ExecutionResult::unknown(
            WaitReason::RuntimeEffectUnconfirmed,
            "exact effect remains unconfirmed",
            UnknownEvidence::at(FailureStage::AgentRestart),
        )
    };
    let receipt = store.finish(&accepted, unknown()).unwrap();
    store.acknowledge(&receipt).unwrap();
    assert_eq!(store.finish(&accepted, unknown()).unwrap(), receipt);
    assert!(store.pending_results().unwrap().is_empty());
    let complete = store
        .finish(
            &accepted,
            ExecutionResult::done(RecipeStopResult::default()),
        )
        .unwrap();
    assert_eq!(complete.fence, accepted.fence);
    assert_eq!(
        store.pending_results().unwrap(),
        vec![(accepted.operation, complete)]
    );
}
