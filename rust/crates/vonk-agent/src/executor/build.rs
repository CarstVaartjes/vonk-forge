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
            Err(_) => ExecutionResult::unknown(
                WaitReason::CleanupUnconfirmed,
                "build effects are not yet confirmed quiescent",
                UnknownEvidence::at(FailureStage::BoundedBuildProcess)
                    .because("bounded unit reconciliation ended without a complete observation"),
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
        let deadline = Instant::now()
            + Duration::from_secs(
                u64::from(claim.observation_budget_seconds)
                    .min(u64::from(request.limits.timeout_seconds)),
            );
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
            tokio::task::spawn_blocking(move || {
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
            let mut source_error = ClientError::Unknown(
                vonk_agent_protocol::generated::TransientReason::LocalStateUnavailable,
            );
            for attempt in 0..3 {
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
                        if build_auth_denied(&error) {
                            return build_transfer_outcome(
                                &error,
                                FailureStage::SourceBundleFetch,
                                "source authority denied the transfer",
                            );
                        }
                        source_error = error;
                    }
                    Err(_) => {}
                }
                if attempt < 2 {
                    tokio::time::sleep(
                        Duration::from_millis(50 * (attempt + 1))
                            .min(deadline.saturating_duration_since(Instant::now())),
                    )
                    .await;
                }
            }
            let Some(archive) = archive else {
                return build_transfer_outcome(
                    &source_error,
                    FailureStage::SourceBundleFetch,
                    "source bundle observation is unavailable",
                );
            };
            self.report_phase(claim, ProgressPhase::Building).await;
            if let Some(runner) = self.runtime.runner.worker() {
                let data_root = self.runtime.data_root.to_path_buf();
                let runtime_root = self.runtime_root.to_path_buf();
                let request = request.clone();
                let cancellation = cancellation.clone();
                let abandoned = worker_owner.flag();
                // The worker owns its request, bytes, paths and runner. Dropping
                // observation does not detach borrowed state or reset its budget.
                tokio::task::spawn_blocking(move || {
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
                builder.build_until(&request, request.build_id, &archive, deadline, &|| {
                    *cancellation.borrow()
                })
            }
        };
        match built {
            Ok(evidence) => {
                self.report_phase(claim, ProgressPhase::Uploading).await;
                let (sender, mut receiver) = tokio::sync::watch::channel(0_u64);
                let progress_client = self.client.clone();
                let progress_claim = claim.clone();
                let total_bytes = evidence.image_bytes;
                let mut progress_task =
                    tokio_util::task::AbortOnDropHandle::new(tokio::spawn(async move {
                        let mut completed_bytes = 0;
                        let mut cadence = tokio::time::interval(Duration::from_secs(1));
                        cadence.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
                        let deadline = tokio::time::Instant::from_std(deadline);
                        while matches!(
                            tokio::time::timeout_at(deadline, receiver.changed()).await,
                            Ok(Ok(()))
                        ) {
                            if tokio::time::timeout_at(deadline, cadence.tick())
                                .await
                                .is_err()
                            {
                                break;
                            }
                            completed_bytes = completed_bytes.max(*receiver.borrow_and_update());
                            let progress = AgentProgress {
                                fence: progress_claim.fence,
                                progress: Some(OperationProgress {
                                    completed_bytes: completed_bytes.into(),
                                    total_bytes: Some(total_bytes.into()),
                                    total_bytes_known: true,
                                    ..phase_progress(ProgressPhase::Uploading)
                                }),
                            };
                            let _ = tokio::time::timeout_at(
                                deadline,
                                progress_client.heartbeat(&progress),
                            )
                            .await;
                        }
                    }));
                let transfer_client = self.client.clone();
                let transfer_fence = claim.fence;
                let result = tokio::time::timeout(
                    deadline.saturating_duration_since(Instant::now()),
                    self.client.upload_recipe_image(
                        request.build_id,
                        &evidence.image_digest,
                        &evidence.oci_layout_sha256,
                        evidence.image_bytes,
                        &builder.layout_path(request.build_id),
                        move |bytes| {
                            transfer_client.set_progress_bytes(transfer_fence, bytes, total_bytes);
                            sender.send_replace(bytes);
                        },
                    ),
                )
                .await
                .unwrap_or(Err(ClientError::Unknown(
                    vonk_agent_protocol::generated::TransientReason::LocalStateUnavailable,
                )));
                // The transfer owns the sender; finishing closes the channel.
                // The reporter drains its last snapshot independently of transfer IO.
                if tokio::time::timeout(
                    crate::client::HEARTBEAT_REQUEST_TIMEOUT + Duration::from_secs(1),
                    &mut progress_task,
                )
                .await
                .is_err()
                {
                    progress_task.abort();
                    let _ = progress_task.await;
                }
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
    error.refused()
}

fn build_transfer_outcome(
    error: &ClientError,
    stage: FailureStage,
    reason: &'static str,
) -> ExecutionResult {
    if build_auth_denied(error) {
        return recipe_build_client_failure_result(error, stage, reason);
    }
    ExecutionResult::unknown(WaitReason::ObservationUnavailable, reason,
        UnknownEvidence::at(stage).because("bounded transfer observation ended; the next attempt uses the authoritative content cursor"))
}
