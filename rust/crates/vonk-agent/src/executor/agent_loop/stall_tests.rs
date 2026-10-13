#![cfg(test)]

use super::super::test_support::*;
use super::*;
use std::sync::atomic::AtomicU64;

/// Exposes a progress counter the executor advances, like the HTTP client's
/// snapshot, while renewals never answer.
#[derive(Clone)]
struct ProgressingClient {
    inner: RecordingClient,
    progress: Arc<AtomicU64>,
}

#[async_trait]
impl LoopClient for ProgressingClient {
    async fn claim(
        &self,
        fingerprint: Option<&str>,
        wait: u64,
        identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        self.inner.claim(fingerprint, wait, identity).await
    }
    async fn heartbeat(&self, _progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        std::future::pending().await
    }
    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        self.inner.submit_result(result).await
    }
    fn progress_observation(&self, _fence: Uuid) -> Option<OperationProgress> {
        Some(OperationProgress {
            completed_bytes: self.progress.load(Ordering::SeqCst).into(),
            ..phase_progress(ProgressPhase::Building)
        })
    }
}

/// Works for several stall windows, advancing its progress when asked to.
struct LongExecutor {
    progress: Arc<AtomicU64>,
    advancing: bool,
    cancelled: Arc<AtomicBool>,
}

#[async_trait(?Send)]
impl Executor for LongExecutor {
    async fn execute(
        &self,
        _claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        let deadline = tokio::time::Instant::now() + Duration::from_millis(3500);
        while tokio::time::Instant::now() < deadline {
            if *cancellation.borrow() {
                self.cancelled.store(true, Ordering::SeqCst);
                break;
            }
            if self.advancing {
                self.progress.fetch_add(1, Ordering::SeqCst);
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        ExecutionResult::failed("build process ended")
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_progressing_order_outlives_its_window_and_a_stalled_one_ends() {
    for advancing in [true, false] {
        let directory = tempdir().unwrap();
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
        let mut order = claim();
        order.observation_budget_seconds = 1;
        let progress = Arc::new(AtomicU64::new(0));
        let client = ProgressingClient {
            inner: RecordingClient {
                cancel_requested: false,
                claim: Arc::new(Mutex::new(Some(order))),
                fail_heartbeat: false,
                heartbeats: Arc::new(Mutex::new(Vec::new())),
                results: Arc::new(Mutex::new(Vec::new())),
            },
            progress: progress.clone(),
        };
        let executor = LongExecutor {
            progress,
            advancing,
            cancelled: Arc::new(AtomicBool::new(false)),
        };
        let started = tokio::time::Instant::now();
        tokio::time::timeout(
            Duration::from_secs(6),
            run_once_with_heartbeat_interval(
                &client,
                &mut state,
                &executor,
                RunOncePolicy {
                    preflight_fingerprint: None,
                    wait_seconds: 0,
                    runtime_identity: None,
                    heartbeat_interval: Duration::from_millis(5),
                    heartbeat_retry_interval: Duration::from_millis(5),
                    lease_renewed: crate::systemd_notify::watchdog,
                },
                || Ok(()),
            ),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(executor.cancelled.load(Ordering::SeqCst), !advancing);
        if !advancing {
            assert!(started.elapsed() < Duration::from_millis(2500));
        }
        assert_eq!(client.inner.results.lock().unwrap().len(), 1);
    }
}

#[test]
fn subprocess_activity_advances_the_http_clients_order_progress() {
    let client = AgentHttpClient::for_http_test("http://127.0.0.1:9/", NODE_ID);
    let fence = Uuid::new_v4();
    client.set_progress_phase(fence, ProgressPhase::Building);
    let before = LoopClient::progress_observation(&client, fence).unwrap();
    let reporter = client.clone();
    {
        let _scope = crate::process::report_activity_to(Arc::new(move |amount| {
            reporter.note_process_activity(fence, amount);
        }));
        crate::process::note_activity(4096);
    }
    let after = LoopClient::progress_observation(&client, fence).unwrap();
    assert!(after.advanced_from(Some(&before)));
    assert!(!after.advanced_from(Some(&after)));
    // Re-reporting the same phase keeps the activity already observed.
    client.set_progress_phase(fence, ProgressPhase::Building);
    let rereported = LoopClient::progress_observation(&client, fence).unwrap();
    assert_eq!(rereported.completed_items, after.completed_items);
    // Outside the scope, and for another order, nothing advances.
    crate::process::note_activity(4096);
    client.note_process_activity(Uuid::new_v4(), 4096);
    let unchanged = LoopClient::progress_observation(&client, fence).unwrap();
    assert!(!unchanged.advanced_from(Some(&after)));
    assert!(LoopClient::progress_observation(&client, Uuid::new_v4()).is_none());
}
