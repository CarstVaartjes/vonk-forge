use std::process::Command;

use tempfile::tempdir;

#[test]
fn executable_rejects_secret_values_in_argv() {
    let output = Command::new(env!("CARGO_BIN_EXE_vonk-nas-setup"))
        .args([
            "--template",
            "payload.json",
            "--output",
            "bundle",
            "--database-password",
            "must-not-be-accepted",
        ])
        .output()
        .expect("run setup executable");

    assert!(!output.status.success());
}

#[test]
fn executable_upgrades_a_complete_bundle_with_piped_stdio() {
    let temporary = tempdir().expect("temporary directory");
    let bundle = temporary.path().join("vonk-forge");
    std::fs::create_dir(&bundle).expect("bundle");
    std::fs::create_dir(bundle.join("secrets")).expect("secrets");
    std::fs::write(bundle.join("docker-compose.yaml"), "old compose\n").expect("compose");
    std::fs::write(
        bundle.join(".env"),
        "VONK_PUBLIC_HOST=kept.example.test\nVONK_HERMES_ENABLED=false\nSITE_LOCAL=kept\n",
    )
    .expect("environment");
    let template = temporary.path().join("payload.json");
    std::fs::write(
        &template,
        r#"{
          "schema_version": 2,
          "docker_compose_yaml": "services:\n  api:\n    image: example.invalid/api@sha256:new\n",
          "required_values": [
            {"env": "VONK_PUBLIC_HOST", "prompt": "Public hostname"}
          ],
          "secrets": [],
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
    .expect("template");

    let output = Command::new(env!("CARGO_BIN_EXE_vonk-nas-setup"))
        .args([
            "--template",
            template.to_str().expect("UTF-8 path"),
            "--upgrade",
        ])
        .current_dir(temporary.path())
        .output()
        .expect("run setup executable");

    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8_lossy(&output.stdout).contains("Bundle ready at"));
    assert_eq!(
        std::fs::read_to_string(bundle.join("docker-compose.yaml")).expect("updated compose"),
        "services:\n  api:\n    image: example.invalid/api@sha256:new\n"
    );
    assert_eq!(
        std::fs::read_to_string(bundle.join(".env")).expect("preserved environment"),
        "VONK_PUBLIC_HOST=kept.example.test\nVONK_HERMES_ENABLED=false\nSITE_LOCAL=kept\n"
    );
}
