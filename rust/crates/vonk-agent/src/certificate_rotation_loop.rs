//! Level-triggered renewal: the active certificate owns the renewal window.
use super::jittered_backoff;
use std::{future::Future, time::Duration};
use vonk_agent::{
    client::{AgentHttpClient, ClientError},
    config::{AgentConfig, POLL_MAX_SECONDS, POLL_MIN_SECONDS},
    identity::certificate_near_expiry,
    rotation::{RotationError, rotate_if_due},
    systemd_notify,
};

pub(super) async fn run_rotation_lane(config: AgentConfig, client: AgentHttpClient) {
    reconcile(
        || rotate_if_due(&config, &client),
        || {
            certificate_near_expiry(&config.data_dir.join("credentials"), chrono::Utc::now())
                .unwrap_or(true)
        },
        |error, failures| {
            let minimum = Duration::from_secs(POLL_MIN_SECONDS);
            let cap = Duration::from_secs(POLL_MAX_SECONDS);
            match error {
                Some(RotationError::Client(error)) => error.retry_delay(failures, minimum, cap),
                None => ClientError::Retryable.retry_delay(0, cap, cap),
                _ => jittered_backoff(failures, POLL_MIN_SECONDS, POLL_MAX_SECONDS),
            }
            // A zero Retry-After must not turn a continuing reconcile into a busy loop.
            .max(Duration::from_secs(1))
        },
        systemd_notify::renewal_status,
    )
    .await;
}

async fn reconcile<Rotate, Attempt, Urgent, Delay, Status>(
    mut rotate: Rotate,
    mut urgent: Urgent,
    mut delay: Delay,
    mut status: Status,
) where
    Rotate: FnMut() -> Attempt,
    Attempt: Future<Output = Result<bool, RotationError>>,
    Urgent: FnMut() -> bool,
    Delay: FnMut(Option<&RotationError>, u32) -> Duration,
    Status: FnMut(Option<&str>),
{
    let mut failures = 0_u32;
    let mut blocked = None;
    // Randomize the first observation and subsequent polls. This picks a moment
    // after the half-life boundary without persisting a separate renewal schedule.
    tokio::time::sleep(delay(None, 0)).await;
    loop {
        let wait = if let Some(reason) = blocked {
            // Refusal stops effects, not the daemon or its other lanes.
            status(Some(reason));
            delay(None, 0)
        } else {
            // Bound an individual observation, never the standing renewal intent.
            let outcome = tokio::time::timeout(Duration::from_secs(300), rotate())
                .await
                .unwrap_or(Err(RotationError::ObservationEnded));
            match outcome {
                Ok(_) => {
                    failures = 0;
                    status(if urgent() {
                        Some("Certificate has less than one-quarter lifetime remaining")
                    } else {
                        None
                    });
                    delay(None, 0)
                }
                Err(error) => {
                    eprintln!("vonk-agent: certificate renewal failed: {error}");
                    if error.fatal()
                        || matches!(&error, RotationError::Client(e) if !e.retryable() && !matches!(e, ClientError::Identity | ClientError::CredentialRead(_)))
                    {
                        let reason = if matches!(&error, RotationError::Client(e) if matches!(e.status(), Some(401 | 403)))
                        {
                            "Certificate renewal refused; re-enrollment needed"
                        } else {
                            "Certificate renewal blocked; credential or authority repair needed"
                        };
                        blocked = Some(reason);
                        status(Some(reason));
                        delay(None, 0)
                    } else {
                        status(Some(if urgent() {
                            "Certificate renewal failing; less than one-quarter lifetime remaining"
                        } else {
                            "Certificate renewal failing; retrying"
                        }));
                        let wait = delay(Some(&error), failures);
                        failures = failures.saturating_add(1);
                        wait
                    }
                }
            }
        };
        tokio::time::sleep(wait).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        cell::{Cell, RefCell},
        future,
    };
    use vonk_agent::client::{ClientError, ControllerError};

    #[tokio::test(start_paused = true)]
    async fn ca_outage_retries_retry_after_until_recovery_and_clears_health() {
        // Wrong implementation: four attempts or ignoring Retry-After loses recovery.
        let started = tokio::time::Instant::now();
        let recovered = Cell::new(false);
        let statuses = RefCell::new(Vec::new());
        let lane = reconcile(
            || {
                let mut unavailable = ControllerError::from_status(503);
                unavailable.retry_after_seconds = Some(7);
                future::ready(if started.elapsed() < Duration::from_secs(40) {
                    Err(RotationError::Client(ClientError::Controller(Box::new(
                        unavailable,
                    ))))
                } else {
                    recovered.set(true);
                    Ok(true)
                })
            },
            || !recovered.get(),
            |error, _| match error {
                Some(RotationError::Client(error)) => {
                    error.retry_delay(0, Duration::from_secs(1), Duration::from_secs(60))
                }
                _ => Duration::from_secs(1),
            },
            |status| {
                statuses
                    .borrow_mut()
                    .push((started.elapsed(), status.map(str::to_owned)))
            },
        );
        tokio::pin!(lane);
        tokio::select! {
            _ = &mut lane => panic!("renewal lane exited"),
            _ = tokio::time::sleep(Duration::from_secs(50)) => {}
        }
        assert!(recovered.get());
        let statuses = statuses.borrow();
        let first_failure = statuses
            .iter()
            .find(|(_, s)| s.as_deref().is_some_and(|s| s.contains("failing")))
            .unwrap();
        assert!(
            first_failure
                .1
                .as_deref()
                .unwrap()
                .contains("less than one-quarter")
        );
        assert!(statuses.iter().any(
            |(time, s)| *time == first_failure.0 + Duration::from_secs(7) && s == &first_failure.1
        ));
        assert!(statuses.last().unwrap().1.is_none());
        assert!(
            started.elapsed() < Duration::from_secs(100),
            "renewal missed expiry"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn refused_identity_stops_effects_keeps_lane_alive_and_preserves_status() {
        // Wrong implementation: refusal terminates the lane or retries a denied request.
        for code in [401, 403] {
            let refused = Cell::new(false);
            let statuses = RefCell::new(Vec::new());
            let lane = reconcile(
                || {
                    assert!(!refused.replace(true), "refused renewal replayed");
                    future::ready(Err(RotationError::Client(ClientError::Controller(
                        Box::new(ControllerError::from_status(code)),
                    ))))
                },
                || false,
                |_, _| Duration::from_secs(1),
                |status| statuses.borrow_mut().push(status.map(str::to_owned)),
            );
            tokio::pin!(lane);
            tokio::select! {
                _ = &mut lane => panic!("refused renewal killed the daemon lane"),
                _ = tokio::time::sleep(Duration::from_secs(1000)) => {}
            }
            assert!(refused.get());
            assert!(statuses.borrow().iter().all(|s| {
                s.as_deref()
                    .is_some_and(|s| s.contains("re-enrollment needed"))
            }));
        }
    }
}
