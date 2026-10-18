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
    // Carry the closed cause separately from free text: long protocol words
    // can be redacted by the diagnostic sanitizer as opaque values.
    UnknownEvidence::at(stage)
        .because(match error {
            crate::host_runtime::HostRuntimeError::HelperProtocolBound {
                limit, observed, ..
            } => {
                format!(
                    "{} limit={limit:?} observed={observed}",
                    error.preflight_code()
                )
            }
            _ => error.preflight_code().to_owned(),
        })
        .helper(runtime_helper_code(error))
}

pub(super) fn temporary_observation_error(error: &crate::host_runtime::HostRuntimeError) -> bool {
    use crate::host_runtime::HostRuntimeError;
    match error {
        // Socket/file observation loss is not authenticated authority denial.
        HostRuntimeError::Io(_) => true,
        // The shared disposition: a wait or an unconfirmed answer is observed
        // again; a refused grant (400/404/422, a denied exact operation) or a
        // security refusal is definite.
        HostRuntimeError::Controller(error) => {
            matches!(
                error.disposition(),
                Disposition::Wait | Disposition::Observe
            )
        }
        // A dead container is a successful observation: the helper inspected it
        // and found it exited. Retrying would only meet the same corpse.
        HostRuntimeError::HelperRejected { code, .. } => !matches!(
            code,
            HelperErrorCode::RuntimeProcessExited
                | HelperErrorCode::GrantInvalid
                | HelperErrorCode::GrantNodeMismatch
                | HelperErrorCode::GrantUnauthorized
                | HelperErrorCode::PeerIdentityInvalid
                | HelperErrorCode::PackageVerificationFailed
        ),
        HostRuntimeError::HelperProtocol(_) => true,
        // Caller-supplied request bounds and an unbound reply still cannot
        // authorize any effect. Observation loss never fabricates a receipt.
        HostRuntimeError::HelperProtocolBound { .. } => true,
        HostRuntimeError::StopUncertain => true,
    }
}

#[cfg(test)]
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
    // The reason is a typed sentence: the lead, the helper's code and the exit
    // facts.  The workload's own words (its exception, its paths) are evidence
    // and travel in the diagnostic and the captured streams, never here.
    let facts = error
        .diagnostic()
        .map(|detail| {
            crate::failure_evidence::exit_facts(detail)
                .map(|(key, value)| format!("{key}={value}"))
                .collect::<Vec<_>>()
                .join(" ")
        })
        .unwrap_or_default();
    let reason = if facts.is_empty() {
        format!("{lead}: {error}")
    } else {
        format!("{lead}: {error}: {facts}")
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
    let failure = if temporary_observation_error(error) {
        failure
            .kind(AgentFailureKind::TemporaryDependency)
            .retry_after(Some(5))
    } else {
        failure
    };
    ExecutionResult::Failed(failure)
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
/// denied path, status and request id were unrecoverable.  Only the route
/// name, HTTP status, validated error code, request id and transport category
/// are captured; queries, credentials, headers and response bodies stay unread.
pub(super) fn controller_denial_diagnostic(error: &ClientError) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(status) = error.status() {
        parts.push(format!("http_status={status}"));
    }
    if let Some(code) = error.code() {
        parts.push(format!("error_code={code}"));
    }
    // The route by name: a URL path would cost the whole evidence group.
    if let Some(endpoint) = error
        .endpoint()
        .map(str::to_owned)
        .or_else(|| error.transport_endpoint())
        .as_deref()
        .and_then(crate::client::endpoint_name)
    {
        parts.push(format!("endpoint={endpoint}"));
    }
    if let Some(request_id) = error.request_id() {
        parts.push(format!("request_id={request_id}"));
    }
    if let Some(kind) = error.transport_kind() {
        parts.push(format!("transport={kind}"));
    }
    if let Some(stalled) = error.stalled() {
        parts.push(format!(
            "inactivity_seconds={} bytes={}",
            stalled.window.as_secs(),
            stalled.bytes
        ));
    }
    if let ClientError::Local(local) = error {
        // The local cause by kind only: paths and messages stay unread.
        parts.push(format!("local={:?}", local.kind()));
    }
    if let ClientError::Unsettled(cause) = error {
        parts.push(format!("unsettled={cause}"));
    }
    parts.push(format!("disposition={}", error.disposition().as_str()));
    // Leave headroom below the 512-character wire field for redaction.
    parts.join(" ").chars().take(400).collect()
}

/// Classify a refused distribution and keep the bounded denial facts.
///
/// The incident's `invalid-authority` outcome reported only that the
/// distribution failed, so the denied request and status were unrecoverable.
pub(super) fn distribution_failure_result(error: &ClientError) -> ExecutionResult {
    let failure_kind = recipe_build_client_failure_kind(error);
    let mut failure = Failure::new("Controller distribution could not be verified and retained")
        .kind(failure_kind)
        .stage(FailureStage::ArtifactDistribution);
    if matches!(
        failure_kind,
        AgentFailureKind::TemporaryDependency | AgentFailureKind::ResourcePrerequisite
    ) {
        failure = failure.retry_after(Some(error.retry_after_seconds().unwrap_or(5).min(30)));
    }
    let diagnostic = controller_denial_diagnostic(error);
    if !diagnostic.is_empty() {
        failure = failure.diagnostic(diagnostic);
    }
    ExecutionResult::Failed(failure)
}

pub(super) fn recipe_build_client_failure_kind(error: &ClientError) -> AgentFailureKind {
    if error
        .code()
        .is_some_and(|code| vocabulary::is(code, DistributionCode::DistributionExpired))
    {
        // A fresh authorized claim renews the expired transfer grant.
        return AgentFailureKind::TemporaryDependency;
    }
    if crate::client::local_storage_full(error) {
        // A full disk on this Spark is a resource the order waits for, never a
        // credential or authority failure.
        return AgentFailureKind::ResourcePrerequisite;
    }
    // The one mapping from the shared disposition onto the wire's failure kind.
    match error.disposition() {
        Disposition::Security => AgentFailureKind::InvalidAuthority,
        Disposition::Fail => AgentFailureKind::InvalidContract,
        Disposition::Wait => AgentFailureKind::TemporaryDependency,
        Disposition::Observe => AgentFailureKind::UncertainEffect,
    }
}

pub(super) fn recipe_build_client_failure_result(
    error: &ClientError,
    stage: FailureStage,
    reason: &'static str,
) -> ExecutionResult {
    let failure_kind = recipe_build_client_failure_kind(error);
    let mut failure = Failure::new(reason).kind(failure_kind).stage(stage);
    if matches!(
        failure_kind,
        AgentFailureKind::TemporaryDependency | AgentFailureKind::ResourcePrerequisite
    ) {
        failure = failure.retry_after(Some(error.retry_after_seconds().unwrap_or(5).min(30)));
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

/// The helper refused the request before it did anything: the grant or the
/// caller was not accepted (`vonk-agent-helper` answers these before it claims
/// the request). Nothing changed on the Spark, so the answer is definite.
pub(super) fn refused_before_effect(error: &crate::host_runtime::HostRuntimeError) -> bool {
    matches!(
        error,
        crate::host_runtime::HostRuntimeError::HelperRejected {
            code: HelperErrorCode::GrantInvalid
                | HelperErrorCode::GrantNodeMismatch
                | HelperErrorCode::GrantUnauthorized
                | HelperErrorCode::GrantTimeWindow
                | HelperErrorCode::PeerIdentityInvalid,
            ..
        }
    )
}

/// A Stop the helper refused before any effect is a definite failure that
/// carries the helper's code; any other failure leaves the stop unconfirmed.
pub(super) fn stop_failure(
    unconfirmed_reason: &'static str,
    error: &crate::host_runtime::HostRuntimeError,
) -> ExecutionResult {
    if refused_before_effect(error) {
        return runtime_failure(
            "container runtime stop was refused before any effect",
            error,
        );
    }
    unconfirmed(
        WaitReason::StopUnconfirmed,
        unconfirmed_reason,
        host_runtime_evidence(FailureStage::Stop, error),
    )
}

pub(super) fn runtime_failure(
    reason: &str,
    error: &crate::host_runtime::HostRuntimeError,
) -> ExecutionResult {
    if runtime_helper_code(error) == HelperErrorCode::GrantTimeWindow {
        return ExecutionResult::Failed(
            Failure::new("helper grant time window elapsed; Controller will issue a fresh grant")
                .kind(AgentFailureKind::UncertainEffect)
                .retry_after(Some(2))
                .helper(HelperErrorCode::GrantTimeWindow, None),
        );
    }
    if temporary_observation_error(error) {
        return ExecutionResult::unknown(
            WaitReason::RuntimeEffectUnconfirmed,
            reason,
            host_runtime_evidence(FailureStage::Unknown, error),
        );
    }
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
    .helper(runtime_helper_code(error), None)
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
    preparation_failure_at(error, FailureStage::Unknown, "preparation")
}

/// A preparation step that stopped on a local error: the step, the stage and
/// the error's safe category stay visible, and only a cause that cannot change
/// for this exact request ends the start.
pub(super) fn preparation_failure_at(
    error: &OciError,
    fallback: FailureStage,
    step: &str,
) -> ExecutionResult {
    let (stage, category) = match error {
        OciError::Install { .. } => error.safe_install_context(),
        _ => error.safe_start_context(),
    };
    let stage = if matches!(stage, FailureStage::Unknown) {
        fallback
    } else {
        stage
    };
    if error.is_cancelled() {
        // The attempt's budget or lease ended; copied and downloaded bytes
        // stay in place and the next attempt resumes from them.
        return temporary_preparation_failure(
            stage,
            step,
            "attempt ended before the step finished; progress is kept for the next attempt",
        );
    }
    let failure = Failure::new(format!(
        "container runtime could not prepare the workload (stage={stage}; category={category})"
    ))
    .stage(stage)
    .diagnostic(preparation_diagnostic(step, category));
    let failure = if retryable_runtime_preparation(error) {
        failure
            .code(FailureCode::RuntimeObservationUnavailable)
            .kind(AgentFailureKind::TemporaryDependency)
            .retry_after(Some(5))
    } else {
        failure
    };
    ExecutionResult::Failed(failure)
}

fn preparation_diagnostic(step: &str, cause: &str) -> String {
    format!("step={step} cause={cause}")
}

/// A preparation step ended for a cause that clears by itself (an attempt
/// budget, a lease, a busy local owner): retried with the usual backoff, with
/// the step and cause named instead of an anonymous observation gap.
pub(super) fn temporary_preparation_failure(
    stage: FailureStage,
    step: &str,
    cause: &str,
) -> ExecutionResult {
    ExecutionResult::Failed(
        Failure::new("workload preparation is temporarily unavailable")
            .code(FailureCode::RuntimeObservationUnavailable)
            .kind(AgentFailureKind::TemporaryDependency)
            .retry_after(Some(5))
            .stage(stage)
            .diagnostic(preparation_diagnostic(step, cause)),
    )
}

/// A job step before its process is dispatched stopped, so this attempt never
/// ran the job. A cause that clears by itself (local storage, metadata,
/// capacity, the attempt's lease) waits at `stage`, a stage the job reaches
/// only before launch, and the Controller issues the job again; a refused
/// workload input ends it with that evidence.
pub(super) fn job_step_before_launch(
    stage: FailureStage,
    step: &str,
    error: &OciError,
) -> ExecutionResult {
    let (_, category) = error.safe_start_context();
    if error.is_cancelled() || retryable_runtime_preparation(error) {
        temporary_preparation_failure(stage, step, category)
    } else {
        failed_stage_owned(
            "job preparation was refused",
            stage,
            preparation_diagnostic(step, category),
        )
    }
}

fn retryable_runtime_preparation(error: &OciError) -> bool {
    match error {
        OciError::Start { source, .. } | OciError::Install { source, .. } => {
            retryable_runtime_preparation(source)
        }
        // These refuse workload input or bytes at the image ingress.
        OciError::Workload(_) | OciError::ImageDigest => false,
        // Local process, storage, metadata and capacity are observations.
        _ => true,
    }
}

#[cfg(test)]
mod tests;
