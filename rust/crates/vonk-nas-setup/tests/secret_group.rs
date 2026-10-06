//! Secrets read by capability-free containers are written 0640 with a
//! dedicated group, repaired on upgrade, and never widened when the caller
//! cannot assign that group.
#![cfg(unix)]

use std::io::Cursor;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;

use tempfile::tempdir;
use vonk_nas_setup::{
    CanonicalTemplatePayload, PromptIo, SecretGenerationError, SecretGenerator, SetupRequest,
    parse_template_payload, prepare,
};

struct Generator;

impl SecretGenerator for Generator {
    fn generate(&self, _bytes: usize) -> Result<String, SecretGenerationError> {
        Ok("generated-secret".to_owned())
    }
}

fn payload(gid: u32) -> CanonicalTemplatePayload {
    let json = format!(
        r#"{{
          "schema_version": 2,
          "docker_compose_yaml": "services:\n  api:\n    image: example.invalid/api:1\n",
          "generated_secrets": {{
            "random_text": [
              {{"file": "tailscale-token", "bytes": 16}},
              {{"file": "private-token", "bytes": 16}}
            ]
          }},
          "group_readable_secrets": {{"gid": {gid}, "files": ["tailscale-token", "absent-token"]}}
        }}"#
    );
    parse_template_payload(json.as_bytes()).expect("valid payload")
}

fn run(payload: &CanonicalTemplatePayload, request: SetupRequest) -> String {
    let mut output = Vec::new();
    let mut prompt = PromptIo::new(Cursor::new(Vec::<u8>::new()), &mut output);
    prepare(payload, request, &mut prompt, &Generator).expect("setup succeeds");
    String::from_utf8(output).expect("UTF-8 transcript")
}

fn own_gid(root: &Path) -> u32 {
    std::fs::metadata(root).expect("root metadata").gid()
}

fn mode(path: &Path) -> u32 {
    std::fs::metadata(path)
        .expect("metadata")
        .permissions()
        .mode()
        & 0o777
}

fn is_root() -> bool {
    let output = std::process::Command::new("id")
        .arg("-u")
        .output()
        .expect("id");
    String::from_utf8_lossy(&output.stdout).trim() == "0"
}

#[test]
fn fresh_install_writes_only_the_listed_secrets_group_readable() {
    let root = tempdir().expect("temporary directory");
    let gid = own_gid(root.path());
    let transcript = run(&payload(gid), SetupRequest::install(root.path()));

    let secrets = root.path().join("vonk-forge/secrets");
    let shared = std::fs::metadata(secrets.join("tailscale-token")).expect("shared");
    assert_eq!(shared.gid(), gid);
    assert_eq!(shared.permissions().mode() & 0o777, 0o640);
    assert_eq!(mode(&secrets.join("private-token")), 0o600);
    assert!(transcript.contains(&format!("set group {gid} and mode 0640 on tailscale-token")));
}

#[test]
fn upgrade_repairs_existing_owner_only_files_and_is_idempotent() {
    let root = tempdir().expect("temporary directory");
    let gid = own_gid(root.path());
    let payload = payload(gid);
    run(&payload, SetupRequest::install(root.path()));
    let shared = root.path().join("vonk-forge/secrets/tailscale-token");
    std::fs::set_permissions(&shared, std::fs::Permissions::from_mode(0o600))
        .expect("simulate the pre-fix installer");

    let transcript = run(&payload, SetupRequest::upgrade(root.path()));
    assert_eq!(mode(&shared), 0o640);
    assert_eq!(std::fs::metadata(&shared).expect("metadata").gid(), gid);
    assert!(transcript.contains("tailscale-token"));
    assert_eq!(
        std::fs::read_to_string(&shared).expect("content"),
        "generated-secret\n"
    );

    let second = run(&payload, SetupRequest::upgrade(root.path()));
    assert!(!second.contains("Secret permissions"), "{second}");
    assert_eq!(mode(&shared), 0o640);
}

#[test]
fn a_caller_that_cannot_assign_the_group_keeps_files_owner_only() {
    if is_root() {
        return;
    }
    let root = tempdir().expect("temporary directory");
    // Group 1 is never one an unprivileged test process may assign.
    let transcript = run(&payload(1), SetupRequest::install(root.path()));
    let shared = root.path().join("vonk-forge/secrets/tailscale-token");
    assert_eq!(mode(&shared), 0o600);
    assert!(
        transcript.contains("NOT applied to tailscale-token"),
        "{transcript}"
    );
    assert!(transcript.contains("sudo"));
}

#[test]
fn payload_rejects_a_root_group_and_duplicate_files() {
    for group in [
        r#"{"gid": 0, "files": ["a"]}"#,
        r#"{"gid": 5, "files": []}"#,
        r#"{"gid": 5, "files": ["a", "a"]}"#,
        r#"{"gid": 5, "files": ["../a"]}"#,
    ] {
        let json = format!(
            r#"{{"schema_version": 2, "docker_compose_yaml": "services: {{}}\n", "group_readable_secrets": {group}}}"#
        );
        assert!(parse_template_payload(json.as_bytes()).is_err(), "{group}");
    }
}
