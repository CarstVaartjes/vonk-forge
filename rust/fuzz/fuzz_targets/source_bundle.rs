//! Invariant: a bundle never panics the agent, and whatever it materializes
//! stays inside the root: only regular files and directories, no links, nothing
//! beside the root.
#![no_main]

use std::{fs, path::Path};

use libfuzzer_sys::fuzz_target;
use vonk_agent::build_source::{materialize_source_bundle, source_bundle_digest};

fuzz_target!(|data: &[u8]| {
    // A mutated header fails its checksum before the parser looks at the entry,
    // so repair the checksums: hostile names, link types and sizes then reach
    // the parser's own checks.
    let mut archive = data.to_vec();
    repair_checksums(&mut archive);
    let data = archive.as_slice();
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

/// Rewrite the checksum field of every 512-byte header, walking entry by entry.
fn repair_checksums(archive: &mut [u8]) {
    let mut offset = 0_usize;
    // Every entry takes at least one block, so the block count bounds the walk.
    for _ in 0..archive.len() / 512 {
        if offset.saturating_add(512) > archive.len() {
            return;
        }
        let header = &mut archive[offset..offset + 512];
        if header.iter().all(|byte| *byte == 0) {
            return;
        }
        let size = header[124..136]
            .iter()
            .take_while(|byte| (b'0'..=b'7').contains(byte))
            .fold(0_usize, |size, byte| {
                size.saturating_mul(8)
                    .saturating_add(usize::from(byte - b'0'))
            });
        header[148..156].fill(b' ');
        let sum: u32 = header.iter().map(|byte| u32::from(*byte)).sum();
        header[148..156].copy_from_slice(format!("{sum:06o}\0 ").as_bytes());
        offset = offset
            .saturating_add(512)
            .saturating_add(size.div_ceil(512).saturating_mul(512));
    }
}

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
