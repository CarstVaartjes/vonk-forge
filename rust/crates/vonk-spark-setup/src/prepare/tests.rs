use super::*;

#[test]
fn host_validation_rejects_non_debian_and_missing_systemd_before_setup() {
    assert!(matches!(
        validate_host_description("ID=fedora\n", true, "amd64", "amd64"),
        Err(SetupError::UnsupportedHost)
    ));
    assert!(matches!(
        validate_host_description("ID=debian\n", false, "amd64", "amd64"),
        Err(SetupError::UnsupportedHost)
    ));
}

#[test]
fn host_validation_rejects_a_release_for_another_architecture() {
    assert!(matches!(
        validate_native_architecture("x86_64"),
        Err(SetupError::UnsupportedHost)
    ));
    assert!(validate_native_architecture("aarch64").is_ok());
    assert!(matches!(
        validate_host_description("ID=ubuntu\n", true, "amd64", "arm64"),
        Err(SetupError::UnsupportedHost)
    ));
}

#[test]
fn signed_artifact_addresses_are_safe_independent_of_publication_layout() {
    assert!(safe_artifact_address("relocated/packages/agent.deb"));
    for unsafe_path in [
        "/absolute",
        "../outside",
        "a/../b",
        "a//b",
        "https://peer/a",
        "a/%2e%2e/b",
    ] {
        assert!(!safe_artifact_address(unsafe_path));
    }
}

#[test]
fn package_release_identity_is_bound_to_platform_and_version() {
    let artifact: InstallerPackageArtifact = serde_json::from_value(serde_json::json!({
        "architecture": "linux-arm64", "host_signature": "a".repeat(128),
        "package_version": "1.0.0", "path": "immutable", "sha256": "b".repeat(64),
        "size": 1, "target_binary_digest": "c".repeat(64),
        "target_build_digest": format!("sha256:{}", "d".repeat(64)),
    }))
    .unwrap();
    assert!(valid_package_release_identity(
        &artifact,
        "linux-arm64",
        "1.0.0"
    ));
    assert!(!valid_package_release_identity(
        &artifact,
        "linux-amd64",
        "1.0.0"
    ));
    assert!(!valid_package_release_identity(
        &artifact,
        "linux-arm64",
        "1.0.1"
    ));
}
