use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    net::{IpAddr, ToSocketAddrs},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path, PathBuf},
    time::Duration,
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use url::Url;
use vonk_agent_protocol::{
    RecipeRunInspectionBinding, canonical_json as canonical_protocol_json,
    hex_sha256 as protocol_sha256,
};

use crate::{
    health::readiness_endpoint,
    inventory::{available_disk_bytes, available_memory_bytes},
    process::{ProcessError, ProcessRunner, Program},
    workloads::{
        ArgumentValue, ArtifactSpec, Placement, WorkloadError, WorkloadSpec, image_digest,
        managed_path,
    },
};

#[derive(Debug, Error)]
pub enum OciError {
    #[error("OCI subprocess failed")]
    Process(#[from] ProcessError),
    #[error("workload policy rejected the request")]
    Workload(#[from] WorkloadError),
    #[error("container runtime rejected the request")]
    Runtime,
    #[error("container image digest did not match")]
    ImageDigest,
    #[error("managed artifact content is corrupt")]
    Artifact,
    #[error("managed workload storage failed")]
    Io(#[from] std::io::Error),
    #[error("managed workload metadata is invalid")]
    Json(#[from] serde_json::Error),
    #[error("local disk or memory capacity changed after admission")]
    Capacity,
}

pub struct OciRuntime<'a, R> {
    pub runner: &'a R,
    pub data_root: &'a Path,
    pub huggingface_curl_config: Option<&'a Path>,
}

pub const MAX_MANAGED_RECIPE_RUNS: usize = 64;
const MAX_RUN_DIRECTORY_ENTRIES: usize = 4096;
const MAX_HTTP_FILE_NAME_BYTES: usize = 255;
const LEGACY_DS4_TARGET_FILE: &str =
    "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf";
const LEGACY_DS4_DRAFTER_FILE: &str = "DeepSeek-V4-Flash-DSpark-support-0731.gguf";
const LEGACY_DS4_MODEL_SHA256: &str =
    "a54f12dd8653ff220efed3d5b1efa667ab95f060e16211f1cdba7e0a2dcfeafb";

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeRunObservation {
    pub run_id: String,
    pub ready: bool,
}

#[derive(Debug, Clone)]
pub struct RecipeRunInspectionPlan {
    pub binding: RecipeRunInspectionBinding,
    pub arguments: Vec<String>,
    pub endpoint_address: Option<IpAddr>,
    pub endpoint_port: u16,
    pub health_path: String,
}

type LoadedRunLifecycle = (
    WorkloadSpec,
    String,
    Placement,
    Option<RecipeRunInspectionBinding>,
);

#[derive(Debug, Clone)]
pub struct RecipeRunStartIdentity {
    pub mapping_generation: u64,
    pub mapping_id: uuid::Uuid,
    pub recipe_content_sha256: String,
    pub recipe_revision_id: uuid::Uuid,
    pub run_generation: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct JobOutputState {
    pub output_path: &'static str,
    pub file_count: usize,
    pub total_bytes: u64,
    pub manifest_sha256: String,
}

#[derive(Debug, Serialize)]
struct RuntimeContract<'a> {
    schema_version: u8,
    interface: &'static str,
    installation_id: &'a str,
    run_id: &'a str,
    artifacts: Vec<RuntimeArtifact>,
    #[serde(skip_serializing_if = "Option::is_none")]
    endpoint: Option<RuntimeEndpoint<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    job: Option<RuntimeJob<'a>>,
    placement: RuntimePlacement<'a>,
}

#[derive(Debug, Serialize)]
struct RuntimeArtifact {
    id: String,
    kind: String,
    repository: String,
    revision: String,
    path: String,
}

#[derive(Debug, Serialize)]
struct RuntimeEndpoint<'a> {
    listen_host: &'static str,
    listen_port: u16,
    protocol: &'a str,
    model_aliases: &'a [String],
    health_path: &'a str,
}

#[derive(Debug, Serialize)]
struct RuntimeJob<'a> {
    interface: &'a str,
    input: &'a Option<serde_json::Value>,
    input_path: &'static str,
    output_path: &'a str,
    timeout_seconds: u16,
    #[serde(skip_serializing_if = "Option::is_none")]
    parameters: Option<&'a serde_json::Value>,
}

#[derive(Debug, Serialize)]
struct RuntimePlacement<'a> {
    endpoint_address: Option<std::net::IpAddr>,
    rank: u32,
    role: &'a str,
    world_size: u32,
    local_address: Option<std::net::IpAddr>,
    master_address: Option<std::net::IpAddr>,
    master_port: Option<u16>,
}

#[derive(Debug, Deserialize)]
struct RuntimePolicy {
    runtime_interface: String,
    architecture: String,
    required_image_label: RuntimePolicyLabel,
}

#[derive(Debug, Deserialize)]
struct RuntimePolicyLabel {
    name: String,
    value: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RunLifecycle {
    installation_id: String,
    placement: Placement,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    observation: Option<RecipeRunInspectionBinding>,
}

struct RecipeRunProbe {
    run_id: String,
    address: IpAddr,
    port: u16,
    health_path: String,
}

pub struct RuntimeStartPlan {
    pub image_digest: String,
    pub pre_start: Vec<Vec<String>>,
    pub main: Vec<String>,
}

pub struct RuntimeStopPlan {
    pub remove: Vec<String>,
    pub image_digest: Option<String>,
    pub post_stop: Vec<Vec<String>>,
}

fn runtime_policy() -> Result<RuntimePolicy, OciError> {
    serde_json::from_str(include_str!(
        "../../../../schemas/global/container-runtime-policy-v1.json"
    ))
    .map_err(OciError::Json)
}

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

    pub fn ensure_disk_available(&self, required_bytes: u64) -> Result<(), OciError> {
        let required = required_bytes
            .checked_add(10_000_000_000)
            .ok_or(OciError::Capacity)?;
        if available_disk_bytes(self.data_root).map_err(|_| OciError::Capacity)? < required {
            return Err(OciError::Capacity);
        }
        Ok(())
    }

    pub fn ensure_memory_available(
        &self,
        required_bytes: u64,
        meminfo_path: &Path,
    ) -> Result<(), OciError> {
        let required = required_bytes
            .checked_add(4_000_000_000)
            .ok_or(OciError::Capacity)?;
        if available_memory_bytes(self.runner, meminfo_path).map_err(|_| OciError::Capacity)?
            < required
        {
            return Err(OciError::Capacity);
        }
        Ok(())
    }

    pub fn install(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        recipe_content_sha256: &str,
    ) -> Result<(), OciError> {
        if recipe_content_sha256.len() != 64
            || !recipe_content_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(OciError::Artifact);
        }
        self.verify_image(spec)?;
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let models = self.data_root.join("models").join("sha256");
        fs::create_dir_all(&models)?;
        fs::create_dir_all(&installation)?;
        fs::set_permissions(&installation, fs::Permissions::from_mode(0o700))?;
        for artifact in &spec.artifacts {
            self.materialize_artifact(&models, artifact)?;
        }
        atomic_write(&installation, "spec.json", &serde_json::to_vec(spec)?)?;
        atomic_write(
            &installation,
            "recipe-content.sha256",
            recipe_content_sha256.as_bytes(),
        )?;
        File::open(&installation)?.sync_all()?;
        Ok(())
    }

    pub fn verify_image(&self, spec: &WorkloadSpec) -> Result<(), OciError> {
        spec.validate()?;
        let policy = runtime_policy()?;
        let image_name = spec
            .runtime
            .image
            .split_once('@')
            .map(|(name, _)| name)
            .ok_or(OciError::ImageDigest)?;
        let build_id = image_name
            .strip_prefix("localhost/vonk/recipe-build-")
            .ok_or(OciError::ImageDigest)?;
        if spec.runtime.interface != policy.runtime_interface
            || spec.runtime.architecture != policy.architecture
            || policy.required_image_label.name != "ai.vonkforge.runtime-interface"
            || policy.required_image_label.value != "v1"
            || uuid::Uuid::parse_str(build_id)
                .ok()
                .is_none_or(|value| value.to_string() != build_id)
            || image_digest(&spec.runtime.image).is_none()
        {
            return Err(OciError::ImageDigest);
        }
        Ok(())
    }

    pub fn start_arguments(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
    ) -> Result<Vec<String>, OciError> {
        spec.validate()?;
        placement.validate()?;
        let endpoint = spec.endpoint.as_ref();
        let job = spec.job.as_ref();
        if (placement.world_size > 1) != spec.runtime.placement_environment.is_some() {
            return Err(OciError::Runtime);
        }
        if spec.security.host_network
            && (placement.world_size < 2
                || endpoint.is_none_or(|endpoint| placement.port != endpoint.port))
        {
            return Err(OciError::Runtime);
        }
        managed_path(self.data_root, "installations", installation_id)?;
        let run_root = managed_path(self.data_root, "runs", run_id)?;
        let outputs = run_root.join("outputs");
        let metadata = self.run_metadata_path(run_id)?;
        let shared_memory_bytes =
            (placement.reserved_memory_bytes / 8).clamp(64 * 1024 * 1024, 16 * 1024 * 1024 * 1024);
        let mut arguments = vec!["run".to_owned()];
        if job.is_some() {
            arguments.extend([
                "--name".to_owned(),
                format!("vonk-{run_id}"),
                "--restart".to_owned(),
                "no".to_owned(),
            ]);
        } else {
            arguments.extend([
                "--detach".to_owned(),
                "--name".to_owned(),
                format!("vonk-{run_id}"),
                "--restart".to_owned(),
                "no".to_owned(),
            ]);
        }
        arguments.extend([
            "--read-only".to_owned(),
            "--tmpfs".to_owned(),
            "/tmp:rw,nosuid,nodev,mode=1777,size=1073741824".to_owned(),
            "--init".to_owned(),
            "--pull".to_owned(),
            "never".to_owned(),
            "--log-driver".to_owned(),
            "local".to_owned(),
            "--log-opt".to_owned(),
            "max-size=10m".to_owned(),
            "--log-opt".to_owned(),
            "max-file=3".to_owned(),
            "--cap-drop=ALL".to_owned(),
            "--security-opt=no-new-privileges".to_owned(),
            "--network".to_owned(),
            if spec.security.host_network {
                "host".to_owned()
            } else {
                "bridge".to_owned()
            },
            "--pids-limit".to_owned(),
            "4096".to_owned(),
            "--memory".to_owned(),
            placement.reserved_memory_bytes.to_string(),
            "--memory-swap".to_owned(),
            placement.reserved_memory_bytes.to_string(),
            "--shm-size".to_owned(),
            shared_memory_bytes.to_string(),
            "--user".to_owned(),
            spec.security.user.clone(),
            "--env".to_owned(),
            format!("VONK_RANK={}", placement.rank),
            "--env".to_owned(),
            format!("VONK_WORLD_SIZE={}", placement.world_size),
            "--env".to_owned(),
            "VONK_RUNTIME_SPEC=/run/vonk/runtime.json".to_owned(),
            "--env".to_owned(),
            "VONK_MODEL_ROOT=/models".to_owned(),
        ]);
        if let Some(endpoint) = endpoint {
            arguments.extend([
                "--env".to_owned(),
                "VONK_LISTEN_HOST=0.0.0.0".to_owned(),
                "--env".to_owned(),
                format!("VONK_LISTEN_PORT={}", endpoint.port),
            ]);
        } else if let Some(job) = job {
            arguments.extend([
                "--env".to_owned(),
                "VONK_INPUT_ROOT=/inputs".to_owned(),
                "--env".to_owned(),
                "VONK_OUTPUT_ROOT=/outputs".to_owned(),
                "--env".to_owned(),
                format!("VONK_JOB_TIMEOUT_SECONDS={}", job.timeout_seconds),
            ]);
        }
        if spec.security.host_network {
            arguments.extend([
                "--ipc".to_owned(),
                "host".to_owned(),
                "--device".to_owned(),
                "/dev/infiniband:/dev/infiniband".to_owned(),
                "--ulimit".to_owned(),
                "memlock=-1:-1".to_owned(),
                "--ulimit".to_owned(),
                "stack=67108864:67108864".to_owned(),
            ]);
        } else if let Some(endpoint) = endpoint {
            arguments.extend([
                "--publish".to_owned(),
                match placement.endpoint_address {
                    Some(IpAddr::V4(address)) => {
                        format!("{address}:{}:{}", placement.port, endpoint.port)
                    }
                    Some(IpAddr::V6(address)) => {
                        format!("[{address}]:{}:{}", placement.port, endpoint.port)
                    }
                    None => format!("{}:{}", placement.port, endpoint.port),
                },
            ]);
        }
        if let Some(binding) = &spec.runtime.placement_environment {
            let master = placement.master_address.ok_or(OciError::Runtime)?;
            let local = placement.local_address.ok_or(OciError::Runtime)?;
            let master_port = placement.master_port.ok_or(OciError::Runtime)?;
            arguments.extend([
                "--env".to_owned(),
                format!("{}={master}", binding.master_address),
                "--env".to_owned(),
                format!("{}={local}", binding.local_address),
            ]);
            arguments.extend([
                "--env".to_owned(),
                format!("{}={master_port}", binding.master_port),
            ]);
            if placement.rank == 0 && !spec.security.host_network {
                let publication = match master {
                    IpAddr::V4(address) => {
                        format!("{address}:{master_port}:{master_port}")
                    }
                    IpAddr::V6(address) => {
                        format!("[{address}]:{master_port}:{master_port}")
                    }
                };
                arguments.extend(["--publish".to_owned(), publication]);
            }
        }
        if spec
            .security
            .devices
            .iter()
            .any(|device| device == "nvidia.com/gpu=all")
        {
            arguments.extend(["--device".to_owned(), "nvidia.com/gpu=all".to_owned()]);
        }
        for environment in &spec.runtime.environment {
            let Some(value) = &environment.value else {
                return Err(OciError::Runtime);
            };
            let value = match value {
                ArgumentValue::Boolean(value) => value.to_string(),
                ArgumentValue::Integer(value) => value.to_string(),
                ArgumentValue::String(value) => value.clone(),
            };
            arguments.extend(["--env".to_owned(), format!("{}={value}", environment.name)]);
        }
        let storage_artifacts = self.storage_artifacts_for_spec(spec, installation_id)?;
        if storage_artifacts.len() != spec.artifacts.len() {
            return Err(OciError::Runtime);
        }
        for (artifact, storage_artifact) in spec.artifacts.iter().zip(&storage_artifacts) {
            let source = self
                .data_root
                .join("models")
                .join("sha256")
                .join(artifact_key(storage_artifact)?);
            arguments.extend([
                "--mount".to_owned(),
                format!(
                    "type=bind,src={},dst={},readonly",
                    source.display(),
                    artifact.mount.target
                ),
            ]);
        }
        for mount in &spec.security.mounts {
            match mount.source.as_str() {
                // The model mount remains part of the signed security policy. Each artifact is
                // mounted separately above so the shared cache root is never exposed.
                "model" => continue,
                "outputs" => {
                    let mut value =
                        format!("type=bind,src={},dst={}", outputs.display(), mount.target);
                    if mount.read_only {
                        value.push_str(",readonly");
                    }
                    arguments.extend(["--mount".to_owned(), value]);
                }
                "inputs" => {
                    let value = format!(
                        "type=bind,src={},dst={},readonly",
                        run_root.join("inputs").display(),
                        mount.target
                    );
                    arguments.extend(["--mount".to_owned(), value]);
                }
                _ => return Err(OciError::Runtime),
            }
        }
        arguments.extend([
            "--mount".to_owned(),
            format!(
                "type=bind,src={},dst=/run/vonk/runtime.json,readonly",
                metadata.join("runtime.json").display()
            ),
        ]);
        arguments.push(spec.runtime.image.clone());
        arguments.extend(spec.runtime.entrypoint.iter().cloned());
        for argument in &spec.runtime.arguments {
            arguments.push(format!("--{}", argument.name.replace('_', "-")));
            match &argument.value {
                ArgumentValue::Boolean(true) => {}
                ArgumentValue::Boolean(false) => arguments.push("false".to_owned()),
                ArgumentValue::Integer(value) => arguments.push(value.to_string()),
                ArgumentValue::String(value) => arguments.push(value.clone()),
            }
        }
        Ok(arguments)
    }

    pub fn prepare_start(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.prepare_start_internal(spec, installation_id, run_id, placement, None)
    }

    pub fn prepare_start_with_inspection_identity(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
        identity: &RecipeRunStartIdentity,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.prepare_start_internal(spec, installation_id, run_id, placement, Some(identity))
    }

    fn prepare_start_internal(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
        identity: Option<&RecipeRunStartIdentity>,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.verify_image(spec)?;
        let state = managed_path(self.data_root, "runs", run_id)?;
        fs::create_dir_all(&state)?;
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
        let outputs = state.join("outputs");
        fs::create_dir_all(&outputs)?;
        fs::set_permissions(&outputs, fs::Permissions::from_mode(0o700))?;
        if spec.job.is_some() {
            let inputs = state.join("inputs");
            let metadata = fs::symlink_metadata(&inputs)?;
            if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
                return Err(OciError::Artifact);
            }
        }
        let metadata = self.ensure_run_metadata(run_id)?;
        self.write_runtime_contract(spec, installation_id, run_id, placement, None)?;
        let main = self.start_arguments(spec, installation_id, run_id, placement)?;
        let runtime_image_digest = format!(
            "sha256:{}",
            image_digest(&spec.runtime.image).ok_or(OciError::ImageDigest)?
        );
        let pre_start = spec
            .lifecycle
            .pre_start
            .iter()
            .map(|hook| hook_arguments(&main, &spec.runtime.image, hook))
            .collect::<Result<Vec<_>, _>>()?;
        let observation = identity
            .map(|identity| {
                let local_address = placement.local_address.ok_or(OciError::Artifact)?;
                let master_address = placement.master_address.ok_or(OciError::Artifact)?;
                let master_port = placement.master_port.ok_or(OciError::Artifact)?;
                if placement.world_size <= 1
                    || identity.mapping_generation == 0
                    || identity.run_generation == 0
                    || identity.recipe_content_sha256 != self.recipe_digest(installation_id)?
                {
                    return Err(OciError::Artifact);
                }
                let mut arguments = vec![runtime_image_digest.clone()];
                arguments.extend(main.clone());
                let binding = RecipeRunInspectionBinding {
                    artifact_set_digest: self.artifact_set_digest(installation_id)?,
                    image_digest: image_digest(&spec.runtime.image)
                        .ok_or(OciError::ImageDigest)?
                        .to_owned(),
                    installation_id: uuid::Uuid::parse_str(installation_id)
                        .map_err(|_| OciError::Artifact)?,
                    local_address,
                    master_address,
                    master_port,
                    mapping_generation: identity.mapping_generation,
                    mapping_id: identity.mapping_id,
                    model_identity: spec
                        .artifacts
                        .first()
                        .map(|artifact| format!("{}@{}", artifact.repository, artifact.revision))
                        .ok_or(OciError::Artifact)?,
                    port: placement.port,
                    rank: placement.rank,
                    recipe_content_sha256: identity.recipe_content_sha256.clone(),
                    recipe_revision_id: identity.recipe_revision_id,
                    role: placement.role.clone(),
                    run_id: uuid::Uuid::parse_str(run_id).map_err(|_| OciError::Artifact)?,
                    run_generation: identity.run_generation,
                    runtime_arguments_sha256: protocol_sha256(
                        &canonical_protocol_json(&arguments).map_err(|_| OciError::Artifact)?,
                    ),
                    world_size: placement.world_size,
                };
                binding.validate().map_err(|_| OciError::Artifact)?;
                Ok(binding)
            })
            .transpose()?;
        atomic_write(
            &metadata,
            "lifecycle.json",
            &serde_json::to_vec(&RunLifecycle {
                installation_id: installation_id.to_owned(),
                placement: placement.clone(),
                observation,
            })?,
        )?;
        Ok(RuntimeStartPlan {
            image_digest: runtime_image_digest,
            pre_start,
            main,
        })
    }

    /// Reconstruct the exact runtime plan for a previously launched service.
    ///
    /// Collective readiness is deliberately a separate, non-starting phase for
    /// distributed workloads.  It may inspect only the run identity retained
    /// by `prepare_start`; a changed request, installation, specification, or
    /// placement fails closed instead of being allowed to inspect a different
    /// container under the same run id.
    pub fn prepare_retained_start(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
    ) -> Result<RuntimeStartPlan, OciError> {
        self.verify_image(spec)?;
        let Some((retained_spec, retained_installation_id, retained_placement, _)) =
            self.load_run_lifecycle(run_id)?
        else {
            return Err(OciError::Runtime);
        };
        if retained_spec != *spec
            || retained_installation_id != installation_id
            || retained_placement != *placement
        {
            return Err(OciError::Runtime);
        }
        Ok(RuntimeStartPlan {
            image_digest: format!(
                "sha256:{}",
                image_digest(&spec.runtime.image).ok_or(OciError::ImageDigest)?
            ),
            pre_start: Vec::new(),
            main: self.start_arguments(spec, installation_id, run_id, placement)?,
        })
    }

    pub fn prepare_retained_start_with_inspection_identity(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
        identity: &RecipeRunStartIdentity,
    ) -> Result<RuntimeStartPlan, OciError> {
        let plan = self.prepare_retained_start(spec, installation_id, run_id, placement)?;
        let Some((_, _, _, Some(binding))) = self.load_run_lifecycle(run_id)? else {
            return Err(OciError::Runtime);
        };
        if binding.mapping_id != identity.mapping_id
            || binding.mapping_generation != identity.mapping_generation
            || binding.recipe_revision_id != identity.recipe_revision_id
            || binding.recipe_content_sha256 != identity.recipe_content_sha256
            || binding.run_generation != identity.run_generation
        {
            return Err(OciError::Runtime);
        }
        Ok(plan)
    }

    pub fn prepare_job_start(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
        parameters: &serde_json::Value,
        timeout_seconds: u16,
    ) -> Result<RuntimeStartPlan, OciError> {
        let Some(installed_job) = spec.job.as_ref() else {
            return Err(OciError::Runtime);
        };
        if timeout_seconds == 0 || timeout_seconds > installed_job.timeout_seconds {
            return Err(OciError::Runtime);
        }
        let mut effective = spec.clone();
        effective
            .job
            .as_mut()
            .ok_or(OciError::Runtime)?
            .timeout_seconds = timeout_seconds;
        let plan = self.prepare_start(&effective, installation_id, run_id, placement)?;
        self.write_runtime_contract(
            &effective,
            installation_id,
            run_id,
            placement,
            Some(parameters),
        )?;
        Ok(plan)
    }

    pub fn prepare_stop(&self, run_id: &str) -> Result<RuntimeStopPlan, OciError> {
        let lifecycle = self.load_run_lifecycle(run_id)?;
        let stop_timeout = lifecycle
            .as_ref()
            .map(|(spec, _, _, _)| spec.lifecycle.stop_timeout_seconds)
            .unwrap_or(30);
        let (image_digest, post_stop) = match lifecycle {
            Some((spec, installation_id, placement, _)) => {
                let main = self.start_arguments(&spec, &installation_id, run_id, &placement)?;
                (
                    Some(format!(
                        "sha256:{}",
                        image_digest(&spec.runtime.image).ok_or(OciError::ImageDigest)?
                    )),
                    spec.lifecycle
                        .post_stop
                        .iter()
                        .map(|hook| hook_arguments(&main, &spec.runtime.image, hook))
                        .collect::<Result<Vec<_>, _>>()?,
                )
            }
            None => (None, Vec::new()),
        };
        Ok(RuntimeStopPlan {
            remove: vec![run_id.to_owned(), stop_timeout.to_string()],
            image_digest,
            post_stop,
        })
    }

    pub fn complete_stop(&self, run_id: &str) -> Result<(), OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        match fs::remove_file(metadata.join("lifecycle.json")) {
            Ok(()) => File::open(metadata)?.sync_all().map_err(OciError::Io),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
    }

    pub fn recipe_run_inspection_plans(&self) -> Result<Vec<RecipeRunInspectionPlan>, OciError> {
        let runs = self.data_root.join("runs");
        let metadata = match fs::symlink_metadata(&runs) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
            Err(error) => return Err(error.into()),
        };
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let mut run_ids = Vec::new();
        for entry in fs::read_dir(&runs)? {
            if run_ids.len() == MAX_RUN_DIRECTORY_ENTRIES {
                return Err(OciError::Artifact);
            }
            let entry = entry?;
            let file_type = entry.file_type()?;
            let run_id = entry
                .file_name()
                .into_string()
                .map_err(|_| OciError::Artifact)?;
            if !canonical_uuid(&run_id) || !file_type.is_dir() || file_type.is_symlink() {
                return Err(OciError::Artifact);
            }
            run_ids.push(run_id);
        }
        run_ids.sort_unstable();

        let mut plans = Vec::new();
        for run_id in run_ids {
            // Run directories intentionally outlive their lifecycle after a
            // successful stop and older agents retained the same residue. A
            // missing lifecycle is therefore historical, while a present but
            // malformed lifecycle remains an active-assignment integrity error.
            let Some((spec, installation_id, placement, observation)) =
                self.load_run_lifecycle(&run_id)?
            else {
                continue;
            };
            let Some(binding) = observation else {
                continue;
            };
            binding.validate().map_err(|_| OciError::Artifact)?;
            if binding.run_id.to_string() != run_id
                || binding.installation_id.to_string() != installation_id
                || binding.rank != placement.rank
                || binding.role != placement.role
                || binding.world_size != placement.world_size
                || Some(binding.local_address) != placement.local_address
                || Some(binding.master_address) != placement.master_address
                || Some(binding.master_port) != placement.master_port
                || binding.port != placement.port
                || binding.recipe_content_sha256 != self.recipe_digest(&installation_id)?
                || binding.artifact_set_digest != self.artifact_set_digest(&installation_id)?
                || binding.image_digest
                    != image_digest(&spec.runtime.image).ok_or(OciError::ImageDigest)?
                || binding.model_identity
                    != spec
                        .artifacts
                        .first()
                        .map(|artifact| format!("{}@{}", artifact.repository, artifact.revision))
                        .ok_or(OciError::Artifact)?
            {
                return Err(OciError::Artifact);
            }
            let retained =
                self.prepare_retained_start(&spec, &installation_id, &run_id, &placement)?;
            let mut arguments = vec![retained.image_digest];
            arguments.extend(retained.main);
            if binding.runtime_arguments_sha256
                != protocol_sha256(
                    &canonical_protocol_json(&arguments).map_err(|_| OciError::Artifact)?,
                )
            {
                return Err(OciError::Artifact);
            }
            if plans.len() == MAX_MANAGED_RECIPE_RUNS {
                return Err(OciError::Artifact);
            }
            let endpoint_owner = binding.local_address == binding.master_address;
            let health_path = spec
                .endpoint
                .as_ref()
                .ok_or(OciError::Artifact)?
                .health_path
                .clone();
            plans.push(RecipeRunInspectionPlan {
                binding,
                arguments,
                endpoint_address: endpoint_owner
                    .then_some(placement.endpoint_address.ok_or(OciError::Artifact)?),
                endpoint_port: placement.port,
                health_path,
            });
        }
        Ok(plans)
    }

    pub fn recipe_run_observations(&self) -> Result<Vec<RecipeRunObservation>, OciError> {
        let probes = self.recipe_run_probes()?;
        probes
            .into_iter()
            .map(|probe| {
                let ready = self.readiness_request(probe.address, probe.port, &probe.health_path);
                Ok(RecipeRunObservation {
                    run_id: probe.run_id,
                    ready,
                })
            })
            .collect()
    }

    fn recipe_run_probes(&self) -> Result<Vec<RecipeRunProbe>, OciError> {
        let runs = self.data_root.join("runs");
        let metadata = match fs::symlink_metadata(&runs) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
            Err(error) => return Err(error.into()),
        };
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let mut run_ids = Vec::new();
        for entry in fs::read_dir(&runs)? {
            if run_ids.len() == MAX_RUN_DIRECTORY_ENTRIES {
                return Err(OciError::Artifact);
            }
            let entry = entry?;
            let file_type = entry.file_type()?;
            let run_id = entry
                .file_name()
                .into_string()
                .map_err(|_| OciError::Artifact)?;
            if !canonical_uuid(&run_id) || !file_type.is_dir() || file_type.is_symlink() {
                return Err(OciError::Artifact);
            }
            run_ids.push(run_id);
        }
        run_ids.sort_unstable();

        let mut probes = Vec::with_capacity(run_ids.len());
        for run_id in run_ids {
            let lifecycle = match self.load_run_lifecycle(&run_id) {
                Ok(lifecycle) => lifecycle,
                Err(OciError::Artifact | OciError::Json(_) | OciError::Workload(_)) => continue,
                Err(error) => return Err(error),
            };
            let Some((spec, _, placement, observation)) = lifecycle else {
                continue;
            };
            if observation.is_some() {
                continue;
            }
            let Some(endpoint) = spec.endpoint.as_ref() else {
                continue;
            };
            if endpoint.health_path.contains(['?', '#', '\0'])
                || !endpoint
                    .health_path
                    .bytes()
                    .all(|byte| byte.is_ascii_graphic())
            {
                continue;
            }
            if probes.len() == MAX_MANAGED_RECIPE_RUNS {
                return Err(OciError::Artifact);
            }
            probes.push(RecipeRunProbe {
                run_id,
                // Legacy distributed runs expose readiness only on the
                // retained endpoint owner, avoiding a false failure for a
                // healthy headless worker. New exact-observation lifecycles
                // are filtered above and inspect every local rank separately.
                address: if placement.world_size > 1 {
                    placement.master_address.ok_or(OciError::Artifact)?
                } else {
                    placement
                        .endpoint_address
                        .unwrap_or(IpAddr::V4(std::net::Ipv4Addr::LOCALHOST))
                },
                port: placement.port,
                health_path: endpoint.health_path.clone(),
            });
        }
        Ok(probes)
    }

    pub(crate) fn readiness_request(&self, address: IpAddr, port: u16, health_path: &str) -> bool {
        let endpoint = readiness_endpoint(address, port, health_path);
        let output = self.runner.run(
            Program::Curl,
            &[
                "--silent".to_owned(),
                "--show-error".to_owned(),
                "--connect-timeout".to_owned(),
                "2".to_owned(),
                "--max-time".to_owned(),
                "3".to_owned(),
                "--max-filesize".to_owned(),
                (64 * 1024).to_string(),
                "--noproxy".to_owned(),
                "*".to_owned(),
                "--proto".to_owned(),
                "=http".to_owned(),
                "--output".to_owned(),
                "/dev/null".to_owned(),
                "--write-out".to_owned(),
                "%{http_code}".to_owned(),
                endpoint,
            ],
            Duration::from_secs(5),
        );
        let Ok(output) = output else {
            return false;
        };
        output.success
            && std::str::from_utf8(&output.stdout)
                .ok()
                .and_then(|status| status.parse::<u16>().ok())
                .is_some_and(|status| (200..300).contains(&status))
    }

    fn load_run_lifecycle(&self, run_id: &str) -> Result<Option<LoadedRunLifecycle>, OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        let path = metadata.join("lifecycle.json");
        let Some(record) = self.read_run_lifecycle(&path)? else {
            return Ok(None);
        };
        record.placement.validate()?;
        if !canonical_uuid(&record.installation_id) {
            return Err(OciError::Artifact);
        }
        managed_path(self.data_root, "installations", &record.installation_id)?;
        let spec = self.load_spec(&record.installation_id)?;
        Ok(Some((
            spec,
            record.installation_id,
            record.placement,
            record.observation,
        )))
    }

    fn read_run_lifecycle(&self, path: &Path) -> Result<Option<RunLifecycle>, OciError> {
        let metadata = match fs::symlink_metadata(path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() > 16 * 1024
        {
            return Err(OciError::Artifact);
        }
        let record: RunLifecycle = serde_json::from_slice(&read_regular_file(path, 16 * 1024)?)?;
        Ok(Some(record))
    }

    fn run_metadata_path(&self, run_id: &str) -> Result<PathBuf, OciError> {
        if !canonical_uuid(run_id) {
            return Err(OciError::Artifact);
        }
        Ok(self.data_root.join("run-metadata").join(run_id))
    }

    fn ensure_run_metadata(&self, run_id: &str) -> Result<PathBuf, OciError> {
        let root = self.data_root.join("run-metadata");
        fs::create_dir_all(&root)?;
        let root_metadata = fs::symlink_metadata(&root)?;
        if !root_metadata.file_type().is_dir() || root_metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let metadata = self.run_metadata_path(run_id)?;
        match fs::create_dir(&metadata) {
            Ok(()) => fs::set_permissions(&metadata, fs::Permissions::from_mode(0o700))?,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                let metadata_type = fs::symlink_metadata(&metadata)?.file_type();
                if !metadata_type.is_dir() || metadata_type.is_symlink() {
                    return Err(OciError::Artifact);
                }
            }
            Err(error) => return Err(error.into()),
        }
        Ok(metadata)
    }

    pub fn uninstall(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
    ) -> Result<(), OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let metadata = fs::symlink_metadata(&installation)?;
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        self.load_spec(installation_id)?;
        if self.recipe_digest(installation_id)? != expected_recipe_digest {
            return Err(OciError::Artifact);
        }
        fs::remove_dir_all(installation)?;
        File::open(self.data_root.join("installations"))?.sync_all()?;
        Ok(())
    }

    pub fn uninstall_with_model_cleanup(
        &self,
        installation_id: &str,
        expected_recipe_digest: &str,
        model_version_sha256: &str,
    ) -> Result<u64, OciError> {
        let (_, persisted) = self.load_persisted_spec(installation_id)?;
        if self.recipe_digest(installation_id)? != expected_recipe_digest
            || !spec_references_model(&persisted, model_version_sha256)
        {
            return Err(OciError::Artifact);
        }
        let candidate_keys = persisted
            .artifacts
            .iter()
            .map(artifact_key)
            .collect::<Result<BTreeSet<_>, _>>()?;
        let remaining = self.installed_specs_except(&[installation_id])?;
        if remaining
            .iter()
            .any(|(_, spec)| spec_references_model(spec, model_version_sha256))
        {
            return Err(OciError::Artifact);
        }
        self.uninstall(installation_id, expected_recipe_digest)?;
        self.remove_unreferenced_artifacts(&candidate_keys)
    }

    pub fn uninstall_model(
        &self,
        installations: &[(String, String)],
        model_version_sha256: &str,
    ) -> Result<u64, OciError> {
        if installations.is_empty() {
            return Err(OciError::Artifact);
        }
        let target_ids = installations
            .iter()
            .map(|(installation_id, _)| installation_id.as_str())
            .collect::<BTreeSet<_>>();
        if target_ids.len() != installations.len() {
            return Err(OciError::Artifact);
        }
        let mut candidate_keys = BTreeSet::new();
        for (installation_id, expected_recipe_digest) in installations {
            let (_, persisted) = self.load_persisted_spec(installation_id)?;
            if self.recipe_digest(installation_id)? != *expected_recipe_digest
                || !spec_references_model(&persisted, model_version_sha256)
            {
                return Err(OciError::Artifact);
            }
            for artifact in &persisted.artifacts {
                candidate_keys.insert(artifact_key(artifact)?);
            }
        }
        let excluded = target_ids.iter().copied().collect::<Vec<_>>();
        let remaining = self.installed_specs_except(&excluded)?;
        if remaining
            .iter()
            .any(|(_, spec)| spec_references_model(spec, model_version_sha256))
        {
            return Err(OciError::Artifact);
        }
        for (installation_id, expected_recipe_digest) in installations {
            self.uninstall(installation_id, expected_recipe_digest)?;
        }
        self.remove_unreferenced_artifacts(&candidate_keys)
    }

    fn installed_specs_except(
        &self,
        excluded: &[&str],
    ) -> Result<Vec<(String, WorkloadSpec)>, OciError> {
        let root = self.data_root.join("installations");
        let mut entries = fs::read_dir(&root)?.collect::<Result<Vec<_>, _>>()?;
        if entries.len() > MAX_RUN_DIRECTORY_ENTRIES {
            return Err(OciError::Artifact);
        }
        entries.sort_by_key(fs::DirEntry::file_name);
        let excluded = excluded.iter().copied().collect::<BTreeSet<_>>();
        let mut result = Vec::with_capacity(entries.len());
        for entry in entries {
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| OciError::Artifact)?;
            if excluded.contains(name.as_str()) {
                continue;
            }
            let metadata = entry.file_type()?;
            if !metadata.is_dir() || metadata.is_symlink() {
                return Err(OciError::Artifact);
            }
            let (_, persisted) = self.load_persisted_spec(&name)?;
            result.push((name, persisted));
        }
        Ok(result)
    }

    fn remove_unreferenced_artifacts(
        &self,
        candidate_keys: &BTreeSet<String>,
    ) -> Result<u64, OciError> {
        let referenced = self
            .installed_specs_except(&[])?
            .into_iter()
            .flat_map(|(_, spec)| spec.artifacts)
            .map(|artifact| artifact_key(&artifact))
            .collect::<Result<BTreeSet<_>, _>>()?;
        let root = self.data_root.join("models").join("sha256");
        let mut removed_bytes = 0_u64;
        for key in candidate_keys.difference(&referenced) {
            if !lower_hex(key, 64) {
                return Err(OciError::Artifact);
            }
            let path = root.join(key);
            let metadata = match fs::symlink_metadata(&path) {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error.into()),
            };
            if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
                return Err(OciError::Artifact);
            }
            removed_bytes = removed_bytes
                .checked_add(read_manifest(&path)?.total_bytes)
                .ok_or(OciError::Artifact)?;
            fs::remove_dir_all(&path)?;
        }
        File::open(&root)?.sync_all()?;
        Ok(removed_bytes)
    }

    pub fn load_spec(&self, installation_id: &str) -> Result<WorkloadSpec, OciError> {
        self.load_persisted_spec(installation_id)
            .map(|(spec, _)| spec)
    }

    fn load_persisted_spec(
        &self,
        installation_id: &str,
    ) -> Result<(WorkloadSpec, WorkloadSpec), OciError> {
        let persisted = self.read_persisted_spec(installation_id)?;
        let spec = if persisted.validate().is_ok() {
            persisted.clone()
        } else {
            if self.recipe_digest(installation_id)? != persisted.identity.recipe_revision_sha256 {
                return Err(OciError::Artifact);
            }
            normalize_legacy_ds4_spec(persisted.clone())?
        };
        Ok((spec, persisted))
    }

    fn read_persisted_spec(&self, installation_id: &str) -> Result<WorkloadSpec, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let directory_metadata = fs::symlink_metadata(&installation)?;
        if !directory_metadata.file_type().is_dir() || directory_metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        let path = installation.join("spec.json");
        let metadata = fs::symlink_metadata(&path)?;
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() > 64 * 1024
        {
            return Err(OciError::Artifact);
        }
        serde_json::from_slice(&read_regular_file(&path, 64 * 1024)?).map_err(OciError::Json)
    }

    fn storage_artifacts_for_spec(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
    ) -> Result<Vec<ArtifactSpec>, OciError> {
        match self.load_persisted_spec(installation_id) {
            Ok((loaded, persisted)) if loaded == *spec => Ok(persisted.artifacts),
            Ok(_) => Err(OciError::Runtime),
            Err(OciError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {
                Ok(spec.artifacts.clone())
            }
            Err(error) => Err(error),
        }
    }

    pub fn verify_installation(&self, installation_id: &str) -> Result<(), OciError> {
        let (spec, persisted) = self.load_persisted_spec(installation_id)?;
        if spec.artifacts.len() != persisted.artifacts.len() {
            return Err(OciError::Artifact);
        }
        let models = self.data_root.join("models").join("sha256");
        for (artifact, storage_artifact) in spec.artifacts.iter().zip(&persisted.artifacts) {
            let destination = models.join(artifact_key(storage_artifact)?);
            if artifact.kind == "http.file" {
                let (_, file_name) = http_file_url(&artifact.repository)?;
                verify_or_migrate_cached_http_file(&destination, &file_name, artifact)?;
            } else {
                verify_manifest(&destination)?;
            }
        }
        Ok(())
    }

    pub fn recipe_digest(&self, installation_id: &str) -> Result<String, OciError> {
        let path = managed_path(self.data_root, "installations", installation_id)?
            .join("recipe-content.sha256");
        let value =
            String::from_utf8(read_regular_file(&path, 64)?).map_err(|_| OciError::Artifact)?;
        if value.len() != 64
            || !value
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            return Err(OciError::Artifact);
        }
        Ok(value)
    }

    pub fn recipe_digest_if_present(
        &self,
        installation_id: &str,
    ) -> Result<Option<String>, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let metadata = match fs::symlink_metadata(&installation) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            return Err(OciError::Artifact);
        }
        self.recipe_digest(installation_id).map(Some)
    }

    pub fn installed_bytes(&self, installation_id: &str) -> Result<u64, OciError> {
        let installation = managed_path(self.data_root, "installations", installation_id)?;
        let mut files = BTreeMap::new();
        let mut total = 0;
        visit_files(&installation, &installation, &mut files, &mut total)?;
        let (_, persisted) = self.load_persisted_spec(installation_id)?;
        for artifact in &persisted.artifacts {
            let manifest = read_manifest(
                &self
                    .data_root
                    .join("models")
                    .join("sha256")
                    .join(artifact_key(artifact)?),
            )?;
            total = total
                .checked_add(manifest.total_bytes)
                .ok_or(OciError::Artifact)?;
        }
        Ok(total)
    }

    pub fn artifact_set_digest(&self, installation_id: &str) -> Result<String, OciError> {
        let (_, persisted) = self.load_persisted_spec(installation_id)?;
        let mut identities = Vec::with_capacity(persisted.artifacts.len());
        for artifact in &persisted.artifacts {
            let key = artifact_key(artifact)?;
            let manifest = read_manifest(&self.data_root.join("models").join("sha256").join(&key))?;
            identities.push(serde_json::json!({
                "artifact": artifact,
                "key": key,
                "manifest": manifest,
            }));
        }
        Ok(hex::encode(Sha256::digest(serde_json::to_vec(
            &identities,
        )?)))
    }

    fn write_runtime_contract(
        &self,
        spec: &WorkloadSpec,
        installation_id: &str,
        run_id: &str,
        placement: &Placement,
        parameters: Option<&serde_json::Value>,
    ) -> Result<(), OciError> {
        let metadata = self.run_metadata_path(run_id)?;
        let artifacts = spec
            .artifacts
            .iter()
            .map(|artifact| RuntimeArtifact {
                id: artifact.id.clone(),
                kind: artifact.kind.clone(),
                repository: artifact.repository.clone(),
                revision: artifact.revision.clone(),
                path: artifact.mount.target.clone(),
            })
            .collect();
        let contract = RuntimeContract {
            schema_version: 1,
            interface: "vonk.runtime.v1",
            installation_id,
            run_id,
            artifacts,
            endpoint: spec.endpoint.as_ref().map(|endpoint| RuntimeEndpoint {
                listen_host: "0.0.0.0",
                listen_port: endpoint.port,
                protocol: &endpoint.protocol,
                model_aliases: &endpoint.model_aliases,
                health_path: &endpoint.health_path,
            }),
            job: spec.job.as_ref().map(|job| RuntimeJob {
                interface: &job.interface,
                input: &job.input,
                input_path: "/inputs",
                output_path: &job.output_path,
                timeout_seconds: job.timeout_seconds,
                parameters,
            }),
            placement: RuntimePlacement {
                endpoint_address: placement.endpoint_address,
                rank: placement.rank,
                role: &placement.role,
                world_size: placement.world_size,
                local_address: placement.local_address,
                master_address: placement.master_address,
                master_port: placement.master_port,
            },
        };
        atomic_write(&metadata, "runtime.json", &serde_json::to_vec(&contract)?)?;
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

    fn materialize_artifact(
        &self,
        models: &Path,
        artifact: &crate::workloads::ArtifactSpec,
    ) -> Result<(), OciError> {
        let key = artifact_key(artifact)?;
        let destination = models.join(&key);
        let http_file = if artifact.kind == "http.file" {
            Some(http_file_url(&artifact.repository)?)
        } else {
            None
        };
        if destination.exists() {
            return match &http_file {
                Some((_, file_name)) => {
                    verify_or_migrate_cached_http_file(&destination, file_name, artifact)
                }
                None => verify_manifest(&destination),
            };
        }
        let staging = models.join(format!(".{key}.{}.staging", std::process::id()));
        fs::create_dir(&staging)?;
        let download = match artifact.kind.as_str() {
            "huggingface.snapshot" => self.download_huggingface(&staging, artifact),
            "http.file" => {
                http_file
                    .as_ref()
                    .ok_or(OciError::Artifact)
                    .and_then(|(url, file_name)| {
                        self.download_https(
                            url,
                            &staging.join(file_name),
                            artifact.download_bytes,
                            false,
                        )
                    })
            }
            "oci.artifact" => oci_endpoint(&artifact.repository).and_then(|endpoint| {
                let address = match endpoint.address {
                    IpAddr::V4(address) => address.to_string(),
                    IpAddr::V6(address) => format!("[{address}]"),
                };
                let arguments = vec![
                    "pull".to_owned(),
                    format!("{}@{}", artifact.repository, artifact.revision),
                    "--resolve".to_owned(),
                    format!("{}:{}:{}", endpoint.hostname, endpoint.port, address),
                    "--output".to_owned(),
                    staging.display().to_string(),
                ];
                let output = self.runner.run_bounded_directory(
                    Program::Oras,
                    &arguments,
                    Duration::from_secs(3600),
                    &staging,
                    artifact.download_bytes,
                )?;
                if output.success {
                    Ok(())
                } else {
                    Err(OciError::Artifact)
                }
            }),
            _ => Err(OciError::Artifact),
        };
        if download.is_err() {
            let _ = fs::remove_dir_all(&staging);
            return Err(OciError::Artifact);
        }
        let manifest = create_manifest(&staging)?;
        if manifest.total_bytes != artifact.download_bytes
            || http_file.as_ref().is_some_and(|(_, file_name)| {
                manifest.files.get(file_name).map(String::as_str)
                    != artifact.revision.strip_prefix("sha256:")
            })
        {
            let _ = fs::remove_dir_all(&staging);
            return Err(OciError::Artifact);
        }
        atomic_write(
            &staging,
            ".vonk-manifest.json",
            &serde_json::to_vec(&manifest)?,
        )?;
        fs::rename(&staging, &destination)?;
        File::open(models)?.sync_all()?;
        Ok(())
    }

    fn download_huggingface(
        &self,
        staging: &Path,
        artifact: &crate::workloads::ArtifactSpec,
    ) -> Result<(), OciError> {
        let repository = huggingface_repository(&artifact.repository)?;
        let mut metadata_url =
            Url::parse("https://huggingface.co/api/models").map_err(|_| OciError::Artifact)?;
        metadata_url
            .path_segments_mut()
            .map_err(|_| OciError::Artifact)?
            .extend(repository)
            .push("revision")
            .push(&artifact.revision);
        let metadata_path = staging.join(".huggingface-model.json");
        self.download_https(&metadata_url, &metadata_path, 8 * 1024 * 1024, true)?;
        let metadata = fs::read(&metadata_path)?;
        fs::remove_file(&metadata_path)?;
        if metadata.len() > 8 * 1024 * 1024 {
            return Err(OciError::Artifact);
        }
        let model: HuggingFaceModel = serde_json::from_slice(&metadata)?;
        if model.siblings.is_empty() || model.siblings.len() > 20_000 {
            return Err(OciError::Artifact);
        }
        let mut remaining = artifact.download_bytes;
        let mut selector_matches = vec![false; artifact.include_paths.len()];
        for file in model.siblings {
            let relative = safe_relative_path(&file.rfilename)?;
            let included = artifact.include_paths.is_empty()
                || artifact.include_paths.iter().enumerate().fold(
                    false,
                    |included, (index, selector)| {
                        let matched = selector.strip_suffix('/').map_or_else(
                            || file.rfilename == *selector,
                            |prefix| {
                                file.rfilename
                                    .strip_prefix(prefix)
                                    .is_some_and(|tail| tail.starts_with('/'))
                            },
                        );
                        selector_matches[index] |= matched;
                        included || matched
                    },
                );
            if !included {
                continue;
            }
            let destination = staging.join(&relative);
            if let Some(parent) = destination.parent() {
                fs::create_dir_all(parent)?;
            }
            let mut url = Url::parse("https://huggingface.co/").map_err(|_| OciError::Artifact)?;
            url.path_segments_mut()
                .map_err(|_| OciError::Artifact)?
                .extend(repository.iter().copied())
                .push("resolve")
                .push(&artifact.revision)
                .extend(
                    relative
                        .components()
                        .filter_map(|component| match component {
                            Component::Normal(value) => value.to_str(),
                            _ => None,
                        }),
                );
            url.query_pairs_mut().append_pair("download", "true");
            self.download_https(&url, &destination, remaining, true)?;
            let downloaded = fs::metadata(&destination)?.len();
            remaining = remaining
                .checked_sub(downloaded)
                .ok_or(OciError::Artifact)?;
            if let Some(lfs) = file.lfs
                && (!lower_hex(&lfs.sha256, 64) || sha256_file(&destination)? != lfs.sha256)
            {
                return Err(OciError::Artifact);
            }
        }
        if remaining != 0 || selector_matches.iter().any(|matched| !matched) {
            return Err(OciError::Artifact);
        }
        Ok(())
    }

    fn download_https(
        &self,
        url: &Url,
        destination: &Path,
        maximum_bytes: u64,
        allow_huggingface_auth: bool,
    ) -> Result<(), OciError> {
        if maximum_bytes == 0 {
            return Err(OciError::Artifact);
        }
        let mut current = url.clone();
        for _ in 0..=5 {
            let endpoint = public_https_endpoint(&current)?;
            let mut arguments = curl_arguments(&current, destination, maximum_bytes, &endpoint);
            if allow_huggingface_auth
                && current.host_str() == Some("huggingface.co")
                && let Some(path) = self.huggingface_curl_config
            {
                validate_curl_config(path)?;
                arguments.splice(0..0, ["--config".to_owned(), path.display().to_string()]);
            }
            let output = self
                .runner
                .run(Program::Curl, &arguments, Duration::from_secs(3600))?;
            if !output.success {
                return Err(OciError::Artifact);
            }
            let status = std::str::from_utf8(&output.stdout)
                .map_err(|_| OciError::Artifact)?
                .trim_end_matches(['\r', '\n']);
            let (code, redirect) = status.split_once('\t').ok_or(OciError::Artifact)?;
            let code = code.parse::<u16>().map_err(|_| OciError::Artifact)?;
            if (200..300).contains(&code) {
                if fs::metadata(destination)?.len() > maximum_bytes {
                    return Err(OciError::Artifact);
                }
                return Ok(());
            }
            if !(300..400).contains(&code) || redirect.is_empty() {
                return Err(OciError::Artifact);
            }
            current = current.join(redirect).map_err(|_| OciError::Artifact)?;
        }
        Err(OciError::Artifact)
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ArtifactManifest {
    schema_version: u8,
    files: BTreeMap<String, String>,
    total_bytes: u64,
}

#[derive(Debug, Deserialize)]
struct HuggingFaceModel {
    siblings: Vec<HuggingFaceFile>,
}

#[derive(Debug, Deserialize)]
struct HuggingFaceFile {
    rfilename: String,
    lfs: Option<HuggingFaceLfs>,
}

#[derive(Debug, Deserialize)]
struct HuggingFaceLfs {
    sha256: String,
}

fn huggingface_repository(value: &str) -> Result<[&str; 2], OciError> {
    let parts = value.split('/').collect::<Vec<_>>();
    if parts.len() != 2
        || parts.iter().any(|part| {
            part.is_empty()
                || part.len() > 96
                || !part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        })
    {
        return Err(OciError::Artifact);
    }
    Ok([parts[0], parts[1]])
}

fn http_file_url(value: &str) -> Result<(Url, String), OciError> {
    if value
        .bytes()
        .any(|byte| byte.is_ascii_control() || byte == b'\\')
    {
        return Err(OciError::Artifact);
    }
    let url = Url::parse(value).map_err(|_| OciError::Artifact)?;
    let file_name = url
        .path()
        .rsplit('/')
        .next()
        .ok_or(OciError::Artifact)?
        .to_owned();
    if file_name.is_empty()
        || matches!(file_name.as_str(), "." | "..")
        || file_name.len() > MAX_HTTP_FILE_NAME_BYTES
        || !file_name.is_ascii()
        || file_name
            .bytes()
            .any(|byte| byte.is_ascii_control() || matches!(byte, b'%' | b'/' | b'\\'))
    {
        return Err(OciError::Artifact);
    }
    Ok((url, file_name))
}

fn normalize_legacy_ds4_spec(mut spec: WorkloadSpec) -> Result<WorkloadSpec, OciError> {
    let invalid = || OciError::Workload(WorkloadError::Invalid("legacy artifact mounts"));
    let authority = match spec.identity.recipe_revision_sha256.as_str() {
        "373169b0ef24f8d21b0aa40e918e13554bb4d788b4bd426df9f14b64b47d184a" => (
            "ac139f771cc97b27c1cf6fd97404b6a4db56d6d1725b4282cc5af0289a5421b3",
            "337c9d850a70b6a8907e588d4fee1d447f770bc004cb15bbc45283d017dca389",
            8080,
        ),
        "32f09e39052ec5c13292c9bec5577d8536690d74576a4c3ac6c8ef4cf493927e" => (
            "a1dbca13724678dbce47a1caff4a7ae4b6c557a6ac6ca5c0e3a99733fcc3f2b0",
            "73e2ec403510447cfbc067d0bdba20cfd941bd741e8b90a764edef3bae83c12a",
            8080,
        ),
        _ => return Err(invalid()),
    };
    if spec.identity.model_version_sha256 != LEGACY_DS4_MODEL_SHA256
        || spec.identity.harness_sha256 != authority.0
        || spec.identity.runtime_distribution_sha256 != authority.1
        || spec.identity.patch_bundle_sha256.is_some()
        || !spec.model_dependencies.is_empty()
        || spec.runtime.adapter != "ds4"
        || spec.runtime.adapter_version != 1
        || !spec.runtime.arguments.is_empty()
        || spec.runtime.placement_environment.is_some()
        || spec.runtime.environment.len() != 2
        || spec.runtime.environment[0].name != "DS4_LOG_LEVEL"
        || !matches!(
            &spec.runtime.environment[0].value,
            Some(ArgumentValue::String(value)) if value == "INFO"
        )
        || spec.runtime.environment[0].secret.is_some()
        || spec.runtime.environment[1].name != "HF_HUB_OFFLINE"
        || !matches!(
            &spec.runtime.environment[1].value,
            Some(ArgumentValue::String(value)) if value == "1"
        )
        || spec.runtime.environment[1].secret.is_some()
        || spec.artifacts.len() != 2
        || spec.endpoint.as_ref().is_none_or(|endpoint| {
            endpoint.protocol != "openai"
                || endpoint.port != authority.2
                || endpoint.model_aliases != ["deepseek-v4-flash"]
                || endpoint.health_path != "/v1/models"
        })
        || spec.job.is_some()
        || spec.topology.name != "solo"
        || spec.topology.node_count != 1
        || spec.topology.rank != 0
        || spec.topology.role != "entrypoint"
        || spec.security.devices != ["nvidia.com/gpu=all"]
        || spec.security.host_network
        || spec.security.privileged
        || !spec.security.capabilities.is_empty()
        || spec.security.user != "10001:10001"
        || !spec.lifecycle.pre_start.is_empty()
        || !spec.lifecycle.post_stop.is_empty()
        || spec.lifecycle.stop_timeout_seconds != 120
    {
        return Err(invalid());
    }

    let expected_artifacts = [
        (
            "target",
            LEGACY_DS4_TARGET_FILE,
            "sha256:ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0",
            86_720_111_488,
        ),
        (
            "drafter",
            LEGACY_DS4_DRAFTER_FILE,
            "sha256:7e319924541db3f7a163ed7e11d7532a70d48228ab59d36cb81e1d4511885360",
            5_989_114_272,
        ),
    ];
    let source_revision = "e7f04037032990db0346398d249baf9fb9df1ccc";
    for (artifact, (id, expected_file, revision, bytes)) in
        spec.artifacts.iter().zip(expected_artifacts)
    {
        let (url, file_name) = http_file_url(&artifact.repository)?;
        if file_name != expected_file
            || url.as_str() != artifact.repository
            || artifact.id != id
            || artifact.kind != "http.file"
            || artifact.repository
                != format!(
                    "https://huggingface.co/antirez/deepseek-v4-gguf/resolve/{source_revision}/{expected_file}"
                )
            || artifact.revision != revision
            || artifact.download_bytes != bytes
            || artifact.installed_bytes != bytes
            || artifact.mount.target != "/models"
            || !artifact.mount.read_only
            || artifact.roles != ["entrypoint"]
        {
            return Err(invalid());
        }
    }
    let expected_entrypoint = [
        "/opt/vonk/bin/ds4-serve",
        "--model",
        LEGACY_DS4_TARGET_FILE,
        "--mtp",
        LEGACY_DS4_DRAFTER_FILE,
        "--ctx",
        "131072",
        "--batched-session",
        "2",
        "--dspark",
        "--cuda",
        "--host",
        "0.0.0.0",
        "--port",
        "",
    ]
    .map(str::to_owned);
    let mut expected_entrypoint = expected_entrypoint.to_vec();
    expected_entrypoint[2] = format!("/models/{LEGACY_DS4_TARGET_FILE}");
    expected_entrypoint[4] = format!("/models/{LEGACY_DS4_DRAFTER_FILE}");
    *expected_entrypoint.last_mut().ok_or_else(&invalid)? = authority.2.to_string();
    if spec.runtime.entrypoint != expected_entrypoint {
        return Err(invalid());
    }
    spec.runtime.entrypoint[2] = format!("/models/target/{LEGACY_DS4_TARGET_FILE}");
    spec.runtime.entrypoint[4] = format!("/models/drafter/{LEGACY_DS4_DRAFTER_FILE}");
    for artifact in &mut spec.artifacts {
        artifact.mount.target = format!("/models/{}", artifact.id);
    }
    spec.validate()?;
    Ok(spec)
}

fn verify_or_migrate_cached_http_file(
    root: &Path,
    file_name: &str,
    artifact: &crate::workloads::ArtifactSpec,
) -> Result<(), OciError> {
    let manifest = read_manifest(root)?;
    remove_legacy_manifest_orphan(root)?;
    let observed = create_manifest(root)?;
    let expected_digest = artifact
        .revision
        .strip_prefix("sha256:")
        .ok_or(OciError::Artifact)?;
    // The payload rename and manifest replacement are individually atomic. A restart may observe
    // either name on either side, but both must still describe exactly the immutable artifact.
    let exact_file_is_current = |manifest: &ArtifactManifest| {
        if manifest.total_bytes != artifact.download_bytes || manifest.files.len() != 1 {
            return None;
        }
        let (name, digest) = manifest.files.first_key_value()?;
        if digest != expected_digest
            || (name != file_name && (file_name == "artifact" || name != "artifact"))
        {
            return None;
        }
        Some(name == file_name)
    };
    let manifest_is_current = exact_file_is_current(&manifest).ok_or(OciError::Artifact)?;
    let observed_is_current = exact_file_is_current(&observed).ok_or(OciError::Artifact)?;

    if !observed_is_current {
        let migrated_path = root.join(file_name);
        match fs::symlink_metadata(&migrated_path) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            _ => return Err(OciError::Artifact),
        }
        fs::rename(root.join("artifact"), migrated_path)?;
    }

    let migrated_manifest = create_manifest(root)?;
    if exact_file_is_current(&migrated_manifest) != Some(true) {
        return Err(OciError::Artifact);
    }
    if !manifest_is_current {
        atomic_write_artifact_manifest(root, &serde_json::to_vec(&migrated_manifest)?)?;
    }
    verify_manifest(root)?;
    File::open(root)?.sync_all()?;
    Ok(())
}

fn remove_legacy_manifest_orphan(root: &Path) -> Result<(), OciError> {
    let mut orphan = None;
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let Ok(name) = entry.file_name().into_string() else {
            continue;
        };
        let Some(pid) = name
            .strip_prefix("..vonk-manifest.json.")
            .and_then(|value| value.strip_suffix(".tmp"))
        else {
            continue;
        };
        if pid
            .parse::<u32>()
            .ok()
            .is_none_or(|value| value == 0 || value.to_string() != pid)
            || orphan.is_some()
        {
            return Err(OciError::Artifact);
        }
        let metadata = fs::symlink_metadata(entry.path())?;
        if !metadata.file_type().is_file()
            || metadata.file_type().is_symlink()
            || metadata.len() > 64 * 1024
        {
            return Err(OciError::Artifact);
        }
        orphan = Some(entry.path());
    }
    if let Some(path) = orphan {
        fs::remove_file(path)?;
        File::open(root)?.sync_all()?;
    }
    Ok(())
}

fn atomic_write_artifact_manifest(root: &Path, value: &[u8]) -> Result<(), OciError> {
    atomic_write_artifact_manifest_with(root, value, |directory| {
        File::open(directory)?.sync_all()?;
        Ok(())
    })
}

fn atomic_write_artifact_manifest_with<F>(
    root: &Path,
    value: &[u8],
    mut sync_directory: F,
) -> Result<(), OciError>
where
    F: FnMut(&Path) -> Result<(), OciError>,
{
    let parent = root.parent().ok_or(OciError::Artifact)?;
    let temporary = parent.join(format!(".vonk-manifest.{}.tmp", uuid::Uuid::new_v4()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(value)?;
    file.sync_all()?;
    fs::rename(temporary, root.join(".vonk-manifest.json"))?;
    sync_directory(root)?;
    sync_directory(parent)?;
    Ok(())
}

fn safe_relative_path(value: &str) -> Result<PathBuf, OciError> {
    if value.is_empty()
        || value.len() > 512
        || value.contains('\0')
        || value.contains('\\')
        || value.split('/').count() > 32
    {
        return Err(OciError::Artifact);
    }
    let path = PathBuf::from(value);
    if path
        .components()
        .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(OciError::Artifact);
    }
    Ok(path)
}

struct PublicEndpoint {
    hostname: String,
    port: u16,
    address: IpAddr,
}

fn public_https_endpoint(url: &Url) -> Result<PublicEndpoint, OciError> {
    public_https_endpoint_with(url, |hostname, port| {
        (hostname, port)
            .to_socket_addrs()
            .map_err(|_| OciError::Artifact)
            .map(|addresses| addresses.map(|address| address.ip()).collect())
    })
}

fn public_https_endpoint_with<F>(url: &Url, resolver: F) -> Result<PublicEndpoint, OciError>
where
    F: FnOnce(&str, u16) -> Result<Vec<IpAddr>, OciError>,
{
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err(OciError::Artifact);
    }
    let hostname = url.host_str().ok_or(OciError::Artifact)?.to_owned();
    let port = url.port_or_known_default().ok_or(OciError::Artifact)?;
    let addresses = resolver(&hostname, port)?;
    if addresses.is_empty() || addresses.iter().any(|address| !is_public_ip(*address)) {
        return Err(OciError::Artifact);
    }
    Ok(PublicEndpoint {
        hostname,
        port,
        address: addresses[0],
    })
}

fn oci_endpoint(repository: &str) -> Result<PublicEndpoint, OciError> {
    if repository.contains("://")
        || repository.contains('@')
        || repository.contains('?')
        || repository.contains('#')
    {
        return Err(OciError::Artifact);
    }
    let url = Url::parse(&format!("https://{repository}")).map_err(|_| OciError::Artifact)?;
    if url.host_str() != Some("ghcr.io")
        || url.path() == "/"
        || url.path().contains("//")
        || url.path().contains("..")
    {
        return Err(OciError::Artifact);
    }
    public_https_endpoint(&url)
}

fn is_public_ip(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => {
            let octets = address.octets();
            !address.is_private()
                && !address.is_loopback()
                && !address.is_link_local()
                && !address.is_broadcast()
                && !address.is_documentation()
                && !address.is_multicast()
                && !address.is_unspecified()
                && octets[0] != 0
                && !(octets[0] == 100 && (64..=127).contains(&octets[1]))
                && !(octets[0] == 192 && octets[1] == 0 && octets[2] == 0)
                && !(octets[0] == 198 && matches!(octets[1], 18 | 19))
                && octets[0] < 240
        }
        IpAddr::V6(address) => {
            if let Some(mapped) = address.to_ipv4_mapped() {
                return is_public_ip(IpAddr::V4(mapped));
            }
            let segments = address.segments();
            // Fail closed to ordinary globally routed unicast space. Exclude
            // the IETF special-purpose blocks at the start of 2001::/16,
            // deprecated 6to4, and the documentation allocation.
            segments[0] & 0xe000 == 0x2000
                && !(segments[0] == 0x2001 && segments[1] < 0x0200)
                && !(segments[0] == 0x2001 && segments[1] == 0x0db8)
                && segments[0] != 0x2002
                && segments[0] != 0x3ffe
                && !(segments[0] == 0x3fff && segments[1] & 0xf000 == 0)
        }
    }
}

fn curl_arguments(
    url: &Url,
    destination: &Path,
    maximum_bytes: u64,
    endpoint: &PublicEndpoint,
) -> Vec<String> {
    let resolve_address = match endpoint.address {
        IpAddr::V4(address) => address.to_string(),
        IpAddr::V6(address) => format!("[{address}]"),
    };
    vec![
        "--fail".to_owned(),
        "--proto".to_owned(),
        "=https".to_owned(),
        "--tlsv1.3".to_owned(),
        "--max-redirs".to_owned(),
        "0".to_owned(),
        "--max-filesize".to_owned(),
        maximum_bytes.to_string(),
        "--resolve".to_owned(),
        format!(
            "{}:{}:{}",
            endpoint.hostname, endpoint.port, resolve_address
        ),
        "--connect-timeout".to_owned(),
        "15".to_owned(),
        "--retry".to_owned(),
        "3".to_owned(),
        "--retry-all-errors".to_owned(),
        "--write-out".to_owned(),
        "%{http_code}\t%{redirect_url}\n".to_owned(),
        "--output".to_owned(),
        destination.display().to_string(),
        url.as_str().to_owned(),
    ]
}

fn validate_curl_config(path: &Path) -> Result<(), OciError> {
    let metadata = fs::symlink_metadata(path)?;
    let effective_uid = rustix::process::geteuid().as_raw();
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() > 4096
        || !matches!(metadata.uid(), 0) && metadata.uid() != effective_uid
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err(OciError::Artifact);
    }
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String, OciError> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn hook_arguments(main: &[String], image: &str, hook: &[String]) -> Result<Vec<String>, OciError> {
    if hook.is_empty() || main.first().map(String::as_str) != Some("run") {
        return Err(OciError::Runtime);
    }
    let image_index = main
        .iter()
        .position(|value| value == image)
        .ok_or(OciError::Runtime)?;
    let mut arguments = vec!["run".to_owned(), "--rm".to_owned()];
    let mut index = 1;
    while index < image_index {
        match main[index].as_str() {
            "--detach" => index += 1,
            "--name" | "--restart" | "--publish" => index += 2,
            _ => {
                arguments.push(main[index].clone());
                index += 1;
            }
        }
    }
    arguments.push(image.to_owned());
    arguments.extend(hook.iter().cloned());
    Ok(arguments)
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn artifact_key(artifact: &crate::workloads::ArtifactSpec) -> Result<String, OciError> {
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(artifact)?)))
}

fn spec_references_model(spec: &WorkloadSpec, model_version_sha256: &str) -> bool {
    spec.identity.model_version_sha256 == model_version_sha256
        || spec
            .model_dependencies
            .iter()
            .any(|dependency| dependency.content_sha256 == model_version_sha256)
}

fn create_manifest(root: &Path) -> Result<ArtifactManifest, OciError> {
    let mut files = BTreeMap::new();
    let mut total = 0_u64;
    visit_files(root, root, &mut files, &mut total)?;
    if files.is_empty() {
        return Err(OciError::Artifact);
    }
    Ok(ArtifactManifest {
        schema_version: 1,
        files,
        total_bytes: total,
    })
}

fn visit_files(
    root: &Path,
    directory: &Path,
    files: &mut BTreeMap<String, String>,
    total: &mut u64,
) -> Result<(), OciError> {
    let mut entries = fs::read_dir(directory)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(fs::DirEntry::file_name);
    for entry in entries {
        let metadata = entry.file_type()?;
        let path = entry.path();
        if metadata.is_symlink() {
            return Err(OciError::Artifact);
        }
        if metadata.is_dir() {
            visit_files(root, &path, files, total)?;
        } else if metadata.is_file() {
            let relative = path.strip_prefix(root).map_err(|_| OciError::Artifact)?;
            let name = relative
                .to_str()
                .ok_or(OciError::Artifact)?
                .replace('\\', "/");
            if name == ".vonk-manifest.json" {
                continue;
            }
            if name.contains("..") {
                return Err(OciError::Artifact);
            }
            let mut file = File::open(&path)?;
            let mut hasher = Sha256::new();
            let mut buffer = [0_u8; 64 * 1024];
            loop {
                let read = file.read(&mut buffer)?;
                if read == 0 {
                    break;
                }
                hasher.update(&buffer[..read]);
                *total = total.checked_add(read as u64).ok_or(OciError::Artifact)?;
            }
            files.insert(name, hex::encode(hasher.finalize()));
        } else {
            return Err(OciError::Artifact);
        }
    }
    Ok(())
}

fn verify_manifest(root: &Path) -> Result<(), OciError> {
    let expected = read_manifest(root)?;
    let observed = create_manifest(root)?;
    if expected.files != observed.files || expected.total_bytes != observed.total_bytes {
        return Err(OciError::Artifact);
    }
    Ok(())
}

fn read_manifest(root: &Path) -> Result<ArtifactManifest, OciError> {
    let raw = fs::read(root.join(".vonk-manifest.json"))?;
    if raw.len() > 64 * 1024 {
        return Err(OciError::Artifact);
    }
    let manifest: ArtifactManifest = serde_json::from_slice(&raw)?;
    if manifest.schema_version != 1 {
        return Err(OciError::Artifact);
    }
    Ok(manifest)
}

fn atomic_write(root: &Path, name: &str, value: &[u8]) -> Result<(), OciError> {
    let temporary: PathBuf = root.join(format!(".{name}.{}.tmp", std::process::id()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(value)?;
    file.sync_all()?;
    fs::rename(temporary, root.join(name))?;
    Ok(())
}

fn read_regular_file(path: &Path, maximum_bytes: u64) -> Result<Vec<u8>, OciError> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)?;
    let metadata = file.metadata()?;
    if !metadata.file_type().is_file() || metadata.len() > maximum_bytes {
        return Err(OciError::Artifact);
    }
    let mut value = Vec::with_capacity(metadata.len() as usize);
    Read::by_ref(&mut file)
        .take(maximum_bytes.saturating_add(1))
        .read_to_end(&mut value)?;
    if value.len() as u64 > maximum_bytes {
        return Err(OciError::Artifact);
    }
    Ok(value)
}

fn canonical_uuid(value: &str) -> bool {
    uuid::Uuid::parse_str(value).is_ok_and(|parsed| parsed.to_string() == value)
}

#[cfg(test)]
mod tests {
    use super::{
        OciError, atomic_write_artifact_manifest_with, is_public_ip, public_https_endpoint_with,
    };
    use std::{cell::RefCell, collections::VecDeque, fs, net::IpAddr};
    use tempfile::tempdir;
    use url::Url;

    #[test]
    fn artifact_manifest_syncs_destination_before_source_parent() {
        let directory = tempdir().unwrap();
        let artifact = directory.path().join("artifact");
        fs::create_dir(&artifact).unwrap();
        let synced = RefCell::new(Vec::new());

        atomic_write_artifact_manifest_with(&artifact, b"manifest", |path| {
            synced.borrow_mut().push(path.to_owned());
            Ok(())
        })
        .unwrap();

        assert_eq!(
            synced.into_inner(),
            vec![artifact.clone(), directory.path().to_owned()]
        );
        assert_eq!(
            fs::read(artifact.join(".vonk-manifest.json")).unwrap(),
            b"manifest"
        );
    }

    #[test]
    fn only_globally_routable_ipv6_is_public() {
        for value in [
            "::1",
            "fe80::1",
            "fec0::1",
            "fc00::1",
            "2001:db8::1",
            "3ffe::1",
        ] {
            assert!(!is_public_ip(value.parse::<IpAddr>().unwrap()), "{value}");
        }
        assert!(is_public_ip("2606:4700:4700::1111".parse().unwrap()));
    }

    #[test]
    fn endpoint_validation_rejects_private_and_changed_dns_answers() {
        let url = Url::parse("https://models.example/weights").unwrap();
        let answers = RefCell::new(VecDeque::from([
            vec!["93.184.216.34".parse().unwrap()],
            vec!["127.0.0.1".parse().unwrap()],
        ]));
        let resolve = |_: &str, _: u16| answers.borrow_mut().pop_front().ok_or(OciError::Artifact);

        let endpoint = public_https_endpoint_with(&url, resolve).unwrap();
        assert_eq!(endpoint.address, "93.184.216.34".parse::<IpAddr>().unwrap());
        assert!(public_https_endpoint_with(&url, resolve).is_err());
    }
}
