#![forbid(unsafe_code)]
#[rustfmt::skip]
pub mod compiled_execution_plan;
pub mod generated;
pub mod runtime_preflight;
mod wire_schema;
pub use generated::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult,
    AgentUpgradePayload as AgentUpgradeRequest,
    ArtifactDistributionPayload as ArtifactDistributionRequest, DistributionAssignment,
    DistributionObject, EnrollmentEvidence, EnrollmentSubmitRequest as EnrollmentRequest,
    InventoryRequest, RecipeBuildAdditionalContext, RecipeBuildArgument, RecipeBuildBaseImage,
    RecipeBuildEvidence, RecipeBuildLimits, RecipeBuildMetadata, RecipeBuildNetwork,
    RecipeBuildOptions, RecipeBuildPolicy, RecipeBuildPolicyFinding, RecipeBuildRequest,
    RecipeImageImportEvidence, RecipeImageImportRequest,
    RecipeInstallPayload as RecipeInstallRequest, RecipeJobEvidence, RecipeJobFile,
    RecipeJobInputFile, RecipeJobOutputLimits, RecipeJobOutputManifest, RecipeJobOutputMapping,
    RecipeJobRunRequest, RecipeJobRunResult, RecipeModelCleanupInstallation,
    RecipeModelCleanupPayload as RecipeModelCleanupRequest, RecipeModelCleanupResult,
    RecipeStartPayload as RecipeStartRequest, RecipeStartPayloadPhase as RecipeStartPhase,
    RecipeStopPayload as RecipeStopRequest, RecipeStopResult,
    RecipeUninstallPayload as RecipeUninstallRequest, RecipeUninstallResult,
};
pub use generated::{
    ExecuteContainerRuntimeRequestOperationAction as HostHelperContainerRuntimeAction,
    HostHelperGrantClaims, HostHelperSignature as HostHelperGrantSignature,
    HostHelperSignature as RecipeRunObservationReceiptSignature,
    HostOperation as HostHelperOperation, HostRuntimeRequest,
    HostRuntimeRequestAction as HostRuntimeAction, RecipeRunInspectionBinding,
    RecipeRunObservationReceiptClaims,
    RecipeRunObservationReceiptClaimsOutcome as RecipeRunObservationOutcome,
    RecipeRunObservationWire, RecipeRunObservationsWire,
    RestartVonkUnitOperationUnit as HostHelperRestartUnit, SignedHostHelperGrant,
    SignedRecipeRunObservationReceipt as RecipeRunObservationReceipt,
};

pub mod operation_progress;
pub use operation_progress::{
    OperationCheckpoint, OperationMemberProgress, OperationProgress, ProgressActivity,
};

pub mod failure_evidence;

pub mod package_upgrade;
pub use package_upgrade::{
    PackageActivationPhase, PackageActivationReceipt, PackageRollbackAuthority,
    PackageRollbackSource,
};

use std::collections::{BTreeMap, BTreeSet};

#[cfg(test)]
use chrono::DateTime;
use serde::{Serialize, de::DeserializeOwned};
use serde_json::Value;
use sha2::{Digest, Sha256};
use thiserror::Error;
#[cfg(test)]
use uuid::Uuid;

pub const MAX_HOST_RUNTIME_ARGUMENTS: usize = 512;
pub const MAX_DOCUMENT_BYTES: usize = 64 * 1024;
pub const MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES: usize =
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES + MAX_DOCUMENT_BYTES;
pub const RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY: &str = "vonk.recipe-run-observation-helper";
pub const RECIPE_RUN_OBSERVATION_SCHEMA_VERSION: u8 = 2;
const RECIPE_RUN_OBSERVATION_RECEIPT_DOMAIN: &[u8] = b"VONK-RECIPE-RUN-OBSERVATION-RECEIPT-V1\0";
pub const HOST_HELPER_AUTHORITY: &str = "vonk.host-maintenance-helper";
const HOST_HELPER_GRANT_DOMAIN: &[u8] = b"VONK-HOST-MAINTENANCE-HELPER-GRANT-V1\0";

impl HostHelperOperation {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        canonical_generated_json(self)?;
        let valid = match self {
            Self::InstallVonkDebOperation(operation) => {
                operation.rollback.valid()
                    && operation.rollback.source.package_sha256 != operation.package_sha256
            }
            Self::ConfirmPackageActivationOperation(_)
            | Self::RestartVonkUnitOperation(_)
            | Self::ScheduleRebootOperation(_) => true,
            Self::ExecuteContainerRuntimeRequestOperation(operation) => {
                operation.job_id.get_version() == Some(uuid::Version::Random)
                    && operation.operation_id.get_version() == Some(uuid::Version::Random)
                    && operation.fence.get_version() == Some(uuid::Version::Random)
                    && operation
                        .observation_identity_sha256
                        .as_ref()
                        .is_none_or(|digest| {
                            operation.action == HostHelperContainerRuntimeAction::RunInspect
                                && lower_hex(digest, 64)
                        })
            }
        };
        if valid {
            Ok(())
        } else {
            Err(ProtocolError::Identity("host helper operation"))
        }
    }
}

impl HostHelperGrantClaims {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1
            || self.authority != HOST_HELPER_AUTHORITY
            || self.request_id.get_version() != Some(uuid::Version::Random)
            || !valid_node_id(&self.node_id)
            || self.issued_at <= 0
            || !(1..=300).contains(&(self.expires_at - self.issued_at))
        {
            return Err(ProtocolError::Identity("host helper grant claims"));
        }
        self.operation.validate()
    }
}

impl SignedHostHelperGrant {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        self.claims.validate()?;
        if self.schema_version != 1
            || self.signature.algorithm != "ed25519"
            || !lower_hex(&self.signature.key_id, 64)
            || !lower_hex(&self.signature.value, 128)
        {
            return Err(ProtocolError::Identity("signed host helper grant"));
        }
        Ok(())
    }
}

pub fn host_helper_grant_signing_bytes(
    claims: &HostHelperGrantClaims,
) -> Result<Vec<u8>, ProtocolError> {
    claims.validate()?;
    let mut value = HOST_HELPER_GRANT_DOMAIN.to_vec();
    value.extend(canonical_json(claims)?);
    Ok(value)
}

impl HostRuntimeRequest {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1
            || self.attempt == 0
            || (self.arguments.is_empty() != (self.action == HostRuntimeAction::RuntimePreflight))
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
impl RecipeRunInspectionBinding {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.mapping_generation == 0
            || self.run_generation == 0
            || self.world_size == 0
            || self.rank >= self.world_size
            || self.port == 0
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
        let singleton = self.world_size == 1;
        let rendezvous_valid = if singleton {
            self.local_address.is_none()
                && self.master_address.is_none()
                && self.master_port.is_none()
        } else {
            self.local_address.is_some_and(valid_fabric_address)
                && self.master_address.is_some_and(valid_fabric_address)
                && self.master_port.is_some_and(|port| port >= 1024)
        };
        if !rendezvous_valid {
            return Err(ProtocolError::Identity("recipe run inspection rendezvous"));
        }
        Ok(())
    }
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

impl RecipeRunObservationWire {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        RecipeRunInspectionBinding::from(self).validate()?;
        self.helper_receipt.validate()?;
        if self.schema_version != 1
            || !valid_node_id(&self.node_id)
            || self.node_id.is_empty()
            || !lower_hex(&self.observation_identity_sha256, 64)
            || !lower_hex(&self.observation_receipt_public_key, 64)
            || self.grant.validate().is_err()
            || self.grant.claims.node_id != self.node_id
            || self.grant.claims.request_id != self.helper_receipt.claims.request_id
            || self.helper_receipt.claims.node_id != self.node_id
            || self.helper_receipt.claims.observation_identity_sha256
                != self.observation_identity_sha256
            || match &self.grant.claims.operation {
                HostHelperOperation::ExecuteContainerRuntimeRequestOperation(operation) => {
                    operation.action != HostHelperContainerRuntimeAction::RunInspect
                        || operation.job_id != self.run_id
                        || u32::try_from(self.run_generation).ok() != Some(operation.attempt)
                        || operation.request_sha256 != self.helper_receipt.claims.request_sha256
                        || operation.observation_identity_sha256.as_deref()
                            != Some(self.observation_identity_sha256.as_str())
                }
                _ => true,
            }
            || self.observed_at.timestamp() != self.helper_receipt.claims.observed_at
            || (self.local_address == self.master_address) != self.endpoint_ready.is_some()
        {
            return Err(ProtocolError::Identity("recipe run observation"));
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
                | "runtime.preflight.v1"
                | "artifact.distribution.v1"
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
        let maximum_bytes = if matches!(
            self.operation.as_str(),
            "recipe.install" | "recipe.start" | "recipe.job.run.v1"
        ) {
            MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES
        } else {
            MAX_DOCUMENT_BYTES
        };
        if payload.len() > maximum_bytes || hex_sha256(&payload) != self.payload_digest {
            return Err(ProtocolError::Identity("claim payload digest"));
        }
        Ok(())
    }
}

/// Agent command to consume one Controller assignment. The destination is
/// selected by the agent configuration, so claims cannot choose a filesystem
/// path; only the plan identity crosses the wire.
/// Canonical schema-1 inventory evidence reported by a Spark agent.
impl InventoryRequest {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1
            || self.disk_total_bytes > 16 * 1024_u64.pow(4)
            || self.disk_free_bytes > 16 * 1024_u64.pow(4)
            || self.host_memory_total_bytes > 16 * 1024_u64.pow(4)
            || self.host_memory_free_bytes > 16 * 1024_u64.pow(4)
            || self.gpu_memory_total_bytes > 16 * 1024_u64.pow(4)
            || self.gpu_memory_free_bytes > 16 * 1024_u64.pow(4)
            || self.gpu_count > 64
            || self.disk_free_bytes > self.disk_total_bytes
            || self.host_memory_free_bytes > self.host_memory_total_bytes
            || self.gpu_memory_free_bytes > self.gpu_memory_total_bytes
            || self.capabilities.len() > 64
            || self
                .capabilities
                .iter()
                .any(|value| !valid_inventory_capability(value))
            || {
                let mut unique = BTreeSet::new();
                self.capabilities.iter().any(|value| !unique.insert(value))
            }
            || self.nvidia_driver_version.is_empty()
            || self.container_runtime_version.is_empty()
            || self.nvidia_driver_version.len() > 256
            || self.container_runtime_version.len() > 256
            || !self.nvidia_driver_version.is_ascii()
            || !self.container_runtime_version.is_ascii()
            || (self.fabric_address.is_none() != self.fabric_bandwidth_mbps.is_none())
            || self
                .fabric_bandwidth_mbps
                .is_some_and(|value| !(1..=1_000_000).contains(&value))
        {
            return Err(ProtocolError::Identity("inventory request"));
        }
        Ok(())
    }
}

fn valid_inventory_capability(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || (index > 0 && matches!(byte, b'.' | b'_' | b'-'))
        })
}

impl ArtifactDistributionRequest {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 1
            || !lower_hex(&self.authority_revision, 64)
            || !lower_hex(&self.plan_digest, 64)
        {
            return Err(ProtocolError::Identity("artifact distribution request"));
        }
        Ok(())
    }
}

impl AgentUpgradeRequest {
    pub fn parse(claim: &AgentClaim) -> Result<Self, ProtocolError> {
        if claim.operation != "agent.upgrade.v1" {
            return Err(ProtocolError::Identity("agent upgrade operation"));
        }
        let generated::AgentClaimPayload::AgentUpgradePayload(value) = &claim.payload else {
            return Err(ProtocolError::Identity("agent upgrade payload"));
        };
        let value = value.clone();
        let url = url::Url::parse(&value.package_url)
            .map_err(|_| ProtocolError::Identity("agent upgrade URL"))?;
        let source_url = url::Url::parse(&value.source_package_url)
            .map_err(|_| ProtocolError::Identity("rollback package URL"))?;
        if !value.rollback.valid()
            || !(1..=1024 * 1024 * 1024).contains(&value.source_package_bytes)
            || source_url.scheme() != "https"
            || source_url.host_str() != Some("install.vonkforge.ai")
            || source_url.port().is_some()
            || !source_url.username().is_empty()
            || source_url.password().is_some()
            || source_url.query().is_some()
            || source_url.fragment().is_some()
            || !source_url.path().ends_with("/vonk-forge-agent.deb")
            || value.schema_version != 1
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

impl AgentProgress {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        validate_attempt_identity(self.schema_version, self.attempt, &self.node_id)?;
        if canonical_json(&self.progress)?.len() > MAX_DOCUMENT_BYTES {
            return Err(ProtocolError::Identity("progress document"));
        }
        self.progress.validate()?;
        Ok(())
    }
}

impl AgentDirective {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        validate_attempt_identity(self.schema_version, self.attempt, &self.node_id)
    }
}

/// A content-addressed object authorized by one exact Controller assignment.
impl DistributionObject {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        let valid_name = !self.name.is_empty()
            && self.name.chars().count() <= 512
            && !self.name.starts_with('/')
            && !self.name.contains(['\\', '\0']);
        if !valid_name
            || self
                .name
                .split('/')
                .any(|part| part.is_empty() || part == "." || part == "..")
            || !lower_hex(&self.sha256, 64)
            || self.bytes > 16 * 1024_u64.pow(4)
            || self.bytes == 0
                && !(self.kind == "model"
                    && self.sha256
                        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
            || !matches!(self.kind.as_str(), "model" | "oci-archive" | "oci-layer")
        {
            return Err(ProtocolError::Identity("distribution object"));
        }
        Ok(())
    }
}

/// Controller-issued, node-scoped authorization for a complete model and
/// executable image set.  The assignment is carried alongside every fetch;
/// an enrolled agent cannot turn a digest into a general object browser.
impl DistributionAssignment {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.schema_version != 2
            || self.assignment_id.get_version() != Some(uuid::Version::Random)
            || self.generation == 0
            || !valid_node_id(&self.node_id)
            || self.expires_at.offset().local_minus_utc() != 0
            || !lower_hex(&self.plan_digest, 64)
            || !lower_hex(&self.model_artifact_set_sha256, 64)
            || !valid_oci_digest(&self.oci_image_digest)
            || !lower_hex(&self.oci_archive_sha256, 64)
            || self.objects.is_empty()
            || self.objects.len() > 4096
        {
            return Err(ProtocolError::Identity("distribution assignment"));
        }
        let mut digests = BTreeSet::new();
        let mut has_model = false;
        let mut has_archive = false;
        for object in &self.objects {
            object.validate()?;
            if !digests.insert(object.sha256.as_str()) {
                return Err(ProtocolError::Identity("distribution object duplicate"));
            }
            has_model |= object.kind == "model";
            has_archive |= object.kind == "oci-archive" && object.sha256 == self.oci_archive_sha256;
        }
        if !has_model || !has_archive {
            return Err(ProtocolError::Identity(
                "distribution assignment object coverage",
            ));
        }
        Ok(())
    }
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

impl EnrollmentEvidence {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if !lower_hex(&self.observation_receipt_public_key, 64) {
            return Err(ProtocolError::Identity(
                "enrollment observation receipt public key",
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum RecipeOperationRequest {
    RuntimePreflight(runtime_preflight::RuntimePreflightRequest),
    Build(Box<RecipeBuildRequest>),
    ImageImport(RecipeImageImportRequest),
    JobRun(RecipeJobRunRequest),
    Install(RecipeInstallRequest),
    Start(RecipeStartRequest),
    Stop(RecipeStopRequest),
    Uninstall(RecipeUninstallRequest),
    ModelCleanup(RecipeModelCleanupRequest),
}

impl RecipeJobRunResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        let manifest = serde_json::json!({
            "schema_version": self.output_manifest.schema_version,
            "total_bytes": self.output_manifest.total_bytes,
            "files": self.output_manifest.files,
        });
        let valid = self.schema_version == 1
            && self
                .diagnostics
                .as_ref()
                .is_none_or(|value| value.validate().is_ok())
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
                .try_fold(0_u64, |total, file| {
                    total.checked_add(u64::from(file.size_bytes))
                })
                == Some(u64::from(self.output_manifest.total_bytes))
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

/// Typed receipt emitted after a recipe image has been built and the exported
/// archive has been bound to its content identity.
/// Typed receipt emitted after a node verifies and imports the exact build
/// archive identified by the Controller operation.
impl RecipeStopResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.stopped {
            Ok(())
        } else {
            Err(ProtocolError::Identity("recipe stop result"))
        }
    }
}

impl RecipeUninstallResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if self.uninstalled && self.removed_model_bytes <= 16 * 1024_u64.pow(4) {
            Ok(())
        } else {
            Err(ProtocolError::Identity("recipe uninstall result"))
        }
    }
}

impl RecipeModelCleanupResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if (1..=512).contains(&self.uninstalled_installations)
            && self.removed_model_bytes <= 16 * 1024_u64.pow(4)
        {
            Ok(())
        } else {
            Err(ProtocolError::Identity("recipe model cleanup result"))
        }
    }
}

impl RecipeOperationRequest {
    pub fn parse(claim: &AgentClaim) -> Result<Self, ProtocolError> {
        claim.validate()?;
        let request = match (claim.operation.as_str(), &claim.payload) {
            (
                "runtime.preflight.v1",
                generated::AgentClaimPayload::RuntimePreflightRequest(value),
            ) => Self::RuntimePreflight(value.clone()),
            ("recipe.build.v1", generated::AgentClaimPayload::RecipeBuildRequest(value)) => {
                Self::Build(Box::new(value.clone()))
            }
            (
                "recipe.image.import.v1",
                generated::AgentClaimPayload::RecipeImageImportRequest(value),
            ) => Self::ImageImport(value.clone()),
            ("recipe.job.run.v1", generated::AgentClaimPayload::RecipeJobRunRequest(value)) => {
                Self::JobRun(value.clone())
            }
            ("recipe.install", generated::AgentClaimPayload::RecipeInstallPayload(value)) => {
                Self::Install(value.clone())
            }
            ("recipe.start", generated::AgentClaimPayload::RecipeStartPayload(value)) => {
                Self::Start(value.clone())
            }
            ("recipe.stop", generated::AgentClaimPayload::RecipeStopPayload(value)) => {
                Self::Stop(value.clone())
            }
            ("recipe.uninstall", generated::AgentClaimPayload::RecipeUninstallPayload(value)) => {
                Self::Uninstall(value.clone())
            }
            (
                "recipe.model-uninstall.v1",
                generated::AgentClaimPayload::RecipeModelCleanupPayload(value),
            ) => Self::ModelCleanup(value.clone()),
            _ => return Err(ProtocolError::Identity("recipe operation payload")),
        };
        request.validate()?;
        Ok(request)
    }

    fn validate(&self) -> Result<(), ProtocolError> {
        let valid_common = |version: u8, plan: &str| version == 1 && lower_hex(plan, 64);
        let valid = match self {
            Self::RuntimePreflight(value) => value.validate().is_ok(),
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
                value.schema_version == 2
                    && lower_hex(&value.plan_digest, 64)
                    && value.expected_bytes <= 16 * 1024_u64.pow(4)
                    && valid_role(&value.role)
                    && value.compiled_execution_plan.schema_version == 2
            }
            Self::Start(value) => {
                let valid_phase = match (&value.phase, &value.start_deadline, value.run_generation)
                {
                    // Role-ordered distributed starts are deliberately
                    // unphased.  The collective readiness variant carries
                    // the complete phase envelope below.
                    (None, None, None) => value.world_size > 1,
                    // A singleton has no rendezvous phase, but still carries
                    // its run generation so its exact observation binding is
                    // persisted from the initial start.
                    (None, None, Some(generation)) => generation > 0,
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
                value.schema_version == 2
                    && lower_hex(&value.plan_digest, 64)
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
                    && value.compiled_execution_plan.schema_version == 2
            }
            Self::Stop(value) => valid_common(value.schema_version, &value.plan_digest),
            Self::Uninstall(value) => {
                valid_common(value.schema_version, &value.plan_digest)
                    && lower_hex(&value.recipe_content_sha256, 64)
                    && value
                        .cleanup_model_content_sha256
                        .as_ref()
                        .is_none_or(|digest| lower_hex(digest, 64))
            }
            Self::ModelCleanup(value) => {
                valid_common(value.schema_version, &value.plan_digest)
                    && lower_hex(&value.model_content_sha256, 64)
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
mod inventory_tests {
    use super::*;

    fn inventory() -> InventoryRequest {
        InventoryRequest {
            schema_version: 1,
            observed_at: "2026-08-03T00:00:00Z".parse().unwrap(),
            disk_total_bytes: 2 * 1024_u64.pow(4),
            disk_free_bytes: 1024_u64.pow(4),
            host_memory_total_bytes: 2 * 1024_u64.pow(4),
            host_memory_free_bytes: 1024_u64.pow(4),
            gpu_memory_total_bytes: 100_000,
            gpu_memory_free_bytes: 80_000,
            gpu_count: 1,
            artifact_store_read_only: false,
            capabilities: vec!["recipe.build.v1".to_owned()],
            fabric_address: None,
            fabric_bandwidth_mbps: None,
            nvidia_driver_version: "550.1".to_owned(),
            container_runtime_version: "podman-5".to_owned(),
        }
    }

    #[test]
    fn inventory_validation_matches_python_bounds_and_shapes() {
        let value = inventory();
        value.validate().unwrap();

        let mut invalid = value.clone();
        invalid.gpu_count = 65;
        assert!(invalid.validate().is_err());
        let mut invalid = value.clone();
        invalid.capabilities = vec!["Recipe.Build".to_owned()];
        assert!(invalid.validate().is_err());
        let mut invalid = value.clone();
        invalid.fabric_bandwidth_mbps = Some(0);
        assert!(invalid.validate().is_err());
        let mut invalid = value;
        invalid.nvidia_driver_version = "é".to_owned();
        assert!(invalid.validate().is_err());
    }
}

#[cfg(test)]
mod recipe_model_cleanup_tests {
    use super::*;

    fn claim(payload: Value) -> AgentClaim {
        AgentClaim {
            attempt: 1,
            authority_revision: "a".repeat(64),
            deadline: "2026-09-01T12:00:00+00:00".parse().unwrap(),
            fence: Uuid::parse_str("00000000-0000-4000-8000-000000000001").unwrap(),
            job_id: Uuid::parse_str("00000000-0000-4000-8000-000000000002").unwrap(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            operation: "recipe.model-uninstall.v1".to_owned(),
            operation_id: Uuid::parse_str("00000000-0000-4000-8000-000000000003").unwrap(),
            payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
            payload,
            schema_version: 1,
        }
    }

    #[test]
    fn controller_model_cleanup_payload_uses_content_digest_and_rejects_retired_name() {
        let payload = serde_json::json!({
            "schema_version": 1,
            "model_content_sha256": "f".repeat(64),
            "plan_digest": "b".repeat(64),
            "installations": [{
                "installation_id": "00000000-0000-4000-8000-000000000004",
                "recipe_content_sha256": "c".repeat(64)
            }]
        });
        let parsed = RecipeOperationRequest::parse(&claim(payload.clone())).unwrap();
        let RecipeOperationRequest::ModelCleanup(request) = parsed else {
            panic!("model cleanup payload parsed as the wrong operation");
        };
        assert_eq!(request.model_content_sha256, "f".repeat(64));

        let mut retired = payload;
        retired["model_version_sha256"] = retired["model_content_sha256"].take();
        assert!(RecipeOperationRequest::parse(&claim(retired)).is_err());
    }

    #[test]
    fn uninstall_payload_requires_explicit_nullable_cleanup_field() {
        let mut payload = serde_json::json!({
            "schema_version": 1,
            "installation_id": "00000000-0000-4000-8000-000000000004",
            "plan_digest": "b".repeat(64),
            "recipe_content_sha256": "c".repeat(64),
            "cleanup_model_content_sha256": null,
        });
        let mut uninstall_claim = claim(serde_json::json!({
            "schema_version": 1,
            "installation_id": "00000000-0000-4000-8000-000000000004",
            "plan_digest": "b".repeat(64),
            "recipe_content_sha256": "c".repeat(64),
            "cleanup_model_content_sha256": null,
        }));
        uninstall_claim.operation = "recipe.uninstall".to_owned();
        assert!(RecipeOperationRequest::parse(&uninstall_claim).is_ok());
        payload
            .as_object_mut()
            .unwrap()
            .remove("cleanup_model_content_sha256");
        uninstall_claim.payload = payload;
        uninstall_claim.payload_digest =
            hex_sha256(&canonical_json(&uninstall_claim.payload).unwrap());
        assert!(RecipeOperationRequest::parse(&uninstall_claim).is_err());
    }

    #[test]
    fn lifecycle_success_bodies_are_strict_and_bounded() {
        let stop: RecipeStopResult =
            serde_json::from_value(serde_json::json!({"stopped": true})).unwrap();
        assert!(stop.validate().is_ok());
        assert!(
            serde_json::from_value::<RecipeStopResult>(
                serde_json::json!({"stopped": true, "extra": 1})
            )
            .is_err()
        );
        let uninstall: RecipeUninstallResult = serde_json::from_value(
            serde_json::json!({"uninstalled": true, "removed_model_bytes": 0}),
        )
        .unwrap();
        assert!(uninstall.validate().is_ok());
        let oversized_uninstall: RecipeUninstallResult = serde_json::from_value(
            serde_json::json!({"uninstalled": true, "removed_model_bytes": 17592186044417_u64}),
        )
        .unwrap();
        assert!(oversized_uninstall.validate().is_err());
        let cleanup: RecipeModelCleanupResult = serde_json::from_value(
            serde_json::json!({"uninstalled_installations": 1, "removed_model_bytes": 0}),
        )
        .unwrap();
        assert!(cleanup.validate().is_ok());
        assert!(
            serde_json::from_value::<RecipeModelCleanupResult>(
                serde_json::json!({"uninstalled_installations": 0, "removed_model_bytes": 0})
            )
            .unwrap()
            .validate()
            .is_err()
        );
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
            "compiled_execution_plan": {},
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
            "schema_version": 2,
            "world_size": world_size,
        });
        if world_size == 1 {
            payload
                .as_object_mut()
                .unwrap()
                .insert("run_generation".to_owned(), Value::from(1));
        }
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
    fn schema_two_start_payload_allows_unphased_distributed_role_ordering() {
        let single = parsed_start(start_payload(1, 0, None, None, None)).unwrap();
        assert_eq!(single.phase, None);
        assert_eq!(single.start_deadline, None);
        assert_eq!(single.run_generation, Some(1));
        let unphased_wire = serde_json::to_value(single).unwrap();
        assert!(unphased_wire.get("phase").is_none());
        assert!(unphased_wire.get("start_deadline").is_none());

        for field in ["local_address", "master_address", "master_port"] {
            let mut omitted = start_payload(1, 0, None, None, None);
            omitted.as_object_mut().unwrap().remove(field);
            assert!(
                parsed_start(omitted).is_err(),
                "omitted singleton field {field} must be rejected"
            );
        }

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
        assert_eq!(distributed.run_generation, None);
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

        for field in ["local_address", "master_address", "master_port"] {
            let mut omitted = start_payload(
                2,
                1,
                Some("192.168.100.3"),
                Some("192.168.100.2"),
                Some("rank-launch"),
            );
            omitted.as_object_mut().unwrap().remove(field);
            assert!(
                parsed_start(omitted).is_err(),
                "omitted distributed field {field} must be rejected"
            );
        }
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

        let mut missing_phase = start_payload(
            2,
            1,
            Some("192.168.100.3"),
            Some("192.168.100.2"),
            Some("rank-launch"),
        );
        missing_phase.as_object_mut().unwrap().remove("phase");
        assert!(parsed_start(missing_phase).is_err());

        let mut unphased_with_deadline =
            start_payload(2, 1, Some("192.168.100.3"), Some("192.168.100.2"), None);
        unphased_with_deadline.as_object_mut().unwrap().insert(
            "start_deadline".to_owned(),
            Value::String("2026-09-01T12:00:00+00:00".to_owned()),
        );
        assert!(parsed_start(unphased_with_deadline).is_err());

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

    #[test]
    fn authenticated_launch_claims_use_the_dedicated_document_ceiling() {
        let mut payload = start_payload(1, 0, None, None, None);
        payload
            .as_object_mut()
            .unwrap()
            .get_mut("compiled_execution_plan")
            .unwrap()
            .as_object_mut()
            .unwrap()
            .insert("artifact".to_owned(), Value::String("x".repeat(516 * 1024)));
        assert!(claim(payload).validate().is_ok());

        let oversized = serde_json::json!({
            "value": "x".repeat(MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES)
        });
        assert!(claim(oversized).validate().is_err());
    }
}

#[cfg(test)]
mod recipe_install_tests {
    use super::*;

    #[test]
    fn schema_two_install_requires_the_inline_compiled_plan() {
        let payload = serde_json::json!({
            "compiled_execution_plan": {},
            "expected_bytes": 1024,
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "plan_digest": "a".repeat(64),
            "rank": 0,
            "role": "entrypoint",
            "schema_version": 2,
        });
        let claim = AgentClaim {
            attempt: 1,
            authority_revision: "b".repeat(64),
            deadline: "2026-09-01T12:00:00+00:00".parse().unwrap(),
            fence: Uuid::new_v4(),
            job_id: Uuid::new_v4(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            operation: "recipe.install".to_owned(),
            operation_id: Uuid::new_v4(),
            payload_digest: hex_sha256(&canonical_json(&payload).unwrap()),
            payload,
            schema_version: 1,
        };
        let RecipeOperationRequest::Install(request) =
            RecipeOperationRequest::parse(&claim).expect("schema 2 install wire should parse")
        else {
            panic!("expected install request");
        };
        assert_eq!(request.schema_version, 2);
        assert!(request.compiled_execution_plan.is_object());
    }
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
        && value.inputs.iter().try_fold(0_u64, |total, file| {
            total.checked_add(u64::from(file.size_bytes))
        }) == Some(u64::from(value.input_total_bytes))
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
        && value.compiled_execution_plan.schema_version == 2
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

fn validate_build(value: &RecipeBuildRequest) -> bool {
    value.schema_version == 1
        && value.kind == "recipe.build.v1"
        && lower_hex(&value.recipe_content_sha256, 64)
        && lower_hex(&value.source_bundle_sha256, 64)
        && lower_hex(&value.build_input_sha256, 64)
        && (1..=64 * 1024 * 1024).contains(&value.source_bundle_bytes)
        && value.platform == "linux/arm64"
        && valid_bundle_path(&value.dockerfile)
        && value.target.as_ref().is_none_or(|target| {
            !target.is_empty()
                && target.len() <= 64
                && target
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
        })
        && value.arguments.len() <= 64
        && value
            .arguments
            .iter()
            .all(|argument| valid_name(&argument.name) && valid_scalar(&argument.value))
        && value.capabilities.len() <= 11
        && value
            .capabilities
            .iter()
            .enumerate()
            .all(|(index, capability)| {
                !capability.starts_with("SYS_")
                    && matches!(
                        capability.as_str(),
                        "CHOWN"
                            | "DAC_OVERRIDE"
                            | "FOWNER"
                            | "FSETID"
                            | "KILL"
                            | "MKNOD"
                            | "NET_BIND_SERVICE"
                            | "SETFCAP"
                            | "SETGID"
                            | "SETPCAP"
                            | "SETUID"
                    )
                    && !value.capabilities[..index].contains(capability)
            })
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
        && valid_build_options(&value.options)
        && value.limits.cpu_cores >= 1
        && value.limits.cpu_cores <= 256
        && value.limits.memory_bytes > 0
        && value.limits.memory_bytes <= 16 * 1024_u64.pow(4)
        && value.limits.temporary_bytes > 0
        && value.limits.temporary_bytes <= 16 * 1024_u64.pow(4)
        && value.limits.processes >= 1
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

fn valid_build_metadata_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            (index > 0 || byte.is_ascii_alphanumeric())
                && (byte.is_ascii_alphanumeric() || b"._/-".contains(&byte))
        })
}

fn valid_build_environment_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            (index > 0 || byte.is_ascii_uppercase() || byte == b'_')
                && (byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
        })
}

fn valid_build_options(value: &RecipeBuildOptions) -> bool {
    value.additional_contexts.len() <= 16
        && value
            .additional_contexts
            .iter()
            .enumerate()
            .all(|(index, item)| {
                valid_name(&item.name)
                    && valid_bundle_path(&item.path)
                    && !value.additional_contexts[..index]
                        .iter()
                        .any(|prior| prior.name == item.name)
            })
        && [&value.annotations, &value.labels, &value.layer_labels]
            .into_iter()
            .all(|entries| {
                entries.len() <= 64
                    && entries.iter().enumerate().all(|(index, item)| {
                        valid_build_metadata_name(&item.name)
                            && item.value.len() <= 1024
                            && !item.value.contains('\0')
                            && !entries[..index].iter().any(|prior| prior.name == item.name)
                    })
            })
        && value.environment.len() <= 64
        && value.environment.iter().enumerate().all(|(index, item)| {
            valid_build_environment_name(&item.name)
                && valid_scalar(&item.value)
                && !value.environment[..index]
                    .iter()
                    .any(|prior| prior.name == item.name)
        })
        && matches!(value.format.as_str(), "oci" | "docker")
        && value
            .ignorefile
            .as_ref()
            .is_none_or(|path| valid_bundle_path(path))
        && (1..=32).contains(&value.jobs)
        && matches!(value.layer_compression.as_str(), "disabled" | "gzip")
        && value.os_features.len() <= 32
        && value
            .os_features
            .iter()
            .enumerate()
            .all(|(index, feature)| {
                !feature.is_empty()
                    && feature.len() <= 64
                    && feature
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
                    && !value.os_features[..index].contains(feature)
            })
        && value.os_version.as_ref().is_none_or(|version| {
            !version.is_empty()
                && version.len() <= 64
                && version
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"._+-".contains(&byte))
        })
        && (65_536..=64 * 1024_u64.pow(3)).contains(&value.shm_bytes)
        && matches!(value.squash.as_str(), "none" | "new" | "all")
        && value
            .timestamp
            .is_none_or(|timestamp| timestamp <= 4_102_444_800)
        && value.unset_environment.len() <= 64
        && value
            .unset_environment
            .iter()
            .enumerate()
            .all(|(index, name)| {
                valid_build_environment_name(name)
                    && !value.unset_environment[..index].contains(name)
            })
        && value.unset_labels.len() <= 64
        && value.unset_labels.iter().enumerate().all(|(index, name)| {
            valid_build_metadata_name(name) && !value.unset_labels[..index].contains(name)
        })
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
            compiled_execution_plan: serde_json::json!({}),
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
    fn job_contract_binds_canonical_inputs_and_requires_plan_object() {
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
        command.compiled_execution_plan = serde_json::json!(null);
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
            local_address: Some("192.168.100.11".parse().unwrap()),
            master_address: Some("192.168.100.10".parse().unwrap()),
            master_port: Some(29500),
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

    #[test]
    fn singleton_observation_uses_the_same_required_signed_shape() {
        let mut binding = binding();
        binding.rank = 0;
        binding.role = "entrypoint".to_owned();
        binding.world_size = 1;
        binding.local_address = None;
        binding.master_address = None;
        binding.master_port = None;
        binding.validate().unwrap();

        let observed_at = DateTime::from_timestamp(1_788_000_000, 0).unwrap();
        let identity_sha256 = "a".repeat(64);
        let receipt = RecipeRunObservationReceipt {
            schema_version: 1,
            claims: RecipeRunObservationReceiptClaims {
                schema_version: 1,
                authority: RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY.to_owned(),
                node_id: "spk_11111111111111111111111111111111".to_owned(),
                request_id: Uuid::new_v4(),
                request_sha256: "b".repeat(64),
                observation_identity_sha256: identity_sha256.clone(),
                outcome: RecipeRunObservationOutcome::Running,
                observed_at: observed_at.timestamp(),
            },
            signature: RecipeRunObservationReceiptSignature {
                algorithm: "ed25519".to_owned(),
                key_id: "c".repeat(64),
                value: "d".repeat(128),
            },
        };
        let grant = SignedHostHelperGrant {
            schema_version: 1,
            claims: HostHelperGrantClaims {
                schema_version: 1,
                authority: "vonk.host-maintenance-helper".to_owned(),
                request_id: receipt.claims.request_id,
                node_id: receipt.claims.node_id.clone(),
                issued_at: observed_at.timestamp(),
                expires_at: observed_at.timestamp() + 60,
                operation: HostHelperOperation::ExecuteContainerRuntimeRequest {
                    action: HostHelperContainerRuntimeAction::RunInspect,
                    job_id: binding.run_id,
                    operation_id: Uuid::new_v4(),
                    attempt: binding.run_generation as u32,
                    fence: Uuid::new_v4(),
                    request_sha256: "b".repeat(64),
                    observation_identity_sha256: Some(identity_sha256.clone()),
                },
            },
            signature: HostHelperGrantSignature {
                algorithm: "ed25519".to_owned(),
                key_id: "f".repeat(64),
                value: "e".repeat(128),
            },
        };
        let observation = RecipeRunObservationWire {
            schema_version: 1,
            node_id: receipt.claims.node_id.clone(),
            binding,
            observed_at,
            endpoint_ready: Some(true),
            observation_identity_sha256: identity_sha256,
            grant,
            helper_receipt: receipt,
            observation_receipt_public_key: "e".repeat(64),
        };
        observation.validate().unwrap();
        let mut wrong_operation = observation.clone();
        wrong_operation.grant.claims.operation = HostHelperOperation::RestartVonkUnit {
            unit: HostHelperRestartUnit::Agent,
        };
        assert!(wrong_operation.validate().is_err());

        let mut partial_rendezvous = observation.binding.clone();
        partial_rendezvous.world_size = 2;
        partial_rendezvous.master_address = Some("10.0.0.2".parse().unwrap());
        assert!(partial_rendezvous.validate().is_err());

        let mut encoded = serde_json::to_value(&observation).unwrap();
        encoded.as_object_mut().unwrap().remove("endpoint_ready");
        assert!(serde_json::from_value::<RecipeRunObservationWire>(encoded).is_err());
    }
}

#[cfg(test)]
mod distribution_tests {
    use super::*;

    fn assignment() -> DistributionAssignment {
        DistributionAssignment {
            schema_version: 2,
            assignment_id: Uuid::new_v4(),
            plan_digest: "a".repeat(64),
            generation: 3,
            node_id: "spk_".to_owned() + &"b".repeat(32),
            expires_at: DateTime::parse_from_rfc3339("2026-09-05T12:00:00+00:00").unwrap(),
            model_artifact_set_sha256: "c".repeat(64),
            objects: vec![
                DistributionObject {
                    name: "weights/model.bin".to_owned(),
                    sha256: "d".repeat(64),
                    bytes: 13,
                    kind: "model".to_owned(),
                },
                DistributionObject {
                    name: "image.oci.tar".to_owned(),
                    sha256: "e".repeat(64),
                    bytes: 11,
                    kind: "oci-archive".to_owned(),
                },
            ],
            oci_image_digest: "sha256:".to_owned() + &"f".repeat(64),
            oci_archive_sha256: "e".repeat(64),
        }
    }

    #[test]
    fn assignment_serde_round_trip_matches_python_wire_shape() {
        let value = assignment();
        value.validate().unwrap();
        let encoded = serde_json::to_value(&value).unwrap();
        let decoded: DistributionAssignment = serde_json::from_value(encoded.clone()).unwrap();
        assert_eq!(decoded, value);
        assert_eq!(serde_json::to_value(decoded).unwrap(), encoded);
    }

    #[test]
    fn object_name_validation_matches_python_boundary() {
        let mut value = assignment();
        for name in [
            "weights/model bin",
            "__init__.py",
            "nested/UPPERCASE.bin",
            "模型.bin",
        ] {
            value.objects[0].name = name.to_owned();
            value.validate().unwrap();
        }
        value.objects[0].name = "模型 file_".repeat(64);
        value.validate().unwrap();
        value.objects[0].name = "../model.bin".to_owned();
        assert!(value.validate().is_err());
        value.objects[0].name = "model\0.bin".to_owned();
        assert!(value.validate().is_err());
    }

    #[test]
    fn empty_model_support_object_uses_the_canonical_empty_digest() {
        let mut value = assignment();
        value.objects[0] = DistributionObject {
            name: "tokenizer_config.json".to_owned(),
            sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855".to_owned(),
            bytes: 0,
            kind: "model".to_owned(),
        };
        value.validate().unwrap();
        value.objects[0].kind = "oci-archive".to_owned();
        assert!(value.validate().is_err());
    }
}

impl generated::AgentRuntimeIdentity {
    pub fn observation_receipt_public_key(&self) -> Result<[u8; 32], ProtocolError> {
        if !lower_hex(&self.observation_receipt_public_key, 64) {
            return Err(ProtocolError::Identity("observation receipt public key"));
        }
        let mut bytes = [0; 32];
        for (index, pair) in self
            .observation_receipt_public_key
            .as_bytes()
            .chunks_exact(2)
            .enumerate()
        {
            let digit = |value: u8| {
                if value.is_ascii_digit() {
                    value - b'0'
                } else {
                    value - b'a' + 10
                }
            };
            bytes[index] = digit(pair[0]) * 16 + digit(pair[1]);
        }
        Ok(bytes)
    }
}

/// Validate an outgoing generated document before producing its canonical bytes.
/// This also covers direct Rust construction, which does not invoke Deserialize.
pub fn canonical_generated_json<T: Serialize + DeserializeOwned>(
    document: &T,
) -> Result<Vec<u8>, ProtocolError> {
    let value = serde_json::to_value(document)?;
    let _: T = serde_json::from_value(value.clone())?;
    canonical_json(&value)
}
