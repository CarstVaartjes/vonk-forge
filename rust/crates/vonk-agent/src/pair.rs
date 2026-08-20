use std::{
    fs,
    future::Future,
    io::{BufReader, Cursor},
    path::Path,
    time::Duration,
};

use rcgen::PublicKeyData;
use reqwest::{Certificate, Client, StatusCode};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use url::Url;
use x509_parser::{extensions::GeneralName, parse_x509_certificate, pem::parse_x509_pem};

use crate::{
    config::AgentConfig,
    identity::{
        IdentityMaterial, PendingIdentity, clear_pending, generate_pending, load_pending,
        persist_identity, persist_pending,
    },
};

const MAX_RESPONSE_BYTES: usize = 64 * 1024;
const MACHINE_EVIDENCE_PATH: &str = "/var/lib/vonk-forge-agent/machine-evidence";

#[derive(Debug, Error)]
pub enum PairingError {
    #[error("controller CA could not be read")]
    CaRead(#[from] std::io::Error),
    #[error("controller CA is invalid")]
    CaInvalid,
    #[error("controller CA fingerprint does not match the configured pin")]
    CaPin,
    #[error("pairing token is invalid")]
    Token,
    #[error("pairing request failed")]
    Transport(#[from] reqwest::Error),
    #[error("controller rejected pairing")]
    Rejected,
    #[error("timed out waiting for pairing approval")]
    ApprovalTimeout,
    #[error("controller pairing response is invalid")]
    Response,
    #[error("issued certificate is not bound to this node and key")]
    Certificate,
    #[error("local identity operation failed")]
    Identity(#[from] crate::identity::IdentityError),
}

#[derive(Debug, Clone, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EnrollmentEvidence {
    pub agent_digest: String,
    pub boot_id: String,
    pub csr_public_key_fingerprint: String,
    pub hardware_fingerprint: String,
    pub host_key_fingerprint: String,
    pub node_id: String,
}

#[derive(Serialize)]
#[serde(deny_unknown_fields)]
struct EnrollmentRequest<'a> {
    csr: &'a str,
    evidence: &'a EnrollmentEvidence,
    grant_token: &'a str,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct EnrollmentResponse {
    pub id: String,
    pub node_id: String,
    pub state: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IssuedResponse {
    pub node_id: String,
    pub certificate_pem: String,
    pub chain_pem: String,
    pub serial: String,
    pub fingerprint: String,
    pub not_before: String,
    pub not_after: String,
    pub generation: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EnrollmentOutcome {
    Pending(EnrollmentResponse),
    Issued,
}

pub async fn complete_pairing_with<F, Fut, O>(
    max_attempts: usize,
    retry_interval: Duration,
    mut attempt: F,
    mut observe_pending: O,
) -> Result<(), PairingError>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<EnrollmentOutcome, PairingError>>,
    O: FnMut(&EnrollmentResponse),
{
    for attempt_number in 0..max_attempts {
        match attempt().await? {
            EnrollmentOutcome::Issued => return Ok(()),
            EnrollmentOutcome::Pending(pending) => observe_pending(&pending),
        }
        if attempt_number + 1 < max_attempts {
            tokio::time::sleep(retry_interval).await;
        }
    }
    Err(PairingError::ApprovalTimeout)
}

pub async fn pair(
    config: &AgentConfig,
    enrollment: &Url,
    token: &str,
    ca_sha256: &str,
    evidence: EnrollmentEvidence,
) -> Result<EnrollmentOutcome, PairingError> {
    validate_token(token)?;
    if enrollment != &config.enrollment_url || ca_sha256 != config.ca_sha256 {
        return Err(PairingError::CaPin);
    }
    let ca_metadata = fs::symlink_metadata(&config.ca_path)?;
    if !ca_metadata.file_type().is_file()
        || ca_metadata.file_type().is_symlink()
        || ca_metadata.len() > MAX_RESPONSE_BYTES as u64
    {
        return Err(PairingError::CaInvalid);
    }
    let ca_pem = fs::read(&config.ca_path)?;
    verify_ca_pin(&ca_pem, ca_sha256)?;

    let credential_root = config.data_dir.join("credentials");
    let pending = match load_pending(&credential_root)? {
        Some(pending) => pending,
        None => {
            let pending = generate_pending(&config.node_id)?;
            persist_pending(&credential_root, &pending)?;
            pending
        }
    };
    let mut evidence = evidence;
    evidence.node_id.clone_from(&config.node_id);
    evidence
        .csr_public_key_fingerprint
        .clone_from(&pending.public_key_fingerprint);
    let client = Client::builder()
        .https_only(true)
        .tls_built_in_root_certs(false)
        .add_root_certificate(Certificate::from_pem(&ca_pem).map_err(|_| PairingError::CaInvalid)?)
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(30))
        .build()?;
    let csr = std::str::from_utf8(&pending.csr_pem).map_err(|_| PairingError::Response)?;
    let endpoint = enrollment
        .join("/agent/v1/enroll")
        .map_err(|_| PairingError::Response)?;
    let response = client
        .post(endpoint)
        .header("content-type", "application/json")
        .json(&EnrollmentRequest {
            csr,
            evidence: &evidence,
            grant_token: token,
        })
        .send()
        .await?;
    let status = response.status().as_u16();
    if response
        .content_length()
        .is_some_and(|length| length > MAX_RESPONSE_BYTES as u64)
    {
        return Err(PairingError::Response);
    }
    let body = response.bytes().await?;
    if body.len() > MAX_RESPONSE_BYTES {
        return Err(PairingError::Response);
    }
    let parsed = validate_enrollment_response(status, &body, &config.node_id)?;
    if status == StatusCode::OK.as_u16() {
        let issued: IssuedResponse =
            serde_json::from_slice(&body).map_err(|_| PairingError::Response)?;
        validate_issued(&issued, &pending, &config.node_id)?;
        persist_identity(
            &credential_root,
            &IdentityMaterial {
                node_id: issued.node_id,
                private_key_pem: pending.private_key_pem,
                certificate_pem: issued.certificate_pem.into_bytes(),
                chain_pem: issued.chain_pem.into_bytes(),
                serial: issued.serial,
                fingerprint: issued.fingerprint,
                generation: issued.generation,
            },
        )?;
        clear_pending(&credential_root)?;
    }
    Ok(parsed)
}

pub fn validate_enrollment_response(
    status: u16,
    body: &[u8],
    node_id: &str,
) -> Result<EnrollmentOutcome, PairingError> {
    match status {
        202 => {
            let pending: EnrollmentResponse =
                serde_json::from_slice(body).map_err(|_| PairingError::Response)?;
            if pending.node_id != node_id
                || !matches!(pending.state.as_str(), "pending-approval" | "issuing")
                || pending.id.is_empty()
            {
                return Err(PairingError::Response);
            }
            Ok(EnrollmentOutcome::Pending(pending))
        }
        200 => {
            let issued: IssuedResponse =
                serde_json::from_slice(body).map_err(|_| PairingError::Response)?;
            if issued.node_id != node_id
                || issued.generation == 0
                || issued.serial.is_empty()
                || issued.fingerprint.len() != 64
                || issued.not_before.is_empty()
                || issued.not_after.is_empty()
            {
                return Err(PairingError::Response);
            }
            Ok(EnrollmentOutcome::Issued)
        }
        401 | 403 | 409 | 410 => Err(PairingError::Rejected),
        _ => Err(PairingError::Response),
    }
}

pub fn verify_ca_pin(ca_pem: &[u8], expected: &str) -> Result<(), PairingError> {
    if expected.len() != 64
        || !expected
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(PairingError::CaPin);
    }
    let mut reader = BufReader::new(Cursor::new(ca_pem));
    let certificate = rustls_pemfile::certs(&mut reader)
        .next()
        .ok_or(PairingError::CaInvalid)?
        .map_err(|_| PairingError::CaInvalid)?;
    if rustls_pemfile::certs(&mut reader).next().is_some() {
        return Err(PairingError::CaInvalid);
    }
    if hex::encode(Sha256::digest(certificate.as_ref())) != expected {
        return Err(PairingError::CaPin);
    }
    Ok(())
}

pub fn collect_evidence(agent_path: &Path) -> Result<EnrollmentEvidence, PairingError> {
    collect_evidence_from(
        agent_path,
        Path::new("/etc/machine-id"),
        Path::new("/proc/sys/kernel/random/boot_id"),
        Path::new(MACHINE_EVIDENCE_PATH),
    )
}

fn collect_evidence_from(
    agent_path: &Path,
    machine_path: &Path,
    boot_path: &Path,
    native_evidence_path: &Path,
) -> Result<EnrollmentEvidence, PairingError> {
    let machine = bounded_file(machine_path)?;
    let native_evidence = bounded_file(native_evidence_path)?;
    if native_evidence.len() != 64
        || !native_evidence
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(PairingError::Response);
    }
    Ok(EnrollmentEvidence {
        agent_digest: hex::encode(Sha256::digest(fs::read(agent_path)?)),
        boot_id: bounded_file(boot_path)?,
        csr_public_key_fingerprint: String::new(),
        hardware_fingerprint: hex::encode(Sha256::digest(machine.as_bytes())),
        host_key_fingerprint: hex::encode(Sha256::digest(native_evidence.as_bytes())),
        node_id: String::new(),
    })
}

fn validate_token(token: &str) -> Result<(), PairingError> {
    if token.len() != 43
        || !token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    {
        return Err(PairingError::Token);
    }
    Ok(())
}

fn bounded_file(path: &Path) -> Result<String, PairingError> {
    let value = fs::read(path)?;
    if value.is_empty() || value.len() > 16 * 1024 {
        return Err(PairingError::Response);
    }
    String::from_utf8(value)
        .map(|value| value.trim().to_owned())
        .map_err(|_| PairingError::Response)
}

pub fn validate_issued(
    issued: &IssuedResponse,
    pending: &PendingIdentity,
    node_id: &str,
) -> Result<(), PairingError> {
    let (_, pem) =
        parse_x509_pem(issued.certificate_pem.as_bytes()).map_err(|_| PairingError::Certificate)?;
    let (_, certificate) =
        parse_x509_certificate(&pem.contents).map_err(|_| PairingError::Certificate)?;
    let common_name_matches = certificate
        .subject()
        .iter_common_name()
        .any(|name| name.as_str().is_ok_and(|value| value == node_id));
    let expected_uri = format!("spiffe://vonk-forge.local/node/{node_id}");
    let san_matches = certificate
        .subject_alternative_name()
        .map_err(|_| PairingError::Certificate)?
        .is_some_and(|extension| {
            extension
                .value
                .general_names
                .iter()
                .any(|name| matches!(name, GeneralName::URI(value) if *value == expected_uri))
        });
    let key = rcgen::KeyPair::from_pem(
        std::str::from_utf8(&pending.private_key_pem).map_err(|_| PairingError::Certificate)?,
    )
    .map_err(|_| PairingError::Certificate)?;
    let fingerprint = hex::encode(Sha256::digest(&pem.contents));
    if !common_name_matches
        || !san_matches
        || certificate.public_key().raw != key.subject_public_key_info()
        || fingerprint != issued.fingerprint
    {
        return Err(PairingError::Certificate);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn enrollment_evidence_uses_native_machine_evidence_without_ssh() {
        let directory = tempfile::tempdir().unwrap();
        let agent = directory.path().join("vonk-agent");
        let machine = directory.path().join("machine-id");
        let boot = directory.path().join("boot-id");
        let native = directory.path().join("machine-evidence");
        fs::write(&agent, b"agent bytes").unwrap();
        fs::write(&machine, b"machine-id\n").unwrap();
        fs::write(&boot, b"boot-id\n").unwrap();
        fs::write(
            &native,
            b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n",
        )
        .unwrap();

        let evidence = collect_evidence_from(&agent, &machine, &boot, &native).unwrap();

        assert_eq!(evidence.boot_id, "boot-id");
        assert_eq!(
            evidence.hardware_fingerprint,
            hex::encode(Sha256::digest(b"machine-id"))
        );
        assert_eq!(
            evidence.host_key_fingerprint,
            hex::encode(Sha256::digest(
                b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            ))
        );
    }
}
