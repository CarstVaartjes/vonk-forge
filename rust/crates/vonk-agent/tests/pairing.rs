#![forbid(unsafe_code)]

use std::{fs, os::unix::fs::PermissionsExt, path::Path};

use chrono::{TimeZone, Utc};
use rcgen::string::Ia5String;
use rcgen::{CertificateParams, DistinguishedName, DnType, KeyPair, SanType};
use sha2::{Digest, Sha256};
use tempfile::tempdir;
use url::Url;
use vonk_agent::{
    config::AgentConfig,
    identity::{
        IdentityMaterial, active_identity_paths, generate_pending, load_pending, persist_identity,
        persist_pending, publish_staged, renewal_due, stage_identity, staged_identity_paths,
    },
    pair::{
        EnrollmentEvidence, EnrollmentOutcome, EnrollmentResponse, IssuedResponse, PairingError,
        pair, validate_enrollment_response, validate_issued,
    },
};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";
const COMMON: &str = r#"ca_path = "/etc/vonk-forge-agent/controller-ca.pem"
ca_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
data_dir = "/var/lib/vonk-forge"
node_id = "spk_0123456789abcdef0123456789abcdef"
poll_min_seconds = 2
poll_max_seconds = 60
"#;

fn legacy_config(data_dir: &Path) -> AgentConfig {
    AgentConfig::parse(&format!(
        "controller_url = \"https://agents.vonk.test/\"\nca_path = \"/etc/vonk-forge-agent/controller-ca.pem\"\nca_sha256 = \"{}\"\ndata_dir = \"{}\"\nnode_id = \"{NODE_ID}\"\npoll_min_seconds = 2\npoll_max_seconds = 60\n",
        "a".repeat(64),
        data_dir.display(),
    ))
    .unwrap()
}

#[test]
fn config_is_strict_and_rejects_secret_fields() {
    let document = format!(
        "enrollment_url = \"https://enroll.vonk.test/\"\ncontroller_url = \"https://agents.vonk.test/\"\n{COMMON}token = \"must-not-be-configurable\"\n"
    );
    assert!(AgentConfig::parse(&document).is_err());
}

#[test]
fn config_parses_distinct_enrollment_and_controller_origins() {
    let config = AgentConfig::parse(&format!(
        "enrollment_url = \"https://enroll.vonk.test/\"\ncontroller_url = \"https://agents.vonk.test/\"\n{COMMON}"
    ))
    .unwrap();

    assert_eq!(
        config.enrollment_url.unwrap().as_str(),
        "https://enroll.vonk.test/"
    );
    assert_eq!(config.controller_url.as_str(), "https://agents.vonk.test/");
}

#[test]
fn config_rejects_unsafe_enrollment_and_controller_origins() {
    for origin in [
        "http://agents.vonk.test/",
        "https://user@agents.vonk.test/",
        "https://agents.vonk.test/enroll",
        "https://agents.vonk.test/?target=pair",
        "https://agents.vonk.test/#pair",
    ] {
        let enrollment = AgentConfig::parse(&format!(
            "enrollment_url = \"{origin}\"\ncontroller_url = \"https://agents.vonk.test/\"\n{COMMON}"
        ));
        assert!(
            enrollment
                .unwrap_err()
                .to_string()
                .contains("enrollment_url")
        );

        let controller = AgentConfig::parse(&format!(
            "enrollment_url = \"https://enroll.vonk.test/\"\ncontroller_url = \"{origin}\"\n{COMMON}"
        ));
        assert!(
            controller
                .unwrap_err()
                .to_string()
                .contains("controller_url")
        );
    }
}

#[test]
fn identity_is_persisted_atomically_with_private_modes() {
    let directory = tempdir().unwrap();
    let material = IdentityMaterial {
        node_id: NODE_ID.to_owned(),
        private_key_pem: b"PRIVATE".to_vec(),
        certificate_pem: b"CERTIFICATE".to_vec(),
        chain_pem: b"CHAIN".to_vec(),
        serial: "42".to_owned(),
        fingerprint: "b".repeat(64),
        generation: 1,
    };

    persist_identity(directory.path(), &material).unwrap();

    for name in [
        "private-key.pem",
        "certificate.pem",
        "chain.pem",
        "identity.json",
    ] {
        let metadata = fs::metadata(directory.path().join(name)).unwrap();
        assert_eq!(metadata.permissions().mode() & 0o777, 0o600, "{name}");
    }
    assert_eq!(
        fs::read(directory.path().join("private-key.pem")).unwrap(),
        b"PRIVATE"
    );
}

#[test]
fn pending_identity_is_reused_for_approval_pickup() {
    let directory = tempdir().unwrap();
    let pending = generate_pending(NODE_ID).unwrap();
    persist_pending(directory.path(), &pending).unwrap();

    let recovered = load_pending(directory.path()).unwrap().unwrap();

    assert_eq!(recovered.private_key_pem, pending.private_key_pem);
    assert_eq!(recovered.csr_pem, pending.csr_pem);
    assert_eq!(
        recovered.public_key_fingerprint,
        pending.public_key_fingerprint
    );
}

#[test]
fn expired_or_reused_token_responses_fail_closed() {
    for status in [401, 403, 409, 410] {
        let error =
            validate_enrollment_response(status, b"{\"detail\":\"denied\"}", NODE_ID).unwrap_err();
        assert!(error.to_string().contains("rejected"));
    }
}

#[test]
fn pending_enrollment_never_publishes_credentials() {
    let outcome = validate_enrollment_response(
        202,
        br#"{"id":"2a73f0fe-ecaa-4ce7-a840-35fcb488f63e","node_id":"spk_0123456789abcdef0123456789abcdef","state":"pending-approval"}"#,
        NODE_ID,
    )
    .unwrap();
    assert_eq!(
        outcome,
        EnrollmentOutcome::Pending(EnrollmentResponse {
            id: "2a73f0fe-ecaa-4ce7-a840-35fcb488f63e".to_owned(),
            node_id: NODE_ID.to_owned(),
            state: "pending-approval".to_owned(),
        })
    );
}

#[tokio::test]
async fn pairing_rejects_controller_url_before_any_identity_material_is_written() {
    let directory = tempdir().unwrap();
    let ca_key = KeyPair::generate().unwrap();
    let ca = CertificateParams::new(vec!["controller.vonkforge.test".to_owned()])
        .unwrap()
        .self_signed(&ca_key)
        .unwrap();
    let ca_path = directory.path().join("ca.pem");
    fs::write(&ca_path, ca.pem()).unwrap();
    let data_dir = directory.path().join("state");
    let config = AgentConfig {
        enrollment_url: Some(Url::parse("https://enroll.vonkforge.test/").unwrap()),
        controller_url: Url::parse("https://127.0.0.1:1/").unwrap(),
        ca_path,
        ca_sha256: hex::encode(Sha256::digest(ca.der())),
        data_dir: data_dir.clone(),
        node_id: NODE_ID.to_owned(),
        poll_min_seconds: 2,
        poll_max_seconds: 60,
        fabric_address: None,
        fabric_bandwidth_mbps: None,
        huggingface_curl_config: None,
    };
    let evidence = EnrollmentEvidence {
        agent_digest: "a".repeat(64),
        boot_id: "boot".to_owned(),
        csr_public_key_fingerprint: String::new(),
        hardware_fingerprint: "hardware".to_owned(),
        host_key_fingerprint: "host".to_owned(),
        node_id: String::new(),
    };

    let result = pair(
        &config,
        &config.controller_url,
        &"t".repeat(43),
        &config.ca_sha256,
        evidence,
    )
    .await;

    assert!(matches!(result, Err(PairingError::CaPin)));
    assert!(!data_dir.exists());
}

#[tokio::test]
async fn pairing_refuses_legacy_config_before_identity_or_network_mutation() {
    let directory = tempdir().unwrap();
    let data_dir = directory.path().join("state");
    let config = legacy_config(&data_dir);
    let evidence = EnrollmentEvidence {
        agent_digest: "a".repeat(64),
        boot_id: "boot".to_owned(),
        csr_public_key_fingerprint: String::new(),
        hardware_fingerprint: "hardware".to_owned(),
        host_key_fingerprint: "host".to_owned(),
        node_id: String::new(),
    };

    let result = pair(
        &config,
        &Url::parse("https://enroll.vonk.test/").unwrap(),
        &"t".repeat(43),
        &config.ca_sha256,
        evidence,
    )
    .await;

    assert_eq!(
        result.unwrap_err().to_string(),
        "agent configuration has no enrollment URL"
    );
    assert!(!data_dir.exists());
}

#[test]
fn issued_certificate_must_bind_the_generated_key_and_node_identity() {
    let pending = generate_pending(NODE_ID).unwrap();
    let key = KeyPair::from_pem(std::str::from_utf8(&pending.private_key_pem).unwrap()).unwrap();
    let mut parameters = CertificateParams::default();
    let mut subject = DistinguishedName::new();
    subject.push(DnType::CommonName, NODE_ID);
    parameters.distinguished_name = subject;
    parameters.subject_alt_names = vec![SanType::URI(
        Ia5String::try_from(format!("spiffe://vonk-forge.local/node/{NODE_ID}")).unwrap(),
    )];
    let certificate = parameters.self_signed(&key).unwrap();
    let response = IssuedResponse {
        node_id: NODE_ID.to_owned(),
        certificate_pem: certificate.pem(),
        chain_pem: certificate.pem(),
        serial: "42".to_owned(),
        fingerprint: hex::encode(Sha256::digest(certificate.der())),
        not_before: "2026-08-07T00:00:00+00:00".to_owned(),
        not_after: "2026-08-08T00:00:00+00:00".to_owned(),
        generation: 1,
    };

    validate_issued(&response, &pending, NODE_ID).unwrap();
    assert!(validate_issued(&response, &pending, "spk_ffffffffffffffffffffffffffffffff").is_err());
}

#[test]
fn staged_generation_survives_restart_and_publishes_with_one_pointer_write() {
    let directory = tempdir().unwrap();
    let active = IdentityMaterial {
        node_id: NODE_ID.to_owned(),
        private_key_pem: b"ACTIVE-PRIVATE".to_vec(),
        certificate_pem: b"ACTIVE-CERTIFICATE".to_vec(),
        chain_pem: b"ACTIVE-CHAIN".to_vec(),
        serial: "1".to_owned(),
        fingerprint: "a".repeat(64),
        generation: 1,
    };
    persist_identity(directory.path(), &active).unwrap();
    let staged = IdentityMaterial {
        node_id: NODE_ID.to_owned(),
        private_key_pem: b"STAGED-PRIVATE".to_vec(),
        certificate_pem: b"STAGED-CERTIFICATE".to_vec(),
        chain_pem: b"STAGED-CHAIN".to_vec(),
        serial: "2".to_owned(),
        fingerprint: "b".repeat(64),
        generation: 2,
    };

    stage_identity(directory.path(), &staged).unwrap();
    let (generation, staged_paths) = staged_identity_paths(directory.path()).unwrap().unwrap();
    assert_eq!(generation, 2);
    assert_eq!(
        fs::read(&staged_paths.private_key).unwrap(),
        b"STAGED-PRIVATE"
    );
    assert_eq!(
        fs::read(active_identity_paths(directory.path()).unwrap().private_key).unwrap(),
        b"ACTIVE-PRIVATE"
    );

    publish_staged(directory.path(), generation).unwrap();
    assert!(staged_identity_paths(directory.path()).unwrap().is_none());
    assert_eq!(
        fs::read(active_identity_paths(directory.path()).unwrap().private_key).unwrap(),
        b"STAGED-PRIVATE"
    );
}

#[test]
fn renewal_due_is_derived_from_active_certificate_validity() {
    let directory = tempdir().unwrap();
    let key = KeyPair::generate().unwrap();
    let certificate = CertificateParams::new(vec!["agent.vonkforge.test".to_owned()])
        .unwrap()
        .self_signed(&key)
        .unwrap();
    persist_identity(
        directory.path(),
        &IdentityMaterial {
            node_id: NODE_ID.to_owned(),
            private_key_pem: key.serialize_pem().into_bytes(),
            certificate_pem: certificate.pem().into_bytes(),
            chain_pem: certificate.pem().into_bytes(),
            serial: "1".to_owned(),
            fingerprint: hex::encode(Sha256::digest(certificate.der())),
            generation: 1,
        },
    )
    .unwrap();

    assert!(
        renewal_due(
            directory.path(),
            Utc.with_ymd_and_hms(4095, 1, 1, 0, 0, 0).unwrap()
        )
        .unwrap()
    );
}
