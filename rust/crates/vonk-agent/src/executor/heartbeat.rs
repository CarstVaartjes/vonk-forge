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
    match error {
        ClientError::Transport(_) | ClientError::Retryable | ClientError::Protocol => {
            HeartbeatFailure::Retryable
        }
        ClientError::Controller(controller) => {
            if controller.status == 409 && controller.code == vonk_agent_protocol::generated::ControllerErrorCode::SupersededOperationCancelled.as_str() {
                HeartbeatFailure::SupersededCancellation
            } else if !matches!(controller.status, 401 | 403) {
                HeartbeatFailure::Retryable
            } else {
                // Only authenticated denial ends authority observation here.
                // Stale attempts and unusable replies are observed again above.
                HeartbeatFailure::Terminal
            }
        }
        // Unusable authority remains terminal. Result refusals belong to a
        // different boundary and must not be interpreted as a lease renewal.
        ClientError::CredentialRead(_) | ClientError::Identity | ClientError::Pin => {
            HeartbeatFailure::Terminal
        }
        ClientError::ResultSuperseded | ClientError::ResultRejected(_) => {
            HeartbeatFailure::Retryable
        }
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
    // This owning budget is immutable even when the lease advances. A start
    // additionally clips it to its accepted phase deadline.
    let remaining = Duration::from_secs(u64::from(claim.observation_budget_seconds))
        .min(claim_start_deadline(&claim).map_or(Duration::MAX, remaining_lease));
    let observation_end = tokio::time::Instant::now() + remaining;
    let mut cancellation_observed = false;
    let mut delay = schedule.interval;
    loop {
        tokio::select! {
            _ = &mut stop => return Ok(cancellation_observed),
            _ = tokio::time::sleep(delay.min(observation_end.saturating_duration_since(tokio::time::Instant::now()))) => {}
        }
        let progress = AgentProgress {
            fence: claim.fence,
            progress: None,
        };
        let observed = tokio::time::timeout_at(observation_end, client.heartbeat(&progress)).await;
        let observed = observed.unwrap_or(Err(ClientError::Retryable));
        let directive = match observed {
            Ok(directive) if directive.deadline >= deadline || directive.cancel_requested => {
                directive
            }
            Ok(_) => {
                // A stale projection cannot renew authority or revoke it. Ask
                // the owner again within the original observation budget.
                if tokio::time::Instant::now() >= observation_end {
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
                HeartbeatFailure::Retryable => {
                    let now = Utc::now();
                    if tokio::time::Instant::now() >= observation_end {
                        // The immutable attempt budget is spent. Settlement
                        // owns its outcome; renewal cannot extend this budget.
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
