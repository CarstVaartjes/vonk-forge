//! Exact OCI archive verification and rootless import.

use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    time::Duration,
};

use serde::Serialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use uuid::Uuid;
use vonk_agent_protocol::RecipeImageImportRequest;

use crate::process::{ProcessError, ProcessRunner, Program};

#[derive(Debug, Error)]
pub enum ImageImportError {
    #[error("OCI archive storage is invalid")]
    Io(#[from] std::io::Error),
    #[error("OCI archive digest or size does not match")]
    Digest,
    #[error("rootless OCI import failed")]
    Process(#[from] ProcessError),
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ImageImportEvidence {
    pub build_id: Uuid,
    pub image_bytes: u64,
    pub image_digest: String,
    pub oci_layout_sha256: String,
}

pub struct ImageImporter<'a, R> {
    pub runner: &'a R,
    pub data_root: &'a Path,
}

impl<R: ProcessRunner> ImageImporter<'_, R> {
    pub fn staging_path(&self, operation_id: Uuid) -> Result<PathBuf, ImageImportError> {
        let root = self
            .data_root
            .join("image-imports")
            .join(operation_id.to_string());
        fs::create_dir_all(&root)?;
        Ok(root.join("image.oci.tar"))
    }

    pub fn import(
        &self,
        request: &RecipeImageImportRequest,
        archive: &Path,
    ) -> Result<ImageImportEvidence, ImageImportError> {
        if fs::metadata(archive)?.len() != request.image_bytes
            || sha256_file(archive)? != request.oci_layout_sha256
        {
            return Err(ImageImportError::Digest);
        }
        let loaded = self.runner.run(
            Program::Podman,
            &[
                "load".to_owned(),
                "--input".to_owned(),
                archive.display().to_string(),
            ],
            Duration::from_secs(600),
        )?;
        if !loaded.success {
            return Err(ImageImportError::Digest);
        }
        let tag = format!("localhost/vonk/recipe-build-{}", request.build_id);
        let identifier = loaded_image_identifier(&loaded.stdout, &tag)?;
        let inspected = self.runner.run(
            Program::Podman,
            &[
                "image".to_owned(),
                "inspect".to_owned(),
                "--format".to_owned(),
                "{{.Digest}}\t{{.Os}}\t{{.Architecture}}\t{{index .Config.Labels \"ai.vonkforge.runtime-interface\"}}\t{{.Config.User}}".to_owned(),
                identifier.clone(),
            ],
            Duration::from_secs(60),
        )?;
        let fields = std::str::from_utf8(&inspected.stdout)
            .ok()
            .map(str::trim)
            .map(|value| value.split('\t').collect::<Vec<_>>())
            .unwrap_or_default();
        if !inspected.success
            || fields.len() != 5
            || fields[0] != request.image_digest
            || fields[1] != "linux"
            || fields[2] != "arm64"
            || fields[3] != "v1"
            || !numeric_non_root_user(fields[4])
        {
            return Err(ImageImportError::Digest);
        }
        let tagged = self.runner.run(
            Program::Podman,
            &["tag".to_owned(), identifier, tag],
            Duration::from_secs(60),
        )?;
        if !tagged.success {
            return Err(ImageImportError::Digest);
        }
        Ok(ImageImportEvidence {
            build_id: request.build_id,
            image_bytes: request.image_bytes,
            image_digest: request.image_digest.clone(),
            oci_layout_sha256: request.oci_layout_sha256.clone(),
        })
    }
}

fn loaded_image_identifier(payload: &[u8], expected_tag: &str) -> Result<String, ImageImportError> {
    let text = std::str::from_utf8(payload).map_err(|_| ImageImportError::Digest)?;
    let identifiers = text
        .lines()
        .filter_map(|line| line.strip_prefix("Loaded image: "))
        .collect::<Vec<_>>();
    let [identifier] = identifiers.as_slice() else {
        return Err(ImageImportError::Digest);
    };
    if *identifier != expected_tag
        && identifier.strip_prefix("sha256:").is_none_or(|digest| {
            digest.len() != 64
                || !digest
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        })
    {
        return Err(ImageImportError::Digest);
    }
    Ok((*identifier).to_owned())
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

fn numeric_non_root_user(value: &str) -> bool {
    let mut parts = value.split(':');
    let valid = |part: &str| {
        !part.is_empty() && !part.starts_with('0') && part.bytes().all(|byte| byte.is_ascii_digit())
    };
    valid(parts.next().unwrap_or_default())
        && parts.next().is_none_or(valid)
        && parts.next().is_none()
}
