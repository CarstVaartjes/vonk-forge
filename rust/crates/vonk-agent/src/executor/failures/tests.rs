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
    assert_eq!(failure.code, Some(FailureCode::WorkloadHostMemoryExhausted));
    let diagnostics = crate::failure_evidence::from_failure(
        &vonk_agent_protocol::generated::AgentOperation::RecipeStart,
        &failure,
    );
    assert_eq!(
        diagnostics.category,
        crate::failure_evidence::FailureCategory::Capacity
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
    build_claim.operation = "recipe.build.v1".parse().unwrap();
    let reason = "Podman could not import the verified base image (temporary-storage-exhausted)";
    let result = ExecutionResult::Failed(
        Failure::new(reason)
            .stage(FailureStage::BaseImageImport)
            .diagnostic("temporary-storage-exhausted"),
    );

    let failed = failed_outcome(&build_claim, result);

    assert_eq!(failed.code, FailureCode::RecipeBuildFailed);
    assert_eq!(failed.reason, reason);
    let evidence = evidence_of(&failed);
    assert_eq!(evidence.stage.as_deref(), Some("base-image-import"));
    assert_eq!(
        evidence.diagnostic.as_deref(),
        Some("temporary-storage-exhausted")
    );
    // Only the declared evidence fields exist on the typed failure: there is
    // no key through which a host path or any other detail could cross.
    let wire = serde_json::to_value(&failed).unwrap();
    let mut keys: Vec<_> = wire.as_object().unwrap().keys().cloned().collect();
    keys.sort();
    assert_eq!(keys, ["code", "evidence", "kind", "reason"]);
}

#[test]
fn recipe_build_client_failures_keep_typed_retry_and_refusal_evidence() {
    let mut unavailable = ControllerError::from_status(503);
    unavailable.retry_after_seconds = Some(19);
    let denied = ControllerError::from_status(403);
    let invalid_contract = ControllerError::from_status(422);
    let conflict = ControllerError::from_status(409);
    let cases = [
        (
            ClientError::Controller(Box::new(unavailable)),
            FailureStage::SourceBundleFetch,
            AgentFailureKind::TemporaryDependency,
            Some(19),
        ),
        (
            ClientError::Controller(Box::new(denied)),
            FailureStage::SourceBundleFetch,
            AgentFailureKind::InvalidAuthority,
            None,
        ),
        (
            ClientError::Controller(Box::new(invalid_contract)),
            FailureStage::SourceBundleFetch,
            AgentFailureKind::InvalidContract,
            None,
        ),
        (
            ClientError::Protocol,
            FailureStage::ImageUpload,
            AgentFailureKind::InvalidContract,
            None,
        ),
        (
            ClientError::Controller(Box::new(conflict)),
            FailureStage::ImageUpload,
            AgentFailureKind::IntegrityFailure,
            None,
        ),
        (
            ClientError::Retryable,
            FailureStage::ImageUpload,
            AgentFailureKind::TemporaryDependency,
            None,
        ),
    ];
    let mut build_claim = claim();
    build_claim.operation = "recipe.build.v1".parse().unwrap();

    for (error, stage, expected_kind, expected_retry_after) in cases {
        let result = recipe_build_client_failure_result(
            &error,
            stage,
            "build dependency could not be confirmed",
        );
        let failure = failed_outcome(&build_claim, result);

        assert_eq!(failure.code, FailureCode::RecipeBuildFailed);
        assert_eq!(
            evidence_of(&failure).stage.as_deref(),
            Some(stage.to_string().as_str())
        );
        assert_eq!(failure.failure_kind, Some(expected_kind));
        assert_eq!(failure.retry_after_seconds, expected_retry_after);
        assert_eq!(failure.reason, "build dependency could not be confirmed");
    }
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
fn distribution_failure_uses_operation_specific_result_code() {
    let mut distribution_claim = claim();
    distribution_claim.operation = AgentOperation::ArtifactDistributionV1;
    let failed = failed_outcome(
        &distribution_claim,
        ExecutionResult::failed("distribution object digest mismatch"),
    );
    assert_eq!(failed.code, FailureCode::ArtifactDistributionFailed);
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
fn an_expired_distribution_grant_is_a_wait_not_a_denial_of_authority() {
    let expired = ClientError::Controller(Box::new(ControllerError {
        operation: "controller.request /agent/distribution/manifests".to_owned(),
        endpoint: "/agent/distribution/manifests".to_owned(),
        status: 403,
        code: vonk_agent_protocol::generated::DistributionCode::DistributionExpired
            .as_str()
            .to_owned(),
        request_id: None,
        decision: "exit",
        retry_after_seconds: None,
        summary: None,
    }));
    let revoked = ClientError::Controller(Box::new(ControllerError {
        operation: "controller.request /agent/distribution/manifests".to_owned(),
        endpoint: "/agent/distribution/manifests".to_owned(),
        status: 403,
        code: vonk_agent_protocol::generated::DistributionCode::DistributionWrongNode
            .as_str()
            .to_owned(),
        request_id: None,
        decision: "exit",
        retry_after_seconds: None,
        summary: None,
    }));

    let kind = |result: ExecutionResult| result.failure().unwrap().failure_kind;
    assert_eq!(
        kind(distribution_failure_result(&expired)),
        Some(AgentFailureKind::TemporaryDependency)
    );
    assert_eq!(
        kind(distribution_failure_result(&revoked)),
        Some(AgentFailureKind::InvalidAuthority)
    );
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
    assert!(body.reason.contains("helper_runtime_process_exited"));
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

#[test]
fn a_foreign_container_is_refused_untouched_and_named_not_an_invalid_contract() {
    // Wrong implementation: the start failed with no failure kind, which the
    // Controller reads as an invalid contract and ends; or it waited with no
    // action. The refusal is a prerequisite that names the container.
    let mut start_claim = claim();
    start_claim.operation = AgentOperation::RecipeStart;
    let run_id = "11111111-1111-4111-8111-111111111111";
    let failed = failed_outcome(&start_claim, super::retained_container_foreign(run_id));

    assert_eq!(failed.code, FailureCode::RetainedContainerForeign);
    assert_eq!(
        failed.failure_kind,
        Some(AgentFailureKind::ResourcePrerequisite)
    );
    assert!(
        failed
            .retry_after_seconds
            .is_some_and(|seconds| seconds > 0)
    );
    assert!(failed.reason.contains(&format!("\"vonk-{run_id}\"")));
    let evidence = evidence_of(&failed);
    assert_eq!(evidence.stage.as_deref(), Some("retained-container"));
    assert_eq!(
        evidence.diagnostic.as_deref(),
        Some(format!("container=\"vonk-{run_id}\"").as_str())
    );
}

#[test]
fn a_removed_retained_container_re_issues_the_start() {
    // Wrong implementation: after removing its own stale container the start
    // reported a terminal failure, so the load ended instead of starting fresh.
    let mut start_claim = claim();
    start_claim.operation = AgentOperation::RecipeStart;
    let failed = failed_outcome(
        &start_claim,
        super::retained_container_removed("11111111-1111-4111-8111-111111111111"),
    );

    assert_eq!(
        failed.failure_kind,
        Some(AgentFailureKind::TemporaryDependency)
    );
    assert_ne!(failed.code, FailureCode::RetainedContainerForeign);
}

#[test]
fn an_unconfirmed_stop_carries_the_helper_verdict_as_evidence() {
    // Wrong implementation: the stop error was discarded, so the wait said
    // "remains unconfirmed" and nothing more.
    let error = crate::host_runtime::HostRuntimeError::HelperRejected {
        code: HelperErrorCode::OperationIo,
        diagnostic: None,
        process_logs: None,
    };
    let evidence = super::host_runtime_evidence(FailureStage::Stop, &error);

    assert_eq!(evidence.stage, FailureStage::Stop);
    assert_eq!(evidence.helper_error_code.as_deref(), Some("operation_io"));
    assert_eq!(evidence.diagnostic.as_deref(), Some("helper_operation_io"));
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
fn runtime_failure_names_the_helper_protocol_cause_to_the_controller() {
    // Wrong implementation: the named cause stopped at `preflight_code()`,
    // so the Controller only ever saw the collapsed
    // `helper_protocol_invalid` label on the live blocked-start path.
    let mut start_claim = claim();
    start_claim.operation = AgentOperation::RecipeStart;
    let error = crate::host_runtime::HostRuntimeError::HelperProtocol(
        crate::host_runtime::HelperProtocolCause::OutcomeMalformed,
    );
    let failed = super::runtime_failure("container runtime could not start the workload", &error);
    let body = failed_outcome(&start_claim, failed);
    let reason = body.reason.as_str();
    assert!(
        reason.contains("container runtime could not start the workload: helper_outcome_malformed"),
        "the failure reason must name the violated contract, got {reason}"
    );
}

#[test]
fn runtime_failure_names_the_agent_built_request_to_the_controller() {
    // Wrong implementation: a Start whose agent-built request failed
    // canonical validation reported the collapsed `helper_protocol_invalid`
    // on the live blocked-start path, with no helper involved and nothing
    // for an operator to act on.
    let mut start_claim = claim();
    start_claim.operation = AgentOperation::RecipeStart;
    let error = crate::host_runtime::HostRuntimeError::HelperProtocol(
        crate::host_runtime::HelperProtocolCause::RequestDocument,
    );
    let failed = super::runtime_failure("container runtime could not start the workload", &error);
    let body = failed_outcome(&start_claim, failed);
    let reason = body.reason.as_str();
    assert!(
        reason.contains(
            "container runtime could not start the workload: helper_request_document_invalid"
        ),
        "the failure reason must name the agent-built request, got {reason}"
    );
}

#[test]
fn a_firewall_refusal_names_its_cause_in_the_start_failure_text() {
    // Wrong implementation: the helper's diagnostic travelled only as
    // attached logs, so the failure text read `helper_runtime_fabric_
    // firewall_rejected` and nothing said which argument was refused.
    let mut start_claim = claim();
    start_claim.operation = AgentOperation::RecipeStart;
    let error = crate::host_runtime::HostRuntimeError::HelperRejected {
        code: HelperErrorCode::RuntimeFabricFirewallRejected,
        diagnostic: Some(
            "check-fabric-run endpoint=8000: vonk-forge-docker-firewall: host endpoint \
             port 8000 is not authorized (authorized host endpoint ports: 8888)"
                .to_owned(),
        ),
        process_logs: None,
    };
    let failed = super::runtime_failure("container runtime could not start the workload", &error);
    let result = failed_outcome(&start_claim, failed);
    let reason = result.reason.as_str();
    assert!(
        reason.contains("helper_runtime_fabric_firewall_rejected")
            && reason.contains("host endpoint port 8000 is not authorized")
            && reason.contains("authorized host endpoint ports: 8888"),
        "{reason}"
    );
}

#[test]
fn runtime_failure_names_stop_uncertain_without_calling_it_protocol() {
    // Wrong implementation: an ambiguous stop carried the protocol label,
    // so "we could not confirm the stop" read as a corrupt helper reply.
    let mut start_claim = claim();
    start_claim.operation = AgentOperation::RecipeStart;
    let error = crate::host_runtime::HostRuntimeError::StopUncertain;
    let failed = super::runtime_failure("container runtime could not start the workload", &error);
    let body = failed_outcome(&start_claim, failed);
    let reason = body.reason.as_str();
    assert!(
        reason.contains("container runtime could not start the workload: helper_stop_uncertain"),
        "an ambiguous stop must not read as a malformed reply, got {reason}"
    );
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
    // Wrong implementation: a new cause's code was absent from
    // `stable_runtime_helper_error_code`, so normalization silently dropped
    // it and the Controller saw no cause at all.
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
        crate::host_runtime::HelperProtocolCause::RequestInstallationIdentity,
        crate::host_runtime::HelperProtocolCause::RequestBytes,
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
        assert!(
            code.starts_with("runtime_helper_"),
            "a pull failure code stays in the runtime_helper_ namespace, got {code}"
        );
        let result = failed_outcome(
            &pull_claim,
            super::runtime_failure("runtime image pull failed", &error),
        );
        assert_eq!(
            evidence_of(&result).helper_error_code.as_deref(),
            Some(code.as_str()),
            "{code} must survive normalization rather than be silently dropped"
        );
    }
}

#[test]
fn agent_upgrade_failure_preserves_only_bounded_helper_diagnostics() {
    let mut upgrade_claim = claim();
    upgrade_claim.operation = "agent.upgrade.v1".parse().unwrap();
    let result = failed_outcome(
        &upgrade_claim,
        ExecutionResult::Failed(
            Failure::new("agent upgrade helper rejected the request: package_install_failed")
                .helper(HelperErrorCode::PackageInstallFailed, Some(75)),
        ),
    );

    assert_eq!(result.code, FailureCode::AgentUpgradeFailed);
    assert_eq!(
        result.reason,
        "agent upgrade helper rejected the request: package_install_failed"
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
