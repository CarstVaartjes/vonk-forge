//! Validation.

use super::*;

#[cfg(test)]
pub(super) fn validate_docker_run(
    arguments: &[String],
    roots: &ManagedRoots,
    agent_data_owner_uid: Option<u32>,
) -> Result<ValidatedDockerRun, OperationError> {
    validate_docker_run_with_archive(arguments, roots, agent_data_owner_uid, None, None)
}

pub(super) fn validate_docker_run_with_archive(
    arguments: &[String],
    roots: &ManagedRoots,
    agent_data_owner_uid: Option<u32>,
    archive_sha256: Option<&str>,
    registry_index_digest: Option<&str>,
) -> Result<ValidatedDockerRun, OperationError> {
    if arguments.first().map(String::as_str) != Some("run") {
        return Err(OperationError::InvalidOperation);
    }
    let mut index = 1;
    let mut detach = false;
    let mut remove = false;
    let mut name: Option<String> = None;
    let mut entrypoint: Option<String> = None;
    let mut restart = false;
    let mut read_only = false;
    let mut temporary_filesystem = false;
    let mut init = false;
    let mut pull_never = false;
    let mut local_logging = false;
    let mut log_max_size = false;
    let mut log_max_file = false;
    let mut cap_drop = false;
    let mut no_new_privileges = false;
    let mut network: Option<&str> = None;
    let mut infiniband = false;
    let mut memlock = false;
    let mut stack = false;
    let mut pids = false;
    let mut memory: Option<u64> = None;
    let mut memory_swap: Option<u64> = None;
    let mut shm_size: Option<u64> = None;
    let mut user: Option<(u32, Option<u32>)> = None;
    let mut publishes = 0_usize;
    let mut published_ports = BTreeSet::new();
    let mut published_host_ports = Vec::new();
    let mut environments = 0_usize;
    let mut listen_port = None;
    let mut master_port = None;
    let mut rank = None;
    let mut world_size = None;
    let mut local_address = None;
    let mut master_address = None;
    let mut caller_fabric_environment = false;
    let mut job_timeout_seconds = None;
    let mut gpu = false;
    let mut home = false;
    let mut xdg_cache_home = false;
    let mut tmpdir = false;
    let mut models = Vec::new();
    let mut model_sources = BTreeSet::new();
    let mut model_targets = BTreeSet::new();
    let mut outputs = None;
    let mut cache_root = None;
    let mut inputs = None;
    let mut runtime_contract = None;

    while index < arguments.len() {
        let flag = &arguments[index];
        if !flag.starts_with('-') {
            break;
        }
        match flag.as_str() {
            "--detach" if !detach => detach = true,
            "--rm" if !remove => remove = true,
            "--read-only" if !read_only => read_only = true,
            "--tmpfs" if !temporary_filesystem => {
                index += 1;
                if arguments.get(index).map(String::as_str)
                    != Some("/tmp:rw,nosuid,nodev,mode=1777,size=1073741824")
                {
                    return Err(OperationError::InvalidOperation);
                }
                temporary_filesystem = true;
            }
            "--init" if !init => init = true,
            "--pull" if !pull_never => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("never") {
                    return Err(OperationError::InvalidOperation);
                }
                pull_never = true;
            }
            "--log-driver" if !local_logging => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("local") {
                    return Err(OperationError::InvalidOperation);
                }
                local_logging = true;
            }
            "--log-opt" if !log_max_size || !log_max_file => {
                index += 1;
                match arguments.get(index).map(String::as_str) {
                    Some("max-size=10m") if !log_max_size => log_max_size = true,
                    Some("max-file=3") if !log_max_file => log_max_file = true,
                    _ => return Err(OperationError::InvalidOperation),
                }
            }
            "--cap-drop=ALL" if !cap_drop => cap_drop = true,
            "--security-opt=no-new-privileges" if !no_new_privileges => no_new_privileges = true,
            "--name" if name.is_none() => {
                index += 1;
                let value = arguments
                    .get(index)
                    .ok_or(OperationError::InvalidOperation)?;
                let run_id = value
                    .strip_prefix("vonk-")
                    .ok_or(OperationError::InvalidOperation)?;
                if uuid::Uuid::parse_str(run_id)
                    .ok()
                    .map(|value| value.to_string())
                    != Some(run_id.to_owned())
                {
                    return Err(OperationError::InvalidOperation);
                }
                name = Some(run_id.to_owned());
            }
            "--entrypoint" if entrypoint.is_none() => {
                index += 1;
                let value = arguments
                    .get(index)
                    .filter(|value| valid_entrypoint(value))
                    .ok_or(OperationError::InvalidOperation)?;
                entrypoint = Some(value.clone());
            }
            "--restart" if !restart => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("no") {
                    return Err(OperationError::InvalidOperation);
                }
                restart = true;
            }
            "--network" if network.is_none() => {
                index += 1;
                network = match arguments.get(index).map(String::as_str) {
                    Some("none") => Some("none"),
                    Some("bridge") => Some("bridge"),
                    Some("host") => Some("host"),
                    _ => return Err(OperationError::InvalidOperation),
                };
            }
            "--device" if !gpu || !infiniband => {
                index += 1;
                match arguments.get(index).map(String::as_str) {
                    Some("nvidia.com/gpu=all") if !gpu => gpu = true,
                    Some("/dev/infiniband:/dev/infiniband") if !infiniband => infiniband = true,
                    _ => return Err(OperationError::InvalidOperation),
                }
            }
            "--ulimit" if !memlock || !stack => {
                index += 1;
                match arguments.get(index).map(String::as_str) {
                    Some("memlock=-1:-1") if !memlock => memlock = true,
                    Some("stack=67108864:67108864") if !stack => stack = true,
                    _ => return Err(OperationError::InvalidOperation),
                }
            }
            "--pids-limit" if !pids => {
                index += 1;
                if arguments.get(index).map(String::as_str) != Some("4096") {
                    return Err(OperationError::InvalidOperation);
                }
                pids = true;
            }
            "--memory" if memory.is_none() => {
                index += 1;
                let value = arguments
                    .get(index)
                    .and_then(|value| value.parse::<u64>().ok())
                    .filter(|value| (64 * 1024 * 1024..=128_000_000_000).contains(value))
                    .ok_or(OperationError::InvalidOperation)?;
                memory = Some(value);
            }
            "--memory-swap" if memory_swap.is_none() => {
                index += 1;
                memory_swap = arguments
                    .get(index)
                    .and_then(|value| value.parse::<u64>().ok());
            }
            "--shm-size" if shm_size.is_none() => {
                index += 1;
                shm_size = arguments
                    .get(index)
                    .and_then(|value| value.parse::<u64>().ok())
                    .filter(|value| (64 * 1024 * 1024..=16 * 1024 * 1024 * 1024).contains(value));
            }
            "--user" if user.is_none() => {
                index += 1;
                user = Some(parse_numeric_user(
                    arguments
                        .get(index)
                        .ok_or(OperationError::InvalidOperation)?,
                )?);
            }
            "--publish" if publishes < 2 => {
                index += 1;
                let (_, host_port, container_port) = parse_publication(
                    arguments
                        .get(index)
                        .ok_or(OperationError::InvalidOperation)?,
                )
                .ok_or(OperationError::InvalidOperation)?;
                if !published_ports.insert(container_port) {
                    return Err(OperationError::InvalidOperation);
                }
                published_host_ports.push((host_port, container_port));
                publishes += 1;
            }
            "--env" if environments < 160 => {
                index += 1;
                let value = arguments
                    .get(index)
                    .ok_or(OperationError::InvalidOperation)?;
                if !valid_environment(value) {
                    return Err(OperationError::InvalidOperation);
                }
                caller_fabric_environment |= value.split_once('=').is_some_and(|(name, _)| {
                    crate::runtime_fabric::ENVIRONMENT_NAMES.contains(&name)
                });
                if let Some(value) = value.strip_prefix("VONK_WORLD_SIZE=") {
                    // This argv member must be a decimal token, not a Serde
                    // private arbitrary-precision number object.
                    if !value
                        .strip_prefix('-')
                        .unwrap_or(value)
                        .bytes()
                        .all(|byte| byte.is_ascii_digit())
                    {
                        return Err(OperationError::InvalidOperation);
                    }
                    let parsed: Integer = parse_strict(value.as_bytes())
                        .map_err(|_| OperationError::InvalidOperation)?;
                    if parsed <= 0 || world_size.replace(parsed).is_some() {
                        return Err(OperationError::InvalidOperation);
                    }
                }
                for (name, target) in [
                    ("VONK_LOCAL_ADDR=", &mut local_address),
                    ("VONK_MASTER_ADDR=", &mut master_address),
                ] {
                    if let Some(value) = value.strip_prefix(name) {
                        let parsed = value
                            .parse::<Ipv4Addr>()
                            .map_err(|_| OperationError::InvalidOperation)?;
                        if target.replace(parsed).is_some() {
                            return Err(OperationError::InvalidOperation);
                        }
                    }
                }
                if let Some(value) = value.strip_prefix("VONK_LISTEN_PORT=") {
                    let parsed = value
                        .parse::<u16>()
                        .ok()
                        .filter(|port| (1024..=65535).contains(port))
                        .ok_or(OperationError::InvalidOperation)?;
                    if listen_port.replace(parsed).is_some() {
                        return Err(OperationError::InvalidOperation);
                    }
                }
                if let Some(value) = value.strip_prefix("VONK_MASTER_PORT=") {
                    let parsed = value
                        .parse::<u16>()
                        .ok()
                        .filter(|port| (1024..=65535).contains(port))
                        .ok_or(OperationError::InvalidOperation)?;
                    if master_port.replace(parsed).is_some() {
                        return Err(OperationError::InvalidOperation);
                    }
                }
                if let Some(value) = value.strip_prefix("VONK_RANK=") {
                    // This argv member must be a decimal token, not a Serde
                    // private arbitrary-precision number object.
                    if !value
                        .strip_prefix('-')
                        .unwrap_or(value)
                        .bytes()
                        .all(|byte| byte.is_ascii_digit())
                    {
                        return Err(OperationError::InvalidOperation);
                    }
                    let parsed: Integer = parse_strict(value.as_bytes())
                        .map_err(|_| OperationError::InvalidOperation)?;
                    if parsed < 0 || rank.replace(parsed).is_some() {
                        return Err(OperationError::InvalidOperation);
                    }
                }
                if let Some(value) = value.strip_prefix("VONK_JOB_TIMEOUT_SECONDS=") {
                    let parsed = value
                        .parse::<u16>()
                        .ok()
                        .filter(|seconds| (1..=3600).contains(seconds))
                        .ok_or(OperationError::InvalidOperation)?;
                    if job_timeout_seconds.replace(parsed).is_some() {
                        return Err(OperationError::InvalidOperation);
                    }
                }
                home |= value == "HOME=/outputs/cache/home";
                xdg_cache_home |= value == "XDG_CACHE_HOME=/outputs/cache";
                tmpdir |= value == "TMPDIR=/outputs/tmp";
                environments += 1;
            }
            "--mount" => {
                index += 1;
                let value = arguments
                    .get(index)
                    .ok_or(OperationError::InvalidOperation)?;
                let (source, target, readonly) = parse_mount(value)?;
                if readonly && valid_model_mount(&source, target, roots) {
                    if !model_sources.insert(source.clone())
                        || !model_targets.insert(target.to_owned())
                    {
                        return Err(OperationError::InvalidOperation);
                    }
                    models.push(source);
                } else if target == "/outputs"
                    && !readonly
                    && source.file_name().and_then(|value| value.to_str()) == Some("outputs")
                    && source.parent().and_then(Path::parent)
                        == Some(roots.agent_data.join("runs").as_path())
                    && outputs.is_none()
                {
                    outputs = Some(source);
                } else if target == "/outputs/cache"
                    && !readonly
                    && valid_runtime_cache_mount(&source, roots)
                    && cache_root.is_none()
                {
                    cache_root = Some(source);
                } else if target == "/inputs"
                    && readonly
                    && source.file_name().and_then(|value| value.to_str()) == Some("inputs")
                    && source.parent().and_then(Path::parent)
                        == Some(roots.agent_data.join("runs").as_path())
                    && inputs.is_none()
                {
                    inputs = Some(source);
                } else if target == "/run/vonk/runtime.json"
                    && readonly
                    && source.starts_with(roots.agent_data.join("run-metadata"))
                    && source.file_name().and_then(|value| value.to_str()) == Some("runtime.json")
                    && runtime_contract.is_none()
                {
                    runtime_contract = Some(source);
                } else {
                    return Err(OperationError::InvalidOperation);
                }
            }
            _ => return Err(OperationError::InvalidOperation),
        }
        index += 1;
    }
    let image_reference = arguments
        .get(index)
        .cloned()
        .ok_or(OperationError::InvalidOperation)?;
    let entrypoint = entrypoint.ok_or(OperationError::InvalidOperation)?;
    if arguments.get(index + 1) != Some(&entrypoint) {
        return Err(OperationError::InvalidOperation);
    }
    let (_image, embedded_digest) = parse_local_image_reference(&image_reference)?;
    let registry_index_digest = registry_index_digest.unwrap_or(embedded_digest.as_str());
    if !valid_oci_digest(registry_index_digest)
        || archive_sha256.is_some_and(|digest| !lower_hex(digest, 64))
    {
        return Err(OperationError::InvalidOperation);
    }
    let (uid, _gid) = user.ok_or(OperationError::InvalidOperation)?;
    let outputs = outputs.ok_or(OperationError::InvalidOperation)?;
    let cache_root = cache_root.ok_or(OperationError::InvalidOperation)?;
    require_runtime_directory(&cache_root, agent_data_owner_uid, uid)?;
    let runtime_contract = runtime_contract.ok_or(OperationError::InvalidOperation)?;
    let state_run_id = outputs
        .parent()
        .and_then(Path::file_name)
        .and_then(|value| value.to_str())
        .ok_or(OperationError::InvalidOperation)?;
    let named_run_id = name.as_deref();
    if !((detach && !remove && restart && named_run_id == Some(state_run_id))
        || (!detach && remove && !restart && named_run_id.is_none())
        || (!detach
            && !remove
            && restart
            && named_run_id == Some(state_run_id)
            && job_timeout_seconds.is_some()))
        || !read_only
        || !temporary_filesystem
        || !init
        || !pull_never
        || !local_logging
        || !log_max_size
        || !log_max_file
        || !cap_drop
        || !no_new_privileges
        || !valid_entrypoint(&entrypoint)
        || network.is_none()
        || !pids
        || memory.is_none()
        || memory_swap != memory
        || shm_size.is_none_or(|value| value > memory.unwrap_or_default())
        || (detach && (inputs.is_some() || job_timeout_seconds.is_some()))
        || (!detach && (inputs.is_none() || job_timeout_seconds.is_none()))
        || (network != Some("host") && (infiniband || memlock || stack))
        || !home
        || !xdg_cache_home
        || !tmpdir
        || environments == 0
        || outputs.parent().and_then(Path::parent) != Some(roots.agent_data.join("runs").as_path())
        || runtime_contract.parent().and_then(Path::parent)
            != Some(roots.agent_data.join("run-metadata").as_path())
        || runtime_contract
            .parent()
            .and_then(Path::file_name)
            .and_then(|value| value.to_str())
            != Some(state_run_id)
        || inputs.as_ref().is_some_and(|inputs| {
            inputs
                .parent()
                .and_then(Path::file_name)
                .and_then(|value| value.to_str())
                != Some(state_run_id)
        })
    {
        return Err(OperationError::InvalidOperation);
    }
    let endpoint_workload = listen_port.is_some();
    let distributed_workload = master_port.is_some();
    let bridge_workload = endpoint_workload && !distributed_workload;
    let expected_network = if distributed_workload {
        "host"
    } else if bridge_workload {
        "bridge"
    } else {
        "none"
    };
    let endpoint_published = listen_port.is_some_and(|port| published_ports.contains(&port));
    if network != Some(expected_network)
        || publishes != published_ports.len()
        || (!bridge_workload && publishes != 0)
        || (bridge_workload && (publishes != 1 || !endpoint_published))
    {
        return Err(OperationError::InvalidOperation);
    }
    let native_fabric = if distributed_workload {
        let (Some(local), Some(master), Some(port), Some(rank), Some(world_size)) =
            (local_address, master_address, master_port, rank, world_size)
        else {
            return Err(OperationError::InvalidOperation);
        };
        if !detach
            || !gpu
            || !infiniband
            || !memlock
            || !stack
            || world_size < 2
            || rank >= world_size
            || caller_fabric_environment
            || endpoint_workload != (local == master)
        {
            return Err(OperationError::InvalidOperation);
        }
        Some(NativeFabric {
            local,
            master,
            port,
        })
    } else {
        None
    };
    if models.is_empty()
        || models.len() > MAX_COMPILED_MODEL_FILES
        || models.len() > 1 && model_targets.contains("/models")
    {
        return Err(OperationError::InvalidOperation);
    }
    let canonical_model_root = canonical_model_root(roots, &models, agent_data_owner_uid)?;
    // The writable cache belongs to the same installation as the validated
    // models. Checking the leaf alone would permit another installation or
    // an installation symlink to redirect the privileged mount and ACLs.
    let cache_installation = cache_root.parent().ok_or(OperationError::UnsafePath)?;
    require_safe_directory(cache_installation, agent_data_owner_uid)?;
    let installation_id = cache_installation
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| valid_artifact_id(value))
        .ok_or(OperationError::UnsafePath)?
        .to_owned();
    let canonical_cache = cache_root
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    if canonical_cache.parent() != canonical_model_root.parent() {
        return Err(OperationError::UnsafePath);
    }
    let mut model_files = 0_usize;
    let mut model_bytes = 0_u64;
    for path in &models {
        require_safe_model_path(path, &canonical_model_root, agent_data_owner_uid)?;
        collect_model_tree(
            path,
            agent_data_owner_uid,
            &mut model_files,
            &mut model_bytes,
        )?;
        if model_files > MAX_COMPILED_MODEL_FILES || model_bytes > MAX_COMPILED_MODEL_BYTES {
            return Err(OperationError::InvalidOperation);
        }
        let canonical = path
            .canonicalize()
            .map_err(|_| OperationError::UnsafePath)?;
        if !canonical.starts_with(&canonical_model_root) {
            return Err(OperationError::UnsafePath);
        }
    }
    for path in inputs.iter().chain([&outputs, &runtime_contract]) {
        let metadata = fs::symlink_metadata(path).map_err(|_| OperationError::UnsafePath)?;
        if metadata.file_type().is_symlink()
            || !(metadata.is_dir() || metadata.is_file())
            || roots.agent_data.canonicalize().ok().is_none_or(|root| {
                path.canonicalize()
                    .ok()
                    .is_none_or(|canonical| !canonical.starts_with(root))
            })
        {
            return Err(OperationError::UnsafePath);
        }
    }
    let agent_data = roots
        .agent_data
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let runs = roots.agent_data.join("runs");
    let run_root = runs.join(state_run_id);
    let metadata_root = roots.agent_data.join("run-metadata");
    let run_metadata = metadata_root.join(state_run_id);
    for path in [&runs, &run_root, &metadata_root, &run_metadata] {
        require_safe_directory(path, agent_data_owner_uid)?;
    }
    let canonical_runs = runs
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_run = run_root
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_metadata_root = metadata_root
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    let canonical_run_metadata = run_metadata
        .canonicalize()
        .map_err(|_| OperationError::UnsafePath)?;
    if canonical_runs.parent() != Some(agent_data.as_path())
        || canonical_run.parent() != Some(canonical_runs.as_path())
        || canonical_metadata_root.parent() != Some(agent_data.as_path())
        || canonical_run_metadata.parent() != Some(canonical_metadata_root.as_path())
        || outputs
            .canonicalize()
            .ok()
            .and_then(|path| path.parent().map(Path::to_path_buf))
            != Some(canonical_run.clone())
        || inputs.as_ref().is_some_and(|path| {
            path.canonicalize()
                .ok()
                .and_then(|path| path.parent().map(Path::to_path_buf))
                != Some(canonical_run.clone())
        })
        || runtime_contract
            .canonicalize()
            .ok()
            .and_then(|path| path.parent().map(Path::to_path_buf))
            != Some(canonical_run_metadata)
    {
        return Err(OperationError::UnsafePath);
    }
    let mut compiled_arguments = arguments.to_vec();
    compiled_arguments[index] = image_reference.clone();
    let tmp_root = outputs.join("tmp").join(state_run_id);
    Ok(ValidatedDockerRun {
        local_image_reference: image_reference,
        registry_index_digest: registry_index_digest.to_owned(),
        platform_manifest_digest: embedded_digest,
        archive_sha256: archive_sha256.unwrap_or_default().to_owned(),
        arguments: compiled_arguments,
        entrypoint,
        detached: detach,
        image_index: index,
        run_id: state_run_id.to_owned(),
        installation_id,
        uid,
        models,
        inputs,
        cache_root: cache_root.clone(),
        outputs,
        cache_home: cache_root.join("home"),
        tmp_root,
        runtime_contract,
        host_endpoint_port: (network == Some("host")).then_some(listen_port).flatten(),
        published_endpoint_port: published_host_ports
            .iter()
            .find(|(_, container)| listen_port == Some(*container))
            .map(|(host, _)| *host),
        native_fabric,
        launch_hca: None,
        job_timeout_seconds,
    })
}

pub(super) fn parse_mount(value: &str) -> Result<(PathBuf, &str, bool), OperationError> {
    let fields = value.split(',').collect::<Vec<_>>();
    if !(fields.len() == 3 || fields.len() == 4) || fields[0] != "type=bind" {
        return Err(OperationError::InvalidOperation);
    }
    let source = fields[1]
        .strip_prefix("src=")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .ok_or(OperationError::InvalidOperation)?;
    let target = fields[2]
        .strip_prefix("dst=")
        .ok_or(OperationError::InvalidOperation)?;
    let readonly = fields.get(3).is_some_and(|value| *value == "readonly");
    if fields.len() == 4 && !readonly {
        return Err(OperationError::InvalidOperation);
    }
    Ok((source, target, readonly))
}

pub(super) fn validate_runtime_start_plan(plan: &RecipeStartPayload) -> Result<(), OperationError> {
    let encoded_plan = canonical_json(&plan.compiled_execution_plan)
        .map_err(|_| OperationError::InvalidOperation)?;
    let encoded_claim = canonical_json(plan).map_err(|_| OperationError::InvalidOperation)?;
    let compiled = &plan.compiled_execution_plan;
    compiled
        .validate()
        .map_err(|_| OperationError::InvalidOperation)?;
    let placement = &compiled.runtime.placement;
    let phase_binding_valid = match (plan.phase, plan.start_deadline.as_deref()) {
        (None, None) => true,
        (Some(_), Some(deadline)) => !deadline.is_empty() && deadline.len() <= 64,
        _ => false,
    };
    if plan.run_generation == 0
        || plan.run_generation > i64::MAX as u64
        || placement.rank >= placement.world_size
        || !lower_hex(&plan.plan_digest, 64)
        || !valid_oci_digest(plan.image_digest())
        || compiled.job.is_some()
        || compiled.endpoint.is_none()
        || plan.endpoint_address().is_none()
        || plan.port().is_none()
        || (placement.world_size == 1
            && (placement.rank != 0
                || placement.local_address.is_some()
                || placement.master_address.is_some()
                || placement.master_port.is_some()
                || plan.phase.is_some()
                || plan.start_deadline.is_some()))
        || (placement.world_size > 1
            && (placement.local_address.is_none()
                || placement.master_address.is_none()
                || placement.master_port.is_none()))
        || !phase_binding_valid
        || encoded_plan.len() > vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
        || encoded_claim.len() > vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES
    {
        return Err(OperationError::InvalidOperation);
    }
    Ok(())
}

pub(super) fn validate_runtime_job_plan(plan: &RecipeJobRunRequest) -> Result<(), OperationError> {
    let encoded_plan = canonical_json(&plan.compiled_execution_plan)
        .map_err(|_| OperationError::InvalidOperation)?;
    let encoded_claim = canonical_json(plan).map_err(|_| OperationError::InvalidOperation)?;
    let compiled = &plan.compiled_execution_plan;
    compiled
        .validate()
        .map_err(|_| OperationError::InvalidOperation)?;
    let placement = &compiled.runtime.placement;
    let job = compiled
        .job
        .as_ref()
        .ok_or(OperationError::InvalidOperation)?;
    if plan.run_generation == 0
        || plan.run_generation > i64::MAX as u64
        || !lower_hex(&plan.plan_digest, 64)
        || !valid_oci_digest(&compiled.runtime_image.image_digest)
        || !(1..=3600).contains(&job.timeout_seconds)
        || placement.rank != 0
        || placement.world_size != 1
        || plan.input_total_bytes > 1024 * 1024 * 1024
        || plan.inputs.iter().try_fold(0_u64, |total, file| {
            total.checked_add(u64::from(file.size_bytes))
        }) != Some(u64::from(plan.input_total_bytes))
        || encoded_plan.len() > vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
        || encoded_claim.len() > vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES
    {
        return Err(OperationError::InvalidOperation);
    }
    Ok(())
}

pub(super) fn validate_runtime_stop_plan(plan: &RecipeStopPayload) -> Result<(), OperationError> {
    let encoded_claim = canonical_json(plan).map_err(|_| OperationError::InvalidOperation)?;
    if plan.run_generation == 0
        || plan.run_generation > i64::MAX as u64
        || !lower_hex(&plan.plan_digest, 64)
        || !lower_hex(&plan.recipe_content_sha256, 64)
        || !(1..=600).contains(&plan.stop_timeout_seconds)
        || encoded_claim.len() > vonk_agent_protocol::MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES
    {
        return Err(OperationError::InvalidOperation);
    }
    Ok(())
}

pub(super) fn runtime_plan_prefix(
    plan: &CompiledExecutionPlan,
    command: Vec<String>,
) -> Vec<String> {
    // The helper request names the archive, the image digest in both the
    // registry and platform slots (Controller-built images have no separate
    // registry index), and the local reference.
    let mut arguments = vec![
        plan.runtime_image.oci_layout_sha256.clone(),
        plan.runtime_image.image_digest.clone(),
        plan.runtime_image.image_digest.clone(),
        plan.runtime_image.local_image_reference(),
    ];
    arguments.extend(command);
    arguments
}

/// Compute Linux exec limits from this helper process' current stack limit
/// and page size. The kernel's aggregate rule is
/// `max(ARG_MAX, min(_STK_LIM * 3/4, RLIMIT_STACK / 4))`; `ARG_MAX` is the
/// documented 128 KiB floor and `_STK_LIM` is the Linux 8 MiB stack ceiling.
/// Each individual argument/environment string is separately limited to 32
/// pages. This measures the invocation the helper will actually launch.
pub(super) fn current_linux_exec_invocation_limits() -> Result<ExecInvocationLimits, OperationError>
{
    let page_size = u64::try_from(rustix::param::page_size())
        .ok()
        .filter(|value| *value > 0)
        .ok_or(OperationError::RuntimeInvocationLimitsUnavailable)?;
    let string_bytes = page_size
        .checked_mul(32)
        .ok_or(OperationError::RuntimeInvocationLimitsUnavailable)?;
    let stack_bytes = rustix::process::getrlimit(rustix::process::Resource::Stack)
        .current
        .unwrap_or(u64::MAX);
    let total_bytes =
        LINUX_ARG_MAX_FLOOR_BYTES.max((stack_bytes / 4).min(LINUX_ARG_MAX_CEILING_BYTES));
    Ok(ExecInvocationLimits {
        total_bytes,
        string_bytes,
    })
}

pub(super) fn validate_runtime_invocation(arguments: &[String]) -> Result<(), OperationError> {
    match measure_exec_invocation(
        "/usr/bin/docker",
        arguments,
        &HELPER_COMMAND_ENV,
        current_linux_exec_invocation_limits()?,
    ) {
        Ok(_) => Ok(()),
        Err(CompiledOciError::InvocationBytes { limit, observed }) => {
            Err(OperationError::RuntimeInvocationLimitExceeded {
                string_limit: false,
                limit_bytes: limit,
                observed_bytes: observed,
            })
        }
        Err(CompiledOciError::InvocationStringBytes { limit, observed }) => {
            Err(OperationError::RuntimeInvocationLimitExceeded {
                string_limit: true,
                limit_bytes: limit,
                observed_bytes: observed,
            })
        }
        Err(_) => Err(OperationError::InvalidOperation),
    }
}

#[cfg(test)]
mod tests;

impl ValidatedDockerRun {
    pub(super) fn docker_arguments(&self) -> Result<Vec<String>, OperationError> {
        let marker_index = self
            .image_index
            .checked_add(1)
            .ok_or(OperationError::InvalidOperation)?;
        if self.arguments.get(marker_index) != Some(&self.entrypoint) {
            return Err(OperationError::InvalidOperation);
        }
        let mut arguments = self.arguments.clone();
        arguments.remove(marker_index);
        if let Some((identity, launch)) = &self.launch_hca {
            for argument in &mut arguments {
                if argument == identity {
                    argument.clone_from(launch);
                }
            }
        }
        Ok(arguments)
    }
}
