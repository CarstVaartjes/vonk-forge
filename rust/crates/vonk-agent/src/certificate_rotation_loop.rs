//! Bounded observations of standing certificate renewal intent.
use super::jittered_backoff;
#[cfg(test)]
use std::future::Future;
use std::time::Duration;
use vonk_agent::{
    client::AgentHttpClient,
    config::{AgentConfig, POLL_MAX_SECONDS, POLL_MIN_SECONDS},
    rotation::{RotationError, rotate_if_due},
    systemd_notify,
};

/// Start only once a usable identity exists.  An expired active certificate
/// is never used for work, but it is not a reason to exit either: renewal is
/// retried idle until it succeeds or the Controller refuses this identity.
#[cfg(test)]
pub(super) async fn ensure_startup_identity<IdentityCheck, Rotate, RotateFuture, Delay>(
    mut active_identity_is_valid: IdentityCheck,
    rotate: Rotate,
    delay: Delay,
) -> Result<(), RotationError>
where
    IdentityCheck: FnMut() -> Result<bool, RotationError>,
    Rotate: FnMut() -> RotateFuture,
    RotateFuture: Future<Output = Result<bool, RotationError>>,
    Delay: FnMut(u32) -> Duration,
{
    if active_identity_is_valid().unwrap_or(false) {
        return Ok(());
    }
    rotate_until_settled(rotate, active_identity_is_valid, delay)
        .await
        .map(|_| ())
}

/// Observe certificate rotation for at most four attempts within 300 seconds.
/// Unknown replies retain the durable CSR; standing renewal schedules a fresh bounded attempt.
/// Authentication and verified content failures end immediately.
#[cfg(test)]
pub(super) async fn rotate_until_settled<Rotate, RotateFuture, IdentityCheck, Delay>(
    rotate: Rotate,
    active_identity_is_valid: IdentityCheck,
    delay: Delay,
) -> Result<bool, RotationError>
where
    Rotate: FnMut() -> RotateFuture,
    RotateFuture: Future<Output = Result<bool, RotationError>>,
    IdentityCheck: FnMut() -> Result<bool, RotationError>,
    Delay: FnMut(u32) -> Duration,
{
    rotate_until_settled_with_status(
        rotate,
        active_identity_is_valid,
        delay,
        systemd_notify::progress,
    )
    .await
}

#[cfg(test)]
async fn rotate_until_settled_with_status<Rotate, RotateFuture, IdentityCheck, Delay, Status>(
    mut rotate: Rotate,
    mut active_identity_is_valid: IdentityCheck,
    mut delay: Delay,
    mut status: Status,
) -> Result<bool, RotationError>
where
    Rotate: FnMut() -> RotateFuture,
    RotateFuture: Future<Output = Result<bool, RotationError>>,
    IdentityCheck: FnMut() -> Result<bool, RotationError>,
    Delay: FnMut(u32) -> Duration,
    Status: FnMut(&str),
{
    let deadline = tokio::time::Instant::now() + Duration::from_secs(300);
    for failures in 1..=4_u32 {
        let outcome = tokio::time::timeout_at(deadline, rotate())
            .await
            .map_err(|_| RotationError::ObservationEnded)?;
        let retry_after = match &outcome {
            Err(RotationError::Client(error)) => error.retry_after_seconds().unwrap_or(0),
            _ => 0,
        };
        let reason = match outcome {
            Ok(true) => {
                status("Certificate renewal settled");
                return Ok(true);
            }
            Ok(false) if active_identity_is_valid().unwrap_or(false) => return Ok(false),
            Ok(false) => "no replacement certificate was activated".to_owned(),
            Err(error) if error.fatal() => return Err(error),
            Err(error) => error.to_string(),
        };
        let wait = delay(failures)
            .min(Duration::from_secs(60))
            .saturating_add(Duration::from_secs(u64::from(retry_after)))
            .min(deadline.saturating_duration_since(tokio::time::Instant::now()));
        if active_identity_is_valid().unwrap_or(false) {
            status("Degraded: certificate renewal unavailable before expiry");
            eprintln!(
                "vonk-agent: certificate renewal unavailable ({reason}); retrying in {} seconds while the active certificate remains valid",
                wait.as_secs()
            );
        } else {
            eprintln!(
                "vonk-agent: active certificate has expired and is not used; renewal unavailable ({reason}); retrying in {} seconds",
                wait.as_secs()
            );
            status("Degraded: active certificate unavailable; renewal observation pending");
        }
        if failures == 4 || tokio::time::Instant::now() >= deadline {
            return Err(RotationError::ObservationEnded);
        }
        tokio::time::sleep(wait).await;
    }
    Err(RotationError::ObservationEnded)
}

pub(super) async fn run_rotation_lane(
    config: AgentConfig,
    client: AgentHttpClient,
) -> Result<(), RotationError> {
    let mut failures = 0_u32;
    loop {
        // This lane is the sole renewal retry owner. Each observation ends in
        // 300 seconds; the next one uses the same durable CSR and authority.
        let outcome =
            tokio::time::timeout(Duration::from_secs(300), rotate_if_due(&config, &client))
                .await
                .unwrap_or(Err(RotationError::ObservationEnded));
        let root = config.data_dir.join("credentials");
        let _ = vonk_agent::identity::record_renewal_health(&root, outcome.is_err());
        let wait = match outcome {
            Err(error) if error.fatal() => {
                systemd_notify::progress("Degraded: credential authority refused");
                return Err(error);
            }
            Err(error) => {
                failures = failures.saturating_add(1);
                systemd_notify::progress("Degraded: certificate renewal observation unavailable");
                eprintln!("vonk-agent: certificate renewal observation ended: {error}");
                match error {
                    RotationError::Client(error) => error.retry_delay(failures),
                    _ => jittered_backoff(failures, POLL_MIN_SECONDS, POLL_MAX_SECONDS),
                }
            }
            Ok(renewed) => {
                if renewed {
                    systemd_notify::progress("Certificate renewal settled");
                }
                failures = 0;
                Duration::from_secs(POLL_MIN_SECONDS)
            }
        };
        if vonk_agent::identity::renewal_health(&root, chrono::Utc::now())
            .is_some_and(|(_, remaining)| remaining < 0.25)
        {
            systemd_notify::progress(
                "Degraded: certificate has less than one quarter lifetime remaining",
            );
        }
        // Do not shorten the Controller's minimum across observation batches.
        // Every wait has the bounded RetryInfo/backoff deadline above.
        tokio::time::sleep(wait).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::future;

    #[tokio::test]
    async fn renewal_failure_alerts_before_expiry_and_clears_after_recovery() {
        let mut attempts = 0;
        let mut statuses = Vec::new();
        let renewed = rotate_until_settled_with_status(
            || {
                attempts += 1;
                future::ready(if attempts == 1 {
                    Err(RotationError::Identity(
                        vonk_agent::identity::IdentityError::Io(std::io::Error::other(
                            "credential storage unavailable",
                        )),
                    ))
                } else {
                    Ok(true)
                })
            },
            || Ok(true),
            |_| Duration::ZERO,
            |status| statuses.push(status.to_owned()),
        )
        .await
        .unwrap();
        assert!(renewed);
        assert_eq!(attempts, 2);
        assert_eq!(statuses.len(), 2);
        assert_ne!(statuses[0], statuses[1]);
        // A completed observation leaves no gate for the next due rotation.
        assert!(
            rotate_until_settled(|| future::ready(Ok(true)), || Ok(true), |_| Duration::ZERO)
                .await
                .unwrap()
        );
    }

    #[tokio::test]
    async fn storage_observation_ends_boundedly_and_accepts_the_next_request() {
        let mut attempts = 0;
        let result = rotate_until_settled(
            || {
                attempts += 1;
                future::ready(Err(RotationError::Identity(
                    vonk_agent::identity::IdentityError::Io(std::io::Error::other(
                        "credential storage unavailable",
                    )),
                )))
            },
            || {
                Err(RotationError::Identity(
                    vonk_agent::identity::IdentityError::Node,
                ))
            },
            |_| Duration::ZERO,
        )
        .await;
        assert!(result.is_err());
        assert_eq!(attempts, 4);
        assert!(
            rotate_until_settled(|| future::ready(Ok(true)), || Ok(true), |_| Duration::ZERO)
                .await
                .unwrap()
        );
    }
}
