use std::cell::RefCell;
use std::collections::VecDeque;
use std::io::Cursor;
use std::path::{Path, PathBuf};

use base64ct::{Base64UrlUnpadded, Encoding};
use ed25519_dalek::pkcs8::{DecodePrivateKey, EncodePrivateKey};
use p256::elliptic_curve::sec1::ToEncodedPoint;
use rcgen::{
    CertificateParams, DnType, ExtendedKeyUsagePurpose, Issuer, KeyPair, KeyUsagePurpose,
    PKCS_ED25519,
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tempfile::tempdir;
use vonk_nas_setup::{
    CanonicalTemplatePayload, PromptIo, SecretGenerationError, SecretGenerator, SetupRequest,
    prepare,
};

struct SequenceGenerator {
    values: RefCell<VecDeque<String>>,
}

impl SequenceGenerator {
    fn new(values: impl IntoIterator<Item = &'static str>) -> Self {
        Self {
            values: RefCell::new(values.into_iter().map(str::to_owned).collect()),
        }
    }
}

impl SecretGenerator for SequenceGenerator {
    fn generate(&self, _bytes: usize) -> Result<String, SecretGenerationError> {
        self.values
            .borrow_mut()
            .pop_front()
            .ok_or(SecretGenerationError)
    }
}

#[test]
fn generated_text_ed25519_keys_and_postgres_urls_are_valid_and_related() {
    let payload = CanonicalTemplatePayload::from_json(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "internal_values": [],
          "required_values": [],
          "secrets": [],
          "generated_secrets": {
            "random_text": [
              {"file": "postgres-password", "prompt": "Control database password", "bytes": 32},
              {"file": "litellm-database-password", "prompt": "LiteLLM database password", "bytes": 32}
            ],
            "ed25519_pkcs8_pem": [
              {"file": "token-signing-key", "prompt": "Token signing key"}
            ],
            "postgres_urls": [
              {
                "file": "database-url",
                "password_file": "postgres-password",
                "scheme": "postgresql+psycopg",
                "username": "control",
                "host": "postgres",
                "port": 5432,
                "database": "control"
              },
              {
                "file": "litellm-database-url",
                "password_file": "litellm-database-password",
                "scheme": "postgresql",
                "username": "litellm",
                "host": "postgres",
                "port": 5432,
                "database": "litellm"
              }
            ]
          },
          "step_ca_controller": null,
          "hermes": null
        }"#,
    )
    .expect("valid generated-secret payload");
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(b"\n\n\n".to_vec()), &mut output);
    let generator = SequenceGenerator::new(["control:p@ss word/?", "litellm-secret"]);

    let result = prepare(
        &payload,
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &generator,
    )
    .expect("generated bundle");

    assert_eq!(
        std::fs::read_to_string(result.root.join("secrets/postgres-password"))
            .expect("control password"),
        "control:p@ss word/?\n"
    );
    assert_eq!(
        std::fs::read_to_string(result.root.join("secrets/database-url")).expect("control URL"),
        "postgresql+psycopg://control:control%3Ap%40ss%20word%2F%3F@postgres:5432/control\n"
    );
    assert_eq!(
        std::fs::read_to_string(result.root.join("secrets/litellm-database-url"))
            .expect("LiteLLM URL"),
        "postgresql://litellm:litellm-secret@postgres:5432/litellm\n"
    );
    let private_key = std::fs::read_to_string(result.root.join("secrets/token-signing-key"))
        .expect("Ed25519 private key");
    let key_pair = KeyPair::from_pem(&private_key).expect("valid PKCS#8 PEM");
    assert!(key_pair.is_compatible(&PKCS_ED25519));
}

const PKI_PAYLOAD: &[u8] = br#"{
  "schema_version": 2,
  "docker_compose_yaml": "services: {}\n",
  "internal_values": [],
  "required_values": [
    {"env": "VONK_CONTROL_HOSTNAME", "prompt": "Control hostname"},
    {"env": "VONK_AGENT_ENROLL_HOSTNAME", "prompt": "Enrollment hostname"},
    {"env": "VONK_AGENT_HOSTNAME", "prompt": "Agent hostname"},
    {"env": "VONK_REGISTRY_HOSTNAME", "prompt": "Registry hostname"}
  ],
  "secrets": [],
  "generated_secrets": {
    "random_text": [],
    "ed25519_pkcs8_pem": [],
    "postgres_urls": []
  },
  "step_ca_controller": {
    "prompt": "Step CA/controller PKI",
    "hostname_envs": [
      "VONK_CONTROL_HOSTNAME",
      "VONK_AGENT_ENROLL_HOSTNAME",
      "VONK_AGENT_HOSTNAME",
      "VONK_REGISTRY_HOSTNAME"
    ],
    "provisioner_name": "vonk-forge-agent",
    "kid_env": "AGENT_CA_PROVISIONER_KID",
    "password_bytes": 32,
    "files": {
      "root_certificate": "step-ca/root-certificate",
      "intermediate_certificate": "step-ca/intermediate-certificate",
      "intermediate_private_key": "step-ca/intermediate-key",
      "controller_server_certificate": "controller-server-certificate",
      "controller_server_private_key": "controller-server-key",
      "provisioner_private_jwk": "agent-ca-credential",
      "provisioner_public_jwk": "agent-ca-provisioner-public-jwk",
      "ca_config": "step-ca/ca.json",
      "password": "step-ca-password"
    }
  },
  "hermes": null
}"#;

const PKI_FILES: [&str; 9] = [
    "step-ca/root-certificate",
    "step-ca/intermediate-certificate",
    "step-ca/intermediate-key",
    "controller-server-certificate",
    "controller-server-key",
    "agent-ca-credential",
    "agent-ca-provisioner-public-jwk",
    "step-ca/ca.json",
    "step-ca-password",
];

fn pki_payload() -> CanonicalTemplatePayload {
    CanonicalTemplatePayload::from_json(PKI_PAYLOAD).expect("valid PKI payload")
}

fn parse_certificate(pem: &[u8]) -> x509_parser::certificate::X509Certificate<'static> {
    let (_, pem) = x509_parser::pem::parse_x509_pem(pem).expect("certificate PEM");
    let leaked = Box::leak(pem.contents.into_boxed_slice());
    let (_, certificate) = x509_parser::parse_x509_certificate(leaked).expect("X.509 certificate");
    certificate
}

fn install_pki_bundle(output_root: &Path) -> PathBuf {
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(
        Cursor::new(
            b"control.example.test\nenroll.example.test\nagents.example.test\nregistry.example.test\n\n"
                .to_vec(),
        ),
        &mut output,
    );
    prepare(
        &pki_payload(),
        SetupRequest::install(output_root),
        &mut prompt,
        &SequenceGenerator::new(["step-ca-password"]),
    )
    .expect("generated PKI bundle")
    .root
}

fn replace_controller_leaf(
    secrets: &Path,
    not_before: time::OffsetDateTime,
    not_after: time::OffsetDateTime,
) {
    let password =
        std::fs::read_to_string(secrets.join("step-ca-password")).expect("Step CA password");
    let encrypted_intermediate = std::fs::read_to_string(secrets.join("step-ca/intermediate-key"))
        .expect("encrypted intermediate key");
    let intermediate_signing_key = ed25519_dalek::SigningKey::from_pkcs8_encrypted_pem(
        &encrypted_intermediate,
        password.trim().as_bytes(),
    )
    .expect("decrypted intermediate key");
    let intermediate_der = intermediate_signing_key
        .to_pkcs8_der()
        .expect("intermediate PKCS#8 DER");
    let intermediate_key =
        KeyPair::try_from(intermediate_der.as_bytes()).expect("rcgen intermediate key");
    let intermediate_pem =
        std::fs::read_to_string(secrets.join("step-ca/intermediate-certificate"))
            .expect("intermediate certificate");
    let issuer =
        Issuer::from_ca_cert_pem(&intermediate_pem, intermediate_key).expect("intermediate issuer");

    let controller_key = KeyPair::generate_for(&PKCS_ED25519).expect("controller key");
    let mut params = CertificateParams::new(vec![
        "control.example.test".to_owned(),
        "enroll.example.test".to_owned(),
        "agents.example.test".to_owned(),
        "registry.example.test".to_owned(),
    ])
    .expect("controller certificate parameters");
    params.not_before = not_before;
    params.not_after = not_after;
    params
        .distinguished_name
        .push(DnType::CommonName, "control.example.test");
    params.key_usages.push(KeyUsagePurpose::DigitalSignature);
    params
        .extended_key_usages
        .push(ExtendedKeyUsagePurpose::ServerAuth);
    params.use_authority_key_identifier_extension = true;
    let controller_certificate = params
        .signed_by(&controller_key, &issuer)
        .expect("controller certificate");

    std::fs::write(
        secrets.join("controller-server-certificate"),
        format!("{}{}", controller_certificate.pem(), intermediate_pem),
    )
    .expect("replace controller certificate");
    std::fs::write(
        secrets.join("controller-server-key"),
        controller_key.serialize_pem(),
    )
    .expect("replace controller key");
}

fn upgrade_pki_bundle(output_root: &Path) -> Result<(), vonk_nas_setup::SetupError> {
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);
    prepare(
        &pki_payload(),
        SetupRequest::upgrade(output_root),
        &mut prompt,
        &SequenceGenerator::new([]),
    )?;
    assert!(output.is_empty(), "PKI renewal must not prompt");
    Ok(())
}

fn authority_snapshot(secrets: &Path) -> Vec<(&'static str, Vec<u8>)> {
    [
        "step-ca/root-certificate",
        "step-ca/intermediate-certificate",
        "step-ca/intermediate-key",
        "agent-ca-credential",
        "agent-ca-provisioner-public-jwk",
        "step-ca/ca.json",
        "step-ca-password",
    ]
    .into_iter()
    .map(|path| {
        (
            path,
            std::fs::read(secrets.join(path))
                .unwrap_or_else(|error| panic!("snapshot {path}: {error}")),
        )
    })
    .collect()
}

fn assert_controller_pair_is_current_and_coherent(secrets: &Path) {
    let certificate_bytes =
        std::fs::read(secrets.join("controller-server-certificate")).expect("controller cert");
    let (chain_bytes, _) =
        x509_parser::pem::parse_x509_pem(&certificate_bytes).expect("controller certificate PEM");
    let (trailing, bundled_intermediate_pem) =
        x509_parser::pem::parse_x509_pem(chain_bytes).expect("bundled intermediate PEM");
    assert!(
        trailing.iter().all(u8::is_ascii_whitespace),
        "controller chain must contain only the leaf and intermediate"
    );
    let certificate = parse_certificate(&certificate_bytes);
    let intermediate_bytes =
        std::fs::read(secrets.join("step-ca/intermediate-certificate")).expect("intermediate cert");
    let (_, expected_intermediate_pem) =
        x509_parser::pem::parse_x509_pem(&intermediate_bytes).expect("preserved intermediate PEM");
    let intermediate = parse_certificate(&intermediate_bytes);
    assert_eq!(
        bundled_intermediate_pem.contents, expected_intermediate_pem.contents,
        "bundled chain must contain the preserved intermediate"
    );
    certificate
        .verify_signature(Some(intermediate.public_key()))
        .expect("controller certificate signed by preserved intermediate");
    assert!(certificate.validity().is_valid());
    let remaining_seconds = certificate.validity().not_after.timestamp()
        - time::OffsetDateTime::now_utc().unix_timestamp();
    assert!(
        (396 * 24 * 60 * 60..=397 * 24 * 60 * 60).contains(&remaining_seconds),
        "renewed controller certificate must have 397-day validity"
    );
    let sans = certificate
        .subject_alternative_name()
        .expect("SAN extension")
        .expect("SAN present")
        .value
        .general_names
        .iter()
        .filter_map(|name| match name {
            x509_parser::extensions::GeneralName::DNSName(value) => Some(*value),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        sans,
        [
            "control.example.test",
            "enroll.example.test",
            "agents.example.test",
            "registry.example.test"
        ]
    );
    let key_pem =
        std::fs::read_to_string(secrets.join("controller-server-key")).expect("controller key");
    let key = KeyPair::from_pem(&key_pem).expect("controller PKCS#8 key");
    assert!(key.is_compatible(&PKCS_ED25519));
    assert_eq!(
        key.public_key_raw(),
        certificate.public_key().subject_public_key.data.as_ref()
    );
}

#[test]
fn upgrade_renews_controller_leaf_with_29_days_remaining_and_preserves_authority() {
    let temporary = tempdir().expect("temporary directory");
    let bundle = install_pki_bundle(temporary.path());
    let secrets = bundle.join("secrets");
    let now = time::OffsetDateTime::now_utc();
    replace_controller_leaf(
        &secrets,
        now - time::Duration::hours(1),
        now + time::Duration::days(29),
    );
    let old_certificate =
        std::fs::read(secrets.join("controller-server-certificate")).expect("old cert");
    let old_key = std::fs::read(secrets.join("controller-server-key")).expect("old key");
    let authority_before = authority_snapshot(&secrets);
    let environment_before = std::fs::read(bundle.join(".env")).expect("environment before");
    #[cfg(unix)]
    let old_inodes = {
        use std::os::unix::fs::MetadataExt;
        (
            std::fs::metadata(secrets.join("controller-server-certificate"))
                .expect("old cert metadata")
                .ino(),
            std::fs::metadata(secrets.join("controller-server-key"))
                .expect("old key metadata")
                .ino(),
        )
    };

    upgrade_pki_bundle(temporary.path()).expect("near-expiry controller leaf renewed");

    assert_ne!(
        std::fs::read(secrets.join("controller-server-certificate")).expect("renewed cert"),
        old_certificate
    );
    assert_ne!(
        std::fs::read(secrets.join("controller-server-key")).expect("renewed key"),
        old_key
    );
    assert_eq!(authority_snapshot(&secrets), authority_before);
    assert_eq!(
        std::fs::read(bundle.join(".env")).expect("environment after"),
        environment_before
    );
    assert_controller_pair_is_current_and_coherent(&secrets);
    #[cfg(unix)]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};

        for (path, old_inode) in [
            ("controller-server-certificate", old_inodes.0),
            ("controller-server-key", old_inodes.1),
        ] {
            let metadata = std::fs::metadata(secrets.join(path)).expect("renewed metadata");
            assert_ne!(
                metadata.ino(),
                old_inode,
                "{path} must be staged and renamed"
            );
            assert_eq!(metadata.permissions().mode() & 0o777, 0o600, "{path} mode");
        }
    }
}

#[test]
fn upgrade_preserves_controller_leaf_with_31_days_remaining_byte_for_byte() {
    let temporary = tempdir().expect("temporary directory");
    let bundle = install_pki_bundle(temporary.path());
    let secrets = bundle.join("secrets");
    let now = time::OffsetDateTime::now_utc();
    replace_controller_leaf(
        &secrets,
        now - time::Duration::hours(1),
        now + time::Duration::days(31),
    );
    let certificate_before =
        std::fs::read(secrets.join("controller-server-certificate")).expect("cert before");
    let key_before = std::fs::read(secrets.join("controller-server-key")).expect("key before");

    upgrade_pki_bundle(temporary.path()).expect("healthy controller leaf accepted");

    assert_eq!(
        std::fs::read(secrets.join("controller-server-certificate")).expect("cert after"),
        certificate_before
    );
    assert_eq!(
        std::fs::read(secrets.join("controller-server-key")).expect("key after"),
        key_before
    );
}

#[test]
fn upgrade_recovers_an_expired_controller_leaf_without_rotating_authority() {
    let temporary = tempdir().expect("temporary directory");
    let bundle = install_pki_bundle(temporary.path());
    let secrets = bundle.join("secrets");
    let now = time::OffsetDateTime::now_utc();
    replace_controller_leaf(
        &secrets,
        now - time::Duration::days(397),
        now - time::Duration::days(1),
    );
    let old_certificate =
        std::fs::read(secrets.join("controller-server-certificate")).expect("expired cert");
    let old_key = std::fs::read(secrets.join("controller-server-key")).expect("expired key");
    let authority_before = authority_snapshot(&secrets);

    upgrade_pki_bundle(temporary.path()).expect("expired controller leaf recovered");

    assert_ne!(
        std::fs::read(secrets.join("controller-server-certificate")).expect("renewed cert"),
        old_certificate
    );
    assert_ne!(
        std::fs::read(secrets.join("controller-server-key")).expect("renewed key"),
        old_key
    );
    assert_eq!(authority_snapshot(&secrets), authority_before);
    assert_controller_pair_is_current_and_coherent(&secrets);
}

#[test]
fn upgrade_rejects_corrupt_expired_controller_key_before_renewal() {
    let temporary = tempdir().expect("temporary directory");
    let bundle = install_pki_bundle(temporary.path());
    let secrets = bundle.join("secrets");
    let now = time::OffsetDateTime::now_utc();
    replace_controller_leaf(
        &secrets,
        now - time::Duration::days(397),
        now - time::Duration::days(1),
    );
    std::fs::write(
        secrets.join("controller-server-key"),
        KeyPair::generate_for(&PKCS_ED25519)
            .expect("unrelated key")
            .serialize_pem(),
    )
    .expect("corrupt controller key");
    let certificate_before =
        std::fs::read(secrets.join("controller-server-certificate")).expect("cert before");
    let key_before = std::fs::read(secrets.join("controller-server-key")).expect("key before");

    let error = upgrade_pki_bundle(temporary.path())
        .expect_err("expired controller leaf with mismatched key rejected");

    assert!(error.to_string().contains("private key does not match"));
    assert_eq!(
        std::fs::read(secrets.join("controller-server-certificate")).expect("cert after"),
        certificate_before
    );
    assert_eq!(
        std::fs::read(secrets.join("controller-server-key")).expect("key after"),
        key_before
    );
}

#[test]
fn step_ca_controller_group_is_one_coherent_pki_and_jwk_authority() {
    let payload = pki_payload();
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(
        Cursor::new(
            b"control.example.test\nenroll.example.test\nagents.example.test\nregistry.example.test\n\n"
                .to_vec(),
        ),
        &mut output,
    );
    let generator = SequenceGenerator::new(["step-ca-password"]);

    let result = prepare(
        &payload,
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &generator,
    )
    .expect("generated PKI bundle");
    let secrets = result.root.join("secrets");

    let root_pem = std::fs::read(secrets.join("step-ca/root-certificate")).expect("root");
    let intermediate_pem =
        std::fs::read(secrets.join("step-ca/intermediate-certificate")).expect("intermediate");
    let server_pem =
        std::fs::read(secrets.join("controller-server-certificate")).expect("server certificate");
    let root = parse_certificate(&root_pem);
    let intermediate = parse_certificate(&intermediate_pem);
    let server = parse_certificate(&server_pem);
    root.verify_signature(None).expect("self-signed root");
    intermediate
        .verify_signature(Some(root.public_key()))
        .expect("intermediate signed by root");
    server
        .verify_signature(Some(intermediate.public_key()))
        .expect("server signed by intermediate");

    let sans = server
        .subject_alternative_name()
        .expect("SAN extension")
        .expect("SAN present")
        .value
        .general_names
        .iter()
        .filter_map(|name| match name {
            x509_parser::extensions::GeneralName::DNSName(value) => Some(*value),
            _ => None,
        })
        .collect::<Vec<_>>();
    assert_eq!(
        sans,
        [
            "control.example.test",
            "enroll.example.test",
            "agents.example.test",
            "registry.example.test"
        ]
    );
    let server_key_pem =
        std::fs::read_to_string(secrets.join("controller-server-key")).expect("server key");
    let server_key = KeyPair::from_pem(&server_key_pem).expect("server PKCS#8 key");
    assert!(server_key.is_compatible(&PKCS_ED25519));
    assert_eq!(
        server_key.public_key_raw(),
        server.public_key().subject_public_key.data.as_ref()
    );
    let password = std::fs::read_to_string(secrets.join("step-ca-password")).expect("CA password");
    let encrypted_intermediate = std::fs::read_to_string(secrets.join("step-ca/intermediate-key"))
        .expect("encrypted intermediate key");
    ed25519_dalek::SigningKey::from_pkcs8_encrypted_pem(
        &encrypted_intermediate,
        password.trim().as_bytes(),
    )
    .expect("password decrypts intermediate Ed25519 key");

    let public_jwk: Value = serde_json::from_slice(
        &std::fs::read(secrets.join("agent-ca-provisioner-public-jwk")).expect("public JWK"),
    )
    .expect("public JWK JSON");
    let private_jwk: Value = serde_json::from_slice(
        &std::fs::read(secrets.join("agent-ca-credential")).expect("private JWK"),
    )
    .expect("private JWK JSON");
    for field in ["alg", "crv", "kid", "kty", "use", "x", "y"] {
        assert_eq!(private_jwk[field], public_jwk[field], "JWK field {field}");
    }
    let canonical = format!(
        "{{\"crv\":\"P-256\",\"kty\":\"EC\",\"x\":\"{}\",\"y\":\"{}\"}}",
        public_jwk["x"].as_str().expect("x"),
        public_jwk["y"].as_str().expect("y")
    );
    let expected_kid = Base64UrlUnpadded::encode_string(&Sha256::digest(canonical.as_bytes()));
    assert_eq!(public_jwk["kid"], expected_kid);
    let private_scalar =
        Base64UrlUnpadded::decode_vec(private_jwk["d"].as_str().expect("private scalar"))
            .expect("base64url private scalar");
    let secret_key = p256::SecretKey::from_slice(&private_scalar).expect("P-256 private scalar");
    let point = secret_key.public_key().to_encoded_point(false);
    assert_eq!(
        Base64UrlUnpadded::decode_vec(public_jwk["x"].as_str().expect("x")).expect("x"),
        point.x().expect("x coordinate").as_slice()
    );
    assert_eq!(
        Base64UrlUnpadded::decode_vec(public_jwk["y"].as_str().expect("y")).expect("y"),
        point.y().expect("y coordinate").as_slice()
    );

    let ca_config: Value =
        serde_json::from_slice(&std::fs::read(secrets.join("step-ca/ca.json")).expect("CA config"))
            .expect("CA config JSON");
    assert_eq!(ca_config["authority"]["provisioners"][0]["key"], public_jwk);
    assert_eq!(
        ca_config["authority"]["provisioners"][0]["name"],
        "vonk-forge-agent"
    );
    let environment = std::fs::read_to_string(result.root.join(".env")).expect("environment");
    assert!(environment.contains(&format!("AGENT_CA_PROVISIONER_KID={expected_kid}\n")));

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        for path in PKI_FILES {
            assert_eq!(
                std::fs::metadata(secrets.join(path))
                    .unwrap_or_else(|error| panic!("metadata for {path}: {error}"))
                    .permissions()
                    .mode()
                    & 0o777,
                0o600,
                "mode for {path}"
            );
        }
    }

    let private_jwk_before =
        std::fs::read(secrets.join("agent-ca-credential")).expect("private JWK before upgrade");
    let mut upgrade_output = Vec::new();
    let mut upgrade_prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut upgrade_output);
    prepare(
        &payload,
        SetupRequest::upgrade(temporary.path()),
        &mut upgrade_prompt,
        &SequenceGenerator::new([]),
    )
    .expect("upgrade preserves complete PKI");
    assert!(upgrade_output.is_empty());
    assert_eq!(
        std::fs::read(secrets.join("agent-ca-credential")).expect("private JWK after upgrade"),
        private_jwk_before
    );

    std::fs::remove_file(secrets.join("controller-server-key")).expect("remove one PKI member");
    let mut partial_output = Vec::new();
    let mut partial_prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut partial_output);
    let error = prepare(
        &payload,
        SetupRequest::upgrade(temporary.path()),
        &mut partial_prompt,
        &SequenceGenerator::new([]),
    )
    .expect_err("partial PKI is rejected rather than regenerated");
    assert!(error.to_string().contains("partial"));
}

#[test]
fn complete_existing_pki_can_be_imported_without_regeneration() {
    let source = tempdir().expect("source directory");
    let payload = pki_payload();
    let mut source_output = Vec::new();
    let mut source_prompt = PromptIo::new(
        Cursor::new(
            b"control.example.test\nenroll.example.test\nagents.example.test\nregistry.example.test\n\n"
                .to_vec(),
        ),
        &mut source_output,
    );
    let source_result = prepare(
        &payload,
        SetupRequest::install(source.path()),
        &mut source_prompt,
        &SequenceGenerator::new(["imported-step-ca-password"]),
    )
    .expect("source PKI");

    let target = tempdir().expect("target directory");
    let input = format!(
        "control.example.test\nenroll.example.test\nagents.example.test\nregistry.example.test\n{}\n",
        source_result.root.join("secrets").display()
    );
    let mut target_output = Vec::new();
    let mut target_prompt = PromptIo::new(Cursor::new(input.into_bytes()), &mut target_output);
    let target_result = prepare(
        &payload,
        SetupRequest::install(target.path()),
        &mut target_prompt,
        &SequenceGenerator::new([]),
    )
    .expect("imported PKI");

    for path in PKI_FILES {
        assert_eq!(
            std::fs::read(source_result.root.join("secrets").join(path))
                .unwrap_or_else(|error| panic!("source {path}: {error}")),
            std::fs::read(target_result.root.join("secrets").join(path))
                .unwrap_or_else(|error| panic!("target {path}: {error}")),
            "imported {path}"
        );
    }
    assert_eq!(
        std::fs::read_to_string(source_result.root.join(".env")).expect("source environment"),
        std::fs::read_to_string(target_result.root.join(".env")).expect("target environment")
    );
}
