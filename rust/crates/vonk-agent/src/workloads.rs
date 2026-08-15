use std::{net::IpAddr, path::Path};

use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum WorkloadError {
    #[error("workload specification is invalid: {0}")]
    Invalid(&'static str),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WorkloadSpec {
    pub runtime: RuntimeSpec,
    pub artifacts: Vec<ArtifactSpec>,
    pub endpoint: EndpointSpec,
    pub security: SecuritySpec,
    pub lifecycle: LifecycleSpec,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub placement_environment: Option<PlacementEnvironmentSpec>,
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

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactSpec {
    pub id: String,
    pub kind: String,
    pub repository: String,
    pub revision: String,
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
        self.runtime.validate()?;
        if self.artifacts.is_empty() || self.artifacts.len() > 16 {
            return Err(WorkloadError::Invalid("artifacts"));
        }
        for artifact in &self.artifacts {
            artifact.validate()?;
        }
        if self.endpoint.protocol != "openai"
            || self.endpoint.port < 1024
            || self.endpoint.model_aliases.is_empty()
            || self.endpoint.health_path.len() > 256
            || !self.endpoint.health_path.starts_with('/')
            || self.endpoint.health_path.contains("..")
        {
            return Err(WorkloadError::Invalid("endpoint"));
        }
        if self.security.privileged
            || !self.security.capabilities.is_empty()
            || !canonical_runtime_mounts(&self.security.mounts)
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
                || matches!(&argument.value, ArgumentValue::String(value) if value.len() > 1024 || value.contains('\0'))
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

impl ArtifactSpec {
    fn validate(&self) -> Result<(), WorkloadError> {
        if self.repository.is_empty()
            || self.repository.len() > 512
            || self.repository.contains('\0')
            || self.download_bytes == 0
            || self.installed_bytes == 0
            || !valid_name(&self.id)
            || self.roles.is_empty()
            || self.roles.iter().any(|role| !valid_role(role))
            || !self.mount.target.starts_with('/')
            || self.mount.target.contains("..")
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
        && value.len() <= 64
        && value
            .iter()
            .all(|item| !item.is_empty() && item.len() <= 512 && !item.contains('\0'))
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

fn canonical_runtime_mounts(mounts: &[MountSpec]) -> bool {
    mounts.len() == 2
        && mounts
            .iter()
            .any(|mount| mount.source == "model" && mount.target == "/models" && mount.read_only)
        && mounts
            .iter()
            .any(|mount| mount.source == "state" && mount.target == "/state" && !mount.read_only)
}
