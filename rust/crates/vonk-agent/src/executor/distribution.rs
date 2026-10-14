//! Distribution.

use super::installation::attempt_end_cause;
use super::*;

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_distribution(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        if claim.validate().is_err() {
            return failed("artifact distribution claim is invalid");
        }
        let vonk_agent_protocol::generated::AgentClaimPayload::ArtifactDistributionPayload(request) =
            &claim.payload
        else {
            return failed("artifact distribution request is invalid");
        };
        self.report_phase(claim, ProgressPhase::Preparing).await;
        let manifest = run_with_authority(
            self.client.distribution_manifest(&request.plan_digest),
            lease_deadline.clone(),
            cancellation.clone(),
        )
        .await;
        let assignment = match manifest {
            Some(Ok(value)) => value,
            Some(Err(error)) => return distribution_failure_result(&error),
            None if *cancellation.borrow() => {
                return cancelled("controller cancelled distribution");
            }
            None => {
                return temporary_preparation_failure(
                    FailureStage::ArtifactDistribution,
                    "distribution_manifest",
                    attempt_end_cause(&lease_deadline),
                );
            }
        };
        let bytes = assignment
            .objects
            .iter()
            .map(|object| object.bytes)
            .sum::<u64>();
        let destination = self.runtime.data_root.join("distribution");
        let download = run_with_authority(
            self.client.download_distribution_with_progress(
                &request.plan_digest,
                &destination,
                |item| {
                    self.client.set_progress_phase(claim.fence, item.phase);
                    self.client.set_progress_bytes(
                        claim.fence,
                        item.bytes,
                        item.total_bytes.unwrap_or(bytes),
                    );
                },
            ),
            lease_deadline.clone(),
            cancellation.clone(),
        )
        .await;
        let evidence = match download {
            Some(Ok(value)) => value,
            Some(Err(ClientError::CredentialRead(_))) => {
                return temporary_preparation_failure(
                    FailureStage::ArtifactDistribution,
                    "distribution_download",
                    "controller credential is not readable yet",
                );
            }
            Some(Err(error)) => return distribution_failure_result(&error),
            None if *cancellation.borrow() => {
                return cancelled("controller cancelled during distribution");
            }
            None => {
                return temporary_preparation_failure(
                    FailureStage::ArtifactDistribution,
                    "distribution_download",
                    attempt_end_cause(&lease_deadline),
                );
            }
        };
        // Docker pulls are content-addressed and idempotent: reissuing the exact
        // digest reconciles interrupted pulls and reuses already imported layers.
        let pulled = run_with_authority(
            self.pull_runtime_image(
                claim,
                &evidence.oci_image_digest,
                &evidence.oci_image_config_digest,
            ),
            lease_deadline.clone(),
            cancellation.clone(),
        )
        .await;
        match pulled {
            Some(Ok(())) => distribution_success(evidence),
            Some(Err(error)) if temporary_observation_error(&error) => {
                temporary_preparation_failure(
                    FailureStage::ArtifactDistribution,
                    "image_pull",
                    &error.preflight_code(),
                )
            }
            Some(Err(error)) => runtime_failure("runtime image pull was denied", &error),
            // Dropping the helper observer cannot prove an image pull stopped.
            // The next request reconciles the same content-addressed image.
            None if *cancellation.borrow() => ExecutionResult::unknown(
                WaitReason::RuntimeEffectUnconfirmed,
                "superseded distribution effects await observation",
                UnknownEvidence::at(FailureStage::ArtifactDistribution),
            ),
            None => temporary_preparation_failure(
                FailureStage::ArtifactDistribution,
                "image_pull",
                attempt_end_cause(&lease_deadline),
            ),
        }
    }
}

/// Observe the same renewing authority through silent network and helper work.
/// Losing either receiver ends this attempt; dropping the future retains exact
/// partial content for the next fenced attempt.
///
/// There is no budget here: a transfer ends on its own inactivity window, and
/// the order's stall window ends work whose reported progress stops.
pub(super) async fn run_with_authority<T, F>(
    operation: F,
    mut lease: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
    mut cancellation: tokio::sync::watch::Receiver<bool>,
) -> Option<T>
where
    F: Future<Output = T>,
{
    tokio::pin!(operation);
    loop {
        if *cancellation.borrow() || remaining_lease(*lease.borrow()).is_zero() {
            return None;
        }
        let lease_remaining = remaining_lease(*lease.borrow());
        tokio::select! {
            biased;
            () = wait_for_cancellation(&mut cancellation) => return None,
            changed = lease.changed() => {
                if changed.is_err() { return None; }
            }
            _ = tokio::time::sleep(lease_remaining) => {
                if remaining_lease(*lease.borrow()).is_zero() { return None; }
            }
            result = &mut operation => return Some(result),
        }
    }
}

#[cfg(test)]
mod tests;
