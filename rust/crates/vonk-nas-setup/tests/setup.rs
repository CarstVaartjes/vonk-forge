use std::io::Cursor;
use std::path::Path;

use tempfile::tempdir;
use vonk_nas_setup::{
    CanonicalTemplatePayload, PromptIo, SecretGenerationError, SecretGenerator, SecretInput,
    SetupOutcome, SetupRequest, parse_template_payload, prepare,
};

struct FixedSecretGenerator;

struct SizedSecretGenerator;

struct FixedHiddenInput;

impl<R: std::io::BufRead, W: std::io::Write> SecretInput<R, W> for FixedHiddenInput {
    fn read_secret(
        &mut self,
        _label: &str,
        _reader: &mut R,
        _writer: &mut W,
    ) -> std::io::Result<String> {
        Ok("hidden-secret-answer".to_owned())
    }
}

impl SecretGenerator for FixedSecretGenerator {
    fn generate(&self, bytes: usize) -> Result<String, SecretGenerationError> {
        assert_eq!(bytes, 16);
        Ok("generated-secret".to_owned())
    }
}

impl SecretGenerator for SizedSecretGenerator {
    fn generate(&self, bytes: usize) -> Result<String, SecretGenerationError> {
        Ok(format!("generated-{bytes}-byte-secret"))
    }
}

fn payload() -> CanonicalTemplatePayload {
    parse_template_payload(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services:\n  api:\n    image: example.invalid/api:latest\n",
          "preflight": ["Complete the Tailscale prerequisites."],
          "required_values": [
            {"env": "VONK_PUBLIC_HOST", "prompt": "Public hostname"}
          ],
          "secrets": [
            {"file": "database-password", "prompt": "Database password"}
          ],
          "generated_secrets": {
            "random_text": [{"file": "generated-token", "bytes": 16}]
          },
          "hermes": {
            "env": "VONK_HERMES_ENABLED",
            "prompt": "Enable Hermes?",
            "enabled_value": "true",
            "disabled_value": "false"
          }
        }"#,
    )
    .expect("valid fixture")
}

/// The site-facing part of the real release payload (scripts/build-nas-compose-bundle).
fn site_payload() -> CanonicalTemplatePayload {
    parse_template_payload(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "preflight": ["Complete the Tailscale prerequisites."],
          "internal_values": [],
          "required_values": [
            {"env": "NAS_LAN_IP", "prompt": "Reserved NAS LAN IP", "validation": "ipv4"},
            {"env": "VONK_MANAGEMENT_CIDRS", "prompt": "Trusted Spark management CIDRs", "validation": "cidr_list"},
            {"env": "VONK_DIRECT_FABRIC_CIDRS", "prompt": "Direct GPU fabric CIDRs", "default": "192.168.100.0/24", "validation": "optional_cidr_list"},
            {"env": "VONK_CONTROL_HOSTNAME", "prompt": "Control hostname", "validation": "hostname"}
          ],
          "secrets": [
            {"file": "tailscale-oauth-client-id", "prompt": "Tailscale OAuth client ID", "secure_remote_only": true},
            {"file": "tailscale-oauth-client-secret", "prompt": "Tailscale OAuth client secret", "secure_remote_only": true},
            {"file": "litellm-upstream-key", "prompt": "LiteLLM upstream key", "optional": true},
            {"file": "hf-token", "prompt": "Hugging Face token", "optional": true}
          ],
          "install_modes": {
            "prompt": "Install mode",
            "default": "secure-remote",
            "lab_value": "lab",
            "secure_remote_value": "secure-remote",
            "lab_values": [
              {"env": "VONK_CONTROL_HOSTNAME", "value": "vonk-forge.local"}
            ]
          },
          "generated_secrets": {
            "random_text": [
              {"file": "admin-password", "bytes": 24},
              {"file": "hermes-litellm-key", "bytes": 32, "prefix": "sk-"}
            ]
          },
          "hermes": {
            "env": "COMPOSE_PROFILES",
            "prompt": "Enable Hermes?",
            "enabled_value": "hermes",
            "disabled_value": ""
          }
        }"#,
    )
    .expect("valid site fixture")
}

/// Run one operation with exactly `answers` as input: a missing answer fails
/// with `InputEnded`, and a leftover answer means a prompt was skipped.
fn run_with_answers(
    payload: &CanonicalTemplatePayload,
    request: SetupRequest,
    answers: &str,
    generator: &impl SecretGenerator,
) -> (SetupOutcome, String) {
    let mut input = Cursor::new(answers.as_bytes().to_vec());
    let mut output = Vec::new();
    let result = prepare(
        payload,
        request,
        &mut PromptIo::new(&mut input, &mut output),
        generator,
    )
    .expect("setup succeeds");
    assert_eq!(
        input.position(),
        answers.len() as u64,
        "every answer must be consumed"
    );
    (result, String::from_utf8(output).expect("UTF-8 transcript"))
}

fn environment(root: &Path) -> Vec<String> {
    std::fs::read_to_string(root.join(".env"))
        .expect("environment")
        .lines()
        .map(str::to_owned)
        .collect()
}

fn secret(root: &Path, file: &str) -> String {
    std::fs::read_to_string(root.join("secrets").join(file))
        .unwrap_or_else(|error| panic!("secret {file}: {error}"))
}

#[test]
fn install_creates_only_the_secure_drag_and_drop_bundle() {
    let temporary = tempdir().expect("temporary directory");
    let (result, _) = run_with_answers(
        &payload(),
        SetupRequest::install(temporary.path()),
        "forge.example.test\ntyped-database-password\nn\n",
        &FixedSecretGenerator,
    );

    assert_eq!(
        result.root,
        std::fs::canonicalize(temporary.path())
            .expect("canonical output")
            .join("vonk-forge")
    );
    let mut entries = std::fs::read_dir(&result.root)
        .expect("bundle directory")
        .map(|entry| entry.expect("directory entry").file_name())
        .collect::<Vec<_>>();
    entries.sort();
    assert_eq!(
        entries,
        [
            ".env",
            ".vonk-verified-secrets",
            "backups",
            "backups-offhost",
            "docker-compose.yaml",
            "secrets"
        ]
    );
    assert_eq!(
        std::fs::read_to_string(result.root.join("docker-compose.yaml")).expect("compose"),
        "services:\n  api:\n    image: example.invalid/api:latest\n"
    );
    assert_eq!(
        std::fs::read_to_string(result.root.join(".env")).expect("environment"),
        "VONK_PUBLIC_HOST=forge.example.test\nVONK_HERMES_ENABLED=false\n"
    );
    assert_eq!(
        secret(&result.root, "database-password"),
        "typed-database-password\n"
    );
    assert_eq!(
        secret(&result.root, "generated-token"),
        "generated-secret\n"
    );

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        assert_eq!(
            std::fs::metadata(&result.root)
                .expect("bundle metadata")
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            std::fs::metadata(result.root.join("secrets"))
                .expect("secrets metadata")
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        for directory in ["backups", "backups-offhost", "secrets/gateway"] {
            assert_eq!(
                std::fs::metadata(result.root.join(directory))
                    .expect("backup metadata")
                    .permissions()
                    .mode()
                    & 0o777,
                0o700
            );
        }
        assert_eq!(
            std::fs::metadata(result.root.join("secrets/database-password"))
                .expect("secret metadata")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        assert_eq!(
            std::fs::metadata(result.root.join(".env"))
                .expect("environment metadata")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}

#[test]
fn optional_huggingface_secret_is_prompted_and_can_be_skipped() {
    let payload = parse_template_payload(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "required_values": [],
          "secrets": [
            {
              "file": "hf-token",
              "prompt": "Hugging Face access token (optional; leave blank for public models)",
              "optional": true
            }
          ]
        }"#,
    )
    .expect("valid optional-secret payload");
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(b"\n".to_vec()), &mut output);

    let result = prepare(
        &payload,
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("bundle prepared without an HF token");

    assert_eq!(
        std::fs::read(result.root.join("secrets/hf-token")).expect("HF token file"),
        b""
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        assert_eq!(
            std::fs::metadata(result.root.join("secrets/hf-token"))
                .expect("HF token metadata")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}

#[test]
fn secure_remote_install_asks_only_for_site_inputs_and_generates_the_rest() {
    let temporary = tempdir().expect("temporary directory");
    // Default mode, NAS IP, control hostname, both OAuth values, the optional
    // upstream key, a skipped HF token, and the Hermes question.
    let (result, transcript) = run_with_answers(
        &site_payload(),
        SetupRequest::install(temporary.path()),
        "\n192.168.1.22\nvonk-forge.example.ts.net\noauth-id\noauth-secret\nupstream-key\n\nn\n",
        &SizedSecretGenerator,
    );

    assert_eq!(result.hermes_enabled, Some(false));
    let preflight = transcript
        .find("Complete the Tailscale prerequisites.")
        .expect("preflight shown");
    let first_secret = transcript
        .find("Tailscale OAuth client ID")
        .expect("OAuth prompt shown");
    assert!(preflight < first_secret);
    // Defaulted values are reported, never secrets.
    assert!(transcript.contains("192.168.1.0/24"), "{transcript}");
    for value in ["oauth-secret", "upstream-key", "generated-"] {
        assert!(!transcript.contains(value), "{value} leaked: {transcript}");
    }
    assert_eq!(
        environment(&result.root),
        [
            "COMPOSE_PROFILES=secure-remote",
            "NAS_LAN_IP=192.168.1.22",
            "VONK_MANAGEMENT_CIDRS=192.168.1.0/24",
            "VONK_DIRECT_FABRIC_CIDRS=192.168.100.0/24",
            "VONK_CONTROL_HOSTNAME=vonk-forge.example.ts.net",
        ]
    );
    assert_eq!(
        secret(&result.root, "tailscale-oauth-client-id"),
        "oauth-id\n"
    );
    assert_eq!(
        secret(&result.root, "tailscale-oauth-client-secret"),
        "oauth-secret\n"
    );
    assert_eq!(
        secret(&result.root, "litellm-upstream-key"),
        "upstream-key\n"
    );
    assert_eq!(secret(&result.root, "hf-token"), "");
    assert_eq!(
        secret(&result.root, "admin-password"),
        "generated-24-byte-secret\n"
    );
    assert_eq!(
        secret(&result.root, "hermes-litellm-key"),
        "sk-generated-32-byte-secret\n"
    );
}

#[test]
fn lab_install_asks_only_for_the_nas_address_and_optional_secrets() {
    let temporary = tempdir().expect("temporary directory");
    let (result, _) = run_with_answers(
        &site_payload(),
        SetupRequest::install(temporary.path()),
        "lab\n10.0.4.9\n\nhf_test_token\n",
        &SizedSecretGenerator,
    );

    assert_eq!(result.hermes_enabled, Some(false));
    assert_eq!(
        environment(&result.root),
        [
            "COMPOSE_PROFILES=",
            "VONK_CONTROL_HOSTNAME=vonk-forge.local",
            "NAS_LAN_IP=10.0.4.9",
            "VONK_MANAGEMENT_CIDRS=10.0.4.0/24",
            "VONK_DIRECT_FABRIC_CIDRS=192.168.100.0/24",
        ]
    );
    assert_eq!(secret(&result.root, "tailscale-oauth-client-id"), "");
    assert_eq!(secret(&result.root, "tailscale-oauth-client-secret"), "");
    assert_eq!(secret(&result.root, "litellm-upstream-key"), "");
    assert_eq!(secret(&result.root, "hf-token"), "hf_test_token\n");
    assert_eq!(
        secret(&result.root, "admin-password"),
        "generated-24-byte-secret\n"
    );

    // A lab upgrade restores missing lab-only and optional secrets empty,
    // without asking.
    std::fs::remove_file(result.root.join("secrets/tailscale-oauth-client-secret"))
        .expect("remove lab-only secret");
    std::fs::remove_file(result.root.join("secrets/hf-token")).expect("remove optional secret");
    let (_, transcript) = run_with_answers(
        &site_payload(),
        SetupRequest::upgrade(temporary.path()),
        "",
        &SizedSecretGenerator,
    );
    assert!(transcript.is_empty(), "{transcript}");
    assert_eq!(secret(&result.root, "tailscale-oauth-client-secret"), "");
    assert_eq!(secret(&result.root, "hf-token"), "");
}

#[test]
fn secure_remote_bundle_upgrades_and_toggles_hermes_beside_its_profile() {
    let payload = parse_template_payload(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "install_modes": {
            "prompt": "Install mode",
            "default": "secure-remote",
            "lab_value": "lab",
            "secure_remote_value": "secure-remote",
            "lab_values": []
          },
          "hermes": {
            "env": "COMPOSE_PROFILES",
            "prompt": "Enable Hermes?",
            "enabled_value": "hermes",
            "disabled_value": ""
          }
        }"#,
    )
    .expect("valid secure-remote fixture");
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(b"secure-remote\nn\n".to_vec()), &mut output);
    let result = prepare(
        &payload,
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("secure remote bundle prepared");
    let environment = result.root.join(".env");

    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);
    let upgraded = prepare(
        &payload,
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("ordinary upgrade of a secure-remote bundle");
    assert_eq!(upgraded.hermes_enabled, Some(false));
    assert!(output.is_empty());
    assert_eq!(
        std::fs::read_to_string(&environment).expect("environment"),
        "COMPOSE_PROFILES=secure-remote\n"
    );

    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);
    prepare(
        &payload,
        SetupRequest::upgrade(temporary.path()).with_hermes_enabled(true),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("Hermes enabled beside secure-remote");
    assert_eq!(
        std::fs::read_to_string(&environment).expect("environment"),
        "COMPOSE_PROFILES=\"secure-remote,hermes\"\n"
    );

    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);
    let preserved = prepare(
        &payload,
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("enabled Hermes preserved beside secure-remote");
    assert_eq!(preserved.hermes_enabled, Some(true));
    assert_eq!(
        std::fs::read_to_string(&environment).expect("environment"),
        "COMPOSE_PROFILES=\"secure-remote,hermes\"\n"
    );
}

#[test]
fn payload_rejects_multiline_preflight_items() {
    let payload = serde_json::json!({
        "schema_version": 2,
        "docker_compose_yaml": "services: {}\n",
        "preflight": ["safe", "not\na checklist item"],
        "required_values": [],
        "secrets": []
    });

    parse_template_payload(&serde_json::to_vec(&payload).expect("payload JSON"))
        .expect_err("multiline checklist rejected");
}

fn compose_payload(compose: &str) -> CanonicalTemplatePayload {
    parse_template_payload(
        &serde_json::to_vec(&serde_json::json!({
            "schema_version": 2,
            "docker_compose_yaml": compose,
            "required_values": [],
            "secrets": []
        }))
        .expect("payload JSON"),
    )
    .expect("valid payload")
}

#[test]
fn upgrade_replaces_compose_and_drops_retired_runtime_configs() {
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::new()), &mut output);
    let installed = prepare(
        &compose_payload("services: {}\n"),
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("bundle installed");
    // Earlier releases rendered runtime configs into the bundle.
    let retired = installed.root.join("secrets/runtime-configs");
    std::fs::create_dir(&retired).expect("retired runtime configs");
    std::fs::write(retired.join("vonk_runtime_0123456789abcdef"), "old\n")
        .expect("retired runtime config");

    let upgraded = prepare(
        &compose_payload("services:\n  upgraded: {}\n"),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("bundle upgraded");
    assert_eq!(upgraded.root, installed.root);
    assert_eq!(
        std::fs::read_to_string(upgraded.root.join("docker-compose.yaml"))
            .expect("upgraded compose"),
        "services:\n  upgraded: {}\n"
    );
    assert!(!retired.exists());
}

#[test]
fn upgrade_adds_private_backup_directories_to_an_existing_bundle() {
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::new()), &mut output);
    let installed = prepare(
        &compose_payload("services: {}\n"),
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("bundle installed");
    for directory in ["backups", "backups-offhost"] {
        std::fs::remove_dir(installed.root.join(directory)).expect("simulate pre-backup bundle");
    }

    prepare(
        &compose_payload("services: {}\n"),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("backup directories added during upgrade");

    #[cfg(unix)]
    for directory in ["backups", "backups-offhost"] {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(installed.root.join(directory))
                .expect("backup metadata")
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
    }

    // The gateway mount is created if missing, keeps the mode the Controller
    // gave it, and its Controller-owned contents are not validated as secrets.
    let gateway = installed.root.join("secrets/gateway");
    assert!(gateway.is_dir());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&gateway, std::fs::Permissions::from_mode(0o770))
            .expect("controller mode");
    }
    std::fs::create_dir(gateway.join("nested")).expect("controller-owned entry");

    // A bundle that already has both directories upgrades again.
    prepare(
        &compose_payload("services: {}\n"),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("repeat upgrade accepts the backup directories");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&gateway)
                .expect("gateway")
                .permissions()
                .mode()
                & 0o777,
            0o770
        );
    }
}

fn write_existing_bundle(root: &Path) {
    let mut previous = payload();
    previous.docker_compose_yaml = "old compose\n".to_owned();
    previous.generated_secrets.as_mut().unwrap().random_text[0].file = "site-secret".to_owned();
    let (installed, _) = run_with_answers(
        &previous,
        SetupRequest::install(root),
        "kept.example.test\nkept-database-password\nn\n",
        &FixedSecretGenerator,
    );
    std::fs::write(installed.root.join("secrets/site-secret"), "kept-secret\n").unwrap();
}

#[test]
fn explicit_upgrade_atomically_replaces_only_compose() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    std::fs::write(
        bundle.join("docker-compose.yaml"),
        "services:\n  api:\n    image: example.invalid/api@sha256:old\n",
    )
    .expect("old pinned compose");
    #[cfg(unix)]
    let old_inode = {
        use std::os::unix::fs::MetadataExt;
        std::fs::metadata(bundle.join("docker-compose.yaml"))
            .expect("old compose metadata")
            .ino()
    };
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("bundle upgraded");

    assert_eq!(
        std::fs::read_to_string(bundle.join("docker-compose.yaml")).expect("compose"),
        "services:\n  api:\n    image: example.invalid/api:latest\n"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join(".env")).expect("environment"),
        "VONK_PUBLIC_HOST=kept.example.test\nVONK_HERMES_ENABLED=false\n"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join("secrets/site-secret")).expect("secret"),
        "kept-secret\n"
    );
    assert!(
        output.is_empty(),
        "ordinary upgrades must preserve Hermes without prompting"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        assert_ne!(
            std::fs::metadata(bundle.join("docker-compose.yaml"))
                .expect("new compose metadata")
                .ino(),
            old_inode,
            "compose replacement must use rename, not in-place truncation"
        );
    }
}

#[test]
fn upgrade_prompts_only_for_new_undefaulted_inputs_and_preserves_existing_values() {
    let payload = parse_template_payload(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services:\n  api:\n    image: example.invalid/api@sha256:new\n",
          "internal_values": [
            {"env": "SITE_LOCAL", "value": "release-owned"}
          ],
          "required_values": [
            {"env": "VONK_PUBLIC_HOST", "prompt": "Public hostname"},
            {"env": "NEW_RELEASE_VALUE", "prompt": "New release value"},
            {"env": "NEW_DEFAULTED_VALUE", "prompt": "New defaulted value", "default": "the-default"}
          ],
          "secrets": [
            {"file": "database-password", "prompt": "Database password"},
            {"file": "new-release-secret", "prompt": "New release secret"},
            {"file": "new-optional-secret", "prompt": "New optional secret", "optional": true}
          ],
          "generated_secrets": {
            "random_text": [
              {"file": "site-secret", "bytes": 16},
              {"file": "new-generated-secret", "bytes": 16}
            ],
            "ed25519_pkcs8_pem": [{"file": "new-signing-key"}]
          },
          "hermes": {
            "env": "VONK_HERMES_ENABLED",
            "prompt": "Enable Hermes?",
            "enabled_value": "true",
            "disabled_value": "false"
          }
        }"#,
    )
    .expect("valid upgraded payload");
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");

    let (result, _) = run_with_answers(
        &payload,
        SetupRequest::upgrade(temporary.path()),
        "new-value\nnew-secret-value\n",
        &FixedSecretGenerator,
    );

    assert_eq!(result.hermes_enabled, Some(false));
    assert_eq!(
        environment(&bundle),
        [
            "VONK_PUBLIC_HOST=kept.example.test",
            "VONK_HERMES_ENABLED=false",
            "SITE_LOCAL=release-owned",
            "NEW_RELEASE_VALUE=new-value",
            "NEW_DEFAULTED_VALUE=the-default",
        ]
    );
    assert_eq!(
        secret(&bundle, "database-password"),
        "kept-database-password\n"
    );
    assert_eq!(secret(&bundle, "site-secret"), "kept-secret\n");
    assert_eq!(secret(&bundle, "new-release-secret"), "new-secret-value\n");
    assert_eq!(secret(&bundle, "new-optional-secret"), "");
    assert_eq!(
        secret(&bundle, "new-generated-secret"),
        "generated-secret\n"
    );
    assert!(secret(&bundle, "new-signing-key").starts_with("-----BEGIN PRIVATE KEY-----"));
}

#[test]
fn upgrade_rewrites_an_old_environment_with_only_known_keys() {
    let payload = parse_template_payload(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "internal_values": [],
          "required_values": [
            {"env": "NAS_LAN_IP", "prompt": "Reserved NAS LAN IP", "validation": "ipv4"},
            {"env": "VONK_CONTROL_HOSTNAME", "prompt": "Control hostname", "validation": "hostname"}
          ],
          "optional_values": ["VONK_BACKUP_OFFHOST_PATH", "VONK_RECIPE_LIBRARY_RELEASE"],
          "install_modes": {
            "prompt": "Install mode",
            "lab_value": "lab",
            "secure_remote_value": "secure-remote",
            "lab_values": []
          }
        }"#,
    )
    .expect("valid payload");
    let temporary = tempdir().expect("temporary directory");
    let bundle = temporary.path().join("vonk-forge");
    std::fs::create_dir_all(bundle.join("secrets")).expect("bundle");
    std::fs::write(bundle.join("docker-compose.yaml"), "old compose\n").expect("compose");
    std::fs::write(
        bundle.join(".env"),
        "COMPOSE_PROJECT_NAME=vonk-forge-control\n\
         COMPOSE_PROFILES=secure-remote\n\
         NAS_LAN_IP=192.168.1.20\n\
         VONK_AGENT_ENROLL_HOSTNAME=enroll.example.test\n\
         VONK_CONTROL_HOSTNAME=vonk.example.test\n\
         AGENT_CA_PROVISIONER_KID=old-kid\n\
         VONK_BACKUP_OFFHOST_PATH=/mnt/offhost\n",
    )
    .expect("old environment");

    let (result, transcript) = run_with_answers(
        &payload,
        SetupRequest::upgrade(temporary.path()),
        "",
        &FixedSecretGenerator,
    );

    assert_eq!(
        environment(&bundle),
        [
            "COMPOSE_PROFILES=secure-remote",
            "NAS_LAN_IP=192.168.1.20",
            "VONK_CONTROL_HOSTNAME=vonk.example.test",
            "VONK_BACKUP_OFFHOST_PATH=/mnt/offhost",
        ]
    );
    assert_eq!(
        result.dropped_environment,
        [
            "COMPOSE_PROJECT_NAME",
            "VONK_AGENT_ENROLL_HOSTNAME",
            "AGENT_CA_PROVISIONER_KID"
        ]
    );
    assert!(transcript.is_empty(), "dropped keys are never prompted for");
}

#[test]
fn upgrade_never_introduces_a_compose_project_name() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    std::fs::write(
        bundle.join(".env"),
        "COMPOSE_PROFILES=\nNAS_LAN_IP=192.168.1.20\n\
         VONK_MANAGEMENT_CIDRS=192.168.1.0/24\nVONK_DIRECT_FABRIC_CIDRS=192.168.100.0/24\n\
         VONK_CONTROL_HOSTNAME=vonk.example.test\n",
    )
    .expect("old environment");

    let (result, _) = run_with_answers(
        &site_payload(),
        SetupRequest::upgrade(temporary.path()),
        "",
        &SizedSecretGenerator,
    );

    assert!(result.dropped_environment.is_empty());
    assert!(
        environment(&bundle)
            .iter()
            .all(|line| !line.starts_with("COMPOSE_PROJECT_NAME")),
        "{:?}",
        environment(&bundle)
    );
}

#[test]
fn upgrade_can_disable_hermes_without_deleting_its_secrets() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    std::fs::write(
        bundle.join(".env"),
        "VONK_PUBLIC_HOST=kept.example.test\nVONK_HERMES_ENABLED=true\n",
    )
    .expect("enabled environment");
    std::fs::write(bundle.join("secrets/generated-token"), "kept-token\n").expect("token");

    let (result, transcript) = run_with_answers(
        &payload(),
        SetupRequest::upgrade(temporary.path()).with_hermes_enabled(false),
        "",
        &FixedSecretGenerator,
    );

    assert_eq!(result.hermes_enabled, Some(false));
    assert!(transcript.is_empty());
    assert_eq!(
        environment(&bundle),
        [
            "VONK_PUBLIC_HOST=kept.example.test",
            "VONK_HERMES_ENABLED=false"
        ]
    );
    assert_eq!(secret(&bundle, "generated-token"), "kept-token\n");
}

#[cfg(unix)]
#[test]
fn upgrade_preserves_an_unconsumed_secret_symlink() {
    use std::os::unix::fs::symlink;
    let temporary = tempdir().unwrap();
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let outside = temporary.path().join("outside");
    std::fs::write(&outside, b"unrelated user bytes").unwrap();
    let link = bundle.join("secrets/unconsumed-link");
    symlink(&outside, &link).unwrap();
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);
    for _ in 0..2 {
        prepare(
            &payload(),
            SetupRequest::upgrade(temporary.path()),
            &mut prompt,
            &FixedSecretGenerator,
        )
        .unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), outside);
        assert_eq!(std::fs::read(&outside).unwrap(), b"unrelated user bytes");
        assert_eq!(
            std::fs::read_to_string(bundle.join("docker-compose.yaml")).unwrap(),
            payload().docker_compose_yaml
        );
    }
}

#[cfg(unix)]
#[test]
fn consumed_secret_symlink_has_no_unverified_effect_and_valid_input_is_admitted() {
    use std::os::unix::fs::symlink;
    let temporary = tempdir().unwrap();
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let secret = bundle.join("secrets/database-password");
    let verified = std::fs::read(&secret).unwrap();
    let outside = temporary.path().join("outside");
    std::fs::write(&outside, b"untrusted bytes").unwrap();
    std::fs::remove_file(&secret).unwrap();
    symlink(&outside, &secret).unwrap();
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);
    prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .unwrap();
    assert_eq!(std::fs::read(&secret).unwrap(), verified);
    assert_eq!(
        std::fs::read_to_string(bundle.join("docker-compose.yaml")).unwrap(),
        payload().docker_compose_yaml
    );
    assert_eq!(std::fs::read(&outside).unwrap(), b"untrusted bytes");
    prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .unwrap();
    assert_eq!(std::fs::read(&secret).unwrap(), verified);
}

#[cfg(unix)]
#[test]
fn damaged_generated_compose_is_replaced_and_old_bytes_are_preserved() {
    use std::os::unix::fs::symlink;
    for fault in 0..3 {
        let temporary = tempdir().unwrap();
        write_existing_bundle(temporary.path());
        let bundle = temporary.path().join("vonk-forge");
        let compose = bundle.join("docker-compose.yaml");
        let outside = temporary.path().join("outside");
        std::fs::write(&outside, b"user data").unwrap();
        std::fs::remove_file(&compose).unwrap();
        match fault {
            0 => {}
            1 => {
                std::fs::create_dir(&compose).unwrap();
                std::fs::write(compose.join("preserved"), b"old local state").unwrap();
            }
            _ => symlink(&outside, &compose).unwrap(),
        }
        let mut output = Vec::new();
        let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);
        for _ in 0..2 {
            prepare(
                &payload(),
                SetupRequest::upgrade(temporary.path()),
                &mut prompt,
                &FixedSecretGenerator,
            )
            .unwrap();
            assert_eq!(
                std::fs::read_to_string(&compose).unwrap(),
                payload().docker_compose_yaml
            );
            assert_eq!(std::fs::read(&outside).unwrap(), b"user data");
        }
        if fault == 1 {
            assert!(std::fs::read_dir(&bundle).unwrap().flatten().any(|entry| {
                std::fs::read(entry.path().join("preserved/preserved"))
                    .is_ok_and(|bytes| bytes == b"old local state")
            }));
        }
    }
}

#[cfg(unix)]
#[test]
fn cleanup_loss_does_not_hide_publication_or_block_a_fresh_upgrade() {
    use std::os::unix::fs::PermissionsExt;
    let temporary = tempdir().unwrap();
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let retired = bundle.join("secrets/runtime-configs");
    std::fs::create_dir(&retired).unwrap();
    std::fs::write(retired.join("old-config"), b"obsolete").unwrap();
    std::fs::set_permissions(&retired, std::fs::Permissions::from_mode(0o000)).unwrap();
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);
    assert!(
        prepare(
            &payload(),
            SetupRequest::upgrade(temporary.path()),
            &mut prompt,
            &FixedSecretGenerator
        )
        .is_ok()
    );
    assert_ne!(
        std::fs::read_to_string(bundle.join("docker-compose.yaml")).unwrap(),
        "old compose\n"
    );
    if retired.exists() {
        std::fs::set_permissions(&retired, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    let mut next_output = Vec::new();
    let mut next_prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut next_output);
    assert!(
        prepare(
            &payload(),
            SetupRequest::upgrade(temporary.path()),
            &mut next_prompt,
            &FixedSecretGenerator
        )
        .is_ok()
    );
    assert!(!retired.exists());
}

#[test]
fn upgrade_preserves_unmanaged_top_level_entries_before_writing() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    std::fs::write(bundle.join("legacy-install.sh"), "operator data\n").expect("legacy file");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let result = prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("unconsumed entries cannot block publication");

    assert_ne!(
        std::fs::read_to_string(bundle.join("docker-compose.yaml")).expect("compose"),
        "old compose\n",
        "the current request publishes despite unrelated leftovers"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join("legacy-install.sh")).expect("legacy file"),
        "operator data\n",
        "the installer must not delete an unknown operator file"
    );
    assert_eq!(result.root, std::fs::canonicalize(&bundle).unwrap());
    let mut next_output = Vec::new();
    let mut next_prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut next_output);
    assert!(
        prepare(
            &payload(),
            SetupRequest::upgrade(temporary.path()),
            &mut next_prompt,
            &FixedSecretGenerator
        )
        .is_ok()
    );
}

#[test]
fn upgrade_accepts_a_sync_directory_beside_the_bundle() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let sync = bundle.join(".sync");
    let job = sync.join("sync-job");
    std::fs::create_dir(&sync).expect("sync directory");
    std::fs::create_dir(&job).expect("sync job directory");
    std::fs::write(job.join("bisync.db"), "sync database\n").expect("sync database");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let result = prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("a real .sync directory is tolerated");

    assert_eq!(
        std::fs::read_to_string(job.join("bisync.db")).expect("preserved sync database"),
        "sync database\n",
        "the installer must not touch the sync tool metadata"
    );
    assert_ne!(
        std::fs::read_to_string(result.root.join("docker-compose.yaml")).expect("compose"),
        "old compose\n"
    );
}

#[test]
fn upgrade_preserves_a_sync_regular_file() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    std::fs::write(bundle.join(".sync"), "not a directory\n").expect("sync file");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let result = prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("unconsumed entries cannot block publication");

    assert_ne!(
        std::fs::read_to_string(bundle.join("docker-compose.yaml")).expect("compose"),
        "old compose\n",
        "the current request publishes despite unrelated leftovers"
    );
    assert_eq!(result.root, std::fs::canonicalize(&bundle).unwrap());
    let mut next_output = Vec::new();
    let mut next_prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut next_output);
    assert!(
        prepare(
            &payload(),
            SetupRequest::upgrade(temporary.path()),
            &mut next_prompt,
            &FixedSecretGenerator
        )
        .is_ok()
    );
}

#[cfg(unix)]
#[test]
fn upgrade_preserves_a_sync_symlink() {
    use std::os::unix::fs::symlink;

    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let outside = temporary.path().join("outside");
    std::fs::create_dir(&outside).expect("outside directory");
    symlink(&outside, bundle.join(".sync")).expect("sync symlink");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let result = prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("unconsumed entries cannot block publication");

    assert_ne!(
        std::fs::read_to_string(bundle.join("docker-compose.yaml")).expect("compose"),
        "old compose\n",
        "the current request publishes despite unrelated leftovers"
    );
    assert_eq!(result.root, std::fs::canonicalize(&bundle).unwrap());
    let mut next_output = Vec::new();
    let mut next_prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut next_output);
    assert!(
        prepare(
            &payload(),
            SetupRequest::upgrade(temporary.path()),
            &mut next_prompt,
            &FixedSecretGenerator
        )
        .is_ok()
    );
}

#[test]
fn upgrade_removes_an_empty_interrupted_installer_staging_directory() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let stale_staging = bundle.join(".vonk-forge.setup-16573-0");
    std::fs::create_dir(&stale_staging).expect("stale staging directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let result = prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("empty interrupted staging directory is recovered");

    assert_eq!(
        result.root,
        std::fs::canonicalize(&bundle).expect("canonical bundle")
    );
    assert!(!stale_staging.exists());
    assert_ne!(
        std::fs::read_to_string(result.root.join("docker-compose.yaml")).expect("compose"),
        "old compose\n"
    );
}

#[test]
fn upgrade_preserves_a_nonempty_interrupted_installer_staging_directory() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let stale_staging = bundle.join(".vonk-forge.setup-16573-0");
    std::fs::create_dir(&stale_staging).expect("stale staging directory");
    std::fs::write(stale_staging.join("operator-data"), "preserve me\n").expect("staging content");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let result = prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("unconsumed entries cannot block publication");

    assert_eq!(
        std::fs::read_to_string(stale_staging.join("operator-data"))
            .expect("preserved staging content"),
        "preserve me\n"
    );
    assert_eq!(result.root, std::fs::canonicalize(&bundle).unwrap());
    let mut next_output = Vec::new();
    let mut next_prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut next_output);
    assert!(
        prepare(
            &payload(),
            SetupRequest::upgrade(temporary.path()),
            &mut next_prompt,
            &FixedSecretGenerator
        )
        .is_ok()
    );
}

#[cfg(unix)]
#[test]
fn upgrade_preserves_a_staging_name_symlink_without_touching_its_target() {
    use std::os::unix::fs::symlink;

    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let outside = temporary.path().join("outside");
    std::fs::create_dir(&outside).expect("outside directory");
    symlink(&outside, bundle.join(".vonk-forge.setup-16573-0")).expect("staging symlink");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let result = prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("unconsumed entries cannot block publication");

    assert!(outside.exists());
    assert_eq!(result.root, std::fs::canonicalize(&bundle).unwrap());
    let mut next_output = Vec::new();
    let mut next_prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut next_output);
    assert!(
        prepare(
            &payload(),
            SetupRequest::upgrade(temporary.path()),
            &mut next_prompt,
            &FixedSecretGenerator
        )
        .is_ok()
    );
}

#[test]
fn prompts_retry_invalid_required_values_and_confirmation() {
    let temporary = tempdir().expect("temporary directory");
    let (result, _) = run_with_answers(
        &payload(),
        SetupRequest::install(temporary.path()),
        "\nforge.example.test\n\ndatabase-secret\nperhaps\nyes\n",
        &FixedSecretGenerator,
    );

    assert_eq!(result.hermes_enabled, Some(true));
    assert_eq!(
        std::fs::read_to_string(result.root.join(".env")).expect("environment"),
        "VONK_PUBLIC_HOST=forge.example.test\nVONK_HERMES_ENABLED=true\n"
    );
}

#[test]
fn typed_site_values_reject_invalid_addresses_cidrs_and_hostnames() {
    let payload = parse_template_payload(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "required_values": [
            {"env": "NAS_LAN_IP", "prompt": "NAS IP", "validation": "ipv4"},
            {"env": "SITE_CIDRS", "prompt": "Site CIDRs", "validation": "cidr_list"},
            {"env": "VONK_CONTROL_HOSTNAME", "prompt": "Control hostname", "validation": "hostname"}
          ]
        }"#,
    )
    .expect("valid typed payload");
    let temporary = tempdir().expect("temporary directory");

    let (result, _) = run_with_answers(
        &payload,
        SetupRequest::install(temporary.path()),
        "not-an-ip\n192.168.1.231\n192.168.1.0/99\n192.168.1.0/24,100.64.0.0/10\nhttps://bad/path\ncontrol.example.test\n",
        &FixedSecretGenerator,
    );

    assert_eq!(
        environment(&result.root),
        [
            "NAS_LAN_IP=192.168.1.231",
            "SITE_CIDRS=\"192.168.1.0/24,100.64.0.0/10\"",
            "VONK_CONTROL_HOSTNAME=control.example.test",
        ]
    );
}

#[test]
fn manually_entered_secret_is_never_written_to_prompt_output() {
    let temporary = tempdir().expect("temporary directory");
    let input = Cursor::new(b"forge.example.test\nsuper-secret-answer\nno\n".to_vec());
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(input, &mut output);

    let result = prepare(
        &payload(),
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("bundle prepared");

    assert_eq!(
        std::fs::read_to_string(result.root.join("secrets/database-password")).expect("secret"),
        "super-secret-answer\n"
    );
    assert!(
        !String::from_utf8(output)
            .expect("UTF-8 prompts")
            .contains("super-secret-answer")
    );
}

#[test]
fn hidden_secret_input_bypasses_echoing_prompt_streams() {
    let temporary = tempdir().expect("temporary directory");
    let input = Cursor::new(b"forge.example.test\nno\n".to_vec());
    let mut output = Vec::new();
    let mut prompt = PromptIo::with_secret_input(input, &mut output, FixedHiddenInput);

    let result = prepare(
        &payload(),
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("bundle prepared with hidden input");

    assert_eq!(
        std::fs::read_to_string(result.root.join("secrets/database-password")).expect("secret"),
        "hidden-secret-answer\n"
    );
    assert!(
        !String::from_utf8(output)
            .expect("UTF-8 prompts")
            .contains("hidden-secret-answer")
    );
}

#[test]
fn os_secret_generator_returns_fresh_hex_encoded_entropy() {
    use vonk_nas_setup::OsSecretGenerator;

    let first = OsSecretGenerator.generate(32).expect("first secret");
    let second = OsSecretGenerator.generate(32).expect("second secret");

    assert_eq!(first.len(), 64);
    assert!(first.bytes().all(|byte| byte.is_ascii_hexdigit()));
    assert_ne!(first, second);
}

#[test]
fn payload_rejects_secret_path_traversal() {
    let error = parse_template_payload(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "required_values": [],
          "secrets": [{"file": "../outside", "prompt": "Unsafe"}],
          "hermes": null
        }"#,
    )
    .expect_err("unsafe secret name rejected");

    assert!(error.to_string().contains("invalid secret filename"));
}

#[cfg(unix)]
#[test]
fn install_accepts_a_real_output_below_a_symlinked_ancestor() {
    use std::os::unix::fs::symlink;

    let temporary = tempdir().expect("temporary directory");
    let actual = temporary.path().join("actual");
    let output = actual.join("output");
    std::fs::create_dir_all(&output).expect("real output");
    let linked = temporary.path().join("linked");
    symlink(&actual, &linked).expect("ancestor symlink");
    let requested = linked.join("output");
    let expected = std::fs::canonicalize(&requested)
        .expect("canonical output")
        .join("vonk-forge");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(
        Cursor::new(b"forge.example.test\ndatabase-secret\nn\n".to_vec()),
        &mut output,
    );

    let result = prepare(
        &payload(),
        SetupRequest::install(&requested),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("real output below a symlinked ancestor is accepted");

    assert_eq!(result.root, expected);
}

#[cfg(unix)]
#[test]
fn install_rejects_a_symlinked_output_root() {
    use std::os::unix::fs::symlink;

    let temporary = tempdir().expect("temporary directory");
    let actual = temporary.path().join("actual");
    std::fs::create_dir(&actual).expect("actual output");
    let linked = temporary.path().join("linked");
    symlink(&actual, &linked).expect("output symlink");
    for requested in [linked.clone(), linked.join(".")] {
        let mut output = Vec::new();
        let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

        let error = prepare(
            &payload(),
            SetupRequest::install(&requested),
            &mut prompt,
            &FixedSecretGenerator,
        )
        .expect_err("symlinked output rejected");

        assert!(error.to_string().contains("symbolic link"));
    }
    assert!(!actual.join("vonk-forge").exists());
}

#[test]
fn install_creates_safe_nested_secret_paths() {
    let payload = parse_template_payload(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "required_values": [],
          "generated_secrets": {
            "random_text": [{"file": "step-ca/ca.json", "bytes": 16}]
          },
          "hermes": null
        }"#,
    )
    .expect("nested secret path is valid");
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let result = prepare(
        &payload,
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("nested secret prepared");

    assert_eq!(
        std::fs::read_to_string(result.root.join("secrets/step-ca/ca.json"))
            .expect("nested secret"),
        "generated-secret\n"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(result.root.join("secrets/step-ca"))
                .expect("nested directory metadata")
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            std::fs::metadata(result.root.join("secrets/step-ca/ca.json"))
                .expect("nested secret metadata")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}

#[test]
fn schema_v2_emits_internal_values_and_maps_hermes_to_compose_profiles() {
    let payload = parse_template_payload(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "internal_values": [
            {"env": "DATABASE_URL_FILE", "value": "./secrets/database-url"},
            {"env": "STEP_CA_CONFIG_FILE", "value": "./secrets/step-ca/ca.json"}
          ],
          "required_values": [],
          "secrets": [],
          "generated_secrets": {
            "random_text": [],
            "ed25519_pkcs8_pem": [],
            "postgres_urls": []
          },
          "step_ca_controller": null,
          "hermes": {
            "env": "COMPOSE_PROFILES",
            "prompt": "Enable Hermes?",
            "enabled_value": "hermes",
            "disabled_value": ""
          }
        }"#,
    )
    .expect("valid v2 payload");
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(b"yes\n".to_vec()), &mut output);

    prepare(
        &payload,
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("bundle prepared");

    assert_eq!(
        std::fs::read_to_string(temporary.path().join("vonk-forge/.env")).expect("environment"),
        "DATABASE_URL_FILE=./secrets/database-url\n\
STEP_CA_CONFIG_FILE=./secrets/step-ca/ca.json\n\
COMPOSE_PROFILES=hermes\n"
    );
    assert!(
        !String::from_utf8(output)
            .expect("UTF-8 prompts")
            .contains("secrets/")
    );
}
