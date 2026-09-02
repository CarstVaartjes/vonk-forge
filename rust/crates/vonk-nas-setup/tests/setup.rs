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
          "preflight": ["Complete the Tailscale prerequisites."],
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

#[test]
fn fresh_install_prints_preflight_before_the_first_prompt() {
    let payload = CanonicalTemplatePayload::from_json(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "preflight": [
            "Enable MagicDNS and HTTPS certificates.",
            "Define only the unsuffixed production Services."
          ],
          "required_values": [
            {"env": "VONK_PUBLIC_HOST", "prompt": "Public hostname"}
          ],
          "secrets": [
            {"file": "tailscale-oauth-client-id", "prompt": "Tailscale OAuth client ID", "generate_bytes": null}
          ]
        }"#,
    )
    .expect("valid preflight payload");
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(
        Cursor::new(b"forge.example.test\noauth-client-id\n".to_vec()),
        &mut output,
    );

    prepare(
        &payload,
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("bundle prepared");

    let output = String::from_utf8(output).expect("UTF-8 prompts");
    let preflight = output
        .find("Before continuing, complete this preflight:")
        .expect("preflight is shown");
    let oauth = output
        .find("Tailscale OAuth client ID: ")
        .expect("OAuth prompt is shown");
    assert!(preflight < oauth);
    assert!(output.contains("  [ ] Enable MagicDNS and HTTPS certificates."));
    assert!(output.contains("  [ ] Define only the unsuffixed production Services."));
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

    CanonicalTemplatePayload::from_json(&serde_json::to_vec(&payload).expect("payload JSON"))
        .expect_err("multiline checklist rejected");
}

fn runtime_file_payload(compose: &str, content: &str, mode: u32) -> CanonicalTemplatePayload {
    CanonicalTemplatePayload::from_json(
        &serde_json::to_vec(&serde_json::json!({
            "schema_version": 2,
            "docker_compose_yaml": compose,
            "required_values": [],
            "secrets": [],
            "runtime_files": [
                {
                    "file": "runtime-configs/service.conf",
                    "content": content,
                    "mode": mode
                }
            ]
        }))
        .expect("runtime payload JSON"),
    )
    .expect("valid runtime-file payload")
}

#[test]
fn runtime_files_are_materialized_and_replaced_beneath_the_bundle() {
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::new()), &mut output);
    let installed = prepare(
        &runtime_file_payload("services: {}\n", "first\n", 0o644),
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("runtime file installed");
    let runtime_file = installed.root.join("secrets/runtime-configs/service.conf");
    assert_eq!(
        std::fs::read_to_string(&runtime_file).expect("runtime file"),
        "first\n"
    );

    let upgraded = prepare(
        &runtime_file_payload("services:\n  upgraded: {}\n", "second\n", 0o755),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("runtime file upgraded");
    assert_eq!(upgraded.root, installed.root);
    assert_eq!(
        std::fs::read_to_string(&runtime_file).expect("runtime file"),
        "second\n"
    );
    assert_eq!(
        std::fs::read_to_string(upgraded.root.join("docker-compose.yaml"))
            .expect("upgraded compose"),
        "services:\n  upgraded: {}\n"
    );

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(runtime_file)
                .expect("runtime file metadata")
                .permissions()
                .mode()
                & 0o777,
            0o755
        );
    }
}

#[test]
fn runtime_files_reject_unsafe_paths_and_modes() {
    for (file, mode) in [
        ("../outside", 0o644),
        ("runtime-configs/../outside", 0o644),
        ("runtime-configs/service.conf", 0o777),
    ] {
        let payload = serde_json::json!({
            "schema_version": 2,
            "docker_compose_yaml": "services: {}\n",
            "required_values": [],
            "secrets": [],
            "runtime_files": [{"file": file, "content": "safe\n", "mode": mode}]
        });
        CanonicalTemplatePayload::from_json(
            &serde_json::to_vec(&payload).expect("runtime payload JSON"),
        )
        .expect_err("unsafe runtime file rejected");
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

fn hermes_toggle_payload() -> CanonicalTemplatePayload {
    CanonicalTemplatePayload::from_json(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
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
            "required_values": [
              {"env": "HERMES_ENDPOINT", "prompt": "Hermes endpoint"}
            ],
            "secrets": [
              {"file": "hermes-token", "prompt": "Hermes token"}
            ]
          }
        }"#,
    )
    .expect("valid Hermes toggle payload")
}

#[test]
fn upgrade_can_enable_hermes_in_an_existing_bundle() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let input = Cursor::new(b"https://hermes.example.test\nprivate-hermes-token\n".to_vec());
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(input, &mut output);

    let result = prepare(
        &hermes_toggle_payload(),
        SetupRequest::upgrade(temporary.path()).with_hermes_enabled(true),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("Hermes enabled during upgrade");

    assert_eq!(result.hermes_enabled, Some(true));
    assert_eq!(
        std::fs::read_to_string(bundle.join(".env")).expect("environment"),
        "VONK_PUBLIC_HOST=kept.example.test\n\
VONK_HERMES_ENABLED=true\n\
SITE_LOCAL=kept\n\
HERMES_ENDPOINT=https://hermes.example.test\n"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join("secrets/hermes-token")).expect("Hermes token"),
        "private-hermes-token\n"
    );
}

#[test]
fn upgrade_can_disable_hermes_without_deleting_its_configuration() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    std::fs::write(
        bundle.join(".env"),
        "VONK_PUBLIC_HOST=kept.example.test\n\
VONK_HERMES_ENABLED=true\n\
HERMES_ENDPOINT=https://hermes.example.test\n\
SITE_LOCAL=kept\n",
    )
    .expect("enabled environment");
    std::fs::write(bundle.join("secrets/hermes-token"), "kept-hermes-token\n")
        .expect("Hermes token");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let result = prepare(
        &hermes_toggle_payload(),
        SetupRequest::upgrade(temporary.path()).with_hermes_enabled(false),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("Hermes disabled during upgrade");

    assert_eq!(result.hermes_enabled, Some(false));
    assert_eq!(
        std::fs::read_to_string(bundle.join(".env")).expect("environment"),
        "VONK_PUBLIC_HOST=kept.example.test\n\
VONK_HERMES_ENABLED=false\n\
HERMES_ENDPOINT=https://hermes.example.test\n\
SITE_LOCAL=kept\n"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join("secrets/hermes-token")).expect("Hermes token"),
        "kept-hermes-token\n"
    );
    assert!(output.is_empty());
}

#[test]
fn ordinary_upgrade_preserves_enabled_hermes_without_prompting() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    std::fs::write(
        bundle.join(".env"),
        "VONK_PUBLIC_HOST=kept.example.test\n\
VONK_HERMES_ENABLED=true\n\
HERMES_ENDPOINT=https://hermes.example.test\n\
SITE_LOCAL=kept\n",
    )
    .expect("enabled environment");
    std::fs::write(bundle.join("secrets/hermes-token"), "kept-hermes-token\n")
        .expect("Hermes token");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let result = prepare(
        &hermes_toggle_payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("enabled Hermes state preserved");

    assert_eq!(result.hermes_enabled, Some(true));
    assert!(
        std::fs::read_to_string(bundle.join(".env"))
            .expect("environment")
            .contains("VONK_HERMES_ENABLED=true\n")
    );
    assert!(output.is_empty());
}

#[test]
fn ordinary_upgrade_adds_only_missing_inputs_for_enabled_hermes() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    std::fs::write(
        bundle.join(".env"),
        "VONK_PUBLIC_HOST=kept.example.test\n\
VONK_HERMES_ENABLED=true\n\
SITE_LOCAL=kept\n",
    )
    .expect("enabled environment with missing Hermes input");
    let input = Cursor::new(b"https://hermes.example.test\nprivate-hermes-token\n".to_vec());
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(input, &mut output);

    let result = prepare(
        &hermes_toggle_payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("missing enabled Hermes state repaired");

    assert_eq!(result.hermes_enabled, Some(true));
    assert!(
        std::fs::read_to_string(bundle.join(".env"))
            .expect("environment")
            .contains("HERMES_ENDPOINT=https://hermes.example.test\n")
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join("secrets/hermes-token")).expect("Hermes token"),
        "private-hermes-token\n"
    );
}

#[test]
fn ordinary_upgrade_does_not_add_missing_hermes_inputs_while_disabled() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let result = prepare(
        &hermes_toggle_payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("disabled Hermes state preserved");

    assert_eq!(result.hermes_enabled, Some(false));
    assert!(!bundle.join("secrets/hermes-token").exists());
    assert!(
        !std::fs::read_to_string(bundle.join(".env"))
            .expect("environment")
            .contains("HERMES_ENDPOINT=")
    );
    assert!(output.is_empty());
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
fn upgrade_rejects_a_nonempty_interrupted_installer_staging_directory() {
    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let stale_staging = bundle.join(".vonk-forge.setup-16573-0");
    std::fs::create_dir(&stale_staging).expect("stale staging directory");
    std::fs::write(stale_staging.join("operator-data"), "preserve me\n").expect("staging content");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let error = prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect_err("nonempty staging directory is not deleted");

    assert!(error.to_string().contains("unexpected top-level entry"));
    assert_eq!(
        std::fs::read_to_string(stale_staging.join("operator-data"))
            .expect("preserved staging content"),
        "preserve me\n"
    );
}

#[cfg(unix)]
#[test]
fn upgrade_rejects_a_staging_name_symlink_without_touching_its_target() {
    use std::os::unix::fs::symlink;

    let temporary = tempdir().expect("temporary directory");
    write_existing_bundle(temporary.path());
    let bundle = temporary.path().join("vonk-forge");
    let outside = temporary.path().join("outside");
    std::fs::create_dir(&outside).expect("outside directory");
    symlink(&outside, bundle.join(".vonk-forge.setup-16573-0")).expect("staging symlink");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);

    let error = prepare(
        &payload(),
        SetupRequest::upgrade(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect_err("staging-name symlink is rejected");

    assert!(error.to_string().contains("unexpected top-level entry"));
    assert!(outside.exists());
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
fn typed_site_values_reject_invalid_addresses_cidrs_hostnames_and_origins() {
    let payload = CanonicalTemplatePayload::from_json(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "required_values": [
            {"env": "NAS_LAN_IP", "prompt": "NAS IP", "validation": "ipv4"},
            {"env": "VONK_MANAGEMENT_CIDRS", "prompt": "Management CIDRs", "validation": "cidr_list"},
            {"env": "VONK_DIRECT_FABRIC_CIDRS", "prompt": "Fabric CIDRs", "default": "", "validation": "optional_cidr_list"},
            {"env": "VONK_OPERATOR_JURISDICTION", "prompt": "Jurisdiction", "validation": "jurisdiction"},
            {"env": "VONK_CONTROL_HOSTNAME", "prompt": "Control hostname", "validation": "hostname"}
          ],
          "secrets": [],
          "hermes": {
            "env": "COMPOSE_PROFILES",
            "prompt": "Enable Hermes?",
            "enabled_value": "hermes",
            "disabled_value": "",
            "required_values": [
              {"env": "HERMES_DASHBOARD_ORIGIN", "prompt": "Dashboard", "validation": "https_origin"}
            ],
            "secrets": []
          }
        }"#,
    )
    .expect("valid typed payload");
    let temporary = tempdir().expect("temporary directory");
    let input = Cursor::new(
        b"not-an-ip\n192.168.1.231\n192.168.1.0/99\n192.168.1.0/24,100.64.0.0/10\n\nnl\nZZ\nNL\nhttps://bad/path\ncontrol.example.test\nyes\nhttp://dashboard.example.test\nhttps://dashboard.example.test\n".to_vec(),
    );
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(input, &mut output);

    let result = prepare(
        &payload,
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("invalid typed values are retried");

    assert_eq!(
        std::fs::read_to_string(result.root.join(".env")).expect("environment"),
        "NAS_LAN_IP=192.168.1.231\n\
VONK_MANAGEMENT_CIDRS=\"192.168.1.0/24,100.64.0.0/10\"\n\
VONK_DIRECT_FABRIC_CIDRS=\n\
VONK_OPERATOR_JURISDICTION=NL\n\
VONK_CONTROL_HOSTNAME=control.example.test\n\
COMPOSE_PROFILES=hermes\n\
HERMES_DASHBOARD_ORIGIN=https://dashboard.example.test\n"
    );
    assert!(
        String::from_utf8(output)
            .expect("UTF-8 prompts")
            .matches("The value is invalid.")
            .count()
            >= 5
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
        Cursor::new(b"forge.example.test\n\nn\n".to_vec()),
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
fn hermes_generated_client_key_uses_the_required_prefix() {
    let payload = CanonicalTemplatePayload::from_json(
        br#"{
          "schema_version": 2,
          "docker_compose_yaml": "services: {}\n",
          "required_values": [],
          "secrets": [],
          "hermes": {
            "env": "COMPOSE_PROFILES",
            "prompt": "Enable Hermes?",
            "enabled_value": "hermes",
            "disabled_value": "",
            "required_values": [],
            "secrets": [
              {
                "file": "hermes-litellm-key",
                "prompt": "Hermes LiteLLM key",
                "generate_bytes": 16,
                "prefix": "sk-"
              }
            ]
          }
        }"#,
    )
    .expect("valid prefixed secret payload");
    let temporary = tempdir().expect("temporary directory");
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(b"yes\n\n".to_vec()), &mut output);

    let result = prepare(
        &payload,
        SetupRequest::install(temporary.path()),
        &mut prompt,
        &FixedSecretGenerator,
    )
    .expect("prefixed Hermes key generated");

    assert_eq!(
        std::fs::read_to_string(result.root.join("secrets/hermes-litellm-key"))
            .expect("Hermes LiteLLM key"),
        "sk-generated-secret\n"
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
