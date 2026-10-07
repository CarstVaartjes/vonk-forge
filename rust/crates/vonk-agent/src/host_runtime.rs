//! Narrow client for controller-authorized host container-runtime operations.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use thiserror::Error;
use vonk_agent_protocol::generated::HostHelperResponse as HelperResponse;
use vonk_agent_protocol::generated::{
    HelperErrorCode, HostHelperResponseStatus, RuntimePreflightFindingCode,
};
use vonk_agent_protocol::{
    AgentClaim, HostRuntimeAction, HostRuntimeRequest, HostRuntimeRequestRule, RecipeJobRunRequest,
    RecipeReconciliationIdentity, RecipeRunInspectionRequest, RecipeStartRequest,
    RecipeStopRequest, canonical_generated_json, canonical_json, hex_sha256, parse_strict,
};

use crate::client::{AgentHttpClient, ClientError};
use crate::failure_evidence::{FailureProcessLogs, sanitize_tail};

/// The frame ceiling is owned by the wire contract so the agent, the upgrade
/// channel and the privileged helper cannot drift.
const MAX_HELPER_MESSAGE_BYTES: usize = vonk_agent_protocol::MAX_HELPER_FRAME_BYTES;

/// The existing background observation concurrency wave belongs to native
/// helper work, not to futures that may be cancelled before that work finishes.
/// Foreground lifecycle/diagnostic requests do not wait on this pool.
pub(crate) const BACKGROUND_RUN_INSPECTION_CONCURRENCY: usize = 8;
fn background_inspection_slots() -> std::sync::Arc<tokio::sync::Semaphore> {
    static SLOTS: std::sync::OnceLock<std::sync::Arc<tokio::sync::Semaphore>> =
        std::sync::OnceLock::new();
    SLOTS
        .get_or_init(|| {
            std::sync::Arc::new(tokio::sync::Semaphore::new(
                BACKGROUND_RUN_INSPECTION_CONCURRENCY,
            ))
        })
        .clone()
}

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
        code: HelperErrorCode,
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
    /// The agent-built request carries arguments for the wrong action.
    RequestArgumentsPresence,
    /// The typed lifecycle plan is missing, duplicated, or bound to another
    /// runtime generation.
    RequestPlanBinding,
    /// The agent-built request carries an installation identity for the wrong
    /// action.
    RequestInstallationIdentity,
    /// The agent-built request carries a canonical document larger than the
    /// bounded helper exchange reads.
    RequestBytes,
    /// A typed lifecycle plan exceeds its declared canonical byte ceiling.
    RequestPlanBytes,
    /// An agent-built request argument carries a NUL byte an exec argv cannot
    /// frame.
    RequestArgumentNulByte,
    /// The owner-only signed request file could not be established.
    RequestStorage,
    /// The host clock is before the Unix epoch.
    SystemClock,
    /// An inspection reply did not say whether the process is running.
    InspectionOutcome,
}

impl HelperProtocolCause {
    /// The stable contract code of this cause.
    pub fn code(self) -> HelperErrorCode {
        match self {
            Self::RequestEncoding => HelperErrorCode::RequestEncodingInvalid,
            Self::HelperCallJoin => HelperErrorCode::CallJoinFailed,
            Self::MessageFraming => HelperErrorCode::MessageFramingInvalid,
            Self::ResponseUnbound => HelperErrorCode::ResponseUnbound,
            Self::RejectionMalformed => HelperErrorCode::RejectionMalformed,
            Self::OutcomeMalformed => HelperErrorCode::OutcomeMalformed,
            Self::RequestDocument => HelperErrorCode::RequestDocumentInvalid,
            Self::RequestArgumentsPresence => HelperErrorCode::RequestArgumentsPresenceInvalid,
            Self::RequestPlanBinding => HelperErrorCode::RequestPlanBindingInvalid,
            Self::RequestInstallationIdentity => {
                HelperErrorCode::RequestInstallationIdentityInvalid
            }
            Self::RequestBytes => HelperErrorCode::RequestBytesInvalid,
            Self::RequestPlanBytes => HelperErrorCode::RequestPlanBytesInvalid,
            Self::RequestArgumentNulByte => HelperErrorCode::RequestArgumentNulByte,
            Self::RequestStorage => HelperErrorCode::RequestStorageInvalid,
            Self::SystemClock => HelperErrorCode::SystemClockInvalid,
            Self::InspectionOutcome => HelperErrorCode::InspectionOutcomeInvalid,
        }
    }

    /// The code this cause carries in failure evidence, in the `runtime_helper_`
    /// namespace.
    pub fn runtime_helper_code(self) -> HelperErrorCode {
        match self {
            Self::RequestEncoding => HelperErrorCode::RuntimeHelperRequestEncodingInvalid,
            Self::HelperCallJoin => HelperErrorCode::RuntimeHelperCallJoinFailed,
            Self::MessageFraming => HelperErrorCode::RuntimeHelperMessageFramingInvalid,
            Self::ResponseUnbound => HelperErrorCode::RuntimeHelperResponseUnbound,
            Self::RejectionMalformed => HelperErrorCode::RuntimeHelperRejectionMalformed,
            Self::OutcomeMalformed => HelperErrorCode::RuntimeHelperOutcomeMalformed,
            Self::RequestDocument => HelperErrorCode::RuntimeHelperRequestDocumentInvalid,
            Self::RequestArgumentsPresence => {
                HelperErrorCode::RuntimeHelperRequestArgumentsPresenceInvalid
            }
            Self::RequestPlanBinding => HelperErrorCode::RuntimeHelperRequestPlanBindingInvalid,
            Self::RequestInstallationIdentity => {
                HelperErrorCode::RuntimeHelperRequestInstallationIdentityInvalid
            }
            Self::RequestBytes => HelperErrorCode::RuntimeHelperRequestBytesInvalid,
            Self::RequestPlanBytes => HelperErrorCode::RuntimeHelperRequestPlanBytesInvalid,
            Self::RequestArgumentNulByte => HelperErrorCode::RuntimeHelperRequestArgumentNulByte,
            Self::RequestStorage => HelperErrorCode::RuntimeHelperRequestStorageInvalid,
            Self::SystemClock => HelperErrorCode::RuntimeHelperSystemClockInvalid,
            Self::InspectionOutcome => HelperErrorCode::RuntimeHelperInspectionOutcomeInvalid,
        }
    }

    /// Map one canonical request rule to the cause this agent reports. Every
    /// envelope rule keeps its own code so a refused Start names the rule and,
    /// for a measured bound, the limit and the observed value rather than one
    /// opaque label.
    pub fn from_request_rule(rule: HostRuntimeRequestRule) -> Self {
        match rule {
            HostRuntimeRequestRule::ArgumentsPresence => Self::RequestArgumentsPresence,
            HostRuntimeRequestRule::PlanBinding => Self::RequestPlanBinding,
            HostRuntimeRequestRule::InstallationIdentity => Self::RequestInstallationIdentity,
            HostRuntimeRequestRule::RequestBytes { .. } => Self::RequestBytes,
            HostRuntimeRequestRule::PlanBytes { .. } => Self::RequestPlanBytes,
            HostRuntimeRequestRule::ArgumentNulByte { .. } => Self::RequestArgumentNulByte,
            HostRuntimeRequestRule::Encoding => Self::RequestDocument,
        }
    }
}

impl HostRuntimeError {
    /// The closed finding code this failure reports in a runtime preflight
    /// result and, spelled without the finding prefix, in admission evidence.
    pub fn finding_code(&self) -> RuntimePreflightFindingCode {
        match self {
            Self::Io(_) => RuntimePreflightFindingCode::PreflightFindingHelperIoFailed,
            Self::Controller(ClientError::Protocol) => {
                RuntimePreflightFindingCode::PreflightFindingHelperGrantInvalid
            }
            Self::Controller(ClientError::Controller(error))
                if matches!(error.status, 401 | 403) =>
            {
                RuntimePreflightFindingCode::PreflightFindingHelperGrantUnauthorized
            }
            Self::Controller(_) => {
                RuntimePreflightFindingCode::PreflightFindingHelperGrantUnavailable
            }
            Self::HelperProtocol(cause) | Self::HelperProtocolBound { cause, .. } => {
                helper_finding_code(cause.code())
            }
            // An ambiguous stop is named for what it is, so it can never be
            // read as a malformed helper reply.
            Self::StopUncertain => RuntimePreflightFindingCode::PreflightFindingHelperStopUncertain,
            Self::HelperRejected { code, .. } => helper_finding_code(*code),
        }
    }

    /// Bounded, non-sensitive evidence for the admission receipt: the finding
    /// code's word without the finding prefix.
    pub fn preflight_code(&self) -> String {
        finding_word(self.finding_code())
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
    /// A one-shot job that did not exit cleanly: its exit account and the
    /// container's own output, captured by the helper before removal.
    pub diagnostic: Option<String>,
    pub process_logs: Option<Box<FailureProcessLogs>>,
}

/// What one read-only inspection of a managed run reported.
pub struct RunInspectionReport {
    pub running: bool,
    /// The running container's bounded, sanitized tail when it was requested.
    pub process_logs: Option<Box<FailureProcessLogs>>,
    /// Why a requested tail is missing.
    pub log_error: Option<String>,
}

/// The exact signed lifecycle plan carried beside a privileged runtime request.
/// Job runs use the parent's logical run identity while targeting the exact job
/// container; the typed plan keeps those identities distinct.
#[derive(Clone, Debug)]
pub enum HostRuntimePlan {
    Start(RecipeStartRequest),
    JobRun(RecipeJobRunRequest),
    Stop(RecipeStopRequest),
}

impl HostRuntimePlan {
    fn action(&self) -> HostRuntimeAction {
        match self {
            Self::Start(_) | Self::JobRun(_) => HostRuntimeAction::Start,
            Self::Stop(_) => HostRuntimeAction::Stop,
        }
    }
}

pub struct HostRuntimeBoundary<'a> {
    pub client: &'a AgentHttpClient,
    pub request_root: &'a Path,
    pub helper_socket: &'a Path,
}

struct RequestFileCleanup(PathBuf);

impl Drop for RequestFileCleanup {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

impl HostRuntimeBoundary<'_> {
    /// Ask the helper whether the exact container of one managed run is
    /// running.  The inspection is read-only, so it needs no Controller grant.
    pub async fn inspect_recipe_run(
        &self,
        arguments: Vec<String>,
    ) -> Result<bool, HostRuntimeError> {
        self.inspect_recipe_run_report(arguments, false)
            .await
            .map(|report| report.running)
    }

    pub async fn inspect_recipe_run_for_observation(
        &self,
        arguments: Vec<String>,
    ) -> Result<bool, HostRuntimeError> {
        let permit = background_inspection_slots()
            .acquire_owned()
            .await
            .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::HelperCallJoin))?;
        self.inspect_recipe_run_report_with_permit(arguments, false, Some(permit))
            .await
            .map(|report| report.running)
    }

    /// Like `inspect_recipe_run`, and when `include_logs` is set also reads the
    /// running container's bounded output without stopping it.
    pub async fn inspect_recipe_run_report(
        &self,
        arguments: Vec<String>,
        include_logs: bool,
    ) -> Result<RunInspectionReport, HostRuntimeError> {
        self.inspect_recipe_run_report_with_permit(arguments, include_logs, None)
            .await
    }

    async fn inspect_recipe_run_report_with_permit(
        &self,
        arguments: Vec<String>,
        include_logs: bool,
        permit: Option<tokio::sync::OwnedSemaphorePermit>,
    ) -> Result<RunInspectionReport, HostRuntimeError> {
        let request = HostRuntimeRequest {
            action: HostRuntimeAction::RunInspect,
            fence: uuid::Uuid::new_v4(),
            arguments,
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: None,
            start_plan: None,
            stop_plan: None,
        };
        request
            .validate()
            .map_err(HostRuntimeError::request_refusal)?;
        let body = canonical_json(&request)
            .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestEncoding))?;
        let digest = hex_sha256(&body);
        let request_path = write_request(self.request_root, &digest, &body)?;
        let request_cleanup = RequestFileCleanup(request_path);
        let request_id = uuid::Uuid::new_v4();
        let frame = canonical_generated_json(&RecipeRunInspectionRequest {
            request_id,
            request_sha256: digest,
            include_logs: include_logs.then_some(true),
        })
        .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::RequestEncoding))?;
        let helper_socket = self.helper_socket.to_path_buf();
        let response = tokio::task::spawn_blocking(move || {
            // Both native ownership and its request stay alive until actual
            // socket work returns, even when the awaiting page is cancelled.
            let _permit = permit;
            let _request_cleanup = request_cleanup;
            call_helper(&helper_socket, &frame, Duration::from_secs(15))
        })
        .await
        .map_err(|_| HostRuntimeError::HelperProtocol(HelperProtocolCause::HelperCallJoin))??;
        require_bound_response(&response, &request_id.to_string())?;
        if response.error_code.is_some() {
            return Err(runtime_rejection(&response, HostRuntimeAction::RunInspect));
        }
        if response.status != HostHelperResponseStatus::ContainerRuntimeRequestExecuted
            || response.exit_code.is_some()
        {
            return Err(HostRuntimeError::HelperProtocol(
                HelperProtocolCause::InspectionOutcome,
            ));
        }
        let running = response
            .process_running
            .ok_or(HostRuntimeError::HelperProtocol(
                HelperProtocolCause::InspectionOutcome,
            ))?;
        // A tail was never volunteered: only a request that asked for it may
        // receive one, and the reason it is missing is typed.
        if !include_logs && (response.process_logs.is_some() || response.diagnostic.is_some()) {
            return Err(HostRuntimeError::HelperProtocol(
                HelperProtocolCause::InspectionOutcome,
            ));
        }
        Ok(RunInspectionReport {
            running,
            process_logs: response.process_logs.as_ref().map(|logs| {
                Box::new(FailureProcessLogs {
                    stdout: sanitize_tail(&logs.stdout),
                    stderr: sanitize_tail(&logs.stderr),
                })
            }),
            log_error: response
                .diagnostic
                .as_deref()
                .map(crate::failure_evidence::sanitize_text),
        })
    }

    pub async fn execute(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        self.execute_bound(claim, action, arguments, None, None, None)
            .await
    }

    pub async fn execute_plan(
        &self,
        claim: &AgentClaim,
        arguments: Vec<String>,
        plan: HostRuntimePlan,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        self.execute_bound(claim, plan.action(), arguments, None, None, Some(plan))
            .await
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
            None,
            None,
        )
        .await
    }

    pub async fn reconcile_installation(
        &self,
        claim: &AgentClaim,
        identity: RecipeReconciliationIdentity,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        self.execute_bound(
            claim,
            HostRuntimeAction::InstallationCleanup,
            Vec::new(),
            Some(identity.installation_id),
            Some(identity),
            None,
        )
        .await
    }

    async fn execute_bound(
        &self,
        claim: &AgentClaim,
        action: HostRuntimeAction,
        arguments: Vec<String>,
        installation_id: Option<uuid::Uuid>,
        reconciliation_identity: Option<RecipeReconciliationIdentity>,
        lifecycle_plan: Option<HostRuntimePlan>,
    ) -> Result<HostRuntimeOutcome, HostRuntimeError> {
        let helper_timeout = match action {
            HostRuntimeAction::RuntimePreflight => Duration::from_secs(14),
            HostRuntimeAction::Start => lifecycle_plan
                .as_ref()
                .and_then(|plan| match plan {
                    HostRuntimePlan::JobRun(job) => job
                        .compiled_execution_plan
                        .job
                        .as_ref()
                        .map(|job| u64::from(job.timeout_seconds)),
                    _ => None,
                })
                .filter(|value| (1..=3600).contains(value))
                .map_or(Duration::from_secs(610), |value| {
                    // Leave room after the adapter deadline for bounded inspect/stop/remove and
                    // a truthful uncertainty response from the privileged helper.
                    Duration::from_secs(value + 120)
                }),
            HostRuntimeAction::Stop => lifecycle_plan
                .as_ref()
                .and_then(|plan| match plan {
                    HostRuntimePlan::Stop(stop) => Some(u64::from(stop.stop_timeout_seconds)),
                    _ => None,
                })
                .filter(|value| (1..=600).contains(value))
                .map_or(Duration::from_secs(45), |value| {
                    Duration::from_secs(value + 45)
                }),
            _ => Duration::from_secs(610),
        };
        let (start_plan, job_plan, stop_plan, run_generation) = match &lifecycle_plan {
            Some(HostRuntimePlan::Start(plan)) => {
                (Some(plan.clone()), None, None, Some(plan.run_generation))
            }
            Some(HostRuntimePlan::JobRun(plan)) => {
                (None, Some(plan.clone()), None, Some(plan.run_generation))
            }
            Some(HostRuntimePlan::Stop(plan)) => {
                (None, None, Some(plan.clone()), Some(plan.run_generation))
            }
            None => (None, None, None, None),
        };
        let request = HostRuntimeRequest {
            action,
            fence: claim.fence,
            arguments,
            job_plan,
            installation_id,
            reconciliation_identity: reconciliation_identity.clone(),
            run_generation,
            start_plan,
            stop_plan,
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
                .host_runtime_grant(claim, &request, &digest)
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
            let stop_uncertain =
                response.status == HostHelperResponseStatus::ContainerRuntimeStopUncertain;
            require_bound_response(&response, &request_id)?;
            if response.error_code.is_some() {
                return Err(runtime_rejection(&response, action));
            }
            require_executed_outcome(&response, stop_uncertain)?;
            Ok(HostRuntimeOutcome {
                exit_code: response.exit_code.map(|code| code as i32),
                stop_uncertain,
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
    crate::helper_codes::runtime_rejection(code)
        .is_some_and(crate::helper_codes::is_unbound_rejection)
}

/// The executed-outcome contract. A successful helper reply carries no
/// rejection, no capture diagnostic and no inspection outcome, names the
/// executed status unless it is the deliberate stop-uncertain outcome, and
/// reports a 64-character lowercase evidence digest with, at most, a byte-sized
/// exit code.
fn require_executed_outcome(
    response: &HelperResponse,
    stop_uncertain: bool,
) -> Result<(), HostRuntimeError> {
    let malformed = || HostRuntimeError::HelperProtocol(HelperProtocolCause::OutcomeMalformed);
    if response.process_running.is_some() {
        return Err(malformed());
    }
    // An exit account and output accompany only a job that did not exit
    // cleanly; they are evidence of that exit, never of a clean one.
    let evidence = response.diagnostic.is_some() || response.process_logs.is_some();
    if evidence && !response.exit_code.is_some_and(|code| code != 0)
        || !stop_uncertain
            && response.status != HostHelperResponseStatus::ContainerRuntimeRequestExecuted
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
    let Some(code) = response
        .error_code
        .as_deref()
        .and_then(crate::helper_codes::runtime_rejection)
    else {
        return HostRuntimeError::HelperProtocol(HelperProtocolCause::RejectionMalformed);
    };
    if response.status != HostHelperResponseStatus::Rejected
        || response.exit_code.is_some()
        || response.process_running.is_some()
        || response.diagnostic.is_some()
            && (action != HostRuntimeAction::RunInspect
                || code != HelperErrorCode::RuntimeProcessExited)
    {
        return HostRuntimeError::HelperProtocol(HelperProtocolCause::RejectionMalformed);
    }
    HostRuntimeError::HelperRejected {
        code,
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

/// The finding code of a helper code.  A code the runtime boundary does not
/// accept keeps the opaque `helper_protocol_invalid` label.
fn helper_finding_code(code: HelperErrorCode) -> RuntimePreflightFindingCode {
    use RuntimePreflightFindingCode as Finding;
    match code {
        HelperErrorCode::GrantInvalid => Finding::PreflightFindingHelperGrantInvalid,
        HelperErrorCode::GrantNodeMismatch => Finding::PreflightFindingHelperGrantNodeMismatch,
        HelperErrorCode::GrantUnauthorized => Finding::PreflightFindingHelperGrantUnauthorized,
        HelperErrorCode::PeerIdentityInvalid => Finding::PreflightFindingHelperPeerIdentityInvalid,
        HelperErrorCode::OperationInvalidArtifact => {
            Finding::PreflightFindingHelperOperationInvalidArtifact
        }
        HelperErrorCode::RuntimeImageIdentityInvalid => {
            Finding::PreflightFindingHelperRuntimeImageIdentityInvalid
        }
        HelperErrorCode::RequestReplayed => Finding::PreflightFindingHelperRequestReplayed,
        HelperErrorCode::OperationFailed => Finding::PreflightFindingHelperOperationFailed,
        HelperErrorCode::InstallationReconciliationBusy => {
            Finding::PreflightFindingHelperInstallationReconciliationBusy
        }
        HelperErrorCode::OperationInvalid => Finding::PreflightFindingHelperOperationInvalid,
        HelperErrorCode::OperationUnsafePath => Finding::PreflightFindingHelperOperationUnsafePath,
        HelperErrorCode::OperationCommandFailed => {
            Finding::PreflightFindingHelperOperationCommandFailed
        }
        HelperErrorCode::OperationStopUncertain => {
            Finding::PreflightFindingHelperOperationStopUncertain
        }
        HelperErrorCode::OperationIo => Finding::PreflightFindingHelperOperationIo,
        HelperErrorCode::RuntimeImageLoadFailed => {
            Finding::PreflightFindingHelperRuntimeImageLoadFailed
        }
        HelperErrorCode::RuntimeImageInspectFailed => {
            Finding::PreflightFindingHelperRuntimeImageInspectFailed
        }
        HelperErrorCode::RuntimeImageReceiptFailed => {
            Finding::PreflightFindingHelperRuntimeImageReceiptFailed
        }
        HelperErrorCode::RuntimeProcessExited => {
            Finding::PreflightFindingHelperRuntimeProcessExited
        }
        HelperErrorCode::RuntimeRunMissing => Finding::PreflightFindingHelperRuntimeRunMissing,
        HelperErrorCode::RuntimeFabricUnavailable => {
            Finding::PreflightFindingHelperRuntimeFabricUnavailable
        }
        HelperErrorCode::RuntimeFabricFirewallRejected => {
            Finding::PreflightFindingHelperRuntimeFabricFirewallRejected
        }
        HelperErrorCode::RuntimeEndpointFirewallRejected => {
            Finding::PreflightFindingHelperRuntimeEndpointFirewallRejected
        }
        HelperErrorCode::InstallationReconciliationStorageUnavailable => {
            Finding::PreflightFindingHelperInstallationReconciliationStorageUnavailable
        }
        HelperErrorCode::RequestInvalid => Finding::PreflightFindingHelperRequestInvalid,
        HelperErrorCode::RequestLedgerFailed => Finding::PreflightFindingHelperRequestLedgerFailed,
        HelperErrorCode::RequestEncodingInvalid => {
            Finding::PreflightFindingHelperRequestEncodingInvalid
        }
        HelperErrorCode::CallJoinFailed => Finding::PreflightFindingHelperCallJoinFailed,
        HelperErrorCode::MessageFramingInvalid => {
            Finding::PreflightFindingHelperMessageFramingInvalid
        }
        HelperErrorCode::ResponseUnbound => Finding::PreflightFindingHelperResponseUnbound,
        HelperErrorCode::RejectionMalformed => Finding::PreflightFindingHelperRejectionMalformed,
        HelperErrorCode::OutcomeMalformed => Finding::PreflightFindingHelperOutcomeMalformed,
        HelperErrorCode::RequestDocumentInvalid => {
            Finding::PreflightFindingHelperRequestDocumentInvalid
        }
        HelperErrorCode::RequestSchemaVersionInvalid => {
            Finding::PreflightFindingHelperRequestSchemaVersionInvalid
        }
        HelperErrorCode::RequestAttemptInvalid => {
            Finding::PreflightFindingHelperRequestAttemptInvalid
        }
        HelperErrorCode::RequestArgumentsPresenceInvalid => {
            Finding::PreflightFindingHelperRequestArgumentsPresenceInvalid
        }
        HelperErrorCode::RequestPlanBindingInvalid => {
            Finding::PreflightFindingHelperRequestPlanBindingInvalid
        }
        HelperErrorCode::RequestInstallationIdentityInvalid => {
            Finding::PreflightFindingHelperRequestInstallationIdentityInvalid
        }
        HelperErrorCode::RequestBytesInvalid => Finding::PreflightFindingHelperRequestBytesInvalid,
        HelperErrorCode::RequestPlanBytesInvalid => {
            Finding::PreflightFindingHelperRequestPlanBytesInvalid
        }
        HelperErrorCode::RequestArgumentNulByte => {
            Finding::PreflightFindingHelperRequestArgumentNulByte
        }
        HelperErrorCode::RequestStorageInvalid => {
            Finding::PreflightFindingHelperRequestStorageInvalid
        }
        HelperErrorCode::SystemClockInvalid => Finding::PreflightFindingHelperSystemClockInvalid,
        HelperErrorCode::InspectionOutcomeInvalid => {
            Finding::PreflightFindingHelperInspectionOutcomeInvalid
        }
        _ => Finding::PreflightFindingHelperProtocolInvalid,
    }
}

/// The finding code without its `preflight_finding.` domain prefix: the word
/// an operator reads in an admission receipt.
pub fn finding_word(code: RuntimePreflightFindingCode) -> String {
    let word = code.to_string();
    word.strip_prefix("preflight_finding.")
        .map_or(word.clone(), str::to_owned)
}

fn write_request(root: &Path, digest: &str, body: &[u8]) -> Result<PathBuf, HostRuntimeError> {
    // Publish the directory with its owner-only mode in the mkdir itself.
    // Parallel callers must never observe an intermediate permissive root.
    match fs::DirBuilder::new().mode(0o700).create(root) {
        Ok(()) => {}
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

#[cfg(test)]
mod tests {
    use super::{
        HelperErrorCode, HelperProtocolCause, HostRuntimeError, RuntimePreflightFindingCode,
        call_helper, finding_word, require_bound_response, require_executed_outcome,
        runtime_rejection, write_request,
    };
    use std::fs;
    use std::io::{Read, Write};
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::os::unix::net::UnixListener;
    use std::path::Path;
    use std::time::Duration;
    use uuid::Uuid;
    use vonk_agent_protocol::{HostRuntimeAction, RecipeStartRequest};

    #[test]
    fn request_root_publication_has_no_permissive_intermediate_state() {
        const CHILD: &str = "VONK_REQUEST_ROOT_PUBLICATION_COUNTERPROOF";
        if std::env::var_os(CHILD).is_none() {
            // Umask is process-wide. Give only this isolated child the old
            // publisher's ordinary 022 umask; parallel tests remain untouched.
            let mut child = std::process::Command::new("sh")
                .args(["-c", "umask 022; exec \"$@\"", "request-root-counterproof"])
                .arg(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "host_runtime::tests::request_root_publication_has_no_permissive_intermediate_state",
                    "--nocapture",
                ])
                .env(CHILD, "1")
                .spawn()
                .unwrap();
            let deadline = std::time::Instant::now() + Duration::from_secs(30);
            // Reserve one second inside the existing total budget to reap
            // this exact child; killing is not itself proof of completion.
            let work_deadline = deadline - Duration::from_secs(1);
            loop {
                if let Some(status) = child.try_wait().unwrap() {
                    assert!(
                        status.success(),
                        "request root publication counterproof failed"
                    );
                    return;
                }
                if std::time::Instant::now() >= work_deadline {
                    let kill_error = child.kill().err();
                    while std::time::Instant::now() < deadline {
                        if child.try_wait().unwrap().is_some() {
                            panic!(
                                "request root publication counterproof exceeded its elapsed budget; exact child reaped"
                            );
                        }
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    panic!(
                        "request root publication child {} remains unreaped after its elapsed budget; kill error: {:?}",
                        child.id(),
                        kill_error
                    );
                }
                std::thread::sleep(Duration::from_millis(1));
            }
        }
        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        let temp = tempfile::tempdir().unwrap();
        let old_root = temp.path().join("two-phase");
        // Pause the old real two-phase publisher after mkdir, before chmod.
        // Another actual writer must refuse the published unsafe root.
        fs::create_dir(&old_root).unwrap();
        assert_eq!(
            fs::metadata(&old_root).unwrap().permissions().mode() & 0o777,
            0o755
        );
        let (result_sender, result_receiver) = std::sync::mpsc::channel();
        let writer_root = old_root.clone();
        let writer = std::thread::spawn(move || {
            let result = write_request(&writer_root, &"a".repeat(64), b"{}");
            let _ = result_sender.send(result);
        });
        let result = match result_receiver
            .recv_timeout(deadline.saturating_duration_since(std::time::Instant::now()))
        {
            Ok(result) => result,
            Err(error) => {
                let retained = temp.keep();
                panic!(
                    "request root writer outcome unresolved ({error}); owned fixture retained at {}",
                    retained.display()
                );
            }
        };
        while !writer.is_finished() && std::time::Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(1));
        }
        if !writer.is_finished() {
            let retained = temp.keep();
            panic!(
                "request root writer completion unresolved; owned fixture retained at {}",
                retained.display()
            );
        }
        // The same owned handle was observed finished. Joining only surfaces
        // its panic; it cannot wait for further writer work or fixture cleanup.
        writer.join().unwrap();
        let old_error =
            result.expect_err("another writer must refuse the unsafe intermediate directory");
        assert_eq!(old_error.preflight_code(), "helper_request_storage_invalid");
        fs::set_permissions(&old_root, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(write_request(&old_root, &"a".repeat(64), b"{}").is_ok());

        let atomic_root = temp.path().join("atomic");
        let path = write_request(&atomic_root, &"b".repeat(64), b"{}").unwrap();
        assert_eq!(
            fs::metadata(&atomic_root).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(fs::read(path).unwrap(), b"{}");
        // The cancellation proof below starts all eight native calls against
        // one absent root, and keeps their actual files through cancellation.
    }

    /// Real Unix framing and the production spawn_blocking boundary prove
    /// ownership survives abandoned logical pages. Only native process-running
    /// evidence is a fixture response; permits/socket/request cleanup are real.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn cancelled_observation_pages_keep_native_slots_and_leave_foreground_work_ready() {
        use super::{BACKGROUND_RUN_INSPECTION_CONCURRENCY, HostRuntimeBoundary};
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
        use std::sync::{Arc, Condvar, Mutex};
        struct Gate {
            release: Arc<(Mutex<bool>, Condvar)>,
            stop: Arc<AtomicBool>,
            server: Option<std::thread::JoinHandle<()>>,
            tasks: Arc<Mutex<Vec<tokio::task::AbortHandle>>>,
            deadline: std::time::Instant,
            fixture: Option<tempfile::TempDir>,
        }
        impl Drop for Gate {
            fn drop(&mut self) {
                let mut cleanup_complete = true;
                let tasks = self.tasks.lock().unwrap_or_else(|error| error.into_inner());
                for task in tasks.iter() {
                    task.abort();
                }
                *self
                    .release
                    .0
                    .lock()
                    .unwrap_or_else(|error| error.into_inner()) = true;
                self.release.1.notify_all();
                self.stop.store(true, Ordering::SeqCst);
                if let Some(server) = self.server.take() {
                    while !server.is_finished() && std::time::Instant::now() < self.deadline {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    // Never turn a bounded cleanup into an unbounded join.
                    // A still-running server keeps the fixture below intact.
                    if server.is_finished() {
                        cleanup_complete &= server.join().is_ok();
                    } else {
                        cleanup_complete = false;
                    }
                }
                while tasks.iter().any(|task| !task.is_finished())
                    && std::time::Instant::now() < self.deadline
                {
                    std::thread::sleep(Duration::from_millis(1));
                }
                cleanup_complete &= tasks.iter().all(|task| task.is_finished());
                // An aborted async task can leave real spawn_blocking work
                // alive. Returning all permits fences its request cleanup.
                let slots = super::background_inspection_slots();
                loop {
                    if let Ok(permits) = slots
                        .clone()
                        .try_acquire_many_owned(BACKGROUND_RUN_INSPECTION_CONCURRENCY as u32)
                    {
                        drop(permits);
                        break;
                    }
                    if std::time::Instant::now() >= self.deadline {
                        cleanup_complete = false;
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(1));
                }
                if !cleanup_complete {
                    // Files remain owned by unresolved native calls. Do not
                    // turn a deadline or a failed reaper into false absence.
                    if let Some(fixture) = self.fixture.take() {
                        let _retained = fixture.keep();
                    }
                    eprintln!("inspection fixture cleanup unresolved; owned files retained");
                    if !std::thread::panicking() {
                        panic!("inspection fixture cleanup exceeded its elapsed budget or failed");
                    }
                }
            }
        }
        let deadline = std::time::Instant::now() + Duration::from_secs(30);
        let temp = tempfile::tempdir().unwrap();
        let socket = temp.path().join("inspection.sock");
        let requests = temp.path().join("requests");
        let listener = UnixListener::bind(&socket).unwrap();
        listener.set_nonblocking(true).unwrap();
        let active = Arc::new(AtomicUsize::new(0));
        let maximum = Arc::new(AtomicUsize::new(0));
        let started = Arc::new(AtomicUsize::new(0));
        let release = Arc::new((Mutex::new(false), Condvar::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let tasks = Arc::new(Mutex::new(Vec::new()));
        let mut gate = Gate {
            release: release.clone(),
            stop: stop.clone(),
            server: None,
            tasks: tasks.clone(),
            deadline,
            fixture: Some(temp),
        };
        let native_active = active.clone();
        let native_maximum = maximum.clone();
        let native_started = started.clone();
        let native_requests = requests.clone();
        let server = std::thread::spawn(move || {
            let mut workers = Vec::new();
            while !stop.load(Ordering::SeqCst) {
                assert!(
                    std::time::Instant::now() < deadline,
                    "inspection fixture server exceeded its elapsed budget"
                );
                let (mut stream, _) = match listener.accept() {
                    Ok(connection) => connection,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(1));
                        continue;
                    }
                    Err(error) => panic!("inspection listener: {error}"),
                };
                let active = native_active.clone();
                let maximum = native_maximum.clone();
                let started = native_started.clone();
                let release = release.clone();
                let requests = native_requests.clone();
                workers.push(std::thread::spawn(move || {
                    stream
                        .set_read_timeout(Some(Duration::from_secs(2)))
                        .unwrap();
                    stream
                        .set_write_timeout(Some(Duration::from_secs(2)))
                        .unwrap();
                    let mut prefix = [0; 4];
                    stream.read_exact(&mut prefix).unwrap();
                    let mut body = vec![0; u32::from_be_bytes(prefix) as usize];
                    stream.read_exact(&mut body).unwrap();
                    let request: vonk_agent_protocol::RecipeRunInspectionRequest =
                        vonk_agent_protocol::parse_strict(&body).unwrap();
                    let background = request.include_logs != Some(true);
                    if background {
                        let current = active.fetch_add(1, Ordering::SeqCst) + 1;
                        maximum.fetch_max(current, Ordering::SeqCst);
                        started.fetch_add(1, Ordering::SeqCst);
                        let mut released = release.0.lock().unwrap();
                        while !*released {
                            let remaining = deadline
                                .checked_duration_since(std::time::Instant::now())
                                .expect("inspection fixture release exceeded its elapsed budget");
                            let (next, timeout) =
                                release.1.wait_timeout(released, remaining).unwrap();
                            released = next;
                            assert!(
                                !timeout.timed_out() || *released,
                                "inspection fixture release timed out"
                            );
                        }
                        // Dropping an awaiting logical page must neither free
                        // the native permit nor delete its still-owned request.
                        assert!(
                            requests
                                .join(format!("{}.json", request.request_sha256))
                                .exists()
                        );
                    }
                    let response = super::HelperResponse {
                        schema_version: 1,
                        request_id: Some(request.request_id),
                        status: super::HostHelperResponseStatus::ContainerRuntimeRequestExecuted,
                        process_running: Some(true),
                        exit_code: None,
                        error_code: None,
                        diagnostic: None,
                        process_logs: None,
                    };
                    let body = vonk_agent_protocol::canonical_generated_json(&response).unwrap();
                    if background {
                        // Native inspection is finished before its reply makes
                        // the client's permit available to the next call.
                        active.fetch_sub(1, Ordering::SeqCst);
                    }
                    stream
                        .write_all(&(body.len() as u32).to_be_bytes())
                        .unwrap();
                    stream.write_all(&body).unwrap();
                }));
            }
            let mut worker_failed = false;
            for worker in workers {
                while !worker.is_finished() {
                    assert!(
                        std::time::Instant::now() < deadline,
                        "inspection fixture worker exceeded its elapsed budget"
                    );
                    std::thread::sleep(Duration::from_millis(1));
                }
                worker_failed |= std::thread::JoinHandle::join(worker).is_err();
            }
            assert!(!worker_failed, "inspection fixture worker failed");
        });
        gate.server = Some(server);
        let spawn = |background: bool| {
            let socket = socket.clone();
            let requests = requests.clone();
            let task = tokio::spawn(async move {
                let client = crate::client::AgentHttpClient::for_http_test(
                    "http://127.0.0.1:9/",
                    "spk_0123456789abcdef0123456789abcdef",
                );
                let boundary = HostRuntimeBoundary {
                    client: &client,
                    request_root: &requests,
                    helper_socket: &socket,
                };
                let arguments = vec![Uuid::new_v4().to_string()];
                if background {
                    boundary.inspect_recipe_run_for_observation(arguments).await
                } else {
                    boundary
                        .inspect_recipe_run_report(arguments, true)
                        .await
                        .map(|report| report.running)
                }
            });
            tasks.lock().unwrap().push(task.abort_handle());
            task
        };
        let mut first: Vec<_> = (0..BACKGROUND_RUN_INSPECTION_CONCURRENCY)
            .map(|_| spawn(true))
            .collect();
        tokio::time::timeout(Duration::from_secs(3), async {
            while active.load(Ordering::SeqCst) != BACKGROUND_RUN_INSPECTION_CONCURRENCY {
                for task in &mut first {
                    if task.is_finished() {
                        match task.await {
                            Ok(Err(error)) => panic!(
                                "inspection fixture startup refused request: {}",
                                error.preflight_code()
                            ),
                            Ok(Ok(_)) => {
                                panic!("inspection fixture unexpectedly completed before release")
                            }
                            Err(_) => panic!("inspection fixture startup task failed to join"),
                        }
                    }
                }
                assert!(
                    std::time::Instant::now() < deadline,
                    "inspection fixture startup exceeded its elapsed budget"
                );
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .unwrap();
        for task in first {
            task.abort();
            let _ = task.await;
        }
        for _ in 0..3 {
            let page: Vec<_> = (0..BACKGROUND_RUN_INSPECTION_CONCURRENCY)
                .map(|_| spawn(true))
                .collect();
            tokio::time::sleep(Duration::from_millis(20)).await;
            for task in page {
                task.abort();
                let _ = task.await;
            }
            assert_eq!(
                active.load(Ordering::SeqCst),
                BACKGROUND_RUN_INSPECTION_CONCURRENCY
            );
            assert_eq!(
                started.load(Ordering::SeqCst),
                BACKGROUND_RUN_INSPECTION_CONCURRENCY
            );
        }
        // A real foreground boundary call has its own lifecycle lane. It is
        // not stuck behind permits still owned by abandoned observer futures.
        assert!(
            tokio::time::timeout(Duration::from_secs(2), spawn(false))
                .await
                .unwrap()
                .unwrap()
                .unwrap()
        );
        assert_eq!(
            maximum.load(Ordering::SeqCst),
            BACKGROUND_RUN_INSPECTION_CONCURRENCY
        );
        *gate.release.0.lock().unwrap() = true;
        gate.release.1.notify_all();
        assert!(
            tokio::time::timeout(Duration::from_secs(2), spawn(true))
                .await
                .unwrap()
                .unwrap()
                .unwrap()
        );
        tokio::time::timeout(Duration::from_secs(2), async {
            while active.load(Ordering::SeqCst) != 0 {
                assert!(
                    std::time::Instant::now() < deadline,
                    "inspection fixture draining exceeded its elapsed budget"
                );
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .unwrap();
        // Returning every native permit also proves request cleanup happened
        // in the blocking closures, rather than just in the fixture server.
        let permits = tokio::time::timeout(
            Duration::from_secs(2),
            super::background_inspection_slots()
                .acquire_many_owned(BACKGROUND_RUN_INSPECTION_CONCURRENCY as u32),
        )
        .await
        .unwrap()
        .unwrap();
        *gate.release.0.lock().unwrap() = true;
        gate.release.1.notify_all();
        gate.stop.store(true, Ordering::SeqCst);
        while !gate.server.as_ref().unwrap().is_finished() {
            assert!(
                std::time::Instant::now() < deadline,
                "inspection fixture server shutdown exceeded its elapsed budget"
            );
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        std::thread::JoinHandle::join(gate.server.take().unwrap()).unwrap();
        assert!(fs::read_dir(requests).unwrap().next().is_none());
        drop(permits);
        drop(gate);
    }

    #[test]
    fn runtime_rejection_binds_and_redacts_captured_process_logs() {
        let mut response: super::HelperResponse = vonk_agent_protocol::parse_strict(
            br#"{"schema_version":1,"request_id":null,"status":"rejected","error_code":"runtime_process_exited","diagnostic":"ModuleNotFoundError: runtime module\nAPI_TOKEN=private-value\n"}"#,
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
        // A rejection never carries the inspection outcome that only an
        // executed inspection produces, whatever the action claimed it ran.
        response.error_code = Some("runtime_process_exited".into());
        response.process_running = Some(true);
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
                    r#"{{"schema_version":1,"request_id":null,"status":"rejected","error_code":"{code}"}}"#
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
                br#"{"schema_version":1,"request_id":"20000000-0000-4000-8000-000000000002","status":"rejected","error_code":"grant_unauthorized"}"#,
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
                    r#"{{"schema_version":1,"request_id":null,"status":"rejected","error_code":"{code}"}}"#
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
            br#"{"schema_version":1,"request_id":null,"status":"container-runtime-request-executed"}"#,
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
                    code: HelperErrorCode::OperationUnsafePath,
                    diagnostic: None,
                    process_logs: None,
                },
                "helper_operation_unsafe_path",
            ),
            // The helper's own grant and request rejections name the refusing
            // check, so the operator must see them rather than a protocol error.
            (
                HostRuntimeError::HelperRejected {
                    code: HelperErrorCode::GrantUnauthorized,
                    diagnostic: None,
                    process_logs: None,
                },
                "helper_grant_unauthorized",
            ),
            (
                HostRuntimeError::HelperRejected {
                    code: HelperErrorCode::GrantNodeMismatch,
                    diagnostic: None,
                    process_logs: None,
                },
                "helper_grant_node_mismatch",
            ),
            (
                HostRuntimeError::HelperRejected {
                    code: HelperErrorCode::RequestReplayed,
                    diagnostic: None,
                    process_logs: None,
                },
                "helper_request_replayed",
            ),
            (
                HostRuntimeError::HelperRejected {
                    code: HelperErrorCode::ConcurrencyLimit,
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
            br#"{"schema_version":1,"request_id":"20000000-0000-4000-8000-000000000002","status":"rejected","error_code":"grant_unauthorized"}"#,
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
        let baseline: super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":"{request_id}","status":"rejected","error_code":"operation_failed"}}"#
            )
            .as_bytes(),
        )
        .unwrap();

        // A status other than `rejected`.
        let other_status: super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":"{request_id}","status":"container-runtime-request-executed","error_code":"operation_failed"}}"#
            )
            .as_bytes(),
        )
        .unwrap();
        assert_rejection_malformed(&other_status, HostRuntimeAction::RunInspect);

        // A rejection never carries execution evidence, an exit code or the
        // inspection outcome only an executed inspection owns.
        let mut response = baseline.clone();
        response.exit_code = Some(0);
        assert_rejection_malformed(&response, HostRuntimeAction::RunInspect);
        let mut response = baseline.clone();
        response.process_running = Some(true);
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
        // named the wrong status, or reported a non-byte exit code collapsed into
        // `helper_protocol_invalid`.
        let request_id = "10000000-0000-4000-8000-000000000001";
        let baseline: super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":"{request_id}","status":"container-runtime-request-executed"}}"#
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
            format!(r#"{{"schema_version":1,"request_id":"{request_id}","status":"rejected"}}"#)
                .as_bytes(),
        )
        .unwrap();
        assert_outcome_malformed(&rejected);

        // The exit code must fit a process byte.
        let mut response = baseline.clone();
        response.exit_code = Some(256);
        assert_outcome_malformed(&response);

        // An executed outcome never carries the inspection outcome only an
        // inspection reply does.
        let mut response = baseline;
        response.process_running = Some(true);
        assert_outcome_malformed(&response);

        // The deliberate stop-uncertain outcome and a byte-sized exit code stay
        // accepted.
        let stop_uncertain: super::HelperResponse = vonk_agent_protocol::parse_strict(
            format!(
                r#"{{"schema_version":1,"request_id":"{request_id}","status":"container-runtime-stop-uncertain","exit_code":137}}"#
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
    fn start_plan() -> RecipeStartRequest {
        let mut compiled: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../control/tests/fixtures/compiled_workload_v2.json"
        ))
        .unwrap();
        compiled["runtime"]["placement"]["endpoint_address"] = serde_json::json!("100.100.20.30");
        compiled["security"]["network_mode"] = serde_json::json!("bridge");
        serde_json::from_value(serde_json::json!({
            "run_id": "00000000-0000-4000-8000-000000000003",
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "recipe_revision_id": "00000000-0000-4000-8000-000000000002",
            "mapping_id": "00000000-0000-4000-8000-000000000007",
            "plan_digest": "c".repeat(64),
            "compiled_execution_plan": compiled,
            "run_generation": 1
        }))
        .unwrap()
    }

    fn start_request() -> super::HostRuntimeRequest {
        let plan = start_plan();
        super::HostRuntimeRequest {
            action: HostRuntimeAction::Start,
            fence: Uuid::new_v4(),
            arguments: vec!["sha256:image".to_owned(), "run".to_owned()],
            job_plan: None,
            installation_id: None,
            reconciliation_identity: None,
            run_generation: Some(plan.run_generation),
            start_plan: Some(plan),
            stop_plan: None,
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
    fn unencodable_requests_keep_the_document_cause() {
        let rule = vonk_agent_protocol::HostRuntimeRequestRule::Encoding;
        assert_eq!(
            HostRuntimeError::HelperProtocol(HelperProtocolCause::from_request_rule(rule))
                .preflight_code(),
            "helper_request_document_invalid"
        );
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
    fn every_helper_protocol_cause_names_its_own_finding_and_evidence_code() {
        // Wrong implementation: a cause that fell back to
        // `helper_protocol_invalid` is the collapse this change removes.
        let mut findings = std::collections::BTreeSet::new();
        let mut evidence = std::collections::BTreeSet::new();
        for cause in [
            HelperProtocolCause::RequestEncoding,
            HelperProtocolCause::HelperCallJoin,
            HelperProtocolCause::MessageFraming,
            HelperProtocolCause::ResponseUnbound,
            HelperProtocolCause::RejectionMalformed,
            HelperProtocolCause::OutcomeMalformed,
            HelperProtocolCause::RequestDocument,
            HelperProtocolCause::RequestArgumentsPresence,
            HelperProtocolCause::RequestPlanBinding,
            HelperProtocolCause::RequestInstallationIdentity,
            HelperProtocolCause::RequestBytes,
            HelperProtocolCause::RequestPlanBytes,
            HelperProtocolCause::RequestArgumentNulByte,
            HelperProtocolCause::RequestStorage,
            HelperProtocolCause::SystemClock,
            HelperProtocolCause::InspectionOutcome,
        ] {
            let finding = HostRuntimeError::HelperProtocol(cause).finding_code();
            assert_ne!(
                finding,
                RuntimePreflightFindingCode::PreflightFindingHelperProtocolInvalid,
                "{cause:?} collapsed to the opaque label"
            );
            assert_eq!(finding_word(finding), format!("helper_{}", cause.code()));
            assert!(crate::helper_codes::is_distribution_evidence(
                cause.runtime_helper_code()
            ));
            assert!(findings.insert(finding) && evidence.insert(cause.runtime_helper_code()));
        }
    }
}
