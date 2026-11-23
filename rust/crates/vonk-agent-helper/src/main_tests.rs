#![cfg(test)]
use super::{
    HelperErrorCode, HelperRejection, HelperResponse, HostHelperResponseStatus,
    MAX_CONCURRENT_REQUESTS, acquire_worker,
};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
use vonk_agent_helper::{
    operations::{CommandFailure, OperationError},
    protocol::{ContainerRuntimeAction, HostOperation},
};

#[test]
fn image_collection_ticks_without_requests_and_stops_with_its_owner() {
    use std::sync::mpsc::{RecvTimeoutError, channel};
    use std::time::{Duration, Instant};
    let (observed, ticks) = channel();
    let lifetime = super::spawn_image_collection(
        move || {
            let _ = observed.send(());
        },
        Duration::from_millis(20),
    );
    ticks
        .recv_timeout(Duration::from_secs(2))
        .expect("initial collection");
    ticks
        .recv_timeout(Duration::from_secs(2))
        .expect("idle collection without a helper request");
    drop(lifetime);
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        match ticks.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
            Err(RecvTimeoutError::Disconnected) => break,
            Err(RecvTimeoutError::Timeout) => panic!("collection outlived its helper owner"),
            Ok(()) => {}
        }
    }
}

#[test]
fn helper_request_concurrency_is_bounded_and_reusable() {
    let counter = Arc::new(AtomicUsize::new(0));
    let permits = (0..MAX_CONCURRENT_REQUESTS)
        .map(|_| acquire_worker(&counter).unwrap())
        .collect::<Vec<_>>();

    assert!(acquire_worker(&counter).is_none());
    drop(permits);
    assert_eq!(counter.load(Ordering::Acquire), 0);
    assert!(acquire_worker(&counter).is_some());
}

#[test]
fn runtime_command_rejection_retains_stderr_and_numeric_exit_in_frame() {
    let (mut client, mut server) = std::os::unix::net::UnixStream::pair().unwrap();
    let error = HelperRejection::for_error(
        uuid::Uuid::new_v4().to_string(),
        false,
        OperationError::CommandFailed {
            cause: CommandFailure::Exit {
                code: Some(125),
                stderr: "opaque runtime failure".into(),
            },
        },
    );
    super::reject(&mut server, &error);
    let bytes = vonk_agent_helper::protocol::read_frame(&mut client).unwrap();
    let response: HelperResponse = vonk_agent_protocol::parse_strict(&bytes).unwrap();
    assert_eq!(
        response.error_code,
        Some(HelperErrorCode::OperationCommandFailed)
    );
    let logs = response.process_logs.unwrap();
    assert_eq!(logs.exit_code, Some(125));
    assert!(logs.stderr.text.contains("opaque runtime failure"));
    assert!(logs.stderr.text.len() <= 2048);
}

#[test]
fn framed_rejection_uses_the_shared_response_contract() {
    let (mut client, mut server) = std::os::unix::net::UnixStream::pair().unwrap();
    super::reject(
        &mut server,
        &HelperRejection::new(HelperErrorCode::RequestInvalid, "private diagnostic"),
    );
    let bytes = vonk_agent_helper::protocol::read_frame(&mut client).unwrap();
    let response: HelperResponse = vonk_agent_protocol::parse_strict(&bytes).unwrap();
    assert_eq!(response.status, HostHelperResponseStatus::Rejected);
    assert!(response.request_id.is_none());
    assert_eq!(response.error_code, Some(HelperErrorCode::RequestInvalid));
    assert_eq!(
        vonk_agent_protocol::canonical_generated_json(&response).unwrap(),
        bytes
    );
    assert!(
        !String::from_utf8(bytes)
            .unwrap()
            .contains("private diagnostic")
    );
}

#[test]
fn exited_runtime_diagnostics_survive_the_framed_helper_response() {
    let (mut client, mut server) = std::os::unix::net::UnixStream::pair().unwrap();
    let operation = HostOperation::ExecuteContainerRuntimeRequestOperation(
        vonk_agent_protocol::generated::ExecuteContainerRuntimeRequestOperation {
            type_:
                vonk_agent_protocol::generated::HostOperationKind::ExecuteContainerRuntimeRequest
                    .as_str()
                    .into(),
            installation_intent_nonce: None,
            installation_intent_ordinal: Some(1),
            action: ContainerRuntimeAction::RunInspect,
            fence: uuid::Uuid::nil(),
            request_sha256: "a".repeat(64),
            installation_id: None,
            reconciliation_identity: None,
            run_generation: None,
            runtime_installation_id: None,
            runtime_run_id: None,
            runtime_target_id: None,
            start_plan_sha256: None,
            stop_plan_sha256: None,
        },
    );
    let rejection = HelperRejection::for_operation(
        "10000000-0000-4000-8000-000000000001",
        &operation,
        OperationError::RuntimeProcessExited {
            logs: Some(Box::new(vonk_agent_helper::runtime_logs::retain_container(
                b"worker rank 1 is starting\n",
                &b"startup failed\n".repeat(2000 * 8),
            ))),
            capture_error: None,
            exit_summary: "exit_code=1 exit_cause=unclassified".to_owned(),
        },
    );
    super::reject(&mut server, &rejection);
    let bytes = vonk_agent_helper::protocol::read_frame(&mut client).unwrap();
    let response: HelperResponse = vonk_agent_protocol::parse_strict(&bytes).unwrap();
    let logs = response.process_logs.unwrap();
    assert_eq!(logs.stdout.text, "worker rank 1 is starting\n");
    assert!(logs.stderr.text.contains("startup failed"));
    assert!(logs.stderr.truncated);
    assert!(
        logs.stderr.text.chars().count() <= vonk_agent_protocol::failure_evidence::LOG_TAIL_BYTES
    );
    assert_eq!(
        response.diagnostic.as_deref(),
        Some("exit_code=1 exit_cause=unclassified")
    );
}

#[test]
fn rejection_response_contains_only_stable_diagnostics() {
    let response = HelperResponse {
        diagnostic: None,
        process_logs: None,
        container_labels: None,
        installation_intent_nonce: None,
        schema_version: 1,
        request_id: Some("10000000-0000-4000-8000-000000000001".parse().unwrap()),
        status: HostHelperResponseStatus::Rejected,
        exit_code: None,
        error_code: Some(HelperErrorCode::OperationFailed),
        process_running: None,
        job_progress_at: None,
    };
    let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
    let value: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(value["request_id"], "10000000-0000-4000-8000-000000000001");
    assert!(value.get("detail").is_none());
    assert!(value.get("stderr").is_none());
}

#[test]
fn success_response_omits_unused_optional_fields() {
    let response = HelperResponse {
        diagnostic: None,
        process_logs: None,
        container_labels: None,
        installation_intent_nonce: None,
        schema_version: 1,
        request_id: Some("10000000-0000-4000-8000-000000000001".parse().unwrap()),
        status: HostHelperResponseStatus::PackageInstalled,
        exit_code: None,
        error_code: None,
        process_running: None,
        job_progress_at: None,
    };
    let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
    let value: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert!(value.get("error_code").is_none());
}

#[test]
fn request_id_is_only_attached_after_authorization() {
    let before_authorization =
        HelperRejection::new(HelperErrorCode::GrantUnauthorized, "signature was invalid");
    assert!(before_authorization.request_id.is_none());

    let after_authorization =
        HelperRejection::for_request("request-1", HelperErrorCode::OperationFailed, "dpkg failed");
    assert_eq!(after_authorization.request_id.as_deref(), Some("request-1"));
}

#[test]
fn a_ledger_io_failure_leaves_fresh_grants_admissible_after_repair() {
    // Wrong implementation: a failed local write was classified as token
    // replay and a later distinct grant could not be durably claimed.
    let temp = tempfile::tempdir().unwrap();
    let owner = rustix::process::geteuid().as_raw();
    let root = temp.path().join("requests");
    assert!(super::claim_once_at(&root, "first", owner).is_err());
    assert!(!root.join("first").exists());
    std::fs::create_dir(&root).unwrap();
    assert!(super::claim_once_at(&root, "fresh", owner).is_ok());
    assert!(super::claim_once_at(&root, "fresh", owner).is_err());
    assert!(super::claim_once_at(&root, "next", owner).is_ok());
}

#[test]
fn package_failure_evidence_redacts_details_and_bounds_exit_codes() {
    let operation = HostOperation::InstallVonkDebOperation(
        vonk_agent_protocol::generated::InstallVonkDebOperation {
            type_: vonk_agent_protocol::generated::HostOperationKind::InstallVonkDeb
                .as_str()
                .into(),
            rollback: vonk_agent_protocol::PackageRollbackAuthority {
                source: vonk_agent_protocol::PackageRollbackSource {
                    package_sha256: "a".repeat(64),
                    package_signature: "b".repeat(128),
                    package_version: "0.1.0".into(),
                    binary_sha256: "c".repeat(64),
                    helper_sha256: "d".repeat(64),
                },
                attempt_nonce: "e".repeat(64),
                activation_deadline: 2100000000,
            },
            package_sha256: "a".repeat(64),
            package_signature: "b".repeat(128),
        },
    );
    let install = HelperRejection::for_operation(
        "request-1",
        &operation,
        OperationError::PackageInstallFailed {
            exit_code: Some(75),
            diagnostic: "configuration failed".into(),
        },
    );
    assert_eq!(install.exit_code, Some(75));
    assert!(!install.detail.contains("configuration"));

    let unbounded = HelperRejection::for_operation(
        "request-1",
        &operation,
        OperationError::PackageInstallFailed {
            exit_code: Some(512),
            diagnostic: "configuration failed".into(),
        },
    );
    assert_eq!(unbounded.exit_code, None);
}

#[test]
fn a_failed_command_journals_its_cause_and_keeps_its_wire_reply() {
    for cause in [
        CommandFailure::Runner("compiled command exceeded its deadline".into()),
        CommandFailure::Exit {
            code: Some(1),
            stderr: "Error response from daemon: conflict\n".into(),
        },
        CommandFailure::Exit {
            code: None,
            stderr: String::new(),
        },
        CommandFailure::UnexpectedAnswer,
        CommandFailure::FabricUnobserved,
        CommandFailure::StartFenced,
    ] {
        let error = OperationError::CommandFailed {
            cause: cause.clone(),
        };
        let journaled = error.to_string();
        let rejection =
            HelperRejection::for_error("10000000-0000-4000-8000-000000000001", false, error);
        assert_eq!(rejection.detail, journaled);
        assert!(rejection.detail.ends_with(&cause.to_string()));

        // Command evidence accompanies the unchanged rejection code;
        // numeric status belongs to these command logs, not a job receipt.
        let (mut client, mut server) = std::os::unix::net::UnixStream::pair().unwrap();
        super::reject(&mut server, &rejection);
        let bytes = vonk_agent_helper::protocol::read_frame(&mut client).unwrap();
        let response: HelperResponse = vonk_agent_protocol::parse_strict(&bytes).unwrap();
        assert_eq!(
            response.error_code,
            Some(HelperErrorCode::OperationCommandFailed)
        );
        assert_eq!(response.status, HostHelperResponseStatus::Rejected);
        assert_eq!(response.diagnostic, None);
        assert_eq!(response.exit_code, None);
        let logs = response.process_logs.unwrap();
        if let CommandFailure::Exit { code, .. } = cause {
            assert_eq!(
                logs.exit_code,
                code.and_then(|code| u32::try_from(code).ok())
            );
        }
    }
}
