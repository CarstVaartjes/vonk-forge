#![forbid(unsafe_code)]

use std::collections::BTreeMap;

use chrono::{DateTime, FixedOffset};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::Value;
use sha2::{Digest, Sha256};
use thiserror::Error;
use uuid::Uuid;

pub const MAX_HOST_RUNTIME_ARGUMENTS: usize = 512;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum HostRuntimeAction {
    ImageImport,
    ImageInspect,
    RunInspect,
    Start,
    Stop,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct HostRuntimeRequest {
    pub schema_version: u8,
    pub action: HostRuntimeAction,
    pub job_id: Uuid,
    pub operation_id: Uuid,
    pub attempt: u32,
    pub fence: Uuid,
    pub arguments: Vec<String>,
}

impl HostRuntimeRequest {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1
            || self.attempt == 0
            || self.arguments.is_empty()
            || self.arguments.len() > MAX_HOST_RUNTIME_ARGUMENTS
            || self.arguments.iter().any(|value| {
                value.is_empty() || value.len() > 4096 || value.contains(['\0', '\r', '\n'])
            })
        {
            return Err(ProtocolError::Identity("host runtime request"));
        }
        Ok(())
    }
}

#[derive(Debug, Error)]
pub enum ProtocolError {
    #[error("protocol JSON is invalid")]
    Json(#[from] serde_json::Error),
    #[error("protocol identity is invalid: {0}")]
    Identity(&'static str),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AgentClaim {
    pub attempt: u32,
    pub base_commit: String,
    pub deadline: DateTime<FixedOffset>,
    pub fence: Uuid,
    pub job_id: Uuid,
    pub node_id: String,
    pub operation: String,
    pub operation_id: Uuid,
    pub payload: Value,
    pub payload_digest: String,
    pub schema_version: u8,
}

impl AgentClaim {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1 || self.attempt == 0 {
            return Err(ProtocolError::Identity("claim version or attempt"));
        }
        if !valid_node_id(&self.node_id) || !lower_hex(&self.base_commit, 40) {
            return Err(ProtocolError::Identity("claim node or authority"));
        }
        if !matches!(
            self.operation.as_str(),
            "recipe.build.v1"
                | "recipe.image.import.v1"
                | "recipe.install"
                | "recipe.start"
                | "recipe.stop"
                | "recipe.uninstall"
        ) {
            return Err(ProtocolError::Identity("claim operation"));
        }
        let payload = canonical_json(&self.payload)?;
        if hex_sha256(&payload) != self.payload_digest {
            return Err(ProtocolError::Identity("claim payload digest"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AgentProgress {
    pub attempt: u32,
    pub deadline: DateTime<FixedOffset>,
    pub fence: Uuid,
    pub job_id: Uuid,
    pub node_id: String,
    pub operation_id: Uuid,
    pub progress: Value,
    pub schema_version: u8,
}

impl AgentProgress {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        validate_attempt_identity(self.schema_version, self.attempt, &self.node_id)?;
        if !self.progress.is_object() || canonical_json(&self.progress)?.len() > 64 * 1024 {
            return Err(ProtocolError::Identity("progress document"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AgentDirective {
    pub attempt: u32,
    pub cancel_requested: bool,
    pub deadline: DateTime<FixedOffset>,
    pub fence: Uuid,
    pub job_id: Uuid,
    pub node_id: String,
    pub operation_id: Uuid,
    pub schema_version: u8,
}

impl AgentDirective {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        validate_attempt_identity(self.schema_version, self.attempt, &self.node_id)
    }

    pub fn from_progress(progress: AgentProgress) -> Self {
        Self {
            attempt: progress.attempt,
            cancel_requested: false,
            deadline: progress.deadline,
            fence: progress.fence,
            job_id: progress.job_id,
            node_id: progress.node_id,
            operation_id: progress.operation_id,
            schema_version: progress.schema_version,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct AgentResult {
    pub attempt: u32,
    pub deadline: DateTime<FixedOffset>,
    pub fence: Uuid,
    pub job_id: Uuid,
    pub node_id: String,
    pub operation_id: Uuid,
    pub result: Value,
    pub schema_version: u8,
    pub state: String,
}

impl AgentResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1
            || self.attempt == 0
            || !valid_node_id(&self.node_id)
            || !matches!(
                self.state.as_str(),
                "succeeded" | "failed" | "waiting-for-operator"
            )
        {
            return Err(ProtocolError::Identity("result identity"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EnrollmentRequest {
    pub csr: String,
    pub evidence: EnrollmentEvidence,
    pub grant_token: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EnrollmentEvidence {
    pub agent_digest: String,
    pub boot_id: String,
    pub csr_public_key_fingerprint: String,
    pub hardware_fingerprint: String,
    pub host_key_fingerprint: String,
    pub node_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PackageOperationRequest {
    pub deployment_digest: String,
    pub deployment_id: String,
    pub release_digest: String,
    pub schema_version: u8,
}

#[derive(Debug, Clone, PartialEq)]
pub enum RecipeOperationRequest {
    Build(RecipeBuildRequest),
    ImageImport(RecipeImageImportRequest),
    Install(RecipeInstallRequest),
    Start(RecipeStartRequest),
    Stop(RecipeStopRequest),
    Uninstall(RecipeUninstallRequest),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecipeBuildRequest {
    pub arguments: Vec<RecipeBuildArgument>,
    pub base_image_storage_bytes: u64,
    pub base_images: Vec<RecipeBuildBaseImage>,
    pub build_id: Uuid,
    pub build_input_sha256: String,
    pub dockerfile: String,
    pub kind: String,
    pub limits: RecipeBuildLimits,
    pub network: RecipeBuildNetwork,
    pub platform: String,
    pub recipe_content_sha256: String,
    pub recipe_revision_id: Uuid,
    pub schema_version: u8,
    pub source_bundle_bytes: u64,
    pub source_bundle_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeBuildBaseImage {
    pub manifest_digest: String,
    pub reference: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecipeBuildArgument {
    pub name: String,
    pub value: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeBuildNetwork {
    pub hosts: Vec<String>,
    pub mode: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeBuildLimits {
    pub container_socket: bool,
    pub cpu_cores: u16,
    pub gpu: u8,
    pub host_mounts: bool,
    pub memory_bytes: u64,
    pub output_bytes: u64,
    pub privileged: bool,
    pub processes: u32,
    pub temporary_bytes: u64,
    pub timeout_seconds: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeImageImportRequest {
    pub build_id: Uuid,
    pub image_bytes: u64,
    pub image_digest: String,
    pub kind: String,
    pub mapping_generation: u64,
    pub mapping_id: Uuid,
    // Protocol-v1 name retained for compatibility. Docker-backed nodes bind
    // the complete docker-save archive digest in this field.
    pub oci_layout_sha256: String,
    pub schema_version: u8,
    pub source_node_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecipeInstallRequest {
    pub expected_bytes: u64,
    pub image_digest: String,
    pub installation_id: Uuid,
    pub mapping_generation: u64,
    pub mapping_id: Uuid,
    pub plan_digest: String,
    pub rank: u32,
    pub recipe_build_id: Uuid,
    pub recipe_content_sha256: String,
    pub recipe_revision_id: Uuid,
    pub role: String,
    pub schema_version: u8,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecipeStartRequest {
    pub alias: String,
    pub endpoint_address: std::net::IpAddr,
    pub image_digest: String,
    pub installation_id: Uuid,
    pub local_address: Option<std::net::IpAddr>,
    pub master_address: Option<std::net::IpAddr>,
    pub master_port: Option<u16>,
    pub mapping_generation: u64,
    pub mapping_id: Uuid,
    pub plan_digest: String,
    pub port: u16,
    pub rank: u32,
    pub recipe_content_sha256: String,
    pub recipe_revision_id: Uuid,
    pub reserved_memory_bytes: u64,
    pub role: String,
    pub run_id: Uuid,
    pub schema_version: u8,
    pub world_size: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecipeStopRequest {
    pub plan_digest: String,
    pub run_id: Uuid,
    pub schema_version: u8,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecipeUninstallRequest {
    pub installation_id: Uuid,
    pub plan_digest: String,
    pub recipe_content_sha256: String,
    pub schema_version: u8,
}

impl RecipeOperationRequest {
    pub fn parse(claim: &AgentClaim) -> Result<Self, ProtocolError> {
        claim.validate()?;
        let request = match claim.operation.as_str() {
            "recipe.build.v1" => Self::Build(serde_json::from_value(claim.payload.clone())?),
            "recipe.image.import.v1" => {
                Self::ImageImport(serde_json::from_value(claim.payload.clone())?)
            }
            "recipe.install" => Self::Install(serde_json::from_value(claim.payload.clone())?),
            "recipe.start" => Self::Start(serde_json::from_value(claim.payload.clone())?),
            "recipe.stop" => Self::Stop(serde_json::from_value(claim.payload.clone())?),
            "recipe.uninstall" => Self::Uninstall(serde_json::from_value(claim.payload.clone())?),
            _ => return Err(ProtocolError::Identity("recipe operation")),
        };
        request.validate()?;
        Ok(request)
    }

    fn validate(&self) -> Result<(), ProtocolError> {
        let valid_common = |version: u8, plan: &str| version == 1 && lower_hex(plan, 64);
        let valid = match self {
            Self::Build(value) => validate_build(value),
            Self::ImageImport(value) => {
                value.schema_version == 1
                    && value.kind == "recipe.image.import.v1"
                    && value.mapping_generation >= 1
                    && valid_node_id(&value.source_node_id)
                    && valid_oci_digest(&value.image_digest)
                    && lower_hex(&value.oci_layout_sha256, 64)
                    && (1..=16 * 1024_u64.pow(4)).contains(&value.image_bytes)
            }
            Self::Install(value) => {
                valid_common(value.schema_version, &value.plan_digest)
                    && value.expected_bytes <= 16 * 1024_u64.pow(4)
                    && lower_hex(&value.recipe_content_sha256, 64)
                    && valid_oci_digest(&value.image_digest)
                    && value.mapping_generation >= 1
                    && valid_role(&value.role)
            }
            Self::Start(value) => {
                valid_common(value.schema_version, &value.plan_digest)
                    && lower_hex(&value.recipe_content_sha256, 64)
                    && valid_oci_digest(&value.image_digest)
                    && value.mapping_generation >= 1
                    && value.world_size >= 1
                    && value.rank < value.world_size
                    && valid_role(&value.role)
                    && value.port >= 1024
                    && value.reserved_memory_bytes > 0
                    && value.reserved_memory_bytes <= 16 * 1024_u64.pow(4)
                    && !value.endpoint_address.is_loopback()
                    && !value.endpoint_address.is_unspecified()
                    && !value.endpoint_address.is_multicast()
                    && !link_local(value.endpoint_address)
                    && if value.world_size == 1 {
                        value.rank == 0
                            && value.local_address.is_none()
                            && value.master_address.is_none()
                            && value.master_port.is_none()
                    } else {
                        value.local_address.is_some_and(valid_fabric_address)
                            && value.master_address.is_some_and(valid_fabric_address)
                            && value.master_port.is_some_and(|port| port >= 1024)
                    }
                    && valid_alias(&value.alias)
            }
            Self::Stop(value) => valid_common(value.schema_version, &value.plan_digest),
            Self::Uninstall(value) => {
                valid_common(value.schema_version, &value.plan_digest)
                    && lower_hex(&value.recipe_content_sha256, 64)
            }
        };
        if valid {
            Ok(())
        } else {
            Err(ProtocolError::Identity("recipe payload"))
        }
    }
}

fn validate_build(value: &RecipeBuildRequest) -> bool {
    value.schema_version == 1
        && value.kind == "recipe.build.v1"
        && lower_hex(&value.recipe_content_sha256, 64)
        && lower_hex(&value.source_bundle_sha256, 64)
        && lower_hex(&value.build_input_sha256, 64)
        && (1..=64 * 1024 * 1024).contains(&value.source_bundle_bytes)
        && value.platform == "linux/arm64"
        && valid_bundle_path(&value.dockerfile)
        && value.arguments.len() <= 64
        && value
            .arguments
            .iter()
            .all(|argument| valid_name(&argument.name) && valid_scalar(&argument.value))
        && value.base_images.len() <= 8
        && value.base_images.iter().enumerate().all(|(index, image)| {
            valid_pinned_image(&image.reference, &image.manifest_digest)
                && !value.base_images[..index]
                    .iter()
                    .any(|prior| prior.reference == image.reference)
        })
        && if value.base_images.is_empty() {
            value.base_image_storage_bytes == 0
        } else {
            (1..=16 * 1024_u64.pow(4)).contains(&value.base_image_storage_bytes)
        }
        && matches!(value.network.mode.as_str(), "none" | "public")
        && ((value.network.mode == "none" && value.network.hosts.is_empty())
            || (value.network.mode == "public" && !value.network.hosts.is_empty()))
        && value.network.hosts.len() <= 64
        && value
            .network
            .hosts
            .iter()
            .all(|host| valid_public_host(host))
        && value.limits.cpu_cores >= 1
        && value.limits.cpu_cores <= 256
        && value.limits.memory_bytes > 0
        && value.limits.memory_bytes <= 16 * 1024_u64.pow(4)
        && value.limits.temporary_bytes > 0
        && value.limits.temporary_bytes <= 16 * 1024_u64.pow(4)
        && value.limits.processes >= 1
        && value.limits.processes <= 65_536
        && value.limits.timeout_seconds >= 1
        && value.limits.timeout_seconds <= 86_400
        && value.limits.output_bytes >= 1
        && value.limits.output_bytes <= 16 * 1024_u64.pow(4)
        && value.limits.gpu == 0
        && !value.limits.privileged
        && !value.limits.host_mounts
        && !value.limits.container_socket
}

pub fn parse_strict<T: DeserializeOwned>(input: &[u8]) -> Result<T, ProtocolError> {
    Ok(serde_json::from_slice(input)?)
}

pub fn canonical_json<T: Serialize>(value: &T) -> Result<Vec<u8>, ProtocolError> {
    let value = serde_json::to_value(value)?;
    Ok(serde_json::to_vec(&sort_value(value))?)
}

pub fn hex_sha256(value: &[u8]) -> String {
    Sha256::digest(value)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn sort_value(value: Value) -> Value {
    match value {
        Value::Object(values) => Value::Object(
            values
                .into_iter()
                .map(|(key, value)| (key, sort_value(value)))
                .collect::<BTreeMap<_, _>>()
                .into_iter()
                .collect(),
        ),
        Value::Array(values) => Value::Array(values.into_iter().map(sort_value).collect()),
        other => other,
    }
}

fn valid_node_id(value: &str) -> bool {
    value.len() == 36
        && value.starts_with("spk_")
        && value[4..]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn validate_attempt_identity(
    schema_version: u8,
    attempt: u32,
    node_id: &str,
) -> Result<(), ProtocolError> {
    if schema_version != 1 || attempt == 0 || !valid_node_id(node_id) {
        return Err(ProtocolError::Identity("attempt identity"));
    }
    Ok(())
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_alias(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 63
        && value.bytes().enumerate().all(|(index, byte)| {
            let edge = index == 0 || index + 1 == value.len();
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || !edge && matches!(byte, b'.' | b'_' | b'-')
        })
}

fn valid_oci_digest(value: &str) -> bool {
    value
        .strip_prefix("sha256:")
        .is_some_and(|digest| lower_hex(digest, 64))
}

fn valid_pinned_image(reference: &str, manifest_digest: &str) -> bool {
    let Some((name, digest)) = reference.rsplit_once('@') else {
        return false;
    };
    !name.is_empty()
        && name.len() <= 512
        && name.bytes().all(|byte| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || matches!(byte, b'.' | b'_' | b':' | b'/' | b'-')
        })
        && digest == manifest_digest
        && valid_oci_digest(digest)
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

fn valid_scalar(value: &Value) -> bool {
    match value {
        Value::Bool(_) => true,
        Value::Number(number) => number.as_i64().is_some(),
        Value::String(value) => value.len() <= 1024 && !value.contains('\0'),
        _ => false,
    }
}

fn valid_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.bytes().enumerate().all(|(index, byte)| {
            if index == 0 {
                byte.is_ascii_lowercase()
            } else {
                byte.is_ascii_lowercase()
                    || byte.is_ascii_digit()
                    || matches!(byte, b'.' | b'_' | b'-')
            }
        })
}

fn valid_bundle_path(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 512
        && !value.starts_with('/')
        && !value.contains('\0')
        && value
            .split('/')
            .all(|part| !part.is_empty() && !matches!(part, "." | ".."))
}

fn valid_public_host(value: &str) -> bool {
    let lowered = value.to_ascii_lowercase();
    let reserved = matches!(
        lowered.as_str(),
        "localhost"
            | "localhost.localdomain"
            | "metadata"
            | "metadata.google.internal"
            | "instance-data.ec2.internal"
    ) || lowered.ends_with(".localhost")
        || lowered.ends_with(".localdomain")
        || lowered.ends_with(".internal");
    let numeric = value
        .bytes()
        .all(|byte| byte.is_ascii_digit() || byte == b'.');
    let numeric_public = if numeric {
        value.parse::<std::net::Ipv4Addr>().is_ok_and(public_ipv4)
    } else {
        true
    };
    !value.is_empty()
        && value.len() <= 253
        && !value.starts_with('.')
        && !value.ends_with('.')
        && !reserved
        && numeric_public
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-'))
}

fn public_ipv4(address: std::net::Ipv4Addr) -> bool {
    let octets = address.octets();
    let [first, second, ..] = octets;
    first != 0
        && first != 10
        && first != 127
        && !(first == 100 && (64..=127).contains(&second))
        && !(first == 169 && second == 254)
        && !(first == 172 && (16..=31).contains(&second))
        && !(first == 192 && second == 168)
        && !(first == 198 && (18..=19).contains(&second))
        && first < 224
}

fn link_local(value: std::net::IpAddr) -> bool {
    match value {
        std::net::IpAddr::V4(address) => address.is_link_local() || address.is_broadcast(),
        std::net::IpAddr::V6(address) => address.is_unicast_link_local(),
    }
}

fn valid_fabric_address(value: std::net::IpAddr) -> bool {
    !value.is_loopback() && !value.is_unspecified() && !value.is_multicast() && !link_local(value)
}
