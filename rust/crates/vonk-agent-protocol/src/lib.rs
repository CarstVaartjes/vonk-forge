#![forbid(unsafe_code)]
#[rustfmt::skip]
pub mod compiled_execution_plan;
pub mod compiled_oci;
pub mod generated {
    include!(concat!(env!("OUT_DIR"), "/generated.rs"));
}
/// The build-generated wire code and schema, for tests that inspect them.
#[doc(hidden)]
pub const GENERATED_SOURCE: &str = include_str!(concat!(env!("OUT_DIR"), "/generated.rs"));
#[doc(hidden)]
pub const WIRE_SCHEMA: &str = include_str!(concat!(env!("OUT_DIR"), "/wire.json"));
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
pub mod host_memory_guard_policy;

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

mod build_validation;
mod canonical;
mod host_runtime;
mod recipe_requests;
mod recipe_validation;
mod results;
mod validation;

use build_validation::{link_local, valid_build_options, valid_fabric_address, valid_public_host};
pub use canonical::{canonical_generated_json, canonical_json, hex_sha256, parse_strict};
use canonical::{
    empty_result, lower_hex, valid_build_environment_name, valid_build_metadata_name,
    valid_bundle_path, valid_name, valid_node_id, valid_oci_digest, valid_pinned_image,
    valid_reconciliation_identity, valid_role, valid_scalar,
};
pub use host_runtime::{
    HOST_HELPER_AUTHORITY, HOST_RUNTIME_REQUEST_ENVELOPE_BYTES, HostRuntimeRequestRule,
    MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES, MAX_COMPILED_EXECUTION_PLAN_DOCUMENT_BYTES,
    MAX_DOCUMENT_BYTES, MAX_HELPER_FRAME_BYTES, MAX_HOST_RUNTIME_REQUEST_BYTES,
    host_helper_grant_signing_bytes,
};

pub use recipe_requests::RecipeOperationRequest;
use recipe_validation::{
    valid_job_file_name, valid_media_type, validate_build, validate_recipe_job,
    validate_recipe_start, validate_recipe_stop,
};

pub use validation::{ProtocolError, valid_interface_name};

#[cfg(test)]
use recipe_requests::recipe_start_tests;
