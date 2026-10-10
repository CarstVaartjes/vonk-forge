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
    client::{ClientError, ControllerError},
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
            let response = match client
                .post(endpoint.clone())
                .header("content-type", "application/json")
                .body(body.clone())
                .send()
                .await
            {
                Ok(response) => response,
                Err(_) => continue,
            };
            let status = response.status().as_u16();
            if matches!(status, 429 | 503) {
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
            if matches!(status, 401 | 403) {
                return Err(PairingError::Rejected);
            }
            if (400..500).contains(&status) {
                return Err(PairingError::Status(status));
            }
            if status != 200 {
                continue;
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
mod tests {
    use super::*;

    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    // Scripted HTTP peer checks replay bytes and elapsed time rather than
    // coupling the regression to a fixed number of attempts.
    async fn enrollment_peer(
        replies: Vec<(Duration, String)>,
        expected_body: Vec<u8>,
        stalled_body: bool,
    ) -> (Url, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let endpoint = Url::parse(&format!(
            "http://{}/agent/enroll",
            listener.local_addr().unwrap()
        ))
        .unwrap();
        let started = tokio::time::Instant::now();
        let peer = tokio::spawn(async move {
            for (elapsed, reply) in replies {
                // Paused time must advance for backoff, but not race real TCP
                // readiness. Keep the runtime runnable only during I/O.
                let accepting = listener.accept();
                tokio::pin!(accepting);
                let early = tokio::select! {
                    result = &mut accepting => Some(result),
                    _ = tokio::time::sleep_until(started + elapsed) => None,
                };
                let io_deadline = std::time::Instant::now() + Duration::from_secs(5);
                let io_clock_guard =
                    tokio_util::task::AbortOnDropHandle::new(tokio::spawn(async move {
                        while std::time::Instant::now() < io_deadline {
                            tokio::task::yield_now().await;
                        }
                    }));
                let (mut socket, _) = match early {
                    Some(result) => result.unwrap(),
                    None => accepting.await.unwrap(),
                };
                assert_eq!(tokio::time::Instant::now() - started, elapsed);
                let mut headers = Vec::new();
                while !headers.ends_with(b"\r\n\r\n") && std::time::Instant::now() < io_deadline {
                    headers.push(socket.read_u8().await.unwrap());
                }
                assert!(headers.ends_with(b"\r\n\r\n"));
                let headers = std::str::from_utf8(&headers).unwrap();
                assert!(headers.starts_with("POST /agent/enroll "));
                let length = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().unwrap())
                    })
                    .unwrap();
                let mut body = vec![0; length];
                socket.read_exact(&mut body).await.unwrap();
                assert_eq!(
                    body, expected_body,
                    "replay must preserve the grant and CSR"
                );
                socket.write_all(reply.as_bytes()).await.unwrap();
                // Keep an incomplete body open until observation releases it.
                if stalled_body {
                    io_clock_guard.abort();
                }
                let mut byte = [0];
                assert_eq!(socket.read(&mut byte).await.unwrap(), 0);
                io_clock_guard.abort();
            }
        });
        (endpoint, peer)
    }

    #[tokio::test(start_paused = true)]
    async fn pairing_waits_for_issuance_and_preserves_replay() {
        // Catches the old seven-second observation limit, ignored 503 delays,
        // and added backoff on top of Retry-After. Also exercises 429 and cap.
        let node = "spk_0123456789abcdef0123456789abcdef";
        let directory = tempfile::tempdir().unwrap();
        let pending = prepare_pending(directory.path(), node).unwrap();
        let key = rcgen::KeyPair::from_pem(std::str::from_utf8(&pending.private_key_pem).unwrap())
            .unwrap();
        let mut parameters = rcgen::CertificateParams::default();
        parameters
            .distinguished_name
            .push(rcgen::DnType::CommonName, node);
        parameters.subject_alt_names = vec![rcgen::SanType::URI(
            format!("spiffe://vonk-forge.local/node/{node}")
                .try_into()
                .unwrap(),
        )];
        let certificate = parameters.self_signed(&key).unwrap();
        let issued = IssuedCertificateResponse {
            node_id: node.to_owned(),
            certificate_pem: certificate.pem(),
            chain_pem: certificate.pem(),
            serial: "42".to_owned(),
            fingerprint: hex::encode(Sha256::digest(certificate.der())),
            not_before: "2026-10-10T00:00:00Z".to_owned(),
            not_after: "2026-11-10T00:00:00Z".to_owned(),
            generation: 1,
        };
        let body = canonical_generated_json(&EnrollmentSubmitRequest {
            csr: std::str::from_utf8(&pending.csr_pem).unwrap().to_owned(),
            grant_token: "a".repeat(43),
            evidence: EnrollmentEvidence {
                node_id: node.to_owned(),
                csr_public_key_fingerprint: pending.public_key_fingerprint.clone(),
                agent_digest: "a".repeat(64),
                boot_id: "boot".to_owned(),
                hardware_fingerprint: "b".repeat(64),
                host_key_fingerprint: "c".repeat(64),
            },
        })
        .unwrap();
        let issued_body = String::from_utf8(canonical_generated_json(&issued).unwrap()).unwrap();
        let waiting = "HTTP/1.1 503 Unavailable\r\nRetry-After: 60\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
        let (endpoint, peer) = enrollment_peer(vec![
            (Duration::ZERO, waiting.to_owned()),
            (Duration::from_secs(60), waiting.to_owned()),
            (Duration::from_secs(120), "HTTP/1.1 429 Limited\r\nRetry-After: 9999999999\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_owned()),
            (Duration::from_secs(180), format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{issued_body}", issued_body.len())),
        ], body.clone(), false).await;
        let client = Client::builder().no_proxy().build().unwrap();
        let observed = observe_enrollment(&client, &endpoint, body, node)
            .await
            .unwrap();
        peer.await.unwrap();
        validate_issued(&observed, &pending, node).unwrap();
        assert_eq!(observed.fingerprint, issued.fingerprint);
    }

    #[tokio::test(start_paused = true)]
    async fn pairing_client_refusals_end_without_reading_or_retrying() {
        // An incomplete refusal body must never obscure a final status or
        // turn a security refusal into a transport retry.
        for status in [401, 403, 422, 400, 404, 408, 409] {
            let body = b"{}".to_vec();
            let (endpoint, peer) = enrollment_peer(vec![(Duration::ZERO,
                format!("HTTP/1.1 {status} Refused\r\nContent-Length: 100\r\nConnection: close\r\n\r\n"),
            )], body.clone(), false).await;
            let started = tokio::time::Instant::now();
            let client = Client::builder().no_proxy().build().unwrap();
            let error = observe_enrollment(&client, &endpoint, body, "node")
                .await
                .unwrap_err();
            assert!(matches!(
                (status, error),
                (401 | 403, PairingError::Rejected) | (_, PairingError::Status(_))
            ));
            assert_eq!(tokio::time::Instant::now(), started);
            peer.await.unwrap();
        }
    }

    #[tokio::test(start_paused = true)]
    async fn pairing_deadline_ends_repeated_controller_waits() {
        // Retry-After cannot extend the observation window indefinitely.
        let body = b"{}".to_vec();
        let replies = (0..PAIRING_TIMEOUT.as_secs()).step_by(60).map(|seconds| (
            Duration::from_secs(seconds),
            "HTTP/1.1 503 Unavailable\r\nRetry-After: 60\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_owned(),
        )).collect();
        let (endpoint, peer) = enrollment_peer(replies, body.clone(), false).await;
        let started = tokio::time::Instant::now();
        let client = Client::builder().no_proxy().build().unwrap();
        assert!(matches!(
            observe_enrollment(&client, &endpoint, body, "node").await,
            Err(PairingError::ObservationEnded)
        ));
        assert_eq!(tokio::time::Instant::now() - started, PAIRING_TIMEOUT);
        peer.await.unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn pairing_deadline_covers_a_stalled_response() {
        // A final request/body may not outlive the whole observation deadline.
        let body = b"{}".to_vec();
        let (endpoint, peer) = enrollment_peer(
            vec![(
                Duration::ZERO,
                "HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n".to_owned(),
            )],
            body.clone(),
            true,
        )
        .await;
        let started = tokio::time::Instant::now();
        let client = Client::builder().no_proxy().build().unwrap();
        assert!(matches!(
            observe_enrollment(&client, &endpoint, body, "node").await,
            Err(PairingError::ObservationEnded)
        ));
        assert_eq!(tokio::time::Instant::now() - started, PAIRING_TIMEOUT);
        peer.await.unwrap();
    }

    async fn read_request_headers(socket: &mut tokio::net::TcpStream) {
        let mut request = [0; 4096];
        let mut received = 0;
        // The controlled streaming peer uses this same two-second transport
        // budget; header reads cannot outlive that request.
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        while !request[..received].ends_with(b"\r\n\r\n") {
            assert!(
                received < request.len(),
                "test request headers exceed allocation"
            );
            let amount = tokio::time::timeout_at(deadline, socket.read(&mut request[received..]))
                .await
                .expect("test request header deadline")
                .unwrap();
            assert!(amount > 0, "test request ended before complete headers");
            received += amount;
        }
        assert!(request[..received].starts_with(b"GET "));
    }

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
            read_request_headers(&mut socket).await;
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
        assert!(result.unwrap().is_err());
        let (response, peer) = streaming_response(
            "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n",
            b"{}".to_vec(),
        )
        .await;
        assert_eq!(bounded_pairing_body(response).await.unwrap(), b"{}");
        peer.abort();
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
        assert!(result.unwrap().is_err());
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
            read_request_headers(&mut socket).await;
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
    async fn pairing_stream_timeout_ends_and_a_fresh_reply_is_read() {
        let (response, peer) = streaming_response(
            "HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n",
            b"{ ".to_vec(),
        )
        .await;
        assert!(
            tokio::time::timeout(Duration::from_secs(3), bounded_pairing_body(response))
                .await
                .unwrap()
                .is_err()
        );
        peer.abort();
        let (response, peer) = streaming_response(
            "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n",
            b"{}".to_vec(),
        )
        .await;
        assert_eq!(bounded_pairing_body(response).await.unwrap(), b"{}");
        peer.abort();
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
