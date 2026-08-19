#![forbid(unsafe_code)]

use std::{
    fs,
    os::unix::fs::{PermissionsExt, symlink},
    process::{Command, Output},
};

use tempfile::tempdir;
use url::Url;
use vonk_agent::{
    bootstrap::{BootstrapError, BootstrapRequest, generate_node_id, materialize_config},
    config::{AgentConfig, DEFAULT_CONFIG_PATH},
};

const TOKEN: &str = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ";
const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";
const FINGERPRINT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const PACKAGED_PLACEHOLDER: &str = include_str!("../../../../packaging/config/agent.toml");

fn request() -> BootstrapRequest {
    BootstrapRequest::new(
        TOKEN.to_owned(),
        Url::parse("https://controller.example.test:8443/").unwrap(),
        Url::parse("https://enroll.example.test:8443/").unwrap(),
        FINGERPRINT.to_owned(),
    )
    .unwrap()
}

fn run_agent(arguments: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_vonk-agent"))
        .args(arguments)
        .output()
        .unwrap()
}

fn matching_config(node_id: &str) -> String {
    format!(
        "# preserve this exact valid config\n\
         enrollment_url = \"https://enroll.example.test:8443/\"\n\
         controller_url = \"https://controller.example.test:8443/\"\n\
         ca_path = \"/etc/vonk-forge-agent/controller-ca.pem\"\n\
         ca_sha256 = \"{FINGERPRINT}\"\n\
         data_dir = \"/var/lib/vonk-forge-agent\"\n\
         node_id = \"{node_id}\"\n\
         poll_min_seconds = 2\n\
         poll_max_seconds = 60\n"
    )
}

#[test]
fn bootstrap_generates_a_canonical_node_id() {
    let node_id = generate_node_id();

    assert_eq!(node_id.len(), 36);
    assert!(node_id.starts_with("spk_"));
    assert!(
        node_id[4..]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    );
}

#[test]
fn bootstrap_materializes_the_packaged_placeholder_as_safe_token_free_toml() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("agent.toml");
    fs::write(&path, PACKAGED_PLACEHOLDER).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();

    let config = materialize_config(&path, &request(), NODE_ID).unwrap();

    assert_eq!(config.node_id, NODE_ID);
    assert_eq!(
        config.controller_url.as_str(),
        "https://controller.example.test:8443/"
    );
    assert_eq!(
        config.enrollment_url.as_str(),
        "https://enroll.example.test:8443/"
    );
    assert_eq!(
        config.ca_path.to_str().unwrap(),
        "/etc/vonk-forge-agent/controller-ca.pem"
    );
    assert_eq!(
        config.data_dir.to_str().unwrap(),
        "/var/lib/vonk-forge-agent"
    );
    assert_eq!(DEFAULT_CONFIG_PATH, "/etc/vonk-forge-agent/agent.toml");
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o644
    );

    let document = fs::read_to_string(&path).unwrap();
    assert!(!document.contains(TOKEN));
    assert!(!document.contains("token"));
    assert_eq!(AgentConfig::parse(&document).unwrap(), config);
}

#[test]
fn bootstrap_preserves_a_valid_matching_node_config_byte_for_byte() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("agent.toml");
    let original = matching_config(NODE_ID);
    fs::write(&path, &original).unwrap();

    let config =
        materialize_config(&path, &request(), "spk_ffffffffffffffffffffffffffffffff").unwrap();

    assert_eq!(config.node_id, NODE_ID);
    assert_eq!(fs::read_to_string(path).unwrap(), original);
}

#[test]
fn bootstrap_refuses_conflicting_non_placeholder_values() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("agent.toml");
    fs::write(
        &path,
        matching_config(NODE_ID).replace(
            "https://controller.example.test:8443/",
            "https://other-controller.example.test:8443/",
        ),
    )
    .unwrap();

    assert!(matches!(
        materialize_config(&path, &request(), NODE_ID),
        Err(BootstrapError::Conflict)
    ));
}

#[test]
fn bootstrap_refuses_a_partially_materialized_placeholder_identity() {
    let directory = tempdir().unwrap();
    let path = directory.path().join("agent.toml");
    fs::write(
        &path,
        matching_config("spk_00000000000000000000000000000000"),
    )
    .unwrap();

    assert!(matches!(
        materialize_config(&path, &request(), NODE_ID),
        Err(BootstrapError::Conflict)
    ));
}

#[test]
fn bootstrap_refuses_a_symlinked_configuration_target() {
    let directory = tempdir().unwrap();
    let target = directory.path().join("real-agent.toml");
    let path = directory.path().join("agent.toml");
    fs::write(&target, PACKAGED_PLACEHOLDER).unwrap();
    symlink(&target, &path).unwrap();

    assert!(matches!(
        materialize_config(&path, &request(), NODE_ID),
        Err(BootstrapError::UnsafeConfig)
    ));
    assert_eq!(fs::read_to_string(target).unwrap(), PACKAGED_PLACEHOLDER);
}

#[test]
fn cli_help_exposes_bootstrap_without_changing_pair() {
    let help = run_agent(&["--help"]);
    assert!(help.status.success());
    let help = String::from_utf8(help.stdout).unwrap();
    assert!(help.contains("bootstrap"));
    assert!(help.contains("pair"));
    assert!(help.contains("run"));

    let bootstrap = run_agent(&["bootstrap", "--help"]);
    assert!(bootstrap.status.success());
    let bootstrap = String::from_utf8(bootstrap.stdout).unwrap();
    for flag in [
        "--token",
        "--controller-endpoint",
        "--enrollment-endpoint",
        "--ca-fingerprint",
    ] {
        assert!(
            bootstrap.contains(flag),
            "missing {flag} from bootstrap help"
        );
    }
    for forbidden in ["--token-stdin", "--state-root", "--ca-path"] {
        assert!(
            !bootstrap.contains(forbidden),
            "unexpected {forbidden} in bootstrap help"
        );
    }

    let pair = run_agent(&["pair", "--help"]);
    assert!(pair.status.success());
    let pair = String::from_utf8(pair.stdout).unwrap();
    for flag in ["--enrollment", "--ca-sha256", "--token-stdin"] {
        assert!(pair.contains(flag), "missing {flag} from pair help");
    }
}

#[test]
fn cli_rejects_invalid_bootstrap_values_before_reading_config() {
    for arguments in [
        vec![
            "bootstrap",
            "--token",
            "short",
            "--controller-endpoint",
            "https://controller.example.test/",
            "--enrollment-endpoint",
            "https://enroll.example.test/",
            "--ca-fingerprint",
            FINGERPRINT,
        ],
        vec![
            "bootstrap",
            "--token",
            TOKEN,
            "--controller-endpoint",
            "http://controller.example.test/",
            "--enrollment-endpoint",
            "https://enroll.example.test/",
            "--ca-fingerprint",
            FINGERPRINT,
        ],
        vec![
            "bootstrap",
            "--token",
            TOKEN,
            "--controller-endpoint",
            "https://controller.example.test/",
            "--enrollment-endpoint",
            "https://enroll.example.test/path",
            "--ca-fingerprint",
            FINGERPRINT,
        ],
        vec![
            "bootstrap",
            "--token",
            TOKEN,
            "--controller-endpoint",
            "https://controller.example.test/",
            "--enrollment-endpoint",
            "https://enroll.example.test/",
            "--ca-fingerprint",
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        ],
    ] {
        let output = run_agent(&arguments);
        assert_eq!(output.status.code(), Some(2));
        assert!(
            String::from_utf8(output.stderr)
                .unwrap()
                .contains("invalid value")
        );
    }
}
