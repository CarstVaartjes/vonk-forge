use std::{
    collections::BTreeSet,
    net::IpAddr,
    path::{Component, Path, PathBuf},
};

use crate::compiled_execution_plan::{
    CompiledEnvironmentEntry, CompiledExecutionPlan, CompiledModelArtifact,
};

use super::{CompiledOciError, CompiledOciPaths};

pub(super) fn validate_paths(paths: &CompiledOciPaths) -> Result<(), CompiledOciError> {
    let mut all = vec![
        ("model root", &paths.model_root),
        ("output root", &paths.output_root),
        ("cache root", &paths.cache_root),
        ("runtime spec", &paths.runtime_spec),
    ];
    if let Some(input_root) = &paths.input_root {
        all.push(("input root", input_root));
    }
    for (_, path) in &all {
        if !safe_host_path(path) {
            return Err(CompiledOciError::Invalid("unsafe host path"));
        }
    }
    for (index, (_, left)) in all.iter().enumerate() {
        for (_, right) in all.iter().skip(index + 1) {
            if path_prefix_conflict(left, right) {
                return Err(CompiledOciError::Invalid("conflicting host paths"));
            }
        }
    }
    Ok(())
}

pub(super) fn validate_security(plan: &CompiledExecutionPlan) -> Result<(), CompiledOciError> {
    let placement = &plan.runtime.placement;
    let native_fabric = placement.world_size > 1 && placement.master_port.is_some();
    let expected_network_mode = if native_fabric {
        "host"
    } else if placement.endpoint_address.is_some() {
        "bridge"
    } else {
        "none"
    };
    if native_fabric
        && (plan.topology.node_count < 2
            || placement.local_address.is_none()
            || placement.master_address.is_none()
            || plan.endpoint.is_none()
            || !plan.security.gpu)
    {
        return Err(CompiledOciError::Invalid(
            "native fabric placement is incomplete",
        ));
    }
    if plan.security.network_mode.as_str() != expected_network_mode {
        return Err(CompiledOciError::Invalid("security override"));
    }
    Ok(())
}

pub(super) fn ordered_environment(
    plan: &CompiledExecutionPlan,
) -> Result<Vec<CompiledEnvironmentEntry>, CompiledOciError> {
    let mut names = BTreeSet::<String>::new();
    let mut environment = Vec::with_capacity(plan.runtime.env.len() + 12);
    for entry in &plan.runtime.env {
        if !names.insert(entry.name.clone()) {
            return Err(CompiledOciError::Invalid("duplicate environment entry"));
        }
        environment.push(entry.clone());
    }
    let mut add = |name: &str, value: String| -> Result<(), CompiledOciError> {
        if let Some(existing) = environment.iter().find(|entry| entry.name == name) {
            if existing.value != value {
                return Err(CompiledOciError::Invalid(
                    "conflicting platform environment",
                ));
            }
            return Ok(());
        }
        names.insert(name.to_owned());
        environment.push(CompiledEnvironmentEntry {
            name: name.to_owned(),
            value,
        });
        Ok(())
    };
    let placement = &plan.runtime.placement;
    add("VONK_RANK", placement.rank.to_string())?;
    add("VONK_WORLD_SIZE", placement.world_size.to_string())?;
    add("VONK_RUNTIME_SPEC", "/run/vonk/runtime.json".to_owned())?;
    add("VONK_MODEL_ROOT", "/models".to_owned())?;
    if let Some(address) = placement.local_address {
        add("VONK_LOCAL_ADDR", address.to_string())?;
    }
    if let Some(address) = placement.master_address {
        add("VONK_MASTER_ADDR", address.to_string())?;
    }
    if let Some(port) = placement.master_port {
        add("VONK_MASTER_PORT", port.to_string())?;
    }
    if plan.runtime.placement.endpoint_address.is_some()
        && let Some(endpoint) = &plan.endpoint
    {
        add("VONK_LISTEN_HOST", "0.0.0.0".to_owned())?;
        add("VONK_LISTEN_PORT", endpoint.port.to_string())?;
    }
    if let Some(job) = &plan.job {
        add("VONK_INPUT_ROOT", "/inputs".to_owned())?;
        add("VONK_OUTPUT_ROOT", "/outputs".to_owned())?;
        add("VONK_JOB_TIMEOUT_SECONDS", job.timeout_seconds.to_string())?;
    }
    Ok(environment)
}

pub(super) fn publications(plan: &CompiledExecutionPlan) -> Result<Vec<String>, CompiledOciError> {
    if plan.security.network_mode.as_str() == "host" {
        return Ok(Vec::new());
    }
    let placement = &plan.runtime.placement;
    let mut result = Vec::new();
    if let (Some(endpoint), Some(endpoint_address), Some(port)) =
        (&plan.endpoint, placement.endpoint_address, placement.port)
    {
        let first = match endpoint_address {
            IpAddr::V4(address) => format!("{address}:{port}:{}", endpoint.port),
            IpAddr::V6(address) => format!("[{address}]:{port}:{}", endpoint.port),
        };
        result.push(first);
    }
    if placement.rank == 0
        && let (Some(master), Some(master_port)) = (placement.master_address, placement.master_port)
    {
        let publication = match master {
            IpAddr::V4(address) => format!("{address}:{master_port}:{master_port}"),
            IpAddr::V6(address) => format!("[{address}]:{master_port}:{master_port}"),
        };
        if !result.contains(&publication) {
            result.push(publication);
        }
    }
    Ok(result)
}

pub(super) fn model_source(
    paths: &CompiledOciPaths,
    artifact: &CompiledModelArtifact,
) -> Result<PathBuf, CompiledOciError> {
    let source = paths
        .model_root
        .join(&artifact.selection_id)
        .join(&artifact.path);
    if !source.starts_with(&paths.model_root) {
        return Err(CompiledOciError::Invalid(
            "model path escaped selection root",
        ));
    }
    Ok(source)
}

pub(super) fn model_target(artifact: &CompiledModelArtifact) -> Result<String, CompiledOciError> {
    if !artifact.mount.target.starts_with("/models")
        || artifact.mount.target.contains("//")
        || artifact
            .mount
            .target
            .split('/')
            .any(|part| part == ".." || part == ".")
    {
        return Err(CompiledOciError::Invalid("unsafe model mount target"));
    }
    let target = format!(
        "{}/{}",
        artifact.mount.target.trim_end_matches('/'),
        artifact.path
    );
    if target.contains("//") || target.split('/').any(|part| part == ".." || part == ".") {
        return Err(CompiledOciError::Invalid("unsafe model mount target"));
    }
    Ok(target)
}

fn safe_host_path(path: &Path) -> bool {
    path.is_absolute()
        && path
            .components()
            .all(|component| matches!(component, Component::RootDir | Component::Normal(_)))
        && !path.to_string_lossy().contains(',')
}

fn path_prefix_conflict(left: &Path, right: &Path) -> bool {
    left == right || left.starts_with(right) || right.starts_with(left)
}
