//! Exact OCI archive verification before the privileged runtime boundary.

use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
};

use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use uuid::Uuid;
use vonk_agent_protocol::RecipeImageImportRequest;

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
        self.verify(request, archive)?;
        let destination = self.cached_archive_path(&request.oci_layout_sha256)?;
        if destination.exists() {
            self.verify(request, &destination)?;
            return Ok(destination);
        }
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
        }
        let temporary = destination.with_extension(format!("{}.partial", std::process::id()));
        fs::copy(archive, &temporary)?;
        if let Err(error) = self.verify(request, &temporary) {
            let _ = fs::remove_file(&temporary);
            return Err(error);
        }
        fs::rename(&temporary, &destination)?;
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
        if fs::metadata(archive)?.len() != image_bytes
            || sha256_file(archive)? != archive_sha256
            || image_digest.len() != 71
            || !image_digest.starts_with("sha256:")
            || !image_digest[7..]
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(ImageImportError::Digest);
        }
        let destination = self.cached_archive_path(archive_sha256)?;
        if destination.exists() {
            if fs::metadata(&destination)?.len() != image_bytes
                || sha256_file(&destination)? != archive_sha256
            {
                return Err(ImageImportError::Digest);
            }
            return Ok(destination);
        }
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
        }
        let temporary = destination.with_extension(format!("{}.partial", std::process::id()));
        fs::copy(archive, &temporary)?;
        if fs::metadata(&temporary)?.len() != image_bytes
            || sha256_file(&temporary)? != archive_sha256
        {
            let _ = fs::remove_file(&temporary);
            return Err(ImageImportError::Digest);
        }
        fs::rename(&temporary, &destination)?;
        Ok(destination)
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
            "localhost/vonk/distributed-image".to_owned(),
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
        if fs::metadata(archive)?.len() != request.image_bytes
            || sha256_file(archive)? != request.oci_layout_sha256
        {
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
            format!("localhost/vonk/recipe-build-{}", request.build_id),
        ]
    }
}

fn sha256_file(path: &Path) -> Result<String, std::io::Error> {
    let mut file = fs::File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(hex::encode(digest.finalize()))
}
