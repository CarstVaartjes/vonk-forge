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
            Err(error) => ExecutionResult::unknown(
                WaitReason::CleanupUnconfirmed,
                "build effects are not yet confirmed quiescent",
                UnknownEvidence::at(FailureStage::BoundedBuildProcess).because(format!(
                    "bounded unit reconciliation ended without a complete observation: {error}"
                )),
            ),
        }
    }
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
                    error.to_string(),
                    UnknownEvidence::at(FailureStage::BoundedBuildProcess)
                        .because("bounded exact build cleanup is not yet confirmed"),
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
                // Retain the content-bound export until managed cache cleanup.
                // A lost acceptance response retries upload without rebuilding.
                ExecutionResult::done(evidence)
            }
            Err(error) => {
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
                    UnknownEvidence::at(FailureStage::BoundedBuildProcess).because("bounded build observation ended; retained content and exact units are reconciled on the next request"))
            }
        }
    }
}

fn build_auth_denied(error: &ClientError) -> bool {
    matches!(error, ClientError::Identity | ClientError::Pin)
        || matches!(error, ClientError::Controller(value) if matches!(value.status, 401 | 403))
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
    if build_auth_denied(error) {
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
