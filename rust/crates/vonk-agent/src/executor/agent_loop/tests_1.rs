#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn failed_execution_emits_the_controller_failure_contract() {
    let directory = tempdir().unwrap();
    let client = RecordingClient {
        cancel_requested: false,
        claim: Arc::new(Mutex::new(Some(claim()))),
        fail_heartbeat: false,
        heartbeats: Arc::new(Mutex::new(Vec::new())),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &FailedExecutor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_secs(10),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .unwrap();

    let results = client.results.lock().unwrap();
    assert_eq!(results[0].state, AgentResultState::Failed);
    let AgentResultResult::OutcomeFailed(failed) = &results[0].result else {
        panic!("a failed executor reports a typed failure");
    };
    assert_eq!(failed.code, FailureCode::RecipeInstallFailed);
    assert_eq!(failed.reason, "rootless image build failed");
    failed
        .evidence
        .as_ref()
        .and_then(|evidence| evidence.diagnostics.as_ref())
        .expect("bounded diagnostics")
        .validate()
        .unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn artifact_job_heartbeat_cancellation_is_preserved_as_terminal_cancelled() {
    let directory = tempdir().unwrap();
    let mut job_claim: AgentClaim = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    job_claim.deadline = (Utc::now() + ChronoDuration::seconds(20)).fixed_offset();
    job_claim.validate().unwrap();
    let RecipeOperationRequest::JobRun(request) =
        RecipeOperationRequest::parse(&job_claim).unwrap()
    else {
        panic!("expected canonical job claim");
    };
    let client = RecordingClient {
        cancel_requested: true,
        claim: Arc::new(Mutex::new(Some(job_claim))),
        fail_heartbeat: false,
        heartbeats: Arc::new(Mutex::new(Vec::new())),
        results: Arc::new(Mutex::new(Vec::new())),
    };
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();

    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &CancellationExecutor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(1),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .unwrap();

    let results = client.results.lock().unwrap();
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].state.as_str(), "cancelled");
    let vonk_agent_protocol::generated::AgentResultResult::OutcomeFailed(outcome) =
        &results[0].result
    else {
        panic!("expected a typed cancelled outcome");
    };
    assert_eq!(outcome.code, FailureCode::OperationCancelled);
    let result = outcome.receipt.as_ref().expect("the job receipt is kept");
    result.validate().unwrap();
    assert_eq!(result.job_id, request.job_id);
    assert_eq!(result.run_id, request.run_id);
    assert!(result.output_manifest.files.is_empty());
    assert_eq!(result.output_manifest.total_bytes, 0);
    assert_eq!(result.exit_code, 130);
    assert_eq!(
        result.reason.as_deref(),
        Some("controller cancellation requested")
    );
}
