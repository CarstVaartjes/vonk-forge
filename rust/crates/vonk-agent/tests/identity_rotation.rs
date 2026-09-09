#![forbid(unsafe_code)]

use std::{
    fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

use rcgen::string::Ia5String;
use rcgen::{
    BasicConstraints, Certificate, CertificateParams, CertificateSigningRequestParams,
    DistinguishedName, DnType, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair, KeyUsagePurpose,
    PKCS_ED25519, SanType,
};
use rustls::{
    RootCertStore, ServerConfig, ServerConnection, StreamOwned, server::WebPkiClientVerifier,
};
use sha2::{Digest, Sha256};
use tempfile::tempdir;
use time::OffsetDateTime;
use url::Url;
use vonk_agent::{
    client::AgentHttpClient,
    config::AgentConfig,
    identity::{
        IdentityMaterial, active_identity_paths, generate_pending, load_pending,
        persist_paired_identity, persist_pending, publish_staged, stage_identity,
        staged_identity_paths,
    },
    pair::IssuedCertificateResponse,
    rotation::rotate_if_due,
};
use vonk_agent_protocol::{
    canonical_generated_json,
    generated::{ActivateRequest, RenewRequest},
};

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

fn client_material(
    issuer: &Issuer<'_, KeyPair>,
    ca: &Certificate,
    generation: u64,
) -> IdentityMaterial {
    let key = KeyPair::generate_for(&PKCS_ED25519).unwrap();
    let mut params = CertificateParams::default();
    let mut subject = DistinguishedName::new();
    subject.push(DnType::CommonName, NODE_ID);
    params.distinguished_name = subject;
    params.subject_alt_names = vec![SanType::URI(
        Ia5String::try_from(format!("spiffe://vonk-forge.local/node/{NODE_ID}")).unwrap(),
    )];
    params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
    params.not_before = OffsetDateTime::now_utc() - time::Duration::hours(18);
    params.not_after = OffsetDateTime::now_utc() + time::Duration::hours(6);
    let certificate = params.signed_by(&key, issuer).unwrap();
    IdentityMaterial {
        node_id: NODE_ID.to_owned(),
        private_key_pem: key.serialize_pem().into_bytes(),
        certificate_pem: certificate.pem().into_bytes(),
        chain_pem: ca.pem().into_bytes(),
        serial: generation.to_string(),
        fingerprint: hex::encode(Sha256::digest(certificate.der())),
        generation,
    }
}

fn der(pem: &[u8]) -> Vec<u8> {
    let mut reader = pem;
    rustls_pemfile::certs(&mut reader)
        .next()
        .unwrap()
        .unwrap()
        .to_vec()
}

fn accept_bounded(listener: &TcpListener) -> TcpStream {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        match listener.accept() {
            Ok((socket, _)) => {
                socket
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                socket
                    .set_write_timeout(Some(Duration::from_secs(5)))
                    .unwrap();
                return socket;
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(Instant::now() < deadline, "rotation request did not arrive");
                thread::sleep(Duration::from_millis(5));
            }
            Err(error) => panic!("accept failed: {error}"),
        }
    }
}

fn read_request(stream: &mut StreamOwned<ServerConnection, TcpStream>) -> (String, Vec<u8>) {
    let mut request = Vec::new();
    loop {
        let mut buffer = [0; 4096];
        let count = stream.read(&mut buffer).unwrap();
        assert!(count > 0, "incomplete request");
        request.extend_from_slice(&buffer[..count]);
        assert!(request.len() <= 64 * 1024);
        if let Some(boundary) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
            let header = std::str::from_utf8(&request[..boundary]).unwrap();
            let length: usize = header
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse().unwrap())
                })
                .unwrap();
            let start = boundary + 4;
            if request.len() >= start + length {
                assert_eq!(request.len(), start + length);
                let path = header
                    .lines()
                    .next()
                    .unwrap()
                    .strip_prefix("POST ")
                    .unwrap()
                    .strip_suffix(" HTTP/1.1")
                    .unwrap()
                    .to_owned();
                return (path, request[start..].to_vec());
            }
        }
    }
}

fn respond(stream: &mut StreamOwned<ServerConnection, TcpStream>, status: u16, body: &[u8]) {
    write!(stream, "HTTP/1.1 {status} Response\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).unwrap();
    stream.write_all(body).unwrap();
    stream.flush().unwrap();
}

#[tokio::test]
async fn rotation_after_reenrollment_uses_issued_identity_and_preserves_pending_until_activation() {
    let directory = tempdir().unwrap();
    let mut ca_params = CertificateParams::default();
    ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    ca_params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
    let ca_key = KeyPair::generate_for(&PKCS_ED25519).unwrap();
    let ca = ca_params.self_signed(&ca_key).unwrap();
    let issuer = Issuer::new(ca_params, ca_key);
    let data_dir = directory.path().join("state");
    let credentials = data_dir.join("credentials");
    // A previous Controller's unexpired generation 2 survives re-enrollment.
    let previous = client_material(&issuer, &ca, 2);
    stage_identity(&credentials, &previous).unwrap();
    publish_staged(&credentials, 2).unwrap();
    let paired = client_material(&issuer, &ca, 1);
    persist_paired_identity(&credentials, &paired).unwrap();
    let pending = generate_pending(NODE_ID).unwrap();
    persist_pending(&credentials, &pending).unwrap();

    let server_key = KeyPair::generate_for(&PKCS_ED25519).unwrap();
    let mut server_params = CertificateParams::new(vec!["127.0.0.1".to_owned()]).unwrap();
    server_params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
    let server_cert = server_params.signed_by(&server_key, &issuer).unwrap();
    let mut roots = RootCertStore::empty();
    roots.add(ca.der().clone()).unwrap();
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let verifier = WebPkiClientVerifier::builder_with_provider(Arc::new(roots), provider.clone())
        .build()
        .unwrap();
    let tls = Arc::new(
        ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_client_cert_verifier(verifier)
            .with_single_cert(
                vec![server_cert.der().clone()],
                rustls_pemfile::private_key(&mut server_key.serialize_pem().as_bytes())
                    .unwrap()
                    .unwrap(),
            )
            .unwrap(),
    );
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let endpoint = Url::parse(&format!("https://{}/", listener.local_addr().unwrap())).unwrap();
    let ca_path = directory.path().join("ca.pem");
    fs::write(&ca_path, ca.pem()).unwrap();
    let config = AgentConfig {
        controller_url: endpoint.clone(),
        enrollment_url: endpoint,
        ca_path,
        ca_sha256: hex::encode(Sha256::digest(ca.der())),
        data_dir,
        node_id: NODE_ID.to_owned(),
        poll_min_seconds: 2,
        poll_max_seconds: 60,
        fabric_address: None,
        fabric_bandwidth_mbps: None,
        huggingface_curl_config: None,
    };
    let issued_der = Arc::new(Mutex::new(Vec::new()));
    let controller_der = issued_der.clone();
    let expected_csr = pending.csr_pem.clone();
    let paired_der = der(&paired.certificate_pem);
    // The fixture enforces mTLS and the current renew/activate contracts. It is
    // an independent Controller peer, not a mock of stage_identity or rotation.
    let controller = thread::spawn(move || {
        for attempt in 0..4 {
            let socket = accept_bounded(&listener);
            let mut stream = StreamOwned::new(ServerConnection::new(tls.clone()).unwrap(), socket);
            let (path, body) = read_request(&mut stream);
            let peer = stream.conn.peer_certificates().unwrap()[0].to_vec();
            if attempt == 0 {
                assert_eq!(path, "/agent/v1/renew");
                assert_eq!(peer, paired_der);
                let request: RenewRequest = serde_json::from_slice(&body).unwrap();
                assert_eq!(request.node_id, NODE_ID);
                assert_eq!(request.csr.as_bytes(), expected_csr);
                let mut csr = CertificateSigningRequestParams::from_pem(&request.csr).unwrap();
                csr.params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
                csr.params.not_before = OffsetDateTime::now_utc() - time::Duration::minutes(1);
                csr.params.not_after = OffsetDateTime::now_utc() + time::Duration::days(1);
                let certificate = csr.signed_by(&issuer).unwrap();
                *controller_der.lock().unwrap() = certificate.der().to_vec();
                let issued = IssuedCertificateResponse {
                    node_id: NODE_ID.to_owned(),
                    certificate_pem: certificate.pem(),
                    chain_pem: ca.pem(),
                    serial: "2".to_owned(),
                    fingerprint: hex::encode(Sha256::digest(certificate.der())),
                    not_before: chrono::DateTime::from_timestamp(
                        csr.params.not_before.unix_timestamp(),
                        0,
                    )
                    .unwrap()
                    .to_rfc3339(),
                    not_after: chrono::DateTime::from_timestamp(
                        csr.params.not_after.unix_timestamp(),
                        0,
                    )
                    .unwrap()
                    .to_rfc3339(),
                    generation: 2,
                };
                respond(
                    &mut stream,
                    200,
                    &canonical_generated_json(&issued).unwrap(),
                );
            } else {
                assert_eq!(path, "/agent/v1/renew/activate");
                assert_eq!(
                    peer,
                    *controller_der.lock().unwrap(),
                    "activation must present newly issued leaf"
                );
                let request: ActivateRequest = serde_json::from_slice(&body).unwrap();
                assert_eq!(request.node_id, NODE_ID);
                assert_eq!(request.generation, 2);
                // Leave the first activation retryable to exercise persistence.
                respond(&mut stream, if attempt == 1 { 503 } else { 204 }, b"");
            }
        }
    });

    let client = AgentHttpClient::from_config(&config).unwrap();
    let error = rotate_if_due(&config, &client).await.unwrap_err();
    assert!(error.retryable());
    let retained = load_pending(&credentials).unwrap().unwrap();
    assert_eq!(retained.csr_pem, pending.csr_pem);
    assert_eq!(retained.private_key_pem, pending.private_key_pem);
    assert_eq!(
        der(&fs::read(
            staged_identity_paths(&credentials)
                .unwrap()
                .unwrap()
                .1
                .certificate
        )
        .unwrap()),
        *issued_der.lock().unwrap()
    );
    assert_eq!(
        fs::read(active_identity_paths(&credentials).unwrap().certificate).unwrap(),
        paired.certificate_pem
    );
    assert!(!credentials.join("expired-staged.json").exists());

    assert!(rotate_if_due(&config, &client).await.unwrap());
    assert!(load_pending(&credentials).unwrap().is_none());
    assert!(staged_identity_paths(&credentials).unwrap().is_none());
    assert_eq!(
        der(&fs::read(active_identity_paths(&credentials).unwrap().certificate).unwrap()),
        *issued_der.lock().unwrap()
    );
    // The existing client's replacement identity must also be the issued leaf.
    client.activate(2).await.unwrap();
    assert!(!rotate_if_due(&config, &client).await.unwrap());
    controller.join().unwrap();
    let archives: Vec<_> = fs::read_dir(credentials.join("retired-generations"))
        .unwrap()
        .collect();
    assert_eq!(archives.len(), 1);
    let archived = archives[0].as_ref().unwrap().path().join("identity");
    assert_eq!(
        fs::read(archived.join("certificate.pem")).unwrap(),
        previous.certificate_pem
    );
    assert_eq!(
        fs::read(archived.join("private-key.pem")).unwrap(),
        previous.private_key_pem
    );
}
