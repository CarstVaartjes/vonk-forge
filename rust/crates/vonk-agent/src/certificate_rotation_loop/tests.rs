use super::*;
use std::{
    cell::{Cell, RefCell},
    future,
};
use vonk_agent::client::{ClientError, ControllerError};

#[tokio::test(start_paused = true)]
async fn expired_recovery_waits_for_retry_after_then_recovers() {
    // Wrong implementation: ignoring Retry-After hammers proof recovery;
    // retaining a transient refusal prevents renewal after capacity clears.
    for status in [429, 503] {
        let mut capped = ControllerError::from_status(status);
        capped.retry_after_seconds = Some(600);
        assert_eq!(
            renewal_retry_delay(
                Some(&RotationError::Client(ClientError::Controller(Box::new(
                    capped
                )))),
                0,
            ),
            Duration::from_secs(60),
        );
        let started = tokio::time::Instant::now();
        let recovered = Cell::new(false);
        let lane = reconcile(
            || {
                let mut limited = ControllerError::from_status(status);
                limited.retry_after_seconds = Some(7);
                future::ready(if started.elapsed().is_zero() {
                    Err(RotationError::Client(ClientError::Controller(Box::new(
                        limited,
                    ))))
                } else {
                    assert!(
                        started.elapsed() > Duration::from_secs(7),
                        "recovery remained synchronized at server deadline"
                    );
                    recovered.set(true);
                    Ok(true)
                })
            },
            || Some(vec![1]),
            || true,
            renewal_retry_delay,
            |_| {},
        );
        tokio::pin!(lane);
        tokio::select! {
            _ = &mut lane => panic!("recovery lane exited"),
            _ = tokio::time::sleep(Duration::from_secs(61)) => {}
        }
        assert!(recovered.get(), "recovery stayed blocked after {status}");
    }
}

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
async fn refused_identity_rechecks_without_certificate_change_and_reports_until_success() {
    // Wrong implementation: a terminal latch requires re-enrollment or restart.
    for code in [401, 403] {
        let attempts = Cell::new(0);
        let statuses = RefCell::new(Vec::new());
        let started = tokio::time::Instant::now();
        let lane = reconcile(
            || {
                attempts.set(attempts.get() + 1);
                future::ready(if attempts.get() <= 2 {
                    Err(RotationError::Client(ClientError::Controller(Box::new(
                        ControllerError::from_status(code),
                    ))))
                } else {
                    assert!(started.elapsed() >= Duration::from_secs(75));
                    Ok(true)
                })
            },
            || Some(vec![1]),
            || false,
            |_, _| Duration::from_secs(60),
            |status| statuses.borrow_mut().push(status.map(str::to_owned)),
        );
        tokio::pin!(lane);
        tokio::select! {
            _ = &mut lane => panic!("refused renewal killed the daemon lane"),
            _ = tokio::time::sleep(Duration::from_secs(120)) => {}
        }
        assert!(attempts.get() >= 3);
        let statuses = statuses.borrow();
        assert!(
            statuses
                .iter()
                .filter(|s| s
                    .as_deref()
                    .is_some_and(|s| s.contains("authority refused")))
                .count()
                >= 3
        );
        assert!(statuses.last().unwrap().is_none());
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
                .contains("authority refused")
        );
        std::fs::write(&certificate, b"re-enrolled certificate").unwrap();
        tokio::time::sleep(Duration::from_secs(35)).await;
    };
    tokio::select! {
        _ = &mut lane => panic!("renewal lane exited"),
        _ = reenroll => {}
    }
    assert!(recovered.get());
    assert!(status.borrow().is_none());
    assert!(started.elapsed() < Duration::from_secs(45));
}
