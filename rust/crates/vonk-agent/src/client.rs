use std::{
    collections::HashMap,
    fmt, fs,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};

use futures_util::{StreamExt, TryStreamExt, stream};
use reqwest::{Certificate, Client, Identity, StatusCode};
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt, BufWriter};
use tokio_util::io::ReaderStream;
use url::Url;
use vonk_agent_protocol::generated::{
    ActivateRequest, AgentUpgradeGrantRequest, ClaimRequest, HostHelperGrantResponse,
    HostRuntimeGrantRequest, HostRuntimeGrantRequestAction, IssuedCertificateResponse,
    PackageActivationGrantRequest, RecipeRunObservationGrantRequest, RecipeRunObservationGrantWire,
    RenewRequest, TelemetryRequest,
};
use vonk_agent_protocol::{
    AgentClaim, AgentDirective, AgentProgress, AgentResult, DistributionAssignment,
    HostHelperContainerRuntimeAction, HostHelperOperation, HostRuntimeAction, HostRuntimeRequest,
    InventoryRequest, MAX_COMPILED_EXECUTION_PLAN_CLAIM_BYTES,
    RECIPE_RUN_OBSERVATION_SCHEMA_VERSION, RecipeRunInspectionBinding, RecipeRunObservationWire,
    RecipeRunObservationsWire, SignedHostHelperGrant, canonical_generated_json, canonical_json,
    hex_sha256, parse_strict,
};

use crate::{
    config::AgentConfig,
    failure_evidence::sanitize_text,
    identity::{IdentityPaths, active_identity_paths},
    inventory::Inventory,
    oci::MAX_MANAGED_RECIPE_RUNS,
    pair::verify_ca_pin,
    runtime_identity::AgentRuntimeIdentity,
    telemetry::{TelemetrySample, valid_report_batch},
    workloads::CompiledExecutionPlan,
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
const RECIPE_IMAGE_UPLOAD_TIMEOUT: Duration = Duration::from_secs(60 * 60);
// Renewal has to finish before the active certificate expires. Keep each
// controller call bounded so a stalled endpoint cannot consume the entire
// remaining validity window of the 90-second acceptance certificate.
const ROTATION_REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
const HOST_RUNTIME_GRANT_TTL_SECONDS: u16 = 10;
const DISTRIBUTION_CONCURRENCY: usize = 16;
#[cfg(test)]
thread_local! {
    static DISTRIBUTION_HASH_READ_BYTES: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

#[cfg(test)]
fn distribution_hash_read_bytes() -> u64 {
    DISTRIBUTION_HASH_READ_BYTES.with(std::cell::Cell::get)
}

/// Release the page cache for a completed distribution object.
///
/// Best effort on purpose: the object is transferred, verified and receipted,
/// and a failed hint must not turn that into a failed transfer.  A node that
/// keeps the pages is the behaviour that shipped before this call.
fn release_distribution_page_cache(path: &Path) {
    if let Ok(file) = std::fs::File::open(path) {
        let _ = rustix::fs::fadvise(&file, 0, None, rustix::fs::Advice::DontNeed);
    }
}
/// Longest a single heartbeat request may spend before it is treated as lost.
/// The renewal loop reserves lease margin against the same budget, so one
/// slow or dropped request cannot consume the whole accepted lease.
pub(crate) const HEARTBEAT_REQUEST_TIMEOUT: Duration = Duration::from_secs(15);
/// Part of the accepted lease the renewal loop keeps in hand to schedule the
/// next authorised attempt rather than discovering the expiry while sending.
pub(crate) const HEARTBEAT_LEASE_MARGIN: Duration = Duration::from_secs(1);

#[derive(Debug, Error)]
pub enum ClientError {
    #[error("agent credential could not be read")]
    CredentialRead(#[from] std::io::Error),
    #[error("agent TLS identity is invalid")]
    Identity,
    #[error("controller transport failed")]
    Transport(#[from] reqwest::Error),
    #[error("controller rejected: {0}")]
    Controller(Box<ControllerError>),
    #[error("controller temporarily rejected the request")]
    Retryable,
    #[error("controller protocol response is invalid")]
    Protocol,
    // The Controller did not accept this result: the attempt is no longer
    // current, or its outcome was already consumed.  It is deliberately
    // distinct from an accepted result and from a transport failure, because
    // the agent must keep evidence the Controller never confirmed rather than
    // treat the refusal as an acknowledgement.
    #[error("controller did not accept the result for this attempt")]
    ResultSuperseded,
    // The Controller refused this exact result at its ingress validation
    // boundary (HTTP 422).  Re-sending the same bytes cannot succeed, but the
    // refusal is about this operation's evidence, not about the agent's
    // identity or transport, so the caller records the bounded refusal durably
    // and keeps the rest of the loop alive instead of exiting.
    #[error("controller refused the result as invalid")]
    ResultRejected(Box<ControllerError>),
    #[error("exact recipe run observation is not ready for authorization")]
    ObservationNotReady,
    #[error("controller CA pin is invalid")]
    Pin,
}

#[derive(Debug)]
pub struct ControllerError {
    pub operation: String,
    pub endpoint: String,
    pub status: u16,
    pub code: String,
    pub request_id: Option<String>,
    pub decision: &'static str,
    pub retry_after_seconds: Option<u32>,
    /// Bounded, sanitized Controller validation context, when the endpoint
    /// published one.  Never raw request bytes and never the rejected values.
    pub summary: Option<String>,
}

impl fmt::Display for ControllerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "{} {} HTTP {} [{}]",
            self.operation, self.code, self.status, self.decision
        )?;
        if let Some(request_id) = &self.request_id {
            write!(formatter, " request_id={request_id}")?;
        }
        Ok(())
    }
}

impl std::error::Error for ControllerError {}

impl ControllerError {
    pub fn from_status(status: u16) -> Self {
        let status = StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        controller_error(status, "/", "controller.request", None, None)
    }

    /// Return the bounded context to persist for a refused submission.
    ///
    /// The Controller's own validation digest names the boundary and the
    /// failing field and rule, which is what an operator needs; the status,
    /// code and request id already have their own columns and log fields, so
    /// they are only the fallback when no digest was published.  Either way the
    /// result stays bounded and free of credentials and rejected values.
    pub fn rejection_context(&self) -> String {
        let context = self.summary.clone().unwrap_or_else(|| self.to_string());
        context.chars().take(MAX_REJECTION_CONTEXT_CHARS).collect()
    }

    /// Whether this refusal is the Controller asking for the same request again.
    ///
    /// One owner: the renewal loop classifies a failed heartbeat against this
    /// exact set rather than restating the statuses, so a change here cannot
    /// leave the two disagreeing.
    pub(crate) fn retryable(&self) -> bool {
        matches!(self.status, 408 | 429 | 500..=599)
    }
}

impl ClientError {
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Transport(_) | Self::Retryable)
            || matches!(self, Self::Controller(error) if error.retryable())
    }

    pub fn retry_after_seconds(&self) -> Option<u32> {
        match self {
            Self::Controller(error) => error.retry_after_seconds,
            _ => None,
        }
    }

    pub fn status(&self) -> Option<u16> {
        match self {
            Self::Controller(error) | Self::ResultRejected(error) => Some(error.status),
            _ => None,
        }
    }

    pub fn code(&self) -> Option<&str> {
        match self {
            Self::Controller(error) | Self::ResultRejected(error) => Some(&error.code),
            _ => None,
        }
    }

    pub fn request_id(&self) -> Option<&str> {
        match self {
            Self::Controller(error) | Self::ResultRejected(error) => error.request_id.as_deref(),
            _ => None,
        }
    }

    /// Return the safe transport boundary when reqwest reliably identified it.
    /// A broad `connect` label is preferred to guessing DNS, TCP, or TLS.
    pub fn transport_kind(&self) -> Option<&'static str> {
        match self {
            Self::Transport(error) if error.is_timeout() => Some("timeout"),
            Self::Transport(error) if error.is_connect() => Some("connect"),
            Self::Transport(error) if error.is_body() => Some("body"),
            Self::Transport(error) if error.is_decode() => Some("protocol"),
            Self::Transport(_) => Some("unknown"),
            _ => None,
        }
    }

    /// Return only the URL path captured by reqwest; queries and credentials
    /// are intentionally unavailable to callers of the diagnostic surface.
    pub fn endpoint(&self) -> Option<&str> {
        match self {
            Self::Controller(error) | Self::ResultRejected(error) => Some(&error.endpoint),
            _ => None,
        }
    }

    pub fn transport_endpoint(&self) -> Option<String> {
        match self {
            Self::Transport(error) => error.url().map(|url| url.path().to_owned()),
            _ => None,
        }
    }

    pub fn decision(&self) -> &'static str {
        if self.retryable() {
            "retry"
        } else if matches!(self, Self::ResultRejected(_)) {
            "record"
        } else {
            "exit"
        }
    }
}

/// Serialize the current claim contract used by the HTTP transport.
pub fn claim_request_document(
    node_id: &str,
    capabilities: &[&str],
    hostname: Option<&str>,
    wait_seconds: u64,
    runtime_identity: &AgentRuntimeIdentity,
) -> Result<Vec<u8>, ClientError> {
    if !runtime_identity.self_test_passed {
        return Err(ClientError::Protocol);
    }
    runtime_identity
        .observation_receipt_public_key()
        .map_err(|_| ClientError::Protocol)?;
    canonical_generated_json(&ClaimRequest {
        capabilities: capabilities
            .iter()
            .map(|value| (*value).to_owned())
            .collect(),
        hostname: hostname.map(str::to_owned),
        lease_seconds: 60,
        node_id: node_id.to_owned(),
        protocol_version: 3,
        runtime_identity: runtime_identity.clone(),
        wait_seconds: u32::try_from(wait_seconds.min(60)).expect("bounded claim wait"),
    })
    .map_err(|_| ClientError::Protocol)
}

pub type ExactRecipeRunObservation = RecipeRunObservationWire;

/// Validate and construct the one current snapshot envelope used by both the
/// production executor and the Linux wire probe.
pub fn build_exact_recipe_run_observations(
    node_id: &str,
    observed_at: chrono::DateTime<chrono::Utc>,
    observations: &[ExactRecipeRunObservation],
) -> Result<RecipeRunObservationsWire, ClientError> {
    if observations.len() > MAX_MANAGED_RECIPE_RUNS {
        return Err(ClientError::Protocol);
    }
    if !valid_node_id(node_id) {
        return Err(ClientError::Protocol);
    }
    let mut run_ids = std::collections::BTreeSet::new();
    for observation in observations {
        observation.validate().map_err(|_| ClientError::Protocol)?;
        if observation.node_id != node_id || !run_ids.insert(observation.run_id) {
            return Err(ClientError::Protocol);
        }
        let receipt_request_id = observation.helper_receipt.claims.request_id.to_string();
        let (grant_job_id, grant_request_sha256) = match &observation.grant.claims.operation {
            HostHelperOperation::ExecuteContainerRuntimeRequestOperation(operation) => {
                (&operation.job_id, &operation.request_sha256)
            }
            _ => return Err(ClientError::Protocol),
        };
        if observation.schema_version != 1
            || !valid_sha256(&observation.observation_identity_sha256)
            || observation.helper_receipt.validate().is_err()
            || observation.helper_receipt.claims.node_id != node_id
            || observation
                .helper_receipt
                .claims
                .observation_identity_sha256
                != observation.observation_identity_sha256
            || hex::decode(&observation.observation_receipt_public_key)
                .ok()
                .filter(|key| key.len() == 32)
                .map(|key| hex_sha256(&key))
                != Some(observation.helper_receipt.signature.key_id.clone())
            || *grant_job_id != observation.run_id
            || grant_request_sha256 != &observation.helper_receipt.claims.request_sha256
            || observation.grant.claims.request_id.to_string() != receipt_request_id
            || observation.observed_at.timestamp() != observation.helper_receipt.claims.observed_at
            || (observation.local_address == observation.master_address)
                != observation.endpoint_ready.is_some()
        {
            return Err(ClientError::Protocol);
        }
    }
    Ok(RecipeRunObservationsWire {
        schema_version: RECIPE_RUN_OBSERVATION_SCHEMA_VERSION,
        observed_at: observed_at.into(),
        runs: observations.to_vec(),
    })
}

#[derive(Debug)]
pub struct RecipeRunInspectionGrant {
    pub grant: SignedHostHelperGrant,
    pub observation_identity_sha256: String,
}

#[derive(Debug, Clone)]
pub struct DistributionDownloadEvidence {
    pub assignment_id: uuid::Uuid,
    pub model_artifact_set_sha256: String,
    pub model_digests: Vec<String>,
    pub model_paths: Vec<std::path::PathBuf>,
    pub oci_archive_path: std::path::PathBuf,
    pub oci_archive_sha256: String,
    pub oci_archive_bytes: u64,
    pub oci_image_digest: String,
    pub downloaded_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DistributionProgress {
    pub phase: &'static str,
    pub completed_items: u64,
    pub total_items: u64,
    pub object_sha256: String,
    pub kind: String,
    pub bytes: u64,
    pub total_bytes: Option<u64>,
}

struct DistributionProgressTracker<F> {
    object_bytes: Vec<u64>,
    bytes: u64,
    completed_items: u64,
    total_bytes: u64,
    callback: F,
}

impl<F: FnMut(DistributionProgress)> DistributionProgressTracker<F> {
    fn report(
        &mut self,
        index: usize,
        object: &vonk_agent_protocol::DistributionObject,
        bytes: u64,
        phase: &'static str,
        completed: bool,
    ) {
        // Retried ranges and verification can report the same offset again.
        // Count each object's bytes once, regardless of completion order.
        self.bytes += bytes.saturating_sub(self.object_bytes[index]);
        self.object_bytes[index] = self.object_bytes[index].max(bytes);
        self.completed_items += u64::from(completed);
        (self.callback)(DistributionProgress {
            phase,
            completed_items: self.completed_items,
            total_items: self.object_bytes.len() as u64,
            object_sha256: object.sha256.clone(),
            kind: object.kind.to_string(),
            bytes: self.bytes,
            total_bytes: Some(self.total_bytes),
        });
    }
}

struct ProgressSnapshot {
    operation_id: uuid::Uuid,
    phase: String,
    counters: Option<(u64, u64)>,
}

#[derive(Clone)]
pub struct AgentHttpClient {
    client: Arc<RwLock<Client>>,
    controller: Url,
    node_id: String,
    progress_phase: Arc<Mutex<Option<ProgressSnapshot>>>,
    // This process alone can vouch for a completed private object. A restart
    // deliberately loses the receipt and rehashes any preexisting final file.
    distribution_receipts: Arc<Mutex<HashMap<PathBuf, DistributionObjectReceipt>>>,
}

#[derive(Clone)]
struct DistributionObjectReceipt {
    digest: String,
    size: u64,
    dev: u64,
    ino: u64,
    mtime: (i64, i64),
    ctime: (i64, i64),
}

impl DistributionObjectReceipt {
    fn new(digest: &str, metadata: &fs::Metadata) -> Self {
        Self {
            digest: digest.to_owned(),
            size: metadata.len(),
            dev: metadata.dev(),
            ino: metadata.ino(),
            mtime: (metadata.mtime(), metadata.mtime_nsec()),
            ctime: (metadata.ctime(), metadata.ctime_nsec()),
        }
    }

    fn matches(&self, digest: &str, metadata: &fs::Metadata) -> bool {
        self.digest == digest
            && self.size == metadata.len()
            && self.dev == metadata.dev()
            && self.ino == metadata.ino()
            && self.mtime == (metadata.mtime(), metadata.mtime_nsec())
            && self.ctime == (metadata.ctime(), metadata.ctime_nsec())
    }
}

impl AgentHttpClient {
    #[cfg(test)]
    pub(crate) fn for_http_test(controller: &str, node_id: &str) -> Self {
        Self {
            client: Arc::new(RwLock::new(reqwest::Client::new())),
            controller: Url::parse(controller).expect("test controller URL must be valid"),
            node_id: node_id.to_owned(),
            progress_phase: Arc::new(Mutex::new(None)),
            distribution_receipts: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub(crate) fn node_id(&self) -> &str {
        &self.node_id
    }

    pub fn from_config(config: &AgentConfig) -> Result<Self, ClientError> {
        let paths = active_identity_paths(&config.data_dir.join("credentials"))
            .map_err(|_| ClientError::Identity)?;
        Self::from_identity_paths(config, &paths)
    }

    pub fn from_identity_paths(
        config: &AgentConfig,
        paths: &IdentityPaths,
    ) -> Result<Self, ClientError> {
        let client = Self::build_client(config, paths)?;
        Ok(Self {
            client: Arc::new(RwLock::new(client)),
            controller: config.controller_url.clone(),
            node_id: config.node_id.clone(),
            progress_phase: Arc::new(Mutex::new(None)),
            distribution_receipts: Arc::new(Mutex::new(HashMap::new())),
        })
    }

    fn build_client(config: &AgentConfig, paths: &IdentityPaths) -> Result<Client, ClientError> {
        let ca_pem = fs::read(&config.ca_path)?;
        verify_ca_pin(&ca_pem, &config.ca_sha256).map_err(|_| ClientError::Pin)?;
        let mut identity_pem = fs::read(&paths.certificate)?;
        identity_pem.extend_from_slice(&fs::read(&paths.chain)?);
        identity_pem.extend_from_slice(&fs::read(&paths.private_key)?);
        let identity = Identity::from_pem(&identity_pem).map_err(|_| ClientError::Identity)?;
        let ca = Certificate::from_pem(&ca_pem).map_err(|_| ClientError::Identity)?;
        let client = Client::builder()
            .https_only(true)
            .tls_built_in_root_certs(false)
            .add_root_certificate(ca)
            .identity(identity)
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(75))
            .build()?;
        Ok(client)
    }

    pub(crate) async fn activate_replacement(
        &self,
        replacement: &Self,
        generation: u64,
    ) -> Result<(), ClientError> {
        if self.controller != replacement.controller
            || self.node_id != replacement.node_id
            || Arc::ptr_eq(&self.client, &replacement.client)
        {
            return Err(ClientError::Protocol);
        }
        // Drain requests using the old identity before the Controller revokes
        // it, then replace the shared transport before admitting another one.
        // A PUT can take an hour. Periodically release our place in the writer
        // queue so heartbeats can renew that operation's lease while it drains.
        let mut transport = loop {
            if let Ok(guard) =
                tokio::time::timeout(Duration::from_millis(100), self.client.write()).await
            {
                break guard;
            }
            tokio::task::yield_now().await;
        };
        replacement.activate(generation).await?;
        *transport = replacement.current_client().await.clone();
        Ok(())
    }

    async fn current_client(&self) -> RwLockReadGuard<'_, Client> {
        // Each request expression retains this guard through send().await.
        // Streaming response bodies may continue after their authenticated
        // headers arrive; uploads retain it until their response arrives.
        self.client.read().await
    }

    pub async fn claim(
        &self,
        capabilities: &[&str],
        wait_seconds: u64,
        runtime_identity: Option<&AgentRuntimeIdentity>,
    ) -> Result<Option<AgentClaim>, ClientError> {
        let hostname = local_hostname();
        let body = claim_request_document(
            &self.node_id,
            capabilities,
            hostname.as_deref(),
            wait_seconds,
            runtime_identity.ok_or(ClientError::Protocol)?,
        )?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/claim")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        let status = response.status();
        if status == StatusCode::NO_CONTENT {
            return Ok(None);
        }
        classify_response(&response)?;
        let body = bounded_claim_body(response).await?;
        parse_claim_response(status.as_u16(), &body)
    }

    pub async fn submit_result(&self, result: &AgentResult) -> Result<(), ClientError> {
        result.validate().map_err(|_| ClientError::Protocol)?;
        let body = canonical_json(result).map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/result")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        match response.status() {
            StatusCode::NO_CONTENT | StatusCode::ACCEPTED => Ok(()),
            // A 409 fences this result: the attempt is no longer current, or
            // its outcome was already consumed.  Either way the Controller did
            // not acknowledge this submission, so the caller keeps the evidence
            // instead of deleting it on the strength of a refusal.
            StatusCode::CONFLICT => Err(ClientError::ResultSuperseded),
            // A 422 refuses these exact bytes at the Controller's ingress
            // validation boundary.  The caller records the bounded refusal and
            // keeps the loop alive; it never treats the refusal as acceptance.
            // This is the one boundary that reads the error body: the
            // Controller publishes a bounded, redacted validation problem
            // there, and a refusal recorded without it cannot say which field
            // or rule rejected the result.  Anything absent, oversized or not
            // of the declared shape leaves the digest unset rather than
            // guessing at it.
            StatusCode::UNPROCESSABLE_ENTITY => {
                let mut error = response_controller_error(&response);
                error.summary = bounded_body(response)
                    .await
                    .ok()
                    .and_then(|body| controller_rejection_digest(&body));
                Err(ClientError::ResultRejected(Box::new(error)))
            }
            _ => {
                classify_response(&response)?;
                Err(ClientError::Protocol)
            }
        }
    }

    pub(crate) fn set_progress_phase(&self, operation_id: uuid::Uuid, phase: &str) {
        *self
            .progress_phase
            .lock()
            .expect("progress phase lock poisoned") = Some(ProgressSnapshot {
            operation_id,
            phase: phase.to_owned(),
            counters: None,
        });
    }

    pub(crate) fn set_progress_bytes(&self, operation_id: uuid::Uuid, bytes: u64, total: u64) {
        if let Some(snapshot) = self
            .progress_phase
            .lock()
            .expect("progress phase lock poisoned")
            .as_mut()
            && snapshot.operation_id == operation_id
        {
            let high_water = snapshot
                .counters
                .map(|(current, _)| current)
                .unwrap_or(0)
                .max(bytes);
            snapshot.counters = Some((high_water, total));
        }
    }

    pub async fn heartbeat(&self, progress: &AgentProgress) -> Result<AgentDirective, ClientError> {
        let mut progress = progress.clone();
        if let Some(measured) = progress.progress.as_mut()
            && measured.phase == "executing"
            && let Some(snapshot) = self
                .progress_phase
                .lock()
                .expect("progress phase lock poisoned")
                .as_ref()
            && snapshot.operation_id == progress.operation_id
        {
            measured.phase.clone_from(&snapshot.phase);
            if let Some((bytes, total)) = snapshot.counters {
                measured.completed_bytes = bytes;
                measured.total_bytes = Some(total);
                measured.total_bytes_known = true;
            }
        }
        progress.validate().map_err(|_| ClientError::Protocol)?;
        if progress.node_id != self.node_id {
            return Err(ClientError::Protocol);
        }
        let body = canonical_json(&progress).map_err(|_| ClientError::Protocol)?;
        // A renewal that arrives after the accepted lease deadline is still
        // accepted inside the Controller's renewal allowance, so the attempt
        // must not be truncated to the lease that is being recovered: bounding
        // it that way turned "the lease is nearly spent" into "no round trip can
        // complete", which is what made one late renewal the last one.
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/heartbeat")?)
            .header("content-type", "application/json")
            .timeout(HEARTBEAT_REQUEST_TIMEOUT)
            .body(body)
            .send()
            .await?;
        classify_response(&response)?;
        let body = bounded_body(response).await?;
        let directive = parse_strict::<AgentDirective>(&body).map_err(|_| ClientError::Protocol)?;
        directive.validate().map_err(|_| ClientError::Protocol)?;
        if directive.schema_version != progress.schema_version
            || directive.job_id != progress.job_id
            || directive.operation_id != progress.operation_id
            || directive.attempt != progress.attempt
            || directive.fence != progress.fence
            || directive.node_id != progress.node_id
            || directive.deadline < progress.deadline
        {
            return Err(ClientError::Protocol);
        }
        Ok(directive)
    }

    pub async fn host_runtime_grant(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        request_sha256: &str,
        installation_id: Option<uuid::Uuid>,
    ) -> Result<SignedHostHelperGrant, ClientError> {
        if claim.node_id != self.node_id || !valid_sha256(request_sha256) || claim.attempt == 0 {
            return Err(ClientError::Protocol);
        }
        let body = canonical_generated_json(&HostRuntimeGrantRequest {
            node_id: self.node_id.clone(),
            job_id: claim.job_id,
            operation_id: claim.operation_id,
            attempt: claim.attempt,
            fence: claim.fence,
            action: host_runtime_grant_action(action),
            request_sha256: request_sha256.to_owned(),
            expires_in_seconds: u32::from(HOST_RUNTIME_GRANT_TTL_SECONDS),
            installation_id,
        })
        .map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/host-runtime/grant")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        classify_response(&response)?;
        let body = bounded_body(response).await?;
        let response: HostHelperGrantResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        Ok(response.grant)
    }

    pub async fn recipe_run_inspection_grant(
        &self,
        binding: &RecipeRunInspectionBinding,
        request: &HostRuntimeRequest,
        request_sha256: &str,
    ) -> Result<RecipeRunInspectionGrant, ClientError> {
        binding.validate().map_err(|_| ClientError::Protocol)?;
        request.validate().map_err(|_| ClientError::Protocol)?;
        let expected_attempt = binding.run_generation;
        if request.action != HostRuntimeAction::RunInspect
            || request.job_id != binding.run_id
            || request.attempt != expected_attempt
            || request.observation.as_ref() != Some(binding)
            || !valid_sha256(request_sha256)
            || hex_sha256(&canonical_json(request).map_err(|_| ClientError::Protocol)?)
                != request_sha256
        {
            return Err(ClientError::Protocol);
        }
        let body = canonical_generated_json(&RecipeRunObservationGrantRequest {
            schema_version: 1,
            node_id: self.node_id.clone(),
            run_id: binding.run_id,
            installation_id: binding.installation_id,
            recipe_revision_id: binding.recipe_revision_id,
            recipe_content_sha256: binding.recipe_content_sha256.clone(),
            mapping_id: binding.mapping_id,
            mapping_generation: binding.mapping_generation,
            run_generation: binding.run_generation,
            image_digest: binding.image_digest.clone(),
            artifact_set_digest: binding.artifact_set_digest.clone(),
            model_identity: binding.model_identity.clone(),
            rank: binding.rank,
            role: binding.role.clone(),
            world_size: binding.world_size,
            local_address: binding.local_address,
            master_address: binding.master_address,
            master_port: binding.master_port,
            port: binding.port,
            runtime_arguments_sha256: binding.runtime_arguments_sha256.clone(),
            job_id: request.job_id,
            operation_id: request.operation_id,
            attempt: request.attempt,
            fence: request.fence,
            request_sha256: request_sha256.to_owned(),
            expires_in_seconds: HOST_RUNTIME_GRANT_TTL_SECONDS as u8,
        })
        .map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/recipe-runs/observation-grants")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if response.status() == StatusCode::TOO_EARLY {
            return Err(ClientError::ObservationNotReady);
        }
        // An unconsumed grant from the previous sweep is the controller's
        // bounded "not yet", not a lost authorization.  Reading it as a hard
        // failure abandoned every other run's observation for that tick and
        // slowed the claim cadence to the idle one.
        if response.status() == StatusCode::CONFLICT
            && response
                .headers()
                .get("x-vonk-error-code")
                .and_then(|value| value.to_str().ok())
                == Some("controller.recipe_run.observation_pending")
        {
            return Err(ClientError::ObservationNotReady);
        }
        classify_response(&response)?;
        let body = bounded_body(response).await?;
        let response: RecipeRunObservationGrantWire =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        if response.schema_version != 1
            || !valid_sha256(&response.observation_identity_sha256)
            || response.grant.validate().is_err()
            || response.grant.claims.node_id != self.node_id
        {
            return Err(ClientError::Protocol);
        }
        let operation = match &response.grant.claims.operation {
            HostHelperOperation::ExecuteContainerRuntimeRequestOperation(operation) => (
                &operation.action,
                &operation.job_id,
                &operation.operation_id,
                &operation.attempt,
                &operation.fence,
                &operation.request_sha256,
                operation
                    .observation_identity_sha256
                    .as_ref()
                    .ok_or(ClientError::Protocol)?,
            ),
            _ => return Err(ClientError::Protocol),
        };
        if *operation.0 != HostHelperContainerRuntimeAction::RunInspect
            || operation.1 != &request.job_id
            || operation.2 != &request.operation_id
            || *operation.3 != request.attempt
            || operation.4 != &request.fence
            || operation.5 != request_sha256
            || operation.6 != &response.observation_identity_sha256
        {
            return Err(ClientError::Protocol);
        }
        Ok(RecipeRunInspectionGrant {
            grant: response.grant,
            observation_identity_sha256: response.observation_identity_sha256,
        })
    }

    pub async fn package_activation_grant(
        &self,
        receipt: &vonk_agent_protocol::PackageActivationReceipt,
        runtime_identity: &AgentRuntimeIdentity,
    ) -> Result<SignedHostHelperGrant, ClientError> {
        let body = canonical_generated_json(&PackageActivationGrantRequest {
            node_id: self.node_id.clone(),
            receipt: receipt.clone(),
            runtime_identity: runtime_identity.clone(),
        })
        .map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/agent-upgrade/activation-grant")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        classify_response(&response)?;
        let body = bounded_body(response).await?;
        let response: HostHelperGrantResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        Ok(response.grant)
    }

    pub async fn agent_upgrade_grant(
        &self,
        claim: &AgentClaim,
        package_sha256: &str,
        package_signature: &str,
    ) -> Result<SignedHostHelperGrant, ClientError> {
        if claim.node_id != self.node_id
            || !valid_sha256(package_sha256)
            || package_signature.len() != 128
            || !package_signature
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            || claim.attempt == 0
        {
            return Err(ClientError::Protocol);
        }
        let body = canonical_generated_json(&AgentUpgradeGrantRequest {
            node_id: self.node_id.clone(),
            job_id: claim.job_id,
            operation_id: claim.operation_id,
            attempt: claim.attempt,
            fence: claim.fence,
            package_sha256: package_sha256.to_owned(),
            package_signature: package_signature.to_owned(),
            expires_in_seconds: u32::from(HOST_RUNTIME_GRANT_TTL_SECONDS),
        })
        .map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/agent-upgrade/grant")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        classify_response(&response)?;
        let body = bounded_body(response).await?;
        let response: HostHelperGrantResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        Ok(response.grant)
    }

    pub async fn recipe_spec(
        &self,
        installation_id: &str,
    ) -> Result<CompiledExecutionPlan, ClientError> {
        if uuid::Uuid::parse_str(installation_id).is_err() {
            return Err(ClientError::Protocol);
        }
        let response = self
            .current_client()
            .await
            .get(self.endpoint(&format!(
                "/agent/recipe-installations/{installation_id}/spec"
            ))?)
            .send()
            .await?;
        classify_response(&response)?;
        let body = bounded_claim_body(response).await?;
        let spec: CompiledExecutionPlan =
            serde_json::from_slice(&body).map_err(|_| ClientError::Protocol)?;
        spec.validate().map_err(|_| ClientError::Protocol)?;
        Ok(spec)
    }

    pub async fn source_bundle(
        &self,
        source_sha256: &str,
        expected_bytes: u64,
    ) -> Result<Vec<u8>, ClientError> {
        if source_sha256.len() != 64
            || !source_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            || !(1..=64 * 1024 * 1024).contains(&expected_bytes)
        {
            return Err(ClientError::Protocol);
        }
        let response = self
            .current_client()
            .await
            .get(self.endpoint(&format!("/agent/source-bundles/{source_sha256}"))?)
            .send()
            .await?;
        classify_response(&response)?;
        if response.content_length() != Some(expected_bytes) {
            return Err(ClientError::Protocol);
        }
        bounded_body_limit(response, expected_bytes as usize).await
    }

    pub async fn download_recipe_job_input(
        &self,
        job_id: uuid::Uuid,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
    ) -> Result<(), ClientError> {
        if !valid_sha256(sha256) || expected_bytes > 512 * 1024 * 1024 || !destination.is_absolute()
        {
            return Err(ClientError::Protocol);
        }
        let mut response = self
            .current_client()
            .await
            .get(self.endpoint(&format!("/agent/recipe-jobs/{job_id}/inputs/{sha256}"))?)
            .send()
            .await?;
        classify_response(&response)?;
        if response.content_length() != Some(expected_bytes) {
            return Err(ClientError::Protocol);
        }
        let parent = destination.parent().ok_or(ClientError::Protocol)?;
        let temporary = parent.join(format!(".job-input-{}.tmp", uuid::Uuid::new_v4()));
        let result = async {
            let mut output = tokio::fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .mode(0o600)
                .open(&temporary)
                .await?;
            let mut observed = 0_u64;
            let mut hasher = Sha256::new();
            while let Some(chunk) = response.chunk().await? {
                observed = observed
                    .checked_add(chunk.len() as u64)
                    .filter(|value| *value <= expected_bytes)
                    .ok_or(ClientError::Protocol)?;
                hasher.update(&chunk);
                output.write_all(&chunk).await?;
            }
            if observed != expected_bytes || hex::encode(hasher.finalize()) != sha256 {
                return Err(ClientError::Protocol);
            }
            output.sync_all().await?;
            drop(output);
            tokio::fs::hard_link(&temporary, destination).await?;
            tokio::fs::remove_file(&temporary).await?;
            Ok(())
        }
        .await;
        if result.is_err() {
            let _ = tokio::fs::remove_file(&temporary).await;
        }
        result
    }

    pub async fn upload_recipe_job_output(
        &self,
        job_id: uuid::Uuid,
        name: &str,
        media_type: &str,
        sha256: &str,
        expected_bytes: u64,
        path: &Path,
    ) -> Result<(), ClientError> {
        if name.is_empty()
            || name == "manifest.json"
            || name.len() > 128
            || !name.as_bytes()[0].is_ascii_alphanumeric()
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
            || !valid_sha256(sha256)
            || media_type.is_empty()
            || media_type.len() > 128
            || tokio::fs::metadata(path).await?.len() != expected_bytes
        {
            return Err(ClientError::Protocol);
        }
        let file = tokio::fs::File::open(path).await?;
        let response = self
            .current_client()
            .await
            .put(self.endpoint(&format!("/agent/recipe-jobs/{job_id}/outputs/{sha256}"))?)
            .header("x-vonk-artifact-name", name)
            .header("content-type", media_type)
            .header("content-length", expected_bytes)
            .timeout(Duration::from_secs(3600))
            .body(reqwest::Body::wrap_stream(ReaderStream::new(file)))
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_response(&response)?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn upload_recipe_image<F>(
        &self,
        build_id: uuid::Uuid,
        image_digest: &str,
        oci_layout_sha256: &str,
        image_bytes: u64,
        path: &Path,
        progress: F,
    ) -> Result<(), ClientError>
    where
        F: Fn(u64) + Send + Sync + 'static,
    {
        use futures_util::StreamExt;
        let progress = Arc::new(progress);
        if !valid_oci_digest(image_digest)
            || !valid_sha256(oci_layout_sha256)
            || !(1..=16 * 1024_u64.pow(4)).contains(&image_bytes)
            || tokio::fs::metadata(path).await?.len() != image_bytes
        {
            return Err(ClientError::Protocol);
        }
        use tokio::io::AsyncSeekExt;
        let endpoint = self.endpoint(&format!("/agent/recipe-builds/{build_id}/image"))?;
        // Retry from the Controller's persisted cursor, never from optimistic sent bytes.
        for attempt in 0..3 {
            let transfer = async {
                let status = self
                    .current_client()
                    .await
                    .head(endpoint.clone())
                    .header("x-vonk-image-digest", image_digest)
                    .header("x-vonk-oci-layout-sha256", oci_layout_sha256)
                    .header("x-vonk-image-bytes", image_bytes)
                    .send()
                    .await?;
                if status.status() == StatusCode::CONFLICT {
                    return Err(ClientError::Retryable);
                }
                if status.status() != StatusCode::OK {
                    classify_response(&status)?;
                    return Err(ClientError::Protocol);
                }
                let offset = status
                    .headers()
                    .get("x-vonk-upload-offset")
                    .and_then(|value| value.to_str().ok())
                    .and_then(|value| value.parse::<u64>().ok())
                    .filter(|value| *value <= image_bytes)
                    .ok_or(ClientError::Protocol)?;
                match status
                    .headers()
                    .get("x-vonk-upload-complete")
                    .and_then(|value| value.to_str().ok())
                {
                    Some("true") if offset == image_bytes => {
                        progress(image_bytes);
                        return Ok(());
                    }
                    Some("false") => (),
                    _ => return Err(ClientError::Protocol),
                }
                progress(offset);
                let mut file = tokio::fs::File::open(path).await?;
                file.seek(std::io::SeekFrom::Start(offset)).await?;
                let report = Arc::clone(&progress);
                let mut sent = offset;
                let body = ReaderStream::with_capacity(file, 1024 * 1024).inspect(move |chunk| {
                    if let Ok(bytes) = chunk {
                        sent += bytes.len() as u64;
                        report(sent);
                    }
                });
                let response = self
                    .current_client()
                    .await
                    .put(endpoint.clone())
                    .header("content-type", "application/x-tar")
                    .header("content-length", image_bytes - offset)
                    .header("x-vonk-image-bytes", image_bytes)
                    .header("x-vonk-upload-offset", offset)
                    .header("x-vonk-image-digest", image_digest)
                    .header("x-vonk-oci-layout-sha256", oci_layout_sha256)
                    .timeout(RECIPE_IMAGE_UPLOAD_TIMEOUT)
                    .body(reqwest::Body::wrap_stream(body))
                    .send()
                    .await?;
                if response.status() == StatusCode::NO_CONTENT {
                    Ok(())
                } else if response.status() == StatusCode::CONFLICT {
                    Err(ClientError::Retryable)
                } else {
                    classify_response(&response)?;
                    Err(ClientError::Protocol)
                }
            }
            .await;
            match transfer {
                Err(error) if error.retryable() && attempt < 2 => {
                    tokio::time::sleep(Duration::from_secs(1 << attempt)).await;
                }
                result => return result,
            }
        }
        unreachable!("last transfer attempt returns")
    }

    pub async fn download_artifact(
        &self,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
    ) -> Result<(), ClientError> {
        self.download_content_addressed(
            &format!("/agent/artifacts/{sha256}"),
            None,
            sha256,
            expected_bytes,
            destination,
        )
        .await
    }

    /// Download one object from the Controller's assignment-bound delivery
    /// API. A `.partial` destination is a resumable checkpoint; the assignment,
    /// ETag, length, and range are checked on every response.
    pub async fn download_distribution_object(
        &self,
        plan_digest: &str,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
    ) -> Result<(), ClientError> {
        if !valid_sha256(plan_digest) {
            return Err(ClientError::Protocol);
        }
        self.download_trusted_distribution_object_with_progress(
            plan_digest,
            sha256,
            expected_bytes,
            destination,
            destination.parent().ok_or(ClientError::Protocol)?,
            |_, _| {},
        )
        .await
    }

    /// Fetch and validate the assignment manifest before selecting model files
    /// or an OCI archive. The manifest is bounded and mTLS-authenticated.
    pub async fn distribution_manifest(
        &self,
        plan_digest: &str,
    ) -> Result<DistributionAssignment, ClientError> {
        if !valid_sha256(plan_digest) {
            return Err(ClientError::Protocol);
        }
        let response = self
            .current_client()
            .await
            .get(self.endpoint(&format!("/agent/distribution/manifests/{plan_digest}"))?)
            .send()
            .await?;
        classify_response(&response)?;
        let body = bounded_body(response).await?;
        let assignment: DistributionAssignment =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        assignment.validate().map_err(|_| ClientError::Protocol)?;
        if assignment.plan_digest != plan_digest || assignment.node_id != self.node_id {
            return Err(ClientError::Protocol);
        }
        Ok(assignment)
    }

    /// Consume a complete assignment. Every model/configuration object and the
    /// exact OCI archive is fetched through the assignment-bound endpoint;
    /// complete files are reused by trusted metadata, while `.partial` files
    /// resume by identity and length after an agent process restart. Model
    /// objects are stored below one content-addressed root so assignments with
    /// different plan digests can reuse the same verified bytes.
    pub async fn download_distribution(
        &self,
        plan_digest: &str,
        destination_root: &Path,
        archive_root: &Path,
    ) -> Result<DistributionDownloadEvidence, ClientError> {
        self.download_distribution_with_progress(
            plan_digest,
            destination_root,
            archive_root,
            |_| {},
        )
        .await
    }

    pub async fn download_distribution_with_progress<F>(
        &self,
        plan_digest: &str,
        destination_root: &Path,
        archive_root: &Path,
        progress: F,
    ) -> Result<DistributionDownloadEvidence, ClientError>
    where
        F: FnMut(DistributionProgress),
    {
        if !valid_sha256(plan_digest)
            || !destination_root.is_absolute()
            || !archive_root.is_absolute()
        {
            return Err(ClientError::Protocol);
        }
        let assignment = self.distribution_manifest(plan_digest).await?;
        let model_root = destination_root.join("models");
        let oci_root = archive_root.to_path_buf();
        tokio::fs::create_dir_all(&model_root).await?;
        tokio::fs::create_dir_all(&oci_root).await?;
        tokio::fs::set_permissions(&model_root, std::fs::Permissions::from_mode(0o700)).await?;
        tokio::fs::set_permissions(&oci_root, std::fs::Permissions::from_mode(0o700)).await?;
        ensure_private_parent(&model_root, destination_root).await?;
        ensure_private_parent(&oci_root, archive_root).await?;
        let tracker = Mutex::new(DistributionProgressTracker {
            object_bytes: vec![0; assignment.objects.len()],
            bytes: 0,
            completed_items: 0,
            total_bytes: assignment.objects.iter().map(|object| object.bytes).sum(),
            callback: progress,
        });
        let mut pending = Vec::new();
        for (index, object) in assignment.objects.iter().enumerate() {
            let path = if object.kind == "model" {
                model_root.join(&object.sha256)
            } else if object.sha256 == assignment.oci_archive_sha256 && object.kind == "oci-archive"
            {
                oci_root.join(&object.sha256)
            } else if object.kind == "oci-layer" {
                oci_root.join("layers").join(&object.sha256)
            } else {
                continue;
            };
            let managed_root = if object.kind == "model" {
                destination_root
            } else {
                archive_root
            };
            if !path.starts_with(managed_root) {
                return Err(ClientError::Protocol);
            }
            if let Some(parent) = path.parent() {
                tokio::fs::create_dir_all(parent).await?;
            }
            pending.push((index, object, path, managed_root));
        }
        // Start large objects first so a large image archive does not become
        // a lone serial tail after all of the smaller model files finish.
        pending.sort_by_key(|(_, object, _, _)| std::cmp::Reverse(object.bytes));
        // Bound both network traffic and disk buffers. These futures stay
        // owned by this call: an error or cancellation drops the remaining
        // transfers, whose partial files remain resumable on disk.
        let mut completed: Vec<_> = stream::iter(pending)
            .map(|(index, object, path, managed_root)| {
                let tracker = &tracker;
                async move {
                    self.download_trusted_distribution_object_with_progress(
                        plan_digest,
                        &object.sha256,
                        object.bytes,
                        &path,
                        managed_root,
                        |bytes, phase| {
                            tracker
                                .lock()
                                .expect("distribution progress lock")
                                .report(index, object, bytes, phase, false);
                        },
                    )
                    .await?;
                    tracker.lock().expect("distribution progress lock").report(
                        index,
                        object,
                        object.bytes,
                        "verifying",
                        true,
                    );
                    Ok::<_, ClientError>((index, path))
                }
            })
            .buffer_unordered(DISTRIBUTION_CONCURRENCY)
            .try_collect()
            .await?;
        completed.sort_unstable_by_key(|(index, _)| *index);
        let mut model_paths = Vec::new();
        let mut model_digests = Vec::new();
        for (index, path) in completed {
            let object = &assignment.objects[index];
            if object.kind == "model" {
                model_paths.push(path);
                model_digests.push(object.sha256.clone());
            }
        }
        let downloaded_bytes = tracker
            .into_inner()
            .expect("distribution progress lock")
            .bytes;
        let archive_path = oci_root.join(&assignment.oci_archive_sha256);
        if !archive_path.exists() {
            return Err(ClientError::Protocol);
        }
        let oci_archive_sha256 = assignment.oci_archive_sha256.clone();
        let oci_archive_bytes = assignment
            .objects
            .iter()
            .find(|object| object.sha256 == oci_archive_sha256)
            .map(|object| object.bytes)
            .ok_or(ClientError::Protocol)?;
        Ok(DistributionDownloadEvidence {
            assignment_id: assignment.assignment_id,
            model_artifact_set_sha256: assignment.model_artifact_set_sha256,
            model_digests,
            model_paths,
            oci_archive_path: archive_path,
            oci_archive_sha256,
            oci_archive_bytes,
            oci_image_digest: assignment.oci_image_digest,
            downloaded_bytes,
        })
    }

    async fn download_trusted_distribution_object_with_progress<F>(
        &self,
        plan_digest: &str,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
        managed_root: &Path,
        mut progress: F,
    ) -> Result<(), ClientError>
    where
        F: FnMut(u64, &'static str),
    {
        // The assignment-bound mTLS endpoint and its exact ranged response
        // headers establish the transfer contract. Hash the completed object
        // before accepting it so a digest-named cache file cannot be trusted
        // merely because its length and custody metadata look correct.
        if !valid_sha256(plan_digest)
            || !valid_sha256(sha256)
            || !(1..=16 * 1024_u64.pow(4)).contains(&expected_bytes)
            || !destination.is_absolute()
        {
            return Err(ClientError::Protocol);
        }
        let parent = destination.parent().ok_or(ClientError::Protocol)?;
        if !destination.starts_with(managed_root) {
            return Err(ClientError::Protocol);
        }
        ensure_private_parent(parent, managed_root).await?;

        if let Some(file) = inspect_trusted_final(destination, expected_bytes).await? {
            let before = file.metadata().await?;
            let cached = self
                .distribution_receipts
                .lock()
                .expect("distribution receipt lock")
                .get(destination)
                .is_some_and(|receipt| receipt.matches(sha256, &before));
            if cached {
                return Ok(());
            }
            drop(file);
            progress(expected_bytes, "verifying");
            match sha256_path_with_metadata(destination, expected_bytes).await {
                Ok((observed, metadata))
                    if observed == sha256
                        && validate_trusted_metadata(&metadata, expected_bytes) =>
                {
                    self.distribution_receipts
                        .lock()
                        .expect("distribution receipt lock")
                        .insert(
                            destination.to_path_buf(),
                            DistributionObjectReceipt::new(sha256, &metadata),
                        );
                    // Verifying an object already on disk reads all of it, so it
                    // fills the page cache exactly as a transfer does. The live
                    // observation that motivated this: a re-verified 199 GB
                    // object left the device reporting about 2 GB free, and the
                    // checkpoint loader refused its 1.27 GB staging buffer.
                    release_distribution_page_cache(destination);
                    return Ok(());
                }
                // The object sits at its exact digest name with exact custody
                // and still fails its content check, so this is a proven
                // mismatch inside the managed cache.  Quarantine only that
                // object: leaving it in place makes every later attempt trust
                // the metadata and re-report the same failure without ever
                // rehydrating the bytes from the authorized assignment.
                Ok(_) => {
                    self.distribution_receipts
                        .lock()
                        .expect("distribution receipt lock")
                        .remove(destination);
                    quarantine_proven_object(destination, expected_bytes).await?
                }
                Err(error) => return Err(error),
            }
        }

        let partial = partial_path(destination);
        let mut output = open_trusted_partial(&partial).await?;
        let metadata = output.metadata().await?;
        let mut offset = metadata.len();
        if offset > expected_bytes {
            return Err(ClientError::Protocol);
        }
        // A resumed partial has no process receipt: read its durable prefix
        // exactly once, then hash the remaining authenticated range as written.
        let mut hasher = Sha256::new();
        if offset > 0 {
            output.seek(std::io::SeekFrom::Start(0)).await?;
            let mut remaining = offset;
            let mut buffer = vec![0_u8; 64 * 1024];
            while remaining > 0 {
                let take = buffer.len().min(remaining as usize);
                let count = output.read(&mut buffer[..take]).await?;
                if count == 0 {
                    return Err(ClientError::Protocol);
                }
                hasher.update(&buffer[..count]);
                #[cfg(test)]
                DISTRIBUTION_HASH_READ_BYTES.with(|bytes| bytes.set(bytes.get() + count as u64));
                remaining -= count as u64;
            }
            let after = output.metadata().await?;
            if !same_file_metadata(&metadata, &after) {
                return Err(ClientError::Protocol);
            }
        }

        // TLS/network chunks can be much smaller than an efficient disk
        // write. Coalesce them so Tokio does not dispatch a blocking file
        // operation for every received chunk. Keep this writer across range
        // retries; a process restart resumes from the actual partial length.
        let mut output = BufWriter::with_capacity(1024 * 1024, output);
        progress(offset, "copying");
        let mut last_progress = tokio::time::Instant::now();
        let mut retries = 0_u32;
        while offset < expected_bytes {
            let end = expected_bytes
                .saturating_sub(1)
                .min(offset.saturating_add(8 * 1024 * 1024 - 1));
            let mut url = self.endpoint(&format!("/agent/distribution/objects/{sha256}"))?;
            url.query_pairs_mut()
                .append_pair("plan_digest", plan_digest);
            let attempt: Result<(), ClientError> = async {
                let mut response = self
                    .current_client()
                    .await
                    .get(url)
                    .header("range", format!("bytes={offset}-{end}"))
                    .header("if-range", format!("\"sha256:{sha256}\""))
                    .send()
                    .await?;
                classify_response(&response)?;
                let expected_etag = format!("\"sha256:{sha256}\"");
                let expected_range = format!("bytes {offset}-{end}/{expected_bytes}");
                if response.status() != StatusCode::PARTIAL_CONTENT
                    || response.content_length() != Some(end - offset + 1)
                    || response
                        .headers()
                        .get("etag")
                        .and_then(|value| value.to_str().ok())
                        != Some(expected_etag.as_str())
                    || response
                        .headers()
                        .get("content-range")
                        .and_then(|value| value.to_str().ok())
                        != Some(expected_range.as_str())
                {
                    return Err(ClientError::Protocol);
                }
                while let Some(chunk) = response.chunk().await? {
                    if chunk.len() as u64 > end + 1 - offset {
                        return Err(ClientError::Protocol);
                    }
                    output.write_all(&chunk).await?;
                    hasher.update(&chunk);
                    // This writer survives network retries, so resume from
                    // its accepted bytes even within an interrupted range.
                    offset += chunk.len() as u64;
                    if last_progress.elapsed() >= Duration::from_millis(200) {
                        progress(offset, "copying");
                        last_progress = tokio::time::Instant::now();
                    }
                }
                if offset != end + 1 {
                    return Err(ClientError::Protocol);
                }
                Ok(())
            }
            .await;
            match attempt {
                Ok(()) => retries = 0,
                Err(error) if error.retryable() && retries < 4 => {
                    progress(offset, "copying");
                    tokio::time::sleep(Duration::from_millis(500 * (1 << retries))).await;
                    retries += 1;
                }
                Err(error) => return Err(error),
            }
        }
        progress(offset, "copying");
        output.flush().await?;
        output.get_ref().sync_all().await?;
        let output = output.into_inner();
        let synced_metadata = output.metadata().await?;
        let partial_metadata = tokio::fs::symlink_metadata(&partial).await?;
        if !validate_trusted_metadata(&synced_metadata, expected_bytes)
            || !validate_trusted_metadata(&partial_metadata, expected_bytes)
            || !same_file_metadata(&synced_metadata, &partial_metadata)
        {
            return Err(ClientError::Protocol);
        }
        // Prove the bytes we hold before they take the digest name.  Renaming
        // first and checking afterwards leaves a file at the digest name after
        // a mismatch, so every later attempt would trust its metadata and
        // re-report the same failure instead of transferring the object again.
        progress(expected_bytes, "verifying");
        let received_digest = hex::encode(hasher.finalize());
        if received_digest != sha256 {
            // Exactly-owned received bytes that are provably not the requested
            // object: discard this partial so the next authorized attempt
            // starts from empty rather than resuming bytes we cannot keep.
            quarantine_proven_object_matching(&partial, expected_bytes, &synced_metadata).await?;
            return Err(ClientError::Protocol);
        }
        let before_rename = tokio::fs::symlink_metadata(&partial).await?;
        if !same_file_metadata(&synced_metadata, &before_rename) {
            return Err(ClientError::Protocol);
        }
        tokio::fs::rename(&partial, destination).await?;
        sync_parent(parent).await?;
        let final_file = inspect_trusted_final(destination, expected_bytes)
            .await?
            .ok_or(ClientError::Protocol)?;
        let final_metadata = final_file.metadata().await?;
        let output_after = output.metadata().await?;
        // Rename changes ctime but cannot change the already-synced content
        // of this private inode. Bind the receipt to its post-rename ctime.
        if !same_file_content_identity(&synced_metadata, &output_after)
            || !same_file_metadata(&output_after, &final_metadata)
        {
            return Err(ClientError::Protocol);
        }
        self.distribution_receipts
            .lock()
            .expect("distribution receipt lock")
            .insert(
                destination.to_path_buf(),
                DistributionObjectReceipt::new(sha256, &final_metadata),
            );
        // The transfer and its verification both filled the page cache with an
        // object of up to hundreds of gigabytes. It stays on disk and keeps its
        // receipt; the resident pages do not need to stay with it.
        release_distribution_page_cache(destination);
        Ok(())
    }

    async fn download_content_addressed(
        &self,
        endpoint: &str,
        plan_digest: Option<&str>,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
    ) -> Result<(), ClientError> {
        self.download_content_addressed_with_progress(
            endpoint,
            plan_digest,
            sha256,
            expected_bytes,
            destination,
            |_| {},
        )
        .await
    }

    async fn download_content_addressed_with_progress<F>(
        &self,
        endpoint: &str,
        plan_digest: Option<&str>,
        sha256: &str,
        expected_bytes: u64,
        destination: &Path,
        mut progress: F,
    ) -> Result<(), ClientError>
    where
        F: FnMut(u64),
    {
        if !valid_sha256(sha256) || !(1..=16 * 1024_u64.pow(4)).contains(&expected_bytes) {
            return Err(ClientError::Protocol);
        }
        let existing = match tokio::fs::metadata(destination).await {
            Ok(metadata) => metadata.len(),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
            Err(error) => return Err(ClientError::CredentialRead(error)),
        };
        if existing > expected_bytes {
            return Err(ClientError::Protocol);
        }
        if existing == expected_bytes {
            if sha256_path(destination, expected_bytes).await? == sha256 {
                progress(expected_bytes);
                return Ok(());
            }
            // A complete object with the wrong identity is corruption, not a
            // resumable checkpoint. Keep it for diagnostics and fail closed.
            return Err(ClientError::Protocol);
        }
        let mut output = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(destination)
            .await?;
        let mut offset = existing;
        while offset < expected_bytes {
            let end = expected_bytes
                .saturating_sub(1)
                .min(offset.saturating_add(8 * 1024 * 1024 - 1));
            let mut url = self.endpoint(&format!("{endpoint}/{sha256}"))?;
            if let Some(plan_digest) = plan_digest {
                url.query_pairs_mut()
                    .append_pair("plan_digest", plan_digest);
            }
            let response = self
                .current_client()
                .await
                .get(url)
                .header("range", format!("bytes={offset}-{end}"))
                .header("if-range", format!("\"sha256:{sha256}\""))
                .send()
                .await?;
            let expected_etag = format!("\"sha256:{sha256}\"");
            let expected_range = format!("bytes {offset}-{end}/{expected_bytes}");
            if response.status() != StatusCode::PARTIAL_CONTENT
                || response.content_length() != Some(end - offset + 1)
                || response
                    .headers()
                    .get("etag")
                    .and_then(|value| value.to_str().ok())
                    != Some(expected_etag.as_str())
                || response
                    .headers()
                    .get("content-range")
                    .and_then(|value| value.to_str().ok())
                    != Some(expected_range.as_str())
            {
                classify_response(&response)?;
                return Err(ClientError::Protocol);
            }
            let mut copied = 0_u64;
            let expected_chunk = end - offset + 1;
            let mut response = response;
            while let Some(chunk) = response.chunk().await? {
                copied = copied.saturating_add(chunk.len() as u64);
                if copied > expected_chunk {
                    return Err(ClientError::Protocol);
                }
                output.write_all(&chunk).await?;
            }
            if copied != expected_chunk {
                return Err(ClientError::Protocol);
            }
            offset = end + 1;
            progress(offset);
        }
        output.sync_all().await?;
        if sha256_path(destination, expected_bytes).await? != sha256 {
            return Err(ClientError::Protocol);
        }
        Ok(())
    }

    pub async fn report_inventory(&self, inventory: &Inventory) -> Result<(), ClientError> {
        let request = InventoryRequest {
            schema_version: 1,
            observed_at: chrono::Utc::now().into(),
            disk_total_bytes: inventory.disk_total_bytes,
            disk_free_bytes: inventory.disk_available_bytes,
            host_memory_total_bytes: inventory.memory_total_bytes,
            host_memory_free_bytes: inventory.memory_available_bytes,
            gpu_memory_total_bytes: inventory.gpu_memory_total_bytes,
            gpu_memory_free_bytes: inventory.gpu_memory_free_bytes,
            gpu_count: inventory.gpu_count,
            memory_pool: inventory.memory_pool,
            artifact_store_read_only: inventory.artifact_store_read_only,
            capabilities: inventory.capabilities.clone(),
            fabric_address: inventory.fabric_address.map(|value| value.to_string()),
            fabric_bandwidth_mbps: inventory
                .fabric_bandwidth_mbps
                .map(|value| u32::try_from(value).map_err(|_| ClientError::Protocol))
                .transpose()?,
            nvidia_driver_version: inventory.nvidia_driver_version.clone(),
            container_runtime_version: inventory.container_runtime_version.clone(),
        };
        request.validate().map_err(|_| ClientError::Protocol)?;
        let body = canonical_generated_json(&request).map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/inventory")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_response(&response)?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn report_exact_recipe_run_observations(
        &self,
        observations: &[ExactRecipeRunObservation],
    ) -> Result<(), ClientError> {
        let envelope =
            build_exact_recipe_run_observations(&self.node_id, chrono::Utc::now(), observations)?;
        let body = canonical_generated_json(&envelope).map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/recipe-runs/observations")?)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_response(&response)?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn report_telemetry(&self, samples: &[TelemetrySample]) -> Result<(), ClientError> {
        if !valid_report_batch(samples) {
            return Err(ClientError::Protocol);
        }
        let request = TelemetryRequest {
            schema_version: 1,
            samples: samples.iter().map(|sample| sample.wire().clone()).collect(),
        };
        let body = canonical_generated_json(&request).map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/telemetry")?)
            .timeout(Duration::from_secs(2))
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            Ok(())
        } else {
            classify_response(&response)?;
            Err(ClientError::Protocol)
        }
    }

    pub async fn renew(&self, csr: &[u8]) -> Result<IssuedCertificateResponse, ClientError> {
        let csr = std::str::from_utf8(csr).map_err(|_| ClientError::Protocol)?;
        if csr.is_empty() || csr.len() > 16 * 1024 {
            return Err(ClientError::Protocol);
        }
        let request = RenewRequest {
            csr: csr.to_owned(),
            node_id: self.node_id.clone(),
        };
        let body = canonical_generated_json(&request).map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/renew")?)
            .timeout(ROTATION_REQUEST_TIMEOUT)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if !response.status().is_success() {
            // Renewal can identify one narrowly defined recovery case from
            // its bounded, safe error body. Keep the status, path, request ID,
            // and canonical code in the contextual Controller error for all
            // outcomes; generic 401/403 responses remain rejections.
            let status = response.status();
            let endpoint = response.url().path().to_owned();
            let operation = format!("controller.request {endpoint}");
            let request_id = response
                .headers()
                .get("x-request-id")
                .and_then(|value| value.to_str().ok())
                .filter(|value| valid_error_token(value))
                .map(str::to_owned);
            let header_code = response
                .headers()
                .get("x-vonk-error-code")
                .and_then(|value| value.to_str().ok())
                .filter(|value| valid_error_code(value))
                .map(str::to_owned);
            let body = bounded_body(response).await?;
            let code = if status == StatusCode::FORBIDDEN && is_rotation_conflict(&body) {
                Some("agent.certificate.rotation.conflict".to_owned())
            } else {
                header_code
            };
            return Err(ClientError::Controller(Box::new(controller_error(
                status, &endpoint, &operation, request_id, code,
            ))));
        }
        let body = bounded_body(response).await?;
        let issued: IssuedCertificateResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        if issued.node_id != self.node_id || issued.generation == 0 {
            return Err(ClientError::Protocol);
        }
        Ok(issued)
    }

    /// Request recovery of an unactivated staged certificate whose CSR does
    /// not match the durable pending CSR.  The endpoint is authenticated with
    /// this client's active identity and is intentionally separate from the
    /// normal renewal operation so a 403 cannot silently become a replacement
    /// request.
    pub async fn recover_renewal(
        &self,
        csr: &[u8],
    ) -> Result<IssuedCertificateResponse, ClientError> {
        let csr = std::str::from_utf8(csr).map_err(|_| ClientError::Protocol)?;
        if csr.is_empty() || csr.len() > 16 * 1024 {
            return Err(ClientError::Protocol);
        }
        let request = RenewRequest {
            csr: csr.to_owned(),
            node_id: self.node_id.clone(),
        };
        let body = canonical_generated_json(&request).map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/renew/recover")?)
            .timeout(ROTATION_REQUEST_TIMEOUT)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        classify_response(&response)?;
        let body = bounded_body(response).await?;
        let issued: IssuedCertificateResponse =
            parse_strict(&body).map_err(|_| ClientError::Protocol)?;
        if issued.node_id != self.node_id || issued.generation == 0 {
            return Err(ClientError::Protocol);
        }
        Ok(issued)
    }

    pub async fn activate(&self, generation: u64) -> Result<(), ClientError> {
        if generation == 0 {
            return Err(ClientError::Protocol);
        }
        let request = ActivateRequest {
            generation,
            node_id: self.node_id.clone(),
        };
        let body = canonical_generated_json(&request).map_err(|_| ClientError::Protocol)?;
        let response = self
            .current_client()
            .await
            .post(self.endpoint("/agent/renew/activate")?)
            .timeout(ROTATION_REQUEST_TIMEOUT)
            .header("content-type", "application/json")
            .body(body)
            .send()
            .await?;
        if response.status() != StatusCode::NO_CONTENT {
            classify_response(&response)?;
            return Err(ClientError::Protocol);
        }
        Ok(())
    }

    fn endpoint(&self, path: &str) -> Result<Url, ClientError> {
        self.controller
            .join(path)
            .map_err(|_| ClientError::Protocol)
    }
}

fn host_runtime_grant_action(action: HostRuntimeAction) -> HostRuntimeGrantRequestAction {
    match action {
        HostRuntimeAction::RuntimePreflight => HostRuntimeGrantRequestAction::RuntimePreflight,
        HostRuntimeAction::ImageImport => HostRuntimeGrantRequestAction::ImageImport,
        HostRuntimeAction::ImageInspect => HostRuntimeGrantRequestAction::ImageInspect,
        HostRuntimeAction::RunInspect => HostRuntimeGrantRequestAction::RunInspect,
        HostRuntimeAction::Start => HostRuntimeGrantRequestAction::Start,
        HostRuntimeAction::Stop => HostRuntimeGrantRequestAction::Stop,
        HostRuntimeAction::InstallationCleanup => {
            HostRuntimeGrantRequestAction::InstallationCleanup
        }
    }
}

pub fn parse_claim_response(status: u16, body: &[u8]) -> Result<Option<AgentClaim>, ClientError> {
    match status {
        204 if body.is_empty() => Ok(None),
        200 if body.len() <= MAX_CLAIM_BODY_BYTES => {
            let claim: AgentClaim = parse_strict(body).map_err(|_| ClientError::Protocol)?;
            claim.validate().map_err(|_| ClientError::Protocol)?;
            Ok(Some(claim))
        }
        status => {
            let status_code =
                StatusCode::from_u16(status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
            if status_code.is_success() {
                Err(ClientError::Protocol)
            } else {
                classify_status(status_code).map(|_| None)
            }
        }
    }
}

fn classify_status(status: StatusCode) -> Result<(), ClientError> {
    if status.is_success() {
        return Ok(());
    }
    Err(ClientError::Controller(Box::new(controller_error(
        status,
        "/",
        "controller.request",
        None,
        None,
    ))))
}

fn classify_response(response: &reqwest::Response) -> Result<(), ClientError> {
    if response.status().is_success() {
        return Ok(());
    }
    Err(ClientError::Controller(Box::new(
        response_controller_error(response),
    )))
}

/// Build the bounded Controller error for one non-success response.
///
/// Only the URL path, the validated error token/code headers and the retry
/// hint are captured; bodies, queries and credentials stay unread.
fn response_controller_error(response: &reqwest::Response) -> ControllerError {
    let endpoint = response.url().path();
    let operation = format!("controller.request {endpoint}");
    let request_id = response
        .headers()
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .filter(|value| valid_error_token(value))
        .map(str::to_owned);
    let code = response
        .headers()
        .get("x-vonk-error-code")
        .and_then(|value| value.to_str().ok())
        .filter(|value| valid_error_code(value))
        .map(str::to_owned);
    let mut error = controller_error(response.status(), endpoint, &operation, request_id, code);
    error.retry_after_seconds = response
        .headers()
        .get(reqwest::header::RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| {
            value.parse::<u32>().ok().or_else(|| {
                let deadline = chrono::DateTime::parse_from_rfc2822(value).ok()?;
                let delay =
                    (deadline.with_timezone(&chrono::Utc) - chrono::Utc::now()).num_seconds();
                u32::try_from(delay.max(0)).ok()
            })
        });
    error
}

/// Digest one Controller validation problem into a bounded refusal reason.
///
/// The published shape is `{"detail": str, "issues": [{"type", "loc", "msg"}]}`
/// with an optional `context`.  Only structural facts are kept: the detail
/// line, and each retained issue's location and error type.  Messages are
/// dropped because the location and type already name the field and the rule,
/// and dropping them keeps submitted content and validator input out of the
/// durable record.
///
/// A union payload fails every branch, so the Controller can report a hundred
/// issues for one bad field.  A constraint failure is therefore reported in
/// preference to the shape mismatches of branches that never applied, only a
/// bounded number of issues is kept, and the rest are counted.  Anything that
/// does not match the declared shape yields `None` rather than a guess.
fn controller_rejection_digest(body: &[u8]) -> Option<String> {
    let value: serde_json::Value = serde_json::from_slice(body).ok()?;
    let object = value.as_object()?;
    let detail = object
        .get("detail")
        .and_then(serde_json::Value::as_str)
        .filter(|detail| !detail.is_empty() && detail.len() <= MAX_REJECTION_CONTEXT_CHARS)
        .map(sanitize_text);
    let mut specific = Vec::new();
    let mut structural = Vec::new();
    let mut reported = 0_usize;
    if let Some(issues) = object.get("issues").and_then(serde_json::Value::as_array) {
        reported = issues.len();
        for issue in issues {
            let Some(issue) = issue.as_object() else {
                continue;
            };
            let Some(location) = issue
                .get("loc")
                .and_then(serde_json::Value::as_array)
                .map(|segments| {
                    segments
                        .iter()
                        .map(render_rejection_location)
                        .collect::<Vec<_>>()
                        .join(".")
                })
                .filter(|location| !location.is_empty())
            else {
                continue;
            };
            let kind = issue
                .get("type")
                .and_then(serde_json::Value::as_str)
                .filter(|kind| valid_error_token(kind))
                .unwrap_or("invalid");
            let rendered = format!("{location} ({kind})");
            if STRUCTURAL_ERROR_TYPES.contains(&kind) {
                structural.push(rendered);
            } else {
                specific.push(rendered);
            }
        }
    }
    // A union payload fails every branch at once, so a shape mismatch against
    // a branch that never applied would otherwise bury the single constraint
    // the producer actually broke.  Shape mismatches are reported only when
    // there is no constraint failure to report instead.
    let ranked = if specific.is_empty() {
        structural
    } else {
        specific
    };
    let kept = ranked.len().min(MAX_REJECTION_ISSUES);
    if kept == 0 && detail.is_none() {
        return None;
    }
    let mut digest = detail.unwrap_or_else(|| "request is invalid".to_owned());
    if kept > 0 {
        digest.push_str(": ");
        digest.push_str(&ranked[..kept].join("; "));
        if reported > kept {
            digest.push_str(&format!("; +{} more", reported - kept));
        }
    }
    Some(
        sanitize_text(&digest)
            .chars()
            .take(MAX_REJECTION_CONTEXT_CHARS)
            .collect(),
    )
}

/// Render one reported location segment as bounded, sanitized text.
fn render_rejection_location(segment: &serde_json::Value) -> String {
    let text = match segment {
        serde_json::Value::String(text) => text.clone(),
        serde_json::Value::Number(number) => number.to_string(),
        _ => return "?".to_owned(),
    };
    sanitize_text(&text)
        .chars()
        .take(MAX_REJECTION_LOCATION_CHARS)
        .collect()
}

fn controller_error(
    status: StatusCode,
    endpoint: &str,
    operation: &str,
    request_id: Option<String>,
    supplied_code: Option<String>,
) -> ControllerError {
    let status_code = status.as_u16();
    let code = supplied_code.unwrap_or_else(|| match status_code {
        401 => "controller.authentication_required".to_owned(),
        403 => "controller.request_rejected".to_owned(),
        408 => "controller.timeout".to_owned(),
        429 => "controller.rate_limited".to_owned(),
        500..=599 => "controller.unavailable".to_owned(),
        _ => format!("controller.http_{status_code}"),
    });
    let decision = if matches!(status_code, 408 | 429 | 500..=599) {
        "retry"
    } else {
        "exit"
    };
    ControllerError {
        operation: operation.to_owned(),
        endpoint: endpoint.to_owned(),
        status: status_code,
        code,
        request_id,
        decision,
        retry_after_seconds: None,
        summary: None,
    }
}

fn valid_error_token(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte))
}

fn valid_error_code(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || (index > 0 && b"_.:-".contains(&byte))
        })
}

fn is_rotation_conflict(body: &[u8]) -> bool {
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(body) else {
        return false;
    };
    let code = value.get("code").and_then(serde_json::Value::as_str);
    let detail = value.get("detail").and_then(serde_json::Value::as_str);
    matches!(
        code,
        Some("agent.certificate.rotation.conflict") | Some("agent_certificate_rotation_conflict")
    ) || matches!(
        detail,
        Some("a different certificate rotation is already staged")
    )
}

async fn bounded_body(response: reqwest::Response) -> Result<Vec<u8>, ClientError> {
    bounded_body_limit(response, MAX_BODY_BYTES).await
}

async fn bounded_claim_body(response: reqwest::Response) -> Result<Vec<u8>, ClientError> {
    bounded_body_limit(response, MAX_CLAIM_BODY_BYTES).await
}

async fn sha256_path(path: &Path, expected_bytes: u64) -> Result<String, ClientError> {
    Ok(sha256_path_with_metadata(path, expected_bytes).await?.0)
}

async fn sha256_path_with_metadata(
    path: &Path,
    expected_bytes: u64,
) -> Result<(String, fs::Metadata), ClientError> {
    let mut file = tokio::fs::OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32)
        .open(path)
        .await?;
    let before = file.metadata().await?;
    if !before.file_type().is_file() || before.file_type().is_symlink() || before.nlink() != 1 {
        return Err(ClientError::Protocol);
    }
    let mut digest = Sha256::new();
    let mut total = 0_u64;
    // Keep the buffer on the heap: this async future is held by the default
    // tokio worker stack, where a 1 MiB inline array can overflow it.  64 KiB
    // matches the bounded buffers used by the adjacent OCI hashing paths.
    let mut buffer = vec![0_u8; 64 * 1024];
    loop {
        let count = tokio::io::AsyncReadExt::read(&mut file, &mut buffer).await?;
        if count == 0 {
            break;
        }
        total = total.saturating_add(count as u64);
        if total > expected_bytes {
            return Err(ClientError::Protocol);
        }
        digest.update(&buffer[..count]);
        #[cfg(test)]
        DISTRIBUTION_HASH_READ_BYTES.with(|bytes| bytes.set(bytes.get() + count as u64));
    }
    if total != expected_bytes {
        return Err(ClientError::Protocol);
    }
    let after = file.metadata().await?;
    if !same_file_metadata(&before, &after) {
        return Err(ClientError::Protocol);
    }
    Ok((hex::encode(digest.finalize()), before))
}

fn same_file_metadata(before: &fs::Metadata, after: &fs::Metadata) -> bool {
    same_file_content_identity(before, after)
        && before.ctime() == after.ctime()
        && before.ctime_nsec() == after.ctime_nsec()
}

fn same_file_content_identity(before: &fs::Metadata, after: &fs::Metadata) -> bool {
    before.dev() == after.dev()
        && before.ino() == after.ino()
        && before.len() == after.len()
        && before.mtime() == after.mtime()
        && before.mtime_nsec() == after.mtime_nsec()
}

async fn ensure_private_parent(parent: &Path, managed_root: &Path) -> Result<(), ClientError> {
    let metadata = tokio::fs::symlink_metadata(parent).await?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.mode() & 0o022 != 0
    {
        return Err(ClientError::Protocol);
    }
    if !parent.starts_with(managed_root) {
        return Err(ClientError::Protocol);
    }
    let relative = parent
        .strip_prefix(managed_root)
        .map_err(|_| ClientError::Protocol)?;
    let mut component = managed_root.to_path_buf();
    for part in relative.components() {
        component.push(part.as_os_str());
        let metadata = tokio::fs::symlink_metadata(&component).await?;
        if metadata.file_type().is_symlink() {
            return Err(ClientError::Protocol);
        }
    }
    let canonical_root = tokio::fs::canonicalize(managed_root).await?;
    let canonical_parent = tokio::fs::canonicalize(parent).await?;
    if !canonical_parent.starts_with(canonical_root) {
        return Err(ClientError::Protocol);
    }
    Ok(())
}

fn partial_path(path: &Path) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(".partial");
    PathBuf::from(value)
}

fn validate_trusted_metadata(metadata: &fs::Metadata, expected_bytes: u64) -> bool {
    metadata.file_type().is_file()
        && !metadata.file_type().is_symlink()
        && metadata.nlink() == 1
        && metadata.uid() == rustix::process::geteuid().as_raw()
        && metadata.mode() & 0o777 == 0o600
        && metadata.len() == expected_bytes
}

async fn inspect_trusted_final(
    path: &Path,
    expected_bytes: u64,
) -> Result<Option<tokio::fs::File>, ClientError> {
    let path_metadata = match tokio::fs::symlink_metadata(path).await {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    if !validate_trusted_metadata(&path_metadata, expected_bytes) {
        return Err(ClientError::Protocol);
    }
    let file = tokio::fs::OpenOptions::new()
        .read(true)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32)
        .open(path)
        .await
        .map_err(|_| ClientError::Protocol)?;
    let opened_metadata = file.metadata().await?;
    if !validate_trusted_metadata(&opened_metadata, expected_bytes)
        || opened_metadata.dev() != path_metadata.dev()
        || opened_metadata.ino() != path_metadata.ino()
    {
        return Err(ClientError::Protocol);
    }
    Ok(Some(file))
}

async fn open_trusted_partial(path: &Path) -> Result<tokio::fs::File, ClientError> {
    if let Ok(metadata) = tokio::fs::symlink_metadata(path).await
        && !validate_trusted_metadata(&metadata, metadata.len())
    {
        return Err(ClientError::Protocol);
    }
    let file = tokio::fs::OpenOptions::new()
        .create(true)
        .read(true)
        .append(true)
        .write(true)
        .mode(0o600)
        .custom_flags((rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC).bits() as i32)
        .open(path)
        .await
        .map_err(|_| ClientError::Protocol)?;
    let metadata = file.metadata().await?;
    if !metadata.file_type().is_file()
        || metadata.file_type().is_symlink()
        || metadata.nlink() != 1
        || metadata.uid() != rustix::process::geteuid().as_raw()
        || metadata.mode() & 0o777 != 0o600
    {
        return Err(ClientError::Protocol);
    }
    Ok(file)
}

/// Remove one managed cache object whose content check already failed.
///
/// The caller has proven both the digest mismatch and the exact custody of the
/// file, so this never reaches an unrelated path.  Custody is re-validated
/// immediately before removal, and a file whose ownership, link count, type or
/// mode is not exactly ours is refused rather than deleted: a permission
/// problem, a symlink or an ambiguous hardlink is a failure to report, never a
/// reason to widen what this agent is willing to remove.
async fn quarantine_proven_object(path: &Path, expected_bytes: u64) -> Result<(), ClientError> {
    let metadata = tokio::fs::symlink_metadata(path).await?;
    if !validate_trusted_metadata(&metadata, expected_bytes) {
        return Err(ClientError::Protocol);
    }
    tokio::fs::remove_file(path).await?;
    if let Some(parent) = path.parent() {
        sync_parent(parent).await?;
    }
    Ok(())
}

async fn quarantine_proven_object_matching(
    path: &Path,
    expected_bytes: u64,
    opened: &fs::Metadata,
) -> Result<(), ClientError> {
    let metadata = tokio::fs::symlink_metadata(path).await?;
    if !validate_trusted_metadata(&metadata, expected_bytes)
        || !same_file_metadata(opened, &metadata)
    {
        return Err(ClientError::Protocol);
    }
    quarantine_proven_object(path, expected_bytes).await
}

async fn sync_parent(parent: &Path) -> Result<(), ClientError> {
    tokio::fs::File::open(parent).await?.sync_all().await?;
    Ok(())
}

async fn bounded_body_limit(
    mut response: reqwest::Response,
    maximum_bytes: usize,
) -> Result<Vec<u8>, ClientError> {
    if response
        .content_length()
        .is_some_and(|length| length > maximum_bytes as u64)
    {
        return Err(ClientError::Protocol);
    }
    let mut body = Vec::with_capacity(response.content_length().unwrap_or(0) as usize);
    while let Some(chunk) = response.chunk().await? {
        if body.len().saturating_add(chunk.len()) > maximum_bytes {
            return Err(ClientError::Protocol);
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_node_id(value: &str) -> bool {
    value.len() == 36
        && value.starts_with("spk_")
        && value[4..]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn local_hostname() -> Option<String> {
    let raw = fs::read_to_string("/proc/sys/kernel/hostname").ok()?;
    let hostname = raw.trim();
    valid_reported_hostname(hostname).then(|| hostname.to_owned())
}

fn valid_reported_hostname(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && value.split('.').all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && label
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                && label
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphanumeric)
                && label
                    .as_bytes()
                    .last()
                    .is_some_and(u8::is_ascii_alphanumeric)
        })
}

fn valid_oci_digest(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(valid_sha256)
}

#[cfg(test)]
mod tests {
    use super::{
        AgentHttpClient, AgentResult, ClientError, ExactRecipeRunObservation,
        MAX_REJECTION_CONTEXT_CHARS, controller_rejection_digest, distribution_hash_read_bytes,
        is_rotation_conflict, partial_path, valid_reported_hostname,
    };
    use crate::{
        oci::OciRuntime,
        process::{ProcessError, ProcessOutput, ProcessRunner, Program},
        telemetry::TelemetrySample,
        workloads::CompiledExecutionPlan,
    };
    use chrono::{DateTime, Utc};
    use serde_json::{Value, json};
    use std::{
        collections::HashMap,
        io::{Read, Write},
        net::TcpListener,
        os::unix::fs::PermissionsExt,
        path::{Path, PathBuf},
        sync::Arc,
        thread,
        time::Duration,
    };
    use tokio::sync::RwLock;
    use url::Url;
    use uuid::Uuid;
    use vonk_agent_protocol::generated::{
        AgentClaimPayload, AgentOperation, DistributionObjectKind,
        ExecuteContainerRuntimeRequestOperation, OperationProgress, RecipeImageImportRequest,
    };
    use vonk_agent_protocol::{
        AgentClaim, AgentDirective, AgentProgress, HostHelperContainerRuntimeAction,
        HostHelperGrantClaims, HostHelperGrantSignature, HostHelperOperation, HostRuntimeAction,
        HostRuntimeRequest, RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY, RecipeRunInspectionBinding,
        RecipeRunObservationOutcome, RecipeRunObservationReceipt,
        RecipeRunObservationReceiptClaims, RecipeRunObservationReceiptSignature,
        SignedHostHelperGrant, canonical_json, hex_sha256,
    };

    struct NoProcess;

    #[test]
    fn renewal_conflict_is_distinguished_from_revoked_identity() {
        assert!(is_rotation_conflict(
            br#"{"detail":"a different certificate rotation is already staged"}"#
        ));
        assert!(is_rotation_conflict(
            br#"{"code":"agent.certificate.rotation.conflict"}"#
        ));
        assert!(!is_rotation_conflict(
            br#"{"detail":"agent certificate is not active"}"#
        ));
    }

    impl ProcessRunner for NoProcess {
        fn run(
            &self,
            _: Program,
            _: &[String],
            _: Duration,
        ) -> Result<ProcessOutput, ProcessError> {
            panic!("distribution/install handoff test must not launch a process");
        }
    }

    fn inspection_binding() -> RecipeRunInspectionBinding {
        RecipeRunInspectionBinding {
            artifact_set_digest: "a".repeat(64),
            image_digest: "b".repeat(64),
            installation_id: Uuid::new_v4(),
            local_address: Some("192.168.100.11".parse().unwrap()),
            master_address: Some("192.168.100.10".parse().unwrap()),
            master_port: Some(29500),
            mapping_generation: 4,
            mapping_id: Uuid::new_v4(),
            model_identity: "example/model@immutable".to_owned(),
            port: 8000,
            rank: 1,
            recipe_content_sha256: "c".repeat(64),
            recipe_revision_id: Uuid::new_v4(),
            role: "worker".to_owned(),
            run_id: Uuid::new_v4(),
            run_generation: 3,
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

    fn observation_receipt(
        node_id: &str,
        observation_identity_sha256: &str,
    ) -> RecipeRunObservationReceipt {
        RecipeRunObservationReceipt {
            schema_version: 1,
            claims: RecipeRunObservationReceiptClaims {
                schema_version: 1,
                authority: RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY.to_owned(),
                node_id: node_id.to_owned(),
                request_id: Uuid::new_v4(),
                request_sha256: "f".repeat(64),
                observation_identity_sha256: observation_identity_sha256.to_owned(),
                outcome: RecipeRunObservationOutcome::NotRunning,
                observed_at: Utc::now().timestamp(),
            },
            signature: RecipeRunObservationReceiptSignature {
                algorithm: "ed25519".to_owned(),
                key_id: hex_sha256(&[0; 32]),
                value: "b".repeat(128),
            },
        }
    }

    fn request_capture_client(
        response_status: u16,
        response_headers: Vec<String>,
        response_body: Vec<u8>,
        response_delay: Option<Duration>,
    ) -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            let header_end = loop {
                let size = stream.read(&mut buffer).unwrap();
                assert_ne!(size, 0);
                request.extend_from_slice(&buffer[..size]);
                if let Some(index) = request.windows(4).position(|value| value == b"\r\n\r\n") {
                    break index + 4;
                }
            };
            let headers = std::str::from_utf8(&request[..header_end]).unwrap();
            let content_length = headers
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().unwrap())
                })
                .unwrap();
            while request.len() - header_end < content_length {
                let size = stream.read(&mut buffer).unwrap();
                assert_ne!(size, 0);
                request.extend_from_slice(&buffer[..size]);
            }
            let tolerate_response_write_error = response_delay.is_some();
            if let Some(response_delay) = response_delay {
                thread::sleep(response_delay);
            }
            let response_write = write!(
                stream,
                "HTTP/1.1 {response_status} Test\r\n{}Content-Length: {}\r\nConnection: close\r\n\r\n",
                response_headers
                    .iter()
                    .map(|header| format!("{header}\r\n"))
                    .collect::<String>(),
                response_body.len()
            )
            .and_then(|()| stream.write_all(&response_body));
            if !tolerate_response_write_error {
                response_write.unwrap();
            }
            request
        });
        (
            AgentHttpClient {
                client: Arc::new(RwLock::new(reqwest::Client::new())),
                controller: Url::parse(&format!("http://{address}/")).unwrap(),
                node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
                progress_phase: Default::default(),
                distribution_receipts: Default::default(),
            },
            server,
        )
    }

    #[test]
    fn reported_hostname_is_bounded_dns_syntax() {
        assert!(valid_reported_hostname("spark-3542"));
        assert!(valid_reported_hostname("spark-3542.lab.internal"));
        assert!(!valid_reported_hostname(""));
        assert!(!valid_reported_hostname("-spark"));
        assert!(!valid_reported_hostname("spark_3542"));
        assert!(!valid_reported_hostname(&"a".repeat(256)));
    }

    #[test]
    fn cloned_clients_share_the_rotatable_transport() {
        let client = AgentHttpClient::for_http_test(
            "http://127.0.0.1/",
            "spk_0123456789abcdef0123456789abcdef",
        );
        let operation_client = client.clone();
        assert!(Arc::ptr_eq(&client.client, &operation_client.client));

        let replacement = reqwest::Client::builder().build().unwrap();
        *client.client.try_write().expect("uncontended test client") = replacement;
        assert!(Arc::ptr_eq(&client.client, &operation_client.client));
    }

    #[tokio::test]
    async fn cloned_operation_client_sends_through_replaced_transport() {
        let (client, server) = request_capture_client(204, Vec::new(), Vec::new(), None);
        let operation_client = client.clone();
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(
            "x-rotation-marker",
            reqwest::header::HeaderValue::from_static("fresh"),
        );
        let replacement = reqwest::Client::builder()
            .default_headers(headers)
            .build()
            .unwrap();
        *client.client.try_write().expect("uncontended test client") = replacement;

        operation_client
            .report_telemetry(&[telemetry_sample()])
            .await
            .unwrap();
        let request = server.join().unwrap();
        assert!(
            String::from_utf8_lossy(&request)
                .to_ascii_lowercase()
                .contains("x-rotation-marker: fresh")
        );
    }

    #[tokio::test]
    async fn rotation_drains_requests_without_starving_heartbeats() {
        use std::sync::atomic::{AtomicBool, Ordering};
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        for activation_status in [204, 403] {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}/", listener.local_addr().unwrap());
            let first_received = Arc::new(tokio::sync::Notify::new());
            let release_first = Arc::new(tokio::sync::Notify::new());
            let first_finished = Arc::new(AtomicBool::new(false));
            let server = {
                let first_received = first_received.clone();
                let release_first = release_first.clone();
                let first_finished = first_finished.clone();
                tokio::spawn(async move {
                    let mut handlers = Vec::new();
                    for index in 0..4 {
                        let (mut stream, _) = listener.accept().await.unwrap();
                        let first_received = first_received.clone();
                        let release_first = release_first.clone();
                        let first_finished = first_finished.clone();
                        handlers.push(tokio::spawn(async move {
                            let mut request = Vec::new();
                            let mut buf = [0; 4096];
                            loop {
                                let size = stream.read(&mut buf).await.unwrap();
                                assert_ne!(size, 0);
                                request.extend_from_slice(&buf[..size]);
                                if let Some(end) = request.windows(4).position(|b| b == b"\r\n\r\n") {
                                    let headers = String::from_utf8_lossy(&request[..end]);
                                    let length: usize = headers.lines().find_map(|line| {
                                        let (name, value) = line.split_once(':')?;
                                        name.eq_ignore_ascii_case("content-length")
                                            .then(|| value.trim().parse().unwrap())
                                    }).unwrap();
                                    if request.len() >= end + 4 + length { break; }
                                }
                            }
                            let request = String::from_utf8(request).unwrap().to_ascii_lowercase();
                            let activating = request.starts_with("post /agent/renew/activate ");
                            if index == 0 {
                                first_received.notify_one();
                                release_first.notified().await;
                                first_finished.store(true, Ordering::SeqCst);
                            }
                            let status = if activating {
                                assert!(first_finished.load(Ordering::SeqCst), "activation raced an old request");
                                activation_status
                            } else { 204 };
                            stream.write_all(format!(
                                "HTTP/1.1 {status} Test\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                            ).as_bytes()).await.unwrap();
                            request
                        }));
                    }
                    let mut requests = Vec::new();
                    for handler in handlers {
                        requests.push(handler.await.unwrap());
                    }
                    requests
                })
            };
            let client =
                AgentHttpClient::for_http_test(&url, "spk_0123456789abcdef0123456789abcdef");
            let replacement = AgentHttpClient::for_http_test(&url, client.node_id());
            let mut headers = reqwest::header::HeaderMap::new();
            headers.insert(
                "x-rotation-marker",
                reqwest::header::HeaderValue::from_static("fresh"),
            );
            *replacement.client.try_write().unwrap() = reqwest::Client::builder()
                .default_headers(headers)
                .build()
                .unwrap();
            let operation = client.clone();
            let request = tokio::spawn(async move {
                operation
                    .report_telemetry(&[telemetry_sample()])
                    .await
                    .unwrap();
            });
            first_received.notified().await;
            let rotation = client.activate_replacement(&replacement, 2);
            tokio::pin!(rotation);
            // Poll the real activation while an HTTP response is outstanding.
            assert!(
                tokio::time::timeout(Duration::from_millis(200), &mut rotation)
                    .await
                    .is_err()
            );
            let samples = [telemetry_sample()];
            let probe = client.report_telemetry(&samples);
            tokio::pin!(probe);
            tokio::select! {
                result = &mut rotation => panic!("rotation interrupted an active request: {result:?}"),
                result = &mut probe => result.unwrap(),
                _ = tokio::time::sleep(Duration::from_secs(2)) => panic!("rotation starved the heartbeat"),
            }
            release_first.notify_one();
            request.await.unwrap();
            let result = tokio::time::timeout(Duration::from_secs(2), &mut rotation)
                .await
                .unwrap();
            if activation_status == 204 {
                result.unwrap();
            } else {
                let error = result.unwrap_err();
                assert_eq!(error.status(), Some(403));
                assert!(!error.retryable());
            }
            client
                .report_telemetry(&[telemetry_sample()])
                .await
                .unwrap();
            let requests = server.await.unwrap();
            assert!(!requests[1].contains("x-rotation-marker"));
            assert!(requests[2].starts_with("post /agent/renew/activate "));
            assert!(requests[2].contains("x-rotation-marker: fresh"));
            assert_eq!(
                requests[3].contains("x-rotation-marker: fresh"),
                activation_status == 204
            );
        }
    }

    fn observation_client(status: u16) -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
        request_capture_client(status, Vec::new(), Vec::new(), None)
    }

    #[tokio::test]
    async fn distribution_consumer_fetches_manifest_and_all_objects_with_ranges() {
        let model = b"model payload".to_vec();
        let config = b"config!".to_vec();
        let archive = b"oci archive".to_vec();
        let digest = |bytes: &[u8]| hex_sha256(bytes);
        let model_digest = digest(&model);
        let config_digest = digest(&config);
        let archive_digest = digest(&archive);
        let assignment = vonk_agent_protocol::DistributionAssignment {
            schema_version: 2,
            assignment_id: Uuid::new_v4(),
            plan_digest: "a".repeat(64),
            generation: 1,
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            expires_at: DateTime::parse_from_rfc3339("2030-01-01T00:00:00Z").unwrap(),
            model_artifact_set_sha256: "b".repeat(64),
            objects: vec![
                vonk_agent_protocol::DistributionObject {
                    name: "weights/model.bin".to_owned(),
                    sha256: model_digest.clone(),
                    bytes: model.len() as u64,
                    kind: DistributionObjectKind::Model,
                },
                vonk_agent_protocol::DistributionObject {
                    name: "config/tokenizer.json".to_owned(),
                    sha256: config_digest.clone(),
                    bytes: config.len() as u64,
                    kind: DistributionObjectKind::Model,
                },
                vonk_agent_protocol::DistributionObject {
                    name: "image.oci.tar".to_owned(),
                    sha256: archive_digest.clone(),
                    bytes: archive.len() as u64,
                    kind: DistributionObjectKind::OciArchive,
                },
            ],
            oci_image_digest: format!("sha256:{}", "d".repeat(64)),
            oci_archive_sha256: archive_digest.clone(),
        };
        assignment.validate().unwrap();
        let manifest = canonical_json(&assignment).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let objects = [
                (model_digest, model),
                (config_digest, config),
                (archive_digest, archive),
            ];
            for _ in 0..4 {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = Vec::new();
                let mut buffer = [0_u8; 4096];
                while !request.windows(4).any(|value| value == b"\r\n\r\n") {
                    let size = stream.read(&mut buffer).unwrap();
                    assert!(size > 0);
                    request.extend_from_slice(&buffer[..size]);
                }
                let headers = String::from_utf8_lossy(&request);
                let target = headers
                    .lines()
                    .next()
                    .unwrap()
                    .split_whitespace()
                    .nth(1)
                    .unwrap();
                if target.starts_with("/agent/distribution/manifests/") {
                    write!(
                        stream,
                        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        manifest.len()
                    )
                    .unwrap();
                    stream.write_all(&manifest).unwrap();
                    continue;
                }
                let object_digest = target
                    .strip_prefix("/agent/distribution/objects/")
                    .unwrap()
                    .split('?')
                    .next()
                    .unwrap();
                let body = objects
                    .iter()
                    .find(|(digest, _)| digest == object_digest)
                    .unwrap()
                    .1
                    .as_slice();
                let range = headers.lines().find_map(|line| {
                    line.strip_prefix("range: bytes=")
                        .or_else(|| line.strip_prefix("Range: bytes="))
                });
                let (start, end) = range
                    .unwrap()
                    .split_once('-')
                    .map(|(start, end)| {
                        (
                            start.parse::<usize>().unwrap(),
                            end.parse::<usize>().unwrap(),
                        )
                    })
                    .unwrap();
                let chunk = &body[start..=end];
                write!(stream, "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes {}-{}/{}\r\nETag: \"sha256:{}\"\r\nConnection: close\r\n\r\n", chunk.len(), start, end, body.len(), object_digest).unwrap();
                stream.write_all(chunk).unwrap();
            }
        });
        let client =
            AgentHttpClient::for_http_test(&format!("http://{address}/"), &assignment.node_id);
        let root = tempfile::tempdir().unwrap();
        let evidence = client
            .download_distribution(&assignment.plan_digest, root.path(), root.path())
            .await
            .unwrap();
        assert_eq!(
            std::fs::read(&evidence.model_paths[0]).unwrap(),
            b"model payload"
        );
        assert_eq!(std::fs::read(&evidence.model_paths[1]).unwrap(), b"config!");
        assert_eq!(
            std::fs::read(&evidence.oci_archive_path).unwrap(),
            b"oci archive"
        );
        server.join().unwrap();
    }

    #[tokio::test]
    async fn distribution_downloads_overlap_without_reordering_evidence_or_progress() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        async fn request(stream: &mut tokio::net::TcpStream) -> String {
            let mut bytes = Vec::new();
            while !bytes.ends_with(b"\r\n\r\n") {
                assert!(bytes.len() < 4096);
                bytes.push(stream.read_u8().await.unwrap());
            }
            String::from_utf8(bytes).unwrap()
        }

        let first = b"first model file";
        let second = b"second model file";
        let (archive, image_digest) = oci_archive_fixture();
        let mut assignment = distribution_assignment_fixture(first, &archive, &image_digest);
        assignment.objects.insert(
            1,
            vonk_agent_protocol::DistributionObject {
                name: "weights/second.bin".to_owned(),
                sha256: hex_sha256(second),
                bytes: second.len() as u64,
                kind: DistributionObjectKind::Model,
            },
        );
        assignment.validate().unwrap();
        let manifest = canonical_json(&assignment).unwrap();
        let objects = HashMap::from([
            (hex_sha256(first), first.to_vec()),
            (hex_sha256(second), second.to_vec()),
            (hex_sha256(&archive), archive),
        ]);
        let completion_order = assignment
            .objects
            .iter()
            .rev()
            .map(|object| object.sha256.clone())
            .collect::<Vec<_>>();
        let completed = Arc::new(tokio::sync::Semaphore::new(0));
        let observed = completed.clone();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            assert!(
                request(&mut stream)
                    .await
                    .contains("/agent/distribution/manifests/")
            );
            stream
                .write_all(
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        manifest.len(),
                    )
                    .as_bytes(),
                )
                .await
                .unwrap();
            stream.write_all(&manifest).await.unwrap();
            drop(stream);

            // Withhold bodies until all three requests are in flight. A serial
            // downloader cannot pass this barrier; no throughput threshold is used.
            let mut pending = Vec::new();
            for _ in 0..3 {
                let (mut stream, _) = listener.accept().await.unwrap();
                let headers = request(&mut stream).await;
                pending.push((stream, headers));
            }
            for digest in completion_order {
                let position = pending
                    .iter()
                    .position(|(_, headers)| {
                        headers.contains(&format!("/agent/distribution/objects/{digest}?"))
                    })
                    .unwrap();
                let (mut stream, headers) = pending.swap_remove(position);
                let body = &objects[&digest];
                assert!(
                    headers
                        .to_lowercase()
                        .contains(&format!("range: bytes=0-{}", body.len() - 1))
                );
                stream.write_all(format!(
                    "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes 0-{}/{}\r\nETag: \"sha256:{}\"\r\nConnection: close\r\n\r\n",
                    body.len(), body.len() - 1, body.len(), digest,
                ).as_bytes()).await.unwrap();
                stream.write_all(body).await.unwrap();
                drop(stream);
                // Force reverse verification order before releasing the next
                // body, so completion order cannot accidentally match the manifest.
                observed.acquire().await.unwrap().forget();
            }
        });
        let client =
            AgentHttpClient::for_http_test(&format!("http://{address}/"), &assignment.node_id);
        let root = tempfile::tempdir().unwrap();
        let mut snapshots = Vec::new();
        let mut completed_items = 0;
        let result = tokio::time::timeout(
            Duration::from_secs(10),
            client.download_distribution_with_progress(
                &assignment.plan_digest,
                root.path(),
                root.path(),
                |item| {
                    if item.completed_items > completed_items {
                        assert_eq!(item.completed_items, completed_items + 1);
                        completed_items = item.completed_items;
                        completed.add_permits(1);
                    }
                    snapshots.push(item);
                },
            ),
        )
        .await;
        if result.is_err() {
            server.abort();
        }
        let evidence = result
            .expect("independent distribution requests must overlap")
            .unwrap();
        server.await.unwrap();
        assert_eq!(
            evidence.model_digests,
            vec![hex_sha256(first), hex_sha256(second)]
        );
        assert_eq!(std::fs::read(&evidence.model_paths[0]).unwrap(), first);
        assert_eq!(std::fs::read(&evidence.model_paths[1]).unwrap(), second);
        assert!(
            snapshots
                .windows(2)
                .all(|pair| pair[0].bytes <= pair[1].bytes
                    && pair[0].completed_items <= pair[1].completed_items)
        );
        let last = snapshots.last().unwrap();
        assert_eq!(last.completed_items, assignment.objects.len() as u64);
        assert_eq!(last.bytes, evidence.downloaded_bytes);
    }

    fn oci_archive_fixture() -> (Vec<u8>, String) {
        fn descriptor(media_type: &str, bytes: &[u8]) -> Value {
            json!({
                "mediaType": media_type,
                "digest": format!("sha256:{}", hex_sha256(bytes)),
                "size": bytes.len(),
            })
        }

        let config = br#"{"architecture":"arm64","os":"linux"}"#;
        let layer = b"small arm64 layer";
        let manifest = canonical_json(&json!({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": descriptor("application/vnd.oci.image.config.v1+json", config),
            "layers": [descriptor("application/vnd.oci.image.layer.v1.tar", layer)],
        }))
        .unwrap();
        let manifest_digest = hex_sha256(&manifest);
        let index = canonical_json(&json!({
            "schemaVersion": 2,
            "manifests": [{
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": format!("sha256:{manifest_digest}"),
                "size": manifest.len(),
                "platform": {"architecture": "arm64", "os": "linux"},
            }],
        }))
        .unwrap();
        let layout = br#"{"imageLayoutVersion":"1.0.0"}"#;
        let mut builder = tar::Builder::new(Vec::new());
        for (name, bytes) in [
            ("oci-layout", layout.as_slice()),
            ("index.json", index.as_slice()),
            (
                &format!("blobs/sha256/{}", hex_sha256(config)),
                config.as_slice(),
            ),
            (
                &format!("blobs/sha256/{}", hex_sha256(layer)),
                layer.as_slice(),
            ),
            (
                &format!("blobs/sha256/{manifest_digest}"),
                manifest.as_slice(),
            ),
        ] {
            let mut header = tar::Header::new_gnu();
            header.set_size(bytes.len() as u64);
            header.set_mode(0o644);
            header.set_cksum();
            builder
                .append_data(&mut header, name, bytes)
                .expect("OCI fixture member");
        }
        (
            builder.into_inner().expect("OCI fixture archive"),
            manifest_digest,
        )
    }

    fn distribution_assignment_fixture(
        model: &[u8],
        archive: &[u8],
        image_digest: &str,
    ) -> vonk_agent_protocol::DistributionAssignment {
        let model_object = vonk_agent_protocol::DistributionObject {
            name: "weights/model.bin".to_owned(),
            sha256: hex_sha256(model),
            bytes: model.len() as u64,
            kind: DistributionObjectKind::Model,
        };
        let archive_object = vonk_agent_protocol::DistributionObject {
            name: "image.oci.tar".to_owned(),
            sha256: hex_sha256(archive),
            bytes: archive.len() as u64,
            kind: DistributionObjectKind::OciArchive,
        };
        vonk_agent_protocol::DistributionAssignment {
            schema_version: 2,
            assignment_id: Uuid::new_v4(),
            plan_digest: "a".repeat(64),
            generation: 1,
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            expires_at: DateTime::parse_from_rfc3339("2030-01-01T00:00:00Z").unwrap(),
            model_artifact_set_sha256: "b".repeat(64),
            objects: vec![model_object, archive_object],
            oci_image_digest: format!("sha256:{image_digest}"),
            oci_archive_sha256: hex_sha256(archive),
        }
    }

    #[derive(Clone, Copy)]
    enum DistributionFixtureMode {
        Good,
        WrongEtagFirstObject,
        /// Serve the requested range with the correct length and ETag but
        /// different bytes, so only the content check can reject it.
        WrongBodyFirstObject,
        InterruptFirstObject,
        UnavailableFirstObject,
    }

    fn authenticated_test_client(controller: &str, node_id: &str) -> AgentHttpClient {
        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(
            "x-vonk-fixture-auth",
            reqwest::header::HeaderValue::from_static("enrolled-agent"),
        );
        AgentHttpClient {
            client: Arc::new(RwLock::new(
                reqwest::Client::builder()
                    .default_headers(headers)
                    .build()
                    .unwrap(),
            )),
            controller: Url::parse(controller).unwrap(),
            node_id: node_id.to_owned(),
            progress_phase: Default::default(),
            distribution_receipts: Default::default(),
        }
    }

    fn distribution_fixture_server(
        assignment: vonk_agent_protocol::DistributionAssignment,
        objects: HashMap<String, Vec<u8>>,
        expected_requests: usize,
        mode: DistributionFixtureMode,
    ) -> (AgentHttpClient, thread::JoinHandle<Vec<Vec<u8>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let manifest = canonical_json(&assignment).unwrap();
        let node_id = assignment.node_id.clone();
        let server = thread::spawn(move || {
            let mut requests = Vec::new();
            for _ in 0..expected_requests {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = Vec::new();
                let mut buffer = [0_u8; 4096];
                while !request.windows(4).any(|value| value == b"\r\n\r\n") {
                    let size = stream.read(&mut buffer).unwrap();
                    assert!(size > 0);
                    request.extend_from_slice(&buffer[..size]);
                }
                requests.push(request.clone());
                let headers_end = request
                    .windows(4)
                    .position(|value| value == b"\r\n\r\n")
                    .unwrap()
                    + 4;
                let headers = String::from_utf8_lossy(&request[..headers_end]);
                let target = headers
                    .lines()
                    .next()
                    .unwrap()
                    .split_whitespace()
                    .nth(1)
                    .unwrap();
                let authorized = headers
                    .lines()
                    .any(|line| line.eq_ignore_ascii_case("x-vonk-fixture-auth: enrolled-agent"));
                if !authorized {
                    write!(
                        stream,
                        "HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    .unwrap();
                    continue;
                }
                if target.starts_with("/agent/distribution/manifests/") {
                    assert!(target.ends_with(&assignment.plan_digest));
                    write!(
                        stream,
                        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        manifest.len()
                    )
                    .unwrap();
                    stream.write_all(&manifest).unwrap();
                    continue;
                }
                if matches!(mode, DistributionFixtureMode::UnavailableFirstObject)
                    && requests.len() == 1
                {
                    write!(stream, "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
                    continue;
                }
                let (path, query) = target.split_once('?').unwrap();
                assert!(path.starts_with("/agent/distribution/objects/"));
                assert!(query == format!("plan_digest={}", assignment.plan_digest));
                let digest = path.rsplit('/').next().unwrap();
                let source = objects.get(digest).unwrap();
                let range = headers
                    .lines()
                    .find_map(|line| {
                        line.strip_prefix("range: bytes=")
                            .or_else(|| line.strip_prefix("Range: bytes="))
                    })
                    .unwrap();
                let (start, end) = range
                    .split_once('-')
                    .map(|(start, end)| {
                        (
                            start.parse::<usize>().unwrap(),
                            end.parse::<usize>().unwrap(),
                        )
                    })
                    .unwrap();
                assert!(end >= start && end < source.len());
                assert!(end - start < 8 * 1024 * 1024);
                let body = source[start..=end].to_vec();
                let body = if matches!(mode, DistributionFixtureMode::WrongBodyFirstObject)
                    && digest == assignment.objects[0].sha256
                {
                    // Same length, same range, same ETag: the transfer contract
                    // holds while the bytes are not the requested object, so the
                    // client can only reject this on its own content check.
                    let mut mutated = body;
                    *mutated.first_mut().expect("non-empty object body") ^= 0xff;
                    mutated
                } else {
                    body
                };
                let response_digest =
                    if matches!(mode, DistributionFixtureMode::WrongEtagFirstObject)
                        && digest == assignment.objects[0].sha256
                    {
                        "0".repeat(64)
                    } else {
                        digest.to_owned()
                    };
                write!(
                    stream,
                    "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes {}-{}/{}\r\nETag: \"sha256:{}\"\r\nConnection: close\r\n\r\n",
                    body.len(), start, end, source.len(), response_digest
                )
                .unwrap();
                if matches!(mode, DistributionFixtureMode::InterruptFirstObject)
                    && requests.len() == 1
                {
                    stream.write_all(&body[..5]).unwrap();
                    stream.flush().unwrap();
                    std::thread::sleep(Duration::from_millis(50));
                    continue;
                }
                stream.write_all(&body).unwrap();
            }
            requests
        });
        (
            authenticated_test_client(&format!("http://{address}/"), &node_id),
            server,
        )
    }

    #[tokio::test]
    async fn distribution_acceptance_uses_authenticated_ranges_and_reuses_import_cache() {
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        assignment.validate().unwrap();
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        objects.insert(hex_sha256(&archive), archive.clone());
        let (client, server) = distribution_fixture_server(
            assignment.clone(),
            objects,
            4,
            DistributionFixtureMode::Good,
        );
        let root = tempfile::tempdir().unwrap();
        let assignment_root = root.path().join("distribution").join("plan");
        let archive_root = root.path().join("oci-archives");
        std::fs::create_dir_all(&assignment_root).unwrap();
        let mut snapshots = Vec::new();
        let evidence = client
            .download_distribution_with_progress(
                &assignment.plan_digest,
                &assignment_root,
                &archive_root,
                |item| snapshots.push(item),
            )
            .await
            .unwrap();
        assert!(snapshots.iter().any(|item| item.phase == "copying"));
        assert!(snapshots.iter().any(|item| item.phase == "verifying"));
        assert!(
            snapshots
                .windows(2)
                .all(|pair| pair[0].bytes <= pair[1].bytes)
        );
        let final_progress = snapshots.last().unwrap();
        assert_eq!(final_progress.bytes, evidence.downloaded_bytes);
        assert_eq!(final_progress.completed_items, final_progress.total_items);
        assert_eq!(evidence.oci_image_digest, format!("sha256:{image_digest}"));
        assert_eq!(
            evidence.oci_archive_path,
            archive_root.join(&assignment.oci_archive_sha256)
        );
        assert_eq!(std::fs::read(&evidence.oci_archive_path).unwrap(), archive);
        assert_eq!(std::fs::read(&evidence.model_paths[0]).unwrap(), model);
        assert_eq!(
            std::fs::metadata(&evidence.oci_archive_path)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        assert!(
            !PathBuf::from(format!("{}.partial", evidence.oci_archive_path.display())).exists()
        );
        let importer = crate::image_importer::ImageImporter {
            data_root: root.path(),
        };
        let cached = importer
            .retain_verified_distribution_archive(
                &evidence.oci_archive_sha256,
                &evidence.oci_image_digest,
                evidence.oci_archive_bytes,
                &evidence.oci_archive_path,
            )
            .unwrap();
        assert_eq!(cached, evidence.oci_archive_path);
        let before_retry_hash_reads = distribution_hash_read_bytes();
        let reused_evidence = client
            .download_distribution(&assignment.plan_digest, &assignment_root, &archive_root)
            .await
            .unwrap();
        assert_eq!(
            distribution_hash_read_bytes(),
            before_retry_hash_reads,
            "same-process assignment replay must reuse exact verified-object custody"
        );
        assert_eq!(reused_evidence.downloaded_bytes, evidence.downloaded_bytes);
        let reused = importer
            .retain_verified_distribution_archive(
                &evidence.oci_archive_sha256,
                &evidence.oci_image_digest,
                evidence.oci_archive_bytes,
                &evidence.oci_archive_path,
            )
            .unwrap();
        assert_eq!(cached, reused);
        assert!(
            !archive_root
                .join(format!("{}.partial", assignment.oci_archive_sha256))
                .exists()
        );
        assert!(
            std::fs::read_dir(&archive_root)
                .unwrap()
                .flatten()
                .all(|entry| !entry.file_name().to_string_lossy().contains("partial"))
        );
        assert_eq!(
            importer.distribution_runtime_arguments(
                &evidence.oci_archive_sha256,
                &evidence.oci_image_digest,
                evidence.oci_archive_bytes,
                &cached,
            )[1],
            evidence.oci_archive_sha256
        );
        let requests = server.join().unwrap();
        assert_eq!(requests.len(), 4);
        let archive_gets = requests
            .iter()
            .filter(|request| {
                String::from_utf8_lossy(request).contains(&format!(
                    "/agent/distribution/objects/{}",
                    assignment.oci_archive_sha256
                ))
            })
            .count();
        assert_eq!(archive_gets, 1);
        assert!(requests.iter().all(|request| {
            String::from_utf8_lossy(request)
                .to_ascii_lowercase()
                .contains("x-vonk-fixture-auth: enrolled-agent")
        }));
    }

    #[tokio::test]
    async fn content_addressed_distribution_handoff_reuses_objects_across_plan_digests() {
        let model = b"model payload".to_vec();
        let (archive, _) = oci_archive_fixture();
        let image_digest = "1".repeat(64);
        let mut assignment = distribution_assignment_fixture(&model, &archive, &image_digest);
        assignment.plan_digest = "a".repeat(64);
        assignment.model_artifact_set_sha256 = "d".repeat(64);
        assignment.validate().unwrap();
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(&model), model.clone());
        objects.insert(hex_sha256(&archive), archive.clone());
        let (client, server) = distribution_fixture_server(
            assignment.clone(),
            objects.clone(),
            3,
            DistributionFixtureMode::Good,
        );
        let root = tempfile::tempdir().unwrap();
        let distribution_root = root.path().join("distribution");
        let archive_root = root.path().join("oci-archives");
        std::fs::create_dir_all(&distribution_root).unwrap();
        std::fs::create_dir_all(&archive_root).unwrap();
        let evidence = client
            .download_distribution(&assignment.plan_digest, &distribution_root, &archive_root)
            .await
            .unwrap();
        assert_eq!(server.join().unwrap().len(), 3);
        assert_eq!(
            evidence.model_paths,
            vec![distribution_root.join("models").join(hex_sha256(&model))]
        );
        assert_eq!(
            evidence.oci_archive_path,
            archive_root.join(&assignment.oci_archive_sha256)
        );

        let mut plan_value: Value = serde_json::from_str(include_str!(
            "../../../../agent_protocol/tests/fixtures/compiled-execution-plan-v2.json"
        ))
        .unwrap();
        let model_sha256 = hex_sha256(&model);
        let archive_sha256 = hex_sha256(&archive);
        plan_value["identity"]["execution_sha256"] = json!("e".repeat(64));
        plan_value["identity"]["model_artifact_set_sha256"] =
            json!(assignment.model_artifact_set_sha256.clone());
        plan_value["identity"]["model_artifact_bytes"] = json!(model.len());
        plan_value["artifacts"][0]["sha256"] = json!(model_sha256.clone());
        plan_value["artifacts"][0]["size_bytes"] = json!(model.len());
        plan_value["artifacts"][0]["distribution_object"]["sha256"] = json!(model_sha256);
        plan_value["artifacts"][0]["distribution_object"]["bytes"] = json!(model.len());
        plan_value["runtime"]["image_digest"] = json!(format!("sha256:{image_digest}"));
        plan_value["runtime_image"]["image_digest"] = json!(format!("sha256:{image_digest}"));
        plan_value["runtime_image"]["platform_manifest_digest"] =
            json!(format!("sha256:{image_digest}"));
        plan_value["runtime_image"]["oci_layout_sha256"] = json!(archive_sha256.clone());
        plan_value["runtime_image"]["image_bytes"] = json!(archive.len());
        plan_value["runtime_image"]["local_image_reference"] = json!(format!(
            "localhost/vonk/compiled-runtime-{archive_sha256}@sha256:{image_digest}"
        ));
        plan_value["runtime_image"]["distribution_object"]["sha256"] = json!(archive_sha256);
        plan_value["runtime_image"]["distribution_object"]["bytes"] = json!(archive.len());
        let plan: CompiledExecutionPlan = serde_json::from_value(plan_value).unwrap();
        plan.validate().unwrap();

        let runner = NoProcess;
        let runtime = OciRuntime {
            runner: &runner,
            data_root: root.path(),
            huggingface_curl_config: None,
        };
        let first_installation = "cb555393-764b-4eb6-8f15-b416d289428f";
        let before_install_hash_reads = crate::oci::test_sha256_open_file_call_count();
        runtime
            .install(
                &plan,
                first_installation,
                &plan.identity.recipe_revision_sha256,
            )
            .unwrap();
        assert_eq!(
            crate::oci::test_sha256_open_file_call_count(),
            before_install_hash_reads,
            "fresh installation hashes the source while copying it"
        );
        assert_eq!(
            std::fs::read(root.path().join(format!(
                "installations/{first_installation}/models/primary/weights.bin"
            )))
            .unwrap(),
            model
        );
        assert!(archive_root.join(&archive_sha256).is_file());

        let before_reinstall = crate::oci::test_sha256_open_file_call_count();
        runtime
            .install(
                &plan,
                first_installation,
                &plan.identity.recipe_revision_sha256,
            )
            .unwrap();
        assert_eq!(
            crate::oci::test_sha256_open_file_call_count(),
            before_reinstall,
            "reinstall reuses the receipt-bound destination without rereading the source"
        );

        let mut second_assignment = assignment.clone();
        second_assignment.plan_digest = "f".repeat(64);
        second_assignment.model_artifact_set_sha256 = "c".repeat(64);
        let (second_client, second_server) = distribution_fixture_server(
            second_assignment.clone(),
            objects,
            1,
            DistributionFixtureMode::Good,
        );
        let reused = second_client
            .download_distribution(
                &second_assignment.plan_digest,
                &distribution_root,
                &archive_root,
            )
            .await
            .unwrap();
        assert_eq!(reused.downloaded_bytes, evidence.downloaded_bytes);
        assert_eq!(
            reused.model_paths,
            vec![distribution_root.join("models").join(&model_sha256)]
        );
        assert_eq!(
            std::fs::read_dir(distribution_root.join("models"))
                .unwrap()
                .count(),
            1,
            "the same object is retained once across artifact sets"
        );
        assert_eq!(second_server.join().unwrap().len(), 1);

        let mut second_plan = plan.clone();
        second_plan.identity.execution_sha256 = "f".repeat(64);
        second_plan.identity.model_artifact_set_sha256 =
            second_assignment.model_artifact_set_sha256.clone();
        let second_installation = "cb555393-764b-4eb6-8f15-b416d2894290";
        runtime
            .install(
                &second_plan,
                second_installation,
                &second_plan.identity.recipe_revision_sha256,
            )
            .unwrap();
        assert_eq!(
            std::fs::read(root.path().join(format!(
                "installations/{second_installation}/models/primary/weights.bin"
            )))
            .unwrap(),
            model
        );
    }

    #[tokio::test]
    async fn distribution_acceptance_resumes_partial_object_and_rejects_corruption() {
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        objects.insert(hex_sha256(&archive), archive.clone());
        let root = tempfile::tempdir().unwrap();
        let model_path = root
            .path()
            .join("models")
            .join(&assignment.objects[0].sha256);
        let partial_path = PathBuf::from(format!("{}.partial", model_path.display()));
        std::fs::create_dir_all(model_path.parent().unwrap()).unwrap();
        std::fs::write(&partial_path, &model[..5]).unwrap();
        std::fs::set_permissions(&partial_path, std::fs::Permissions::from_mode(0o600)).unwrap();
        let (client, server) = distribution_fixture_server(
            assignment.clone(),
            objects,
            3,
            DistributionFixtureMode::Good,
        );
        client
            .download_distribution(&assignment.plan_digest, root.path(), root.path())
            .await
            .unwrap();
        assert_eq!(std::fs::read(&model_path).unwrap(), model);
        let requests = server.join().unwrap();
        let model_request = requests
            .iter()
            .map(|request| String::from_utf8_lossy(request).to_ascii_lowercase())
            .find(|request| {
                request.contains(&format!(
                    "/agent/distribution/objects/{}?",
                    assignment.objects[0].sha256
                ))
            })
            .unwrap();
        assert!(model_request.contains("range: bytes=5-"));

        let corrupt_root = tempfile::tempdir().unwrap();
        // Keep the unrelated archive cached so this fixture can serve just
        // the manifest and bad model response, independent of request order.
        let cached_archive = corrupt_root.path().join(&assignment.oci_archive_sha256);
        std::fs::write(&cached_archive, &archive).unwrap();
        std::fs::set_permissions(&cached_archive, std::fs::Permissions::from_mode(0o600)).unwrap();
        let (corrupt_client, corrupt_server) = distribution_fixture_server(
            assignment.clone(),
            {
                let mut values = HashMap::new();
                values.insert(hex_sha256(model), model.to_vec());
                values.insert(hex_sha256(&archive), archive);
                values
            },
            2,
            DistributionFixtureMode::WrongEtagFirstObject,
        );
        assert!(matches!(
            corrupt_client
                .download_distribution(
                    &assignment.plan_digest,
                    corrupt_root.path(),
                    corrupt_root.path(),
                )
                .await,
            Err(ClientError::Protocol)
        ));
        assert_eq!(corrupt_server.join().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn direct_distribution_object_resumes_private_partial_atomically() {
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        objects.insert(hex_sha256(&archive), archive);
        let (client, server) = distribution_fixture_server(
            assignment.clone(),
            objects,
            1,
            DistributionFixtureMode::Good,
        );
        let root = tempfile::tempdir().unwrap();
        let destination = root.path().join("config.json");
        let partial = partial_path(&destination);
        std::fs::write(&partial, &model[..5]).unwrap();
        std::fs::set_permissions(&partial, std::fs::Permissions::from_mode(0o600)).unwrap();
        client
            .download_distribution_object(
                &assignment.plan_digest,
                &assignment.objects[0].sha256,
                model.len() as u64,
                &destination,
            )
            .await
            .unwrap();
        assert_eq!(std::fs::read(&destination).unwrap(), model);
        assert!(!partial.exists());
        assert_eq!(server.join().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn direct_distribution_repairs_a_corrupt_owned_object_from_the_source() {
        // A cache object can carry the exact digest name, the exact expected
        // length and exact custody and still hold the wrong bytes.  Leaving it
        // in place made every later attempt re-report the mismatch without ever
        // transferring the object again, so the cache stayed poisoned forever.
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        objects.insert(hex_sha256(&archive), archive);
        let (client, server) = distribution_fixture_server(
            assignment.clone(),
            objects,
            1,
            DistributionFixtureMode::Good,
        );
        let root = tempfile::tempdir().unwrap();
        let destination = root.path().join("config.json");
        let mut corrupt = model.to_vec();
        corrupt[0] ^= 0xff;
        std::fs::write(&destination, &corrupt).unwrap();
        std::fs::set_permissions(&destination, std::fs::Permissions::from_mode(0o600)).unwrap();

        client
            .download_distribution_object(
                &assignment.plan_digest,
                &assignment.objects[0].sha256,
                model.len() as u64,
                &destination,
            )
            .await
            .unwrap();

        assert_eq!(std::fs::read(&destination).unwrap(), model);
        assert!(!partial_path(&destination).exists());
        assert_eq!(server.join().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn distribution_receipt_invalidates_on_same_size_mutation() {
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        objects.insert(hex_sha256(&archive), archive);
        let (client, server) = distribution_fixture_server(
            assignment.clone(),
            objects,
            2,
            DistributionFixtureMode::Good,
        );
        let root = tempfile::tempdir().unwrap();
        let destination = root.path().join("config.json");
        let digest = &assignment.objects[0].sha256;
        client
            .download_distribution_object(
                &assignment.plan_digest,
                digest,
                model.len() as u64,
                &destination,
            )
            .await
            .unwrap();
        let mut corrupt = model.to_vec();
        corrupt[0] ^= 0xff;
        std::fs::write(&destination, &corrupt).unwrap();
        client
            .download_distribution_object(
                &assignment.plan_digest,
                digest,
                model.len() as u64,
                &destination,
            )
            .await
            .unwrap();
        assert_eq!(std::fs::read(destination).unwrap(), model);
        assert_eq!(server.join().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn distribution_rejects_partial_replacement_after_stream_hash() {
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        objects.insert(hex_sha256(&archive), archive);
        let (client, server) = distribution_fixture_server(
            assignment.clone(),
            objects,
            1,
            DistributionFixtureMode::Good,
        );
        let root = tempfile::tempdir().unwrap();
        let destination = root.path().join("config.json");
        let partial = partial_path(&destination);
        let replacement = root.path().join("replacement");
        let mut corrupt = model.to_vec();
        corrupt[0] ^= 0xff;
        let mut swapped = false;
        let result = client
            .download_trusted_distribution_object_with_progress(
                &assignment.plan_digest,
                &assignment.objects[0].sha256,
                model.len() as u64,
                &destination,
                root.path(),
                |_, phase| {
                    if phase == "verifying" && !swapped {
                        std::fs::write(&replacement, &corrupt).unwrap();
                        std::fs::set_permissions(
                            &replacement,
                            std::fs::Permissions::from_mode(0o600),
                        )
                        .unwrap();
                        std::fs::rename(&replacement, &partial).unwrap();
                        swapped = true;
                    }
                },
            )
            .await;
        assert!(matches!(result, Err(ClientError::Protocol)));
        assert!(!destination.exists());
        assert_eq!(server.join().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn direct_distribution_discards_bytes_that_fail_their_content_check() {
        // The received bytes are proven before they take the digest name.  A
        // file renamed with the digest name first and checked afterwards would
        // keep that name after the mismatch and poison later attempts.
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        objects.insert(hex_sha256(&archive), archive);
        let (client, server) = distribution_fixture_server(
            assignment.clone(),
            objects,
            1,
            DistributionFixtureMode::WrongBodyFirstObject,
        );
        let root = tempfile::tempdir().unwrap();
        let destination = root.path().join("config.json");

        assert!(matches!(
            client
                .download_distribution_object(
                    &assignment.plan_digest,
                    &assignment.objects[0].sha256,
                    model.len() as u64,
                    &destination,
                )
                .await,
            Err(ClientError::Protocol)
        ));

        assert!(!destination.exists());
        assert!(!partial_path(&destination).exists());
        assert_eq!(server.join().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn direct_distribution_refuses_an_object_whose_custody_is_not_exactly_ours() {
        // A symlink at the digest name is a failure to report, never a reason
        // to widen what this agent removes: the linked file must survive.
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        objects.insert(hex_sha256(&archive), archive);
        let (client, server) = distribution_fixture_server(
            assignment.clone(),
            objects,
            0,
            DistributionFixtureMode::Good,
        );
        let root = tempfile::tempdir().unwrap();
        let linked = root.path().join("linked");
        let mut corrupt = model.to_vec();
        corrupt[0] ^= 0xff;
        std::fs::write(&linked, &corrupt).unwrap();
        std::fs::set_permissions(&linked, std::fs::Permissions::from_mode(0o600)).unwrap();
        let destination = root.path().join("config.json");
        std::os::unix::fs::symlink(&linked, &destination).unwrap();

        assert!(matches!(
            client
                .download_distribution_object(
                    &assignment.plan_digest,
                    &assignment.objects[0].sha256,
                    model.len() as u64,
                    &destination,
                )
                .await,
            Err(ClientError::Protocol)
        ));

        assert!(
            std::fs::symlink_metadata(&destination)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert_eq!(std::fs::read(&linked).unwrap(), corrupt);
        assert_eq!(server.join().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn direct_distribution_retries_interrupted_body_from_appended_offset() {
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        let (client, server) = distribution_fixture_server(
            assignment.clone(),
            objects,
            2,
            DistributionFixtureMode::InterruptFirstObject,
        );
        let root = tempfile::tempdir().unwrap();
        let destination = root.path().join("image.tar");
        let mut updates = Vec::new();
        client
            .download_trusted_distribution_object_with_progress(
                &assignment.plan_digest,
                &hex_sha256(model),
                model.len() as u64,
                &destination,
                root.path(),
                |bytes, phase| updates.push((bytes, phase)),
            )
            .await
            .unwrap();
        assert_eq!(std::fs::read(&destination).unwrap(), model);
        assert!(!partial_path(&destination).exists());
        let requests = server.join().unwrap();
        assert!(
            String::from_utf8_lossy(&requests[1])
                .to_lowercase()
                .contains("range: bytes=5-")
        );
        assert!(updates.windows(2).all(|pair| pair[0].0 <= pair[1].0));
        assert_eq!(updates.last(), Some(&(model.len() as u64, "verifying")));
    }

    #[tokio::test]
    async fn direct_distribution_retries_service_unavailability_but_not_bad_identity() {
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        for (mode, request_count, succeeds) in [
            (DistributionFixtureMode::UnavailableFirstObject, 2, true),
            (DistributionFixtureMode::WrongEtagFirstObject, 1, false),
        ] {
            let mut objects = HashMap::new();
            objects.insert(hex_sha256(model), model.to_vec());
            let (client, server) =
                distribution_fixture_server(assignment.clone(), objects, request_count, mode);
            let root = tempfile::tempdir().unwrap();
            let result = client
                .download_distribution_object(
                    &assignment.plan_digest,
                    &hex_sha256(model),
                    model.len() as u64,
                    &root.path().join("image.tar"),
                )
                .await;
            if succeeds {
                result.unwrap();
            } else {
                assert!(matches!(result, Err(ClientError::Protocol)));
            }
            assert_eq!(server.join().unwrap().len(), request_count);
        }
    }

    #[test]
    fn trusted_partial_paths_keep_same_stem_objects_distinct() {
        let json = Path::new("/tmp/config.json");
        let yaml = Path::new("/tmp/config.yaml");
        assert_ne!(partial_path(json), partial_path(yaml));
        assert_eq!(
            partial_path(json),
            PathBuf::from("/tmp/config.json.partial")
        );
    }

    #[tokio::test]
    async fn distribution_acceptance_rejects_an_unauthorized_destination() {
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        objects.insert(hex_sha256(&archive), archive.clone());
        let (authorized_client, authorized_server) = distribution_fixture_server(
            assignment.clone(),
            objects.clone(),
            1,
            DistributionFixtureMode::Good,
        );
        let unauthorized_client = AgentHttpClient::for_http_test(
            authorized_client.controller.as_str(),
            &assignment.node_id,
        );
        assert!(matches!(
            unauthorized_client
                .distribution_manifest(&assignment.plan_digest)
                .await,
            Err(ClientError::Controller(error)) if error.status == 401
        ));
        assert_eq!(authorized_server.join().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn distribution_acceptance_repairs_a_corrupt_owned_archive_from_the_source() {
        // This used to stay a permanent failure.  A corrupt object at its exact
        // digest name with exact custody is repairable inside the managed cache,
        // so the authorized assignment rehydrates it instead of leaving every
        // later acceptance run blocked on the same object.
        let model = b"small model object";
        let (archive, image_digest) = oci_archive_fixture();
        let assignment = distribution_assignment_fixture(model, &archive, &image_digest);
        let mut objects = HashMap::new();
        objects.insert(hex_sha256(model), model.to_vec());
        objects.insert(hex_sha256(&archive), archive.clone());
        let root = tempfile::tempdir().unwrap();
        let model_path = root
            .path()
            .join("models")
            .join(&assignment.objects[0].sha256);
        std::fs::create_dir_all(model_path.parent().unwrap()).unwrap();
        std::fs::write(&model_path, model).unwrap();
        std::fs::set_permissions(&model_path, std::fs::Permissions::from_mode(0o600)).unwrap();
        let archive_root = root.path().join("oci-archives");
        let destination = archive_root.join(&assignment.oci_archive_sha256);
        std::fs::create_dir_all(&archive_root).unwrap();
        std::fs::write(&destination, vec![0_u8; archive.len()]).unwrap();
        std::fs::set_permissions(&destination, std::fs::Permissions::from_mode(0o600)).unwrap();
        let (client, server) = distribution_fixture_server(
            assignment.clone(),
            objects,
            2,
            DistributionFixtureMode::Good,
        );

        client
            .download_distribution(&assignment.plan_digest, root.path(), &archive_root)
            .await
            .unwrap();

        assert_eq!(std::fs::read(&destination).unwrap(), archive);
        assert_eq!(std::fs::read(&model_path).unwrap(), model);
        assert_eq!(server.join().unwrap().len(), 2);
    }

    fn job_input_client(
        declared_bytes: usize,
        body: Vec<u8>,
    ) -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            while !request.windows(4).any(|value| value == b"\r\n\r\n") {
                let read = stream.read(&mut buffer).unwrap();
                assert_ne!(read, 0);
                request.extend_from_slice(&buffer[..read]);
            }
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {declared_bytes}\r\nConnection: close\r\n\r\n"
            )
            .unwrap();
            stream.write_all(&body).unwrap();
            request
        });
        (
            AgentHttpClient {
                client: Arc::new(RwLock::new(reqwest::Client::new())),
                controller: Url::parse(&format!("http://{address}/")).unwrap(),
                node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
                progress_phase: Default::default(),
                distribution_receipts: Default::default(),
            },
            server,
        )
    }

    fn telemetry_sample() -> TelemetrySample {
        serde_json::from_value(serde_json::json!({
            "boot_id": "00000000-0000-4000-8000-000000000001",
            "observed_at": "2026-08-15T12:00:01Z",
            "cpu_utilization_percent": 12.5,
            "load_average_1m": 1.25,
            "memory_total_bytes": 128000000000_u64,
            "memory_available_bytes": 64000000000_u64,
            "disk_total_bytes": 1000000000000_u64,
            "disk_free_bytes": 750000000000_u64,
            "gpu_utilization_percent": null,
            "gpu_memory_total_bytes": 128000000000_u64,
            "gpu_memory_free_bytes": 63000000000_u64,
            "temperature_c": 41.5,
            "power_watts": 17.25,
            "network_receive_bytes_per_second": 1024.5,
            "network_transmit_bytes_per_second": 512.25,
            "gap_samples": 0,
            "details": {
                "accelerator_name": "NVIDIA GB10",
                "accelerator_performance_state": null
            },
            "metrics": {
                "schema_version": 2,
                "series": [],
                "capabilities": [],
                "runtimes": [],
                "workloads": [],
                "provenance": {
                    "collector": "test",
                    "collector_version": "1"
                }
            }
        }))
        .unwrap()
    }

    fn heartbeat_client(
        response: AgentDirective,
    ) -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
        let response_body = canonical_json(&response).unwrap();
        request_capture_client(
            200,
            vec!["Content-Type: application/json".to_owned()],
            response_body,
            None,
        )
    }

    fn host_runtime_grant_client() -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
        request_capture_client(
            200,
            vec!["Content-Type: application/json".to_owned()],
            br#"{"grant":{"claims":{"authority":"vonk.host-maintenance-helper","expires_at":2100000010,"issued_at":2100000000,"node_id":"spk_0123456789abcdef0123456789abcdef","operation":{"action":"image-import","attempt":1,"fence":"44d4e914-34df-4962-a802-d1f7dcd928aa","job_id":"84ddf214-f067-4bbf-917e-95df32a07fd8","operation_id":"f450b5ac-5a78-4af5-9670-e874f735e3ee","request_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","type":"execute-container-runtime-request"},"request_id":"84ddf214-f067-4bbf-917e-95df32a07fd8","schema_version":1},"schema_version":1,"signature":{"algorithm":"ed25519","key_id":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","value":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}}}"#.to_vec(),
            None,
        )
    }

    fn delayed_upload_client(
        response_delay: Duration,
    ) -> (AgentHttpClient, thread::JoinHandle<Vec<u8>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let base_client = authenticated_test_client(&format!("http://{address}"), "spk_test");
        let server = thread::spawn(move || {
            for offset in [0, 9] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = Vec::new();
                let mut buffer = [0u8; 1024];
                while !request.windows(4).any(|part| part == b"\r\n\r\n") {
                    let size = stream.read(&mut buffer).unwrap();
                    assert!(size > 0);
                    request.extend_from_slice(&buffer[..size]);
                }
                assert!(request.starts_with(b"HEAD "));
                write!(stream,"HTTP/1.1 200 OK\r\nx-vonk-upload-offset: {offset}\r\nx-vonk-upload-complete: false\r\nConnection: close\r\n\r\n").unwrap();
                drop(stream);
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = Vec::new();
                loop {
                    let size = stream.read(&mut buffer).unwrap();
                    assert!(size > 0);
                    request.extend_from_slice(&buffer[..size]);
                    if request.ends_with(b"archive") {
                        break;
                    }
                }
                if offset == 0 {
                    // The receiver persisted only nine bytes before losing the connection.
                    // No response reaches the sender; its retry must discover the cursor.
                    assert!(request.ends_with(b"accepted archive"));
                    drop(stream);
                    continue;
                }
                thread::sleep(response_delay);
                stream
                    .write_all(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
                    .unwrap();
                return request;
            }
            unreachable!()
        });
        let http_client = reqwest::Client::builder()
            .timeout(Duration::from_millis(50))
            .build()
            .unwrap();
        (
            AgentHttpClient {
                client: Arc::new(RwLock::new(http_client)),
                controller: base_client.controller,
                node_id: base_client.node_id,
                progress_phase: Default::default(),
                distribution_receipts: Default::default(),
            },
            server,
        )
    }

    fn progress() -> AgentProgress {
        AgentProgress {
            attempt: 2,
            deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:00+00:00").unwrap(),
            fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
            job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
            progress: Some(OperationProgress {
                phase: "executing".to_owned(),
                completed_bytes: 21,
                total_bytes: Some(42),
                total_bytes_known: true,
                completed_items: Some(1),
                total_items: Some(2),
                object_sha256: None,
                kind: None,
                activity: None,
                observed_at: None,
                last_progress_at: None,
                bytes_per_second: None,
                smoothed_bytes_per_second: None,
                eta_seconds: None,
                elapsed_seconds: None,
                checkpoint: None,
                members: Vec::new(),
            }),
            schema_version: 1,
        }
    }

    #[tokio::test]
    async fn heartbeat_posts_exact_progress_and_accepts_matching_renewal() {
        let progress = progress();
        let directive = AgentDirective {
            attempt: progress.attempt,
            cancel_requested: false,
            deadline: progress.deadline + chrono::Duration::seconds(30),
            fence: progress.fence,
            job_id: progress.job_id,
            node_id: progress.node_id.clone(),
            operation_id: progress.operation_id,
            schema_version: progress.schema_version,
        };
        let (client, server) = heartbeat_client(directive.clone());

        assert_eq!(client.heartbeat(&progress).await.unwrap(), directive);
        let request = server.join().unwrap();
        let (headers, body) = request
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .map(|index| (&request[..index], &request[index + 4..]))
            .unwrap();
        assert!(
            std::str::from_utf8(headers)
                .unwrap()
                .starts_with("POST /agent/heartbeat HTTP/1.1\r\n")
        );
        assert_eq!(
            serde_json::from_slice::<AgentProgress>(body).unwrap(),
            progress
        );
        assert_eq!(
            serde_json::from_slice::<AgentProgress>(body)
                .unwrap()
                .progress
                .unwrap()
                .total_bytes,
            Some(42)
        );
    }

    #[tokio::test]
    async fn ordinary_heartbeat_preserves_active_upload_counters() {
        let mut progress = progress();
        progress.progress.as_mut().unwrap().phase = "executing".to_owned();
        let directive = AgentDirective {
            attempt: progress.attempt,
            cancel_requested: false,
            deadline: progress.deadline + chrono::Duration::seconds(30),
            fence: progress.fence,
            job_id: progress.job_id,
            node_id: progress.node_id.clone(),
            operation_id: progress.operation_id,
            schema_version: progress.schema_version,
        };
        let (client, server) = heartbeat_client(directive);
        client.set_progress_phase(progress.operation_id, "uploading");
        client.set_progress_bytes(progress.operation_id, 512, 1024);
        client.heartbeat(&progress).await.unwrap();
        let request = server.join().unwrap();
        let start = request
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .unwrap()
            + 4;
        let received = vonk_agent_protocol::parse_strict::<AgentProgress>(&request[start..])
            .unwrap()
            .progress
            .unwrap();
        assert_eq!(received.phase, "uploading");
        assert_eq!(received.completed_bytes, 512);
        assert_eq!(received.total_bytes, Some(1024));
    }

    #[tokio::test]
    async fn lease_only_heartbeat_omits_measured_progress() {
        let mut progress = progress();
        progress.progress = None;
        let directive = AgentDirective {
            attempt: progress.attempt,
            cancel_requested: false,
            deadline: progress.deadline + chrono::Duration::seconds(30),
            fence: progress.fence,
            job_id: progress.job_id,
            node_id: progress.node_id.clone(),
            operation_id: progress.operation_id,
            schema_version: progress.schema_version,
        };
        let (client, server) = heartbeat_client(directive.clone());

        assert_eq!(client.heartbeat(&progress).await.unwrap(), directive);
        let request = server.join().unwrap();
        let body = request
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .map(|index| &request[index + 4..])
            .unwrap();
        let document: Value = serde_json::from_slice(body).unwrap();
        assert!(!document.as_object().unwrap().contains_key("progress"));
        assert_eq!(
            serde_json::from_slice::<AgentProgress>(body).unwrap(),
            progress
        );
    }

    #[tokio::test]
    async fn heartbeat_rejects_legacy_progress_response_shape() {
        let progress = progress();
        let (client, server) = request_capture_client(
            200,
            vec!["Content-Type: application/json".to_owned()],
            canonical_json(&progress).unwrap(),
            None,
        );

        assert!(matches!(
            client.heartbeat(&progress).await,
            Err(ClientError::Protocol)
        ));
        server.join().unwrap();
    }

    #[tokio::test]
    async fn heartbeat_rejects_mismatched_or_regressing_renewal() {
        let progress = progress();
        let directive = AgentDirective {
            attempt: progress.attempt,
            cancel_requested: false,
            deadline: progress.deadline - chrono::Duration::seconds(1),
            fence: progress.fence,
            job_id: progress.job_id,
            node_id: progress.node_id.clone(),
            operation_id: progress.operation_id,
            schema_version: progress.schema_version,
        };
        let (client, server) = heartbeat_client(directive);

        assert!(matches!(
            client.heartbeat(&progress).await,
            Err(ClientError::Protocol)
        ));
        server.join().unwrap();
    }

    #[tokio::test]
    async fn host_runtime_grant_ttl_fits_inside_renewed_operation_lease() {
        let payload = RecipeImageImportRequest {
            build_id: Uuid::new_v4(),
            image_bytes: 1,
            image_digest: format!("sha256:{}", "b".repeat(64)),
            kind: "image.import".to_owned(),
            mapping_generation: 1,
            mapping_id: Uuid::new_v4(),
            oci_layout_sha256: "c".repeat(64),
            schema_version: 1,
            source_node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
        };
        let payload_digest = hex_sha256(&canonical_json(&payload).unwrap());
        let claim = AgentClaim {
            schema_version: 1,
            job_id: Uuid::parse_str("84ddf214-f067-4bbf-917e-95df32a07fd8").unwrap(),
            operation_id: Uuid::parse_str("f450b5ac-5a78-4af5-9670-e874f735e3ee").unwrap(),
            attempt: 1,
            fence: Uuid::parse_str("44d4e914-34df-4962-a802-d1f7dcd928aa").unwrap(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            operation: AgentOperation::RecipeImageImportV1,
            authority_revision: "a".repeat(64),
            payload_digest,
            payload: AgentClaimPayload::RecipeImageImportRequest(payload),
            deadline: DateTime::parse_from_rfc3339("2099-01-01T00:00:00+00:00").unwrap(),
        };
        let (client, server) = host_runtime_grant_client();

        client
            .host_runtime_grant(
                &claim,
                HostRuntimeAction::ImageImport,
                &"c".repeat(64),
                None,
            )
            .await
            .unwrap();
        let request = server.join().unwrap();
        let body = request
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .map(|index| &request[index + 4..])
            .unwrap();
        let body: serde_json::Value = serde_json::from_slice(body).unwrap();

        assert_eq!(body["expires_in_seconds"], 10);
    }

    #[tokio::test]
    async fn exact_inspection_grant_binds_fresh_envelope_and_full_identity() {
        let binding = inspection_binding();
        let request = HostRuntimeRequest {
            schema_version: 1,
            action: HostRuntimeAction::RunInspect,
            job_id: binding.run_id,
            operation_id: Uuid::new_v4(),
            attempt: binding.run_generation,
            fence: Uuid::new_v4(),
            arguments: vec![format!("sha256:{}", binding.image_digest), "run".to_owned()],
            observation: Some(binding.clone()),
            installation_id: None,
        };
        let digest = hex_sha256(&canonical_json(&request).unwrap());
        let request_id = Uuid::new_v4();
        let response = serde_json::to_vec(&serde_json::json!({
            "schema_version": 1,
            "observation_identity_sha256": "e".repeat(64),
            "grant": {
                "schema_version": 1,
                "claims": {
                    "schema_version": 1,
                    "authority": "vonk.host-maintenance-helper",
                    "request_id": request_id,
                    "node_id": "spk_0123456789abcdef0123456789abcdef",
                    "issued_at": 1_788_000_000,
                    "expires_at": 1_788_000_010,
                    "operation": {
                        "type": "execute-container-runtime-request",
                        "action": "run-inspect",
                        "job_id": binding.run_id,
                        "operation_id": request.operation_id,
                        "attempt": request.attempt,
                        "fence": request.fence,
                        "request_sha256": digest,
                        "observation_identity_sha256": "e".repeat(64)
                    }
                },
                "signature": {
                    "algorithm": "ed25519",
                    "key_id": "f".repeat(64),
                    "value": "e".repeat(128)
                }
            }
        }))
        .unwrap();
        let (client, server) = request_capture_client(200, vec![], response, None);

        client
            .recipe_run_inspection_grant(&binding, &request, &digest)
            .await
            .unwrap();
        let raw = server.join().unwrap();
        let (headers, body) = raw
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .map(|index| (&raw[..index], &raw[index + 4..]))
            .unwrap();
        assert!(
            std::str::from_utf8(headers)
                .unwrap()
                .starts_with("POST /agent/recipe-runs/observation-grants HTTP/1.1\r\n")
        );
        let body: serde_json::Value = serde_json::from_slice(body).unwrap();
        assert_eq!(body["node_id"], client.node_id);
        assert_eq!(body["job_id"], binding.run_id.to_string());
        assert_eq!(body["attempt"], binding.run_generation);
        assert_eq!(body["run_generation"], binding.run_generation);
        assert_eq!(body["request_sha256"], digest);
        assert_eq!(body["expires_in_seconds"], 10);

        let (client, server) = request_capture_client(425, vec![], vec![], None);
        assert!(matches!(
            client
                .recipe_run_inspection_grant(&binding, &request, &digest)
                .await,
            Err(ClientError::ObservationNotReady)
        ));
        server.join().unwrap();

        // An outstanding unconsumed grant is the same bounded "not yet" as the
        // 425 above: the run is authorized, its inspection is just in flight.
        let (client, server) = request_capture_client(
            409,
            vec!["x-vonk-error-code: controller.recipe_run.observation_pending".to_owned()],
            vec![],
            None,
        );
        assert!(matches!(
            client
                .recipe_run_inspection_grant(&binding, &request, &digest)
                .await,
            Err(ClientError::ObservationNotReady)
        ));
        server.join().unwrap();

        // A conflict the controller did not classify as pending stays a hard
        // failure: an authority rejection must never read as a retry.
        let (client, server) = request_capture_client(
            409,
            vec!["x-vonk-error-code: controller.conflict".to_owned()],
            vec![],
            None,
        );
        assert!(matches!(
            client
                .recipe_run_inspection_grant(&binding, &request, &digest)
                .await,
            Err(ClientError::Controller(_))
        ));
        server.join().unwrap();
    }

    #[tokio::test]
    async fn exact_worker_observation_reports_process_without_endpoint_result() {
        let binding = inspection_binding();
        let helper_receipt =
            observation_receipt("spk_0123456789abcdef0123456789abcdef", &"e".repeat(64));
        let observations = vec![ExactRecipeRunObservation {
            schema_version: 1,
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            observed_at: chrono::DateTime::from_timestamp(helper_receipt.claims.observed_at, 0)
                .unwrap()
                .into(),
            artifact_set_digest: binding.artifact_set_digest.clone(),
            image_digest: binding.image_digest.clone(),
            installation_id: binding.installation_id,
            local_address: binding.local_address,
            mapping_generation: binding.mapping_generation,
            mapping_id: binding.mapping_id,
            master_address: binding.master_address,
            master_port: binding.master_port,
            model_identity: binding.model_identity.clone(),
            port: binding.port,
            rank: binding.rank,
            recipe_content_sha256: binding.recipe_content_sha256.clone(),
            recipe_revision_id: binding.recipe_revision_id,
            role: binding.role.clone(),
            run_generation: binding.run_generation,
            run_id: binding.run_id,
            runtime_arguments_sha256: binding.runtime_arguments_sha256.clone(),
            world_size: binding.world_size,
            endpoint_ready: None,
            grant: SignedHostHelperGrant {
                schema_version: 1,
                claims: HostHelperGrantClaims {
                    schema_version: 1,
                    authority: "vonk.host-maintenance-helper".to_owned(),
                    request_id: helper_receipt.claims.request_id,
                    node_id: helper_receipt.claims.node_id.clone(),
                    issued_at: helper_receipt.claims.observed_at - 1,
                    expires_at: helper_receipt.claims.observed_at + 60,
                    operation: HostHelperOperation::ExecuteContainerRuntimeRequestOperation(
                        ExecuteContainerRuntimeRequestOperation {
                            action: HostHelperContainerRuntimeAction::RunInspect,
                            type_: "execute-container-runtime-request".to_owned(),
                            job_id: binding.run_id,
                            operation_id: Uuid::new_v4(),
                            attempt: binding.run_generation,
                            fence: Uuid::new_v4(),
                            request_sha256: helper_receipt.claims.request_sha256.clone(),
                            observation_identity_sha256: Some("e".repeat(64)),
                            installation_id: None,
                        },
                    ),
                },
                signature: HostHelperGrantSignature {
                    algorithm: "ed25519".to_owned(),
                    key_id: "f".repeat(64),
                    value: "e".repeat(128),
                },
            },
            observation_identity_sha256: "e".repeat(64),
            helper_receipt,
            observation_receipt_public_key: "00".repeat(32),
        }];
        let (client, server) = observation_client(204);
        client
            .report_exact_recipe_run_observations(&observations)
            .await
            .unwrap();
        let raw = server.join().unwrap();
        let body = raw
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .map(|index| &raw[index + 4..])
            .unwrap();
        let body: serde_json::Value = serde_json::from_slice(body).unwrap();
        assert_eq!(body["schema_version"], 2);
        assert!(body["runs"][0].get("process_running").is_none());
        assert_eq!(
            body["runs"][0]["helper_receipt"]["claims"]["outcome"],
            "not-running"
        );
        assert_eq!(body["runs"][0]["endpoint_ready"], serde_json::Value::Null);
        assert_eq!(body["runs"][0]["run_generation"], 3);
        assert!(body["runs"][0]["observed_at"].is_string());
    }

    #[tokio::test]
    async fn telemetry_posts_large_valid_metrics_without_content_loss() {
        let mut sample = telemetry_sample();
        sample.metrics.series = (0..143)
            .map(|index| {
                serde_json::from_value(json!({
                    "key": format!("device.metric_{index}"), "scope": "node",
                    "process_name": "測".repeat(128), "value": "測".repeat(256),
                    "unit": "state", "source": "native-collector",
                    "measurement_kind": "measured", "observed_at": sample.observed_at,
                    "freshness": "fresh", "freshness_threshold_seconds": 6.0,
                    "support_status": "available", "aggregation": "last"
                }))
                .unwrap()
            })
            .collect();
        let (client, server) = observation_client(204);
        client
            .report_telemetry(std::slice::from_ref(&sample))
            .await
            .unwrap();
        let request = server.join().unwrap();
        let index = request
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .unwrap()
            + 4;
        let body: Value = serde_json::from_slice(&request[index..]).unwrap();
        assert!(request.len() - index > 64 * 1024);
        assert_eq!(
            body["samples"][0]["metrics"]["series"],
            json!(sample.metrics.series)
        );
    }

    #[tokio::test]
    async fn telemetry_posts_current_contract_without_node_identity() {
        let sample = telemetry_sample();
        let (client, server) = observation_client(204);

        client
            .report_telemetry(std::slice::from_ref(&sample))
            .await
            .unwrap();

        let request = server.join().unwrap();
        let (headers, body) = request
            .windows(4)
            .position(|value| value == b"\r\n\r\n")
            .map(|index| (&request[..index], &request[index + 4..]))
            .unwrap();
        let headers = std::str::from_utf8(headers).unwrap().to_ascii_lowercase();
        assert!(headers.starts_with("post /agent/telemetry http/1.1\r\n"));
        assert!(headers.contains("content-type: application/json"));

        let body: serde_json::Value = serde_json::from_slice(body).unwrap();
        assert_eq!(
            body.as_object()
                .unwrap()
                .keys()
                .cloned()
                .collect::<Vec<_>>(),
            ["samples", "schema_version"]
        );
        assert_eq!(body["schema_version"], 1);
        assert_eq!(body["samples"].as_array().unwrap().len(), 1);
        let sample = body["samples"][0].as_object().unwrap();
        let mut keys = sample.keys().cloned().collect::<Vec<_>>();
        keys.sort();
        assert_eq!(
            keys,
            [
                "boot_id",
                "cpu_utilization_percent",
                "details",
                "disk_free_bytes",
                "disk_total_bytes",
                "gap_samples",
                "gpu_memory_free_bytes",
                "gpu_memory_total_bytes",
                "gpu_utilization_percent",
                "load_average_1m",
                "memory_available_bytes",
                "memory_total_bytes",
                "metrics",
                "network_receive_bytes_per_second",
                "network_transmit_bytes_per_second",
                "observed_at",
                "power_watts",
                "temperature_c",
            ]
        );
        assert!(!body.to_string().contains("node_id"));
        assert_eq!(sample["gpu_utilization_percent"], serde_json::Value::Null);
        assert_eq!(
            sample["details"],
            serde_json::json!({
                "accelerator_name": "NVIDIA GB10",
                "accelerator_performance_state": null
            })
        );
    }

    #[tokio::test]
    async fn telemetry_allows_the_two_second_controller_budget() {
        let sample = telemetry_sample();
        let (client, server) = request_capture_client(
            204,
            Vec::new(),
            Vec::new(),
            Some(Duration::from_millis(1_500)),
        );

        client
            .report_telemetry(std::slice::from_ref(&sample))
            .await
            .expect("telemetry should allow the two-second controller budget");
        server.join().unwrap();
    }

    #[tokio::test]
    async fn telemetry_rejects_empty_or_more_than_sixteen_samples_before_transport() {
        let client = AgentHttpClient {
            client: Arc::new(RwLock::new(reqwest::Client::new())),
            controller: Url::parse("http://127.0.0.1:9/").unwrap(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            progress_phase: Default::default(),
            distribution_receipts: Default::default(),
        };
        assert!(matches!(
            client.report_telemetry(&[]).await,
            Err(ClientError::Protocol)
        ));
        let samples = (0..17).map(|_| telemetry_sample()).collect::<Vec<_>>();
        assert!(matches!(
            client.report_telemetry(&samples).await,
            Err(ClientError::Protocol)
        ));
    }

    #[tokio::test]
    async fn telemetry_accepts_only_204_and_preserves_status_classification() {
        let samples = [telemetry_sample()];
        for (status, expected) in [
            (200, "protocol"),
            (401, "authentication"),
            (429, "retryable"),
        ] {
            let (client, server) = observation_client(status);
            let error = client.report_telemetry(&samples).await.unwrap_err();
            server.join().unwrap();
            match expected {
                "protocol" => assert!(matches!(error, ClientError::Protocol)),
                "authentication" => {
                    assert_eq!(error.status(), Some(401));
                    assert!(!error.retryable());
                }
                "retryable" => {
                    assert_eq!(error.status(), Some(429));
                    assert!(error.retryable());
                }
                _ => unreachable!("unexpected expected classification"),
            }
        }
    }

    #[tokio::test]
    async fn controller_rejection_preserves_safe_status_code_and_request_id() {
        let sample = telemetry_sample();
        let (client, server) = request_capture_client(
            403,
            vec![
                "X-Request-ID: 00000000-0000-4000-8000-000000000099".to_owned(),
                "X-Vonk-Error-Code: controller.request_rejected".to_owned(),
            ],
            Vec::new(),
            None,
        );

        let error = client
            .report_telemetry(std::slice::from_ref(&sample))
            .await
            .expect_err("Controller rejection should be surfaced");
        server.join().unwrap();
        assert_eq!(error.status(), Some(403));
        assert_eq!(error.code(), Some("controller.request_rejected"));
        assert_eq!(
            error.request_id(),
            Some("00000000-0000-4000-8000-000000000099")
        );
        assert!(!error.retryable());
    }

    fn rejection_problem(issues: serde_json::Value) -> Vec<u8> {
        serde_json::json!({
            "context": null,
            "detail": "request is invalid",
            "issues": issues,
        })
        .to_string()
        .into_bytes()
    }

    /// The failed distribution envelope spark-3542's agent retained.
    fn retained_result() -> AgentResult {
        let document = json!({
            "schema_version": 1,
            "job_id": "f4156489-a522-454b-bc6e-d2f871fa3db3",
            "operation_id": "7c2ae819-58c1-4414-90b6-93dc19a33f48",
            "attempt": 2,
            "fence": "35a57c2a-0b03-4101-8f89-6b3806570c50",
            "node_id": "spk_0123456789abcdef0123456789abcdef",
            "deadline": "2026-09-16T23:30:02.001555Z",
            "state": "failed",
            "result": {
                "status": "failed",
                "error_code": "artifact_distribution_failed",
                "failure_kind": "temporary-dependency",
                "reason": "Controller distribution could not be verified and retained"
            }
        });
        let bytes = document.to_string();
        vonk_agent_protocol::parse_strict(bytes.as_bytes())
            .expect("the retained failure envelope parses")
    }

    #[test]
    fn a_union_refusal_digest_reports_the_broken_constraint_not_the_branches() {
        // The Controller reports every union branch that did not match, so the
        // digest must surface the one constraint the producer broke and count
        // the rest, instead of listing branch shape mismatches.
        let body = rejection_problem(serde_json::json!([
            {
                "type": "missing",
                "loc": ["body", "result", "RuntimePreflightResult", "schema_version"],
                "msg": "Field required"
            },
            {
                "type": "extra_forbidden",
                "loc": ["body", "result", "RuntimePreflightResult", "diagnostics"],
                "msg": "Extra inputs are not permitted"
            },
            {
                "type": "missing",
                "loc": ["body", "result", "AgentInstallResult", "installed_bytes"],
                "msg": "Field required"
            },
            {
                "type": "string_pattern_mismatch",
                "loc": ["body", "result", "failure_kind"],
                "msg": "String should match pattern"
            },
            {
                "type": "is_instance_of",
                "loc": ["body", "result", "AgentFailureResult", "failure_kind"],
                "msg": "Input should be an instance of AgentFailureKind"
            },
            {"type": "missing", "loc": ["body", "state"], "msg": "Field required"}
        ]));

        let digest = controller_rejection_digest(&body).expect("a declared problem digest");

        assert!(digest.starts_with("request is invalid: "));
        assert!(digest.contains("body.result.failure_kind (string_pattern_mismatch)"));
        assert!(digest.contains("body.result.AgentFailureResult.failure_kind (is_instance_of)"));
        assert!(digest.contains("+4 more"));
        assert!(!digest.contains("RuntimePreflightResult"));
        assert!(!digest.contains("AgentInstallResult"));
        assert!(digest.len() <= MAX_REJECTION_CONTEXT_CHARS);
    }

    #[test]
    fn a_refusal_digest_falls_back_to_shape_errors_and_stays_bounded() {
        // With no constraint failure to report, the shape mismatches that
        // remain are still more useful than nothing, and an over-long location
        // is truncated rather than refusing the record.
        let long = "segment".repeat(40);
        let body = rejection_problem(serde_json::json!([
            {"type": "missing", "loc": ["body", long, "state"], "msg": "Field required"}
        ]));

        let digest = controller_rejection_digest(&body).expect("a declared problem digest");

        assert!(digest.starts_with("request is invalid: body."));
        assert!(digest.ends_with("(missing)"));
        assert!(!digest.contains(&"segment".repeat(20)));
        assert!(digest.len() <= MAX_REJECTION_CONTEXT_CHARS);
    }

    #[test]
    fn a_refusal_digest_redacts_credentials_and_rejects_undeclared_shapes() {
        let sensitive = serde_json::json!({
            "detail": "authorization: bearer suppressed",
            "issues": []
        })
        .to_string()
        .into_bytes();
        assert_eq!(
            controller_rejection_digest(&sensitive).as_deref(),
            Some("[redacted diagnostic line]")
        );

        // A body that is absent, not JSON, or not the declared object shape
        // yields no digest rather than a guess.
        assert_eq!(controller_rejection_digest(b""), None);
        assert_eq!(controller_rejection_digest(b"not json"), None);
        assert_eq!(controller_rejection_digest(b"[1,2,3]"), None);
        assert_eq!(controller_rejection_digest(b"{}"), None);
        let detail_only = br#"{"detail":"request is invalid"}"#;
        assert_eq!(
            controller_rejection_digest(detail_only).as_deref(),
            Some("request is invalid")
        );
    }

    #[tokio::test]
    async fn a_refused_result_records_the_controller_validation_digest() {
        // End to end over the real transport: the 422 problem the Controller
        // publishes is what the durable refusal keeps, so an operator can read
        // the failing field and rule from the agent's own record.
        let body = rejection_problem(serde_json::json!([
            {
                "type": "missing",
                "loc": ["body", "result", "RuntimePreflightResult", "schema_version"],
                "msg": "Field required"
            },
            {
                "type": "is_instance_of",
                "loc": ["body", "result", "AgentFailureResult", "failure_kind"],
                "msg": "Input should be an instance of AgentFailureKind"
            }
        ]));
        let (client, server) = request_capture_client(
            422,
            vec!["X-Vonk-Error-Code: controller.invalid_request".to_owned()],
            body,
            None,
        );

        let error = client
            .submit_result(&retained_result())
            .await
            .expect_err("a 422 is a refusal, never an acceptance");
        server.join().unwrap();

        let ClientError::ResultRejected(error) = error else {
            panic!("a 422 must map to a typed ingress refusal");
        };
        assert_eq!(
            error.rejection_context(),
            "request is invalid: body.result.AgentFailureResult.failure_kind \
             (is_instance_of); +1 more"
        );
    }

    #[tokio::test]
    async fn recipe_image_upload_resumes_interruption_and_overrides_ordinary_timeout() {
        let directory = tempfile::tempdir().unwrap();
        let archive = directory.path().join("image.docker.tar");
        std::fs::write(&archive, b"accepted archive").unwrap();
        let (client, server) = delayed_upload_client(Duration::from_millis(150));

        let result = client
            .upload_recipe_image(
                Uuid::parse_str("45ea6921-50c9-4971-be2a-4cd04ce05069").unwrap(),
                &format!("sha256:{}", "b".repeat(64)),
                &"a".repeat(64),
                16,
                &archive,
                |_| {},
            )
            .await;
        let request = server.join().unwrap();

        assert!(
            result.is_ok(),
            "large upload inherited ordinary timeout: {result:?}"
        );
        assert!(request.starts_with(b"PUT /agent/recipe-builds/"));
        assert!(request.ends_with(b"archive"));
        let headers = String::from_utf8_lossy(&request);
        assert!(headers.contains("x-vonk-upload-offset: 9"));
        assert!(headers.contains("content-length: 7"));
    }

    #[tokio::test]
    async fn recipe_job_input_stream_is_exact_and_cleans_interrupted_or_invalid_temps() {
        let cases = [
            (7, b"weights".to_vec(), "a".repeat(64), false),
            (7, b"short".to_vec(), hex_sha256(b"short!!"), false),
            (8, b"oversize".to_vec(), hex_sha256(b"oversize"), false),
            (7, b"weights".to_vec(), hex_sha256(b"weights"), true),
        ];
        for (declared, body, digest, succeeds) in cases {
            let directory = tempfile::tempdir().unwrap();
            let destination = directory.path().join("input.bin");
            let (client, server) = job_input_client(declared, body);
            let result = client
                .download_recipe_job_input(
                    Uuid::parse_str("45ea6921-50c9-4971-be2a-4cd04ce05069").unwrap(),
                    &digest,
                    7,
                    &destination,
                )
                .await;
            server.join().unwrap();
            assert_eq!(result.is_ok(), succeeds);
            assert_eq!(destination.exists(), succeeds);
            assert!(
                !std::fs::read_dir(directory.path())
                    .unwrap()
                    .filter_map(Result::ok)
                    .any(|entry| entry
                        .file_name()
                        .to_string_lossy()
                        .starts_with(".job-input-"))
            );
        }
    }
}
