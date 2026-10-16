//! A Start that answered an error.
//!
//! The error says the Start did not report success, not what the container
//! did: a lost or unreadable reply, or a launch whose answer the helper could
//! not read, can leave this start's workload running. Before anything stops
//! it, the exact container is read again through the grant-free inspection,
//! and the start goes on by what is there: a running container is this start's
//! and goes on to readiness; an exited one reports its own exit; proven absence
//! leaves nothing to stop; an unread inspection stops nothing and waits.
//!
//! Two answers keep the exact stop they had. A container under the name that
//! is not this start's (the helper already read it): the stop removes one of
//! this order's and refuses any other. And a refused authority: the start is
//! not this agent's to adopt.

use super::*;
use crate::host_runtime::HostRuntimeError;

const START_FAILED: &str = "container runtime could not start the workload";

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
    /// exact inspection arguments.
    pub(super) async fn reobserve_failed_start(
        &self,
        claim: &AgentClaim,
        run_id: &str,
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
        let request_root = self.runtime_root.join("runtime-requests");
        let inspection = HostRuntimeBoundary {
            client: self.client,
            request_root: &request_root,
            helper_socket: self.helper_socket,
        };
        let helper_said = |error: &HostRuntimeError, said: HelperErrorCode| matches!(error, HostRuntimeError::HelperRejected { code, .. } if *code == said);
        let ended = match inspection
            .inspect_recipe_run_report(arguments.to_vec(), true)
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
}

#[cfg(test)]
mod tests;
