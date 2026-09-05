//! Pure projection of a signed compiled workload into Podman launch pieces.
//!
//! This module deliberately does not execute Podman or touch the filesystem.  The
//! caller supplies the already materialized paths and may add run-specific
//! arguments (such as a container name) around the returned launch pieces.

use std::{
    collections::BTreeSet,
    net::IpAddr,
    path::{Component, Path, PathBuf},
};

use thiserror::Error;

use crate::workloads::{
    CompiledEndpoint, CompiledEnvironmentEntry, CompiledExecutionPlan, CompiledJob,
    CompiledLifecycle, CompiledModelArtifact, CompiledTopology, WorkloadError,
};

const TMPFS_SPEC: &str = "/tmp:rw,nosuid,nodev,mode=1777,size=1073741824";
const PID_LIMIT: u64 = 4096;

#[derive(Debug, Error)]
pub enum CompiledOciError {
    #[error("compiled execution plan is invalid")]
    Workload(#[from] WorkloadError),
    #[error("compiled OCI projection rejected: {0}")]
    Invalid(&'static str),
}

/// Host paths for the content-addressed image and the selected, materialized
/// model files.  Paths are lexical inputs to this pure projection; the caller
/// remains responsible for checking that receipts match bytes on disk.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledOciPaths {
    pub image_archive: PathBuf,
    pub model_root: PathBuf,
    pub input_root: Option<PathBuf>,
    pub output_root: PathBuf,
    pub cache_root: PathBuf,
    pub runtime_spec: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OciImageReceipt {
    pub image_digest: String,
    pub archive_name: String,
    pub oci_layout_sha256: String,
    pub image_bytes: u64,
    pub archive_path: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OciMount {
    pub source: PathBuf,
    pub target: String,
    pub read_only: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OciSecurityOptions {
    pub user: String,
    pub devices: Vec<String>,
    pub capabilities: Vec<String>,
    pub privileged: bool,
    /// The only network mode admitted by the current signed compiled plan.
    /// Host and bridge modes require a future signed contract field.
    pub network_mode: OciNetworkMode,
    pub read_only_root: bool,
    pub no_new_privileges: bool,
    pub memory_bytes: u64,
    pub shared_memory_bytes: u64,
    pub pids_limit: u64,
    pub tmpfs: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OciNetworkMode {
    None,
}

impl OciNetworkMode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
        }
    }
}

/// All pieces needed by the production executor to launch one compiled plan.
/// `command` is passed directly to Podman after the image.  It is never parsed
/// as a shell command, and every item in `runtime.argv` is preserved verbatim.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledOciInvocation {
    pub image_receipt: OciImageReceipt,
    pub image: String,
    pub command: Vec<String>,
    pub environment: Vec<CompiledEnvironmentEntry>,
    pub mounts: Vec<OciMount>,
    pub publishes: Vec<String>,
    pub detach: bool,
    pub security: OciSecurityOptions,
    pub topology: CompiledTopology,
    pub lifecycle: CompiledLifecycle,
    pub endpoint: Option<CompiledEndpoint>,
    pub job: Option<CompiledJob>,
}

impl CompiledOciInvocation {
    /// Build the complete direct `podman run` argument vector.  A caller that
    /// needs a run-specific `--name` can insert it before these options; this
    /// method intentionally has no run-id or shell input.
    pub fn podman_arguments(&self) -> Vec<String> {
        let mut arguments = vec!["run".to_owned()];
        if self.detach {
            arguments.push("--detach".to_owned());
        }
        arguments.extend([
            "--read-only".to_owned(),
            "--tmpfs".to_owned(),
            self.security.tmpfs.clone(),
            "--init".to_owned(),
            "--pull".to_owned(),
            "never".to_owned(),
            "--cap-drop=ALL".to_owned(),
            "--security-opt=no-new-privileges".to_owned(),
            "--user".to_owned(),
            self.security.user.clone(),
            "--memory".to_owned(),
            self.security.memory_bytes.to_string(),
            "--memory-swap".to_owned(),
            self.security.memory_bytes.to_string(),
            "--shm-size".to_owned(),
            self.security.shared_memory_bytes.to_string(),
            "--pids-limit".to_owned(),
            self.security.pids_limit.to_string(),
        ]);
        arguments.extend([
            "--network".to_owned(),
            self.security.network_mode.as_str().to_owned(),
        ]);
        for device in &self.security.devices {
            arguments.extend(["--device".to_owned(), device.clone()]);
        }
        for environment in &self.environment {
            arguments.extend([
                "--env".to_owned(),
                format!("{}={}", environment.name, environment.value),
            ]);
        }
        for publish in &self.publishes {
            arguments.extend(["--publish".to_owned(), publish.clone()]);
        }
        for mount in &self.mounts {
            let mut value = format!(
                "type=bind,src={},dst={}",
                mount.source.display(),
                mount.target
            );
            if mount.read_only {
                value.push_str(",readonly");
            }
            arguments.extend(["--mount".to_owned(), value]);
        }
        arguments.push(self.image.clone());
        arguments.extend(self.command.iter().cloned());
        arguments
    }
}

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
    let mut source_paths = BTreeSet::new();
    let mut target_paths = BTreeSet::new();
    for artifact in &plan.artifacts {
        let source = model_source(paths, artifact)?;
        let target = model_target(artifact)?;
        if !source_paths.insert(source.clone()) || !target_paths.insert(target.clone()) {
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
            "model" if mount.target == "/models" && mount.read_only => saw_model = true,
            "inputs" if has_job && mount.target == "/inputs" && mount.read_only => {
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
            "outputs" if mount.target == "/outputs" && !mount.read_only => {
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
        devices: plan.security.devices.clone(),
        capabilities: plan.security.capabilities.clone(),
        privileged: plan.security.privileged,
        network_mode: OciNetworkMode::None,
        read_only_root: plan.security.read_only_root,
        no_new_privileges: plan.security.no_new_privileges,
        memory_bytes: plan.runtime.placement.reserved_memory_bytes,
        shared_memory_bytes: (plan.runtime.placement.reserved_memory_bytes / 8)
            .clamp(64 * 1024 * 1024, 16 * 1024 * 1024 * 1024),
        pids_limit: PID_LIMIT,
        tmpfs: TMPFS_SPEC.to_owned(),
    };
    Ok(CompiledOciInvocation {
        image_receipt: OciImageReceipt {
            image_digest: plan.runtime_image.image_digest.clone(),
            archive_name: plan.runtime_image.distribution_object.name.clone(),
            oci_layout_sha256: plan.runtime_image.oci_layout_sha256.clone(),
            image_bytes: plan.runtime_image.image_bytes,
            archive_path: paths.image_archive.clone(),
        },
        image: plan.runtime.image_digest.clone(),
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

fn validate_paths(paths: &CompiledOciPaths) -> Result<(), CompiledOciError> {
    let mut all = vec![
        ("image archive", &paths.image_archive),
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

fn validate_security(plan: &CompiledExecutionPlan) -> Result<(), CompiledOciError> {
    if plan.security.privileged
        || !plan.security.capabilities.is_empty()
        || !plan.security.read_only_root
        || !plan.security.no_new_privileges
    {
        return Err(CompiledOciError::Invalid("security override"));
    }
    if plan.security.host_network {
        return Err(CompiledOciError::Invalid(
            "host network mode is not authorized",
        ));
    }
    Ok(())
}

fn ordered_environment(
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
    if let Some(endpoint) = &plan.endpoint {
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

fn publications(plan: &CompiledExecutionPlan) -> Result<Vec<String>, CompiledOciError> {
    let Some(endpoint) = &plan.endpoint else {
        return Ok(Vec::new());
    };
    let placement = &plan.runtime.placement;
    let first = match placement.endpoint_address {
        Some(IpAddr::V4(address)) => format!("{address}:{}:{}", placement.port, endpoint.port),
        Some(IpAddr::V6(address)) => format!("[{address}]:{}:{}", placement.port, endpoint.port),
        None => format!("{}:{}", placement.port, endpoint.port),
    };
    let mut result = vec![first];
    if placement.rank == 0 {
        if let (Some(master), Some(master_port)) = (placement.master_address, placement.master_port)
        {
            let publication = match master {
                IpAddr::V4(address) => format!("{address}:{master_port}:{master_port}"),
                IpAddr::V6(address) => format!("[{address}]:{master_port}:{master_port}"),
            };
            if !result.contains(&publication) {
                result.push(publication);
            }
        }
    }
    Ok(result)
}

fn model_source(
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

fn model_target(artifact: &CompiledModelArtifact) -> Result<String, CompiledOciError> {
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

#[cfg(test)]
mod tests {
    use super::{CompiledOciError, CompiledOciPaths, project};
    use crate::workloads::CompiledExecutionPlan;
    use serde_json::{Value, json};
    use std::path::PathBuf;

    fn fixture() -> Value {
        serde_json::from_str(include_str!(
            "../../../../control/tests/fixtures/compiled_workload_v2.json"
        ))
        .unwrap()
    }

    fn paths() -> CompiledOciPaths {
        CompiledOciPaths {
            image_archive: PathBuf::from("/run/vonk/images/image.oci.tar"),
            model_root: PathBuf::from("/run/vonk/models"),
            input_root: None,
            output_root: PathBuf::from("/run/vonk/outputs"),
            cache_root: PathBuf::from("/run/vonk/cache"),
            runtime_spec: PathBuf::from("/run/vonk/runtime.json"),
        }
    }

    #[test]
    fn fixture_projects_colliding_selection_files_without_flattening() {
        let plan: CompiledExecutionPlan = serde_json::from_value(fixture()).unwrap();
        let invocation = project(&plan, &paths()).unwrap();
        assert_eq!(
            invocation.mounts[0].source,
            std::path::Path::new("/run/vonk/models/primary/config.json")
        );
        assert_eq!(invocation.mounts[0].target, "/models/config.json");
        assert_eq!(
            invocation.mounts[1].source,
            std::path::Path::new("/run/vonk/models/draft/config.json")
        );
        assert_eq!(invocation.mounts[1].target, "/models/draft/config.json");
        assert!(invocation.mounts[0].read_only && invocation.mounts[1].read_only);
        assert_eq!(
            invocation.image_receipt.archive_path,
            std::path::Path::new("/run/vonk/images/image.oci.tar")
        );
    }

    #[test]
    fn opaque_argv_stays_byte_for_byte_after_image_boundary() {
        let mut value = fixture();
        let compact_json = format!("{{\"payload\":\"{}\"}}", "x".repeat(4_090));
        let unicode = "🙂".repeat(16_384);
        value["runtime"]["argv"] = json!(["--network", compact_json, "", unicode, "--device"]);
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let invocation = project(&plan, &paths()).unwrap();
        assert_eq!(&invocation.command[0], "/opt/vonk/bin/vllm");
        assert_eq!(&invocation.command[1..], &plan.runtime.argv);
        let podman = invocation.podman_arguments();
        let image_index = podman
            .iter()
            .position(|item| item == &plan.runtime.image_digest)
            .unwrap();
        assert_eq!(&podman[image_index + 1..], &invocation.command);
        assert!(
            podman
                .windows(2)
                .any(|window| window == ["--network", "none"])
        );
        assert!(!podman[..image_index].contains(&"bridge".to_owned()));
        assert!(!podman[..image_index].contains(&"host".to_owned()));
    }

    #[test]
    fn conflicting_platform_environment_is_rejected() {
        let mut value = fixture();
        value["runtime"]["env"] = json!([{"name":"VONK_RUNTIME_SPEC","value":"/tmp/override"}]);
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        assert!(matches!(
            project(&plan, &paths()),
            Err(CompiledOciError::Invalid(
                "conflicting platform environment"
            ))
        ));
    }

    #[test]
    fn duplicate_materialized_target_is_rejected() {
        let mut value = fixture();
        value["artifacts"][1]["mount"]["target"] = json!("/models");
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        assert!(matches!(
            project(&plan, &paths()),
            Err(CompiledOciError::Invalid("duplicate materialized path"))
        ));
    }

    #[test]
    fn identical_receipts_may_be_reused_by_separate_selections() {
        let mut value = fixture();
        let receipt = value["artifacts"][0]["distribution_object"].clone();
        let digest = value["artifacts"][0]["sha256"].clone();
        value["identity"]["model_artifact_bytes"] = json!(7);
        value["artifacts"][1]["sha256"] = digest;
        value["artifacts"][1]["size_bytes"] = json!(7);
        value["artifacts"][1]["distribution_object"] = receipt;
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let invocation = project(&plan, &paths()).unwrap();
        assert_eq!(
            invocation.mounts[0].source,
            std::path::Path::new("/run/vonk/models/primary/config.json")
        );
        assert_eq!(
            invocation.mounts[1].source,
            std::path::Path::new("/run/vonk/models/draft/config.json")
        );
    }

    #[test]
    fn empty_support_file_remains_a_read_only_mount() {
        let mut value = fixture();
        value["identity"]["model_artifact_bytes"] = json!(0);
        let artifact = &mut value["artifacts"][0];
        artifact["file_id"] = json!("tokenizer-config");
        artifact["path"] = json!("tokenizer_config.json");
        artifact["sha256"] = json!(crate::workloads::EMPTY_SHA256);
        artifact["size_bytes"] = json!(0);
        artifact["roles"] = json!(["tokenizer"]);
        artifact["distribution_object"] = json!({
            "name":"tokenizer_config.json",
            "sha256":crate::workloads::EMPTY_SHA256,
            "bytes":0,
            "kind":"model"
        });
        value["artifacts"] = json!([artifact.clone()]);
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let invocation = project(&plan, &paths()).unwrap();
        assert_eq!(
            invocation.mounts[0].source,
            std::path::Path::new("/run/vonk/models/primary/tokenizer_config.json")
        );
    }

    #[test]
    fn job_projection_keeps_input_output_and_lifecycle_boundaries() {
        let mut value = fixture();
        value["endpoint"] = Value::Null;
        value["job"] = json!({
            "interface": "image-job",
            "input": {"path": "/inputs"},
            "output_path": "/outputs",
            "timeout_seconds": 90
        });
        value["security"]["mounts"] = json!([
            {"source":"model","target":"/models","read_only":true},
            {"source":"inputs","target":"/inputs","read_only":true},
            {"source":"outputs","target":"/outputs","read_only":false}
        ]);
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let mut layout = paths();
        layout.input_root = Some(PathBuf::from("/run/vonk/inputs"));
        let invocation = project(&plan, &layout).unwrap();
        assert!(!invocation.detach);
        assert!(
            invocation
                .mounts
                .iter()
                .any(|mount| mount.target == "/inputs" && mount.read_only)
        );
        assert_eq!(invocation.lifecycle.stop_timeout_seconds, 30);
        assert!(invocation.publishes.is_empty());
        assert!(
            invocation
                .podman_arguments()
                .windows(2)
                .any(|window| window == ["--network", "none"])
        );
    }

    #[test]
    fn unauthorized_host_network_mode_is_rejected() {
        let mut value = fixture();
        value["security"]["host_network"] = json!(true);
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        assert!(matches!(
            project(&plan, &paths()),
            Err(CompiledOciError::Invalid(
                "host network mode is not authorized"
            ))
        ));
    }

    #[test]
    fn endpoint_lifecycle_commands_remain_direct_argv_vectors() {
        let mut value = fixture();
        value["lifecycle"] = json!({
            "pre_start": [["/opt/vonk/pre", "--network", "value"]],
            "post_stop": [["/opt/vonk/post", ""]],
            "stop_timeout_seconds": 45
        });
        let plan: CompiledExecutionPlan = serde_json::from_value(value).unwrap();
        let invocation = project(&plan, &paths()).unwrap();
        assert_eq!(invocation.lifecycle.pre_start[0][1], "--network");
        assert_eq!(invocation.lifecycle.post_stop[0][1], "");
        assert_eq!(invocation.lifecycle.stop_timeout_seconds, 45);
    }
}
