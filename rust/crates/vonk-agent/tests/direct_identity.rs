use std::collections::BTreeSet;
use std::fs;
use std::process::Command;

use vonk_agent::runtime_identity::AgentRuntimeIdentity;

#[test]
fn direct_identity_binds_version_build_and_binary_to_the_executable() {
    let directory = tempfile::tempdir().unwrap();
    let executable = directory.path().join("vonk-agent");
    fs::write(&executable, b"direct-agent-binary").unwrap();

    let identity = AgentRuntimeIdentity::from_executable(&executable).unwrap();

    assert_eq!(
        identity.semantic_version,
        env!("VONK_AGENT_SEMANTIC_VERSION")
    );
    assert_eq!(
        identity.architecture,
        if cfg!(target_arch = "aarch64") {
            "linux-arm64"
        } else {
            "linux-amd64"
        }
    );
    assert_eq!(
        identity.binary_digest,
        "b34766f06d9295426db46931bbe384c8cf4860dd0c87a39e128f9d1d420a1da9"
    );
    assert!(identity.build_digest.starts_with("sha256:"));
    assert_eq!(identity.build_digest.len(), 71);
    assert_ne!(
        identity.build_digest,
        format!("sha256:{}", identity.binary_digest)
    );
    assert!(!identity.self_test_passed);
    let fields = serde_json::to_value(&identity).unwrap();
    assert_eq!(
        fields
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([
            "architecture",
            "binary_digest",
            "build_digest",
            "self_test_passed",
            "semantic_version",
        ])
    );
}

#[test]
fn self_test_rejects_missing_configuration_instead_of_hashing_only_the_binary() {
    let directory = tempfile::tempdir().unwrap();
    let missing = directory.path().join("missing-agent.toml");

    let result = Command::new(env!("CARGO_BIN_EXE_vonk-agent"))
        .args(["--config", missing.to_str().unwrap(), "self-test"])
        .output()
        .unwrap();

    assert!(!result.status.success());
}

#[test]
fn direct_identity_rejects_a_symlinked_executable() {
    let directory = tempfile::tempdir().unwrap();
    let executable = directory.path().join("vonk-agent");
    let linked = directory.path().join("linked-agent");
    fs::write(&executable, b"direct-agent-binary").unwrap();
    std::os::unix::fs::symlink(&executable, &linked).unwrap();

    assert!(AgentRuntimeIdentity::from_executable(&linked).is_err());
}
