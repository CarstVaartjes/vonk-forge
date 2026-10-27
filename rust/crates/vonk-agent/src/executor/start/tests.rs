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
    request.compiled_execution_plan.runtime.placement.port = Some(address.port());
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

/// One collective-readiness start whose endpoint answers 503 to its first
/// `unready` probes, with `dropper` standing in for the helper's drops.
async fn collective_start_with_drops(
    dropper: crate::page_cache::Dropper,
    unready: usize,
) -> ExecutionResult {
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
    request.compiled_execution_plan.runtime.placement.port = Some(address.port());
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
        let mut probes = 0;
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
            stream.set_nonblocking(false).unwrap();
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
                probes += 1;
                let status = if probes <= unready {
                    "503 Unavailable"
                } else {
                    "200 OK"
                };
                write!(
                    stream,
                    "HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                .unwrap();
                continue;
            }
            if bytes.starts_with(b"POST /agent/host-runtime/grant ") {
                let grant: vonk_agent_protocol::SignedHostHelperGrant = serde_json::from_str(include_str!(
                    "../../../../../../agent_protocol/fixtures/host-helper-grant-python-issued.json"
                )).unwrap();
                let body = vonk_agent_protocol::canonical_generated_json(&json!({"grant": grant}))
                    .unwrap();
                write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).unwrap();
                stream.write_all(&body).unwrap();
            } else {
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                    .unwrap();
            }
        }
    });
    let socket = runtime_root.path().join("helper.sock");
    let helper = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    let helper_peer = thread::spawn(move || {
        let (mut stream, _) = helper.accept().unwrap();
        let mut prefix = [0; 4];
        stream.read_exact(&mut prefix).unwrap();
        let mut bytes = vec![0; u32::from_be_bytes(prefix) as usize];
        stream.read_exact(&mut bytes).unwrap();
        let grant: vonk_agent_protocol::SignedHostHelperGrant =
            serde_json::from_slice(&bytes).unwrap();
        let response = vonk_agent_protocol::generated::HostHelperResponse {
            schema_version: 1,
            request_id: Some(grant.claims.request_id),
            status: vonk_agent_protocol::generated::HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
            process_running: None,
            exit_code: Some(0),
            installation_intent_nonce: None,
            job_progress_at: None,
            error_code: None,
            diagnostic: None,
            process_logs: None,
        };
        let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
        stream
            .write_all(&(body.len() as u32).to_be_bytes())
            .unwrap();
        stream.write_all(&body).unwrap();
    });
    let phases = crate::page_cache::LoadPhases::new(dropper, Duration::from_millis(20));
    crate::page_cache::install_for_helper(&socket, &phases);
    let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: &socket,
        runtime,
        runtime_root: runtime_root.path(),
    };
    let (_lease, lease) = tokio::sync::watch::channel(claim.deadline);
    let (_cancel, cancellation) = tokio::sync::watch::channel(false);
    let result = executor
        .execute_start(&claim, lease, cancellation, request)
        .await;
    done.store(true, Ordering::SeqCst);
    peer.join().unwrap();
    helper_peer.join().unwrap();
    result
}

fn counting_dropper(
    problem: Option<vonk_agent_protocol::generated::PageCacheDropProblem>,
) -> (
    crate::page_cache::Dropper,
    Arc<std::sync::atomic::AtomicUsize>,
) {
    let drops = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let counted = drops.clone();
    let dropper: crate::page_cache::Dropper = Arc::new(move || {
        counted.fetch_add(1, Ordering::SeqCst);
        Box::pin(async move {
            vonk_agent_protocol::generated::PageCacheDropReport {
                request_id: Uuid::new_v4(),
                dropped_at: Utc::now().fixed_offset(),
                cached_before_bytes: problem.is_none().then_some(8 << 30),
                cached_after_bytes: problem.is_none().then_some(1 << 30),
                mem_available_after_bytes: None,
                sync: None,
                problem,
            }
        })
    });
    (dropper, drops)
}

fn start_page_cache(
    result: &ExecutionResult,
) -> vonk_agent_protocol::generated::PageCacheDropEvidence {
    let ExecutionResult::Done(OutcomeDoneResult::RecipeStartResult(started)) = result else {
        panic!("the workload starts: {result:?}");
    };
    started
        .preload_diagnostics
        .as_ref()
        .and_then(|diagnostics| diagnostics.page_cache.clone())
        .expect("the start reports its drops")
}

#[tokio::test]
async fn a_loading_workload_is_dropped_until_it_is_ready_and_its_start_reports_the_drops() {
    let (dropper, drops) = counting_dropper(None);
    let result = collective_start_with_drops(dropper, 2).await;
    let evidence = start_page_cache(&result);
    let made = drops.load(Ordering::SeqCst);
    assert!(made >= 2, "drops while loading: {made}");
    assert_eq!(evidence.drops as usize, made);
    assert_eq!(evidence.reclaimed_bytes, made as u64 * (7 << 30));
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert_eq!(drops.load(Ordering::SeqCst), made, "no drop after ready");
}

#[tokio::test]
async fn a_host_that_refuses_the_drop_still_starts_its_workload_with_a_note() {
    // Fault: the helper's sandbox refuses /proc/sys/vm/drop_caches.
    let (dropper, drops) = counting_dropper(Some(
        vonk_agent_protocol::generated::PageCacheDropProblem::NotPermitted,
    ));
    let result = collective_start_with_drops(dropper, 2).await;
    let evidence = start_page_cache(&result);
    assert!(drops.load(Ordering::SeqCst) >= 2);
    assert_eq!(evidence.drops, 0);
    assert!(evidence.problems >= 2);
    assert_eq!(
        evidence.last_problem.and_then(|report| report.problem),
        Some(vonk_agent_protocol::generated::PageCacheDropProblem::NotPermitted)
    );
}

#[test]
fn every_workload_container_start_drops_the_page_cache_first() {
    // Guard: a Start plan is built only inside `launch_after_drop`, so no new
    // start path can skip the drop. Matches (`Start(_)`, `Start(plan)) =>`)
    // only read plans.
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut pending = vec![root];
    let mut builders = Vec::new();
    while let Some(path) = pending.pop() {
        if path.is_dir() {
            pending.extend(
                fs::read_dir(&path)
                    .unwrap()
                    .map(|entry| entry.unwrap().path()),
            );
            continue;
        }
        let source = fs::read_to_string(&path).unwrap();
        for (index, line) in source.lines().enumerate() {
            if line.contains("HostRuntimePlan::Start(") && !line.contains("=>") {
                builders.push((path.clone(), index, source.clone()));
            }
        }
    }
    let sites: Vec<_> = builders
        .iter()
        .map(|(path, index, _)| format!("{}:{}", path.display(), index + 1))
        .collect();
    assert_eq!(sites.len(), 1, "Start plans built at {sites:?}");
    let (path, index, source) = &builders[0];
    assert!(path.ends_with("executor/start.rs"));
    let before: Vec<&str> = source.lines().take(*index).collect();
    let function = before
        .iter()
        .rev()
        .find(|line| line.trim_start().starts_with("async fn "))
        .unwrap();
    assert!(function.contains("fn launch_after_drop("), "{function}");
    let body = before
        .iter()
        .rev()
        .take_while(|line| !line.trim_start().starts_with("async fn "))
        .any(|line| line.contains("load.drop_now().await"));
    assert!(body, "launch_after_drop drops before it starts");
}
