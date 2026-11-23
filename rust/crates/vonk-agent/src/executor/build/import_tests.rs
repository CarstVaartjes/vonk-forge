#![cfg(test)]
use super::*;
use crate::recipe_builder::{PodmanImportDiagnostic, RecipeBuildError};

#[test]
fn completed_podman_import_failure_retains_evidence_and_only_retries_transient_causes() {
    for diagnostic in [
        PodmanImportDiagnostic::TemporaryStorageExhausted,
        PodmanImportDiagnostic::SubordinateIdMappingUnavailable,
        PodmanImportDiagnostic::ArchiveFormatRejected,
        PodmanImportDiagnostic::PermissionDenied,
        PodmanImportDiagnostic::DeadlineExceeded,
        PodmanImportDiagnostic::DiagnosticOutputLimitExceeded,
        PodmanImportDiagnostic::SubprocessUnavailable,
        PodmanImportDiagnostic::Unknown,
    ] {
        let error = RecipeBuildError::BaseImageImport {
            diagnostic,
            logs: Box::new(crate::failure_evidence::FailureProcessLogs {
                exit_code: None,
                stdout: crate::failure_evidence::log_tail(b""),
                stderr: crate::failure_evidence::log_tail(
                    b"import rejected\nAuthorization: Bearer never-persist\n",
                ),
            }),
        };
        let ExecutionResult::Failed(failure) = build_error_outcome(&error) else {
            panic!("a completed import failure must not become an unknown observation");
        };
        assert_eq!(failure.stage, Some(FailureStage::BaseImageImport));
        assert_eq!(failure.diagnostic, Some(diagnostic.to_string()));
        let logs = failure.process_logs.as_ref().expect("retain import stderr");
        assert!(logs.stderr.text.contains("import rejected"));
        assert!(!logs.stderr.text.contains("never-persist"));
        let kind = match diagnostic {
            PodmanImportDiagnostic::TemporaryStorageExhausted => {
                Some(AgentFailureKind::ResourcePrerequisite)
            }
            PodmanImportDiagnostic::DeadlineExceeded => Some(AgentFailureKind::TemporaryDependency),
            PodmanImportDiagnostic::SubordinateIdMappingUnavailable
            | PodmanImportDiagnostic::ArchiveFormatRejected
            | PodmanImportDiagnostic::PermissionDenied
            | PodmanImportDiagnostic::DiagnosticOutputLimitExceeded
            | PodmanImportDiagnostic::SubprocessUnavailable
            | PodmanImportDiagnostic::NonzeroWithoutOutput
            | PodmanImportDiagnostic::Unknown => None,
        };
        assert_eq!(failure.failure_kind, kind);
        assert_eq!(failure.retry_after_seconds, kind.map(|_| 30));
        let wire = ExecutionResult::Failed(failure).finish_for(&AgentOperation::RecipeBuildV1);
        let vonk_agent_protocol::generated::AgentResultResult::OutcomeFailed(outcome) = wire.result
        else {
            panic!("the Controller must receive a definite failure");
        };
        assert_eq!(outcome.failure_kind, kind);
        assert_eq!(outcome.retry_after_seconds, kind.map(|_| 30));
        assert_eq!(
            outcome.evidence.unwrap().stage.as_deref(),
            Some(FailureStage::BaseImageImport.as_str())
        );
    }
}
