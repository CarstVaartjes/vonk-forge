use std::io::Cursor;
use std::path::Path;

use tempfile::tempdir;
use vonk_nas_setup::{
    CanonicalTemplatePayload, PromptIo, SecretGenerationError, SecretGenerator, SecretInput,
    SetupRequest, prepare,
};

struct FixedSecretGenerator;

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

fn payload() -> CanonicalTemplatePayload {
    CanonicalTemplatePayload::from_json(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services:\n  api:\n    image: example.invalid/api@sha256:abc\n",
          "required_values": [
            {"env": "VONK_PUBLIC_HOST", "prompt": "Public hostname"}
          ],
          "secrets": [
            {"file": "database-password", "prompt": "Database password", "generate_bytes": 16}
          ],
          "hermes": {
            "env": "VONK_HERMES_ENABLED",
            "prompt": "Enable Hermes?",
            "enabled_value": "true",
            "disabled_value": "false",
            "required_values": [],
            "secrets": []
          }
        }"#,
    )
    .expect("valid fixture")
}

#[test]
fn install_creates_only_the_secure_drag_and_drop_bundle() {
    let temporary = tempdir().expect("temporary directory");
    let input = Cursor::new(b"forge.example.test\n\nn\n".to_vec());
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(input, &mut output);

    let result = prepare(
        &payload(),
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("bundle prepared");

    assert_eq!(result.root, temporary.path().join("vonk-forge"));
    let mut entries = std::fs::read_dir(&result.root)
        .expect("bundle directory")
        .map(|entry| entry.expect("directory entry").file_name())
        .collect::<Vec<_>>();
    entries.sort();
    assert_eq!(entries, [".env", "docker-compose.yaml", "secrets"]);
    assert_eq!(
        std::fs::read_to_string(result.root.join("docker-compose.yaml")).expect("compose"),
        "services:\n  api:\n    image: example.invalid/api@sha256:abc\n"
    );
    assert_eq!(
        std::fs::read_to_string(result.root.join(".env")).expect("environment"),
        "VONK_PUBLIC_HOST=forge.example.test\nVONK_HERMES_ENABLED=false\n"
    );
    assert_eq!(
        std::fs::read_to_string(result.root.join("secrets/database-password"))
            .expect("database password"),
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

fn write_existing_bundle(root: &Path) {
    let bundle = root.join("vonk-forge");
    std::fs::create_dir(&bundle).expect("bundle directory");
    std::fs::create_dir(bundle.join("secrets")).expect("secret directory");
    std::fs::write(bundle.join("docker-compose.yaml"), "old compose\n").expect("compose");
    std::fs::write(
        bundle.join(".env"),
        "VONK_PUBLIC_HOST=kept.example.test\nVONK_HERMES_ENABLED=false\nSITE_LOCAL=kept\n",
    )
    .expect("environment");
    std::fs::write(
        bundle.join("secrets/database-password"),
        "kept-database-password\n",
    )
    .expect("database password");
    std::fs::write(bundle.join("secrets/site-secret"), "kept-secret\n").expect("secret");
}

#[test]
fn explicit_upgrade_atomically_replaces_only_compose() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
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
        "services:\n  api:\n    image: example.invalid/api@sha256:abc\n"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join(".env")).expect("environment"),
        "VONK_PUBLIC_HOST=kept.example.test\nVONK_HERMES_ENABLED=false\nSITE_LOCAL=kept\n"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join("secrets/site-secret")).expect("secret"),
        "kept-secret\n"
    );
    assert!(
        output.is_empty(),
        "upgrade must not ask for site-local values"
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
fn upgrade_prompts_only_for_new_release_inputs_and_preserves_existing_values() {
    let payload = CanonicalTemplatePayload::from_json(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services:\n  api:\n    image: example.invalid/api@sha256:new\n",
          "required_values": [
            {"env": "VONK_PUBLIC_HOST", "prompt": "Public hostname"},
            {"env": "NEW_RELEASE_VALUE", "prompt": "New release value"}
          ],
          "secrets": [
            {"file": "database-password", "prompt": "Database password", "generate_bytes": 16},
            {"file": "new-release-secret", "prompt": "New release secret", "generate_bytes": 16}
          ],
          "hermes": {
            "env": "VONK_HERMES_ENABLED",
            "prompt": "Enable Hermes?",
            "enabled_value": "true",
            "disabled_value": "false",
            "required_values": [],
            "secrets": []
          }
        }"#,
    )
    .expect("valid upgraded payload");
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let input = Cursor::new(b"new-value\n\n".to_vec());
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(input, &mut output);

    let result = prepare(
        &payload,
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("bundle upgraded with new inputs");

    assert_eq!(result.hermes_enabled, Some(false));
    assert_eq!(
        std::fs::read_to_string(bundle.join(".env")).expect("environment"),
        "VONK_PUBLIC_HOST=kept.example.test\nVONK_HERMES_ENABLED=false\nSITE_LOCAL=kept\nNEW_RELEASE_VALUE=new-value\n"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join("secrets/database-password"))
            .expect("database password"),
        "kept-database-password\n"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join("secrets/new-release-secret"))
            .expect("new release secret"),
        "generated-secret\n"
    );
}

#[cfg(unix)]
#[test]
fn upgrade_rejects_a_symlink_hidden_inside_secrets() {
    use std::os::unix::fs::symlink;

    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    symlink(
        temporary.path().join("outside"),
        bundle.join("secrets/unsafe-link"),
    )
    .expect("secret symlink");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let error = prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect_err("unsafe bundle rejected");

    assert!(error.to_string().contains("unsafe"));
    assert_eq!(
        std::fs::read_to_string(bundle.join("docker-compose.yaml")).expect("compose"),
        "old compose\n",
        "validation must happen before release-controlled state changes"
    );
}

#[test]
fn upgrade_rejects_unmanaged_top_level_entries_before_writing() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    std::fs::write(bundle.join("legacy-install.sh"), "operator data\n").expect("legacy file");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let error = prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect_err("noncanonical bundle rejected");

    assert!(error.to_string().contains("unexpected top-level entry"));
    assert_eq!(
        std::fs::read_to_string(bundle.join("docker-compose.yaml")).expect("compose"),
        "old compose\n",
        "validation must happen before release-controlled state changes"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join("legacy-install.sh")).expect("legacy file"),
        "operator data\n",
        "the installer must not delete an unknown operator file"
    );
}

#[test]
fn prompts_retry_invalid_required_values_and_confirmation() {
    let temporary = tempdir().expect("temporary directory");
    let input = Cursor::new(b"\nforge.example.test\n\nperhaps\nyes\n".to_vec());
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(input, &mut output);

    let result = prepare(
        &payload(),
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("invalid interactive input is retried");

    assert_eq!(result.hermes_enabled, Some(true));
    assert_eq!(
        std::fs::read_to_string(result.root.join(".env")).expect("environment"),
        "VONK_PUBLIC_HOST=forge.example.test\nVONK_HERMES_ENABLED=true\n"
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
    let error = CanonicalTemplatePayload::from_json(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "required_values": [],
          "secrets": [{"file": "../outside", "prompt": "Unsafe", "generate_bytes": 32}],
          "hermes": null
        }"#,
    )
    .expect_err("unsafe secret name rejected");

    assert!(error.to_string().contains("invalid secret filename"));
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
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let error = prepare(
        &payload(),
        SetupRequest::install(&linked),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect_err("symlinked output rejected");

    assert!(error.to_string().contains("symbolic link"));
    assert!(!actual.join("vonk-forge").exists());
}

#[test]
fn hermes_prompts_are_conditional_on_opt_in() {
    let payload = CanonicalTemplatePayload::from_json(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "required_values": [],
          "secrets": [],
          "hermes": {
            "env": "VONK_HERMES_ENABLED",
            "prompt": "Enable Hermes?",
            "enabled_value": "true",
            "disabled_value": "false",
            "required_values": [{"env": "HERMES_ENDPOINT", "prompt": "Hermes endpoint"}],
            "secrets": [{"file": "hermes-token", "prompt": "Hermes token"}]
          }
        }"#,
    )
    .expect("valid fixture");
    let temporary = tempdir().expect("temporary directory");
    let input = Cursor::new(b"yes\nhttps://hermes.example.test\nprivate-hermes-token\n".to_vec());
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(input, &mut output);

    let result = prepare(
        &payload,
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("Hermes bundle prepared");

    assert_eq!(result.hermes_enabled, Some(true));
    assert_eq!(
        std::fs::read_to_string(result.root.join(".env")).expect("environment"),
        "VONK_HERMES_ENABLED=true\nHERMES_ENDPOINT=https://hermes.example.test\n"
    );
    assert_eq!(
        std::fs::read_to_string(result.root.join("secrets/hermes-token")).expect("token"),
        "private-hermes-token\n"
    );
    assert!(
        !String::from_utf8(output)
            .expect("UTF-8 prompts")
            .contains("private-hermes-token")
    );
}

#[test]
fn install_creates_safe_nested_secret_paths() {
    let payload = CanonicalTemplatePayload::from_json(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "required_values": [],
          "secrets": [
            {"file": "step-ca/ca.json", "prompt": "Step CA configuration", "generate_bytes": 16}
          ],
          "hermes": null
        }"#,
    )
    .expect("nested secret path is valid");
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(b"\n".to_vec()), &mut output);

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
    let payload = CanonicalTemplatePayload::from_json(
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
            "disabled_value": "",
            "required_values": [],
            "secrets": []
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
