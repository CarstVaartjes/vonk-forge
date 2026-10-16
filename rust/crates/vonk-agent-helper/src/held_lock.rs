//! Exclusive file locks held by the helper.
//!
//! `vonk-agent` keeps a copy in `src/held_lock.rs`; the agent does not depend
//! on this crate, so change both together.
//!
//! A `flock` belongs to the open file, and a child that any thread forks
//! shares that open file until it execs. Closing the helper's descriptor alone
//! leaves the lock held for such a child, and the next locker is refused as
//! busy. A held lock is therefore released explicitly when it is dropped.

use std::fs::File;

pub(crate) struct HeldLock(File);

impl HeldLock {
    /// Take `file`'s exclusive lock without waiting.
    pub(crate) fn try_exclusive(file: File) -> rustix::io::Result<Self> {
        rustix::fs::flock(&file, rustix::fs::FlockOperation::NonBlockingLockExclusive)?;
        Ok(Self(file))
    }
}

impl Drop for HeldLock {
    fn drop(&mut self) {
        // Unlocking acts on the open file itself, whoever else still shares it.
        let _ = rustix::fs::flock(&self.0, rustix::fs::FlockOperation::Unlock);
    }
}

#[cfg(test)]
mod tests {
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
}
