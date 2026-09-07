#![forbid(unsafe_code)]

use chrono::{DateTime, FixedOffset, Utc};
use serde_json::{Value, json};
use tempfile::tempdir;
use uuid::Uuid;
use vonk_agent::state::{BeginDecision, StateError, StateStore};
use vonk_agent::workloads::CompiledExecutionPlan;
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES,
    RecipeOperationRequest, canonical_json, hex_sha256,
};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

fn claim(attempt: u32, deadline: &str) -> AgentClaim {
    let payload = json!({});
    AgentClaim {
        attempt,
        authority_revision: "b".repeat(64),
        deadline: DateTime::<FixedOffset>::parse_from_rfc3339(deadline).unwrap(),
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
        node_id: NODE_ID.to_owned(),
        operation: "node.probe".to_owned(),
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
fn heartbeat_renewal_is_durable_and_used_by_the_terminal_result() {
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let claim = claim(2, "2099-01-01T00:00:00+00:00");
    assert_eq!(
        state.begin(&claim, Utc::now()).unwrap(),
        BeginDecision::Execute
    );
    let request = AgentProgress {
        attempt: claim.attempt,
        deadline: claim.deadline,
        fence: claim.fence,
        job_id: claim.job_id,
        node_id: claim.node_id.clone(),
        operation_id: claim.operation_id,
        progress: json!({"phase": "executing"}),
        schema_version: claim.schema_version,
    };
    let renewed = AgentDirective {
        attempt: claim.attempt,
        cancel_requested: false,
        deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:30+00:00").unwrap(),
        fence: claim.fence,
        job_id: claim.job_id,
        node_id: claim.node_id.clone(),
        operation_id: claim.operation_id,
        schema_version: claim.schema_version,
    };

    state.apply_heartbeat(&request, &renewed).unwrap();
    drop(state);
    let mut reopened = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let result = reopened
        .finish(&claim, "succeeded", json!({"status": "ok"}))
        .unwrap();

    assert_eq!(result.deadline, renewed.deadline);
}

#[test]
fn heartbeat_renewal_rejects_stale_or_foreign_directives() {
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let claim = claim(2, "2099-01-01T00:00:00+00:00");
    state.begin(&claim, Utc::now()).unwrap();
    let request = AgentProgress {
        attempt: claim.attempt,
        deadline: claim.deadline,
        fence: claim.fence,
        job_id: claim.job_id,
        node_id: claim.node_id.clone(),
        operation_id: claim.operation_id,
        progress: json!({"phase": "executing"}),
        schema_version: claim.schema_version,
    };
    let mut directive = AgentDirective {
        attempt: claim.attempt,
        cancel_requested: false,
        deadline: claim.deadline - chrono::Duration::seconds(1),
        fence: claim.fence,
        job_id: claim.job_id,
        node_id: claim.node_id.clone(),
        operation_id: claim.operation_id,
        schema_version: claim.schema_version,
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
    assert_eq!(
        parsed.operation_id,
        claim(1, "2099-01-01T00:00:00+00:00").operation_id
    );

    let mut large_claim = claim(1, "2099-01-01T00:00:00+00:00");
    large_claim.payload.as_object_mut().unwrap().insert(
        "compiled_execution_plan".to_owned(),
        json!({"artifact": "x".repeat(516 * 1024)}),
    );
    large_claim.payload_digest = hex_sha256(&canonical_json(&large_claim.payload).unwrap());
    let large_body = canonical_json(&large_claim).unwrap();
    assert!(large_body.len() > 512 * 1024);
    assert!(vonk_agent::client::parse_claim_response(200, &large_body).is_ok());
    assert!(
        vonk_agent::client::parse_claim_response(
            200,
            &vec![b'x'; MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES + 1]
        )
        .is_err()
    );
}

#[test]
fn real_751_artifact_claim_parses_through_rust_and_full_plan_dto() {
    let plan: Value = serde_json::from_str(include_str!(
        "../../../../control/tests/fixtures/compiled_plan_751.json"
    ))
    .unwrap();
    assert_eq!(plan["artifacts"].as_array().unwrap().len(), 751);
    let payload = json!({
        "schema_version": 2,
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "plan_digest": "a".repeat(64),
        "expected_bytes": 4096,
        "rank": 0,
        "role": "entrypoint",
        "compiled_execution_plan": plan,
    });
    let mut raw = claim(1, "2099-01-01T00:00:00+00:00");
    raw.operation = "recipe.install".to_owned();
    raw.payload_digest = hex_sha256(&canonical_json(&payload).unwrap());
    raw.payload = payload;
    let body = canonical_json(&raw).unwrap();
    assert!(body.len() > 500 * 1024);
    let parsed = vonk_agent::client::parse_claim_response(200, &body)
        .unwrap()
        .unwrap();
    let request = RecipeOperationRequest::parse(&parsed).unwrap();
    let RecipeOperationRequest::Install(request) = request else {
        panic!("expected install request");
    };
    let plan: CompiledExecutionPlan =
        serde_json::from_value(request.compiled_execution_plan).unwrap();
    plan.validate().unwrap();
    assert_eq!(plan.artifacts.len(), 751);
}
