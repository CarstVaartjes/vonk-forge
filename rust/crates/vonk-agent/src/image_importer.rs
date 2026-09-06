//! Exact OCI archive verification before the privileged runtime boundary.

use std::{
    fs,
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};

use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use uuid::Uuid;
use vonk_agent_protocol::RecipeImageImportRequest;

use crate::workloads::CompiledExecutionPlan;

#[derive(Debug, Error)]
pub enum ImageImportError {
    #[error("OCI archive storage is invalid")]
    Io(#[from] std::io::Error),
    #[error("OCI archive digest or size does not match")]
    Digest,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ImageImportEvidence {
    pub build_id: Uuid,
    pub image_bytes: u64,
    pub image_digest: String,
    pub oci_layout_sha256: String,
}

pub struct ImageImporter<'a> {
    pub data_root: &'a Path,
}

impl ImageImporter<'_> {
    /// Return a verified shared archive cache entry when one exists. The
    /// content address is the archive digest, so two assignments can reuse
    /// one download without sharing mutable staging state.
    pub fn cached_archive_path(&self, archive_sha256: &str) -> Result<PathBuf, ImageImportError> {
        if archive_sha256.len() != 64
            || !archive_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(ImageImportError::Digest);
        }
        Ok(self.data_root.join("oci-archives").join(archive_sha256))
    }

    pub fn verified_cached_archive(
        &self,
        request: &RecipeImageImportRequest,
    ) -> Result<Option<PathBuf>, ImageImportError> {
        let path = self.cached_archive_path(&request.oci_layout_sha256)?;
        if !path.exists() {
            return Ok(None);
        }
        match self.verify(request, &path) {
            Ok(_) => Ok(Some(path)),
            Err(ImageImportError::Digest) => Ok(None),
            Err(error) => Err(error),
        }
    }

    pub fn retain_verified_archive(
        &self,
        request: &RecipeImageImportRequest,
        archive: &Path,
    ) -> Result<PathBuf, ImageImportError> {
        validate_archive_metadata(archive, request.image_bytes, false)?;
        if !path_within_root(archive, self.data_root)? {
            return Err(ImageImportError::Digest);
        }
        let destination = self.cached_archive_path(&request.oci_layout_sha256)?;
        if destination.exists() {
            self.verify(request, &destination)?;
            return Ok(destination);
        }
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
            ensure_private_directory(parent, self.data_root)?;
        }
        copy_archive_atomic(
            archive,
            &destination,
            request.image_bytes,
            Some(&request.oci_layout_sha256),
        )?;
        validate_archive_metadata(&destination, request.image_bytes, true)?;
        Ok(destination)
    }

    /// Retain a Controller-distributed archive using its assignment identity.
    /// This path is independent of recipe build IDs because the Controller
    /// plan, archive digest, and image digest are the authority for delivery.
    pub fn retain_verified_distribution_archive(
        &self,
        archive_sha256: &str,
        image_digest: &str,
        image_bytes: u64,
        archive: &Path,
    ) -> Result<PathBuf, ImageImportError> {
        // Controller distribution is already assignment-bound and mTLS
        // authenticated. Keep the digest as the immutable cache ID while
        // relying on the transfer's private, single-link metadata here.
        if !valid_sha256(archive_sha256)
            || image_digest.len() != 71
            || !image_digest.starts_with("sha256:")
            || !image_digest[7..]
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(ImageImportError::Digest);
        }
        let destination = self.cached_archive_path(archive_sha256)?;
        let parent = destination.parent().ok_or(ImageImportError::Digest)?;
        fs::create_dir_all(parent)?;
        ensure_private_directory(parent, self.data_root)?;
        if let Ok(metadata) = fs::symlink_metadata(&destination) {
            if !validate_archive_metadata_value(&metadata, image_bytes, true) {
                return Err(ImageImportError::Digest);
            }
            return Ok(destination);
        }
        if !matches!(fs::symlink_metadata(&destination), Err(error) if error.kind() == std::io::ErrorKind::NotFound)
        {
            return Err(ImageImportError::Digest);
        }
        validate_archive_metadata(archive, image_bytes, true)?;
        if !path_within_root(archive, self.data_root)? {
            return Err(ImageImportError::Digest);
        }
        if archive == destination {
            return Ok(destination);
        }
        fs::set_permissions(archive, fs::Permissions::from_mode(0o600))?;
        match fs::rename(archive, &destination) {
            Ok(()) => {
                sync_directory(archive.parent().ok_or(ImageImportError::Digest)?)?;
                sync_directory(destination.parent().ok_or(ImageImportError::Digest)?)?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::CrossesDevices => {
                copy_archive_atomic(archive, &destination, image_bytes, None)?;
            }
            Err(error) => return Err(error.into()),
        }
        validate_archive_metadata(&destination, image_bytes, true)?;
        Ok(destination)
    }

    /// Retain the exact OCI archive authorized by a compiled execution plan.
    /// The plan's archive receipt is the only source for image bytes and
    /// layout identity at this boundary.
    pub fn retain_compiled_runtime_image(
        &self,
        plan: &CompiledExecutionPlan,
        archive: &Path,
    ) -> Result<PathBuf, ImageImportError> {
        plan.validate().map_err(|_| ImageImportError::Digest)?;
        let image = &plan.runtime_image;
        self.retain_verified_distribution_archive(
            &image.oci_layout_sha256,
            &image.image_digest,
            image.image_bytes,
            archive,
        )
    }

    pub fn distribution_runtime_arguments(
        &self,
        archive_sha256: &str,
        image_digest: &str,
        image_bytes: u64,
        archive: &Path,
    ) -> Vec<String> {
        vec![
            archive.display().to_string(),
            archive_sha256.to_owned(),
            image_bytes.to_string(),
            image_digest.to_owned(),
            image_digest.to_owned(),
            format!("localhost/vonk/compiled-runtime-{archive_sha256}@{image_digest}"),
        ]
    }

    pub fn staging_path(&self, operation_id: Uuid) -> Result<PathBuf, ImageImportError> {
        let root = self
            .data_root
            .join("image-imports")
            .join(operation_id.to_string());
        fs::create_dir_all(&root)?;
        Ok(root.join("image.docker.tar"))
    }

    pub fn verify(
        &self,
        request: &RecipeImageImportRequest,
        archive: &Path,
    ) -> Result<ImageImportEvidence, ImageImportError> {
        validate_archive_metadata(archive, request.image_bytes, false)?;
        if !path_within_root(archive, self.data_root)? {
            return Err(ImageImportError::Digest);
        }
        if sha256_file(archive)? != request.oci_layout_sha256 {
            return Err(ImageImportError::Digest);
        }
        Ok(ImageImportEvidence {
            build_id: request.build_id,
            image_bytes: request.image_bytes,
            image_digest: request.image_digest.clone(),
            oci_layout_sha256: request.oci_layout_sha256.clone(),
        })
    }

    pub fn runtime_arguments(
        &self,
        request: &RecipeImageImportRequest,
        archive: &Path,
    ) -> Vec<String> {
        vec![
            archive.display().to_string(),
            request.oci_layout_sha256.clone(),
            request.image_bytes.to_string(),
            request.image_digest.clone(),
            request.image_digest.clone(),
            format!(
                "localhost/vonk/recipe-build-{}@{}",
                request.build_id, request.image_digest
            ),
        ]
    }
}

fn sha256_file(path: &Path) -> Result<String, std::io::Error> {
    let mut file = fs::OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32)
        .open(path)?;
    let before = file.metadata()?;
    if !before.is_file() || before.nlink() != 1 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "OCI archive is not a regular single-link file",
        ));
    }
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    let after = file.metadata()?;
    if before.dev() != after.dev() || before.ino() != after.ino() || before.len() != after.len() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "OCI archive changed while hashing",
        ));
    }
    Ok(hex::encode(digest.finalize()))
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn validate_archive_metadata(
    path: &Path,
    expected_bytes: u64,
    require_private: bool,
) -> Result<(), ImageImportError> {
    let metadata = fs::symlink_metadata(path)?;
    if !validate_archive_metadata_value(&metadata, expected_bytes, require_private) {
        return Err(ImageImportError::Digest);
    }
    Ok(())
}

fn validate_archive_metadata_value(
    metadata: &fs::Metadata,
    expected_bytes: u64,
    require_private: bool,
) -> bool {
    metadata.is_file()
        && !metadata.file_type().is_symlink()
        && metadata.nlink() == 1
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && metadata.len() == expected_bytes
        && (!require_private || metadata.mode() & 0o777 == 0o600)
}

fn ensure_private_directory(path: &Path, managed_root: &Path) -> Result<(), ImageImportError> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.mode() & 0o022 != 0
        || !path_within_root(path, managed_root)?
    {
        return Err(ImageImportError::Digest);
    }
    Ok(())
}

fn path_within_root(path: &Path, managed_root: &Path) -> Result<bool, ImageImportError> {
    if !path.starts_with(managed_root) {
        return Ok(false);
    }
    let relative = path
        .strip_prefix(managed_root)
        .map_err(|_| ImageImportError::Digest)?;
    let mut component = managed_root.to_path_buf();
    for part in relative.components() {
        component.push(part.as_os_str());
        if fs::symlink_metadata(&component)?.file_type().is_symlink() {
            return Ok(false);
        }
    }
    let root = managed_root.canonicalize()?;
    let candidate = path.canonicalize()?;
    Ok(candidate.starts_with(root))
}

fn copy_archive_atomic(
    source: &Path,
    destination: &Path,
    expected_bytes: u64,
    expected_sha256: Option<&str>,
) -> Result<(), ImageImportError> {
    let source_metadata = fs::symlink_metadata(source)?;
    if !validate_archive_metadata_value(&source_metadata, expected_bytes, false) {
        return Err(ImageImportError::Digest);
    }
    let mut input = fs::OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32)
        .open(source)?;
    let opened_metadata = input.metadata()?;
    if opened_metadata.dev() != source_metadata.dev()
        || opened_metadata.ino() != source_metadata.ino()
    {
        return Err(ImageImportError::Digest);
    }
    let temporary =
        destination.with_extension(format!("partial.{}.{}", std::process::id(), Uuid::new_v4()));
    let result: Result<(), ImageImportError> = (|| {
        let mut output = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32,
            )
            .open(&temporary)?;
        let mut digest = expected_sha256.map(|_| Sha256::new());
        let mut copied = 0_u64;
        let mut buffer = [0_u8; 1024 * 1024];
        loop {
            let read = input.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            output.write_all(&buffer[..read])?;
            if let Some(digest) = digest.as_mut() {
                digest.update(&buffer[..read]);
            }
            copied = copied
                .checked_add(read as u64)
                .ok_or(ImageImportError::Digest)?;
        }
        let observed_digest = digest.map(|digest| hex::encode(digest.finalize()));
        if copied != expected_bytes
            || observed_digest.as_deref() != expected_sha256
            || !validate_archive_metadata_value(&output.metadata()?, expected_bytes, true)
            || input.metadata()?.ino() != source_metadata.ino()
            || input.metadata()?.len() != expected_bytes
        {
            return Err(ImageImportError::Digest);
        }
        output.flush()?;
        output.sync_all()?;
        drop(output);
        fs::rename(&temporary, destination)?;
        sync_directory(destination.parent().ok_or(ImageImportError::Digest)?)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn sync_directory(path: &Path) -> Result<(), ImageImportError> {
    fs::File::open(path)?.sync_all()?;
    Ok(())
}
