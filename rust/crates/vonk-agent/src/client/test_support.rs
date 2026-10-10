#![cfg(test)]

pub(super) const TEST_PLAN_DIGEST: &str =
    "abababababababababababababababababababababababababababababababab";

pub(super) const TEST_NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

use super::*;

pub(super) use crate::{
    oci::OciRuntime,
    process::{ProcessError, ProcessOutput, ProcessRunner, Program},
    telemetry::TelemetrySample,
    workloads::CompiledExecutionPlan,
};

pub(super) use chrono::{DateTime, Utc};

pub(super) use serde_json::{Value, json};

pub(super) use std::{
    collections::HashMap,
    io::{Read, Write},
    net::TcpListener,
    sync::Arc,
    thread,
    time::Duration,
};

pub(super) use tokio::sync::RwLock;

pub(super) use url::Url;

pub(super) use uuid::Uuid;

pub(super) use vonk_agent_protocol::generated::{
    AgentClaimPayload, AgentOperation, ArtifactDistributionPayload, OperationProgress,
    ProgressPhase,
};

pub(super) use vonk_agent_protocol::{AgentDirective, AgentProgress, canonical_json, hex_sha256};

pub(super) struct NoProcess;

pub(super) fn valid_inventory() -> vonk_agent_protocol::InventoryRequest {
    vonk_agent_protocol::InventoryRequest {
        schema_version: 1,
        observed_at: Utc::now().into(),
        disk_total_bytes: 100,
        disk_free_bytes: 50,
        host_memory_total_bytes: 100,
        host_memory_free_bytes: 50,
        gpu_memory_total_bytes: 100,
        gpu_memory_free_bytes: 50,
        gpu_count: 1,
        memory_pool: vonk_agent_protocol::generated::InventoryRequestMemoryPool::Separate,
        artifact_store_read_only: false,
        capabilities: vec!["runtime.oci".to_owned()],
        fabric_address: None,
        fabric_bandwidth_mbps: None,
        network_interfaces: None,
        nas_route_interface: None,
        nvidia_driver_version: "580.1".to_owned(),
        container_runtime_version: "podman 5".to_owned(),
    }
}

pub(super) fn interface(
    name: &str,
    speed: Option<u32>,
) -> vonk_agent_protocol::generated::NetworkInterface {
    vonk_agent_protocol::generated::NetworkInterface {
        name: name.to_owned(),
        kind: vonk_agent_protocol::generated::NetworkInterfaceKind::Wired,
        link_speed_mbps: speed,
        carrier: true,
    }
}

impl ProcessRunner for NoProcess {
    fn run(&self, _: Program, _: &[String], _: Duration) -> Result<ProcessOutput, ProcessError> {
        panic!("distribution/install handoff test must not launch a process");
    }
}

// These capture cases share one deadline across peer acceptance, request
// reads, response writes and observation. A failed client cannot leave a
// blocking fixture thread behind the test's final join.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum CaptureStage {
    Headers,
    Body,
}

pub(super) struct CapturePeer {
    pub(super) task: tokio::task::JoinHandle<Vec<u8>>,
    pub(super) deadline: tokio::time::Instant,
    pub(super) stages: tokio::sync::mpsc::Receiver<CaptureStage>,
}

impl Drop for CapturePeer {
    fn drop(&mut self) {
        // Also cancel acceptance/read futures when the client assertion
        // fails before the explicit completion observation.
        self.task.abort();
    }
}

pub(super) async fn request_capture_client(
    response_status: u16,
    response_headers: Vec<String>,
    response_body: Vec<u8>,
    response_delay: Option<Duration>,
    budget: Duration,
) -> (AgentHttpClient, CapturePeer) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let deadline = tokio::time::Instant::now() + budget;
    let (stage_observer, stages) = tokio::sync::mpsc::channel(2);
    let server = tokio::spawn(async move {
        let (mut stream, _) = tokio::time::timeout_at(deadline, listener.accept())
            .await
            .expect("capture peer acceptance deadline")
            .unwrap();
        let mut request = Vec::new();
        let mut buffer = [0_u8; 4096];
        let header_end = loop {
            let size = tokio::time::timeout_at(deadline, stream.read(&mut buffer))
                .await
                .expect("capture request header deadline")
                .unwrap();
            assert_ne!(size, 0, "capture request ended before complete headers");
            request.extend_from_slice(&buffer[..size]);
            if let Some(index) = request.windows(4).position(|value| value == b"\r\n\r\n") {
                break index + 4;
            }
            // Nonblocking acknowledgement of an accepted, incomplete
            // request; the next read owns the same absolute deadline.
            let _ = stage_observer.try_send(CaptureStage::Headers);
        };
        let headers = std::str::from_utf8(&request[..header_end]).unwrap();
        let content_length = headers
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("content-length")
                    .then(|| value.trim().parse::<usize>().unwrap())
            })
            .unwrap_or(0);
        while request.len() - header_end < content_length {
            let _ = stage_observer.try_send(CaptureStage::Body);
            let size = tokio::time::timeout_at(deadline, stream.read(&mut buffer))
                .await
                .expect("capture request body deadline")
                .unwrap();
            assert_ne!(size, 0, "capture request ended before declared body");
            request.extend_from_slice(&buffer[..size]);
        }
        if let Some(response_delay) = response_delay {
            tokio::time::timeout_at(deadline, tokio::time::sleep(response_delay))
                .await
                .expect("capture response delay deadline");
        }
        let mut response = format!(
            "HTTP/1.1 {response_status} Test\r\n{}Content-Length: {}\r\nConnection: close\r\n\r\n",
            response_headers
                .iter()
                .map(|header| format!("{header}\r\n"))
                .collect::<String>(),
            response_body.len(),
        )
        .into_bytes();
        response.extend_from_slice(&response_body);
        let written = tokio::time::timeout_at(deadline, stream.write_all(&response))
            .await
            .expect("capture response deadline");
        if response_delay.is_none() {
            written.unwrap();
        }
        request
    });
    let client = authenticated_test_client(&format!("http://{address}/"), TEST_NODE_ID);
    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert(
        "x-vonk-fixture-auth",
        reqwest::header::HeaderValue::from_static("enrolled-agent"),
    );
    *client.client.write().await = reqwest::Client::builder()
        .default_headers(headers)
        .timeout(budget)
        .build()
        .unwrap();
    (
        client,
        CapturePeer {
            task: server,
            deadline,
            stages,
        },
    )
}

pub(super) async fn finish_capture_peer(mut server: CapturePeer) -> Vec<u8> {
    match tokio::time::timeout_at(server.deadline, &mut server.task).await {
        Ok(result) => result.expect("capture peer failed"),
        Err(error) => {
            server.task.abort();
            let _ = tokio::time::timeout(PEER_BUDGET, &mut server.task).await;
            panic!("capture peer completion deadline: {error}");
        }
    }
}

pub(super) async fn observation_client(status: u16) -> (AgentHttpClient, CapturePeer) {
    request_capture_client(
        status,
        Vec::new(),
        Vec::new(),
        None,
        CONTROLLER_REQUEST_TIMEOUT,
    )
    .await
}

pub(super) fn distribution_assignment_fixture(
    model: &[u8],
) -> vonk_agent_protocol::DistributionAssignment {
    vonk_agent_protocol::DistributionAssignment {
        objects: vec![vonk_agent_protocol::DistributionObject {
            name: "weights/model.bin".to_owned(),
            sha256: hex_sha256(model),
            bytes: model.len() as u64,
            kind: "model".to_owned(),
        }],
        oci_image_digest: format!("sha256:{}", "1".repeat(64)),
        oci_image_config_digest: format!("sha256:{}", "2".repeat(64)),
    }
}

#[derive(Clone, Copy)]
pub(super) enum DistributionFixtureMode {
    Good,
    WrongEtagFirstObject,
    /// Serve the requested range with the correct length and ETag but
    /// different bytes, so only the content check can reject it.
    InterruptFirstObject,
    UnavailableFirstObject,
    MalformedFirstManifest,
    MalformedFirstThreeManifests,
    InterruptFirstFiveObjects,
}

pub(super) fn authenticated_test_client(controller: &str, node_id: &str) -> AgentHttpClient {
    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert(
        "x-vonk-fixture-auth",
        reqwest::header::HeaderValue::from_static("enrolled-agent"),
    );
    AgentHttpClient {
        client: Arc::new(RwLock::new(
            reqwest::Client::builder()
                .retry(reqwest::retry::never())
                .timeout(CONTROLLER_REQUEST_TIMEOUT)
                .default_headers(headers)
                .build()
                .unwrap(),
        )),
        controller: Url::parse(controller).unwrap(),
        node_id: node_id.to_owned(),
        identity_content: Default::default(),
        progress_phase: Default::default(),
    }
}

pub(super) fn distribution_fixture_server(
    assignment: vonk_agent_protocol::DistributionAssignment,
    objects: HashMap<String, Vec<u8>>,
    expected_requests: usize,
    mode: DistributionFixtureMode,
) -> (AgentHttpClient, ThreadPeer<Vec<Vec<u8>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let manifest = canonical_json(&assignment).unwrap();
    let node_id = TEST_NODE_ID.to_owned();
    let server = spawn_peer(move || {
        let deadline = std::time::Instant::now() + PEER_BUDGET;
        let mut requests = Vec::new();
        let mut served_plan: Option<String> = None;
        for _ in 0..expected_requests {
            let mut stream = accept_peer(&listener, deadline);
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            while !request.windows(4).any(|value| value == b"\r\n\r\n") {
                assert!(
                    std::time::Instant::now() < deadline,
                    "fixture read deadline"
                );
                let size = stream.read(&mut buffer).unwrap();
                assert!(size > 0);
                request.extend_from_slice(&buffer[..size]);
            }
            requests.push(request.clone());
            let headers_end = request
                .windows(4)
                .position(|value| value == b"\r\n\r\n")
                .unwrap()
                + 4;
            let headers = String::from_utf8_lossy(&request[..headers_end]);
            let target = headers
                .lines()
                .next()
                .unwrap()
                .split_whitespace()
                .nth(1)
                .unwrap();
            let authorized = headers
                .lines()
                .any(|line| line.eq_ignore_ascii_case("x-vonk-fixture-auth: enrolled-agent"));
            if !authorized {
                write!(
                    stream,
                    "HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                .unwrap();
                continue;
            }
            if target.starts_with("/agent/distribution/manifests/") {
                served_plan = target.rsplit('/').next().map(str::to_owned);
                if (matches!(mode, DistributionFixtureMode::MalformedFirstManifest)
                    && requests.len() == 1)
                    || (matches!(mode, DistributionFixtureMode::MalformedFirstThreeManifests)
                        && requests.len() <= 3)
                {
                    stream
                        .write_all(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nConnection: close\r\n\r\n{",
                        )
                        .unwrap();
                    continue;
                }
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    manifest.len()
                )
                .unwrap();
                stream.write_all(&manifest).unwrap();
                continue;
            }
            if matches!(mode, DistributionFixtureMode::UnavailableFirstObject)
                && requests.len() == 1
            {
                write!(stream, "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
                continue;
            }
            let (path, query) = target.split_once('?').unwrap();
            assert!(path.starts_with("/agent/distribution/objects/"));
            assert!(
                served_plan
                    .as_deref()
                    .is_none_or(|plan| query == format!("plan_digest={plan}"))
            );
            let digest = path.rsplit('/').next().unwrap();
            let source = objects.get(digest).unwrap();
            let range = headers
                .lines()
                .find_map(|line| {
                    line.strip_prefix("range: bytes=")
                        .or_else(|| line.strip_prefix("Range: bytes="))
                })
                .unwrap();
            let (start, end) = range
                .split_once('-')
                .map(|(start, end)| {
                    (
                        start.parse::<usize>().unwrap(),
                        end.parse::<usize>().unwrap(),
                    )
                })
                .unwrap();
            assert!(end >= start && end < source.len());
            assert!(end - start < 8 * 1024 * 1024);
            let body = source[start..=end].to_vec();
            let response_digest = if matches!(mode, DistributionFixtureMode::WrongEtagFirstObject)
                && digest == assignment.objects[0].sha256
            {
                "0".repeat(64)
            } else {
                digest.to_owned()
            };
            write!(
                stream,
                "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes {}-{}/{}\r\nETag: \"sha256:{}\"\r\nConnection: close\r\n\r\n",
                body.len(), start, end, source.len(), response_digest
            )
            .unwrap();
            if matches!(mode, DistributionFixtureMode::InterruptFirstObject) && requests.len() == 1
            {
                stream.write_all(&body[..5]).unwrap();
                stream.flush().unwrap();
                std::thread::sleep(Duration::from_millis(50));
                continue;
            }
            if matches!(mode, DistributionFixtureMode::InterruptFirstFiveObjects)
                && requests.len() <= 5
            {
                stream.write_all(&body[..body.len() / 2]).unwrap();
                stream.flush().unwrap();
                continue;
            }
            stream.write_all(&body).unwrap();
        }
        requests
    });
    (
        authenticated_test_client(&format!("http://{address}/"), &node_id),
        server,
    )
}

pub(super) fn job_input_client(
    declared_bytes: usize,
    body: Vec<u8>,
) -> (AgentHttpClient, ThreadPeer<Vec<u8>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let server = spawn_peer(move || {
        let deadline = std::time::Instant::now() + PEER_BUDGET;
        let mut stream = accept_peer(&listener, deadline);
        let mut request = Vec::new();
        let mut buffer = [0_u8; 4096];
        while !request.windows(4).any(|value| value == b"\r\n\r\n") {
            assert!(
                std::time::Instant::now() < deadline,
                "fixture read deadline"
            );
            let read = stream.read(&mut buffer).unwrap();
            assert_ne!(read, 0);
            request.extend_from_slice(&buffer[..read]);
        }
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Length: {declared_bytes}\r\nConnection: close\r\n\r\n"
        )
        .unwrap();
        stream.write_all(&body).unwrap();
        request
    });
    (
        AgentHttpClient {
            client: Arc::new(RwLock::new(reqwest::Client::new())),
            controller: Url::parse(&format!("http://{address}/")).unwrap(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            identity_content: Default::default(),
            progress_phase: Default::default(),
        },
        server,
    )
}

pub(super) fn telemetry_sample() -> TelemetrySample {
    serde_json::from_value(serde_json::json!({
        "boot_id": "00000000-0000-4000-8000-000000000001",
        "observed_at": "2026-08-15T12:00:01Z",
        "memory_total_bytes": 128000000000_u64,
        "memory_available_bytes": 64000000000_u64,
        "disk_total_bytes": 1000000000000_u64,
        "disk_free_bytes": 750000000000_u64,
        "gpu_utilization_percent": null,
        "gpu_memory_total_bytes": 128000000000_u64,
        "gpu_memory_free_bytes": 63000000000_u64
    }))
    .unwrap()
}

pub(super) async fn heartbeat_client(response: AgentDirective) -> (AgentHttpClient, CapturePeer) {
    let response_body = canonical_json(&response).unwrap();
    request_capture_client(
        200,
        vec!["Content-Type: application/json".to_owned()],
        response_body,
        None,
        HEARTBEAT_REQUEST_TIMEOUT,
    )
    .await
}

pub(super) async fn host_runtime_grant_client() -> (AgentHttpClient, CapturePeer) {
    request_capture_client(
        200,
        vec!["Content-Type: application/json".to_owned()],
        br#"{"grant":{"claims":{"authority":"vonk.host-maintenance-helper","expires_at":2100000010,"issued_at":2100000000,"node_id":"spk_0123456789abcdef0123456789abcdef","operation":{"action":"image-pull","fence":"44d4e914-34df-4962-a802-d1f7dcd928aa","request_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","type":"execute-container-runtime-request"},"request_id":"84ddf214-f067-4bbf-917e-95df32a07fd8","schema_version":1},"schema_version":1,"signature":{"algorithm":"ed25519","key_id":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","value":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}}}"#.to_vec(),
        None,
     CONTROLLER_REQUEST_TIMEOUT).await
}

pub(super) fn delayed_upload_client(
    response_delay: Duration,
) -> (AgentHttpClient, ThreadPeer<Vec<u8>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let base_client = authenticated_test_client(&format!("http://{address}"), "spk_test");
    let server = spawn_peer(move || {
        let deadline = std::time::Instant::now() + PEER_BUDGET;
        for offset in [0, 9] {
            let mut stream = accept_peer(&listener, deadline);
            let mut request = Vec::new();
            let mut buffer = [0u8; 1024];
            while !request.windows(4).any(|part| part == b"\r\n\r\n") {
                assert!(
                    std::time::Instant::now() < deadline,
                    "fixture read deadline"
                );
                let size = stream.read(&mut buffer).unwrap();
                assert!(size > 0);
                request.extend_from_slice(&buffer[..size]);
            }
            assert!(request.starts_with(b"HEAD "));
            write!(stream,"HTTP/1.1 200 OK\r\nx-vonk-upload-offset: {offset}\r\nx-vonk-upload-complete: false\r\nConnection: close\r\n\r\n").unwrap();
            drop(stream);
            let mut stream = accept_peer(&listener, deadline);
            let mut request = Vec::new();
            loop {
                assert!(
                    std::time::Instant::now() < deadline,
                    "fixture read deadline"
                );
                let size = stream.read(&mut buffer).unwrap();
                assert!(size > 0);
                request.extend_from_slice(&buffer[..size]);
                if request.ends_with(b"archive") {
                    break;
                }
            }
            if offset == 0 {
                // The receiver persisted only nine bytes before losing the connection.
                // No response reaches the sender; its retry must discover the cursor.
                assert!(request.ends_with(b"accepted archive"));
                drop(stream);
                continue;
            }
            thread::sleep(response_delay);
            stream
                .write_all(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
                .unwrap();
            return request;
        }
        unreachable!()
    });
    let http_client = reqwest::Client::builder()
        .timeout(Duration::from_millis(50))
        .build()
        .unwrap();
    (
        AgentHttpClient {
            client: Arc::new(RwLock::new(http_client)),
            controller: base_client.controller,
            node_id: base_client.node_id,
            identity_content: Default::default(),
            progress_phase: Default::default(),
        },
        server,
    )
}

pub(super) fn progress() -> AgentProgress {
    AgentProgress {
        fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
        progress: Some(OperationProgress {
            phase: ProgressPhase::Executing.as_str().to_owned(),
            completed_bytes: 21_u64.into(),
            total_bytes: Some(42_u64.into()),
            total_bytes_known: true,
            completed_items: Some(1_u64.into()),
            total_items: Some(2_u64.into()),
            object_sha256: None,
            kind: None,
            activity: None,
            observed_at: None,
            last_progress_at: None,
            bytes_per_second: None,
            smoothed_bytes_per_second: None,
            eta_seconds: None,
            elapsed_seconds: None,
            checkpoint: None,
            members: Vec::new(),
        }),
    }
}

pub(super) fn job_run_plan_fixture() -> (Value, vonk_agent_protocol::RecipeJobRunRequest) {
    let claim: Value = serde_json::from_str(include_str!(
        "../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    let plan = serde_json::from_value(claim["payload"].clone()).unwrap();
    (claim, plan)
}

pub(super) fn rejection_problem(issues: serde_json::Value) -> Vec<u8> {
    serde_json::json!({
        "context": null,
        "detail": "request is invalid",
        "issues": issues,
    })
    .to_string()
    .into_bytes()
}

/// The failed distribution envelope spark-3542's agent retained.
pub(super) fn retained_result() -> AgentResult {
    let document = json!({
        "fence": "35a57c2a-0b03-4101-8f89-6b3806570c50",
        "state": vonk_agent_protocol::generated::AgentResultState::Failed.as_str(),
        "result": {
            "status": vonk_agent_protocol::generated::AgentResultState::Failed.as_str(),
            "error_code": vonk_agent_protocol::generated::FailureCode::ArtifactDistributionFailed.as_str(),
            "failure_kind": vonk_agent_protocol::generated::AgentFailureKind::TemporaryDependency.as_str(),
            "reason": "Controller distribution could not be verified and retained"
        }
    });
    let bytes = document.to_string();
    vonk_agent_protocol::parse_strict(bytes.as_bytes())
        .expect("the retained failure envelope parses")
}

/// One deadline covers fixture acceptance, reads, writes and result observation.
pub(super) const PEER_BUDGET: Duration = Duration::from_secs(15);

pub(super) struct ThreadPeer<T> {
    result: std::sync::mpsc::Receiver<std::thread::Result<T>>,
}

impl<T> ThreadPeer<T> {
    pub(super) fn finish(self) -> std::thread::Result<T> {
        self.result
            .recv_timeout(PEER_BUDGET)
            .expect("fixture completion deadline")
    }
}

pub(super) fn spawn_peer<T: Send + 'static>(
    work: impl FnOnce() -> T + Send + 'static,
) -> ThreadPeer<T> {
    let (sender, result) = std::sync::mpsc::sync_channel(1);
    thread::spawn(move || {
        let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(work));
        let _ = sender.send(outcome);
    });
    ThreadPeer { result }
}

pub(super) fn accept_peer(
    listener: &TcpListener,
    deadline: std::time::Instant,
) -> std::net::TcpStream {
    listener.set_nonblocking(true).unwrap();
    loop {
        assert!(
            std::time::Instant::now() < deadline,
            "fixture accept deadline"
        );
        match listener.accept() {
            Ok((stream, _)) => {
                stream
                    .set_read_timeout(Some(
                        deadline.saturating_duration_since(std::time::Instant::now()),
                    ))
                    .unwrap();
                stream
                    .set_write_timeout(Some(
                        deadline.saturating_duration_since(std::time::Instant::now()),
                    ))
                    .unwrap();
                return stream;
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(5))
            }
            Err(error) => panic!("fixture acceptance failed: {error}"),
        }
    }
}

/// Cancelling observation drops the peer and aborts its bounded worker.
pub(super) struct AsyncPeer<T> {
    task: tokio::task::JoinHandle<T>,
}

impl<T> AsyncPeer<T> {
    pub(super) fn abort(&self) {
        self.task.abort();
    }
}

impl<T> std::future::Future for AsyncPeer<T> {
    type Output = Result<T, tokio::task::JoinError>;

    fn poll(
        mut self: std::pin::Pin<&mut Self>,
        context: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Self::Output> {
        std::pin::Pin::new(&mut self.task).poll(context)
    }
}

impl<T> Drop for AsyncPeer<T> {
    fn drop(&mut self) {
        self.task.abort();
    }
}

pub(super) fn spawn_async_peer<T: Send + 'static>(
    work: impl std::future::Future<Output = T> + Send + 'static,
) -> AsyncPeer<T> {
    AsyncPeer {
        task: tokio::spawn(async move {
            tokio::time::timeout(PEER_BUDGET, work)
                .await
                .expect("fixture task deadline")
        }),
    }
}
