//! Failures.

use super::*;

pub fn recipe_install_success(installed_bytes: u64) -> ExecutionResult {
    ExecutionResult::done(AgentInstallResult { installed_bytes })
}

/// The serving rank reports its endpoint; every other rank, and every
/// rank-launch phase, reports an empty result.
pub fn recipe_start_success(request: &RecipeStartRequest) -> ExecutionResult {
    ExecutionResult::done(recipe_start_result(request))
}

/// The start result of a rank: its endpoint when it serves one.
pub fn recipe_start_result(request: &RecipeStartRequest) -> RecipeStartResult {
    let placement = request.placement();
    let endpoint = match (placement.endpoint_address, placement.port, &request.phase) {
        (Some(address), Some(port), None | Some(RecipeStartPhase::CollectiveReadiness)) => {
            let host = match address {
                std::net::IpAddr::V4(address) => address.to_string(),
                std::net::IpAddr::V6(address) => format!("[{address}]"),
            };
            Some(format!("http://{host}:{port}"))
        }
        _ => None,
    };
    RecipeStartResult {
        endpoint,
        preload_diagnostics: None,
    }
}

pub fn distribution_success(evidence: DistributionDownloadEvidence) -> ExecutionResult {
    ExecutionResult::done(ArtifactDistributionResult {
        downloaded_bytes: evidence.downloaded_bytes,
    })
}

/// A failure that carries the workload's own output, or the typed reason there
/// is none.
pub(super) fn failed_with_evidence(
    reason: &'static str,
    evidence: (Option<crate::failure_evidence::FailureProcessLogs>, String),
) -> ExecutionResult {
    let (logs, account) = evidence;
    ExecutionResult::Failed(
        Failure::new(reason)
            .diagnostic(account.chars().take(480).collect::<String>())
            .process_logs(logs),
    )
}

pub(super) fn failed(reason: &'static str) -> ExecutionResult {
    ExecutionResult::failed(reason)
}

pub(super) fn cancelled(reason: &'static str) -> ExecutionResult {
    ExecutionResult::cancelled(reason)
}

pub(super) fn temporary_reconciliation_failure(
    stage: FailureStage,
    code: FailureCode,
    diagnostic: impl Into<String>,
) -> ExecutionResult {
    ExecutionResult::Failed(
        Failure::new("installation reconciliation is waiting for its local owner")
            .code(code)
            .kind(AgentFailureKind::TemporaryDependency)
            .retry_after(Some(2))
            .stage(stage)
            .diagnostic(diagnostic),
    )
}

pub(super) fn retryable_reconciliation_storage_error(error: &OciError) -> bool {
    let OciError::Io(error) = error else {
        return false;
    };
    matches!(
        error.kind(),
        std::io::ErrorKind::Interrupted
            | std::io::ErrorKind::WouldBlock
            | std::io::ErrorKind::TimedOut
    ) || error.raw_os_error().is_some_and(|code| {
        code == rustix::io::Errno::IO.raw_os_error()
            || code == rustix::io::Errno::NOSPC.raw_os_error()
    })
}

impl StopStall {
    pub(super) fn unproven(result: ExecutionResult) -> Self {
        Self {
            result,
            identity_refused: false,
        }
    }
}

/// A container this agent cannot prove it owns occupies the name the start
/// needs. The name is quoted because the text sanitizer redacts a bare 40+
/// character identifier-like word, and the operator needs this one. It is left untouched, the start waits visibly (a prerequisite, not a
/// broken contract) and proceeds once the name is free.
pub(super) fn retained_container_foreign(run_id: &str) -> ExecutionResult {
    ExecutionResult::Failed(
        Failure::new(format!(
            "container \"vonk-{run_id}\" occupies the name this start needs and is not \
             provably this order's; it was left untouched, and the start proceeds once it is gone"
        ))
        .code(FailureCode::RetainedContainerForeign)
        .kind(AgentFailureKind::ResourcePrerequisite)
        .retry_after(Some(RETAINED_FOREIGN_RETRY_SECONDS))
        .stage(FailureStage::RetainedContainer)
        .diagnostic(format!("container=\"vonk-{run_id}\"")),
    )
}

/// The exact retained container was removed: the next attempt starts fresh.
pub(super) fn retained_container_removed(run_id: &str) -> ExecutionResult {
    ExecutionResult::Failed(
        Failure::new("a retained container of this order was removed; the start is re-issued")
            .kind(AgentFailureKind::TemporaryDependency)
            .retry_after(Some(2))
            .stage(FailureStage::RetainedContainer)
            .diagnostic(format!("removed=\"vonk-{run_id}\"")),
    )
}

/// The effect could not be established; the Controller observes it.
pub(super) fn unconfirmed(
    wait_reason: WaitReason,
    reason: &'static str,
    evidence: UnknownEvidence,
) -> ExecutionResult {
    ExecutionResult::unknown(wait_reason, reason, evidence)
}

/// Where a privileged runtime call stopped: the helper's own code when it gave
/// one, otherwise the bounded category of the failure.
pub(super) fn host_runtime_evidence(
    stage: FailureStage,
    error: &crate::host_runtime::HostRuntimeError,
) -> UnknownEvidence {
    let evidence = UnknownEvidence::at(stage).because(error.preflight_code());
    match error {
        crate::host_runtime::HostRuntimeError::HelperRejected { code, .. } => {
            evidence.helper(*code)
        }
        _ => evidence,
    }
}

pub(super) fn temporary_observation_error(error: &crate::host_runtime::HostRuntimeError) -> bool {
    use crate::host_runtime::HostRuntimeError;
    match error {
        HostRuntimeError::Io(error) => error.kind() != std::io::ErrorKind::PermissionDenied,
        HostRuntimeError::Controller(ClientError::Protocol) => true,
        HostRuntimeError::Controller(ClientError::Controller(error))
            if matches!(error.status, 401 | 403) =>
        {
            false
        }
        HostRuntimeError::Controller(_) => true,
        HostRuntimeError::HelperRejected { code, .. } => {
            matches!(
                code,
                HelperErrorCode::OperationIo
                    | HelperErrorCode::RuntimeImageInspectFailed
                    | HelperErrorCode::InstallationReconciliationStorageUnavailable
            )
        }
        HostRuntimeError::HelperProtocol(_) => true,
        HostRuntimeError::HelperProtocolBound { .. } => true,
        HostRuntimeError::StopUncertain => false,
    }
}

pub(super) fn temporary_runtime_observation_failure() -> ExecutionResult {
    ExecutionResult::Failed(
        Failure::new("exact workload runtime observation is temporarily unavailable")
            .code(FailureCode::RuntimeObservationUnavailable)
            .kind(AgentFailureKind::TemporaryDependency)
            .retry_after(Some(5)),
    )
}

/// Refuse a start whose observation failed, carrying what the helper captured.
///
/// A rejection raised on the inspection path can carry the exact container's
/// output -- the one case the diagnostic gate admits for a privileged action,
/// admitted because the inspection already proved the container's identity and
/// sanitized the text.  Reporting only the code left an operator with "the
/// observation failed" when the answer was that the workload process had exited
/// and printed why.
pub(super) fn runtime_observation_failure(
    error: &crate::host_runtime::HostRuntimeError,
) -> ExecutionResult {
    // The observation worked when the helper says the process exited: it
    // inspected the container and found it dead. Name that, not a failed look.
    let exited = matches!(
        error,
        crate::host_runtime::HostRuntimeError::HelperRejected { code, .. }
            if *code == HelperErrorCode::RuntimeProcessExited
    );
    let lead = if exited {
        "the workload process exited"
    } else {
        "exact workload runtime observation failed"
    };
    let reason = match error.diagnostic() {
        Some(detail) if !detail.is_empty() => format!("{lead}: {error}: {detail}"),
        _ => format!("{lead}: {error}"),
    };
    // A process-exit failure always says what it knows: its logs, or the
    // typed reason there are none.
    let diagnostic = match (exited, error.diagnostic(), error.process_logs()) {
        (true, None, None) => Some("exit_state_unavailable=\"helper reported no detail\" logs_unavailable=\"not captured\"".to_owned()),
        (_, detail, _) => detail.map(|detail| detail.chars().take(480).collect::<String>()),
    };
    let mut failure = Failure::new(reason).process_logs(crate::failure_evidence::diagnostic_logs(
        error.process_logs(),
        diagnostic.as_deref(),
    ));
    if diagnostic.as_deref().is_some_and(|detail| {
        detail
            .split_whitespace()
            .any(|token| token == "exit_cause=host_memory_exhausted")
    }) {
        failure = failure.code(FailureCode::WorkloadHostMemoryExhausted);
    }
    if let Some(diagnostic) = &diagnostic {
        failure = failure.diagnostic(diagnostic.clone());
    }
    if let crate::host_runtime::HostRuntimeError::HelperRejected { code, .. } = error {
        failure = failure.helper(*code, None);
    }
    ExecutionResult::Failed(failure)
}

pub(super) fn failed_owned(reason: String) -> ExecutionResult {
    ExecutionResult::failed(reason)
}

pub(super) fn failed_stage(
    reason: &'static str,
    stage: FailureStage,
    diagnostic: &'static str,
) -> ExecutionResult {
    ExecutionResult::Failed(Failure::new(reason).stage(stage).diagnostic(diagnostic))
}

pub(super) fn failed_stage_owned(
    reason: &'static str,
    stage: FailureStage,
    diagnostic: String,
) -> ExecutionResult {
    ExecutionResult::Failed(Failure::new(reason).stage(stage).diagnostic(diagnostic))
}

/// Bound the safe control-plane facts of a refused Controller request.
///
/// An authority denial used to report only that the request failed, so the
/// denied path, status and request id were unrecoverable.  Only the URL path,
/// HTTP status, validated error code, request id and transport category are
/// captured; queries, credentials, headers and response bodies stay unread.
pub(super) fn controller_denial_diagnostic(error: &ClientError) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(status) = error.status() {
        parts.push(format!("http_status={status}"));
    }
    if let Some(code) = error.code() {
        parts.push(format!("error_code={code}"));
    }
    if let Some(endpoint) = error
        .endpoint()
        .map(str::to_owned)
        .or_else(|| error.transport_endpoint())
    {
        parts.push(format!("endpoint={endpoint}"));
    }
    if let Some(request_id) = error.request_id() {
        parts.push(format!("request_id={request_id}"));
    }
    if let Some(kind) = error.transport_kind() {
        parts.push(format!("transport={kind}"));
    }
    // Leave headroom below the 512-character wire field for redaction.
    parts.join(" ").chars().take(400).collect()
}

/// Classify a refused distribution and keep the bounded denial facts.
///
/// The incident's `invalid-authority` outcome reported only that the
/// distribution failed, so the denied request and status were unrecoverable.
pub(super) fn distribution_failure_result(error: &ClientError) -> ExecutionResult {
    let failure_kind = if error.retryable()
        // A grant that merely ran out of time is renewed by the next
        // attempt's claim; it is a wait, not a denial of authority.
        || error
            .code()
            .is_some_and(|code| vocabulary::is(code, DistributionCode::DistributionExpired))
    {
        AgentFailureKind::TemporaryDependency
    } else if matches!(error.status(), Some(401 | 403)) {
        AgentFailureKind::InvalidAuthority
    } else {
        AgentFailureKind::IntegrityFailure
    };
    let mut failure = Failure::new("Controller distribution could not be verified and retained")
        .kind(failure_kind)
        .retry_after(error.retry_after_seconds())
        .stage(FailureStage::ArtifactDistribution);
    let diagnostic = controller_denial_diagnostic(error);
    if !diagnostic.is_empty() {
        failure = failure.diagnostic(diagnostic);
    }
    ExecutionResult::Failed(failure)
}

pub(super) fn recipe_build_client_failure_kind(error: &ClientError) -> AgentFailureKind {
    if error.retryable() {
        return AgentFailureKind::TemporaryDependency;
    }
    if matches!(error.status(), Some(401 | 403))
        || matches!(
            error,
            ClientError::CredentialRead(_) | ClientError::Identity | ClientError::Pin
        )
    {
        return AgentFailureKind::InvalidAuthority;
    }
    match error {
        ClientError::Controller(controller) if (400..=499).contains(&controller.status) => {
            if controller.status == 404 {
                AgentFailureKind::ResourcePrerequisite
            } else if controller.status == 409 {
                AgentFailureKind::IntegrityFailure
            } else {
                AgentFailureKind::InvalidContract
            }
        }
        ClientError::ResultRejected(_) | ClientError::Protocol => AgentFailureKind::InvalidContract,
        ClientError::ResultSuperseded => AgentFailureKind::UncertainEffect,
        _ => AgentFailureKind::IntegrityFailure,
    }
}

pub(super) fn recipe_build_client_failure_result(
    error: &ClientError,
    stage: FailureStage,
    reason: &'static str,
) -> ExecutionResult {
    let failure_kind = recipe_build_client_failure_kind(error);
    let mut failure = Failure::new(reason).kind(failure_kind).stage(stage);
    if failure_kind == AgentFailureKind::TemporaryDependency {
        failure = failure.retry_after(error.retry_after_seconds());
    }
    let diagnostic = controller_denial_diagnostic(error);
    if !diagnostic.is_empty() {
        failure = failure.diagnostic(diagnostic);
    }
    ExecutionResult::Failed(failure)
}

/// The bounded helper error code a runtime image pull failure reports. It is
/// the producer for the codes `helper_codes::is_distribution_evidence` lets
/// survive into the Controller's normalized failure body.
pub(super) fn runtime_helper_code(
    error: &crate::host_runtime::HostRuntimeError,
) -> HelperErrorCode {
    use crate::host_runtime::HostRuntimeError;
    match error {
        HostRuntimeError::HelperRejected { code, .. } => *code,
        HostRuntimeError::Io(_) => HelperErrorCode::RuntimeHelperUnavailable,
        HostRuntimeError::Controller(_) => HelperErrorCode::RuntimeAuthorityUnavailable,
        HostRuntimeError::HelperProtocol(cause)
        | HostRuntimeError::HelperProtocolBound { cause, .. } => cause.runtime_helper_code(),
        HostRuntimeError::StopUncertain => HelperErrorCode::RuntimeHelperStopUncertain,
    }
}

pub(super) fn runtime_failure(
    reason: &str,
    error: &crate::host_runtime::HostRuntimeError,
) -> ExecutionResult {
    let failure = Failure::new(match error.diagnostic() {
        // A refusal that names its own cause (for example which argument the
        // Spark firewall rejected) belongs in the text an operator reads first,
        // not only in the attached diagnostic logs. A container's own output
        // stays in the logs: it is evidence, not the reason.
        Some(detail) if !detail.is_empty() && error.process_logs().is_none() => {
            format!("{reason}: {}: {detail}", error.preflight_code())
        }
        _ => format!("{reason}: {}", error.preflight_code()),
    })
    .process_logs(crate::failure_evidence::diagnostic_logs(
        error.process_logs(),
        error.diagnostic(),
    ))
    // Bounded integers only: the refusing rule and the measured bound. The
    // offending argument itself never crosses this boundary.
    .refusal_bound(error.refusal_bound().map(|(limit, observed)| RefusalBound {
        rule: error.preflight_code(),
        limit,
        observed,
    }));
    let failure = if temporary_observation_error(error) {
        // Peer observations do not become permanent admission decisions.
        // The Controller's durable recovery counter owns the retry-to-end
        // budget; every attempt re-observes through a fresh accepted grant.
        failure
            .kind(AgentFailureKind::TemporaryDependency)
            .retry_after(Some(2))
    } else {
        failure
    };
    ExecutionResult::Failed(failure)
}

pub(super) fn runtime_preparation_failure(error: &OciError) -> ExecutionResult {
    let (stage, category) = error.safe_start_context();
    failed_owned(format!(
        "container runtime could not prepare the workload (stage={stage}; category={category})"
    ))
}

#[cfg(test)]
mod tests;
