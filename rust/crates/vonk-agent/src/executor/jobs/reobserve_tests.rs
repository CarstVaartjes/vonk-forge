#![cfg(test)]
//! A job whose helper answer was lost is re-observed by its identity before
//! anything stops it. The helper and the Controller are real sockets: the
//! helper answers the job re-observation from a script, the Controller refuses
//! every grant, and both write one shared log so the order of what the agent
//! asked is the evidence.

use super::super::test_support::*;
use super::tests::job_claim;
use super::*;
use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use vonk_agent_protocol::generated::{
    HostHelperProcessLogs, HostHelperResponse, HostHelperResponseStatus,
};

/// What the helper answers to one job re-observation.
#[derive(Clone, Copy)]
pub(super) enum Answer {
    Running,
    Progressing,
    Exited(u32),
    Absent,
    /// A helper that predates job re-observation refuses the request.
    Refused,
}

type Log = Arc<Mutex<Vec<&'static str>>>;

pub(super) struct Peers {
    log: Log,
    done: Arc<AtomicBool>,
    pub(super) socket: PathBuf,
    controller: String,
    pub(super) runtime_root: tempfile::TempDir,
}

impl Peers {
    /// Every event is logged before its answer is written, so the log is
    /// complete once the agent has its answers.
    fn log(&self) -> Vec<&'static str> {
        self.log.lock().unwrap().clone()
    }
}

impl Drop for Peers {
    fn drop(&mut self) {
        self.done.store(true, Ordering::SeqCst);
    }
}

/// The next connection, until the test is done; a peer never outlives its
/// deadline.
fn accept<L>(
    done: &AtomicBool,
    deadline: Instant,
    accept: impl Fn() -> std::io::Result<L>,
) -> Option<L> {
    loop {
        assert!(Instant::now() < deadline, "test peer outlived its deadline");
        match accept() {
            Ok(stream) => return Some(stream),
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                if done.load(Ordering::SeqCst) {
                    return None;
                }
                std::thread::sleep(Duration::from_millis(1));
            }
            Err(error) => panic!("peer unavailable: {error}"),
        }
    }
}

/// A helper answering the job re-observation from `script` (its last answer
/// repeats), and a Controller that refuses every grant.
pub(super) fn peers(job_id: Uuid, script: Vec<Answer>) -> Peers {
    let log: Log = Arc::default();
    let done = Arc::new(AtomicBool::new(false));
    let runtime_root = tempdir().unwrap();
    let socket = runtime_root.path().join("helper.sock");
    let requests = runtime_root.path().join("runtime-requests");
    let helper = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    helper.set_nonblocking(true).unwrap();
    let controller = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    controller.set_nonblocking(true).unwrap();
    let address = controller.local_addr().unwrap();
    let deadline = Instant::now() + Duration::from_secs(60);
    let (helper_log, helper_done) = (log.clone(), done.clone());
    std::thread::spawn(move || {
        let mut answers = script.into_iter().peekable();
        let mut last = Answer::Running;
        while let Some((mut stream, _)) = accept(&helper_done, deadline, || helper.accept()) {
            stream.set_nonblocking(false).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut prefix = [0; 4];
            stream.read_exact(&mut prefix).unwrap();
            let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
            stream.read_exact(&mut body).unwrap();
            let request: vonk_agent_protocol::RecipeRunInspectionRequest =
                vonk_agent_protocol::parse_strict(&body).expect("only inspections reach it");
            // The re-observation names the job's whole identity.
            let runtime: vonk_agent_protocol::HostRuntimeRequest = serde_json::from_slice(
                &fs::read(requests.join(format!("{}.json", request.request_sha256))).unwrap(),
            )
            .unwrap();
            assert_eq!(
                runtime.arguments[..2],
                [
                    vonk_agent_protocol::JOB_REOBSERVATION_ARGUMENT.to_owned(),
                    job_id.to_string()
                ]
            );
            last = answers.next().unwrap_or(last);
            let mut response = HostHelperResponse {
                installation_intent_nonce: None,
                schema_version: 1,
                request_id: Some(request.request_id),
                status: HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
                process_running: Some(true),
                job_progress_at: None,
                exit_code: None,
                error_code: None,
                diagnostic: None,
                process_logs: None,
            };
            let event = match last {
                Answer::Running => "running",
                Answer::Progressing => {
                    response.job_progress_at = Some(Utc::now().fixed_offset());
                    "progressing"
                }
                Answer::Exited(code) => {
                    response.process_running = Some(false);
                    response.exit_code = Some(code);
                    response.diagnostic = Some(format!("exit_code={code}"));
                    response.process_logs = Some(HostHelperProcessLogs {
                        exit_code: None,
                        stdout: crate::failure_evidence::log_tail(b"job output\n"),
                        stderr: crate::failure_evidence::log_tail(b"job error\n"),
                    });
                    "exited"
                }
                Answer::Absent | Answer::Refused => {
                    response.status = HostHelperResponseStatus::Rejected;
                    response.process_running = None;
                    response.error_code = Some(if matches!(last, Answer::Absent) {
                        HelperErrorCode::RuntimeRunMissing
                    } else {
                        HelperErrorCode::OperationInvalid
                    });
                    if matches!(last, Answer::Absent) {
                        "absent"
                    } else {
                        "refused"
                    }
                }
            };
            helper_log.lock().unwrap().push(event);
            let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
            stream
                .write_all(&(body.len() as u32).to_be_bytes())
                .unwrap();
            stream.write_all(&body).unwrap();
        }
    });
    let (controller_log, controller_done) = (log.clone(), done.clone());
    std::thread::spawn(move || {
        while let Some((mut stream, _)) = accept(&controller_done, deadline, || controller.accept())
        {
            stream.set_nonblocking(false).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            while !request.windows(4).any(|part| part == b"\r\n\r\n") {
                assert!(Instant::now() < deadline, "request headers never ended");
                let read = stream.read(&mut buffer).unwrap();
                assert!(read > 0);
                request.extend_from_slice(&buffer[..read]);
            }
            assert!(request.starts_with(b"POST /agent/host-runtime/grant "));
            controller_log.lock().unwrap().push("grant");
            write!(
                stream,
                "HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            .unwrap();
        }
    });
    Peers {
        log,
        done,
        socket,
        controller: format!("http://{address}/"),
        runtime_root,
    }
}

/// Settle a dispatched job whose helper call broke (the helper restarted
/// while it waited), dispatched `ago`.
async fn settle_broken_call(
    script: Vec<Answer>,
    ago: Duration,
) -> (
    Result<HostRuntimeOutcome, Box<ExecutionResult>>,
    Vec<&'static str>,
) {
    let (claim, request) = job_claim();
    let stop_plan = exact_stop_plan_from_claim(&claim, &request.run_id.to_string(), true).unwrap();
    let peers = peers(request.job_id, script);
    let data = tempdir().unwrap();
    let client = AgentHttpClient::for_http_test(&peers.controller, NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: &peers.socket,
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
        runtime_root: peers.runtime_root.path(),
    };
    let job_scope = request.job_id.to_string();
    let job = DispatchedJob {
        claim: &claim,
        request: &request,
        stop_plan: &stop_plan,
        job_scope: &job_scope,
        started: Instant::now(),
    };
    let (_cancel, mut cancellation) = tokio::sync::watch::channel(false);
    let broken = InterruptibleJob::Completed(Err(crate::host_runtime::HostRuntimeError::Io(
        std::io::ErrorKind::UnexpectedEof.into(),
    )));
    let settled = executor
        .settle_job_dispatch(&job, broken, Instant::now() - ago, &mut cancellation)
        .await;
    (settled, peers.log())
}

/// What a job that ended without an observed exit says: it ends failed,
/// never as an unknown the Controller would observe without end.
fn cause(result: &ExecutionResult) -> String {
    let ExecutionResult::Failed(failure) = result else {
        panic!("a job without an observed exit ends failed: {result:?}");
    };
    failure.diagnostic.clone().unwrap_or_default()
}

#[tokio::test]
async fn a_helper_restart_mid_job_resumes_the_wait_and_reports_the_real_exit() {
    let (settled, log) = settle_broken_call(
        vec![Answer::Running, Answer::Running, Answer::Exited(7)],
        Duration::ZERO,
    )
    .await;
    let outcome = settled.unwrap_or_else(|result| panic!("the job's own exit: {result:?}"));
    assert_eq!(outcome.exit_code, Some(7));
    assert!(outcome.process_logs.is_some());
    // Nothing stopped it while it ran: the only stop asked for removes the
    // exited container, after its exit was read.
    assert_eq!(log[..3], ["running", "running", "exited"]);
    assert!(log[3..].iter().all(|event| *event == "grant"));
}

#[tokio::test]
async fn proven_absence_ends_the_job_with_that_evidence_and_no_stop() {
    let (settled, log) = settle_broken_call(vec![Answer::Absent], Duration::ZERO).await;
    let result = settled.err().expect("no exit was observed");
    assert!(cause(&result).contains("absent"), "{}", cause(&result));
    assert_eq!(log, ["absent"]);
}

#[tokio::test]
async fn a_job_still_running_at_its_deadline_is_stopped() {
    let (_claim, request) = job_claim();
    let timeout = request.compiled_execution_plan.job.as_ref().unwrap();
    let grace = request
        .compiled_execution_plan
        .lifecycle
        .stop_timeout_seconds;
    let ago = Duration::from_secs(u64::from(timeout.timeout_seconds + grace) + 1);
    let (settled, log) = settle_broken_call(vec![Answer::Running], ago).await;
    let result = settled.err().expect("a job stopped at its deadline");
    // The test helper refuses every grant, so the stop stays unconfirmed and
    // the end says so.
    let cause = cause(&result);
    assert!(cause.contains("deadline and stop grace"), "{cause}");
    assert!(cause.contains("stop unconfirmed"), "{cause}");
    assert_eq!(log, ["running", "grant"]);
}

#[tokio::test]
async fn a_helper_without_job_reobservation_keeps_the_stop_it_had() {
    let (settled, log) = settle_broken_call(vec![Answer::Refused], Duration::ZERO).await;
    let result = settled.err().expect("no exit was observed");
    assert!(
        cause(&result).contains("cannot re-observe"),
        "{}",
        cause(&result)
    );
    assert_eq!(log, ["refused", "grant"]);
}

#[tokio::test]
async fn an_unreadable_retained_exit_reports_the_exit_the_container_recorded() {
    let (claim, request) = job_claim();
    let peers = peers(request.job_id, vec![Answer::Exited(3)]);
    let data = tempdir().unwrap();
    let client = AgentHttpClient::for_http_test(&peers.controller, NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: &peers.socket,
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
        runtime_root: peers.runtime_root.path(),
    };
    // Dispatched (its intent is durable), then its exit account was lost.
    fs::create_dir_all(
        data.path()
            .join("runs")
            .join(request.job_id.to_string())
            .join("outputs"),
    )
    .unwrap();
    executor.runtime.persist_job_intent(&request).unwrap();
    let (_lease, lease) = tokio::sync::watch::channel(claim.deadline);
    let (_cancel, cancel) = tokio::sync::watch::channel(false);
    let result = executor
        .execute_job_run(&claim, lease, cancel, request)
        .await;
    let ExecutionResult::Failed(failure) = &result else {
        panic!("the recorded exit is the job's result: {result:?}");
    };
    assert_eq!(
        failure.receipt.as_ref().map(|receipt| receipt.exit_code),
        Some(3)
    );
    assert!(failure.process_logs.is_some());
    assert_eq!(peers.log()[0], "exited");
}

/// Claim a job whose whole scope is gone, the helper answering `script`; with
/// `lease_ended` a fresh dispatch stops at its first download. The result and
/// what the agent asked, in order.
async fn claim_without_record(
    script: Vec<Answer>,
    lease_ended: bool,
) -> (ExecutionResult, Vec<&'static str>) {
    let (claim, request) = job_claim();
    let peers = peers(request.job_id, script);
    let data = tempdir().unwrap();
    let client = AgentHttpClient::for_http_test(&peers.controller, NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: &peers.socket,
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
        runtime_root: peers.runtime_root.path(),
    };
    let lease = if lease_ended {
        claim.deadline - chrono::Duration::minutes(5)
    } else {
        claim.deadline
    };
    let (_lease, lease) = tokio::sync::watch::channel(lease);
    let (_cancel, cancel) = tokio::sync::watch::channel(false);
    let result = executor
        .execute_job_run(&claim, lease, cancel, request)
        .await;
    (result, peers.log())
}

/// A job that ran before its whole scope was lost is read by its identity:
/// the exit the helper kept is its result, and it never runs twice.
#[tokio::test]
async fn a_job_without_its_record_reports_its_kept_exit_and_never_runs_again() {
    let (result, log) = claim_without_record(vec![Answer::Exited(3)], false).await;
    let receipt = match &result {
        ExecutionResult::Failed(failure) => failure.receipt.as_ref(),
        ExecutionResult::Unknown(unconfirmed) => unconfirmed.receipt.as_ref(),
        _ => None,
    };
    assert_eq!(
        receipt.map(|receipt| receipt.exit_code),
        Some(3),
        "{result:?}"
    );
    // Progress sampling may also read the same exit. Only cleanup asks for
    // authority; no inspection or lost receipt may relaunch the process.
    assert_eq!(log.last(), Some(&"grant"));
    assert!(log[..log.len() - 1].iter().all(|event| *event == "exited"));
}

/// Only proven absence permits fresh dispatch; an unsupported probe cannot
/// establish that a job was never executed.
#[tokio::test]
async fn a_job_never_seen_is_dispatched_afresh() {
    let (result, log) = claim_without_record(vec![Answer::Absent], true).await;
    assert_eq!(
        result.failure().and_then(|failure| failure.stage),
        Some(FailureStage::ArtifactDistribution),
        "{result:?}"
    );
    assert_eq!(log, ["absent"]);
}

/// A job whose re-observation was not read runs nothing: the attempt waits
/// at a stage the job reaches only before launch, so it is issued again.
#[tokio::test]
async fn a_job_whose_reobservation_was_unread_runs_nothing() {
    let (claim, request) = job_claim();
    let data = tempdir().unwrap();
    let runtime_root = tempdir().unwrap();
    let client = AgentHttpClient::for_http_test("http://127.0.0.1:9/", NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: &runtime_root.path().join("no-helper.sock"),
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
        runtime_root: runtime_root.path(),
    };
    let (_lease, lease) = tokio::sync::watch::channel(claim.deadline);
    let (_cancel, cancel) = tokio::sync::watch::channel(false);
    let result = executor
        .execute_job_run(&claim, lease, cancel, request)
        .await;
    let failure = result.failure().expect("a wait");
    assert_eq!(
        failure.failure_kind,
        Some(AgentFailureKind::TemporaryDependency)
    );
    assert_eq!(failure.stage, Some(FailureStage::RunStorage));
}

#[tokio::test]
async fn a_progressing_job_survives_a_lost_helper_reply_past_its_original_deadline() {
    let (settled, log) = settle_broken_call(
        vec![Answer::Progressing, Answer::Exited(7)],
        Duration::from_secs(7200),
    )
    .await;
    let outcome =
        settled.unwrap_or_else(|result| panic!("progressing job was stopped: {result:?}"));
    assert_eq!(outcome.exit_code, Some(7));
    assert_eq!(log[..2], ["progressing", "exited"]);
    assert!(log[2..].iter().all(|event| *event == "grant"));
}

#[tokio::test]
async fn an_unsupported_probe_never_authorizes_fresh_inference() {
    let (result, log) = claim_without_record(vec![Answer::Refused], true).await;
    assert!(matches!(result, ExecutionResult::Failed(_)));
    assert_ne!(
        result.failure().and_then(|failure| failure.stage),
        Some(FailureStage::ArtifactDistribution),
        "unsupported observation is not proof of absence"
    );
    assert_eq!(log, ["refused"]);
}

/// A restarted agent whose job outlives its lease: the shared observer and a
/// real journal, the job's container answered by the test helper.
struct Restarted {
    peers: Peers,
    data: tempfile::TempDir,
    client: AgentHttpClient,
    store: StateStore,
    claim: AgentClaim,
    request: vonk_agent_protocol::RecipeJobRunRequest,
}

impl Restarted {
    fn new(script: Vec<Answer>, lease_ago: ChronoDuration) -> Self {
        let (mut claim, request) = job_claim();
        claim.deadline = (Utc::now() - lease_ago).fixed_offset();
        let peers = peers(request.job_id, script);
        let data = tempdir().unwrap();
        let client = AgentHttpClient::for_http_test(&peers.controller, NODE_ID);
        let mut store = StateStore::open(&data.path().join("state.sqlite"), NODE_ID).unwrap();
        let accepted_at = claim.deadline.with_timezone(&Utc) - ChronoDuration::seconds(1);
        store.begin(&claim, accepted_at).unwrap();
        Self {
            peers,
            data,
            client,
            store,
            claim,
            request,
        }
    }

    async fn pass(&mut self, now: DateTime<Utc>) {
        let executor = RecipeExecutor {
            client: &self.client,
            helper_socket: &self.peers.socket,
            runtime: OciRuntime {
                runner: &NoProcess,
                data_root: self.data.path(),
            },
            runtime_root: self.peers.runtime_root.path(),
        };
        crate::executor::operation_observation::reobserve_operations(
            &mut self.store,
            &executor,
            None,
            now,
        )
        .await
        .unwrap();
    }

    fn result(&self) -> Option<AgentResultResult> {
        self.store
            .pending_results()
            .unwrap()
            .into_iter()
            .find(|(_, result)| result.fence == self.claim.fence)
            .map(|(_, result)| result.result)
    }
}

#[tokio::test]
async fn a_job_running_at_its_lease_deadline_reports_its_later_exit() {
    let mut agent = Restarted::new(vec![Answer::Running], ChronoDuration::zero());
    let lease = agent.claim.deadline.with_timezone(&Utc);
    agent.pass(lease).await;
    assert_eq!(agent.store.observation_claims().unwrap().len(), 1);
    assert_eq!(
        agent.peers.log(),
        ["running"],
        "nothing stops a job within its deadline"
    );

    // The job exits and its measured exit is retained.
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: agent.data.path(),
    };
    let scope = agent
        .data
        .path()
        .join("runs")
        .join(agent.request.job_id.to_string());
    fs::create_dir_all(scope.join("outputs")).unwrap();
    runtime.persist_job_intent(&agent.request).unwrap();
    runtime
        .persist_job_completion(&job_receipt(
            &agent.request,
            0,
            Instant::now(),
            empty_job_output_manifest(),
            None,
        ))
        .unwrap();
    agent.pass(lease + ChronoDuration::minutes(10)).await;
    assert!(
        matches!(agent.result(), Some(AgentResultResult::OutcomeDone(_))),
        "the job's exit is reported: {:?}",
        agent.result()
    );
    assert!(agent.store.observation_claims().unwrap().is_empty());
}

#[tokio::test]
async fn a_job_past_its_own_deadline_is_stopped_and_reported() {
    let (_claim, request) = job_claim();
    let job = request.compiled_execution_plan.job.as_ref().unwrap();
    let grace = request
        .compiled_execution_plan
        .lifecycle
        .stop_timeout_seconds;
    let ago = ChronoDuration::seconds(i64::from(job.timeout_seconds + grace) + 1);
    let mut agent = Restarted::new(vec![Answer::Running], ago);
    agent.pass(Utc::now()).await;
    let Some(AgentResultResult::OutcomeFailed(failure)) = agent.result() else {
        panic!("a job past its deadline ends failed: {:?}", agent.result());
    };
    let diagnostic = format!("{failure:?}");
    assert!(
        diagnostic.contains("deadline and stop grace"),
        "{diagnostic}"
    );
    // The test Controller refuses every grant, so the stop stays unconfirmed
    // and the end says so.
    assert!(diagnostic.contains("stop unconfirmed"), "{diagnostic}");
    assert_eq!(agent.peers.log(), ["running", "grant"]);
    assert!(agent.store.observation_claims().unwrap().is_empty());
}
