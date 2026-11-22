//! Diagnostics.

use super::*;

#[derive(Debug, Error)]
pub enum RecipeBuildError {
    #[error("source bundle failed canonical verification")]
    Source(#[from] BuildSourceError),
    #[error("rootless image build failed")]
    Process(#[from] ProcessError),
    #[error("build evidence is invalid")]
    Evidence,
    #[error("base image registry content is invalid")]
    BaseImageContent,
    #[error("base image manifest transfer or evidence failed")]
    BaseImageManifest,
    #[error("base image blob transfer or evidence failed")]
    BaseImageBlob,
    #[error("base image OCI archive verification failed")]
    BaseImageArchive,
    #[error("Podman could not import the verified base image ({diagnostic})")]
    BaseImageImport {
        diagnostic: PodmanImportDiagnostic,
        logs: Box<crate::failure_evidence::FailureProcessLogs>,
    },
    /// The process did not return command output (spawn, deadline or guard).
    #[error("Podman base image import did not complete ({diagnostic})")]
    BaseImageImportProcess { diagnostic: PodmanImportDiagnostic },
    #[error("Podman imported base image evidence is invalid")]
    BaseImageInspect,
    #[error("Podman recipe image build failed ({diagnostic})")]
    ImageBuild {
        diagnostic: PodmanBuildDiagnostic,
        logs: Box<crate::failure_evidence::FailureProcessLogs>,
    },
    #[error("built recipe image evidence is invalid")]
    ImageInspect,
    #[error("platform runtime adapter is invalid")]
    AdapterInvalid,
    #[error("platform runtime adapter build failed ({diagnostic})")]
    AdapterBuild {
        diagnostic: PodmanBuildDiagnostic,
        logs: Box<crate::failure_evidence::FailureProcessLogs>,
    },
    #[error("adapted runtime image evidence is invalid")]
    AdapterInspect,
    #[error("Podman command failed during {stage} ({diagnostic})")]
    Command {
        stage: FailureStage,
        diagnostic: PodmanBuildDiagnostic,
        logs: Box<crate::failure_evidence::FailureProcessLogs>,
    },
    #[error("build output exceeded its declared limit")]
    OutputLimit,
    #[error(
        "source build network policy is unsupported: public host allowlists require an installed egress boundary"
    )]
    NetworkPolicy,
    #[error("build egress boundary failed during {stage} ({diagnostic})")]
    NetworkBoundary {
        stage: FailureStage,
        diagnostic: PodmanBuildDiagnostic,
        logs: Box<crate::failure_evidence::FailureProcessLogs>,
    },
    #[error("build storage is unavailable")]
    Io(#[from] std::io::Error),
    /// A build effect a bounded cleanup pass still observes: what the cleanup
    /// waits for and what it saw last.
    #[error("waiting for {subject}: {observed}")]
    CleanupPending { subject: String, observed: String },
    /// A build path the cleanup could not change, with the OS error. The path
    /// is quoted because the text sanitizer redacts a bare 40+ character
    /// identifier-like word.
    #[error("build cleanup could not {action} \"{}\": {source}", path.display())]
    CleanupPath {
        action: &'static str,
        path: std::path::PathBuf,
        source: std::io::Error,
    },
    /// A cleanup precondition that fails the same way on every attempt.
    #[error("build cleanup cannot proceed: {0}")]
    CleanupBlocked(String),
}

/// Classification supplements the bounded, sanitized command output; it never
/// replaces the subprocess evidence.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PodmanImportDiagnostic {
    TemporaryStorageExhausted,
    SubordinateIdMappingUnavailable,
    ArchiveFormatRejected,
    PermissionDenied,
    DeadlineExceeded,
    DiagnosticOutputLimitExceeded,
    SubprocessUnavailable,
    NonzeroWithoutOutput,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PodmanBuildDiagnostic {
    TemporaryStorageExhausted,
    SubordinateIdMappingUnavailable,
    UserNamespaceDenied,
    ProcMountDenied,
    PermissionDenied,
    MemoryLimitExceeded,
    StorageDriverFailure,
    SystemdScopeFailure,
    /// A recipe's own `patch` step rejected a hunk or found it already applied:
    /// the base image drifted from what the recipe's patch was written against.
    PatchRejected,
    /// A Dockerfile `RUN` step exited nonzero for a reason no class above names.
    BuildStepFailed,
    NonzeroWithoutOutput,
    Unknown,
}

impl PodmanBuildDiagnostic {
    /// The contract's finding code for this classification: the one spelling of
    /// the fact. The runtime preflight reports it as the finding's code and the
    /// failure evidence derives its kebab-case diagnostic from it ([`fmt::Display`]).
    pub fn finding_code(self) -> RuntimePreflightFindingCode {
        match self {
            Self::TemporaryStorageExhausted => {
                RuntimePreflightFindingCode::PreflightFindingTemporaryStorageExhausted
            }
            Self::SubordinateIdMappingUnavailable => {
                RuntimePreflightFindingCode::PreflightFindingSubordinateIdMappingUnavailable
            }
            Self::UserNamespaceDenied => {
                RuntimePreflightFindingCode::PreflightFindingUserNamespaceDenied
            }
            Self::ProcMountDenied => RuntimePreflightFindingCode::PreflightFindingProcMountDenied,
            Self::PermissionDenied => RuntimePreflightFindingCode::PreflightFindingPermissionDenied,
            Self::MemoryLimitExceeded => {
                RuntimePreflightFindingCode::PreflightFindingMemoryLimitExceeded
            }
            Self::StorageDriverFailure => {
                RuntimePreflightFindingCode::PreflightFindingStorageDriverFailure
            }
            Self::SystemdScopeFailure => {
                RuntimePreflightFindingCode::PreflightFindingSystemdScopeFailure
            }
            Self::PatchRejected => RuntimePreflightFindingCode::PreflightFindingPatchRejected,
            Self::BuildStepFailed => RuntimePreflightFindingCode::PreflightFindingBuildStepFailed,
            Self::NonzeroWithoutOutput => {
                RuntimePreflightFindingCode::PreflightFindingNonzeroWithoutOutput
            }
            Self::Unknown => {
                RuntimePreflightFindingCode::PreflightFindingUnclassifiedPodmanBuildFailure
            }
        }
    }
}

/// The diagnostic text agents have always reported (`temporary-storage-exhausted`):
/// the finding code's own word without its domain prefix, in kebab case.
impl fmt::Display for PodmanBuildDiagnostic {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let code = self.finding_code().to_string();
        let word = code
            .rsplit_once('.')
            .map_or(code.as_str(), |(_, word)| word);
        formatter.write_str(&word.replace('_', "-"))
    }
}

impl fmt::Display for PodmanImportDiagnostic {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::NonzeroWithoutOutput => {
                return PodmanBuildDiagnostic::NonzeroWithoutOutput.fmt(formatter);
            }
            Self::TemporaryStorageExhausted => "temporary-storage-exhausted",
            Self::SubordinateIdMappingUnavailable => "subordinate-id-mapping-unavailable",
            Self::ArchiveFormatRejected => "archive-format-rejected",
            Self::PermissionDenied => "permission-denied",
            Self::DeadlineExceeded => "deadline-exceeded",
            Self::DiagnosticOutputLimitExceeded => "diagnostic-output-limit-exceeded",
            Self::SubprocessUnavailable => "subprocess-unavailable",
            Self::Unknown => "unclassified-podman-load-failure",
        })
    }
}

impl RecipeBuildError {
    /// The typed failure this build error reports: its reason, stage, bounded
    /// cause and the captured process output.
    pub(crate) fn failure_evidence(&self) -> crate::outcome::Failure {
        use crate::outcome::Failure;
        let failure = Failure::new(self.to_string());
        match self {
            Self::Process(error) => failure
                .stage(FailureStage::BoundedBuildProcess)
                .diagnostic(process_error_diagnostic(error)),
            Self::BaseImageImportProcess { diagnostic } => failure
                .stage(FailureStage::BaseImageImport)
                .diagnostic(diagnostic.to_string()),
            Self::BaseImageImport { diagnostic, logs } => failure
                .stage(FailureStage::BaseImageImport)
                .diagnostic(diagnostic.to_string())
                .process_logs(Some((**logs).clone())),
            Self::ImageBuild { diagnostic, logs } => failure
                .stage(FailureStage::ImageBuild)
                .diagnostic(diagnostic.to_string())
                .process_logs(Some((**logs).clone())),
            Self::AdapterBuild { diagnostic, logs } => failure
                .stage(FailureStage::RuntimeAdapter)
                .diagnostic(diagnostic.to_string())
                .process_logs(Some((**logs).clone())),
            Self::Command {
                stage,
                diagnostic,
                logs,
            }
            | Self::NetworkBoundary {
                stage,
                diagnostic,
                logs,
            } => failure
                .stage(*stage)
                .diagnostic(diagnostic.to_string())
                .process_logs(Some((**logs).clone())),
            _ => failure,
        }
    }
}

pub(super) fn command_failure(
    stage: FailureStage,
    output: &crate::process::ProcessOutput,
) -> RecipeBuildError {
    RecipeBuildError::Command {
        stage,
        diagnostic: podman_build_diagnostic(output),
        logs: Box::new(sanitized_process_logs(output)),
    }
}

pub(super) fn sanitized_process_logs(
    output: &crate::process::ProcessOutput,
) -> crate::failure_evidence::FailureProcessLogs {
    crate::failure_evidence::FailureProcessLogs {
        exit_code: output.exit_code.and_then(|code| u32::try_from(code).ok()),
        stdout: crate::failure_evidence::log_tail(&output.stdout),
        // The tail is taken after Podman's echo of the failed step is bounded,
        // so the step's own error lines above it are what the tail keeps.
        stderr: crate::failure_evidence::podman_stderr_tail(&output.stderr),
    }
}

pub(super) fn process_error_diagnostic(error: &ProcessError) -> &'static str {
    match error {
        ProcessError::Io(_) => "subprocess-unavailable",
        ProcessError::Timeout => "deadline-exceeded",
        ProcessError::OutputLimit => "diagnostic-output-limit-exceeded",
        ProcessError::StorageLimit => "declared-storage-limit-exceeded",
        ProcessError::Cancelled => "controller-cancelled",
    }
}

#[cfg(test)]
mod tests;
