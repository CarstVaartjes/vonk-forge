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

            if let Some(checkpoint) = read_reconciliation_checkpoint(&checkpoint_path)? {
                if checkpoint.schema_version != INSTALLATION_RECONCILIATION_SCHEMA_VERSION
                    || checkpoint.identity != *identity
                {
                    return Err(OciError::Artifact);
                }
                match checkpoint.state {
                    InstallationReconciliationState::Complete => {
                        if path_exists_without_following(&installation)?
                            || path_exists_without_following(&quarantine)?
                        {
                            return Err(OciError::Artifact);
                        }
                        return Ok(InstallationReconciliationProgress { complete: true });
                    }
                    InstallationReconciliationState::Prepared => {
                        let location = match (
                            read_reconciliation_directory_identity(&installation)?,
                            read_reconciliation_directory_identity(&quarantine)?,
                        ) {
                            (Some(identity), None) | (None, Some(identity)) => identity,
                            _ => return Err(OciError::Artifact),
                        };
                        if location
                            != (
                                checkpoint.installation_device,
                                checkpoint.installation_inode,
                            )
                        {
                            return Err(OciError::Artifact);
                        }
                        return Ok(InstallationReconciliationProgress { complete: false });
                    }
                    InstallationReconciliationState::Removing => {
                        match (
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
                        }
                        return Ok(InstallationReconciliationProgress { complete: false });
                    }
                }
            }

            if path_exists_without_following(&quarantine)? {
                return Err(OciError::Artifact);
            }
            let directory_metadata = fs::symlink_metadata(&installation)?;
            if !trusted_installation_directory(&directory_metadata) {
                return Err(OciError::Artifact);
            }
            let checkpoint = InstallationReconciliationCheckpoint {
                schema_version: INSTALLATION_RECONCILIATION_SCHEMA_VERSION,
                state: InstallationReconciliationState::Prepared,
                identity: identity.clone(),
                installation_device: directory_metadata.dev(),
                installation_inode: directory_metadata.ino(),
            };
            write_reconciliation_checkpoint(&root, &checkpoint_path, &checkpoint)?;
            Ok(InstallationReconciliationProgress { complete: false })
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
        let root = self.ensure_installation_reconciliation_root()?;
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

    pub(super) fn refuse_reconciled_installation(
        &self,
        installation_id: &str,
    ) -> Result<(), OciError> {
        let root = self.ensure_installation_reconciliation_root()?;
        let path = reconciliation_checkpoint_path(&root, installation_id)?;
        match fs::symlink_metadata(path) {
            Ok(_) => Err(OciError::Artifact),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
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
    let file = match OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
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
        return Err(OciError::Artifact);
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take(MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_INSTALLATION_RECONCILIATION_RECEIPT_BYTES {
        return Err(OciError::Artifact);
    }
    Ok(Some(serde_json::from_slice(&bytes)?))
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
    file.write_all(&bytes)?;
    file.sync_all()?;
    fs::rename(&temporary, destination)?;
    File::open(root)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests;
