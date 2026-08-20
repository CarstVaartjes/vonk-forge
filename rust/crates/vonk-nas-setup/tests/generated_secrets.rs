use std::cell::RefCell;
use std::collections::VecDeque;
use std::io::Cursor;

use base64ct::{Base64UrlUnpadded, Encoding};
use ed25519_dalek::pkcs8::DecodePrivateKey;
use p256::elliptic_curve::sec1::ToEncodedPoint;
use rcgen::{KeyPair, PKCS_ED25519};
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
