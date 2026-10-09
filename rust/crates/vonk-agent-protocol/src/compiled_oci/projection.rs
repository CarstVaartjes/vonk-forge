use std::collections::BTreeSet;

use crate::compiled_execution_plan::CompiledExecutionPlan;

use super::validation::{
    model_source, model_target, ordered_environment, publications, validate_paths,
    validate_security,
};
use super::{
    CompiledOciError, CompiledOciInvocation, CompiledOciPaths, OciImageReceipt, OciMount,
    OciNetworkMode, OciSecurityOptions,
};

const TMPFS_SPEC: &str = "/tmp:rw,nosuid,nodev,mode=1777,size=1073741824";
const PID_LIMIT: u64 = 4096;

/// Convert a validated compiled Controller plan into direct Podman pieces.
/// Every host path is checked lexically and every selected model file remains
/// nested under its selection ID, so colliding names such as `config.json`
/// cannot flatten into one mount or one receipt.
pub fn project(
    plan: &CompiledExecutionPlan,
    paths: &CompiledOciPaths,
) -> Result<CompiledOciInvocation, CompiledOciError> {
    plan.validate()?;
    validate_paths(paths)?;
    validate_security(plan)?;

    let mut mounts = Vec::with_capacity(plan.artifacts.len() + 3);
    let mut target_paths = BTreeSet::new();
    for artifact in &plan.artifacts {
        let source = model_source(paths, artifact)?;
        let target = model_target(artifact)?;
        // A physical file may be projected at more than one model target.
        // Workload validation has already proved that repeated source paths
        // carry the exact same receipt-bound object; retain every distinct
        // target mount while reusing that source.
        if !target_paths.insert(target.clone()) {
            return Err(CompiledOciError::Invalid("duplicate materialized path"));
        }
        mounts.push(OciMount {
            source,
            target,
            read_only: true,
        });
    }

    let has_job = plan.job.is_some();
    let mut saw_model = false;
    let mut saw_inputs = false;
    let mut saw_outputs = false;
    for mount in &plan.security.mounts {
        if !target_paths.insert(mount.target.clone()) {
            return Err(CompiledOciError::Invalid(
                "conflicting security mount target",
            ));
        }
        match mount.source.as_str() {
            "model"
                if (mount.target == "/models" || mount.target.starts_with("/models/"))
                    && plan
                        .artifacts
                        .iter()
                        .any(|artifact| artifact.mount.target == mount.target) =>
            {
                saw_model = true
            }
            "inputs" if has_job && mount.target == "/inputs" => {
                saw_inputs = true;
                let source = paths
                    .input_root
                    .as_ref()
                    .ok_or(CompiledOciError::Invalid("job input root is missing"))?;
                mounts.push(OciMount {
                    source: source.clone(),
                    target: mount.target.clone(),
                    read_only: true,
                });
            }
            "outputs" if mount.target == "/outputs" => {
                saw_outputs = true;
                mounts.push(OciMount {
                    source: paths.output_root.clone(),
                    target: mount.target.clone(),
                    read_only: false,
                });
            }
            _ => return Err(CompiledOciError::Invalid("conflicting security mount")),
        }
    }
    if !saw_model || !saw_outputs || saw_inputs != has_job {
        return Err(CompiledOciError::Invalid("incomplete security mounts"));
    }

    let cache_target = "/outputs/cache".to_owned();
    if !target_paths.insert(cache_target.clone()) {
        return Err(CompiledOciError::Invalid(
            "cache mount conflicts with output",
        ));
    }
    mounts.push(OciMount {
        source: paths.cache_root.clone(),
        target: cache_target,
        read_only: false,
    });
    let runtime_target = "/run/vonk/runtime.json".to_owned();
    if !target_paths.insert(runtime_target.clone()) {
        return Err(CompiledOciError::Invalid(
            "runtime metadata mount conflicts",
        ));
    }
    mounts.push(OciMount {
        source: paths.runtime_spec.clone(),
        target: runtime_target,
        read_only: true,
    });

    let environment = ordered_environment(plan)?;
    let publishes = publications(plan)?;
    let command = std::iter::once(plan.runtime.executable.clone())
        .chain(plan.runtime.argv.iter().cloned())
        .collect();
    let security = OciSecurityOptions {
        user: plan.security.user.clone(),
        devices: plan.security.devices(),
        network_mode: match plan.security.network_mode.as_str() {
            "none" => OciNetworkMode::None,
            "bridge" => OciNetworkMode::Bridge,
            "host" => OciNetworkMode::Host,
            _ => return Err(CompiledOciError::Invalid("unsupported network mode")),
        },
        memory_bytes: plan.runtime.placement.reserved_memory_bytes,
        shared_memory_bytes: (plan.runtime.placement.reserved_memory_bytes / 8)
            .clamp(64 * 1024 * 1024, 16 * 1024 * 1024 * 1024),
        pids_limit: PID_LIMIT,
        tmpfs: TMPFS_SPEC.to_owned(),
    };
    Ok(CompiledOciInvocation {
        image_receipt: OciImageReceipt {
            image_digest: plan.runtime_image.image_digest.clone(),
            local_image_config_id: plan.runtime_image.local_image_config_id.clone(),
            runtime_interface_label: plan.runtime_image.runtime_interface_label.clone(),
            oci_layout_sha256: plan.runtime_image.oci_layout_sha256.clone(),
            image_bytes: plan.runtime_image.image_bytes,
        },
        image: plan.runtime_image.local_image_reference(),
        command,
        environment,
        mounts,
        publishes,
        detach: plan.endpoint.is_some(),
        security,
        topology: plan.topology.clone(),
        lifecycle: plan.lifecycle.clone(),
        endpoint: plan.endpoint.clone(),
        job: plan.job.clone(),
    })
}

/// Build the exact Podman run vector used for one named compiled workload.
pub fn start_arguments_for_paths(
    plan: &CompiledExecutionPlan,
    paths: &CompiledOciPaths,
    run_id: &str,
) -> Result<Vec<String>, CompiledOciError> {
    let mut arguments = project(plan, paths)?.podman_arguments();
    arguments.splice(
        1..1,
        [
            "--name".to_owned(),
            format!("vonk-{run_id}"),
            "--restart".to_owned(),
            "no".to_owned(),
        ],
    );
    Ok(arguments)
}
