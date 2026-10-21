#![cfg(test)]
//! Collective readiness observes a launched rank even when preparation is unavailable.

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn collective_readiness_retries_inspection_without_repreparing_a_launched_rank() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap();
    let data = tempdir().unwrap();
    let runtime_root = tempdir().unwrap();
    let mut claim = super::super::runtime_observation_tests::runtime_claims().remove(0);
    let AgentClaimPayload::RecipeStartPayload(request) = &mut claim.payload else {
        panic!("start fixture");
    };
    request.phase = Some(RecipeStartPhase::CollectiveReadiness);
    request
        .compiled_execution_plan
        .runtime
        .placement
        .endpoint_address = Some("127.0.0.1".parse().unwrap());
    request.compiled_execution_plan.runtime.placement.port = Some(address.port().into());
    let request = request.clone();
    let mut install = claim.clone();
    install.operation = AgentOperation::RecipeInstall;
    install.payload = serde_json::from_value(json!({
        "installation_id": request.installation_id,
        "plan_digest": request.plan_digest,
        "expected_bytes": 0,
        "compiled_execution_plan": request.compiled_execution_plan,
    }))
    .unwrap();
    super::super::content_observation_tests::prepare_effect(data.path(), &install);
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
    };
    runtime
        .prepare_start_with_inspection_identity(
            &request.compiled_execution_plan,
            &request.installation_id.to_string(),
            &request.run_id.to_string(),
            &request.compiled_execution_plan.runtime.placement,
            &RecipeRunStartIdentity {
                run_generation: request.run_generation,
            },
        )
        .unwrap();
    let done = Arc::new(AtomicBool::new(false));
    let peer_done = done.clone();
    let peer = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(30);
        let mut requests = Vec::new();
        while !peer_done.load(Ordering::SeqCst) {
            assert!(Instant::now() < deadline);
            let mut stream = match listener.accept() {
                Ok((stream, _)) => stream,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(1));
                    continue;
                }
                Err(error) => panic!("readiness peer: {error}"),
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut bytes = Vec::new();
            let mut buffer = [0; 4096];
            loop {
                let size = stream.read(&mut buffer).unwrap();
                assert!(size > 0);
                bytes.extend_from_slice(&buffer[..size]);
                if let Some(end) = bytes.windows(4).position(|part| part == b"\r\n\r\n") {
                    let headers = std::str::from_utf8(&bytes[..end]).unwrap();
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().unwrap())
                        })
                        .unwrap_or(0);
                    if bytes.len() >= end + 4 + length {
                        break;
                    }
                }
            }
            if bytes.starts_with(b"GET ") {
                // The launched endpoint is healthy once its inspection succeeds.
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    .unwrap();
                continue;
            }
            requests.push(bytes);
            if requests.len() <= 2 {
                stream.write_all(b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
            } else {
                // The Controller and helper are scripted peers here. The real
                // issuer/signature/identity checks have their authority suites.
                let grant: vonk_agent_protocol::SignedHostHelperGrant = serde_json::from_str(include_str!(
                    "../../../../../../agent_protocol/fixtures/host-helper-grant-python-issued.json"
                )).unwrap();
                let body = vonk_agent_protocol::canonical_generated_json(&json!({"grant": grant}))
                    .unwrap();
                write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).unwrap();
                stream.write_all(&body).unwrap();
            }
        }
        requests
    });
    let socket = runtime_root.path().join("helper.sock");
    let helper = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    helper.set_nonblocking(true).unwrap();
    let helper_done = done.clone();
    let helper_peer = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(30);
        while !helper_done.load(Ordering::SeqCst) {
            assert!(Instant::now() < deadline);
            let mut stream = match helper.accept() {
                Ok((stream, _)) => stream,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(1));
                    continue;
                }
                Err(error) => panic!("helper peer: {error}"),
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut prefix = [0; 4];
            stream.read_exact(&mut prefix).unwrap();
            let mut bytes = vec![0; u32::from_be_bytes(prefix) as usize];
            stream.read_exact(&mut bytes).unwrap();
            let grant: vonk_agent_protocol::SignedHostHelperGrant =
                serde_json::from_slice(&bytes).unwrap();
            let response = vonk_agent_protocol::generated::HostHelperResponse {
                schema_version: 1, request_id: Some(grant.claims.request_id),
                status: vonk_agent_protocol::generated::HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
                process_running: None, exit_code: Some(0),
                installation_intent_nonce: None, job_progress_at: None,
                error_code: None, diagnostic: None, process_logs: None,
            };
            let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
            stream
                .write_all(&(body.len() as u32).to_be_bytes())
                .unwrap();
            stream.write_all(&body).unwrap();
            return true;
        }
        false
    });
    let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: &socket,
        runtime,
        runtime_root: runtime_root.path(),
    };
    let (_lease, lease) = tokio::sync::watch::channel(claim.deadline);
    let (_cancel, cancellation) = tokio::sync::watch::channel(false);
    // Lost inspection replies preserve the rank and the next attempt asks only
    // for inspection again. Preparation endpoints deliberately return 503 too.
    let mut results = Vec::new();
    for _ in 0..3 {
        claim.fence = Uuid::new_v4();
        let result = executor
            .execute_start(&claim, lease.clone(), cancellation.clone(), request.clone())
            .await;
        results.push(result);
    }
    done.store(true, Ordering::SeqCst);
    let requests = peer.join().unwrap();
    assert!(helper_peer.join().unwrap());
    assert_eq!(requests.len(), 3);
    for bytes in requests {
        assert!(bytes.starts_with(b"POST /agent/host-runtime/grant "));
        let end = bytes
            .windows(4)
            .position(|part| part == b"\r\n\r\n")
            .unwrap()
            + 4;
        let body: vonk_agent_protocol::generated::HostRuntimeGrantRequest =
            serde_json::from_slice(&bytes[end..]).unwrap();
        assert_eq!(
            body.action,
            vonk_agent_protocol::generated::HostRuntimeGrantRequestAction::RunInspect
        );
        assert_eq!(body.run_generation, Some(request.run_generation));
    }
    assert!(
        results[..2]
            .iter()
            .all(|result| matches!(result, ExecutionResult::Unknown(_)))
    );
    assert!(matches!(results.last(), Some(ExecutionResult::Done(_))));
    executor
        .runtime
        .prepare_retained_start_with_inspection_identity(
            &request.compiled_execution_plan,
            &request.installation_id.to_string(),
            &request.run_id.to_string(),
            &request.compiled_execution_plan.runtime.placement,
            &RecipeRunStartIdentity {
                run_generation: request.run_generation,
            },
        )
        .unwrap();
}
