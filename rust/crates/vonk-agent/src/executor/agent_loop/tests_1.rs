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

    {
        let results = client.results.lock().unwrap();
        assert_eq!(results[0].state, AgentResultState::Failed);
        let AgentResultResult::OutcomeFailed(failed) = &results[0].result else {
            panic!("a failed executor reports a typed failure");
        };
        vonk_agent_protocol::revalidate(failed).unwrap();
        failed
            .evidence
            .as_ref()
            .and_then(|evidence| evidence.diagnostics.as_ref())
            .expect("bounded diagnostics")
            .validate()
            .unwrap();
    }
    let mut fresh = claim();
    fresh.fence = Uuid::new_v4();
    *client.claim.lock().unwrap() = Some(fresh.clone());
    run_once(&client, &mut state, &FailedExecutor, None, 0, None)
        .await
        .unwrap();
    assert!(
        client
            .results
            .lock()
            .unwrap()
            .iter()
            .any(|result| result.fence == fresh.fence)
    );
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

    {
        let results = client.results.lock().unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].state, AgentResultState::Cancelled);
        let vonk_agent_protocol::generated::AgentResultResult::OutcomeFailed(outcome) =
            &results[0].result
        else {
            panic!("expected a typed cancelled outcome");
        };
        let result = outcome.receipt.as_ref().expect("the job receipt is kept");
        result.validate().unwrap();
        assert_eq!(result.job_id, request.job_id);
        assert_eq!(result.run_id, request.run_id);
        assert!(result.output_manifest.files.is_empty());
        assert_eq!(result.output_manifest.total_bytes, 0);
        assert_eq!(result.exit_code, 130);
    }
    let mut fresh = claim();
    fresh.fence = Uuid::new_v4();
    *client.claim.lock().unwrap() = Some(fresh.clone());
    run_once(&client, &mut state, &FailedExecutor, None, 0, None)
        .await
        .unwrap();
    assert!(
        client
            .results
            .lock()
            .unwrap()
            .iter()
            .any(|result| result.fence == fresh.fence)
    );
}

#[tokio::test]
async fn journal_finish_failure_ends_unknown_then_a_fresh_fence_executes_once() {
    #[derive(Clone)]
    struct JournalClient {
        inner: RecordingClient,
        lock: Arc<Mutex<Option<rusqlite::Connection>>>,
    }
    #[async_trait]
    impl LoopClient for JournalClient {
        async fn claim(
            &self,
            fingerprint: Option<&str>,
            wait_seconds: u64,
            identity: Option<&AgentRuntimeIdentity>,
        ) -> Result<Option<AgentClaim>, ClientError> {
            self.inner.claim(fingerprint, wait_seconds, identity).await
        }
        async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
            self.inner.heartbeat(progress).await
        }
        async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
            self.lock.lock().unwrap().take();
            self.inner.submit_result(result).await
        }
    }
    struct JournalExecutor {
        path: std::path::PathBuf,
        lock: Arc<Mutex<Option<rusqlite::Connection>>>,
        effects: std::sync::atomic::AtomicUsize,
    }
    #[async_trait(?Send)]
    impl Executor for JournalExecutor {
        async fn execute(
            &self,
            _claim: &AgentClaim,
            _deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
            _cancel: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            if self.effects.fetch_add(1, Ordering::SeqCst) == 0 {
                let connection = rusqlite::Connection::open(&self.path).unwrap();
                connection.execute_batch("BEGIN IMMEDIATE").unwrap();
                *self.lock.lock().unwrap() = Some(connection);
            }
            recipe_install_success(0)
        }
    }
    let root = tempdir().unwrap();
    let path = root.path().join("state.sqlite");
    let mut state = StateStore::open(&path, NODE_ID).unwrap();
    let lock = Arc::new(Mutex::new(None));
    let original = claim();
    let client = JournalClient {
        inner: RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(original.clone()))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        },
        lock: lock.clone(),
    };
    let executor = JournalExecutor {
        path,
        lock,
        effects: std::sync::atomic::AtomicUsize::new(0),
    };
    tokio::time::timeout(
        Duration::from_secs(5),
        run_once(&client, &mut state, &executor, None, 0, None),
    )
    .await
    .unwrap()
    .unwrap();
    assert!(matches!(
        client.inner.results.lock().unwrap()[0].result,
        AgentResultResult::OutcomeUnknown(_)
    ));
    assert!(executor.lock.lock().unwrap().is_none());
    let mut fresh = original;
    fresh.fence = Uuid::new_v4();
    *client.inner.claim.lock().unwrap() = Some(fresh.clone());
    tokio::time::timeout(
        Duration::from_secs(5),
        run_once(&client, &mut state, &executor, None, 0, None),
    )
    .await
    .unwrap()
    .unwrap();
    assert_eq!(executor.effects.load(Ordering::SeqCst), 2);
    assert!(
        client
            .inner
            .results
            .lock()
            .unwrap()
            .iter()
            .any(|result| result.fence == fresh.fence
                && matches!(result.result, AgentResultResult::OutcomeDone(_)))
    );
}
