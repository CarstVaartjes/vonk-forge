use std::{collections::BTreeSet, net::IpAddr, path::Path};

use serde::{Deserialize, Serialize};
use thiserror::Error;
use vonk_agent_protocol::DistributionObject;

#[derive(Debug, Error)]
pub enum WorkloadError {
    #[error("workload specification is invalid: {0}")]
    Invalid(&'static str),
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

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
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
    pub endpoint: Option<CompiledEndpoint>,
    pub job: Option<CompiledJob>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CompiledExecutionPlanWire {
    schema_version: u8,
    identity: CompiledWorkloadIdentity,
    runtime: CompiledRuntime,
    artifacts: Vec<CompiledModelArtifact>,
    runtime_image: CompiledRuntimeImage,
    security: CompiledSecurity,
    topology: CompiledTopology,
    lifecycle: CompiledLifecycle,
    endpoint: RequiredOption<CompiledEndpoint>,
    job: RequiredOption<CompiledJob>,
}

#[derive(Debug)]
struct RequiredOption<T>(Option<T>);

impl<'de, T> Deserialize<'de> for RequiredOption<T>
where
    T: Deserialize<'de>,
{
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        Ok(Self(Option::<T>::deserialize(deserializer)?))
    }
}

impl<'de> Deserialize<'de> for CompiledExecutionPlan {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let value = serde_json::Value::deserialize(deserializer)?;
        let object = value.as_object().ok_or_else(|| {
            <D::Error as serde::de::Error>::custom("compiled plan is not an object")
        })?;
        if !object.contains_key("endpoint") {
            return Err(<D::Error as serde::de::Error>::missing_field("endpoint"));
        }
        if !object.contains_key("job") {
            return Err(<D::Error as serde::de::Error>::missing_field("job"));
        }
        let wire: CompiledExecutionPlanWire =
            serde_json::from_value(value).map_err(<D::Error as serde::de::Error>::custom)?;
        Ok(Self {
            schema_version: wire.schema_version,
            identity: wire.identity,
            runtime: wire.runtime,
            artifacts: wire.artifacts,
            runtime_image: wire.runtime_image,
            security: wire.security,
            topology: wire.topology,
            lifecycle: wire.lifecycle,
            endpoint: wire.endpoint.0,
            job: wire.job.0,
        })
    }
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
    pub registry_manifest_digest: Option<String>,
    pub platform_manifest_digest: String,
    pub local_image_config_id: String,
    pub runtime_interface_label: String,
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
    pub network_mode: String,
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
            || self.network_mode != "none"
            || !self.capabilities.is_empty()
            || self
                .devices
                .iter()
                .any(|value| value != "nvidia.com/gpu=all")
            || !numeric_non_root_user(&self.user)
            || self.mounts.len() > 4
            || self.mounts.iter().any(|mount| {
                !mount.read_only && mount.target != "/outputs"
                    || mount.read_only
                        && !(mount.target == "/inputs"
                            || mount.target == "/models"
                            || mount.target.starts_with("/models/"))
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
            || self
                .registry_manifest_digest
                .as_ref()
                .is_some_and(|value| !valid_sha256_prefixed(value))
            || !valid_sha256_prefixed(&self.platform_manifest_digest)
            || !valid_sha256_prefixed(&self.local_image_config_id)
            || self.platform_manifest_digest != self.image_digest
            || self.runtime_interface_label.is_empty()
            || self.runtime_interface_label.len() > 128
            || !lower_hex(&self.oci_layout_sha256, 64)
            || self.image_bytes == 0
            || self.architecture != "linux-arm64"
            || self.runtime_interface != "vonk.runtime.v1"
            || !matches!(self.source.as_str(), "published" | "controller-build")
            || (self.source == "published" && self.build_id.is_some())
            || (self.source == "published" && self.registry_manifest_digest.is_none())
            || (self.source == "controller-build"
                && self.build_id.as_deref().is_none_or(str::is_empty))
            || (self.source == "controller-build" && self.registry_manifest_digest.is_some())
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

fn valid_sha256_prefixed(value: &str) -> bool {
    value.starts_with("sha256:") && lower_hex(&value[7..], 64)
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
