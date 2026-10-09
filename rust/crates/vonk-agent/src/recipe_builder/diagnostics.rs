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
        logs: Option<Box<crate::failure_evidence::FailureProcessLogs>>,
    },
    #[error("Podman imported base image evidence is invalid")]
    BaseImageInspect,
    #[error("Podman recipe image build failed ({diagnostic})")]
    ImageBuild {
        diagnostic: PodmanBuildDiagnostic,
        logs: Option<Box<crate::failure_evidence::FailureProcessLogs>>,
    },
    #[error("built recipe image evidence is invalid")]
    ImageInspect,
    #[error("platform runtime adapter is invalid")]
    AdapterInvalid,
    #[error("platform runtime adapter build failed ({diagnostic})")]
    AdapterBuild {
        diagnostic: PodmanBuildDiagnostic,
        logs: Option<Box<crate::failure_evidence::FailureProcessLogs>>,
    },
    #[error("adapted runtime image evidence is invalid")]
    AdapterInspect,
    #[error("Podman could not export the built recipe image")]
    ImageExport,
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
}

/// Stable, secret-free evidence extracted from bounded Podman output. Raw
/// subprocess output can contain host paths and registry details, so operation
/// results expose only this reviewed classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PodmanImportDiagnostic {
    TemporaryStorageExhausted,
    DeclaredStorageLimitExceeded,
    SubordinateIdMappingUnavailable,
    ArchiveFormatRejected,
    PermissionDenied,
    DeadlineExceeded,
    DiagnosticOutputLimitExceeded,
    SubprocessUnavailable,
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
            Self::TemporaryStorageExhausted => "temporary-storage-exhausted",
            Self::DeclaredStorageLimitExceeded => "declared-storage-limit-exceeded",
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
            Self::BaseImageImport { diagnostic, logs } => failure
                .stage(FailureStage::BaseImageImport)
                .diagnostic(diagnostic.to_string())
                .process_logs(logs.as_deref().cloned()),
            Self::ImageBuild { diagnostic, logs } => failure
                .stage(FailureStage::ImageBuild)
                .diagnostic(diagnostic.to_string())
                .process_logs(logs.as_deref().cloned()),
            Self::AdapterBuild { diagnostic, logs } => failure
                .stage(FailureStage::RuntimeAdapter)
                .diagnostic(diagnostic.to_string())
                .process_logs(logs.as_deref().cloned()),
            Self::NetworkBoundary {
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

pub(super) fn sanitized_process_logs(
    output: &crate::process::ProcessOutput,
) -> crate::failure_evidence::FailureProcessLogs {
    crate::failure_evidence::FailureProcessLogs {
        stdout: crate::failure_evidence::log_tail(&output.stdout),
        stderr: crate::failure_evidence::log_tail(&output.stderr),
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
mod tests {
    use super::*;
    use crate::process::ProcessOutput;

    #[test]
    fn podman_failure_retains_sanitized_final_ring_output() {
        let output = crate::process::ProcessOutput {
            success: false,
            stdout: Vec::new(),
            stderr: format!(
                "{}\nAuthorization: Bearer never-persist\npermission denied mounting proc\n",
                "noise\n".repeat(5000)
            )
            .into_bytes(),
        };
        let errors = [
            RecipeBuildError::ImageBuild {
                diagnostic: podman_build_diagnostic(&output),
                logs: Some(Box::new(super::sanitized_process_logs(&output))),
            },
            super::network_boundary_error(FailureStage::EgressNetworkCreate, &output),
        ];
        for error in errors {
            let body = error.failure_evidence();
            let diagnostics = crate::failure_evidence::from_failure(
                &vonk_agent_protocol::generated::AgentOperation::RecipeBuildV1,
                &body,
            );
            assert!(diagnostics.stderr.truncated);
            assert!(diagnostics.stderr.text.contains("permission denied"));
            assert!(!format!("{body:?}").contains("never-persist"));
        }
    }

    #[test]
    fn podman_build_failures_have_stable_secret_free_diagnostics() {
        for (stdout, stderr, _diagnostic) in [
            (b"".as_slice(), b"".as_slice(), "nonzero-without-output"),
            (
                b"".as_slice(),
                b"write /private/secret: no space left on device".as_slice(),
                "temporary-storage-exhausted",
            ),
            (
                b"".as_slice(),
                b"fuse-overlayfs: operation failed for /private/secret".as_slice(),
                "storage-driver-failure",
            ),
            (
                b"".as_slice(),
                b"Failed to start transient scope unit".as_slice(),
                "systemd-scope-failure",
            ),
            (
                b"".as_slice(),
                b"apparmor=\"DENIED\" operation=\"userns_create\" profile=\"podman\"".as_slice(),
                "user-namespace-denied",
            ),
            (
                b"".as_slice(),
                b"mount `proc` to `proc`: Operation not permitted".as_slice(),
                "proc-mount-denied",
            ),
            (
                b"".as_slice(),
                b"open /private/secret: permission denied".as_slice(),
                "permission-denied",
            ),
            (
                b"patching file a.py\nHunk #1 FAILED at 628.\n1 out of 1 hunk FAILED -- saving rejects to file a.py.rej\n"
                    .as_slice(),
                b"Error: building at STEP \"RUN patch -p1\": while running runtime: exit status 1"
                    .as_slice(),
                "patch-rejected",
            ),
            (
                b"Reversed (or previously applied) patch detected!  Assume -R? [n]\nSkipping patch.\n"
                    .as_slice(),
                b"".as_slice(),
                "patch-rejected",
            ),
            (
                b"".as_slice(),
                b"Error: building at STEP \"RUN make\": while running runtime: exit status 2"
                    .as_slice(),
                "build-step-failed",
            ),
            (
                b"opaque /private/secret".as_slice(),
                b"".as_slice(),
                "unclassified-podman-build-failure",
            ),
        ] {
            let classified = podman_build_diagnostic(&ProcessOutput {
                success: false,
                stdout: stdout.to_vec(),
                stderr: stderr.to_vec(),
            });
            let error = RecipeBuildError::ImageBuild {
                diagnostic: classified,
                logs: None,
            };
            assert!(!format!("{:?}", error.failure_evidence()).contains("private"));
            assert!(!format!("{:?}", error.failure_evidence()).contains("secret"));
        }
    }

    #[test]
    fn podman_import_process_failures_have_stable_secret_free_evidence() {
        for (error, _diagnostic) in [
            (
                ProcessError::StorageLimit,
                "declared-storage-limit-exceeded",
            ),
            (ProcessError::Timeout, "deadline-exceeded"),
            (
                ProcessError::OutputLimit,
                "diagnostic-output-limit-exceeded",
            ),
            (
                ProcessError::Io(std::io::Error::other("/private/secret")),
                "subprocess-unavailable",
            ),
        ] {
            let error = podman_import_process_error(error);
            assert!(!format!("{:?}", error.failure_evidence()).contains("private"));
            assert!(!format!("{:?}", error.failure_evidence()).contains("secret"));
        }
    }

    #[test]
    fn bounded_build_process_failures_have_stable_secret_free_evidence() {
        for (error, _diagnostic) in [
            (
                ProcessError::StorageLimit,
                "declared-storage-limit-exceeded",
            ),
            (ProcessError::Timeout, "deadline-exceeded"),
            (
                ProcessError::OutputLimit,
                "diagnostic-output-limit-exceeded",
            ),
            (
                ProcessError::Io(std::io::Error::other("/private/secret")),
                "subprocess-unavailable",
            ),
            (ProcessError::Cancelled, "controller-cancelled"),
        ] {
            let evidence = RecipeBuildError::Process(error).failure_evidence();
            assert!(!format!("{evidence:?}").contains("private"));
            assert!(!format!("{evidence:?}").contains("secret"));
        }
    }
}
