//! Exclusive file locks held by the agent.
//!
//! A copy of `vonk-agent-helper/src/held_lock.rs`; the agent does not depend
//! on the helper crate, so change both together.
//!
//! A `flock` belongs to the open file, and a child that any thread forks
//! shares that open file until it execs. Closing our descriptor alone
//! leaves the lock held for such a child, and the next locker is refused as
//! busy. A held lock is therefore released explicitly when it is dropped.

use std::fs::File;

pub(crate) struct HeldLock(File);

impl HeldLock {
    /// Take `file`'s exclusive lock without waiting.
    #[expect(
        clippy::disallowed_methods,
        reason = "HeldLock is the one owner of the exclusive flock: it unlocks explicitly on drop, so a forked child cannot keep it held"
    )]
    pub(crate) fn try_exclusive(file: File) -> rustix::io::Result<Self> {
        rustix::fs::flock(&file, rustix::fs::FlockOperation::NonBlockingLockExclusive)?;
        Ok(Self(file))
    }
}

impl Drop for HeldLock {
    #[expect(
        clippy::disallowed_methods,
        reason = "HeldLock is the one owner of the exclusive flock: it unlocks explicitly on drop, so a forked child cannot keep it held"
    )]
    fn drop(&mut self) {
        // Unlocking acts on the open file itself, whoever else still shares it.
        #[expect(
            clippy::let_underscore_must_use,
            reason = "the lock is released when the descriptor closes anyway"
        )]
        let _ = rustix::fs::flock(&self.0, rustix::fs::FlockOperation::Unlock);
    }
}

#[cfg(test)]
mod tests;
