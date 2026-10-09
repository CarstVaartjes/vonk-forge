//! Bounded observations of standing certificate renewal intent.
use super::jittered_backoff;
use std::{future::Future, time::Duration};
use vonk_agent::{
    client::AgentHttpClient,
    config::{AgentConfig, POLL_MAX_SECONDS, POLL_MIN_SECONDS},
    rotation::{RotationError, active_identity_is_valid, rotate_if_due},
    systemd_notify,
};

/// Start only once a usable identity exists.  An expired active certificate
/// is never used for work, but it is not a reason to exit either: renewal is
/// retried idle until it succeeds or the Controller refuses this identity.
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

/// Observe certificate rotation for at most four attempts. Unknown replies
/// retain the durable CSR; standing renewal schedules a fresh bounded attempt.
/// Authentication and verified content failures end immediately.
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
    for failures in 1..=4_u32 {
        let reason = match rotate().await {
            Ok(true) => {
                status("Certificate renewal settled");
                return Ok(true);
            }
            Ok(false) if active_identity_is_valid().unwrap_or(false) => return Ok(false),
            Ok(false) => "no replacement certificate was activated".to_owned(),
            Err(error) if error.fatal() => return Err(error),
            Err(error) => error.to_string(),
        };
        let wait = delay(failures).min(Duration::from_secs(60));
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
        if failures == 4 {
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
    let minimum = POLL_MIN_SECONDS;
    let interval = Duration::from_secs(minimum);
    loop {
        let outcome = rotate_until_settled(
            || rotate_if_due(&config, &client),
            || active_identity_is_valid(&config),
            |failures| jittered_backoff(failures, minimum, POLL_MAX_SECONDS),
        )
        .await;
        if let Err(error) = outcome
            && error.fatal()
        {
            return Err(error);
        }
        // Standing renewal schedules a fresh bounded observation.
        tokio::time::sleep(interval).await;
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
