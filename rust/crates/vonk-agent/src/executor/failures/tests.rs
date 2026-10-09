#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn host_memory_safety_stop_is_a_typed_capacity_failure() {
    let error = crate::host_runtime::HostRuntimeError::HelperRejected {
        code: HelperErrorCode::RuntimeProcessExited,
        diagnostic: Some("observed_at=2026-10-08T00:00:00+00:00 reason=workload.host_memory_exhausted exit_cause=host_memory_exhausted mem_available_bytes=1 memory_full_avg10=99 trigger=sustained_full_psi sustained_ms=3000".into()),
        process_logs: None,
    };
    let ExecutionResult::Failed(failure) = runtime_observation_failure(&error) else {
        panic!("expected failure");
    };
    let diagnostics = crate::failure_evidence::from_failure(
        &vonk_agent_protocol::generated::AgentOperation::RecipeStart,
        &failure,
    );
    assert!(
        diagnostics
            .preflight
            .iter()
            .any(|p| p.name == "exit_cause" && p.value == "host_memory_exhausted")
    );
    // Fresh success uses the normal operation path; no ban survives ending.
    assert!(matches!(
        recipe_install_success(0),
        ExecutionResult::Done(_)
    ));
}

#[tokio::test]
async fn skipped_malformed_run_does_not_hide_or_empty_valid_run_reports() {
    let server = ObservationServer::new(Some(204));
    let valid_run = Uuid::new_v4();
    let result = report_complete_recipe_run_observations(
        &server.client,
        Utc::now(),
        vec![
            Ok(exact_observation(valid_run)),
            Err(RecipeObservationError::SkippedRun),
        ],
    )
    .await
    .unwrap();
    assert_eq!(result, 1);
    let reports = server.finish();
    assert_eq!(reports.len(), 1);
    assert_eq!(reports[0]["runs"].as_array().unwrap().len(), 1);
    assert_eq!(reports[0]["runs"][0]["run_id"], valid_run.to_string());

    let server = ObservationServer::new(Some(204));
    assert_eq!(
        report_complete_recipe_run_observations(
            &server.client,
            Utc::now(),
            vec![Err(RecipeObservationError::SkippedRun)],
        )
        .await
        .unwrap(),
        0
    );
    assert!(server.finish().is_empty());
}

#[tokio::test]
async fn corrupt_exact_lifecycle_is_skipped_without_reporting_false_absence() {
    let data = tempdir().unwrap();
    let runtime = tempdir().unwrap();
    let run_id = "45ea6921-50c9-4971-be2a-4cd04ce05069";
    fs::create_dir_all(data.path().join("runs").join(run_id)).unwrap();
    let metadata = data.path().join("run-metadata").join(run_id);
    fs::create_dir_all(&metadata).unwrap();
    fs::write(metadata.join("lifecycle.json"), b"not-json").unwrap();
    // The Controller owns this run, so its integrity failure stays.
    let server = ObservationServer::with_disposition(Some(204), Some(""));
    let runner = NoProcess;
    let executor = RecipeExecutor {
        client: &server.client,
        runtime: OciRuntime {
            runner: &runner,
            data_root: data.path(),
        },
        runtime_root: runtime.path(),
    };
    assert_eq!(
        executor
            .report_exact_recipe_run_observations()
            .await
            .unwrap(),
        0
    );
    assert_eq!(
        server.finish(),
        vec![json!({ "disposition": format!("/agent/recipe-runs/{run_id}/disposition") })]
    );
    assert!(metadata.join("lifecycle.json").exists());
}

#[test]
fn failed_recipe_build_preserves_only_safe_classified_evidence() {
    let mut build_claim = claim();
    build_claim.operation = AgentOperation::RecipeBuildV1;
    let reason = "Podman could not import the verified base image (temporary-storage-exhausted)";
    let result = ExecutionResult::Failed(
        Failure::new(reason)
            .stage(FailureStage::BaseImageImport)
            .diagnostic("temporary-storage-exhausted"),
    );

    let failed = failed_outcome(&build_claim, result);

    let evidence = evidence_of(&failed);
    assert_eq!(
        evidence.diagnostic.as_deref(),
        Some("temporary-storage-exhausted")
    );
    let wire = serde_json::to_vec(&failed).unwrap();
    let consumed: vonk_agent_protocol::generated::OutcomeFailed =
        vonk_agent_protocol::parse_strict(&wire).unwrap();
    assert_eq!(evidence_of(&consumed).diagnostic, evidence.diagnostic);
}

#[test]
fn distribution_result_is_controller_safe() {
    let success = distribution_success(DistributionDownloadEvidence {
        model_digests: vec!["d".repeat(64)],
        model_paths: vec![std::path::PathBuf::from("/run/private/model.bin")],
        oci_image_digest: format!("sha256:{}", "b".repeat(64)),
        oci_image_config_digest: format!("sha256:{}", "c".repeat(64)),
        downloaded_bytes: 456,
    });
    // Only the byte count crosses; the local paths and digests stay behind.
    let ExecutionResult::Done(OutcomeDoneResult::ArtifactDistributionResult(body)) = &success
    else {
        panic!("a distribution reports its artifact distribution result");
    };
    assert_eq!(body.downloaded_bytes, 456);

    let mut distribution_claim = claim();
    distribution_claim.operation = AgentOperation::ArtifactDistributionV1;
    let finished = success.finish(&distribution_claim);
    let result = AgentResult {
        fence: Uuid::new_v4(),
        result: finished.result,
        state: finished.state,
    };
    result.validate().unwrap();
    result
        .validate_for_operation(&distribution_claim.operation)
        .unwrap();
    assert_eq!(
        serde_json::to_value(&result.result).unwrap(),
        json!({"kind": "done", "result": {"downloaded_bytes": 456}})
    );
}

#[test]
fn a_denied_controller_request_keeps_bounded_denial_facts() {
    // The incident's invalid-authority failure named neither the denied
    // request nor the status, so its cause could not be recovered from the
    // retained evidence.
    let error = ClientError::Controller(Box::new(ControllerError {
        operation: "controller.request /agent/distribution/assignment".to_owned(),
        endpoint: "/agent/distribution/assignment".to_owned(),
        status: 403,
        code: vonk_agent_protocol::generated::SecurityRefusalReason::ControllerRequestRejected
            .as_str()
            .to_owned(),
        request_id: Some("req-403".to_owned()),
        decision: "exit",
        retry_after_seconds: None,
        summary: None,
    }));

    let diagnostic = controller_denial_diagnostic(&error);

    assert!(diagnostic.contains("http_status=403"));
    assert!(diagnostic.contains("error_code=controller.request_rejected"));
    assert!(diagnostic.contains("endpoint=/agent/distribution/assignment"));
    assert!(diagnostic.contains("request_id=req-403"));
    assert!(diagnostic.len() <= 512);
}

#[test]
fn an_invalid_authority_distribution_failure_preserves_denial_context() {
    let mut distribution_claim = claim();
    distribution_claim.operation = AgentOperation::ArtifactDistributionV1;
    let error = ClientError::Controller(Box::new(ControllerError {
        operation: "controller.request /agent/distribution/assignment".to_owned(),
        endpoint: "/agent/distribution/assignment".to_owned(),
        status: 401,
        code:
            vonk_agent_protocol::generated::SecurityRefusalReason::ControllerAuthenticationRequired
                .as_str()
                .to_owned(),
        request_id: Some("req-401".to_owned()),
        decision: "exit",
        retry_after_seconds: None,
        summary: None,
    }));

    let raw = distribution_failure_result(&error);
    assert_eq!(raw.state(), AgentResultState::Failed);
    let failure = raw.failure().expect("a failure");
    assert_eq!(
        failure.failure_kind,
        Some(AgentFailureKind::InvalidAuthority)
    );
    assert_eq!(failure.stage.as_deref(), Some("artifact-distribution"));

    let failed = failed_outcome(&distribution_claim, raw);

    assert_eq!(failed.code, FailureCode::ArtifactDistributionFailed);
    assert_eq!(
        failed.failure_kind,
        Some(AgentFailureKind::InvalidAuthority)
    );
    let evidence = evidence_of(&failed);
    assert_eq!(evidence.stage.as_deref(), Some("artifact-distribution"));
    let diagnostic = evidence.diagnostic.as_deref().unwrap();
    assert!(diagnostic.contains("http_status=401"));
    assert!(diagnostic.contains("request_id=req-401"));
}

#[test]
fn an_exited_workload_failure_carries_the_captured_container_output() {
    // Wrong implementation this catches: the guard's rejection was reported as
    // its code alone, so an operator read "the observation failed" when the
    // answer was that the workload process had exited and printed why.  The
    // inspection gate admits that text precisely because the inspection
    // already proved the container's identity and sanitized it.
    let error = crate::host_runtime::HostRuntimeError::HelperRejected {
        code: HelperErrorCode::RuntimeProcessExited,
        diagnostic: None,
        process_logs: Some(Box::new(crate::failure_evidence::FailureProcessLogs {
            stdout: crate::failure_evidence::log_tail(b"rank 0 listening on 8888\n"),
            stderr: crate::failure_evidence::log_tail(b"ModuleNotFoundError: runtime module\n"),
        })),
    };
    let result = runtime_observation_failure(&error);
    assert_eq!(result.state(), AgentResultState::Failed);
    let failure = result.failure().expect("a failure");
    let reason = failure.reason.as_str();
    assert!(reason.contains("runtime_process_exited"), "{reason}");
    // Both streams arrive as themselves: merging them into one tail is what
    // discarded the stream that was written first.
    let logs = failure.process_logs.as_ref().expect("captured output");
    assert!(logs.stdout.text.contains("listening on 8888"));
    assert!(logs.stderr.text.contains("ModuleNotFoundError"));
}

#[test]
fn an_exited_workload_failure_always_has_logs_or_a_typed_reason_and_the_exit_facts() {
    // The class guard: a process exit is never reported as a bare code.
    // Wrong implementation this catches: a helper that returned neither
    // logs nor a diagnostic left the evidence with empty tails and category
    // `unknown`, the exact shape that made a silent crash undiagnosable.
    let cases = [
        (None, None),
        (
            Some("exit_code=137 oom_killed=true exit_cause=oom_killed no_output=true"),
            None,
        ),
        (
            Some("exit_code=1 exit_cause=bad_arguments"),
            Some(crate::failure_evidence::FailureProcessLogs {
                stdout: crate::failure_evidence::log_tail(b""),
                stderr: crate::failure_evidence::log_tail(b"error: unknown argument\n"),
            }),
        ),
    ];
    for (diagnostic, logs) in cases {
        let error = crate::host_runtime::HostRuntimeError::HelperRejected {
            code: HelperErrorCode::RuntimeProcessExited,
            diagnostic: diagnostic.map(str::to_owned),
            process_logs: logs.map(Box::new),
        };
        let result = runtime_observation_failure(&error);
        let failure = result.failure().expect("a failure");
        assert!(
            failure.process_logs.is_some() || failure.diagnostic.is_some(),
            "{diagnostic:?}"
        );
        let evidence = crate::failure_evidence::from_failure(
            &vonk_agent_protocol::generated::AgentOperation::RecipeStart,
            failure,
        );
        if diagnostic.is_some_and(|text| text.contains("oom_killed")) {
            assert!(
                evidence
                    .preflight
                    .iter()
                    .any(|p| p.name == "exit_cause" && p.value == "oom_killed")
            );
            assert!(
                evidence
                    .preflight
                    .iter()
                    .any(|p| p.name == "exit_code" && p.value == "137")
            );
            assert_eq!(
                evidence.category,
                crate::failure_evidence::FailureCategory::Capacity
            );
        }
    }
}

#[test]
fn preparation_failure_keeps_safe_stage_and_permission_boundary_in_controller_result() {
    use crate::oci::OciError;

    let mut start_claim = claim();
    start_claim.operation = AgentOperation::RecipeStart;
    let error = OciError::Start {
        stage: FailureStage::OutputStorage,
        source: Box::new(OciError::Io(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "/private/secret-credential-value",
        ))),
    };
    let failure = super::runtime_preparation_failure(&error);
    let failed = failed_outcome(&start_claim, failure);
    assert_eq!(
        failed.reason,
        "container runtime could not prepare the workload (stage=output-storage; category=storage-permission-denied)"
    );
}

#[test]
fn rank_launch_failure_keeps_sanitized_logs_in_the_controller_contract() {
    let mut start_claim = claim();
    start_claim.operation = AgentOperation::RecipeStart;
    let error = crate::host_runtime::HostRuntimeError::HelperRejected {
        code: HelperErrorCode::RuntimeProcessExited,
        diagnostic: None,
        process_logs: Some(Box::new(crate::failure_evidence::FailureProcessLogs {
            stdout: crate::failure_evidence::log_tail(b"starting the engine core\n"),
            stderr: crate::failure_evidence::log_tail(
                b"ModuleNotFoundError: runtime module\nAPI_TOKEN=private-value\n",
            ),
        })),
    };
    let failed = super::runtime_failure("rank process did not remain stable after launch", &error);
    let body = failed_outcome(&start_claim, failed);
    let diagnostics = evidence_of(&body).diagnostics.as_ref().unwrap();
    diagnostics.validate().unwrap();
    assert!(diagnostics.stdout.text.contains("starting the engine core"));
    assert!(diagnostics.stderr.text.contains("ModuleNotFoundError"));
    assert!(
        !serde_json::to_string(&body)
            .unwrap()
            .contains("private-value")
    );
}

#[tokio::test]
async fn a_never_enabled_operation_fails_definitively_and_never_waits() {
    // Wrong implementation: the unsupported-operation executor reported a
    // wait for an operator about work that never ran.
    let mut start_claim = claim();
    start_claim.operation = AgentOperation::RecipeStart;
    let (_lease_sender, lease_deadline) = tokio::sync::watch::channel(start_claim.deadline);
    let (_cancel_sender, cancellation) = tokio::sync::watch::channel(false);
    let result = RejectingExecutor
        .execute(&start_claim, lease_deadline, cancellation)
        .await;

    assert_eq!(result.state(), AgentResultState::Failed);
    assert_eq!(failed_outcome(&start_claim, result).failure_kind, None);
}

#[test]
fn a_refused_request_bound_reaches_the_failure_evidence() {
    // Wrong implementation: the refusal named its rule but not the bound, so
    // an operator had to read the constants to tell 518 of 4096 from 5000
    // of 4096.
    let mut start_claim = claim();
    start_claim.operation = AgentOperation::RecipeStart;
    let limit = vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES as u64;
    let error = crate::host_runtime::HostRuntimeError::HelperProtocolBound {
        cause: crate::host_runtime::HelperProtocolCause::RequestBytes,
        limit: Some(limit),
        observed: limit + 1,
    };
    let failed = super::runtime_failure("container runtime could not start the workload", &error);
    let body = failed_outcome(&start_claim, failed);
    let diagnostics = evidence_of(&body).diagnostics.as_ref().unwrap();
    let refusal = diagnostics
        .preflight
        .iter()
        .find(|property| property.name == "request_refusal")
        .expect("the refusal bound must be reported");
    assert!(refusal.value.contains("request_bytes_invalid"));
    assert!(refusal.value.contains(&format!("limit={limit}")));
    assert!(refusal.value.contains(&format!("observed={}", limit + 1)));
    assert!(body.reason.contains("helper_request_bytes_invalid"));
}

#[test]
fn image_pull_helper_protocol_cause_survives_normalization() {
    // Wrong implementation: normalization dropped a helper cause, or an
    // unavailable observation became a definitive failure. Both paths must
    // retain their typed cause in the current wire contract even when the
    // free-text sanitizer redacts a long diagnostic word.
    let mut pull_claim = claim();
    pull_claim.operation = AgentOperation::ArtifactDistributionV1;
    let mut errors: Vec<crate::host_runtime::HostRuntimeError> = [
        crate::host_runtime::HelperProtocolCause::RequestEncoding,
        crate::host_runtime::HelperProtocolCause::HelperCallJoin,
        crate::host_runtime::HelperProtocolCause::MessageFraming,
        crate::host_runtime::HelperProtocolCause::ResponseUnbound,
        crate::host_runtime::HelperProtocolCause::RejectionMalformed,
        crate::host_runtime::HelperProtocolCause::OutcomeMalformed,
        crate::host_runtime::HelperProtocolCause::RequestDocument,
        crate::host_runtime::HelperProtocolCause::RequestArgumentsPresence,
        crate::host_runtime::HelperProtocolCause::RequestPlanBinding,
        crate::host_runtime::HelperProtocolCause::RequestInstallationIdentity,
        crate::host_runtime::HelperProtocolCause::RequestBytes,
        crate::host_runtime::HelperProtocolCause::RequestPlanBytes,
        crate::host_runtime::HelperProtocolCause::RequestArgumentNulByte,
        crate::host_runtime::HelperProtocolCause::RequestStorage,
        crate::host_runtime::HelperProtocolCause::SystemClock,
        crate::host_runtime::HelperProtocolCause::InspectionOutcome,
    ]
    .into_iter()
    .map(crate::host_runtime::HostRuntimeError::HelperProtocol)
    .collect();
    errors.push(crate::host_runtime::HostRuntimeError::StopUncertain);
    for error in errors {
        let code = super::runtime_helper_code(&error);
        let result = failed_outcome(
            &pull_claim,
            ExecutionResult::Failed(Failure::new("runtime image pull failed").helper(code, None)),
        );
        assert_eq!(
            evidence_of(&result).helper_error_code.as_deref(),
            Some(code.as_str()),
            "{code} must survive normalization rather than be silently dropped"
        );
        let finished =
            super::runtime_failure("runtime image pull failed", &error).finish(&pull_claim);
        let wire = AgentResult {
            fence: pull_claim.fence,
            result: finished.result,
            state: finished.state,
        };
        wire.validate_for_operation(&pull_claim.operation).unwrap();
        let AgentResultResult::OutcomeUnknown(unknown) = wire.result else {
            panic!("an unavailable helper observation must remain unknown");
        };
        assert_eq!(
            unknown.evidence.as_ref().unwrap().helper_error_code.as_deref(),
            Some(code.as_str()),
            "{code} must survive unknown-outcome normalization independently of diagnostic redaction"
        );
    }
}

#[test]
fn agent_upgrade_failure_preserves_only_bounded_helper_diagnostics() {
    let mut upgrade_claim = claim();
    upgrade_claim.operation = AgentOperation::AgentUpgradeV1;
    let result = failed_outcome(
        &upgrade_claim,
        ExecutionResult::Failed(
            Failure::new("agent upgrade helper rejected the request: package_install_failed")
                .helper(HelperErrorCode::PackageInstallFailed, Some(75)),
        ),
    );

    let evidence = evidence_of(&result);
    assert_eq!(
        evidence.helper_error_code.as_deref(),
        Some("package_install_failed")
    );
    assert_eq!(evidence.helper_exit_code, Some(75));

    // An unlisted helper code, or an exit status outside one byte, is dropped.
    let rejected = failed_outcome(
        &upgrade_claim,
        ExecutionResult::Failed(
            Failure::new("agent upgrade failed")
                .helper(HelperErrorCode::ConcurrencyLimit, Some(512)),
        ),
    );
    assert!(evidence_of(&rejected).helper_error_code.is_none());
    assert!(evidence_of(&rejected).helper_exit_code.is_none());
    let out_of_range = failed_outcome(
        &upgrade_claim,
        ExecutionResult::Failed(
            Failure::new("agent upgrade failed")
                .helper(HelperErrorCode::PackageInstallFailed, Some(512)),
        ),
    );
    assert!(evidence_of(&out_of_range).helper_exit_code.is_none());
}

#[test]
fn image_pull_failure_preserves_only_bounded_helper_diagnostics() {
    let mut pull_claim = claim();
    pull_claim.operation = AgentOperation::ArtifactDistributionV1;
    for code in [
        HelperErrorCode::RuntimeHelperUnavailable,
        HelperErrorCode::RuntimeAuthorityUnavailable,
        HelperErrorCode::RuntimeHelperProtocolInvalid,
        HelperErrorCode::GrantUnauthorized,
        HelperErrorCode::RequestReplayed,
    ] {
        let result = failed_outcome(
            &pull_claim,
            ExecutionResult::Failed(Failure::new("runtime image pull failed").helper(code, None)),
        );

        assert_eq!(
            evidence_of(&result).helper_error_code,
            Some(code.to_string())
        );
    }

    let rejected = failed_outcome(
        &pull_claim,
        ExecutionResult::Failed(
            Failure::new("runtime image pull failed")
                .helper(HelperErrorCode::PackageInstallFailed, None),
        ),
    );
    assert!(evidence_of(&rejected).helper_error_code.is_none());
}

#[tokio::test]
async fn observation_misses_end_without_effects_and_a_fresh_fence_executes() {
    struct RecoveringExecutor(
        std::sync::Mutex<Option<crate::host_runtime::HostRuntimeError>>,
        std::sync::atomic::AtomicUsize,
    );
    #[async_trait(?Send)]
    impl Executor for RecoveringExecutor {
        async fn execute(
            &self,
            _claim: &AgentClaim,
            _deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
            _cancellation: tokio::sync::watch::Receiver<bool>,
        ) -> ExecutionResult {
            if let Some(error) = self.0.lock().unwrap().take() {
                return super::runtime_failure("runtime observation unavailable", &error);
            }
            self.1.fetch_add(1, Ordering::SeqCst);
            recipe_install_success(0)
        }
    }
    for error in [
        crate::host_runtime::HostRuntimeError::Io(std::io::Error::other(
            "temporary storage observation",
        )),
        crate::host_runtime::HostRuntimeError::HelperProtocol(
            crate::host_runtime::HelperProtocolCause::OutcomeMalformed,
        ),
        crate::host_runtime::HostRuntimeError::StopUncertain,
        crate::host_runtime::HostRuntimeError::Controller(ClientError::Identity),
    ] {
        let root = tempdir().unwrap();
        let mut state = StateStore::open(&root.path().join("state.sqlite"), NODE_ID).unwrap();
        let executor = RecoveringExecutor(
            std::sync::Mutex::new(Some(error)),
            std::sync::atomic::AtomicUsize::new(0),
        );
        let original = claim();
        let client = RecordingClient {
            cancel_requested: false,
            claim: Arc::new(Mutex::new(Some(original.clone()))),
            fail_heartbeat: false,
            heartbeats: Arc::new(Mutex::new(Vec::new())),
            results: Arc::new(Mutex::new(Vec::new())),
        };
        tokio::time::timeout(
            Duration::from_secs(1),
            run_once(&client, &mut state, &executor, None, 0, None),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(executor.1.load(Ordering::SeqCst), 0);
        assert!(matches!(
            client.results.lock().unwrap()[0].result,
            AgentResultResult::OutcomeUnknown(_)
        ));
        let mut fresh = original;
        fresh.fence = Uuid::new_v4();
        *client.claim.lock().unwrap() = Some(fresh.clone());
        tokio::time::timeout(
            Duration::from_secs(1),
            run_once(&client, &mut state, &executor, None, 0, None),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(executor.1.load(Ordering::SeqCst), 1);
        assert!(
            client
                .results
                .lock()
                .unwrap()
                .iter()
                .any(|result| result.fence == fresh.fence
                    && matches!(result.result, AgentResultResult::OutcomeDone(_)))
        );
    }
}
