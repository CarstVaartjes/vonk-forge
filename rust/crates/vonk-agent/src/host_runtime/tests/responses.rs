#![cfg(test)]

use super::*;

#[test]
fn runtime_rejection_binds_and_redacts_captured_process_logs() {
    let mut response: super::super::HelperResponse = vonk_agent_protocol::parse_strict(
        br#"{"schema_version":1,"request_id":null,"status":"rejected","error_code":"runtime_process_exited","diagnostic":"ModuleNotFoundError: runtime module\nAPI_TOKEN=private-value\n"}"#,
    ).unwrap();
    let error = super::super::runtime_rejection(&response, HostRuntimeAction::RunInspect);
    assert!(error.diagnostic().unwrap().contains("ModuleNotFoundError"));
    assert!(!error.diagnostic().unwrap().contains("private-value"));
    assert!(
        super::super::runtime_rejection(&response, HostRuntimeAction::Start)
            .diagnostic()
            .is_none()
    );
    response.error_code = Some(HelperErrorCode::OperationUnsafePath);
    assert!(
        super::super::runtime_rejection(&response, HostRuntimeAction::RunInspect)
            .diagnostic()
            .is_none()
    );
    response.process_running = Some(true);
    assert!(require_executed_outcome(&response, false).is_err());
    require_executed_outcome(&valid_executed_response(), false).unwrap();
}

#[test]
fn unbound_helper_rejection_names_the_check_that_refused() {
    // The helper attaches the request identity only after it authorizes the
    // grant, so these refusals cannot echo it. Requiring the identity anyway
    // reported each as `helper_protocol_invalid` -- the same label a corrupt
    // reply gets -- which is how a live privileged start became
    // unattributable with no diagnostic to read.
    let request_id = "10000000-0000-4000-8000-000000000001";
    for (code, expected) in [
        ("grant_node_mismatch", "helper_grant_node_mismatch"),
        ("grant_unauthorized", "helper_grant_unauthorized"),
        ("peer_identity_invalid", "helper_peer_identity_invalid"),
    ] {
        let response: super::super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":null,"status":"rejected","error_code":"{code}"}}"#
            )
            .as_bytes(),
        )
        .unwrap();
        super::super::require_bound_response(&response, request_id).unwrap();
        assert_eq!(
            super::super::runtime_rejection(&response, HostRuntimeAction::Start).preflight_code(),
            expected
        );
    }
}

#[test]
fn a_reply_this_agent_cannot_bind_is_still_a_protocol_error() {
    let request_id = "10000000-0000-4000-8000-000000000001";
    let rejected_for_another_request: super::super::HelperResponse =
        vonk_agent_protocol::parse_strict(
            br#"{"schema_version":1,"request_id":"20000000-0000-4000-8000-000000000002","status":"rejected","error_code":"grant_unauthorized"}"#,
        )
        .unwrap();
    assert!(
        super::super::require_bound_response(&rejected_for_another_request, request_id).is_err()
    );

    // Every other code is produced only after the helper knows the request,
    // so an unbound reply claiming one cannot be accounted for.
    for code in [
        "request_replayed",
        "request_ledger_failed",
        "operation_failed",
        "runtime_process_exited",
    ] {
        let unbound: super::super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":null,"status":"rejected","error_code":"{code}"}}"#
            )
            .as_bytes(),
        )
        .unwrap();
        assert!(
            super::super::require_bound_response(&unbound, request_id).is_err(),
            "{code} must not be accepted without a request identity"
        );
    }

    let unbound_success: super::super::HelperResponse = vonk_agent_protocol::parse_strict(
        br#"{"schema_version":1,"request_id":null,"status":"container-runtime-request-executed"}"#,
    )
    .unwrap();
    assert!(super::super::require_bound_response(&unbound_success, request_id).is_err());
}

#[test]
fn a_large_frame_is_admitted_and_an_oversized_one_reports_its_bound() {
    // Wrong implementation: the 256 KiB ceiling refused a legitimate large
    // command line while the plan it came from was still admitted.
    let above_the_old_ceiling = vec![b'x'; 256 * 1024 + 1];
    let error = call_helper(
        Path::new("/nonexistent"),
        &above_the_old_ceiling,
        Duration::from_secs(1),
    )
    .expect_err("the absent socket refuses");
    assert!(matches!(error, HostRuntimeError::Io(_)));

    let oversized = vec![b'x'; super::super::MAX_HELPER_MESSAGE_BYTES + 1];
    let error = call_helper(
        Path::new("/nonexistent"),
        &oversized,
        Duration::from_secs(1),
    )
    .expect_err("a frame above the ceiling is refused");
    assert_eq!(error.preflight_code(), "helper_message_framing_invalid");
    assert_eq!(
        error.refusal_bound(),
        Some((
            Some(super::super::MAX_HELPER_MESSAGE_BYTES as u64),
            super::super::MAX_HELPER_MESSAGE_BYTES as u64 + 1,
        ))
    );
}

#[test]
fn helper_message_framing_refusal_names_the_message_contract() {
    // Wrong implementation: an empty, oversized, truncated or undecodable
    // length-prefixed helper message collapsed into
    // `helper_protocol_invalid`.
    let oversized = vec![0_u8; super::super::MAX_HELPER_MESSAGE_BYTES + 1];
    for body in [&b""[..], &oversized[..]] {
        let error = call_helper(Path::new("/nonexistent"), body, Duration::from_secs(1))
            .expect_err("an out-of-range request body is refused before connecting");
        assert!(error.diagnostic().is_none());
    }

    let temp = tempfile::tempdir().unwrap();
    let socket = temp.path().join("helper.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    let server = std::thread::spawn(move || {
        for reply in [
            &0_u32.to_be_bytes()[..],
            &(super::super::MAX_HELPER_MESSAGE_BYTES as u32 + 1).to_be_bytes()[..],
        ] {
            let (mut stream, _) = listener.accept().unwrap();
            let mut prefix = [0_u8; 4];
            stream.read_exact(&mut prefix).unwrap();
            let mut body = vec![0_u8; u32::from_be_bytes(prefix) as usize];
            stream.read_exact(&mut body).unwrap();
            stream.write_all(reply).unwrap();
        }
        let (mut stream, _) = listener.accept().unwrap();
        let mut prefix = [0_u8; 4];
        stream.read_exact(&mut prefix).unwrap();
        let mut body = vec![0_u8; u32::from_be_bytes(prefix) as usize];
        stream.read_exact(&mut body).unwrap();
        let undecodable = b"not-json";
        stream
            .write_all(&(undecodable.len() as u32).to_be_bytes())
            .unwrap();
        stream.write_all(undecodable).unwrap();
        let (mut stream, _) = listener.accept().unwrap();
        let mut prefix = [0; 4];
        stream.read_exact(&mut prefix).unwrap();
        let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
        stream.read_exact(&mut body).unwrap();
        let response = valid_executed_response();
        let bytes = vonk_agent_protocol::canonical_json(&response).unwrap();
        stream
            .write_all(&(bytes.len() as u32).to_be_bytes())
            .unwrap();
        stream.write_all(&bytes).unwrap();
    });
    for attempt in 0..3 {
        let error = match call_helper(&socket, b"{}", Duration::from_secs(5)) {
            Ok(_) => panic!("helper reply {attempt} must be refused"),
            Err(error) => error,
        };
        assert!(error.diagnostic().is_none());
        assert!(error.diagnostic().is_none());
    }
    require_executed_outcome(
        &call_helper(&socket, b"{}", Duration::from_secs(1)).unwrap(),
        false,
    )
    .unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    while !server.is_finished() && std::time::Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(1));
    }
    assert!(server.is_finished());
    drop(server);
}

#[test]
fn foreign_request_identity_refusal_names_the_binding_contract() {
    // Wrong implementation: a reply that named a different request, or
    // declared a schema this agent cannot bind, collapsed into
    // `helper_protocol_invalid`.
    let request_id = "10000000-0000-4000-8000-000000000001";
    let mut foreign: super::super::HelperResponse = vonk_agent_protocol::parse_strict(
        br#"{"schema_version":1,"request_id":"20000000-0000-4000-8000-000000000002","status":"rejected","error_code":"grant_unauthorized"}"#,
    )
    .unwrap();
    let error = require_bound_response(&foreign, request_id)
        .expect_err("a reply bound to another request must be refused");
    assert!(error.diagnostic().is_none());

    // The wire schema pins `schema_version` to 1, so a reply that declares
    // another schema can only arrive through a decoding path that skipped
    // that constraint. The binding check still refuses it.
    foreign.schema_version = 2;
    foreign.request_id = Some(Uuid::parse_str(request_id).unwrap());
    let error = require_bound_response(&foreign, request_id)
        .expect_err("a reply declaring another schema must be refused");
    assert!(error.diagnostic().is_none());
    foreign.schema_version = 1;
    require_bound_response(&foreign, request_id).unwrap();
}

#[test]
fn malformed_helper_rejection_names_the_rejection_contract() {
    // Wrong implementation: a rejection whose status, evidence, exit code or
    // code broke the rejection contract, or a diagnostic attached to
    // anything but `(RunInspect, runtime_process_exited)`, collapsed into
    // `helper_protocol_invalid`.
    let request_id = "10000000-0000-4000-8000-000000000001";
    let baseline: super::super::HelperResponse = vonk_agent_protocol::parse_strict(
        format!(
            r#"{{"schema_version":1,"request_id":"{request_id}","status":"rejected","error_code":"operation_failed"}}"#
        )
        .as_bytes(),
    )
    .unwrap();

    // A status other than `rejected`.
    let other_status: super::super::HelperResponse = vonk_agent_protocol::parse_strict(
        format!(
            r#"{{"schema_version":1,"request_id":"{request_id}","status":"container-runtime-request-executed","error_code":"operation_failed"}}"#
        )
        .as_bytes(),
    )
    .unwrap();
    assert_rejection_malformed(&other_status, HostRuntimeAction::RunInspect);

    // A rejection never carries execution evidence, an exit code or the
    // inspection outcome only an executed inspection owns.
    let mut response = baseline.clone();
    response.exit_code = Some(0);
    assert_rejection_malformed(&response, HostRuntimeAction::RunInspect);
    let mut response = baseline.clone();
    response.process_running = Some(true);
    assert_rejection_malformed(&response, HostRuntimeAction::RunInspect);

    // A code outside the stable set.
    let response = baseline.clone();
    let mut document = serde_json::to_value(&response).unwrap();
    document["error_code"] = serde_json::Value::String("untrusted_response_detail".into());
    assert!(
        vonk_agent_protocol::parse_strict::<super::super::HelperResponse>(
            &serde_json::to_vec(&document).unwrap()
        )
        .is_err()
    );

    // A diagnostic is only meaningful for
    // `(RunInspect, runtime_process_exited)`.
    let mut response = baseline.clone();
    response.diagnostic = Some("private detail".into());
    assert_rejection_malformed(&response, HostRuntimeAction::RunInspect);

    // A rejection must name a code at all.
    let mut response = baseline;
    response.error_code = None;
    assert_rejection_malformed(&response, HostRuntimeAction::RunInspect);
}

#[test]
fn malformed_executed_outcome_names_the_outcome_contract() {
    // Wrong implementation: an executed outcome that carried a diagnostic,
    // named the wrong status, or reported a non-byte exit code collapsed into
    // `helper_protocol_invalid`.
    let request_id = "10000000-0000-4000-8000-000000000001";
    let baseline: super::super::HelperResponse = vonk_agent_protocol::parse_strict(
        format!(
            r#"{{"schema_version":1,"request_id":"{request_id}","status":"container-runtime-request-executed"}}"#
        )
        .as_bytes(),
    )
    .unwrap();

    // A capture diagnostic is not part of an executed outcome.
    let mut response = baseline.clone();
    response.diagnostic = Some("private detail".into());
    assert_outcome_malformed(&response);

    // The status must be the executed one unless it is stop-uncertain.
    let rejected: super::super::HelperResponse = vonk_agent_protocol::parse_strict(
        format!(r#"{{"schema_version":1,"request_id":"{request_id}","status":"rejected"}}"#)
            .as_bytes(),
    )
    .unwrap();
    assert_outcome_malformed(&rejected);

    // The exit code must fit a process byte.
    let mut response = baseline.clone();
    response.exit_code = Some(256);
    assert_outcome_malformed(&response);

    // An executed outcome never carries the inspection outcome only an
    // inspection reply does.
    let mut response = baseline;
    response.process_running = Some(true);
    assert_outcome_malformed(&response);

    // The deliberate stop-uncertain outcome and a byte-sized exit code stay
    // accepted.
    let stop_uncertain: super::super::HelperResponse = vonk_agent_protocol::parse_strict(
        format!(
            r#"{{"schema_version":1,"request_id":"{request_id}","status":"container-runtime-stop-uncertain","exit_code":137}}"#
        )
        .as_bytes(),
    )
    .unwrap();
    assert!(require_executed_outcome(&stop_uncertain, true).is_ok());
}
