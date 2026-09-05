use std::{collections::BTreeSet, net::IpAddr, path::Path};

use serde::{Deserialize, Serialize};
use thiserror::Error;
use vonk_agent_protocol::DistributionObject;

#[derive(Debug, Error)]
pub enum WorkloadError {
    #[error("workload specification is invalid: {0}")]
    Invalid(&'static str),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WorkloadSpec {
    pub identity: WorkloadIdentitySpec,
    pub model_dependencies: Vec<ModelDependencySpec>,
    pub runtime: RuntimeSpec,
    pub artifacts: Vec<ArtifactSpec>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub endpoint: Option<EndpointSpec>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job: Option<JobSpec>,
    pub security: SecuritySpec,
    pub lifecycle: LifecycleSpec,
    pub topology: TopologySpec,
}

pub const EMPTY_SHA256: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

// These bounds carry the compiler's structured argv without treating engine
// arguments as container-engine options.  Keep the first item non-empty (it
// is the executable), while subsequent items are opaque values and may be
// empty.  The total bound prevents a large number of individually valid
// values from creating an unbounded launch request.
const MAX_ARGV_ITEMS: usize = 512;
const MAX_ARGV_ITEM_BYTES: usize = 65_536;
const MAX_ARGV_BYTES: usize = 1024 * 1024;
const MAX_RUNTIME_ARGUMENT_BYTES: usize = 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledExecutionPlan {
    pub schema_version: u8,
    pub identity: CompiledWorkloadIdentity,
    pub runtime: CompiledRuntime,
    pub artifacts: Vec<CompiledModelArtifact>,
    pub runtime_image: CompiledRuntimeImage,
    pub security: CompiledSecurity,
    pub topology: CompiledTopology,
    pub lifecycle: CompiledLifecycle,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub endpoint: Option<CompiledEndpoint>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job: Option<CompiledJob>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledWorkloadIdentity {
    pub recipe_revision_sha256: String,
    pub execution_sha256: String,
    pub harness_sha256: String,
    pub build_input_sha256: Option<String>,
    pub model_artifact_set_sha256: String,
    pub model_artifact_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledRuntime {
    pub executable: String,
    pub argv: Vec<String>,
    pub env: Vec<CompiledEnvironmentEntry>,
    pub image_digest: String,
    pub placement: CompiledRuntimePlacement,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledEnvironmentEntry {
    pub name: String,
    pub value: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledRuntimePlacement {
    pub endpoint_address: Option<IpAddr>,
    pub rank: u32,
    pub role: String,
    pub world_size: u32,
    pub local_address: Option<IpAddr>,
    pub master_address: Option<IpAddr>,
    pub master_port: Option<u16>,
    pub port: u16,
    pub reserved_memory_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledModelArtifact {
    pub selection_id: String,
    pub file_id: String,
    pub path: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub roles: Vec<String>,
    pub mount: CompiledArtifactMount,
    pub model: ModelArtifactIdentity,
    pub distribution_object: DistributionObject,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledArtifactMount {
    pub target: String,
    pub read_only: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ModelArtifactIdentity {
    pub publisher: String,
    pub slug: String,
    pub content_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledRuntimeImage {
    pub image_digest: String,
    pub oci_layout_sha256: String,
    pub image_bytes: u64,
    pub architecture: String,
    pub runtime_interface: String,
    pub source: String,
    pub build_id: Option<String>,
    pub distribution_object: DistributionObject,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledSecurity {
    pub devices: Vec<String>,
    pub capabilities: Vec<String>,
    pub host_network: bool,
    pub privileged: bool,
    pub user: String,
    pub mounts: Vec<MountSpec>,
    pub read_only_root: bool,
    pub no_new_privileges: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledTopology {
    pub name: String,
    pub mode: String,
    pub backend: String,
    pub node_count: u32,
    pub world_size: u32,
    pub rank: u32,
    pub role: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledLifecycle {
    pub pre_start: Vec<Vec<String>>,
    pub post_stop: Vec<Vec<String>>,
    pub stop_timeout_seconds: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledEndpoint {
    pub protocol: String,
    pub port: u16,
    pub model_aliases: Vec<String>,
    pub health_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CompiledJob {
    pub interface: String,
    pub input: Option<serde_json::Value>,
    pub output_path: String,
    pub timeout_seconds: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WorkloadIdentitySpec {
    pub recipe_revision_sha256: String,
    pub model_version_sha256: String,
    pub harness_sha256: String,
    pub runtime_distribution_sha256: String,
    pub patch_bundle_sha256: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ModelDependencySpec {
    pub kind: String,
    pub publisher: String,
    pub slug: String,
    pub content_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TopologySpec {
    pub name: String,
    pub node_count: u32,
    pub rank: u32,
    pub role: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeSpec {
    pub interface: String,
    pub adapter: String,
    pub adapter_version: u32,
    pub image: String,
    pub architecture: String,
    pub entrypoint: Vec<String>,
    pub arguments: Vec<RuntimeArgument>,
    pub environment: Vec<RuntimeEnvironment>,
    pub writable_paths: Vec<RuntimeWritablePath>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub placement_environment: Option<PlacementEnvironmentSpec>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeWritablePath {
    pub name: String,
    pub path: String,
    pub persistent: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PlacementEnvironmentSpec {
    pub local_address: String,
    pub master_address: String,
    pub master_port: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeEnvironment {
    pub name: String,
    pub value: Option<ArgumentValue>,
    pub secret: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RuntimeArgument {
    pub name: String,
    pub value: ArgumentValue,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(untagged)]
pub enum ArgumentValue {
    Boolean(bool),
    Integer(i64),
    String(String),
}

impl ArgumentValue {
    fn as_string(&self) -> Option<&str> {
        match self {
            Self::String(value) => Some(value),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactSpec {
    pub id: String,
    pub kind: String,
    pub repository: String,
    pub revision: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub include_paths: Vec<String>,
    pub download_bytes: u64,
    pub installed_bytes: u64,
    pub mount: ArtifactMountSpec,
    pub roles: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactMountSpec {
    pub target: String,
    pub read_only: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct EndpointSpec {
    pub protocol: String,
    pub port: u16,
    pub model_aliases: Vec<String>,
    pub health_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct JobSpec {
    pub interface: String,
    pub input: Option<serde_json::Value>,
    pub output_path: String,
    pub timeout_seconds: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SecuritySpec {
    pub devices: Vec<String>,
    pub capabilities: Vec<String>,
    pub host_network: bool,
    pub privileged: bool,
    pub user: String,
    pub mounts: Vec<MountSpec>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct LifecycleSpec {
    pub pre_start: Vec<Vec<String>>,
    pub post_stop: Vec<Vec<String>>,
    pub stop_timeout_seconds: u16,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MountSpec {
    pub source: String,
    pub target: String,
    pub read_only: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Placement {
    #[serde(default)]
    pub endpoint_address: Option<IpAddr>,
    pub rank: u32,
    pub role: String,
    pub world_size: u32,
    pub local_address: Option<IpAddr>,
    pub master_address: Option<IpAddr>,
    pub master_port: Option<u16>,
    pub port: u16,
    pub reserved_memory_bytes: u64,
}

impl WorkloadSpec {
    pub fn validate(&self) -> Result<(), WorkloadError> {
        if !lower_hex(&self.identity.recipe_revision_sha256, 64)
            || !lower_hex(&self.identity.model_version_sha256, 64)
            || !lower_hex(&self.identity.harness_sha256, 64)
            || !lower_hex(&self.identity.runtime_distribution_sha256, 64)
            || self
                .identity
                .patch_bundle_sha256
                .as_ref()
                .is_some_and(|value| !lower_hex(value, 64))
            || self.model_dependencies.len() > 16
            || self.model_dependencies.iter().any(|dependency| {
                dependency.kind != "model-version"
                    || !valid_name(&dependency.publisher)
                    || !valid_name(&dependency.slug)
                    || !lower_hex(&dependency.content_sha256, 64)
            })
            || !valid_name(&self.topology.name)
            || !(1..=128).contains(&self.topology.node_count)
            || self.topology.rank >= self.topology.node_count
            || !valid_role(&self.topology.role)
        {
            return Err(WorkloadError::Invalid("authority binding"));
        }
        self.runtime.validate()?;
        if self.artifacts.is_empty() || self.artifacts.len() > 16 {
            return Err(WorkloadError::Invalid("artifacts"));
        }
        let mut artifact_ids = BTreeSet::new();
        let mut artifact_targets = BTreeSet::new();
        for artifact in &self.artifacts {
            artifact.validate()?;
            if !artifact_ids.insert(artifact.id.as_str())
                || !artifact_targets.insert(artifact.mount.target.as_str())
            {
                return Err(WorkloadError::Invalid("artifact mounts"));
            }
        }
        if self.artifacts.len() > 1
            && self
                .artifacts
                .iter()
                .any(|artifact| artifact.mount.target != format!("/models/{}", artifact.id))
        {
            return Err(WorkloadError::Invalid("artifact mounts"));
        }
        match (&self.endpoint, &self.job) {
            (Some(endpoint), None)
                if endpoint.protocol == "openai"
                    && endpoint.port >= 1024
                    && !endpoint.model_aliases.is_empty()
                    && endpoint.health_path.len() <= 256
                    && endpoint.health_path.starts_with('/')
                    && !endpoint.health_path.contains("..") => {}
            (None, Some(job))
                if matches!(
                    job.interface.as_str(),
                    "image-job" | "audio-job" | "video-job" | "mesh-job" | "artifact-job"
                ) && job.input.as_ref().is_none_or(|input| {
                    input.is_object()
                        && input.get("path").and_then(serde_json::Value::as_str) == Some("/inputs")
                }) && job.output_path == "/outputs"
                    && (1..=3600).contains(&job.timeout_seconds)
                    && self.lifecycle.pre_start.is_empty()
                    && self.lifecycle.post_stop.is_empty() => {}
            _ => return Err(WorkloadError::Invalid("workload interface")),
        }
        if self.security.privileged
            || !self.security.capabilities.is_empty()
            || !canonical_runtime_mounts(&self.security.mounts, self.job.is_some())
            || self.job.is_some() && self.security.host_network
            || self
                .security
                .devices
                .iter()
                .any(|value| value != "nvidia.com/gpu=all")
            || !numeric_non_root_user(&self.security.user)
            || self.lifecycle.pre_start.len() > 16
            || self.lifecycle.post_stop.len() > 16
            || !(1..=600).contains(&self.lifecycle.stop_timeout_seconds)
            || self
                .lifecycle
                .pre_start
                .iter()
                .chain(&self.lifecycle.post_stop)
                .any(|argv| !valid_argv(argv))
        {
            return Err(WorkloadError::Invalid("security"));
        }
        Ok(())
    }
}

impl CompiledExecutionPlan {
    pub fn validate(&self) -> Result<(), WorkloadError> {
        if self.schema_version != 2
            || !lower_hex(&self.identity.recipe_revision_sha256, 64)
            || !lower_hex(&self.identity.harness_sha256, 64)
            || !lower_hex(&self.identity.execution_sha256, 64)
            || self
                .identity
                .build_input_sha256
                .as_ref()
                .is_some_and(|value| !lower_hex(value, 64))
            || !lower_hex(&self.identity.model_artifact_set_sha256, 64)
            || self.artifacts.is_empty()
            || self.artifacts.len() > 4096
            || self.runtime.image_digest != self.runtime_image.image_digest
            || self.runtime.placement.rank != self.topology.rank
            || self.runtime.placement.role != self.topology.role
            || self.runtime.placement.world_size != self.topology.world_size
            || self.topology.world_size == 0
            || self.topology.rank >= self.topology.world_size
            || self.topology.node_count == 0
            || self.topology.world_size < self.topology.node_count
            || self.endpoint.is_some() == self.job.is_some()
        {
            return Err(WorkloadError::Invalid("compiled execution identity"));
        }
        let mut selected = BTreeSet::new();
        let mut materialized = BTreeSet::new();
        let mut by_digest = std::collections::BTreeMap::new();
        for artifact in &self.artifacts {
            artifact.validate()?;
            if !selected.insert((artifact.selection_id.as_str(), artifact.file_id.as_str()))
                || !materialized.insert((artifact.selection_id.as_str(), artifact.path.as_str()))
            {
                return Err(WorkloadError::Invalid("compiled model artifact identity"));
            }
            if let Some(previous) = by_digest.insert(artifact.sha256.as_str(), artifact.size_bytes)
                && previous != artifact.size_bytes
            {
                return Err(WorkloadError::Invalid("compiled model artifact bytes"));
            }
        }
        let total = by_digest
            .values()
            .copied()
            .try_fold(0_u64, |sum, value| sum.checked_add(value))
            .ok_or(WorkloadError::Invalid("compiled model artifact bytes"))?;
        if total != self.identity.model_artifact_bytes {
            return Err(WorkloadError::Invalid("compiled model artifact-set bytes"));
        }
        self.runtime.validate()?;
        self.runtime_image.validate()?;
        self.security.validate()?;
        self.topology.validate()?;
        self.lifecycle.validate()?;
        match (&self.endpoint, &self.job) {
            (Some(endpoint), None) => endpoint.validate()?,
            (None, Some(job)) => job.validate()?,
            _ => unreachable!("validated endpoint/job discriminator"),
        }
        Ok(())
    }
}

impl CompiledModelArtifact {
    fn validate(&self) -> Result<(), WorkloadError> {
        if !valid_name(&self.selection_id)
            || !valid_name(&self.file_id)
            || !valid_model_path(&self.path)
            || !lower_hex(&self.sha256, 64)
            || (self.size_bytes == 0 && self.sha256 != EMPTY_SHA256)
            || !self.roles.is_empty()
                && (self.roles.windows(2).any(|pair| pair[0] >= pair[1])
                    || self.roles.iter().any(|role| !valid_role(role)))
            || !valid_name(&self.model.publisher)
            || !valid_name(&self.model.slug)
            || !lower_hex(&self.model.content_sha256, 64)
            || self.distribution_object.kind != "model"
            || self.distribution_object.name != self.path
            || self.distribution_object.sha256 != self.sha256
            || self.distribution_object.bytes != self.size_bytes
            || !(self.mount.target == "/models" || self.mount.target.starts_with("/models/"))
            || !self.mount.read_only
            || (self.size_bytes == 0
                && self
                    .roles
                    .iter()
                    .any(|role| matches!(role.as_str(), "model" | "weight" | "weights")))
        {
            return Err(WorkloadError::Invalid("compiled model artifact"));
        }
        if self.roles.is_empty() {
            return Err(WorkloadError::Invalid("compiled model artifact roles"));
        }
        if self.size_bytes > 0 {
            self.distribution_object
                .validate()
                .map_err(|_| WorkloadError::Invalid("compiled model distribution object"))?;
        }
        Ok(())
    }
}

impl CompiledRuntime {
    fn validate(&self) -> Result<(), WorkloadError> {
        if self.executable.is_empty()
            || !self.executable.starts_with('/')
            || self.executable.len() > MAX_ARGV_ITEM_BYTES
            || self.executable.contains(['\0', '\r', '\n'])
            || !valid_opaque_argv(&self.argv)
            || self.env.len() > 128
        {
            return Err(WorkloadError::Invalid("compiled runtime"));
        }
        for entry in &self.env {
            if entry.name.is_empty()
                || entry.name.len() > 128
                || !entry.name.bytes().enumerate().all(|(index, byte)| {
                    if index == 0 {
                        byte.is_ascii_uppercase()
                    } else {
                        byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_'
                    }
                })
                || entry.value.len() > MAX_ARGV_ITEM_BYTES
                || entry.value.contains('\0')
            {
                return Err(WorkloadError::Invalid("compiled runtime environment"));
            }
        }
        if self.placement.world_size == 0
            || self.placement.rank >= self.placement.world_size
            || !valid_role(&self.placement.role)
            || self.placement.port == 0
            || self.placement.reserved_memory_bytes == 0
        {
            return Err(WorkloadError::Invalid("compiled runtime placement"));
        }
        Ok(())
    }
}

impl CompiledSecurity {
    fn validate(&self) -> Result<(), WorkloadError> {
        if self.privileged
            || !self.no_new_privileges
            || !self.read_only_root
            || !self.capabilities.is_empty()
            || self
                .devices
                .iter()
                .any(|value| value != "nvidia.com/gpu=all")
            || !numeric_non_root_user(&self.user)
            || self.mounts.len() > 4
            || self.mounts.iter().any(|mount| {
                !mount.read_only && mount.target != "/outputs"
                    || mount.read_only && !matches!(mount.target.as_str(), "/models" | "/inputs")
                    || !matches!(mount.source.as_str(), "model" | "inputs" | "outputs")
            })
        {
            return Err(WorkloadError::Invalid("compiled security"));
        }
        Ok(())
    }
}

impl CompiledTopology {
    fn validate(&self) -> Result<(), WorkloadError> {
        if !valid_name(&self.name)
            || !matches!(self.mode.as_str(), "single" | "distributed")
            || !matches!(self.backend.as_str(), "local" | "nccl" | "gloo")
            || self.node_count == 0
            || self.world_size == 0
            || self.rank >= self.world_size
            || !valid_role(&self.role)
        {
            return Err(WorkloadError::Invalid("compiled topology"));
        }
        Ok(())
    }
}

impl CompiledLifecycle {
    fn validate(&self) -> Result<(), WorkloadError> {
        if self.pre_start.len() > 16
            || self.post_stop.len() > 16
            || !(1..=600).contains(&self.stop_timeout_seconds)
            || self
                .pre_start
                .iter()
                .chain(&self.post_stop)
                .any(|argv| !valid_argv(argv))
        {
            return Err(WorkloadError::Invalid("compiled lifecycle"));
        }
        Ok(())
    }
}

impl CompiledEndpoint {
    fn validate(&self) -> Result<(), WorkloadError> {
        if self.protocol != "openai"
            || self.port < 1024
            || self.model_aliases.is_empty()
            || self.health_path.len() > 256
            || !self.health_path.starts_with('/')
            || self.health_path.contains("..")
        {
            return Err(WorkloadError::Invalid("compiled endpoint"));
        }
        Ok(())
    }
}

impl CompiledJob {
    fn validate(&self) -> Result<(), WorkloadError> {
        if !matches!(
            self.interface.as_str(),
            "image-job" | "audio-job" | "video-job" | "mesh-job" | "artifact-job"
        ) || self.output_path != "/outputs"
            || !(1..=3600).contains(&self.timeout_seconds)
            || self.input.as_ref().is_some_and(|input| {
                !input.is_object()
                    || input.get("path").and_then(serde_json::Value::as_str) != Some("/inputs")
            })
        {
            return Err(WorkloadError::Invalid("compiled job"));
        }
        Ok(())
    }
}

impl CompiledRuntimeImage {
    fn validate(&self) -> Result<(), WorkloadError> {
        if !self.image_digest.starts_with("sha256:")
            || !lower_hex(&self.image_digest[7..], 64)
            || !lower_hex(&self.oci_layout_sha256, 64)
            || self.image_bytes == 0
            || self.architecture != "linux-arm64"
            || self.runtime_interface != "vonk.runtime.v1"
            || !matches!(self.source.as_str(), "published" | "controller-build")
            || (self.source == "published" && self.build_id.is_some())
            || (self.source == "controller-build"
                && self.build_id.as_deref().is_none_or(str::is_empty))
            || self.distribution_object.kind != "oci-archive"
            || self.distribution_object.name != "image.oci.tar"
            || self.distribution_object.sha256 != self.oci_layout_sha256
            || self.distribution_object.bytes != self.image_bytes
        {
            return Err(WorkloadError::Invalid("compiled runtime image"));
        }
        self.distribution_object
            .validate()
            .map_err(|_| WorkloadError::Invalid("compiled image distribution object"))
    }
}

pub fn materialized_model_path(
    root: &Path,
    artifact: &CompiledModelArtifact,
) -> Result<std::path::PathBuf, WorkloadError> {
    if !root.is_absolute() {
        return Err(WorkloadError::Invalid("compiled model root"));
    }
    artifact.validate()?;
    let relative = Path::new(&artifact.selection_id).join(&artifact.path);
    if relative
        .components()
        .any(|component| !matches!(component, std::path::Component::Normal(_)))
    {
        return Err(WorkloadError::Invalid("compiled model path"));
    }
    Ok(root.join(relative))
}

impl RuntimeSpec {
    fn validate(&self) -> Result<(), WorkloadError> {
        if self.interface != "vonk.runtime.v1"
            || !valid_name(&self.adapter)
            || self.adapter_version == 0
            || self.architecture != "linux/arm64"
            || image_digest(&self.image).is_none()
            || !valid_argv(&self.entrypoint)
            || self.arguments.len() > 128
            || self.environment.len() > 128
        {
            return Err(WorkloadError::Invalid("runtime"));
        }
        let argument_bytes = self.arguments.iter().try_fold(0_usize, |total, argument| {
            total
                .checked_add(argument.name.len())
                .and_then(|value| value.checked_add(argument.value.as_string().map_or(0, str::len)))
        });
        if argument_bytes.is_none_or(|bytes| bytes > MAX_RUNTIME_ARGUMENT_BYTES) {
            return Err(WorkloadError::Invalid("runtime argument size"));
        }
        for argument in &self.arguments {
            if argument.name.is_empty()
                || argument.name.len() > 64
                || !argument.name.bytes().enumerate().all(|(index, byte)| {
                    if index == 0 {
                        byte.is_ascii_lowercase()
                    } else {
                        byte.is_ascii_lowercase()
                            || byte.is_ascii_digit()
                            || matches!(byte, b'_' | b'-')
                    }
                })
                || matches!(&argument.value, ArgumentValue::String(value) if value.len() > MAX_ARGV_ITEM_BYTES || value.contains('\0'))
            {
                return Err(WorkloadError::Invalid("runtime argument"));
            }
        }
        for environment in &self.environment {
            if environment.name.is_empty()
                || environment.name.len() > 128
                || !environment.name.bytes().enumerate().all(|(index, byte)| {
                    if index == 0 {
                        byte.is_ascii_uppercase()
                    } else {
                        byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_'
                    }
                })
                || (environment.value.is_some() == environment.secret.is_some())
                || environment
                    .secret
                    .as_ref()
                    .is_some_and(|name| !valid_name(name))
            {
                return Err(WorkloadError::Invalid("runtime environment"));
            }
        }
        validate_runtime_writable_paths(
            self.adapter.as_str(),
            &self.writable_paths,
            &self.environment,
        )?;
        if self.placement_environment.as_ref().is_some_and(|binding| {
            binding.local_address != "VONK_LOCAL_ADDR"
                || binding.master_address != "VONK_MASTER_ADDR"
                || binding.master_port != "VONK_MASTER_PORT"
        }) {
            return Err(WorkloadError::Invalid("runtime placement environment"));
        }
        Ok(())
    }
}

fn validate_runtime_writable_paths(
    adapter: &str,
    paths: &[RuntimeWritablePath],
    environment: &[RuntimeEnvironment],
) -> Result<(), WorkloadError> {
    const COMMON: &[(&str, &str, bool)] = &[
        ("home", "/outputs/cache/home", true),
        ("xdg-cache", "/outputs/cache", true),
        ("xdg-config", "/outputs/cache/config", true),
        ("temporary", "/outputs/tmp", false),
    ];
    const PYTHON: &[(&str, &str, bool)] = &[
        ("huggingface", "/outputs/cache/huggingface", true),
        (
            "transformers",
            "/outputs/cache/huggingface/transformers",
            true,
        ),
        ("torch", "/outputs/cache/torch", true),
        ("torch-extensions", "/outputs/cache/torch_extensions", true),
        ("torchinductor", "/outputs/cache/torchinductor", true),
        ("triton", "/outputs/cache/triton", true),
        ("cuda", "/outputs/cache/cuda", true),
        ("uv", "/outputs/cache/uv", true),
    ];
    const VLLM: &[(&str, &str, bool)] = &[("vllm", "/outputs/cache/vllm", true)];
    let expected_paths: Vec<(&str, &str, bool)> = match adapter {
        "vllm" => COMMON.iter().chain(PYTHON).chain(VLLM).copied().collect(),
        "sglang" | "tensorrt-llm" | "ds4" | "diffusers" | "comfyui" | "pytorch-pipeline" => {
            COMMON.iter().chain(PYTHON).copied().collect()
        }
        "llama-cpp" => COMMON.to_vec(),
        _ => return Err(WorkloadError::Invalid("runtime writable paths")),
    };
    if paths.len() != expected_paths.len() || paths.len() > 64 {
        return Err(WorkloadError::Invalid("runtime writable paths"));
    }
    let mut names = BTreeSet::new();
    let mut values = BTreeSet::new();
    for (path, expected) in paths.iter().zip(expected_paths) {
        if !valid_name(&path.name)
            || !names.insert(path.name.as_str())
            || path.name != expected.0
            || path.path != expected.1
            || path.persistent != expected.2
            || !path.path.starts_with("/outputs/")
            || path.path.contains("//")
            || path.path.split('/').any(|part| matches!(part, "." | ".."))
            || path.path.ends_with('/')
            || !values.insert(path.path.as_str())
        {
            return Err(WorkloadError::Invalid("runtime writable paths"));
        }
    }
    let expected: Vec<(&str, &str)> = match adapter {
        "vllm" => [
            ("HOME", "/outputs/cache/home"),
            ("XDG_CACHE_HOME", "/outputs/cache"),
            ("XDG_CONFIG_HOME", "/outputs/cache/config"),
            ("TMPDIR", "/outputs/tmp"),
            ("HF_HOME", "/outputs/cache/huggingface"),
            (
                "TRANSFORMERS_CACHE",
                "/outputs/cache/huggingface/transformers",
            ),
            ("VLLM_CACHE_ROOT", "/outputs/cache/vllm"),
            ("TRITON_CACHE_DIR", "/outputs/cache/triton"),
            ("TORCH_HOME", "/outputs/cache/torch"),
            ("TORCH_EXTENSIONS_DIR", "/outputs/cache/torch_extensions"),
            ("TORCHINDUCTOR_CACHE_DIR", "/outputs/cache/torchinductor"),
            ("CUDA_CACHE_PATH", "/outputs/cache/cuda"),
            ("UV_CACHE_DIR", "/outputs/cache/uv"),
        ]
        .into_iter()
        .collect(),
        "llama-cpp" => [
            ("HOME", "/outputs/cache/home"),
            ("XDG_CACHE_HOME", "/outputs/cache"),
            ("XDG_CONFIG_HOME", "/outputs/cache/config"),
            ("TMPDIR", "/outputs/tmp"),
        ]
        .into_iter()
        .collect(),
        "sglang" | "tensorrt-llm" | "ds4" | "diffusers" | "comfyui" | "pytorch-pipeline" => [
            ("HOME", "/outputs/cache/home"),
            ("XDG_CACHE_HOME", "/outputs/cache"),
            ("XDG_CONFIG_HOME", "/outputs/cache/config"),
            ("TMPDIR", "/outputs/tmp"),
            ("HF_HOME", "/outputs/cache/huggingface"),
            (
                "TRANSFORMERS_CACHE",
                "/outputs/cache/huggingface/transformers",
            ),
            ("TORCH_HOME", "/outputs/cache/torch"),
            ("TORCH_EXTENSIONS_DIR", "/outputs/cache/torch_extensions"),
            ("TORCHINDUCTOR_CACHE_DIR", "/outputs/cache/torchinductor"),
            ("TRITON_CACHE_DIR", "/outputs/cache/triton"),
        ]
        .into_iter()
        .collect(),
        _ => return Err(WorkloadError::Invalid("runtime writable environment")),
    };
    for (name, expected_value) in &expected {
        let Some(value) = environment.iter().find(|item| item.name == *name) else {
            return Err(WorkloadError::Invalid("runtime writable environment"));
        };
        if value.value.as_ref().and_then(ArgumentValue::as_string) != Some(expected_value) {
            return Err(WorkloadError::Invalid("runtime writable environment"));
        }
    }
    for item in environment {
        let optional_path = matches!(
            item.name.as_str(),
            "FLASHINFER_WORKSPACE_BASE"
                | "VLLM_FLASHINFER_AUTOTUNE_CACHE_DIR"
                | "TILELANG_CACHE_DIR"
                | "TILELANG_TMP_DIR"
                | "B12X_CUTE_COMPILE_CACHE_DIR"
                | "TVM_CACHE_DIR"
                | "TVM_FFI_CACHE_DIR"
                | "TORCH_FR_DUMP_TEMP_FILE"
                | "TORCH_NCCL_DEBUG_INFO_PIPE_FILE"
        );
        if optional_path {
            let Some(value) = item.value.as_ref().and_then(ArgumentValue::as_string) else {
                return Err(WorkloadError::Invalid("runtime writable environment"));
            };
            if !value.starts_with("/outputs/cache/")
                || value.contains("//")
                || value.split('/').any(|part| matches!(part, "." | ".."))
            {
                return Err(WorkloadError::Invalid("runtime writable environment"));
            }
            continue;
        }
        if let Some((_, expected_value)) = expected.iter().find(|(name, _)| *name == item.name)
            && item.value.as_ref().and_then(ArgumentValue::as_string) != Some(*expected_value)
        {
            return Err(WorkloadError::Invalid("runtime writable environment"));
        }
    }
    Ok(())
}

impl ArtifactSpec {
    fn validate(&self) -> Result<(), WorkloadError> {
        if self.repository.is_empty()
            || self.repository.len() > 512
            || self.repository.contains('\0')
            || self.download_bytes == 0
            || self.installed_bytes == 0
            || !valid_name(&self.id)
            || matches!(self.id.as_str(), "." | "..")
            || self.roles.is_empty()
            || self.roles.iter().any(|role| !valid_role(role))
            || self.include_paths.len() > 256
            || self.include_paths.iter().enumerate().any(|(index, path)| {
                !valid_snapshot_selector(path)
                    || self.include_paths[..index].iter().any(|seen| seen >= path)
            })
            || self.kind != "huggingface.snapshot" && !self.include_paths.is_empty()
            || !self.mount.read_only
            || (self.mount.target != "/models"
                && self.mount.target != format!("/models/{}", self.id))
        {
            return Err(WorkloadError::Invalid("artifact"));
        }
        let immutable = match self.kind.as_str() {
            "huggingface.snapshot" => {
                lower_hex(&self.revision, 40) || lower_hex(&self.revision, 64)
            }
            "http.file" | "oci.artifact" => self
                .revision
                .strip_prefix("sha256:")
                .is_some_and(|value| lower_hex(value, 64)),
            _ => false,
        };
        if !immutable {
            return Err(WorkloadError::Invalid("artifact revision"));
        }
        Ok(())
    }
}

fn valid_snapshot_selector(value: &str) -> bool {
    let path = value.strip_suffix('/').unwrap_or(value);
    !path.is_empty()
        && path.len() <= 512
        && !path.contains(['\\', '\0', '*', '?', '[', ']'])
        && path.split('/').count() <= 32
        && path.split('/').all(|part| {
            !part.is_empty()
                && !matches!(part, "." | "..")
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        })
}

fn valid_model_path(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 512
        && !value.contains(['\\', '\0'])
        && value.split('/').all(|part| {
            !part.is_empty()
                && !matches!(part, "." | "..")
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        })
}

impl Placement {
    pub fn validate(&self) -> Result<(), WorkloadError> {
        if self.rank >= self.world_size
            || self.world_size == 0
            || !valid_role(&self.role)
            || self.port < 1024
            || self.reserved_memory_bytes == 0
            || if self.world_size == 1 {
                self.local_address.is_some()
                    || self.master_address.is_some()
                    || self.master_port.is_some()
                    || self.rank != 0
            } else {
                self.local_address.is_none()
                    || self.master_address.is_none()
                    || self.master_port.is_none_or(|port| port < 1024)
            }
        {
            return Err(WorkloadError::Invalid("placement"));
        }
        Ok(())
    }
}

pub fn image_digest(image: &str) -> Option<&str> {
    let (name, digest) = image.rsplit_once("@sha256:")?;
    if name.is_empty()
        || name.len() > 512
        || name
            .bytes()
            .any(|byte| byte.is_ascii_whitespace() || byte == 0)
        || !lower_hex(digest, 64)
    {
        return None;
    }
    Some(digest)
}

pub fn managed_path(
    root: &Path,
    category: &str,
    identifier: &str,
) -> Result<std::path::PathBuf, WorkloadError> {
    if !matches!(category, "installations" | "models" | "runs")
        || identifier.is_empty()
        || identifier.len() > 128
        || !identifier
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(WorkloadError::Invalid("managed path"));
    }
    Ok(root.join(category).join(identifier))
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_role(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.bytes().enumerate().all(|(index, byte)| {
            if index == 0 {
                byte.is_ascii_lowercase()
            } else {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
            }
        })
}

fn valid_name(value: &str) -> bool {
    valid_role(value)
        || !value.is_empty()
            && value.len() <= 64
            && value.bytes().all(|byte| {
                byte.is_ascii_lowercase()
                    || byte.is_ascii_digit()
                    || matches!(byte, b'.' | b'_' | b'-')
            })
}

fn valid_argv(value: &[String]) -> bool {
    !value.is_empty()
        && value.len() <= MAX_ARGV_ITEMS
        && !value[0].is_empty()
        && value
            .iter()
            .all(|item| item.len() <= MAX_ARGV_ITEM_BYTES && !item.contains('\0'))
        && value
            .iter()
            .try_fold(0_usize, |total, item| total.checked_add(item.len()))
            .is_some_and(|bytes| bytes <= MAX_ARGV_BYTES)
}

fn valid_opaque_argv(value: &[String]) -> bool {
    value.len() <= MAX_ARGV_ITEMS
        && value
            .iter()
            .all(|item| item.len() <= MAX_ARGV_ITEM_BYTES && !item.contains('\0'))
        && value
            .iter()
            .try_fold(0_usize, |total, item| total.checked_add(item.len()))
            .is_some_and(|bytes| bytes <= MAX_ARGV_BYTES)
}

fn numeric_non_root_user(value: &str) -> bool {
    let mut parts = value.split(':');
    let valid = |part: &str| {
        !part.is_empty() && !part.starts_with('0') && part.bytes().all(|byte| byte.is_ascii_digit())
    };
    valid(parts.next().unwrap_or_default())
        && parts.next().is_none_or(valid)
        && parts.next().is_none()
}

fn canonical_runtime_mounts(mounts: &[MountSpec], job: bool) -> bool {
    mounts.len() == if job { 3 } else { 2 }
        && mounts
            .iter()
            .any(|mount| mount.source == "model" && mount.target == "/models" && mount.read_only)
        && (!job
            || mounts.iter().any(|mount| {
                mount.source == "inputs" && mount.target == "/inputs" && mount.read_only
            }))
        && mounts.iter().any(|mount| {
            mount.source == "outputs" && mount.target == "/outputs" && !mount.read_only
        })
}
