#![cfg(test)]
//! An install claim on a disk too short for it makes its own room: it removes
//! unused model files, least recently used first, never its own, until the
//! install fits, and then installs. The disk is a `FakeDisk`: a fixed
//! capacity whose free space is what the files below the data root leave.

use super::super::test_support::*;
use super::*;
use crate::store_retention::fake_disk::{FakeDisk, stored_bytes};

/// Bytes of each unused model file.
const UNUSED: u64 = 4096;

/// A data root holding the plan's own two model files (made first, so they
/// are the least recently used) and two unused ones, the older first.
fn stocked() -> (
    tempfile::TempDir,
    crate::workloads::CompiledExecutionPlan,
    Vec<PathBuf>,
    PathBuf,
    PathBuf,
) {
    let data = tempdir().unwrap();
    crate::store_retention::tests::authorize_retention(data.path());
    let plan: crate::workloads::CompiledExecutionPlan =
        serde_json::from_value(compiled_plan()).unwrap();
    let own = stock_store(data.path(), &plan);
    let store = data.path().join("distribution/models");
    let unused = |name: &str| {
        // ctime orders them: the store's recency is the last change.
        std::thread::sleep(Duration::from_millis(20));
        let path = store.join(name.repeat(64));
        fs::write(&path, vec![0_u8; UNUSED as usize]).unwrap();
        path
    };
    let older = unused("a");
    let newer = unused("b");
    (data, plan, own, older, newer)
}

#[test]
fn an_install_on_a_short_disk_frees_the_least_recently_used_unused_files_and_succeeds() {
    let (data, plan, own, older, newer) = stocked();
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    let installation_id = FIRST;
    let writes = install_write_bytes(data.path(), &plan, installation_id);
    // Short by half an unused file: one removal makes the room.
    let capacity = stored_bytes(data.path()) + writes + INSTALL_HEADROOM_BYTES - UNUSED / 2;
    let _disk = FakeDisk::mount(data.path(), capacity);
    assert!(
        runtime.ensure_disk_available(writes).is_err(),
        "the disk is too short for the install before room is made"
    );

    runtime
        .install_with_space_check(
            &plan,
            installation_id,
            &plan.identity.recipe_revision_sha256,
            writes,
        )
        .unwrap();

    // The least recently used unused file went; the claim's own files, older
    // still, stayed; the newer unused file was not needed.
    assert!(!older.exists());
    assert!(newer.exists());
    assert!(own.iter().all(|path| path.exists()));
    runtime.verify_installation(installation_id).unwrap();
}

#[test]
fn room_for_an_install_counts_its_runtime_image() {
    let (data, mut plan, own, older, newer) = stocked();
    plan.runtime_image.image_bytes = 1_000_000;
    let runner = NoProcess;
    let runtime = runtime(data.path(), &runner);
    let writes = install_write_bytes(data.path(), &plan, FIRST);
    // Room for the model writes, but not for the image as well.
    let capacity = stored_bytes(data.path())
        + writes
        + INSTALL_HEADROOM_BYTES
        + plan.runtime_image.image_bytes
        - UNUSED / 2;
    let _disk = FakeDisk::mount(data.path(), capacity);
    assert!(runtime.ensure_disk_available(writes).is_ok());

    runtime.make_room_for_install(&plan, FIRST);

    assert!(!older.exists());
    assert!(newer.exists());
    assert!(own.iter().all(|path| path.exists()));
}
