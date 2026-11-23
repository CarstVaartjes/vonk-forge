use std::{
    fs,
    io::{BufReader, Cursor},
    path::Path,
    time::Duration,
};

use rcgen::PublicKeyData;
use reqwest::{Certificate, Client};
use sha2::{Digest, Sha256};
use thiserror::Error;
use url::Url;
use x509_parser::{extensions::GeneralName, parse_x509_certificate, pem::parse_x509_pem};

use vonk_agent_protocol::canonical_generated_json;
pub use vonk_agent_protocol::generated::{
    EnrollmentEvidence, EnrollmentSubmitRequest, IssuedCertificateResponse,
};

use crate::{
    client::{ClientError, ControllerError, Disposition},
    config::AgentConfig,
    identity::{IdentityMaterial, PendingIdentity, persist_paired_identity, prepare_pending},
};

const MAX_RESPONSE_BYTES: usize = 64 * 1024;
const PAIRING_TIMEOUT: Duration = Duration::from_secs(10 * 60);
const MACHINE_EVIDENCE_PATH: &str = "/var/lib/vonk-forge-agent/machine-evidence";

#[derive(Debug, Error)]
pub enum PairingError {
    #[error("pairing observation ended without verified certificate content")]
    ObservationEnded,
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
    #[error("controller pairing response is invalid")]
    Response,
    #[error(
        "controller pairing response exceeds {maximum_bytes} bytes (observed {observed_bytes})"
    )]
    ResponseTooLarge {
        maximum_bytes: usize,
        observed_bytes: u64,
    },
    #[error("controller pairing returned unexpected HTTP status {0}")]
    Status(u16),
    #[error("issued certificate is not bound to this node and key")]
    Certificate,
    #[error("local identity operation failed")]
    Identity(#[from] crate::identity::IdentityError),
}

pub async fn pair(
    config: &AgentConfig,
    enrollment: &Url,
    token: &str,
    ca_sha256: &str,
    evidence: EnrollmentEvidence,
) -> Result<(), PairingError> {
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
    let pending = prepare_pending(&credential_root, &config.node_id)?;
    let mut evidence = evidence;
    evidence.node_id.clone_from(&config.node_id);
    evidence
        .csr_public_key_fingerprint
        .clone_from(&pending.public_key_fingerprint);
    let client = Client::builder()
        .https_only(true)
        .tls_certs_only([Certificate::from_pem(&ca_pem).map_err(|_| PairingError::CaInvalid)?])
        .connect_timeout(Duration::from_secs(10))
        .build()?;
    let csr = std::str::from_utf8(&pending.csr_pem).map_err(|_| PairingError::Response)?;
    let endpoint = enrollment
        .join("/agent/enroll")
        .map_err(|_| PairingError::Response)?;
    let request = EnrollmentSubmitRequest {
        csr: csr.to_owned(),
        evidence,
        grant_token: token.to_owned(),
    };
    let body = canonical_generated_json(&request).map_err(|_| PairingError::Response)?;
    let issued = observe_enrollment(&client, &endpoint, body, &config.node_id).await?;
    validate_issued(&issued, &pending, &config.node_id)?;
    persist_paired_identity(
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
    Ok(())
}

// Reconnect with the identical token and durable CSR. The deadline covers
// requests, body reads and retry sleeps, including a stalled final response.
async fn observe_enrollment(
    client: &Client,
    endpoint: &Url,
    body: Vec<u8>,
    node_id: &str,
) -> Result<IssuedCertificateResponse, PairingError> {
    let deadline = tokio::time::Instant::now() + PAIRING_TIMEOUT;
    tokio::time::timeout_at(deadline, async {
        let mut attempt = 0_u32;
        let mut delay = Duration::ZERO;
        while tokio::time::Instant::now() < deadline {
            tokio::time::sleep(delay).await;
            delay = ClientError::Retryable.retry_delay(
                attempt,
                Duration::from_secs(1),
                Duration::from_secs(60),
            );
            attempt = attempt.saturating_add(1);
            // One enrollment exchange: a small request and response.
            let response = match client
                .post(endpoint.clone())
                .timeout(Duration::from_secs(30))
                .header("content-type", "application/json")
                .body(body.clone())
                .send()
                .await
            {
                Ok(response) => response,
                Err(_) => continue,
            };
            let status = response.status().as_u16();
            let verdict = if status == 200 {
                None
            } else {
                Some(crate::client::http_status_disposition(status))
            };
            if verdict == Some(Disposition::Wait) {
                let mut error = ControllerError::from_status(status);
                error.retry_after_seconds = response
                    .headers()
                    .get(reqwest::header::RETRY_AFTER)
                    .and_then(|value| value.to_str().ok())
                    .and_then(|value| value.parse::<u64>().ok())
                    .map(|seconds| seconds.min(60) as u32);
                delay = ClientError::Controller(Box::new(error)).retry_delay(
                    attempt - 1,
                    Duration::from_secs(1),
                    Duration::from_secs(60),
                );
                continue;
            }
            match verdict {
                Some(Disposition::Security) => return Err(PairingError::Rejected),
                Some(Disposition::Fail) => return Err(PairingError::Status(status)),
                Some(_) => continue,
                None => {}
            }
            let observed = match bounded_pairing_body(response).await {
                Ok(observed) => observed,
                Err(_) => continue,
            };
            match validate_enrollment_response(status, &observed, node_id) {
                Err(PairingError::Response) => continue,
                result => return result,
            }
        }
        Err(PairingError::ObservationEnded)
    })
    .await
    .map_err(|_| PairingError::ObservationEnded)?
}

async fn bounded_pairing_body(mut response: reqwest::Response) -> Result<Vec<u8>, PairingError> {
    if let Some(length) = response.content_length()
        && length > MAX_RESPONSE_BYTES as u64
    {
        return Err(PairingError::ResponseTooLarge {
            maximum_bytes: MAX_RESPONSE_BYTES,
            observed_bytes: length,
        });
    }
    // Reserve this physical body budget once. Geometric Vec growth from an
    // arbitrary first chunk could otherwise reserve beyond the byte ceiling.
    let mut body = Vec::with_capacity(MAX_RESPONSE_BYTES);
    while let Some(chunk) = response.chunk().await? {
        // The declared length is only an early refusal. Check actual bytes
        // before growing the retained body, including chunked responses.
        if chunk.len() > MAX_RESPONSE_BYTES - body.len() {
            return Err(PairingError::ResponseTooLarge {
                maximum_bytes: MAX_RESPONSE_BYTES,
                observed_bytes: (body.len() as u64).saturating_add(chunk.len() as u64),
            });
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

pub fn validate_enrollment_response(
    status: u16,
    body: &[u8],
    node_id: &str,
) -> Result<IssuedCertificateResponse, PairingError> {
    match status {
        200 => {
            let issued: IssuedCertificateResponse =
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
            Ok(issued)
        }
        401 | 403 => Err(PairingError::Rejected),
        _ => Err(PairingError::Status(status)),
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
    issued: &IssuedCertificateResponse,
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
mod tests;
