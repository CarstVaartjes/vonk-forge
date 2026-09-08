#![forbid(unsafe_code)]

use std::io::Cursor;

use tempfile::tempdir;
use vonk_agent::build_source::{BuildSourceError, materialize_source_bundle};
use vonk_agent_protocol::{
    canonical_json,
    generated::{SourceBundleDigestManifest, SourceBundleFile},
    hex_sha256,
};

fn tar(files: &[(&str, &[u8])]) -> Vec<u8> {
    let mut payload = Vec::new();
    {
        let mut archive = tar::Builder::new(&mut payload);
        for (path, content) in files {
            let mut header = tar::Header::new_ustar();
            header.set_path(path).unwrap();
            header.set_size(content.len() as u64);
            header.set_mode(0o644);
            header.set_uid(0);
            header.set_gid(0);
            header.set_mtime(0);
            header.set_cksum();
            archive.append(&header, Cursor::new(*content)).unwrap();
        }
        archive.finish().unwrap();
    }
    payload
}

#[test]
fn canonical_bundle_digest_is_verified_before_materialization() {
    let payload = tar(&[("Dockerfile", b"FROM scratch\nUSER 10001\n")]);
    let file_sha = hex_sha256(b"FROM scratch\nUSER 10001\n");
    let manifest = SourceBundleDigestManifest {
        schema_version: 1,
        files: vec![SourceBundleFile {
            mode: 420_i64.try_into().unwrap(),
            path: "Dockerfile".into(),
            sha256: file_sha,
            size: 24,
        }],
        total_bytes: 24,
    };
    let expected = hex_sha256(&canonical_json(&manifest).unwrap());
    let root = tempdir().unwrap();

    let source = materialize_source_bundle(&payload, &expected, root.path()).unwrap();

    assert_eq!(source.sha256, expected);
    assert_eq!(source.files["Dockerfile"], b"FROM scratch\nUSER 10001\n");
    assert_eq!(
        std::fs::read(root.path().join("Dockerfile")).unwrap(),
        source.files["Dockerfile"]
    );
}

#[test]
fn archive_rejects_traversal_and_digest_substitution() {
    let payload = tar(&[("Dockerfile", b"FROM scratch\nUSER 10001\n")]);
    let root = tempdir().unwrap();
    assert!(matches!(
        materialize_source_bundle(&payload, &"f".repeat(64), root.path()),
        Err(BuildSourceError::Digest)
    ));

    let mut malicious = Vec::new();
    {
        let mut archive = tar::Builder::new(&mut malicious);
        let mut header = tar::Header::new_ustar();
        header.set_size(1);
        header.set_mode(0o644);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.as_mut_bytes()[..100].fill(0);
        header.as_mut_bytes()[..10].copy_from_slice(b"../escape\0");
        header.set_cksum();
        archive.append(&header, Cursor::new(b"x")).unwrap();
        archive.finish().unwrap();
    }
    assert!(matches!(
        materialize_source_bundle(&malicious, &"f".repeat(64), root.path()),
        Err(BuildSourceError::Path)
    ));
}
