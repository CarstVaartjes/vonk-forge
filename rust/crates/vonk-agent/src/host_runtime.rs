//! Narrow client for controller-authorized host container-runtime operations.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use ring::signature;
use thiserror::Error;
use vonk_agent_protocol::generated::HostHelperResponse as HelperResponse;
use vonk_agent_protocol::{
    AgentClaim, HostRuntimeAction, HostRuntimeRequest, HostRuntimeRequestRule,
    RecipeRunInspectionBinding, RecipeRunObservationOutcome, RecipeRunObservationReceipt,
    SignedHostHelperGrant, canonical_json, hex_sha256, parse_strict,
    recipe_run_observation_receipt_signing_bytes,
};

use crate::client::{AgentHttpClient, ClientError};
use crate::failure_evidence::{FailureProcessLogs, sanitize_tail};

/// The frame ceiling is owned by the wire contract so the agent, the upgrade
/// channel and the privileged helper cannot drift.
const MAX_HELPER_MESSAGE_BYTES: usize = vonk_agent_protocol::MAX_HELPER_FRAME_BYTES;

#[derive(Debug, Error)]
pub enum HostRuntimeError {
    #[error("host runtime request storage is invalid")]
    Io(#[from] std::io::Error),
    #[error("host runtime authority is unavailable")]
    Controller(#[from] ClientError),
    #[error("host runtime helper protocol contract is invalid")]
    HelperProtocol(HelperProtocolCause),
    /// A request contract was refused for exceeding a measured bound. The limit
    /// and the observed value travel with the error so the failure evidence can
    /// name them without carrying engine-owned content.
    #[error("host runtime helper protocol bound was exceeded")]
    HelperProtocolBound {
        cause: HelperProtocolCause,
        limit: Option<u64>,
        observed: u64,
    },
    /// The helper answered, but did not confirm whether the workload started or
    /// stopped. That is an ambiguous effect that needs reconciliation, not a
    /// malformed reply, so it keeps a state of its own.
    #[error("host runtime could not confirm the workload outcome")]
    StopUncertain,
    #[error("host runtime helper rejected request: {code}")]
    HelperRejected {
        code: String,
        diagnostic: Option<String>,
        /// The rejected container's own retained output, per stream, when the
        /// helper could read it. Absence is reported, never read as empty.
        process_logs: Option<Box<FailureProcessLogs>>,
    },
}

/// The distinct contracts this agent verifies while exchanging one message with
/// the privileged helper.
///
/// Every one of these contracts was previously collapsed into a single
/// `helper_protocol_invalid` label that could not say which was violated -- the
/// live symptom of a blocked privileged start, with no helper involvement and
/// nothing to act on. That label now survives only as the fallback for a cause
/// or helper code that is not on the stable allowlist.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HelperProtocolCause {
    /// The canonical request body or the signed grant could not be encoded.
    RequestEncoding,
    /// The blocking helper-call worker failed to join.
    HelperCallJoin,
    /// The length-prefixed helper message was empty, oversized or undecodable.
    MessageFraming,
    /// The reply did not bind to the request this agent sent.
    ResponseUnbound,
    /// A helper rejection did not match the stable rejection contract.
    RejectionMalformed,
    /// An executed helper outcome did not match the executed-outcome contract.
    OutcomeMalformed,
    /// The agent-built runtime request or inspection binding failed canonical
    /// validation before any helper was called.
    RequestDocument,
    /// The agent-built request targets another schema version.
    RequestSchemaVersion,
    /// The agent-built request carries a zero attempt.
    RequestAttempt,
    /// The agent-built request carries arguments for the wrong action.
    RequestArgumentsPresence,
    /// The agent-built request carries an installation identity for the wrong
    /// action.
    RequestInstallationIdentity,
    /// The agent-built request carries a canonical document larger than the
    /// bounded helper exchange reads.
    RequestBytes,
    /// An agent-built request argument carries a NUL byte an exec argv cannot
    /// frame.
    RequestArgumentNulByte,
    /// The owner-only signed request file could not be established.
    RequestStorage,
    /// The host clock is before the Unix epoch.
    SystemClock,
    /// An executed inspection reply did not carry exactly one signed
    /// observation receipt.
    InspectionReceipt,
    /// The signed observation receipt did not prove this inspection.
    ObservationReceipt,
    /// The signed observation time is not representable.
    ObservationTimestamp,
}

impl HelperProtocolCause {
    /// The stable, un-prefixed contract code. `preflight_code()` reports it in
    /// the `helper_<code>` namespace.
    pub fn code(self) -> &'static str {
        match self {
            Self::RequestEncoding => "request_encoding_invalid",
            Self::HelperCallJoin => "call_join_failed",
            Self::MessageFraming => "message_framing_invalid",
            Self::ResponseUnbound => "response_unbound",
            Self::RejectionMalformed => "rejection_malformed",
            Self::OutcomeMalformed => "outcome_malformed",
            Self::RequestDocument => "request_document_invalid",
            Self::RequestSchemaVersion => "request_schema_version_invalid",
            Self::RequestAttempt => "request_attempt_invalid",
            Self::RequestArgumentsPresence => "request_arguments_presence_invalid",
            Self::RequestInstallationIdentity => "request_installation_identity_invalid",
            Self::RequestBytes => "request_bytes_invalid",
            Self::RequestArgumentNulByte => "request_argument_nul_byte",
            Self::RequestStorage => "request_storage_invalid",
            Self::SystemClock => "system_clock_invalid",
            Self::InspectionReceipt => "inspection_receipt_invalid",
            Self::ObservationReceipt => "observation_receipt_invalid",
            Self::ObservationTimestamp => "observation_timestamp_invalid",
        }
    }

    /// Map one canonical request rule to the cause this agent reports. Every
    /// envelope rule keeps its own code so a refused Start names the rule and,
    /// for a measured bound, the limit and the observed value rather than one
    /// opaque label.
    pub fn from_request_rule(rule: HostRuntimeRequestRule) -> Self {
        match rule {
            HostRuntimeRequestRule::SchemaVersion => Self::RequestSchemaVersion,
            HostRuntimeRequestRule::Attempt => Self::RequestAttempt,
            HostRuntimeRequestRule::ArgumentsPresence => Self::RequestArgumentsPresence,
            HostRuntimeRequestRule::InstallationIdentity => Self::RequestInstallationIdentity,
            HostRuntimeRequestRule::RequestBytes { .. } => Self::RequestBytes,
            HostRuntimeRequestRule::ArgumentNulByte { .. } => Self::RequestArgumentNulByte,
            // The observation binding is an inspection contract rather than one
            // of the argument-envelope rules, and it is unreachable from a
            // Start, which always carries `observation: None`.
            HostRuntimeRequestRule::ObservationBinding
            | HostRuntimeRequestRule::ObservationAction
            | HostRuntimeRequestRule::Encoding => Self::RequestDocument,
        }
    }
}

impl HostRuntimeError {
    /// Bounded, non-sensitive evidence for the admission receipt.
    pub fn preflight_code(&self) -> String {
        match self {
            Self::Io(_) => "helper_io_failed".to_owned(),
            Self::Controller(ClientError::Protocol) => "helper_grant_invalid".to_owned(),
            Self::Controller(ClientError::Controller(error))
                if matches!(error.status, 401 | 403) =>
            {
                "helper_grant_unauthorized".to_owned()
            }
            Self::Controller(_) => "helper_grant_unavailable".to_owned(),
            Self::HelperProtocol(cause) if stable_runtime_error_code(cause.code()) => {
                format!("helper_{}", cause.code())
            }
            Self::HelperProtocolBound { cause, .. } if stable_runtime_error_code(cause.code()) => {
                format!("helper_{}", cause.code())
            }
            // A cause that is not on the namespace allowlist keeps the previous
            // opaque label, exactly as an unlisted helper rejection code does.
            Self::HelperProtocol(_) | Self::HelperProtocolBound { .. } => {
                "helper_protocol_invalid".to_owned()
            }
            // An ambiguous stop is named for what it is, so it can never be
            // read as a malformed helper reply.
            Self::StopUncertain => "helper_stop_uncertain".to_owned(),
            Self::HelperRejected { code, .. } if stable_runtime_error_code(code) => {
                format!("helper_{code}")
            }
            Self::HelperRejected { .. } => "helper_protocol_invalid".to_owned(),
        }
    }

    /// Build the refusal for one canonical request rule, carrying the measured
    /// bound when the rule has one.
    fn request_refusal(rule: HostRuntimeRequestRule) -> Self {
        let cause = HelperProtocolCause::from_request_rule(rule);
        match rule.bound() {
            Some((limit, observed)) => Self::HelperProtocolBound {
                cause,
                limit,
                observed,
            },
            None => Self::HelperProtocol(cause),
        }
    }

    /// The measured limit and observed value behind a refusal, when the rule
    /// measured one. Only these bounded integers ever cross; the offending
    /// argument itself never does.
    pub fn refusal_bound(&self) -> Option<(Option<u64>, u64)> {
        match self {
            Self::HelperProtocolBound {
                limit, observed, ..
            } => Some((*limit, *observed)),
            _ => None,
        }
    }

    pub fn diagnostic(&self) -> Option<&str> {
        match self {
            Self::HelperRejected { diagnostic, .. } => diagnostic.as_deref(),
            _ => None,
        }
    }

    /// The rejected container's own retained output, when the helper read it.
    pub fn process_logs(&self) -> Option<&FailureProcessLogs> {
        match self {
            Self::HelperRejected { process_logs, .. } => process_logs.as_deref(),
            _ => None,
        }
    }
}

pub struct HostRuntimeOutcome {
    pub exit_code: Option<i32>,
    pub stop_uncertain: bool,
}

pub struct RecipeRunInspectionOutcome {
    pub grant: SignedHostHelperGrant,
    pub observation_identity_sha256: String,
    pub receipt: RecipeRunObservationReceipt,
    pub process_running: bool,
}

pub struct HostRuntimeBoundary<'a> {
    pub client: &'a AgentHttpClient,
    pub request_root: &'a Path,
    pub helper_socket: &'a Path,
    pub observation_receipt_public_key: [u8; 32],
}

struct RequestFileCleanup(PathBuf);

impl Drop for RequestFileCleanup {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

impl HostRuntimeBoundary<'_> {
    pub async fn inspect_recipe_run(
        &self,
        binding: RecipeRunInspectionBinding,
        arguments: Vec<String>,
    ) -> Result<RecipeRunInspectionOutcome, HostRuntimeError> {
        binding
            .validate()
            .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestDocument))?;
        let attempt = binding.run_generation;
        let request = HostRuntimeRequest {
            schema_version: 1,
            action: HostRuntimeAction::RunInspect,
            job_id: binding.run_id,
            operation_id: uuid::Uuid::new_v4(),
            attempt,
            fence: uuid::Uuid::new_v4(),
            arguments,
            observation: Some(binding.clone()),
            installation_id: None,
            reconciliation_identity: None,
        };
        request
            .validate()
            .map_err(HostRuntimeError::request_refusal)?;
        let body = canonical_json(&request)
            .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestEncoding))?;
        let digest = hex_sha256(&body);
        let request_path = write_request(self.request_root, &digest, &body)?;
        let _request_cleanup = RequestFileCleanup(request_path);
        let authorization = self
            .client
            .recipe_run_inspection_grant(&binding, &request, &digest)
            .await?;
        let request_id = authorization.grant.claims.request_id.to_string();
        let grant_bytes = canonical_json(&authorization.grant)
            .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestEncoding))?;
        let helper_socket = self.helper_socket.to_path_buf();
        let response = tokio::task::spawn_blocking(move || {
            call_helper(&helper_socket, &grant_bytes, Duration::from_secs(15))
        })
        .await
        .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::HelperCallJoin))??;
        require_bound_response(&response, &request_id)?;
        if response.error_code.is_some() {
            return Err(runtime_rejection(&response, HostRuntimeAction::RunInspect));
        }
        let receipt = require_inspection_receipt(&response)?.clone();
        let issued_at = authorization.grant.claims.issued_at;
        let expires_at = authorization.grant.claims.expires_at;
        let received_at = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::SystemClock))?
            .as_secs() as i64;
        let process_running = verify_observation_receipt(
            &receipt,
            &self.observation_receipt_public_key,
            self.client.node_id(),
            &request_id,
            &digest,
            &authorization.observation_identity_sha256,
            issued_at,
            expires_at,
            received_at,
        )?;
        Ok(RecipeRunInspectionOutcome {
            grant: authorization.grant,
            observation_identity_sha256: authorization.observation_identity_sha256,
            receipt,
            process_running,
        })
    }

    pub async fn execute(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        self.execute_bound(claim, action, arguments, None).await
    }

    pub async fn cleanup_installation(
        &self,
        claim: &AgentClaim,
        installation_id: uuid::Uuid,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        self.execute_bound(
            claim,
            HostRuntimeAction::InstallationCleanup,
            Vec::new(),
            Some(installation_id),
        )
        .await
    }

    async fn execute_bound(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
        installation_id: Option<uuid::Uuid>,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        let helper_timeout = match action {
            HostRuntimeAction::RuntimePreflight => Duration::from_secs(14),
            HostRuntimeAction::Start => arguments
                .iter()
                .find_map(|value| value.strip_prefix("VONK_JOB_TIMEOUT_SECONDS="))
                .and_then(|value| value.parse::<u64>().ok())
                .filter(|value| (1..=3600).contains(value))
                .map_or(Duration::from_secs(610), |value| {
                    // Leave room after the adapter deadline for bounded inspect/stop/remove and
                    // a truthful uncertainty response from the privileged helper.
                    Duration::from_secs(value + 120)
                }),
            HostRuntimeAction::Stop => arguments
                .get(1)
                .and_then(|value| value.parse::<u64>().ok())
                .filter(|value| (1..=600).contains(value))
                .map_or(Duration::from_secs(45), |value| {
                    Duration::from_secs(value + 45)
                }),
            _ => Duration::from_secs(610),
        };
        let request = HostRuntimeRequest {
            schema_version: 1,
            action,
            job_id: claim.job_id,
            operation_id: claim.operation_id,
            attempt: claim.attempt,
            fence: claim.fence,
            arguments,
            observation: None,
            installation_id,
        };
        request
            .validate()
            .map_err(HostRuntimeError::request_refusal)?;
        let body = canonical_json(&request)
            .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestEncoding))?;
        let digest = hex_sha256(&body);
        let request_path = write_request(self.request_root, &digest, &body)?;
        // The attached helper call is deliberately run on a blocking worker so a cancellation
        // heartbeat can issue a concurrent STOP. If that cancellation drops this future, still
        // remove the signed request file; the helper already received its canonical body.
        let _request_cleanup = RequestFileCleanup(request_path);
        async {
            let grant = self
                .client
                .host_runtime_grant(claim, action, &digest, installation_id)
                .await?;
            let request_id = grant.claims.request_id.to_string();
            let grant = canonical_json(&grant).map_err(|_| {
                HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestEncoding)
            })?;
            let helper_socket = self.helper_socket.to_path_buf();
            let response = tokio::task::spawn_blocking(move || {
                call_helper(&helper_socket, &grant, helper_timeout)
            })
            .await
            .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::HelperCallJoin))??;
            let stop_uncertain = response.status == "container-runtime-stop-uncertain";
            require_bound_response(&response, &request_id)?;
            if response.error_code.is_some() {
                return Err(runtime_rejection(&response, action));
            }
            require_executed_outcome(&response, stop_uncertain)?;
            Ok(HostRuntimeOutcome {
                exit_code: response.exit_code.map(|code| code as i32),
                stop_uncertain,
            })
        }
        .await
    }
}

/// Bind the helper's reply to the request this agent sent before anything in it
/// is trusted.
///
/// The helper attaches the request identity only once it has authorized the
/// grant, so a rejection raised by an earlier check arrives with
/// `request_id: null`. That is the normal shape of the helper saying which check
/// refused -- `grant_node_mismatch` and `grant_unauthorized` cannot be reported
/// any other way -- and demanding an identity that the helper deliberately
/// withholds reported each of them as a malformed reply instead. The code set
/// keeps acceptance closed to the rejections the helper can only raise before it
/// trusts the grant, so an unbound reply can never be mistaken for a bound one.
fn require_bound_response(
    response: &HelperResponse,
    request_id: &str,
) -> Result<(), HostRuntimeError> {
    if response.schema_version != 1 {
        return Err(HostRuntimeError::HelperProtocol(
            HelperProtocolCause::ResponseUnbound,
        ));
    }
    if response.request_id.map(|id| id.to_string()).as_deref() == Some(request_id) {
        return Ok(());
    }
    if response.request_id.is_none()
        && response
            .error_code
            .as_deref()
            .is_some_and(unbound_rejection_is_expected)
    {
        return Ok(());
    }
    Err(HostRuntimeError::HelperProtocol(
        HelperProtocolCause::ResponseUnbound,
    ))
}

/// The codes the helper can only raise before it has trusted the grant, and so
/// the only rejections that legitimately arrive without the request identity.
/// Every other code is produced after the helper knows the request, so an
/// unbound reply claiming one is a reply this agent cannot account for.
fn unbound_rejection_is_expected(code: &str) -> bool {
    matches!(
        code,
        "grant_invalid"
            | "grant_node_mismatch"
            | "grant_unauthorized"
            | "peer_identity_invalid"
            | "request_invalid"
    )
}

/// The executed-outcome contract. A successful helper reply carries no
/// rejection, no capture diagnostic and no signed inspection receipt, names the
/// executed status unless it is the deliberate stop-uncertain outcome, and
/// reports a 64-character lowercase evidence digest with, at most, a byte-sized
/// exit code.
fn require_executed_outcome(
    response: &HelperResponse,
    stop_uncertain: bool,
) -> Result<(), HostRuntimeError> {
    let malformed = || HostRuntimeError::HelperProtocol(HelperProtocolCause::OutcomeMalformed);
    if response.observation_receipt.is_some() {
        return Err(malformed());
    }
    if response.diagnostic.is_some()
        || !stop_uncertain && response.status != "container-runtime-request-executed"
    {
        return Err(malformed());
    }
    if response
        .evidence_sha256
        .as_deref()
        .is_none_or(|value| !lower_hex(value, 64))
    {
        return Err(malformed());
    }
    if response
        .exit_code
        .is_some_and(|code| !(0..=255).contains(&code))
    {
        return Err(malformed());
    }
    Ok(())
}

fn runtime_rejection(response: &HelperResponse, action: HostRuntimeAction) -> HostRuntimeError {
    let Some(code) = response.error_code.as_deref() else {
        return HostRuntimeError::HelperProtocol(HelperProtocolCause::RejectionMalformed);
    };
    if response.status != "rejected"
        || response.evidence_sha256.is_some()
        || response.exit_code.is_some()
        || response.observation_receipt.is_some()
        || !stable_runtime_error_code(code)
        || response.diagnostic.is_some()
            && (action != HostRuntimeAction::RunInspect || code != "runtime_process_exited")
    {
        return HostRuntimeError::HelperProtocol(HelperProtocolCause::RejectionMalformed);
    }
    HostRuntimeError::HelperRejected {
        code: code.to_owned(),
        diagnostic: response
            .diagnostic
            .as_deref()
            .map(crate::failure_evidence::sanitize_text),
        process_logs: response.process_logs.as_ref().map(|logs| {
            Box::new(FailureProcessLogs {
                stdout: sanitize_tail(&logs.stdout),
                stderr: sanitize_tail(&logs.stderr),
            })
        }),
    }
}

fn stable_runtime_error_code(value: &str) -> bool {
    // Every code the privileged helper can name, not only the operation ones.
    // The helper's grant, peer and request rejections were absent, so the agent
    // reported each of them as an opaque protocol error even when the helper had
    // said which check refused -- which is how a live privileged start became
    // unattributable.
    matches!(
        value,
        "operation_failed"
            | "operation_invalid"
            | "operation_unsafe_path"
            | "operation_invalid_artifact"
            | "operation_command_failed"
            | "operation_stop_uncertain"
            | "operation_io"
            | "runtime_image_load_failed"
            | "runtime_image_inspect_failed"
            | "runtime_image_identity_invalid"
            | "runtime_image_receipt_failed"
            | "runtime_process_exited"
            | "runtime_run_missing"
            | "runtime_fabric_unavailable"
            | "runtime_fabric_firewall_rejected"
            | "grant_invalid"
            | "grant_node_mismatch"
            | "grant_unauthorized"
            | "peer_identity_invalid"
            | "request_invalid"
            | "request_replayed"
            | "request_ledger_failed"
            // The agent's own helper-protocol causes share the `helper_<code>`
            // namespace, so this allowlist stays the single owner of the codes
            // `preflight_code()` may name. A cause missing here keeps the
            // previous opaque label instead of inventing an unowned code.
            | "request_encoding_invalid"
            | "call_join_failed"
            | "message_framing_invalid"
            | "response_unbound"
            | "rejection_malformed"
            | "outcome_malformed"
            // The rest of the agent-side contracts share the same namespace.
            | "request_document_invalid"
            | "request_schema_version_invalid"
            | "request_attempt_invalid"
            | "request_arguments_presence_invalid"
            | "request_installation_identity_invalid"
            | "request_bytes_invalid"
            | "request_argument_nul_byte"
            | "request_storage_invalid"
            | "system_clock_invalid"
            | "inspection_receipt_invalid"
            | "observation_receipt_invalid"
            | "observation_timestamp_invalid"
    )
}

fn require_inspection_receipt(
    response: &HelperResponse,
) -> Result<&RecipeRunObservationReceipt, HostRuntimeError> {
    if response.status != "container-runtime-request-executed"
        || response.error_code.is_some()
        || response
            .evidence_sha256
            .as_deref()
            .is_none_or(|value| !lower_hex(value, 64))
        || response.exit_code.is_some()
    {
        return Err(HostRuntimeError::HelperProtocol(
            HelperProtocolCause::InspectionReceipt,
        ));
    }
    response
        .observation_receipt
        .as_ref()
        .ok_or(HostRuntimeError::HelperProtocol(
            HelperProtocolCause::InspectionReceipt,
        ))
}

#[allow(clippy::too_many_arguments)]
fn verify_observation_receipt(
    receipt: &RecipeRunObservationReceipt,
    public_key: &[u8; 32],
    node_id: &str,
    request_id: &str,
    request_sha256: &str,
    observation_identity_sha256: &str,
    issued_at: i64,
    expires_at: i64,
    received_at: i64,
) -> Result<bool, HostRuntimeError> {
    receipt
        .validate()
        .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::ObservationReceipt))?;
    if receipt.claims.node_id != node_id
        || receipt.claims.request_id.to_string() != request_id
        || receipt.claims.request_sha256 != request_sha256
        || receipt.claims.observation_identity_sha256 != observation_identity_sha256
        || receipt.signature.key_id != hex_sha256(public_key)
        || receipt.claims.observed_at < issued_at
        || receipt.claims.observed_at >= expires_at
        || received_at > expires_at.saturating_add(5)
    {
        return Err(HostRuntimeError::HelperProtocol(
            HelperProtocolCause::ObservationReceipt,
        ));
    }
    let signature_bytes = hex::decode(&receipt.signature.value)
        .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::ObservationReceipt))?;
    signature::UnparsedPublicKey::new(&signature::ED25519, public_key)
        .verify(
            &recipe_run_observation_receipt_signing_bytes(&receipt.claims).map_err(|_| {
                HostRuntimeError::HelperProtocol(HelperProtocolCause::ObservationReceipt)
            })?,
            &signature_bytes,
        )
        .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::ObservationReceipt))?;
    Ok(receipt.claims.outcome == RecipeRunObservationOutcome::Running)
}

fn write_request(root: &Path, digest: &str, body: &[u8]) -> Result<PathBuf, HostRuntimeError> {
    match fs::create_dir(root) {
        Ok(()) => fs::set_permissions(root, fs::Permissions::from_mode(0o700))?,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error.into()),
    }
    let metadata = fs::symlink_metadata(root)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err(HostRuntimeError::HelperProtocol(
            HelperProtocolCause::RequestStorage,
        ));
    }
    let destination = root.join(format!("{digest}.json"));
    match fs::symlink_metadata(&destination) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.permissions().mode() & 0o077 != 0
                || fs::read(&destination)? != body
            {
                return Err(HostRuntimeError::HelperProtocol(
                    HelperProtocolCause::RequestStorage,
                ));
            }
            return Ok(destination);
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::SystemClock))?
        .as_nanos();
    let temporary = root.join(format!(".{digest}.{}.{nonce}.tmp", std::process::id()));
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(body)?;
    file.sync_all()?;
    match fs::hard_link(&temporary, &destination) {
        Ok(()) => fs::remove_file(&temporary)?,
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            fs::remove_file(&temporary)?;
            let metadata = fs::symlink_metadata(&destination)?;
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || metadata.nlink() != 1
                || metadata.permissions().mode() & 0o077 != 0
                || fs::read(&destination)? != body
            {
                return Err(HostRuntimeError::HelperProtocol(
                    HelperProtocolCause::RequestStorage,
                ));
            }
            return Ok(destination);
        }
        Err(error) => {
            let _ = fs::remove_file(&temporary);
            return Err(error.into());
        }
    }
    File::open(root)?.sync_all()?;
    Ok(destination)
}

fn call_helper(
    socket: &Path,
    body: &[u8],
    read_timeout: Duration,
) -> Result<HelperResponse, HostRuntimeError> {
    if body.is_empty() || body.len() > MAX_HELPER_MESSAGE_BYTES {
        // Report the ceiling and the observed length; the body itself is
        // engine-owned content and never travels into the evidence.
        return Err(HostRuntimeError::HelperProtocolBound {
            cause: HelperProtocolCause::MessageFraming,
            limit: Some(MAX_HELPER_MESSAGE_BYTES as u64),
            observed: body.len() as u64,
        });
    }
    let mut stream = UnixStream::connect(socket)?;
    stream.set_read_timeout(Some(read_timeout))?;
    stream.set_write_timeout(Some(Duration::from_secs(10)))?;
    stream.write_all(&(body.len() as u32).to_be_bytes())?;
    stream.write_all(body)?;
    stream.flush()?;
    let mut prefix = [0_u8; 4];
    stream.read_exact(&mut prefix)?;
    let length = u32::from_be_bytes(prefix) as usize;
    if length == 0 || length > MAX_HELPER_MESSAGE_BYTES {
        return Err(HostRuntimeError::HelperProtocol(
            HelperProtocolCause::MessageFraming,
        ));
    }
    let mut response = vec![0_u8; length];
    stream.read_exact(&mut response)?;
    parse_strict(&response)
        .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::MessageFraming))
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

#[cfg(test)]
mod tests {
    use super::{
        HelperProtocolCause, HelperResponse, HostRuntimeError, call_helper, require_bound_response,
        require_executed_outcome, require_inspection_receipt, runtime_rejection,
        verify_observation_receipt, write_request,
    };
    use ring::signature::{Ed25519KeyPair, KeyPair};
    use std::fs;
    use std::io::{Read, Write};
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::os::unix::net::UnixListener;
    use std::path::Path;
    use std::time::Duration;
    use uuid::Uuid;
    use vonk_agent_protocol::{
        HostRuntimeAction, RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY, RecipeRunObservationOutcome,
        RecipeRunObservationReceipt, RecipeRunObservationReceiptClaims,
        RecipeRunObservationReceiptSignature, hex_sha256,
        recipe_run_observation_receipt_signing_bytes,
    };

    fn signed_receipt(signer: &Ed25519KeyPair, request_id: Uuid) -> RecipeRunObservationReceipt {
        let claims = RecipeRunObservationReceiptClaims {
            schema_version: 1,
            authority: RECIPE_RUN_OBSERVATION_RECEIPT_AUTHORITY.to_owned(),
            node_id: "spk_0123456789abcdef0123456789abcdef".to_owned(),
            request_id,
            request_sha256: "a".repeat(64),
            observation_identity_sha256: "b".repeat(64),
            outcome: RecipeRunObservationOutcome::Running,
            observed_at: 105,
        };
        RecipeRunObservationReceipt {
            schema_version: 1,
            signature: RecipeRunObservationReceiptSignature {
                algorithm: "ed25519".to_owned(),
                key_id: hex_sha256(signer.public_key().as_ref()),
                value: hex::encode(
                    signer
                        .sign(&recipe_run_observation_receipt_signing_bytes(&claims).unwrap())
                        .as_ref(),
                ),
            },
            claims,
        }
    }

    #[test]
    fn runtime_rejection_binds_and_redacts_captured_process_logs() {
        let mut response: super::HelperResponse = vonk_agent_protocol::parse_strict(
            br#"{"schema_version":1,"request_id":null,"status":"rejected","evidence_sha256":null,"error_code":"runtime_process_exited","diagnostic":"ModuleNotFoundError: runtime module\nAPI_TOKEN=private-value\n"}"#,
        ).unwrap();
        let error = super::runtime_rejection(&response, HostRuntimeAction::RunInspect);
        assert!(error.diagnostic().unwrap().contains("ModuleNotFoundError"));
        assert!(!error.diagnostic().unwrap().contains("private-value"));
        assert!(matches!(
            super::runtime_rejection(&response, HostRuntimeAction::Start),
            super::HostRuntimeError::HelperProtocol(super::HelperProtocolCause::RejectionMalformed,)
        ));
        response.error_code = Some("operation_unsafe_path".into());
        assert!(matches!(
            super::runtime_rejection(&response, HostRuntimeAction::RunInspect),
            super::HostRuntimeError::HelperProtocol(super::HelperProtocolCause::RejectionMalformed,)
        ));
        // A rejection never carries the observation receipt that only an
        // executed run produces, whatever the action claimed it ran.
        response.error_code = Some("runtime_process_exited".into());
        response.observation_receipt = Some(signed_receipt(
            &Ed25519KeyPair::from_seed_unchecked(&[7; 32]).unwrap(),
            Uuid::new_v4(),
        ));
        assert!(matches!(
            super::runtime_rejection(&response, HostRuntimeAction::RunInspect),
            super::HostRuntimeError::HelperProtocol(super::HelperProtocolCause::RejectionMalformed,)
        ));
    }

    #[test]
    fn unbound_helper_rejection_names_the_check_that_refused() {
        // The helper attaches the request identity only after it authorizes the
        // grant, so these refusals cannot echo it. Requiring the identity anyway
        // reported each as `helper_protocol_invalid` -- the same label a corrupt
        // reply gets -- which is how a live privileged start became
        // unattributable with no diagnostic to read.
        let request_id = "10000000-0000-4000-8000-000000000001";
        for (code, expected) in [
            ("grant_node_mismatch", "helper_grant_node_mismatch"),
            ("grant_unauthorized", "helper_grant_unauthorized"),
            ("peer_identity_invalid", "helper_peer_identity_invalid"),
        ] {
            let response: super::HelperResponse = vonk_agent_protocol::parse_strict(
                format!(
                    r#"{{"schema_version":1,"request_id":null,"status":"rejected","evidence_sha256":null,"error_code":"{code}"}}"#
                )
                .as_bytes(),
            )
            .unwrap();
            super::require_bound_response(&response, request_id).unwrap();
            assert_eq!(
                super::runtime_rejection(&response, HostRuntimeAction::Start).preflight_code(),
                expected
            );
        }
    }

    #[test]
    fn a_reply_this_agent_cannot_bind_is_still_a_protocol_error() {
        let request_id = "10000000-0000-4000-8000-000000000001";
        let rejected_for_another_request: super::HelperResponse =
            vonk_agent_protocol::parse_strict(
                br#"{"schema_version":1,"request_id":"20000000-0000-4000-8000-000000000002","status":"rejected","evidence_sha256":null,"error_code":"grant_unauthorized"}"#,
            )
            .unwrap();
        assert!(super::require_bound_response(&rejected_for_another_request, request_id).is_err());

        // Every other code is produced only after the helper knows the request,
        // so an unbound reply claiming one cannot be accounted for.
        for code in [
            "request_replayed",
            "request_ledger_failed",
            "operation_failed",
            "runtime_process_exited",
        ] {
            let unbound: super::HelperResponse = vonk_agent_protocol::parse_strict(
                format!(
                    r#"{{"schema_version":1,"request_id":null,"status":"rejected","evidence_sha256":null,"error_code":"{code}"}}"#
                )
                .as_bytes(),
            )
            .unwrap();
            assert!(
                super::require_bound_response(&unbound, request_id).is_err(),
                "{code} must not be accepted without a request identity"
            );
        }

        let unbound_success: super::HelperResponse = vonk_agent_protocol::parse_strict(
            br#"{"schema_version":1,"request_id":null,"status":"container-runtime-request-executed","evidence_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}"#,
        )
        .unwrap();
        assert!(super::require_bound_response(&unbound_success, request_id).is_err());
    }

    #[test]
    fn preflight_reports_the_failed_boundary_without_exposing_error_details() {
        use crate::client::ClientError;
        let cases = [
            (
                HostRuntimeError::Controller(ClientError::Protocol),
                "helper_grant_invalid",
            ),
            (
                HostRuntimeError::Controller(ClientError::Controller(Box::new(
                    crate::client::ControllerError::from_status(403),
                ))),
                "helper_grant_unauthorized",
            ),
            (
                HostRuntimeError::Controller(ClientError::Retryable),
                "helper_grant_unavailable",
            ),
            (
                HostRuntimeError::Io(std::io::Error::other("private path or transport detail")),
                "helper_io_failed",
            ),
            (
                HostRuntimeError::HelperRejected {
                    code: "operation_unsafe_path".to_owned(),
                    diagnostic: None,
                    process_logs: None,
                },
                "helper_operation_unsafe_path",
            ),
            // The helper's own grant and request rejections name the refusing
            // check, so the operator must see them rather than a protocol error.
            (
                HostRuntimeError::HelperRejected {
                    code: "grant_unauthorized".to_owned(),
                    diagnostic: None,
                    process_logs: None,
                },
                "helper_grant_unauthorized",
            ),
            (
                HostRuntimeError::HelperRejected {
                    code: "grant_node_mismatch".to_owned(),
                    diagnostic: None,
                    process_logs: None,
                },
                "helper_grant_node_mismatch",
            ),
            (
                HostRuntimeError::HelperRejected {
                    code: "request_replayed".to_owned(),
                    diagnostic: None,
                    process_logs: None,
                },
                "helper_request_replayed",
            ),
            (
                HostRuntimeError::HelperRejected {
                    code: "untrusted response detail".to_owned(),
                    diagnostic: None,
                    process_logs: None,
                },
                "helper_protocol_invalid",
            ),
        ];
        for (error, expected) in cases {
            assert_eq!(error.preflight_code(), expected);
        }
    }

    #[test]
    fn helper_receipt_signature_is_required_and_bound_to_exact_request() {
        let signer = Ed25519KeyPair::from_seed_unchecked(&[7; 32]).unwrap();
        let request_id = Uuid::new_v4();
        let receipt = signed_receipt(&signer, request_id);
        let public_key: [u8; 32] = signer.public_key().as_ref().try_into().unwrap();
        assert!(
            verify_observation_receipt(
                &receipt,
                &public_key,
                "spk_0123456789abcdef0123456789abcdef",
                &request_id.to_string(),
                &"a".repeat(64),
                &"b".repeat(64),
                100,
                110,
                106,
            )
            .unwrap()
        );

        for mismatch in ["request", "observation", "node", "replay"] {
            let mut changed = receipt.clone();
            let (node, request, observation, issued) = match mismatch {
                "request" => (
                    changed.claims.node_id.clone(),
                    "c".repeat(64),
                    "b".repeat(64),
                    100,
                ),
                "observation" => (
                    changed.claims.node_id.clone(),
                    "a".repeat(64),
                    "c".repeat(64),
                    100,
                ),
                "node" => (
                    "spk_11111111111111111111111111111111".to_owned(),
                    "a".repeat(64),
                    "b".repeat(64),
                    100,
                ),
                _ => {
                    changed.claims.observed_at = 99;
                    (
                        changed.claims.node_id.clone(),
                        "a".repeat(64),
                        "b".repeat(64),
                        100,
                    )
                }
            };
            assert!(
                verify_observation_receipt(
                    &changed,
                    &public_key,
                    &node,
                    &request_id.to_string(),
                    &request,
                    &observation,
                    issued,
                    110,
                    106,
                )
                .is_err()
            );
        }

        let mut forged_outcome = receipt;
        forged_outcome.claims.outcome = RecipeRunObservationOutcome::NotRunning;
        assert!(
            verify_observation_receipt(
                &forged_outcome,
                &public_key,
                "spk_0123456789abcdef0123456789abcdef",
                &request_id.to_string(),
                &"a".repeat(64),
                &"b".repeat(64),
                100,
                110,
                106,
            )
            .is_err()
        );
    }

    #[test]
    fn helper_success_without_an_execution_receipt_is_rejected() {
        let response: HelperResponse = serde_json::from_value(serde_json::json!({
            "schema_version": 1,
            "request_id": Uuid::new_v4().to_string(),
            "status": "container-runtime-request-executed",
            "evidence_sha256": "a".repeat(64)
        }))
        .unwrap();
        assert!(require_inspection_receipt(&response).is_err());
    }

    #[test]
    fn a_large_frame_is_admitted_and_an_oversized_one_reports_its_bound() {
        // Wrong implementation: the 256 KiB ceiling refused a legitimate large
        // command line while the plan it came from was still admitted.
        let above_the_old_ceiling = vec![b'x'; 256 * 1024 + 1];
        let error = call_helper(
            Path::new("/nonexistent"),
            &above_the_old_ceiling,
            Duration::from_secs(1),
        )
        .expect_err("the absent socket refuses");
        assert_eq!(error.preflight_code(), "helper_io_failed");

        let oversized = vec![b'x'; super::MAX_HELPER_MESSAGE_BYTES + 1];
        let error = call_helper(
            Path::new("/nonexistent"),
            &oversized,
            Duration::from_secs(1),
        )
        .expect_err("a frame above the ceiling is refused");
        assert_eq!(error.preflight_code(), "helper_message_framing_invalid");
        assert_eq!(
            error.refusal_bound(),
            Some((
                Some(super::MAX_HELPER_MESSAGE_BYTES as u64),
                super::MAX_HELPER_MESSAGE_BYTES as u64 + 1,
            ))
        );
    }

    #[test]
    fn request_is_owner_only_atomic_and_idempotent() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("requests");
        let path = write_request(&root, &"a".repeat(64), b"{}").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"{}");
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(write_request(&root, &"a".repeat(64), b"{}").unwrap(), path);
        assert!(write_request(&root, &"a".repeat(64), b"[]").is_err());
    }

    #[test]
    fn request_root_may_not_be_a_symlink() {
        let temp = tempfile::tempdir().unwrap();
        let target = temp.path().join("target");
        fs::create_dir(&target).unwrap();
        let link = temp.path().join("link");
        symlink(&target, &link).unwrap();
        assert!(write_request(&link, &"a".repeat(64), b"{}").is_err());
    }

    #[test]
    fn request_encoding_refusal_names_the_request_body_contract() {
        // Wrong implementation: a request body or signed grant that could not be
        // canonically encoded collapsed into `helper_protocol_invalid`, which an
        // operator could not tell apart from a corrupt reply.
        let error = HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestEncoding);
        assert_eq!(error.preflight_code(), "helper_request_encoding_invalid");
        assert!(error.diagnostic().is_none());
    }

    #[test]
    fn helper_call_join_refusal_names_the_blocking_worker() {
        // Wrong implementation: a blocking helper-call worker that failed to
        // join collapsed into `helper_protocol_invalid`.
        let error = HostRuntimeError::HelperProtocol(HelperProtocolCause::HelperCallJoin);
        assert_eq!(error.preflight_code(), "helper_call_join_failed");
        assert!(error.diagnostic().is_none());
    }

    #[test]
    fn helper_message_framing_refusal_names_the_message_contract() {
        // Wrong implementation: an empty, oversized, truncated or undecodable
        // length-prefixed helper message collapsed into
        // `helper_protocol_invalid`.
        let oversized = vec![0_u8; super::MAX_HELPER_MESSAGE_BYTES + 1];
        for body in [&b""[..], &oversized[..]] {
            let error = call_helper(Path::new("/nonexistent"), body, Duration::from_secs(1))
                .expect_err("an out-of-range request body is refused before connecting");
            assert_eq!(error.preflight_code(), "helper_message_framing_invalid");
        }

        let temp = tempfile::tempdir().unwrap();
        let socket = temp.path().join("helper.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let server = std::thread::spawn(move || {
            for reply in [
                &0_u32.to_be_bytes()[..],
                &(super::MAX_HELPER_MESSAGE_BYTES as u32 + 1).to_be_bytes()[..],
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut prefix = [0_u8; 4];
                stream.read_exact(&mut prefix).unwrap();
                let mut body = vec![0_u8; u32::from_be_bytes(prefix) as usize];
                stream.read_exact(&mut body).unwrap();
                stream.write_all(reply).unwrap();
            }
            let (mut stream, _) = listener.accept().unwrap();
            let mut prefix = [0_u8; 4];
            stream.read_exact(&mut prefix).unwrap();
            let mut body = vec![0_u8; u32::from_be_bytes(prefix) as usize];
            stream.read_exact(&mut body).unwrap();
            let undecodable = b"not-json";
            stream
                .write_all(&(undecodable.len() as u32).to_be_bytes())
                .unwrap();
            stream.write_all(undecodable).unwrap();
        });
        for attempt in 0..3 {
            let error = match call_helper(&socket, b"{}", Duration::from_secs(5)) {
                Ok(_) => panic!("helper reply {attempt} must be refused"),
                Err(error) => error,
            };
            assert_eq!(error.preflight_code(), "helper_message_framing_invalid");
            assert!(error.diagnostic().is_none());
        }
        server.join().unwrap();
    }

    #[test]
    fn foreign_request_identity_refusal_names_the_binding_contract() {
        // Wrong implementation: a reply that named a different request, or
        // declared a schema this agent cannot bind, collapsed into
        // `helper_protocol_invalid`.
        let request_id = "10000000-0000-4000-8000-000000000001";
        let mut foreign: super::HelperResponse = vonk_agent_protocol::parse_strict(
            br#"{"schema_version":1,"request_id":"20000000-0000-4000-8000-000000000002","status":"rejected","evidence_sha256":null,"error_code":"grant_unauthorized"}"#,
        )
        .unwrap();
        let error = require_bound_response(&foreign, request_id)
            .expect_err("a reply bound to another request must be refused");
        assert_eq!(error.preflight_code(), "helper_response_unbound");
        assert!(error.diagnostic().is_none());

        // The wire schema pins `schema_version` to 1, so a reply that declares
        // another schema can only arrive through a decoding path that skipped
        // that constraint. The binding check still refuses it.
        foreign.schema_version = 2;
        foreign.request_id = Some(Uuid::parse_str(request_id).unwrap());
        let error = require_bound_response(&foreign, request_id)
            .expect_err("a reply declaring another schema must be refused");
        assert_eq!(error.preflight_code(), "helper_response_unbound");
    }

    #[test]
    fn malformed_helper_rejection_names_the_rejection_contract() {
        // Wrong implementation: a rejection whose status, evidence, exit code or
        // code broke the rejection contract, or a diagnostic attached to
        // anything but `(RunInspect, runtime_process_exited)`, collapsed into
        // `helper_protocol_invalid`.
        let request_id = "10000000-0000-4000-8000-000000000001";
        let digest = "a".repeat(64);
        let baseline: super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":"{request_id}","status":"rejected","evidence_sha256":null,"error_code":"operation_failed"}}"#
            )
            .as_bytes(),
        )
        .unwrap();

        // A status other than `rejected`.
        let other_status: super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":"{request_id}","status":"container-runtime-request-executed","evidence_sha256":null,"error_code":"operation_failed"}}"#
            )
            .as_bytes(),
        )
        .unwrap();
        assert_rejection_malformed(&other_status, HostRuntimeAction::RunInspect);

        // A rejection never carries execution evidence, an exit code or the
        // observation receipt only an executed run owns.
        let mut response = baseline.clone();
        response.evidence_sha256 = Some(digest);
        assert_rejection_malformed(&response, HostRuntimeAction::RunInspect);
        let mut response = baseline.clone();
        response.exit_code = Some(0);
        assert_rejection_malformed(&response, HostRuntimeAction::RunInspect);
        let mut response = baseline.clone();
        response.observation_receipt = Some(signed_receipt(
            &Ed25519KeyPair::from_seed_unchecked(&[7; 32]).unwrap(),
            Uuid::new_v4(),
        ));
        assert_rejection_malformed(&response, HostRuntimeAction::RunInspect);

        // A code outside the stable set.
        let mut response = baseline.clone();
        response.error_code = Some("untrusted_response_detail".into());
        assert_rejection_malformed(&response, HostRuntimeAction::RunInspect);

        // A diagnostic is only meaningful for
        // `(RunInspect, runtime_process_exited)`.
        let mut response = baseline.clone();
        response.diagnostic = Some("private detail".into());
        assert_rejection_malformed(&response, HostRuntimeAction::RunInspect);

        // A rejection must name a code at all.
        let mut response = baseline;
        response.error_code = None;
        assert_rejection_malformed(&response, HostRuntimeAction::RunInspect);
    }

    fn assert_rejection_malformed(response: &super::HelperResponse, action: HostRuntimeAction) {
        let error = runtime_rejection(response, action);
        assert_eq!(error.preflight_code(), "helper_rejection_malformed");
        assert!(error.diagnostic().is_none());
    }

    #[test]
    fn malformed_executed_outcome_names_the_outcome_contract() {
        // Wrong implementation: an executed outcome that carried a diagnostic,
        // named the wrong status, omitted or mis-spelled its evidence digest, or
        // reported a non-byte exit code collapsed into
        // `helper_protocol_invalid`.
        let request_id = "10000000-0000-4000-8000-000000000001";
        let digest = "a".repeat(64);
        let baseline: super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":"{request_id}","status":"container-runtime-request-executed","evidence_sha256":"{digest}"}}"#
            )
            .as_bytes(),
        )
        .unwrap();

        // A capture diagnostic is not part of an executed outcome.
        let mut response = baseline.clone();
        response.diagnostic = Some("private detail".into());
        assert_outcome_malformed(&response);

        // The status must be the executed one unless it is stop-uncertain.
        let rejected: super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":"{request_id}","status":"rejected","evidence_sha256":null}}"#
            )
            .as_bytes(),
        )
        .unwrap();
        assert_outcome_malformed(&rejected);

        // The evidence digest must be present, lowercase and 64 hex characters.
        let mut response = baseline.clone();
        response.evidence_sha256 = None;
        assert_outcome_malformed(&response);
        let mut response = baseline.clone();
        response.evidence_sha256 = Some("A".repeat(64));
        assert_outcome_malformed(&response);

        // The exit code must fit a process byte.
        let mut response = baseline.clone();
        response.exit_code = Some(256);
        assert_outcome_malformed(&response);

        // An executed outcome never carries the signed inspection receipt only a
        // rejection-free inspection does.
        let mut response = baseline;
        response.observation_receipt = Some(signed_receipt(
            &Ed25519KeyPair::from_seed_unchecked(&[7; 32]).unwrap(),
            Uuid::new_v4(),
        ));
        assert_outcome_malformed(&response);

        // The deliberate stop-uncertain outcome and a byte-sized exit code stay
        // accepted.
        let stop_uncertain: super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":"{request_id}","status":"container-runtime-stop-uncertain","evidence_sha256":"{digest}","exit_code":137}}"#
            )
            .as_bytes(),
        )
        .unwrap();
        assert!(require_executed_outcome(&stop_uncertain, true).is_ok());
    }

    fn assert_outcome_malformed(response: &super::HelperResponse) {
        let error = require_executed_outcome(response, false)
            .expect_err("a malformed executed outcome must be refused");
        assert_eq!(error.preflight_code(), "helper_outcome_malformed");
        assert!(error.diagnostic().is_none());
    }

    /// The exact Start request shape `execute_bound` sends, so each rule test
    /// drives the canonical validator instead of asserting a bare constant.
    fn start_request() -> super::HostRuntimeRequest {
        super::HostRuntimeRequest {
            schema_version: 1,
            action: HostRuntimeAction::Start,
            job_id: Uuid::new_v4(),
            operation_id: Uuid::new_v4(),
            attempt: 1,
            fence: Uuid::new_v4(),
            arguments: vec!["sha256:image".to_owned(), "run".to_owned()],
            observation: None,
            installation_id: None,
            reconciliation_identity: None,
        }
    }

    fn request_rule_code(request: &super::HostRuntimeRequest) -> String {
        let rule = request
            .validate()
            .expect_err("this request must violate the rule under test");
        let error = HostRuntimeError::request_refusal(rule);
        assert!(error.diagnostic().is_none());
        error.preflight_code()
    }

    #[test]
    fn request_schema_version_refusal_names_the_schema_rule() {
        // Wrong implementation: a request whose schema version was not current
        // collapsed into `helper_request_document_invalid`, so a live Start
        // could not say which envelope rule refused it.
        let mut request = start_request();
        request.schema_version = 2;
        assert_eq!(
            request_rule_code(&request),
            "helper_request_schema_version_invalid"
        );
    }

    #[test]
    fn request_attempt_refusal_names_the_attempt_rule() {
        // Wrong implementation: a zero attempt collapsed into
        // `helper_request_document_invalid`.
        let mut request = start_request();
        request.attempt = 0;
        assert_eq!(
            request_rule_code(&request),
            "helper_request_attempt_invalid"
        );
    }

    #[test]
    fn request_arguments_presence_refusal_names_the_presence_rule() {
        // Wrong implementation: a Start with no arguments, or a preflight that
        // carried them, collapsed into `helper_request_document_invalid`.
        let mut absent = start_request();
        absent.arguments.clear();
        assert_eq!(
            request_rule_code(&absent),
            "helper_request_arguments_presence_invalid"
        );

        let mut present = start_request();
        present.action = HostRuntimeAction::RuntimePreflight;
        assert_eq!(
            request_rule_code(&present),
            "helper_request_arguments_presence_invalid"
        );
    }

    #[test]
    fn request_installation_identity_refusal_names_the_identity_rule() {
        // Wrong implementation: an installation identity on an action that is
        // not cleanup collapsed into `helper_request_document_invalid`.
        let mut request = start_request();
        request.installation_id = Some(Uuid::new_v4());
        assert_eq!(
            request_rule_code(&request),
            "helper_request_installation_identity_invalid"
        );
    }

    fn request_at_bytes(target: usize) -> super::HostRuntimeRequest {
        let mut request = start_request();
        let base = vonk_agent_protocol::canonical_json(&request)
            .expect("a start request canonically encodes")
            .len();
        request.arguments.push("x".repeat(target - base - 3));
        request
    }

    #[test]
    fn request_bytes_refusal_names_the_request_bound() {
        // Wrong implementation: a request whose canonical document outgrew the
        // bounded helper exchange collapsed into
        // `helper_request_document_invalid`, so a refused Start could not say
        // which rule or which bound refused it.
        let request = request_at_bytes(vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES + 1);
        assert_eq!(request_rule_code(&request), "helper_request_bytes_invalid");
    }

    #[test]
    fn request_argument_refusals_name_the_argument_kind() {
        // Wrong implementation: an empty argument, or one carrying a byte an
        // exec argv cannot frame, collapsed into
        // `helper_request_document_invalid`, so the code could not name the
        // kind of violation rather than an index.
        let mut nul = start_request();
        nul.arguments = vec!["sha256:image".to_owned(), "run\0--flag".to_owned()];
        assert_eq!(request_rule_code(&nul), "helper_request_argument_nul_byte");
    }

    #[test]
    fn a_large_or_multiline_argument_is_admitted() {
        // The authoritative size limit is the canonical request byte ceiling,
        // not a per-argument round number: an inline engine configuration can
        // exceed 4096 bytes, and CR/LF are legal bytes in an exec argv element.
        let mut long = start_request();
        long.arguments = vec![
            "sha256:image".to_owned(),
            format!(
                "--speculative-config={{\"capture\":\"{}\"}}",
                "x".repeat(8_192)
            ),
        ];
        assert!(
            long.validate().is_ok(),
            "a legitimate large inline configuration must be framed"
        );

        let mut multiline = start_request();
        multiline.arguments = vec![
            "sha256:image".to_owned(),
            "line one\nline two\r\n".to_owned(),
        ];
        assert!(
            multiline.validate().is_ok(),
            "CR/LF are legal argv bytes and must not be refused"
        );
    }

    #[test]
    fn a_request_at_the_byte_ceiling_is_admitted_and_one_byte_over_is_refused() {
        // Wrong implementation: the request byte budget equalled the frame
        // budget while its comment called it a backstop below it, and the
        // helper read enforced a private 64 KiB round number, so a request the
        // agent called valid could still be refused after a successful install.
        let limit = vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES;
        assert_eq!(request_at_bytes(limit).validate(), Ok(()));
        assert!(
            request_at_bytes(limit + 1).validate().is_err(),
            "one canonical byte over the request ceiling must be refused"
        );
    }

    #[test]
    fn a_request_bytes_refusal_carries_the_limit_and_the_observed_bytes() {
        // Wrong implementation: the refusal named the rule but not the bound, so
        // an operator could not tell one byte over from a thousand without
        // reading the constants.
        let limit = vonk_agent_protocol::MAX_HOST_RUNTIME_REQUEST_BYTES;
        let request = request_at_bytes(limit + 1);
        let rule = request
            .validate()
            .expect_err("the byte bound must be refused");
        let error = HostRuntimeError::request_refusal(rule);
        assert_eq!(error.preflight_code(), "helper_request_bytes_invalid");
        assert_eq!(
            error.refusal_bound(),
            Some((Some(limit as u64), limit as u64 + 1))
        );
    }

    #[test]
    fn a_per_argument_refusal_carries_the_element_length() {
        // Only the offending element's length crosses, never the element.
        let mut nul = start_request();
        nul.arguments = vec!["sha256:image".to_owned(), "run\0--flag".to_owned()];
        let rule = nul.validate().expect_err("a NUL argument must be refused");
        assert_eq!(
            HostRuntimeError::request_refusal(rule).refusal_bound(),
            Some((None, 10))
        );
    }

    #[test]
    fn inspection_observation_rules_keep_the_document_cause() {
        // The observation binding is an inspection contract, not one of the
        // argument-envelope rules, and a Start always carries
        // `observation: None`, so a Start can never reach these rules.
        for rule in [
            vonk_agent_protocol::HostRuntimeRequestRule::ObservationBinding,
            vonk_agent_protocol::HostRuntimeRequestRule::ObservationAction,
            vonk_agent_protocol::HostRuntimeRequestRule::Encoding,
        ] {
            assert_eq!(
                HostRuntimeError::HelperProtocol(HelperProtocolCause::from_request_rule(rule))
                    .preflight_code(),
                "helper_request_document_invalid"
            );
        }
    }

    #[test]
    fn request_storage_refusal_names_the_signed_request_file() {
        // Wrong implementation: a request root or signed request file that
        // violated the owner-only storage contract collapsed into
        // `helper_protocol_invalid`, which runs on every Start before the
        // helper call.
        let temp = tempfile::tempdir().unwrap();
        let permissive = temp.path().join("permissive");
        fs::create_dir(&permissive).unwrap();
        fs::set_permissions(&permissive, fs::Permissions::from_mode(0o755)).unwrap();
        let error = write_request(&permissive, &"a".repeat(64), b"{}")
            .expect_err("a group/world-readable request root must be refused");
        assert_eq!(error.preflight_code(), "helper_request_storage_invalid");
        assert!(error.diagnostic().is_none());

        // An existing signed request file with different bytes is refused
        // rather than overwritten.
        let root = temp.path().join("requests");
        let path = write_request(&root, &"b".repeat(64), b"{}").unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"{}");
        let error = write_request(&root, &"b".repeat(64), b"[]")
            .expect_err("a mismatched existing request file must be refused");
        assert_eq!(error.preflight_code(), "helper_request_storage_invalid");
    }

    #[test]
    fn system_clock_refusal_names_the_host_clock() {
        // Wrong implementation: a host clock before the Unix epoch collapsed
        // into `helper_protocol_invalid`. The conversion runs on the storage
        // nonce, so it can fire on Start. A pre-epoch clock cannot be staged in
        // a test; this pins the mapping both conversion sites use.
        let error = HostRuntimeError::HelperProtocol(HelperProtocolCause::SystemClock);
        assert_eq!(error.preflight_code(), "helper_system_clock_invalid");
        assert!(error.diagnostic().is_none());
    }

    #[test]
    fn inspection_receipt_refusal_names_the_inspection_reply() {
        // Wrong implementation: an executed RunInspect reply with the wrong
        // status, unexpected execution evidence, or no signed receipt collapsed
        // into `helper_protocol_invalid`.
        let request_id = "10000000-0000-4000-8000-000000000001";
        let digest = "a".repeat(64);
        let no_receipt: super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":"{request_id}","status":"container-runtime-request-executed","evidence_sha256":"{digest}"}}"#
            )
            .as_bytes(),
        )
        .unwrap();
        let error = match require_inspection_receipt(&no_receipt) {
            Ok(_) => panic!("an executed inspection without a receipt must be refused"),
            Err(error) => error,
        };
        assert_eq!(error.preflight_code(), "helper_inspection_receipt_invalid");
        assert!(error.diagnostic().is_none());

        let rejected: super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":"{request_id}","status":"rejected","evidence_sha256":null,"error_code":"operation_failed"}}"#
            )
            .as_bytes(),
        )
        .unwrap();
        let error = match require_inspection_receipt(&rejected) {
            Ok(_) => panic!("a rejection is never an executed inspection"),
            Err(error) => error,
        };
        assert_eq!(error.preflight_code(), "helper_inspection_receipt_invalid");
    }

    #[test]
    fn observation_receipt_refusal_names_the_signed_proof() {
        // Wrong implementation: a signed observation receipt that did not prove
        // this node, request, observation identity or freshness window collapsed
        // into `helper_protocol_invalid`.
        let signer = Ed25519KeyPair::from_seed_unchecked(&[7; 32]).unwrap();
        let request_id = Uuid::new_v4();
        let receipt = signed_receipt(&signer, request_id);
        let public_key: [u8; 32] = signer.public_key().as_ref().try_into().unwrap();
        let error = verify_observation_receipt(
            &receipt,
            &public_key,
            "spk_11111111111111111111111111111111",
            &request_id.to_string(),
            &"a".repeat(64),
            &"b".repeat(64),
            100,
            110,
            106,
        )
        .expect_err("a receipt for another node must be refused");
        assert_eq!(error.preflight_code(), "helper_observation_receipt_invalid");
        assert!(error.diagnostic().is_none());
    }

    #[test]
    fn observation_timestamp_refusal_names_the_receipt_time() {
        // Wrong implementation: a signed observation time outside the
        // representable range collapsed into `helper_protocol_invalid`. This is
        // the executor's conversion of `receipt.claims.observed_at`; an
        // unrepresentable value cannot be staged through the wire schema, so
        // this pins the mapping that site uses.
        let error = HostRuntimeError::HelperProtocol(HelperProtocolCause::ObservationTimestamp);
        assert_eq!(
            error.preflight_code(),
            "helper_observation_timestamp_invalid"
        );
        assert!(error.diagnostic().is_none());
    }

    #[test]
    fn stop_uncertain_is_named_without_pretending_the_reply_was_malformed() {
        // Wrong implementation: the executor's stop-uncertain short-circuit
        // returned `HostRuntimeError::Protocol`, so "we could not confirm the
        // stop" was reported as `helper_protocol_invalid` -- indistinguishable
        // from a corrupt reply. It is an ambiguous effect, not a malformed one.
        let error = HostRuntimeError::StopUncertain;
        assert_eq!(error.preflight_code(), "helper_stop_uncertain");
        assert!(error.diagnostic().is_none());
    }

    #[test]
    fn every_helper_protocol_cause_is_on_the_stable_allowlist() {
        // Wrong implementation: a cause whose code is absent from
        // `stable_runtime_error_code` silently fell back to
        // `helper_protocol_invalid`, which is the collapse this change removes.
        for cause in [
            HelperProtocolCause::RequestEncoding,
            HelperProtocolCause::HelperCallJoin,
            HelperProtocolCause::MessageFraming,
            HelperProtocolCause::ResponseUnbound,
            HelperProtocolCause::RejectionMalformed,
            HelperProtocolCause::OutcomeMalformed,
            HelperProtocolCause::RequestDocument,
            HelperProtocolCause::RequestSchemaVersion,
            HelperProtocolCause::RequestAttempt,
            HelperProtocolCause::RequestArgumentsPresence,
            HelperProtocolCause::RequestInstallationIdentity,
            HelperProtocolCause::RequestBytes,
            HelperProtocolCause::RequestArgumentNulByte,
            HelperProtocolCause::RequestStorage,
            HelperProtocolCause::SystemClock,
            HelperProtocolCause::InspectionReceipt,
            HelperProtocolCause::ObservationReceipt,
            HelperProtocolCause::ObservationTimestamp,
        ] {
            assert!(
                super::stable_runtime_error_code(cause.code()),
                "{} is not on the helper_<code> allowlist",
                cause.code()
            );
        }
    }
}
