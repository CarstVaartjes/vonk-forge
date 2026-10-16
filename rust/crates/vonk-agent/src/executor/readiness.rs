//! Readiness.

use super::*;

pub fn readiness_identity(spec: &CompiledExecutionPlan) -> (String, String) {
    let image_digest = spec.runtime_image.image_digest.clone();
    let model_identity = spec
        .artifacts
        .first()
        .map(|artifact| {
            format!(
                "{}/{}@{}",
                artifact.model.publisher, artifact.model.slug, artifact.model.content_sha256
            )
        })
        .unwrap_or_default();
    (image_digest, model_identity)
}

pub(super) async fn wait_ready_with_runtime_guard_and_cancellation<R, G>(
    readiness: R,
    runtime_guard: G,
    mut cancellation: tokio::sync::watch::Receiver<bool>,
) -> ReadinessOutcome
where
    R: Future<Output = Result<(), crate::health::HealthError>>,
    G: Future<Output = Result<(), crate::host_runtime::HostRuntimeError>>,
{
    if *cancellation.borrow() {
        return ReadinessOutcome::Cancelled;
    }
    tokio::select! {
        result = readiness => match result {
            Ok(()) => ReadinessOutcome::Ready,
            Err(crate::health::HealthError::Deadline) => ReadinessOutcome::Deadline,
            Err(error) => ReadinessOutcome::Unprobed(error),
        },
        guard = runtime_guard => match guard {
            Ok(()) => ReadinessOutcome::Stalled,
            Err(error) => ReadinessOutcome::GuardFailed(error),
        },
        _ = cancellation.changed() => ReadinessOutcome::Cancelled,
    }
}

/// Why a workload that did not become ready is stopped (its journal marker
/// and the reason), `None` when it is ready, or the result that ends the start
/// without a stop: what the agent could not probe or inspect is not evidence
/// that the workload must be stopped.
pub(super) fn not_ready(
    outcome: ReadinessOutcome,
) -> Result<Option<(&'static str, &'static str)>, Box<ExecutionResult>> {
    Ok(match outcome {
        ReadinessOutcome::Ready => None,
        ReadinessOutcome::Cancelled | ReadinessOutcome::Deadline => Some((
            "readiness_deadline=true",
            "workload did not become ready before its deadline",
        )),
        ReadinessOutcome::Unprobed(crate::health::HealthError::Path) => Some((
            "readiness_path_invalid=true",
            "workload health path is invalid",
        )),
        ReadinessOutcome::Unprobed(error) => {
            // The probe could not run, not the workload: it is left running,
            // named with its cause in the journal, and the start waits; the
            // retry adopts the running workload.
            eprintln!(
                "vonk-agent: workload readiness probe failed: {error} ({:?})",
                std::error::Error::source(&error)
            );
            return Err(Box::new(temporary_preparation_failure(
                FailureStage::Unknown,
                "readiness_probe",
                "workload readiness probe could not run",
            )));
        }
        ReadinessOutcome::Stalled => Some((
            "readiness_stalled=true",
            "workload made no progress for a full stall window before becoming ready",
        )),
        ReadinessOutcome::GuardFailed(error) => {
            // The runtime could not be inspected, so the effect cannot be
            // bound.  Name that instead of reporting a readiness deadline the
            // workload never reached: a temporary inspection failure is the
            // code the Controller already retries for a start, so it becomes
            // self-healing, and any other rejection carries the captured
            // container output the inspection gate admits.
            return Err(Box::new(if temporary_observation_error(&error) {
                temporary_preparation_failure(
                    FailureStage::Unknown,
                    "readiness_inspect",
                    &error.preflight_code(),
                )
            } else {
                runtime_observation_failure(&error)
            }));
        }
    })
}

/// How often a starting workload is inspected while it loads.
pub(super) const LOAD_INSPECTION_INTERVAL: Duration = Duration::from_secs(10);

/// Guard a starting workload until it stops making progress.
///
/// Each inspection proves the exact container still runs and reads its
/// bounded log tail; a tail that changed since the previous inspection is
/// progress (a silent model load or compile still writes its log). The guard
/// ends with `Ok(())` once a full `window` passes without progress, and with
/// the inspection's error -- an exited container carries its own logs -- as
/// soon as one fails. A start that binds an accepted deadline is held to it
/// until its log first grows; from then on only a stall ends it, the same rule
/// the Controller applies to the deadline it extends on reported progress.
///
/// `stall_at` publishes the instant the load currently counts as stalled, so a
/// readiness probe bounded by it never ends progressing work first.
pub(super) async fn guard_load<I, F>(
    window: Duration,
    accepted: Option<tokio::time::Instant>,
    stall_at: &tokio::sync::watch::Sender<DateTime<FixedOffset>>,
    mut inspect: I,
    mut progressed: impl FnMut(),
) -> Result<(), crate::host_runtime::HostRuntimeError>
where
    I: FnMut() -> F,
    F: Future<Output = Result<Option<String>, crate::host_runtime::HostRuntimeError>>,
{
    let mut anchor = tokio::time::Instant::now();
    let mut grown = false;
    let mut last_tail: Option<String> = None;
    loop {
        let stall = match accepted {
            Some(accepted) if !grown => accepted.min(anchor + window),
            _ => anchor + window,
        };
        let now = tokio::time::Instant::now();
        stall_at.send_replace(
            (Utc::now()
                + chrono::Duration::from_std(stall.saturating_duration_since(now))
                    .unwrap_or_default())
            .fixed_offset(),
        );
        if now >= stall {
            return Ok(());
        }
        tokio::time::sleep_until(stall.min(now + LOAD_INSPECTION_INTERVAL)).await;
        if tokio::time::Instant::now() >= stall {
            return Ok(());
        }
        // An inspection that cannot answer before the stall is itself no
        // progress.
        let Ok(observed) = tokio::time::timeout_at(stall, inspect()).await else {
            return Ok(());
        };
        if let Some(tail) = observed? {
            if last_tail.as_ref().is_some_and(|last| *last != tail) {
                anchor = tokio::time::Instant::now();
                grown = true;
                progressed();
            }
            last_tail = Some(tail);
        }
    }
}

pub(super) fn before_phase_deadline(
    lease_deadline: &tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
    start_deadline: Option<&DateTime<FixedOffset>>,
) -> bool {
    Utc::now() < crate::health::phase_deadline(lease_deadline, start_deadline)
}

pub(super) async fn wait_for_launch_stability(
    mut lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
    mut cancellation: tokio::sync::watch::Receiver<bool>,
    start_deadline: Option<DateTime<FixedOffset>>,
    duration: Duration,
) -> bool {
    let stable_at = tokio::time::Instant::now() + duration;
    loop {
        if *cancellation.borrow()
            || !before_phase_deadline(&lease_deadline, start_deadline.as_ref())
        {
            return false;
        }
        let effective = crate::health::phase_deadline(&lease_deadline, start_deadline.as_ref());
        let until_deadline = (effective - Utc::now()).to_std().unwrap_or(Duration::ZERO);
        tokio::select! {
            _ = tokio::time::sleep_until(stable_at) => {
                return before_phase_deadline(&lease_deadline, start_deadline.as_ref())
                    && !*cancellation.borrow();
            }
            _ = tokio::time::sleep(until_deadline) => return false,
            changed = lease_deadline.changed() => {
                if changed.is_err() {
                    return false;
                }
            }
            changed = cancellation.changed() => {
                if changed.is_err() || *cancellation.borrow() {
                    return false;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests;
