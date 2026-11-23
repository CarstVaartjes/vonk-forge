//! Level-triggered renewal: the active certificate owns the renewal window.
use super::jittered_backoff;
use std::{future::Future, time::Duration};
use vonk_agent::{
    client::{AgentHttpClient, ClientError, Disposition},
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
        renewal_retry_delay,
        systemd_notify::renewal_status,
    )
    .await;
}

fn renewal_retry_delay(error: Option<&RotationError>, failures: u32) -> Duration {
    let minimum = Duration::from_secs(POLL_MIN_SECONDS);
    let cap = Duration::from_secs(POLL_MAX_SECONDS);
    match error {
        Some(RotationError::Client(error)) => {
            let wait = error.retry_delay(failures, minimum, cap);
            if error.retry_after_seconds().is_some() {
                // Respect the server's minimum, then spread recovering agents
                // over the remaining bounded window using the shared full jitter.
                wait + ClientError::Retryable.retry_delay(0, cap, cap.saturating_sub(wait))
            } else {
                wait
            }
        }
        None => ClientError::Retryable.retry_delay(0, cap, cap),
        _ => jittered_backoff(failures, POLL_MIN_SECONDS, POLL_MAX_SECONDS),
    }
    .max(Duration::from_secs(1))
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
    let mut blocked: Option<String> = None;
    let mut observed = None;
    let mut refused_until = None;
    loop {
        if let Some(current) = certificate()
            && observed.as_ref() != Some(&current)
        {
            observed = Some(current);
            blocked = None;
            failures = 0;
        }
        if refused_until.is_some_and(|deadline| tokio::time::Instant::now() >= deadline) {
            blocked = None;
        }
        let wait = if let (Some(reason), Some(deadline)) = (&blocked, refused_until) {
            // Refusal stops effects, not the daemon or its other lanes.
            status(Some(reason));
            deadline
                .saturating_duration_since(tokio::time::Instant::now())
                .min(Duration::from_secs(30))
        } else {
            // Every request inside one observation is a bounded exchange.
            // Activation waits for the transport's write guard, which is
            // waiting on its holders: requests that end on their own exchange
            // total or transfer inactivity. No total may cut that wait short.
            let outcome = rotate().await;
            match outcome {
                Ok(_) => {
                    failures = 0;
                    refused_until = None;
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
                        let reason = if matches!(&error, RotationError::Client(e) if e.disposition() == Disposition::Security)
                        {
                            "Certificate renewal authority refused; retrying authority check"
                        } else {
                            "Certificate renewal blocked; credential or authority repair needed"
                        };
                        let wait = jittered_backoff(failures, 30, 600);
                        let reason = format!(
                            "{reason}: {error}; next check in {} seconds",
                            wait.as_secs()
                        );
                        status(Some(&reason));
                        blocked = Some(reason);
                        failures = failures.saturating_add(1);
                        refused_until = Some(tokio::time::Instant::now() + wait);
                        wait.min(Duration::from_secs(30))
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
mod tests;
