//! Lifecycle for the oci boundary.

use super::*;

impl<R: ProcessRunner> OciRuntime<'_, R> {
    pub fn start_arguments(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
    ) -> Result<Vec<String>, OciError> {
        spec.validate()?;
        placement.validate_bound()?;
        if placement != &spec.runtime.placement {
            return Err(OciError::Runtime);
        }
        managed_path(self.data_root, "installations", installation_id)?;
        let run_root = managed_path(self.data_root, "runs", run_id)?;
        let outputs = run_root.join("outputs");
        let metadata = self.run_metadata_path(run_id)?;
        let runtime_cache =
            managed_path(self.data_root, "installations", installation_id)?.join("runtime-cache");
        start_arguments_for_paths(
            spec,
            &CompiledOciPaths {
                model_root: self
                    .data_root
                    .join("installations")
                    .join(installation_id)
                    .join("models"),
                input_root: spec.job.as_ref().map(|_| run_root.join("inputs")),
                output_root: outputs,
                cache_root: runtime_cache,
                runtime_spec: metadata.join("runtime.json"),
            },
            run_id,
        )
    }

    pub(super) fn ensure_runtime_cache(&self, installation_id: &str) -> Result<PathBuf, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let cache = installation.join("runtime-cache");
        fs::create_dir_all(&cache)?;
        fs::set_permissions(&cache, fs::Permissions::from_mode(0o700))?;
        let metadata = fs::symlink_metadata(&cache)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        Ok(cache)
    }

    pub fn prepare_start(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.prepare_start_internal(spec, installation_id, run_id, placement, None)
    }

    pub fn prepare_start_with_inspection_identity(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
        identity: &RecipeRunStartIdentity,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.prepare_start_internal(spec, installation_id, run_id, placement, Some(identity))
    }

    pub(super) fn prepare_start_internal(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
        identity: Option<&RecipeRunStartIdentity>,
    ) -> Result<RuntimeStartPlan, OciError> {
        start_stage(FailureStage::ImageVerification, || self.verify_image(spec))?;
        let state = start_stage(FailureStage::RunStorage, || {
            let state = managed_path(self.data_root, "runs", run_id)?;
            fs::create_dir_all(&state)?;
            fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
            Ok(state)
        })?;
        start_stage(FailureStage::OutputStorage, || {
            let outputs = state.join("outputs");
            fs::create_dir_all(&outputs)?;
            fs::set_permissions(&outputs, fs::Permissions::from_mode(0o700))?;
            ensure_runtime_tmp(&outputs)
        })?;
        start_stage(FailureStage::RuntimeCache, || {
            self.ensure_runtime_cache(installation_id)
        })?;
        start_stage(FailureStage::JobInputs, || {
            if spec.job.is_some() {
                let inputs = state.join("inputs");
                let metadata = fs::symlink_metadata(&inputs)?;
                if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
                    return Err(OciError::Artifact);
                }
            }
            Ok(())
        })?;
        let metadata = start_stage(FailureStage::RuntimeMetadata, || {
            let metadata = self.ensure_run_metadata(run_id)?;
            self.write_runtime_contract(spec, run_id)?;
            // The first authorized helper invocation resets private runtime
            // tmp. Keep the marker outside writable mounts.
            atomic_write(&metadata, "tmp-reset-required", b"")?;
            File::open(&metadata)?.sync_all()?;
            Ok(metadata)
        })?;
        let main = start_stage(FailureStage::RuntimeProjection, || {
            self.start_arguments(spec, installation_id, run_id, placement)
        })?;
        let runtime_image_digest = spec.runtime_image.image_digest.clone();
        let run_generation = identity
            .map(|identity| {
                (1..=i64::MAX as u64)
                    .contains(&identity.run_generation)
                    .then_some(identity.run_generation)
                    .ok_or(OciError::Artifact)
            })
            .transpose()
            .map_err(|source| OciError::Start {
                stage: FailureStage::ObservationIdentity,
                source: Box::new(source),
            })?;
        start_stage(FailureStage::LifecycleMetadata, || {
            atomic_write(
                &metadata,
                "lifecycle.json",
                &serde_json::to_vec(&RunLifecycle {
                    installation_id: installation_id.to_owned(),
                    placement: placement.clone(),
                    run_generation,
                })?,
            )
        })?;
        Ok(RuntimeStartPlan {
            image_digest: runtime_image_digest,
            registry_index_digest: spec.runtime_image.image_digest.clone(),
            platform_manifest_digest: spec.runtime_image.image_digest.clone(),
            archive_sha256: spec.runtime_image.oci_layout_sha256.clone(),
            image_reference: spec.runtime_image.local_image_reference(),
            main,
        })
    }

    /// Reconstruct the exact runtime plan for a previously launched service.
    ///
    /// Collective readiness is deliberately a separate, non-starting phase for
    /// distributed workloads.  It may inspect only the run identity retained
    /// by `prepare_start`; a changed request, installation, specification, or
    /// placement fails closed instead of being allowed to inspect a different
    /// container under the same run id.
    pub fn prepare_retained_start(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.verify_image(spec)?;
        let Some((retained_spec, retained_installation_id, retained_placement, _)) =
            self.load_run_lifecycle(run_id)?
        else {
            return Err(OciError::Runtime);
        };
        if retained_spec != *spec
            || retained_installation_id != installation_id
            || retained_placement != *placement
        {
            return Err(OciError::Runtime);
        }
        // Retained reconstruction is inspection/collective-readiness only.
        // Only the authorized helper may reset runtime-owned temporary files.
        Ok(RuntimeStartPlan {
            image_digest: spec.runtime_image.image_digest.clone(),
            registry_index_digest: spec.runtime_image.image_digest.clone(),
            platform_manifest_digest: spec.runtime_image.image_digest.clone(),
            archive_sha256: spec.runtime_image.oci_layout_sha256.clone(),
            image_reference: spec.runtime_image.local_image_reference(),
            main: self.start_arguments(spec, installation_id, run_id, placement)?,
        })
    }

    /// A fresh claim may observe an earlier exact Start without rewriting its
    /// runtime contract or clearing tmp.
    pub fn prepare_retained_start_if_present(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
        identity: Option<&RecipeRunStartIdentity>,
    ) -> Result<Option<RuntimeStartPlan>, OciError> {
        if self.load_run_lifecycle(run_id)?.is_none() {
            return Ok(None);
        }
        let plan = match identity {
            Some(identity) => self.prepare_retained_start_with_inspection_identity(
                spec,
                installation_id,
                run_id,
                placement,
                identity,
            )?,
            None => self.prepare_retained_start(spec, installation_id, run_id, placement)?,
        };
        Ok(Some(plan))
    }

    pub fn prepare_retained_start_with_inspection_identity(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
        identity: &RecipeRunStartIdentity,
    ) -> Result<RuntimeStartPlan, OciError> {
        let plan = self.prepare_retained_start(spec, installation_id, run_id, placement)?;
        let Some((_, _, _, Some(run_generation))) = self.load_run_lifecycle(run_id)? else {
            return Err(OciError::Runtime);
        };
        if run_generation != identity.run_generation {
            return Err(OciError::Runtime);
        }
        Ok(plan)
    }

    pub fn prepare_job_start(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        run_id: &str,
        placement: &CompiledRuntimePlacement,
        invocation: &CompiledExecutionPlan,
    ) -> Result<RuntimeStartPlan, OciError> {
        invocation.validate()?;
        if !same_job_workload(spec, invocation) {
            return Err(OciError::Runtime);
        }
        self.prepare_start(invocation, installation_id, run_id, placement)
    }

    pub fn prepare_stop(&self, run_id: &str) -> Result<RuntimeStopPlan, OciError> {
        let lifecycle = self.load_run_lifecycle(run_id)?;
        let stop_timeout = lifecycle
            .as_ref()
            .map(|(spec, _, _, _)| spec.lifecycle.stop_timeout_seconds)
            .unwrap_or(30);
        let (
            image_digest,
            registry_index_digest,
            platform_manifest_digest,
            archive_sha256,
            image_reference,
        ) = match lifecycle {
            Some((spec, _, _, _)) => (
                Some(spec.runtime_image.image_digest.clone()),
                Some(spec.runtime_image.image_digest.clone()),
                Some(spec.runtime_image.image_digest.clone()),
                Some(spec.runtime_image.oci_layout_sha256.clone()),
                Some(spec.runtime_image.local_image_reference()),
            ),
            None => (None, None, None, None, None),
        };
        Ok(RuntimeStopPlan {
            remove: vec![run_id.to_owned(), stop_timeout.to_string()],
            image_digest,
            registry_index_digest,
            platform_manifest_digest,
            archive_sha256,
            image_reference,
        })
    }

    pub fn complete_stop(&self, run_id: &str) -> Result<(), OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        let directory = match fs::symlink_metadata(&metadata) {
            Ok(directory) => directory,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error.into()),
        };
        if !directory.file_type().is_dir()
            || directory.file_type().is_symlink()
            || directory.uid() != rustix::process::geteuid().as_raw()
            || directory.mode() & 0o077 != 0
        {
            return Err(OciError::Artifact);
        }
        match fs::remove_file(metadata.join("lifecycle.json")) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        match File::open(metadata) {
            Ok(directory) => directory.sync_all().map_err(OciError::Io),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
    }
}

#[cfg(test)]
mod tests;
