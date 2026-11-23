#![cfg(test)]
use super::PreparedRuntimeIdentity;

#[test]
fn prepared_identity_becomes_wire_identity_after_self_test() {
    let directory = tempfile::tempdir().unwrap();
    let executable = directory.path().join("vonk-agent");
    std::fs::write(&executable, b"direct-agent-binary").unwrap();

    let complete = PreparedRuntimeIdentity::from_executable(&executable)
        .unwrap()
        .mark_self_test_passed()
        .unwrap();
    assert!(complete.package_activation.is_none());
}
