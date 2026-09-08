//! Canonical, bounded source-bundle verification and materialization.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, OpenOptions},
    io::{Cursor, Read, Write},
    path::{Component, Path, PathBuf},
};

use thiserror::Error;
use vonk_agent_protocol::{
    canonical_json,
    generated::{SourceBundleDigestManifest, SourceBundleFile},
    hex_sha256,
};

const MAX_ARCHIVE_BYTES: usize = 64 * 1024 * 1024;
const MAX_FILES: usize = 4096;
const MAX_FILE_BYTES: u64 = 32 * 1024 * 1024;
const MAX_TOTAL_BYTES: u64 = 256 * 1024 * 1024;

#[derive(Debug, Error)]
pub enum BuildSourceError {
    #[error("source bundle archive is invalid")]
    Archive,
    #[error("source bundle path is forbidden")]
    Path,
    #[error("source bundle contains a forbidden entry")]
    Entry,
    #[error("source bundle exceeds its size bounds")]
    Size,
    #[error("source bundle digest does not match")]
    Digest,
    #[error("source bundle could not be materialized")]
    Io(#[from] std::io::Error),
}

#[derive(Debug)]
pub struct MaterializedSource {
    pub sha256: String,
    pub total_bytes: u64,
    pub files: BTreeMap<String, Vec<u8>>,
}

pub fn materialize_source_bundle(
    payload: &[u8],
    expected_sha256: &str,
    root: &Path,
) -> Result<MaterializedSource, BuildSourceError> {
    if payload.is_empty() || payload.len() > MAX_ARCHIVE_BYTES {
        return Err(BuildSourceError::Size);
    }
    if expected_sha256.len() != 64
        || !expected_sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(BuildSourceError::Digest);
    }
    fs::create_dir_all(root)?;
    let mut archive = tar::Archive::new(Cursor::new(payload));
    let entries = archive.entries().map_err(|_| BuildSourceError::Archive)?;
    let mut seen = BTreeSet::new();
    let mut files = BTreeMap::new();
    let mut manifest = Vec::new();
    let mut total = 0_u64;
    for entry in entries {
        let mut entry = entry.map_err(|_| BuildSourceError::Archive)?;
        let path = entry.path().map_err(|_| BuildSourceError::Path)?;
        let normalized = safe_path(&path)?;
        if !seen.insert(normalized.clone()) {
            return Err(BuildSourceError::Path);
        }
        if entry.header().entry_type().is_dir() {
            fs::create_dir_all(root.join(&normalized))?;
            continue;
        }
        if !entry.header().entry_type().is_file() || files.len() >= MAX_FILES {
            return Err(BuildSourceError::Entry);
        }
        let size = entry.size();
        total = total.checked_add(size).ok_or(BuildSourceError::Size)?;
        if size > MAX_FILE_BYTES || total > MAX_TOTAL_BYTES {
            return Err(BuildSourceError::Size);
        }
        let mut content = Vec::with_capacity(size as usize);
        entry
            .by_ref()
            .take(MAX_FILE_BYTES + 1)
            .read_to_end(&mut content)?;
        if content.len() as u64 != size {
            return Err(BuildSourceError::Size);
        }
        let raw_mode = entry
            .header()
            .mode()
            .map_err(|_| BuildSourceError::Archive)?;
        let mode = if raw_mode & 0o111 == 0 { 0o644 } else { 0o755 };
        manifest.push(SourceBundleFile {
            mode: i64::from(mode)
                .try_into()
                .map_err(|_| BuildSourceError::Entry)?,
            path: normalized.clone(),
            sha256: hex_sha256(&content),
            size: size.try_into().map_err(|_| BuildSourceError::Size)?,
        });
        files.insert(normalized, content);
    }
    if files.is_empty() {
        return Err(BuildSourceError::Entry);
    }
    manifest.sort_by(|left, right| left.path.as_bytes().cmp(right.path.as_bytes()));
    let manifest = SourceBundleDigestManifest {
        files: manifest,
        schema_version: 1,
        total_bytes: total.try_into().map_err(|_| BuildSourceError::Size)?,
    };
    let canonical = canonical_json(&manifest).map_err(|_| BuildSourceError::Archive)?;
    let sha256 = hex_sha256(&canonical);
    if sha256 != expected_sha256 {
        return Err(BuildSourceError::Digest);
    }
    for item in &manifest.files {
        let destination = root.join(&item.path);
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&destination)?;
        output.write_all(&files[&item.path])?;
        output.sync_all()?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(
                &destination,
                fs::Permissions::from_mode(
                    (*item.mode)
                        .try_into()
                        .map_err(|_| BuildSourceError::Entry)?,
                ),
            )?;
        }
    }
    Ok(MaterializedSource {
        sha256,
        total_bytes: total,
        files,
    })
}

fn safe_path(path: &Path) -> Result<String, BuildSourceError> {
    if path.as_os_str().is_empty() || path.is_absolute() {
        return Err(BuildSourceError::Path);
    }
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(value) if !value.is_empty() => normalized.push(value),
            _ => return Err(BuildSourceError::Path),
        }
    }
    let value = normalized.to_str().ok_or(BuildSourceError::Path)?;
    if value.is_empty() || value.len() > 512 || value.contains('\0') {
        return Err(BuildSourceError::Path);
    }
    Ok(value.replace(std::path::MAIN_SEPARATOR, "/"))
}
