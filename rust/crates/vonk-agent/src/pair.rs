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
    config::AgentConfig,
    identity::{
        IdentityMaterial, PendingIdentity, generate_pending, load_pending, persist_paired_identity,
        persist_pending,
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
        .tls_certs_only([Certificate::from_pem(&ca_pem).map_err(|_| PairingError::CaInvalid)?])
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(30))
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
    let response = client
        .post(endpoint)
        .header("content-type", "application/json")
        .body(body)
        .send()
        .await?;
    let status = response.status().as_u16();
    let body = bounded_pairing_body(response).await?;
    let issued = validate_enrollment_response(status, &body, &config.node_id)?;
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
            generation: u64::from(issued.generation),
        },
    )?;
    Ok(())
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
        401 | 403 | 409 | 410 => Err(PairingError::Rejected),
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
mod tests {
    use super::*;

    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    // Keep the peer open after the supplied bytes. An oversized response must
    // be refused before EOF; a whole-body reader would wait for the timeout.
    async fn streaming_response(
        headers: &str,
        body: Vec<u8>,
    ) -> (reqwest::Response, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let headers = headers.to_owned();
        let peer = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = [0; 4096];
            socket.read(&mut request).await.unwrap();
            socket.write_all(headers.as_bytes()).await.unwrap();
            let _ = socket.write_all(&body).await;
            std::future::pending::<()>().await;
        });
        let response = Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .unwrap()
            .get(format!("http://{address}/agent/enroll"))
            .send()
            .await
            .unwrap();
        (response, peer)
    }

    #[tokio::test]
    async fn pairing_declared_oversize_refuses_before_body_arrives() {
        let headers = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n",
            MAX_RESPONSE_BYTES + 1
        );
        let (response, peer) = streaming_response(&headers, Vec::new()).await;
        let result =
            tokio::time::timeout(Duration::from_secs(1), bounded_pairing_body(response)).await;
        peer.abort();
        assert!(matches!(
            result,
            Ok(Err(PairingError::ResponseTooLarge {
                maximum_bytes: MAX_RESPONSE_BYTES,
                ..
            }))
        ));
    }

    #[tokio::test]
    async fn pairing_chunked_oversize_refuses_before_eof_and_recovers() {
        let headers = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n";
        let mut chunks = format!("{:x}\r\n", MAX_RESPONSE_BYTES).into_bytes();
        chunks.extend(vec![b' '; MAX_RESPONSE_BYTES]);
        chunks.extend_from_slice(b"\r\n1\r\nx\r\n");
        let (response, peer) = streaming_response(headers, chunks).await;
        let result =
            tokio::time::timeout(Duration::from_secs(1), bounded_pairing_body(response)).await;
        peer.abort();
        assert!(matches!(
            result,
            Ok(Err(PairingError::ResponseTooLarge {
                maximum_bytes: MAX_RESPONSE_BYTES,
                ..
            }))
        ));
        // A fresh response after the fault clears uses the same reader and
        // accepts the complete exact-boundary JSON without truncation.
        let node = "spk_0123456789abcdef0123456789abcdef";
        let mut body = serde_json::to_vec(&serde_json::json!({
            "node_id":node,"certificate_pem":"certificate","chain_pem":"chain",
            "serial":"123","fingerprint":"a".repeat(64),
            "not_before":"2026-10-07T00:00:00Z","not_after":"2026-11-06T00:00:00Z","generation":1
        }))
        .unwrap();
        body.resize(MAX_RESPONSE_BYTES, b' ');
        let mut chunks = format!("{:x}\r\n", body.len()).into_bytes();
        chunks.extend_from_slice(&body);
        chunks.extend_from_slice(b"\r\n0\r\n\r\n");
        let (response, peer) = streaming_response(headers, chunks).await;
        let recovered = bounded_pairing_body(response).await.unwrap();
        peer.abort();
        assert_eq!(recovered, body);
        assert!(recovered.capacity() <= MAX_RESPONSE_BYTES);
        assert_eq!(
            validate_enrollment_response(200, &recovered, node)
                .unwrap()
                .serial,
            "123"
        );
    }

    #[tokio::test]
    async fn pairing_reader_cancellation_releases_incomplete_response() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (closed, observed_close) = tokio::sync::oneshot::channel();
        let peer = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = [0; 4096];
            socket.read(&mut request).await.unwrap();
            socket
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n{")
                .await
                .unwrap();
            let mut byte = [0];
            let result = socket.read(&mut byte).await;
            let _ = closed.send(matches!(result, Ok(0)) || result.is_err());
        });
        let response = Client::new()
            .get(format!("http://{address}/agent/enroll"))
            .send()
            .await
            .unwrap();
        let reader = tokio::spawn(bounded_pairing_body(response));
        tokio::task::yield_now().await;
        reader.abort();
        assert!(reader.await.unwrap_err().is_cancelled());
        assert!(
            tokio::time::timeout(Duration::from_secs(1), observed_close)
                .await
                .unwrap()
                .unwrap()
        );
        peer.await.unwrap();
    }

    #[tokio::test]
    async fn pairing_stream_timeout_preserves_transport_cause() {
        let (response, peer) = streaming_response(
            "HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n",
            b"{ ".to_vec(),
        )
        .await;
        let result = bounded_pairing_body(response).await;
        peer.abort();
        match result {
            Err(PairingError::Transport(error)) => assert!(error.is_timeout()),
            other => panic!("stream timeout lost transport cause: {other:?}"),
        }
    }

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
