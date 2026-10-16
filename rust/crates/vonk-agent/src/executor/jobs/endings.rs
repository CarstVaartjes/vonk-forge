//! How a job ends when its effect is not confirmed.
//!
//! A job run is irreversible: the Controller observes an unknown effect of
//! one without end and never claims it again. So a job reports an unknown
//! only when something bounds it:
//!
//! - after its exit is receipted, the unknown carries the receipt, and the
//!   Controller ends the order with that exit;
//! - while a cancellation's stop is unconfirmed, the cancel bounds it: the
//!   Controller completes every cancel.
//!
//! Every other uncertainty is observed again within the claim until the
//! job's deadline plus its stop grace, then the job ends failed with what was
//! observed. [`bounded_job_result`] is the net at the one exit: an unknown
//! with neither a receipt nor a bound becomes a failure, and that is a
//! defect to fix where it was built.

use super::*;

impl DispatchedJob<'_> {
    /// An unconfirmed step after the job's exit is receipted. It carries the
    /// receipt: the Controller then knows the process ceased and ends the
    /// order with that exit instead of observing it without a bound.
    pub(super) fn unknown_after_exit(
        &self,
        receipt: &RecipeJobRunResult,
        stage: FailureStage,
        reason: &'static str,
        cause: String,
    ) -> ExecutionResult {
        ExecutionResult::Unknown(crate::outcome::Unconfirmed {
            receipt: Some(receipt.clone()),
            ..unconfirmed_job(self.claim, self.request, stage, reason, cause)
        })
    }

    /// A cancellation whose stop is unconfirmed: the cancel bounds it.
    pub(super) fn unknown_while_cancelling(
        &self,
        stage: FailureStage,
        cause: String,
    ) -> Box<ExecutionResult> {
        Box::new(ExecutionResult::Unknown(crate::outcome::Unconfirmed {
            wait_reason: WaitReason::JobStopUnconfirmed,
            ..unconfirmed_job(
                self.claim,
                self.request,
                stage,
                "job cancellation stop remains unobserved",
                cause,
            )
        }))
    }

    /// The end of a job whose exit was never read: why its answer was lost,
    /// and what was observed until the job's deadline plus its stop grace (or
    /// what proved at once that no exit will be read).
    pub(super) fn unobserved_failure(
        &self,
        stage: FailureStage,
        cause: &str,
        observed: &str,
    ) -> Box<ExecutionResult> {
        let diagnostic = bounded_text(&format!("cause={cause}; observed={observed}"));
        eprintln!(
            "<4>vonk-agent: job {} fence {} ended at {stage} without an observed exit: {diagnostic}",
            self.request.job_id, self.claim.fence
        );
        Box::new(ExecutionResult::Failed(
            Failure::new("job ended without an observed exit")
                .code(FailureCode::RecipeJobRunFailed)
                .kind(AgentFailureKind::UncertainEffect)
                .stage(stage)
                .diagnostic(diagnostic),
        ))
    }
}

/// The helper reported an exit code outside 0..=255: the same answer on every
/// look, so the job ends failed at once with the raw value.
pub(super) fn out_of_range_exit(exit_code: Option<i32>) -> Box<ExecutionResult> {
    Box::new(ExecutionResult::Failed(
        Failure::new("job exit code is out of range")
            .code(FailureCode::RecipeJobRunFailed)
            .kind(AgentFailureKind::InvalidContract)
            .stage(FailureStage::JobState)
            .diagnostic(format!("exit_code_raw={exit_code:?}")),
    ))
}

/// The net at a job's one exit: an unknown with neither a receipt nor a
/// cancellation's bound would be observed without end, so it ends failed
/// with its own evidence.
pub(super) fn bounded_job_result(result: ExecutionResult) -> ExecutionResult {
    let ExecutionResult::Unknown(unconfirmed) = &result else {
        return result;
    };
    if unconfirmed.receipt.is_some() || unconfirmed.wait_reason == WaitReason::JobStopUnconfirmed {
        return result;
    }
    let cause = unconfirmed
        .evidence
        .diagnostic
        .as_deref()
        .unwrap_or_default();
    let diagnostic = bounded_text(&format!(
        "unknown without receipt or bound: {}; {cause}",
        unconfirmed.reason
    ));
    // `<3>` is the journal's error priority: this is a defect.
    eprintln!("<3>vonk-agent: job {diagnostic}");
    ExecutionResult::Failed(
        Failure::new("job ended without an observed exit")
            .code(FailureCode::RecipeJobRunFailed)
            .kind(AgentFailureKind::UncertainEffect)
            .stage(unconfirmed.evidence.stage)
            .diagnostic(diagnostic),
    )
}

fn bounded_text(text: &str) -> String {
    crate::failure_evidence::sanitize_text(text)
        .chars()
        .take(512)
        .collect()
}

/// An unobserved job effect: the step it stopped at and why travel to the
/// Controller and into the agent journal. No synthetic process exit is
/// attached; the callers above say what bounds it.
fn unconfirmed_job(
    claim: &AgentClaim,
    request: &vonk_agent_protocol::RecipeJobRunRequest,
    stage: FailureStage,
    reason: &'static str,
    cause: String,
) -> crate::outcome::Unconfirmed {
    let cause = bounded_text(&cause);
    // `<4>` is the journal's warning priority (systemd SyslogLevelPrefix).
    eprintln!(
        "<4>vonk-agent: job {} fence {} unconfirmed at {stage}: {cause}",
        request.job_id, claim.fence
    );
    crate::outcome::Unconfirmed {
        wait_reason: WaitReason::JobStateUncertain,
        reason: reason.to_owned(),
        evidence: UnknownEvidence::at(stage).because(cause),
        receipt: None,
    }
}
