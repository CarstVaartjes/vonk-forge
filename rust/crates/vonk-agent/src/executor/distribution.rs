//! Distribution.

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
            Duration::from_secs(75),
        )
        .await;
        let assignment = match manifest {
            Some(Ok(value)) => value,
            Some(Err(error)) => return distribution_failure_result(&error),
            None if *cancellation.borrow() => {
                return cancelled("controller cancelled distribution");
            }
            None => return temporary_runtime_observation_failure(),
        };
        // Bytes bound transfer time independently of the renewable lease. The
        // Controller also caps redispatch using the durable attempt count.
        let bytes = assignment
            .objects
            .iter()
            .map(|object| object.bytes)
            .sum::<u64>();
        let budget = Duration::from_secs(75 + bytes.div_ceil(1024 * 1024));
        let destination = self.runtime.data_root.join("distribution");
        let download = run_with_authority(
            async {
                for attempt in 0..3_u32 {
                    let current = self
                        .client
                        .download_distribution_with_progress(
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
                        )
                        .await;
                    match current {
                        Err(ref error)
                            if error.retryable()
                                && error.retry_after_seconds().is_none()
                                && attempt < 2 =>
                        {
                            tokio::time::sleep(Duration::from_millis(100 * u64::from(attempt + 1)))
                                .await;
                        }
                        result => return result,
                    }
                }
                unreachable!("the final bounded transfer attempt returns")
            },
            lease_deadline.clone(),
            cancellation.clone(),
            budget,
        )
        .await;
        let evidence = match download {
            Some(Ok(value)) => value,
            Some(Err(ClientError::CredentialRead(_))) => {
                return temporary_runtime_observation_failure();
            }
            Some(Err(error)) => return distribution_failure_result(&error),
            None if *cancellation.borrow() => {
                return cancelled("controller cancelled during distribution");
            }
            None => return temporary_runtime_observation_failure(),
        };
        // Docker pulls are content-addressed and idempotent: reissuing the exact
        // digest reconciles interrupted pulls and reuses already imported layers.
        let pulled = run_with_authority(
            self.pull_runtime_image(
                claim,
                &evidence.oci_image_digest,
                &evidence.oci_image_config_digest,
            ),
            lease_deadline,
            cancellation.clone(),
            Duration::from_secs(3 * 60 * 60),
        )
        .await;
        match pulled {
            Some(Ok(())) => distribution_success(evidence),
            Some(Err(error)) if temporary_observation_error(&error) => {
                temporary_runtime_observation_failure()
            }
            Some(Err(error)) => runtime_failure("runtime image pull was denied", &error),
            None if *cancellation.borrow() => cancelled("controller cancelled during image pull"),
            None => temporary_runtime_observation_failure(),
        }
    }
}

/// Observe the same renewing authority through silent network and helper work.
/// Losing either receiver ends this attempt; dropping the future retains exact
/// partial content for the next bounded, fenced attempt.
pub(super) async fn run_with_authority<T, F>(
    operation: F,
    mut lease: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
    mut cancellation: tokio::sync::watch::Receiver<bool>,
    budget: Duration,
) -> Option<T>
where
    F: Future<Output = T>,
{
    let deadline = tokio::time::Instant::now() + budget;
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
            _ = tokio::time::sleep_until(deadline) => return None,
            _ = tokio::time::sleep(lease_remaining) => {
                if remaining_lease(*lease.borrow()).is_zero() { return None; }
            }
            result = &mut operation => return Some(result),
        }
    }
}

#[cfg(test)]
mod tests;
