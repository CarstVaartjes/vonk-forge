//! Packages.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    pub fn prepare_package_custody(&self) -> Result<(), OperationError> {
        self.observe_package_custody(|| {})
    }

    fn observe_package_custody(&self, observing: impl FnOnce()) -> Result<(), OperationError> {
        let _install_guard = match self.package_install.try_lock() {
            Ok(guard) => guard,
            Err(std::sync::TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => {
                return Err(OperationError::PackagePreparationUnavailable);
            }
        };
        observing();
        if !self.roots.package_custody.is_absolute() {
            return Err(OperationError::UnsafePath);
        }
        let custody_parent = self
            .roots
            .package_custody
            .parent()
            .ok_or(OperationError::UnsafePath)?;
        require_safe_directory(custody_parent, self.required_owner_uid)?;
        ensure_private_directory(&self.roots.package_custody, self.required_owner_uid)?;

        let deadline = Instant::now() + Duration::from_millis(100);
        for invocation in fs::read_dir(&self.roots.package_custody)? {
            if Instant::now() >= deadline {
                break;
            }
            let invocation = match invocation {
                Ok(invocation) => invocation,
                Err(error) => {
                    eprintln!("package custody entry observation unavailable: {error}");
                    continue;
                }
            };
            // An unproven entry remains inert. It must not veto cleanup of an
            // independent entry or admission into a fresh private namespace.
            if let Err(error) = self.clean_package_candidate(&invocation) {
                eprintln!("package custody entry observation unavailable: {error}");
            }
        }
        sync_directory(&self.roots.package_custody)
    }

    fn clean_package_candidate(&self, invocation: &fs::DirEntry) -> Result<(), OperationError> {
        let name = invocation.file_name();
        let name = name.to_str().ok_or(OperationError::UnsafePath)?;
        if !lower_hex(name, 32) {
            return Err(OperationError::UnsafePath);
        }
        let directory = invocation.path();
        require_exact_directory(&directory, self.required_owner_uid, 0o700)?;
        let mut candidates = fs::read_dir(&directory)?;
        let candidate = candidates.next().transpose()?;
        if candidates.next().is_some() {
            return Err(OperationError::PackagePreparationUnavailable);
        }
        if let Some(candidate) = candidate {
            let name = candidate.file_name();
            let name = name
                .to_str()
                .and_then(|value| value.strip_suffix(".deb"))
                .ok_or(OperationError::UnsafePath)?;
            if !lower_hex(name, 64) {
                return Err(OperationError::UnsafePath);
            }
            let metadata = fs::symlink_metadata(candidate.path())?;
            if !safe_custody_file(&metadata, self.required_owner_uid, metadata.len()) {
                return Err(OperationError::UnsafePath);
            }
            fs::remove_file(candidate.path())?;
        }
        fs::remove_dir(directory)?;
        Ok(())
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn install_package(
        &self,
        digest: &str,
        detached_signature: &str,
        rollback: &PackageRollbackAuthority,
        node_id: &str,
    ) -> Result<(), OperationError> {
        let _install_guard = match self.package_install.try_lock() {
            Ok(guard) => guard,
            Err(std::sync::TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
            Err(std::sync::TryLockError::WouldBlock) => {
                return Err(OperationError::PackagePreparationUnavailable);
            }
        };
        require_safe_directory(&self.roots.incoming, self.package_owner_uid)?;
        let incoming = self.roots.incoming.join(format!("{digest}.deb"));
        let package = self.take_package_custody(&incoming, digest, detached_signature)?;
        let source = self.take_package_custody(
            &self
                .roots
                .incoming
                .join(format!("{}.deb", rollback.source.package_sha256)),
            &rollback.source.package_sha256,
            &rollback.source.package_signature,
        )?;
        self.runner
            .arm_package_rollback(node_id, source.path(), package.path(), digest, rollback)
            .map_err(|_| OperationError::PackagePreflightFailed)?;
        source.cleanup()?;
        let package_name = package.path().to_string_lossy().into_owned();
        self.require_package_field(&package_name, "Package", "vonk-forge-agent")?;
        self.require_package_field(&package_name, "Architecture", "arm64")?;
        let result = self
            .runner
            .run(
                Path::new("/usr/bin/dpkg"),
                &[
                    "--install".to_owned(),
                    "--force-confold".to_owned(),
                    package_name,
                ],
            )
            .map_err(|diagnostic| OperationError::PackageInstallFailed {
                exit_code: None,
                diagnostic,
            })?;
        if !result.success {
            let _ = self.runner.package_activation_failed();
            return Err(OperationError::PackageInstallFailed {
                exit_code: result.exit_code,
                diagnostic: String::from_utf8_lossy(&result.stdout).into_owned(),
            });
        }
        package.cleanup()?;
        Ok(())
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn take_package_custody(
        &self,
        incoming: &Path,
        expected_digest: &str,
        detached_signature: &str,
    ) -> Result<CustodiedPackage, OperationError> {
        self.take_package_custody_until(
            incoming,
            expected_digest,
            detached_signature,
            Instant::now() + Duration::from_secs(120),
        )
    }

    fn take_package_custody_until(
        &self,
        incoming: &Path,
        expected_digest: &str,
        detached_signature: &str,
        deadline: Instant,
    ) -> Result<CustodiedPackage, OperationError> {
        if !self.roots.package_custody.is_absolute() {
            return Err(OperationError::UnsafePath);
        }
        let custody_parent = self
            .roots
            .package_custody
            .parent()
            .ok_or(OperationError::UnsafePath)?;
        require_safe_directory(custody_parent, self.required_owner_uid)?;
        ensure_private_directory(&self.roots.package_custody, self.required_owner_uid)?;

        // The agent owns `incoming`, so a path verified there cannot be handed to
        // a privileged process. Copy through one no-follow descriptor into a
        // fresh root-only namespace and make every subsequent consumer use it.
        let invocation = uuid::Uuid::new_v4().simple().to_string();
        let invocation_directory = self.roots.package_custody.join(invocation);
        fs::create_dir(&invocation_directory)?;
        fs::set_permissions(&invocation_directory, fs::Permissions::from_mode(0o700))?;
        require_exact_directory(&invocation_directory, self.required_owner_uid, 0o700)?;
        let candidate = invocation_directory.join(format!("{expected_digest}.deb"));
        let custody = CustodiedPackage::new(
            candidate,
            invocation_directory,
            self.roots.package_custody.clone(),
        );

        let mut source = OpenOptions::new()
            .read(true)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK).bits() as i32,
            )
            .open(incoming)?;
        let source_before = source.metadata()?;
        require_agent_artifact(&source_before, self.package_owner_uid)?;
        let mut destination = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(custody.path())?;
        let mut digest = Sha256::new();
        let mut consumed = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        // Bound copy observations between regular-file I/O calls.
        loop {
            if Instant::now() >= deadline {
                return Err(OperationError::PackagePreparationUnavailable);
            }
            let count = source.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            consumed = consumed
                .checked_add(count as u64)
                .filter(|value| *value <= MAX_ARTIFACT_BYTES)
                .ok_or(OperationError::InvalidArtifact)?;
            digest.update(&buffer[..count]);
            destination.write_all(&buffer[..count])?;
        }
        destination.sync_all()?;
        let source_after = source.metadata()?;
        let destination_metadata = destination.metadata()?;
        let observed_digest = hex::encode(digest.finalize());
        if artifact_identity(&source_before) != artifact_identity(&source_after)
            || consumed != source_before.len()
            || observed_digest != expected_digest
            || !safe_custody_file(&destination_metadata, self.required_owner_uid, consumed)
        {
            return Err(OperationError::InvalidArtifact);
        }
        let signature_bytes =
            hex::decode(detached_signature).map_err(|_| OperationError::InvalidArtifact)?;
        signature::UnparsedPublicKey::new(&signature::ED25519, self.release_public_key)
            .verify(
                &artifact_signing_bytes("deb", expected_digest)
                    .map_err(|_| OperationError::InvalidArtifact)?,
                &signature_bytes,
            )
            .map_err(|_| OperationError::InvalidArtifact)?;
        drop(destination);
        drop(source);
        sync_directory(&self.roots.package_custody)?;
        Ok(custody)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn require_package_field(
        &self,
        package: &str,
        field: &str,
        expected: &str,
    ) -> Result<(), OperationError> {
        let result = self
            .runner
            .run(
                Path::new("/usr/bin/dpkg-deb"),
                &["--field".to_owned(), package.to_owned(), field.to_owned()],
            )
            .map_err(|_| OperationError::PackageMetadataInvalid)?;
        if !result.success || result.stdout != format!("{expected}\n").as_bytes() {
            return Err(OperationError::PackageMetadataInvalid);
        }
        Ok(())
    }
}

impl CustodiedPackage {
    pub(super) fn new(path: PathBuf, invocation_directory: PathBuf, custody_root: PathBuf) -> Self {
        Self {
            path,
            invocation_directory,
            custody_root,
            cleaned: false,
        }
    }

    pub(super) fn path(&self) -> &Path {
        &self.path
    }

    pub(super) fn cleanup(mut self) -> Result<(), OperationError> {
        self.cleanup_inner()?;
        self.cleaned = true;
        Ok(())
    }

    pub(super) fn cleanup_inner(&self) -> Result<(), OperationError> {
        match fs::remove_file(&self.path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        match fs::remove_dir(&self.invocation_directory) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        sync_directory(&self.custody_root)
    }
}

impl Drop for CustodiedPackage {
    fn drop(&mut self) {
        if !self.cleaned {
            let _ = self.cleanup_inner();
        }
    }
}

#[cfg(test)]
mod tests;
