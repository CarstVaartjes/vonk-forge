//! Heartbeat.

use super::*;

pub(super) fn phase_progress(phase: ProgressPhase) -> OperationProgress {
    OperationProgress {
        phase: phase.to_string(),
        completed_bytes: 0_u64.into(),
        total_bytes: None,
        total_bytes_known: false,
        completed_items: None,
        total_items: None,
        object_sha256: None,
        kind: None,
        activity: None,
        observed_at: None,
        last_progress_at: None,
        bytes_per_second: None,
        smoothed_bytes_per_second: None,
        eta_seconds: None,
        elapsed_seconds: None,
        checkpoint: None,
        members: Vec::new(),
    }
}

/// Classify one failed renewal against the complete `ClientError` set.
///
/// There is deliberately no catch-all arm: adding an error class is a compile
/// error here until someone decides, in writing, whether it is recoverable.
pub(super) fn classify_heartbeat_failure(error: &ClientError) -> HeartbeatFailure {
    if matches!(error, ClientError::Controller(controller) if controller.code == vonk_agent_protocol::generated::ControllerErrorCode::SupersededOperationCancelled.as_str())
    {
        return HeartbeatFailure::SupersededCancellation;
    }
    // The shared disposition decides; there is no second status table here.
    match error.disposition() {
        Disposition::Security => HeartbeatFailure::Terminal,
        // The Controller refused the renewal's own bytes: the same renewal can
        // never be accepted, so the fence ends with that typed refusal as its
        // evidence instead of being retried silently until the order stalls.
        Disposition::Fail if matches!(error, ClientError::Controller(controller) if controller.request_invalid()) => {
            HeartbeatFailure::Refused
        }
        // A lapsed or not-yet-current fence (the Controller answers 404/409
        // while it settles the attempt), a busy Controller, a credential being
        // replaced, a stalled transfer: re-observe the same fence until the
        // order stalls, without stopping work.
        Disposition::Fail | Disposition::Wait | Disposition::Observe => HeartbeatFailure::Retryable,
    }
}

/// The immutable start deadline a two-phase start bound, if this claim is one.
///
/// The lease is the thing a renewal recovers, so it cannot also be the recovery
/// budget.  A distributed start persists its own budget in the payload the agent
/// executes, and that is the clock the renewal loop retries against.
pub(super) fn claim_start_deadline(claim: &AgentClaim) -> Option<DateTime<FixedOffset>> {
    let vonk_agent_protocol::generated::AgentClaimPayload::RecipeStartPayload(request) =
        &claim.payload
    else {
        return None;
    };
    request
        .start_deadline
        .as_deref()
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
}

pub(super) async fn run_heartbeats<C: LoopClient>(
    client: C,
    mut state: Option<StateStore>,
    claim: AgentClaim,
    lease_deadline: tokio::sync::watch::Sender<DateTime<FixedOffset>>,
    cancellation: tokio::sync::watch::Sender<bool>,
    mut stop: tokio::sync::oneshot::Receiver<()>,
    schedule: HeartbeatSchedule,
) -> Result<bool, LoopError> {
    let mut deadline = claim.deadline;
    // Renewal is re-observed until the order stalls; renewal itself is never
    // progress. A start is held to its accepted deadline until it progresses;
    // from then on only a stall ends it, as the Controller extends that
    // deadline on the progress the start reports.
    let mut stall = StallWindow::new(Duration::from_secs(u64::from(
        claim.observation_budget_seconds,
    )));
    let start_end = claim_start_deadline(&claim)
        .map(|start| tokio::time::Instant::now() + remaining_lease(start));
    let mut observation_end = || {
        stall.observe(client.progress_observation(claim.fence));
        match (start_end, stall.progressed_deadline()) {
            (None, _) => stall.deadline(),
            (Some(start), None) => start.min(stall.deadline()),
            (Some(start), Some(progressed)) => start.max(progressed),
        }
    };
    let mut cancellation_observed = false;
    let mut delay = schedule.interval;
    let mut reported_cause: Option<String> = None;
    loop {
        tokio::select! {
            _ = &mut stop => return Ok(cancellation_observed),
            _ = tokio::time::sleep(delay.min(observation_end().saturating_duration_since(tokio::time::Instant::now()))) => {}
        }
        // Every renewal carries the order's progress, so the Controller's
        // stall window advances on the same bytes, items and phases as ours.
        let progress = AgentProgress {
            fence: claim.fence,
            progress: client.progress_observation(claim.fence),
        };
        let observed =
            tokio::time::timeout_at(observation_end(), client.heartbeat(&progress)).await;
        let observed = observed.unwrap_or(Err(ClientError::Retryable));
        let directive = match observed {
            Ok(directive) if directive.deadline >= deadline || directive.cancel_requested => {
                directive
            }
            Ok(_) => {
                // A stale projection cannot renew authority or revoke it. Ask
                // the owner again until the order stalls.
                if tokio::time::Instant::now() >= observation_end() {
                    cancellation.send_replace(true);
                    return Ok(true);
                }
                delay = schedule.retry_interval;
                continue;
            }
            Err(error) => match classify_heartbeat_failure(&error) {
                HeartbeatFailure::SupersededCancellation => {
                    // The Controller has already invalidated this exact old
                    // command. It is an expected cancellation, not an agent loop
                    // failure; preserve the executor's eventual stop evidence.
                    eprintln!(
                        "vonk-agent: superseded operation cancellation observed for fence {}",
                        claim.fence
                    );
                    cancellation.send_replace(true);
                    return Ok(true);
                }
                HeartbeatFailure::Terminal => return Err(error.into()),
                HeartbeatFailure::Refused => {
                    // The work stops at this fence; the typed refusal (status,
                    // code, request id) is the evidence the loop reports.
                    eprintln!(
                        "vonk-agent: heartbeat refused for fence {}: {error}",
                        claim.fence
                    );
                    cancellation.send_replace(true);
                    return Err(error.into());
                }
                HeartbeatFailure::Retryable => {
                    // The next renewal carries the same, newer progress; the
                    // cause is reported each time its kind changes (a status,
                    // not a per-response request id).
                    let kind = error
                        .status()
                        .map_or_else(|| error.to_string(), |status| format!("HTTP {status}"));
                    if reported_cause.as_ref() != Some(&kind) {
                        eprintln!(
                            "vonk-agent: progress post for fence {} deferred: {error}",
                            claim.fence
                        );
                        reported_cause = Some(kind);
                    }
                    let now = Utc::now();
                    if tokio::time::Instant::now() >= observation_end() {
                        // The order stalled. Settlement owns its outcome;
                        // renewal cannot extend the stall window.
                        cancellation.send_replace(true);
                        return Ok(true);
                    }
                    // Retry promptly while the accepted lease can still be
                    // extended in time, then settle onto the ordinary renewal
                    // cadence: a lapsed lease bounds how often we may ask, not
                    // whether we may ask.  The loop stays alive so a renewal
                    // that lands inside the Controller's allowance still
                    // re-acquires the attempt, and so the agent keeps observing
                    // the Controller's cancellation.
                    delay = if now < deadline.with_timezone(&Utc) {
                        schedule
                            .retry_interval
                            .min(remaining_lease(deadline).saturating_sub(HEARTBEAT_LEASE_MARGIN))
                            .max(HEARTBEAT_RETRY_FLOOR)
                    } else {
                        schedule.interval
                    };
                    continue;
                }
            },
        };
        // The accepted lease advanced, so the ordinary renewal cadence
        // applies again until the next transient failure.
        delay = schedule.interval;
        reported_cause = None;
        // Correlation is validated by the HTTP client before this boundary.
        if directive.cancel_requested {
            cancellation.send_replace(true);
            return Ok(true);
        }
        if let Some(state) = state.as_mut()
            && let Err(error) = state.apply_heartbeat(&progress, &directive)
        {
            eprintln!("vonk-agent: heartbeat journal projection deferred: {error}");
        }
        lease_deadline.send_replace(directive.deadline);
        (schedule.renewed)();
        deadline = directive.deadline;
        cancellation_observed |= directive.cancel_requested;
        if directive.cancel_requested {
            cancellation.send_replace(true);
        }
    }
}

/// Time left before the accepted lease stops authorising a renewal.
pub(super) fn remaining_lease(deadline: DateTime<FixedOffset>) -> Duration {
    (deadline.with_timezone(&Utc) - Utc::now())
        .to_std()
        .unwrap_or(Duration::ZERO)
}

#[cfg(test)]
mod disposition_tests {
    use super::*;

    fn refused(status: u16) -> ClientError {
        ClientError::Controller(Box::new(ControllerError::from_status(status)))
    }

    #[test]
    fn a_definite_renewal_refusal_ends_the_fence_and_a_wait_is_retried() {
        // Wrong implementation: a renewal whose own bytes were refused
        // (400/413/422) was retried silently until the order stalled.
        for status in [400, 413, 422] {
            assert!(
                matches!(
                    classify_heartbeat_failure(&refused(status)),
                    HeartbeatFailure::Refused
                ),
                "HTTP {status}"
            );
        }
        for status in [401, 403] {
            assert!(matches!(
                classify_heartbeat_failure(&refused(status)),
                HeartbeatFailure::Terminal
            ));
        }
        for error in [
            refused(429),
            refused(503),
            // The Controller settles a lapsed fence before it accepts it again.
            refused(404),
            refused(409),
            refused(410),
            ClientError::Retryable,
            ClientError::Identity,
            ClientError::Local(std::io::Error::from(std::io::ErrorKind::StorageFull)),
        ] {
            assert!(matches!(
                classify_heartbeat_failure(&error),
                HeartbeatFailure::Retryable
            ));
        }
        assert!(matches!(
            classify_heartbeat_failure(&ClientError::Pin),
            HeartbeatFailure::Terminal
        ));
    }
}
