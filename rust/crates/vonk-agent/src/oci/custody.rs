//! Custody for the oci boundary.

use super::*;

impl<R: ProcessRunner> OciRuntime<'_, R> {
    pub fn uninstall(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
    ) -> Result<(), OciError> {
        self.finalize_uninstall(installation_id, expected_recipe_digest)
    }

    pub fn validate_uninstall(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
    ) -> Result<(), OciError> {
        self.load_uninstall_spec(installation_id, expected_recipe_digest)
            .map(|_| ())
    }

    pub(super) fn load_uninstall_spec(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
    ) -> Result<CompiledExecutionPlan, OciError> {
        let plan = self.read_persisted_spec(installation_id)?;
        plan.validate_storage()?;
        if self.recipe_digest(installation_id)? != expected_recipe_digest {
            return Err(OciError::Artifact);
        }
        Ok(plan)
    }

    pub fn runtime_cache_present(&self, installation_id: &str) -> Result<bool, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let cache = installation.join("runtime-cache");
        match fs::symlink_metadata(cache) {
            Ok(metadata) if metadata.file_type().is_dir() && !metadata.file_type().is_symlink() => {
                Ok(true)
            }
            Ok(_) => Err(OciError::Artifact),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error.into()),
        }
    }

    pub fn finalize_uninstall(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
    ) -> Result<(), OciError> {
        let identity = RecipeReconciliationIdentity {
            installation_id: uuid::Uuid::parse_str(installation_id)
                .map_err(|_| OciError::Artifact)?,
            plan_digest: expected_recipe_digest.to_owned(),
        };
        self.prepare_reconciliation(&identity)?;
        self.finalize_reconciliation(&identity).map(|_| ())
    }

    /// Remove this installation's materialized model files, then the shared
    /// store's copy of each one that no installation links any more. Other
    /// installations keep their own files, and a store object one of them still
    /// links stays for them and for future installations.
    pub fn uninstall_with_model_cleanup(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
        model_content_sha256: &str,
    ) -> Result<u64, OciError> {
        // Reference observations may be incomplete after interrupted removal.
        // The accepted exact removal continues; uncertain shared objects stay.
        let removed_model_bytes = self
            .validate_uninstall_with_model_cleanup(
                installation_id,
                expected_recipe_digest,
                model_content_sha256,
            )
            .unwrap_or(0);
        let store_objects = self
            .model_store_objects(
                installation_id,
                expected_recipe_digest,
                model_content_sha256,
            )
            .unwrap_or_default();
        self.uninstall(installation_id, expected_recipe_digest)?;
        self.reclaim_unshared_model_objects(&store_objects);
        Ok(removed_model_bytes)
    }

    /// The shared-store objects (by file digest) the installation's files of one
    /// model were made from.
    pub fn model_store_objects(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
        model_content_sha256: &str,
    ) -> Result<Vec<String>, OciError> {
        let persisted = self.load_uninstall_spec(installation_id, expected_recipe_digest)?;
        if !spec_references_model(&persisted, model_content_sha256) {
            return Err(OciError::Artifact);
        }
        Ok(unique_plan_artifacts(&persisted)
            .into_iter()
            .filter(|artifact| artifact.model.content_sha256 == model_content_sha256)
            .map(|artifact| artifact.sha256.clone())
            .filter(|sha256| lower_hex(sha256, 64))
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect())
    }

    /// Remove each named store object that nothing links any more (its only
    /// link is the store's own name for it), and return the bytes that freed.
    ///
    /// Installations link store objects instead of copying them, so removing an
    /// installation alone frees none of its model bytes. An object that still
    /// has another link belongs to an installation that is still using it and
    /// is left alone. Removal is best effort: an object that cannot be removed
    /// stays for the next uninstall.
    pub fn reclaim_unshared_model_objects(&self, digests: &[String]) -> u64 {
        let owner = rustix::process::geteuid().as_raw();
        digests
            .iter()
            .filter(|digest| lower_hex(digest, 64))
            .filter_map(|digest| {
                let path = store_object_path(self.data_root, digest);
                let metadata = fs::symlink_metadata(&path).ok()?;
                (metadata.file_type().is_file()
                    && metadata.uid() == owner
                    && metadata.nlink() == 1
                    && fs::remove_file(&path).is_ok())
                .then_some(metadata.len())
            })
            .fold(0_u64, u64::saturating_add)
    }

    pub fn validate_uninstall_with_model_cleanup(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
        model_content_sha256: &str,
    ) -> Result<u64, OciError> {
        if !lower_hex(model_content_sha256, 64) {
            return Err(OciError::Artifact);
        }
        let persisted = self.load_uninstall_spec(installation_id, expected_recipe_digest)?;
        if !spec_references_model(&persisted, model_content_sha256) {
            return Err(OciError::Artifact);
        }
        let removed_model_bytes =
            materialized_model_bytes(self.data_root, installation_id, &persisted)?;
        Ok(removed_model_bytes)
    }

    pub fn load_spec(&self, installation_id: &str) -> Result<CompiledExecutionPlan, OciError> {
        self.load_persisted_spec(installation_id)
            .map(|(spec, _)| spec)
    }

    pub(super) fn load_persisted_spec(
        &self,
        installation_id: &str,
    ) -> Result<(CompiledExecutionPlan, CompiledExecutionPlan), OciError> {
        let persisted = self.read_persisted_spec(installation_id)?;
        persisted.validate()?;
        Ok((persisted.clone(), persisted))
    }

    pub(super) fn read_persisted_spec(
        &self,
        installation_id: &str,
    ) -> Result<CompiledExecutionPlan, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let directory_metadata = fs::symlink_metadata(&installation)?;
        if !directory_metadata.file_type().is_dir() || directory_metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let path = installation.join("spec.json");
        let metadata = fs::symlink_metadata(&path)?;
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() > MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES as u64
        {
            return Err(OciError::Artifact);
        }
        serde_json::from_slice(&read_regular_file(
            &path,
            MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES as u64,
        )?)
        .map_err(OciError::Json)
    }

    pub fn verify_installation(&self, installation_id: &str) -> Result<(), OciError> {
        let (_, plan) = self.load_persisted_spec(installation_id)?;
        self.verify_plan_materialization(installation_id, &plan)
    }

    pub(super) fn verify_plan_materialization(
        &self,
        installation_id: &str,
        plan: &CompiledExecutionPlan,
    ) -> Result<(), OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let models = installation.join("models");
        let receipt = read_installation_metadata(&installation)?
            .filter(|receipt| receipt_matches_plan(receipt, plan));
        let receipt_index = receipt.as_ref().map(|receipt| {
            receipt
                .entries
                .iter()
                .map(|entry| ((entry.selection_id.as_str(), entry.path.as_str()), entry))
                .collect::<BTreeMap<_, _>>()
        });
        if receipt.is_some() {
            let mut fast_path = true;
            for artifact in unique_plan_artifacts(plan) {
                let destination = models.join(&artifact.selection_id).join(&artifact.path);
                let Some(entry) = receipt_index.as_ref().and_then(|index| {
                    index.get(&(artifact.selection_id.as_str(), artifact.path.as_str()))
                }) else {
                    fast_path = false;
                    break;
                };
                let (_, metadata) = open_trusted_model_file(
                    &destination,
                    artifact.size_bytes,
                    shared_store_inode(self.data_root, &artifact.sha256),
                )?;
                if !metadata_matches_receipt(&metadata, entry) {
                    fast_path = false;
                    break;
                }
            }
            if fast_path {
                return Ok(());
            }
        }

        let unique_artifacts = unique_plan_artifacts(plan);
        let mut refreshed = Vec::with_capacity(unique_artifacts.len());
        for artifact in unique_artifacts {
            let destination = models.join(&artifact.selection_id).join(&artifact.path);
            let (_file, metadata) = open_trusted_model_file(
                &destination,
                artifact.size_bytes,
                shared_store_inode(self.data_root, &artifact.sha256),
            )?;
            if let Some(entry) = receipt_index.as_ref().and_then(|index| {
                index
                    .get(&(artifact.selection_id.as_str(), artifact.path.as_str()))
                    .filter(|entry| metadata_matches_receipt(&metadata, entry))
            }) {
                refreshed.push((**entry).clone());
                continue;
            }
            // Model files reached this installation from the Controller over
            // mTLS; size and trusted custody (checked above) are the check.
            refreshed.push(installation_metadata_entry(artifact, &metadata));
        }
        sort_metadata_entries(&mut refreshed);
        atomic_write(
            &installation,
            INSTALLATION_METADATA_FILE,
            &serde_json::to_vec(&InstallationMetadataReceipt {
                schema_version: INSTALLATION_METADATA_SCHEMA_VERSION,
                entries: refreshed,
            })?,
        )?;
        File::open(&installation)?.sync_all()?;
        Ok(())
    }

    pub fn begin_installation_acl_transition(
        &self,
        installation_id: &str,
    ) -> Result<InstallationAclTransition, OciError> {
        let (_, plan) = self.load_persisted_spec(installation_id)?;
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let receipt = read_installation_metadata(&installation)?
            .filter(|receipt| receipt_matches_plan(receipt, &plan))
            .ok_or(OciError::Artifact)?;
        let models = installation.join("models");
        let mut files = Vec::with_capacity(receipt.entries.len());
        for entry in &receipt.entries {
            let path = models.join(&entry.selection_id).join(&entry.path);
            let (file, metadata) = open_trusted_model_file(
                &path,
                entry.size_bytes,
                shared_store_inode(self.data_root, &entry.sha256),
            )?;
            if !metadata_matches_receipt(&metadata, entry) {
                return Err(OciError::Artifact);
            }
            files.push((path, file, metadata));
        }
        Ok(InstallationAclTransition {
            installation_id: installation_id.to_owned(),
            receipt,
            files,
        })
    }

    /// Record the ACL transition of an installation's model files.
    ///
    /// The step reads and verifies only: it works on a copy of the receipt and
    /// changes the stored one in a single atomic write at the end, so a failed
    /// attempt leaves the transition and the installation untouched and the
    /// same call can be repeated until it succeeds.
    pub fn finish_installation_acl_transition(
        &self,
        installation_id: &str,
        transition: &InstallationAclTransition,
    ) -> Result<(), OciError> {
        if transition.installation_id != installation_id {
            return Err(OciError::Artifact);
        }
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        // Receipt damage does not invalidate retained, verified open handles.
        // Reconstruct bookkeeping from those handles after the ACL transition.
        let previous_receipt = transition.receipt.clone();
        let mut receipt = transition.receipt.clone();
        for (entry, (path, file, before)) in receipt.entries.iter_mut().zip(transition.files.iter())
        {
            let after = file.metadata()?;
            let shared = shared_store_inode(self.data_root, &entry.sha256);
            let (reopened, path_after) = open_trusted_model_file(path, entry.size_bytes, shared)?;
            if before.dev() != after.dev()
                || before.ino() != after.ino()
                || before.len() != after.len()
                || before.mtime() != after.mtime()
                || before.mtime_nsec() != after.mtime_nsec()
                || after.dev() != path_after.dev()
                || after.ino() != path_after.ino()
                || after.len() != path_after.len()
                || after.mtime() != path_after.mtime()
                || after.mtime_nsec() != path_after.mtime_nsec()
                || after.ctime() != path_after.ctime()
                || after.ctime_nsec() != path_after.ctime_nsec()
                || !trusted_model_file(file, &after, entry.size_bytes, shared)
                || !trusted_model_file(&reopened, &path_after, entry.size_bytes, shared)
            {
                return Err(OciError::Artifact);
            }
            entry.ctime_ns = timestamp_ns(after.ctime(), after.ctime_nsec());
        }
        if receipt == previous_receipt {
            return Ok(());
        }
        atomic_write(
            &installation,
            INSTALLATION_METADATA_FILE,
            &serde_json::to_vec(&receipt)?,
        )?;
        File::open(&installation)?.sync_all()?;
        Ok(())
    }

    pub fn recipe_digest(&self, installation_id: &str) -> Result<String, OciError> {
        let path = managed_path(self.data_root, "installations", installation_id)?
            .join("recipe-content.sha256");
        let value =
            String::from_utf8(read_regular_file(&path, 64)?).map_err(|_| OciError::Artifact)?;
        if value.len() != 64
            || !value
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(OciError::Artifact);
        }
        Ok(value)
    }

    pub fn recipe_digest_if_present(
        &self,
        installation_id: &str,
    ) -> Result<Option<String>, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let metadata = match fs::symlink_metadata(&installation) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        self.recipe_digest(installation_id).map(Some)
    }

    /// The logical size of the installation tree: every file counted at its
    /// full length, whether it is a private copy or a link to a shared model
    /// object. Only metadata is read; no content is.
    pub fn installed_bytes(&self, installation_id: &str) -> Result<u64, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        tree_bytes(&installation)
    }

    pub fn artifact_set_digest(&self, installation_id: &str) -> Result<String, OciError> {
        let (_, persisted) = self.load_persisted_spec(installation_id)?;
        Ok(persisted.identity.model_artifact_set_sha256)
    }

    pub(super) fn write_runtime_contract(
        &self,
        spec: &CompiledExecutionPlan,
        run_id: &str,
    ) -> Result<(), OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        atomic_write(&metadata, "runtime.json", &serde_json::to_vec(spec)?)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests;
