#![cfg(test)]
//! An image pull reports the bytes Docker reads through the loopback store,
//! and the order's heartbeat carries them to the Controller.
//!
//! Wrong implementations caught: a pull that reports no progress (its order's
//! stall window then acts as a fixed cap) and renewals that post no progress.

use super::super::test_support::*;
use super::*;
use crate::image_store::{LoopbackImageStore, StoreResponse, StoreUpstream};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

const BLOB: usize = 4096;
const DIGEST: &str = "0e16f117c4f95ba678a0d89ef1fe2cbf2c08bec568f9c55d4873272ac1d69b7e";

/// The Controller's image store: one blob, streamed in two chunks.
struct Blob;

#[async_trait]
impl StoreUpstream for Blob {
    async fn get(
        &self,
        _head: bool,
        _path: &str,
        _accept: Option<&str>,
        _range: Option<&str>,
    ) -> Result<StoreResponse, ClientError> {
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert("content-length", BLOB.to_string().parse().unwrap());
        let chunks = [Ok(vec![7_u8; BLOB / 2]), Ok(vec![7_u8; BLOB / 2])];
        Ok(StoreResponse {
            status: reqwest::StatusCode::OK,
            headers,
            body: futures_util::stream::iter(chunks).boxed(),
        })
    }
}

/// Renews through the recording client; observes progress as the real HTTP
/// client does, from its order snapshot.
#[derive(Clone)]
struct PullingClient {
    http: AgentHttpClient,
    inner: RecordingClient,
}

#[async_trait]
impl LoopClient for PullingClient {
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
    fn progress_observation(&self, fence: Uuid) -> Option<OperationProgress> {
        LoopClient::progress_observation(&self.http, fence)
    }
}

/// Pulls one blob through the loopback store, as Docker does, then keeps
/// the pull running (extraction) for a few renewals.
struct PullingExecutor {
    http: AgentHttpClient,
}

#[async_trait(?Send)]
impl Executor for PullingExecutor {
    async fn execute(
        &self,
        claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        _cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        self.http
            .set_progress_phase(claim.fence, ProgressPhase::Pulling);
        let pulled = Inactivity::new(Duration::from_secs(60));
        let progress = super::super::runtime::pull_progress(&self.http, claim.fence, &pulled);
        let store = LoopbackImageStore::bind().await.unwrap();
        let digest = format!("sha256:{DIGEST}");
        let address = store.pull_arguments(&digest, &digest)[0].clone();
        let ((), failure) = store
            .serve_while(&Blob, &progress, async {
                let mut docker = tokio::net::TcpStream::connect(&address).await.unwrap();
                docker
                    .write_all(
                        format!("GET /v2/vonk/runtime/blobs/{digest} HTTP/1.1\r\n\r\n").as_bytes(),
                    )
                    .await
                    .unwrap();
                let mut response = Vec::new();
                docker.read_to_end(&mut response).await.unwrap();
                tokio::time::sleep(Duration::from_millis(100)).await;
            })
            .await;
        assert!(failure.is_none(), "{failure:?}");
        assert_eq!(pulled.bytes(), BLOB as u64);
        ExecutionResult::failed("pull fixture ends")
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn image_pull_bytes_reach_the_order_heartbeat() {
    let directory = tempfile::tempdir().unwrap();
    let mut state = StateStore::open(&directory.path().join("state.sqlite"), NODE_ID).unwrap();
    let http = AgentHttpClient::for_http_test("http://127.0.0.1:1/", NODE_ID);
    let client = PullingClient {
        http: http.clone(),
        inner: RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(claim()))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        },
    };
    tokio::time::timeout(
        Duration::from_secs(10),
        run_once_with_heartbeat_interval(
            &client,
            &mut state,
            &PullingExecutor { http },
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
    let heartbeats = client.inner.heartbeats.lock().unwrap();
    let pulled = heartbeats
        .iter()
        .filter_map(|heartbeat| heartbeat.progress.as_ref())
        .find(|progress| progress.completed_items == Some(4096_u64.into()))
        .expect("a renewal carried the pull's progress to the Controller");
    assert_eq!(pulled.phase, ProgressPhase::Pulling.as_str());
    // Pull activity never inflates the order's artifact bytes.
    assert_eq!(pulled.completed_bytes, 0);
}

/// The Controller's image store refusing every read with `status`.
struct Refusing(u16);

#[async_trait]
impl StoreUpstream for Refusing {
    async fn get(
        &self,
        _head: bool,
        _path: &str,
        _accept: Option<&str>,
        _range: Option<&str>,
    ) -> Result<StoreResponse, ClientError> {
        Err(ClientError::Controller(Box::new(
            ControllerError::from_status(self.0),
        )))
    }
}

/// Docker's view of one refused read, and the helper's verdict on the pull.
async fn refused_pull(status: u16) -> crate::host_runtime::HostRuntimeError {
    let store = LoopbackImageStore::bind().await.unwrap();
    let digest = format!("sha256:{DIGEST}");
    let address = store.pull_arguments(&digest, &digest)[0].clone();
    let (pulled, failure) = store
        .serve_while(&Refusing(status), &|_| {}, async {
            let mut docker = tokio::net::TcpStream::connect(&address).await.unwrap();
            docker
                .write_all(
                    format!("GET /v2/vonk/runtime/blobs/{digest} HTTP/1.1\r\n\r\n").as_bytes(),
                )
                .await
                .unwrap();
            let mut response = String::new();
            docker.read_to_string(&mut response).await.unwrap();
            assert!(response.starts_with("HTTP/1.1 502"), "{response}");
            Err::<(), _>(crate::host_runtime::HostRuntimeError::HelperRejected {
                code: HelperErrorCode::RuntimeImageLoadFailed,
                diagnostic: Some("received unexpected HTTP status: 502 Bad Gateway".to_owned()),
                process_logs: None,
            })
        })
        .await;
    super::super::runtime::with_store_failure(pulled.unwrap_err(), failure)
}

// P6: Docker only sees a 502; before, the Controller's answer behind it stayed
// in the agent log and the order reported an anonymous pull failure.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_forwarded_pull_error_reaches_the_orders_evidence_with_its_cause() {
    for status in [404, 503] {
        let error = refused_pull(status).await;
        assert!(temporary_observation_error(&error));
        // What the distribution and install lanes report for a retried pull.
        let finished = temporary_preparation_failure(
            FailureStage::ArtifactDistribution,
            "image_pull",
            &error.cause(),
        )
        .finish_for(&AgentOperation::ArtifactDistributionV1);
        let evidence = serde_json::to_string(&finished.result).unwrap();
        assert!(evidence.contains(&format!("HTTP {status}")), "{evidence}");
        assert!(
            evidence.contains("image store upstream refused"),
            "{evidence}"
        );
        assert!(evidence.contains("502 Bad Gateway"), "{evidence}");
    }
    // A denied read is the pull's own refusal: it surfaces as denial, not as
    // a retried, anonymous helper failure.
    let denied = refused_pull(403).await;
    assert!(matches!(
        &denied,
        crate::host_runtime::HostRuntimeError::Controller(ClientError::Controller(error))
            if error.status == 403
    ));
    assert!(!temporary_observation_error(&denied));
}
