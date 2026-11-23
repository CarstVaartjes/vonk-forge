#![cfg(test)]

use std::cell::Cell;
use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::time::{Duration, Instant};

use vonk_agent_protocol::generated::PackageActivationOutcome as Outcome;
use vonk_agent_protocol::{
    PackageActivationPhase as Phase, PackageRollbackAuthority, parse_strict,
};

use super::activation::Observed;
use super::custody::now;
use super::recovery::{retry_process_proof, rollback_retry_budget};
use super::{ROLLBACK_RETRY_TIMEOUT, Store, Transaction};
fn transaction() -> Transaction {
    Transaction {
        schema_version: 2,
        node_id: "spk_11111111111111111111111111111111".into(),
        candidate_sha256: "a".repeat(64),
        candidate_version: "0.2.0".into(),
        candidate_binary_sha256: "b".repeat(64),
        candidate_helper_sha256: "c".repeat(64),
        rollback: PackageRollbackAuthority {
            source: vonk_agent_protocol::PackageRollbackSource {
                package_sha256: "d".repeat(64),
                package_signature: "e".repeat(128),
                package_version: "0.1.0".into(),
                binary_sha256: "f".repeat(64),
                helper_sha256: "1".repeat(64),
            },
            attempt_nonce: "2".repeat(64),
            activation_deadline: 200,
        },
        phase: Phase::Armed,
        created_at: 100,
        updated_at: 100,
        outcome: Outcome::AwaitingControllerActivation,
    }
}
#[test]
fn acknowledgement_is_bound_to_node_candidate_attempt_and_deadline() {
    let store = Store::system();
    let tx = transaction();
    assert!(
        store
            .check_acknowledgement(
                &tx,
                &tx.node_id,
                &tx.candidate_sha256,
                &tx.rollback.attempt_nonce,
                150
            )
            .is_ok()
    );
    for (node, candidate, nonce, time) in [
        (
            "other",
            tx.candidate_sha256.as_str(),
            tx.rollback.attempt_nonce.as_str(),
            150,
        ),
        (
            tx.node_id.as_str(),
            "wrong",
            tx.rollback.attempt_nonce.as_str(),
            150,
        ),
        (
            tx.node_id.as_str(),
            tx.candidate_sha256.as_str(),
            "wrong",
            150,
        ),
        (
            tx.node_id.as_str(),
            tx.candidate_sha256.as_str(),
            tx.rollback.attempt_nonce.as_str(),
            200,
        ),
    ] {
        assert!(
            store
                .check_acknowledgement(&tx, node, candidate, nonce, time)
                .is_err()
        );
    }
    let mut reverting = tx.clone();
    reverting.phase = Phase::RollingBack;
    assert!(
        store
            .check_acknowledgement(
                &reverting,
                &tx.node_id,
                &tx.candidate_sha256,
                &tx.rollback.attempt_nonce,
                150
            )
            .is_err()
    );
    assert!(
        store
            .check_acknowledgement(
                &tx,
                &tx.node_id,
                &tx.candidate_sha256,
                &tx.rollback.attempt_nonce,
                150
            )
            .is_ok()
    );
}
#[test]
fn activation_receipt_permissions_survive_service_umask() {
    let status = std::process::Command::new("/bin/sh")
        .args(["-c", "umask 077; exec \"$@\"", "receipt-test"])
        .arg(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "package_rollback::tests::durable_transaction_roundtrip_retains_interrupted_rollback",
        ])
        .status()
        .unwrap();
    assert!(status.success());
}

#[test]
fn durable_transaction_roundtrip_retains_interrupted_rollback() {
    let temporary = tempfile::tempdir().unwrap();
    fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o755)).unwrap();
    let store = Store {
        root: temporary.path().join("rollback"),
        owner: fs::metadata(temporary.path()).unwrap().uid(),
    };
    let _lock = store.lock().unwrap();
    let mut tx = transaction();
    tx.phase = Phase::RollingBack;
    store.write(&tx).unwrap();
    let receipt = temporary.path().join("package-activation.receipt.json");
    assert_eq!(fs::metadata(&receipt).unwrap().mode() & 0o777, 0o644);
    assert_eq!(
        fs::metadata(store.root.join("transaction.json"))
            .unwrap()
            .mode()
            & 0o777,
        0o600
    );
    let recovered = store.read().unwrap();
    assert_eq!(recovered.phase, Phase::RollingBack);
    assert_eq!(recovered.rollback, tx.rollback);
    assert_eq!(recovered.node_id, tx.node_id);
    fs::set_permissions(
        store.root.join("transaction.json"),
        fs::Permissions::from_mode(0o644),
    )
    .unwrap();
    assert_eq!(store.read().unwrap().rollback, tx.rollback);
    assert_eq!(
        fs::metadata(store.root.join("transaction.json"))
            .unwrap()
            .mode()
            & 0o777,
        0o600
    );
    fs::set_permissions(
        store.root.join("transaction.json"),
        fs::Permissions::from_mode(0o666),
    )
    .unwrap();
    assert!(store.read().is_err());
    store.write(&tx).unwrap();
    assert!(store.read().is_ok());
}
#[test]
fn current_transaction_rejects_commands_paths_and_schema_coercion() {
    let mut value = serde_json::to_value(transaction()).unwrap();
    value["command"] = serde_json::json!("sh -c arbitrary");
    assert!(parse_strict::<Transaction>(&serde_json::to_vec(&value).unwrap()).is_err());
    let mut value = serde_json::to_value(transaction()).unwrap();
    value["rollback"]["source"]["package_sha256"] = serde_json::json!("../../source.deb");
    assert!(parse_strict::<Transaction>(&serde_json::to_vec(&value).unwrap()).is_err());
    let mut value = serde_json::to_value(transaction()).unwrap();
    value["schema_version"] = serde_json::json!(2.0);
    assert!(parse_strict::<Transaction>(&serde_json::to_vec(&value).unwrap()).is_err());
    assert!(parse_strict::<Transaction>(&serde_json::to_vec(&transaction()).unwrap()).is_ok());
}

#[test]
fn process_proof_retries_transient_identity_failures() {
    let mut attempts = 0;
    let result = retry_process_proof(
        Instant::now() + Duration::from_secs(1),
        Duration::ZERO,
        || {
            attempts += 1;
            if attempts < 3 {
                Err("identity not ready".into())
            } else {
                Ok(())
            }
        },
    );
    assert!(result.is_ok());
    assert_eq!(attempts, 3);
}

#[test]
fn process_proof_returns_the_last_error_at_the_deadline() {
    let mut attempts = 0;
    let result = retry_process_proof(Instant::now(), Duration::ZERO, || {
        attempts += 1;
        Err("identity unavailable".into())
    });
    assert!(result.is_err());
    assert_eq!(attempts, 1);
    assert!(retry_process_proof(Instant::now(), Duration::ZERO, || Ok(())).is_ok());
}
#[test]
fn rollback_retry_end_survives_restart_and_a_fresh_request_is_admitted() {
    let mut tx = transaction();
    let end =
        tx.rollback.activation_deadline + i64::try_from(ROLLBACK_RETRY_TIMEOUT.as_secs()).unwrap();
    assert_eq!(
        rollback_retry_budget(&tx, end - 1),
        Some(Duration::from_secs(1))
    );
    tx.updated_at = end - 1;
    tx.phase = Phase::RollingBack;
    assert!(rollback_retry_budget(&tx, end).is_none());
    let temporary = tempfile::tempdir().unwrap();
    let store = Store {
        root: temporary.path().join("rollback"),
        owner: fs::metadata(temporary.path()).unwrap().uid(),
    };
    let lock = store.lock().unwrap();
    tx.phase = Phase::RollbackFailed;
    tx.outcome = Outcome::SourceRestoreFailed;
    store.write(&tx).unwrap();
    let mut fresh = transaction();
    fresh.created_at = end;
    fresh.updated_at = end;
    fresh.rollback.activation_deadline = end + 120;
    fresh.rollback.attempt_nonce = "7".repeat(64);
    store.write(&fresh).unwrap();
    assert_eq!(store.read().unwrap().rollback, fresh.rollback);
    drop(lock);
    assert!(store.lock().is_ok());
}

#[test]
fn more_private_parent_permissions_do_not_block_fresh_admission() {
    let temporary = tempfile::tempdir().unwrap();
    fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let store = Store {
        root: temporary.path().join("rollback"),
        owner: fs::metadata(temporary.path()).unwrap().uid(),
    };
    drop(store.lock().unwrap());
    assert!(store.lock().is_ok());
    assert_eq!(
        fs::metadata(temporary.path()).unwrap().mode() & 0o777,
        0o700
    );
}

#[test]
fn busy_owner_ends_boundedly_and_releases_for_a_fresh_request() {
    let temporary = tempfile::tempdir().unwrap();
    fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o755)).unwrap();
    let store = Store {
        root: temporary.path().join("rollback"),
        owner: fs::metadata(temporary.path()).unwrap().uid(),
    };
    let owner = store.lock().unwrap();
    let start = Instant::now();
    assert!(store.lock().is_err());
    assert!(start.elapsed() < Duration::from_secs(3));
    drop(owner);
    let fresh = store.lock().unwrap();
    fs::write(store.root.join("transaction.json"), b"damaged history").unwrap();
    let tx = transaction();
    store.write(&tx).unwrap();
    assert_eq!(store.read().unwrap().rollback, tx.rollback);
    drop(fresh);
    assert!(store.lock().is_ok());
}
#[test]
fn unexpected_journal_object_is_a_miss_for_current_verified_publication() {
    let temporary = tempfile::tempdir().unwrap();
    fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o755)).unwrap();
    let store = Store {
        root: temporary.path().join("rollback"),
        owner: fs::metadata(temporary.path()).unwrap().uid(),
    };
    let owner = store.lock().unwrap();
    let journal = store.root.join("transaction.json");
    fs::create_dir(&journal).unwrap();
    fs::write(journal.join("unrecognized-entry"), b"preserve this").unwrap();
    assert!(store.read().is_err());
    store.write(&transaction()).unwrap();
    assert_eq!(store.read().unwrap().rollback, transaction().rollback);
    drop(owner);
    assert!(store.lock().is_ok());
}
#[test]
fn damaged_lock_shape_does_not_poison_fresh_admission() {
    let temporary = tempfile::tempdir().unwrap();
    fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o755)).unwrap();
    let store = Store {
        root: temporary.path().join("rollback"),
        owner: fs::metadata(temporary.path()).unwrap().uid(),
    };
    drop(store.lock().unwrap());
    fs::remove_file(store.root.join("lock")).unwrap();
    fs::create_dir(store.root.join("lock")).unwrap();
    fs::write(store.root.join("lock/unknown-entry"), b"preserved").unwrap();
    drop(store.lock().unwrap());
    assert!(store.lock().is_ok());
}

fn observed(status: Result<&str, &str>) -> Observed {
    let tx = transaction();
    Observed {
        dpkg_status: status.map(str::to_owned).map_err(str::to_owned),
        agent_sha256: tx.candidate_binary_sha256,
        helper_sha256: tx.candidate_helper_sha256,
    }
}

/// Confirm against a tempdir store; returns the result and the stored phase.
fn confirm(observed: &Observed) -> (Result<(), String>, Phase) {
    let temporary = tempfile::tempdir().unwrap();
    fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o755)).unwrap();
    let store = Store {
        root: temporary.path().join("rollback"),
        owner: fs::metadata(temporary.path()).unwrap().uid(),
    };
    let _lock = store.lock().unwrap();
    let tx = transaction();
    store.write(&tx).unwrap();
    let result = store.finish_acknowledgement(tx, observed, 150);
    (result, store.read().unwrap().phase)
}

#[test]
fn acknowledgement_requires_the_candidate_installed() {
    let installed = "install ok installed\t0.2.0";
    assert_eq!(
        confirm(&observed(Ok(installed))),
        (Ok(()), Phase::Acknowledged)
    );
    for status in [
        Ok("install ok half-configured\t0.2.0"),
        Ok("install ok installed\t0.1.0"),
        Err("dpkg-query failed"),
    ] {
        let (result, phase) = confirm(&observed(status));
        assert!(result.is_err());
        assert_eq!(phase, Phase::Armed);
    }
}

fn tempdir_store(root: &tempfile::TempDir) -> Store {
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o755)).unwrap();
    Store {
        root: root.path().join("rollback"),
        owner: fs::metadata(root.path()).unwrap().uid(),
    }
}

#[test]
fn retiring_an_armed_transaction_ends_the_watchdog_without_a_restore() {
    let temporary = tempfile::tempdir().unwrap();
    let store = tempdir_store(&temporary);
    drop(store.lock().unwrap());
    store.write(&transaction()).unwrap();
    store.retire().unwrap();
    assert!(!store.root.join("transaction.json").exists());
    assert!(
        !temporary
            .path()
            .join("package-activation.receipt.json")
            .exists()
    );
    // The watchdog finds no authority and ends. A surviving journal would be
    // rewritten as RollbackFailed (its deadline is long past).
    assert!(store.watch().is_ok());
    assert!(!store.root.join("transaction.json").exists());
}

#[test]
fn retiring_leaves_finished_and_missing_transactions_alone() {
    let temporary = tempfile::tempdir().unwrap();
    let store = tempdir_store(&temporary);
    store.retire().unwrap();
    assert!(!store.root.exists(), "no store is created");
    drop(store.lock().unwrap());
    let mut tx = transaction();
    tx.phase = Phase::Acknowledged;
    store.write(&tx).unwrap();
    store.retire().unwrap();
    assert_eq!(store.read().unwrap().phase, Phase::Acknowledged);
    assert!(
        temporary
            .path()
            .join("package-activation.receipt.json")
            .exists()
    );
}

fn watch_outcome(tx: &Transaction, restore: &Result<(), String>) -> (Phase, u32, u32) {
    let temporary = tempfile::tempdir().unwrap();
    let store = tempdir_store(&temporary);
    drop(store.lock().unwrap());
    store.write(tx).unwrap();
    let (attempts, heals) = (Cell::new(0), Cell::new(0));
    // A failed restore is also the watchdog's own error; the journal carries it.
    #[expect(
        clippy::let_underscore_must_use,
        reason = "a failed restore is also the watchdog's own error; the journal carries it"
    )]
    let _ = store.watch_with(
        |_, _| {
            attempts.set(attempts.get() + 1);
            restore.clone()
        },
        || heals.set(heals.get() + 1),
    );
    (store.read().unwrap().phase, attempts.get(), heals.get())
}

#[test]
fn a_failed_restore_ends_rollback_failed_and_starts_the_agent_again() {
    // Armed and due: the deadline is the current second.
    let mut due = transaction();
    due.rollback.activation_deadline = now().unwrap();
    let (phase, attempts, heals) = watch_outcome(&due, &Err("dpkg failed".into()));
    assert_eq!((phase, attempts, heals), (Phase::RollbackFailed, 1, 1));
    // The retry budget is spent (a restart after the failure): no attempt, and
    // the agent is still started again.
    let (phase, attempts, heals) = watch_outcome(&transaction(), &Err("dpkg failed".into()));
    assert_eq!((phase, attempts, heals), (Phase::RollbackFailed, 0, 1));
}

#[test]
fn a_successful_restore_starts_nothing_extra() {
    let mut due = transaction();
    due.rollback.activation_deadline = now().unwrap();
    let (phase, attempts, heals) = watch_outcome(&due, &Ok(()));
    assert_eq!((phase, attempts, heals), (Phase::RolledBack, 1, 0));
}

#[test]
fn only_listed_phase_moves_are_legal() {
    use super::recovery::can_move;
    // A finished transaction never moves again, except Acknowledged repeating.
    for to in [
        Phase::Armed,
        Phase::ActivationFailed,
        Phase::RollingBack,
        Phase::RolledBack,
        Phase::RollbackFailed,
    ] {
        assert!(!can_move(Phase::RolledBack, to));
        assert!(!can_move(Phase::Acknowledged, to));
        assert!(!can_move(to, Phase::Armed));
    }
    assert!(can_move(Phase::Armed, Phase::Acknowledged));
    assert!(can_move(Phase::RollbackFailed, Phase::RollingBack));
    assert!(!can_move(Phase::ActivationFailed, Phase::Acknowledged));
}

#[test]
fn an_illegal_move_is_refused_and_not_journaled() {
    let temporary = tempfile::tempdir().unwrap();
    fs::set_permissions(temporary.path(), fs::Permissions::from_mode(0o755)).unwrap();
    let store = Store {
        root: temporary.path().join("rollback"),
        owner: fs::metadata(temporary.path()).unwrap().uid(),
    };
    let _lock = store.lock().unwrap();
    let mut tx = transaction();
    tx.phase = Phase::RolledBack;
    store.write(&tx).unwrap();
    let outcome = Outcome::ControllerConfirmedActivation;
    assert!(
        store
            .advance(&mut tx, Phase::Acknowledged, outcome, 9)
            .is_err()
    );
    assert_eq!(store.read().unwrap().phase, Phase::RolledBack);
}
