use super::*;
use tempfile::tempdir;

const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

#[test]
fn interrupted_csr_write_replays_the_same_request_after_restart() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    let pending = prepare_pending(&root, NODE_ID).unwrap();
    // Simulate death between the two durable writes and reconstruct a fresh
    // handle, as a package upgrade does. No in-memory retry state survives.
    fs::remove_file(root.join("pending-csr.pem")).unwrap();
    let restarted_root = temporary.path().join("credentials");
    let replayed = prepare_pending(&restarted_root, NODE_ID).unwrap();
    assert_eq!(replayed.private_key_pem, pending.private_key_pem);
    assert_eq!(replayed.csr_pem, pending.csr_pem);
    atomic_private_write(&root, "pending-csr.pem", b"damaged").unwrap();
    assert_eq!(
        prepare_pending(&root, NODE_ID).unwrap().csr_pem,
        pending.csr_pem
    );
    clear_pending(&root).unwrap();
    assert_ne!(
        prepare_pending(&root, NODE_ID).unwrap().csr_pem,
        pending.csr_pem
    );
}

#[test]
fn lost_pending_key_prepares_a_fresh_request_without_a_local_gate() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    let first = prepare_pending(&root, NODE_ID).unwrap();
    atomic_private_write(&root, "pending-key.pem", b"damaged").unwrap();
    let replacement = prepare_pending(&root, NODE_ID).unwrap();
    assert_ne!(replacement.private_key_pem, first.private_key_pem);
    assert_ne!(replacement.csr_pem, first.csr_pem);
    assert_eq!(
        prepare_pending(&root, NODE_ID).unwrap().csr_pem,
        replacement.csr_pem
    );
}

#[test]
fn damaged_staged_pointer_preserves_active_identity_and_replays_pending_request() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    persist_identity(&root, &super::tests::certificate_material(1, false)).unwrap();
    let active = active_identity_paths(&root).unwrap();
    let active_bytes = fs::read(&active.certificate).unwrap();
    let request = prepare_pending(&root, NODE_ID).unwrap();
    atomic_private_write(&root, "staged.json", b"damaged").unwrap();
    assert!(observe_staged_identity(&root).unwrap().is_none());
    let archived = fs::read_dir(&root)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("staged-unavailable-")
        })
        .unwrap();
    assert_eq!(fs::read(archived).unwrap(), b"damaged");
    assert_eq!(fs::read(&active.certificate).unwrap(), active_bytes);
    assert_eq!(
        prepare_pending(&root, NODE_ID).unwrap().csr_pem,
        request.csr_pem
    );
    assert!(observe_staged_identity(&root).unwrap().is_none());
}

#[test]
fn short_lived_certificate_renews_before_expiry_across_upgrade_restart() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    // This fixture has a 24-hour validity, as opposed to the 30-day default.
    persist_identity(&root, &super::tests::certificate_material(1, true)).unwrap();
    let due = renewal_time(&root).unwrap();
    assert!(!renewal_due(&root, due - chrono::Duration::seconds(1)).unwrap());
    assert!(renewal_due(&root, due).unwrap());
    let request = prepare_pending(&root, NODE_ID).unwrap();
    let restarted_root = temporary.path().join("credentials");
    assert!(renewal_due(&restarted_root, due + chrono::Duration::minutes(1)).unwrap());
    assert_eq!(
        prepare_pending(&restarted_root, NODE_ID).unwrap().csr_pem,
        request.csr_pem
    );
    assert!(!identity_expired(&active_identity_paths(&root).unwrap(), due).unwrap());
}

#[test]
fn renewal_status_is_disposable_and_bound_to_current_certificate_content() {
    let temporary = tempdir().unwrap();
    let root = temporary.path().join("credentials");
    persist_identity(&root, &super::tests::certificate_material(1, true)).unwrap();
    let now = renewal_time(&root).unwrap();
    record_renewal_health(&root, true).unwrap();
    assert_eq!(renewal_health(&root, now).unwrap().0, Some(true));
    atomic_private_write(&root, "renewal-health.json", b"damaged").unwrap();
    assert_eq!(renewal_health(&root, now).unwrap().0, None);
    // Damaged monitoring bookkeeping never owns admission or the pending CSR.
    assert!(!prepare_pending(&root, NODE_ID).unwrap().csr_pem.is_empty());
    record_renewal_health(&root, false).unwrap();
    assert_eq!(renewal_health(&root, now).unwrap().0, Some(false));
    let paths = active_identity_paths(&root).unwrap();
    let (_, end) = certificate_validity(&paths.certificate).unwrap();
    assert!(
        renewal_health(&root, end - chrono::Duration::seconds(1))
            .unwrap()
            .1
            < 0.25
    );
}
