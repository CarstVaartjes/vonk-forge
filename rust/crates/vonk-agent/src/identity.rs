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
use sha2::{Digest, Sha256};
use thiserror::Error;
use vonk_agent_protocol::generated::{
    AgentGenerationPointer as GenerationPointer, AgentIdentityMetadata as IdentityMetadata,
};
use x509_parser::{parse_x509_certificate, pem::parse_x509_pem};

const MAX_RETIRED_GENERATIONS: usize = 4;
/// How far this Spark's clock may lag the Controller's: the Controller signs a
/// certificate valid from the instant it issues it, so a fresh certificate is
/// not refused as "not yet valid" by a clock a little behind.  When it expires
/// is never extended.  The same allowance the Controller gives its CA tokens.
const CERTIFICATE_CLOCK_SKEW_SECONDS: i64 = 30;

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
    #[error("cannot replace the active identity generation")]
    ActiveGeneration,
    #[error("identity generation would move backwards")]
    GenerationRollback,
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

pub fn generate_pending(node_id: &str) -> Result<PendingIdentity, IdentityError> {
    pending_from_key(node_id, KeyPair::generate_for(&PKCS_ED25519)?)
}

fn pending_from_key(node_id: &str, key: KeyPair) -> Result<PendingIdentity, IdentityError> {
    if !valid_node_id(node_id) {
        return Err(IdentityError::Node);
    }
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
    let metadata = identity_metadata(material)?;
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

fn identity_metadata(material: &IdentityMaterial) -> Result<Vec<u8>, IdentityError> {
    Ok(serde_json::to_vec(&IdentityMetadata {
        fingerprint: material.fingerprint.clone(),
        generation: material.generation,
        node_id: material.node_id.clone(),
        serial: material.serial.clone(),
    })?)
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
    cleanup_abandoned_generations(root, None)?;
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

/// Reconstruct the CSR from the durable key after an interrupted write or
/// damaged CSR. Ed25519 signs deterministically, preserving the request bytes
/// across restarts. If the key is unavailable, the Controller reconciles any
/// previous staged issuance before accepting the new key's request.
pub fn prepare_pending(root: &Path, node_id: &str) -> Result<PendingIdentity, IdentityError> {
    let key = read_private(&root.join("pending-key.pem"))
        .ok()
        .and_then(|raw| String::from_utf8(raw).ok())
        .and_then(|pem| KeyPair::from_pem(&pem).ok());
    let pending = match key {
        Some(key) => pending_from_key(node_id, key)?,
        None => {
            // Preserve unusable projections without following their paths.
            // A surviving valid key instead reconstructs the same CSR above.
            ensure_private_directory(root)?;
            if fs::symlink_metadata(root.join("pending-key.pem")).is_ok()
                || fs::symlink_metadata(root.join("pending-csr.pem")).is_ok()
            {
                quarantine_pending(root)?;
            }
            generate_pending(node_id)?
        }
    };
    let matches = read_private(&root.join("pending-key.pem"))
        .is_ok_and(|raw| raw == pending.private_key_pem)
        && read_private(&root.join("pending-csr.pem")).is_ok_and(|raw| raw == pending.csr_pem);
    if !matches {
        persist_pending(root, &pending)?;
    }
    Ok(pending)
}

/// A staged pointer is bookkeeping, never authority. Preserve a damaged
/// pointer for diagnosis and replay the pending CSR through the Controller.
/// The active identity and generation directories are never replaced here.
pub fn observe_staged_identity(root: &Path) -> Result<Option<(u64, IdentityPaths)>, IdentityError> {
    match staged_identity_paths(root).and_then(|staged| {
        if let Some((_, paths)) = &staged {
            identity_expired(paths, Utc::now())?;
        }
        Ok(staged)
    }) {
        Ok(staged) => Ok(staged),
        Err(error) => {
            eprintln!("vonk-agent: staged certificate observation unavailable: {error}");
            retire_unavailable_staged(root)?;
            Ok(None)
        }
    }
}

fn quarantine_pending(root: &Path) -> Result<(), IdentityError> {
    // Rename the exact objects without following them. Active authority is
    // untouched; a replacement CSR uses the authenticated recovery endpoint
    // if the Controller already retained the interrupted generation.
    let quarantine = root.join(format!("pending-unavailable-{}", uuid::Uuid::new_v4()));
    fs::create_dir(&quarantine)?;
    fs::set_permissions(&quarantine, fs::Permissions::from_mode(0o700))?;
    for name in ["pending-key.pem", "pending-csr.pem"] {
        match fs::rename(root.join(name), quarantine.join(name)) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    File::open(root)?.sync_all()?;
    Ok(())
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

pub fn retire_unavailable_staged(root: &Path) -> Result<(), IdentityError> {
    match fs::rename(
        root.join("staged.json"),
        root.join(format!("staged-unavailable-{}", uuid::Uuid::new_v4())),
    ) {
        Ok(()) => File::open(root)?.sync_all()?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    Ok(())
}

pub fn identity_expired(paths: &IdentityPaths, now: DateTime<Utc>) -> Result<bool, IdentityError> {
    for path in [&paths.certificate, &paths.chain] {
        if certificate_expired(path, now)? {
            return Ok(true);
        }
    }
    Ok(false)
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
    if material.generation == 0 {
        return Err(IdentityError::Node);
    }
    let active_generation = match load_pointer(root, "active.json")? {
        Some(generation) => Some(generation),
        None => flat_generation(root)?,
    };
    if let Some(active_generation) = active_generation {
        if material.generation == active_generation {
            return Err(IdentityError::ActiveGeneration);
        }
        if material.generation < active_generation {
            return Err(IdentityError::GenerationRollback);
        }
    }
    let destination = root.join(generation_name(material.generation));
    if destination.try_exists()? {
        let metadata = fs::symlink_metadata(&destination)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(std::io::Error::other("identity generation is unsafe").into());
        }
        if load_pointer(root, "active.json")? == Some(material.generation) {
            return Err(IdentityError::ActiveGeneration);
        }
        // Do not leave a durable staged pointer referencing the directory
        // while that directory is being archived.  If the process stops in
        // the replacement gap, the pending CSR remains available and the
        // next renewal replay can restage the Controller's exact response.
        if load_pointer(root, "staged.json")? == Some(material.generation) {
            fs::remove_file(root.join("staged.json"))?;
            File::open(root)?.sync_all()?;
        }
        let expected_metadata = identity_metadata(material)?;
        let matches = [
            &(
                destination.join("private-key.pem"),
                material.private_key_pem.as_slice(),
            ),
            &(
                destination.join("certificate.pem"),
                material.certificate_pem.as_slice(),
            ),
            &(destination.join("chain.pem"), material.chain_pem.as_slice()),
        ]
        .iter()
        .all(|(path, expected)| read_private(path).is_ok_and(|actual| actual == *expected))
            && read_private(&destination.join("identity.json"))
                .is_ok_and(|actual| actual == expected_metadata);
        if !matches {
            let archive = replacement_archive_path(root, material.generation)?;
            fs::rename(&destination, archive)?;
            prune_retired_generations(root)?;
        }
    }
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
    File::open(root)?.sync_all()?;
    Ok(())
}

fn flat_generation(root: &Path) -> Result<Option<u64>, IdentityError> {
    let path = root.join("identity.json");
    if !path.try_exists()? {
        return Ok(None);
    }
    let raw = read_private(&path)?;
    let metadata: IdentityMetadata = serde_json::from_slice(&raw)
        .map_err(|_| std::io::Error::other("flat identity metadata is invalid"))?;
    Ok(Some(metadata.generation))
}

fn replacement_archive_path(root: &Path, generation: u64) -> Result<PathBuf, IdentityError> {
    let retired = root.join("retired-generations");
    ensure_private_directory(&retired)?;
    let base = format!(
        "identity-{}-{generation:020}-{}",
        chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default(),
        std::process::id()
    );
    for suffix in 0..1000_u16 {
        let name = if suffix == 0 {
            base.clone()
        } else {
            format!("{base}-{suffix}")
        };
        let path = retired.join(name);
        if !path.try_exists()? {
            return Ok(path);
        }
    }
    Err(std::io::Error::other("identity replacement archive is exhausted").into())
}

fn prune_retired_generations(root: &Path) -> Result<(), IdentityError> {
    let retired = root.join("retired-generations");
    let mut archives = Vec::new();
    for entry in fs::read_dir(&retired)? {
        let entry = entry?;
        if !entry.file_name().to_string_lossy().starts_with("identity-") {
            continue;
        }
        let metadata = fs::symlink_metadata(entry.path())?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(std::io::Error::other("retired identity archive is unsafe").into());
        }
        archives.push(entry.path());
    }
    archives.sort();
    while archives.len() > MAX_RETIRED_GENERATIONS {
        fs::remove_dir_all(archives.remove(0))?;
    }
    File::open(retired)?.sync_all()?;
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
    cleanup_abandoned_generations(root, Some(generation))?;
    File::open(root)?.sync_all()?;
    Ok(())
}

fn cleanup_abandoned_generations(
    root: &Path,
    active_generation: Option<u64>,
) -> Result<(), IdentityError> {
    let active_name = active_generation.map(generation_name);
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if (name.starts_with("generation-") || name.starts_with("replaced-generation-"))
            && active_name.as_deref() != Some(name.as_ref())
        {
            let metadata = fs::symlink_metadata(entry.path())?;
            if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
                return Err(std::io::Error::other("identity generation is unsafe").into());
            }
            fs::remove_dir_all(entry.path())?;
        }
    }
    Ok(())
}

pub fn active_certificate_serial(root: &Path) -> Result<String, IdentityError> {
    let paths = active_identity_paths(root)?;
    let directory = paths.certificate.parent().ok_or(IdentityError::Node)?;
    let metadata: IdentityMetadata =
        serde_json::from_slice(&read_private(&directory.join("identity.json"))?)?;
    Ok(metadata.serial)
}

pub fn renewal_due(root: &Path, now: DateTime<Utc>) -> Result<bool, IdentityError> {
    Ok(now >= renewal_time(root)?)
}

/// The scheduling boundary owned by the active signed certificate's validity.
pub fn renewal_time(root: &Path) -> Result<DateTime<Utc>, IdentityError> {
    let paths = active_identity_paths(root)?;
    let (not_before, not_after) = certificate_validity(&paths.certificate)?;
    let lifetime = not_after - not_before;
    if lifetime <= chrono::Duration::zero() {
        return Err(std::io::Error::other("active certificate validity is invalid").into());
    }
    Ok(not_after - lifetime / 2)
}

/// One random moment per observed certificate, with no persisted schedule.
#[derive(Default)]
pub struct RenewalWindow {
    certificate: Vec<u8>,
    due: Option<DateTime<Utc>>,
}

impl RenewalWindow {
    pub fn due(
        &mut self,
        root: &Path,
        now: DateTime<Utc>,
        entropy: impl FnOnce() -> u16,
    ) -> Result<bool, IdentityError> {
        let paths = active_identity_paths(root)?;
        let certificate = read_private(&paths.certificate)?;
        if self.due.is_none() || self.certificate != certificate {
            let (start, end) = certificate_validity(&paths.certificate)?;
            let lifetime = end - start;
            if lifetime <= chrono::Duration::zero() {
                return Err(std::io::Error::other("active certificate validity is invalid").into());
            }
            // Uniformly sample 50–55% elapsed. A restart at 55% is always due.
            let window_ms = (lifetime / 20).num_milliseconds();
            let offset = window_ms * i64::from(entropy()) / i64::from(u16::MAX);
            self.due = Some(start + lifetime / 2 + chrono::Duration::milliseconds(offset));
            self.certificate = certificate;
        }
        Ok(self.due.is_some_and(|due| now >= due))
    }
}

/// Renewal health derives from the same signed validity as admission.
pub fn certificate_near_expiry(root: &Path, now: DateTime<Utc>) -> Result<bool, IdentityError> {
    let paths = active_identity_paths(root)?;
    let (start, end) = certificate_validity(&paths.certificate)?;
    Ok(end - now < (end - start) / 4)
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

fn certificate_expired(path: &Path, now: DateTime<Utc>) -> Result<bool, IdentityError> {
    let raw = read_private(path)?;
    let mut remaining = raw.as_slice();
    let mut found = false;
    while !remaining.iter().all(u8::is_ascii_whitespace) {
        let (rest, pem) = parse_x509_pem(remaining)
            .map_err(|_| std::io::Error::other("identity certificate chain is invalid"))?;
        let (_, certificate) = parse_x509_certificate(&pem.contents)
            .map_err(|_| std::io::Error::other("identity certificate chain is invalid"))?;
        let not_before = Utc
            .timestamp_opt(certificate.validity().not_before.timestamp(), 0)
            .single()
            .ok_or_else(|| std::io::Error::other("identity certificate validity is invalid"))?;
        let not_after = Utc
            .timestamp_opt(certificate.validity().not_after.timestamp(), 0)
            .single()
            .ok_or_else(|| std::io::Error::other("identity certificate validity is invalid"))?;
        found = true;
        if now + chrono::Duration::seconds(CERTIFICATE_CLOCK_SKEW_SECONDS) < not_before
            || now >= not_after
        {
            return Ok(true);
        }
        remaining = rest;
    }
    if !found {
        return Err(std::io::Error::other("identity certificate chain is empty").into());
    }
    Ok(false)
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

pub(crate) fn atomic_private_write(
    root: &Path,
    name: &str,
    value: &[u8],
) -> Result<(), std::io::Error> {
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
mod tests;

#[cfg(test)]
#[path = "identity_renewal_tests.rs"]
mod renewal_tests;
