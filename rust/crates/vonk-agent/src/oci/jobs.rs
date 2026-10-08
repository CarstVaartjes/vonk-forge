//! Jobs for the oci boundary.

use super::*;

impl<R: ProcessRunner> OciRuntime<'_, R> {
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
                        return Err(OciError::Artifact);
                    }
                    fs::remove_dir_all(&path)?;
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
