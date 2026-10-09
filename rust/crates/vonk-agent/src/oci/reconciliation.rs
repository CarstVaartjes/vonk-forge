//! Reconciliation for the oci boundary.

use super::*;

impl<R: ProcessRunner> OciRuntime<'_, R> {
    /// Write or resume a durable cleanup checkpoint for the exact installed
    /// contract. A receipt is written before the installation directory is
    /// moved or removed, and the checkpoint lives outside that directory.
    pub fn prepare_reconciliation(
        &self,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<InstallationReconciliationProgress, OciError> {
        identity.validate().map_err(|_| OciError::Artifact)?;
        let installation_id = identity.installation_id.to_string();
        let lock = self.lock_installation_reconciliation(&installation_id)?;
        let result = (|| {
            let root = self.ensure_installation_reconciliation_root()?;
            let checkpoint_path = reconciliation_checkpoint_path(&root, &installation_id)?;
            let quarantine = reconciliation_quarantine_path(&root, &installation_id)?;
            let installation = managed_path(self.data_root, "installations", &installation_id)?;

            let installed = read_reconciliation_directory_identity(&installation)?;
            let quarantined = read_reconciliation_directory_identity(&quarantine)?;
            if installed.is_some() && quarantined.is_some() {
                // Preserve historical bytes without granting them authority over
                // the current installation. Finalization still checks its inode.
                fs::rename(
                    &quarantine,
                    root.join(format!("{}.retained", uuid::Uuid::new_v4())),
                )?;
                File::open(&root)?.sync_all()?;
            }
            // Current managed effects reconstruct the disposable checkpoint.
            // Never let damaged bytes or obsolete inode provenance decide
            // whether a current authorized reconciliation may run.
            let (state, directory_identity) = match (
                read_reconciliation_directory_identity(&installation)?,
                read_reconciliation_directory_identity(&quarantine)?,
            ) {
                (Some(found), None) => (InstallationReconciliationState::Prepared, found),
                (None, Some(found)) => (InstallationReconciliationState::Removing, found),
                (None, None) => (InstallationReconciliationState::Complete, (0, 0)),
                (Some(_), Some(_)) => return Err(OciError::Artifact),
            };
            let checkpoint = InstallationReconciliationCheckpoint {
                schema_version: INSTALLATION_RECONCILIATION_SCHEMA_VERSION,
                state,
                identity: identity.clone(),
                installation_device: directory_identity.0,
                installation_inode: directory_identity.1,
            };
            write_reconciliation_checkpoint(&root, &checkpoint_path, &checkpoint)?;
            Ok(InstallationReconciliationProgress {
                complete: state == InstallationReconciliationState::Complete,
            })
        })();
        drop(lock);
        result
    }

    /// Finish the exact identity-bound removal. Rename first moves the
    /// installation into a private quarantine path, so a crash during
    /// recursive removal can resume without reopening deleted plan metadata.
    pub fn finalize_reconciliation(
        &self,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<InstallationReconciliationProgress, OciError> {
        identity.validate().map_err(|_| OciError::Artifact)?;
        let installation_id = identity.installation_id.to_string();
        let lock = self.lock_installation_reconciliation(&installation_id)?;
        let result = (|| {
            let root = self.ensure_installation_reconciliation_root()?;
            let checkpoint_path = reconciliation_checkpoint_path(&root, &installation_id)?;
            let quarantine = reconciliation_quarantine_path(&root, &installation_id)?;
            let installation = managed_path(self.data_root, "installations", &installation_id)?;
            let mut checkpoint =
                read_reconciliation_checkpoint(&checkpoint_path)?.ok_or(OciError::Artifact)?;
            if checkpoint.schema_version != INSTALLATION_RECONCILIATION_SCHEMA_VERSION
                || checkpoint.identity != *identity
            {
                return Err(OciError::Artifact);
            }
            if checkpoint.state == InstallationReconciliationState::Complete {
                if path_exists_without_following(&installation)?
                    || path_exists_without_following(&quarantine)?
                {
                    return Err(OciError::Artifact);
                }
                return Ok(InstallationReconciliationProgress { complete: true });
            }

            match checkpoint.state {
                InstallationReconciliationState::Prepared => {
                    match (
                        read_reconciliation_directory_identity(&installation)?,
                        read_reconciliation_directory_identity(&quarantine)?,
                    ) {
                        (Some(found), None) => {
                            if found
                                != (
                                    checkpoint.installation_device,
                                    checkpoint.installation_inode,
                                )
                            {
                                return Err(OciError::Artifact);
                            }
                            fs::rename(&installation, &quarantine)?;
                            File::open(self.data_root.join("installations"))?.sync_all()?;
                            File::open(&root)?.sync_all()?;
                        }
                        (None, Some(found)) => {
                            if found
                                != (
                                    checkpoint.installation_device,
                                    checkpoint.installation_inode,
                                )
                            {
                                return Err(OciError::Artifact);
                            }
                        }
                        _ => return Err(OciError::Artifact),
                    }
                    checkpoint.state = InstallationReconciliationState::Removing;
                    write_reconciliation_checkpoint(&root, &checkpoint_path, &checkpoint)?;
                }
                InstallationReconciliationState::Removing => match (
                    read_reconciliation_directory_identity(&installation)?,
                    read_reconciliation_directory_identity(&quarantine)?,
                ) {
                    (None, None) => {}
                    (None, Some(found))
                        if found
                            == (
                                checkpoint.installation_device,
                                checkpoint.installation_inode,
                            ) => {}
                    _ => return Err(OciError::Artifact),
                },
                InstallationReconciliationState::Complete => unreachable!("handled above"),
            }

            if path_exists_without_following(&quarantine)? {
                fs::remove_dir_all(&quarantine)?;
                File::open(&root)?.sync_all()?;
            }
            if path_exists_without_following(&installation)?
                || path_exists_without_following(&quarantine)?
            {
                return Err(OciError::Artifact);
            }
            checkpoint.state = InstallationReconciliationState::Complete;
            write_reconciliation_checkpoint(&root, &checkpoint_path, &checkpoint)?;
            Ok(InstallationReconciliationProgress { complete: true })
        })();
        drop(lock);
        result
    }

    pub(super) fn ensure_installation_reconciliation_root(&self) -> Result<PathBuf, OciError> {
        let root = self.data_root.join(INSTALLATION_RECONCILIATION_ROOT);
        match fs::symlink_metadata(&root) {
            Ok(metadata) if trusted_private_directory(&metadata) => {}
            Ok(_) => return Err(OciError::Artifact),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                fs::create_dir(&root)?;
                fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
                File::open(self.data_root)?.sync_all()?;
            }
            Err(error) => return Err(error.into()),
        }
        let metadata = fs::symlink_metadata(&root)?;
        if !trusted_private_directory(&metadata) {
            return Err(OciError::Artifact);
        }
        Ok(root)
    }

    pub(super) fn lock_installation_reconciliation(
        &self,
        installation_id: &str,
    ) -> Result<File, OciError> {
        if !canonical_uuid(installation_id) {
            return Err(OciError::Artifact);
        }
        let root = self.data_root.join("installation-ownership");
        match fs::symlink_metadata(&root) {
            Ok(metadata) if trusted_private_directory(&metadata) => {}
            Ok(_) => return Err(OciError::Artifact),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                fs::create_dir(&root)?;
                fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
            }
            Err(error) => return Err(error.into()),
        }
        let path = root.join(format!("{installation_id}.lock"));
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .mode(0o600)
            .open(path)?;
        if !trusted_receipt_metadata(&file.metadata()?) {
            return Err(OciError::Artifact);
        }
        // flock(2) locks belong to the open file description, and a concurrent
        // fork (another thread spawning a process) briefly shares it until exec
        // closes it, so a just-released lock can still look held for a few
        // milliseconds. Wait a short bounded grace period before reporting
        // busy; a genuinely held lock still reports the retryable busy error.
        let deadline = std::time::Instant::now() + RECONCILIATION_LOCK_GRACE;
        loop {
            match rustix::fs::flock(&file, rustix::fs::FlockOperation::NonBlockingLockExclusive) {
                Ok(()) => return Ok(file),
                Err(error)
                    if error == rustix::io::Errno::AGAIN
                        || error == rustix::io::Errno::WOULDBLOCK =>
                {
                    if std::time::Instant::now() >= deadline {
                        return Err(OciError::ReconciliationBusy);
                    }
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(error) => return Err(OciError::Io(error.into())),
            }
        }
    }

    pub(super) fn supersede_reconciliation_checkpoint(
        &self,
        installation_id: &str,
    ) -> Result<(), OciError> {
        // Admission owns the current signed plan. Cleanup history has no
        // authority over a fresh install; quarantine bytes remain untouched.
        let root = self.data_root.join(INSTALLATION_RECONCILIATION_ROOT);
        let path = reconciliation_checkpoint_path(&root, installation_id)?;
        // The per-installation lock fences an active cleanup. Historical
        // checkpoints do not hold authority over a new preparation request.
        let retired = root.join(format!("{}.retired", uuid::Uuid::new_v4()));
        // Retirement is bookkeeping: failure cannot veto current admission.
        // Preserve non-file records without following them when possible.
        if fs::rename(path, retired).is_ok() {
            let _ = File::open(root).and_then(|directory| directory.sync_all());
        }
        Ok(())
    }
}

pub(super) fn trusted_private_directory(metadata: &fs::Metadata) -> bool {
    metadata.file_type().is_dir()
        && !metadata.file_type().is_symlink()
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && metadata.mode() & 0o777 == 0o700
}

pub(super) fn trusted_installation_directory(metadata: &fs::Metadata) -> bool {
    trusted_private_directory(metadata)
}

pub(super) fn reconciliation_checkpoint_path(
    root: &Path,
    installation_id: &str,
) -> Result<PathBuf, OciError> {
    if !canonical_uuid(installation_id) {
        return Err(OciError::Artifact);
    }
    Ok(root.join(format!("{installation_id}.json")))
}

pub(super) fn reconciliation_quarantine_path(
    root: &Path,
    installation_id: &str,
) -> Result<PathBuf, OciError> {
    if !canonical_uuid(installation_id) {
        return Err(OciError::Artifact);
    }
    Ok(root.join(format!("{installation_id}.partial")))
}

pub(super) fn path_exists_without_following(path: &Path) -> Result<bool, OciError> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
    }
}

pub(super) fn read_reconciliation_directory_identity(
    path: &Path,
) -> Result<Option<(u64, u64)>, OciError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if trusted_installation_directory(&metadata) => {
            Ok(Some((metadata.dev(), metadata.ino())))
        }
        Ok(_) => Err(OciError::Artifact),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

pub(super) fn read_reconciliation_checkpoint(
    path: &Path,
) -> Result<Option<InstallationReconciliationCheckpoint>, OciError> {
    match fs::symlink_metadata(path) {
        Ok(metadata)
            if trusted_receipt_metadata(&metadata)
                && metadata.len() <= MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES => {}
        Ok(_) => return Ok(None),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    }
    let file = match OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32)
        .open(path)
    {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    let metadata = file.metadata()?;
    if !trusted_receipt_metadata(&metadata)
        || metadata.len() > MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES
    {
        return Ok(None);
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take(MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES {
        return Ok(None);
    }
    Ok(serde_json::from_slice(&bytes).ok())
}

pub(super) fn write_reconciliation_checkpoint(
    root: &Path,
    destination: &Path,
    checkpoint: &InstallationReconciliationCheckpoint,
) -> Result<(), OciError> {
    let bytes = canonical_protocol_json(checkpoint).map_err(|_| OciError::Artifact)?;
    if bytes.len() as u64 > MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES {
        return Err(OciError::Artifact);
    }
    let temporary = root.join(format!(".checkpoint-{}.tmp", uuid::Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .mode(0o600)
        .open(&temporary)?;
    let result = (|| {
        file.write_all(&bytes)?;
        file.sync_all()?;
        if let Ok(metadata) = fs::symlink_metadata(destination)
            && !metadata.file_type().is_file()
        {
            // A malformed local record may be a directory or symlink. Preserve
            // it without following it and publish the freshly observed record.
            fs::rename(
                destination,
                root.join(format!("{}.retired", uuid::Uuid::new_v4())),
            )?;
        }
        fs::rename(&temporary, destination)?;
        File::open(root)?.sync_all()?;
        Ok(())
    })();
    drop(file);
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

#[cfg(test)]
mod tests;
