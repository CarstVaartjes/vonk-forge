use std::cell::RefCell;
use std::collections::VecDeque;
use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use base64ct::{Base64UrlUnpadded, Encoding};
use ed25519_dalek::pkcs8::{DecodePrivateKey, EncodePrivateKey};
use rcgen::{
    BasicConstraints, CertificateParams, CertifiedIssuer, DnType, ExtendedKeyUsagePurpose, IsCa,
    Issuer, KeyPair, KeyUsagePurpose, PKCS_ED25519,
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tempfile::{TempDir, tempdir};
use vonk_nas_setup::{
    CanonicalTemplatePayload, PromptIo, SecretGenerationError, SecretGenerator, SetupRequest,
    parse_template_payload, prepare,
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
    let payload = parse_template_payload(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "internal_values": [],
          "required_values": [],
          "secrets": [],
          "generated_secrets": {
            "random_text": [
              {"file": "postgres-password", "bytes": 32},
              {"file": "litellm-database-password", "bytes": 32}
            ],
            "ed25519_pkcs8_pem": [
              {"file": "token-signing-key"}
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
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);
    let generator = SequenceGenerator::new(["control:p@ss word/?", "litellm-secret"]);

    let result = prepare(
        &payload,
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &generator,
    )
    .expect("generated bundle");

    assert!(output.is_empty(), "generated secrets must not prompt");
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
    ed25519_dalek::SigningKey::from_pkcs8_pem(&private_key).expect("canonical Ed25519 PKCS#8 PEM");
    assert_eq!(
        private_key.lines().map(str::len).collect::<Vec<_>>(),
        [27, 64, 25]
    );
}

const PKI_PAYLOAD: &[u8] = br#"{
  "schema_version": 2,
  "docker_compose_yaml": "services: {}\n",
  "internal_values": [],
  "required_values": [
    {"env": "VONK_CONTROL_HOSTNAME", "prompt": "Control hostname", "validation": "hostname"}
  ],
  "secrets": [],
  "generated_secrets": {
    "random_text": [],
    "ed25519_pkcs8_pem": [],
    "postgres_urls": []
  },
  "step_ca_controller": {
    "hostname_env": "VONK_CONTROL_HOSTNAME",
    "provisioner_name": "vonk-forge-agent",
    "password_bytes": 32,
    "files": {
      "root_certificate": "step-ca/root-certificate",
      "intermediate_certificate": "step-ca/intermediate-certificate",
      "intermediate_private_key": "step-ca/intermediate-key",
      "controller_server_certificate": "controller-server-certificate",
      "controller_server_private_key": "controller-server-key",
      "provisioner_public_jwk": "agent-ca-provisioner-public-jwk",
      "password": "step-ca-password"
    }
  },
  "hermes": null
}"#;

const PKI_FILES: &[&str] = &[
    "step-ca/root-certificate",
    "step-ca/intermediate-certificate",
    "step-ca/intermediate-key",
    "controller-server-certificate",
    "controller-server-key",
    "agent-ca-provisioner-public-jwk",
    "step-ca-password",
];

fn pki_payload() -> CanonicalTemplatePayload {
    parse_template_payload(PKI_PAYLOAD).expect("valid PKI payload")
}

/// The controller SANs: the control hostname plus the names derived from it.
fn controller_sans(control: &str) -> Vec<String> {
    std::iter::once(control.to_owned())
        .chain(["enroll", "agents", "registry"].map(|prefix| format!("{prefix}.{control}")))
        .collect()
}

fn dns_sans(certificate: &x509_parser::certificate::X509Certificate<'_>) -> Vec<String> {
    certificate
        .subject_alternative_name()
        .expect("valid SAN extension")
        .expect("SAN extension present")
        .value
        .general_names
        .iter()
        .filter_map(|name| match name {
            x509_parser::extensions::GeneralName::DNSName(value) => Some((*value).to_owned()),
            _ => None,
        })
        .collect()
}

fn parse_certificate(pem: &[u8]) -> x509_parser::certificate::X509Certificate<'static> {
    let (_, pem) = x509_parser::pem::parse_x509_pem(pem).expect("certificate PEM");
    let leaked = Box::leak(pem.contents.into_boxed_slice());
    let (_, certificate) = x509_parser::parse_x509_certificate(leaked).expect("X.509 certificate");
    certificate
}

fn install_pki_bundle(output_root: &Path) -> PathBuf {
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(b"control.example.test\n".to_vec()), &mut output);
    prepare(
        &pki_payload(),
        SetupRequest::install(output_root),
        &mut prompt,
        &SequenceGenerator::new(["step-ca-password"]),
    )
    .expect("generated PKI bundle")
    .root
}

struct PkiFixture {
    _root: TempDir,
    bundle: PathBuf,
}

static PKI_FIXTURE: OnceLock<PkiFixture> = OnceLock::new();

fn pki_fixture() -> &'static Path {
    PKI_FIXTURE
        .get_or_init(|| {
            let root = tempdir().expect("PKI fixture directory");
            let bundle = install_pki_bundle(root.path());
            PkiFixture {
                _root: root,
                bundle,
            }
        })
        .bundle
        .as_path()
}

fn copy_fixture_tree(source: &Path, destination: &Path) {
    let metadata = std::fs::symlink_metadata(source)
        .unwrap_or_else(|error| panic!("inspect fixture path {}: {error}", source.display()));
    let file_type = metadata.file_type();
    assert!(
        !file_type.is_symlink(),
        "fixture source must not contain symbolic links: {}",
        source.display()
    );

    if file_type.is_dir() {
        std::fs::create_dir(destination).unwrap_or_else(|error| {
            panic!(
                "create fixture directory {}: {error}",
                destination.display()
            )
        });
        for entry in std::fs::read_dir(source)
            .unwrap_or_else(|error| panic!("read fixture directory {}: {error}", source.display()))
        {
            let entry = entry.expect("fixture directory entry");
            copy_fixture_tree(&entry.path(), &destination.join(entry.file_name()));
        }
        std::fs::set_permissions(destination, metadata.permissions()).unwrap_or_else(|error| {
            panic!(
                "preserve fixture directory permissions {}: {error}",
                destination.display()
            )
        });
        return;
    }

    assert!(
        file_type.is_file(),
        "fixture source must contain only directories and regular files: {}",
        source.display()
    );
    std::fs::copy(source, destination).unwrap_or_else(|error| {
        panic!(
            "copy fixture file {} to {}: {error}",
            source.display(),
            destination.display()
        )
    });
    std::fs::set_permissions(destination, metadata.permissions()).unwrap_or_else(|error| {
        panic!(
            "preserve fixture file permissions {}: {error}",
            destination.display()
        )
    });
}

fn clone_pki_bundle(output_root: &Path) -> PathBuf {
    let source = pki_fixture();
    let bundle_name = source.file_name().expect("PKI fixture bundle name");
    let destination = output_root.join(bundle_name);
    copy_fixture_tree(source, &destination);
    destination
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
    let mut params = CertificateParams::new(controller_sans("control.example.test"))
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

/// Keep the intermediate signing identity while supplying an expired root.
/// The root private key is not retained by setup, so it cannot silently renew
/// this authority. Configuration must still converge independently.
fn replace_ca_with_expired_root(secrets: &Path) {
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

    let now = time::OffsetDateTime::now_utc();
    let root_key = KeyPair::generate_for(&PKCS_ED25519).expect("root key");
    let mut root_params = CertificateParams::new(Vec::<String>::new()).expect("root parameters");
    root_params.not_before = now - time::Duration::days(10);
    root_params.not_after = now - time::Duration::days(1);
    root_params
        .distinguished_name
        .push(DnType::CommonName, "Vonk Forge Root CA");
    root_params.is_ca = IsCa::Ca(BasicConstraints::Constrained(1));
    root_params.key_usages = vec![
        KeyUsagePurpose::DigitalSignature,
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::CrlSign,
    ];
    root_params.use_authority_key_identifier_extension = true;
    let root = CertifiedIssuer::self_signed(root_params, root_key).expect("self-signed root");

    let mut intermediate_params =
        CertificateParams::new(Vec::<String>::new()).expect("intermediate parameters");
    intermediate_params.not_before = now - time::Duration::hours(1);
    intermediate_params.not_after = now + time::Duration::days(1825);
    intermediate_params
        .distinguished_name
        .push(DnType::CommonName, "Vonk Forge Agent Intermediate CA");
    intermediate_params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
    intermediate_params.key_usages = vec![
        KeyUsagePurpose::DigitalSignature,
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::CrlSign,
    ];
    intermediate_params.use_authority_key_identifier_extension = true;
    let intermediate = CertifiedIssuer::signed_by(intermediate_params, intermediate_key, &root)
        .expect("legacy intermediate");

    let chain = std::fs::read_to_string(secrets.join("controller-server-certificate"))
        .expect("controller chain");
    let marker = "-----END CERTIFICATE-----";
    let leaf_end = chain.find(marker).expect("controller leaf end") + marker.len();
    let leaf_pem = &chain[..leaf_end];
    std::fs::write(secrets.join("step-ca/root-certificate"), root.pem())
        .expect("write legacy root");
    std::fs::write(
        secrets.join("step-ca/intermediate-certificate"),
        intermediate.pem(),
    )
    .expect("write legacy intermediate");
    std::fs::write(
        secrets.join("controller-server-certificate"),
        format!("{leaf_pem}\n{}", intermediate.pem()),
    )
    .expect("write controller chain");
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
        "agent-ca-provisioner-public-jwk",
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
    assert_eq!(
        dns_sans(&certificate),
        controller_sans("control.example.test")
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
    let bundle = clone_pki_bundle(temporary.path());
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
    let bundle = clone_pki_bundle(temporary.path());
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
fn upgrade_renews_controller_leaf_when_the_control_hostname_changes() {
    let temporary = tempdir().expect("temporary directory");
    let bundle = clone_pki_bundle(temporary.path());
    let secrets = bundle.join("secrets");
    let old_certificate =
        std::fs::read(secrets.join("controller-server-certificate")).expect("old cert");
    let old_key = std::fs::read(secrets.join("controller-server-key")).expect("old key");
    let authority_before = authority_snapshot(&secrets);
    let environment_path = bundle.join(".env");
    let environment = std::fs::read_to_string(&environment_path).expect("environment");
    let updated_environment =
        environment.replace("control.example.test", "control.renamed.example.test");
    assert_ne!(updated_environment, environment);
    std::fs::write(&environment_path, &updated_environment).expect("update environment");

    upgrade_pki_bundle(temporary.path()).expect("hostname change renews controller leaf");

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
        std::fs::read_to_string(&environment_path).expect("environment after"),
        updated_environment
    );

    let certificate_bytes =
        std::fs::read(secrets.join("controller-server-certificate")).expect("controller cert");
    assert_eq!(
        dns_sans(&parse_certificate(&certificate_bytes)),
        controller_sans("control.renamed.example.test")
    );
}

#[test]
fn upgrade_recovers_an_expired_controller_leaf_without_rotating_authority() {
    let temporary = tempdir().expect("temporary directory");
    let bundle = clone_pki_bundle(temporary.path());
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
    let bundle = clone_pki_bundle(temporary.path());
    let secrets = bundle.join("secrets");
    let now = time::OffsetDateTime::now_utc();
    replace_controller_leaf(
        &secrets,
        now - time::Duration::days(397),
        now - time::Duration::days(1),
    );
    let verified_key = std::fs::read(secrets.join("controller-server-key")).unwrap();
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

    assert!(upgrade_pki_bundle(temporary.path()).is_err());
    assert_eq!(
        std::fs::read(secrets.join("controller-server-certificate")).expect("cert after"),
        certificate_before
    );
    assert_eq!(
        std::fs::read(secrets.join("controller-server-key")).expect("key after"),
        key_before
    );
    std::fs::write(secrets.join("controller-server-key"), verified_key).unwrap();
    upgrade_pki_bundle(temporary.path()).unwrap();
    assert_controller_pair_is_current_and_coherent(&secrets);
    upgrade_pki_bundle(temporary.path()).unwrap();
}

#[test]
fn unavailable_root_signing_authority_does_not_block_compose_and_fresh_upgrade_recovers() {
    const CHILD_ROOT: &str = "VONK_NAS_EXPIRED_ROOT_TEST_DIRECTORY";
    if let Some(root) = std::env::var_os(CHILD_ROOT) {
        assert!(upgrade_pki_bundle(Path::new(&root)).is_err());
        return;
    }
    let temporary = tempdir().unwrap();
    let bundle = clone_pki_bundle(temporary.path());
    let secrets = bundle.join("secrets");
    let authority = authority_snapshot(&secrets);
    let originals = PKI_FILES
        .iter()
        .map(|path| (*path, std::fs::read(secrets.join(path)).unwrap()))
        .collect::<Vec<_>>();
    replace_ca_with_expired_root(&secrets);
    std::fs::write(bundle.join("docker-compose.yaml"), b"old compose").unwrap();
    let mut child = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "unavailable_root_signing_authority_does_not_block_compose_and_fresh_upgrade_recovers",
        ])
        .env(CHILD_ROOT, temporary.path())
        .spawn()
        .unwrap();
    // Three credential attempts include encrypted-key validation. Allow for
    // concurrent CI load, but kill and reap a stuck request instead of checking
    // elapsed time only after an unbounded call has returned.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(120);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if std::time::Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("expired-root upgrade exceeded its test deadline");
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    };
    assert!(
        status.success(),
        "expired-root upgrade child failed: {status}"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join("docker-compose.yaml")).unwrap(),
        pki_payload().docker_compose_yaml
    );
    // The root private key is deliberately not retained; no new root authority
    // may be invented to repair a certificate. Once verified source returns,
    // the same normal request works without a lingering gate.
    for (path, content) in originals {
        std::fs::write(secrets.join(path), content).unwrap();
    }
    upgrade_pki_bundle(temporary.path()).unwrap();
    assert_eq!(authority_snapshot(&secrets), authority);
    upgrade_pki_bundle(temporary.path()).unwrap();
}

#[test]
fn step_ca_controller_group_is_one_coherent_pki_and_jwk_authority() {
    let payload = pki_payload();
    let generated_bundle = pki_fixture();
    let secrets = generated_bundle.join("secrets");

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
    // Strict RFC 5280 verification rejects a non-self-signed certificate that
    // omits the Authority Key Identifier.  The agent reaches the Controller with
    // that verifier enabled and only the root as its trust anchor, so the
    // generated intermediate must bind the issuer key identifier.
    let root_ski = root
        .extensions()
        .iter()
        .find_map(|extension| match extension.parsed_extension() {
            x509_parser::extensions::ParsedExtension::SubjectKeyIdentifier(value) => Some(value.0),
            _ => None,
        })
        .expect("root subject key identifier");
    let intermediate_aki = intermediate
        .extensions()
        .iter()
        .find_map(|extension| match extension.parsed_extension() {
            x509_parser::extensions::ParsedExtension::AuthorityKeyIdentifier(value) => {
                value.key_identifier.as_ref().map(|identifier| identifier.0)
            }
            _ => None,
        })
        .expect("intermediate authority key identifier");
    assert_eq!(
        intermediate_aki, root_ski,
        "intermediate authority key identifier must match the root subject key identifier"
    );

    assert_eq!(dns_sans(&server), controller_sans("control.example.test"));
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
    let (label, encrypted_document) = pkcs8::SecretDocument::from_pem(&encrypted_intermediate)
        .expect("encrypted intermediate PEM");
    assert_eq!(label, "ENCRYPTED PRIVATE KEY");
    let encrypted_info = pkcs8::EncryptedPrivateKeyInfoRef::try_from(encrypted_document.as_bytes())
        .expect("encrypted intermediate PKCS#8");
    assert!(matches!(
        encrypted_info.encryption_algorithm,
        pkcs8::pkcs5::EncryptionScheme::Pbes2(pkcs8::pkcs5::pbes2::Parameters {
            kdf: pkcs8::pkcs5::pbes2::Kdf::Pbkdf2(_),
            ..
        })
    ));
    let plaintext = encrypted_info
        .decrypt(password.trim().as_bytes())
        .expect("password decrypts intermediate PKCS#8");
    let private_key_info = pkcs8::PrivateKeyInfoRef::try_from(plaintext.as_bytes())
        .expect("decrypted intermediate PKCS#8");
    // Catches the dalek encoder's optional public-key extension: OpenSSL and
    // the Controller's cryptography loader reject that OneAsymmetricKey form.
    assert_eq!(private_key_info.version(), pkcs8::Version::V1);
    ed25519_dalek::SigningKey::from_pkcs8_encrypted_pem(
        &encrypted_intermediate,
        password.trim().as_bytes(),
    )
    .expect("password decrypts intermediate Ed25519 key");

    let public_jwk: Value = serde_json::from_slice(
        &std::fs::read(secrets.join("agent-ca-provisioner-public-jwk")).expect("public JWK"),
    )
    .expect("public JWK JSON");
    let canonical = format!(
        "{{\"crv\":\"P-256\",\"kty\":\"EC\",\"x\":\"{}\",\"y\":\"{}\"}}",
        public_jwk["x"].as_str().expect("x"),
        public_jwk["y"].as_str().expect("y")
    );
    let expected_kid = Base64UrlUnpadded::encode_string(&Sha256::digest(canonical.as_bytes()));
    assert_eq!(public_jwk["kid"], expected_kid);
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

    let temporary = tempdir().expect("temporary directory");
    let bundle = clone_pki_bundle(temporary.path());
    let secrets = bundle.join("secrets");
    let issuer_identity_before = std::fs::read(secrets.join("agent-ca-provisioner-public-jwk"))
        .expect("private JWK before upgrade");
    let certificate_before =
        std::fs::read(secrets.join("controller-server-certificate")).expect("cert before upgrade");
    // A bundle from before the single control hostname still carries the
    // separately configured names and the provisioner KID; they are inert.
    let mut environment = std::fs::read_to_string(bundle.join(".env")).expect("environment");
    environment.push_str(
        "VONK_AGENT_ENROLL_HOSTNAME=enroll.example.test\n\
         VONK_AGENT_HOSTNAME=agents.example.test\n\
         VONK_REGISTRY_HOSTNAME=registry.example.test\n\
         AGENT_CA_PROVISIONER_KID=legacy-kid\n",
    );
    std::fs::write(bundle.join(".env"), environment).expect("legacy environment");
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
        std::fs::read(secrets.join("agent-ca-provisioner-public-jwk"))
            .expect("private JWK after upgrade"),
        issuer_identity_before
    );
    assert_eq!(
        std::fs::read(secrets.join("controller-server-certificate")).expect("cert after upgrade"),
        certificate_before
    );

    std::fs::remove_file(secrets.join("controller-server-key")).expect("remove one PKI member");
    upgrade_pki_bundle(temporary.path()).expect("lost member restored from verified publication");
    assert_eq!(
        std::fs::read(secrets.join("agent-ca-provisioner-public-jwk")).unwrap(),
        issuer_identity_before
    );
    assert_eq!(
        std::fs::read(secrets.join("controller-server-certificate")).unwrap(),
        certificate_before
    );
    assert_controller_pair_is_current_and_coherent(&secrets);
    upgrade_pki_bundle(temporary.path()).expect("fresh upgrade admitted after repair");
}

#[test]
fn lost_and_malformed_generated_config_repairs_without_rotating_authority() {
    let temporary = tempdir().unwrap();
    let bundle = clone_pki_bundle(temporary.path());
    let secrets = bundle.join("secrets");
    let authority = authority_snapshot(&secrets);
    let key = std::fs::read(secrets.join("controller-server-key")).unwrap();
    let environment = std::fs::read(bundle.join(".env")).unwrap();
    for damaged in [
        None,
        Some(b"garbage\nVONK_CONTROL_HOSTNAME=\"unterminated\n".as_slice()),
    ] {
        match damaged {
            None => std::fs::remove_file(bundle.join(".env")).unwrap(),
            Some(content) => std::fs::write(bundle.join(".env"), content).unwrap(),
        }
        upgrade_pki_bundle(temporary.path()).unwrap();
        assert_eq!(std::fs::read(bundle.join(".env")).unwrap(), environment);
        assert_eq!(authority_snapshot(&secrets), authority);
        assert_eq!(
            std::fs::read(secrets.join("controller-server-key")).unwrap(),
            key
        );
        upgrade_pki_bundle(temporary.path()).unwrap();
    }
}

#[test]
fn missing_or_non_directory_secret_root_rehydrates_verified_authority() {
    for fault in 0..2 {
        let temporary = tempdir().unwrap();
        let bundle = clone_pki_bundle(temporary.path());
        let secrets = bundle.join("secrets");
        let authority = authority_snapshot(&secrets);
        let certificate = std::fs::read(secrets.join("controller-server-certificate")).unwrap();
        std::fs::rename(&secrets, bundle.join("lost-secrets")).unwrap();
        if fault == 1 {
            std::fs::write(&secrets, b"damaged directory projection").unwrap();
        }
        upgrade_pki_bundle(temporary.path()).unwrap();
        assert_eq!(authority_snapshot(&secrets), authority);
        assert_eq!(
            std::fs::read(secrets.join("controller-server-certificate")).unwrap(),
            certificate
        );
        assert_controller_pair_is_current_and_coherent(&secrets);
        upgrade_pki_bundle(temporary.path()).unwrap();
        assert_eq!(authority_snapshot(&secrets), authority);
    }
}

#[test]
fn lost_install_response_reuses_published_authority_on_a_fresh_install_request() {
    let temporary = tempdir().unwrap();
    let bundle = clone_pki_bundle(temporary.path());
    let secrets = bundle.join("secrets");
    let authority = authority_snapshot(&secrets);
    let key = std::fs::read(secrets.join("controller-server-key")).unwrap();
    for _ in 0..2 {
        let mut output = Vec::new();
        prepare(
            &pki_payload(),
            SetupRequest::install(temporary.path()),
            &mut PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output),
            &SequenceGenerator::new([]),
        )
        .unwrap();
        assert_eq!(authority_snapshot(&secrets), authority);
        assert_eq!(
            std::fs::read(secrets.join("controller-server-key")).unwrap(),
            key
        );
        assert!(output.is_empty());
    }
}
