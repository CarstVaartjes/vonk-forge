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
    async fn observe(
        &self,
        _claim: &AgentClaim,
    ) -> Option<crate::executor::operation_observation::OperationObservation> {
        None
    }

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

/// A distributed start whose accepted deadline is `after` from now.
fn start_claim(after: ChronoDuration) -> AgentClaim {
    let mut start = claim();
    start.operation = AgentOperation::RecipeStart;
    let mut plan: Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
    ))
    .unwrap();
    plan["runtime"]["placement"]["world_size"] = json!(2);
    plan["runtime"]["placement"]["local_address"] = json!("192.168.100.3");
    plan["runtime"]["placement"]["master_address"] = json!("192.168.100.2");
    plan["runtime"]["placement"]["master_port"] = json!(29500);
    start.payload = serde_json::from_value(json!({
        "run_id": "00000000-0000-4000-8000-0000000000aa",
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "recipe_revision_id": "00000000-0000-4000-8000-0000000000bb",
        "mapping_id": "00000000-0000-4000-8000-0000000000cc",
        "run_generation": 1,
        "plan_digest": "a".repeat(64),
        "compiled_execution_plan": plan,
        "phase": vonk_agent_protocol::generated::RecipeStartPayloadPhase::CollectiveReadiness.as_str(),
        "start_deadline": (Utc::now() + after).to_rfc3339(),
    }))
    .unwrap();
    start
}

// Wrong implementation the start case catches: renewal retries were capped at
// a start's accepted deadline even while the load kept progressing, so an
// unreachable Controller cancelled a model load that was still advancing.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_progressing_order_outlives_its_window_and_a_stalled_one_ends() {
    for (start, advancing) in [(false, true), (false, false), (true, true), (true, false)] {
        let directory = tempdir().unwrap();
        let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
        let mut order = if start {
            start_claim(ChronoDuration::seconds(1))
        } else {
            claim()
        };
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
    assert_eq!(after.completed_items, None);
    assert_eq!(after.activity_sequence, Some(4096_u64.into()));
    assert_eq!(rereported.activity_sequence, after.activity_sequence);
    // Outside the scope, and for another order, nothing advances.
    crate::process::note_activity(4096);
    client.note_process_activity(Uuid::new_v4(), 4096);
    let unchanged = LoopClient::progress_observation(&client, fence).unwrap();
    assert!(!unchanged.advanced_from(Some(&after)));
    assert!(LoopClient::progress_observation(&client, Uuid::new_v4()).is_none());
}

/// Answers every renewal through `inner`, which refuses the first one, and
/// reports the activity the executor observed as the order's progress.
#[derive(Clone)]
struct FlakyRenewalClient {
    inner: RecordingClient,
    activity: Arc<AtomicU64>,
}

#[async_trait]
impl LoopClient for FlakyRenewalClient {
    async fn claim(
        &self,
        fingerprint: Option<&str>,
        wait: u64,
        identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        self.inner.claim(fingerprint, wait, identity).await
    }
    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        self.inner.heartbeat(progress).await
    }
    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        self.inner.submit_result(result).await
    }
    fn progress_observation(&self, _fence: Uuid) -> Option<OperationProgress> {
        Some(OperationProgress {
            completed_items: Some(self.activity.load(Ordering::SeqCst).into()),
            ..phase_progress(ProgressPhase::Starting)
        })
    }
}

// P6: a progress post that fails loses nothing. Before, a load's log growth
// went out on a second, fire-and-forget post whose error (auth included) was
// discarded; now the order's one renewal lane carries it, reports the cause
// and the next renewal carries the same activity.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_failed_progress_post_is_carried_by_the_next_heartbeat() {
    let directory = tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let activity = Arc::new(AtomicU64::new(7));
    let client = FlakyRenewalClient {
        inner: RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(claim()))),
            fail_heartbeat: true,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        },
        activity,
    };
    let executor = HeartbeatGatedExecutor {
        heartbeats: client.inner.heartbeats.clone(),
        minimum: 2,
        observed_deadline: Arc::new(Mutex::new(None)),
    };
    run_once_with_heartbeat_interval(
        &client,
        &mut state,
        &executor,
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
    let heartbeats = client.inner.heartbeats.lock().unwrap().clone();
    assert!(heartbeats.len() >= 2);
    let items = |index: usize| {
        heartbeats[index]
            .progress
            .as_ref()
            .and_then(|progress| progress.completed_items.clone())
    };
    // The refused first post carried the activity; the next one carries it too.
    assert_eq!(items(0), Some(7_u64.into()));
    assert_eq!(items(1), items(0));
    assert_eq!(client.inner.results.lock().unwrap().len(), 1);
}
