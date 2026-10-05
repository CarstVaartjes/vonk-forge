//! The agent's typed operation outcome.
//!
//! Every executor ends an operation in one of three ways, and this module is the
//! only place that turns one into the protocol message the Controller reads:
//!
//! * [`ExecutionResult::Done`]: the effect is established; the operation's typed
//!   success body;
//! * [`ExecutionResult::Failed`]: a *definite* failure, described by a
//!   [`Failure`] (the reason, a closed [`FailureCode`], the bounded evidence);
//!   a confirmed cancellation is the `operation_cancelled` code;
//! * [`ExecutionResult::Unknown`]: the effect could not be established, with a
//!   closed [`WaitReason`].
//!
//! [`ExecutionResult::finish`] normalizes the draft (sanitized text, a stable code
//! per operation, bounded diagnostics) and builds the generated
//! `OutcomeDone` / `OutcomeFailed` / `OutcomeUnknown` of the shared contract. No
//! result body is assembled from loose JSON anywhere in the agent.

use crate::failure_evidence::{self, FailureProcessLogs, sanitize_text};
use crate::vocabulary;
use vonk_agent_protocol::AgentClaim;
use vonk_agent_protocol::generated::{
    AgentFailureKind, AgentOperation, AgentResultResult, AgentResultState, FailureCode,
    OutcomeDone, OutcomeDoneKind, OutcomeDoneResult, OutcomeEvidence, OutcomeFailed,
    OutcomeFailedKind, OutcomeUnknown, OutcomeUnknownKind, RecipeJobRunResult,
    SecurityRefusalReason, WaitReason,
};

/// The rule a refused request broke and the bound it was measured against.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RefusalBound {
    pub rule: String,
    pub limit: Option<u64>,
    pub observed: u64,
}

/// What an executor knows about a definite failure, before it is normalized.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Failure {
    pub reason: String,
    /// The closed code, when the failure has a specific one; otherwise the
    /// operation's own `*_failed` code is used.
    pub code: Option<FailureCode>,
    pub failure_kind: Option<AgentFailureKind>,
    pub retry_after_seconds: Option<u32>,
    pub stage: Option<String>,
    pub diagnostic: Option<String>,
    pub helper_error_code: Option<String>,
    pub helper_exit_code: Option<u32>,
    /// The rejected container's own output, when the helper read it.
    pub process_logs: Option<FailureProcessLogs>,
    pub refusal_bound: Option<RefusalBound>,
    /// The process receipt of a one-shot job whose process ran.
    pub receipt: Option<RecipeJobRunResult>,
}

impl Failure {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
            code: None,
            failure_kind: None,
            retry_after_seconds: None,
            stage: None,
            diagnostic: None,
            helper_error_code: None,
            helper_exit_code: None,
            process_logs: None,
            refusal_bound: None,
            receipt: None,
        }
    }

    pub fn code(mut self, code: FailureCode) -> Self {
        self.code = Some(code);
        self
    }

    pub fn kind(mut self, kind: AgentFailureKind) -> Self {
        self.failure_kind = Some(kind);
        self
    }

    pub fn retry_after(mut self, seconds: Option<u32>) -> Self {
        self.retry_after_seconds = seconds;
        self
    }

    pub fn stage(mut self, stage: impl Into<String>) -> Self {
        self.stage = Some(stage.into());
        self
    }

    pub fn diagnostic(mut self, diagnostic: impl Into<String>) -> Self {
        self.diagnostic = Some(diagnostic.into());
        self
    }

    pub fn helper(mut self, code: impl Into<String>, exit_code: Option<u32>) -> Self {
        self.helper_error_code = Some(code.into());
        self.helper_exit_code = exit_code;
        self
    }

    pub fn process_logs(mut self, logs: Option<FailureProcessLogs>) -> Self {
        self.process_logs = logs;
        self
    }

    pub fn refusal_bound(mut self, bound: Option<RefusalBound>) -> Self {
        self.refusal_bound = bound;
        self
    }

    pub fn receipt(mut self, receipt: RecipeJobRunResult) -> Self {
        self.receipt = Some(receipt);
        self
    }

    fn cancelled(&self) -> bool {
        self.code == Some(FailureCode::OperationCancelled)
    }
}

/// What an executor knows about the effect it could not establish: where it
/// stopped and why. An unknown outcome with no evidence leaves the Controller
/// nothing to observe and nothing to show, so [`ExecutionResult::unknown`]
/// cannot be built without it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownEvidence {
    /// The step that could not be confirmed (a short, stable word).
    pub stage: &'static str,
    /// A bounded, non-sensitive cause: an error category or helper code.
    pub diagnostic: Option<String>,
    /// The privileged helper's own verdict, when it gave one.
    pub helper_error_code: Option<String>,
}

impl UnknownEvidence {
    pub fn at(stage: &'static str) -> Self {
        Self {
            stage,
            diagnostic: None,
            helper_error_code: None,
        }
    }

    pub fn because(mut self, diagnostic: impl Into<String>) -> Self {
        self.diagnostic = Some(diagnostic.into());
        self
    }

    pub fn helper(mut self, code: impl Into<String>) -> Self {
        self.helper_error_code = Some(code.into());
        self
    }

    fn into_wire(self) -> OutcomeEvidence {
        OutcomeEvidence {
            diagnostics: None,
            diagnostic: self
                .diagnostic
                .as_deref()
                .map(sanitize_text)
                .map(|text| text.chars().take(512).collect::<String>())
                .filter(|text| !text.is_empty()),
            helper_error_code: self
                .helper_error_code
                .map(|code| code.chars().take(128).collect::<String>())
                .filter(|code| !code.is_empty()),
            helper_exit_code: None,
            package_activation: None,
            stage: Some(self.stage.to_owned()),
        }
    }
}

/// An effect the executor could not establish.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unconfirmed {
    pub wait_reason: WaitReason,
    pub reason: String,
    pub evidence: UnknownEvidence,
    pub receipt: Option<RecipeJobRunResult>,
}

/// What an executor produced for one operation.
#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(clippy::large_enum_variant)]
pub enum ExecutionResult {
    Done(OutcomeDoneResult),
    Failed(Failure),
    Unknown(Unconfirmed),
}

/// The state word and the typed result body of a finished operation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finished {
    pub state: AgentResultState,
    pub result: AgentResultResult,
}

impl ExecutionResult {
    /// The effect is established.
    pub fn done(result: impl Into<OutcomeDoneResult>) -> Self {
        Self::Done(result.into())
    }

    /// A definite failure with no more specific cause than its reason.
    pub fn failed(reason: impl Into<String>) -> Self {
        Self::Failed(Failure::new(reason))
    }

    /// The Controller's cancellation is confirmed: the effect is stopped.
    pub fn cancelled(reason: impl Into<String>) -> Self {
        Self::Failed(Failure::new(reason).code(FailureCode::OperationCancelled))
    }

    /// The effect could not be established. The evidence says where it stopped;
    /// the Controller observes the effect and decides.
    pub fn unknown(
        wait_reason: WaitReason,
        reason: impl Into<String>,
        evidence: UnknownEvidence,
    ) -> Self {
        Self::Unknown(Unconfirmed {
            wait_reason,
            reason: reason.into(),
            evidence,
            receipt: None,
        })
    }

    /// The state word this result is reported under.
    pub fn state(&self) -> AgentResultState {
        match self {
            Self::Done(_) => AgentResultState::Succeeded,
            Self::Failed(failure) if failure.cancelled() => AgentResultState::Cancelled,
            Self::Failed(_) => AgentResultState::Failed,
            Self::Unknown(_) => AgentResultState::WaitingForOperator,
        }
    }

    /// The failure of a failed or cancelled result.
    pub fn failure(&self) -> Option<&Failure> {
        match self {
            Self::Failed(failure) => Some(failure),
            _ => None,
        }
    }

    /// Normalize this result for the operation of `claim` and build the protocol
    /// message: sanitized text, a stable code, bounded diagnostics.
    pub fn finish(self, claim: &AgentClaim) -> Finished {
        self.finish_for(&claim.operation)
    }

    /// [`Self::finish`] for an operation known without its claim.
    pub fn finish_for(self, operation: &AgentOperation) -> Finished {
        let state = self.state();
        let result = match self {
            Self::Done(result) => AgentResultResult::OutcomeDone(OutcomeDone {
                kind: OutcomeDoneKind::Done,
                result,
            }),
            Self::Failed(failure) => {
                AgentResultResult::OutcomeFailed(finish_failure(operation, failure))
            }
            Self::Unknown(unconfirmed) => AgentResultResult::OutcomeUnknown(OutcomeUnknown {
                kind: OutcomeUnknownKind::Unknown,
                wait_reason: unconfirmed.wait_reason,
                reason: bounded_reason(&unconfirmed.reason),
                retry_after_seconds: None,
                evidence: Some(unconfirmed.evidence.into_wire()),
                receipt: unconfirmed.receipt,
            }),
        };
        Finished { state, result }
    }
}

/// The failure code of an operation that reports no more specific one.
pub fn default_failure_code(operation: &AgentOperation) -> FailureCode {
    match operation {
        AgentOperation::AgentUpgradeV1 => FailureCode::AgentUpgradeFailed,
        AgentOperation::ArtifactDistributionV1 => FailureCode::ArtifactDistributionFailed,
        AgentOperation::RecipeBuildV1 => FailureCode::RecipeBuildFailed,
        AgentOperation::RecipeJobRunV1 => FailureCode::RecipeJobRunFailed,
        AgentOperation::RecipeInstall => FailureCode::RecipeInstallFailed,
        AgentOperation::RecipeStart => FailureCode::RecipeStartFailed,
        AgentOperation::RecipeStop => FailureCode::RecipeStopFailed,
        AgentOperation::RecipeUninstall => FailureCode::RecipeUninstallFailed,
        AgentOperation::RuntimePreflightV1
        | AgentOperation::RecipeBuildCleanupV1
        | AgentOperation::RecipeReconcile => FailureCode::OperationFailed,
    }
}

/// Sanitized and bounded to the wire limit of a reason.
fn bounded_reason(reason: &str) -> String {
    let reason: String = sanitize_text(reason).chars().take(1024).collect();
    if reason.is_empty() {
        "agent operation failed".to_owned()
    } else {
        reason
    }
}

fn finish_failure(operation: &AgentOperation, failure: Failure) -> OutcomeFailed {
    if failure.cancelled() {
        // A confirmed cancellation carries its reason and, for a job, its
        // receipt as reported: there is nothing to diagnose.
        return OutcomeFailed {
            kind: OutcomeFailedKind::Failed,
            code: FailureCode::OperationCancelled,
            reason: bounded_reason(&failure.reason),
            failure_kind: None,
            retry_after_seconds: None,
            evidence: None,
            receipt: failure.receipt,
        };
    }
    let diagnostics = failure_evidence::from_failure(operation, &failure);
    let code = failure
        .code
        .unwrap_or_else(|| default_failure_code(operation));
    if let Some(mut receipt) = failure.receipt.clone()
        && *operation == AgentOperation::RecipeJobRunV1
    {
        // A job reports its own process outcome; the typed receipt keeps its
        // structure and gains the diagnostics, never a rewritten body.
        receipt.reason = receipt
            .reason
            .as_deref()
            .map(sanitize_text)
            .filter(|reason| !reason.is_empty());
        let reason = receipt
            .reason
            .clone()
            .unwrap_or_else(|| bounded_reason(&failure.reason));
        receipt.diagnostics = Some(diagnostics);
        return OutcomeFailed {
            kind: OutcomeFailedKind::Failed,
            code,
            reason,
            failure_kind: failure.failure_kind,
            retry_after_seconds: failure.retry_after_seconds,
            evidence: None,
            receipt: Some(receipt),
        };
    }
    let helper_error_code = failure
        .helper_error_code
        .as_deref()
        .filter(|code| helper_error_code_allowed(operation, code))
        .map(str::to_owned);
    // The helper's exit status accompanies only a failed package install.
    let helper_exit_code = failure
        .helper_exit_code
        .filter(|code| *code <= 255)
        .filter(|_| helper_error_code.as_deref() == Some("package_install_failed"));
    OutcomeFailed {
        kind: OutcomeFailedKind::Failed,
        code,
        reason: bounded_reason(&failure.reason),
        failure_kind: failure.failure_kind,
        retry_after_seconds: failure.retry_after_seconds,
        evidence: Some(OutcomeEvidence {
            diagnostics: Some(diagnostics),
            diagnostic: failure.diagnostic.as_deref().map(sanitize_text),
            helper_error_code,
            helper_exit_code,
            package_activation: None,
            stage: failure.stage.as_deref().map(sanitize_text),
        }),
        receipt: None,
    }
}

/// The helper codes that cross the wire, per operation.
fn helper_error_code_allowed(operation: &AgentOperation, code: &str) -> bool {
    match operation {
        AgentOperation::AgentUpgradeV1 => matches!(
            code,
            "package_verification_failed"
                | "package_metadata_failed"
                | "package_custody_failed"
                | "package_install_failed"
        ),
        AgentOperation::ArtifactDistributionV1 => stable_runtime_helper_error_code(code),
        _ => false,
    }
}

/// The refusals a helper raises before it has trusted a grant.
pub const GRANT_REFUSALS: [SecurityRefusalReason; 4] = [
    SecurityRefusalReason::GrantInvalid,
    SecurityRefusalReason::GrantNodeMismatch,
    SecurityRefusalReason::GrantUnauthorized,
    SecurityRefusalReason::PeerIdentityInvalid,
];

/// The identity refusals of an artifact or runtime image a helper names.
pub const HELPER_IDENTITY_REFUSALS: [SecurityRefusalReason; 2] = [
    SecurityRefusalReason::OperationInvalidArtifact,
    SecurityRefusalReason::RuntimeImageIdentityInvalid,
];

/// The runtime helper's stable error vocabulary.
pub fn stable_runtime_helper_error_code(value: &str) -> bool {
    // The helper refuses before it trusts a grant; those refusals, and the other
    // contract words it shares with the agent, are named by the generated enums.
    vocabulary::is_any(value, &GRANT_REFUSALS)
        || vocabulary::is(value, SecurityRefusalReason::RequestReplayed)
        || vocabulary::is(value, FailureCode::OperationFailed)
        || vocabulary::is_any(value, &HELPER_IDENTITY_REFUSALS)
        || matches!(
            value,
            "operation_invalid"
            | "operation_unsafe_path"
            | "operation_command_failed"
            | "operation_stop_uncertain"
            | "operation_io"
            | "runtime_image_load_failed"
            | "runtime_image_inspect_failed"
            | "runtime_image_receipt_failed"
            | "runtime_helper_unavailable"
            | "runtime_authority_unavailable"
            | "runtime_helper_protocol_invalid"
            // Any host runtime call can be refused before the helper trusts the
            // grant, the image import included. Dropping those codes here left
            // the normalized failure with no cause at all.
            | "request_invalid"
            | "request_ledger_failed"
            // An agent-side helper-protocol cause keeps its own code here or
            // normalization silently drops it from the Controller's failure
            // body.
            | "runtime_helper_request_encoding_invalid"
            | "runtime_helper_call_join_failed"
            | "runtime_helper_message_framing_invalid"
            | "runtime_helper_response_unbound"
            | "runtime_helper_rejection_malformed"
            | "runtime_helper_outcome_malformed"
            | "runtime_helper_request_document_invalid"
            | "runtime_helper_request_schema_version_invalid"
            | "runtime_helper_request_attempt_invalid"
            | "runtime_helper_request_arguments_presence_invalid"
            | "runtime_helper_request_plan_binding_invalid"
            | "runtime_helper_request_installation_identity_invalid"
            | "runtime_helper_request_bytes_invalid"
            | "runtime_helper_request_plan_bytes_invalid"
            | "runtime_helper_request_argument_nul_byte"
            | "runtime_helper_request_storage_invalid"
            | "runtime_helper_system_clock_invalid"
            | "runtime_helper_inspection_outcome_invalid"
            | "runtime_helper_stop_uncertain"
        )
}
