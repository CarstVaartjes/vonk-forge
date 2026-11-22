use super::{
    STATE_DATABASE_DISK_RESERVE_BYTES, disk_reserve_degraded, maintain_state_database_reserve,
};
use std::{fs, os::unix::fs::PermissionsExt};
use tempfile::tempdir;

#[test]
fn low_disk_is_degraded_without_marking_the_agent_unavailable_at_the_reserve_boundary() {
    assert!(disk_reserve_degraded(0));
    assert!(disk_reserve_degraded(STATE_DATABASE_DISK_RESERVE_BYTES - 1));
    assert!(!disk_reserve_degraded(STATE_DATABASE_DISK_RESERVE_BYTES));
    assert!(!disk_reserve_degraded(
        STATE_DATABASE_DISK_RESERVE_BYTES + 1
    ));
}

#[test]
fn reserve_is_created_when_space_allows_and_released_for_state_recovery() {
    let root = tempdir().unwrap();
    let reserve = root.path().join(".state-db-reserve");
    assert!(
        maintain_state_database_reserve(root.path(), STATE_DATABASE_DISK_RESERVE_BYTES * 2)
            .unwrap()
    );
    let metadata = fs::metadata(&reserve).unwrap();
    assert_eq!(metadata.len(), STATE_DATABASE_DISK_RESERVE_BYTES);
    assert_eq!(metadata.permissions().mode() & 0o777, 0o600);
    assert!(
        !maintain_state_database_reserve(root.path(), STATE_DATABASE_DISK_RESERVE_BYTES - 1)
            .unwrap()
    );
    assert!(!reserve.exists());
}

#[test]
fn interrupted_reserve_allocation_is_repaired_on_the_next_inventory() {
    let root = tempdir().unwrap();
    let reserve = root.path().join(".state-db-reserve");
    fs::write(&reserve, b"partial allocation").unwrap();
    fs::set_permissions(&reserve, fs::Permissions::from_mode(0o600)).unwrap();
    assert!(
        maintain_state_database_reserve(root.path(), STATE_DATABASE_DISK_RESERVE_BYTES * 2)
            .unwrap()
    );
    assert_eq!(
        fs::metadata(reserve).unwrap().len(),
        STATE_DATABASE_DISK_RESERVE_BYTES
    );
}
