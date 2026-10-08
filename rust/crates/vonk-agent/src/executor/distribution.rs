//! Distribution.

use super::*;

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_distribution(&self, claim: &AgentClaim) -> ExecutionResult {
        if claim.validate().is_err() {
            return failed("artifact distribution claim is invalid");
        }
        let vonk_agent_protocol::generated::AgentClaimPayload::ArtifactDistributionPayload(request) =
            &claim.payload
        else {
            return failed("artifact distribution request is invalid");
        };
        if request.validate().is_err() {
            return failed("artifact distribution plan identity is invalid");
        }
        self.report_phase(claim, ProgressPhase::Preparing).await;
        let destination = self.runtime.data_root.join("distribution");
        let (progress_sender, mut progress_receiver) =
            tokio::sync::watch::channel::<Option<DistributionProgress>>(None);
        let progress_client = self.client.clone();
        let progress_claim = claim.clone();
        let mut progress_task = tokio::spawn(async move {
            // Progress is a snapshot, not an event log. Coalesce fast
            // transfer updates instead of accumulating an unbounded queue
            // of heartbeat requests before image import can begin.
            let mut completed_bytes = 0_u64;
            let mut completed_items = 0_u64;
            let mut cadence = tokio::time::interval(Duration::from_secs(1));
            cadence.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            while progress_receiver.changed().await.is_ok() {
                cadence.tick().await;
                let Some(item) = progress_receiver.borrow_and_update().clone() else {
                    continue;
                };
                // Retries rescan durable objects from the beginning. Keep the
                // operation-wide high-water mark while those objects replay.
                progress_client.set_progress_phase(progress_claim.fence, item.phase);
                completed_bytes = completed_bytes.max(item.bytes);
                completed_items = completed_items.max(item.completed_items);
                let progress = AgentProgress {
                    fence: progress_claim.fence,
                    progress: Some(OperationProgress {
                        completed_items: Some(completed_items.into()),
                        total_items: Some(item.total_items.into()),
                        object_sha256: Some(item.object_sha256),
                        kind: Some(item.kind),
                        completed_bytes: completed_bytes.into(),
                        total_bytes: item.total_bytes.map(Into::into),
                        total_bytes_known: item.total_bytes.is_some(),
                        ..phase_progress(item.phase)
                    }),
                };
                let _ = progress_client.heartbeat(&progress).await;
            }
        });
        let download = {
            let mut result = None;
            for attempt in 0..3_u32 {
                let progress_sender = progress_sender.clone();
                let current = self
                    .client
                    .download_distribution_with_progress(
                        &request.plan_digest,
                        &destination,
                        move |item| {
                            progress_sender.send_replace(Some(item));
                        },
                    )
                    .await;
                match current {
                    Ok(value) => {
                        result = Some(Ok(value));
                        break;
                    }
                    Err(error)
                        if error.retryable()
                            && error.retry_after_seconds().is_none()
                            && attempt < 2 =>
                    {
                        tokio::time::sleep(Duration::from_millis(100 * (attempt + 1) as u64)).await;
                    }
                    Err(error) => {
                        result = Some(Err(error));
                        break;
                    }
                }
            }
            result.expect("bounded distribution retry always records a result")
        };
        // The reporter exits only when every sender is dropped. Keep it
        // alive through retries, then close it before waiting; otherwise
        // a finished transfer can wait forever before pulling its image.
        drop(progress_sender);
        if tokio::time::timeout(
            crate::client::HEARTBEAT_REQUEST_TIMEOUT + Duration::from_secs(1),
            &mut progress_task,
        )
        .await
        .is_err()
        {
            progress_task.abort();
        }
        match download {
            Ok(evidence) => {
                // Models are in place; the runtime image comes from the
                // Controller's layered store, pulling only missing layers.
                if let Err(error) = self
                    .pull_runtime_image(
                        claim,
                        &evidence.oci_image_digest,
                        &evidence.oci_image_config_digest,
                    )
                    .await
                {
                    // HostRuntimeError exposes only bounded, stable
                    // categories, never helper stderr or credentials.
                    return ExecutionResult::Failed(
                        Failure::new(format!("runtime image could not be pulled: {error}"))
                            .helper(runtime_helper_code(&error), None),
                    );
                }
                distribution_success(evidence)
            }
            Err(error) => distribution_failure_result(&error),
        }
    }
}
