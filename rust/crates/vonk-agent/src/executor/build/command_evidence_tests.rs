use super::*;
use crate::recipe_builder::{PodmanBuildDiagnostic, RecipeBuildError};

#[test]
fn host_denials_are_platform_blockers_not_recipe_failures_or_silent_retries() {
    // Wrong implementation: classify the same host denial as a bad recipe,
    // or an unknown effect which the Controller keeps reissuing.
    for diagnostic in [
        PodmanBuildDiagnostic::SubordinateIdMappingUnavailable,
        PodmanBuildDiagnostic::UserNamespaceDenied,
        PodmanBuildDiagnostic::ProcMountDenied,
        PodmanBuildDiagnostic::PermissionDenied,
    ] {
        let error = RecipeBuildError::ImageBuild {
            diagnostic,
            logs: Box::new(crate::failure_evidence::FailureProcessLogs {
                exit_code: Some(125),
                stdout: crate::failure_evidence::log_tail(b""),
                stderr: crate::failure_evidence::log_tail(b"cannot clone: Operation not permitted"),
            }),
        };
        let wire = build_error_outcome(&error).finish_for(&AgentOperation::RecipeBuildV1);
        let vonk_agent_protocol::generated::AgentResultResult::OutcomeFailed(outcome) = wire.result
        else {
            panic!("a host denial is a definite platform blocker");
        };
        assert_eq!(
            outcome.code,
            vonk_agent_protocol::generated::FailureCode::RecipeBuildPlatformBlocked
        );
        assert_eq!(outcome.retry_after_seconds, None);
        let evidence = outcome.evidence.unwrap().diagnostics.unwrap();
        assert_eq!(
            evidence.category,
            crate::failure_evidence::FailureCategory::PlatformPolicy
        );
        assert!(evidence.stderr.text.contains("cannot clone"));
    }
}

#[test]
fn a_podman_go_crash_after_denial_words_is_not_reported_as_a_blocked_platform() {
    // Wrong implementation: the 2026-10-26 incident, a crash after a RUN
    // that printed `permission denied`, became "rootless Podman platform is
    // blocked".
    let output = crate::process::ProcessOutput {
            exit_code: Some(2),
            success: false,
            stdout: b"[2/2] STEP 11/28: RUN uv pip install torch\n".to_vec(),
            stderr: b"error: failed to hardlink: permission denied (os error 13)\npanic: runtime error: invalid memory address\n\ngoroutine 61 [running]:\nnetavark.Teardown(...)\n"
                .to_vec(),
        };
    let error = RecipeBuildError::ImageBuild {
        diagnostic: crate::recipe_builder::podman_build_diagnostic(&output),
        logs: Box::new(crate::failure_evidence::FailureProcessLogs {
            exit_code: Some(2),
            stdout: crate::failure_evidence::log_tail(&output.stdout),
            stderr: crate::failure_evidence::podman_stderr_tail(&output.stderr),
        }),
    };
    let ExecutionResult::Failed(failure) = build_error_outcome(&error) else {
        panic!("a returned crash is a definite failure");
    };
    assert_ne!(
        failure.code,
        Some(vonk_agent_protocol::generated::FailureCode::RecipeBuildPlatformBlocked)
    );
    assert!(!failure.reason.contains("platform is blocked"));
}

#[test]
fn every_nonzero_build_stage_survives_executor_classification_with_its_evidence() {
    let logs = || {
        Box::new(crate::failure_evidence::FailureProcessLogs {
            exit_code: Some(125),
            stdout: crate::failure_evidence::log_tail(b""),
            stderr: crate::failure_evidence::log_tail(b"opaque command failure"),
        })
    };
    for error in [
        RecipeBuildError::ImageBuild {
            diagnostic: PodmanBuildDiagnostic::Unknown,
            logs: logs(),
        },
        RecipeBuildError::AdapterBuild {
            diagnostic: PodmanBuildDiagnostic::Unknown,
            logs: logs(),
        },
        RecipeBuildError::Command {
            stage: FailureStage::ImageVerification,
            diagnostic: PodmanBuildDiagnostic::Unknown,
            logs: logs(),
        },
        RecipeBuildError::NetworkBoundary {
            stage: FailureStage::EgressAddress,
            diagnostic: PodmanBuildDiagnostic::Unknown,
            logs: logs(),
        },
    ] {
        let ExecutionResult::Failed(failure) = build_error_outcome(&error) else {
            panic!("a nonzero command was collapsed into an unknown effect");
        };
        let evidence =
            crate::failure_evidence::from_failure(&AgentOperation::RecipeBuildV1, &failure);
        assert_eq!(evidence.process_exit_code, Some(125));
        assert!(evidence.stderr.text.contains("opaque command failure"));
    }
}
