#![cfg(test)]

use super::*;

#[test]
fn request_is_owner_only_atomic_and_idempotent() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("requests");
    let path = write_request(&root, &"a".repeat(64), b"{}").unwrap();
    assert_eq!(fs::read(&path).unwrap(), b"{}");
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(write_request(&root, &"a".repeat(64), b"{}").unwrap(), path);
    assert_eq!(
        fs::read(write_request(&root, &"a".repeat(64), b"[]").unwrap()).unwrap(),
        b"[]"
    );
}

#[test]
fn request_root_may_not_be_a_symlink() {
    let temp = tempfile::tempdir().unwrap();
    let target = temp.path().join("target");
    fs::create_dir(&target).unwrap();
    let link = temp.path().join("link");
    symlink(&target, &link).unwrap();
    let repaired = write_request(&link, &"a".repeat(64), b"{}").unwrap();
    assert_eq!(fs::read(repaired).unwrap(), b"{}");
    assert!(fs::read_dir(&target).unwrap().next().is_none());
}

#[test]
fn request_arguments_presence_refusal_names_the_presence_rule() {
    // Wrong implementation: a Start with no arguments, or a preflight that
    // carried them, collapsed into `helper_request_document_invalid`.
    let mut absent = start_request();
    absent.arguments.clear();
    assert_eq!(
        request_rule_code(&absent),
        "helper_request_arguments_presence_invalid"
    );

    let mut present = start_request();
    present.action = HostRuntimeAction::RuntimePreflight;
    assert_eq!(
        request_rule_code(&present),
        "helper_request_arguments_presence_invalid"
    );
}

#[test]
fn request_installation_identity_refusal_names_the_identity_rule() {
    // Wrong implementation: an installation identity on an action that is
    // not cleanup collapsed into `helper_request_document_invalid`.
    let mut request = start_request();
    request.installation_id = Some(Uuid::new_v4());
    assert_eq!(
        request_rule_code(&request),
        "helper_request_installation_identity_invalid"
    );
}

#[test]
fn request_bytes_refusal_names_the_request_bound() {
    // Wrong implementation: a request whose canonical document outgrew the
    // bounded helper exchange collapsed into
    // `helper_request_document_invalid`, so a refused Start could not say
    // which rule or which bound refused it.
    let request = request_at_bytes(vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES + 1);
    assert_eq!(request_rule_code(&request), "helper_request_bytes_invalid");
}

#[test]
fn request_argument_refusals_name_the_argument_kind() {
    // Wrong implementation: an empty argument, or one carrying a byte an
    // exec argv cannot frame, collapsed into
    // `helper_request_document_invalid`, so the code could not name the
    // kind of violation rather than an index.
    let mut nul = start_request();
    nul.arguments = vec!["sha256:image".to_owned(), "run\0--flag".to_owned()];
    assert_eq!(request_rule_code(&nul), "helper_request_argument_nul_byte");
}

#[test]
fn a_large_or_multiline_argument_is_admitted() {
    // The authoritative size limit is the canonical request byte ceiling,
    // not a per-argument round number: an inline engine configuration can
    // exceed 4096 bytes, and CR/LF are legal bytes in an exec argv element.
    let mut long = start_request();
    long.arguments = vec![
        "sha256:image".to_owned(),
        format!(
            "--speculative-config={{\"capture\":\"{}\"}}",
            "x".repeat(8_192)
        ),
    ];
    assert!(
        long.validate().is_ok(),
        "a legitimate large inline configuration must be framed"
    );

    let mut multiline = start_request();
    multiline.arguments = vec![
        "sha256:image".to_owned(),
        "line one\nline two\r\n".to_owned(),
    ];
    assert!(
        multiline.validate().is_ok(),
        "CR/LF are legal argv bytes and must not be refused"
    );
}

#[test]
fn a_request_at_the_byte_ceiling_is_admitted_and_one_byte_over_is_refused() {
    // Wrong implementation: the request byte budget equalled the frame
    // budget while its comment called it a backstop below it, and the
    // helper read enforced a private 64 KiB round number, so a request the
    // agent called valid could still be refused after a successful install.
    let limit = vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES;
    assert_eq!(request_at_bytes(limit).validate(), Ok(()));
    assert!(
        request_at_bytes(limit + 1).validate().is_err(),
        "one canonical byte over the request ceiling must be refused"
    );
}

#[test]
fn a_request_bytes_refusal_carries_the_limit_and_the_observed_bytes() {
    // Wrong implementation: the refusal named the rule but not the bound, so
    // an operator could not tell one byte over from a thousand without
    // reading the constants.
    let limit = vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES;
    let request = request_at_bytes(limit + 1);
    let rule = request
        .validate()
        .expect_err("the byte bound must be refused");
    let error = HostRuntimeError::request_refusal(rule);
    assert_eq!(error.preflight_code(), "helper_request_bytes_invalid");
    assert_eq!(
        error.refusal_bound(),
        Some((Some(limit as u64), limit as u64 + 1))
    );
}

#[test]
fn a_per_argument_refusal_carries_the_element_length() {
    // Only the offending element's length crosses, never the element.
    let mut nul = start_request();
    nul.arguments = vec!["sha256:image".to_owned(), "run\0--flag".to_owned()];
    let rule = nul.validate().expect_err("a NUL argument must be refused");
    assert_eq!(
        HostRuntimeError::request_refusal(rule).refusal_bound(),
        Some((None, 10))
    );
}

#[test]
fn unencodable_requests_keep_the_document_cause() {
    let rule = vonk_agent_protocol::HostRuntimeRequestRule::Encoding;
    assert_eq!(
        HostRuntimeError::HelperProtocol(HelperProtocolCause::from_request_rule(rule))
            .preflight_code(),
        "helper_request_document_invalid"
    );
}

#[test]
fn request_storage_refusal_names_the_signed_request_file() {
    // Wrong implementation: a request root or signed request file that
    // violated the owner-only storage contract collapsed into
    // `helper_protocol_invalid`, which runs on every Start before the
    // helper call.
    let temp = tempfile::tempdir().unwrap();
    let permissive = temp.path().join("permissive");
    fs::create_dir(&permissive).unwrap();
    fs::set_permissions(&permissive, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(write_request(&permissive, &"a".repeat(64), b"{}").is_ok());
    let repaired = write_request(&permissive, &"a".repeat(64), b"{}").unwrap();
    assert_eq!(fs::read(repaired).unwrap(), b"{}");

    // Damaged request projections are replaced by the current signed bytes.
    let root = temp.path().join("requests");
    let path = write_request(&root, &"b".repeat(64), b"{}").unwrap();
    fs::write(&path, b"damaged").unwrap();
    assert_eq!(write_request(&root, &"b".repeat(64), b"{}").unwrap(), path);
    assert_eq!(fs::read(&path).unwrap(), b"{}");
}
