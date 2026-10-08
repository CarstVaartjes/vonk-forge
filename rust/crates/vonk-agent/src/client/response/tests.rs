#![cfg(test)]

use super::super::test_support::*;
use super::*;

#[test]
fn only_refused_identity_is_fatal_for_the_agent() {
    for status in [401, 403] {
        let error = ClientError::Controller(Box::new(ControllerError::from_status(status)));
        assert!(error.fatal());
        assert_eq!(
            error.decision(),
            vonk_agent_protocol::generated::AgentClientDecision::Exit.as_str()
        );
    }
    assert!(ClientError::Identity.fatal());
    assert!(ClientError::Pin.fatal());
    for status in [400, 404, 409, 422] {
        let error = ClientError::Controller(Box::new(ControllerError::from_status(status)));
        assert!(!error.fatal());
        assert!(!error.retryable());
        assert_eq!(
            error.decision(),
            vonk_agent_protocol::generated::AgentClientDecision::Defer.as_str()
        );
    }
    assert!(!ClientError::Protocol.fatal());
    assert!(!ClientError::Retryable.fatal());
}

#[tokio::test]
async fn capture_peer_drop_releases_silent_header_and_body_readers() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    // A lost caller before either complete headers or a complete body must
    // close its exact accepted socket, rather than leave a background peer
    // alive until the ordinary request deadline.
    for (partial, expected_stage) in [
        (b"POST / HTTP/1.1\r\n".as_slice(), CaptureStage::Headers),
        (
            b"POST / HTTP/1.1\r\nContent-Length: 1\r\n\r\n".as_slice(),
            CaptureStage::Body,
        ),
    ] {
        let (client, mut server) =
            request_capture_client(204, Vec::new(), Vec::new(), None, HEARTBEAT_REQUEST_TIMEOUT)
                .await;
        let address = client.controller.socket_addrs(|| None).unwrap()[0];
        let mut stream = tokio::net::TcpStream::connect(address).await.unwrap();
        stream.write_all(partial).await.unwrap();
        let stage = tokio::time::timeout_at(server.deadline, server.stages.recv())
            .await
            .expect("capture reader phase acknowledgement deadline")
            .expect("capture peer ended before entering its incomplete read");
        assert_eq!(stage, expected_stage);
        drop(server);
        let mut byte = [0_u8; 1];
        let closed = tokio::time::timeout(Duration::from_secs(1), stream.read(&mut byte))
            .await
            .expect("dropped capture peer retained its accepted socket");
        match closed {
            Ok(0) => (),
            Err(error) => assert_eq!(error.kind(), std::io::ErrorKind::ConnectionReset),
            other => panic!("dropped capture peer did not close its socket: {other:?}"),
        }
    }
}

#[tokio::test]
async fn exact_worker_observation_reports_process_without_endpoint_result() {
    let run_id = Uuid::new_v4();
    let observations = vec![ExactRecipeRunObservation {
        run_id,
        run_generation: i64::MAX as u64,
        process_running: false,
        endpoint_ready: None,
        failure_diagnostics: None,
    }];
    let (client, server) = observation_client(204).await;
    client
        .report_exact_recipe_run_observations(Utc::now(), &observations)
        .await
        .unwrap();
    let raw = finish_capture_peer(server).await;
    let body = raw
        .windows(4)
        .position(|value| value == b"\r\n\r\n")
        .map(|index| &raw[index + 4..])
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(body).unwrap();
    assert!(body["observed_at"].is_string());
    assert_eq!(body["runs"][0]["run_id"], run_id.to_string());
    assert_eq!(body["runs"][0]["process_running"], false);
    assert_eq!(body["runs"][0]["endpoint_ready"], serde_json::Value::Null);
    assert_eq!(body["runs"][0]["run_generation"], i64::MAX as u64);
}

#[tokio::test]
async fn controller_rejection_preserves_safe_status_code_and_request_id() {
    let sample = telemetry_sample();
    let (client, server) = request_capture_client(
        403,
        vec![
            "X-Request-ID: 00000000-0000-4000-8000-000000000099".to_owned(),
            format!(
                "X-Vonk-Error-Code: {}",
                vonk_agent_protocol::generated::SecurityRefusalReason::ControllerRequestRejected
                    .as_str()
            ),
        ],
        Vec::new(),
        None,
        CONTROLLER_REQUEST_TIMEOUT,
    )
    .await;

    let error = client
        .report_telemetry(std::slice::from_ref(&sample))
        .await
        .expect_err("Controller rejection should be surfaced");
    finish_capture_peer(server).await;
    assert_eq!(error.status(), Some(403));
    assert_eq!(
        error.code(),
        Some(
            vonk_agent_protocol::generated::SecurityRefusalReason::ControllerRequestRejected
                .as_str()
        )
    );
    assert_eq!(
        error.request_id(),
        Some("00000000-0000-4000-8000-000000000099")
    );
    assert!(!error.retryable());
}

#[test]
fn a_union_refusal_digest_reports_the_broken_constraint_not_the_branches() {
    // The Controller reports every union branch that did not match, so the
    // digest must surface the one constraint the producer broke and count
    // the rest, instead of listing branch shape mismatches.
    let body = rejection_problem(serde_json::json!([
        {
            "type": "missing",
            "loc": ["body", "result", "RuntimePreflightResult", "schema_version"],
            "msg": "Field required"
        },
        {
            "type": "extra_forbidden",
            "loc": ["body", "result", "RuntimePreflightResult", "diagnostics"],
            "msg": "Extra inputs are not permitted"
        },
        {
            "type": "missing",
            "loc": ["body", "result", "AgentInstallResult", "installed_bytes"],
            "msg": "Field required"
        },
        {
            "type": "string_pattern_mismatch",
            "loc": ["body", "result", "failure_kind"],
            "msg": "String should match pattern"
        },
        {
            "type": "is_instance_of",
            "loc": ["body", "result", "AgentFailureResult", "failure_kind"],
            "msg": "Input should be an instance of AgentFailureKind"
        },
        {"type": "missing", "loc": ["body", "state"], "msg": "Field required"}
    ]));

    let digest = controller_rejection_digest(&body).expect("a declared problem digest");

    assert!(digest.starts_with("request is invalid: "));
    assert!(digest.contains("body.result.failure_kind (string_pattern_mismatch)"));
    assert!(digest.contains("body.result.AgentFailureResult.failure_kind (is_instance_of)"));
    assert!(digest.contains("+4 more"));
    assert!(!digest.contains("RuntimePreflightResult"));
    assert!(!digest.contains("AgentInstallResult"));
    assert!(digest.len() <= MAX_REJECTION_CONTEXT_CHARS);
}

#[test]
fn a_refusal_digest_falls_back_to_shape_errors_and_stays_bounded() {
    // With no constraint failure to report, the shape mismatches that
    // remain are still more useful than nothing, and an over-long location
    // is truncated rather than refusing the record.
    let long = "segment".repeat(40);
    let body = rejection_problem(serde_json::json!([
        {"type": "missing", "loc": ["body", long, "state"], "msg": "Field required"}
    ]));

    let digest = controller_rejection_digest(&body).expect("a declared problem digest");

    assert!(digest.starts_with("request is invalid: body."));
    assert!(digest.ends_with("(missing)"));
    assert!(!digest.contains(&"segment".repeat(20)));
    assert!(digest.len() <= MAX_REJECTION_CONTEXT_CHARS);
}

#[test]
fn a_refusal_digest_redacts_credentials_and_rejects_undeclared_shapes() {
    let sensitive = serde_json::json!({
        "detail": "authorization: bearer suppressed",
        "issues": []
    })
    .to_string()
    .into_bytes();
    assert_eq!(
        controller_rejection_digest(&sensitive).as_deref(),
        Some("[redacted diagnostic line]")
    );

    // A body that is absent, not JSON, or not the declared object shape
    // yields no digest rather than a guess.
    assert_eq!(controller_rejection_digest(b""), None);
    assert_eq!(controller_rejection_digest(b"not json"), None);
    assert_eq!(controller_rejection_digest(b"[1,2,3]"), None);
    assert_eq!(controller_rejection_digest(b"{}"), None);
    let detail_only = br#"{"detail":"request is invalid"}"#;
    assert_eq!(
        controller_rejection_digest(detail_only).as_deref(),
        Some("request is invalid")
    );
}

#[tokio::test]
async fn a_refused_result_records_the_controller_validation_digest() {
    // End to end over the real transport: the 422 problem the Controller
    // publishes is what the durable refusal keeps, so an operator can read
    // the failing field and rule from the agent's own record.
    let body = rejection_problem(serde_json::json!([
        {
            "type": "missing",
            "loc": ["body", "result", "RuntimePreflightResult", "schema_version"],
            "msg": "Field required"
        },
        {
            "type": "is_instance_of",
            "loc": ["body", "result", "AgentFailureResult", "failure_kind"],
            "msg": "Input should be an instance of AgentFailureKind"
        }
    ]));
    let (client, server) = request_capture_client(
        422,
        vec!["X-Vonk-Error-Code: controller.invalid_request".to_owned()],
        body,
        None,
        CONTROLLER_REQUEST_TIMEOUT,
    )
    .await;

    let error = client
        .submit_result(&retained_result())
        .await
        .expect_err("a 422 is a refusal, never an acceptance");
    finish_capture_peer(server).await;

    let ClientError::ResultRejected(error) = error else {
        panic!("a 422 must map to a typed ingress refusal");
    };
    assert_eq!(
        error.rejection_context(),
        "request is invalid: body.result.AgentFailureResult.failure_kind \
         (is_instance_of); +1 more"
    );
}
