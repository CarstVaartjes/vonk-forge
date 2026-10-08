//! Build.

use super::*;

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_build_cleanup(
        &self,
        request: vonk_agent_protocol::RecipeBuildCleanupRequest,
    ) -> ExecutionResult {
        match crate::recipe_builder::cleanup_build(self.runtime.runner, &request) {
            Ok(evidence) => ExecutionResult::done(evidence),
            Err(_) => failed("recipe build cleanup could not confirm the service stopped"),
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
        self.report_phase(claim, ProgressPhase::Downloading).await;
        let archive = match self
            .client
            .source_bundle(
                &request.source_bundle_sha256,
                u64::from(request.source_bundle_bytes),
            )
            .await
        {
            Ok(archive) => archive,
            Err(error) => {
                return recipe_build_client_failure_result(
                    &error,
                    FailureStage::SourceBundleFetch,
                    "authorized source bundle could not be fetched",
                );
            }
        };
        let builder = RecipeBuilder {
            runner: self.runtime.runner,
            data_root: self.runtime.data_root,
            runtime_root: self.runtime_root,
            egress_binary: Path::new("/usr/lib/vonk-forge/vonk-build-egress"),
        };
        self.report_phase(claim, ProgressPhase::Building).await;
        let cancelled = || *cancellation.borrow();
        match builder.build_cancellable(&request, request.build_id, &archive, &cancelled) {
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
                        while receiver.changed().await.is_ok() {
                            cadence.tick().await;
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
                            let _ = progress_client.heartbeat(&progress).await;
                        }
                    }));
                let transfer_client = self.client.clone();
                let transfer_fence = claim.fence;
                let result = self
                    .client
                    .upload_recipe_image(
                        request.build_id,
                        &evidence.image_digest,
                        &evidence.oci_layout_sha256,
                        evidence.image_bytes,
                        &builder.layout_path(request.build_id),
                        move |bytes| {
                            transfer_client.set_progress_bytes(transfer_fence, bytes, total_bytes);
                            sender.send_replace(bytes);
                        },
                    )
                    .await;
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
                    return recipe_build_client_failure_result(
                        &error,
                        FailureStage::ImageUpload,
                        "Controller did not confirm the built OCI image upload",
                    );
                }
                // The Controller now holds these exact bytes and
                // converts them into its layered store; the local
                // archive is no longer needed.
                let _ = std::fs::remove_file(builder.layout_path(request.build_id));
                ExecutionResult::done(evidence)
            }
            Err(error) => ExecutionResult::Failed(error.failure_evidence()),
        }
    }
}
