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
    G: Future<Output = Result<std::convert::Infallible, crate::host_runtime::HostRuntimeError>>,
{
    if *cancellation.borrow() {
        return ReadinessOutcome::Cancelled;
    }
    tokio::select! {
        result = readiness => match result {
            Ok(()) => ReadinessOutcome::Ready,
            Err(_) => ReadinessOutcome::Deadline,
        },
        guard = runtime_guard => match guard {
            Ok(never) => match never {},
            Err(error) => ReadinessOutcome::GuardFailed(error),
        },
        _ = cancellation.changed() => ReadinessOutcome::Cancelled,
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
