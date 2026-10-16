#![cfg(test)]
//! A short disk for tests: a fixed capacity mounted over a directory, whose
//! free space is that capacity less the bytes stored below it, counted once
//! per file however many links it has. A real filesystem cannot be made short
//! without root, and btrfs accounts writes and removals late, so a test that
//! sized itself against the host disk could not see room being made.

use std::{
    collections::BTreeSet,
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    sync::{Mutex, MutexGuard},
};

static MOUNTED: Mutex<Vec<(PathBuf, u64)>> = Mutex::new(Vec::new());

fn mounted() -> MutexGuard<'static, Vec<(PathBuf, u64)>> {
    MOUNTED
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// A short disk mounted over a directory until dropped.
pub(crate) struct FakeDisk(PathBuf);

impl FakeDisk {
    pub(crate) fn mount(root: &Path, capacity: u64) -> Self {
        mounted().push((root.to_path_buf(), capacity));
        Self(root.to_path_buf())
    }
}

impl Drop for FakeDisk {
    fn drop(&mut self) {
        mounted().retain(|(root, _)| root != &self.0);
    }
}

/// `(free, total)` of the short disk holding `path`, if one is mounted.
pub(super) fn usage(path: &Path) -> Option<(u64, u64)> {
    let mounted = mounted();
    let (root, capacity) = mounted.iter().find(|(root, _)| path.starts_with(root))?;
    Some((capacity.saturating_sub(stored_bytes(root)), *capacity))
}

/// The bytes of the regular files below `root`, each counted once.
pub(crate) fn stored_bytes(root: &Path) -> u64 {
    let mut seen = BTreeSet::new();
    add_files(root, &mut seen);
    seen.into_iter().map(|(_, bytes)| bytes).sum()
}

/// Every regular file below `directory` by its identity and length.
fn add_files(directory: &Path, seen: &mut BTreeSet<((u64, u64), u64)>) {
    for entry in fs::read_dir(directory).into_iter().flatten().flatten() {
        let Ok(metadata) = fs::symlink_metadata(entry.path()) else {
            continue;
        };
        if metadata.is_dir() {
            add_files(&entry.path(), seen);
        } else if metadata.is_file() {
            seen.insert(((metadata.dev(), metadata.ino()), metadata.len()));
        }
    }
}
