#![cfg(test)]
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
        readiness_endpoint("10.0.0.211".parse().unwrap(), 8101, "/v1/models"),
        "http://10.0.0.211:8101/v1/models"
    );
    assert_eq!(
        readiness_endpoint("fd00::10".parse().unwrap(), 8101, "/health"),
        "http://[fd00::10]:8101/health"
    );
}

#[tokio::test]
async fn readiness_honors_a_controller_renewed_deadline() {
    let listener = TcpListener::bind(crate::client::test_support::FIXTURE_BIND).unwrap();
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
        crate::client::test_support::FIXTURE_HOST.parse().unwrap(),
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
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let lease =
        (Utc::now() + ChronoDuration::minutes(5)).with_timezone(&FixedOffset::east_opt(0).unwrap());
    let immutable = (Utc::now() - ChronoDuration::milliseconds(1))
        .with_timezone(&FixedOffset::east_opt(0).unwrap());
    let (_sender, receiver) = tokio::sync::watch::channel(lease);

    let started = tokio::time::Instant::now();
    assert!(
        wait_ready_until(
            crate::client::test_support::FIXTURE_HOST.parse().unwrap(),
            65534,
            "/health",
            receiver,
            Some(immutable)
        )
        .await
        .is_err()
    );
    assert!(started.elapsed() < Duration::from_secs(1));
    let listener = tokio::net::TcpListener::bind(crate::client::test_support::FIXTURE_BIND)
        .await
        .unwrap();
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
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"),
        )
        .await
        .unwrap()
        .unwrap();
    });
    let fresh = (Utc::now() + ChronoDuration::seconds(3)).fixed_offset();
    let (_sender, receiver) = tokio::sync::watch::channel(fresh);
    wait_ready_until(
        crate::client::test_support::FIXTURE_HOST.parse().unwrap(),
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
    let listener = TcpListener::bind(crate::client::test_support::FIXTURE_BIND).unwrap();
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
    let lapsed =
        (Utc::now() - ChronoDuration::seconds(1)).with_timezone(&FixedOffset::east_opt(0).unwrap());
    let open_budget = (Utc::now() + ChronoDuration::seconds(30))
        .with_timezone(&FixedOffset::east_opt(0).unwrap());
    let (_sender, receiver) = tokio::sync::watch::channel(lapsed);

    wait_ready_until(
        crate::client::test_support::FIXTURE_HOST.parse().unwrap(),
        port,
        "/health",
        receiver,
        Some(open_budget),
    )
    .await
    .unwrap();
    server.join().unwrap();
}
