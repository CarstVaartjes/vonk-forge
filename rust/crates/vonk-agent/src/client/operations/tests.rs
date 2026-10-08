#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[tokio::test]
async fn heartbeat_preserves_lossless_progress_counters_on_the_network() {
    for counter in ["18446744073709551616".to_owned(), "9".repeat(200)] {
        let mut progress = progress();
        let measured = progress.progress.as_mut().unwrap();
        measured.completed_bytes = serde_json::from_str(&counter).unwrap();
        measured.total_bytes = Some(serde_json::from_str(&counter).unwrap());
        measured.completed_items = Some(serde_json::from_str(&counter).unwrap());
        measured.total_items = Some(serde_json::from_str(&counter).unwrap());
        let directive = AgentDirective {
            cancel_requested: false,
            deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:30+00:00").unwrap(),
            fence: progress.fence,
        };
        let (client, server) = heartbeat_client(directive.clone()).await;
        assert_eq!(client.heartbeat(&progress).await.unwrap(), directive);
        let request = finish_capture_peer(server).await;
        let start = request.windows(4).position(|v| v == b"\r\n\r\n").unwrap() + 4;
        let observed =
            vonk_agent_protocol::parse_strict::<AgentProgress>(&request[start..]).unwrap();
        assert_eq!(observed, progress);
        assert!(
            std::str::from_utf8(&request[start..])
                .unwrap()
                .contains(&format!("\"completed_bytes\":{counter}"))
        );
    }
}

#[tokio::test]
async fn ordinary_heartbeat_preserves_active_upload_counters() {
    let mut progress = progress();
    progress.progress.as_mut().unwrap().phase = ProgressPhase::Executing.as_str().to_owned();
    let directive = AgentDirective {
        cancel_requested: false,
        deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:30+00:00").unwrap(),
        fence: progress.fence,
    };
    let (client, server) = heartbeat_client(directive).await;
    client.set_progress_phase(progress.fence, ProgressPhase::Uploading);
    client.set_progress_bytes(progress.fence, 512, 1024);
    client.heartbeat(&progress).await.unwrap();
    let request = finish_capture_peer(server).await;
    let start = request
        .windows(4)
        .position(|value| value == b"\r\n\r\n")
        .unwrap()
        + 4;
    let received = vonk_agent_protocol::parse_strict::<AgentProgress>(&request[start..])
        .unwrap()
        .progress
        .unwrap();
    assert_eq!(received.phase, ProgressPhase::Uploading.as_str());
    assert_eq!(received.completed_bytes, 512);
    assert_eq!(received.total_bytes, Some(1024_u64.into()));
}

#[tokio::test]
async fn lease_only_heartbeat_omits_measured_progress() {
    let mut progress = progress();
    progress.progress = None;
    let directive = AgentDirective {
        cancel_requested: false,
        deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:30+00:00").unwrap(),
        fence: progress.fence,
    };
    let (client, server) = heartbeat_client(directive.clone()).await;

    assert_eq!(client.heartbeat(&progress).await.unwrap(), directive);
    let request = finish_capture_peer(server).await;
    let body = request
        .windows(4)
        .position(|value| value == b"\r\n\r\n")
        .map(|index| &request[index + 4..])
        .unwrap();
    let document: Value = serde_json::from_slice(body).unwrap();
    assert!(!document.as_object().unwrap().contains_key("progress"));
    assert_eq!(
        serde_json::from_slice::<AgentProgress>(body).unwrap(),
        progress
    );
}

#[tokio::test]
async fn heartbeat_rejects_legacy_progress_response_shape() {
    let progress = progress();
    let (client, server) = request_capture_client(
        200,
        vec!["Content-Type: application/json".to_owned()],
        canonical_json(&progress).unwrap(),
        None,
        CONTROLLER_REQUEST_TIMEOUT,
    )
    .await;

    assert!(matches!(
        client.heartbeat(&progress).await,
        Err(ClientError::Protocol)
    ));
    finish_capture_peer(server).await;
}
