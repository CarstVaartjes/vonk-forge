#![cfg(test)]

use super::*;

pub(in crate::executor) const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

pub(in crate::executor) struct NoProcess;

impl ProcessRunner for NoProcess {
    fn run(
        &self,
        _program: Program,
        _arguments: &[String],
        _timeout: Duration,
    ) -> Result<ProcessOutput, ProcessError> {
        panic!("corrupt lifecycle enumeration must not execute a process")
    }
}

pub(in crate::executor) struct ObservationServer {
    pub(in crate::executor) client: AgentHttpClient,
    pub(in crate::executor) stop: Arc<AtomicBool>,
    pub(in crate::executor) worker: thread::JoinHandle<Vec<Value>>,
}

impl ObservationServer {
    pub(in crate::executor) fn new(status: Option<u16>) -> Self {
        Self::with_disposition(status, None)
    }

    /// Also answer run disposition lookups: `Some("unowned")` sends the
    /// Controller's header, `Some("")` answers known (no header), and
    /// each lookup is recorded as `{"disposition": "<request path>"}`.
    pub(in crate::executor) fn with_disposition(
        status: Option<u16>,
        disposition: Option<&'static str>,
    ) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = stop.clone();
        let worker = thread::spawn(move || {
            let mut reports = Vec::new();
            while !stopped.load(Ordering::SeqCst) {
                let (mut stream, _) = match listener.accept() {
                    Ok(connection) => connection,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(1));
                        continue;
                    }
                    Err(error) => panic!("observation listener: {error}"),
                };
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .unwrap();
                let mut request = Vec::new();
                let mut buffer = [0_u8; 4096];
                let header_end = loop {
                    let size = stream.read(&mut buffer).unwrap();
                    assert_ne!(size, 0);
                    request.extend_from_slice(&buffer[..size]);
                    if let Some(index) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
                        break index + 4;
                    }
                };
                let headers = std::str::from_utf8(&request[..header_end]).unwrap();
                if let Some(path) = headers
                    .strip_prefix("GET ")
                    .and_then(|line| line.split_once(" HTTP/1.1\r\n"))
                    .map(|(path, _)| path.to_owned())
                {
                    let disposition = disposition.expect("unexpected disposition lookup");
                    assert!(
                        path.starts_with("/agent/recipe-runs/") && path.ends_with("/disposition")
                    );
                    reports.push(json!({ "disposition": path }));
                    let header = if disposition.is_empty() {
                        String::new()
                    } else if let Some(generation) = disposition.strip_prefix("generation=") {
                        format!("x-vonk-recipe-run-generation: {generation}\r\n")
                    } else {
                        format!("x-vonk-recipe-run-disposition: {disposition}\r\n")
                    };
                    write!(
                        stream,
                        "HTTP/1.1 204 Response\r\n{header}Connection: close\r\n\r\n"
                    )
                    .unwrap();
                    continue;
                }
                assert!(headers.starts_with("POST /agent/recipe-runs/observations HTTP/1.1\r\n"));
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().unwrap())
                    })
                    .unwrap();
                while request.len() - header_end < content_length {
                    let size = stream.read(&mut buffer).unwrap();
                    assert_ne!(size, 0);
                    request.extend_from_slice(&buffer[..size]);
                }
                reports.push(serde_json::from_slice(&request[header_end..]).unwrap());
                if let Some(status) = status {
                    write!(stream, "HTTP/1.1 {status} Response\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
                }
            }
            reports
        });
        Self {
            client: AgentHttpClient::for_http_test(
                &format!("http://{address}/"),
                "spk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ),
            stop,
            worker,
        }
    }

    pub(in crate::executor) fn finish(self) -> Vec<Value> {
        self.stop.store(true, Ordering::SeqCst);
        self.worker.join().unwrap()
    }
}

pub(in crate::executor) fn exact_observation(
    run_id: Uuid,
) -> crate::client::ExactRecipeRunObservation {
    crate::client::ExactRecipeRunObservation {
        run_id,
        run_generation: 3,
        process_running: true,
        endpoint_ready: None,
        failure_diagnostics: None,
    }
}

#[derive(Clone)]
pub(in crate::executor) struct RecordingClient {
    pub(in crate::executor) cancel_requested: bool,
    pub(in crate::executor) claim: Arc<Mutex<Option<AgentClaim>>>,
    pub(in crate::executor) fail_heartbeat: bool,
    pub(in crate::executor) heartbeats: Arc<Mutex<Vec<AgentProgress>>>,
    pub(in crate::executor) results: Arc<Mutex<Vec<AgentResult>>>,
}

#[async_trait]
impl LoopClient for RecordingClient {
    async fn claim(
        &self,
        _preflight_fingerprint: Option<&str>,
        _wait_seconds: u64,
        _runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        Ok(self.claim.lock().unwrap().take())
    }

    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        self.heartbeats.lock().unwrap().push(progress.clone());
        if self.fail_heartbeat && self.heartbeats.lock().unwrap().len() == 1 {
            return Err(ClientError::Retryable);
        }
        Ok(AgentDirective {
            cancel_requested: self.cancel_requested,
            deadline: (Utc::now() + ChronoDuration::seconds(30)).fixed_offset(),
            fence: progress.fence,
        })
    }

    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        self.results.lock().unwrap().push(result.clone());
        Ok(())
    }
}

#[derive(Clone)]
pub(in crate::executor) struct SupersededCancellationClient(
    pub(in crate::executor) RecordingClient,
);

#[async_trait]
impl LoopClient for SupersededCancellationClient {
    async fn claim(
        &self,
        preflight_fingerprint: Option<&str>,
        wait_seconds: u64,
        runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        self.0
            .claim(preflight_fingerprint, wait_seconds, runtime_identity)
            .await
    }

    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        self.0.heartbeats.lock().unwrap().push(progress.clone());
        Err(ClientError::Controller(Box::new(ControllerError {
            operation: "controller.request /agent/heartbeat".to_owned(),
            endpoint: "/agent/heartbeat".to_owned(),
            status: 409,
            code: vonk_agent_protocol::generated::ControllerErrorCode::SupersededOperationCancelled
                .as_str()
                .to_owned(),
            request_id: None,
            decision: "exit",
            retry_after_seconds: None,
            summary: None,
        })))
    }

    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        self.0.submit_result(result).await
    }
}

#[derive(Clone)]
pub(in crate::executor) struct TerminalHeartbeatClient {
    pub(in crate::executor) inner: RecordingClient,
    pub(in crate::executor) panic: bool,
    pub(in crate::executor) recovered: Arc<AtomicBool>,
}

#[async_trait]
impl LoopClient for TerminalHeartbeatClient {
    async fn claim(
        &self,
        preflight_fingerprint: Option<&str>,
        wait_seconds: u64,
        runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        self.inner
            .claim(preflight_fingerprint, wait_seconds, runtime_identity)
            .await
    }

    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        if self.recovered.load(Ordering::SeqCst) {
            return self.inner.heartbeat(progress).await;
        }
        assert!(!self.panic, "heartbeat task failed unexpectedly");
        Err(ClientError::Identity)
    }

    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        self.inner.submit_result(result).await
    }
}

/// Refuses every renewal until ``lapsed_after`` and accepts them afterwards.
///
/// That is the shape of a Controller restart or a lost round trip: the
/// accepted lease runs out while the Controller is unreachable, then it
/// answers again.
#[derive(Clone)]
pub(in crate::executor) struct LeaseLapseClient {
    pub(in crate::executor) inner: RecordingClient,
    pub(in crate::executor) lapsed_after: DateTime<Utc>,
    pub(in crate::executor) accepted_at: Arc<Mutex<Vec<DateTime<Utc>>>>,
}

#[async_trait]
impl LoopClient for LeaseLapseClient {
    async fn claim(
        &self,
        preflight_fingerprint: Option<&str>,
        wait_seconds: u64,
        runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        self.inner
            .claim(preflight_fingerprint, wait_seconds, runtime_identity)
            .await
    }

    async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        self.inner.heartbeats.lock().unwrap().push(progress.clone());
        if Utc::now() < self.lapsed_after {
            return Err(ClientError::Retryable);
        }
        self.accepted_at.lock().unwrap().push(Utc::now());
        Ok(AgentDirective {
            cancel_requested: false,
            deadline: (Utc::now() + ChronoDuration::seconds(30)).fixed_offset(),
            fence: progress.fence,
        })
    }

    async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        self.inner.submit_result(result).await
    }
}

/// Keeps the work alive until a renewal is accepted.
///
/// The lease under test lapses in real time, so a fixed work duration would
/// race the executor's own finish against the renewal the test asserts.  It
/// still honours cancellation, so a loop that gives up early ends the test
/// promptly instead of waiting out the cap.
pub(in crate::executor) struct RenewalGatedExecutor {
    pub(in crate::executor) accepted: Arc<Mutex<Vec<DateTime<Utc>>>>,
    pub(in crate::executor) minimum: usize,
    pub(in crate::executor) cap: Duration,
    pub(in crate::executor) cancelled: Arc<AtomicBool>,
}

#[async_trait(?Send)]
impl Executor for RenewalGatedExecutor {
    async fn execute(
        &self,
        _claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        let deadline = std::time::Instant::now() + self.cap;
        while std::time::Instant::now() < deadline {
            if *cancellation.borrow() {
                self.cancelled.store(true, Ordering::SeqCst);
                break;
            }
            if self.accepted.lock().unwrap().len() >= self.minimum {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        recipe_install_success(1)
    }
}

pub(in crate::executor) struct BlockingCancellationExecutor {
    pub(in crate::executor) cancelled: Arc<AtomicBool>,
}

#[async_trait(?Send)]
impl Executor for BlockingCancellationExecutor {
    async fn execute(
        &self,
        _claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        // The real Podman runner is synchronous too. A select in the
        // parent cannot observe a failed heartbeat while this poll blocks.
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while std::time::Instant::now() < deadline {
            if *cancellation.borrow() {
                self.cancelled.store(true, Ordering::SeqCst);
                break;
            }
            thread::sleep(Duration::from_millis(1));
        }
        ExecutionResult::failed("build process ended")
    }
}

pub(in crate::executor) struct HeartbeatGatedExecutor {
    pub(in crate::executor) heartbeats: Arc<Mutex<Vec<AgentProgress>>>,
    pub(in crate::executor) minimum: usize,
    pub(in crate::executor) observed_deadline: Arc<Mutex<Option<DateTime<FixedOffset>>>>,
}

pub(in crate::executor) struct CancelledHeartbeatExecutor(
    pub(in crate::executor) HeartbeatGatedExecutor,
);

#[async_trait(?Send)]
impl Executor for CancelledHeartbeatExecutor {
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        self.0.execute(claim, lease_deadline, cancellation).await;
        ExecutionResult::cancelled("exact workload stop confirmed")
    }
}

#[async_trait(?Send)]
impl Executor for HeartbeatGatedExecutor {
    async fn execute(
        &self,
        _claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        _cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if self.heartbeats.lock().unwrap().len() >= self.minimum {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .expect("heartbeat task did not make progress");
        *self.observed_deadline.lock().unwrap() = Some(*lease_deadline.borrow());
        recipe_install_success(0)
    }
}

pub(in crate::executor) struct FailedExecutor;

#[async_trait(?Send)]
impl Executor for FailedExecutor {
    async fn execute(
        &self,
        _claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        _cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        ExecutionResult::failed("rootless image build failed")
    }
}

pub(in crate::executor) struct CancellationExecutor;

#[async_trait(?Send)]
impl Executor for CancellationExecutor {
    async fn execute(
        &self,
        claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        mut cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        let RecipeOperationRequest::JobRun(request) = RecipeOperationRequest::parse(claim).unwrap()
        else {
            panic!("expected canonical job claim");
        };
        let started = std::time::Instant::now();
        super::wait_for_cancellation(&mut cancellation).await;
        crate::executor::cancelled_job(&request, started, "controller cancellation requested")
    }
}

pub(in crate::executor) struct OrderingExecutor {
    pub(in crate::executor) events: Arc<Mutex<Vec<&'static str>>>,
}

#[async_trait(?Send)]
impl Executor for OrderingExecutor {
    async fn execute(
        &self,
        _claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        _cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        self.events.lock().unwrap().push("execute");
        recipe_install_success(0)
    }
}

/// What the Controller receives for a failed result: the finished outcome,
/// whose bounded diagnostics must be valid.
pub(in crate::executor) fn failed_outcome(
    claim: &AgentClaim,
    result: ExecutionResult,
) -> OutcomeFailed {
    let AgentResultResult::OutcomeFailed(failed) = result.finish(claim).result else {
        panic!("expected a failed outcome");
    };
    if let Some(diagnostics) = failed
        .evidence
        .as_ref()
        .and_then(|evidence| evidence.diagnostics.as_ref())
    {
        diagnostics.validate().unwrap();
    }
    failed
}

pub(in crate::executor) fn evidence_of(failed: &OutcomeFailed) -> &OutcomeEvidence {
    failed.evidence.as_ref().expect("failure evidence")
}

pub(in crate::executor) fn claim() -> AgentClaim {
    let plan: Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
    ))
    .unwrap();
    let payload = json!({
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "plan_digest": "a".repeat(64),
        "expected_bytes": 1,
        "compiled_execution_plan": plan,
    });
    let claim = AgentClaim {
        observation_budget_seconds: 3600,
        deadline: (Utc::now() + ChronoDuration::seconds(20))
            .with_timezone(&FixedOffset::east_opt(0).unwrap()),
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        operation: AgentOperation::RecipeInstall,
        payload: serde_json::from_value(payload).unwrap(),
    };
    RecipeOperationRequest::parse(&claim).unwrap();
    claim
}

pub(in crate::executor) fn count_watchdog_feed() {
    WATCHDOG_FEEDS.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
}

#[derive(Clone)]
pub(in crate::executor) struct RefusingResultClient {
    pub(in crate::executor) submitted: Arc<Mutex<Vec<AgentResult>>>,
}

#[async_trait]
impl LoopClient for RefusingResultClient {
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
        Err(ClientError::ResultSuperseded)
    }
}

pub(in crate::executor) fn ingress_refusal() -> ControllerError {
    ControllerError {
        operation: "controller.request /agent/result".to_owned(),
        endpoint: "/agent/result".to_owned(),
        status: 422,
        code: vonk_agent_protocol::generated::ControllerErrorCode::ControllerInvalidRequest
            .as_str()
            .to_owned(),
        request_id: Some("req-422".to_owned()),
        decision: "exit",
        retry_after_seconds: None,
        summary: Some(
            "request is invalid: body.result.AgentFailureResult.failure_kind \
             (is_instance_of)"
                .to_owned(),
        ),
    }
}

#[derive(Clone)]
pub(in crate::executor) struct IngressRejectingClient {
    pub(in crate::executor) accept: Arc<Mutex<bool>>,
    pub(in crate::executor) submitted: Arc<Mutex<Vec<AgentResult>>>,
}

#[async_trait]
impl LoopClient for IngressRejectingClient {
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
        if *self.accept.lock().unwrap() {
            Ok(())
        } else {
            Err(ClientError::ResultRejected(Box::new(ingress_refusal())))
        }
    }
}

pub(in crate::executor) fn completed_install_result(state: &mut StateStore) -> AgentResult {
    let claim = claim();
    assert!(matches!(
        state.begin(&claim, Utc::now()).unwrap(),
        BeginDecision::Execute
    ));
    state.finish(&claim, recipe_install_success(0)).unwrap()
}
