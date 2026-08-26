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
/// replay can safely finish the replacement.
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

pub fn stage_identity(root: &Path, material: &IdentityMaterial) -> Result<(), IdentityError> {
    ensure_private_directory(root)?;
    if material.generation == 0 {
        return Err(IdentityError::Node);
    }
    let destination = root.join(generation_name(material.generation));
    if !destination.try_exists()? {
        let temporary = root.join(format!(
            ".{}.{}.tmp",
            generation_name(material.generation),
            std::process::id()
        ));
        if temporary.try_exists()? {
            fs::remove_dir_all(&temporary)?;
        }
        persist_identity(&temporary, material)?;
        fs::rename(&temporary, &destination)?;
        File::open(root)?.sync_all()?;
    }
    atomic_private_write(
        root,
        "staged.json",
        &serde_json::to_vec(&GenerationPointer {
            generation: material.generation,
        })?,
    )?;
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
    let certificate_pem = read_private(&paths.certificate)?;
    let (_, pem) = parse_x509_pem(&certificate_pem)
        .map_err(|_| std::io::Error::other("active certificate PEM is invalid"))?;
    let (_, certificate) = parse_x509_certificate(&pem.contents)
        .map_err(|_| std::io::Error::other("active certificate is invalid"))?;
    let not_before = Utc
        .timestamp_opt(certificate.validity().not_before.timestamp(), 0)
        .single()
        .ok_or_else(|| std::io::Error::other("active certificate validity is invalid"))?;
    let not_after = Utc
        .timestamp_opt(certificate.validity().not_after.timestamp(), 0)
        .single()
        .ok_or_else(|| std::io::Error::other("active certificate validity is invalid"))?;
    let lifetime = not_after - not_before;
    if lifetime <= chrono::Duration::zero() {
        return Err(std::io::Error::other("active certificate validity is invalid").into());
    }
    Ok(now >= not_after - lifetime / 3)
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
}
