//! Dispatch.

use super::*;

#[async_trait(?Send)]
impl<R: ProcessRunner> Executor for RecipeExecutor<'_, R> {
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        if claim.operation == AgentOperation::ArtifactDistributionV1 {
            return self.execute_distribution(claim).await;
        }
        let request = match RecipeOperationRequest::parse(claim) {
            Ok(request) => request,
            Err(_) => return failed("recipe operation payload is invalid"),
        };
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
                self.execute_job_run(claim, cancellation, request).await
            }
            RecipeOperationRequest::Install(request) => {
                self.execute_install(claim, cancellation, request).await
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
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        if claim.operation == AgentOperation::AgentUpgradeV1 {
            return match self.upgrades.execute(claim).await {
                Ok(()) => ExecutionResult::unknown(
                    WaitReason::AgentUpgradeAwaitingIdentity,
                    crate::agent_upgrade::UPGRADE_AWAITING_IDENTITY_REASON,
                    UnknownEvidence::at(FailureStage::AgentUpgradeInstalled).because(
                        "the package is installed; the new agent's identity is not yet confirmed",
                    ),
                ),
                Err(error) => {
                    let reason = match error.diagnostic() {
                        Some(detail) if !detail.is_empty() => format!("{error}: {detail}"),
                        _ => error.to_string(),
                    };
                    let mut failure = Failure::new(reason)
                        .process_logs(error.diagnostic().map(crate::failure_evidence::detail_logs));
                    if let Some((code, exit_code)) = error.helper_diagnostics() {
                        failure = failure
                            .helper(code, exit_code.and_then(|code| u32::try_from(code).ok()));
                    }
                    if error.helper_diagnostics().is_some_and(|(code, _)| {
                        code == vonk_agent_protocol::generated::HelperErrorCode::PackagePreparationUnavailable
                    }) {
                        failure = failure.kind(AgentFailureKind::TemporaryDependency).retry_after(Some(2));
                    }
                    ExecutionResult::Failed(failure)
                }
            };
        }
        self.recipes
            .execute(claim, lease_deadline, cancellation)
            .await
    }
}
