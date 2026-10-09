//! Stop.

use super::*;

impl<R> RecipeExecutor<'_, R> {
    // The error is the finished result the caller returns as it is; it is moved
    // once, never copied, so its size does not matter here.
    #[allow(clippy::result_large_err)]
    pub(super) async fn stop_start_run(
        &self,
        claim: &AgentClaim,
        run_id: &str,
        stop_timeout_seconds: u32,
        cancel_pending_start: bool,
    ) -> Result<(), ExecutionResult>
    where
        R: ProcessRunner,
    {
        self.stop_run_exact(claim, run_id, stop_timeout_seconds, cancel_pending_start)
            .await
            .map_err(|stall| stall.result)
    }
}

impl<R> RecipeExecutor<'_, R> {
    /// Stop the run of this order and clear its retained record.
    ///
    /// The helper removes a container only when its labels equal this order's
    /// whole identity (managed, run, target, installation, generation, plan
    /// digest); a container that differs is refused, never touched, and that
    /// refusal is reported as `identity_refused`.
    #[allow(clippy::result_large_err)]
    pub(super) async fn stop_run_exact(
        &self,
        claim: &AgentClaim,
        run_id: &str,
        stop_timeout_seconds: u32,
        cancel_pending_start: bool,
    ) -> Result<(), StopStall>
    where
        R: ProcessRunner,
    {
        let Some(stop_plan) = exact_stop_plan_from_claim(claim, run_id, cancel_pending_start)
        else {
            return Err(StopStall::unproven(unconfirmed(
                WaitReason::StopUnconfirmed,
                "workload stop remains unconfirmed",
                UnknownEvidence::at(FailureStage::StopPlan)
                    .because("no exact stop plan could be derived from the claim"),
            )));
        };
        if stop_plan.stop_timeout_seconds != stop_timeout_seconds {
            return Err(StopStall::unproven(unconfirmed(
                WaitReason::StopUnconfirmed,
                "workload stop remains unconfirmed",
                UnknownEvidence::at(FailureStage::StopPlan)
                    .because("the stop timeout differs from the authorized plan"),
            )));
        }
        if let Err(error) = self
            .execute_host_runtime_plan(claim, Vec::new(), HostRuntimePlan::Stop(stop_plan))
            .await
        {
            return Err(StopStall {
                identity_refused: matches!(
                    &error,
                    crate::host_runtime::HostRuntimeError::HelperRejected { code, .. }
                        if *code == HelperErrorCode::OperationInvalidArtifact
                ),
                result: unconfirmed(
                    WaitReason::StopUnconfirmed,
                    "workload stop remains unconfirmed",
                    host_runtime_evidence(FailureStage::Stop, &error),
                ),
            });
        }
        if let Err(error) = self.runtime.complete_stop(run_id) {
            return Err(StopStall::unproven(unconfirmed(
                WaitReason::CleanupUnconfirmed,
                "workload local cleanup remains unconfirmed",
                UnknownEvidence::at(FailureStage::StopCleanup).because(error.safe_category()),
            )));
        }
        Ok(())
    }
}

impl<R> RecipeExecutor<'_, R> {
    /// A retained run that is not exactly this start's: remove what is proven
    /// this order's and let the start go on, or refuse what is not.
    ///
    /// The exact stop is the proof: it removes a container only when every
    /// identity label matches the authorized order. A container that does not
    /// match is another party's (or another generation's, which the Controller
    /// stops through its own recovery) and is left untouched.
    #[allow(clippy::result_large_err)]
    pub(super) async fn heal_retained_run(
        &self,
        claim: &AgentClaim,
        run_id: &str,
        stop_timeout_seconds: u32,
    ) -> Result<(), ExecutionResult>
    where
        R: ProcessRunner,
    {
        match self
            .stop_run_exact(claim, run_id, stop_timeout_seconds, false)
            .await
        {
            Ok(()) => Ok(()),
            Err(stall) if stall.identity_refused => Err(retained_container_foreign(run_id)),
            Err(stall) => Err(stall.result),
        }
    }
}

impl<R> RecipeExecutor<'_, R> {
    /// Finish the model-custody ACL transition of a start.
    ///
    /// The step is a local verify-and-record that is safe to repeat, so a
    /// transient storage error is retried a few times before the caller reports
    /// anything. Only a persistent failure is left for the Controller to observe.
    pub(super) async fn settle_acl_transition(
        &self,
        installation_id: &str,
        transition: &crate::oci::InstallationAclTransition,
    ) -> Result<(), OciError>
    where
        R: ProcessRunner,
    {
        let mut attempt = 0_u32;
        loop {
            match self
                .runtime
                .finish_installation_acl_transition(installation_id, transition)
            {
                Err(_) if attempt < ACL_SETTLE_RETRIES => {
                    attempt += 1;
                    tokio::time::sleep(Duration::from_millis(100 * u64::from(attempt))).await;
                }
                other => return other,
            }
        }
    }
}

impl<R> RecipeExecutor<'_, R> {
    pub(super) async fn cancel_start_run(
        &self,
        claim: &AgentClaim,
        run_id: &str,
        stop_timeout_seconds: u32,
    ) -> ExecutionResult
    where
        R: ProcessRunner,
    {
        if let Err(uncertain) = self
            .stop_start_run(claim, run_id, stop_timeout_seconds, true)
            .await
        {
            return uncertain;
        }
        ExecutionResult::cancelled("controller cancellation confirmed after exact workload stop")
    }
}

impl<R: ProcessRunner> RecipeExecutor<'_, R> {
    pub(super) async fn execute_stop(
        &self,
        claim: &AgentClaim,
        cancellation: tokio::sync::watch::Receiver<bool>,
        request: vonk_agent_protocol::RecipeStopRequest,
    ) -> ExecutionResult {
        self.report_phase(claim, ProgressPhase::Stopping).await;
        let run_id = request.run_id.to_string();
        // The signed request already binds the exact runtime target. Local
        // history is a projection, never an additional stop admission gate.
        if let Err(error) = self
            .execute_host_runtime_plan(claim, Vec::new(), HostRuntimePlan::Stop(request.clone()))
            .await
        {
            unconfirmed(
                WaitReason::StopUnconfirmed,
                "container runtime stop remains unconfirmed",
                host_runtime_evidence(FailureStage::Stop, &error),
            )
        } else {
            if let Err(error) = self.runtime.complete_stop(&run_id) {
                return unconfirmed(
                    WaitReason::StopMetadataUnconfirmed,
                    "container runtime stop metadata remains unconfirmed",
                    UnknownEvidence::at(FailureStage::StopMetadata).because(error.safe_category()),
                );
            }
            if *cancellation.borrow() {
                ExecutionResult::cancelled(
                    "controller cancellation confirmed after exact workload stop",
                )
            } else {
                ExecutionResult::done(RecipeStopResult::default())
            }
        }
    }
}

#[cfg(test)]
mod tests;
