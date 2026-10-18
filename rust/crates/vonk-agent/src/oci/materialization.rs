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
    materialize_compiled_models_observed(data_root, plan, installation_id, &mut |_, _| {}, &|| {
        false
    })
}

/// Put every planned model file in the installation, reporting `(done, total)`
/// bytes. A file already in place counts as done at once.
///
/// The shared distribution object is the one trusted copy of a model file, so
/// a new installation hard-links it: no bytes move and no extra disk is used,
/// however large the model or however many installations already hold it. An
/// installation that already holds a receipted private file is repaired by the
/// normal relinker. New private copies are never created: a hardlink failure
/// reports the managed layout fault and preserves the source for recovery.
pub(super) fn materialize_compiled_models_observed(
    data_root: &Path,
    plan: &CompiledExecutionPlan,
    installation_id: &str,
    progress: &mut dyn FnMut(u64, u64),
    cancelled: &dyn Fn() -> bool,
) -> Result<Vec<PathBuf>, OciError> {
    materialize_compiled_models_with(data_root, plan, installation_id, true, progress, cancelled)
}

/// `link` is false only to inject EXDEV at the hardlink boundary in tests.
pub(super) fn materialize_compiled_models_with(
    data_root: &Path,
    plan: &CompiledExecutionPlan,
    installation_id: &str,
    link: bool,
    progress: &mut dyn FnMut(u64, u64),
    cancelled: &dyn Fn() -> bool,
) -> Result<Vec<PathBuf>, OciError> {
    materialize_compiled_models_controlled(
        data_root,
        plan,
        installation_id,
        link,
        progress,
        cancelled,
    )
}

pub(super) fn materialize_compiled_models_controlled(
    data_root: &Path,
    plan: &CompiledExecutionPlan,
    installation_id: &str,
    link: bool,
    progress: &mut dyn FnMut(u64, u64),
    cancelled: &dyn Fn() -> bool,
) -> Result<Vec<PathBuf>, OciError> {
    if !data_root.is_absolute() {
        return Err(OciError::Artifact);
    }
    plan.validate()?;
    let installation = managed_path(data_root, "installations", installation_id)?;
    let destination_root = installation.join("models");
    repair_managed_model_directory(&installation)?;
    repair_managed_model_directory(&destination_root)?;

    let model_root = data_root.join("distribution").join("models");
    let model_metadata = fs::symlink_metadata(&model_root)?;
    if model_metadata.file_type().is_symlink() || !model_metadata.is_dir() {
        return Err(OciError::Artifact);
    }
    let mut materialized = Vec::with_capacity(plan.artifacts.len());
    let mut physical_by_path: BTreeMap<(String, String), PhysicalMaterialization> = BTreeMap::new();
    let total_bytes: u64 = unique_plan_artifacts(plan)
        .iter()
        .map(|artifact| artifact.size_bytes)
        .sum();
    let mut done_bytes = 0_u64;
    progress(0, total_bytes);
    for artifact in &plan.artifacts {
        check_materialization_cancelled(cancelled)?;
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
        let mut directory = destination_root.clone();
        for component in parent
            .strip_prefix(&destination_root)
            .map_err(|_| OciError::Artifact)?
            .components()
        {
            check_materialization_cancelled(cancelled)?;
            directory.push(component.as_os_str());
            repair_managed_model_directory(&directory)?;
        }
        let source = model_root.join(&artifact.sha256);
        if !source.starts_with(&model_root) {
            return Err(OciError::Artifact);
        }
        let shared = shared_store_inode(data_root, &artifact.sha256);
        if let Ok(metadata) = fs::symlink_metadata(&destination) {
            if metadata.file_type().is_symlink() || !metadata.file_type().is_file() {
                // Retire only the exact managed name, without touching targets
                // or traversing an unmatched directory. The source is refetched
                // under the current assignment before materialization.
                fs::rename(
                    &destination,
                    destination.with_extension(format!("{}.damaged", uuid::Uuid::new_v4())),
                )?;
            }
            // A file that already is the shared object needs no receipt: it is
            // the one trusted inode, and nothing is left to place.
            let already_shared = shared == Some((metadata.dev(), metadata.ino()));
            if already_shared
                && open_trusted_model_file(&destination, artifact.size_bytes, shared).is_ok()
            {
                check_materialization_cancelled(cancelled)?;
                physical_by_path.insert(physical_key, (destination.clone(), physical));
                materialized.push(destination);
                done_bytes += artifact.size_bytes;
                progress(done_bytes, total_bytes);
                continue;
            }
        }
        let (_source_file, source_metadata) =
            open_trusted_model_file(&source, artifact.size_bytes, shared)?;
        let temporary = destination.with_extension(format!(
            "{}.{}.{}.partial",
            std::process::id(),
            uuid::Uuid::new_v4(),
            artifact.file_id
        ));
        let linked = if link {
            fs::hard_link(&source, &temporary)
        } else {
            Err(std::io::Error::from_raw_os_error(
                rustix::io::Errno::XDEV.raw_os_error(),
            ))
        };
        linked.map_err(|error| OciError::ModelStoreLayout {
            source_path: source
                .strip_prefix(data_root)
                .unwrap_or(&source)
                .to_path_buf(),
            destination: destination
                .strip_prefix(data_root)
                .unwrap_or(&destination)
                .to_path_buf(),
            errno: error.raw_os_error(),
        })?;
        {
            let mut temporary_guard = TemporaryArtifact::new(temporary.clone());
            // The name just linked must be the object opened above, still a
            // trusted model file, before it replaces anything.
            let (_, linked) = open_trusted_model_file(&temporary, artifact.size_bytes, shared)?;
            if linked.dev() != source_metadata.dev() || linked.ino() != source_metadata.ino() {
                return Err(OciError::Artifact);
            }
            check_materialization_cancelled(cancelled)?;
            fs::rename(&temporary, &destination)?;
            temporary_guard.retain();
            sync_parent(parent)?;
            physical_by_path.insert(physical_key, (destination.clone(), physical));
            materialized.push(destination);
            done_bytes += artifact.size_bytes;
            progress(done_bytes, total_bytes);
        }
    }
    File::open(&destination_root)?.sync_all()?;
    Ok(materialized)
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod recovery_tests;

fn check_materialization_cancelled(cancelled: &dyn Fn() -> bool) -> Result<(), OciError> {
    if cancelled() {
        return Err(ProcessError::Cancelled.into());
    }
    Ok(())
}

/// Reconstruct only a managed projection name; never follow a stored symlink.
fn repair_managed_model_directory(path: &Path) -> Result<(), OciError> {
    match fs::symlink_metadata(path) {
        Ok(metadata)
            if metadata.is_dir()
                && !metadata.file_type().is_symlink()
                && metadata.uid() == rustix::process::geteuid().as_raw() =>
        {
            return Ok(());
        }
        Ok(_) => fs::rename(
            path,
            path.with_extension(format!("{}.damaged", uuid::Uuid::new_v4())),
        )?,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    fs::create_dir_all(path)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    sync_parent(path.parent().ok_or(OciError::Artifact)?)?;
    Ok(())
}
