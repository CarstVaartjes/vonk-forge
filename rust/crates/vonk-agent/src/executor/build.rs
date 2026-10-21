//! Build.

use super::*;

pub(super) struct BuildWorkerOwner(std::sync::Arc<std::sync::atomic::AtomicBool>);
impl BuildWorkerOwner {
    pub(super) fn new() -> Self {
        Self(std::sync::Arc::new(std::sync::atomic::AtomicBool::new(
            false,
        )))
    }
    pub(super) fn flag(&self) -> std::sync::Arc<std::sync::atomic::AtomicBool> {
        self.0.clone()
    }
}
impl Drop for BuildWorkerOwner {
    fn drop(&mut self) {
        self.0.store(true, std::sync::atomic::Ordering::Release);
    }
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_build_cleanup(
        &self,
        request: vonk_agent_protocol::RecipeBuildCleanupRequest,
    ) -> ExecutionResult {
        let result = if let Some(runner) = self.runtime.runner.worker() {
            let data_root = self.runtime.data_root.to_path_buf();
            let runtime_root = self.runtime_root.to_path_buf();
            tokio::task::spawn_blocking(move || {
                crate::recipe_builder::cleanup_build_storage(
                    runner.as_ref(),
                    &data_root,
                    &runtime_root,
                    &request,
                )
            })
            .await
            .unwrap_or(Err(crate::recipe_builder::RecipeBuildError::Evidence))
        } else {
            crate::recipe_builder::cleanup_build_storage(
                self.runtime.runner,
                self.runtime.data_root,
                self.runtime_root,
                &request,
            )
        };
        match result {
            Ok(evidence) => ExecutionResult::done(evidence),
            Err(error) => build_cleanup_unconfirmed(&error),
        }
    }
}

/// A build cleanup that could not confirm quiescence reports its cause and
/// subject. A cause waiting cannot end fails with that evidence (the Controller
/// plans the cleanup again at its bounded rate); any other is an unconfirmed
/// effect that names what it waits for.
pub(super) fn build_cleanup_unconfirmed(
    error: &crate::recipe_builder::RecipeBuildError,
) -> ExecutionResult {
    if error.cleanup_cannot_succeed_as_is() {
        return ExecutionResult::Failed(
            error
                .failure_evidence()
                .stage(FailureStage::BoundedBuildProcess)
                .diagnostic(error.cleanup_diagnostic()),
        );
    }
    ExecutionResult::unknown(
        WaitReason::CleanupUnconfirmed,
        error.cleanup_reason(),
        UnknownEvidence::at(FailureStage::BoundedBuildProcess)
            .because(error.cleanup_diagnostic())
            .diagnostics(crate::failure_evidence::from_failure(
                &AgentOperation::RecipeBuildCleanupV1,
                &error.failure_evidence(),
            )),
    )
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_build(
        &self,
        claim: &AgentClaim,
        cancellation: tokio::sync::watch::Receiver<bool>,
        request: Box<vonk_agent_protocol::RecipeBuildRequest>,
    ) -> ExecutionResult {
        let worker_owner = BuildWorkerOwner::new();
        // The build envelope is the Controller's resource limit on untrusted
        // build code. Within it, the order lives while its processes progress.
        let deadline =
            Instant::now() + Duration::from_secs(u64::from(request.limits.timeout_seconds));
        let activity: crate::process::ActivitySink = {
            let client = self.client.clone();
            let fence = claim.fence;
            std::sync::Arc::new(move |amount| client.note_process_activity(fence, amount))
        };
        let builder = RecipeBuilder {
            runner: self.runtime.runner,
            data_root: self.runtime.data_root,
            runtime_root: self.runtime_root,
            egress_binary: Path::new("/usr/lib/vonk-forge/vonk-build-egress"),
        };
        let cached = if let Some(runner) = self.runtime.runner.worker() {
            let data_root = self.runtime.data_root.to_path_buf();
            let runtime_root = self.runtime_root.to_path_buf();
            let request = request.clone();
            let cancellation = cancellation.clone();
            let abandoned = worker_owner.flag();
            let activity = activity.clone();
            tokio::task::spawn_blocking(move || {
                let _activity = crate::process::report_activity_to(activity);
                crate::recipe_builder::cleanup_build_until(
                    runner.as_ref(),
                    &vonk_agent_protocol::RecipeBuildCleanupRequest {
                        build_id: request.build_id,
                        operation_id: request.build_id,
                    },
                    deadline,
                )?;
                Ok(RecipeBuilder {
                    runner: runner.as_ref(),
                    data_root: &data_root,
                    runtime_root: &runtime_root,
                    egress_binary: Path::new("/usr/lib/vonk-forge/vonk-build-egress"),
                }
                .retained_export(&request, request.build_id, deadline, &|| {
                    *cancellation.borrow() || abandoned.load(std::sync::atomic::Ordering::Acquire)
                }))
            })
            .await
            .unwrap_or(Err(crate::recipe_builder::RecipeBuildError::Evidence))
        } else {
            let _activity = crate::process::report_activity_to(activity.clone());
            Ok(
                builder.retained_export(&request, request.build_id, deadline, &|| {
                    *cancellation.borrow()
                }),
            )
        };
        let cached = match cached {
            Ok(value) => value,
            Err(error) => {
                return ExecutionResult::unknown(
                    WaitReason::CleanupUnconfirmed,
                    error.cleanup_reason(),
                    UnknownEvidence::at(FailureStage::BoundedBuildProcess)
                        .because(error.cleanup_diagnostic()),
                );
            }
        };
        let built = if let Some(evidence) = cached {
            Ok(evidence)
        } else {
            self.report_phase(claim, ProgressPhase::Downloading).await;
            let mut archive = None;
            let mut source_error = ClientError::Retryable;
            let mut source_cause =
                "no source bundle attempt ran before cancellation or the build deadline".to_owned();
            for attempt in 0..3_u32 {
                if *cancellation.borrow() || Instant::now() >= deadline {
                    break;
                }
                match tokio::time::timeout(
                    deadline.saturating_duration_since(Instant::now()),
                    self.client.source_bundle(
                        &request.source_bundle_sha256,
                        u64::from(request.source_bundle_bytes),
                    ),
                )
                .await
                {
                    Ok(Ok(bytes)) => {
                        archive = Some(bytes);
                        break;
                    }
                    Ok(Err(error)) => {
                        if !error.retryable() {
                            return build_transfer_outcome(
                                &error,
                                FailureStage::SourceBundleFetch,
                                "source bundle transfer was refused",
                            );
                        }
                        source_cause = error.to_string();
                        source_error = error;
                    }
                    Err(elapsed) => {
                        source_cause = format!("source bundle attempt {attempt}: build {elapsed}");
                        source_error = ClientError::Retryable;
                    }
                }
                if attempt < 2 {
                    tokio::time::sleep(
                        source_error
                            .retry_delay(
                                attempt,
                                Duration::from_millis(100),
                                Duration::from_secs(3),
                            )
                            .min(deadline.saturating_duration_since(Instant::now())),
                    )
                    .await;
                }
            }
            let Some(archive) = archive else {
                return build_transfer_outcome_because(
                    &source_error,
                    FailureStage::SourceBundleFetch,
                    "source bundle observation is unavailable",
                    &source_cause,
                );
            };
            self.report_phase(claim, ProgressPhase::Building).await;
            if let Some(runner) = self.runtime.runner.worker() {
                let data_root = self.runtime.data_root.to_path_buf();
                let runtime_root = self.runtime_root.to_path_buf();
                let request = request.clone();
                let cancellation = cancellation.clone();
                let abandoned = worker_owner.flag();
                let activity = activity.clone();
                // The worker owns its request, bytes, paths and runner. Dropping
                // observation does not detach borrowed state or reset its budget.
                tokio::task::spawn_blocking(move || {
                    let _activity = crate::process::report_activity_to(activity);
                    RecipeBuilder {
                        runner: runner.as_ref(),
                        data_root: &data_root,
                        runtime_root: &runtime_root,
                        egress_binary: Path::new("/usr/lib/vonk-forge/vonk-build-egress"),
                    }
                    .build_until(
                        &request,
                        request.build_id,
                        &archive,
                        deadline,
                        &|| {
                            *cancellation.borrow()
                                || abandoned.load(std::sync::atomic::Ordering::Acquire)
                        },
                    )
                })
                .await
                .unwrap_or(Err(crate::recipe_builder::RecipeBuildError::Evidence))
            } else {
                let _activity = crate::process::report_activity_to(activity.clone());
                builder.build_until(&request, request.build_id, &archive, deadline, &|| {
                    *cancellation.borrow()
                })
            }
        };
        match built {
            Ok(evidence) => {
                self.report_phase(claim, ProgressPhase::Uploading).await;
                let transfer_client = self.client.clone();
                let transfer_fence = claim.fence;
                let total_bytes = evidence.image_bytes;
                // The envelope bounds the untrusted build, not the upload of
                // its result: the upload ends only when its bytes stop moving,
                // and resumes from the Controller's cursor.
                let result = self
                    .client
                    .upload_recipe_image(
                        request.build_id,
                        &evidence.image_digest,
                        &evidence.oci_layout_sha256,
                        evidence.image_bytes,
                        &builder.layout_path(request.build_id),
                        // The order's heartbeat carries these bytes to the
                        // Controller, so its stall window sees the upload move.
                        move |bytes| {
                            transfer_client.set_progress_bytes(transfer_fence, bytes, total_bytes);
                        },
                    )
                    .await;
                if let Err(error) = result {
                    return build_transfer_outcome(
                        &error,
                        FailureStage::ImageUpload,
                        "Controller did not confirm the built OCI image upload",
                    );
                }
                // The Controller holds the image now and the result is journaled,
                // so the export's owner has ended: it is scratch, not a cache. A
                // failed upload keeps it for the retry, until store retention.
                // Retention owns export removal after receipt custody; a crash
                // before finish can still observe the exact uploaded result.
                ExecutionResult::done(evidence)
            }
            Err(error) => build_error_outcome(&error),
        }
    }
}

/// The builder has returned: a nonzero import is a definite attempt result,
/// not a lost observation. Keep its bounded output so the next action follows
/// the cause. Only classified temporary pressure or a deadline warrants retry.
pub(super) fn build_error_outcome(
    error: &crate::recipe_builder::RecipeBuildError,
) -> ExecutionResult {
    use crate::recipe_builder::{PodmanBuildDiagnostic, PodmanImportDiagnostic, RecipeBuildError};

    if let RecipeBuildError::BaseImageImport { diagnostic, .. }
    | RecipeBuildError::BaseImageImportProcess { diagnostic } = error
    {
        let failure = error.failure_evidence();
        return ExecutionResult::Failed(match diagnostic {
            PodmanImportDiagnostic::TemporaryStorageExhausted => failure
                .kind(AgentFailureKind::ResourcePrerequisite)
                .retry_after(Some(30)),
            PodmanImportDiagnostic::DeadlineExceeded => failure
                .kind(AgentFailureKind::TemporaryDependency)
                .retry_after(Some(30)),
            PodmanImportDiagnostic::SubordinateIdMappingUnavailable
            | PodmanImportDiagnostic::PermissionDenied => platform_build_blocked(failure),
            // Unknown output is evidence to inspect, not evidence of a
            // transient fault. Retrying the same verified bytes hides it.
            _ => failure,
        });
    }
    // A returned nonzero command is a definite failure. Do not replace its
    // status and tails with a generic observation-unavailable account.
    if let RecipeBuildError::ImageBuild { diagnostic, .. }
    | RecipeBuildError::AdapterBuild { diagnostic, .. }
    | RecipeBuildError::Command { diagnostic, .. }
    | RecipeBuildError::NetworkBoundary { diagnostic, .. } = error
    {
        let failure = error.failure_evidence();
        return ExecutionResult::Failed(match diagnostic {
            PodmanBuildDiagnostic::TemporaryStorageExhausted => failure
                .kind(AgentFailureKind::ResourcePrerequisite)
                .retry_after(Some(30)),
            PodmanBuildDiagnostic::SubordinateIdMappingUnavailable
            | PodmanBuildDiagnostic::UserNamespaceDenied
            | PodmanBuildDiagnostic::ProcMountDenied
            | PodmanBuildDiagnostic::PermissionDenied => platform_build_blocked(failure),
            _ => failure,
        });
    }
    // Actual source/adapter digest ingress still refuses substitution.
    if matches!(
        error,
        crate::recipe_builder::RecipeBuildError::Source(
            crate::build_source::BuildSourceError::Archive
                | crate::build_source::BuildSourceError::Path
                | crate::build_source::BuildSourceError::Entry
                | crate::build_source::BuildSourceError::Size
                | crate::build_source::BuildSourceError::Digest
        ) | crate::recipe_builder::RecipeBuildError::AdapterInvalid
    ) {
        return ExecutionResult::Failed(error.failure_evidence());
    }
    ExecutionResult::unknown(WaitReason::ObservationUnavailable, error.to_string(),
        UnknownEvidence::at(FailureStage::BoundedBuildProcess)
            .because("bounded build observation ended; retained content and exact units are reconciled on the next request")
            .diagnostics(crate::failure_evidence::from_failure(&AgentOperation::RecipeBuildV1, &error.failure_evidence())))
}

fn platform_build_blocked(mut failure: crate::outcome::Failure) -> crate::outcome::Failure {
    failure.reason = format!("rootless Podman platform is blocked: {}", failure.reason);
    failure.code(vonk_agent_protocol::generated::FailureCode::RecipeBuildPlatformBlocked)
}

fn build_transfer_outcome(
    error: &ClientError,
    stage: FailureStage,
    reason: &'static str,
) -> ExecutionResult {
    build_transfer_outcome_because(error, stage, reason, &error.to_string())
}

/// A transfer that ended keeps its cause; the next attempt resumes from the
/// authoritative content cursor.
fn build_transfer_outcome_because(
    error: &ClientError,
    stage: FailureStage,
    reason: &'static str,
    cause: &str,
) -> ExecutionResult {
    // A definite refusal (authority, 400/422) ends the build with its typed
    // evidence; only a wait or an unconfirmed transfer is observed again.
    if matches!(
        error.disposition(),
        Disposition::Security | Disposition::Fail
    ) {
        return recipe_build_client_failure_result(error, stage, reason);
    }
    ExecutionResult::unknown(
        WaitReason::ObservationUnavailable,
        reason,
        UnknownEvidence::at(stage).because(format!(
            "bounded transfer observation ended: {cause}; the next attempt uses the authoritative content cursor"
        )),
    )
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod import_tests {
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
                PodmanImportDiagnostic::DeadlineExceeded => {
                    Some(AgentFailureKind::TemporaryDependency)
                }
                _ => None,
            };
            assert_eq!(failure.failure_kind, kind);
            assert_eq!(failure.retry_after_seconds, kind.map(|_| 30));
            let wire = ExecutionResult::Failed(failure)
                .finish_for(&vonk_agent_protocol::generated::AgentOperation::RecipeBuildV1);
            let vonk_agent_protocol::generated::AgentResultResult::OutcomeFailed(outcome) =
                wire.result
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
}

#[cfg(test)]
mod command_evidence_tests {
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
                    stderr: crate::failure_evidence::log_tail(
                        b"cannot clone: Operation not permitted",
                    ),
                }),
            };
            let wire = build_error_outcome(&error).finish_for(&AgentOperation::RecipeBuildV1);
            let vonk_agent_protocol::generated::AgentResultResult::OutcomeFailed(outcome) =
                wire.result
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
}
