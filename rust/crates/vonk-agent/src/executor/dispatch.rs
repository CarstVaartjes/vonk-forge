//! Dispatch.

use super::*;

#[async_trait(?Send)]
impl<R: ProcessRunner> Executor for RecipeExecutor<'_, R> {
    async fn observe(
        &self,
        claim: &AgentClaim,
    ) -> Option<operation_observation::OperationObservation> {
        Some(self.observe_operation(claim, None).await)
    }
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        if claim.operation == AgentOperation::ArtifactDistributionV1 {
            return self
                .execute_distribution(claim, lease_deadline, cancellation)
                .await;
        }
        let request = match RecipeOperationRequest::parse(claim) {
            Ok(request) => request,
            Err(_) => return failed("recipe operation payload is invalid"),
        };
        // The one place a compiled plan is validated; parse stays structural so
        // journal re-observation survives upgrades that tighten the rules.
        if let Some(refusal) = refuse_invalid_plan(&request) {
            return refusal;
        }
        match request {
            RecipeOperationRequest::RuntimePreflight(request) => {
                self.execute_runtime_preflight(claim, cancellation, request)
                    .await
            }
            RecipeOperationRequest::BuildCleanup(request) => {
                self.execute_build_cleanup(request).await
            }
            RecipeOperationRequest::Build(request) => {
                self.execute_build(claim, cancellation, request).await
            }
            RecipeOperationRequest::JobRun(request) => {
                self.execute_job_run(claim, lease_deadline, cancellation, request)
                    .await
            }
            RecipeOperationRequest::Install(request) => {
                self.execute_install(claim, lease_deadline, cancellation, request)
                    .await
            }
            RecipeOperationRequest::Reconcile(request) => {
                self.execute_reconcile(claim, cancellation, request).await
            }
            RecipeOperationRequest::Start(request) => {
                self.execute_start(claim, lease_deadline, cancellation, request)
                    .await
            }
            RecipeOperationRequest::Stop(request) => {
                self.execute_stop(claim, cancellation, request).await
            }
            RecipeOperationRequest::Uninstall(request) => {
                self.execute_uninstall(claim, cancellation, request).await
            }
        }
    }
}

#[cfg(test)]
#[async_trait(?Send)]
impl Executor for RejectingExecutor {
    async fn observe(
        &self,
        _claim: &AgentClaim,
    ) -> Option<crate::executor::operation_observation::OperationObservation> {
        None
    }

    async fn execute(
        &self,
        claim: &AgentClaim,
        _lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        _cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        let _ = claim;
        failed("operation is not enabled by this agent build")
    }
}

#[async_trait(?Send)]
impl<R: ProcessRunner> Executor for ControlExecutor<'_, R> {
    async fn observe(
        &self,
        claim: &AgentClaim,
    ) -> Option<operation_observation::OperationObservation> {
        Some(
            self.recipes
                .observe_operation(
                    claim,
                    (claim.operation == AgentOperation::AgentUpgradeV1).then(|| {
                        super::upgrade_observation::UpgradeObservation {
                            node_id: self.upgrades.client.node_id(),
                            identity: self.runtime_identity,
                            receipt: crate::package_activation::read_receipt(
                                self.activation_receipt,
                            ),
                        }
                    }),
                )
                .await,
        )
    }
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        if claim.operation == AgentOperation::AgentUpgradeV1 {
            return upgrade_outcome(self.upgrades.execute(claim).await);
        }

        self.recipes
            .execute(claim, lease_deadline, cancellation)
            .await
    }
}

pub(crate) fn upgrade_outcome(
    result: Result<(), crate::agent_upgrade::AgentUpgradeError>,
) -> ExecutionResult {
    match result {
        Ok(()) => ExecutionResult::unknown(
            WaitReason::AgentUpgradeAwaitingIdentity,
            crate::agent_upgrade::UPGRADE_AWAITING_IDENTITY_REASON,
            UnknownEvidence::at(FailureStage::AgentUpgradeInstalled)
                .because("the package is installed; the new agent's identity is not yet confirmed"),
        ),
        Err(error) if !error.definite() => ExecutionResult::unknown(
            WaitReason::RuntimeEffectUnconfirmed,
            error.to_string(),
            UnknownEvidence::at(FailureStage::Unknown),
        ),
        Err(error) => {
            // The reason is the typed refusal; the helper's own words are the
            // captured detail beside it, never the reason.
            let mut failure = Failure::new(error.to_string())
                .process_logs(error.diagnostic().map(crate::failure_evidence::detail_logs));
            if let Some((code, exit_code)) = error.helper_diagnostics() {
                failure = failure.helper(code, exit_code.and_then(|code| u32::try_from(code).ok()));
            }
            ExecutionResult::Failed(failure)
        }
    }
}

#[cfg(test)]
mod tests;
