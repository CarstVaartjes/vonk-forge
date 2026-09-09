use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};

use chrono::{DateTime, TimeZone, Utc};
use rcgen::string::Ia5String;
use rcgen::{
    CertificateParams, DistinguishedName, DnType, KeyPair, PKCS_ED25519, PublicKeyData, SanType,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use x509_parser::{parse_x509_certificate, pem::parse_x509_pem};

#[derive(Debug, Error)]
pub enum IdentityError {
    #[error("identity storage failed")]
    Io(#[from] std::io::Error),
    #[error("identity generation failed")]
    Generate(#[from] rcgen::Error),
    #[error("node identity is invalid")]
    Node,
    #[error("identity metadata serialization failed")]
    Json(#[from] serde_json::Error),
}

#[derive(Debug)]
pub struct PendingIdentity {
    pub private_key_pem: Vec<u8>,
    pub csr_pem: Vec<u8>,
    pub public_key_fingerprint: String,
}

#[derive(Debug, Clone)]
pub struct IdentityMaterial {
    pub node_id: String,
    pub private_key_pem: Vec<u8>,
    pub certificate_pem: Vec<u8>,
    pub chain_pem: Vec<u8>,
    pub serial: String,
    pub fingerprint: String,
    pub generation: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IdentityPaths {
    pub private_key: PathBuf,
    pub certificate: PathBuf,
    pub chain: PathBuf,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct GenerationPointer {
    generation: u64,
}

#[derive(Serialize)]
struct IdentityMetadata<'a> {
    fingerprint: &'a str,
    generation: u64,
    node_id: &'a str,
    serial: &'a str,
}

pub fn generate_pending(node_id: &str) -> Result<PendingIdentity, IdentityError> {
    if !valid_node_id(node_id) {
        return Err(IdentityError::Node);
    }
    let key = KeyPair::generate_for(&PKCS_ED25519)?;
    let mut parameters = CertificateParams::default();
    let mut distinguished_name = DistinguishedName::new();
    distinguished_name.push(DnType::CommonName, node_id);
    parameters.distinguished_name = distinguished_name;
    parameters.subject_alt_names = vec![SanType::URI(
        Ia5String::try_from(format!("spiffe://vonk-forge.local/node/{node_id}"))
            .map_err(|_| IdentityError::Node)?,
    )];
    let csr = parameters.serialize_request(&key)?.pem()?;
    let public_key_fingerprint = hex::encode(Sha256::digest(key.subject_public_key_info()));
    Ok(PendingIdentity {
        private_key_pem: key.serialize_pem().into_bytes(),
        csr_pem: csr.into_bytes(),
        public_key_fingerprint,
    })
}

pub fn persist_identity(root: &Path, material: &IdentityMaterial) -> Result<(), IdentityError> {
    if !valid_node_id(&material.node_id) {
        return Err(IdentityError::Node);
    }
    ensure_private_directory(root)?;
    let metadata = serde_json::to_vec(&IdentityMetadata {
        fingerprint: &material.fingerprint,
        generation: material.generation,
        node_id: &material.node_id,
        serial: &material.serial,
    })?;
    for (name, value) in [
        ("private-key.pem", material.private_key_pem.as_slice()),
        ("certificate.pem", material.certificate_pem.as_slice()),
        ("chain.pem", material.chain_pem.as_slice()),
        ("identity.json", metadata.as_slice()),
    ] {
        atomic_private_write(root, name, value)?;
    }
    File::open(root)?.sync_all()?;
    Ok(())
}

/// Persist a newly paired identity and select it even when a previous
/// certificate rotation left an active generation pointer behind.
///
/// Pointer retirement is deliberately last. If the process is interrupted
/// before that switch, the previous identity remains selected and enrollment
/// replay can safely finish the replacement. Retained generation directories are
/// audit material: a later rotation verifies their contents before reuse and
/// archives an unselected collision from a previous pairing lineage.
pub fn persist_paired_identity(
    root: &Path,
    material: &IdentityMaterial,
) -> Result<(), IdentityError> {
    persist_identity(root, material)?;
    archive_pointer(root, "staged.json", "pre-reenroll-staged.json")?;
    archive_pointer(root, "active.json", "pre-reenroll-active.json")?;
    clear_pending(root)?;
    File::open(root)?.sync_all()?;
    Ok(())
}

pub fn persist_pending(root: &Path, pending: &PendingIdentity) -> Result<(), IdentityError> {
    ensure_private_directory(root)?;
    atomic_private_write(root, "pending-key.pem", &pending.private_key_pem)?;
    atomic_private_write(root, "pending-csr.pem", &pending.csr_pem)?;
    File::open(root)?.sync_all()?;
    Ok(())
}

pub fn load_pending(root: &Path) -> Result<Option<PendingIdentity>, IdentityError> {
    let key_path = root.join("pending-key.pem");
    let csr_path = root.join("pending-csr.pem");
    let key_exists = key_path.try_exists()?;
    let csr_exists = csr_path.try_exists()?;
    if key_exists != csr_exists {
        return Err(std::io::Error::other("pending identity is incomplete").into());
    }
    if !key_exists {
        return Ok(None);
    }
    let private_key_pem = read_private(&key_path)?;
    let csr_pem = read_private(&csr_path)?;
    let key = KeyPair::from_pem(
        std::str::from_utf8(&private_key_pem)
            .map_err(|_| std::io::Error::other("pending key is not UTF-8 PEM"))?,
    )?;
    Ok(Some(PendingIdentity {
        public_key_fingerprint: hex::encode(Sha256::digest(key.subject_public_key_info())),
        private_key_pem,
        csr_pem,
    }))
}

pub fn clear_pending(root: &Path) -> Result<(), IdentityError> {
    for name in ["pending-key.pem", "pending-csr.pem"] {
        match fs::remove_file(root.join(name)) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    File::open(root)?.sync_all()?;
    Ok(())
}

pub fn active_identity_paths(root: &Path) -> Result<IdentityPaths, IdentityError> {
    match load_pointer(root, "active.json")? {
        Some(generation) => generation_paths(root, generation),
        None => flat_paths(root),
    }
}

pub fn staged_identity_paths(root: &Path) -> Result<Option<(u64, IdentityPaths)>, IdentityError> {
    load_pointer(root, "staged.json")?
        .map(|generation| Ok((generation, generation_paths(root, generation)?)))
        .transpose()
}

pub fn identity_expired(paths: &IdentityPaths, now: DateTime<Utc>) -> Result<bool, IdentityError> {
    let (_, not_after) = certificate_validity(&paths.certificate)?;
    Ok(now >= not_after)
}

pub fn retire_expired_staged(root: &Path, generation: u64) -> Result<(), IdentityError> {
    if load_pointer(root, "staged.json")? != Some(generation) {
        return Err(std::io::Error::other("staged identity generation changed").into());
    }
    generation_paths(root, generation)?;
    archive_pointer(root, "staged.json", "expired-staged.json")?;
    clear_pending(root)?;
    File::open(root)?.sync_all()?;
    Ok(())
}

pub fn stage_identity(root: &Path, material: &IdentityMaterial) -> Result<(), IdentityError> {
    ensure_private_directory(root)?;
    if material.generation == 0 || !valid_node_id(&material.node_id) {
        return Err(IdentityError::Node);
    }
    let active = load_pointer(root, "active.json")?;
    let staged = load_pointer(root, "staged.json")?;
    if staged.is_some_and(|generation| generation != material.generation) {
        return Err(std::io::Error::other("another identity generation is staged").into());
    }
    let destination = root.join(generation_name(material.generation));
    let exists = match fs::symlink_metadata(&destination) {
        Ok(_) => true,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
        Err(error) => return Err(error.into()),
    };
    let matches = exists && identity_matches(&destination, material)?;
    if !matches {
        if active == Some(material.generation) || staged == Some(material.generation) {
            return Err(std::io::Error::other("selected identity generation differs").into());
        }
        // Write a complete private directory first. A failed write cannot change
        // the current identity or move the retained generation out of the way.
        let temporary = tempfile::Builder::new()
            .prefix(".identity-generation-")
            .tempdir_in(root)?;
        persist_identity(temporary.path(), material)?;
        if exists {
            archive_unselected_generation(root, &destination)?;
        }
        // Neither pointer selects this destination. If interrupted after the
        // archive, replay installs it anew; if interrupted after this rename,
        // replay must verify all material before publishing the staged pointer.
        fs::rename(temporary.path(), &destination)?;
        File::open(root)?.sync_all()?;
    }
    atomic_private_write(
        root,
        "staged.json",
        &serde_json::to_vec(&GenerationPointer {
            generation: material.generation,
        })?,
    )?;
    File::open(root)?.sync_all()?;
    Ok(())
}

fn identity_matches(root: &Path, material: &IdentityMaterial) -> Result<bool, IdentityError> {
    let metadata = fs::symlink_metadata(root)?;
    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
        return Err(std::io::Error::other("identity generation is unsafe").into());
    }
    let expected_metadata = serde_json::to_vec(&IdentityMetadata {
        fingerprint: &material.fingerprint,
        generation: material.generation,
        node_id: &material.node_id,
        serial: &material.serial,
    })?;
    let mut matches = true;
    for (name, expected) in [
        ("private-key.pem", material.private_key_pem.as_slice()),
        ("certificate.pem", material.certificate_pem.as_slice()),
        ("chain.pem", material.chain_pem.as_slice()),
        ("identity.json", expected_metadata.as_slice()),
    ] {
        // Read every file even after a mismatch so an unsafe or incomplete
        // retained directory fails closed rather than being silently replaced.
        matches &= read_private(&root.join(name))? == expected;
    }
    Ok(matches)
}

fn archive_unselected_generation(root: &Path, generation: &Path) -> Result<(), IdentityError> {
    let retired = root.join("retired-generations");
    ensure_private_directory(&retired)?;
    let archive = tempfile::Builder::new()
        .prefix("identity-")
        .tempdir_in(&retired)?
        .keep();
    // Keep the archive even if a later operation fails. Old key/certificate
    // bytes must never be removed by a temporary-directory destructor.
    fs::rename(generation, archive.join("identity"))?;
    File::open(&archive)?.sync_all()?;
    File::open(&retired)?.sync_all()?;
    File::open(root)?.sync_all()?;
    Ok(())
}

pub fn publish_staged(root: &Path, generation: u64) -> Result<(), IdentityError> {
    if load_pointer(root, "staged.json")? != Some(generation) {
        return Err(std::io::Error::other("staged identity generation changed").into());
    }
    generation_paths(root, generation)?;
    atomic_private_write(
        root,
        "active.json",
        &serde_json::to_vec(&GenerationPointer { generation })?,
    )?;
    match fs::remove_file(root.join("staged.json")) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    clear_pending(root)?;
    File::open(root)?.sync_all()?;
    Ok(())
}

pub fn renewal_due(root: &Path, now: DateTime<Utc>) -> Result<bool, IdentityError> {
    let paths = active_identity_paths(root)?;
    let (not_before, not_after) = certificate_validity(&paths.certificate)?;
    let lifetime = not_after - not_before;
    if lifetime <= chrono::Duration::zero() {
        return Err(std::io::Error::other("active certificate validity is invalid").into());
    }
    Ok(now >= not_after - lifetime / 3)
}

fn certificate_validity(path: &Path) -> Result<(DateTime<Utc>, DateTime<Utc>), IdentityError> {
    let certificate_pem = read_private(path)?;
    let (_, pem) = parse_x509_pem(&certificate_pem)
        .map_err(|_| std::io::Error::other("identity certificate PEM is invalid"))?;
    let (_, certificate) = parse_x509_certificate(&pem.contents)
        .map_err(|_| std::io::Error::other("identity certificate is invalid"))?;
    let not_before = Utc
        .timestamp_opt(certificate.validity().not_before.timestamp(), 0)
        .single()
        .ok_or_else(|| std::io::Error::other("identity certificate validity is invalid"))?;
    let not_after = Utc
        .timestamp_opt(certificate.validity().not_after.timestamp(), 0)
        .single()
        .ok_or_else(|| std::io::Error::other("identity certificate validity is invalid"))?;
    Ok((not_before, not_after))
}

fn load_pointer(root: &Path, name: &str) -> Result<Option<u64>, IdentityError> {
    let path = root.join(name);
    if !path.try_exists()? {
        return Ok(None);
    }
    let raw = read_private(&path)?;
    let pointer: GenerationPointer = serde_json::from_slice(&raw)?;
    if pointer.generation == 0 {
        return Err(std::io::Error::other("identity generation pointer is invalid").into());
    }
    Ok(Some(pointer.generation))
}

fn archive_pointer(root: &Path, name: &str, archive: &str) -> Result<(), IdentityError> {
    let path = root.join(name);
    match path.try_exists() {
        Ok(false) => return Ok(()),
        Ok(true) => {}
        Err(error) => return Err(error.into()),
    }
    let raw = read_private(&path)?;
    let pointer: GenerationPointer = serde_json::from_slice(&raw)?;
    if pointer.generation == 0 {
        return Err(std::io::Error::other("identity generation pointer is invalid").into());
    }
    atomic_private_write(root, archive, &raw)?;
    fs::remove_file(path)?;
    File::open(root)?.sync_all()?;
    Ok(())
}

fn generation_paths(root: &Path, generation: u64) -> Result<IdentityPaths, IdentityError> {
    let directory = root.join(generation_name(generation));
    let metadata = fs::symlink_metadata(&directory)?;
    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
        return Err(std::io::Error::other("identity generation is unsafe").into());
    }
    let paths = flat_paths(&directory)?;
    for path in [&paths.private_key, &paths.certificate, &paths.chain] {
        read_private(path)?;
    }
    Ok(paths)
}

fn flat_paths(root: &Path) -> Result<IdentityPaths, IdentityError> {
    let paths = IdentityPaths {
        private_key: root.join("private-key.pem"),
        certificate: root.join("certificate.pem"),
        chain: root.join("chain.pem"),
    };
    for path in [&paths.private_key, &paths.certificate, &paths.chain] {
        read_private(path)?;
    }
    Ok(paths)
}

fn generation_name(generation: u64) -> String {
    format!("generation-{generation:020}")
}

fn ensure_private_directory(root: &Path) -> Result<(), std::io::Error> {
    fs::create_dir_all(root)?;
    let metadata = fs::symlink_metadata(root)?;
    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
        return Err(std::io::Error::other("identity root is unsafe"));
    }
    fs::set_permissions(root, fs::Permissions::from_mode(0o700))
}

fn atomic_private_write(root: &Path, name: &str, value: &[u8]) -> Result<(), std::io::Error> {
    let temporary = temporary_path(root, name);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    if let Err(error) = (|| {
        file.write_all(value)?;
        file.sync_all()?;
        fs::rename(&temporary, root.join(name))?;
        Ok::<(), std::io::Error>(())
    })() {
        let _ = fs::remove_file(&temporary);
        return Err(error);
    }
    Ok(())
}

fn read_private(path: &Path) -> Result<Vec<u8>, std::io::Error> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.permissions().mode() & 0o077 != 0
        || metadata.len() > 64 * 1024
    {
        return Err(std::io::Error::other("pending identity path is unsafe"));
    }
    fs::read(path)
}

fn temporary_path(root: &Path, name: &str) -> PathBuf {
    root.join(format!(".{name}.{}.tmp", std::process::id()))
}

fn valid_node_id(value: &str) -> bool {
    value.len() == 36
        && value.starts_with("spk_")
        && value[4..]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rcgen::{CertificateParams, KeyPair, PKCS_ED25519, date_time_ymd};
    use std::os::unix::fs::{MetadataExt, symlink};
    use tempfile::tempdir;

    const NODE_ID: &str = "spk_0123456789abcdef0123456789abcdef";

    fn material(generation: u64, marker: u8) -> IdentityMaterial {
        IdentityMaterial {
            node_id: NODE_ID.to_owned(),
            private_key_pem: vec![marker, b'k'],
            certificate_pem: vec![marker, b'c'],
            chain_pem: vec![marker, b'h'],
            serial: format!("serial-{generation}"),
            fingerprint: format!("fingerprint-{generation}"),
            generation,
        }
    }

    fn certificate_material(generation: u64, expired: bool) -> IdentityMaterial {
        certificate_material_until(
            generation,
            if expired { (2026, 8, 2) } else { (2026, 8, 4) },
        )
    }

    fn certificate_material_until(generation: u64, not_after: (i32, u8, u8)) -> IdentityMaterial {
        let key = KeyPair::generate_for(&PKCS_ED25519).unwrap();
        let mut parameters = CertificateParams::default();
        parameters.not_before = date_time_ymd(2026, 8, 1);
        parameters.not_after = date_time_ymd(not_after.0, not_after.1, not_after.2);
        let certificate = parameters.self_signed(&key).unwrap();
        IdentityMaterial {
            node_id: NODE_ID.to_owned(),
            private_key_pem: key.serialize_pem().into_bytes(),
            certificate_pem: certificate.pem().into_bytes(),
            chain_pem: certificate.pem().into_bytes(),
            serial: format!("serial-{generation}"),
            fingerprint: format!("fingerprint-{generation}"),
            generation,
        }
    }

    #[test]
    fn expired_staged_identity_is_retired_without_changing_the_active_identity() {
        let temporary = tempdir().unwrap();
        let root = temporary.path().join("credentials");
        persist_identity(&root, &certificate_material(1, false)).unwrap();
        stage_identity(&root, &certificate_material(2, true)).unwrap();
        persist_pending(&root, &generate_pending(NODE_ID).unwrap()).unwrap();
        let (generation, paths) = staged_identity_paths(&root).unwrap().unwrap();
        let now = Utc.with_ymd_and_hms(2026, 8, 3, 0, 0, 0).unwrap();

        assert!(identity_expired(&paths, now).unwrap());
        retire_expired_staged(&root, generation).unwrap();

        assert!(staged_identity_paths(&root).unwrap().is_none());
        assert!(root.join("expired-staged.json").is_file());
        assert!(!root.join("pending-key.pem").exists());
        assert!(!root.join("pending-csr.pem").exists());
        assert_eq!(
            active_identity_paths(&root).unwrap(),
            flat_paths(&root).unwrap(),
        );
    }

    #[test]
    fn paired_identity_retires_stale_rotation_pointers_only_after_replacement_exists() {
        let temporary = tempdir().unwrap();
        let root = temporary.path().join("credentials");
        stage_identity(&root, &material(3, b'o')).unwrap();
        publish_staged(&root, 3).unwrap();

        persist_identity(&root, &material(1, b'n')).unwrap();
        assert_eq!(
            fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
            vec![b'o', b'c'],
            "writing replacement material alone must not bypass the active pointer",
        );

        persist_paired_identity(&root, &material(1, b'n')).unwrap();

        assert!(!root.join("active.json").exists());
        assert_eq!(
            fs::read(root.join("pre-reenroll-active.json")).unwrap(),
            br#"{"generation":3}"#,
        );
        assert!(root.join(generation_name(3)).is_dir());
        assert_eq!(
            fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
            vec![b'n', b'c'],
        );
    }

    #[test]
    fn paired_identity_archives_staged_pointer_before_switching_active_identity() {
        let temporary = tempdir().unwrap();
        let root = temporary.path().join("credentials");
        stage_identity(&root, &material(2, b'o')).unwrap();
        publish_staged(&root, 2).unwrap();
        stage_identity(&root, &material(3, b's')).unwrap();

        persist_paired_identity(&root, &material(1, b'n')).unwrap();

        assert!(!root.join("staged.json").exists());
        assert_eq!(
            fs::read(root.join("pre-reenroll-staged.json")).unwrap(),
            br#"{"generation":3}"#,
        );
        assert_eq!(
            fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
            vec![b'n', b'c'],
        );
    }

    fn retired_identities(root: &Path) -> Vec<PathBuf> {
        let retired = root.join("retired-generations");
        if !retired.exists() {
            return Vec::new();
        }
        fs::read_dir(retired)
            .unwrap()
            .map(|entry| entry.unwrap().path().join("identity"))
            .collect()
    }

    #[test]
    fn rotation_after_reenrollment_archives_a_different_pairing_lineage() {
        let temporary = tempdir().unwrap();
        let root = temporary.path().join("credentials");
        // The previous Controller's generation need not be expired to collide.
        let old = certificate_material_until(2, (2099, 1, 1));
        stage_identity(&root, &old).unwrap();
        publish_staged(&root, 2).unwrap();
        assert!(!identity_expired(&active_identity_paths(&root).unwrap(), Utc::now()).unwrap());
        let paired = certificate_material_until(1, (2099, 1, 1));
        persist_paired_identity(&root, &paired).unwrap();
        let rotated = certificate_material_until(2, (2099, 1, 1));

        stage_identity(&root, &rotated).unwrap();

        let staged = staged_identity_paths(&root).unwrap().unwrap().1;
        assert_eq!(
            fs::read(staged.certificate).unwrap(),
            rotated.certificate_pem
        );
        assert_eq!(
            fs::read(staged.private_key).unwrap(),
            rotated.private_key_pem
        );
        assert_eq!(
            fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
            paired.certificate_pem,
        );
        let archives = retired_identities(&root);
        assert_eq!(archives.len(), 1);
        assert_eq!(
            fs::read(archives[0].join("certificate.pem")).unwrap(),
            old.certificate_pem
        );
        assert_eq!(
            fs::read(archives[0].join("private-key.pem")).unwrap(),
            old.private_key_pem
        );
        assert_eq!(fs::metadata(&archives[0]).unwrap().mode() & 0o777, 0o700);
        assert_eq!(
            fs::metadata(archives[0].join("private-key.pem"))
                .unwrap()
                .mode()
                & 0o777,
            0o600
        );
        publish_staged(&root, 2).unwrap();
        assert_eq!(
            fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
            rotated.certificate_pem,
        );
    }

    #[test]
    fn exact_replay_preserves_generation_files_and_does_not_archive() {
        let temporary = tempdir().unwrap();
        let root = temporary.path().join("credentials");
        let issued = certificate_material(2, false);
        stage_identity(&root, &issued).unwrap();
        let path = staged_identity_paths(&root).unwrap().unwrap().1.private_key;
        let before = fs::metadata(&path).unwrap();

        stage_identity(&root, &issued).unwrap();

        let after = fs::metadata(&path).unwrap();
        assert_eq!(
            (before.ino(), before.mtime(), before.mtime_nsec()),
            (after.ino(), after.mtime(), after.mtime_nsec())
        );
        assert_eq!(fs::read(path).unwrap(), issued.private_key_pem);
        assert!(retired_identities(&root).is_empty());
    }

    #[test]
    fn selected_generation_rejects_every_material_mismatch_without_mutation() {
        for active in [false, true] {
            for field in ["key", "certificate", "chain", "metadata"] {
                let temporary = tempdir().unwrap();
                let root = temporary.path().join("credentials");
                let original = certificate_material(2, false);
                stage_identity(&root, &original).unwrap();
                if active {
                    publish_staged(&root, 2).unwrap();
                }
                let mut different = original.clone();
                let other = certificate_material(2, false);
                match field {
                    "key" => different.private_key_pem = other.private_key_pem,
                    "certificate" => different.certificate_pem = other.certificate_pem,
                    "chain" => different.chain_pem = other.chain_pem,
                    "metadata" => different.serial = "different-serial".to_owned(),
                    _ => unreachable!(),
                }
                assert!(
                    stage_identity(&root, &different).is_err(),
                    "{field}, active={active}"
                );
                let directory = root.join(generation_name(2));
                assert_eq!(
                    fs::read(directory.join("private-key.pem")).unwrap(),
                    original.private_key_pem
                );
                assert_eq!(
                    fs::read(directory.join("certificate.pem")).unwrap(),
                    original.certificate_pem
                );
                assert_eq!(
                    load_pointer(&root, "active.json").unwrap(),
                    active.then_some(2)
                );
                assert_eq!(
                    load_pointer(&root, "staged.json").unwrap(),
                    (!active).then_some(2)
                );
                assert!(retired_identities(&root).is_empty());
            }
        }
    }

    #[test]
    fn interrupted_collision_replays_before_and_after_replacement_rename() {
        for replacement_written in [false, true] {
            let temporary = tempdir().unwrap();
            let root = temporary.path().join("credentials");
            let paired = certificate_material(1, false);
            persist_paired_identity(&root, &paired).unwrap();
            let destination = root.join(generation_name(2));
            let old = certificate_material(2, true);
            persist_identity(&destination, &old).unwrap();
            // Simulate process loss after archiving the unselected collision,
            // optionally after installing the new directory but before staging.
            archive_unselected_generation(&root, &destination).unwrap();
            let issued = certificate_material(2, false);
            if replacement_written {
                persist_identity(&destination, &issued).unwrap();
            }
            stage_identity(&root, &issued).unwrap();
            assert_eq!(retired_identities(&root).len(), 1);
            assert_eq!(
                fs::read(staged_identity_paths(&root).unwrap().unwrap().1.certificate).unwrap(),
                issued.certificate_pem
            );
            assert_eq!(
                fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
                paired.certificate_pem
            );
        }
    }

    #[test]
    fn archive_failure_preserves_unselected_material_and_active_identity() {
        let temporary = tempdir().unwrap();
        let root = temporary.path().join("credentials");
        let paired = certificate_material(1, false);
        persist_paired_identity(&root, &paired).unwrap();
        let destination = root.join(generation_name(2));
        let old = certificate_material(2, true);
        persist_identity(&destination, &old).unwrap();
        symlink(temporary.path(), root.join("retired-generations")).unwrap();

        assert!(stage_identity(&root, &certificate_material(2, false)).is_err());

        assert_eq!(
            fs::read(destination.join("private-key.pem")).unwrap(),
            old.private_key_pem
        );
        assert_eq!(
            fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
            paired.certificate_pem
        );
        assert!(!root.join("staged.json").exists());
    }

    #[test]
    fn unsafe_generation_and_conflicting_staged_pointer_fail_closed() {
        let temporary = tempdir().unwrap();
        let root = temporary.path().join("credentials");
        persist_paired_identity(&root, &certificate_material(1, false)).unwrap();
        let destination = root.join(generation_name(2));
        symlink(root.join("absent"), &destination).unwrap();
        assert!(stage_identity(&root, &certificate_material(2, false)).is_err());
        assert!(
            fs::symlink_metadata(&destination)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        fs::remove_file(destination).unwrap();
        stage_identity(&root, &certificate_material(2, false)).unwrap();

        assert!(stage_identity(&root, &certificate_material(3, false)).is_err());
        assert_eq!(load_pointer(&root, "staged.json").unwrap(), Some(2));
        assert!(!root.join(generation_name(3)).exists());
    }

    #[test]
    fn damaged_unselected_generations_leave_both_pointers_unchanged() {
        for damage in ["missing-chain", "unsafe-mode", "regular-file"] {
            let temporary = tempdir().unwrap();
            let root = temporary.path().join("credentials");
            let active = certificate_material(1, false);
            stage_identity(&root, &active).unwrap();
            publish_staged(&root, 1).unwrap();
            let destination = root.join(generation_name(2));
            let old = certificate_material(2, false);
            persist_identity(&destination, &old).unwrap();
            match damage {
                "missing-chain" => fs::remove_file(destination.join("chain.pem")).unwrap(),
                "unsafe-mode" => fs::set_permissions(
                    destination.join("chain.pem"),
                    fs::Permissions::from_mode(0o644),
                )
                .unwrap(),
                "regular-file" => {
                    fs::remove_dir_all(&destination).unwrap();
                    fs::write(&destination, b"damaged-generation").unwrap();
                }
                _ => unreachable!(),
            }
            let identity_before = fs::read(root.join("active.json")).unwrap();

            assert!(
                stage_identity(&root, &certificate_material(2, false)).is_err(),
                "{damage}"
            );

            assert_eq!(fs::read(root.join("active.json")).unwrap(), identity_before);
            assert!(!root.join("staged.json").exists());
            assert_eq!(
                fs::read(active_identity_paths(&root).unwrap().certificate).unwrap(),
                active.certificate_pem
            );
            assert!(retired_identities(&root).is_empty());
            assert!(destination.exists());
        }
    }

    #[test]
    fn interrupted_private_temporary_directory_is_not_selected_or_removed() {
        let temporary = tempdir().unwrap();
        let root = temporary.path().join("credentials");
        persist_paired_identity(&root, &certificate_material(1, false)).unwrap();
        let abandoned = root.join(".identity-generation-interrupted");
        let old = certificate_material(2, false);
        persist_identity(&abandoned, &old).unwrap();
        let issued = certificate_material(2, false);

        stage_identity(&root, &issued).unwrap();

        assert_eq!(
            fs::read(abandoned.join("private-key.pem")).unwrap(),
            old.private_key_pem
        );
        assert_eq!(
            fs::read(staged_identity_paths(&root).unwrap().unwrap().1.certificate).unwrap(),
            issued.certificate_pem
        );
        assert!(retired_identities(&root).is_empty());
    }
}
