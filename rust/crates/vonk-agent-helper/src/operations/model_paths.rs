//! Model paths.

use super::*;

pub(super) fn valid_model_mount(source: &Path, target: &str, roots: &ManagedRoots) -> bool {
    if target.chars().count() > MAX_COMPILED_MODEL_PATH_CHARS
        || (target != "/models"
            && (!target.starts_with("/models/")
                || target.ends_with('/')
                || !target.split('/').skip(1).all(valid_model_path_component)))
    {
        return false;
    }
    let Some(model_root) = runtime_model_root(source, roots) else {
        return false;
    };
    let relative = source.strip_prefix(model_root).ok();
    let components = relative
        .into_iter()
        .flat_map(Path::components)
        .collect::<Vec<_>>();
    let new_layout = components.len() >= 3
        && matches!(components[0], Component::Normal(value) if lower_hex(&value.to_string_lossy(), 64))
        && matches!(components[1], Component::Normal(value) if valid_artifact_id(&value.to_string_lossy()))
        && valid_model_file_path_components(&components[2..]);
    let selection_layout = components.len() >= 2
        && matches!(components[0], Component::Normal(value) if valid_artifact_id(&value.to_string_lossy()) && value != "sha256")
        && valid_model_file_path_components(&components[1..]);
    if !(new_layout || selection_layout) {
        return false;
    }
    if target == "/models" {
        return true;
    }
    target
        .strip_prefix("/models/")
        .is_some_and(|value| value.split('/').all(valid_model_path_component))
}

pub(super) fn valid_model_file_path_components(components: &[Component<'_>]) -> bool {
    let mut chars = 0_usize;
    components.iter().enumerate().all(|(index, component)| {
        let Component::Normal(value) = component else {
            return false;
        };
        let Some(value) = value.to_str() else {
            return false;
        };
        chars = chars.saturating_add(value.chars().count());
        if index > 0 {
            chars = chars.saturating_add(1);
        }
        chars <= MAX_COMPILED_MODEL_PATH_CHARS && valid_model_path_component(value)
    })
}

pub(super) fn valid_model_path_component(value: &str) -> bool {
    !value.is_empty() && !matches!(value, "." | "..") && !value.contains(['\\', '\0'])
}

pub(super) fn require_safe_model_path(
    path: &Path,
    canonical_model_root: &Path,
    required_owner_uid: Option<u32>,
) -> Result<(), OperationError> {
    let canonical_path = path
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    if !canonical_path.starts_with(canonical_model_root) {
        return Err(OperationError::UnsafePath);
    }
    let mut current = path.to_path_buf();
    let metadata = fs::symlink_metadata(&current).map_err(|_| OperationError::UnsafePath)?;
    if metadata.file_type().is_symlink()
        || !(metadata.is_file() || metadata.is_dir())
        || metadata.mode() & 0o022 != 0
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
    {
        return Err(OperationError::UnsafePath);
    }
    while let Some(parent) = current.parent() {
        let metadata = fs::symlink_metadata(parent).map_err(|_| OperationError::UnsafePath)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_dir()
            || metadata.mode() & 0o022 != 0
            || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
        {
            return Err(OperationError::UnsafePath);
        }
        if parent.canonicalize().ok().as_deref() == Some(canonical_model_root) {
            return Ok(());
        }
        current = parent.to_path_buf();
    }
    Err(OperationError::UnsafePath)
}

pub(super) fn collect_model_tree(
    path: &Path,
    required_owner_uid: Option<u32>,
    file_count: &mut usize,
    total_bytes: &mut u64,
) -> Result<(), OperationError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
    if metadata.file_type().is_symlink()
        || metadata.mode() & 0o022 != 0
        || required_owner_uid.is_some_and(|uid| metadata.uid() != uid)
    {
        return Err(OperationError::UnsafePath);
    }
    if metadata.is_file() {
        *file_count = file_count
            .checked_add(1)
            .ok_or(OperationError::InvalidOperation)?;
        *total_bytes = total_bytes
            .checked_add(metadata.len())
            .ok_or(OperationError::InvalidOperation)?;
        return Ok(());
    }
    if !metadata.is_dir() {
        return Err(OperationError::UnsafePath);
    }
    let mut entries = fs::read_dir(path)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(fs::DirEntry::file_name);
    for entry in entries {
        collect_model_tree(&entry.path(), required_owner_uid, file_count, total_bytes)?;
    }
    Ok(())
}

pub(super) fn canonical_model_root(
    roots: &ManagedRoots,
    models: &[PathBuf],
    agent_data_owner_uid: Option<u32>,
) -> Result<PathBuf, OperationError> {
    let agent_data = &roots.agent_data;
    let installations = agent_data.join("installations");
    let model_root = models
        .first()
        .and_then(|path| runtime_model_root(path, roots))
        .ok_or(OperationError::UnsafePath)?;
    if models
        .iter()
        .any(|path| runtime_model_root(path, roots).as_deref() != Some(model_root.as_path()))
    {
        return Err(OperationError::UnsafePath);
    }
    let installation = model_root.parent().ok_or(OperationError::UnsafePath)?;
    for path in [
        agent_data.as_path(),
        installations.as_path(),
        installation,
        model_root.as_path(),
    ] {
        require_safe_directory(path, agent_data_owner_uid)?;
    }
    let canonical_agent_data = agent_data
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_installations = installations
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_installation = installation
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_models = model_root
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    if canonical_installations.parent() != Some(canonical_agent_data.as_path())
        || canonical_installation.parent() != Some(canonical_installations.as_path())
        || canonical_models.parent() != Some(canonical_installation.as_path())
    {
        return Err(OperationError::UnsafePath);
    }
    Ok(canonical_models)
}

pub(super) fn runtime_model_root(source: &Path, roots: &ManagedRoots) -> Option<PathBuf> {
    let installations = roots.agent_data.join("installations");
    let relative = source.strip_prefix(&installations).ok()?;
    let mut components = relative.components();
    let installation = match components.next()? {
        Component::Normal(value) if valid_artifact_id(&value.to_string_lossy()) => value,
        _ => return None,
    };
    if !matches!(components.next(), Some(Component::Normal(value)) if value == "models") {
        return None;
    }
    Some(installations.join(installation).join("models"))
}

#[cfg(test)]
mod tests;
