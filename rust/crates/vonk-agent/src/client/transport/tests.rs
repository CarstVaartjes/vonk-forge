#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn expired_renewal_proof_matches_the_controller_signed_wire_fixture() {
    let expected: vonk_agent_protocol::generated::ExpiredRenewRequest =
        vonk_agent_protocol::parse_strict(include_bytes!(
            "../../../../../../agent_protocol/fixtures/expired-renewal-request.json"
        ))
        .unwrap();
    let signer = ring::signature::Ed25519KeyPair::from_seed_unchecked(&[42; 32]).unwrap();
    let actual = super::expired_renewal_request(
        &expected.node_id,
        expected.serial.clone(),
        expected.csr.as_bytes(),
        1_800_000_000,
        &signer,
    )
    .unwrap();
    assert_eq!(actual, expected);
}

#[test]
fn renewal_conflict_is_distinguished_from_revoked_identity() {
    assert!(is_rotation_conflict(
        br#"{"detail":"a different certificate rotation is already staged"}"#
    ));
    assert!(is_rotation_conflict(
        &canonical_json(&vonk_agent_protocol::generated::ControllerRefusalBody { code: Some(vonk_agent_protocol::generated::SecurityRefusalReason::AgentCertificateRotationConflict.as_str().into()), detail: None }).unwrap()
    ));
    assert!(!is_rotation_conflict(
        br#"{"detail":"agent certificate is not active"}"#
    ));
}

#[test]
fn reported_hostname_is_bounded_dns_syntax() {
    assert!(valid_reported_hostname("spark-3542"));
    assert!(valid_reported_hostname("spark-3542.lab.internal"));
    assert!(!valid_reported_hostname(""));
    assert!(!valid_reported_hostname("-spark"));
    assert!(!valid_reported_hostname("spark_3542"));
    assert!(!valid_reported_hostname(&"a".repeat(256)));
}

#[test]
fn cloned_clients_share_the_rotatable_transport() {
    let client =
        AgentHttpClient::for_http_test("http://127.0.0.1/", "spk_0123456789abcdef0123456789abcdef");
    let operation_client = client.clone();
    assert!(Arc::ptr_eq(&client.client, &operation_client.client));

    let replacement = reqwest::Client::builder().build().unwrap();
    *client.client.try_write().expect("uncontended test client") = replacement;
    assert!(Arc::ptr_eq(&client.client, &operation_client.client));
}

#[tokio::test]
async fn cloned_operation_client_sends_through_replaced_transport() {
    let (client, server) = request_capture_client(
        204,
        Vec::new(),
        Vec::new(),
        None,
        CONTROLLER_REQUEST_TIMEOUT,
    )
    .await;
    let operation_client = client.clone();
    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert(
        "x-rotation-marker",
        reqwest::header::HeaderValue::from_static("fresh"),
    );
    let replacement = reqwest::Client::builder()
        .default_headers(headers)
        .timeout(CONTROLLER_REQUEST_TIMEOUT)
        .build()
        .unwrap();
    *client.client.try_write().expect("uncontended test client") = replacement;

    operation_client
        .report_telemetry(&[telemetry_sample()])
        .await
        .unwrap();
    let request = finish_capture_peer(server).await;
    assert!(
        String::from_utf8_lossy(&request)
            .to_ascii_lowercase()
            .contains("x-rotation-marker: fresh")
    );
}

#[tokio::test]
async fn rotation_drains_requests_without_starving_heartbeats() {
    use std::sync::atomic::{AtomicBool, Ordering};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    for activation_status in [204, 403] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/", listener.local_addr().unwrap());
        let first_received = Arc::new(tokio::sync::Notify::new());
        let release_first = Arc::new(tokio::sync::Notify::new());
        let first_finished = Arc::new(AtomicBool::new(false));
        let server = {
            let first_received = first_received.clone();
            let release_first = release_first.clone();
            let first_finished = first_finished.clone();
            spawn_async_peer(async move {
                let mut handlers = Vec::new();
                for index in 0..4 {
                    let (mut stream, _) = tokio::time::timeout(PEER_BUDGET, listener.accept())
                        .await
                        .expect("fixture I/O deadline")
                        .unwrap();
                    let first_received = first_received.clone();
                    let release_first = release_first.clone();
                    let first_finished = first_finished.clone();
                    handlers.push(spawn_async_peer(async move {
                        let mut request = Vec::new();
                        let mut buf = [0; 4096];
                        loop {
                            let size = tokio::time::timeout(PEER_BUDGET, stream.read(&mut buf)).await.expect("fixture I/O deadline").unwrap();
                            assert_ne!(size, 0);
                            request.extend_from_slice(&buf[..size]);
                            if let Some(end) = request.windows(4).position(|b| b == b"\r\n\r\n") {
                                let headers = String::from_utf8_lossy(&request[..end]);
                                let length: usize = headers.lines().find_map(|line| {
                                    let (name, value) = line.split_once(':')?;
                                    name.eq_ignore_ascii_case("content-length")
                                        .then(|| value.trim().parse().unwrap())
                                }).unwrap();
                                if request.len() >= end + 4 + length { break; }
                            }
                        }
                        let request = String::from_utf8(request).unwrap().to_ascii_lowercase();
                        let activating = request.starts_with("post /agent/renew/activate ");
                        if index == 0 {
                            first_received.notify_one();
                            tokio::time::timeout(PEER_BUDGET, release_first.notified()).await.expect("fixture notification deadline");
                            first_finished.store(true, Ordering::SeqCst);
                        }
                        let status = if activating {
                            assert!(first_finished.load(Ordering::SeqCst), "activation raced an old request");
                            activation_status
                        } else { 204 };
                        stream.write_all(format!(
                            "HTTP/1.1 {status} Test\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        ).as_bytes()).await.unwrap();
                        request
                    }));
                }
                let mut requests = Vec::new();
                for handler in handlers {
                    requests.push(handler.await.unwrap());
                }
                requests
            })
        };
        let client = AgentHttpClient::for_http_test(&url, "spk_0123456789abcdef0123456789abcdef");
        let replacement = AgentHttpClient::for_http_test(&url, client.node_id());
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(
            "x-rotation-marker",
            reqwest::header::HeaderValue::from_static("fresh"),
        );
        *replacement.client.try_write().unwrap() = reqwest::Client::builder()
            .default_headers(headers)
            .build()
            .unwrap();
        let operation = client.clone();
        let request = spawn_async_peer(async move {
            operation
                .report_telemetry(&[telemetry_sample()])
                .await
                .unwrap();
        });
        tokio::time::timeout(PEER_BUDGET, first_received.notified())
            .await
            .expect("fixture notification deadline");
        let rotation = client.activate_replacement(&replacement, 2);
        tokio::pin!(rotation);
        // Poll the real activation while an HTTP response is outstanding.
        assert!(
            tokio::time::timeout(Duration::from_millis(200), &mut rotation)
                .await
                .is_err()
        );
        let samples = [telemetry_sample()];
        let probe = client.report_telemetry(&samples);
        tokio::pin!(probe);
        tokio::select! {
            result = &mut rotation => panic!("rotation interrupted an active request: {result:?}"),
            result = &mut probe => result.unwrap(),
            _ = tokio::time::sleep(Duration::from_secs(2)) => panic!("rotation starved the heartbeat"),
        }
        release_first.notify_one();
        request.await.unwrap();
        let result = tokio::time::timeout(Duration::from_secs(2), &mut rotation)
            .await
            .unwrap();
        if activation_status == 204 {
            result.unwrap();
        } else {
            let error = result.unwrap_err();
            assert_eq!(error.status(), Some(403));
            assert!(!error.retryable());
        }
        client
            .report_telemetry(&[telemetry_sample()])
            .await
            .unwrap();
        let requests = server.await.unwrap();
        assert!(!requests[1].contains("x-rotation-marker"));
        assert!(requests[2].starts_with("post /agent/renew/activate "));
        assert!(requests[2].contains("x-rotation-marker: fresh"));
        assert_eq!(
            requests[3].contains("x-rotation-marker: fresh"),
            activation_status == 204
        );
    }
}

#[tokio::test]
async fn rotation_silent_activation_retries_same_generation_and_repairs_heartbeat() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    // Catch falsely installing a timed-out identity, retaining the writer
    // after timeout, changing the retry generation, or admitting an old
    // certificate request while activation can revoke that certificate.
    let budget = ROTATION_REQUEST_TIMEOUT * 2 + HEARTBEAT_REQUEST_TIMEOUT * 2;
    let deadline = tokio::time::Instant::now() + budget;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/", listener.local_addr().unwrap());
    let activation_received = Arc::new(tokio::sync::Notify::new());
    let expected_progress = progress();
    let directive = AgentDirective {
        cancel_requested: false,
        deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:30+00:00").unwrap(),
        fence: expected_progress.fence,
    };
    let response_body = canonical_json(&directive).unwrap();
    // JoinSet owns the socket task: every assertion failure drops/aborts
    // the peer, including a peer intentionally withholding its response.
    let mut peers = tokio::task::JoinSet::new();
    let received = activation_received.clone();
    peers.spawn(async move {
        tokio::time::timeout_at(deadline, async move {
            let mut requests = Vec::new();
            for index in 0..4 {
                let (mut stream, _) = tokio::time::timeout(PEER_BUDGET, listener.accept()).await.expect("fixture I/O deadline").unwrap();
                let mut request = Vec::new();
                let mut buffer = [0_u8; 4096];
                let header_end = loop {
                    let size = tokio::time::timeout_at(deadline, stream.read(&mut buffer))
                        .await.expect("rotation request read deadline").unwrap();
                    assert_ne!(size, 0, "rotation request ended before its body");
                    request.extend_from_slice(&buffer[..size]);
                    if let Some(end) = request.windows(4).position(|v| v == b"\r\n\r\n") {
                        let header_end = end + 4;
                        let headers = std::str::from_utf8(&request[..end]).unwrap();
                        let length: usize = headers.lines().find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse().unwrap())
                        }).unwrap();
                        if request.len() >= header_end + length { break header_end; }
                    }
                };
                let headers = String::from_utf8_lossy(&request[..header_end]).to_ascii_lowercase();
                assert!(headers.contains(if index == 1 {
                    "x-rotation-marker: old"
                } else {
                    "x-rotation-marker: fresh"
                }));
                if index == 0 || index == 2 {
                    assert!(headers.starts_with("post /agent/renew/activate "));
                } else {
                    assert!(headers.starts_with("post /agent/heartbeat "));
                }
                requests.push(request[header_end..].to_vec());
                if index == 0 {
                    received.notify_one();
                    assert!(tokio::time::timeout(
                        Duration::from_millis(200), listener.accept()
                    ).await.is_err(), "old request overlapped activation");
                    // Withhold all response bytes. The real reqwest
                    // activation deadline must close this exact socket.
                    assert_eq!(tokio::time::timeout(PEER_BUDGET, stream.read(&mut buffer)).await.expect("fixture I/O deadline").unwrap(), 0);
                } else {
                    let (status, body) = if index == 2 {
                        (204, &[][..])
                    } else {
                        (200, response_body.as_slice())
                    };
                    stream.write_all(format!(
                        "HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()
                    ).as_bytes()).await.unwrap();
                    stream.write_all(body).await.unwrap();
                }
            }
            assert_eq!(requests[0], requests[2], "retry changed accepted generation bytes");
            for index in [1, 3] {
                assert_eq!(serde_json::from_slice::<AgentProgress>(&requests[index]).unwrap(), expected_progress);
            }
        }).await.expect("owned rotation peer exceeded request budgets");
    });
    let client = AgentHttpClient::for_http_test(&url, TEST_NODE_ID);
    let mut old_headers = reqwest::header::HeaderMap::new();
    old_headers.insert(
        "x-rotation-marker",
        reqwest::header::HeaderValue::from_static("old"),
    );
    *client.client.try_write().unwrap() = reqwest::Client::builder()
        .default_headers(old_headers)
        .timeout(HEARTBEAT_REQUEST_TIMEOUT)
        .build()
        .unwrap();
    let replacement = AgentHttpClient::for_http_test(&url, TEST_NODE_ID);
    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert(
        "x-rotation-marker",
        reqwest::header::HeaderValue::from_static("fresh"),
    );
    *replacement.client.try_write().unwrap() = reqwest::Client::builder()
        .default_headers(headers)
        .timeout(HEARTBEAT_REQUEST_TIMEOUT)
        .build()
        .unwrap();
    tokio::time::timeout_at(deadline, async {
        let started = tokio::time::Instant::now();
        let rotation = client.activate_replacement(&replacement, 2);
        tokio::pin!(rotation);
        tokio::select! {
            received = tokio::time::timeout_at(deadline, activation_received.notified()) => {
                received.expect("activation peer acceptance deadline");
            },
            result = &mut rotation => panic!("activation ended before silent peer: {result:?}"),
        }
        assert!(
            client.client.try_read().is_err(),
            "activation did not fence old transport"
        );
        let probe_progress = progress();
        assert!(
            tokio::time::timeout(
                Duration::from_millis(200),
                client.heartbeat(&probe_progress)
            )
            .await
            .is_err(),
            "old certificate heartbeat escaped activation fence"
        );
        let error = rotation.await.unwrap_err();
        assert!(
            started.elapsed() < HEARTBEAT_REQUEST_TIMEOUT,
            "activation deadline consumed the heartbeat request budget"
        );
        assert!(matches!(&error, ClientError::Transport(cause) if cause.is_timeout()));
        assert!(error.retryable());
        assert_eq!(
            error.status(),
            None,
            "unknown activation was reported as accepted/refused"
        );
        assert!(
            client.client.try_write().is_ok(),
            "timed-out activation retained writer"
        );
        // Unknown activation keeps the previously accepted transport.
        // This real request must carry old, not prematurely fresh, identity.
        assert_eq!(client.heartbeat(&progress()).await.unwrap(), directive);
        client.activate_replacement(&replacement, 2).await.unwrap();
        assert_eq!(client.heartbeat(&progress()).await.unwrap(), directive);
        tokio::time::timeout(PEER_BUDGET, peers.join_next())
            .await
            .expect("fixture completion deadline")
            .unwrap()
            .unwrap();
    })
    .await
    .expect("rotation recovery exceeded its owning request budgets");
}

#[tokio::test]
async fn heartbeat_posts_exact_progress_and_accepts_matching_renewal() {
    let progress = progress();
    let directive = AgentDirective {
        cancel_requested: false,
        deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:30+00:00").unwrap(),
        fence: progress.fence,
    };
    let (client, server) = heartbeat_client(directive.clone()).await;

    assert_eq!(client.heartbeat(&progress).await.unwrap(), directive);
    let request = finish_capture_peer(server).await;
    let (headers, body) = request
        .windows(4)
        .position(|value| value == b"\r\n\r\n")
        .map(|index| (&request[..index], &request[index + 4..]))
        .unwrap();
    assert!(
        std::str::from_utf8(headers)
            .unwrap()
            .starts_with("POST /agent/heartbeat HTTP/1.1\r\n")
    );
    assert_eq!(
        serde_json::from_slice::<AgentProgress>(body).unwrap(),
        progress
    );
    assert_eq!(
        serde_json::from_slice::<AgentProgress>(body)
            .unwrap()
            .progress
            .unwrap()
            .total_bytes,
        Some(42_u64.into())
    );
}

#[tokio::test]
async fn heartbeat_rejects_mismatched_or_regressing_renewal() {
    let progress = progress();
    let directive = AgentDirective {
        cancel_requested: false,
        deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:30+00:00").unwrap(),
        fence: Uuid::new_v4(),
    };
    let (client, server) = heartbeat_client(directive).await;

    assert!(matches!(
        client.heartbeat(&progress).await,
        Err(ClientError::Protocol)
    ));
    finish_capture_peer(server).await;
}

#[tokio::test]
async fn rotation_drain_deadline_releases_queue_for_a_fresh_heartbeat() {
    let (client, server) = heartbeat_client(AgentDirective {
        fence: progress().fence,
        deadline: (Utc::now() + chrono::Duration::seconds(30)).into(),
        cancel_requested: false,
    })
    .await;
    let replacement = AgentHttpClient::for_http_test(client.controller.as_str(), client.node_id());
    let active_request = client.client.read().await;
    let error = client
        .activate_replacement_until(
            &replacement,
            2,
            tokio::time::Instant::now() + Duration::from_millis(10),
        )
        .await
        .unwrap_err();
    assert!(error.retryable());
    assert!(!error.fatal());
    drop(active_request);
    client.heartbeat(&progress()).await.unwrap();
    finish_capture_peer(server).await;
}
