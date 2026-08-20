use std::{fs, os::unix::fs::PermissionsExt, time::Duration};

use chrono::{TimeZone, Utc};
use vonk_agent::{
    readiness::{ReadinessReceipt, verify_readiness_at},
    runtime_identity::AgentRuntimeIdentity,
};

fn identity(build: char, binary: char) -> AgentRuntimeIdentity {
    AgentRuntimeIdentity {
        semantic_version: "0.1.0".to_owned(),
        build_digest: format!("sha256:{}", build.to_string().repeat(64)),
        binary_digest: binary.to_string().repeat(64),
        architecture: "linux-amd64".to_owned(),
        self_test_passed: true,
    }
}

#[test]
fn readiness_receipt_binds_controller_acceptance_to_exact_process_and_identity() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("readiness.json");
    let accepted_at = Utc.with_ymd_and_hms(2026, 8, 20, 12, 0, 0).unwrap();
    let runtime_identity = identity('b', 'c');
    ReadinessReceipt::new(
        runtime_identity.clone(),
        4242,
        998_877,
        "00000000-0000-4000-8000-000000000001".to_owned(),
        accepted_at,
    )
    .write_secure(&path)
    .unwrap();

    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    verify_readiness_at(
        &path,
        &runtime_identity,
        4242,
        998_877,
        "00000000-0000-4000-8000-000000000001",
        accepted_at + Duration::from_secs(10),
        Duration::from_secs(30),
    )
    .unwrap();
}

#[test]
fn readiness_receipt_rejects_stale_process_or_identity_acceptance() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("readiness.json");
    let accepted_at = Utc.with_ymd_and_hms(2026, 8, 20, 12, 0, 0).unwrap();
    let runtime_identity = identity('b', 'c');
    ReadinessReceipt::new(
        runtime_identity.clone(),
        4242,
        998_877,
        "00000000-0000-4000-8000-000000000001".to_owned(),
        accepted_at,
    )
    .write_secure(&path)
    .unwrap();

    assert!(
        verify_readiness_at(
            &path,
            &runtime_identity,
            4243,
            998_877,
            "00000000-0000-4000-8000-000000000001",
            accepted_at + Duration::from_secs(10),
            Duration::from_secs(30),
        )
        .is_err()
    );
    assert!(
        verify_readiness_at(
            &path,
            &identity('d', 'c'),
            4242,
            998_877,
            "00000000-0000-4000-8000-000000000001",
            accepted_at + Duration::from_secs(10),
            Duration::from_secs(30),
        )
        .is_err()
    );
    assert!(
        verify_readiness_at(
            &path,
            &runtime_identity,
            4242,
            998_877,
            "00000000-0000-4000-8000-000000000001",
            accepted_at + Duration::from_secs(31),
            Duration::from_secs(30),
        )
        .is_err()
    );
}
