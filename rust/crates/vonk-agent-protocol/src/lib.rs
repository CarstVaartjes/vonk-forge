#![forbid(unsafe_code)]
#[rustfmt::skip]
pub mod compiled_execution_plan;
pub mod compiled_oci;
pub mod generated;
pub mod integer;
pub mod runtime_preflight;
mod wire_datetime;
mod wire_schema;
pub use generated::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult,
    AgentUpgradePayload as AgentUpgradeRequest,
    ArtifactDistributionPayload as ArtifactDistributionRequest, DistributionAssignment,
    DistributionObject, EnrollmentEvidence, EnrollmentSubmitRequest as EnrollmentRequest,
    InventoryRequest, InventoryRequestMemoryPool as MemoryPool, NetworkInterface,
    NetworkInterfaceKind, RecipeBuildAdapter, RecipeBuildAdapterDefinition,
    RecipeBuildAdditionalContext, RecipeBuildBaseImage, RecipeBuildCleanupEvidence,
    RecipeBuildCleanupRequest, RecipeBuildEvidence, RecipeBuildLimits, RecipeBuildMetadata,
    RecipeBuildNetwork, RecipeBuildOptions, RecipeBuildRequest,
    RecipeInstallPayload as RecipeInstallRequest, RecipeJobEvidence, RecipeJobFile,
    RecipeJobInputFile, RecipeJobOutputLimits, RecipeJobOutputManifest, RecipeJobOutputMapping,
    RecipeJobRunRequest, RecipeJobRunResult, RecipeReconcilePayload as RecipeReconcileRequest,
    RecipeReconcileResult, RecipeStartPayload as RecipeStartRequest,
    RecipeStartPayloadPhase as RecipeStartPhase, RecipeStopPayload as RecipeStopRequest,
    RecipeStopResult, RecipeUninstallPayload as RecipeUninstallRequest, RecipeUninstallResult,
};
pub use generated::{
    ExecuteContainerRuntimeRequestOperationAction as HostHelperContainerRuntimeAction,
    HostHelperGrantClaims, HostHelperSignature as HostHelperGrantSignature,
    HostOperation as HostHelperOperation, HostRuntimeRequest,
    HostRuntimeRequestAction as HostRuntimeAction, RecipeReconciliationIdentity,
    RecipeRunInspectionRequest, RecipeRunObservationWire, RecipeRunObservationsWire,
    SignedHostHelperGrant,
};

pub mod operation_progress;
pub use operation_progress::{
    OperationCheckpoint, OperationMemberProgress, OperationProgress, ProgressActivity,
};

pub mod failure_evidence;

pub mod passthrough;
pub use passthrough::{revalidate, validate_generated};

pub mod package_upgrade;
pub use package_upgrade::{
    PackageActivationPhase, PackageActivationReceipt, PackageRollbackAuthority,
    PackageRollbackSource,
};

use std::collections::BTreeSet;

#[cfg(test)]
use serde_json::Value;

use serde::{Serialize, de::DeserializeOwned};
use sha2::{Digest, Sha256};
use thiserror::Error;
#[cfg(test)]
use uuid::Uuid;

/// The authoritative ceiling on one privileged-helper frame, in bytes.
///
/// A frame is one signed helper message: the agent canonicalizes the signed
/// grant, frames it, and the helper allocates exactly the framed length before
/// it parses. This is the bound on the *grant*, not on the runtime request the
/// grant authorizes. The grant carries the four identities and the request
/// digest; the request document itself is never framed (see
/// [`MAX_HOST_RUNTIME_REQUEST_BYTES`]). The worst case is one framed grant plus
/// one framed reply and their parsed forms -- a few MiB, acceptable on a node
/// that already stages multi-gigabyte images.
pub const MAX_HELPER_FRAME_BYTES: usize = 1024 * 1024;
/// The authoritative ceiling on one canonical [`HostRuntimeRequest`], in bytes.
///
/// A request carries its exact typed Start, JobRun, or Stop plan plus projected
/// argv. The nested compiled plan is separately limited to 16 MiB and its
/// enclosing typed claim to 18 MiB; one helper frame of additional space bounds
/// the remaining request envelope. The helper reads the canonical request from
/// the agent's owner-only request file after verifying its signed digest.
pub const MAX_HOST_RUNTIME_REQUEST_BYTES: usize =
    MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES + MAX_HELPER_FRAME_BYTES;
/// The bytes a canonical [`HostRuntimeRequest`] spends on everything but its
/// argument payload: field names, identity and version fields, optional
/// typed-plan keys, and array syntax.
///
/// The projected argv's `MAX_ARGV_BYTES` remains separately bounded by one
/// helper frame minus this envelope. The complete request can be larger because
/// it also carries the exact typed plan and compiled execution claim. The value
/// is a margin above a measured maximum, never merely equal to one:
/// `the_declared_envelope_covers_the_largest_contract_permitted_request`
/// re-measures the largest contract-permitted envelope and fails if it
/// outgrows this constant.
pub const HOST_RUNTIME_REQUEST_ENVELOPE_BYTES: usize = 16 * 1024;
/// The ceiling on one generic claim payload or progress document.
///
/// The load-bearing case is an `artifact.distribution.v1` claim, whose
/// assignment admits up to 4096 distribution objects; at the measured ~120
/// bytes per object a full assignment is roughly 0.5 MiB, so the previous
/// 64 KiB was a latent refusal for a large model (the real GLM shape is ~150
/// objects, ~18 KiB -- only 3x under the old ceiling). 2 MiB is a backstop
/// above the admitted assignment; the compiled plan document is separately
/// bounded below.
pub const MAX_DOCUMENT_BYTES: usize = 2 * 1024 * 1024;
/// The ceiling on the compiled execution plan document.
///
/// This is the largest document on the wire, so it keeps the largest bound.
/// The plan admits 4096 artifacts; at the measured marginal cost of a fixture
/// artifact (~1.2 KiB) that is roughly 5 MiB, and the largest real plan (the
/// GLM EXL3 dual shape, 149 artifacts) is about 180 KiB. 16 MiB is already
/// more than 3x the admitted maximum and ~90x the real maximum, so it is kept.
/// It also bounds the agent's single largest HTTP body allocation, which is
/// why it is not raised further.
pub const MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES: usize =
    MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES + MAX_DOCUMENT_BYTES;
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
            Self::ConfirmPackageActivationOperation(_) => true,
            Self::ExecuteContainerRuntimeRequestOperation(operation) => {
                operation.fence.get_version() == Some(uuid::Version::Random)
                    && (operation.installation_id.is_some()
                        == (operation.action
                            == HostHelperContainerRuntimeAction::InstallationCleanup))
                    && operation
                        .reconciliation_identity
                        .as_ref()
                        .is_none_or(|identity| {
                            operation.action
                                == HostHelperContainerRuntimeAction::InstallationCleanup
                                && operation.installation_id == Some(identity.installation_id)
                                && valid_reconciliation_identity(identity)
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

/// One distinct contract of an agent-built [`HostRuntimeRequest`].
///
/// `validate()` used to answer every violation with one opaque `ProtocolError`,
/// so the agent could only report `helper_request_document_invalid` no matter
/// which rule refused. A live blocked Start could not say whether the typed
/// plan, argument envelope, or one argument's value was wrong.
///
/// A rule that measures a bound carries it, so a refusal can report the limit
/// and the observed value without ever carrying the argument itself.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum HostRuntimeRequestRule {
    /// Arguments are present or absent for the wrong action.
    #[error("host runtime request argument presence is invalid")]
    ArgumentsPresence,
    /// A typed start/stop plan is absent, duplicated, or bound to another
    /// generation.
    #[error("host runtime request plan binding is invalid")]
    PlanBinding,
    /// An installation identity is present or absent for the wrong action.
    #[error("host runtime request installation identity is invalid")]
    InstallationIdentity,
    /// The canonical request document is larger than the bounded helper
    /// exchange reads. The budget is a byte budget, so a count ceiling is
    /// deliberately absent: every argument costs at least three canonical
    /// bytes, so this rule always fires before any derived count could.
    #[error("host runtime request document exceeds the bounded exchange")]
    RequestBytes { limit: u64, observed: u64 },
    /// A typed plan or its nested compiled plan exceeds its declared ceiling.
    #[error("host runtime request plan exceeds its canonical byte ceiling")]
    PlanBytes { limit: u64, observed: u64 },
    /// An argument carries a NUL byte, which an exec argv cannot frame.
    #[error("host runtime request argument carries a NUL byte")]
    ArgumentNulByte { observed: u64 },
    /// The arguments could not be canonicalized.
    #[error("host runtime request arguments could not be encoded")]
    Encoding,
}

impl HostRuntimeRequestRule {
    /// The limit and the observed value of a rule that measured one.
    ///
    /// `None` means the rule is not a measured bound. A `None` limit is a rule
    /// with no numeric ceiling (a refused byte, not a refused length).
    pub fn bound(self) -> Option<(Option<u64>, u64)> {
        match self {
            Self::RequestBytes { limit, observed } | Self::PlanBytes { limit, observed } => {
                Some((Some(limit), observed))
            }
            Self::ArgumentNulByte { observed } => Some((None, observed)),
            _ => None,
        }
    }
}

impl HostRuntimeRequest {
    pub fn validate(&self) -> Result<(), HostRuntimeRequestRule> {
        if self.arguments.is_empty()
            != matches!(
                self.action,
                HostRuntimeAction::RuntimePreflight
                    | HostRuntimeAction::Stop
                    | HostRuntimeAction::InstallationCleanup
            )
        {
            return Err(HostRuntimeRequestRule::ArgumentsPresence);
        }
        if self.installation_id.is_some() != (self.action == HostRuntimeAction::InstallationCleanup)
        {
            return Err(HostRuntimeRequestRule::InstallationIdentity);
        }
        match (&self.action, &self.reconciliation_identity) {
            (_, None) => {}
            (HostRuntimeAction::InstallationCleanup, Some(identity))
                if valid_reconciliation_identity(identity)
                    && self.installation_id == Some(identity.installation_id) => {}
            _ => return Err(HostRuntimeRequestRule::InstallationIdentity),
        }
        match self.action {
            HostRuntimeAction::Start => {
                if (self.start_plan.is_none() == self.job_plan.is_none())
                    || self.stop_plan.is_some()
                {
                    return Err(HostRuntimeRequestRule::PlanBinding);
                }
                if let Some(plan) = &self.start_plan {
                    if self.run_generation != Some(plan.run_generation) {
                        return Err(HostRuntimeRequestRule::PlanBinding);
                    }
                    validate_runtime_plan_size(plan, &plan.compiled_execution_plan)?;
                } else if let Some(plan) = &self.job_plan {
                    if self.run_generation != Some(plan.run_generation) {
                        return Err(HostRuntimeRequestRule::PlanBinding);
                    }
                    validate_runtime_plan_size(plan, &plan.compiled_execution_plan)?;
                }
            }
            HostRuntimeAction::Stop => {
                let Some(plan) = &self.stop_plan else {
                    return Err(HostRuntimeRequestRule::PlanBinding);
                };
                if self.start_plan.is_some()
                    || self.job_plan.is_some()
                    || self.run_generation != Some(plan.run_generation)
                {
                    return Err(HostRuntimeRequestRule::PlanBinding);
                }
                let encoded = canonical_json(plan).map_err(|_| HostRuntimeRequestRule::Encoding)?;
                if encoded.len() > MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES {
                    return Err(HostRuntimeRequestRule::PlanBytes {
                        limit: MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES as u64,
                        observed: encoded.len() as u64,
                    });
                }
            }
            _ if self.start_plan.is_some()
                || self.job_plan.is_some()
                || self.stop_plan.is_some()
                || self.run_generation.is_some() =>
            {
                return Err(HostRuntimeRequestRule::PlanBinding);
            }
            _ => {}
        }
        let encoded = canonical_json(self).map_err(|_| HostRuntimeRequestRule::Encoding)?;
        if encoded.len() > MAX_HOST_RUNTIME_REQUEST_BYTES {
            return Err(HostRuntimeRequestRule::RequestBytes {
                limit: MAX_HOST_RUNTIME_REQUEST_BYTES as u64,
                observed: encoded.len() as u64,
            });
        }
        for value in &self.arguments {
            if value.contains('\0') {
                return Err(HostRuntimeRequestRule::ArgumentNulByte {
                    observed: value.len() as u64,
                });
            }
        }
        Ok(())
    }
}

fn validate_runtime_plan_size<T: Serialize, C: Serialize>(
    plan: &T,
    compiled_execution_plan: &C,
) -> Result<(), HostRuntimeRequestRule> {
    let compiled_bytes =
        canonical_json(compiled_execution_plan).map_err(|_| HostRuntimeRequestRule::Encoding)?;
    if compiled_bytes.len() > MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES {
        return Err(HostRuntimeRequestRule::PlanBytes {
            limit: MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES as u64,
            observed: compiled_bytes.len() as u64,
        });
    }
    let plan_bytes = canonical_json(plan).map_err(|_| HostRuntimeRequestRule::Encoding)?;
    if plan_bytes.len() > MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES {
        return Err(HostRuntimeRequestRule::PlanBytes {
            limit: MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES as u64,
            observed: plan_bytes.len() as u64,
        });
    }
    Ok(())
}

#[cfg(test)]
mod installation_cleanup_contract_tests {
    use super::*;

    fn request(action: HostRuntimeAction, installation_id: Option<Uuid>) -> HostRuntimeRequest {
        HostRuntimeRequest {
            action,
            fence: Uuid::new_v4(),
            arguments: Vec::new(),
            job_plan: None,
            installation_id,
            reconciliation_identity: None,
            run_generation: None,
            start_plan: None,
            stop_plan: None,
        }
    }

    #[test]
    fn cleanup_requires_one_installation_identity_and_other_actions_reject_it() {
        let installation_id = Uuid::new_v4();
        assert!(
            request(
                HostRuntimeAction::InstallationCleanup,
                Some(installation_id)
            )
            .validate()
            .is_ok()
        );
        assert!(
            request(HostRuntimeAction::InstallationCleanup, None)
                .validate()
                .is_err()
        );
        let mut ordinary = request(HostRuntimeAction::Start, Some(installation_id));
        ordinary.arguments.push("run".to_owned());
        assert!(ordinary.validate().is_err());

        for raw in [
            serde_json::json!({
                "action": "installation-cleanup",
                "fence": Uuid::new_v4(),
                "arguments": [],
            }),
            serde_json::json!({
                "action": "installation-cleanup",
                "fence": Uuid::new_v4(),
                "arguments": [],
                "installation_id": null,
            }),
        ] {
            let parsed: HostRuntimeRequest = serde_json::from_value(raw).unwrap();
            assert!(parsed.validate().is_err());
        }
    }
}

/// The bounds that decide whether a Start is framed at all.
///
/// Every test here names the wrong implementation it refuses to accept, so a
/// future change cannot quietly restore a round-number cap beside the byte
/// budget.
#[cfg(test)]
mod host_runtime_request_bound_tests {
    use super::{
        HOST_RUNTIME_REQUEST_ENVELOPE_BYTES, HostRuntimeAction, HostRuntimeRequest,
        HostRuntimeRequestRule, MAX_HOST_RUNTIME_REQUEST_BYTES, canonical_json,
    };
    use uuid::Uuid;

    fn start(arguments: Vec<String>) -> HostRuntimeRequest {
        let plan = super::recipe_start_tests::valid_start_plan();
        HostRuntimeRequest {
            action: HostRuntimeAction::Start,
            fence: Uuid::new_v4(),
            arguments,
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: Some(plan.run_generation),
            start_plan: Some(plan),
            stop_plan: None,
        }
    }

    fn canonical_length(request: &HostRuntimeRequest) -> usize {
        canonical_json(request)
            .expect("a host runtime request must canonically encode")
            .len()
    }

    #[test]
    fn the_declared_envelope_covers_the_largest_contract_permitted_request() {
        // Wrong implementation: `MAX_ARGV_BYTES` was set equal to the request
        // ceiling while its comment called it a backstop "below" that ceiling,
        // so an argv inside the plan's own budget could still be unframeable.
        // Re-measure the envelope rather than restating the subtraction.
        let request = HostRuntimeRequest {
            action: HostRuntimeAction::RunInspect,
            fence: Uuid::new_v4(),
            arguments: Vec::new(),
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: None,
            start_plan: None,
            stop_plan: None,
        };
        let measured = canonical_length(&request);
        assert!(
            measured <= HOST_RUNTIME_REQUEST_ENVELOPE_BYTES,
            "the measured envelope {measured} outgrew the declared \
             HOST_RUNTIME_REQUEST_ENVELOPE_BYTES {HOST_RUNTIME_REQUEST_ENVELOPE_BYTES}, \
             so the derived MAX_ARGV_BYTES no longer leaves room for the request around it"
        );
    }

    #[test]
    fn the_exchange_ceiling_is_admitted_and_one_byte_over_is_refused_with_both_numbers() {
        // Wrong implementation: the request had no byte bound at all, so the
        // helper's private `64 * 1024` read cap refused it opaquely as
        // `helper.unsafe_path` after the agent had called it valid.
        let base = start(vec!["sha256:image".to_owned()]);
        let base_length = canonical_length(&base);
        let filler = MAX_HOST_RUNTIME_REQUEST_BYTES - base_length - 3;

        let mut at_limit = base.clone();
        at_limit.arguments.push("x".repeat(filler));
        assert_eq!(canonical_length(&at_limit), MAX_HOST_RUNTIME_REQUEST_BYTES);
        assert_eq!(at_limit.validate(), Ok(()));

        let mut over_limit = base;
        over_limit.arguments.push("x".repeat(filler + 1));
        assert_eq!(
            canonical_length(&over_limit),
            MAX_HOST_RUNTIME_REQUEST_BYTES + 1
        );
        assert_eq!(
            over_limit.validate(),
            Err(HostRuntimeRequestRule::RequestBytes {
                limit: MAX_HOST_RUNTIME_REQUEST_BYTES as u64,
                observed: MAX_HOST_RUNTIME_REQUEST_BYTES as u64 + 1,
            })
        );
    }

    #[test]
    fn a_command_line_above_the_retired_count_ceiling_is_admitted() {
        // Wrong implementation: `MAX_HOST_RUNTIME_ARGUMENTS = 4096` refused a
        // legitimate command line of 6000 short elements while the canonical
        // request was tens of kilobytes inside the byte budget. The plan
        // projects two arguments per mounted file at an artifact ceiling of
        // 4096, so a count this large is admitted by the plan contract.
        let arguments = (0..6000)
            .map(|index| format!("--mount=type=bind,src={index:05}"))
            .collect();
        let request = start(arguments);
        assert!(canonical_length(&request) < MAX_HOST_RUNTIME_REQUEST_BYTES);
        assert_eq!(request.validate(), Ok(()));
    }

    #[test]
    fn an_empty_argument_and_a_multiline_argument_are_legal_bytes() {
        // Wrong implementation: an argument an exec argv can carry -- empty, or
        // one with CR/LF -- was refused for carrying it, though the plan's own
        // opaque-argv contract permits it byte for byte.
        let request = start(vec![
            "sha256:image".to_owned(),
            String::new(),
            "line one\nline two\r\n".to_owned(),
            String::new(),
        ]);
        assert_eq!(request.validate(), Ok(()));
    }
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
        if !matches!(
            self.operation.as_str(),
            "agent.upgrade.v1"
                | "runtime.preflight.v1"
                | "artifact.distribution.v1"
                | "recipe.build.v1"
                | "recipe.build.cleanup.v1"
                | "recipe.job.run.v1"
                | "recipe.install"
                | "recipe.start"
                | "recipe.stop"
                | "recipe.uninstall"
                | "recipe.reconcile"
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
        if payload.len() > maximum_bytes {
            return Err(ProtocolError::Identity("claim payload size"));
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
            || (self.memory_pool == MemoryPool::Shared && self.gpu_count == 0)
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
            || !self.network_evidence_is_consistent()
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

impl InventoryRequest {
    fn network_evidence_is_consistent(&self) -> bool {
        let interfaces = self.network_interfaces.as_deref().unwrap_or_default();
        let mut names = BTreeSet::new();
        interfaces.len() <= 16
            && interfaces.iter().all(|value| {
                valid_interface_name(&value.name)
                    && names.insert(value.name.as_str())
                    && value
                        .link_speed_mbps
                        .is_none_or(|speed| (1..=1_000_000).contains(&speed))
            })
            && self
                .nas_route_interface
                .as_deref()
                .is_none_or(|route| names.contains(route))
    }
}

/// The interface-name rule of the inventory wire contract: at most 15 ASCII
/// characters, alphanumeric first, then alphanumerics and `._:-`.
pub fn valid_interface_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 15
        && value.starts_with(|first: char| first.is_ascii_alphanumeric())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
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
        if !lower_hex(&self.plan_digest, 64) {
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
        if let Some(progress) = &self.progress {
            if canonical_json(progress)?.len() > MAX_DOCUMENT_BYTES {
                return Err(ProtocolError::Identity("progress document"));
            }
            progress.validate()?;
        }
        Ok(())
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
            || self.kind != "model"
        {
            return Err(ProtocolError::Identity("distribution object"));
        }
        Ok(())
    }
}

/// The model object set of one distribution plan and the runtime image the
/// node pulls by digest from the Controller's layered store. The plan digest
/// scopes every object fetch, so an enrolled agent cannot turn a digest into a
/// general object browser.
impl DistributionAssignment {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if !valid_oci_digest(&self.oci_image_digest)
            || !valid_oci_digest(&self.oci_image_config_digest)
            || self.objects.is_empty()
            || self.objects.len() > 4096
        {
            return Err(ProtocolError::Identity("distribution assignment"));
        }
        let mut digests = BTreeSet::new();
        for object in &self.objects {
            object.validate()?;
            if !digests.insert(object.sha256.as_str()) {
                return Err(ProtocolError::Identity("distribution object duplicate"));
            }
        }
        Ok(())
    }
}

impl AgentResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        // The state is the closed `AgentResultState`, so no other word can reach
        // here. A typed outcome decides its own state word; the legacy state field
        // must say the same thing, so the two cannot drift.
        if let Some(state) = self.result.outcome_state()
            && state != self.state
        {
            return Err(ProtocolError::Identity("result state"));
        }
        Ok(())
    }

    /// Validate the terminal result against the operation carried by its
    /// authoritative claim. The wire envelope intentionally has no duplicate
    /// operation discriminator, so callers must supply the stored operation.
    pub fn validate_for_operation(
        &self,
        operation: &generated::AgentOperation,
    ) -> Result<(), ProtocolError> {
        use generated::{AgentOperation, AgentResultResult, AgentResultState};

        self.validate()?;
        let matches = match &self.result {
            AgentResultResult::OutcomeDone(done) => {
                success_matches(operation, &AgentResultResult::from(done.result.clone()))?
            }
            AgentResultResult::OutcomeFailed(failed) => match &failed.receipt {
                None => true,
                Some(receipt) => {
                    *operation == AgentOperation::RecipeJobRunV1 && {
                        receipt.validate()?;
                        self.state == AgentResultState::Cancelled || receipt.exit_code != 0
                    }
                }
            },
            AgentResultResult::OutcomeUnknown(unknown) => match &unknown.receipt {
                None => true,
                Some(receipt) => {
                    *operation == AgentOperation::RecipeJobRunV1 && {
                        receipt.validate()?;
                        true
                    }
                }
            },
            body => match self.state {
                AgentResultState::Succeeded => success_matches(operation, body)?,
                AgentResultState::Failed => match (body, operation) {
                    (AgentResultResult::AgentFailureResult(result), _) => {
                        result.reason.is_some() || result.error_code.is_some()
                    }
                    (
                        AgentResultResult::RecipeJobRunResult(result),
                        AgentOperation::RecipeJobRunV1,
                    ) => {
                        result.validate()?;
                        result.exit_code != 0
                    }
                    _ => false,
                },
                AgentResultState::Cancelled | AgentResultState::Observing => {
                    match (body, operation) {
                        (AgentResultResult::AgentFailureResult(result), _) => {
                            result.reason.is_some() || result.error_code.is_some()
                        }
                        (
                            AgentResultResult::RecipeJobRunResult(result),
                            AgentOperation::RecipeJobRunV1,
                        ) => {
                            result.validate()?;
                            true
                        }
                        _ => false,
                    }
                }
            },
        };
        if matches {
            Ok(())
        } else {
            Err(ProtocolError::Identity("result operation"))
        }
    }
}

/// Whether a success body is the one the operation reports.
fn success_matches(
    operation: &generated::AgentOperation,
    body: &generated::AgentResultResult,
) -> Result<bool, ProtocolError> {
    use generated::{AgentOperation, AgentResultResult};

    Ok(match operation {
        AgentOperation::RuntimePreflightV1 => {
            let AgentResultResult::RuntimePreflightResult(result) = body else {
                return Err(ProtocolError::Identity("result operation"));
            };
            result.validate()?;
            true
        }
        AgentOperation::AgentUpgradeV1 => {
            matches!(body, AgentResultResult::AgentUpgradeResult(_))
        }
        AgentOperation::ArtifactDistributionV1 => {
            matches!(body, AgentResultResult::ArtifactDistributionResult(_))
        }
        // An empty success deserializes into the first empty-capable
        // variant, so it is recognized by content, not by variant.
        AgentOperation::RecipeBuildCleanupV1 => empty_result(body),
        AgentOperation::RecipeBuildV1 => {
            matches!(body, AgentResultResult::RecipeBuildEvidence(_))
        }
        AgentOperation::RecipeInstall => {
            matches!(body, AgentResultResult::AgentInstallResult(_))
        }
        // Stop, uninstall and reconcile succeed with `{}`, which an
        // untagged parse reads as the first empty variant.
        AgentOperation::RecipeStart
        | AgentOperation::RecipeStop
        | AgentOperation::RecipeUninstall
        | AgentOperation::RecipeReconcile => {
            body.is_empty_success()
                || (*operation == AgentOperation::RecipeStart
                    && matches!(body, AgentResultResult::RecipeStartResult(_)))
        }
        AgentOperation::RecipeJobRunV1 => {
            let AgentResultResult::RecipeJobRunResult(result) = body else {
                return Err(ProtocolError::Identity("result operation"));
            };
            result.validate()?;
            true
        }
    })
}

impl generated::AgentResultResult {
    /// The state word a typed outcome reports under, or `None` for a legacy body.
    pub fn outcome_state(&self) -> Option<generated::AgentResultState> {
        use generated::{AgentResultResult, AgentResultState, FailureCode};

        match self {
            AgentResultResult::OutcomeDone(_) => Some(AgentResultState::Succeeded),
            AgentResultResult::OutcomeFailed(failed) => {
                Some(if failed.code == FailureCode::OperationCancelled {
                    AgentResultState::Cancelled
                } else {
                    AgentResultState::Failed
                })
            }
            AgentResultResult::OutcomeUnknown(_) => Some(AgentResultState::Observing),
            _ => None,
        }
    }

    /// Whether this is the empty `{}` success body of a stop, uninstall,
    /// reconcile or non-serving start.
    pub fn is_empty_success(&self) -> bool {
        match self {
            Self::RecipeStartResult(result) => result.endpoint.is_none(),
            Self::RecipeStopResult(_)
            | Self::RecipeUninstallResult(_)
            | Self::RecipeReconcileResult(_) => true,
            _ => false,
        }
    }
}

impl From<generated::OutcomeDoneResult> for generated::AgentResultResult {
    fn from(value: generated::OutcomeDoneResult) -> Self {
        use generated::{AgentResultResult as To, OutcomeDoneResult as From};

        match value {
            From::RuntimePreflightResult(body) => To::RuntimePreflightResult(body),
            From::AgentInstallResult(body) => To::AgentInstallResult(body),
            From::RecipeStartResult(body) => To::RecipeStartResult(body),
            From::RecipeStopResult(body) => To::RecipeStopResult(body),
            From::RecipeReconcileResult(body) => To::RecipeReconcileResult(body),
            From::RecipeUninstallResult(body) => To::RecipeUninstallResult(body),
            From::RecipeBuildEvidence(body) => To::RecipeBuildEvidence(body),
            From::RecipeBuildCleanupEvidence(body) => To::RecipeBuildCleanupEvidence(body),
            From::RecipeJobRunResult(body) => To::RecipeJobRunResult(body),
            From::ArtifactDistributionResult(body) => To::ArtifactDistributionResult(body),
            From::AgentUpgradeResult(body) => To::AgentUpgradeResult(body),
        }
    }
}

#[cfg(test)]
mod agent_result_binding_tests {
    use super::*;
    use crate::generated::{AgentOperation, AgentResultState};

    fn result(state: AgentResultState, body: Value) -> AgentResult {
        AgentResult {
            fence: Uuid::new_v4(),
            result: serde_json::from_value(body).unwrap(),
            state,
        }
    }

    #[test]
    fn retired_unknown_result_state_is_adopted_without_being_written() {
        let old = serde_json::json!({
            "fence": "00000000-0000-4000-8000-000000000001",
            "state": "waiting-for-operator",
            "result": {"reason": "legacy receipt"}
        });
        let adopted: AgentResult = serde_json::from_value(old).unwrap();
        assert_eq!(adopted.state, AgentResultState::Observing);
        assert_eq!(serde_json::to_value(adopted).unwrap()["state"], "observing");
    }

    fn typed(state: AgentResultState, body: Value) -> AgentResult {
        result(state, body)
    }

    #[test]
    fn typed_outcome_is_bound_to_its_state_and_operation() {
        let done = typed(
            AgentResultState::Succeeded,
            serde_json::json!({"kind": "done", "result": {"installed_bytes": 3}}),
        );
        done.validate_for_operation(&AgentOperation::RecipeInstall)
            .unwrap();
        assert!(
            done.validate_for_operation(&AgentOperation::RecipeStop)
                .is_err()
        );

        let unknown = serde_json::json!({
            "kind": "unknown",
            "wait_reason": "stop-unconfirmed",
            "reason": "workload stop remains unconfirmed",
        });
        typed(AgentResultState::Observing, unknown.clone())
            .validate_for_operation(&AgentOperation::RecipeStop)
            .unwrap();
        for wrong in [
            AgentResultState::Failed,
            AgentResultState::Cancelled,
            AgentResultState::Succeeded,
        ] {
            assert!(
                typed(wrong, unknown.clone())
                    .validate_for_operation(&AgentOperation::RecipeStop)
                    .is_err()
            );
        }

        let cancelled = serde_json::json!({
            "kind": "failed",
            "code": "operation_cancelled",
            "reason": "controller cancellation confirmed after exact workload stop",
        });
        typed(AgentResultState::Cancelled, cancelled.clone())
            .validate_for_operation(&AgentOperation::RecipeStart)
            .unwrap();
        assert!(
            typed(AgentResultState::Failed, cancelled)
                .validate_for_operation(&AgentOperation::RecipeStart)
                .is_err()
        );
    }

    #[test]
    fn a_job_receipt_belongs_to_the_recipe_job_operation_only() {
        let receipt: Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
        ))
        .unwrap();
        let mut receipt = receipt["result"].clone();
        receipt["exit_code"] = serde_json::json!(1);
        let failed = typed(
            AgentResultState::Failed,
            serde_json::json!({
                "kind": "failed",
                "code": "recipe_job_run_failed",
                "reason": "runtime failed",
                "receipt": receipt,
            }),
        );
        failed
            .validate_for_operation(&AgentOperation::RecipeJobRunV1)
            .unwrap();
        assert!(
            failed
                .validate_for_operation(&AgentOperation::RecipeStop)
                .is_err()
        );
    }

    #[test]
    fn succeeded_result_is_bound_to_its_current_operation() {
        let stop = result(AgentResultState::Succeeded, serde_json::json!({}));
        stop.validate_for_operation(&AgentOperation::RecipeStop)
            .unwrap();
        assert!(
            stop.validate_for_operation(&AgentOperation::RecipeInstall)
                .is_err()
        );

        let install = result(
            AgentResultState::Succeeded,
            serde_json::json!({"installed_bytes": 0}),
        );
        install
            .validate_for_operation(&AgentOperation::RecipeInstall)
            .unwrap();
        assert!(
            install
                .validate_for_operation(&AgentOperation::RecipeStop)
                .is_err()
        );
    }

    #[test]
    fn failure_result_retains_optional_fields_but_requires_failure_identity() {
        let failure = result(
            AgentResultState::Observing,
            serde_json::json!({"reason": "operator review required"}),
        );
        failure
            .validate_for_operation(&AgentOperation::RecipeStop)
            .unwrap();

        let empty = result(AgentResultState::Failed, serde_json::json!({}));
        assert!(
            empty
                .validate_for_operation(&AgentOperation::RecipeStop)
                .is_err()
        );
        assert!(
            failure
                .validate_for_operation(&AgentOperation::RecipeJobRunV1)
                .is_ok()
        );
        assert!(
            failure
                .validate_for_operation(&AgentOperation::RecipeStop)
                .is_ok()
        );
    }

    #[test]
    fn process_result_is_valid_only_for_the_recipe_job_operation() {
        let mut job: AgentResult = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-result-v1.json"
        ))
        .unwrap();
        job.state = AgentResultState::Failed;
        let generated::AgentResultResult::RecipeJobRunResult(body) = &mut job.result else {
            panic!("expected typed job result")
        };
        body.exit_code = 1;
        body.reason = Some("runtime failed".to_owned());

        job.validate_for_operation(&AgentOperation::RecipeJobRunV1)
            .unwrap();
        assert!(
            job.validate_for_operation(&AgentOperation::RecipeStop)
                .is_err()
        );
        let generated::AgentResultResult::RecipeJobRunResult(body) = &mut job.result else {
            panic!("expected typed job result")
        };
        body.exit_code = 0;
        assert!(
            job.validate_for_operation(&AgentOperation::RecipeJobRunV1)
                .is_err()
        );
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum RecipeOperationRequest {
    RuntimePreflight(runtime_preflight::RuntimePreflightRequest),
    Build(Box<RecipeBuildRequest>),
    BuildCleanup(RecipeBuildCleanupRequest),
    JobRun(RecipeJobRunRequest),
    Install(RecipeInstallRequest),
    Start(RecipeStartRequest),
    Stop(RecipeStopRequest),
    Uninstall(RecipeUninstallRequest),
    Reconcile(RecipeReconcileRequest),
}

impl RecipeJobRunResult {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        let manifest = generated::RecipeJobOutputManifestContent {
            schema_version: self.output_manifest.schema_version,
            total_bytes: self.output_manifest.total_bytes,
            files: self.output_manifest.files.clone(),
        };
        let valid = self
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

impl RecipeReconciliationIdentity {
    pub fn validate(&self) -> Result<(), ProtocolError> {
        if valid_reconciliation_identity(self) {
            Ok(())
        } else {
            Err(ProtocolError::Identity("recipe reconciliation identity"))
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
            (
                "recipe.build.cleanup.v1",
                generated::AgentClaimPayload::RecipeBuildCleanupRequest(value),
            ) => Self::BuildCleanup(value.clone()),
            ("recipe.build.v1", generated::AgentClaimPayload::RecipeBuildRequest(value)) => {
                Self::Build(Box::new(value.clone()))
            }
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
            ("recipe.reconcile", generated::AgentClaimPayload::RecipeReconcilePayload(value)) => {
                Self::Reconcile(value.clone())
            }
            _ => return Err(ProtocolError::Identity("recipe operation payload")),
        };
        request.validate()?;
        Ok(request)
    }

    fn validate(&self) -> Result<(), ProtocolError> {
        let valid = match self {
            Self::RuntimePreflight(_) => true,
            Self::Build(value) => validate_build(value),
            Self::BuildCleanup(_) => true,
            Self::JobRun(value) => validate_recipe_job(value),
            Self::Install(value) => {
                lower_hex(&value.plan_digest, 64)
                    && value.expected_bytes <= 16 * 1024_u64.pow(4)
                    && valid_role(&value.compiled_execution_plan.runtime.placement.role)
            }
            Self::Start(value) => validate_recipe_start(value),
            Self::Stop(value) => validate_recipe_stop(value),
            Self::Uninstall(value) => {
                lower_hex(&value.plan_digest, 64)
                    && lower_hex(&value.recipe_content_sha256, 64)
                    && value
                        .cleanup_model_content_sha256
                        .as_ref()
                        .is_none_or(|digest| lower_hex(digest, 64))
            }
            Self::Reconcile(value) => {
                valid_reconciliation_identity(&RecipeReconciliationIdentity::from(value))
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
            memory_pool: MemoryPool::Separate,
            artifact_store_read_only: false,
            capabilities: vec!["recipe.build.v1".to_owned()],
            fabric_address: None,
            fabric_bandwidth_mbps: None,
            network_interfaces: None,
            nas_route_interface: None,
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

    #[test]
    fn network_evidence_must_name_its_route_interface() {
        let wifi = NetworkInterface {
            name: "wlP9s9".to_owned(),
            kind: NetworkInterfaceKind::Wifi,
            link_speed_mbps: None,
            carrier: true,
        };
        let mut value = inventory();
        value.network_interfaces = Some(vec![wifi.clone()]);
        value.nas_route_interface = Some("wlP9s9".to_owned());
        value.validate().unwrap();
        value.nas_route_interface = Some("enP7s7".to_owned());
        assert!(value.validate().is_err());
        value.nas_route_interface = None;
        value.network_interfaces = Some(vec![wifi.clone(), wifi]);
        assert!(value.validate().is_err());
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
        let mut plan: Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
        ))
        .unwrap();
        plan["runtime"]["placement"] = serde_json::json!({
            "endpoint_address": if world_size == 1 { Some("100.100.20.30") } else { None },
            "local_address": local_address,
            "master_address": master_address,
            "master_port": if world_size > 1 { Some(29500) } else { None },
            "memory_floor_bytes": 2 * 1024_u64.pow(3),
            "port": 8000,
            "rank": rank,
            "reserved_memory_bytes": 1024,
            "role": if rank == 0 { "entrypoint" } else { "worker" },
            "world_size": world_size,
        });
        let mut payload = serde_json::json!({
            "compiled_execution_plan": plan,
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "mapping_id": "00000000-0000-4000-8000-000000000002",
            "plan_digest": "b".repeat(64),
            "recipe_revision_id": "00000000-0000-4000-8000-000000000003",
            "run_generation": 1,
            "run_id": "00000000-0000-4000-8000-000000000004",
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

    fn claim(payload: Value) -> Result<AgentClaim, ProtocolError> {
        claim_for("recipe.start", payload)
    }

    fn claim_for(operation: &str, payload: Value) -> Result<AgentClaim, ProtocolError> {
        let payload: generated::AgentClaimPayload = serde_json::from_value(payload)?;
        Ok(AgentClaim {
            deadline: "2026-09-01T12:00:00+00:00".parse().unwrap(),
            fence: Uuid::parse_str("00000000-0000-4000-8000-000000000005").unwrap(),
            operation: operation.parse().unwrap(),
            payload,
        })
    }

    fn parsed_start(payload: Value) -> Result<RecipeStartRequest, ProtocolError> {
        match RecipeOperationRequest::parse(&claim(payload)?)? {
            RecipeOperationRequest::Start(request) => Ok(request),
            _ => unreachable!(),
        }
    }

    pub(super) fn valid_start_plan() -> RecipeStartRequest {
        parsed_start(start_payload(1, 0, None, None, None)).unwrap()
    }

    fn stop_payload() -> Value {
        let start = start_payload(1, 0, None, None, None);
        serde_json::json!({
            "run_id": start["run_id"],
            "target_runtime_id": start["run_id"],
            "run_generation": 1,
            "installation_id": start["installation_id"],
            "recipe_revision_id": start["recipe_revision_id"],
            "mapping_id": start["mapping_id"],
            "plan_digest": start["plan_digest"],
            "rank": start["compiled_execution_plan"]["runtime"]["placement"]["rank"],
            "role": start["compiled_execution_plan"]["runtime"]["placement"]["role"],
            "recipe_content_sha256": start["compiled_execution_plan"]["identity"]["recipe_revision_sha256"],
            "stop_timeout_seconds": start["compiled_execution_plan"]["lifecycle"]["stop_timeout_seconds"],
            "cancel_pending_start": false,
        })
    }

    fn parsed_stop(payload: Value) -> Result<RecipeStopRequest, ProtocolError> {
        let claim = claim_for("recipe.stop", payload)?;
        match RecipeOperationRequest::parse(&claim)? {
            RecipeOperationRequest::Stop(request) => Ok(request),
            _ => unreachable!(),
        }
    }

    #[test]
    fn schema_two_start_payload_allows_unphased_distributed_role_ordering() {
        let single = parsed_start(start_payload(1, 0, None, None, None)).unwrap();
        assert_eq!(single.phase, None);
        assert_eq!(single.start_deadline, None);
        assert_eq!(single.run_generation, 1);
        let unphased_wire = serde_json::to_value(single).unwrap();
        assert!(unphased_wire.get("phase").is_none());
        assert!(unphased_wire.get("start_deadline").is_none());

        let mut with_rendezvous = start_payload(1, 0, None, None, None);
        with_rendezvous["compiled_execution_plan"]["runtime"]["placement"]["master_port"] =
            Value::from(29500);
        assert!(parsed_start(with_rendezvous).is_err());

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
        assert_eq!(distributed.run_generation, 1);
    }

    #[test]
    fn typed_runtime_start_and_exact_stop_require_matching_plan_and_generation() {
        let start = valid_start_plan();
        let request = HostRuntimeRequest {
            action: HostRuntimeAction::Start,
            fence: Uuid::new_v4(),
            arguments: vec!["sha256:image".to_owned(), "run".to_owned()],
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: Some(start.run_generation),
            start_plan: Some(start.clone()),
            stop_plan: None,
        };
        assert!(request.validate().is_ok());

        let mut missing_start = request.clone();
        missing_start.start_plan = None;
        assert_eq!(
            missing_start.validate(),
            Err(HostRuntimeRequestRule::PlanBinding)
        );
        let mut mismatched_start_generation = request.clone();
        mismatched_start_generation.run_generation = Some(start.run_generation + 1);
        assert_eq!(
            mismatched_start_generation.validate(),
            Err(HostRuntimeRequestRule::PlanBinding)
        );

        let stop = parsed_stop(stop_payload()).unwrap();
        let stop_request = HostRuntimeRequest {
            action: HostRuntimeAction::Stop,
            fence: Uuid::new_v4(),
            arguments: Vec::new(),
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: Some(stop.run_generation),
            start_plan: None,
            stop_plan: Some(stop),
        };
        assert!(stop_request.validate().is_ok());
        let mut wrong_generation = stop_request.clone();
        wrong_generation.run_generation = Some(2);
        assert_eq!(
            wrong_generation.validate(),
            Err(HostRuntimeRequestRule::PlanBinding)
        );
        let mut argv_stop = stop_request;
        argv_stop.arguments.push("run-id".to_owned());
        assert_eq!(
            argv_stop.validate(),
            Err(HostRuntimeRequestRule::ArgumentsPresence)
        );
    }

    #[test]
    fn typed_job_run_start_requires_the_exact_job_plan_and_generation() {
        let claim: AgentClaim = serde_json::from_str(include_str!(
            "../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
        ))
        .unwrap();
        let generated::AgentClaimPayload::RecipeJobRunRequest(job) = &claim.payload else {
            panic!("expected canonical JobRun claim");
        };
        let request = HostRuntimeRequest {
            action: HostRuntimeAction::Start,
            fence: claim.fence,
            arguments: vec!["sha256:image".to_owned(), "job".to_owned()],
            job_plan: Some(job.clone()),
            installation_id: None,
            reconciliation_identity: None,
            run_generation: Some(job.run_generation),
            start_plan: None,
            stop_plan: None,
        };
        assert!(request.validate().is_ok());
        assert_ne!(job.run_id, job.job_id);

        let mut mismatched_generation = request.clone();
        mismatched_generation.run_generation = Some(job.run_generation + 1);
        assert_eq!(
            mismatched_generation.validate(),
            Err(HostRuntimeRequestRule::PlanBinding)
        );

        let mut duplicate_plan = request;
        duplicate_plan.start_plan = Some(valid_start_plan());
        assert_eq!(
            duplicate_plan.validate(),
            Err(HostRuntimeRequestRule::PlanBinding)
        );
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
            omitted["compiled_execution_plan"]["runtime"]["placement"][field] = Value::Null;
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
    fn formatted_start_deadlines_preserve_lexical_bytes() {
        for deadline in [
            "2026-09-01T12:00:00.123000+00:00",
            "2026-09-01T12:00:00.123Z",
        ] {
            let mut payload = start_payload(
                2,
                1,
                Some("192.168.100.3"),
                Some("192.168.100.2"),
                Some("rank-launch"),
            );
            payload["start_deadline"] = Value::String(deadline.into());
            let request = parsed_start(payload).unwrap();
            assert_eq!(request.start_deadline.as_deref(), Some(deadline));
            assert_eq!(
                serde_json::to_value(request).unwrap()["start_deadline"],
                deadline
            );
        }
    }

    #[test]
    fn authenticated_launch_claims_use_the_dedicated_document_ceiling() {
        let mut payload = start_payload(1, 0, None, None, None);
        payload["compiled_execution_plan"]["runtime"]["argv"] =
            serde_json::json!(["x".repeat(516 * 1024)]);
        assert!(claim(payload.clone()).unwrap().validate().is_ok());

        payload["compiled_execution_plan"]["runtime"]["argv"] =
            serde_json::json!(["x".repeat(MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES)]);
        assert!(claim(payload).unwrap().validate().is_err());
    }
}

#[cfg(test)]
mod recipe_install_tests {
    use super::*;

    #[test]
    fn schema_two_install_requires_the_inline_compiled_plan() {
        let payload = serde_json::json!({
            "compiled_execution_plan": serde_json::from_str::<Value>(include_str!("../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json")).unwrap(),
            "expected_bytes": 1024,
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "plan_digest": "a".repeat(64),
        });
        let payload: generated::AgentClaimPayload = serde_json::from_value(payload).unwrap();
        let claim = AgentClaim {
            deadline: "2026-09-01T12:00:00+00:00".parse().unwrap(),
            fence: Uuid::new_v4(),
            operation: "recipe.install".parse().unwrap(),
            payload,
        };
        let RecipeOperationRequest::Install(request) =
            RecipeOperationRequest::parse(&claim).expect("schema 2 install wire should parse")
        else {
            panic!("expected install request");
        };
        let _ = request;
    }
}

#[cfg(test)]
mod recipe_reconcile_tests {
    use super::*;

    #[test]
    fn reconciliation_names_one_install_and_its_plan() {
        let payload = serde_json::json!({
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "plan_digest": "b".repeat(64),
        });
        let claim = AgentClaim {
            deadline: "2026-09-01T12:00:00+00:00".parse().unwrap(),
            fence: Uuid::new_v4(),
            operation: "recipe.reconcile".parse().unwrap(),
            payload: serde_json::from_value(payload).unwrap(),
        };
        let parsed = RecipeOperationRequest::parse(&claim).unwrap();
        assert!(matches!(parsed, RecipeOperationRequest::Reconcile(_)));
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
    let manifest = generated::RecipeJobInputManifest {
        schema_version: 1,
        total_bytes: value.input_total_bytes,
        files: value.inputs.clone(),
    };
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
    let plan = &value.compiled_execution_plan;
    value.job_id.get_version() == Some(uuid::Version::Random)
        && value.run_id.get_version() == Some(uuid::Version::Random)
        && value.installation_id.get_version() == Some(uuid::Version::Random)
        && value.recipe_revision_id.get_version() == Some(uuid::Version::Random)
        && value.mapping_id.get_version() == Some(uuid::Version::Random)
        && (1..=i64::MAX as u64).contains(&value.run_generation)
        && lower_hex(&value.plan_digest, 64)
        && plan.job.is_some()
        && plan.runtime.placement.world_size == 1
        && inputs_valid
        && manifest_valid
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
}

fn validate_recipe_stop(value: &RecipeStopRequest) -> bool {
    value.run_id.get_version() == Some(uuid::Version::Random)
        && value.target_runtime_id.get_version() == Some(uuid::Version::Random)
        && (1..=i64::MAX as u64).contains(&value.run_generation)
        && value.installation_id.get_version() == Some(uuid::Version::Random)
        && value.recipe_revision_id.get_version() == Some(uuid::Version::Random)
        && value.mapping_id.get_version() == Some(uuid::Version::Random)
        && lower_hex(&value.plan_digest, 64)
        && lower_hex(&value.recipe_content_sha256, 64)
        && valid_role(&value.role)
        && (1..=600).contains(&value.stop_timeout_seconds)
}

/// A start's placement, addresses and image all come from its compiled plan;
/// the payload adds only the run identity and the distributed phase.
fn validate_recipe_start(value: &RecipeStartRequest) -> bool {
    let placement = value.placement();
    let distributed = placement.world_size > 1;
    let utc_deadline = |deadline: &str| {
        chrono::DateTime::parse_from_rfc3339(deadline)
            .is_ok_and(|deadline| deadline.offset().local_minus_utc() == 0)
    };
    let valid_phase = match (&value.phase, &value.start_deadline) {
        // Role-ordered distributed starts are deliberately unphased.
        (None, None) => true,
        (Some(RecipeStartPhase::RankLaunch), Some(deadline)) => {
            distributed && utc_deadline(deadline)
        }
        (Some(RecipeStartPhase::CollectiveReadiness), Some(deadline)) => {
            distributed && value.is_endpoint_owner() && utc_deadline(deadline)
        }
        _ => false,
    };
    let rendezvous_valid = if distributed {
        placement.local_address.is_some_and(valid_fabric_address)
            && placement.master_address.is_some_and(valid_fabric_address)
            && placement.master_port.is_some_and(|port| port >= 1024)
    } else {
        placement.rank == 0
            && placement.local_address.is_none()
            && placement.master_address.is_none()
            && placement.master_port.is_none()
    };
    (1..=i64::MAX as u64).contains(&value.run_generation)
        && value.run_id.get_version() == Some(uuid::Version::Random)
        && value.installation_id.get_version() == Some(uuid::Version::Random)
        && value.recipe_revision_id.get_version() == Some(uuid::Version::Random)
        && value.mapping_id.get_version() == Some(uuid::Version::Random)
        && lower_hex(&value.plan_digest, 64)
        && value.compiled_execution_plan.endpoint.is_some()
        && placement.rank < placement.world_size
        && valid_role(&placement.role)
        && value.port().is_some_and(|port| port >= 1024)
        && placement.reserved_memory_bytes > 0
        && value.endpoint_address().is_some_and(|address| {
            !address.is_loopback()
                && !address.is_unspecified()
                && !address.is_multicast()
                && !link_local(address)
        })
        && rendezvous_valid
        && valid_phase
}

impl RecipeJobRunRequest {
    /// The job's single-rank placement as compiled into the plan.
    pub fn placement(&self) -> &compiled_execution_plan::CompiledRuntimePlacement {
        &self.compiled_execution_plan.runtime.placement
    }
}

impl RecipeStartRequest {
    /// The rank's placement as compiled into the plan.
    pub fn placement(&self) -> &compiled_execution_plan::CompiledRuntimePlacement {
        &self.compiled_execution_plan.runtime.placement
    }

    /// The serving port of this rank.
    pub fn port(&self) -> Option<u16> {
        self.placement().port
    }

    /// The address this rank serves on: the compiled endpoint address, or the
    /// rank's fabric address for a distributed rank without one.
    pub fn endpoint_address(&self) -> Option<std::net::IpAddr> {
        let placement = self.placement();
        placement.endpoint_address.or(if placement.world_size > 1 {
            placement.local_address
        } else {
            None
        })
    }

    /// Whether this rank owns the endpoint: every single-node rank, and the
    /// distributed rank whose fabric address is the rendezvous address.
    pub fn is_endpoint_owner(&self) -> bool {
        let placement = self.placement();
        placement.world_size == 1
            || (placement.local_address.is_some()
                && placement.local_address == placement.master_address)
    }

    /// The pinned runtime image digest (`sha256:<hex>`).
    pub fn image_digest(&self) -> &str {
        &self.compiled_execution_plan.runtime_image.image_digest
    }

    /// The immutable recipe revision content digest.
    pub fn recipe_content_sha256(&self) -> &str {
        &self.compiled_execution_plan.identity.recipe_revision_sha256
    }
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
    lower_hex(&value.recipe_content_sha256, 64)
        && lower_hex(&value.source_bundle_sha256, 64)
        && lower_hex(&value.build_input_sha256, 64)
        && (1..=64 * 1024 * 1024).contains(&value.source_bundle_bytes)
        && valid_bundle_path(&value.dockerfile)
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
        // An empty host list builds offline; otherwise only these public
        // hosts are reachable.
        && value.network.hosts.len() <= 64
        && value
            .network
            .hosts
            .iter()
            .enumerate()
            .all(|(index, host)| !value.network.hosts[..index].contains(host))
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
}

pub fn parse_strict<T: DeserializeOwned>(input: &[u8]) -> Result<T, ProtocolError> {
    Ok(serde_json::from_slice(input)?)
}

pub fn canonical_json<T: Serialize>(value: &T) -> Result<Vec<u8>, ProtocolError> {
    Ok(passthrough::WireDocument::of(value)?.canonical_bytes()?)
}

pub fn hex_sha256(value: &[u8]) -> String {
    Sha256::digest(value)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn valid_node_id(value: &str) -> bool {
    value.len() == 36
        && value.starts_with("spk_")
        && value[4..]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_reconciliation_identity(value: &RecipeReconciliationIdentity) -> bool {
    !value.installation_id.is_nil() && lower_hex(&value.plan_digest, 64)
}

/// An empty success: an object without any non-null value.
fn empty_result(result: &generated::AgentResultResult) -> bool {
    passthrough::WireDocument::of(result).is_ok_and(|document| document.is_object_of_nulls())
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
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

fn valid_scalar(value: &generated::RecipeBuildEnvironmentArgumentValue) -> bool {
    match value {
        generated::RecipeBuildEnvironmentArgumentValue::Boolean(_)
        | generated::RecipeBuildEnvironmentArgumentValue::VonkInteger(_) => true,
        generated::RecipeBuildEnvironmentArgumentValue::String(value) => {
            value.len() <= 1024 && !value.contains('\0')
        }
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
            job_id: Uuid::new_v4(),
            run_id: Uuid::new_v4(),
            installation_id: Uuid::new_v4(),
            recipe_revision_id: Uuid::new_v4(),
            mapping_id: Uuid::new_v4(),
            run_generation: 1,
            plan_digest: "d".repeat(64),
            input_manifest_sha256: hex_sha256(&canonical_json(&manifest).unwrap()),
            input_total_bytes: 123,
            inputs,
            compiled_execution_plan: serde_json::from_value(serde_json::from_str::<Value>(include_str!("../../../../agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json")).unwrap()["payload"]["compiled_execution_plan"].clone()).unwrap(),
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

        let mut command = serde_json::to_value(valid).unwrap();
        command["compiled_execution_plan"] = Value::Null;
        assert!(serde_json::from_value::<RecipeJobRunRequest>(command).is_err());
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
        let generated::AgentResultResult::RecipeJobRunResult(typed) = result.result else {
            panic!("expected typed job result")
        };
        typed.validate().unwrap();
        assert_eq!(
            typed.output_manifest.manifest_sha256,
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
            fence: Uuid::new_v4(),
            result: serde_json::from_value(
                serde_json::json!({"reason": "controller cancellation requested"}),
            )
            .unwrap(),
            state: "cancelled".parse().unwrap(),
        };

        result.validate().unwrap();
    }
}

#[cfg(test)]
mod host_helper_reconciliation_identity_tests {
    use super::*;

    fn identity() -> RecipeReconciliationIdentity {
        RecipeReconciliationIdentity {
            installation_id: Uuid::new_v4(),
            plan_digest: "b".repeat(64),
        }
    }

    fn claims(identity: RecipeReconciliationIdentity) -> HostHelperGrantClaims {
        HostHelperGrantClaims {
            schema_version: 1,
            authority: HOST_HELPER_AUTHORITY.to_owned(),
            request_id: Uuid::new_v4(),
            node_id: "spk_11111111111111111111111111111111".to_owned(),
            issued_at: 2_100_000_000,
            expires_at: 2_100_000_060,
            operation: HostHelperOperation::ExecuteContainerRuntimeRequestOperation(
                generated::ExecuteContainerRuntimeRequestOperation {
                    type_: "execute-container-runtime-request".into(),
                    action: HostHelperContainerRuntimeAction::InstallationCleanup,
                    fence: Uuid::new_v4(),
                    request_sha256: "e".repeat(64),
                    installation_id: Some(identity.installation_id),
                    reconciliation_identity: Some(identity),
                    run_generation: None,
                    runtime_installation_id: None,
                    runtime_run_id: None,
                    runtime_target_id: None,
                    start_plan_sha256: None,
                    stop_plan_sha256: None,
                },
            ),
        }
    }

    #[test]
    fn signed_cleanup_grant_identity_is_strictly_bound_to_action_and_install() {
        let exact = claims(identity());
        exact.validate().unwrap();

        let mut wrong_installation = exact.clone();
        let HostHelperOperation::ExecuteContainerRuntimeRequestOperation(operation) =
            &mut wrong_installation.operation
        else {
            unreachable!();
        };
        operation.installation_id = Some(Uuid::from_u128(8));
        assert!(wrong_installation.validate().is_err());

        let mut wrong_action = exact;
        let HostHelperOperation::ExecuteContainerRuntimeRequestOperation(operation) =
            &mut wrong_action.operation
        else {
            unreachable!();
        };
        operation.action = HostHelperContainerRuntimeAction::Start;
        operation.installation_id = None;
        assert!(wrong_action.validate().is_err());
    }
}

#[cfg(test)]
mod distribution_tests {
    use super::*;

    fn assignment() -> DistributionAssignment {
        DistributionAssignment {
            objects: vec![DistributionObject {
                name: "weights/model.bin".to_owned(),
                sha256: "d".repeat(64),
                bytes: 13,
                kind: "model".parse().unwrap(),
            }],
            oci_image_digest: "sha256:".to_owned() + &"f".repeat(64),
            oci_image_config_digest: "sha256:".to_owned() + &"e".repeat(64),
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
            kind: "model".parse().unwrap(),
        };
        value.validate().unwrap();
        value.objects[0].kind = "oci-archive".parse().unwrap();
        assert!(value.validate().is_err());
    }
}

/// Validate an outgoing generated document before producing its canonical bytes.
/// This also covers direct Rust construction, which does not invoke Deserialize.
pub fn canonical_generated_json<T: Serialize + DeserializeOwned>(
    document: &T,
) -> Result<Vec<u8>, ProtocolError> {
    canonical_json(&revalidate(document)?)
}
