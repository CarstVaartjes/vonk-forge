use std::collections::BTreeSet;
use std::fs;

use vonk_agent::runtime_identity::AgentRuntimeIdentity;

#[test]
fn direct_identity_binds_version_build_and_binary_to_the_executable() {
    let directory = tempfile::tempdir().unwrap();
    let executable = directory.path().join("vonk-agent");
    fs::write(&executable, b"direct-agent-binary").unwrap();

    let identity = AgentRuntimeIdentity::from_executable(&executable).unwrap();

    assert_eq!(identity.semantic_version, env!("CARGO_PKG_VERSION"));
    assert_eq!(
        identity.architecture,
        if cfg!(target_arch = "aarch64") {
            "linux-arm64"
        } else {
            "linux-x86_64"
        }
    );
    assert_eq!(
        identity.binary_digest,
        "b34766f06d9295426db46931bbe384c8cf4860dd0c87a39e128f9d1d420a1da9"
    );
    assert_eq!(
        identity.build_digest,
        "sha256:b34766f06d9295426db46931bbe384c8cf4860dd0c87a39e128f9d1d420a1da9"
    );
    assert!(identity.self_test_passed);
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
fn direct_identity_rejects_a_symlinked_executable() {
    let directory = tempfile::tempdir().unwrap();
    let executable = directory.path().join("vonk-agent");
    let linked = directory.path().join("linked-agent");
    fs::write(&executable, b"direct-agent-binary").unwrap();
    std::os::unix::fs::symlink(&executable, &linked).unwrap();

    assert!(AgentRuntimeIdentity::from_executable(&linked).is_err());
}
