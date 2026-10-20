//! A job whose helper answer was lost.
//!
//! A helper restart breaks the agent's in-flight call; a reply can leave the
//! container's removal uncertain; a retained exit account can be unreadable.
//! None of these says what the job did. Its container is read again by its
//! identity before anything stops it: a running job is waited for, an exited
//! one reports the exit Docker recorded, proven absence ends the request with
//! that evidence, and an unread answer is read again. Only a cancellation or
//! the job's own deadline stops it. After the deadline its exit is still read
//! for the job's stop grace; then the job ends failed with what was seen.

use super::*;
use crate::host_runtime::{HostRuntimeError, RuntimeReobservation};

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

/// How long a job is given to cease after it is asked to stop.
pub(super) fn stop_grace(request: &vonk_agent_protocol::RecipeJobRunRequest) -> Duration {
    Duration::from_secs(u64::from(
        request
            .compiled_execution_plan
            .lifecycle
            .stop_timeout_seconds,
    ))
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    /// Feed measured job output activity to the order's existing heartbeat and
    /// stall window while the helper waits. Re-reading a log is no activity.
    pub(super) async fn observe_job_progress(
        &self,
        claim: &AgentClaim,
        request: &vonk_agent_protocol::RecipeJobRunRequest,
        lease: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        mut cancellation: tokio::sync::watch::Receiver<bool>,
    ) {
        let request_root = self.runtime_root.join("runtime-requests");
        let boundary = HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: self.helper_socket,
        };
        let mut last = None;
        loop {
            if remaining_lease(*lease.borrow()).is_zero() {
                return;
            }
            if let Ok(RuntimeReobservation::Running {
                progressed_at: Some(stamp),
            }) = boundary.reobserve_job(request).await
                && last.is_none_or(|previous| stamp > previous)
            {
                self.client.note_process_activity(claim.fence, 1);
                last = Some(stamp);
            }
            // The enclosing execution owns this observer's lifetime. Unread
            // progress does not renew any deadline; the order's stall ends it.
            tokio::select! {
                () = wait_for_cancellation(&mut cancellation) => return,
                () = tokio::time::sleep(MAX_PAUSE) => {}
            }
        }
    }

    /// Re-observe the job until it shows its exit, is proven absent, or its
    /// deadline plus its stop grace passes; a cancellation stops it. `stage`
    /// and `cause` say why its answer was lost; they lead the evidence of a
    /// job that ends without an observed exit.
    pub(super) async fn reobserve_job(
        &self,
        job: &DispatchedJob<'_>,
        mut deadline: Instant,
        cancellation: &mut tokio::sync::watch::Receiver<bool>,
        stage: FailureStage,
        cause: String,
    ) -> Result<HostRuntimeOutcome, Box<ExecutionResult>> {
        // The job's deadline stops it; its stop grace bounds reading the exit.
        let ended = |observed: String| Err(job.unobserved_failure(stage, &cause, &observed));
        let request_root = self.runtime_root.join("runtime-requests");
        let boundary = HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: self.helper_socket,
        };
        let mut pause = FIRST_PAUSE;
        let mut stopped: Option<String> = None;
        loop {
            // A lost cancellation owner is a cancellation, as for the job's
            // own dispatch.
            if *cancellation.borrow() || cancellation.has_changed().is_err() {
                return match self.remove_job_container(job).await {
                    None => Err(Box::new(self.finish_cancelled_job(job))),
                    Some(stop) => Err(job.unknown_while_cancelling(
                        stage,
                        format!("{cause}; job cancellation {stop}"),
                    )),
                };
            }
            let seen = match boundary.reobserve_job(job.request).await {
                Ok(RuntimeReobservation::Exited(outcome)) => {
                    // Its exit is read; the exited container is removed.
                    if let Some(stop) = self.remove_job_container(job).await {
                        eprintln!(
                            "vonk-agent: job {} exited; its container remains: {stop}",
                            job.job_scope
                        );
                    }
                    return Ok(outcome);
                }
                // Proven absence: no exit will ever be read.
                Ok(RuntimeReobservation::Absent) => {
                    return ended(stopped.map_or(
                        "its container is absent; no exit was read".to_owned(),
                        |stop| format!("its container is absent after the deadline's stop; {stop}"),
                    ));
                }
                Ok(RuntimeReobservation::Running { progressed_at }) => {
                    if let Some(stamp) = progressed_at {
                        let window = job_deadline(Instant::now(), job.request)
                            .saturating_duration_since(Instant::now());
                        let idle = Utc::now()
                            .signed_duration_since(stamp)
                            .to_std()
                            .unwrap_or_default();
                        deadline = Instant::now() + window.saturating_sub(idle);
                    }
                    "it was running".to_owned()
                }
                // A helper that cannot re-observe a job: it is stopped, as it
                // was before re-observation existed, and ends with that.
                Err(HostRuntimeError::HelperRejected {
                    code: HelperErrorCode::OperationInvalid,
                    ..
                }) => {
                    let stop = self
                        .stop_job(job.claim, job.stop_plan, job.job_scope)
                        .await
                        .unwrap_or_else(|| "job stopped".to_owned());
                    return ended(format!("the helper cannot re-observe jobs; {stop}"));
                }
                Err(error) => format!("its re-observation was unread: {}", error.cause()),
            };
            let now = Instant::now();
            if now >= deadline {
                // Asked again on every look until it holds; the stop is exact
                // and idempotent.
                stopped = Some(
                    self.stop_job(job.claim, job.stop_plan, job.job_scope)
                        .await
                        .unwrap_or_else(|| "job stopped".to_owned()),
                );
            }
            let remaining = (deadline + stop_grace(job.request)).saturating_duration_since(now);
            if remaining.is_zero() {
                let stop = stopped.unwrap_or_default();
                return ended(format!(
                    "job deadline and stop grace passed while {seen}; {stop}"
                ));
            }
            tokio::select! {
                biased;
                () = wait_for_cancellation(cancellation) => {}
                () = tokio::time::sleep(pause.min(remaining)) => {}
            }
            pause = (pause * 2).min(MAX_PAUSE);
        }
    }

    /// Whether a job with no record of its own ran before, read once by its
    /// identity: `None` only when its container and retained exit are proven absent;
    /// otherwise the stage and cause its exit is read for.
    pub(super) async fn job_seen_before_dispatch(
        &self,
        request: &vonk_agent_protocol::RecipeJobRunRequest,
    ) -> Result<Option<(FailureStage, String)>, HostRuntimeError> {
        let request_root = self.runtime_root.join("runtime-requests");
        let boundary = HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: self.helper_socket,
        };
        let seen = match boundary.reobserve_job(request).await {
            Ok(RuntimeReobservation::Absent) => return Ok(None),
            Ok(RuntimeReobservation::Running { .. }) => "it was running",
            Ok(RuntimeReobservation::Exited(_)) => "it had exited",
            Err(error) => return Err(error),
        };
        Ok(Some((
            FailureStage::JobState,
            format!("job record absent; {seen}"),
        )))
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
