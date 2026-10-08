//! Generation.

use super::*;

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn runtime_generation_fence_root(&self) -> Result<PathBuf, OperationError> {
        let root = self.roots.data.join(RUNTIME_GENERATION_FENCE_DIRECTORY);
        let created = match fs::symlink_metadata(&root) {
            Ok(_) => false,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => true,
            Err(error) => return Err(error.into()),
        };
        ensure_runtime_directory(&root)?;
        require_exact_directory(&root, Some(rustix::process::geteuid().as_raw()), 0o700)?;
        if created {
            let parent = root.parent().ok_or(OperationError::UnsafePath)?;
            sync_directory(parent)?;
        }
        Ok(root)
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn read_runtime_generation_fence(
        &self,
        installation_id: uuid::Uuid,
        runtime_id: uuid::Uuid,
    ) -> Result<Option<RuntimeGenerationFence>, OperationError> {
        let root = self.runtime_generation_fence_root()?;
        let path = root.join(runtime_generation_fence_filename(
            installation_id,
            runtime_id,
        ));
        let path_metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if path_metadata.file_type().is_symlink()
            || !path_metadata.is_file()
            || path_metadata.nlink() != 1
            || path_metadata.len() == 0
            || path_metadata.len() > MAX_RUNTIME_GENERATION_FENCE_BYTES
            || path_metadata.uid() != rustix::process::geteuid().as_raw()
            || path_metadata.mode() & 0o777 != 0o600
        {
            return Err(OperationError::InvalidArtifact);
        }
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(&path)?;
        let opened = file.metadata()?;
        if artifact_identity(&opened) != artifact_identity(&path_metadata) {
            return Err(OperationError::InvalidArtifact);
        }
        let mut bytes = Vec::with_capacity(path_metadata.len() as usize);
        Read::by_ref(&mut file)
            .take(MAX_RUNTIME_GENERATION_FENCE_BYTES + 1)
            .read_to_end(&mut bytes)?;
        let after = file.metadata()?;
        if bytes.len() as u64 != path_metadata.len()
            || artifact_identity(&after) != artifact_identity(&path_metadata)
        {
            return Err(OperationError::InvalidArtifact);
        }
        let fence: RuntimeGenerationFence =
            parse_strict(&bytes).map_err(|_| OperationError::InvalidArtifact)?;
        if fence.schema_version != RUNTIME_GENERATION_FENCE_SCHEMA_VERSION
            || fence.installation_id != installation_id
            || fence.runtime_id != runtime_id
            || fence.highest_generation == 0
            || fence.highest_generation > i64::MAX as u64
            || canonical_json(&fence).map_err(|_| OperationError::InvalidArtifact)? != bytes
        {
            return Err(OperationError::InvalidArtifact);
        }
        Ok(Some(fence))
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn write_runtime_generation_fence(
        &self,
        fence: &RuntimeGenerationFence,
    ) -> Result<(), OperationError> {
        let root = self.runtime_generation_fence_root()?;
        let path = root.join(runtime_generation_fence_filename(
            fence.installation_id,
            fence.runtime_id,
        ));
        let bytes = canonical_json(fence).map_err(|_| OperationError::InvalidOperation)?;
        if fence.schema_version != RUNTIME_GENERATION_FENCE_SCHEMA_VERSION
            || fence.highest_generation == 0
            || fence.highest_generation > i64::MAX as u64
            || bytes.len() as u64 > MAX_RUNTIME_GENERATION_FENCE_BYTES
        {
            return Err(OperationError::InvalidOperation);
        }
        let temporary = root.join(format!(
            ".runtime-generation-fence-{}.tmp",
            uuid::Uuid::new_v4()
        ));
        let result = (|| {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
                .mode(0o600)
                .open(&temporary)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&temporary, &path)?;
            sync_directory(&root)
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }
}

impl<R: CommandRunner> OperationExecutor<R> {
    pub(super) fn update_runtime_generation_fence(
        &self,
        identity: &RuntimeEffectIdentity,
        use_kind: RuntimeGenerationFenceUse,
    ) -> Result<(), OperationError> {
        if identity.run_generation == 0 || identity.run_generation > i64::MAX as u64 {
            return Err(OperationError::InvalidOperation);
        }
        let current =
            self.read_runtime_generation_fence(identity.installation_id, identity.runtime_id)?;
        if let Some(mut current) = current {
            if identity.run_generation < current.highest_generation {
                return match use_kind {
                    // An exact older Stop may clean up the container whose
                    // labels carry this generation. It cannot rewind the
                    // high-water mark, so a delayed older Start remains
                    // fenced after restart.
                    RuntimeGenerationFenceUse::Stop { .. } => Ok(()),
                    RuntimeGenerationFenceUse::Start => Err(OperationError::InvalidOperation),
                };
            }
            if identity.run_generation == current.highest_generation {
                match use_kind {
                    RuntimeGenerationFenceUse::Start if current.cancelled => {
                        return Err(OperationError::InvalidOperation);
                    }
                    RuntimeGenerationFenceUse::Stop {
                        cancel_pending_start: true,
                    } if !current.cancelled => {
                        current.cancelled = true;
                    }
                    RuntimeGenerationFenceUse::Start | RuntimeGenerationFenceUse::Stop { .. } => {
                        return Ok(());
                    }
                }
            } else {
                current.highest_generation = identity.run_generation;
                current.cancelled = matches!(
                    use_kind,
                    RuntimeGenerationFenceUse::Stop {
                        cancel_pending_start: true
                    }
                );
            }
            self.write_runtime_generation_fence(&current)
        } else {
            self.write_runtime_generation_fence(&RuntimeGenerationFence {
                schema_version: RUNTIME_GENERATION_FENCE_SCHEMA_VERSION,
                installation_id: identity.installation_id,
                runtime_id: identity.runtime_id,
                highest_generation: identity.run_generation,
                cancelled: matches!(
                    use_kind,
                    RuntimeGenerationFenceUse::Stop {
                        cancel_pending_start: true
                    }
                ),
            })
        }
    }
}

#[cfg(test)]
mod tests;

impl JobCancellationFence {
    pub(super) fn begin(
        &self,
        identity: RuntimeEffectIdentity,
    ) -> Result<ActiveJobStart<'_>, OperationError> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| OperationError::CommandFailed)?;
        if state.cancelled.contains(&identity) || !state.active_starts.insert(identity) {
            return Err(OperationError::CommandFailed);
        }
        Ok(ActiveJobStart {
            fence: self,
            identity,
        })
    }

    pub(super) fn cancel(&self, identity: RuntimeEffectIdentity) -> Result<(), OperationError> {
        let mut state = self
            .state
            .lock()
            .map_err(|_| OperationError::CommandFailed)?;
        if state.active_starts.contains(&identity) {
            state.cancelled.insert(identity);
        }
        Ok(())
    }

    pub(super) fn is_active(
        &self,
        identity: RuntimeEffectIdentity,
    ) -> Result<bool, OperationError> {
        Ok(self
            .state
            .lock()
            .map_err(|_| OperationError::CommandFailed)?
            .active_starts
            .contains(&identity))
    }

    pub(super) fn was_cancelled(
        &self,
        identity: RuntimeEffectIdentity,
    ) -> Result<bool, OperationError> {
        Ok(self
            .state
            .lock()
            .map_err(|_| OperationError::CommandFailed)?
            .cancelled
            .contains(&identity))
    }

    pub(super) fn wait_for_active_start(
        &self,
        identity: RuntimeEffectIdentity,
        deadline: Instant,
    ) -> Result<(), OperationError> {
        while self.is_active(identity)? {
            if Instant::now() >= deadline {
                return Err(OperationError::StopUncertain);
            }
            thread::sleep(Duration::from_millis(50));
        }
        Ok(())
    }
}

impl Drop for ActiveJobStart<'_> {
    fn drop(&mut self) {
        if let Ok(mut state) = self.fence.state.lock() {
            state.active_starts.remove(&self.identity);
            state.cancelled.remove(&self.identity);
        }
    }
}
