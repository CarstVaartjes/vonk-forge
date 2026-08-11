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
    pub fn staging_path(&self, operation_id: Uuid) -> Result<PathBuf, ImageImportError> {
        let root = self
            .data_root
            .join("image-imports")
            .join(operation_id.to_string());
        fs::create_dir_all(&root)?;
        Ok(root.join("image.oci.tar"))
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
