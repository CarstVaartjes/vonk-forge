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
        let _lock = self.lock_installation_reconciliation(installation_id)?;
        self.refuse_reconciled_installation(installation_id)?;
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
        if recipe_content_sha256.len() != 64
            || !recipe_content_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(OciError::Artifact);
        }
        self.verify_image(spec)
            .map_err(|error| install_error(FailureStage::ImageVerification, error))?;
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
        materialize_compiled_models_observed(
            self.data_root,
            spec,
            installation_id,
            progress,
            cancelled,
        )
        .map_err(|error| install_error(FailureStage::ModelMaterialization, error))?;
        let encoded_spec = serde_json::to_vec(spec)
            .map_err(OciError::Json)
            .map_err(|error| install_error(FailureStage::InstallationMetadata, error))?;
        if encoded_spec.len() > MAX_COMPILED_EXECUTION_PLAN_SPEC_BYTES {
            return Err(install_error(
                FailureStage::InstallationMetadata,
                OciError::Artifact,
            ));
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
        let _lock = self.lock_installation_reconciliation(installation_id)?;
        self.refuse_reconciled_installation(installation_id)?;
        if self.reuse_completed_install(spec, installation_id, recipe_content_sha256)? {
            return Ok(());
        }
        // Model files already in the shared store are linked, not written, so
        // they need no free space; only what the install must still write does.
        self.ensure_disk_available(
            expected_bytes.saturating_sub(linkable_model_bytes(self.data_root, spec)),
        )?;
        self.install_unlocked(
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
            return Err(OciError::Artifact);
        }
        let mut incomplete = false;
        for name in [
            "spec.json",
            "recipe-content.sha256",
            INSTALLATION_METADATA_FILE,
        ] {
            match fs::symlink_metadata(installation.join(name)) {
                Ok(metadata) if trusted_receipt_metadata(&metadata) => match name {
                    "spec.json" if self.load_spec(installation_id)? == *spec => {}
                    "recipe-content.sha256"
                        if self.recipe_digest(installation_id)? == recipe_content_sha256 => {}
                    INSTALLATION_METADATA_FILE
                        if read_installation_metadata(&installation)?
                            .is_some_and(|receipt| receipt_matches_plan(&receipt, spec)) => {}
                    _ => return Err(OciError::Artifact),
                },
                Ok(_) => return Err(OciError::Artifact),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => incomplete = true,
                Err(error) => return Err(error.into()),
            }
        }
        if incomplete {
            return Ok(false);
        }
        let cache = installation.join("runtime-cache");
        let cache_metadata = fs::symlink_metadata(&cache)?;
        if !cache_metadata.file_type().is_dir()
            || cache_metadata.file_type().is_symlink()
            || cache_metadata.uid() != rustix::process::geteuid().as_raw()
            || cache_metadata.mode() & 0o777 != 0o700
        {
            return Err(OciError::Artifact);
        }
        self.verify_installation(installation_id)?;
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
        let policy = runtime_policy()?;
        // Compiled images are always linux/arm64 and the current runtime
        // interface; the policy must agree.
        if policy.runtime_interface != "vonk.runtime.v1"
            || policy.architecture != "linux/arm64"
            || policy.required_image_label.name != "ai.vonkforge.runtime-interface"
            || spec.runtime_image.runtime_interface_label != policy.required_image_label.value
        {
            return Err(OciError::ImageDigest);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests;
