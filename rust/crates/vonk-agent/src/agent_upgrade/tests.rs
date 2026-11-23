#![cfg(test)]
use super::*;
use vonk_agent_protocol::{canonical_generated_json, parse_strict};

fn response(status: HostHelperResponseStatus) -> HelperResponse {
    HelperResponse {
        diagnostic: None,
        process_logs: None,
        installation_intent_nonce: None,
        schema_version: 1,
        request_id: Some("10000000-0000-4000-8000-000000000001".parse().unwrap()),
        status,
        error_code: None,
        exit_code: None,
        process_running: None,
        job_progress_at: None,

        container_labels: None,
    }
}

#[test]
fn preparation_observation_survives_helper_wire_and_fresh_completion() {
    let request_id = "10000000-0000-4000-8000-000000000001";
    let mut pending = response(HostHelperResponseStatus::Rejected);
    pending.error_code = Some(HelperErrorCode::PackagePreparationUnavailable);
    let body = canonical_generated_json(&pending).unwrap();
    let observed = parse_strict(&body).unwrap();
    let error = validate_helper_response(&observed, request_id).unwrap_err();
    // This retained observation reaches the executor's temporary-dependency
    // path; a malformed-response implementation loses the helper evidence.
    assert!(error.helper_diagnostics().is_some());
    validate_helper_response(
        &response(HostHelperResponseStatus::PackageInstalled),
        request_id,
    )
    .unwrap();
}

#[test]
fn package_failure_detail_survives_wire_roundtrip_and_redacts_secrets() {
    let mut response = response(HostHelperResponseStatus::Rejected);
    response.error_code = Some(HelperErrorCode::PackageInstallFailed);
    response.exit_code = Some(1);
    response.diagnostic = Some("permission denied\npassword=do-not-expose".into());
    let response = parse_strict(&canonical_generated_json(&response).unwrap()).unwrap();
    let error =
        validate_helper_response(&response, "10000000-0000-4000-8000-000000000001").unwrap_err();
    assert!(error.diagnostic().unwrap().contains("permission denied"));
    assert!(!error.diagnostic().unwrap().contains("do-not-expose"));
}

#[test]
fn package_context_rejects_the_shared_inspection_outcome_field() {
    let mut response = response(HostHelperResponseStatus::PackageInstalled);
    response.process_running = Some(true);
    let mut response: HelperResponse =
        parse_strict(&canonical_generated_json(&response).unwrap()).unwrap();
    assert!(validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_err());
    response.status = HostHelperResponseStatus::Rejected;
    assert!(validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_err());
}

#[test]
fn accepts_rejection_without_optional_diagnostics() {
    let response: HelperResponse =
        parse_strict(br#"{"request_id":null,"schema_version":1,"status":"rejected"}"#).unwrap();
    assert!(response.error_code.is_none());
    assert!(validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_err());
}

#[test]
fn accepts_bounded_package_install_diagnostics() {
    let mut response = response(HostHelperResponseStatus::Rejected);
    response.error_code = Some(HelperErrorCode::PackageInstallFailed);
    response.exit_code = Some(75);
    let error =
        validate_helper_response(&response, "10000000-0000-4000-8000-000000000001").unwrap_err();
    assert_eq!(
        error.helper_diagnostics(),
        Some((HelperErrorCode::PackageInstallFailed, Some(75)))
    );
}

#[test]
fn rejects_unbounded_or_misbound_package_exit_diagnostics() {
    let mut response = response(HostHelperResponseStatus::Rejected);
    response.error_code = Some(HelperErrorCode::PackageInstallFailed);
    response.exit_code = Some(256);
    assert!(validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_err());

    response.error_code = Some(HelperErrorCode::OperationFailed);
    response.exit_code = Some(1);
    assert!(validate_helper_response(&response, "10000000-0000-4000-8000-000000000001",).is_err());
}

#[test]
fn rejects_untrusted_helper_diagnostics() {
    let reply = br#"{"request_id":null,"schema_version":1,"status":"rejected","error_code":"dpkg stderr: secret"}"#;
    assert!(parse_strict::<HelperResponse>(reply).is_err());
}

#[test]
fn damaged_retained_package_is_a_miss_and_fresh_verified_content_is_reused() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("package.deb");
    let bytes = b"signed package contents";
    let digest = hex::encode(Sha256::digest(bytes));
    for damaged in [
        b"truncated".as_slice(),
        b"wrong package contents!".as_slice(),
    ] {
        fs::write(&path, damaged).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(!verified_file(&path, bytes.len() as u64, &digest).unwrap());
        assert!(!path.exists());
        fs::write(&path, bytes).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(verified_file(&path, bytes.len() as u64, &digest).unwrap());
        assert_eq!(fs::read(&path).unwrap(), bytes);
    }
}
