//! Installation for the oci boundary.

use super::*;

impl<R: ProcessRunner> OciRuntime<'_, R> {
    pub fn ensure_disk_available(&self, required_bytes: u64) -> Result<(), OciError> {
        let required = required_bytes
            .checked_add(10_000_000_000)
            .ok_or(OciError::Capacity)?;
        if available_disk_bytes(self.data_root).map_err(|_| OciError::Capacity)? < required {
            return Err(OciError::Capacity);
        }
        Ok(())
    }

    pub fn report_preload_memory(
        &self,
        peak_bytes: u64,
        meminfo_path: &Path,
    ) -> vonk_agent_protocol::failure_evidence::FailureDiagnostics {
        let available = fs::read_to_string(meminfo_path).ok().and_then(|text| {
            text.lines().find_map(|line| {
                line.strip_prefix("MemAvailable:")?
                    .split_whitespace()
                    .next()?
                    .parse::<u64>()
                    .ok()?
                    .checked_mul(1024)
            })
        });
        // Informational estimate only. GPU driver queries can block under
        // pressure; this path needs only the host's physical memory snapshot.
        let margin = vonk_agent_protocol::host_memory_guard_policy::PRELOAD_MARGIN_BYTES;
        let warning = available.is_none_or(|bytes| bytes < peak_bytes.saturating_add(margin));
        let mut diagnostics = crate::failure_evidence::collect(
            vonk_agent_protocol::generated::AgentDiagnosticOperation::WorkloadPreloadMemory
                .as_str(),
            vonk_agent_protocol::generated::FailureDiagnosticsCategory::Capacity.as_str(),
            &[],
            &[],
        );
        diagnostics.preflight = [
            (
                "mem_available_bytes",
                available.map_or_else(
                    || {
                        vonk_agent_protocol::generated::ResourceTermProblem::Unknown
                            .as_str()
                            .into()
                    },
                    |bytes| bytes.to_string(),
                ),
            ),
            ("declared_peak_bytes", peak_bytes.to_string()),
            ("margin_bytes", margin.to_string()),
            ("below_estimated_peak", warning.to_string()),
            ("informational_only", "true".into()),
        ]
        .into_iter()
        .map(
            |(name, value)| vonk_agent_protocol::failure_evidence::FailureProperty {
                name: name.into(),
                value,
            },
        )
        .collect();
        eprintln!(
            "vonk-agent: {} available_bytes={available:?} declared_peak_bytes={peak_bytes} margin_bytes={margin} warning={warning}; informational_only=true",
            vonk_agent_protocol::generated::AgentDiagnosticOperation::WorkloadPreloadMemory
                .as_str()
        );
        diagnostics
    }

    pub fn install(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
    ) -> Result<(), OciError> {
        let encoded = serde_json::to_vec(spec)?;
        if encoded.len() > MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES {
            return Err(OciError::Artifact);
        }
        self.verify_image(spec)?;
        if recipe_content_sha256 != spec.identity.recipe_revision_sha256 {
            return Err(OciError::Artifact);
        }
        let _lock = self.lock_installation_reconciliation(installation_id)?;
        self.supersede_reconciliation_checkpoint(installation_id)?;
        self.install_unlocked(
            spec,
            installation_id,
            recipe_content_sha256,
            &mut |_, _| {},
            &|| false,
        )
    }

    pub(super) fn install_unlocked(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
        progress: &mut dyn FnMut(u64, u64),
        cancelled: &dyn Fn() -> bool,
    ) -> Result<(), OciError> {
        self.install_unlocked_controlled(
            spec,
            installation_id,
            recipe_content_sha256,
            progress,
            cancelled,
        )
    }

    fn install_unlocked_controlled(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
        progress: &mut dyn FnMut(u64, u64),
        cancelled: &dyn Fn() -> bool,
    ) -> Result<(), OciError> {
        if recipe_content_sha256.len() != 64
            || !recipe_content_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(OciError::Artifact);
        }
        self.verify_image(spec)
            .map_err(|error| install_error(FailureStage::ImageVerification, error))?;
        let encoded_spec = serde_json::to_vec(spec)
            .map_err(OciError::Json)
            .map_err(|error| install_error(FailureStage::InstallationMetadata, error))?;
        if encoded_spec.len() > MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES {
            return Err(install_error(
                FailureStage::InstallationMetadata,
                OciError::Artifact,
            ));
        }
        let installation =
            managed_path(self.data_root, "installations", installation_id).map_err(|error| {
                install_error(FailureStage::InstallationPath, OciError::Workload(error))
            })?;
        fs::create_dir_all(&installation)
            .map_err(OciError::Io)
            .map_err(|error| install_error(FailureStage::InstallationDirectory, error))?;
        fs::set_permissions(&installation, fs::Permissions::from_mode(0o700))
            .map_err(OciError::Io)
            .map_err(|error| install_error(FailureStage::InstallationDirectory, error))?;
        self.ensure_runtime_cache(installation_id)
            .map_err(|error| install_error(FailureStage::RuntimeCache, error))?;
        repair_installation_projections(&installation)?;
        super::materialization::materialize_compiled_models_controlled(
            self.data_root,
            spec,
            installation_id,
            true,
            progress,
            cancelled,
        )
        .map_err(|error| install_error(FailureStage::ModelMaterialization, error))?;
        if cancelled() {
            return Err(ProcessError::Cancelled.into());
        }
        write_installation_metadata(self.data_root, &installation, spec)
            .map_err(|error| install_error(FailureStage::InstallationMetadata, error))?;
        atomic_write(&installation, "spec.json", &encoded_spec)
            .map_err(|error| install_error(FailureStage::InstallationMetadata, error))?;
        atomic_write(
            &installation,
            "recipe-content.sha256",
            recipe_content_sha256.as_bytes(),
        )
        .map_err(|error| install_error(FailureStage::InstallationMetadata, error))?;
        File::open(&installation)
            .map_err(OciError::Io)
            .and_then(|file| file.sync_all().map_err(OciError::Io))
            .map_err(|error| install_error(FailureStage::InstallationMetadata, error))?;
        Ok(())
    }

    /// A lost install acknowledgement may be retried after the model has
    /// already consumed the admitted space. Only the exact completed,
    /// receipt-backed installation can bypass another full-copy reservation.
    pub fn install_with_space_check(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
        expected_bytes: u64,
    ) -> Result<(), OciError> {
        self.install_with_space_check_observed(
            spec,
            installation_id,
            recipe_content_sha256,
            expected_bytes,
            &mut |_, _| {},
            &|| false,
        )
    }

    /// As `install_with_space_check`, reporting `(copied, total)` model bytes
    /// while the installation's own model copies are written.
    pub fn install_with_space_check_observed(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
        expected_bytes: u64,
        progress: &mut dyn FnMut(u64, u64),
        cancelled: &dyn Fn() -> bool,
    ) -> Result<(), OciError> {
        self.install_with_space_check_controlled(
            spec,
            installation_id,
            recipe_content_sha256,
            expected_bytes,
            progress,
            cancelled,
        )
    }

    pub fn install_with_space_check_controlled(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
        _expected_bytes: u64,
        progress: &mut dyn FnMut(u64, u64),
        cancelled: &dyn Fn() -> bool,
    ) -> Result<(), OciError> {
        if cancelled() {
            return Err(ProcessError::Cancelled.into());
        }
        let encoded = serde_json::to_vec(spec)?;
        if encoded.len() > MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES {
            return Err(OciError::Artifact);
        }
        self.verify_image(spec)?;
        if recipe_content_sha256 != spec.identity.recipe_revision_sha256 {
            return Err(OciError::Artifact);
        }
        let _lock = self.lock_installation_reconciliation(installation_id)?;
        self.supersede_reconciliation_checkpoint(installation_id)?;
        if self.reuse_completed_install(spec, installation_id, recipe_content_sha256)? {
            return Ok(());
        }
        // Model files already in the shared store are linked, not written, so
        // they need no free space; only what the install must still write does.
        self.ensure_disk_available(
            unique_plan_artifacts(spec)
                .iter()
                .map(|artifact| artifact.size_bytes)
                .sum::<u64>()
                .saturating_sub(linkable_model_bytes(self.data_root, spec)),
        )?;
        self.install_unlocked_controlled(
            spec,
            installation_id,
            recipe_content_sha256,
            progress,
            cancelled,
        )
    }

    pub(super) fn reuse_completed_install(
        &self,
        spec: &CompiledExecutionPlan,
        installation_id: &str,
        recipe_content_sha256: &str,
    ) -> Result<bool, OciError> {
        self.verify_image(spec)?;
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let metadata = match fs::symlink_metadata(&installation) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
            Err(error) => return Err(error.into()),
        };
        if !metadata.file_type().is_dir()
            || metadata.file_type().is_symlink()
            || metadata.uid() != rustix::process::geteuid().as_raw()
            || metadata.mode() & 0o777 != 0o700
        {
            // Preserve the unproven entry without following it. Preparation
            // creates a new private directory from the accepted content.
            fs::rename(
                &installation,
                installation.with_extension(format!("{}.damaged", uuid::Uuid::new_v4())),
            )?;
            return Ok(false);
        }
        // Saved projections are disposable; the current accepted plan owns
        // execution authority. Reuse is decided by content receipts alone.
        let Some(receipt) = read_installation_metadata(&installation)? else {
            return Ok(false);
        };
        if !receipt_matches_plan(&receipt, spec)
            || self
                .verify_plan_materialization(installation_id, spec)
                .is_err()
        {
            return Ok(false);
        }
        let encoded_spec = serde_json::to_vec(spec)?;
        if encoded_spec.len() > MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES {
            return Err(OciError::Artifact);
        }
        self.ensure_runtime_cache(installation_id)?;
        repair_installation_projections(&installation)?;
        atomic_write(&installation, "spec.json", &encoded_spec)?;
        atomic_write(
            &installation,
            "recipe-content.sha256",
            recipe_content_sha256.as_bytes(),
        )?;
        Ok(true)
    }

    /// Materialize only the model files authorized by a compiled Controller
    /// plan. Distribution objects live under the plan-independent,
    /// content-addressed model object root; artifact-set membership remains in
    /// the typed plan and each selected path is an explicit projection.
    pub fn materialize_compiled_models(
        &self,
        plan: &CompiledExecutionPlan,
        installation_id: &str,
    ) -> Result<Vec<PathBuf>, OciError> {
        materialize_compiled_models(self.data_root, plan, installation_id)
    }

    pub fn verify_image(&self, spec: &CompiledExecutionPlan) -> Result<(), OciError> {
        spec.validate()?;
        // Runtime policy was admitted by the Controller. Local kit metadata
        // is not image-byte or signature evidence and cannot veto this plan.
        Ok(())
    }
}

fn repair_installation_projections(installation: &Path) -> Result<(), OciError> {
    for name in [
        "spec.json",
        "recipe-content.sha256",
        INSTALLATION_METADATA_FILE,
    ] {
        let path = installation.join(name);
        if let Ok(metadata) = fs::symlink_metadata(&path)
            && !metadata.file_type().is_file()
        {
            fs::rename(
                path,
                installation.join(format!("{}.damaged", uuid::Uuid::new_v4())),
            )?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests;
