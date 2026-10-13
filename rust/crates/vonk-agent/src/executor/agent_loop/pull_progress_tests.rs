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
        store
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
