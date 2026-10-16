#![cfg(test)]
//! A Start that answered an error is read again before anything stops it. The
//! helper and the Controller are real sockets: the helper answers the start's
//! inspection from a script, the Controller refuses every grant (so a stop
//! shows as a grant request), and both write one shared log, so what the agent
//! asked, in order, is the evidence.

use super::super::super::test_support::*;
use super::*;
use std::io::{Read, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use vonk_agent_protocol::generated::{
    HostHelperProcessLogs, HostHelperResponse, HostHelperResponseStatus,
};

const RUN_ID: &str = "00000000-0000-4000-8000-0000000000aa";
/// The generation of the start that is read again.
const GENERATION: u64 = 7;

/// What the helper's inspection of the start's container answers.
#[derive(Clone, Copy)]
enum Seen {
    Running,
    Exited,
    Absent,
    /// The helper closes the connection without an answer.
    Unread,
}

type Log = Arc<Mutex<Vec<&'static str>>>;

/// What answered with an error and is read again.
enum Failed {
    /// The Start itself.
    Start(HostRuntimeError),
    /// A launched rank's granted inspection; the test Controller refuses its
    /// grant as unavailable, a temporary answer.
    RankInspection,
}

/// The next connection, until the test is done.
fn accept<L>(done: &AtomicBool, accept: impl Fn() -> std::io::Result<L>) -> Option<L> {
    let deadline = Instant::now() + Duration::from_secs(30);
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

fn start_claim() -> AgentClaim {
    let mut start = claim();
    start.operation = AgentOperation::RecipeStart;
    let plan: Value = serde_json::from_str(include_str!(
        "../../../../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
    ))
    .unwrap();
    start.payload = serde_json::from_value(json!({
        "run_id": RUN_ID,
        "installation_id": "00000000-0000-4000-8000-000000000001",
        "recipe_revision_id": "00000000-0000-4000-8000-0000000000bb",
        "mapping_id": "00000000-0000-4000-8000-0000000000cc",
        "run_generation": 1,
        "plan_digest": "a".repeat(64),
        "compiled_execution_plan": plan,
    }))
    .unwrap();
    start
}

/// Settle a Start that answered `error`, with the helper's inspection
/// answering `seen`; the result and what the agent asked, in order.
async fn settle(
    seen: Seen,
    error: HostRuntimeError,
) -> (Result<(), Box<ExecutionResult>>, Vec<&'static str>) {
    let (settled, asked, _) = settle_failed(seen, Failed::Start(error)).await;
    (settled, asked)
}

/// Settle what `failed` with the helper's inspection answering `seen`; the
/// result, what the agent asked, in order, and the generation each inspection
/// named.
async fn settle_failed(
    seen: Seen,
    failed: Failed,
) -> (
    Result<(), Box<ExecutionResult>>,
    Vec<&'static str>,
    Vec<Option<u64>>,
) {
    let log: Log = Arc::default();
    let named: Arc<Mutex<Vec<Option<u64>>>> = Arc::default();
    let done = Arc::new(AtomicBool::new(false));
    let runtime_root = tempdir().unwrap();
    let socket = runtime_root.path().join("helper.sock");
    let helper = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    helper.set_nonblocking(true).unwrap();
    let controller = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    controller.set_nonblocking(true).unwrap();
    let address = controller.local_addr().unwrap();
    let (helper_log, helper_done) = (log.clone(), done.clone());
    let (helper_named, requests) = (named.clone(), runtime_root.path().join("runtime-requests"));
    std::thread::spawn(move || {
        while let Some((mut stream, _)) = accept(&helper_done, || helper.accept()) {
            stream.set_nonblocking(false).unwrap();
            let mut prefix = [0; 4];
            stream.read_exact(&mut prefix).unwrap();
            let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
            stream.read_exact(&mut body).unwrap();
            let request: vonk_agent_protocol::RecipeRunInspectionRequest =
                vonk_agent_protocol::parse_strict(&body).expect("only inspections reach it");
            helper_log.lock().unwrap().push("inspect");
            let inspected: vonk_agent_protocol::HostRuntimeRequest = serde_json::from_slice(
                &std::fs::read(requests.join(format!("{}.json", request.request_sha256))).unwrap(),
            )
            .unwrap();
            helper_named.lock().unwrap().push(inspected.run_generation);
            let mut response = HostHelperResponse {
                installation_intent_nonce: None,
                schema_version: 1,
                request_id: Some(request.request_id),
                status: HostHelperResponseStatus::Rejected,
                process_running: None,
                exit_code: None,
                error_code: None,
                diagnostic: None,
                process_logs: None,
            };
            match seen {
                Seen::Running => {
                    response.status = HostHelperResponseStatus::ContainerRuntimeRequestExecuted;
                    response.process_running = Some(true);
                }
                Seen::Exited => {
                    response.error_code = Some(HelperErrorCode::RuntimeProcessExited);
                    response.diagnostic = Some("exit_code=3".to_owned());
                    response.process_logs = Some(HostHelperProcessLogs {
                        stdout: crate::failure_evidence::log_tail(b"loading\n"),
                        stderr: crate::failure_evidence::log_tail(b"out of memory\n"),
                    });
                }
                Seen::Absent => response.error_code = Some(HelperErrorCode::RuntimeRunMissing),
                Seen::Unread => continue,
            }
            let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
            stream
                .write_all(&(body.len() as u32).to_be_bytes())
                .unwrap();
            stream.write_all(&body).unwrap();
        }
    });
    let (controller_log, controller_done) = (log.clone(), done.clone());
    std::thread::spawn(move || {
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
    let data = tempdir().unwrap();
    let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: &socket,
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
        runtime_root: runtime_root.path(),
    };
    let arguments = ["the start's exact inspection arguments".to_owned()];
    let settled = match failed {
        Failed::Start(error) => {
            executor
                .reobserve_failed_start(&start_claim(), RUN_ID, GENERATION, &arguments, &error)
                .await
        }
        Failed::RankInspection => {
            executor
                .inspect_launched_rank(
                    &start_claim(),
                    RUN_ID,
                    GENERATION,
                    &arguments,
                    &tokio::sync::watch::channel(false).1,
                )
                .await
        }
    };
    done.store(true, Ordering::SeqCst);
    let asked = log.lock().unwrap().clone();
    let named = named.lock().unwrap().clone();
    (settled, asked, named)
}

/// The Start's reply was lost: the helper may have launched the container.
fn lost_reply() -> HostRuntimeError {
    HostRuntimeError::Io(std::io::ErrorKind::UnexpectedEof.into())
}

fn ended(settled: Result<(), Box<ExecutionResult>>) -> ExecutionResult {
    *settled.expect_err("the start ends")
}

#[tokio::test]
async fn a_start_whose_container_runs_goes_on_and_nothing_is_stopped() {
    let (settled, asked) = settle(Seen::Running, lost_reply()).await;
    assert!(settled.is_ok());
    assert_eq!(asked, ["inspect"]);
}

#[tokio::test]
async fn proven_absence_stops_nothing() {
    let (settled, asked) = settle(Seen::Absent, lost_reply()).await;
    // The lost reply still says the effect is unconfirmed; nothing is stopped.
    assert!(matches!(ended(settled), ExecutionResult::Unknown(_)));
    assert_eq!(asked, ["inspect"]);
}

#[tokio::test]
async fn an_unread_inspection_waits_and_stops_nothing() {
    let (settled, asked) = settle(Seen::Unread, lost_reply()).await;
    let ExecutionResult::Failed(failure) = ended(settled) else {
        panic!("a wait is a retryable failure");
    };
    assert_eq!(
        failure.failure_kind,
        Some(AgentFailureKind::TemporaryDependency)
    );
    assert_eq!(asked, ["inspect"]);
}

#[tokio::test]
async fn an_exited_container_is_read_before_its_stop() {
    let (settled, asked) = settle(Seen::Exited, lost_reply()).await;
    assert!(settled.is_err());
    assert_eq!(asked, ["inspect", "grant"]);
}

#[tokio::test]
async fn a_foreign_container_under_the_name_keeps_its_exact_stop() {
    let (settled, asked) = settle(
        Seen::Running,
        HostRuntimeError::HelperRejected {
            code: HelperErrorCode::OperationInvalidArtifact,
            diagnostic: None,
            process_logs: None,
        },
    )
    .await;
    assert!(settled.is_err());
    assert_eq!(asked, ["grant"]);
}

/// The start is read again only as its own generation: a container of an
/// older generation under the name is never adopted as this start's.
#[tokio::test]
async fn the_reobservation_names_the_starts_generation() {
    let (settled, _, named) = settle_failed(Seen::Running, Failed::Start(lost_reply())).await;
    assert!(settled.is_ok());
    assert_eq!(named, [Some(GENERATION)]);
}

/// A launched rank whose inspection answered a temporary error is read again
/// before anything stops it: running, it goes on; absent or unread, nothing is
/// stopped; exited, it is read before its exact stop.
#[tokio::test]
async fn a_rank_whose_inspection_failed_is_read_again_before_any_stop() {
    let (settled, asked, named) = settle_failed(Seen::Running, Failed::RankInspection).await;
    assert!(settled.is_ok());
    assert_eq!(asked, ["grant", "inspect"]);
    assert_eq!(named, [Some(GENERATION)]);
    for seen in [Seen::Absent, Seen::Unread] {
        let (settled, asked, _) = settle_failed(seen, Failed::RankInspection).await;
        assert!(settled.is_err());
        assert_eq!(asked, ["grant", "inspect"]);
    }
    let (settled, asked, _) = settle_failed(Seen::Exited, Failed::RankInspection).await;
    assert!(settled.is_err());
    assert_eq!(asked, ["grant", "inspect", "grant"]);
}
