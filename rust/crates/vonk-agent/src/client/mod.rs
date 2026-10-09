//! Controller HTTP transport and artifact transfer.

mod buffering;
mod distribution;
mod errors;
mod grants;
mod jobs;
mod operations;
mod progress;
mod reporting;
mod response;
mod storage;
mod transfer;
mod transport;

mod renewal;

#[cfg(test)]
use renewal::expired_renewal_request;

use std::{
    fmt, fs,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};

use futures_util::{StreamExt, TryStreamExt, stream};
use reqwest::{Certificate, Client, Identity, StatusCode};
use thiserror::Error;
use tokio::io::{AsyncWriteExt, BufWriter};
use tokio_util::io::ReaderStream;
use url::Url;
use vonk_agent_protocol::generated::{
    ActivateRequest, AgentEvidenceCode, AgentUpgradeGrantRequest, BoundedErrorResponse,
    ClaimRequest, ControllerErrorCode, HostHelperGrantResponse, HostRuntimeGrantRequest,
    HostRuntimeGrantRequestAction, PackageActivationGrantRequest, ProgressPhase,
    RequestValidationIssueLocItem, RequestValidationProblem, SecurityRefusalReason,
    TelemetryRequest,
};
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, DistributionAssignment,
    HostRuntimeAction, HostRuntimeRequest, InventoryRequest,
    MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES, RecipeRunObservationWire, RecipeRunObservationsWire,
    SignedHostHelperGrant, canonical_generated_json, canonical_json, hex_sha256, parse_strict,
};

use crate::stream_governor::{self, StreamGovernor};
use crate::{
    config::AgentConfig,
    failure_evidence::sanitize_text,
    identity::{IdentityPaths, active_identity_paths},
    inventory::Inventory,
    oci::MAX_RECIPE_RUN_OBSERVATIONS_PER_BATCH,
    pair::verify_ca_pin,
    runtime_identity::AgentRuntimeIdentity,
    telemetry::{TelemetrySample, valid_report_batch},
};

use tokio::sync::{RwLock, RwLockReadGuard};

const MAX_BODY_BYTES: usize = 64 * 1024;
const MAX_CLAIM_BODY_BYTES: usize = MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES;
/// Longest Controller validation digest retained in one refusal record.
const MAX_REJECTION_CONTEXT_CHARS: usize = 256;
/// Validation issues kept from one refusal.  A union payload fails every
/// branch, so the Controller can report a hundred issues for one bad field;
/// the record keeps a bounded, ranked selection instead of all of them.
const MAX_REJECTION_ISSUES: usize = 4;
/// Longest single location segment kept from a reported issue.
const MAX_REJECTION_LOCATION_CHARS: usize = 48;
/// Pydantic reports these for every union branch that did not match.  They
/// describe the payload's shape rather than the constraint its producer
/// broke, so they rank behind a real constraint failure.
const STRUCTURAL_ERROR_TYPES: [&str; 2] = ["missing", "extra_forbidden"];
const CONTROLLER_REQUEST_TIMEOUT: Duration = Duration::from_secs(75);
const RECIPE_IMAGE_UPLOAD_TIMEOUT: Duration = Duration::from_secs(60 * 60);
// Renewal has to finish before the active certificate expires. Keep each
// controller call bounded so a stalled endpoint cannot consume the entire
// remaining validity window of the 90-second acceptance certificate.
const ROTATION_REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
const HOST_RUNTIME_GRANT_TTL_SECONDS: u16 = 10;
/// Range requests in flight are bounded by an adaptive governor: it starts at
/// what a lone transfer is known to sustain and only adds streams while the
/// aggregate throughput still grows. Objects open at once are the most it can
/// ever allow, so a waiting object holds nothing but its resumable partial.
const DISTRIBUTION_CONCURRENCY: usize = stream_governor::MAX_STREAMS;
/// Bytes requested per HTTP range. Large enough that authorization and
/// request latency vanish next to the transfer; the writer resumes from the
/// bytes it accepted, so a failed range repeats nothing it already wrote.
const DISTRIBUTION_RANGE_BYTES: u64 = 64 * 1024 * 1024;
/// Written bytes after which their writeback starts and their pages are
/// released, so a hundreds-of-gigabytes model never builds a huge dirty backlog
/// or fills the page cache of a shared-memory machine.
const WRITE_BEHIND_BYTES: u64 = 256 * 1024 * 1024;

/// Longest a single heartbeat request may spend before it is treated as lost.
/// The owning observation deadline bounds retries after a lost response.
pub(crate) const HEARTBEAT_REQUEST_TIMEOUT: Duration = Duration::from_secs(15);

/// The one Controller<->agent protocol version this agent speaks.
pub const AGENT_PROTOCOL_VERSION: u32 = 4;

/// Serialize the current claim contract used by the HTTP transport.
pub fn claim_request_document(
    preflight_fingerprint: Option<&str>,
    hostname: Option<&str>,
    wait_seconds: u64,
    runtime_identity: &AgentRuntimeIdentity,
) -> Result<Vec<u8>, ClientError> {
    canonical_generated_json(&ClaimRequest {
        hostname: hostname.map(str::to_owned),
        preflight_fingerprint: preflight_fingerprint.map(str::to_owned),
        protocol_version: AGENT_PROTOCOL_VERSION,
        runtime_identity: runtime_identity.clone(),
        wait_seconds: u32::try_from(wait_seconds.min(60)).expect("bounded claim wait"),
    })
    .map_err(|_| ClientError::Protocol)
}

pub type ExactRecipeRunObservation = RecipeRunObservationWire;

/// Validate and construct the one current observation report used by both
/// the production executor and the Linux wire probe.
pub fn build_exact_recipe_run_observations(
    observed_at: chrono::DateTime<chrono::Utc>,
    observations: &[ExactRecipeRunObservation],
) -> Result<RecipeRunObservationsWire, ClientError> {
    if observations.len() > MAX_RECIPE_RUN_OBSERVATIONS_PER_BATCH {
        return Err(ClientError::Protocol);
    }
    let mut run_ids = std::collections::BTreeSet::new();
    for observation in observations {
        if observation.run_generation == 0
            || observation.run_generation > i64::MAX as u64
            || !run_ids.insert(observation.run_id)
        {
            return Err(ClientError::Protocol);
        }
    }
    Ok(RecipeRunObservationsWire {
        observed_at: observed_at.into(),
        runs: observations.to_vec(),
    })
}

/// The Controller's answer for one locally retained run, by id alone.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecipeRunDisposition {
    /// The Controller has a record of the run; its integrity checks apply.
    /// `run_generation` is its accepted generation while the run is running.
    Known { run_generation: Option<u64> },
    /// The Controller has no record of the run and never will accept it.
    Unowned,
}

/// Response header naming a run the Controller has no record of.
pub const RECIPE_RUN_DISPOSITION_HEADER: &str = "x-vonk-recipe-run-disposition";
/// Response header carrying the accepted generation of a known running run.
pub const RECIPE_RUN_GENERATION_HEADER: &str = "x-vonk-recipe-run-generation";
/// The only disposition value: the Controller never owned this run.
pub const RECIPE_RUN_UNOWNED: &str =
    vonk_agent_protocol::generated::RecipeRunDispositionValue::Unowned.as_str();

#[derive(Clone)]
pub struct AgentHttpClient {
    client: Arc<RwLock<Client>>,
    controller: Url,
    node_id: String,
    progress_phase: Arc<Mutex<Option<ProgressSnapshot>>>,
}

use reporting::*;
pub(crate) use response::classify_response;
pub use response::parse_claim_response;
use response::*;
use storage::*;

#[cfg(test)]
mod test_support;

use buffering::*;
pub use errors::{ClientError, ControllerError};
pub use progress::{DistributionDownloadEvidence, DistributionProgress};
use progress::{DistributionProgressTracker, ProgressSnapshot};
