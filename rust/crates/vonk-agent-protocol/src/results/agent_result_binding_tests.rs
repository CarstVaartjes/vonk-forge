use super::*;
use crate::generated::{AgentOperation, AgentResultState};

fn result(state: AgentResultState, body: Value) -> AgentResult {
    AgentResult {
        fence: Uuid::new_v4(),
        result: serde_json::from_value(body).unwrap(),
        state,
    }
}

#[test]
fn retired_unknown_result_state_is_adopted_without_being_written() {
    let old = serde_json::json!({
        "fence": "00000000-0000-4000-8000-000000000001",
        "state": "waiting-for-operator",
        "result": {"reason": "legacy receipt"}
    });
    let adopted: AgentResult = serde_json::from_value(old).unwrap();
    assert_eq!(adopted.state, AgentResultState::Observing);
    assert_eq!(serde_json::to_value(adopted).unwrap()["state"], "observing");
}

fn typed(state: AgentResultState, body: Value) -> AgentResult {
    result(state, body)
}

#[test]
fn typed_outcome_is_bound_to_its_state_and_operation() {
    let done = typed(
        AgentResultState::Succeeded,
        serde_json::json!({"kind": "done", "result": {"installed_bytes": 3}}),
    );
    done.validate_for_operation(&AgentOperation::RecipeInstall)
        .unwrap();
    assert!(
        done.validate_for_operation(&AgentOperation::RecipeStop)
            .is_err()
    );

    let unknown = serde_json::json!({
        "kind": generated::OutcomeUnknownKind::Unknown,
        "wait_reason": "stop-unconfirmed",
        "reason": "workload stop remains unconfirmed",
    });
    typed(AgentResultState::Observing, unknown.clone())
        .validate_for_operation(&AgentOperation::RecipeStop)
        .unwrap();
    for wrong in [
        AgentResultState::Failed,
        AgentResultState::Cancelled,
        AgentResultState::Succeeded,
    ] {
        assert!(
            typed(wrong, unknown.clone())
                .validate_for_operation(&AgentOperation::RecipeStop)
                .is_err()
        );
    }

    let cancelled = serde_json::json!({
        "kind": generated::OutcomeFailedKind::Failed,
        "code": "operation_cancelled",
        "reason": "controller cancellation confirmed after exact workload stop",
    });
    typed(AgentResultState::Cancelled, cancelled.clone())
        .validate_for_operation(&AgentOperation::RecipeStart)
        .unwrap();
    assert!(
        typed(AgentResultState::Failed, cancelled)
            .validate_for_operation(&AgentOperation::RecipeStart)
            .is_err()
    );
}

#[test]
fn a_job_receipt_belongs_to_the_recipe_job_operation_only() {
    let receipt: Value = serde_json::from_str(include_str!(
            "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
        ))
        .unwrap();
    let mut receipt = receipt["result"].clone();
    receipt["exit_code"] = serde_json::json!(1);
    let failed = typed(
        AgentResultState::Failed,
        serde_json::json!({
            "kind": generated::OutcomeFailedKind::Failed,
            "code": "recipe_job_run_failed",
            "reason": "runtime failed",
            "receipt": receipt,
        }),
    );
    failed
        .validate_for_operation(&AgentOperation::RecipeJobRunV1)
        .unwrap();
    assert!(
        failed
            .validate_for_operation(&AgentOperation::RecipeStop)
            .is_err()
    );
}

#[test]
fn succeeded_result_is_bound_to_its_current_operation() {
    let stop = result(AgentResultState::Succeeded, serde_json::json!({}));
    stop.validate_for_operation(&AgentOperation::RecipeStop)
        .unwrap();
    assert!(
        stop.validate_for_operation(&AgentOperation::RecipeInstall)
            .is_err()
    );

    let install = result(
        AgentResultState::Succeeded,
        serde_json::json!({"installed_bytes": 0}),
    );
    install
        .validate_for_operation(&AgentOperation::RecipeInstall)
        .unwrap();
    assert!(
        install
            .validate_for_operation(&AgentOperation::RecipeStop)
            .is_err()
    );
}

#[test]
fn failure_result_retains_optional_fields_but_requires_failure_identity() {
    let failure = result(
        AgentResultState::Observing,
        serde_json::json!({"reason": "operator review required"}),
    );
    failure
        .validate_for_operation(&AgentOperation::RecipeStop)
        .unwrap();

    let empty = result(AgentResultState::Failed, serde_json::json!({}));
    assert!(
        empty
            .validate_for_operation(&AgentOperation::RecipeStop)
            .is_err()
    );
    assert!(
        failure
            .validate_for_operation(&AgentOperation::RecipeJobRunV1)
            .is_ok()
    );
    assert!(
        failure
            .validate_for_operation(&AgentOperation::RecipeStop)
            .is_ok()
    );
}

#[test]
fn process_result_is_valid_only_for_the_recipe_job_operation() {
    let mut job: AgentResult = serde_json::from_str(include_str!(
            "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
        ))
        .unwrap();
    job.state = AgentResultState::Failed;
    let generated::AgentResultResult::RecipeJobRunResult(body) = &mut job.result else {
        panic!("expected typed job result")
    };
    body.exit_code = 1;
    body.reason = Some("runtime failed".to_owned());

    job.validate_for_operation(&AgentOperation::RecipeJobRunV1)
        .unwrap();
    assert!(
        job.validate_for_operation(&AgentOperation::RecipeStop)
            .is_err()
    );
    let generated::AgentResultResult::RecipeJobRunResult(body) = &mut job.result else {
        panic!("expected typed job result")
    };
    body.exit_code = 0;
    assert!(
        job.validate_for_operation(&AgentOperation::RecipeJobRunV1)
            .is_err()
    );
}
