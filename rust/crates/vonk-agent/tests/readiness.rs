use std::{fs, os::unix::fs::PermissionsExt, time::Duration};

use chrono::{TimeZone, Utc};
use vonk_agent::{
    readiness::{ReadinessReceipt, verify_readiness_at},
    runtime_identity::AgentRuntimeIdentity,
};
use vonk_agent_protocol::generated::{
    AgentRuntimeIdentityArchitecture, PackageActivationReceipt, PackageActivationReceiptPhase,
};

fn identity(build: char, binary: char) -> AgentRuntimeIdentity {
    AgentRuntimeIdentity {
        semantic_version: "0.1.0".to_owned(),
        build_digest: format!("sha256:{}", build.to_string().repeat(64)),
        binary_digest: binary.to_string().repeat(64),
        architecture: AgentRuntimeIdentityArchitecture::LinuxAmd64,
        self_test_passed: true,
        package_activation: None,
        observation_receipt_public_key: "d".repeat(64),
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
fn readiness_after_package_activation_matches_the_direct_self_test() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("readiness.json");
    let accepted_at = Utc.with_ymd_and_hms(2026, 9, 12, 21, 0, 0).unwrap();
    let self_test_identity = identity('b', 'c');
    let mut reported_identity = self_test_identity.clone();
    reported_identity.package_activation = Some(PackageActivationReceipt {
        attempt_nonce: "a".repeat(64),
        candidate_binary_sha256: "e".repeat(64),
        candidate_package_sha256: "f".repeat(64),
        candidate_version: "0.1.0".to_owned(),
        created_at: accepted_at.timestamp() - 3600,
        node_id: "spk_2818d189042b4c77aefa7796f4befd23".to_owned(),
        outcome: "controller_confirmed_activation".to_owned(),
        phase: PackageActivationReceiptPhase::Acknowledged,
        schema_version: 2,
        source_binary_sha256: "1".repeat(64),
        source_package_sha256: "2".repeat(64),
        source_version: "0.0.9".to_owned(),
        updated_at: accepted_at.timestamp() - 3500,
    });

    // The claim publishes upgrade history as well as the current executable.
    // The local readiness consumer obtains its identity from a fresh self-test.
    ReadinessReceipt::new(
        reported_identity,
        4242,
        998_877,
        "00000000-0000-4000-8000-000000000001".to_owned(),
        accepted_at,
    )
    .write_secure(&path)
    .unwrap();
    verify_readiness_at(
        &path,
        &self_test_identity,
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
