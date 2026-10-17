#![cfg(test)]

use super::super::test_support::*;
use super::*;
use crate::outcome::{CORE_ONLY_REASON, Failure, core_only};

/// The Controller refuses every full result as invalid and answers a core-only
/// one as `core_accepted` says.
#[derive(Clone)]
struct EvidenceRefusingClient {
    core_accepted: bool,
    submitted: Arc<Mutex<Vec<AgentResult>>>,
}

fn is_core(result: &AgentResult) -> bool {
    match &result.result {
        AgentResultResult::OutcomeFailed(failed) => failed.reason == CORE_ONLY_REASON,
        AgentResultResult::OutcomeUnknown(unknown) => unknown.reason == CORE_ONLY_REASON,
        _ => false,
    }
}

#[async_trait]
impl LoopClient for EvidenceRefusingClient {
    async fn claim(
        &self,
        _preflight_fingerprint: Option<&str>,
        _wait_seconds: u64,
        _runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        Ok(None)
    }

    async fn heartbeat(&self, _progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        Err(ClientError::Protocol)
    }

    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        self.submitted.lock().unwrap().push(result.clone());
        if is_core(result) && self.core_accepted {
            Ok(())
        } else {
            Err(ClientError::ResultRejected(Box::new(ingress_refusal())))
        }
    }
}

fn completed_failed_result(state: &mut StateStore) -> AgentResult {
    let claim = claim();
    assert!(matches!(
        state.begin(&claim, Utc::now()).unwrap(),
        BeginDecision::Execute
    ));
    let failed = Failure::new("the install failed")
        .diagnostic("helper exited")
        .code(FailureCode::RecipeInstallFailed);
    state
        .finish(&claim, ExecutionResult::Failed(failed))
        .unwrap()
}

async fn deliver(client: &EvidenceRefusingClient, state: &mut StateStore) {
    run_once_with_heartbeat_interval(
        client,
        state,
        &RejectingExecutor,
        RunOncePolicy {
            preflight_fingerprint: None,
            wait_seconds: 0,
            runtime_identity: None,
            heartbeat_interval: Duration::from_millis(10),
            heartbeat_retry_interval: Duration::from_millis(1),
            lease_renewed: crate::systemd_notify::watchdog,
        },
        || Ok(()),
    )
    .await
    .unwrap();
}

#[test]
fn a_core_keeps_the_state_and_code_and_drops_only_evidence() {
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let full = completed_failed_result(&mut state);

    let core = core_only(&full).expect("a failure with evidence has a core");

    let (AgentResultResult::OutcomeFailed(full_failed), AgentResultResult::OutcomeFailed(failed)) =
        (&full.result, &core.result)
    else {
        panic!("both are failed outcomes");
    };
    assert_eq!(core.fence, full.fence);
    assert_eq!(core.state, full.state);
    assert_eq!(failed.code, full_failed.code);
    assert_eq!(failed.failure_kind, full_failed.failure_kind);
    assert!(full_failed.evidence.is_some() && failed.evidence.is_none());
    core.validate_for_operation(&claim().operation).unwrap();
    // A core is offered once: nothing is left to remove from it.
    assert_eq!(core_only(&core), None);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_result_refused_as_invalid_settles_with_its_core() {
    // Wrong implementation this catches: a result refused for its evidence
    // was re-offered unchanged, so its true state and code never reached the
    // Controller while the attempt waited.
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let full = completed_failed_result(&mut state);
    let client = EvidenceRefusingClient {
        core_accepted: true,
        submitted: Arc::new(Mutex::new(Vec::new())),
    };

    deliver(&client, &mut state).await;

    let submitted = client.submitted.lock().unwrap().clone();
    assert_eq!(submitted.len(), 2);
    assert_eq!(submitted[0], full);
    assert!(is_core(&submitted[1]));
    // Accepted: acknowledged and no refusal retained.  The one reconciling
    // resend settles the same way, and then nothing is offered again.
    assert!(state.pending_results().unwrap().is_empty());
    assert!(state.result_rejection(&full, Utc::now()).unwrap().is_none());
    deliver(&client, &mut state).await;
    let settled = client.submitted.lock().unwrap().len();
    deliver(&client, &mut state).await;
    assert_eq!(client.submitted.lock().unwrap().len(), settled);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_refused_core_leaves_the_full_result_retained_and_visible() {
    // The core is offered once; refused too, the full result keeps its
    // custody and the first refusal, re-offered under the cool-down so a
    // corrected Controller accepts it, never hot-looped.
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let full = completed_failed_result(&mut state);
    let client = EvidenceRefusingClient {
        core_accepted: false,
        submitted: Arc::new(Mutex::new(Vec::new())),
    };

    deliver(&client, &mut state).await;
    deliver(&client, &mut state).await;

    assert_eq!(client.submitted.lock().unwrap().len(), 2);
    assert_eq!(state.pending_results().unwrap().len(), 1);
    let rejection = state
        .result_rejection(&full, Utc::now())
        .unwrap()
        .expect("the refusal is retained");
    assert_eq!(rejection.http_status, 422);
    assert!(rejection.retry_due_at > Utc::now());
}
