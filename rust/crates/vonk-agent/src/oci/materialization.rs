//! Materialization for the oci boundary.

use super::*;

pub(super) struct TemporaryArtifact {
    path: PathBuf,
    retained: bool,
}

impl TemporaryArtifact {
    fn new(path: PathBuf) -> Self {
        Self {
            path,
            retained: false,
        }
    }

    fn retain(&mut self) {
        self.retained = true;
    }
}

impl Drop for TemporaryArtifact {
    fn drop(&mut self) {
        if !self.retained {
            let _ = fs::remove_file(&self.path);
        }
    }
}

pub(super) fn materialize_compiled_models(
    data_root: &Path,
    plan: &CompiledExecutionPlan,
    installation_id: &str,
) -> Result<Vec<PathBuf>, OciError> {
    materialize_compiled_models_observed(data_root, plan, installation_id, &mut |_, _| {})
}

/// Bytes between two progress reports while one model file is copied.
pub(super) const MATERIALIZE_PROGRESS_STEP: u64 = 64 * 1024 * 1024;

/// Put every planned model file in the installation, reporting `(done, total)`
/// bytes. A file already in place counts as done at once.
///
/// The shared distribution object is the one trusted copy of a model file, so
/// a new installation hard-links it: no bytes move and no extra disk is used,
/// however large the model or however many installations already hold it. An
/// installation that already holds a private copy, from before files were
/// shared, keeps it until the installation goes. Where a link cannot be made
/// (another filesystem, the link limit) the file is copied instead.
pub(super) fn materialize_compiled_models_observed(
    data_root: &Path,
    plan: &CompiledExecutionPlan,
    installation_id: &str,
    progress: &mut dyn FnMut(u64, u64),
) -> Result<Vec<PathBuf>, OciError> {
    materialize_compiled_models_with(data_root, plan, installation_id, true, progress)
}

/// `link` is false only where a test needs the copy fallback on a filesystem
/// that would allow the link.
pub(super) fn materialize_compiled_models_with(
    data_root: &Path,
    plan: &CompiledExecutionPlan,
    installation_id: &str,
    link: bool,
    progress: &mut dyn FnMut(u64, u64),
) -> Result<Vec<PathBuf>, OciError> {
    if !data_root.is_absolute() {
        return Err(OciError::Artifact);
    }
    plan.validate()?;
    let installation = managed_path(data_root, "installations", installation_id)?;
    let destination_root = installation.join("models");
    fs::create_dir_all(&installation)?;
    fs::set_permissions(&installation, fs::Permissions::from_mode(0o700))?;
    fs::create_dir_all(&destination_root)?;
    fs::set_permissions(&destination_root, fs::Permissions::from_mode(0o700))?;

    let model_root = data_root.join("distribution").join("models");
    let model_metadata = fs::symlink_metadata(&model_root)?;
    if model_metadata.file_type().is_symlink() || !model_metadata.is_dir() {
        return Err(OciError::Artifact);
    }
    let receipt_index = read_installation_metadata(&installation)?
        .filter(|receipt| receipt_matches_plan(receipt, plan))
        .map(|receipt| {
            receipt
                .entries
                .into_iter()
                .map(|entry| ((entry.selection_id.clone(), entry.path.clone()), entry))
                .collect::<BTreeMap<_, _>>()
        });
    let mut materialized = Vec::with_capacity(plan.artifacts.len());
    let mut physical_by_path: BTreeMap<(String, String), PhysicalMaterialization> = BTreeMap::new();
    let total_bytes: u64 = unique_plan_artifacts(plan)
        .iter()
        .map(|artifact| artifact.size_bytes)
        .sum();
    let mut done_bytes = 0_u64;
    progress(0, total_bytes);
    for artifact in &plan.artifacts {
        let physical_key = (artifact.selection_id.clone(), artifact.path.clone());
        let destination = destination_root
            .join(&artifact.selection_id)
            .join(&artifact.path);
        let physical = (
            artifact.file_id.clone(),
            artifact.sha256.clone(),
            artifact.size_bytes,
            artifact.model.publisher.clone(),
            artifact.model.slug.clone(),
            artifact.model.content_sha256.clone(),
        );
        if let Some((_, previous)) = physical_by_path.get(&physical_key) {
            if previous != &physical {
                return Err(OciError::Workload(WorkloadError::Invalid(
                    "compiled model artifact physical identity",
                )));
            }
            // The workload validator proved this is the same receipt-bound
            // physical object. Its first projection performed the only source
            // and destination verification; this projection only adds a
            // second OCI mount intent.
            continue;
        }
        if !destination.starts_with(&destination_root) {
            return Err(OciError::Artifact);
        }
        let parent = destination.parent().ok_or(OciError::Artifact)?;
        fs::create_dir_all(parent)?;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
        let source = model_root.join(&artifact.sha256);
        if !source.starts_with(&model_root) {
            return Err(OciError::Artifact);
        }
        let shared = shared_store_inode(data_root, &artifact.sha256);
        if let Ok(metadata) = fs::symlink_metadata(&destination) {
            if metadata.file_type().is_symlink() || !metadata.file_type().is_file() {
                return Err(OciError::Artifact);
            }
            let reusable = receipt_index.as_ref().and_then(|index| {
                index
                    .get(&(artifact.selection_id.clone(), artifact.path.clone()))
                    .filter(|entry| metadata_matches_receipt(&metadata, entry))
            });
            // A file that already is the shared object needs no receipt: it is
            // the one trusted inode, and nothing is left to place.
            let already_shared = shared == Some((metadata.dev(), metadata.ino()));
            if reusable.is_some() || already_shared {
                let (_, opened_metadata) =
                    open_trusted_model_file(&destination, artifact.size_bytes, shared)?;
                if already_shared
                    || reusable
                        .is_some_and(|entry| metadata_matches_receipt(&opened_metadata, entry))
                {
                    physical_by_path.insert(physical_key, (destination.clone(), physical));
                    materialized.push(destination);
                    done_bytes += artifact.size_bytes;
                    progress(done_bytes, total_bytes);
                    continue;
                }
            }
        }
        let (mut source_file, source_metadata) =
            open_trusted_model_file(&source, artifact.size_bytes, shared)?;
        let temporary = destination.with_extension(format!(
            "{}.{}.{}.partial",
            std::process::id(),
            uuid::Uuid::new_v4(),
            artifact.file_id
        ));
        let linked = if link {
            match fs::hard_link(&source, &temporary) {
                Ok(()) => true,
                Err(error) => {
                    eprintln!(
                        "vonk-agent: {} sha256={} bytes={} cause={error}",
                        vonk_agent_protocol::generated::AgentDiagnosticOperation::ModelMaterializationCopyFallback.as_str(),
                        artifact.sha256, artifact.size_bytes
                    );
                    false
                }
            }
        } else {
            false
        };
        if linked {
            let mut temporary_guard = TemporaryArtifact::new(temporary.clone());
            // The name just linked must be the object opened above, still a
            // trusted model file, before it replaces anything.
            let (_, linked) = open_trusted_model_file(&temporary, artifact.size_bytes, shared)?;
            if linked.dev() != source_metadata.dev() || linked.ino() != source_metadata.ino() {
                return Err(OciError::Artifact);
            }
            fs::rename(&temporary, &destination)?;
            temporary_guard.retain();
            sync_parent(parent)?;
            physical_by_path.insert(physical_key, (destination.clone(), physical));
            materialized.push(destination);
            done_bytes += artifact.size_bytes;
            progress(done_bytes, total_bytes);
            continue;
        }
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(
                (rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32,
            )
            .open(&temporary)?;
        let mut temporary_guard = TemporaryArtifact::new(temporary.clone());
        // The source is an immutable managed distribution object received
        // from the Controller over mTLS. Its bytes are not re-hashed; the
        // retained source handle, exact size and stable metadata bind this
        // copy to the object opened above.
        let mut copied = 0_u64;
        let mut reported = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        let mut remaining_bytes = artifact.size_bytes;
        while remaining_bytes > 0 {
            let wave_bytes = remaining_bytes.min(buffer.len() as u64) as usize;
            let read = source_file.read(&mut buffer[..wave_bytes])?;
            if read == 0 {
                break;
            }
            remaining_bytes -= read as u64;
            output.write_all(&buffer[..read])?;
            copied = copied.checked_add(read as u64).ok_or(OciError::Artifact)?;
            if copied > artifact.size_bytes {
                return Err(OciError::Artifact);
            }
            if copied - reported >= MATERIALIZE_PROGRESS_STEP {
                reported = copied;
                progress(done_bytes + copied, total_bytes);
            }
        }
        output.sync_all()?;
        let source_after = source_file.metadata()?;
        let output_metadata = output.metadata()?;
        if copied != artifact.size_bytes
            || !trusted_model_file(&source_file, &source_after, artifact.size_bytes, shared)
            || !metadata_stable(&source_metadata, &source_after)
            || !trusted_model_file(&output, &output_metadata, artifact.size_bytes, None)
        {
            drop(output);
            let _ = fs::remove_file(&temporary);
            return Err(OciError::Artifact);
        }
        drop(output);
        fs::rename(&temporary, &destination)?;
        temporary_guard.retain();
        sync_parent(parent)?;
        // The copy just filled the page cache with the destination, and the
        // read filled the same cache with the source object.
        // Neither is needed once this artifact is complete, and the workload
        // this installation exists for needs the memory more.
        release_page_cache(&destination)?;
        release_page_cache(&source)?;
        physical_by_path.insert(physical_key, (destination.clone(), physical));
        materialized.push(destination);
        done_bytes += artifact.size_bytes;
        progress(done_bytes, total_bytes);
    }
    File::open(&destination_root)?.sync_all()?;
    Ok(materialized)
}

#[cfg(test)]
mod tests;
