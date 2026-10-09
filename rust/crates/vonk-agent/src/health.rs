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
        .timeout(Duration::from_secs(3))
        .build()?;
    let endpoint = readiness_endpoint(address, port, path);
    loop {
        let deadline = phase_deadline(&lease_deadline, immutable_deadline.as_ref());
        let remaining = (deadline - Utc::now()).to_std().unwrap_or(Duration::ZERO);
        if remaining.is_zero() {
            return Err(HealthError::Deadline);
        }
        if let Ok(Ok(response)) =
            tokio::time::timeout(remaining, client.get(&endpoint).send()).await
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
        .timeout(Duration::from_secs(3))
        .build()
    else {
        return false;
    };
    matches!(client.get(readiness_endpoint(address, port, path)).send().await,
        Ok(response) if response.status().is_success() && response.content_length().is_none_or(|length| length <= 64 * 1024))
}

pub(crate) fn readiness_endpoint(address: IpAddr, port: u16, path: &str) -> String {
    match address {
        IpAddr::V4(address) => format!("http://{address}:{port}{path}"),
        IpAddr::V6(address) => format!("http://[{address}]:{port}{path}"),
    }
}

#[cfg(test)]
mod tests {
    use super::{readiness_endpoint, wait_ready, wait_ready_until};
    use chrono::{Duration as ChronoDuration, FixedOffset, Utc};
    use std::{
        io::{Read, Write},
        net::TcpListener,
        thread,
        time::Duration,
    };

    #[test]
    fn readiness_uses_the_exact_published_address() {
        assert_eq!(
            readiness_endpoint("192.168.1.211".parse().unwrap(), 8101, "/v1/models"),
            "http://192.168.1.211:8101/v1/models"
        );
        assert_eq!(
            readiness_endpoint("fd00::10".parse().unwrap(), 8101, "/health"),
            "http://[fd00::10]:8101/health"
        );
    }

    #[tokio::test]
    async fn readiness_honors_a_controller_renewed_deadline() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            while request.windows(4).all(|value| value != b"\r\n\r\n") {
                let size = stream.read(&mut buffer).unwrap();
                assert_ne!(size, 0);
                request.extend_from_slice(&buffer[..size]);
            }
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                .unwrap();
        });
        let original_deadline = (Utc::now() + ChronoDuration::milliseconds(50))
            .with_timezone(&FixedOffset::east_opt(0).unwrap());
        let (deadline_sender, deadline_receiver) = tokio::sync::watch::channel(original_deadline);
        tokio::time::sleep(Duration::from_millis(60)).await;
        deadline_sender.send_replace(
            (Utc::now() + ChronoDuration::seconds(30))
                .with_timezone(&FixedOffset::east_opt(0).unwrap()),
        );

        wait_ready(
            "127.0.0.1".parse().unwrap(),
            port,
            "/health",
            deadline_receiver,
        )
        .await
        .unwrap();
        server.join().unwrap();
    }

    #[tokio::test]
    async fn immutable_start_deadline_caps_a_renewed_lease() {
        let lease = (Utc::now() + ChronoDuration::minutes(5))
            .with_timezone(&FixedOffset::east_opt(0).unwrap());
        let immutable = (Utc::now() - ChronoDuration::milliseconds(1))
            .with_timezone(&FixedOffset::east_opt(0).unwrap());
        let (_sender, receiver) = tokio::sync::watch::channel(lease);

        let started = tokio::time::Instant::now();
        assert!(
            wait_ready_until(
                "127.0.0.1".parse().unwrap(),
                65534,
                "/health",
                receiver,
                Some(immutable)
            )
            .await
            .is_err()
        );
        assert!(started.elapsed() < Duration::from_secs(1));
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let peer = tokio::spawn(async move {
            let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
            let (mut stream, _) = tokio::time::timeout_at(deadline, listener.accept())
                .await
                .unwrap()
                .unwrap();
            let mut bytes = [0; 1024];
            tokio::time::timeout_at(deadline, stream.read(&mut bytes))
                .await
                .unwrap()
                .unwrap();
            tokio::time::timeout_at(
                deadline,
                stream.write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK",
                ),
            )
            .await
            .unwrap()
            .unwrap();
        });
        let fresh = (Utc::now() + ChronoDuration::seconds(3)).fixed_offset();
        let (_sender, receiver) = tokio::sync::watch::channel(fresh);
        wait_ready_until(
            "127.0.0.1".parse().unwrap(),
            port,
            "/health",
            receiver,
            Some(fresh),
        )
        .await
        .unwrap();
        peer.await.unwrap();
    }

    #[tokio::test]
    async fn a_lapsed_lease_does_not_end_a_wait_with_an_open_start_budget() {
        // Wrong implementation this catches: the readiness deadline was
        // `min(lease, start_deadline)`, so one late renewal ended a start whose
        // own budget was still open.  Observed live on 2026-09-17 the Controller
        // recorded `clock=operation-lease ... elapsed_seconds=81
        // start_deadline=<an hour away>` for a `recipe.start` that then failed
        // with "workload did not become ready before its deadline", which no
        // model needing minutes to load can ever pass.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            while request.windows(4).all(|value| value != b"\r\n\r\n") {
                let size = stream.read(&mut buffer).unwrap();
                assert_ne!(size, 0);
                request.extend_from_slice(&buffer[..size]);
            }
            // A frontier model is not ready the instant the container starts.
            thread::sleep(Duration::from_millis(300));
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                .unwrap();
        });
        let lapsed = (Utc::now() - ChronoDuration::seconds(1))
            .with_timezone(&FixedOffset::east_opt(0).unwrap());
        let open_budget = (Utc::now() + ChronoDuration::seconds(30))
            .with_timezone(&FixedOffset::east_opt(0).unwrap());
        let (_sender, receiver) = tokio::sync::watch::channel(lapsed);

        wait_ready_until(
            "127.0.0.1".parse().unwrap(),
            port,
            "/health",
            receiver,
            Some(open_budget),
        )
        .await
        .unwrap();
        server.join().unwrap();
    }
}
