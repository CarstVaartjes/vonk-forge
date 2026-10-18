#![cfg(test)]
//! An exited job whose outputs cannot be read yet: its exit is receipted, so
//! reading again is safe. Collection is retried until the job's own deadline
//! plus its stop grace, then the job ends failed with its evidence. Every
//! unconfirmed step after the exit carries the receipt, so the Controller
//! ends it instead of observing it without a bound.

use super::super::test_support::*;
use super::tests::job_claim;
use super::*;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;

/// A job that exited 0 with one declared output, receipted before any
/// output was observed, as the next claim of the same job finds it.
struct Exited {
    _data: tempfile::TempDir,
    _runtime_root: tempfile::TempDir,
    outputs: PathBuf,
    claim: AgentClaim,
    request: vonk_agent_protocol::RecipeJobRunRequest,
}

fn exited_job(timeout_seconds: u32, stop_timeout_seconds: u32) -> Exited {
    let (claim, mut request) = job_claim();
    if let Some(job) = request.compiled_execution_plan.job.as_mut() {
        job.timeout_seconds = timeout_seconds;
    }
    request
        .compiled_execution_plan
        .lifecycle
        .stop_timeout_seconds = stop_timeout_seconds;
    let data = tempdir().unwrap();
    let outputs = data
        .path()
        .join("runs")
        .join(request.job_id.to_string())
        .join("outputs");
    fs::create_dir_all(&outputs).unwrap();
    fs::write(outputs.join("result.png"), b"image").unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
    };
    runtime.persist_job_intent(&request).unwrap();
    runtime
        .persist_job_completion(&job_receipt(
            &request,
            0,
            Instant::now(),
            empty_job_output_manifest(),
            None,
        ))
        .unwrap();
    Exited {
        _data: data,
        _runtime_root: tempdir().unwrap(),
        outputs,
        claim,
        request,
    }
}

impl Exited {
    async fn claim_again(&self) -> ExecutionResult {
        self.claim_at("http://127.0.0.1:9/").await
    }

    async fn claim_at(&self, endpoint: &str) -> ExecutionResult {
        let client = AgentHttpClient::for_http_test(endpoint, NODE_ID);
        let executor = RecipeExecutor {
            client: &client,
            helper_socket: std::path::Path::new(crate::host_runtime::HELPER_SOCKET),
            runtime: OciRuntime {
                runner: &NoProcess,
                data_root: self._data.path(),
            },
            runtime_root: self._runtime_root.path(),
        };
        let (_lease, lease) = tokio::sync::watch::channel(self.claim.deadline);
        let (_cancel, cancel) = tokio::sync::watch::channel(false);
        executor
            .execute_job_run(&self.claim, lease, cancel, self.request.clone())
            .await
    }

    /// Make the output unreadable; false when this user reads it anyway
    /// (root ignores permission bits), so the caller skips.
    fn deny_output(&self) -> bool {
        let path = self.outputs.join("result.png");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o000)).unwrap();
        if File::open(&path).is_ok() {
            eprintln!("skipped: this user reads a mode-000 file (root)");
            return false;
        }
        true
    }

    fn allow_output(path: &Path) {
        fs::set_permissions(path, fs::Permissions::from_mode(0o644)).unwrap();
    }
}

#[tokio::test]
async fn unreadable_outputs_are_read_again_until_they_heal() {
    let job = exited_job(60, 30);
    if !job.deny_output() {
        return;
    }
    let path = job.outputs.join("result.png");
    let healer = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(300)).await;
        Exited::allow_output(&path);
    });
    let result = job.claim_again().await;
    healer.await.unwrap();
    // Collection healed: the output was read whole and its delivery started.
    // The test Controller is unreachable, so delivery stays unconfirmed, with
    // the receipt and the collected manifest.
    let ExecutionResult::Unknown(unconfirmed) = &result else {
        panic!("delivery to an unreachable Controller is unconfirmed: {result:?}");
    };
    let receipt = unconfirmed
        .receipt
        .as_ref()
        .expect("the exit travels with it");
    let names = receipt
        .output_manifest
        .files
        .iter()
        .map(|file| file.name.as_str())
        .collect::<Vec<_>>();
    assert_eq!(names, ["result.png"]);
}

#[tokio::test]
async fn outputs_unreadable_past_the_deadline_and_grace_end_the_job_failed() {
    // A one-second job with a one-second stop grace.
    let job = exited_job(1, 1);
    if !job.deny_output() {
        return;
    }
    let result = job.claim_again().await;
    let failure = result
        .failure()
        .expect("a definite failure, not an unknown");
    assert_eq!(failure.stage, Some(FailureStage::OutputStorage));
    let diagnostic = failure.diagnostic.as_deref().unwrap_or_default();
    for fact in [
        "exit_code=0",
        "cause=job output could not be read",
        "entries=1",
        "result.png",
    ] {
        assert!(diagnostic.contains(fact), "{diagnostic}");
    }
    // It ends the job on the wire: a failed outcome, never observed again.
    let AgentResultResult::OutcomeFailed(outcome) = result.finish(&job.claim).result else {
        panic!("failed outcome");
    };
    assert_eq!(outcome.code, FailureCode::RecipeJobRunFailed);
    // The job's files stay for the retention sweep.
    Exited::allow_output(&job.outputs.join("result.png"));
    assert!(job.outputs.join("result.png").exists());
}

#[tokio::test]
async fn an_unconfirmed_step_after_the_exit_carries_the_receipt() {
    // The output root itself is not a directory: a local fault after the
    // exit. The process ceased, so the unknown carries its receipt and the
    // Controller ends the order with that exit.
    let job = exited_job(60, 30);
    fs::remove_dir_all(&job.outputs).unwrap();
    fs::write(&job.outputs, b"not a directory").unwrap();
    let result = job.claim_again().await;
    let ExecutionResult::Unknown(unconfirmed) = &result else {
        panic!("a local fault is unconfirmed: {result:?}");
    };
    assert_eq!(unconfirmed.evidence.stage, FailureStage::OutputStorage);
    assert_eq!(
        unconfirmed
            .receipt
            .as_ref()
            .map(|receipt| receipt.exit_code),
        Some(0)
    );
    let AgentResultResult::OutcomeUnknown(outcome) = result.finish(&job.claim).result else {
        panic!("unknown outcome");
    };
    assert!(outcome.receipt.is_some());
}

#[tokio::test]
async fn refused_output_upload_is_redelivered_from_the_durable_exit_receipt() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let job = exited_job(60, 30);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}/", listener.local_addr().unwrap());
    let uploads = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let observed = uploads.clone();
    let peer = tokio::spawn(async move {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        while tokio::time::Instant::now() < deadline {
            let Ok(Ok((mut stream, _))) =
                tokio::time::timeout_at(deadline, listener.accept()).await
            else {
                break;
            };
            let mut request = Vec::new();
            let header_end = loop {
                let mut byte = [0];
                tokio::time::timeout_at(deadline, stream.read_exact(&mut byte))
                    .await
                    .unwrap()
                    .unwrap();
                request.push(byte[0]);
                if request.ends_with(b"\r\n\r\n") {
                    break request.len();
                }
            };
            let headers = String::from_utf8_lossy(&request).to_ascii_lowercase();
            let length: usize = headers
                .lines()
                .find_map(|line| line.strip_prefix("content-length:").map(str::trim))
                .unwrap_or("0")
                .parse()
                .unwrap();
            request.resize(header_end + length, 0);
            stream.read_exact(&mut request[header_end..]).await.unwrap();
            let status = if headers.starts_with("put ") {
                assert_eq!(&request[header_end..], b"image");
                if observed.fetch_add(1, std::sync::atomic::Ordering::SeqCst) == 0 {
                    "413 Payload Too Large"
                } else {
                    "204 No Content"
                }
            } else {
                "204 No Content"
            };
            stream
                .write_all(
                    format!("HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                        .as_bytes(),
                )
                .await
                .unwrap();
        }
    });
    let first = job.claim_at(&endpoint).await;
    let ExecutionResult::Unknown(pending) = first else {
        panic!("refused upload must remain retryable");
    };
    assert_eq!(pending.evidence.stage, FailureStage::JobOutputDelivery);
    assert_eq!(fs::read(job.outputs.join("result.png")).unwrap(), b"image");
    // NoProcess panics if a retry executes the user's process. A new executor
    // reads the saved receipt and repeats only the content-addressed PUT.
    let mut job = job;
    job.request.output_delivery_only = Some(true);
    let delivered = job.claim_at(&endpoint).await;
    assert_eq!(delivered.state(), AgentResultState::Succeeded);
    assert_eq!(uploads.load(std::sync::atomic::Ordering::SeqCst), 2);
    peer.abort();
}

#[tokio::test]
async fn a_delivery_only_retry_with_a_lost_scope_never_dispatches_the_process() {
    let mut job = exited_job(60, 30);
    job.request.output_delivery_only = Some(true);
    fs::remove_dir_all(job.outputs.parent().unwrap()).unwrap();
    // NoProcess and the absent helper both detect falling through to launch.
    let result = job.claim_again().await;
    assert_eq!(result.state(), AgentResultState::Failed);
    let failure = result.failure().unwrap();
    assert_eq!(failure.stage, Some(FailureStage::JobOutputDelivery));
    assert!(failure.reason.contains("delivery-only retry cannot launch"));
}
