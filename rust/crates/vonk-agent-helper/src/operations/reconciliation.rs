//! Reconciliation.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_installation_cleanup(
        &self,
        installation_id: &str,
    ) -> Result<(), OperationError> {
        self.runtime_installation_cleanup_bound(installation_id, None)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_installation_cleanup_bound(
        &self,
        installation_id: &str,
        expected_installation: Option<(u64, u64)>,
    ) -> Result<(), OperationError> {
        if !valid_artifact_id(installation_id) {
            return Err(OperationError::InvalidOperation);
        }
        let directory_flags = rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC;
        let agent_data = OpenOptions::new()
            .read(true)
            .custom_flags(
                (rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::NOFOLLOW).bits() as i32,
            )
            .open(&self.roots.agent_data)?;
        let installations = match rustix::fs::openat(
            &agent_data,
            "installations",
            directory_flags,
            rustix::fs::Mode::empty(),
        ) {
            Ok(installations) => installations,
            Err(rustix::io::Errno::NOENT) if expected_installation.is_none() => return Ok(()),
            Err(rustix::io::Errno::NOENT) => return Err(OperationError::InvalidArtifact),
            Err(error) => return Err(errno_io(error).into()),
        };
        let installation = match rustix::fs::openat(
            &installations,
            installation_id,
            directory_flags,
            rustix::fs::Mode::empty(),
        ) {
            Ok(installation) => installation,
            Err(rustix::io::Errno::NOENT) if expected_installation.is_none() => return Ok(()),
            Err(rustix::io::Errno::NOENT) => return Err(OperationError::InvalidArtifact),
            Err(error) => return Err(errno_io(error).into()),
        };
        if let Some((device, inode)) = expected_installation {
            let metadata = rustix::fs::fstat(&installation).map_err(errno_io)?;
            if (metadata.st_dev, metadata.st_ino) != (device, inode) {
                return Err(OperationError::InvalidArtifact);
            }
        }
        let cache = match rustix::fs::openat(
            &installation,
            "runtime-cache",
            directory_flags,
            rustix::fs::Mode::empty(),
        ) {
            Ok(cache) => cache,
            Err(rustix::io::Errno::NOENT) => return Ok(()),
            Err(error) => return Err(errno_io(error).into()),
        };
        let cache_device = rustix::fs::fstat(&cache).map_err(errno_io)?.st_dev;
        remove_directory_contents(&cache, cache_device)?;
        rustix::fs::unlinkat(
            &installation,
            "runtime-cache",
            rustix::fs::AtFlags::REMOVEDIR,
        )
        .map_err(errno_io)?;
        Ok(())
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_reconcile_installation(
        &self,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<(), OperationError> {
        self.runtime_reconcile_installation_inner(identity)
            .map_err(|error| match error {
                OperationError::Io(_) => {
                    OperationError::InstallationReconciliationStorageUnavailable
                }
                error => error,
            })
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_reconcile_installation_inner(
        &self,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<(), OperationError> {
        identity
            .validate()
            .map_err(|_| OperationError::InvalidOperation)?;
        let installation_id = identity.installation_id.to_string();
        let _lock = self.lock_installation_runtime(&installation_id)?;
        self.runtime_reconcile_installation_locked(identity)
    }

    pub(super) fn runtime_reconcile_installation_locked(
        &self,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<(), OperationError> {
        let installation_id = identity.installation_id.to_string();
        let root = self.installation_reconciliation_root()?;
        let receipt_path = root.join(format!("{installation_id}.json"));
        let existing = read_helper_reconciliation_receipt(
            &receipt_path,
            Some(rustix::process::geteuid().as_raw()),
        )?;
        self.require_no_unclassified_installation_runtime(&installation_id)?;
        let installation = self
            .roots
            .agent_data
            .join("installations")
            .join(&installation_id);
        if matches!(fs::symlink_metadata(&installation),
            Err(ref error) if error.kind() == std::io::ErrorKind::NotFound)
        {
            // Absence is complete even when a previous receipt was lost.
            return Ok(());
        }
        let (installation_device, installation_inode) =
            self.validate_reconciliation_installation_metadata(identity)?;
        if let Some(receipt) = existing
            && receipt.identity == *identity
            && (receipt.installation_device, receipt.installation_inode)
                != (installation_device, installation_inode)
            && fs::symlink_metadata(installation.join("runtime-cache")).is_ok()
        {
            // Do not delete an unproven replacement under an older receipt.
            // This attempt ends unknown; the receipt is never a START gate.
            return Err(OperationError::InstallationReconciliationStorageUnavailable);
        }
        self.runtime_installation_cleanup_bound(
            &installation_id,
            Some((installation_device, installation_inode)),
        )?;
        write_helper_reconciliation_receipt(
            &root,
            &receipt_path,
            &InstallationReconciliationReceipt {
                schema_version: INSTALLATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION,
                identity: identity.clone(),
                installation_device,
                installation_inode,
            },
        )?;
        Ok(())
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn validate_reconciliation_installation_metadata(
        &self,
        identity: &RecipeReconciliationIdentity,
    ) -> Result<(u64, u64), OperationError> {
        let installation_id = identity.installation_id.to_string();
        let installations = self.roots.agent_data.join("installations");
        let installation = installations.join(&installation_id);
        require_safe_directory(&self.roots.agent_data, self.runtime_request_owner_uid)?;
        require_safe_directory(&installations, self.runtime_request_owner_uid)?;
        let (installation_owner, installation_group) =
            require_exact_directory(&installation, self.runtime_request_owner_uid, 0o700)?;
        let installation_metadata =
            fs::symlink_metadata(&installation).map_err(|_| OperationError::UnsafePath)?;
        if installation_metadata.uid() != installation_owner
            || installation_metadata.gid() != installation_group
        {
            return Err(OperationError::UnsafePath);
        }
        let canonical_agent_data = self
            .roots
            .agent_data
            .canonicalize()
            .map_err(|_| OperationError::UnsafePath)?;
        let canonical_installations = installations
            .canonicalize()
            .map_err(|_| OperationError::UnsafePath)?;
        let canonical_installation = installation
            .canonicalize()
            .map_err(|_| OperationError::UnsafePath)?;
        if canonical_installations.parent() != Some(canonical_agent_data.as_path())
            || canonical_installation.parent() != Some(canonical_installations.as_path())
        {
            return Err(OperationError::UnsafePath);
        }

        Ok((installation_metadata.dev(), installation_metadata.ino()))
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn installation_reconciliation_root(&self) -> Result<PathBuf, OperationError> {
        let root = self.roots.data.join(INSTALLATION_RECONCILIATION_DIRECTORY);
        ensure_runtime_directory(&root)?;
        require_exact_directory(&root, Some(rustix::process::geteuid().as_raw()), 0o700)?;
        Ok(root)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn lock_installation_runtime(
        &self,
        installation_id: &str,
    ) -> Result<File, OperationError> {
        if !valid_artifact_id(installation_id) {
            return Err(OperationError::InvalidOperation);
        }
        let root = self.roots.data.join("runtime-ownership");
        ensure_runtime_directory(&root)?;
        require_exact_directory(&root, Some(rustix::process::geteuid().as_raw()), 0o700)?;
        let lock_path = root.join(format!("{installation_id}.lock"));
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .mode(0o600)
            .open(lock_path)?;
        let metadata = lock.metadata()?;
        if !metadata.is_file()
            || metadata.nlink() != 1
            || metadata.uid() != rustix::process::geteuid().as_raw()
            || metadata.mode() & 0o777 != 0o600
        {
            return Err(OperationError::UnsafePath);
        }
        match rustix::fs::flock(&lock, rustix::fs::FlockOperation::NonBlockingLockExclusive) {
            Ok(()) => Ok(lock),
            Err(error)
                if error == rustix::io::Errno::AGAIN || error == rustix::io::Errno::WOULDBLOCK =>
            {
                Err(OperationError::InstallationReconciliationBusy)
            }
            Err(error) => Err(errno_io(error).into()),
        }
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn refuse_reconciled_runtime(
        &self,
        installation_id: &str,
    ) -> Result<(), OperationError> {
        // Cleanup history is disposable, not standing authority over a later
        // signed START. Generation fencing and current launch validation own
        // stale-effect rejection. Retire the old checkpoint without gating it.
        let root = self.roots.data.join(INSTALLATION_RECONCILIATION_DIRECTORY);
        let path = root.join(format!("{installation_id}.json"));
        let _ = fs::remove_file(path);
        Ok(())
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn require_no_unclassified_installation_runtime(
        &self,
        installation_id: &str,
    ) -> Result<(), OperationError> {
        let cache = self
            .roots
            .agent_data
            .join("installations")
            .join(installation_id)
            .join("runtime-cache");
        // Labels identify accepted effects. The bind source independently
        // catches a container whose labels were damaged: absence of labels
        // alone never proves this cache unused. Unknown unrelated containers
        // retain their bytes and do not enter either exact-target observation.
        for filter in [
            format!("label=ai.vonkforge.installation-id={installation_id}"),
            format!("volume={}", cache.display()),
        ] {
            let output = self.run_docker(&[
                "container".to_owned(),
                "ls".to_owned(),
                "--all".to_owned(),
                "--quiet".to_owned(),
                "--no-trunc".to_owned(),
                "--filter".to_owned(),
                filter,
            ])?;
            // Only a successful empty exact observation proves absence. A
            // container ID, malformed bytes, or failed observation preserves
            // the cache for a subsequent bounded owning-operation attempt.
            if !output.success
                || output.exit_code != Some(0)
                || output.stdout.len() as u64 > MAX_COMMAND_OUTPUT_BYTES
                || !output.stdout.iter().all(u8::is_ascii_whitespace)
            {
                return Err(OperationError::InstallationReconciliationStorageUnavailable);
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests;
