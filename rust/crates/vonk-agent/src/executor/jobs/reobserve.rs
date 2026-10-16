//! A job whose helper answer was lost.
//!
//! A helper restart breaks the agent's in-flight call; a reply can leave the
//! container's removal uncertain; a retained exit account can be unreadable.
//! None of these says what the job did. Its container is read again by its
//! identity before anything stops it: a running job is waited for, an exited
//! one reports the exit Docker recorded, proven absence ends the request with
//! that evidence, and an unread answer is read again. Only a cancellation or
//! the job's own deadline stops it.

use super::*;
use crate::host_runtime::{HostRuntimeError, JobReobservation};

/// The first pause between two re-observations; it doubles up to the cap.
pub(super) const FIRST_PAUSE: Duration = Duration::from_millis(100);
pub(super) const MAX_PAUSE: Duration = Duration::from_secs(10);

/// The job's own deadline, counted from `from`.
pub(super) fn job_deadline(
    from: Instant,
    request: &vonk_agent_protocol::RecipeJobRunRequest,
) -> Instant {
    let timeout = request
        .compiled_execution_plan
        .job
        .as_ref()
        .map_or(0, |job| u64::from(job.timeout_seconds));
    from + Duration::from_secs(timeout)
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    /// Re-observe the job until it shows its exit, is proven absent, or its
    /// deadline or a cancellation stops it. `stage` and `cause` say why its
    /// answer was lost; they lead the evidence of an unconfirmed end.
    pub(super) async fn reobserve_job(
        &self,
        job: &DispatchedJob<'_>,
        deadline: Instant,
        cancellation: &mut tokio::sync::watch::Receiver<bool>,
        stage: FailureStage,
        cause: String,
    ) -> Result<HostRuntimeOutcome, Box<ExecutionResult>> {
        let unconfirmed = |after: String| Err(job.unknown(stage, format!("{cause}; {after}")));
        let request_root = self.runtime_root.join("runtime-requests");
        let boundary = HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: self.helper_socket,
        };
        let mut pause = FIRST_PAUSE;
        loop {
            // A lost cancellation owner is a cancellation, as for the job's
            // own dispatch.
            if *cancellation.borrow() || cancellation.has_changed().is_err() {
                return match self.remove_job_container(job).await {
                    None => Err(Box::new(self.finish_cancelled_job(job))),
                    Some(stop) => unconfirmed(format!("job cancellation {stop}")),
                };
            }
            let seen = match boundary.reobserve_job(job.request).await {
                Ok(JobReobservation::Exited(outcome)) => {
                    // Its exit is read; the exited container is removed.
                    if let Some(stop) = self.remove_job_container(job).await {
                        eprintln!(
                            "vonk-agent: job {} exited; its container remains: {stop}",
                            job.job_scope
                        );
                    }
                    return Ok(outcome);
                }
                Ok(JobReobservation::Absent) => {
                    return unconfirmed("its container is absent; no exit was read".to_owned());
                }
                Ok(JobReobservation::Running) => "it was running".to_owned(),
                // A helper that cannot re-observe a job: it is stopped, as it
                // was before re-observation existed.
                Err(HostRuntimeError::HelperRejected {
                    code: HelperErrorCode::OperationInvalid,
                    ..
                }) => {
                    return unconfirmed(
                        self.stop_job(job.claim, job.stop_plan, job.job_scope)
                            .await
                            .unwrap_or_else(|| "job stopped unobserved".to_owned()),
                    );
                }
                Err(error) => format!("its re-observation was unread: {}", error.cause()),
            };
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                let stop = self
                    .stop_job(job.claim, job.stop_plan, job.job_scope)
                    .await
                    .unwrap_or_else(|| "job stopped".to_owned());
                return unconfirmed(format!("job deadline passed while {seen}; {stop}"));
            }
            tokio::select! {
                biased;
                () = wait_for_cancellation(cancellation) => {}
                () = tokio::time::sleep(pause.min(remaining)) => {}
            }
            pause = (pause * 2).min(MAX_PAUSE);
        }
    }

    /// The job's exact Stop; its record is retired by the caller.
    async fn remove_job_container(&self, job: &DispatchedJob<'_>) -> Option<String> {
        self.execute_host_runtime_plan(
            job.claim,
            Vec::new(),
            HostRuntimePlan::Stop(job.stop_plan.clone()),
        )
        .await
        .err()
        .map(|error| format!("stop unconfirmed: {}", error.cause()))
    }
}
