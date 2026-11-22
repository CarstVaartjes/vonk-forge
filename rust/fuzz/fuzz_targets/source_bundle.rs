//! Invariant: a bundle never panics the agent, and whatever it materializes
//! stays inside the root: only regular files and directories, no links, nothing
//! beside the root.
#![no_main]

use std::{fs, path::Path};

use libfuzzer_sys::fuzz_target;
use vonk_agent::build_source::{materialize_source_bundle, source_bundle_digest};

fuzz_target!(|data: &[u8]| {
    // A mutated archive never matches a digest, so derive the one it would need:
    // the file-writing path is then reached by the fuzzer, not only the walk.
    let Ok(digest) = source_bundle_digest(data) else {
        return;
    };
    let outer = tempfile::tempdir().unwrap();
    let root = outer.path().join("root");
    let _ = materialize_source_bundle(data, &digest, &root);
    for entry in fs::read_dir(outer.path()).unwrap() {
        assert_eq!(entry.unwrap().path(), root, "something beside the root");
    }
    only_plain_entries(&root);
});

fn only_plain_entries(directory: &Path) {
    let Ok(entries) = fs::read_dir(directory) else {
        return;
    };
    for entry in entries {
        let path = entry.unwrap().path();
        let kind = fs::symlink_metadata(&path).unwrap().file_type();
        assert!(!kind.is_symlink(), "link {path:?}");
        if kind.is_dir() {
            only_plain_entries(&path);
        } else {
            assert!(kind.is_file(), "special file {path:?}");
        }
    }
}
