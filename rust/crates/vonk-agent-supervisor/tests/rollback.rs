use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::process::Command;

use ring::signature::{self, Ed25519KeyPair, KeyPair};
use tempfile::TempDir;
use vonk_agent_supervisor::health::ReadinessEvidence;
use vonk_agent_supervisor::slots::{
    CrashPoint, NoCrash, Slot, SlotManifest, SlotManifestClaims, SlotPaths, SlotStore,
    SupervisorStatus, Transition, manifest_signing_bytes,
};

const NOW: i64 = 2_100_000_000;

fn shared_fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../agent_protocol/fixtures")
}

#[test]
fn python_slot_manifest_fixture_is_canonical_and_signature_compatible() {
    let raw = fs::read(shared_fixtures().join("slot-manifest.json")).unwrap();
    let raw = raw.strip_suffix(b"\n").unwrap_or(&raw);
    let manifest: SlotManifest = serde_json::from_slice(raw).unwrap();
    assert_eq!(vonk_agent_protocol::canonical_json(&manifest).unwrap(), raw);
    let public_key =
        hex::decode("66cd608b928b88e50e0efeaa33faf1c43cefe07294b0b87e9fe0aba6a3cf7633").unwrap();
    signature::UnparsedPublicKey::new(&signature::ED25519, public_key)
        .verify(
            &manifest_signing_bytes(&manifest.claims).unwrap(),
            &hex::decode(&manifest.signature.value).unwrap(),
        )
        .unwrap();
}

fn signer() -> Ed25519KeyPair {
    Ed25519KeyPair::from_seed_unchecked(&[19; 32]).unwrap()
}

fn elf(payload: u8) -> Vec<u8> {
    let mut value = vec![payload; 4096];
    value[..7].copy_from_slice(b"\x7fELF\x02\x01\x01");
    value[16..18].copy_from_slice(&3_u16.to_le_bytes());
    value[18..20].copy_from_slice(&183_u16.to_le_bytes());
    value
}

fn write_slot(
    paths: &SlotPaths,
    slot: Slot,
    release: &Ed25519KeyPair,
    payload: u8,
    writes_schema: u32,
    readable: (u32, u32),
) -> String {
    let directory = paths.slot_dir(slot);
    fs::create_dir_all(&directory).unwrap();
    let artifact = elf(payload);
    let digest = vonk_agent_protocol::hex_sha256(&artifact);
    let executable = directory.join("vonk-agent");
    if executable.exists() {
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    fs::write(&executable, &artifact).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o555)).unwrap();
    let claims = SlotManifestClaims {
        schema_version: 1,
        slot,
        architecture: "aarch64".to_owned(),
        artifact_sha256: digest.clone(),
        artifact_size: artifact.len() as u64,
        writes_state_schema: writes_schema,
        min_readable_state_schema: readable.0,
        max_readable_state_schema: readable.1,
    };
    let manifest = SlotManifest::signed(claims, release).unwrap();
    fs::write(
        directory.join("manifest.json"),
        vonk_agent_protocol::canonical_json(&manifest).unwrap(),
    )
    .unwrap();
    digest
}

struct Fixture {
    _temp: TempDir,
    paths: SlotPaths,
    store: SlotStore,
    release: Ed25519KeyPair,
    a_digest: String,
}

fn fixture() -> Fixture {
    let temp = tempfile::tempdir().unwrap();
    let data = temp.path().join("vonk");
    let paths = SlotPaths::under(&data, &temp.path().join("run"));
    fs::create_dir_all(&paths.slots).unwrap();
    fs::create_dir_all(&paths.supervisor).unwrap();
    fs::create_dir_all(&paths.runtime).unwrap();
    let release = signer();
    let a_digest = write_slot(&paths, Slot::A, &release, 1, 1, (1, 2));
    let store = SlotStore::new(paths.clone(), release.public_key().as_ref(), None).unwrap();
    store.initialize(Slot::A, NOW).unwrap();
    Fixture {
        _temp: temp,
        paths,
        store,
        release,
        a_digest,
    }
}

#[test]
fn restrictive_umask_still_publishes_reloadable_state() {
    const CHILD_ENV: &str = "VONK_RESTRICTIVE_UMASK_TEST_CHILD";
    if std::env::var_os(CHILD_ENV).is_some() {
        let fixture = fixture();
        assert_eq!(
            fs::metadata(&fixture.paths.state)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o644
        );
        fixture.store.load().unwrap();
        return;
    }

    let output = Command::new("/bin/sh")
        .args([
            "-c",
            "umask 077; exec \"$1\" --exact restrictive_umask_still_publishes_reloadable_state --nocapture",
            "vonk-restrictive-umask-test",
        ])
        .arg(std::env::current_exe().unwrap())
        .env(CHILD_ENV, "1")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "child test failed:\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn package_staging_slot_is_available_only_from_stable_state() {
    let fixture = fixture();
    assert_eq!(fixture.store.package_staging_slot().unwrap(), Slot::B);

    let b_digest = write_slot(&fixture.paths, Slot::B, &fixture.release, 2, 1, (1, 2));
    let pending = fixture
        .store
        .activate(Slot::B, &b_digest, NOW, &NoCrash)
        .unwrap();
    assert_eq!(pending.status, SupervisorStatus::Pending);
    assert!(fixture.store.package_staging_slot().is_err());
}

fn ready(
    state: &vonk_agent_supervisor::slots::SupervisorState,
    challenge: String,
) -> ReadinessEvidence {
    ReadinessEvidence {
        schema_version: 1,
        generation: state.generation,
        slot: state.active_slot,
        artifact_sha256: state.active_artifact_sha256.clone(),
        state_schema: state.state_schema,
        challenge,
        pid: 1234,
    }
}

#[test]
fn valid_activation_commits_only_after_generation_bound_readiness() {
    let fixture = fixture();
    let b_digest = write_slot(&fixture.paths, Slot::B, &fixture.release, 2, 1, (1, 2));

    let pending = fixture
        .store
        .activate(Slot::B, &b_digest, NOW, &NoCrash)
        .unwrap();

    assert_eq!(pending.status, SupervisorStatus::Pending);
    assert_eq!(pending.active_slot, Slot::B);
    assert_eq!(pending.previous_slot, Some(Slot::A));
    assert_eq!(
        fs::read_link(&fixture.paths.current).unwrap(),
        PathBuf::from("../slots/b")
    );
    assert_eq!(
        fs::read_link(&fixture.paths.previous).unwrap(),
        PathBuf::from("../slots/a")
    );
    assert_eq!(
        fixture
            .store
            .observe_readiness(
                &ready(&pending, fixture.store.challenge().unwrap()),
                NOW + 1
            )
            .unwrap(),
        Transition::Stable
    );
    assert_eq!(
        fixture.store.load().unwrap().status,
        SupervisorStatus::Stable
    );
}

#[test]
fn corrupt_artifact_and_bad_manifest_signature_never_change_selection() {
    let fixture = fixture();
    let b_digest = write_slot(&fixture.paths, Slot::B, &fixture.release, 2, 1, (1, 2));
    let executable = fixture.paths.slot_dir(Slot::B).join("vonk-agent");
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(&executable, elf(9)).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o555)).unwrap();
    assert!(
        fixture
            .store
            .activate(Slot::B, &b_digest, NOW, &NoCrash)
            .is_err()
    );
    assert_eq!(fixture.store.load().unwrap().active_slot, Slot::A);

    let b_digest = write_slot(&fixture.paths, Slot::B, &fixture.release, 2, 1, (1, 2));
    let manifest_path = fixture.paths.slot_dir(Slot::B).join("manifest.json");
    let mut document: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest_path).unwrap()).unwrap();
    document["signature"]["value"] = serde_json::Value::String("0".repeat(128));
    fs::write(
        &manifest_path,
        vonk_agent_protocol::canonical_json(&document).unwrap(),
    )
    .unwrap();
    assert!(
        fixture
            .store
            .activate(Slot::B, &b_digest, NOW, &NoCrash)
            .is_err()
    );
    assert_eq!(fixture.store.load().unwrap().active_slot, Slot::A);
}

#[test]
fn candidate_corruption_after_pending_state_triggers_verified_rollback() {
    let fixture = fixture();
    let b_digest = write_slot(&fixture.paths, Slot::B, &fixture.release, 2, 1, (1, 2));
    fixture
        .store
        .activate(Slot::B, &b_digest, NOW, &NoCrash)
        .unwrap();
    let executable = fixture.paths.slot_dir(Slot::B).join("vonk-agent");
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(&executable, elf(8)).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o555)).unwrap();

    assert_eq!(
        fixture.store.recover(NOW + 1).unwrap(),
        Transition::RollbackStarted
    );
    assert_eq!(fixture.store.load().unwrap().active_slot, Slot::A);
}

#[derive(Debug)]
struct CrashAfterPrevious;

impl vonk_agent_supervisor::slots::CrashHook for CrashAfterPrevious {
    fn hit(&self, point: CrashPoint) -> Result<(), String> {
        if point == CrashPoint::AfterPreviousPointer {
            Err("simulated power loss".to_owned())
        } else {
            Ok(())
        }
    }
}

#[test]
fn power_loss_during_pointer_publication_is_repaired_from_durable_state() {
    let fixture = fixture();
    let b_digest = write_slot(&fixture.paths, Slot::B, &fixture.release, 2, 1, (1, 2));
    assert!(
        fixture
            .store
            .activate(Slot::B, &b_digest, NOW, &CrashAfterPrevious)
            .is_err()
    );
    assert_eq!(fixture.store.load().unwrap().active_slot, Slot::B);

    fixture.store.recover_pointers().unwrap();

    assert_eq!(
        fs::read_link(&fixture.paths.current).unwrap(),
        PathBuf::from("../slots/b")
    );
    assert_eq!(
        fs::read_link(&fixture.paths.previous).unwrap(),
        PathBuf::from("../slots/a")
    );
}

#[test]
fn crash_loop_rolls_back_once_and_records_a_durable_reason() {
    let fixture = fixture();
    let b_digest = write_slot(&fixture.paths, Slot::B, &fixture.release, 2, 1, (1, 2));
    fixture
        .store
        .activate(Slot::B, &b_digest, NOW, &NoCrash)
        .unwrap();

    assert_eq!(
        fixture.store.record_agent_failure(NOW + 1).unwrap(),
        Transition::Restart
    );
    assert_eq!(
        fixture.store.record_agent_failure(NOW + 2).unwrap(),
        Transition::Restart
    );
    assert_eq!(
        fixture.store.record_agent_failure(NOW + 3).unwrap(),
        Transition::RollbackStarted
    );
    let rolled_back = fixture.store.load().unwrap();
    assert_eq!(rolled_back.active_slot, Slot::A);
    assert!(rolled_back.rollback_performed);
    assert!(fixture.paths.rollback_reason.is_file());

    assert_eq!(
        fixture.store.record_agent_failure(NOW + 4).unwrap(),
        Transition::Restart
    );
    assert_eq!(
        fixture.store.record_agent_failure(NOW + 5).unwrap(),
        Transition::Restart
    );
    assert_eq!(
        fixture.store.record_agent_failure(NOW + 6).unwrap(),
        Transition::RecoveryRequired
    );
    assert_eq!(
        fixture.store.load().unwrap().status,
        SupervisorStatus::Failed
    );
}

#[test]
fn readiness_timeout_rolls_back_to_freshly_verified_previous_slot() {
    let fixture = fixture();
    let b_digest = write_slot(&fixture.paths, Slot::B, &fixture.release, 2, 1, (1, 2));
    let pending = fixture
        .store
        .activate(Slot::B, &b_digest, NOW, &NoCrash)
        .unwrap();

    assert_eq!(
        fixture
            .store
            .check_deadline(pending.activation_deadline.unwrap() + 1)
            .unwrap(),
        Transition::RollbackStarted
    );
    assert_eq!(
        fixture.store.load().unwrap().active_artifact_sha256,
        fixture.a_digest
    );
}

#[test]
fn activation_is_refused_when_previous_cannot_read_candidate_state_schema() {
    let fixture = fixture();
    let b_digest = write_slot(&fixture.paths, Slot::B, &fixture.release, 2, 3, (3, 3));

    assert!(
        fixture
            .store
            .activate(Slot::B, &b_digest, NOW, &NoCrash)
            .is_err()
    );
    assert_eq!(fixture.store.load().unwrap().active_slot, Slot::A);
}

#[test]
fn stale_readiness_does_not_commit_a_new_generation() {
    let fixture = fixture();
    let b_digest = write_slot(&fixture.paths, Slot::B, &fixture.release, 2, 1, (1, 2));
    let pending = fixture
        .store
        .activate(Slot::B, &b_digest, NOW, &NoCrash)
        .unwrap();
    let mut stale = ready(&pending, fixture.store.challenge().unwrap());
    stale.generation -= 1;

    assert_eq!(
        fixture.store.observe_readiness(&stale, NOW + 1).unwrap(),
        Transition::Waiting
    );
    assert_eq!(
        fixture.store.load().unwrap().status,
        SupervisorStatus::Pending
    );
}
