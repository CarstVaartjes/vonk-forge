//! Level-triggered renewal: the active certificate owns the renewal window.
use super::jittered_backoff;
use std::{future::Future, time::Duration};
use vonk_agent::{
    client::{AgentHttpClient, ClientError},
    config::{AgentConfig, POLL_MAX_SECONDS, POLL_MIN_SECONDS},
    identity::{RenewalWindow, active_identity_paths, certificate_near_expiry},
    rotation::{RotationError, rotate_if_due},
    systemd_notify,
};

pub(super) async fn run_rotation_lane(config: AgentConfig, client: AgentHttpClient) {
    let root = config.data_dir.join("credentials");
    let mut window = RenewalWindow::default();
    reconcile(
        || {
            let due = window.due(&root, chrono::Utc::now(), || {
                let bytes = uuid::Uuid::new_v4();
                u16::from_le_bytes([bytes.as_bytes()[0], bytes.as_bytes()[1]])
            });
            let config = &config;
            let client = &client;
            async move {
                if due? {
                    rotate_if_due(config, client).await
                } else {
                    client.observe_active_identity(config).await?;
                    Ok(false)
                }
            }
        },
        || {
            let paths = active_identity_paths(&root).ok()?;
            std::fs::read(paths.certificate).ok()
        },
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

async fn reconcile<Rotate, Attempt, Certificate, Urgent, Delay, Status>(
    mut rotate: Rotate,
    mut certificate: Certificate,
    mut urgent: Urgent,
    mut delay: Delay,
    mut status: Status,
) where
    Rotate: FnMut() -> Attempt,
    Attempt: Future<Output = Result<bool, RotationError>>,
    Certificate: FnMut() -> Option<Vec<u8>>,
    Urgent: FnMut() -> bool,
    Delay: FnMut(Option<&RotationError>, u32) -> Duration,
    Status: FnMut(Option<&str>),
{
    let mut failures = 0_u32;
    let mut blocked = None;
    let mut observed = None;
    loop {
        if let Some(current) = certificate()
            && observed.as_ref() != Some(&current)
        {
            observed = Some(current);
            blocked = None;
            failures = 0;
        }
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
                    if error.fatal() {
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
            || Some(vec![1]),
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
                || Some(vec![1]),
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
    #[tokio::test(start_paused = true)]
    async fn non_security_errors_retry_and_recover() {
        // Wrong implementation: other 4xx or non-retryable TLS errors poison renewal.
        for code in [404, 409, 422, 0] {
            let failed = Cell::new(false);
            let recovered = Cell::new(false);
            let lane = reconcile(
                || {
                    future::ready(if !failed.replace(true) {
                        let error = if code == 0 {
                            // Invalid TLS configuration is a non-retryable transport error.
                            ClientError::Transport(
                                reqwest::Client::builder()
                                    .min_tls_version(reqwest::tls::Version::TLS_1_3)
                                    .max_tls_version(reqwest::tls::Version::TLS_1_2)
                                    .build()
                                    .unwrap_err(),
                            )
                        } else {
                            ClientError::Controller(Box::new(ControllerError::from_status(code)))
                        };
                        assert!(!error.retryable());
                        Err(RotationError::Client(error))
                    } else {
                        recovered.set(true);
                        Ok(true)
                    })
                },
                || Some(vec![1]),
                || false,
                |_, _| Duration::from_secs(1),
                |_| {},
            );
            tokio::pin!(lane);
            tokio::select! {
                _ = &mut lane => panic!("renewal lane exited"),
                _ = tokio::time::sleep(Duration::from_secs(3)) => {}
            }
            assert!(recovered.get(), "renewal stayed blocked after {code}");
        }
    }

    #[tokio::test(start_paused = true)]
    async fn replacing_disk_certificate_clears_refusal_without_restart() {
        // Wrong implementation: re-enrollment needs a daemon restart to clear refusal.
        let directory = tempfile::tempdir().unwrap();
        let certificate = directory.path().join("certificate.pem");
        std::fs::write(&certificate, b"refused certificate").unwrap();
        let started = tokio::time::Instant::now();
        let refused = Cell::new(false);
        let recovered = Cell::new(false);
        let status = RefCell::new(None);
        let lane = reconcile(
            || {
                let replaced = std::fs::read(&certificate).unwrap() == b"re-enrolled certificate";
                future::ready(if replaced {
                    recovered.set(true);
                    Ok(true)
                } else {
                    assert!(!refused.replace(true), "refused request replayed");
                    Err(RotationError::Client(ClientError::Controller(Box::new(
                        ControllerError::from_status(403),
                    ))))
                })
            },
            || std::fs::read(&certificate).ok(),
            || false,
            |_, _| Duration::from_secs(1),
            |value| *status.borrow_mut() = value.map(str::to_owned),
        );
        tokio::pin!(lane);
        let reenroll = async {
            tokio::time::sleep(Duration::from_secs(3)).await;
            assert!(
                status
                    .borrow()
                    .as_deref()
                    .unwrap()
                    .contains("re-enrollment needed")
            );
            std::fs::write(&certificate, b"re-enrolled certificate").unwrap();
            tokio::time::sleep(Duration::from_secs(3)).await;
        };
        tokio::select! {
            _ = &mut lane => panic!("renewal lane exited"),
            _ = reenroll => {}
        }
        assert!(recovered.get());
        assert!(status.borrow().is_none());
        assert!(started.elapsed() < Duration::from_secs(10));
    }
}
