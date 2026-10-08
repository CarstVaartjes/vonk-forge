#![forbid(unsafe_code)]

use chrono::{DateTime, FixedOffset, Utc};
use serde_json::Value;
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::outcome::ExecutionResult;
use vonk_agent::state::{BeginDecision, StateError, StateStore};
use vonk_agent::workloads::CompiledExecutionPlan;
use vonk_agent_protocol::generated::{
    AgentClaimPayload, AgentOperation, OperationProgress, RecipeInstallPayload, RecipeStopPayload,
    RecipeStopResult,
};
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, MAX_DOCUMENT_BYTES, RecipeOperationRequest,
    canonical_json,
};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

fn claim(attempt: u128, deadline: &str) -> AgentClaim {
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
        deadline: DateTime::<FixedOffset>::parse_from_rfc3339(deadline).unwrap(),
        // One fence per attempt.
        fence: Uuid::from_u128(0x44d4e914_34df_4962_a802_d1f7dcd92800 + attempt),
        operation: AgentOperation::RecipeStop,
        payload: AgentClaimPayload::RecipeStopPayload(payload),
    }
}

fn operation_progress(phase: &str) -> OperationProgress {
    OperationProgress {
        activity: None,
        bytes_per_second: None,
        checkpoint: None,
        completed_bytes: 0_u64.into(),
        completed_items: None,
        elapsed_seconds: None,
        eta_seconds: None,
        kind: None,
        last_progress_at: None,
        members: Vec::new(),
        object_sha256: None,
        observed_at: None,
        phase: phase.to_owned(),
        smoothed_bytes_per_second: None,
        total_bytes: None,
        total_bytes_known: false,
        total_items: None,
    }
}

#[test]
fn claims_fail_closed_on_deadline_and_replay_by_fence() {
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
    let BeginDecision::Replay(retained) = state.begin(&live, now).unwrap() else {
        panic!("uncertain running custody replayed an effect");
    };
    assert!(matches!(
        retained.result,
        vonk_agent_protocol::generated::AgentResultResult::OutcomeUnknown(_)
    ));
    let result = state
        .finish(&live, ExecutionResult::done(RecipeStopResult::default()))
        .unwrap();
    assert_eq!(
        state.begin(&live, now).unwrap(),
        BeginDecision::Replay(Box::new(result))
    );
}

#[test]
fn heartbeat_renewal_is_durable_and_used_by_the_terminal_result() {
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let claim = claim(2, "2099-01-01T00:00:00+00:00");
    assert_eq!(
        state.begin(&claim, Utc::now()).unwrap(),
        BeginDecision::Execute
    );
    let request = AgentProgress {
        fence: claim.fence,
        progress: Some(operation_progress("executing")),
    };
    let renewed = AgentDirective {
        cancel_requested: false,
        deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:30+00:00").unwrap(),
        fence: claim.fence,
    };

    state.apply_heartbeat(&request, &renewed).unwrap();
    drop(state);
    let mut reopened = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    reopened
        .finish(&claim, ExecutionResult::done(RecipeStopResult::default()))
        .unwrap();
}

#[test]
fn heartbeat_renewal_rejects_stale_or_foreign_directives() {
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let claim = claim(2, "2099-01-01T00:00:00+00:00");
    state.begin(&claim, Utc::now()).unwrap();
    let request = AgentProgress {
        fence: claim.fence,
        progress: Some(operation_progress("executing")),
    };
    let mut directive = AgentDirective {
        cancel_requested: false,
        deadline: claim.deadline - chrono::Duration::seconds(1),
        fence: claim.fence,
    };
    assert!(matches!(
        state.apply_heartbeat(&request, &directive),
        Err(StateError::Stale)
    ));

    directive.deadline = claim.deadline + chrono::Duration::seconds(30);
    directive.fence = Uuid::parse_str("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee").unwrap();
    assert!(matches!(
        state.apply_heartbeat(&request, &directive),
        Err(StateError::Stale)
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
    assert_eq!(parsed.fence, claim(1, "2099-01-01T00:00:00+00:00").fence);

    assert!(
        vonk_agent::client::parse_claim_response(200, &vec![b'x'; MAX_DOCUMENT_BYTES + 1]).is_err()
    );
}

#[test]
fn real_751_artifact_claim_parses_through_rust_and_full_plan_dto() {
    let plan: Value = serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_plan_751.json"
    ))
    .unwrap();
    assert_eq!(plan["artifacts"].as_array().unwrap().len(), 751);
    let compiled_execution_plan: CompiledExecutionPlan = serde_json::from_value(plan).unwrap();
    let payload = RecipeInstallPayload {
        installation_id: Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap(),
        plan_digest: "a".repeat(64),
        expected_bytes: 4096,
        compiled_execution_plan,
    };
    let mut raw = claim(1, "2099-01-01T00:00:00+00:00");
    raw.operation = AgentOperation::RecipeInstall;
    raw.payload = AgentClaimPayload::RecipeInstallPayload(payload);
    let body = canonical_json(&raw).unwrap();
    assert!(body.len() > 256 * 1024);
    let parsed = vonk_agent::client::parse_claim_response(200, &body)
        .unwrap()
        .unwrap();
    let request = RecipeOperationRequest::parse(&parsed).unwrap();
    let RecipeOperationRequest::Install(request) = request else {
        panic!("expected install request");
    };
    let plan = request.compiled_execution_plan;
    plan.validate().unwrap();
    assert_eq!(plan.artifacts.len(), 751);
}
