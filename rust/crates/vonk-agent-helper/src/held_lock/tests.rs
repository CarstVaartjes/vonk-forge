#![cfg(test)]
use super::*;

#[test]
fn a_released_lock_is_free_while_a_forked_child_still_shares_its_file() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("installation.lock");
    let file = File::create(&path).unwrap();
    // What a child forked by another thread holds until it execs.
    let _child_copy = file.try_clone().unwrap();

    drop(HeldLock::try_exclusive(file).unwrap());

    assert!(HeldLock::try_exclusive(File::open(&path).unwrap()).is_ok());
}
