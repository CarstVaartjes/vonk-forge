//! Dispatch.

use super::*;

#[async_trait(?Send)]
impl<R: ProcessRunner> Executor for RecipeExecutor<'_, R> {
    async fn execute(
        &self,
        claim: &AgentClaim,
        lease_deadline: tokio::sync::watch::Receiver<DateTime<FixedOffset>>,
        mut cancellation: tokio::sync::watch::Receiver<bool>,
    ) -> ExecutionResult {
        if claim.operation == AgentOperation::ArtifactDistributionV1 {
            // Dropping a transfer preserves completed objects and partials.
            // A helper image pull may still settle: report uncertainty rather
            // than claiming cancellation proved all external effects stopped.
            return tokio::select! {
                biased;
                _ = cancellation.wait_for(|requested| *requested) => ExecutionResult::unknown(
                    WaitReason::RuntimeEffectUnconfirmed,
                    "superseded distribution effects await observation",
                    UnknownEvidence::at(FailureStage::ArtifactDistribution),
                ),
                result = self.execute_distribution(claim) => result,
            };
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
                    ExecutionResult::Failed(failure)
                }
            };
        }
        self.recipes
            .execute(claim, lease_deadline, cancellation)
            .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::executor::test_support::{NoProcess, claim};

    #[tokio::test]
    async fn superseded_distribution_yields_without_network_or_host_effects() {
        let data = tempfile::tempdir().unwrap();
        let runtime = tempfile::tempdir().unwrap();
        let client = AgentHttpClient::for_http_test(
            "http://127.0.0.1:1/",
            "spk_0123456789abcdef0123456789abcdef",
        );
        let executor = RecipeExecutor {
            client: &client,
            runtime_root: runtime.path(),
            runtime: OciRuntime {
                runner: &NoProcess,
                data_root: data.path(),
            },
        };
        let mut obsolete = claim();
        obsolete.operation = AgentOperation::ArtifactDistributionV1;
        obsolete.payload =
            vonk_agent_protocol::generated::AgentClaimPayload::ArtifactDistributionPayload(
                vonk_agent_protocol::generated::ArtifactDistributionPayload {
                    plan_digest: "a".repeat(64),
                },
            );
        let (_, deadline) = tokio::sync::watch::channel(obsolete.deadline);
        let (cancel, cancellation) = tokio::sync::watch::channel(true);
        let result = tokio::time::timeout(
            Duration::from_secs(1),
            executor.execute(&obsolete, deadline, cancellation),
        )
        .await
        .unwrap();
        assert!(matches!(result, ExecutionResult::Unknown(_)));
        assert!(std::fs::read_dir(data.path()).unwrap().next().is_none());
        // Cancellation ownership is per request; ending an obsolete transfer
        // leaves a new operation free to execute on the normal dispatch path.
        drop(cancel);
        let current = claim();
        let (_, deadline) = tokio::sync::watch::channel(current.deadline);
        let (_cancel, cancellation) = tokio::sync::watch::channel(false);
        let result = tokio::time::timeout(
            Duration::from_secs(1),
            executor.execute(&current, deadline, cancellation),
        )
        .await
        .unwrap();
        assert!(matches!(result, ExecutionResult::Failed(_)));
    }
}
