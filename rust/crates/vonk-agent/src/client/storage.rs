//! Storage for the client boundary.

use super::*;

pub(super) fn same_file_metadata(before: &fs::Metadata, after: &fs::Metadata) -> bool {
    same_file_content_identity(before, after)
        && before.ctime() == after.ctime()
        && before.ctime_nsec() == after.ctime_nsec()
}

pub(super) fn same_file_content_identity(before: &fs::Metadata, after: &fs::Metadata) -> bool {
    before.dev() == after.dev()
        && before.ino() == after.ino()
        && before.len() == after.len()
        && before.mtime() == after.mtime()
        && before.mtime_nsec() == after.mtime_nsec()
}

pub(super) async fn ensure_private_parent(
    parent: &Path,
    managed_root: &Path,
) -> Result<(), ClientError> {
    let metadata = tokio::fs::symlink_metadata(parent)
        .await
        .map_err(|_| ClientError::Retryable)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.mode() & 0o022 != 0
    {
        return Err(ClientError::Retryable);
    }
    if !parent.starts_with(managed_root) {
        return Err(ClientError::Retryable);
    }
    let relative = parent
        .strip_prefix(managed_root)
        .map_err(|_| ClientError::Retryable)?;
    let mut component = managed_root.to_path_buf();
    for part in relative.components() {
        component.push(part.as_os_str());
        let metadata = tokio::fs::symlink_metadata(&component)
            .await
            .map_err(|_| ClientError::Retryable)?;
        if metadata.file_type().is_symlink() {
            return Err(ClientError::Retryable);
        }
    }
    let canonical_root = tokio::fs::canonicalize(managed_root)
        .await
        .map_err(|_| ClientError::Retryable)?;
    let canonical_parent = tokio::fs::canonicalize(parent)
        .await
        .map_err(|_| ClientError::Retryable)?;
    if !canonical_parent.starts_with(canonical_root) {
        return Err(ClientError::Retryable);
    }
    Ok(())
}

pub(super) fn partial_path(path: &Path) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(".partial");
    PathBuf::from(value)
}

pub(super) fn validate_trusted_metadata(metadata: &fs::Metadata, expected_bytes: u64) -> bool {
    metadata.file_type().is_file()
        && !metadata.file_type().is_symlink()
        && metadata.nlink() == 1
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && metadata.mode() & 0o777 == 0o600
        && metadata.len() == expected_bytes
}

/// A completed distribution object is the one trusted copy of a model file
/// that installations link, so it may carry more than one link and, once a
/// workload has started from one of those links, the exact read access the
/// runtime user was granted. Anything else about it is as strict as a partial.
pub(super) fn validate_trusted_final_metadata(
    metadata: &fs::Metadata,
    expected_bytes: u64,
) -> bool {
    metadata.file_type().is_file()
        && !metadata.file_type().is_symlink()
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && matches!(metadata.mode() & 0o777, 0o600 | 0o640)
        && metadata.len() == expected_bytes
}

pub(super) async fn inspect_trusted_final(
    path: &Path,
    expected_bytes: u64,
) -> Result<Option<tokio::fs::File>, ClientError> {
    let path_metadata = match tokio::fs::symlink_metadata(path).await {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(ClientError::Retryable),
    };
    if !validate_trusted_final_metadata(&path_metadata, expected_bytes) {
        // Reject this managed entry, not the authorized transfer. Renaming
        // preserves directories, hard links and symlink targets without
        // following them, while freeing the content-addressed name.
        isolate_managed_entry(path).await?;
        return Ok(None);
    }
    let file = tokio::fs::OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32)
        .open(path)
        .await
        .map_err(|_| ClientError::Retryable)?;
    let opened_metadata = file.metadata().await.map_err(|_| ClientError::Retryable)?;
    if opened_metadata.mode() & 0o777 == 0o640 && !crate::oci::exact_runtime_file_acl(&file) {
        isolate_managed_entry(path).await?;
        return Ok(None);
    }
    if !validate_trusted_final_metadata(&opened_metadata, expected_bytes)
        || opened_metadata.dev() != path_metadata.dev()
        || opened_metadata.ino() != path_metadata.ino()
    {
        return Err(ClientError::Retryable);
    }
    Ok(Some(file))
}

pub(super) async fn open_trusted_partial(path: &Path) -> Result<tokio::fs::File, ClientError> {
    if let Ok(metadata) = tokio::fs::symlink_metadata(path).await
        && !validate_trusted_metadata(&metadata, metadata.len())
    {
        isolate_managed_entry(path).await?;
    }
    let file = tokio::fs::OpenOptions::new()
        .create(true)
        .read(true)
        .append(true)
        .write(true)
        .mode(0o600)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32)
        .open(path)
        .await
        .map_err(|_| ClientError::Retryable)?;
    let metadata = file.metadata().await.map_err(|_| ClientError::Retryable)?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.nlink() != 1
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.mode() & 0o777 != 0o600
    {
        return Err(ClientError::Retryable);
    }
    Ok(file)
}

pub(super) async fn sync_parent(parent: &Path) -> Result<(), ClientError> {
    tokio::fs::File::open(parent)
        .await?
        .sync_all()
        .await
        .map_err(|_| ClientError::Retryable)?;
    Ok(())
}

pub(super) fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

pub(super) fn valid_oci_digest(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(valid_sha256)
}

async fn isolate_managed_entry(path: &Path) -> Result<(), ClientError> {
    let isolated = path.with_extension(format!("{}.damaged", uuid::Uuid::new_v4()));
    tokio::fs::rename(path, isolated)
        .await
        .map_err(|_| ClientError::Retryable)?;
    sync_parent(path.parent().ok_or(ClientError::Retryable)?).await
}
