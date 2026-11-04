//! A Start that answered an error.
//!
//! The error says the Start did not report success, not what the container
//! did: a lost or unreadable reply, or a launch whose answer the helper could
//! not read, can leave this start's workload running. Before anything stops
//! it, the exact container of this start's generation is read again through
//! the grant-free inspection, and the start goes on by what is there: a running
//! container is this start's and goes on to readiness; an exited one reports
//! its own exit; proven absence leaves nothing to stop; an unread inspection
//! stops nothing and waits. A container of another generation is not this
//! start's: the helper refuses it, which reads as unread, and the retry's Start
//! refuses it by name.
//!
//! Two answers keep the exact stop they had. A container under the name that
//! is not this start's (the helper already read it): the stop removes one of
//! this order's and refuses any other. And a refused authority: the start is
//! not this agent's to adopt.
//!
//! A refused inspection grant is not a refused start: it says nothing about
//! the container (a Controller that predates generation-bound inspections
//! refuses the request's shape), so a launched rank is read again and nothing
//! is stopped on it.

use super::*;
use crate::host_runtime::HostRuntimeError;

const START_FAILED: &str = "container runtime could not start the workload";

/// The Controller refused an inspection grant request's own bytes (a
/// Controller that predates generation-bound inspections refuses its shape).
/// That is no answer about the container, so it is read again or waited on,
/// never stopped on.
fn inspection_grant_refused(error: &HostRuntimeError) -> bool {
    matches!(
        error,
        HostRuntimeError::Controller(crate::client::ClientError::Controller(refusal))
            if refusal.request_invalid()
    )
}

/// The Controller or the helper refused this start's authority before any
/// effect.
fn refused_authority(error: &HostRuntimeError) -> bool {
    refused_before_effect(error)
        || matches!(error, HostRuntimeError::Controller(_)) && !temporary_observation_error(error)
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    /// Settle a Start that answered `error` by what this start's container is
    /// doing: `Ok` when it runs, so the start goes on as if Start answered,
    /// otherwise the result that ends the start. `arguments` are the start's
    /// exact inspection arguments, `run_generation` its generation.
    pub(super) async fn reobserve_failed_start(
        &self,
        claim: &AgentClaim,
        run_id: &str,
        run_generation: u64,
        arguments: &[String],
        error: &HostRuntimeError,
    ) -> Result<(), Box<ExecutionResult>> {
        let foreign_name = matches!(
            error,
            HostRuntimeError::HelperRejected { code, .. }
                if *code == HelperErrorCode::OperationInvalidArtifact
        );
        if foreign_name || refused_authority(error) {
            return Err(Box::new(
                match self.stop_start_run(claim, run_id, false).await {
                    Err(uncertain) => uncertain,
                    Ok(()) => runtime_failure(START_FAILED, error),
                },
            ));
        }
        self.reobserve_start(claim, run_id, run_generation, arguments, error)
            .await
    }

    /// Read this start's container again through the grant-free inspection
    /// bound to its generation, and go on by what is there.
    async fn reobserve_start(
        &self,
        claim: &AgentClaim,
        run_id: &str,
        run_generation: u64,
        arguments: &[String],
        error: &HostRuntimeError,
    ) -> Result<(), Box<ExecutionResult>> {
        let request_root = self.runtime_root.join("runtime-requests");
        let inspection = HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: self.helper_socket,
        };
        let helper_said = |error: &HostRuntimeError, said: HelperErrorCode| matches!(error, HostRuntimeError::HelperRejected { code, .. } if *code == said);
        let ended = match inspection
            .inspect_start_run_report(arguments.to_vec(), run_generation)
            .await
        {
            Ok(report) if report.running => return Ok(()),
            // Its process exited: the exit and its output are the result,
            // read before the exact stop frees the name for the retry.
            Err(exited) if helper_said(&exited, HelperErrorCode::RuntimeProcessExited) => {
                match self.stop_start_run(claim, run_id, false).await {
                    Err(uncertain) => uncertain,
                    Ok(()) => runtime_observation_failure(&exited),
                }
            }
            // Proven absent: nothing to stop. The Start's own error says why,
            // as it did after a stop.
            Err(absent) if helper_said(&absent, HelperErrorCode::RuntimeRunMissing) => {
                runtime_failure(START_FAILED, error)
            }
            // The inspection answered neither running nor an exit.
            Ok(_) => unconfirmed(
                WaitReason::RuntimeEffectUnconfirmed,
                START_FAILED,
                host_runtime_evidence(FailureStage::Unknown, error),
            ),
            // Unread: nothing is stopped on what was not seen. The retry's
            // Start adopts a running container of this start.
            Err(unread) if temporary_observation_error(&unread) => temporary_preparation_failure(
                FailureStage::Unknown,
                "start_reobserve",
                &unread.preflight_code(),
            ),
            Err(unread) => runtime_observation_failure(&unread),
        };
        Err(Box::new(ended))
    }

    /// A retained start (an earlier agent may have launched it) or a
    /// collective readiness phase whose granted inspection answered `error`:
    /// the result that ends it. Another generation's container under the name
    /// (the helper refuses it) is healed by the exact stop, which removes one
    /// of this order's and refuses any other; an unread inspection or a
    /// refused inspection grant waits; anything else is reported.
    pub(super) async fn inspected_start_failed(
        &self,
        claim: &AgentClaim,
        run_id: &str,
        retained: bool,
        error: &HostRuntimeError,
    ) -> ExecutionResult {
        if retained {
            if matches!(
                error,
                HostRuntimeError::HelperRejected { code, .. }
                    if *code == HelperErrorCode::OperationInvalidArtifact
            ) {
                return match self.heal_retained_run(claim, run_id).await {
                    Ok(()) => retained_container_removed(run_id),
                    Err(result) => result,
                };
            }
            if temporary_observation_error(error) || inspection_grant_refused(error) {
                return temporary_preparation_failure(
                    FailureStage::RetainedContainer,
                    "retained_inspect",
                    &error.preflight_code(),
                );
            }
            return runtime_observation_failure(error);
        }
        if inspection_grant_refused(error) {
            return temporary_preparation_failure(
                FailureStage::Unknown,
                "collective_inspect",
                &error.preflight_code(),
            );
        }
        runtime_failure(
            "collective workload is not running with exact identity",
            error,
        )
    }

    /// A launched rank's granted inspection, bound to its generation: `Ok`
    /// when its container runs. An error is read again as a Start's error is,
    /// so a lost reply stops nothing; a refused inspection grant is read again
    /// without a stop; a cancellation stops the rank.
    pub(super) async fn inspect_launched_rank(
        &self,
        claim: &AgentClaim,
        run_id: &str,
        run_generation: u64,
        arguments: &[String],
        cancellation: &tokio::sync::watch::Receiver<bool>,
    ) -> Result<(), Box<ExecutionResult>> {
        let Err(error) = self
            .inspect_start_run(claim, arguments.to_vec(), run_generation)
            .await
        else {
            return Ok(());
        };
        if *cancellation.borrow() {
            return Err(Box::new(self.cancel_start_run(claim, run_id).await));
        }
        if inspection_grant_refused(&error) {
            return self
                .reobserve_start(claim, run_id, run_generation, arguments, &error)
                .await;
        }
        self.reobserve_failed_start(claim, run_id, run_generation, arguments, &error)
            .await
    }
}

#[cfg(test)]
mod tests;
