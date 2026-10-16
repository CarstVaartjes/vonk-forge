#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn canonical_job_claim_prepares_without_a_serving_port() {
    let claim: Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    let request: vonk_agent_protocol::RecipeJobRunRequest =
        serde_json::from_value(claim["payload"].clone()).unwrap();
    let spec = request.compiled_execution_plan.clone();
    spec.validate().unwrap();
    let placement = super::job_placement(&spec).unwrap();
    let data = tempdir().unwrap();
    let run_id = request.run_id.to_string();
    fs::create_dir_all(data.path().join("runs").join(&run_id).join("inputs")).unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
    };
    let start = runtime
        .prepare_job_start(
            &spec,
            &request.installation_id.to_string(),
            &run_id,
            &placement,
            &spec,
        )
        .unwrap();
    assert!(!start.main.iter().any(|argument| argument == "--publish"));
    assert!(
        start
            .main
            .windows(2)
            .any(|pair| pair == ["--network", "none"])
    );
    let metadata = data.path().join("run-metadata").join(run_id);
    for name in ["runtime.json", "lifecycle.json"] {
        let persisted: Value =
            serde_json::from_slice(&fs::read(metadata.join(name)).unwrap()).unwrap();
        let port = if name == "runtime.json" {
            &persisted["runtime"]["placement"]["port"]
        } else {
            &persisted["placement"]["port"]
        };
        assert!(port.is_null());
    }
    let mut serving_placement = placement;
    serving_placement.port = Some(1024);
    assert!(
        runtime
            .start_arguments(
                &spec,
                &request.installation_id.to_string(),
                &request.run_id.to_string(),
                &serving_placement
            )
            .is_err()
    );
}

#[test]
fn job_run_stop_binding_keeps_parent_run_and_exact_job_target_distinct() {
    let claim: AgentClaim = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    let AgentClaimPayload::RecipeJobRunRequest(job) = &claim.payload else {
        panic!("expected canonical job-run claim");
    };
    let stop = job_run_stop_plan(&claim, job).expect("accepted JobRun must produce its exact Stop");

    assert_eq!(stop.run_id, job.run_id);
    assert_eq!(stop.target_runtime_id, job.job_id);
    assert_ne!(stop.run_id, stop.target_runtime_id);
    assert_eq!(stop.run_generation, job.run_generation);
    assert_eq!(stop.installation_id, job.installation_id);
    assert_eq!(stop.mapping_id, job.mapping_id);
    assert!(stop.cancel_pending_start);

    let mut stop_claim = claim.clone();
    stop_claim.operation = AgentOperation::RecipeStop;
    stop_claim.payload = AgentClaimPayload::RecipeStopPayload(stop);
    assert!(matches!(
        RecipeOperationRequest::parse(&stop_claim),
        Ok(RecipeOperationRequest::Stop(_))
    ));
}

#[test]
fn job_run_grant_requests_are_the_shared_vector_the_controller_grants() {
    // The agent half of the boundary: each body is what the agent POSTs to
    // `/agent/host-runtime/grant` for the vector's JobRun claim, built by the
    // production request and grant builders. Its one Stop serves cleanup and
    // cancellation alike. The Controller's suite grants each body through its
    // real route mapping in the matching Controller states.
    let vector: Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/job-run-host-runtime-grant-requests-v1.json"
    ))
    .unwrap();
    let claim: AgentClaim = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    assert_eq!(vector["claim"], "recipe-job-run-claim-v1.json");
    let AgentClaimPayload::RecipeJobRunRequest(job) = &claim.payload else {
        panic!("expected canonical job-run claim");
    };
    let cases = vector["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 2);
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let plan = match name {
            "job-run-start" => HostRuntimePlan::JobRun(job.clone()),
            "job-run-stop" => HostRuntimePlan::Stop(job_run_stop_plan(&claim, job).unwrap()),
            other => panic!("{other} has no agent producer"),
        };
        // An input, not a derivation: the Start's arguments come from the
        // host's prepared image, and the Controller binds only their digest.
        let arguments: Vec<String> = serde_json::from_value(case["arguments"].clone()).unwrap();
        let request = crate::host_runtime::runtime_request(
            &claim,
            plan.action(),
            arguments,
            None,
            None,
            Some(&plan),
        );
        request.validate().unwrap();
        let digest = hex_sha256(&canonical_json(&request).unwrap());
        let body = crate::client::build_host_runtime_grant_request(&claim, &request, &digest)
            .and_then(|grant| {
                vonk_agent_protocol::canonical_generated_json(&grant)
                    .map_err(|_| ClientError::Protocol)
            })
            .unwrap();
        let produced: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(
            case["grant_request"], produced,
            "{name}: update the shared vector"
        );
    }
}

#[test]
fn signed_output_mappings_cover_pdf_avif_and_custom_suffixes() {
    let mappings = vec![
        RecipeJobOutputMapping {
            slot: "custom".to_owned(),
            media_type: "application/vnd.vonk.custom".to_owned(),
            extensions: vec![".vonk.bin".to_owned()],
        },
        RecipeJobOutputMapping {
            slot: "document".to_owned(),
            media_type: "application/pdf".to_owned(),
            extensions: vec![".pdf".to_owned()],
        },
        RecipeJobOutputMapping {
            slot: "fallback".to_owned(),
            media_type: "application/octet-stream".to_owned(),
            extensions: vec![".bin".to_owned()],
        },
        RecipeJobOutputMapping {
            slot: "image".to_owned(),
            media_type: "image/avif".to_owned(),
            extensions: vec![".avif".to_owned()],
        },
    ];

    assert_eq!(
        output_media_type("report.pdf", &mappings),
        Some("application/pdf")
    );
    assert_eq!(
        output_media_type("frame.avif", &mappings),
        Some("image/avif")
    );
    assert_eq!(
        output_media_type("artifact.vonk.bin", &mappings),
        Some("application/vnd.vonk.custom")
    );
    assert_eq!(
        output_media_type("artifact.bin", &mappings),
        Some("application/octet-stream")
    );
    assert_eq!(output_media_type("report.PDF", &mappings), None);
    assert_eq!(
        output_media_type("artifact.VONK.bin", &mappings),
        Some("application/octet-stream")
    );
}

#[tokio::test]
async fn active_job_cancellation_runs_the_stop_path_promptly() {
    let (sender, mut cancellation) = tokio::sync::watch::channel(false);
    let (job_stopped, job_drained) = tokio::sync::oneshot::channel::<()>();
    let stopped = Arc::new(AtomicBool::new(false));
    let observed = Arc::clone(&stopped);
    tokio::spawn(async move {
        tokio::task::yield_now().await;
        sender.send_replace(true);
    });

    let result = tokio::time::timeout(
        Duration::from_secs(1),
        run_interruptible_job(
            async move {
                let _ = job_drained.await;
            },
            &mut cancellation,
            move || async move {
                observed.store(true, Ordering::Release);
                let _ = job_stopped.send(());
                Ok::<(), ()>(())
            },
        ),
    )
    .await
    .expect("cancellation did not interrupt the active job");

    assert!(matches!(
        result,
        InterruptibleJob::Cancelled { stopped: true }
    ));
    assert!(stopped.load(Ordering::Acquire));
}

#[test]
fn artifact_job_failure_keeps_current_result_and_typed_diagnostics() {
    let mut job_claim = claim();
    job_claim.operation = "recipe.job.run.v1".parse().unwrap();
    let envelope: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
    ))
    .unwrap();
    let mut receipt: vonk_agent_protocol::RecipeJobRunResult =
        serde_json::from_value(envelope["result"].clone()).unwrap();
    receipt.exit_code = 1;
    receipt.reason = Some("runtime failed".to_owned());
    let result = failed_outcome(
        &job_claim,
        ExecutionResult::Failed(
            Failure::new("runtime failed")
                .code(FailureCode::RecipeJobRunFailed)
                .receipt(receipt),
        ),
    );
    let typed = result.receipt.expect("the receipt is kept");
    typed.validate().unwrap();
    assert_eq!(typed.exit_code, 1);
    assert!(typed.diagnostics.is_some());
}

#[tokio::test]
async fn lost_cancellation_owner_stops_the_job_and_admits_a_fresh_operation() {
    let (sender, mut cancellation) = tokio::sync::watch::channel(false);
    drop(sender);
    let stopped = Arc::new(AtomicBool::new(false));
    let observed = Arc::clone(&stopped);
    let (finish, finished) = tokio::sync::oneshot::channel();
    let result = tokio::time::timeout(
        Duration::from_secs(1),
        run_interruptible_job(
            async {
                let _ = finished.await;
            },
            &mut cancellation,
            move || async move {
                observed.store(true, Ordering::SeqCst);
                let _ = finish.send(());
                Ok::<(), ()>(())
            },
        ),
    )
    .await
    .unwrap();
    assert!(matches!(
        result,
        InterruptibleJob::Cancelled { stopped: true }
    ));
    assert!(stopped.load(Ordering::SeqCst));
    let (_sender, mut fresh) = tokio::sync::watch::channel(false);
    assert!(matches!(
        run_interruptible_job(async { 7 }, &mut fresh, || async { Ok::<(), ()>(()) }).await,
        InterruptibleJob::Completed(7)
    ));
}

#[tokio::test]
async fn completed_job_upload_recovers_without_reexecution_and_new_jobs_are_admitted() {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let mut claim: AgentClaim = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    )).unwrap();
    claim.deadline = (Utc::now() + chrono::Duration::minutes(2)).fixed_offset();
    let AgentClaimPayload::RecipeJobRunRequest(request) = &claim.payload else {
        panic!("job fixture");
    };
    let request = request.clone();
    let data = tempdir().unwrap();
    let runtime_root = tempdir().unwrap();
    let output_root = data
        .path()
        .join("runs")
        .join(request.job_id.to_string())
        .join("outputs");
    fs::create_dir_all(output_root.join("tmp")).unwrap();
    fs::write(output_root.join("tmp/retained"), b"runtime scratch").unwrap();
    let mapping = &request.output_mappings[0];
    let name = format!("result{}", mapping.extensions[0]);
    let path = output_root.join(&name);
    fs::write(&path, b"completed output").unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let inode = fs::metadata(&path).unwrap().ino();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
    };
    runtime.persist_job_intent(&request).unwrap();
    let manifest = collect_job_outputs(
        Some(&output_root),
        &request.output_limits,
        &request.output_mappings,
    )
    .unwrap();
    let receipt = job_receipt(&request, 0, Instant::now(), manifest, None);
    runtime.persist_job_completion(&receipt).unwrap();

    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap();
    let peer = std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut payloads = Vec::new();
        for step in 0..5 {
            let mut stream = loop {
                assert!(
                    Instant::now() < deadline,
                    "bounded output peer did not receive a request"
                );
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(1))
                    }
                    Err(error) => panic!("output peer: {error}"),
                }
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            let header_end = loop {
                let read = stream.read(&mut buffer).unwrap();
                assert!(read > 0);
                request.extend_from_slice(&buffer[..read]);
                if let Some(end) = request.windows(4).position(|part| part == b"\r\n\r\n") {
                    break end + 4;
                }
            };
            let headers = std::str::from_utf8(&request[..header_end]).unwrap();
            assert!(headers.starts_with("PUT /agent/recipe-jobs/"));
            let length = headers
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().unwrap())
                })
                .unwrap();
            while request.len() < header_end + length {
                let read = stream.read(&mut buffer).unwrap();
                assert!(read > 0);
                request.extend_from_slice(&buffer[..read]);
            }
            payloads.push(request[header_end..].to_vec());
            let status = if step < 3 { 503 } else { 204 };
            write!(
                stream,
                "HTTP/1.1 {status} Response\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            .unwrap();
        }
        payloads
    });
    let client = AgentHttpClient::for_http_test(&format!("http://{address}/"), NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: std::path::Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime,
        runtime_root: runtime_root.path(),
    };
    let (_lease, lease) = tokio::sync::watch::channel(claim.deadline);
    let (_cancel, cancel) = tokio::sync::watch::channel(false);
    let started = Instant::now();
    let first = executor
        .execute_job_run(&claim, lease.clone(), cancel.clone(), request.clone())
        .await;
    assert!(matches!(first, ExecutionResult::Unknown(_)));
    assert!(started.elapsed() < Duration::from_secs(5));
    assert_eq!(fs::metadata(&path).unwrap().ino(), inode);
    assert_eq!(fs::read(&path).unwrap(), b"completed output");
    // A restarted executor consults the persisted exit before installation,
    // helper dispatch or input staging. NoProcess detects any wrong relaunch.
    let second = executor
        .execute_job_run(&claim, lease.clone(), cancel.clone(), request.clone())
        .await;
    assert!(matches!(second, ExecutionResult::Done(_)));
    let replay = executor
        .execute_job_run(&claim, lease, cancel, request.clone())
        .await;
    assert!(matches!(replay, ExecutionResult::Done(_)));
    assert_eq!(fs::metadata(&path).unwrap().ino(), inode);
    assert_eq!(
        fs::read(output_root.join("tmp/retained")).unwrap(),
        b"runtime scratch"
    );
    assert!(
        peer.join()
            .unwrap()
            .iter()
            .all(|payload| payload == b"completed output")
    );
    let mut fresh = request;
    fresh.job_id = Uuid::new_v4();
    assert!(
        executor
            .runtime
            .retained_job_completion(&fresh)
            .unwrap()
            .is_none()
    );
    executor
        .runtime
        .job_input_destination(&fresh.job_id.to_string(), "fresh.bin")
        .unwrap();
    executor
        .runtime
        .prepare_job_start(
            &fresh.compiled_execution_plan,
            &fresh.installation_id.to_string(),
            &fresh.job_id.to_string(),
            &job_placement(&fresh.compiled_execution_plan).unwrap(),
            &fresh.compiled_execution_plan,
        )
        .unwrap();
}

pub(super) fn job_claim() -> (AgentClaim, vonk_agent_protocol::RecipeJobRunRequest) {
    let mut claim: AgentClaim = serde_json::from_str(include_str!(
        "../../../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
    ))
    .unwrap();
    claim.deadline = (Utc::now() + chrono::Duration::minutes(2)).fixed_offset();
    let AgentClaimPayload::RecipeJobRunRequest(request) = &claim.payload else {
        panic!("job fixture");
    };
    let request = request.clone();
    (claim, request)
}

#[tokio::test]
async fn unreadable_retained_job_record_reports_its_step_and_cause_on_the_wire() {
    let (claim, request) = job_claim();
    let data = tempdir().unwrap();
    // A damaged durable intent record (torn write, disk fault): its job is
    // re-observed, proven absent here, and the unknown effect says where and
    // why.
    let peers =
        super::reobserve_tests::peers(request.job_id, vec![super::reobserve_tests::Answer::Absent]);
    let scope = data.path().join("runs").join(request.job_id.to_string());
    fs::create_dir_all(&scope).unwrap();
    fs::write(scope.join("job-request.json"), b"{damaged").unwrap();
    let client = AgentHttpClient::for_http_test("http://127.0.0.1:9/", NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: &peers.socket,
        runtime: OciRuntime {
            runner: &NoProcess,
            data_root: data.path(),
        },
        runtime_root: peers.runtime_root.path(),
    };
    let (_lease, lease) = tokio::sync::watch::channel(claim.deadline);
    let (_cancel, cancel) = tokio::sync::watch::channel(false);
    let result = executor
        .execute_job_run(&claim, lease, cancel, request)
        .await;
    let ExecutionResult::Unknown(unconfirmed) = &result else {
        panic!("a damaged record is an unknown effect: {result:?}");
    };
    assert_eq!(unconfirmed.evidence.stage, FailureStage::JobState);
    assert!(
        unconfirmed
            .evidence
            .diagnostic
            .as_deref()
            .is_some_and(|cause| !cause.is_empty())
    );
    let AgentResultResult::OutcomeUnknown(outcome) = result.finish(&claim).result else {
        panic!("unknown outcome");
    };
    let evidence = outcome.evidence.expect("unknown outcome carries evidence");
    assert_eq!(evidence.stage.as_deref(), Some("job-state"));
    assert!(evidence.diagnostic.is_some_and(|cause| !cause.is_empty()));
}

#[test]
fn job_start_preparation_reports_the_runtime_step_that_stopped_it() {
    let (_claim, request) = job_claim();
    let spec = &request.compiled_execution_plan;
    let data = tempdir().unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
    };
    // The job's input directory is missing when the start is prepared.
    let Err(error) = runtime.prepare_job_start(
        spec,
        &request.installation_id.to_string(),
        &request.job_id.to_string(),
        &super::job_placement(spec).unwrap(),
        spec,
    ) else {
        panic!("a start without its input directory is not prepared");
    };
    let (stage, cause) = super::local_step(
        FailureStage::RuntimeProjection,
        "job start preparation",
        &error,
    );
    assert_eq!(stage, FailureStage::JobInputs);
    assert!(!cause.is_empty());
    // An error that names no step of its own keeps the caller's step.
    let (stage, _) = super::local_step(
        FailureStage::RunStorage,
        "job scope cleanup",
        &crate::oci::OciError::Artifact,
    );
    assert_eq!(stage, FailureStage::RunStorage);
}

/// A local step before the job's process is dispatched never leaves the
/// job's effect in doubt: this attempt did not run it. A cause that clears by
/// itself is a typed wait at the step's pre-launch stage (the Controller
/// issues the job again); a refused workload input ends it with evidence.
#[test]
fn a_step_before_launch_waits_at_its_stage_unless_the_input_was_refused() {
    let (claim, request) = job_claim();
    let spec = &request.compiled_execution_plan;
    let data = tempdir().unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
    };
    // Production trigger: the job's input directory is not there yet when its
    // launch is prepared (the step reported an unknown effect, so the job
    // was lost without ever running).
    let Err(error) = runtime.prepare_job_start(
        spec,
        &request.installation_id.to_string(),
        &request.job_id.to_string(),
        &super::job_placement(spec).unwrap(),
        spec,
    ) else {
        panic!("a start without its input directory is not prepared");
    };
    let storage = std::io::Error::from(std::io::ErrorKind::PermissionDenied);
    for (stage, step, error) in [
        (
            FailureStage::RuntimeProjection,
            "job start preparation",
            error,
        ),
        (
            FailureStage::RunStorage,
            "job intent record",
            crate::oci::OciError::Io(storage),
        ),
    ] {
        let result = job_step_before_launch(stage, step, &error);
        assert_eq!(result.state(), AgentResultState::Failed, "{step}");
        let failure = result.failure().expect("a typed failure");
        assert_eq!(
            failure.failure_kind,
            Some(AgentFailureKind::TemporaryDependency),
            "{step}"
        );
        // The step's own stage, not the error's: a later stage would prove
        // nothing about launch.
        assert_eq!(failure.stage, Some(stage), "{step}");
        assert!(
            failure
                .diagnostic
                .as_deref()
                .is_some_and(|d| d.contains(step))
        );
        let finished = result.finish(&claim);
        assert_eq!(finished.state, AgentResultState::Failed);
    }
    let refused = job_step_before_launch(
        FailureStage::JobInputs,
        "job input manifest write",
        &crate::oci::OciError::Workload(
            vonk_agent_protocol::compiled_execution_plan::WorkloadError::Invalid("input"),
        ),
    );
    assert_eq!(refused.state(), AgentResultState::Failed);
    assert_eq!(refused.failure().expect("a failure").failure_kind, None);
}

fn grant_answer(status: u16, code: Option<&str>) -> crate::host_runtime::HostRuntimeError {
    let mut answer = crate::client::ControllerError::from_status(status);
    answer.endpoint = "/agent/host-runtime/grant".into();
    if let Some(code) = code {
        answer.code = code.into();
    }
    crate::host_runtime::HostRuntimeError::Controller(ClientError::Controller(Box::new(answer)))
}

#[test]
fn a_definitely_refused_job_start_ends_the_job_with_its_evidence() {
    // Wrong implementation: every Start grant error became an unknown
    // outcome, so a plan that can never be granted was observed again until
    // the order ended "job custody or delivery remains unobserved".
    let (claim, _request) = job_claim();
    let refused = refused_job_start(&grant_answer(409, Some("incomplete")))
        .expect("a definite refusal ends the job");
    let failure = refused.failure().expect("a failure");
    assert_eq!(
        failure.failure_kind,
        Some(AgentFailureKind::InvalidContract)
    );
    assert_eq!(failure.stage, Some(FailureStage::RuntimeAdapter));
    assert!(failure.retry_after_seconds.is_none());
    let diagnostic = failure.diagnostic.as_deref().unwrap_or_default();
    assert!(diagnostic.contains("error_code=incomplete"), "{diagnostic}");
    assert!(matches!(
        refused.finish(&claim).result,
        AgentResultResult::OutcomeFailed(_)
    ));
    // A denied grant (an older Controller) and an unavailable authority stay
    // waits for the job's existing observation.
    assert!(refused_job_start(&grant_answer(403, None)).is_none());
    assert!(refused_job_start(&grant_answer(503, None)).is_none());
}

#[test]
fn job_outputs_leave_out_the_runtime_cache_mountpoint() {
    // The container runtime creates the cache mountpoint in the host output
    // root, because the cache mount nests inside the `/outputs` bind.
    let root = tempdir().unwrap();
    for mountpoint in vonk_agent_protocol::compiled_oci::runtime_output_mountpoints() {
        fs::create_dir_all(root.path().join(mountpoint).join("home")).unwrap();
        fs::write(root.path().join(mountpoint).join("state.bin"), b"cache").unwrap();
    }
    fs::create_dir(root.path().join("tmp")).unwrap();
    fs::write(root.path().join("result.txt"), b"ok\n").unwrap();
    let limits: RecipeJobOutputLimits = serde_json::from_value(serde_json::json!({
        "max_files": 1,
        "max_file_bytes": 4096,
        "max_total_bytes": 4096,
        "allowed_media_types": ["text/plain"],
    }))
    .unwrap();
    let mappings = vec![RecipeJobOutputMapping {
        slot: "result".to_owned(),
        media_type: "text/plain".to_owned(),
        extensions: vec![".txt".to_owned()],
    }];

    let manifest = collect_job_outputs(Some(root.path()), &limits, &mappings).unwrap();

    let names: Vec<_> = manifest
        .files
        .iter()
        .map(|file| file.name.as_str())
        .collect();
    assert_eq!(names, ["result.txt"]);
}

fn png_outputs(
    max_files: u32,
    max_file_bytes: u32,
    allowed: &str,
) -> (RecipeJobOutputLimits, Vec<RecipeJobOutputMapping>) {
    let limits: RecipeJobOutputLimits = serde_json::from_value(serde_json::json!({
        "max_files": max_files,
        "max_file_bytes": max_file_bytes,
        "max_total_bytes": 8,
        "allowed_media_types": [allowed],
    }))
    .unwrap();
    let mappings = vec![RecipeJobOutputMapping {
        slot: "image".to_owned(),
        media_type: "image/png".to_owned(),
        extensions: vec![".png".to_owned()],
    }];
    (limits, mappings)
}

#[test]
fn outputs_that_break_their_contract_are_a_breach_and_an_unread_root_is_not() {
    // Wrong implementation: every refusal was one anonymous cause, reported
    // as an unobserved effect, so a job that left one file too many was
    // observed for its whole timeout instead of ending failed.
    struct Case {
        rule: &'static str,
        files: &'static [(&'static str, &'static [u8])],
        max_files: u32,
        max_file_bytes: u32,
        allowed: &'static str,
        bound: Option<(u64, u64)>,
    }
    let case = |rule, files, max_files, max_file_bytes, allowed, bound| Case {
        rule,
        files,
        max_files,
        max_file_bytes,
        allowed,
        bound,
    };
    let cases = [
        case(
            "job_output_max_files",
            &[("a.png", b"1"), ("b.png", b"2")],
            1,
            8,
            "image/png",
            Some((1, 2)),
        ),
        case(
            "job_output_entry",
            &[(".partial.png", b"1")],
            4,
            8,
            "image/png",
            None,
        ),
        case(
            "job_output_max_file_bytes",
            &[("a.png", b"123456")],
            4,
            4,
            "image/png",
            Some((4, 6)),
        ),
        case(
            "job_output_max_total_bytes",
            &[("a.png", b"12345"), ("b.png", b"12345")],
            4,
            8,
            "image/png",
            Some((8, 10)),
        ),
        case(
            "job_output_media_type",
            &[("a.bin", b"1")],
            4,
            8,
            "image/png",
            None,
        ),
        case(
            "job_output_allowed_media_type",
            &[("a.png", b"1")],
            4,
            8,
            "image/webp",
            None,
        ),
    ];
    for Case {
        rule,
        files,
        max_files,
        max_file_bytes,
        allowed,
        bound,
    } in cases
    {
        let root = tempdir().unwrap();
        for (name, bytes) in files {
            fs::write(root.path().join(name), bytes).unwrap();
        }
        let (limits, mappings) = png_outputs(max_files, max_file_bytes, allowed);
        let Err(OutputRefusal::Breach(breach)) =
            collect_job_outputs(Some(root.path()), &limits, &mappings)
        else {
            panic!("{rule}: a contract breach");
        };
        assert_eq!(breach.rule, rule);
        assert_eq!(breach.bound, bound, "{rule}");
        assert_eq!(breach.entries.len(), files.len(), "{rule}");
    }
    // A directory the job left is an entry its contract refuses too.
    let root = tempdir().unwrap();
    fs::create_dir(root.path().join("staging")).unwrap();
    let (limits, mappings) = png_outputs(4, 8, "image/png");
    assert!(matches!(
        collect_job_outputs(Some(root.path()), &limits, &mappings),
        Err(OutputRefusal::Breach(OutputBreach {
            rule: "job_output_entry",
            ..
        }))
    ));
    // An output root that cannot be read says nothing about the job.
    assert!(matches!(
        collect_job_outputs(Some(&root.path().join("absent")), &limits, &mappings),
        Err(OutputRefusal::Unreadable(_))
    ));
    assert!(matches!(
        collect_job_outputs(None, &limits, &mappings),
        Err(OutputRefusal::Unreadable(_))
    ));
}

#[tokio::test]
async fn an_exited_job_whose_outputs_break_their_bound_ends_failed_with_the_evidence() {
    // The Step1X geometry job wrote its prepared input next to its one
    // declared mesh: two entries against a bound of one. The exit is
    // receipted, so the next claim of the same job must end it at once.
    let (claim, mut request) = job_claim();
    request.output_limits.max_files = 1;
    let data = tempdir().unwrap();
    let runtime_root = tempdir().unwrap();
    let outputs = data
        .path()
        .join("runs")
        .join(request.job_id.to_string())
        .join("outputs");
    fs::create_dir_all(&outputs).unwrap();
    fs::write(outputs.join("prepared-input.png"), b"input").unwrap();
    fs::write(outputs.join("result.png"), b"mesh").unwrap();
    let runtime = OciRuntime {
        runner: &NoProcess,
        data_root: data.path(),
    };
    runtime.persist_job_intent(&request).unwrap();
    let receipt = job_receipt(
        &request,
        0,
        Instant::now(),
        empty_job_output_manifest(),
        None,
    );
    runtime.persist_job_completion(&receipt).unwrap();
    let client = AgentHttpClient::for_http_test("http://127.0.0.1:9/", NODE_ID);
    let executor = RecipeExecutor {
        client: &client,
        helper_socket: std::path::Path::new(crate::host_runtime::HELPER_SOCKET),
        runtime,
        runtime_root: runtime_root.path(),
    };
    let (_lease, lease) = tokio::sync::watch::channel(claim.deadline);
    let (_cancel, cancel) = tokio::sync::watch::channel(false);
    let result = executor
        .execute_job_run(&claim, lease, cancel, request)
        .await;
    let failure = result.failure().expect("a definite failure");
    assert_eq!(
        failure.failure_kind,
        Some(AgentFailureKind::InvalidContract)
    );
    assert_eq!(failure.stage, Some(FailureStage::OutputStorage));
    assert_eq!(
        failure
            .refusal_bound
            .as_ref()
            .map(|bound| (bound.limit, bound.observed)),
        Some((Some(1), 2))
    );
    let diagnostic = failure.diagnostic.as_deref().unwrap_or_default();
    for fact in ["exit_code=0", "prepared-input.png", "result.png"] {
        assert!(diagnostic.contains(fact), "{diagnostic}");
    }
    // The failure ends the job on the wire: no receipt, so no observation.
    let AgentResultResult::OutcomeFailed(outcome) = result.finish(&claim).result else {
        panic!("failed outcome");
    };
    assert!(outcome.receipt.is_none());
    assert_eq!(
        outcome.failure_kind,
        Some(AgentFailureKind::InvalidContract)
    );
    // The job's files stay for the retention sweep.
    assert!(outputs.join("prepared-input.png").exists());
}
