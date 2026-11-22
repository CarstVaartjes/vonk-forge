use std::{net::IpAddr, time::Duration};

use chrono::{DateTime, FixedOffset, Utc};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum HealthError {
    #[error("workload readiness deadline elapsed")]
    Deadline,
    #[error("workload health path is invalid")]
    Path,
    #[error("workload readiness transport failed")]
    Transport(#[from] reqwest::Error),
}

pub async fn wait_ready(
    address: IpAddr,
    port: u16,
    path: &str,
    lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
) -> Result<(), HealthError> {
    wait_ready_until(address, port, path, lease_deadline, None).await
}

pub(crate) fn phase_deadline(
    lease_deadline: &tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
    start_deadline: Option<&DateTime<FixedOffset>>,
) -> DateTime<Utc> {
    // An operation that binds an immutable start deadline is bounded by that
    // budget.  The attempt lease is renewed by the heartbeat loop and exists to
    // bound takeover latency, not recovery, so a lapse must not end work whose
    // budget is still open: the Controller supersedes through the cancellation
    // directive, and a late renewal re-acquires the exact fence inside this same
    // budget.  Observed live on 2026-09-17, `clock=operation-lease ...
    // elapsed_seconds=81 start_deadline=<an hour away>` ended a start with an
    // hour left, so a model that needs minutes to load could never become ready.
    // An operation that binds no start deadline is bounded by its own work,
    // which is what a stop already is.
    match start_deadline {
        Some(value) => value.with_timezone(&Utc),
        None => lease_deadline.borrow().with_timezone(&Utc),
    }
}

pub async fn wait_ready_until(
    address: IpAddr,
    port: u16,
    path: &str,
    mut lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
    immutable_deadline: Option<DateTime<FixedOffset>>,
) -> Result<(), HealthError> {
    if port < 1024
        || !path.starts_with('/')
        || path.contains("..")
        || path.contains(['?', '#', '\0'])
    {
        return Err(HealthError::Path);
    }
    let client = reqwest::Client::builder()
        .no_proxy()
        .connect_timeout(Duration::from_secs(2))
        .build()?;
    let endpoint = readiness_endpoint(address, port, path);
    loop {
        let deadline = phase_deadline(&lease_deadline, immutable_deadline.as_ref());
        let remaining = (deadline - Utc::now()).to_std().unwrap_or(Duration::ZERO);
        if remaining.is_zero() {
            return Err(HealthError::Deadline);
        }
        if let Ok(Ok(response)) = tokio::time::timeout(remaining, probe(&client, &endpoint)).await
            && response.status().is_success()
            && response
                .content_length()
                .is_none_or(|length| length <= 64 * 1024)
        {
            return Ok(());
        }
        let deadline = phase_deadline(&lease_deadline, immutable_deadline.as_ref());
        let until_deadline = (deadline - Utc::now()).to_std().unwrap_or(Duration::ZERO);
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(1)) => {}
            _ = tokio::time::sleep(until_deadline) => return Err(HealthError::Deadline),
            changed = lease_deadline.changed() => {
                if changed.is_err() {
                    return Err(HealthError::Deadline);
                }
            }
        }
    }
}

/// A cancellable, direct probe of the accepted literal address. It sends no
/// Controller credentials, follows no redirects and retains the existing
/// three-second transport bound. Unlike synchronous Curl it cannot block the
/// observation-page deadline or other ready effect claims.
pub(crate) async fn readiness_once(address: IpAddr, port: u16, path: &str) -> bool {
    if port < 1024
        || !path.starts_with('/')
        || path.contains("..")
        || path.contains(['?', '#', '\0'])
    {
        return false;
    }
    let Ok(client) = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(2))
        .build()
    else {
        return false;
    };
    matches!(probe(&client, &readiness_endpoint(address, port, path)).await,
        Ok(response) if response.status().is_success() && response.content_length().is_none_or(|length| length <= 64 * 1024))
}

/// One readiness probe: a small exchange (status and headers only), so its
/// total is the probe's own bound.
async fn probe(client: &reqwest::Client, endpoint: &str) -> reqwest::Result<reqwest::Response> {
    client
        .get(endpoint)
        .timeout(Duration::from_secs(3))
        .send()
        .await
}

pub(crate) fn readiness_endpoint(address: IpAddr, port: u16, path: &str) -> String {
    match address {
        IpAddr::V4(address) => format!("http://{address}:{port}{path}"),
        IpAddr::V6(address) => format!("http://[{address}]:{port}{path}"),
    }
}

#[cfg(test)]
mod tests;
