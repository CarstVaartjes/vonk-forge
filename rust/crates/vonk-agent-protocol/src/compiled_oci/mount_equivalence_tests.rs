#![cfg(test)]

//! Why per-file model mounts are load-bearing.
//!
//! A directory bind of `model_root/<selection_id>` renders every file that
//! happens to be materialised under that selection, not only the files this
//! plan mounted. These tests derive the container-visible triples in both
//! shapes and show where a directory bind would add paths or lose the
//! per-file read-only status. They pin the reason `project` keeps one
//! read-only mount per artifact.
use super::OciMount;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// The container-visible `(path, content, read_only)` triples.
type Visible = BTreeSet<(String, PathBuf, bool)>;

fn per_file(mounts: &[OciMount]) -> Visible {
    mounts
        .iter()
        .map(|mount| (mount.target.clone(), mount.source.clone(), mount.read_only))
        .collect()
}

/// The triples a directory bind of `source` at `target` would render, given
/// the files actually materialised under `source`. The materialiser creates
/// every plan artifact of the selection and never prunes, so `files` is the
/// union of the selection's artifact paths across every target root.
fn directory_bind(source: &Path, target: &str, read_only: bool, files: &[&str]) -> Visible {
    files
        .iter()
        .map(|file| (format!("{target}/{file}"), source.join(file), read_only))
        .collect()
}

fn selection_dir(name: &str) -> PathBuf {
    PathBuf::from("/run/vonk/installations/inst/models").join(name)
}

fn mount(selection: &str, root: &str, file: &str, read_only: bool) -> OciMount {
    OciMount {
        source: selection_dir(selection).join(file),
        target: format!("{root}/{file}"),
        read_only,
    }
}

#[test]
fn a_single_root_selection_collapses_to_an_equivalent_directory_bind() {
    // The GLM EXL3 dual shape: many artifacts, one target root. A directory
    // bind renders exactly the per-file triples, so the collapse is safe.
    let mounts: Vec<OciMount> = (0..149)
        .map(|index| {
            mount(
                "target",
                "/models/target",
                &format!("shard-{index:03}"),
                true,
            )
        })
        .collect();
    let files: Vec<String> = (0..149).map(|index| format!("shard-{index:03}")).collect();
    let files: Vec<&str> = files.iter().map(String::as_str).collect();
    assert_eq!(
        directory_bind(&selection_dir("target"), "/models/target", true, &files),
        per_file(&mounts)
    );
}

#[test]
fn a_selection_at_two_target_roots_renders_extra_paths() {
    // The case the audit found: `validate_storage` deliberately permits one
    // physical file at more than one `mount.target`. Binding the selection
    // directory to either root exposes the other root's file there.
    let mounts = [
        mount("shared", "/models/target", "target.bin", true),
        mount("shared", "/models/draft", "draft.bin", true),
    ];
    let bound = directory_bind(
        &selection_dir("shared"),
        "/models/target",
        true,
        &["target.bin", "draft.bin"],
    );
    assert_ne!(bound, per_file(&mounts));
    assert!(
        bound.contains(&(
            "/models/target/draft.bin".to_owned(),
            selection_dir("shared").join("draft.bin"),
            true,
        )),
        "a directory bind widens the container's view"
    );
}

#[test]
fn a_mixed_read_only_selection_cannot_be_one_directory_bind() {
    // `read_only` is per artifact, so one directory bind can only reproduce
    // a uniform status for every file under it.
    let mounts = [
        mount("mixed", "/models/target", "readonly.bin", true),
        mount("mixed", "/models/target", "writable.bin", false),
    ];
    for read_only in [true, false] {
        assert_ne!(
            directory_bind(
                &selection_dir("mixed"),
                "/models/target",
                read_only,
                &["readonly.bin", "writable.bin"],
            ),
            per_file(&mounts),
        );
    }
}
