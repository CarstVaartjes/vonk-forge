#![forbid(unsafe_code)]

use std::collections::{BTreeMap, BTreeSet};

use chrono::{DateTime, FixedOffset};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::Value;
use sha2::{Digest, Sha256};
use thiserror::Error;
use uuid::Uuid;

pub const MAX_HOST_RUNTIME_ARGUMENTS: usize = 512;
pub const RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY: &str = "vonk.recipe-run-observation-helper";
const RECIPE_RUN_OBSERVATION_RECEIPT_DOMAIN: &[u8] = b"VONK-RECIPE-RUN-OBSERVATION-RECEIPT-V1\0";

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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub observation: Option<RecipeRunInspectionBinding>,
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
        match (&self.action, &self.observation) {
            (HostRuntimeAction::RunInspect, Some(binding)) => {
                binding.validate()?;
                if self.job_id != binding.run_id
                    || u32::try_from(binding.run_generation).ok() != Some(self.attempt)
                    || hex_sha256(&canonical_json(&self.arguments)?)
                        != binding.runtime_arguments_sha256
                {
                    return Err(ProtocolError::Identity("host runtime observation binding"));
                }
            }
            (HostRuntimeAction::RunInspect, None) => {}
            (_, None) => {}
            (_, Some(_)) => {
                return Err(ProtocolError::Identity("host runtime observation action"));
            }
        }
        Ok(())
    }
}

/// Immutable Controller/run identity carried inside an exact periodic runtime
/// inspection request.  Because the host-helper grant signs the canonical
/// request digest, these fields are bound to the exact RunInspect arguments and
/// cannot be replayed for another generation or installed recipe.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeRunInspectionBinding {
    pub artifact_set_digest: String,
    pub image_digest: String,
    pub installation_id: Uuid,
    pub local_address: std::net::IpAddr,
    pub master_address: std::net::IpAddr,
    pub master_port: u16,
    pub mapping_generation: u64,
    pub mapping_id: Uuid,
    pub model_identity: String,
    pub port: u16,
    pub rank: u32,
    pub recipe_content_sha256: String,
    pub recipe_revision_id: Uuid,
    pub role: String,
    pub run_id: Uuid,
    pub run_generation: u64,
    pub runtime_arguments_sha256: String,
    pub world_size: u32,
}

impl RecipeRunInspectionBinding {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.mapping_generation == 0
            || self.run_generation == 0
            || self.world_size <= 1
            || self.rank >= self.world_size
            || self.master_port == 0
            || self.port == 0
            || !valid_fabric_address(self.local_address)
            || !valid_fabric_address(self.master_address)
            || !valid_role(&self.role)
            || !lower_hex(&self.recipe_content_sha256, 64)
            || !lower_hex(&self.artifact_set_digest, 64)
            || !lower_hex(&self.image_digest, 64)
            || !lower_hex(&self.runtime_arguments_sha256, 64)
            || self.model_identity.is_empty()
            || self.model_identity.len() > 1024
            || self.model_identity.contains(['\0', '\r', '\n'])
            || self.run_id.get_version() != Some(uuid::Version::Random)
            || self.installation_id.get_version() != Some(uuid::Version::Random)
            || self.mapping_id.get_version() != Some(uuid::Version::Random)
            || self.recipe_revision_id.get_version() != Some(uuid::Version::Random)
        {
            return Err(ProtocolError::Identity("recipe run inspection binding"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum RecipeRunObservationOutcome {
    Running,
    NotRunning,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeRunObservationReceiptClaims {
    pub schema_version: u8,
    pub authority: String,
    pub node_id: String,
    pub request_id: Uuid,
    pub request_sha256: String,
    pub observation_identity_sha256: String,
    pub outcome: RecipeRunObservationOutcome,
    pub observed_at: i64,
}

impl RecipeRunObservationReceiptClaims {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1
            || self.authority != RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY
            || !valid_node_id(&self.node_id)
            || self.request_id.get_version() != Some(uuid::Version::Random)
            || !lower_hex(&self.request_sha256, 64)
            || !lower_hex(&self.observation_identity_sha256, 64)
            || self.observed_at <= 0
        {
            return Err(ProtocolError::Identity(
                "recipe run observation receipt claims",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeRunObservationReceiptSignature {
    pub algorithm: String,
    pub key_id: String,
    pub value: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeRunObservationReceipt {
    pub schema_version: u8,
    pub claims: RecipeRunObservationReceiptClaims,
    pub signature: RecipeRunObservationReceiptSignature,
}

impl RecipeRunObservationReceipt {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        self.claims.validate()?;
        if self.schema_version != 1
            || self.signature.algorithm != "ed25519"
            || !lower_hex(&self.signature.key_id, 64)
            || !lower_hex(&self.signature.value, 128)
        {
            return Err(ProtocolError::Identity("recipe run observation receipt"));
        }
        Ok(())
    }
}

pub fn recipe_run_observation_receipt_signing_bytes(
    claims: &RecipeRunObservationReceiptClaims,
) -> Result<Vec<u8>, ProtocolError> {
    claims.validate()?;
    let mut value = RECIPE_RUN_OBSERVATION_RECEIPT_DOMAIN.to_vec();
    value.extend(canonical_json(claims)?);
    Ok(value)
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
    pub authority_revision: String,
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
        if !valid_node_id(&self.node_id) || !lower_hex(&self.authority_revision, 64) {
            return Err(ProtocolError::Identity("claim node or authority"));
        }
        if !matches!(
            self.operation.as_str(),
            "agent.upgrade.v1"
                | "recipe.build.v1"
                | "recipe.image.import.v1"
                | "recipe.job.run.v1"
                | "recipe.install"
                | "recipe.start"
                | "recipe.stop"
                | "recipe.uninstall"
                | "recipe.model-uninstall.v1"
        ) {
            return Err(ProtocolError::Identity("claim operation"));
        }
        let payload = canonical_json(&self.payload)?;
        if !self.payload.is_object()
            || payload.len() > 64 * 1024
            || hex_sha256(&payload) != self.payload_digest
        {
            return Err(ProtocolError::Identity("claim payload digest"));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AgentUpgradeRequest {
    pub architecture: String,
    pub package_bytes: u64,
    pub package_sha256: String,
    pub package_signature: String,
    pub package_url: String,
    pub package_version: String,
    pub schema_version: u8,
    pub target_binary_digest: String,
    pub target_build_digest: String,
}

impl AgentUpgradeRequest {
    pub fn parse(claim: &AgentClaim) -> Result<Self, ProtocolError> {
        if claim.operation != "agent.upgrade.v1" {
            return Err(ProtocolError::Identity("agent upgrade operation"));
        }
        let value: Self = serde_json::from_value(claim.payload.clone())?;
        let url = url::Url::parse(&value.package_url)
            .map_err(|_| ProtocolError::Identity("agent upgrade URL"))?;
        if value.schema_version != 1
            || value.architecture != "linux-arm64"
            || !(1..=1024 * 1024 * 1024).contains(&value.package_bytes)
            || !lower_hex(&value.package_sha256, 64)
            || !lower_hex(&value.package_signature, 128)
            || !lower_hex(&value.target_binary_digest, 64)
            || !value.target_build_digest.starts_with("sha256:")
            || !lower_hex(&value.target_build_digest[7..], 64)
            || value.package_version.is_empty()
            || value.package_version.len() > 128
            || !value
                .package_version
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphanumeric)
            || value
                .package_version
                .bytes()
                .any(|byte| !byte.is_ascii_alphanumeric() && !b".+~-".contains(&byte))
            || !value
                .package_url
                .starts_with("https://install.vonkforge.ai/")
            || url.scheme() != "https"
            || url.host_str() != Some("install.vonkforge.ai")
            || url.port().is_some()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || !url.path().ends_with("/vonk-forge-agent.deb")
        {
            return Err(ProtocolError::Identity("agent upgrade payload"));
        }
        Ok(value)
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
                "succeeded" | "failed" | "cancelled" | "waiting-for-operator"
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

#[derive(Debug, Clone, PartialEq)]
pub enum RecipeOperationRequest {
    Build(RecipeBuildRequest),
    ImageImport(RecipeImageImportRequest),
    JobRun(RecipeJobRunRequest),
    Install(RecipeInstallRequest),
    Start(RecipeStartRequest),
    Stop(RecipeStopRequest),
    Uninstall(RecipeUninstallRequest),
    ModelUninstall(RecipeModelUninstallRequest),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeJobFile {
    pub name: String,
    pub media_type: String,
    pub size_bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeJobInputFile {
    pub slot: String,
    pub name: String,
    pub media_type: String,
    pub size_bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeJobOutputLimits {
    pub max_files: u16,
    pub max_file_bytes: u64,
    pub max_total_bytes: u64,
    pub allowed_media_types: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeJobOutputMapping {
    pub slot: String,
    pub media_type: String,
    pub extensions: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecipeJobRunRequest {
    pub schema_version: u8,
    pub job_id: Uuid,
    pub run_id: Uuid,
    pub installation_id: Uuid,
    pub recipe_revision_id: Uuid,
    pub recipe_content_sha256: String,
    pub image_digest: String,
    pub plan_digest: String,
    pub interface: String,
    pub rank: u32,
    pub role: String,
    pub contract_sha256: String,
    pub input_manifest_sha256: String,
    pub input_total_bytes: u64,
    pub inputs: Vec<RecipeJobInputFile>,
    pub parameters: Value,
    pub output_mappings: Vec<RecipeJobOutputMapping>,
    pub output_limits: RecipeJobOutputLimits,
    pub timeout_seconds: u16,
    pub reserved_memory_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeJobOutputManifest {
    pub schema_version: u8,
    pub manifest_sha256: String,
    pub total_bytes: u64,
    pub files: Vec<RecipeJobFile>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeJobEvidence {
    pub elapsed_milliseconds: u64,
    pub peak_memory_bytes: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeJobRunResult {
    pub schema_version: u8,
    pub job_id: Uuid,
    pub run_id: Uuid,
    pub exit_code: i32,
    pub output_manifest: RecipeJobOutputManifest,
    pub evidence: RecipeJobEvidence,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

impl RecipeJobRunResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        let manifest = serde_json::json!({
            "schema_version": self.output_manifest.schema_version,
            "total_bytes": self.output_manifest.total_bytes,
            "files": self.output_manifest.files,
        });
        let valid = self.schema_version == 1
            && (0..=255).contains(&self.exit_code)
            && self.output_manifest.schema_version == 1
            && self.output_manifest.files.len() <= 32
            && self
                .output_manifest
                .files
                .windows(2)
                .all(|pair| pair[0].name < pair[1].name)
            && self.output_manifest.files.iter().all(|file| {
                valid_job_file_name(&file.name)
                    && valid_media_type(&file.media_type)
                    && file.size_bytes <= 1024 * 1024 * 1024
                    && lower_hex(&file.sha256, 64)
            })
            && self
                .output_manifest
                .files
                .iter()
                .try_fold(0_u64, |total, file| total.checked_add(file.size_bytes))
                == Some(self.output_manifest.total_bytes)
            && self.output_manifest.total_bytes <= 2 * 1024 * 1024 * 1024
            && canonical_json(&manifest)
                .ok()
                .is_some_and(|bytes| hex_sha256(&bytes) == self.output_manifest.manifest_sha256)
            && self.evidence.elapsed_milliseconds <= 7 * 24 * 60 * 60 * 1000
            && self
                .evidence
                .peak_memory_bytes
                .is_none_or(|value| value <= 16 * 1024_u64.pow(4))
            && self.reason.as_ref().is_none_or(|reason| {
                !reason.is_empty() && reason.len() <= 512 && !reason.contains('\0')
            });
        if valid {
            Ok(())
        } else {
            Err(ProtocolError::Identity("recipe job result"))
        }
    }
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

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub enum RecipeStartPhase {
    #[serde(rename = "rank-launch")]
    RankLaunch,
    #[serde(rename = "collective-readiness")]
    CollectiveReadiness,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub phase: Option<RecipeStartPhase>,
    pub plan_digest: String,
    pub port: u16,
    pub rank: u32,
    pub recipe_content_sha256: String,
    pub recipe_revision_id: Uuid,
    pub reserved_memory_bytes: u64,
    pub role: String,
    pub run_id: Uuid,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub run_generation: Option<u64>,
    pub schema_version: u8,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub start_deadline: Option<DateTime<FixedOffset>>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cleanup_model_version_sha256: Option<String>,
    pub installation_id: Uuid,
    pub plan_digest: String,
    pub recipe_content_sha256: String,
    pub schema_version: u8,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeModelUninstallInstallation {
    pub installation_id: Uuid,
    pub recipe_content_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RecipeModelUninstallRequest {
    pub installations: Vec<RecipeModelUninstallInstallation>,
    pub model_version_sha256: String,
    pub plan_digest: String,
    pub schema_version: u8,
}

impl RecipeOperationRequest {
    pub fn parse(claim: &AgentClaim) -> Result<Self, ProtocolError> {
        claim.validate()?;
        let request = match claim.operation.as_str() {
            "recipe.build.v1" => {
                validate_build_wire(&claim.payload)?;
                Self::Build(serde_json::from_value(claim.payload.clone())?)
            }
            "recipe.image.import.v1" => {
                Self::ImageImport(serde_json::from_value(claim.payload.clone())?)
            }
            "recipe.job.run.v1" => {
                validate_job_wire(&claim.payload)?;
                Self::JobRun(serde_json::from_value(claim.payload.clone())?)
            }
            "recipe.install" => Self::Install(serde_json::from_value(claim.payload.clone())?),
            "recipe.start" => Self::Start(serde_json::from_value(claim.payload.clone())?),
            "recipe.stop" => Self::Stop(serde_json::from_value(claim.payload.clone())?),
            "recipe.uninstall" => Self::Uninstall(serde_json::from_value(claim.payload.clone())?),
            "recipe.model-uninstall.v1" => {
                Self::ModelUninstall(serde_json::from_value(claim.payload.clone())?)
            }
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
            Self::JobRun(value) => validate_recipe_job(value),
            Self::Install(value) => {
                valid_common(value.schema_version, &value.plan_digest)
                    && value.expected_bytes <= 16 * 1024_u64.pow(4)
                    && lower_hex(&value.recipe_content_sha256, 64)
                    && valid_oci_digest(&value.image_digest)
                    && value.mapping_generation >= 1
                    && valid_role(&value.role)
            }
            Self::Start(value) => {
                let valid_phase = match (&value.phase, &value.start_deadline, value.run_generation)
                {
                    (None, None, None) => true,
                    (Some(RecipeStartPhase::RankLaunch), Some(deadline), Some(generation)) => {
                        generation > 0
                            && value.world_size > 1
                            && deadline.offset().local_minus_utc() == 0
                    }
                    (
                        Some(RecipeStartPhase::CollectiveReadiness),
                        Some(deadline),
                        Some(generation),
                    ) => {
                        generation > 0
                            && value.world_size > 1
                            && value.local_address.is_some()
                            && value.local_address == value.master_address
                            && deadline.offset().local_minus_utc() == 0
                    }
                    _ => false,
                };
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
                    && valid_phase
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
                    && value
                        .cleanup_model_version_sha256
                        .as_ref()
                        .is_none_or(|digest| lower_hex(digest, 64))
            }
            Self::ModelUninstall(value) => {
                valid_common(value.schema_version, &value.plan_digest)
                    && lower_hex(&value.model_version_sha256, 64)
                    && !value.installations.is_empty()
                    && value.installations.len() <= 512
                    && value
                        .installations
                        .iter()
                        .map(|installation| installation.installation_id)
                        .collect::<BTreeSet<_>>()
                        .len()
                        == value.installations.len()
                    && value
                        .installations
                        .iter()
                        .all(|installation| lower_hex(&installation.recipe_content_sha256, 64))
            }
        };
        if valid {
            Ok(())
        } else {
            Err(ProtocolError::Identity("recipe payload"))
        }
    }
}

#[cfg(test)]
mod recipe_start_tests {
    use super::*;

    fn start_payload(
        world_size: u32,
        rank: u32,
        local_address: Option<&str>,
        master_address: Option<&str>,
        phase: Option<&str>,
    ) -> Value {
        let mut payload = serde_json::json!({
            "alias": "distributed-model",
            "endpoint_address": "100.100.20.30",
            "image_digest": format!("sha256:{}", "a".repeat(64)),
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "local_address": local_address,
            "master_address": master_address,
            "master_port": if world_size > 1 { Some(29500) } else { None },
            "mapping_generation": 1,
            "mapping_id": "00000000-0000-4000-8000-000000000002",
            "plan_digest": "b".repeat(64),
            "port": 8000,
            "rank": rank,
            "recipe_content_sha256": "c".repeat(64),
            "recipe_revision_id": "00000000-0000-4000-8000-000000000003",
            "reserved_memory_bytes": 1024,
            "role": if rank == 0 { "entrypoint" } else { "worker" },
            "run_id": "00000000-0000-4000-8000-000000000004",
            "schema_version": 1,
            "world_size": world_size,
        });
        if let Some(phase) = phase {
            let document = payload.as_object_mut().unwrap();
            document.insert("phase".to_owned(), Value::String(phase.to_owned()));
            document.insert("run_generation".to_owned(), Value::from(1));
            document.insert(
                "start_deadline".to_owned(),
                Value::String("2026-09-01T12:00:00+00:00".to_owned()),
            );
        }
        payload
    }

    fn claim(payload: Value) -> AgentClaim {
        AgentClaim {
            attempt: 1,
            authority_revision: "d".repeat(64),
            deadline: "2026-09-01T12:00:00+00:00".parse().unwrap(),
            fence: Uuid::parse_str("00000000-0000-4000-8000-000000000005").unwrap(),
            job_id: Uuid::parse_str("00000000-0000-4000-8000-000000000006").unwrap(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            operation: "recipe.start".to_owned(),
            operation_id: Uuid::parse_str("00000000-0000-4000-8000-000000000007").unwrap(),
            payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
            payload,
            schema_version: 1,
        }
    }

    fn parsed_start(payload: Value) -> Result<RecipeStartRequest, ProtocolError> {
        match RecipeOperationRequest::parse(&claim(payload))? {
            RecipeOperationRequest::Start(request) => Ok(request),
            _ => unreachable!(),
        }
    }

    #[test]
    fn legacy_start_payloads_without_a_phase_remain_accepted_and_omit_the_field() {
        let single = parsed_start(start_payload(1, 0, None, None, None)).unwrap();
        assert_eq!(single.phase, None);
        assert_eq!(single.start_deadline, None);
        assert_eq!(single.run_generation, None);
        let legacy_wire = serde_json::to_value(single).unwrap();
        assert!(legacy_wire.get("phase").is_none());
        assert!(legacy_wire.get("start_deadline").is_none());

        let distributed = parsed_start(start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            None,
        ))
        .unwrap();
        assert_eq!(distributed.phase, None);
        assert_eq!(distributed.start_deadline, None);
    }

    #[test]
    fn distributed_start_accepts_rank_launch_and_exact_owner_collective_readiness() {
        let launch = parsed_start(start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            Some("rank-launch"),
        ))
        .unwrap();
        assert_eq!(launch.phase, Some(RecipeStartPhase::RankLaunch));

        // Endpoint ownership is identified by the rank's exact local fabric
        // address matching the signed rendezvous address; it need not be rank zero.
        let collective = parsed_start(start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.3"),
            Some("collective-readiness"),
        ))
        .unwrap();
        assert_eq!(
            collective.phase,
            Some(RecipeStartPhase::CollectiveReadiness)
        );
    }

    #[test]
    fn phased_start_rejects_unknown_single_node_and_non_owner_phases() {
        for payload in [
            start_payload(1, 0, None, None, Some("rank-launch")),
            start_payload(1, 0, None, None, Some("collective-readiness")),
            start_payload(
                2,
                1,
                Some("192.168.100.3"),
                Some("192.168.100.2"),
                Some("collective-readiness"),
            ),
            start_payload(
                2,
                1,
                Some("192.168.100.3"),
                Some("192.168.100.2"),
                Some("launch"),
            ),
        ] {
            assert!(parsed_start(payload).is_err());
        }

        let mut missing_deadline = start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            Some("rank-launch"),
        );
        missing_deadline
            .as_object_mut()
            .unwrap()
            .remove("start_deadline");
        assert!(parsed_start(missing_deadline).is_err());

        let mut missing_generation = start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            Some("rank-launch"),
        );
        missing_generation
            .as_object_mut()
            .unwrap()
            .remove("run_generation");
        assert!(parsed_start(missing_generation).is_err());

        let mut legacy_with_deadline =
            start_payload(2, 1, Some("192.168.100.3"), Some("192.168.100.2"), None);
        legacy_with_deadline.as_object_mut().unwrap().insert(
            "start_deadline".to_owned(),
            Value::String("2026-09-01T12:00:00+00:00".to_owned()),
        );
        assert!(parsed_start(legacy_with_deadline).is_err());

        let mut non_utc_deadline = start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            Some("rank-launch"),
        );
        non_utc_deadline.as_object_mut().unwrap().insert(
            "start_deadline".to_owned(),
            Value::String("2026-09-01T14:00:00+02:00".to_owned()),
        );
        assert!(parsed_start(non_utc_deadline).is_err());
    }
}

fn validate_job_wire(value: &Value) -> Result<(), ProtocolError> {
    for field in ["job_id", "run_id", "installation_id", "recipe_revision_id"] {
        let canonical = value
            .get(field)
            .and_then(Value::as_str)
            .and_then(|raw| Uuid::parse_str(raw).ok().map(|parsed| (raw, parsed)))
            .is_some_and(|(raw, parsed)| parsed.to_string() == raw);
        if !canonical {
            return Err(ProtocolError::Identity("recipe job payload"));
        }
    }
    Ok(())
}

fn validate_recipe_job(value: &RecipeJobRunRequest) -> bool {
    let inputs_valid = value.inputs.len() <= 32
        && value
            .inputs
            .windows(2)
            .all(|pair| pair[0].name < pair[1].name)
        && value.inputs.iter().all(|file| {
            valid_job_slot(&file.slot)
                && valid_job_file_name(&file.name)
                && valid_media_type(&file.media_type)
                && file.size_bytes <= 512 * 1024 * 1024
                && lower_hex(&file.sha256, 64)
        })
        && value
            .inputs
            .iter()
            .try_fold(0_u64, |total, file| total.checked_add(file.size_bytes))
            == Some(value.input_total_bytes)
        && value.input_total_bytes <= 1024 * 1024 * 1024;
    let manifest = serde_json::json!({
        "schema_version": 1,
        "total_bytes": value.input_total_bytes,
        "files": value.inputs,
    });
    let manifest_valid = canonical_json(&manifest)
        .ok()
        .is_some_and(|bytes| hex_sha256(&bytes) == value.input_manifest_sha256);
    let limits = &value.output_limits;
    let mappings_valid = (1..=32).contains(&value.output_mappings.len())
        && value
            .output_mappings
            .windows(2)
            .all(|pair| pair[0].slot < pair[1].slot)
        && value.output_mappings.iter().all(|mapping| {
            valid_job_slot(&mapping.slot)
                && valid_media_type(&mapping.media_type)
                && (1..=16).contains(&mapping.extensions.len())
                && mapping.extensions.windows(2).all(|pair| pair[0] < pair[1])
                && mapping
                    .extensions
                    .iter()
                    .all(|extension| valid_job_extension(extension))
        })
        && value
            .output_mappings
            .iter()
            .flat_map(|mapping| mapping.extensions.iter())
            .collect::<BTreeSet<_>>()
            .len()
            == value
                .output_mappings
                .iter()
                .map(|mapping| mapping.extensions.len())
                .sum::<usize>();
    value.schema_version == 1
        && lower_hex(&value.recipe_content_sha256, 64)
        && valid_oci_digest(&value.image_digest)
        && lower_hex(&value.plan_digest, 64)
        && lower_hex(&value.contract_sha256, 64)
        && matches!(
            value.interface.as_str(),
            "audio-job" | "video-job" | "image-job" | "mesh-job" | "artifact-job"
        )
        && value.rank == 0
        && value.role == "entrypoint"
        && inputs_valid
        && manifest_valid
        && value.parameters.is_object()
        && canonical_json(&value.parameters).is_ok_and(|bytes| bytes.len() <= 16 * 1024)
        && valid_job_parameter(&value.parameters, 0)
        && mappings_valid
        && (1..=32).contains(&limits.max_files)
        && (1..=1024 * 1024 * 1024).contains(&limits.max_file_bytes)
        && (1..=2 * 1024 * 1024 * 1024).contains(&limits.max_total_bytes)
        && limits.max_file_bytes <= limits.max_total_bytes
        && !limits.allowed_media_types.is_empty()
        && limits.allowed_media_types.len() <= 16
        && limits
            .allowed_media_types
            .iter()
            .enumerate()
            .all(|(index, media_type)| {
                valid_media_type(media_type)
                    && !limits.allowed_media_types[..index].contains(media_type)
                    && (index == 0 || limits.allowed_media_types[index - 1] < *media_type)
            })
        && limits.allowed_media_types.iter().all(|allowed| {
            value
                .output_mappings
                .iter()
                .any(|mapping| &mapping.media_type == allowed)
        })
        && (1..=3600).contains(&value.timeout_seconds)
        && (1..=16 * 1024_u64.pow(4)).contains(&value.reserved_memory_bytes)
}

fn valid_job_slot(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 32
        && value.as_bytes()[0].is_ascii_alphabetic()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn valid_job_file_name(value: &str) -> bool {
    !value.is_empty()
        && value != "manifest.json"
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn valid_job_extension(value: &str) -> bool {
    let Some(value) = value.strip_prefix('.') else {
        return false;
    };
    !value.is_empty()
        && value.len() <= 16
        && (value.as_bytes()[0].is_ascii_lowercase() || value.as_bytes()[0].is_ascii_digit())
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
}

fn valid_media_type(value: &str) -> bool {
    value.split_once('/').is_some_and(|(kind, subtype)| {
        let valid_part = |part: &str| {
            !part.is_empty()
                && part.len() <= 64
                && (part.as_bytes()[0].is_ascii_lowercase() || part.as_bytes()[0].is_ascii_digit())
                && part.bytes().all(|byte| {
                    byte.is_ascii_lowercase()
                        || byte.is_ascii_digit()
                        || matches!(
                            byte,
                            b'!' | b'#' | b'$' | b'&' | b'^' | b'_' | b'.' | b'+' | b'-'
                        )
                })
        };
        valid_part(kind) && valid_part(subtype)
    })
}

fn valid_job_parameter(value: &Value, depth: usize) -> bool {
    if depth > 8 {
        return false;
    }
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) => true,
        Value::String(value) => value.len() <= 4096 && !value.contains('\0'),
        Value::Array(values) => {
            values.len() <= 128
                && values
                    .iter()
                    .all(|value| valid_job_parameter(value, depth + 1))
        }
        Value::Object(values) => values.iter().all(|(key, value)| {
            let key = key.to_ascii_lowercase();
            values.len() <= 128
                && !key.is_empty()
                && key.len() <= 64
                && !unsafe_parameter_key(&key)
                && valid_job_parameter(value, depth + 1)
        }),
    }
}

fn unsafe_parameter_key(key: &str) -> bool {
    [
        "password",
        "secret",
        "token",
        "authorization",
        "command",
        "shell",
        "environment",
    ]
    .iter()
    .any(|value| key.contains(value))
        || key.match_indices("private").any(|(index, _)| {
            let tail = &key[index + "private".len()..];
            tail.starts_with("key") || tail.get(1..).is_some_and(|value| value.starts_with("key"))
        })
        || key.split(['_', '-']).any(|part| {
            matches!(
                part,
                "path" | "file" | "filename" | "filepath" | "directory" | "folder"
            )
        })
}

fn validate_build_wire(value: &Value) -> Result<(), ProtocolError> {
    let canonical_uuid = |field: &str| {
        value
            .get(field)
            .and_then(Value::as_str)
            .and_then(|raw| Uuid::parse_str(raw).ok().map(|parsed| (raw, parsed)))
            .is_some_and(|(raw, parsed)| parsed.to_string() == raw)
    };
    if !canonical_uuid("build_id") || !canonical_uuid("recipe_revision_id") {
        return Err(ProtocolError::Identity("recipe payload"));
    }
    Ok(())
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
        && name
            .as_bytes()
            .first()
            .is_some_and(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
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
        && !value.contains('\\')
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

#[cfg(test)]
mod recipe_job_tests {
    use super::*;

    fn request() -> RecipeJobRunRequest {
        let inputs = vec![RecipeJobInputFile {
            slot: "input".to_owned(),
            name: "input.mp4".to_owned(),
            media_type: "video/mp4".to_owned(),
            size_bytes: 123,
            sha256: "a".repeat(64),
        }];
        let manifest = serde_json::json!({
            "schema_version": 1,
            "total_bytes": 123,
            "files": inputs,
        });
        RecipeJobRunRequest {
            schema_version: 1,
            job_id: Uuid::new_v4(),
            run_id: Uuid::new_v4(),
            installation_id: Uuid::new_v4(),
            recipe_revision_id: Uuid::new_v4(),
            recipe_content_sha256: "b".repeat(64),
            image_digest: format!("sha256:{}", "c".repeat(64)),
            plan_digest: "d".repeat(64),
            interface: "video-job".to_owned(),
            rank: 0,
            role: "entrypoint".to_owned(),
            contract_sha256: "e".repeat(64),
            input_manifest_sha256: hex_sha256(&canonical_json(&manifest).unwrap()),
            input_total_bytes: 123,
            inputs,
            parameters: serde_json::json!({"guidance_scale": 7}),
            output_mappings: vec![RecipeJobOutputMapping {
                slot: "video".to_owned(),
                media_type: "video/mp4".to_owned(),
                extensions: vec![".mp4".to_owned()],
            }],
            output_limits: RecipeJobOutputLimits {
                max_files: 32,
                max_file_bytes: 1024 * 1024,
                max_total_bytes: 2 * 1024 * 1024,
                allowed_media_types: vec!["video/mp4".to_owned()],
            },
            timeout_seconds: 3600,
            reserved_memory_bytes: 64 * 1024 * 1024 * 1024,
        }
    }

    #[test]
    fn job_contract_binds_canonical_inputs_and_rejects_unsafe_parameters() {
        let valid = request();
        assert!(validate_recipe_job(&valid));

        let mut cross_manifest = valid.clone();
        cross_manifest.inputs[0].name = "other.mp4".to_owned();
        assert!(!validate_recipe_job(&cross_manifest));

        let mut traversal = valid.clone();
        traversal.inputs[0].name = "../input.mp4".to_owned();
        assert!(!validate_recipe_job(&traversal));

        let mut invalid_slot = valid.clone();
        invalid_slot.inputs[0].slot = "0input".to_owned();
        assert!(!validate_recipe_job(&invalid_slot));

        let mut reserved_manifest = valid.clone();
        reserved_manifest.inputs[0].name = "manifest.json".to_owned();
        let manifest = serde_json::json!({
            "schema_version": 1,
            "total_bytes": reserved_manifest.input_total_bytes,
            "files": reserved_manifest.inputs,
        });
        reserved_manifest.input_manifest_sha256 = hex_sha256(&canonical_json(&manifest).unwrap());
        assert!(!validate_recipe_job(&reserved_manifest));

        let mut command = valid;
        command.parameters = serde_json::json!({"shell_command": "curl example.invalid"});
        assert!(!validate_recipe_job(&command));
    }

    #[test]
    fn output_mapping_contract_is_sorted_exact_and_collision_free() {
        let mut valid = request();
        valid.output_mappings = vec![
            RecipeJobOutputMapping {
                slot: "custom".to_owned(),
                media_type: "application/vnd.vonk.custom".to_owned(),
                extensions: vec![".vonk.bin".to_owned()],
            },
            RecipeJobOutputMapping {
                slot: "document".to_owned(),
                media_type: "application/pdf".to_owned(),
                extensions: vec![".pdf".to_owned()],
            },
            RecipeJobOutputMapping {
                slot: "fallback".to_owned(),
                media_type: "application/octet-stream".to_owned(),
                extensions: vec![".bin".to_owned()],
            },
            RecipeJobOutputMapping {
                slot: "image".to_owned(),
                media_type: "image/avif".to_owned(),
                extensions: vec![".avif".to_owned()],
            },
        ];
        valid.output_limits.allowed_media_types = vec![
            "application/octet-stream".to_owned(),
            "application/pdf".to_owned(),
            "application/vnd.vonk.custom".to_owned(),
            "image/avif".to_owned(),
        ];
        assert!(validate_recipe_job(&valid));

        let mut collision = valid.clone();
        collision.output_mappings[3].extensions = vec![".pdf".to_owned()];
        assert!(!validate_recipe_job(&collision));

        let mut undeclared_limit = valid.clone();
        undeclared_limit.output_limits.allowed_media_types = vec!["video/mp4".to_owned()];
        assert!(!validate_recipe_job(&undeclared_limit));

        let mut uppercase = valid;
        uppercase.output_mappings[1].extensions = vec![".PDF".to_owned()];
        assert!(!validate_recipe_job(&uppercase));
    }

    #[test]
    fn python_job_claim_and_result_vectors_have_rust_parity() {
        let claim: AgentClaim = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
        ))
        .unwrap();
        claim.validate().unwrap();
        let parsed = RecipeOperationRequest::parse(&claim).unwrap();
        let RecipeOperationRequest::JobRun(request) = parsed else {
            panic!("job vector parsed as the wrong operation");
        };
        assert_eq!(
            request.input_manifest_sha256,
            "a3fa3ff4a07e23b945e72cda963e6aaf24671bc52642d328180b0ea4cde1776d"
        );

        let result: AgentResult = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
        ))
        .unwrap();
        result.validate().unwrap();
        let typed: RecipeJobRunResult = serde_json::from_value(result.result.clone()).unwrap();
        typed.validate().unwrap();
        assert_eq!(
            result.result["output_manifest"]["manifest_sha256"],
            "9f7781fb8415bc1cb9e835fe4bcc9c8dd8f45f6a6b333f0e0550e812db1da9cd"
        );

        let mut unavailable_peak = typed;
        unavailable_peak.evidence.peak_memory_bytes = None;
        unavailable_peak.validate().unwrap();
        assert!(
            serde_json::to_value(unavailable_peak).unwrap()["evidence"]["peak_memory_bytes"]
                .is_null()
        );
    }

    #[test]
    fn cancelled_is_a_typed_terminal_agent_result_state() {
        let result = AgentResult {
            attempt: 1,
            deadline: DateTime::parse_from_rfc3339("2026-08-28T12:00:00+00:00").unwrap(),
            fence: Uuid::new_v4(),
            job_id: Uuid::new_v4(),
            node_id: "spk_11111111111111111111111111111111".to_owned(),
            operation_id: Uuid::new_v4(),
            result: serde_json::json!({"reason": "controller cancellation requested"}),
            schema_version: 1,
            state: "cancelled".to_owned(),
        };

        result.validate().unwrap();
    }
}

#[cfg(test)]
mod recipe_run_inspection_tests {
    use super::*;

    fn binding() -> RecipeRunInspectionBinding {
        RecipeRunInspectionBinding {
            artifact_set_digest: "a".repeat(64),
            image_digest: "b".repeat(64),
            installation_id: Uuid::new_v4(),
            local_address: "192.168.100.11".parse().unwrap(),
            master_address: "192.168.100.10".parse().unwrap(),
            master_port: 29500,
            mapping_generation: 3,
            mapping_id: Uuid::new_v4(),
            model_identity: "example/model@0123456789abcdef".to_owned(),
            port: 8000,
            rank: 1,
            recipe_content_sha256: "c".repeat(64),
            recipe_revision_id: Uuid::new_v4(),
            role: "worker".to_owned(),
            run_id: Uuid::new_v4(),
            run_generation: 2,
            runtime_arguments_sha256: hex_sha256(
                &canonical_json(&vec![
                    format!("sha256:{}", "b".repeat(64)),
                    "run".to_owned(),
                ])
                .unwrap(),
            ),
            world_size: 2,
        }
    }

    #[test]
    fn exact_inspection_binds_generation_and_is_run_inspect_only() {
        let binding = binding();
        let mut request = HostRuntimeRequest {
            schema_version: 1,
            action: HostRuntimeAction::RunInspect,
            job_id: binding.run_id,
            operation_id: Uuid::new_v4(),
            attempt: binding.run_generation as u32,
            fence: Uuid::new_v4(),
            arguments: vec![format!("sha256:{}", binding.image_digest), "run".to_owned()],
            observation: Some(binding.clone()),
        };
        request.validate().unwrap();

        request.action = HostRuntimeAction::Start;
        assert!(request.validate().is_err());
        request.action = HostRuntimeAction::RunInspect;
        request.observation.as_mut().unwrap().run_generation = 0;
        assert!(request.validate().is_err());
    }

    #[test]
    fn observation_receipt_has_strict_domain_separated_claims() {
        let claims = RecipeRunObservationReceiptClaims {
            schema_version: 1,
            authority: RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY.to_owned(),
            node_id: "spk_11111111111111111111111111111111".to_owned(),
            request_id: Uuid::new_v4(),
            request_sha256: "a".repeat(64),
            observation_identity_sha256: "b".repeat(64),
            outcome: RecipeRunObservationOutcome::NotRunning,
            observed_at: 1_788_000_000,
        };
        let signing = recipe_run_observation_receipt_signing_bytes(&claims).unwrap();
        assert!(signing.starts_with(b"VONK-RECIPE-RUN-OBSERVATION-RECEIPT-V1\0"));
        assert!(signing.ends_with(&canonical_json(&claims).unwrap()));

        let receipt = RecipeRunObservationReceipt {
            schema_version: 1,
            claims: claims.clone(),
            signature: RecipeRunObservationReceiptSignature {
                algorithm: "ed25519".to_owned(),
                key_id: "c".repeat(64),
                value: "d".repeat(128),
            },
        };
        receipt.validate().unwrap();
        let mut replay_shaped = receipt;
        replay_shaped.claims.request_sha256 = "e".repeat(64);
        assert_ne!(
            recipe_run_observation_receipt_signing_bytes(&claims).unwrap(),
            recipe_run_observation_receipt_signing_bytes(&replay_shaped.claims).unwrap()
        );
        replay_shaped.claims.node_id = "wrong".to_owned();
        assert!(replay_shaped.validate().is_err());
    }
}
