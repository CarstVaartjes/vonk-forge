//! Jobs for the oci boundary.

use super::reconciliation::path_exists_without_following;
use super::*;

impl<R: ProcessRunner> OciRuntime<'_, R> {
    /// Completion and its accepted content identity are durable typed records.
    /// A missing exit account after dispatch is unknown, never permission to
    /// repeat a process whose effects may already have happened.
    pub fn retained_job_completion(
        &self,
        request: &vonk_agent_protocol::RecipeJobRunRequest,
    ) -> Result<Option<vonk_agent_protocol::RecipeJobRunResult>, OciError> {
        let scope = managed_path(self.data_root, "runs", &request.job_id.to_string())?;
        match fs::symlink_metadata(&scope) {
            Ok(metadata)
                if metadata.is_dir()
                    && !metadata.file_type().is_symlink()
                    && metadata.uid() == rustix::process::geteuid().as_raw() => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            _ => return Err(OciError::Artifact),
        }
        let intent_path = scope.join("job-request.json");
        if !path_exists_without_following(&intent_path)? {
            return Ok(None);
        }
        let retained: vonk_agent_protocol::RecipeJobRunRequest = serde_json::from_slice(
            &read_regular_file(&intent_path, MAX_COMPILED_DOCUMENT_BYTES)?,
        )?;
        // The plan digest binds exact executable content and input/output
        // contracts. Editorial revision provenance is not compared.
        if retained.plan_digest != request.plan_digest
            || retained.job_id != request.job_id
            || retained.run_id != request.run_id
            || retained.run_generation != request.run_generation
        {
            return Err(OciError::Artifact);
        }
        let receipt: vonk_agent_protocol::RecipeJobRunResult = serde_json::from_slice(
            &read_regular_file(&scope.join("job-exit.json"), MAX_COMPILED_DOCUMENT_BYTES)?,
        )?;
        receipt.validate().map_err(|_| OciError::Artifact)?;
        if receipt.job_id != request.job_id || receipt.run_id != request.run_id {
            return Err(OciError::Artifact);
        }
        Ok(Some(receipt))
    }

    pub fn persist_job_intent(
        &self,
        request: &vonk_agent_protocol::RecipeJobRunRequest,
    ) -> Result<(), OciError> {
        let scope = managed_path(self.data_root, "runs", &request.job_id.to_string())?;
        atomic_write(
            &scope,
            "job-request.json",
            &canonical_protocol_json(request).map_err(|_| OciError::Artifact)?,
        )?;
        File::open(scope)?.sync_all()?;
        Ok(())
    }

    pub fn persist_job_completion(
        &self,
        receipt: &vonk_agent_protocol::RecipeJobRunResult,
    ) -> Result<(), OciError> {
        receipt.validate().map_err(|_| OciError::Artifact)?;
        let scope = managed_path(self.data_root, "runs", &receipt.job_id.to_string())?;
        // Flush outputs before publishing the process exit account; a retry
        // observes this account and uploads rather than starting the process.
        let outputs = self.job_output_root(&receipt.job_id.to_string())?;
        for entry in fs::read_dir(&outputs)? {
            let entry = entry?;
            if entry.file_type()?.is_file() {
                OpenOptions::new()
                    .read(true)
                    .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
                    .open(entry.path())?
                    .sync_all()?;
            }
        }
        File::open(outputs)?.sync_all()?;
        atomic_write(
            &scope,
            "job-exit.json",
            &canonical_protocol_json(receipt).map_err(|_| OciError::Artifact)?,
        )?;
        File::open(scope)?.sync_all()?;
        Ok(())
    }

    pub fn job_input_destination(&self, run_id: &str, name: &str) -> Result<PathBuf, OciError> {
        if name.is_empty()
            || name == "manifest.json"
            || name.len() > 128
            || !name.as_bytes()[0].is_ascii_alphanumeric()
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        {
            return Err(OciError::Artifact);
        }
        let state = managed_path(self.data_root, "runs", run_id)?;
        fs::create_dir_all(&state)?;
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
        let inputs = state.join("inputs");
        fs::create_dir_all(&inputs)?;
        fs::set_permissions(&inputs, fs::Permissions::from_mode(0o700))?;
        let destination = inputs.join(name);
        if fs::symlink_metadata(&destination).is_ok() {
            return Err(OciError::Artifact);
        }
        Ok(destination)
    }

    pub fn write_job_input_manifest(
        &self,
        run_id: &str,
        names: &[String],
        bytes: &[u8],
        expected_sha256: &str,
    ) -> Result<(), OciError> {
        self.verify_job_inputs(run_id, names)?;
        if bytes.len() > 64 * 1024
            || expected_sha256.len() != 64
            || hex::encode(Sha256::digest(bytes)) != expected_sha256
        {
            return Err(OciError::Artifact);
        }
        let inputs = managed_path(self.data_root, "runs", run_id)?.join("inputs");
        let manifest = inputs.join("manifest.json");
        let mut output = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o400)
            .open(&manifest)?;
        output.write_all(bytes)?;
        output.sync_all()?;
        fs::set_permissions(&manifest, fs::Permissions::from_mode(0o400))?;
        File::open(inputs)?.sync_all()?;
        Ok(())
    }

    pub fn verify_job_inputs(&self, run_id: &str, names: &[String]) -> Result<(), OciError> {
        let state = managed_path(self.data_root, "runs", run_id)?;
        fs::create_dir_all(&state)?;
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
        let inputs = state.join("inputs");
        fs::create_dir_all(&inputs)?;
        fs::set_permissions(&inputs, fs::Permissions::from_mode(0o700))?;
        let metadata = fs::symlink_metadata(&inputs)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let mut observed = fs::read_dir(inputs)?
            .map(|entry| {
                let entry = entry?;
                let file_type = entry.file_type()?;
                if !file_type.is_file() || file_type.is_symlink() {
                    return Err(OciError::Artifact);
                }
                entry
                    .file_name()
                    .into_string()
                    .map_err(|_| OciError::Artifact)
            })
            .collect::<Result<Vec<_>, _>>()?;
        observed.sort();
        if observed != names {
            return Err(OciError::Artifact);
        }
        Ok(())
    }

    pub fn job_output_root(&self, run_id: &str) -> Result<PathBuf, OciError> {
        let outputs = managed_path(self.data_root, "runs", run_id)?.join("outputs");
        let metadata = fs::symlink_metadata(&outputs)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        Ok(outputs)
    }

    pub fn cleanup_job_scope(&self, job_id: &str) -> Result<(), OciError> {
        if !canonical_uuid(job_id) {
            return Err(OciError::Artifact);
        }
        for path in [
            self.data_root.join("runs").join(job_id),
            self.data_root.join("run-metadata").join(job_id),
        ] {
            match fs::symlink_metadata(&path) {
                Ok(metadata) => {
                    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
                        // Isolate the exact disposable scope name; never follow
                        // it into a foreign target or leave it vetoing staging.
                        fs::rename(
                            &path,
                            path.with_extension(format!("{}.damaged", uuid::Uuid::new_v4())),
                        )?;
                    } else {
                        fs::remove_dir_all(&path)?;
                    }
                    if let Some(parent) = path.parent() {
                        File::open(parent)?.sync_all()?;
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(error.into()),
            }
        }
        Ok(())
    }

    pub fn job_output_state(&self, run_id: &str) -> Result<JobOutputState, OciError> {
        let outputs = managed_path(self.data_root, "runs", run_id)?.join("outputs");
        let metadata = fs::symlink_metadata(&outputs)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let mut files = BTreeMap::new();
        let mut total_bytes = 0;
        visit_files(&outputs, &outputs, &mut files, &mut total_bytes)?;
        let manifest_sha256 = hex::encode(Sha256::digest(serde_json::to_vec(&files)?));
        Ok(JobOutputState {
            output_path: "/outputs",
            file_count: files.len(),
            total_bytes,
            manifest_sha256,
        })
    }
}
