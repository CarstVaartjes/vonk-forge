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
    Exited(u32),
    Absent,
    /// A helper that predates job re-observation refuses the request.
    Refused,
}

type Log = Arc<Mutex<Vec<&'static str>>>;

pub(super) struct Peers {
    log: Log,
    done: Arc<AtomicBool>,
    threads: Vec<std::thread::JoinHandle<()>>,
    pub(super) socket: PathBuf,
    controller: String,
    pub(super) runtime_root: tempfile::TempDir,
}

impl Peers {
    fn log(mut self) -> Vec<&'static str> {
        self.stop();
        self.log.lock().unwrap().clone()
    }

    fn stop(&mut self) {
        self.done.store(true, Ordering::SeqCst);
        for thread in self.threads.drain(..) {
            thread.join().unwrap();
        }
    }
}

impl Drop for Peers {
    fn drop(&mut self) {
        self.stop();
    }
}

fn accept<L>(done: &AtomicBool, accept: impl Fn() -> std::io::Result<L>) -> Option<L> {
    loop {
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
    let (helper_log, helper_done) = (log.clone(), done.clone());
    let helper = std::thread::spawn(move || {
        let mut answers = script.into_iter().peekable();
        let mut last = Answer::Running;
        while let Some((mut stream, _)) = accept(&helper_done, || helper.accept()) {
            stream.set_nonblocking(false).unwrap();
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
                exit_code: None,
                error_code: None,
                diagnostic: None,
                process_logs: None,
            };
            let event = match last {
                Answer::Running => "running",
                Answer::Exited(code) => {
                    response.process_running = Some(false);
                    response.exit_code = Some(code);
                    response.diagnostic = Some(format!("exit_code={code}"));
                    response.process_logs = Some(HostHelperProcessLogs {
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
    let controller = std::thread::spawn(move || {
        while let Some((mut stream, _)) = accept(&controller_done, || controller.accept()) {
            stream.set_nonblocking(false).unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            while !request.windows(4).any(|part| part == b"\r\n\r\n") {
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
        threads: vec![helper, controller],
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

fn cause(result: &ExecutionResult) -> String {
    let ExecutionResult::Unknown(unconfirmed) = result else {
        panic!("an unobserved job effect is unknown: {result:?}");
    };
    unconfirmed.evidence.diagnostic.clone().unwrap_or_default()
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
    let ago = Duration::from_secs(u64::from(timeout.timeout_seconds) + 1);
    let (settled, log) = settle_broken_call(vec![Answer::Running], ago).await;
    let result = settled.err().expect("a job stopped at its deadline");
    assert!(cause(&result).contains("deadline"), "{}", cause(&result));
    assert_eq!(log, ["running", "grant"]);
}

#[tokio::test]
async fn a_helper_without_job_reobservation_keeps_the_stop_it_had() {
    let (settled, log) = settle_broken_call(vec![Answer::Refused], Duration::ZERO).await;
    assert!(matches!(
        settled.err().as_deref(),
        Some(ExecutionResult::Unknown(_))
    ));
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
