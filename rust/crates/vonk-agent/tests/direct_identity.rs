use std::fs;
use std::process::Command;

use vonk_agent::runtime_identity::PreparedRuntimeIdentity;
use vonk_agent_protocol::generated::AgentRuntimeIdentityArchitecture;

#[test]
fn direct_identity_binds_version_build_and_binary_to_the_executable() {
    let directory = tempfile::tempdir().unwrap();
    let executable = directory.path().join("vonk-agent");
    fs::write(&executable, b"direct-agent-binary").unwrap();

    let identity = PreparedRuntimeIdentity::from_executable(&executable).unwrap();

    assert_eq!(
        identity.semantic_version,
        env!("VONK_AGENT_SEMANTIC_VERSION")
    );
    assert_eq!(
        identity.architecture,
        if cfg!(target_arch = "aarch64") {
            AgentRuntimeIdentityArchitecture::LinuxArm64
        } else {
            AgentRuntimeIdentityArchitecture::LinuxAmd64
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
    assert!(identity.clone().mark_self_test_passed().is_err());
    let complete = identity
        .with_observation_receipt_public_key_bytes([9; 32])
        .mark_self_test_passed()
        .unwrap();
    assert!(complete.self_test_passed);
    assert_eq!(complete.observation_receipt_public_key, "09".repeat(32));
    assert!(complete.package_activation.is_none());
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

    assert!(PreparedRuntimeIdentity::from_executable(&linked).is_err());
}
