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
mod tests {
    use super::*;
    use crate::process::ProcessOutput;

    #[test]
    fn absent_or_unretainable_output_is_never_reported_as_unclassified() {
        for bytes in [
            b"".as_slice(),
            b"\r\n\t\0".as_slice(),
            "\u{200b}".as_bytes(),
        ] {
            let output = ProcessOutput {
                exit_code: Some(125),
                success: false,
                stdout: bytes.to_vec(),
                stderr: bytes.to_vec(),
            };
            assert_eq!(
                podman_import_diagnostic(&output),
                PodmanImportDiagnostic::NonzeroWithoutOutput
            );
            assert_eq!(
                podman_build_diagnostic(&output),
                PodmanBuildDiagnostic::NonzeroWithoutOutput
            );
        }
    }

    #[test]
    fn unclassified_commands_keep_exit_status_and_bounded_stderr_in_wire_evidence() {
        let output = ProcessOutput {
            exit_code: Some(125),
            success: false,
            stdout: vec![],
            stderr: format!("{}\nopaque podman failure", "progress\r".repeat(5000)).into_bytes(),
        };
        assert_eq!(
            podman_import_diagnostic(&output),
            PodmanImportDiagnostic::Unknown
        );
        assert_eq!(
            podman_build_diagnostic(&output),
            PodmanBuildDiagnostic::Unknown
        );
        let errors = [
            RecipeBuildError::BaseImageImport {
                diagnostic: podman_import_diagnostic(&output),
                logs: Box::new(sanitized_process_logs(&output)),
            },
            RecipeBuildError::ImageBuild {
                diagnostic: podman_build_diagnostic(&output),
                logs: Box::new(sanitized_process_logs(&output)),
            },
            RecipeBuildError::AdapterBuild {
                diagnostic: podman_build_diagnostic(&output),
                logs: Box::new(sanitized_process_logs(&output)),
            },
            command_failure(FailureStage::ImageVerification, &output),
            super::network_boundary_error(FailureStage::EgressImageImport, &output),
        ];
        for error in errors {
            let body = error.failure_evidence();
            let evidence = crate::failure_evidence::from_failure(
                &vonk_agent_protocol::generated::AgentOperation::RecipeBuildV1,
                &body,
            );
            assert_eq!(evidence.process_exit_code, Some(125));
            assert!(evidence.stderr.text.contains("opaque podman failure"));
            assert!(evidence.stderr.text.len() <= 2048);
            evidence.validate().unwrap();
        }
    }

    #[test]
    fn podman_failure_retains_sanitized_final_ring_output() {
        let output = crate::process::ProcessOutput {
            exit_code: None,
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
                logs: Box::new(super::sanitized_process_logs(&output)),
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
    fn a_long_echoed_step_keeps_the_error_lines_printed_above_it() {
        // Podman closes a failed build with the whole RUN command echoed; a long
        // one used to fill the retained stderr tail by itself.
        let command = format!(
            "RUN apt-get update && {} && python3 -m pip install flash-attn==2.8.3.post1",
            "curl --fail --location https://example.org/archive.tar.gz --output /tmp/a.tgz && "
                .repeat(40)
        );
        let output = ProcessOutput {
            exit_code: Some(1),
            success: false,
            stdout: b"Building wheel for flash-attn (setup.py): finished with status 'error'\n"
                .to_vec(),
            stderr: format!(
                "error: subprocess-exited-with-error\nERROR: Failed to build flash-attn\n\
                 Error: building at STEP \"{command}\": while running runtime: exit status 1\n"
            )
            .into_bytes(),
        };
        assert!(output.stderr.len() > 3 * 1024);
        let error = RecipeBuildError::ImageBuild {
            diagnostic: podman_build_diagnostic(&output),
            logs: Box::new(sanitized_process_logs(&output)),
        };
        let evidence = crate::failure_evidence::from_failure(
            &vonk_agent_protocol::generated::AgentOperation::RecipeBuildV1,
            &error.failure_evidence(),
        );
        assert!(
            evidence
                .stderr
                .text
                .contains("ERROR: Failed to build flash-attn")
        );
        assert!(
            evidence
                .stderr
                .text
                .contains("error: subprocess-exited-with-error")
        );
        assert!(evidence.stderr.text.len() < 1024);
        // The cut echo is reported as dropped, not hidden.
        assert!(evidence.stderr.truncated);
        assert!(evidence.stderr.dropped_bytes.unwrap() > 2 * 1024);
        let property = |name: &str| {
            evidence
                .preflight
                .iter()
                .find(|property| property.name == name)
                .map(|property| property.value.clone())
        };
        assert_eq!(property("exit_code").as_deref(), Some("1"));
        assert!(
            property("build_step")
                .unwrap()
                .starts_with("RUN apt-get update && curl")
        );
        evidence.validate().unwrap();
    }

    /// The retained stderr and properties of a failed Podman build.
    fn build_evidence(stderr: String) -> vonk_agent_protocol::failure_evidence::FailureDiagnostics {
        let output = ProcessOutput {
            exit_code: Some(125),
            success: false,
            stdout: Vec::new(),
            stderr: stderr.into_bytes(),
        };
        let error = RecipeBuildError::ImageBuild {
            diagnostic: podman_build_diagnostic(&output),
            logs: Box::new(sanitized_process_logs(&output)),
        };
        let evidence = crate::failure_evidence::from_failure(
            &vonk_agent_protocol::generated::AgentOperation::RecipeBuildV1,
            &error.failure_evidence(),
        );
        evidence.validate().unwrap();
        evidence
    }

    #[test]
    fn a_long_copy_step_keeps_the_missing_source_its_cause_names() {
        // Buildah names the missing source after the echoed step: the cause
        // runs past further `": ` separators and must survive whole.
        let sources = (0..20)
            .map(|index| format!("patches/fix-{index:02}-attention-backend.patch"))
            .collect::<Vec<_>>()
            .join(" ");
        let evidence = build_evidence(format!(
            "Error: building at STEP \"COPY {sources} /patches/\": checking on sources under \
             \"/var/lib/vonk-forge-agent/builds/context\": copier: stat: \
             \"/patches/fix-07-attention-backend.patch\": no such file or directory\n"
        ));
        assert!(
            evidence.stderr.text.contains(
                "copier: stat: \"/patches/fix-07-attention-backend.patch\": no such file or directory"
            ),
            "{}",
            evidence.stderr.text
        );
        let step = evidence
            .preflight
            .iter()
            .find(|property| property.name == "build_step")
            .unwrap();
        assert!(
            step.value.starts_with("COPY patches/fix-00"),
            "{}",
            step.value
        );
        assert!(!step.value.contains("copier"), "{}", step.value);
    }

    #[test]
    fn a_long_heredoc_step_keeps_the_error_above_it_on_one_line() {
        let script = (0..120)
            .map(|index| format!("echo configuring stage {index}"))
            .collect::<Vec<_>>()
            .join("\n");
        let evidence = build_evidence(format!(
            "make: *** [Makefile:35: pkg.debian.build] Error 2\n\
             Error: building at STEP \"RUN <<EOF\n{script}\nEOF\": while running runtime: exit status 2\n"
        ));
        let text = &evidence.stderr.text;
        assert!(
            text.contains("make: *** [Makefile:35: pkg.debian.build] Error 2"),
            "{text}"
        );
        assert!(
            text.contains("while running runtime: exit status 2"),
            "{text}"
        );
        assert!(!text.contains("configuring stage 119"), "{text}");
        // The echo is one line: the heredoc's lines are not the tail's lines.
        assert_eq!(
            text.lines()
                .filter(|line| line.contains("configuring"))
                .count(),
            1
        );
        assert!(evidence.stderr.truncated);
    }

    #[test]
    fn package_output_and_echoed_steps_cannot_invent_a_host_denial() {
        // Wrong implementation: search the whole build transcript for unrelated
        // AppArmor/permission words instead of Podman's actual failure cause.
        for (stdout, stderr) in [
            (
                "Setting up apparmor ...\nW: download permission denied; continuing\n",
                "curl: (22) The requested URL returned error: 403\nError: building at STEP \"RUN echo newuidmap apparmor permission denied && curl --fail https://example.org/source\": while running runtime: exit status 22",
            ),
            (
                "",
                "Error: building at STEP \"RUN cat <<EOF\nnewuidmap apparmor permission denied\nEOF\": while running runtime: exit status 22",
            ),
            (
                "",
                "application: open /app/data: permission denied\nError: building at STEP \"RUN application\": while running runtime: exit status 1",
            ),
            (
                "Setting up apparmor ...\nIllegal instruction (core dumped)\ndh_install: error: debian/libnccl2.install (executable config) returned exit code 132\n",
                "unrelated optional check denied\nError: building at STEP \"RUN make pkg.debian.build\": while running runtime: exit status 2",
            ),
        ] {
            let output = ProcessOutput {
                exit_code: Some(1),
                success: false,
                stdout: stdout.as_bytes().to_vec(),
                stderr: stderr.as_bytes().to_vec(),
            };
            assert_eq!(
                podman_build_diagnostic(&output),
                PodmanBuildDiagnostic::BuildStepFailed
            );
        }
    }

    #[test]
    fn a_real_runtime_denial_is_not_hidden_by_an_echoed_step_or_recipe_output() {
        for (stderr, expected) in [
            (
                "Error: building at STEP \"RUN echo hello\": cannot clone: Operation not permitted",
                PodmanBuildDiagnostic::UserNamespaceDenied,
            ),
            (
                "crun: open /run/crun/fixture: permission denied\nError: building at STEP \"RUN echo hello\": while running runtime: exit status 1",
                PodmanBuildDiagnostic::PermissionDenied,
            ),
        ] {
            let output = ProcessOutput {
                exit_code: Some(125),
                success: false,
                stdout: b"STEP 1/1: RUN echo apparmor permission denied\n".to_vec(),
                stderr: stderr.as_bytes().to_vec(),
            };
            assert_eq!(podman_build_diagnostic(&output), expected);
        }
    }

    #[test]
    fn podman_build_failures_have_stable_secret_free_diagnostics() {
        for (stdout, stderr, expected) in [
            (b"".as_slice(), b"".as_slice(), PodmanBuildDiagnostic::NonzeroWithoutOutput),
            (
                b"".as_slice(),
                b"write /private/secret: no space left on device".as_slice(),
                PodmanBuildDiagnostic::TemporaryStorageExhausted,
            ),
            (
                b"".as_slice(),
                b"fuse-overlayfs: operation failed for /private/secret".as_slice(),
                PodmanBuildDiagnostic::StorageDriverFailure,
            ),
            (
                b"".as_slice(),
                b"Failed to start transient scope unit".as_slice(),
                PodmanBuildDiagnostic::SystemdScopeFailure,
            ),
            (
                b"".as_slice(),
                b"apparmor=\"DENIED\" operation=\"userns_create\" profile=\"podman\"".as_slice(),
                PodmanBuildDiagnostic::UserNamespaceDenied,
            ),
            (
                b"".as_slice(),
                b"mount `proc` to `proc`: Operation not permitted".as_slice(),
                PodmanBuildDiagnostic::ProcMountDenied,
            ),
            (
                b"".as_slice(),
                b"open /private/secret: permission denied".as_slice(),
                PodmanBuildDiagnostic::PermissionDenied,
            ),
            (
                b"patching file a.py\nHunk #1 FAILED at 628.\n1 out of 1 hunk FAILED -- saving rejects to file a.py.rej\n"
                    .as_slice(),
                b"Error: building at STEP \"RUN patch -p1\": while running runtime: exit status 1"
                    .as_slice(),
                PodmanBuildDiagnostic::PatchRejected,
            ),
            (
                b"Reversed (or previously applied) patch detected!  Assume -R? [n]\nSkipping patch.\n"
                    .as_slice(),
                b"".as_slice(),
                PodmanBuildDiagnostic::PatchRejected,
            ),
            (
                b"".as_slice(),
                b"Error: building at STEP \"RUN make\": while running runtime: exit status 2"
                    .as_slice(),
                PodmanBuildDiagnostic::BuildStepFailed,
            ),
            (
                b"opaque /private/secret".as_slice(),
                b"".as_slice(),
                PodmanBuildDiagnostic::Unknown,
            ),
        ] {
            let classified = podman_build_diagnostic(&ProcessOutput {
                exit_code: None,
                success: false,
                stdout: stdout.to_vec(),
                stderr: stderr.to_vec(),
            });
            assert_eq!(classified, expected);
            // Classification is bounded; the actual command's separately
            // sanitized tails remain mandatory in the error type.
            assert!(!classified.to_string().contains("private"));
            assert!(!classified.to_string().contains("secret"));
        }
    }

    #[test]
    fn podman_import_process_failures_have_stable_secret_free_evidence() {
        for error in [
            ProcessError::StorageLimit,
            ProcessError::Timeout,
            ProcessError::OutputLimit,
            ProcessError::Io(std::io::Error::other("/private/secret")),
        ] {
            let error = podman_import_process_error(error);
            assert!(!format!("{:?}", error.failure_evidence()).contains("private"));
            assert!(!format!("{:?}", error.failure_evidence()).contains("secret"));
        }
    }

    #[test]
    fn import_host_disk_pressure_is_retryable_storage_not_a_plan_violation() {
        use crate::process::{ProcessDiskReserve, ProcessRunner, Program, SystemProcessRunner};

        let root = tempfile::tempdir().unwrap();
        let archive = std::fs::File::create(root.path().join("base-image.tar")).unwrap();
        // Exercise the import's real free-space guard before Podman starts.
        // This is host pressure, not an oversized recipe working directory.
        let error = SystemProcessRunner
            .run_with_input_disk_reserve_cancellable(
                Program::Podman,
                &["load".to_owned()],
                std::time::Duration::from_secs(1),
                &archive,
                ProcessDiskReserve::new(root.path(), u64::MAX),
                &|| false,
            )
            .unwrap_err();
        assert!(matches!(error, ProcessError::StorageLimit));
        assert!(matches!(
            podman_import_process_error(error),
            RecipeBuildError::BaseImageImportProcess {
                diagnostic: PodmanImportDiagnostic::TemporaryStorageExhausted,
                ..
            }
        ));
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
